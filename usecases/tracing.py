"""Tracing overhead study: Freon once per configuration in CONFIGS, each set on the Ozone service through CM.

  python -m usecases.tracing <opp flags>

Edit CONFIGS to change the study. The flags are opp's (copy them from a rerun.sh; secrets from $CM_PWD / $SSH_PWD),
defaults: --runs 5, --warmup-keys 20000 (every config starts with freshly restarted JVMs), --profiler none.
Output: <out dir>/tracing-<time>/summary.xlsx: a comparison sheet ([min-max] per metric and config) and one sheet
per config. The cluster stays on the last configuration.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from cmclient.cm import CmError
from ozone_perf_profiler import cli, duration, probes
from ozone_perf_profiler.freon_output import FreonResult
from ozone_perf_profiler.options import SSH_PWD_ENV, Args
from ozone_perf_profiler.prompts import Abort, Prompter
from ozone_perf_profiler.remote import Remote, RemoteError, short

from .utils import (Cm, Config, campaign_folder, connect_cm, pause, run_freon, setup_cluster, stop_roles,
                    write_summary)

# Role types kept stopped during every config's runs (the service restart starts them again, so they are stopped
# after each one). Recon's OM delta sync (RocksDB getUpdatesSince on the OM leader) varied between configs.
STOPPED_ROLES = ["OZONE_RECON"]

# Max heap in MB, the same in every config ('ROLE_TYPE:param' keys go to that role type's config groups).
# S3G and HttpFS stay small: Freon uses neither, and ve1125 (125 GB) also hosts OM, SCM, DN, Recon and HDFS.
HEAPS = {
    "OZONE_MANAGER:om_max_heap_size": "31744",                      # 31 GB
    "STORAGE_CONTAINER_MANAGER:scm_max_heap_size": "31744",         # 31 GB
    "OZONE_DATANODE:ozone_datanode_heap_size": "31744",             # 31 GB
    "OZONE_RECON:ozone_recon_max_heap_size": "15360",               # 15 GB
    "S3_GATEWAY:ozone_S3_gateway_max_heap_size": "15360",            # 15 GB
    "HTTPFS_GATEWAY:ozone_httpfs_gateway_max_heap_size": "4096",    # 4 GB
}

# The same in every config: the OTLP endpoint (Jaeger, jager.service on ve1125) and the heaps.
COMMON = {
    "ozone.tracing.endpoint": "http://ve1125.halxg.cloudera.com:4317",
    **HEAPS,
}

CONFIGS = [
    Config("tracing-off", {"ozone.tracing.enabled": "false", **COMMON}),
    Config("tracing-on-s0", {"ozone.tracing.enabled": "true", "ozone.tracing.sampler": "0", **COMMON}),
    Config("tracing-on-s0.1", {"ozone.tracing.enabled": "true", "ozone.tracing.sampler": "0.1", **COMMON}),
    Config("tracing-on-s0.5", {"ozone.tracing.enabled": "true", "ozone.tracing.sampler": "0.5", **COMMON}),
    Config("tracing-on-s1.0", {"ozone.tracing.enabled": "true", "ozone.tracing.sampler": "1.0", **COMMON}),
]


SAFEMODE_TIMEOUT = "10m"
DECOMMISSION_TIMEOUT = "3h"   # SCM copies the datanode's containers to the others first
DECOMMISSION_POLL_S = 60

# Args: <krb5 ccache or ''> <timeout seconds>. Returns once SCM is out of safe mode; fails after the timeout.
_SAFEMODE_WAIT = r'''
[[ -z $1 ]] || export KRB5CCNAME=$1
ozone admin safemode wait -t "$2"
'''


def wait_for_safemode_exit(p: Prompter, args: Args) -> None:
    """After a full restart SCM stays in safe mode until enough datanodes report in, while CM already shows every
    role STARTED; writes fail until then. Runs Ozone's own wait on the Freon host."""
    seconds = duration.parse(SAFEMODE_TIMEOUT)
    p.say(f"  waiting for SCM to leave safe mode (up to {SAFEMODE_TIMEOUT})")
    try:
        with Remote(args.ssh_user or "root", os.environ.get(SSH_PWD_ENV, "")) as remote:
            ccache = ""
            if args.kerberos_keytab and args.kerberos_principal:
                probes.kinit(remote, args.freon_host, args.kerberos_keytab, args.kerberos_principal)
                ccache = probes.KRB5CC
            remote.run(args.freon_host, _SAFEMODE_WAIT, ccache, seconds, timeout=seconds + 60)
    except RemoteError as e:
        raise Abort(f"SCM did not leave safe mode: {e}") from None
    p.ok("SCM is out of safe mode")


