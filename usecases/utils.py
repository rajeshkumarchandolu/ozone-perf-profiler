"""Building blocks for studies: set up the cluster through CM, run Freon with opp, compare the runs.

A study (see tracing.py) is a list of Config and a loop:
    for config in CONFIGS:
        setup_cluster(p, cm, config)              # CM: set, restart, wait until every role is FRESH
        results[config.name] = run_freon(args, folder, config)   # list[FreonResult]
    write_summary(folder, CONFIGS, results)        # comparison: one value per metric and config, via STRATEGY
"""

from __future__ import annotations

import dataclasses
import os
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from cmclient.cm import CmClient, CmCluster, CmError, CmService, normalize_url
from cmclient.http import Tls
from ozone_perf_profiler import duration, pipeline, probes
from ozone_perf_profiler.freon_output import FreonResult
from ozone_perf_profiler.options import CM_PWD_ENV, SSH_PWD_ENV, Args
from ozone_perf_profiler.prompts import Abort, Prompter
from ozone_perf_profiler.remote import Remote, RemoteError


@dataclass(frozen=True)
class Config:
    name: str                 # simple name: run folder suffix and summary.xlsx sheet name
    settings: dict[str, str]  # CM service-level parameters of the Ozone service


@dataclass(frozen=True)
class Cm:
    client: CmClient
    cluster: CmCluster
    service: CmService


# --- cluster setup ------------------------------------------------------------------------------------------------

def connect_cm(args: Args) -> Cm:
    client = CmClient(normalize_url(args.cm_url or ""), Tls(args.cm_ca_cert, args.insecure), args.cm_user or "admin",
                      os.environ.get(CM_PWD_ENV, ""))
    try:
        client.login()
        cluster = next(c for c in client.clusters() if c.name == args.cluster)
        service = next(s for s in client.ozone_services(cluster) if s.name == args.service)
    except StopIteration:
        raise Abort(f"cluster {args.cluster!r} / Ozone service {args.service!r} not found in CM") from None
    except CmError as e:
        raise Abort(f"Cloudera Manager: {e}") from None
    return Cm(client, cluster, service)


def setup_cluster(p: Prompter, cm: Cm, config: Config) -> None:
    """Puts the config in CM and restarts Ozone if needed; returns once every role is FRESH, which is CM's check that
    every process and every gateway's client config runs with the new values."""
    p.section(f"Configuration {config.name}")
    try:
        cm.client.apply_service_config(cm.cluster, cm.service, config.settings, say=lambda m: p.say(f"  {m}"))
    except CmError as e:
        raise Abort(f"Cloudera Manager: {e}") from None
    p.ok(f"{cm.service.name}: every role STARTED and FRESH with {config.settings}")


def pause(p: Prompter, seconds: int, reason: str) -> None:
    if seconds <= 0:
        return
    resume = time.strftime("%H:%M:%S", time.localtime(time.time() + seconds))
    p.say(f"\n  waiting {duration.format(seconds)} ({reason}), until {resume}")
    time.sleep(seconds)


# --- Freon ----------------------------------------------------------------------------------------------------------

def campaign_folder(args: Args, prefix: str) -> Path:
    """<out dir>/<prefix>-<Freon host time>/."""
    try:
        with Remote(args.ssh_user or "root", os.environ.get(SSH_PWD_ENV, "")) as remote:
            name = f"{prefix}-{probes.run_name(remote, args.freon_host)}"
    except RemoteError as e:
        raise Abort(f"cannot reach the Freon host {args.freon_host}: {e}") from None
    folder = args.out_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def run_freon(args: Args, folder: Path, config: Config) -> list[FreonResult]:
    """One opp run (warm-up + the measured runs) into <folder>/<folder name>-<config>/."""
    return pipeline.run(dataclasses.replace(args, out_dir=folder, run_name=f"{folder.name}-{config.name}", yes=True))


# --- comparison -----------------------------------------------------------------------------------------------------

def _num(v: float) -> str:
    return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"


def RANGE(values: list[float]) -> str:
    return f"[{_num(min(values))}-{_num(max(values))}]"


def AVERAGE(values: list[float]) -> str:
    return _num(statistics.mean(values))


Strategy = Callable[[list[float]], str]
STRATEGY: Strategy = RANGE  # how the runs of one config become one value in the comparison


def _timer(stat: str) -> Callable[[FreonResult], float | None]:
    return lambda r: getattr(r.timers.get("key-create"), stat, None)


METRICS: dict[str, Callable[[FreonResult], float | None]] = {
    "time taken s (writing keys)": lambda r: r.write_s,
    "Freon total s (incl. shutdown)": lambda r: r.total_time_s,
    "keys/s (writing keys)": lambda r: r.successes / r.write_s if r.successes and r.write_s else None,
    "key-create mean ms": _timer("mean"),
    "key-create p50 ms": _timer("p50"),
    "key-create p95 ms": _timer("p95"),
    "key-create p99 ms": _timer("p99"),
    "key-create max ms": _timer("max"),
    "failures": lambda r: r.failures,
}


def compare(results: dict[str, list[FreonResult]], strategy: Strategy = STRATEGY) -> pd.DataFrame:
    """One row per metric, one column per config, each cell the strategy over that config's runs."""
    rows = []
    for metric, get in METRICS.items():
        row = {"metric": metric}
        for config, runs in results.items():
            values = [v for v in map(get, runs) if v is not None]
            row[config] = strategy(values) if values else "-"
        rows.append(row)
    return pd.DataFrame(rows)


def _config_sheet(config: Config, runs: list[FreonResult]) -> pd.DataFrame:
    """The config's settings on top, then every Freon value of every run (one column per run)."""
    rows: list[list] = [["Configuration"], *([k, v] for k, v in config.settings.items()), [], [],
                        ["Freon results", *(r.run for r in runs)]]
    rows += [[metric, *(get(r) for r in runs)] for metric, get in METRICS.items()]
    for label in dict.fromkeys(label for r in runs for label in r.values()):
        rows.append([label, *(r.values().get(label) for r in runs)])
    width = max(map(len, rows))
    return pd.DataFrame([r + [""] * (width - len(r)) for r in rows])


def write_summary(p: Prompter, folder: Path, configs: list[Config], results: dict[str, list[FreonResult]],
                  strategy: Strategy = STRATEGY) -> Path:
    table = compare(results, strategy)
    path = folder / "summary.xlsx"
    with pd.ExcelWriter(path) as xl:
        table.to_excel(xl, sheet_name="comparison", index=False)
        for config in configs:
            if config.name in results:
                _config_sheet(config, results[config.name]).to_excel(xl, sheet_name=config.name[:31], index=False,
                                                                     header=False)
    p.section(f"Comparison ({strategy.__name__.lower()} over the runs of each config)")
    p.table(list(table.columns), table.values.tolist())
    p.ok(f"summary written: {path}")
    return path
