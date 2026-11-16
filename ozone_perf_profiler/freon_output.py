"""Parse each run's freon.log into a FreonResult (freon.json); print all runs side by side.

Freon prints its Dropwizard metrics in ConsoleReporter form ("-- Timers --" blocks); the Slf4jReporter form
("type=TIMER, name=..., p99=...") is parsed too, so either reporter works. Times are normalised to milliseconds.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields

from .freon import FreonRun, run_name
from .manifest import RunDir
from .prompts import Prompter

_TO_MS = {"nanoseconds": 1e-6, "microseconds": 1e-3, "milliseconds": 1.0, "seconds": 1000.0}
# ConsoleReporter label (or Slf4jReporter key) -> TimerStats field
_FIELDS = {"count": "count", "mean rate": "mean_rate", "mean_rate": "mean_rate", "min": "min", "max": "max",
           "mean": "mean", "stddev": "stddev", "median": "p50", "p50": "p50", "75%": "p75", "p75": "p75",
           "95%": "p95", "p95": "p95", "98%": "p98", "p98": "p98", "99%": "p99", "p99": "p99",
           "99.9%": "p999", "p999": "p999"}
_RATE_FIELDS = {"count", "mean_rate"}
_CONSOLE_VALUE = re.compile(r"^\s+(.+?)\s*<?=\s*([-+0-9.eE]+)\s*(\S*)")
_SLF4J_TIMER = re.compile(r"type=TIMER,\s*name=([^,]+),(.*)")
_TOTALS = {
    "total_time_s": re.compile(r"Total execution time \(sec\):\s*(\d+)"),
    "failures": re.compile(r"Failures:\s*(\d+)"),
    "successes": re.compile(r"Successful executions:\s*(\d+)"),
}


@dataclass(frozen=True)
class TimerStats:
    count: int
    mean_rate: float | None  # per second
    min: float | None        # every latency in milliseconds
    mean: float | None
    max: float | None
    stddev: float | None
    p50: float | None
    p75: float | None
    p95: float | None
    p98: float | None
    p99: float | None
    p999: float | None

    @classmethod
    def of(cls, values: dict[str, float]) -> TimerStats:
        names = [f for f in cls.__dataclass_fields__ if f != "count"]
        return cls(int(values.get("count", 0)), *(values.get(n) for n in names))


@dataclass(frozen=True)
class FreonResult:
    """Everything one Freon run produced, as Freon printed it. The summary stage picks from this; nothing is
    dropped here. start/end are the host clock around the ozone process: phase 3 queries Prometheus over them."""
    run: str  # run-1, run-2, ... or warmup
    bucket: str
    start_epoch: int
    end_epoch: int
    exit_code: int | None
    total_time_s: int | None
    successes: int | None
    failures: int | None
    timers: dict[str, TimerStats] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and bool(self.successes) and self.failures == 0

    def values(self) -> dict[str, object]:
        """Every value under a readable label, timers flattened as '<timer> <stat> <unit>'."""
        out: dict[str, object] = {"status": "ok" if self.ok else "FAILED"}
        out |= {f.name.replace("_", " "): getattr(self, f.name) for f in fields(self)
                if f.name not in ("run", "timers")}  # run is the column header
        for name, timer in self.timers.items():
            for f in fields(timer):
                unit = "" if f.name == "count" else " /s" if f.name == "mean_rate" else " ms"
                out[f"{name} {f.name.replace('_', ' ')}{unit}"] = getattr(timer, f.name)
        return out


def report(p: Prompter, run_dir: RunDir, run: FreonRun) -> FreonResult:
    folder = f"freon/{run_name(run.index)}"
    text = (run_dir.path / folder / "freon.log").read_text(errors="replace").replace("\r", "\n")
    result = parse(text, run)
    run_dir.write_json(f"{folder}/freon.json", {"ok": result.ok, **asdict(result)})
    _print_status(p, result, text)
    return result


def series(p: Prompter, run_dir: RunDir, results: list[FreonResult]) -> None:
    """All values of all runs: one row per value, one column per run."""
    p.section(f"Freon results, {len(results)} run(s)")
    columns = [r.values() for r in results]
    labels = list(dict.fromkeys(label for column in columns for label in column))  # first-seen order
    p.table(("value", *(r.run for r in results)), [(label, *(_fmt(c.get(label), label) for c in columns))
                                                    for label in labels])
    run_dir.write_json("freon/series.json", [{"ok": r.ok, **asdict(r)} for r in results])
    p.ok(f"saved {run_dir.path}/freon/series.json")


def parse(text: str, run: FreonRun) -> FreonResult:
    totals = {k: _last_int(rx, text) for k, rx in _TOTALS.items()}
    return FreonResult(run=run_name(run.index), bucket=run.spec.bucket, start_epoch=run.start_epoch,
                       end_epoch=run.end_epoch, exit_code=run.exit_code, **totals,
                       timers={**_console_timers(text), **_slf4j_timers(text)})


def _fmt(v: object, label: str = "") -> str:
    if v is None:
        return "-"
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, int):
        return str(v) if label.endswith("epoch") else f"{v:,}"  # epochs stay copy-pasteable
    return f"{v:,.2f}"


def _last_int(rx: re.Pattern[str], text: str) -> int | None:
    found = rx.findall(text)
    return int(found[-1]) if found else None


def _console_timers(text: str) -> dict[str, TimerStats]:
    timers: dict[str, dict[str, float]] = {}
    in_timers, current = False, None
    for line in text.splitlines():
        if line.startswith("-- "):
            in_timers, current = line.startswith("-- Timers"), None
        elif not in_timers or not line.strip():
            continue
        elif not line[0].isspace():
            current = line.strip()
            timers[current] = {}
        elif current and (m := _CONSOLE_VALUE.match(line)) and m.group(1) in _FIELDS:
            name = _FIELDS[m.group(1)]
            timers[current][name] = _value(name, float(m.group(2)), m.group(3))
    return {name: TimerStats.of(values) for name, values in timers.items() if values}


def _slf4j_timers(text: str) -> dict[str, TimerStats]:
    timers = {}
    for m in _SLF4J_TIMER.finditer(text):
        pairs = dict(kv.strip().split("=", 1) for kv in m.group(2).split(",") if "=" in kv)
        unit = pairs.get("duration_unit", "milliseconds")
        values = {_FIELDS[k]: _value(_FIELDS[k], float(v), unit) for k, v in pairs.items()
                  if k in _FIELDS and re.fullmatch(r"[-+0-9.eE]+", v)}
        timers[m.group(1).strip()] = TimerStats.of(values)
    return timers


def _value(name: str, number: float, unit: str) -> float:
    return number if name in _RATE_FIELDS else number * _TO_MS.get(unit, 1.0)


def _print_status(p: Prompter, r: FreonResult, text: str) -> None:
    summary = (f"parsed the Freon run details of {r.run}: {_fmt(r.successes)} keys, {_fmt(r.failures)} failures, "
               f"Freon time {_fmt(r.total_time_s)}s")
    if r.ok:
        p.ok(summary)
        return
    reasons = [why for cond, why in ((r.exit_code != 0, f"exit {r.exit_code}"),
                                     (not r.successes, "no successful keys"),
                                     (bool(r.failures), f"{r.failures} failures")) if cond]
    p.fail(f"{summary}; did not succeed: {', '.join(reasons)}")
    p.say("  last lines of freon.log:")
    for line in [ln for ln in text.splitlines() if ln.strip()][-15:]:
        p.say(f"    {line}")
