"""Cloudera Manager REST API client.

Read methods (clusters, services, roles, hosts, config) are all that ozone_perf_profiler uses, for discovery. The
write methods (update_service_config, service_command, wait_for_command) are for use cases that set up the cluster.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import http as httpclient
from .http import HttpError, Tls, TlsError

COMMAND_POLL_S = 15


class CmError(Exception):
    """CM could not be reached, refused the login or answered unexpectedly."""


class CmTlsError(CmError):
    pass


@dataclass(frozen=True)
class CmCluster:
    name: str
    display_name: str
    version: str


@dataclass(frozen=True)
class CmService:
    name: str
    display_name: str
    state: str
    health: str


@dataclass(frozen=True)
class CmParam:
    name: str
    value: str | None    # set explicitly in CM; None when left at the default
    default: str | None

    @property
    def effective(self) -> str | None:
        return self.value if self.value is not None else self.default


def _same(got: str | None, want: str) -> bool:
    """Equal as text, or as numbers (CM may store 1 for 1.0)."""
    if got is None:
        return False
    if got.strip().lower() == want.strip().lower():
        return True
    try:
        return float(got) == float(want)
    except ValueError:
        return False


def normalize_url(raw: str) -> str:
    """Accepts any CM page URL (e.g. .../cmf/home) and returns scheme://host:port."""
    raw = raw.strip()
    parts = urllib.parse.urlsplit(raw if "://" in raw else "https://" + raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an http(s) URL: {raw!r}")
    return f"{parts.scheme}://{parts.netloc}"


def probe(base_url: str, tls: Tls) -> None:
    """Raises unless something HTTP answers at base_url. 401 still counts as reachable."""
    try:
        httpclient.get(base_url + "/api/version", tls)
    except TlsError as e:
        raise CmTlsError(str(e)) from None
    except HttpError as e:
        if e.status is None:
            raise CmError(str(e)) from None


def _q(name: str) -> str:
    return urllib.parse.quote(name, safe="")


class CmClient:
    def __init__(self, base_url: str, tls: Tls, user: str, password: str):
        self.base_url = base_url
        self.user = user
        self.api_version = ""
        self._tls = tls
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._headers = {"Authorization": "Basic " + token}
        self.raw: dict[str, str] = {}  # response bodies by name, saved with the run for later reference

    @property
    def _api(self) -> str:
        return f"/api/{self.api_version}"

    def login(self) -> str:
        """Authenticates and returns the API version, e.g. 'v54'."""
        version = self._get("/api/version", "version").strip()
        if not re.fullmatch(r"v\d+", version):
            raise CmError(f"unexpected /api/version answer: {version[:80]!r}")
        self.api_version = version
        return version

    def clusters(self) -> list[CmCluster]:
        items = self._get_json(f"{self._api}/clusters", "clusters")["items"]
        return [CmCluster(c["name"], c.get("displayName", c["name"]), c.get("fullVersion", "?")) for c in items]

    def ozone_services(self, cluster: CmCluster) -> list[CmService]:
        items = self._get_json(f"{self._api}/clusters/{_q(cluster.name)}/services", "services")["items"]
        return [CmService(s["name"], s.get("displayName", s["name"]), s.get("serviceState", "?"),
                          s.get("healthSummary", "?")) for s in items if s["type"] == "OZONE"]

    def roles(self, cluster: CmCluster, service: CmService) -> list[dict[str, Any]]:
        return self._get_json(f"{self._service_path(cluster, service)}/roles", "roles")["items"]

    def hostnames(self) -> dict[str, str]:
        """hostId -> hostname."""
        items = self._get_json(f"{self._api}/hosts", "hosts")["items"]
        return {h["hostId"]: h["hostname"] for h in items}

    def role_config(self, cluster: CmCluster, service: CmService, role_name: str) -> list[dict[str, Any]]:
        """Full view: includes defaults, so ports are present even when never changed."""
        path = f"{self._service_path(cluster, service)}/roles/{_q(role_name)}/config?view=full"
        return self._get_json(path, f"role-config-{role_name}").get("items", [])

    def service_config(self, cluster: CmCluster, service: CmService) -> dict[str, CmParam]:
        """Service-level parameters by name, with the value set in CM and the default."""
        items = self._get_json(f"{self._service_path(cluster, service)}/config?view=full", "service-config")["items"]
        return {i["name"]: CmParam(i["name"], i.get("value"), i.get("default")) for i in items}

    # --- writes (use cases only) ---

    def update_service_config(self, cluster: CmCluster, service: CmService, values: dict[str, str]) -> None:
        body = {"items": [{"name": k, "value": v} for k, v in values.items()]}
        self._call("PUT", f"{self._service_path(cluster, service)}/config", body)

    def service_command(self, cluster: CmCluster, service: CmService, command: str) -> int:
        """Starts a service command (restart, deployClientConfig, ...) and returns its command id."""
        answer = self._call("POST", f"{self._service_path(cluster, service)}/commands/{command}", {})
        return int(answer["id"])

    def wait_for_command(self, command_id: int, timeout_s: float) -> dict[str, Any]:
        """Polls a command until it ends; raises CmError unless it succeeded."""
        deadline = time.monotonic() + timeout_s
        while True:
            cmd = self._call("GET", f"{self._api}/commands/{command_id}")
            if not cmd.get("active", True):
                if not cmd.get("success"):
                    raise CmError(f"CM command {cmd.get('name', command_id)} failed: {cmd.get('resultMessage', '?')}")
                return cmd
            if time.monotonic() > deadline:
                raise CmError(f"CM command {cmd.get('name', command_id)} still running after {timeout_s:.0f}s")
            time.sleep(COMMAND_POLL_S)

    def apply_service_config(self, cluster: CmCluster, service: CmService, values: dict[str, str],
                             timeout_s: float = 1800, say: Callable[[str], None] = print) -> bool:
        """Sets the values, restarts the service, redeploys client configs and waits until every role runs, isn't
        BAD and is FRESH: CM's own proof that every process (and every gateway's client config) has the new values.
        Skips the restart only when CM already has the values and no role is stale. Returns True if it restarted."""
        current = self.service_config(cluster, service)
        if unknown := [k for k in values if k not in current]:
            raise CmError(f"not parameters of {service.name}: {', '.join(unknown)}")
        changes = {k: v for k, v in values.items() if not _same(current[k].effective, v)}
        stale = [r["name"] for r in self.roles(cluster, service) if r.get("configStalenessStatus") != "FRESH"]
        if not changes and not stale:
            say(f"CM already has {values} and every role is FRESH: no restart")
            return False
        if changes:
            say(f"setting {changes}, then restarting {service.name}")
            self.update_service_config(cluster, service, changes)
        else:
            say(f"CM already has {values}, but {len(stale)} role(s) are stale: restarting {service.name}")
        self.wait_for_command(self.service_command(cluster, service, "restart"), timeout_s)
        self.wait_for_command(self.service_command(cluster, service, "deployClientConfig"), timeout_s)
        self.wait_for_roles(cluster, service, timeout_s, say)
        return True

    def wait_for_roles(self, cluster: CmCluster, service: CmService, timeout_s: float,
                       say: Callable[[str], None] = print) -> None:
        deadline, last = time.monotonic() + timeout_s, ""
        while True:
            waiting = [f"{r['name']} {r.get('roleState')}/{r.get('healthSummary')}/{r.get('configStalenessStatus')}"
                       for r in self.roles(cluster, service)
                       if (r["type"] != "GATEWAY" and r.get("roleState") != "STARTED")
                       or r.get("healthSummary") == "BAD" or r.get("configStalenessStatus") != "FRESH"]
            if not waiting:
                return
            if time.monotonic() > deadline:
                raise CmError(f"roles not ready after {timeout_s:.0f}s: {', '.join(waiting[:5])}")
            if (now := f"{len(waiting)} role(s) not ready yet, e.g. {waiting[0]}") != last:
                say(now)
                last = now
            time.sleep(COMMAND_POLL_S)

    def _service_path(self, cluster: CmCluster, service: CmService) -> str:
        return f"{self._api}/clusters/{_q(cluster.name)}/services/{_q(service.name)}"

    def _get_json(self, path: str, name: str) -> dict[str, Any]:
        body = self._get(path, name)
        try:
            return json.loads(body)
        except ValueError:
            raise CmError(f"GET {path} did not return JSON: {body[:120]!r}") from None

    def _get(self, path: str, name: str) -> str:
        body = self._raw("GET", path).decode()
        self.raw[name] = body
        return body

    def _call(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        text = self._raw(method, path, body).decode()
        try:
            return json.loads(text) if text.strip() else {}
        except ValueError:
            raise CmError(f"{method} {path} did not return JSON: {text[:120]!r}") from None

    def _raw(self, method: str, path: str, body: Any = None) -> bytes:
        try:
            return httpclient.request(method, self.base_url + path, self._tls, self._headers, body)
        except TlsError as e:
            raise CmTlsError(str(e)) from None
        except HttpError as e:
            if e.status == 401:
                raise CmError("login failed (HTTP 401), check the CM user and password") from None
            if e.status == 403:
                raise CmError(f"{method} {path}: HTTP 403, the CM user lacks the rights for this") from None
            raise CmError(f"{method} {path}: {e}") from None
