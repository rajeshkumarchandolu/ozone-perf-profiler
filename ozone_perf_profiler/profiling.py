"""Profiling during the Freon runs: JFR or async-profiler (one of them) on the chosen Ozone JVMs.

Discovery only checks what is available (jcmd next to each JVM's java; async-profiler under /opt on each host). The
question comes after the Freon settings; async-profiler is installed, where missing, right before Freon starts.
Each Freon run is recorded on its own: recording starts right before the run and stops when it ends (the asked period,
default 30 min, is only a safety limit). Every profiler command runs as the JVM's own user, the only user that can
attach to it. After each run its recordings are copied to <run folder>/profiler/<profiler>/<run>/<service>-<host>.jfr
and, once the size matches, deleted from the host.
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import duration
from .discover import Discovered
from .freon import FREON_RECORDING, REMOTE_BASE
from .options import Args
from .prompts import Abort, Invalid, Ok, Option, Prompter
from .remote import RemoteError, parallel, short

NONE, JFR, ASYNC = "none", "jfr", "async-profiler"
DEFAULT_SERVICES = "om,scm,dn"
DEFAULT_ASYNC_SERVICES = "om,scm,dn,freon"
FREON = "freon"  # the Freon client JVM; async-profiler only
DEFAULT_PERIOD = "30m"  # per-run safety limit: each recording stops when its run ends
JFR_SETTINGS = "profile"  # the JDK's low-overhead profiling template
ASPROF_VERSION = "4.6"
ASPROF_HOME = f"/opt/async-profiler-{ASPROF_VERSION}"
ASPROF = f"{ASPROF_HOME}/bin/asprof"
ASPROF_LIB = f"{ASPROF_HOME}/lib/libasyncProfiler.so"
ASPROF_URL = (f"https://github.com/async-profiler/async-profiler/releases/download/v{ASPROF_VERSION}/"
              f"async-profiler-{ASPROF_VERSION}-linux-x64.tar.gz")
DEFAULT_EVENTS = "cpu,alloc,lock,wall"

_AS_USER = r'''
as_user() {
  local u=$1; shift
  if [[ $u == "$(id -un)" ]]; then "$@"
  elif [[ $(id -u) == 0 ]] && command -v runuser > /dev/null; then runuser -u "$u" -- "$@"
  else sudo -n -u "$u" -- "$@"; fi
}
'''

# --- remote scripts: JFR -------------------------------------------------------------------------------------------

# Args: <pid> <user> <jcmd> <file> <recording name> <seconds> <settings>.
_JFR_START = _AS_USER + r'''
set -u
pid=$1 user=$2 jcmd=$3 file=$4 name=$5 seconds=$6 settings=$7
kill -0 "$pid" 2> /dev/null || { echo "PID $pid is no longer running" >&2; exit 3; }
install -d -m 755 -o "$user" "${file%/*}" || exit 3
as_user "$user" "$jcmd" "$pid" JFR.start name="$name" settings="$settings" duration="${seconds}s" filename="$file" 2>&1
'''

# Args: <pid> <user> <jcmd> <file> <recording name>. Stops the recording if still running; prints the file size.
_JFR_STOP = _AS_USER + r'''
set -u
pid=$1 user=$2 jcmd=$3 file=$4 name=$5
if kill -0 "$pid" 2> /dev/null && as_user "$user" "$jcmd" "$pid" JFR.check name="$name" 2>&1 | grep -q '(running)'
then as_user "$user" "$jcmd" "$pid" JFR.stop name="$name" > /dev/null 2>&1; fi
for _ in $(seq 20); do [[ -s $file ]] && break; sleep 0.5; done
[[ -s $file ]] || { echo "no recording at $file" >&2; exit 3; }
stat -c %s "$file"
'''

# --- remote scripts: async-profiler -------------------------------------------------------------------------------

# Args: <home> <url>. Installs into <home> unless present; links asprof and jfrconv into /usr/local/bin.
_ASPROF_INSTALL = r'''
set -u
home=$1 url=$2
SUDO=; [[ $(id -u) == 0 ]] || SUDO="sudo -n"
if [[ ! -x $home/bin/asprof ]]; then
  [[ $(uname -m) == x86_64 ]] || { echo "the linux-x64 build does not run on $(uname -m)" >&2; exit 3; }
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  curl -fsSL --connect-timeout 15 --max-time 300 -o "$tmp/ap.tgz" "$url" || { echo "download failed: $url" >&2; exit 3; }
  $SUDO mkdir -p "$home" && $SUDO tar -xzf "$tmp/ap.tgz" -C "$home" --strip-components=1 || exit 3
fi
$SUDO ln -sfn "$home/bin/asprof" /usr/local/bin/asprof && $SUDO ln -sfn "$home/bin/jfrconv" /usr/local/bin/jfrconv
"$home/bin/asprof" --version 2>&1 | head -1
'''

# Args: <home>. Prints "<installed yes|no> <kernel.perf_event_paranoid>".
_ASPROF_CHECK = r'''
printf '%s %s\n' "$([[ -x $1/bin/asprof ]] && echo yes || echo no)" "$(sysctl -n kernel.perf_event_paranoid 2> /dev/null || echo ?)"
'''

# Args: <pid> <user> <asprof> <file> <seconds> <events>. asprof -d blocks for the whole period, so it runs detached
# (setsid, its own session) with its output in <file>.log; prints that log once profiling has started.
_ASPROF_START = _AS_USER + r'''
set -u
pid=$1 user=$2 asprof=$3 file=$4 seconds=$5 events=$6
kill -0 "$pid" 2> /dev/null || { echo "PID $pid is no longer running" >&2; exit 3; }
install -d -m 755 -o "$user" "${file%/*}" || exit 3
export -f as_user
setsid nohup bash -c 'echo $$ > "$4.pid"; as_user "$0" "$1" -d "$2" -e "$3" -t -f "$4" "$5"; echo $? > "$4.exit"' \
  "$user" "$asprof" "$seconds" "$events" "$file" "$pid" > "$file.log" 2>&1 < /dev/null &
for _ in $(seq 20); do grep -qE 'Profiling (started|for)' "$file.log" 2> /dev/null && break
  [[ -s $file.exit ]] && break; sleep 0.5; done
cat "$file.log"
'''

# Args: <pid> <user> <asprof> <file>. Stops profiling if the period hasn't ended; prints the recording's size.
_ASPROF_STOP = _AS_USER + r'''
set -u
pid=$1 user=$2 asprof=$3 file=$4
launcher=$(cat "$file.pid" 2> /dev/null)
if [[ ! -s $file.exit && -n $launcher ]] && kill -0 "$launcher" 2> /dev/null; then
  as_user "$user" "$asprof" stop -f "$file" "$pid" >> "$file.log" 2>&1
  kill -- -"$launcher" 2> /dev/null  # the detached asprof would otherwise stop again at the period's end
fi
for _ in $(seq 40); do [[ -s $file ]] && break; sleep 0.5; done
[[ -s $file ]] || { echo "no recording at $file (see $file.log)" >&2; exit 3; }
stat -c %s "$file"
'''


# --- remote scripts: recordings already running on a JVM ---------------------------------------------------------

# Args: <pid> <user> <asprof> <remote base> <file name>. Prints "<running s> <left s or -> <opp file or ->" when a
# profiling session is active on the JVM. An opp session is found by its live launcher under <base>/*/profiler/.
_ASPROF_LEFTOVER = _AS_USER + r'''
set -u
pid=$1 user=$2 asprof=$3 base=$4 name=$5
[[ -x $asprof ]] && kill -0 "$pid" 2> /dev/null || exit 0
status=$(as_user "$user" "$asprof" status "$pid" 2>&1)
[[ $status =~ running\ for\ ([0-9]+)\ seconds ]] || exit 0
running=${BASH_REMATCH[1]} left=- old=-
for f in "$base"/*/profiler/"$name".pid "$base"/*/profiler/*/"$name".pid; do
  launcher=$(cat "$f" 2> /dev/null) && kill -0 "$launcher" 2> /dev/null || continue
  old=${f%.pid}
  period=$(ps -eo pgid=,args= | awk -v g="$launcher" '$1 == g' | grep -oE -- ' -d [0-9]+' | head -1 | tr -dc 0-9)
  [[ -n $period ]] && left=$(( period > running ? period - running : 0 ))
  break
done
echo "$running $left $old"
'''

# Args: <pid> <user> <asprof> <opp file or ->. Stops the session; an opp one still writes its file (and its
# launcher is ended, so it can't stop the new session at the end of its period).
_ASPROF_STOP_LEFTOVER = _AS_USER + r'''
set -u
pid=$1 user=$2 asprof=$3 old=$4
if [[ $old == - ]]; then as_user "$user" "$asprof" stop "$pid" > /dev/null 2>&1
else
  as_user "$user" "$asprof" stop -f "$old" "$pid" >> "$old.log" 2>&1
  launcher=$(cat "$old.pid" 2> /dev/null) && kill -- -"$launcher" 2> /dev/null
fi
exit 0
'''

# Args: <pid> <user> <jcmd>. Prints the names of running opp JFR recordings, one per line.
_JFR_LEFTOVER = _AS_USER + r'''
kill -0 "$1" 2> /dev/null || exit 0
as_user "$2" "$3" "$1" JFR.check 2>&1 | grep '(running)' | grep -oE 'name=opp-[^ ]+' | cut -d= -f2
exit 0
'''

# Args: <pid> <user> <jcmd> <recording names...>. Stops them; each writes its file if it was started with one.
_JFR_STOP_LEFTOVER = _AS_USER + r'''
pid=$1 user=$2 jcmd=$3; shift 3
for name; do as_user "$user" "$jcmd" "$pid" JFR.stop name="$name" > /dev/null 2>&1; done
exit 0
'''

RESTART, WAIT, ABORT = "restart", "wait", "abort"


@dataclass(frozen=True)
class Availability:
    """What discovery found: JVMs JFR can attach to, and the hosts that already have async-profiler."""
    jfr_jvms: int
    jvms: int
    asprof_hosts: frozenset[str]
    hosts: tuple[str, ...]
    paranoid: dict[str, str]  # host -> kernel.perf_event_paranoid


@dataclass(frozen=True)
class Target:
    """One JVM to record."""
    service: str  # om, scm, dn, recon, s3g
    host: str
    pid: int
    user: str
    jcmd: str | None
    remote_file: str
    run: str = ""  # the Freon run this recording covers (run-1, ...); set by for_run

    @property
    def file_name(self) -> str:
        return f"{self.service}-{short(self.host)}.jfr"

    def for_run(self, run: str) -> Target:
        """The same JVM, recorded into <profiler folder>/<run>/ on its host."""
        folder = self.remote_file.rsplit("/", 1)[0]
        return dataclasses.replace(self, run=run, remote_file=f"{folder}/{run}/{self.file_name}")


@dataclass(frozen=True)
class Leftover:
    """A recording already running on a target JVM, e.g. from an interrupted earlier run."""
    target: Target
    running_s: int | None
    left_s: int | None            # None: unknown
    old_file: str | None = None   # async-profiler: the earlier opp run's output file; None: not started by opp
    jfr_names: tuple[str, ...] = ()

    @property
    def by_opp(self) -> bool:
        return self.old_file is not None or bool(self.jfr_names)

    @property
    def run(self) -> str | None:
        """The earlier opp run that started it."""
        if self.old_file:
            return self.old_file.split("/")[3]  # /tmp/opp/<run>/profiler/<file>
        return self.jfr_names[0].removeprefix("opp-") if self.jfr_names else None


@dataclass(frozen=True)
class ProfilePlan:
    profiler: str  # JFR or ASYNC
    targets: tuple[Target, ...]
    period_s: int
    events: str    # async-profiler events; JFR uses JFR_SETTINGS
    name: str      # JFR recording name
    answers: dict[str, object] = field(default_factory=dict)
    leftovers: tuple[Leftover, ...] = ()  # recordings already running on the targets when asked
    on_leftovers: str = RESTART          # RESTART or WAIT
    freon: bool = False                  # async-profiler also on the Freon client JVM of every measured run

    @property
    def settings(self) -> str:
        return f"settings={JFR_SETTINGS}" if self.profiler == JFR else f"async-profiler {ASPROF_VERSION} {self.events}"

    def freon_jvm_opts(self) -> str:
        """The Freon JVM is short-lived, so async-profiler is loaded as an agent at its start and writes the recording
        when it exits; '{dir}' is the run's folder on the Freon host. Same events and per-thread split as the
        services. Empty when the Freon client isn't profiled."""
        if not self.freon:
            return ""
        return f"-agentpath:{ASPROF_LIB}=start,event={self.events},threads,file={{dir}}/{FREON_RECORDING}"


