"""Every option of every phase, declared once. cli.py turns each field into --field-name.

All values are optional: whatever is missing is asked for. Values stay strings until a phase validates them, so a flag
goes through exactly the same check as a typed answer. Secrets only come from the environment or a hidden prompt.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CM_PWD_ENV = "CM_PWD"
SSH_PWD_ENV = "SSH_PWD"
DEFAULT_OUT_DIR = Path(".output")


def _opt(help_text: str, default: object = None) -> Any:
    return dataclasses.field(default=default, metadata={"help": help_text})


@dataclass(frozen=True)
class Args:
    """Every value is optional: what is missing is asked for. Secrets only via $CM_PWD / $SSH_PWD or a prompt."""
    # Cluster discovery
    cm_url: str | None = _opt("any Cloudera Manager URL, e.g. https://cm-host:7183/cmf/home")
    cm_user: str | None = _opt("CM user (read access is enough for opp; use cases that change config need "
                               "write access)")
    cm_ca_cert: str | None = _opt("PEM CA bundle that signs the CM certificate")
    insecure: bool = _opt("skip TLS certificate verification", False)
    cluster: str | None = _opt("CM cluster name")
    service: str | None = _opt("CM Ozone service name")
    ssh_user: str | None = _opt("ssh user on the cluster hosts")
    freon_host: str | None = _opt("host that runs the Freon client")
    keytab_dir: str = _opt("folder on the Freon host whose *.keytab files are offered", "/cdep/keytabs")
    kerberos_keytab: str | None = _opt("keytab path on the Freon host (skips the keytab list)")
    kerberos_principal: str | None = _opt("principal in that keytab")
    # Freon ockg
    threads: str | None = _opt("Freon threads (default 10)")
    keys: str | None = _opt("keys to write (default 100000)")
    size: str | None = _opt("key size, e.g. 1KB, 4MB (default 1KB)")
    replication: str | None = _opt("RATIS/THREE, RATIS/ONE, EC/rs-3-2-1024k or EC/rs-6-3-1024k (default RATIS/THREE)")
    volume: str | None = _opt("Ozone volume (default opp-<run name>)")
    bucket: str | None = _opt("Ozone bucket (default freon)")
    freon_args: str | None = _opt("extra ockg arguments, appended as is, e.g. --freon-args='--om-service-id x'")
    runs: str | None = _opt("Freon runs in the series, each into its own bucket (default 3)")
    grace: str | None = _opt("wait between runs, e.g. 2m, 90s, or seconds (default 5m)")
    warmup_keys: str = _opt("keys of an unreported warm-up run before run 1; 0 skips it", "10")
    cleanup: bool = _opt("delete the Freon volume after the last run", False)
    # Profiling (one of JFR or async-profiler)
    profiler: str | None = _opt("profile the Ozone JVMs during the runs: none, jfr or async-profiler (otherwise asked)")
    profile_services: str | None = _opt("services to profile, comma-separated: om,scm,dn,recon,s3g (default om,scm,dn)")
    profile_period: str | None = _opt("recording period from the start of run 1, e.g. 10m, 600s (default 10m)")
    asprof_events: str | None = _opt("async-profiler events, passed to asprof -e (default cpu,alloc,lock,wall)")
    # Telemetry (Prometheus)
    prometheus_url: str | None = _opt("the Ozone Prometheus (default: the OZONE_PROMETHEUS role in CM)")
    # All phases
    out_dir: Path = _opt("where run folders go", DEFAULT_OUT_DIR)
    run_name: str | None = _opt("run folder and volume name (default: the Freon host's time, "
                                "e.g. 2026-nov-11-20h15m03s)")
    yes: bool = _opt("never prompt: every needed value must come from flags/env", False)
