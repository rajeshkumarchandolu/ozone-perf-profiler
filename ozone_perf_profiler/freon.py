"""Run `ozone freon ockg` detached on the Freon host, follow it, and copy its output back.

Freon runs detached on the host itself, so it finishes even if this CLI or its ssh connection dies.
Start and end times come from the host's clock, the same clock Prometheus samples are stamped against.
"""

from __future__ import annotations

import dataclasses
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import duration, probes
from .discover import Discovered
from .options import Args
from .prompts import Abort, Invalid, Ok, Option, Prompter
from .remote import Remote, RemoteError, short

REMOTE_BASE = "/tmp/opp"
POLL_S = 10
MAX_POLL_FAILURES = 6
DEFAULT_BUCKET = "freon"
DEFAULT_WARMUP_KEYS = 10
# replication -> datanodes it needs
REPLICATIONS = {"RATIS/THREE": 3, "RATIS/ONE": 1, "EC/rs-3-2-1024k": 5, "EC/rs-6-3-1024k": 9}
_SIZE = re.compile(r"(\d+)\s*([KMG]?)B?", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
_OZONE_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")

# Args: <dir> <tmux session> <krb5 ccache or ''> <extra JVM options or ''> <freon args...>. Prints "<launcher> <pid>".
# Detached in a tmux session when tmux is installed (attach to watch it live), else with setsid. Either way it
# outlives the ssh connection. The wrapper sets its own dir, KRB5CCNAME and OZONE_OPTS (e.g. the async-profiler
# agent): a tmux server that is already running does not pass on the environment of the ssh command that starts it.
_START = r'''
set -u
dir=$1 session=$2; shift 2
mkdir -p "$dir" && cd "$dir" || exit 3
[[ ! -e pid ]] || { echo "$dir already holds a run" >&2; exit 3; }
run='cd "$0" || exit 3; [[ -z $1 ]] || export KRB5CCNAME=$1; [[ -z $2 ]] || export OZONE_OPTS="${OZONE_OPTS:-} $2"; '\
'shift 2; echo $$ > pid; date +%s > start; '\
'ozone freon "$@" 2>&1 | tee freon.log; echo "${PIPESTATUS[0]}" > exit; date +%s > end'
if command -v tmux > /dev/null; then
  tmux new-session -d -s "$session" "$(printf '%q ' bash -c "$run" "$dir" "$@")" || exit 3
  launcher=tmux
else
  setsid nohup bash -c "$run" "$dir" "$@" < /dev/null > /dev/null 2>&1 &
  launcher=setsid
fi
for _ in $(seq 20); do [[ -s pid ]] && break; sleep 0.5; done
[[ -s pid ]] || { echo "Freon did not start" >&2; exit 3; }
echo "$launcher $(cat pid)"
'''

# Args: <dir>. STATE running | done <exit code> | lost, then the latest progress line.
_STATUS = r'''
cd "$1" 2> /dev/null || { echo "STATE lost"; exit 0; }
if [[ -s exit ]]; then echo "STATE done $(cat exit)"
elif kill -0 "$(cat pid 2> /dev/null)" 2> /dev/null; then echo "STATE running"
elif [[ -s exit ]]; then echo "STATE done $(cat exit)"
else echo "STATE lost"; fi
tail -c 4000 freon.log 2> /dev/null | tr '\r' '\n' | grep -v '^[[:space:]]*$' | tail -1 | sed 's/^/LAST /'
exit 0
'''

_TIMES = r'''
cd "$1" && printf '%s %s %s\n' "$(cat start)" "$(cat end 2> /dev/null || date +%s)" "$(cat exit 2> /dev/null || echo -)"
'''

# Args: <krb5 ccache or ''> <volume>.
_DELETE_VOLUME = r'''
[[ -z $1 ]] || export KRB5CCNAME=$1
ozone sh volume delete -r -y "/$2"
'''


# region agent log
_DBG_LOG = "/Volumes/cloudera/ozone-perf-profiler/.cursor/debug-30d5c8.log"
_TOP_START = r'''
mkdir -p "$1" && cd "$1" || exit 3
setsid nohup bash -c 'timeout 3h top -b -d 1 -c -w 512 | awk "/^top - /{n=0} n++<25 {print; fflush()}" > "$0/top.txt"' "$1" < /dev/null > /dev/null 2>&1 &
echo $! > top.pid
'''
_TOP_STOP = r'''
cd "$1" || exit 0
kill -- -"$(cat top.pid)" 2> /dev/null; sleep 0.3
for p in $(awk '$1 ~ /^[0-9]+$/ && $9+0 >= 50 {print $1}' top.txt | sort -u); do
  printf '%s %s\n' "$p" "$(tr '\0' ' ' < /proc/$p/cmdline 2> /dev/null | sed -E 's/ -(cp|classpath) [^ ]+//' | cut -c1-300)"
done
exit 0
'''


def _dbg(hypothesis: str, location: str, message: str, data: dict[str, object]) -> None:
    import json
    import threading
    lock = globals().setdefault("_DBG_LOCK", threading.Lock())
    with lock, open(_DBG_LOG, "a") as f:
        f.write(json.dumps({"sessionId": "30d5c8", "runId": data.get("run", "?"), "hypothesisId": hypothesis,
                            "location": location, "message": message, "data": data,
                            "timestamp": int(time.time() * 1000)}) + "\n")


def _hostcpu_start(d: Discovered, name: str) -> list[str]:
    from .remote import parallel
    hosts = sorted(set(d.discovery.topology.hosts) | {d.discovery.freon_host})
    remote_dir = f"{REMOTE_BASE}/{d.run_dir.name}/{name}-hostcpu"
    res = parallel(lambda h: d.remote.run(h, _TOP_START, remote_dir, timeout=30), hosts)
    started = [h for h, r in res.items() if not isinstance(r, Exception)]
    _dbg("B", "freon.py:_hostcpu_start", "host CPU samplers started",
         {"run": f"{d.run_dir.name}/{name}", "hosts": [short(h) for h in started],
          "failed": {short(h): str(r)[:200] for h, r in res.items() if isinstance(r, Exception)}})
    return started


def _hostcpu_stop(d: Discovered, name: str, hosts: list[str]) -> None:
    from .remote import parallel
    remote_dir, run = f"{REMOTE_BASE}/{d.run_dir.name}/{name}-hostcpu", f"{d.run_dir.name}/{name}"

    def stop(h: str) -> None:
        cmdlines = dict(x.split(" ", 1) for x in d.remote.run(h, _TOP_STOP, remote_dir, timeout=60).splitlines()
                        if " " in x)
        local = d.run_dir.path / "hostcpu" / name / f"{short(h)}-top.txt"
        d.remote.fetch(h, f"{remote_dir}/top.txt", local)
        hot, peak = 0, (0.0, "")
        for frame in local.read_text(errors="replace").split("top - ")[2:]:  # [1] averages since boot
            lines = frame.splitlines()
            idle = re.search(r"([\d.]+)\s*id", next((x for x in lines if x.startswith("%Cpu")), ""))
            busy = 100 - float(idle[1]) if idle else 0.0
            peak = max(peak, (busy, lines[0][:8]))
            if busy < 30:
                continue
            hot += 1
            procs = [x.split(None, 11) for x in lines if re.match(r"\s*\d+\s", x)]
            _dbg("B", "freon.py:_hostcpu_stop", "host CPU above 30%",
                 {"run": run, "host": short(h), "time": lines[0][:8], "busy%": round(busy, 1),
                  "top": [{"pid": p[0], "user": p[1], "cpu%": p[8], "cmd": cmdlines.get(p[0], p[-1])[-220:]}
                          for p in procs[:6] if len(p) > 11]})
        _dbg("B", "freon.py:_hostcpu_stop", "host CPU summary",
             {"run": run, "host": short(h), "peak_busy%": round(peak[0], 1), "peak_at": peak[1],
              "seconds_above_30%": hot})

    for h, r in parallel(stop, hosts).items():
        if isinstance(r, Exception):
            _dbg("B", "freon.py:_hostcpu_stop", "host CPU sampler failed",
                 {"run": run, "host": short(h), "error": str(r)[:300]})
# endregion


@dataclass(frozen=True)
class FreonSpec:
    threads: int
    keys: int
    size_bytes: int
    replication: str  # TYPE/FACTOR, a key of REPLICATIONS or anything ockg accepts
    volume: str
    bucket: str
    prefix: str
    extra: tuple[str, ...]

    def argv(self) -> list[str]:
        rtype, factor = self.replication.split("/", 1)
        return ["ockg", "-t", str(self.threads), "-n", str(self.keys), "-s", str(self.size_bytes),
                "-v", self.volume, "-b", self.bucket, "-p", self.prefix,
                "--type", rtype, "--replication", factor, *self.extra]


@dataclass(frozen=True)
class Launch:
    """How Freon was detached on the host: in a tmux session, or with setsid when tmux is missing."""
    launcher: str  # tmux | setsid
    pid: int
    session: str   # the tmux session name; unused with setsid

    @classmethod
    def parse(cls, out: str, session: str) -> Launch:
        launcher, pid = out.split()
        return cls(launcher, int(pid), session)

    def watch(self, target: str, remote_dir: str) -> str:
        if self.launcher == "tmux":
            return f"ssh -t {target} tmux attach -t {self.session}   (detach: Ctrl-b d)"
        return f"ssh {target} tail -f {remote_dir}/freon.log"

    def stop_script(self) -> str:
        """The host-side command that stops this Freon (its tmux session, or setsid's whole process group)."""
        if self.launcher == "tmux":
            return f"tmux kill-session -t {shlex.quote(self.session)}"
        return f"kill -- -{self.pid}"

    def stop(self, target: str) -> str:
        return f"ssh {target} {shlex.quote(self.stop_script())}"


@dataclass(frozen=True)
class FreonRun:
    index: int  # 1-based run number in the series; 0 is the warm-up
    spec: FreonSpec
    host: str
    remote_dir: str
    command: str
    launch: Launch
    start_epoch: int
    end_epoch: int
    exit_code: int | None  # None: the run was lost (killed before it could record its exit)

    @property
    def duration_s(self) -> int:
        return self.end_epoch - self.start_epoch


@dataclass(frozen=True)
class Plan:
    spec: FreonSpec  # shared by every run; spec_for() gives each run its own bucket and key prefix
    runs: int
    grace_s: int     # pause between runs, so one run's tail (compactions, GC) doesn't bleed into the next
    cleanup: bool
    answers: dict[str, object]  # flags that reproduce these answers

    warmup_keys: int  # keys of the unreported warm-up run before run 1; 0 = none

    def spec_for(self, index: int) -> FreonSpec:
        name = run_name(index)
        spec = dataclasses.replace(self.spec, bucket=f"{self.spec.bucket}-{name}", prefix=f"{self.spec.prefix}-{name}")
        if index == 0:
            spec = dataclasses.replace(spec, keys=self.warmup_keys, threads=min(spec.threads, self.warmup_keys))
        return spec


def run_name(index: int) -> str:
    """Folder, bucket and prefix suffix of a run: 'warmup' for index 0, else 'run-<index>'."""
    return "warmup" if index == 0 else f"run-{index}"


def ask(p: Prompter, args: Args, d: Discovered) -> Plan:
    p.section("Freon settings")
    spec, answers = _ask_spec(p, args, d)
    runs = p.ask("Number of runs", _positive("runs"), default="3", preset=args.runs, flag="--runs")
    grace = p.ask("Grace period between runs (e.g. 2m, 300s)", duration.check("grace period"), default="5m",
                  preset=args.grace, flag="--grace") if runs > 1 else 0
    cleanup = args.cleanup or p.confirm(f"Delete volume /{spec.volume} after the last run?", default=False)
    answers |= {"runs": runs, "grace": duration.format(grace) if runs > 1 else None, "cleanup": cleanup}
    try:
        warmup = _positive("warm-up keys", minimum=0)(args.warmup_keys).value
    except Invalid as e:
        raise Abort(f"--warmup-keys: {e}") from None
    answers["warmup_keys"] = None if warmup == DEFAULT_WARMUP_KEYS else warmup
    plan = Plan(spec, runs, grace, cleanup, answers, warmup)
    series = (f"{runs} runs, {duration.format(grace)} apart, buckets {spec.bucket}-run-1..{runs}" if runs > 1
              else "1 run")
    if warmup:
        series = f"a {warmup}-key warm-up into {spec.bucket}-warmup, then {series}"
    p.say(f"\n  {series}, on {d.discovery.freon_host}; run 1:\n"
          f"    ozone freon {shlex.join(plan.spec_for(1).argv())}\n")
    return plan


def execute(p: Prompter, d: Discovered, plan: Plan, index: int, jvm_opts: str = "") -> FreonRun:
    """Runs one Freon of the series (index 0: the warm-up) and copies its log to freon/<run name>/.
    jvm_opts is added to the Freon JVM's OZONE_OPTS, e.g. the async-profiler agent (empty for the warm-up)."""
    spec, host, name = plan.spec_for(index), d.discovery.freon_host, run_name(index)
    p.section(f"Freon warm-up, {spec.keys} keys (not reported)" if index == 0
              else f"Freon run {index}/{plan.runs}")
    command = "ozone freon " + shlex.join(spec.argv())
    remote, kerberos = d.remote, d.discovery.kerberos
    ccache = _ccache(d)
    if kerberos:  # a fresh ticket, so a long run doesn't outlive it
        _remote(lambda: probes.kinit(remote, host, kerberos.keytab, kerberos.principal), "kinit")
    remote_dir = f"{REMOTE_BASE}/{d.run_dir.name}/{name}"
    session = "opp-" + re.sub(r"[.:]", "-", f"{d.run_dir.name}-{name}")  # tmux names can't hold '.' or ':'
    # region agent log
    hostcpu = _hostcpu_start(d, name)
    # endregion
    jvm_opts = jvm_opts.replace("{dir}", remote_dir)
    out = _remote(lambda: remote.run(host, _START, remote_dir, session, ccache, jvm_opts, *spec.argv()),
                  "starting Freon")
    launch = Launch.parse(out, session)
    target = f"{remote.user}@{host}"
    p.ok(f"Freon started on {short(host)} with {launch.launcher} (PID {launch.pid}), output in {remote_dir}")
    if launch.launcher == "setsid":
        p.say(p.dim("    tmux is not installed there, so it runs detached with setsid; it survives ssh drops either way"))
    p.say(f"    watch it: {launch.watch(target, remote_dir)}")
    d.run_dir.update_manifest(f"freon-{name}", {"spec": spec, "host": host, "remote_dir": remote_dir, "launch": launch,
                                        "command": command})
    try:
        state = _follow(p, remote, host, remote_dir)
    except KeyboardInterrupt:
        # region agent log
        _hostcpu_stop(d, name, hostcpu)
        # endregion
        p.say()
        if p.interactive and p.confirm(f"Stop Freon on {short(host)} now?", default=True):
            try:
                remote.run(host, launch.stop_script(), timeout=30)
                p.ok(f"stopped Freon on {short(host)}")
            except RemoteError as e:
                p.warn(f"stopping Freon failed ({e}); stop it with: {launch.stop(target)}")
        else:
            p.warn(f"Freon keeps running on {short(host)}. Stop it with: {launch.stop(target)}")
        raise
    result = _collect(p, d, index, host, remote_dir, command, spec, launch)
    # region agent log
    _hostcpu_stop(d, name, hostcpu)
    # endregion
    p.ok(f"Freon {'warm-up' if index == 0 else f'run {index}'} completed in {result.duration_s}s, "
         f"{_describe_exit(state)}")
    if FREON_RECORDING in jvm_opts:
        _collect_recording(p, d, host, remote_dir, name)
    return result


FREON_RECORDING = "freon.jfr"  # written by the async-profiler agent in the run's folder on the Freon host


def _collect_recording(p: Prompter, d: Discovered, host: str, remote_dir: str, name: str) -> None:
    """Copies the Freon JVM's async-profiler recording to <run folder>/profiler/async-profiler/<run>/freon-<host>.jfr,
    next to the server recordings of the same run."""
    local = d.run_dir.path / "profiler" / "async-profiler" / name / f"freon-{short(host)}.jfr"
    try:
        d.remote.fetch(host, f"{remote_dir}/{FREON_RECORDING}", local)
    except RemoteError as e:
        p.warn(f"no Freon recording copied for {name}: {e}")
        return
    p.ok(f"copied the Freon client's recording to {local} ({local.stat().st_size / 1e6:,.1f} MB)")


def wait_grace(p: Prompter, plan: Plan, next_index: int) -> None:
    resume = time.strftime("%H:%M:%S", time.localtime(time.time() + plan.grace_s))
    p.say(f"\n  grace period: run {next_index} starts in {duration.format(plan.grace_s)} (at {resume})")
    time.sleep(plan.grace_s)


def cleanup(p: Prompter, d: Discovered, plan: Plan) -> None:
    if plan.cleanup:
        _delete_volume(p, d.remote, d.discovery.freon_host, _ccache(d), plan.spec.volume)


def _ccache(d: Discovered) -> str:
    return d.discovery.kerberos.ccache if d.discovery.kerberos else ""


# --- questions ----------------------------------------------------------------------------------------------------

def _ask_spec(p: Prompter, args: Args, d: Discovered) -> tuple[FreonSpec, dict[str, object]]:
    datanodes = len(d.discovery.topology.of_type("OZONE_DATANODE"))
    default_volume = _ozone_name(f"opp-{d.run_dir.name}")
    threads = p.ask("Threads", _positive("threads"), default="10", preset=args.threads, flag="--threads")
    keys = p.ask("Keys to write", _positive("keys"), default="100000", preset=args.keys, flag="--keys")
    size_raw, size = p.ask("Key size", _size, default="1KB", preset=args.size, flag="--size")
    replication = p.choose("Replication", [Option(r, f"{r:<16} needs {n} datanodes", r)
                                           for r, n in REPLICATIONS.items()],
                           preset=args.replication, flag="--replication", validate=_fits(datanodes))
    volume = p.ask("Volume", _name("volume"), default=default_volume, preset=args.volume, flag="--volume")
    bucket = p.ask("Bucket", _name("bucket"), default=DEFAULT_BUCKET, preset=args.bucket, flag="--bucket")
    extra = p.ask("Extra ockg arguments", _shell_words, default="", preset=args.freon_args, flag="--freon-args")
    spec = FreonSpec(threads, keys, size, replication, volume, bucket, d.run_dir.name, extra)
    answers: dict[str, object] = {
        "threads": threads, "keys": keys, "size": size_raw, "replication": replication,
        "volume": None if volume == default_volume else volume,  # the default follows the new run id
        "bucket": None if bucket == DEFAULT_BUCKET else bucket,
        "freon_args": shlex.join(extra) or None,
    }
    return spec, answers


def _positive(what: str, minimum: int = 1) -> Callable[[str], Ok[int]]:
    def check(raw: str) -> Ok[int]:
        if not raw.isdigit() or int(raw) < minimum:
            raise Invalid(f"{what} must be a whole number >= {minimum}")
        return Ok(int(raw))
    return check


def _size(raw: str) -> Ok[tuple[str, int]]:
    m = _SIZE.fullmatch(raw.strip())
    if not m:
        raise Invalid("use a number with an optional unit: 512B, 1KB, 4MB, 1GB")
    size = int(m.group(1)) * _SIZE_UNITS[m.group(2).upper()]
    if size == 0:
        raise Invalid("size must be > 0")
    return Ok((raw.strip(), size), f"{size:,} bytes per key")


def _fits(datanodes: int) -> Callable[[str], Ok[str]]:
    def check(replication: str) -> Ok[str]:
        needed = REPLICATIONS[replication]
        if needed > datanodes:
            raise Invalid(f"{replication} needs {needed} datanodes, the cluster has {datanodes}")
        return Ok(replication, f"{replication} ({datanodes} datanodes available)")
    return check


def _name(what: str) -> Callable[[str], Ok[str]]:
    def check(raw: str) -> Ok[str]:
        if not _OZONE_NAME.fullmatch(raw):
            raise Invalid(f"{what} name: 3-63 chars of a-z, 0-9, '.', '-'; starts and ends with a letter or digit")
        return Ok(raw)
    return check


def _shell_words(raw: str) -> Ok[tuple[str, ...]]:
    try:
        return Ok(tuple(shlex.split(raw)))
    except ValueError as e:
        raise Invalid(f"cannot split into arguments: {e}") from None


def _ozone_name(raw: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", raw.lower()).strip("-.")[:63]


# --- running ------------------------------------------------------------------------------------------------------

def _remote(fn: Callable[[], str], what: str) -> str:
    try:
        return fn()
    except RemoteError as e:
        raise Abort(f"{what} failed: {e}") from None


def _follow(p: Prompter, remote: Remote, host: str, remote_dir: str) -> str:
    """Polls until Freon ends; prints each new progress line. Returns the final STATE."""
    last, failures = "", 0
    while True:
        time.sleep(POLL_S)
        try:
            out = remote.run(host, _STATUS, remote_dir, timeout=60)
        except RemoteError as e:
            failures += 1
            if failures >= MAX_POLL_FAILURES:
                raise Abort(f"lost contact with {short(host)}: {e}") from None
            p.warn(f"status check failed ({failures}/{MAX_POLL_FAILURES}): {e}")
            continue
        failures = 0
        lines = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
        if (progress := lines.get("LAST", "").strip()) and progress != last:
            p.say(f"    {progress}")
            last = progress
        if (state := lines.get("STATE", "lost")) != "running":
            return state


def _describe_exit(state: str) -> str:
    code = state.removeprefix("done ").strip()
    return {"0": "exit 0",
            "lost": "but its exit status was lost"}.get(code, f"exit {code}")


def _collect(p: Prompter, d: Discovered, index: int, host: str, remote_dir: str, command: str, spec: FreonSpec,
             launch: Launch) -> FreonRun:
    remote = d.remote
    start, end, code = _remote(lambda: remote.run(host, _TIMES, remote_dir), "reading the run times").split()
    log = _remote(lambda: remote.run(host, 'cat "$1/freon.log"', remote_dir, timeout=300), "copying freon.log")
    path = d.run_dir.write_text(f"freon/{run_name(index)}/freon.log", log)
    p.ok(f"copied {path} ({len(log):,} bytes)")
    result = FreonRun(index, spec, host, remote_dir, command, launch, int(start), int(end),
                      int(code) if code.lstrip("-").isdigit() else None)
    d.run_dir.update_manifest(f"freon-{run_name(index)}", result)
    return result


def _delete_volume(p: Prompter, remote: Remote, host: str, ccache: str, volume: str) -> None:
    try:
        remote.run(host, _DELETE_VOLUME, ccache, volume, timeout=1800)
    except RemoteError as e:
        p.warn(f"deleting /{volume} failed: {e}")
        return
    p.ok(f"deleted volume /{volume}")
