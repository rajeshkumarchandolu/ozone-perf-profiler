"""Prometheus HTTP API client, read-only: build info, scrape targets, metric names, instant queries."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Any

from . import httpclient
from .httpclient import HttpError, Tls


class PromError(Exception):
    pass


@dataclass(frozen=True)
class Target:
    job: str
    instance: str
    health: str  # up / down / unknown


class NotAValue(PromError):
    """The query returned time series (a range vector) or text instead of one number per series."""


@dataclass(frozen=True)
class Sample:
    labels: dict[str, str]
    value: float  # may be NaN, e.g. 0/0 in a ratio


class PromClient:
    def __init__(self, base_url: str, tls: Tls):
        self.base_url = base_url.rstrip("/")
        self._tls = tls

    def version(self) -> str:
        return self._get("/api/v1/status/buildinfo").get("version", "?")

    def targets(self) -> list[Target]:
        active = self._get("/api/v1/targets", {"state": "active"}).get("activeTargets", [])
        return [Target(t.get("labels", {}).get("job", "?"), t.get("labels", {}).get("instance", "?"),
                       t.get("health", "unknown")) for t in active]

    def metric_names(self) -> list[str]:
        return list(self._get("/api/v1/label/__name__/values"))

    def query(self, expr: str, at: float | None = None) -> list[Sample]:
        """Instant query: one value per series at epoch `at` (Prometheus's now if None).
        A range vector or string result raises NotAValue."""
        data = self._get("/api/v1/query", {"query": expr} if at is None else {"query": expr, "time": at})
        kind, result = data.get("resultType"), data.get("result", [])
        if kind == "vector":
            return [Sample(r.get("metric", {}), float(r["value"][1])) for r in result]
        if kind == "scalar":
            return [Sample({}, float(result[1]))]
        raise NotAValue(f"returns a {'range vector (time series)' if kind == 'matrix' else kind}, not one value "
                        "per series: wrap [$window] in an *_over_time() or increase()")

    def now(self) -> float:
        """The Prometheus server's clock, as a Unix epoch."""
        return self.query("time()")[0].value

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        try:
            body = json.loads(httpclient.get(url, self._tls))
        except HttpError as e:
            raise PromError(f"GET {path}: {e}") from None
        except ValueError:
            raise PromError(f"GET {path}: not a Prometheus API answer (not JSON)") from None
        if body.get("status") != "success":
            raise PromError(f"GET {path}: {body.get('errorType', '?')}: {body.get('error', body)}")
        return body["data"]
