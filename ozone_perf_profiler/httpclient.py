"""Minimal HTTPS GET on urllib, shared by the CM and (later) Prometheus clients."""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

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
    """GET straight to the cluster host. Proxies from the environment or macOS settings are ignored: a corporate
    proxy can't reach cluster hosts, and for plain http it answers itself (e.g. 400) instead of the server."""
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=tls.context()))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        body = " ".join(e.read(300).decode(errors="replace").split())
        raise HttpError(f"HTTP {e.code} {e.reason}" + (f": {body}" if body else ""), e.code) from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            raise TlsError(f"TLS certificate not trusted: {e.reason.verify_message}") from None
        raise HttpError(f"cannot connect: {e.reason}") from None
    except OSError as e:  # timeouts, resets
        raise HttpError(f"cannot connect: {e}") from None