@dataclass(frozen=True)
class Recording:
    service: str
    host: str
    local_file: str | None
    size_bytes: int | None
    error: str | None = None


# --- discovery: what is available ---------------------------------------------------------------------------------

def detect(p: Prompter, d: Discovered) -> Availability:
    jvms = [r for r in d.discovery.topology.jvm_roles if r.name in d.discovery.jvms]
    hosts = tuple(sorted({r.host for r in jvms}))
    found = parallel(lambda h: d.remote.run(h, _ASPROF_CHECK, ASPROF_HOME, timeout=30).split(), hosts)
    checks = {h: out for h, out in found.items() if not isinstance(out, Exception) and len(out) == 2}
    jfr_jvms = sum(bool(d.discovery.jvms[r.name].jcmd) for r in jvms)
    avail = Availability(jfr_jvms, len(jvms), frozenset(h for h, (has, _) in checks.items() if has == "yes"), hosts,
                         {h: paranoid for h, (_, paranoid) in checks.items()})
    p.ok(f"profilers: JFR possible on {jfr_jvms}/{len(jvms)} JVMs; async-profiler {ASPROF_VERSION} installed on "
         f"{len(avail.asprof_hosts)}/{len(hosts)} hosts")
    if strict := sorted(short(h) for h, v in avail.paranoid.items() if v.lstrip("-").isdigit() and int(v) > 1):
        p.warn(f"kernel.perf_event_paranoid > 1 on {len(strict)} host(s) ({', '.join(strict[:5])}): async-profiler "
               "'cpu' falls back to a timer, without kernel frames")
    return avail


