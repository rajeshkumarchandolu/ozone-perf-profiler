"""JFR capture: Java Flight Recorder on the chosen Ozone JVMs while the Freon runs go, .jfr files copied back.

Recording starts with run 1 and lasts the asked period (JFR stops itself at the end of it); if the runs finish
sooner it is stopped then. jcmd runs as the JVM's own user, the only user it can attach as. Each remote file is
deleted once its copy is verified by size.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .discover import Discovered
from .freon import REMOTE_BASE
from .options import Args
from .prompts import Invalid, Ok, Prompter
from .remote import RemoteError, parallel, short

DEFAULT_SERVICES = "om,scm,dn"
DEFAULT_PERIOD_S = "600"
SETTINGS = "profile"  # the JDK's low-overhead profiling template

_AS_USER = r'''
as_user() {
  local u=$1; shift
  if [[ $u == "$(id -un)" ]]; then "$@"
  elif [[ $(id -u) == 0 ]] && command -v runuser > /dev/null; then runuser -u "$u" -- "$@"
  else sudo -n -u "$u" -- "$@"; fi
}
'''

# Args: <pid> <user> <jcmd> <file> <recording name> <seconds> <settings>.
_START = _AS_USER + r'''
set -u
pid=$1 user=$2 jcmd=$3 file=$4 name=$5 seconds=$6 settings=$7
kill -0 "$pid" 2> /dev/null || { echo "PID $pid is no longer running" >&2; exit 3; }
install -d -m 755 -o "$user" "${file%/*}" || exit 3
as_user "$user" "$jcmd" "$pid" JFR.start name="$name" settings="$settings" duration="${seconds}s" filename="$file" 2>&1
'''

# Args: <pid> <user> <jcmd> <file> <recording name>. Stops the recording if still running; prints the file size.
_STOP = _AS_USER + r'''
set -u
pid=$1 user=$2 jcmd=$3 file=$4 name=$5
if kill -0 "$pid" 2> /dev/null && as_user "$user" "$jcmd" "$pid" JFR.check name="$name" 2>&1 | grep -q '(running)'
then as_user "$user" "$jcmd" "$pid" JFR.stop name="$name" > /dev/null 2>&1; fi
for _ in $(seq 20); do [[ -s $file ]] && break; sleep 0.5; done
[[ -s $file ]] || { echo "no recording at $file" >&2; exit 3; }
stat -c %s "$file"
'''


@dataclass(frozen=True)
class Target:
    """One JVM to record."""
    service: str  # om, scm, dn, recon, s3g
    host: str
    pid: int
    user: str
    jcmd: str
    remote_file: str

    @property
    def file_name(self) -> str:
        return f"{self.service}-{short(self.host)}.jfr"


@dataclass(frozen=True)
class JfrPlan:
    targets: tuple[Target, ...]
    period_s: int
    name: str  # the JFR recording name
    answers: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Recording:
    service: str
    host: str
    local_file: str | None
    size_bytes: int | None
    error: str | None = None


def ask(p: Prompter, args: Args, d: Discovered) -> JfrPlan | None:
    p.section("JFR capture")
    if not (args.capture_jfr or p.confirm("Capture JFR on the Ozone service hosts?", default=False)):
        return None
    available = _targets(d)
    targets = p.ask("Services to record (om, scm, dn, recon, s3g)", _services(available), default=DEFAULT_SERVICES,
                    preset=args.jfr_services, flag="--jfr-services")
    period = p.ask("JFR recording period (seconds)", _period, default=DEFAULT_PERIOD_S, preset=args.jfr_period,
                   flag="--jfr-period")
    p.warn(f"JFR '{SETTINGS}' costs a few % of CPU: compare these runs only with other JFR runs")
    services = ",".join(dict.fromkeys(t.service for t in targets))
    answers = {"capture_jfr": True, "jfr_services": None if services == DEFAULT_SERVICES else services,
               "jfr_period": None if str(period) == DEFAULT_PERIOD_S else period}
    return JfrPlan(tuple(targets), period, f"opp-{d.run_dir.name}", answers)


def _targets(d: Discovered) -> list[Target]:
    """Every Ozone JVM that discovery found running with a jcmd next to its java."""
    jvms, folder = d.discovery.jvms, f"{REMOTE_BASE}/{d.run_dir.name}/jfr"
    return [Target(r.short, r.host, jvms[r.name].pid, jvms[r.name].user, jvms[r.name].jcmd,
                   f"{folder}/{r.short}-{short(r.host)}.jfr")
            for r in d.discovery.topology.jvm_roles if r.name in jvms and jvms[r.name].jcmd]


def _services(available: list[Target]) -> Callable[[str], Ok[list[Target]]]:
    def check(raw: str) -> Ok[list[Target]]:
        wanted = [s.strip().lower() for s in raw.split(",") if s.strip()]
        known = sorted({t.service for t in available})
        unknown = [s for s in wanted if s not in known]
        if not wanted or unknown:
            raise Invalid(f"{'unknown: ' + ', '.join(unknown) + '; ' if unknown else ''}"
                          f"choose from {', '.join(known)} (JVMs found running with jcmd)")
        chosen = [t for t in available if t.service in wanted]
        counts = ", ".join(f"{sum(t.service == s for t in chosen)} {s}" for s in dict.fromkeys(wanted))
        return Ok(chosen, f"{len(chosen)} JVMs to record: {counts}")
    return check


def _period(raw: str) -> Ok[int]:
    if not raw.isdigit() or int(raw) < 30:
        raise Invalid("a whole number of seconds, at least 30")
    return Ok(int(raw), f"{int(raw) // 60}m{int(raw) % 60:02d}s from the start of run 1")


def start(p: Prompter, d: Discovered, plan: JfrPlan) -> list[Target]:
    """Starts every recording; returns the JVMs that are recording."""
    def begin(t: Target) -> str:
        return d.remote.run(t.host, _START, t.pid, t.user, t.jcmd, t.remote_file, plan.name, plan.period_s,
                            SETTINGS, timeout=60)

    started = []
    for t, out in parallel(begin, plan.targets).items():
        if isinstance(out, Exception) or "Started recording" not in out:
            p.warn(f"JFR did not start on {t.service}@{short(t.host)}: {str(out).strip()[-200:]}")
        else:
            started.append(t)
    if not started:
        p.fail("JFR did not start on any JVM; the Freon runs go ahead without it")
        return started
    until = time.strftime("%H:%M:%S", time.localtime(time.time() + plan.period_s))
    p.ok(f"JFR recording on {len(started)}/{len(plan.targets)} JVMs for {plan.period_s}s (until {until} local)")
    return started


def finish(p: Prompter, d: Discovered, plan: JfrPlan, started: list[Target]) -> list[Recording]:
    """Stops what is still recording, copies each .jfr to <run folder>/jfr/ and removes the remote copy."""
    p.section("Collecting the JFR recordings")
    folder = d.run_dir.path / "jfr"

    def collect(t: Target) -> Recording:
        size = int(d.remote.run(t.host, _STOP, t.pid, t.user, t.jcmd, t.remote_file, plan.name, timeout=120))
        local = folder / t.file_name
        d.remote.fetch(t.host, t.remote_file, local)
        if local.stat().st_size != size:
            raise RemoteError(f"size mismatch: {size} bytes remote, {local.stat().st_size} local")
        d.remote.run(t.host, 'rm -f "$1"', t.remote_file, timeout=30)
        return Recording(t.service, t.host, str(local), size)

    recordings = []
    for t, result in parallel(collect, started).items():
        if isinstance(result, Exception):
            p.warn(f"{t.service}@{short(t.host)}: {result} (left at {t.host}:{t.remote_file})")
            recordings.append(Recording(t.service, t.host, None, None, str(result)))
        else:
            recordings.append(result)
    copied = [r for r in recordings if r.local_file]
    p.ok(f"copied {len(copied)}/{len(started)} recordings ({sum(r.size_bytes or 0 for r in copied) / 1e6:,.1f} MB) "
         f"to {folder}")
    d.run_dir.update_manifest("jfr", {"settings": SETTINGS, "period_s": plan.period_s, "recordings": recordings})
    return recordings


def interrupted_hint(plan: JfrPlan) -> str:
    folder = plan.targets[0].remote_file.rsplit("/", 1)[0]
    return f"JFR keeps recording for up to {plan.period_s}s; the .jfr files stay in {folder} on each host"
