"""Minimal HTTPS client on urllib, shared by the CM and Prometheus clients."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

TIMEOUT_S = 30


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TlsError(HttpError):
    """The server certificate is not trusted (unknown CA or hostname mismatch)."""


@dataclass(frozen=True)
class Tls:
    ca_cert: str | None = None
    insecure: bool = False

    def context(self) -> ssl.SSLContext:
        if self.insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return ssl.create_default_context(cafile=self.ca_cert)


def get(url: str, tls: Tls, headers: dict[str, str] | None = None, timeout: float = TIMEOUT_S) -> bytes:
    return request("GET", url, tls, headers, timeout=timeout)


def request(method: str, url: str, tls: Tls, headers: dict[str, str] | None = None, body: Any = None,
            timeout: float = TIMEOUT_S) -> bytes:
    """One HTTP call straight to the cluster host; body (if any) is sent as JSON.

    Proxies from the environment or macOS settings are ignored: a corporate proxy can't reach cluster hosts, and for
    plain http it answers itself (e.g. 400) instead of the server."""
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=tls.context()))
    try:
        with opener.open(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        text = " ".join(e.read(300).decode(errors="replace").split())
        raise HttpError(f"HTTP {e.code} {e.reason}" + (f": {text}" if text else ""), e.code) from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            raise TlsError(f"TLS certificate not trusted: {e.reason.verify_message}") from None
        raise HttpError(f"cannot connect: {e.reason}") from None
    except OSError as e:  # timeouts, resets
        raise HttpError(f"cannot connect: {e}") from None