# --- question -----------------------------------------------------------------------------------------------------

def ask(p: Prompter, args: Args, d: Discovered, avail: Availability) -> ProfilePlan | None:
    p.section("Profiling")
    events = args.asprof_events or DEFAULT_EVENTS
    if not re.fullmatch(r"[A-Za-z0-9_.:,]+", events):
        raise Abort(f"--asprof-events: not an event list: {events!r}")
    missing = len(avail.hosts) - len(avail.asprof_hosts)
    profiler = p.choose("Profile the Ozone JVMs during the runs", [
        Option(NONE, "none", NONE),
        Option(JFR, f"JFR (JDK Flight Recorder, settings={JFR_SETTINGS}; possible on {avail.jfr_jvms}/{avail.jvms} JVMs)",
               JFR),
        Option(ASYNC, f"async-profiler {ASPROF_VERSION} ({events}; installed on {len(avail.asprof_hosts)}/"
                      f"{len(avail.hosts)} hosts" + (", the rest installed before Freon starts)" if missing else ")"),
               ASYNC),
    ], preset=args.profiler, flag="--profiler")
    if profiler == NONE:
        return None
    with_freon = profiler == ASYNC  # the Freon client is only profiled with async-profiler (as an agent)
    default_services = DEFAULT_ASYNC_SERVICES if with_freon else DEFAULT_SERVICES
    targets, freon_client = p.ask(f"Services to record (om, scm, dn, recon, s3g{', freon' if with_freon else ''})",
                                  _services(_targets(d, profiler), with_freon), default=default_services,
                                  preset=args.profile_services, flag="--profile-services")
    period = p.ask("Recording limit per run (each recording stops when its run ends; e.g. 30m)", _period,
                   default=DEFAULT_PERIOD, preset=args.profile_period, flag="--profile-period")
    p.warn(f"{profiler} costs a few % of CPU: compare these runs only with other {profiler} runs")
    services = ",".join([*dict.fromkeys(t.service for t in targets), *([FREON] if freon_client else [])])
    answers = {"profiler": profiler, "profile_services": None if services == default_services else services,
               "profile_period": None if period == duration.parse(DEFAULT_PERIOD) else duration.format(period),
               "asprof_events": None if profiler != ASYNC or events == DEFAULT_EVENTS else events}
    plan = ProfilePlan(profiler, tuple(targets), period, events, f"opp-{d.run_dir.name}", answers,
                       freon=freon_client)
    return _ask_leftovers(p, d, plan)


