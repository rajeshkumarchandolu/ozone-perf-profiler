"""Cloudera Manager REST API client. GET only: the tool never changes CM state."""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from . import httpclient
from .httpclient import HttpError, Tls, TlsError


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

    def _service_path(self, cluster: CmCluster, service: CmService) -> str:
        return f"{self._api}/clusters/{_q(cluster.name)}/services/{_q(service.name)}"

    def _get_json(self, path: str, name: str) -> dict[str, Any]:
        body = self._get(path, name)
        try:
            return json.loads(body)
        except ValueError:
            raise CmError(f"GET {path} did not return JSON: {body[:120]!r}") from None

    def _get(self, path: str, name: str) -> str:
        try:
            body = httpclient.get(self.base_url + path, self._tls, self._headers).decode()
        except TlsError as e:
            raise CmTlsError(str(e)) from None
        except HttpError as e:
            if e.status == 401:
                raise CmError("login failed (HTTP 401), check the CM user and password") from None
            raise CmError(f"GET {path}: {e}") from None
        self.raw[name] = body
        return body
