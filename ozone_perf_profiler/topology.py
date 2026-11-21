"""The Ozone service layout as CM describes it. Pure: built from CM JSON, no I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from cmclient.cm import CmCluster, CmService


@dataclass(frozen=True)
class RoleSpec:
    short: str
    main_class: str | None  # JVM main class; None when the role runs no Ozone JVM


# CM role type -> spec. Order is the display order.
ROLE_SPECS: dict[str, RoleSpec] = {
    "OZONE_MANAGER": RoleSpec("om", "org.apache.hadoop.ozone.om.OzoneManagerStarter"),
    "STORAGE_CONTAINER_MANAGER": RoleSpec("scm", "org.apache.hadoop.hdds.scm.server.StorageContainerManagerStarter"),
    "OZONE_DATANODE": RoleSpec("dn", "org.apache.hadoop.ozone.HddsDatanodeService"),
    "OZONE_RECON": RoleSpec("recon", "org.apache.hadoop.ozone.recon.ReconServer"),
    "S3_GATEWAY": RoleSpec("s3g", "org.apache.hadoop.ozone.s3.Gateway"),
    "OZONE_PROMETHEUS": RoleSpec("prometheus", None),
    "GATEWAY": RoleSpec("gateway", None),
}
# Roles whose load the Freon client would disturb if it ran on the same host.
SERVER_TYPES = ("OZONE_MANAGER", "STORAGE_CONTAINER_MANAGER", "OZONE_DATANODE")


def spec(role_type: str) -> RoleSpec:
    return ROLE_SPECS.get(role_type) or RoleSpec(role_type.lower(), None)


@dataclass(frozen=True)
class Role:
    name: str
    type: str
    host: str
    state: str
    health: str
    config: str  # FRESH / STALE / STALE_REFRESHABLE

    @property
    def short(self) -> str:
        return spec(self.type).short

    @property
    def main_class(self) -> str | None:
        return spec(self.type).main_class


@dataclass(frozen=True)
class Topology:
    cm_url: str
    api_version: str
    cluster: CmCluster
    service: CmService
    roles: tuple[Role, ...]
    prometheus_url: str | None = None  # host:port candidate from CM; the telemetry check picks the scheme
    prometheus_source: str | None = None

    @property
    def hosts(self) -> list[str]:
        return sorted({r.host for r in self.roles})

    def of_type(self, *types: str) -> list[Role]:
        return [r for r in self.roles if r.type in types]

    @property
    def jvm_roles(self) -> list[Role]:
        return [r for r in self.roles if r.main_class]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.roles:
            out[r.short] = out.get(r.short, 0) + 1
        return out

    def problems(self) -> list[str]:
        return [f"{r.short} on {r.host}: {why}" for r in self.jvm_roles
                for why in ([f"state {r.state}"] if r.state != "STARTED" else [])
                + ([f"config {r.config}, restart it"] if r.config not in ("FRESH", "?") else [])]

    def freon_candidates(self) -> list[str]:
        """GATEWAY hosts, least disturbing first: fewest OM/SCM/DN roles on the same host."""
        hosts = {r.host for r in self.of_type("GATEWAY")}
        return sorted(hosts, key=lambda h: (len(self.servers_on(h)), h))

    def servers_on(self, host: str) -> list[str]:
        return sorted({r.short for r in self.of_type(*SERVER_TYPES) if r.host == host})


def build(cm_url: str, api_version: str, cluster: CmCluster, service: CmService,
          raw_roles: list[dict[str, Any]], hostnames: dict[str, str]) -> Topology:
    order = list(ROLE_SPECS)
    roles = sorted((_role(r, hostnames) for r in raw_roles),
                   key=lambda r: (order.index(r.type) if r.type in order else len(order), r.host))
    return Topology(cm_url, api_version, cluster, service, tuple(roles))


def _role(raw: dict[str, Any], hostnames: dict[str, str]) -> Role:
    ref = raw.get("hostRef", {})
    host = ref.get("hostname") or hostnames.get(ref.get("hostId", ""), ref.get("hostId", "?"))
    return Role(raw["name"], raw["type"], host, raw.get("roleState", "?"), raw.get("healthSummary", "?"),
                raw.get("configStalenessStatus", "?"))


def prometheus_endpoint(host: str, config_items: list[dict[str, Any]]) -> tuple[str, str] | None:
    """(host:port, CM config name) for the Ozone Prometheus role, from its listen port in the full role config.
    No scheme: CM doesn't say whether the port speaks TLS, so the telemetry check tries https, then http."""
    ports = [(i["name"], i.get("value") or i.get("default")) for i in config_items if "port" in i["name"].lower()]
    ports = [(n, str(v)) for n, v in ports if v and re.fullmatch(r"\d+", str(v))]
    # Prefer the server port over e.g. a node-exporter or debug port.
    ranked = sorted(ports, key=lambda p: ("prometheus" not in p[0].lower(), "server" not in p[0].lower(), p[0]))
    if not ranked:
        return None
    name, port = ranked[0]
    return f"{host}:{port}", name