def _ask_leftovers(p: Prompter, d: Discovered, plan: ProfilePlan) -> ProfilePlan:
    """Recordings still running on the targets (e.g. from an interrupted run) would block or skew the new ones."""
    leftovers = find_leftovers(d, plan)
    if not leftovers:
        return plan
    left = max((lo.left_s for lo in leftovers if lo.left_s is not None), default=None)
    runs = sorted({lo.run for lo in leftovers if lo.run})
    foreign = sum(not lo.by_opp for lo in leftovers)
    origin = ", ".join(filter(None, [f"started by opp run {', '.join(runs)}" if runs else "",
                                     f"{foreign} not started by opp" if foreign else "",
                                     f"~{left}s left" if left is not None else ""]))
    p.warn(f"{plan.profiler} is already running on {len(leftovers)} of the {len(plan.targets)} JVMs ({origin})")
    action = p.choose("What should opp do with them", [
        Option(RESTART, "stop them and start fresh (their recordings are still written on the hosts)", RESTART),
        Option(WAIT, f"wait until they finish{f' (~{left}s)' if left is not None else ''}, then start", WAIT),
        Option(ABORT, "stop opp", ABORT),
    ])
    if action == ABORT:
        raise Abort(f"{plan.profiler} recordings are still running on {len(leftovers)} JVMs")
    return dataclasses.replace(plan, leftovers=tuple(leftovers), on_leftovers=action)


