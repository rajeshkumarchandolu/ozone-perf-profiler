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

from ozone_perf_profiler import cli, duration, probes
from ozone_perf_profiler.freon_output import FreonResult
from ozone_perf_profiler.options import SSH_PWD_ENV, Args
from ozone_perf_profiler.prompts import Abort, Prompter
from ozone_perf_profiler.remote import Remote, RemoteError

from .utils import Config, campaign_folder, connect_cm, pause, run_freon, setup_cluster, write_summary

CONFIGS = [
    Config("tracing-off", {"ozone.tracing.enabled": "false"}),
    Config("tracing-on-s1.0", {"ozone.tracing.enabled": "true", "ozone.tracing.sampler": "1.0"}),
    Config("tracing-on-s0.5", {"ozone.tracing.enabled": "true", "ozone.tracing.sampler": "0.5"}),
    Config("tracing-on-s0.1", {"ozone.tracing.enabled": "true", "ozone.tracing.sampler": "0.1"}),
    Config("tracing-on-s0.01", {"ozone.tracing.enabled": "true", "ozone.tracing.sampler": "0.01"}),
]


SAFEMODE_TIMEOUT = "10m"

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
    ns = vars(parser.parse_args(argv))
    config_pause = ns.pop("config_pause")
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