def isolate_freon_host(p: Prompter, args: Args, cm: Cm) -> None:
    """Keeps the Freon client off every Ozone server: the Ozone DataNode on --freon-host must be DECOMMISSIONED
    (the first time this decommissions it and waits while SCM moves its containers away), then its role is stopped
    for the runs. Runs after every restart: a stopped datanode that were still IN_SERVICE would make SCM re-replicate
    its data during the runs."""
    host = args.freon_host or ""
    if not host:
        raise Abort("--isolate-freon-host needs --freon-host")
    roles = _roles_on(cm, host)
    if servers := sorted({t for t in roles if t in ("OZONE_MANAGER", "STORAGE_CONTAINER_MANAGER")}):
        raise Abort(f"--isolate-freon-host: {host} also runs {', '.join(servers)}; pick a DataNode-only Freon host")
    if "OZONE_DATANODE" not in roles:
        p.ok(f"no Ozone DataNode on the Freon host {short(host)}")
        return
    timeout, deadline, last = duration.parse(DECOMMISSION_TIMEOUT), 0.0, ""
    try:
        with Remote(args.ssh_user or "root", os.environ.get(SSH_PWD_ENV, "")) as remote:
            if args.kerberos_keytab and args.kerberos_principal:
                probes.kinit(remote, host, args.kerberos_keytab, args.kerberos_principal)
            while True:
                state, health = next((v for h, v in probes.datanode_states(remote, host).items()
                                      if short(h) == short(host)), ("NOT LISTED", "?"))
                if state == "DECOMMISSIONED":
                    p.ok(f"DataNode on the Freon host {short(host)} is DECOMMISSIONED ({health})")
                    break
                if state == "IN_SERVICE" and not deadline:
                    if health != "HEALTHY":
                        raise Abort(f"DataNode on {short(host)} is IN_SERVICE but {health}: start it in CM, so SCM "
                                    "can move its containers off, then rerun")
                    p.say(f"  decommissioning the DataNode on {short(host)} (up to {DECOMMISSION_TIMEOUT})")
                    probes.decommission_datanode(remote, host, host)
                elif state not in ("IN_SERVICE", "DECOMMISSIONING"):
                    raise Abort(f"DataNode on {short(host)}: unexpected state {state} ({health})")
                deadline = deadline or time.monotonic() + timeout
                if time.monotonic() > deadline:
                    raise Abort(f"DataNode on {short(host)} still {state} after {DECOMMISSION_TIMEOUT}")
                if f"{state}/{health}" != last:
                    p.say(f"  {short(host)}: {state} ({health})")
                    last = f"{state}/{health}"
                time.sleep(DECOMMISSION_POLL_S)
    except RemoteError as e:
        raise Abort(f"decommissioning the DataNode on {short(host)}: {e}") from None
    stop_roles(p, cm, ["OZONE_DATANODE"], host=host)


def _roles_on(cm: Cm, host: str) -> set[str]:
    """Role types CM has on host."""
    try:
        names = cm.client.hostnames()
        return {r["type"] for r in cm.client.roles(cm.cluster, cm.service)
                if short(names.get(r.get("hostRef", {}).get("hostId", ""), "")) == short(host)}
    except CmError as e:
        raise Abort(f"Cloudera Manager: {e}") from None


def _duration(raw: str) -> int:
    try:
        return duration.parse(raw)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def main(argv: list[str] | None = None) -> int:
    parser = cli._parser()
    parser.prog, parser.description = "python -m usecases.tracing", __doc__
    parser.set_defaults(runs="5", warmup_keys="20000", profiler="none")
    parser.add_argument("--config-pause", type=_duration, default="10m", metavar="DURATION",
                        help="wait after a config's runs (and --cleanup's volume delete) before the next config, so "
                             "background deletion can finish: e.g. 15m, 1h, 900 (seconds) (default 10m; 0 = no wait)")
    parser.add_argument("--isolate-freon-host", action="store_true",
                        help="decommission the Ozone DataNode on --freon-host (once; SCM moves its containers away) "
                             "and keep that role stopped during every config's runs, so the Freon client shares its "
                             "host with no Ozone server")
    ns = vars(parser.parse_args(argv))
    config_pause = ns.pop("config_pause")
    isolate = ns.pop("isolate_freon_host")
    args = Args(**ns)
    p = Prompter(interactive=not args.yes)
    results: dict[str, list[FreonResult]] = {}
    try:
        cm = connect_cm(args)
        folder = campaign_folder(args, "tracing")
        for i, config in enumerate(CONFIGS):
            if i:
                pause(p, config_pause, f"before {config.name}: background deletion of the last config's data")
            setup_cluster(p, cm, config)
            wait_for_safemode_exit(p, args)
            if isolate:
                isolate_freon_host(p, args, cm)
            stop_roles(p, cm, STOPPED_ROLES)
            results[config.name] = run_freon(args, folder, config)
        write_summary(p, folder, CONFIGS, results)
        return 0
    except Abort as e:
        p.fail(f"stopped: {e}")
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted: the cluster stays on its current configuration", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