def find_leftovers(d: Discovered, plan: ProfilePlan) -> list[Leftover]:
    def check(t: Target) -> Leftover | None:
        if plan.profiler == JFR:
            names = tuple(d.remote.run(t.host, _JFR_LEFTOVER, t.pid, t.user, t.jcmd, timeout=60).split())
            return Leftover(t, None, None, jfr_names=names) if names else None
        out = d.remote.run(t.host, _ASPROF_LEFTOVER, t.pid, t.user, ASPROF, REMOTE_BASE, t.file_name,
                           timeout=60).split()
        if len(out) != 3:
            return None
        running, left, old = out
        return Leftover(t, int(running), None if left == "-" else int(left), None if old == "-" else old)

    return [lo for lo in parallel(check, plan.targets).values() if isinstance(lo, Leftover)]


def settle_leftovers(p: Prompter, d: Discovered, plan: ProfilePlan) -> None:
    """Carries out the answer to the leftover question, once, before the first run's recordings start."""
    if not plan.leftovers:
        return
    if plan.on_leftovers == WAIT:
        _wait_for_leftovers(p, d, plan)
        return

    def stop(lo: Leftover) -> None:
        t = lo.target
        if plan.profiler == JFR:
            d.remote.run(t.host, _JFR_STOP_LEFTOVER, t.pid, t.user, t.jcmd, *lo.jfr_names, timeout=120)
        else:
            d.remote.run(t.host, _ASPROF_STOP_LEFTOVER, t.pid, t.user, ASPROF, lo.old_file or "-", timeout=120)

    failed = {lo.target: e for lo, e in parallel(stop, plan.leftovers).items() if isinstance(e, Exception)}
    for t, e in failed.items():
        p.warn(f"stopping the earlier recording on {t.service}@{short(t.host)} failed: {e}")
    p.ok(f"stopped {len(plan.leftovers) - len(failed)} earlier recording(s); their files stay on the hosts under "
         f"{REMOTE_BASE}/<earlier run>/profiler")


def _wait_for_leftovers(p: Prompter, d: Discovered, plan: ProfilePlan, poll_s: int = 30) -> None:
    known = [lo.left_s for lo in plan.leftovers if lo.left_s is not None]
    deadline = time.monotonic() + (max(known) + 120 if known else 3600)
    while (still := find_leftovers(d, plan)) and time.monotonic() < deadline:
        p.say(f"  waiting for {len(still)} earlier recording(s) to finish")
        time.sleep(poll_s)
    if still:
        raise Abort(f"{len(still)} earlier {plan.profiler} recording(s) are still running; stop them or rerun later")
    p.ok("the earlier recordings have finished")


def _targets(d: Discovered, profiler: str) -> list[Target]:
    """Every Ozone JVM discovery found running; JFR also needs a jcmd next to its java."""
    jvms, folder = d.discovery.jvms, f"{REMOTE_BASE}/{d.run_dir.name}/profiler"
    return [Target(r.short, r.host, jvms[r.name].pid, jvms[r.name].user, jvms[r.name].jcmd,
                   f"{folder}/{r.short}-{short(r.host)}.jfr")
            for r in d.discovery.topology.jvm_roles
            if r.name in jvms and (profiler != JFR or jvms[r.name].jcmd)]


def _services(available: list[Target], with_freon: bool) -> Callable[[str], Ok[tuple[list[Target], bool]]]:
    """(service JVMs to record, whether to record the Freon client too)."""
    def check(raw: str) -> Ok[tuple[list[Target], bool]]:
        wanted = [s.strip().lower() for s in raw.split(",") if s.strip()]
        known = sorted({t.service for t in available}) + ([FREON] if with_freon else [])
        unknown = [s for s in wanted if s not in known]
        if not wanted or unknown:
            raise Invalid(f"{'unknown: ' + ', '.join(unknown) + '; ' if unknown else ''}"
                          f"choose from {', '.join(known)}")
        chosen = [t for t in available if t.service in wanted]
        counts = [f"{sum(t.service == s for t in chosen)} {s}" for s in dict.fromkeys(wanted) if s != FREON]
        freon = FREON in wanted
        what = ", ".join(counts + (["the Freon client of each run"] if freon else []))
        return Ok((chosen, freon), f"to record: {what}")
    return check


def _period(raw: str) -> Ok[int]:
    seconds = duration.check("recording limit", minimum=30)(raw).value
    return Ok(seconds, f"each run recorded from its start to its end, at most {duration.format(seconds)}")


# --- before Freon: install ----------------------------------------------------------------------------------------

def install(p: Prompter, d: Discovered, plan: ProfilePlan, avail: Availability) -> None:
    """async-profiler only: installs it on the chosen hosts that lack it; any failure stops the run."""
    if plan.profiler != ASYNC:
        return
    hosts = sorted({t.host for t in plan.targets} | ({d.discovery.freon_host} if plan.freon else set()))
    needed = [h for h in hosts if h not in avail.asprof_hosts]
    if not needed:
        p.ok(f"async-profiler {ASPROF_VERSION} already installed on all {len(hosts)} hosts")
        return
    p.say(f"  installing async-profiler {ASPROF_VERSION} into {ASPROF_HOME} on {len(needed)} host(s)")
    results = parallel(lambda h: d.remote.run(h, _ASPROF_INSTALL, ASPROF_HOME, ASPROF_URL, timeout=600), needed)
    failed = {h: r for h, r in results.items() if isinstance(r, Exception)}
    if failed:
        for host, error in failed.items():
            p.fail(f"{short(host)}: {error}")
        raise Abort(f"async-profiler could not be installed on {len(failed)} host(s); nothing has run yet")
    p.ok(f"async-profiler {ASPROF_VERSION} installed on {len(needed)} host(s) "
         f"({len(hosts) - len(needed)} already had it), linked as /usr/local/bin/asprof")


# --- during the runs ----------------------------------------------------------------------------------------------

def start(p: Prompter, d: Discovered, plan: ProfilePlan, run: str) -> list[Target]:
    """Starts one recording per JVM for this Freon run, right before Freon starts; finish() stops it when the run
    ends. The period is only a safety limit, in case opp dies mid-run. Returns the JVMs that are recording."""
    if not plan.targets:  # only the Freon client: its agent starts with each run
        return []

    def begin(t: Target) -> str:
        if plan.profiler == JFR:
            return d.remote.run(t.host, _JFR_START, t.pid, t.user, t.jcmd, t.remote_file, plan.name, plan.period_s,
                                JFR_SETTINGS, timeout=60)
        return d.remote.run(t.host, _ASPROF_START, t.pid, t.user, ASPROF, t.remote_file, plan.period_s, plan.events,
                            timeout=60)

    started = []
    for t, out in parallel(begin, [t.for_run(run) for t in plan.targets]).items():
        if not isinstance(out, Exception) and _started(plan.profiler, out):
            started.append(t)
        else:
            p.warn(f"{plan.profiler} did not start on {t.service}@{short(t.host)}: {str(out).strip()[-300:]}")
    if not started:
        p.fail(f"{plan.profiler} did not start on any JVM; {run} goes ahead without it")
        return started
    p.ok(f"{plan.profiler} recording {run} on {len(started)}/{len(plan.targets)} JVMs "
         f"(stops when the run ends; at most {duration.format(plan.period_s)})")
    return started


def _started(profiler: str, out: str) -> bool:
    """jcmd prints 'Started recording N'; asprof -d prints 'Profiling for N seconds' (start: 'Profiling started')."""
    if profiler == JFR:
        return "Started recording" in out
    return re.search(r"Profiling (started|for)", out) is not None


def finish(p: Prompter, d: Discovered, plan: ProfilePlan, started: list[Target]) -> list[Recording]:
    """Stops the run's recordings, copies each to <run folder>/profiler/<profiler>/<run>/<service>-<host>.jfr, then
    removes the remote files."""
    run = started[0].run if started else ""
    p.say(f"  stopping and collecting the {plan.profiler} recordings of {run}")
    folder = d.run_dir.path / "profiler" / plan.profiler / run

    def collect(t: Target) -> Recording:
        if plan.profiler == JFR:
            out = d.remote.run(t.host, _JFR_STOP, t.pid, t.user, t.jcmd, t.remote_file, plan.name, timeout=120)
        else:
            out = d.remote.run(t.host, _ASPROF_STOP, t.pid, t.user, ASPROF, t.remote_file, timeout=120)
        size, local = int(out.strip().splitlines()[-1]), folder / t.file_name
        d.remote.fetch(t.host, t.remote_file, local)
        if local.stat().st_size != size:
            raise RemoteError(f"size mismatch: {size} bytes remote, {local.stat().st_size} local")
        if plan.profiler == ASYNC:
            d.remote.fetch(t.host, f"{t.remote_file}.log", local.with_name(local.name + ".log"))
        d.remote.run(t.host, 'rm -f "$1" "$1.log" "$1.pid" "$1.exit"', t.remote_file, timeout=30)
        return Recording(t.service, t.host, str(local), size)

    recordings = []
    for t, result in parallel(collect, started).items():
        if isinstance(result, Exception):
            p.warn(f"{t.service}@{short(t.host)}: {result} (left at {t.host}:{t.remote_file})")
            recordings.append(Recording(t.service, t.host, None, None, str(result)))
        else:
            recordings.append(result)
    copied = [r for r in recordings if r.local_file]
    p.ok(f"copied {len(copied)}/{len(started)} recordings of {run} "
         f"({sum(r.size_bytes or 0 for r in copied) / 1e6:,.1f} MB) to {folder}")
    d.run_dir.update_manifest(f"profiling-{run}", {"profiler": plan.profiler, "settings": plan.settings,
                                                   "limit_s": plan.period_s, "recordings": recordings})
    return recordings


def interrupted_hint(plan: ProfilePlan, started: list[Target]) -> str:
    folder = started[0].remote_file.rsplit("/", 1)[0]
    return (f"{plan.profiler} keeps recording for up to {duration.format(plan.period_s)}; "
            f"the files stay in {folder} on each host")
