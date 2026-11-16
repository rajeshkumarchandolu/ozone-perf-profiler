"""Telemetry: JVM metrics of every Ozone process from the cluster's Prometheus, one value per Freon run.

Each catalog entry is a PromQL query that must give one value per series: $window is replaced with Freon's
execution time for the run (e.g. 90s) and the query is evaluated as an instant query at the run's end, on the
Prometheus clock, so the aggregation over the run (avg_over_time, max_over_time, increase, ratios...) is written in
the query itself. A query that returns time series
(a range vector) or fails is dropped from the comparison. Adding a metric means adding one catalog line.

The Ozone Prometheus sink names Hadoop's JvmMetrics jvm_metrics_* and labels each series with processname and
hostname, so every query returns one value per Ozone process on each host. Node metrics are not collected yet.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from .discover import Discovered
from .freon_output import FreonResult
from .options import Args
from .prom import PromClient, PromError, Sample
from .prompts import Invalid, Ok, Prompter
from .remote import RemoteError, short

WINDOW = "$window"
MAX_SKEW_S = 5        # warn above this clock offset between the Freon host and Prometheus
SCRAPE_WAIT_S = 30   # the end of the last run must be scraped before it is queried
CHECK_WINDOW = "60s"  # used to test-run every query before the runs start


@dataclass(frozen=True)
class Metric:
    name: str
    query: str  # PromQL with $window; must give one value per series


CATALOG = (
    # memory, MB
    Metric("heap used MB, avg", "avg_over_time(jvm_metrics_mem_heap_used_m[$window])"),
    Metric("heap used MB, max", "max_over_time(jvm_metrics_mem_heap_used_m[$window])"),
    Metric("heap committed MB, max", "max_over_time(jvm_metrics_mem_heap_committed_m[$window])"),
    Metric("heap max MB", "max_over_time(jvm_metrics_mem_heap_max_m[$window])"),
    Metric("non-heap used MB, max", "max_over_time(jvm_metrics_mem_non_heap_used_m[$window])"),
    Metric("non-heap committed MB, max", "max_over_time(jvm_metrics_mem_non_heap_committed_m[$window])"),
    # GC, during the run
    Metric("GC count", "increase(jvm_metrics_gc_count[$window])"),
    Metric("GC time ms", "increase(jvm_metrics_gc_time_millis[$window])"),
    Metric("GC avg pause ms",
           "increase(jvm_metrics_gc_time_millis[$window]) / increase(jvm_metrics_gc_count[$window])"),
    Metric("GC young count", "increase(jvm_metrics_gc_count_g1_young_generation[$window])"),
    Metric("GC young time ms", "increase(jvm_metrics_gc_time_millis_g1_young_generation[$window])"),
    Metric("GC old count", "increase(jvm_metrics_gc_count_g1_old_generation[$window])"),
    Metric("GC old time ms", "increase(jvm_metrics_gc_time_millis_g1_old_generation[$window])"),
    Metric("GC pauses over warn threshold", "increase(jvm_metrics_gc_num_warn_threshold_exceeded[$window])"),
    Metric("GC pauses over info threshold", "increase(jvm_metrics_gc_num_info_threshold_exceeded[$window])"),
    Metric("GC extra sleep ms", "increase(jvm_metrics_gc_total_extra_sleep_time[$window])"),
    # threads
    Metric("threads runnable, avg", "avg_over_time(jvm_metrics_threads_runnable[$window])"),
    Metric("threads blocked, max", "max_over_time(jvm_metrics_threads_blocked[$window])"),
    Metric("threads waiting, avg", "avg_over_time(jvm_metrics_threads_waiting[$window])"),
    Metric("threads timed-waiting, avg", "avg_over_time(jvm_metrics_threads_timed_waiting[$window])"),
    # cpu and logs
    Metric("JVM CPU load, avg", "avg_over_time(jvm_metrics_cpu_jvm_load[$window])"),
    Metric("log errors", "increase(jvm_metrics_log_error[$window])"),
    Metric("log warnings", "increase(jvm_metrics_log_warn[$window])"),
)


@dataclass(frozen=True)
class Source:
    client: PromClient
    metrics: tuple[Metric, ...]  # the catalog entries that passed the check
    skew_s: float                # Freon host clock minus Prometheus clock; run times are shifted by it
    answers: dict[str, object]

    def prom_time(self, host_epoch: float) -> float:
        return host_epoch - self.skew_s


@dataclass(frozen=True)
class Value:
    """One metric of one series (an Ozone process on a host) in one run."""
    run: str
    metric: str
    query: str   # as evaluated, $window expanded
    at: float    # evaluation time: the run's end on the Prometheus clock (Unix epoch, UTC)
    series: str  # <processname>@<short host>, or the labels the query kept
    value: float


# --- before the runs: find Prometheus, test every query -----------------------------------------------------------

def ask(p: Prompter, args: Args, d: Discovered) -> Source:
    """Asked before Freon starts, so a wrong URL or a broken query shows up now and not after the runs."""
    default = d.discovery.topology.prometheus_url
    client = p.ask("Prometheus URL", _connect(d), default=default, preset=args.prometheus_url,
                   flag="--prometheus-url")
    metrics = _check_queries(p, client)
    skew = _clock_skew(p, d, client)
    answers = {"prometheus_url": None if client.base_url == default else client.base_url}
    return Source(client, metrics, skew, answers)


def _clock_skew(p: Prompter, d: Discovered, client: PromClient) -> float:
    """Run times come from the Freon host's clock; queries need Prometheus's. Measure the offset once."""
    host = d.discovery.freon_host
    try:
        before = client.now()
        host_now = float(d.remote.run(host, "date +%s", timeout=30).strip()) + 0.5  # date +%s truncates
        prom_now = (before + client.now()) / 2  # the ssh call sits between the two Prometheus reads
    except (PromError, RemoteError, ValueError) as e:
        p.warn(f"could not compare the clocks of {short(host)} and Prometheus ({e}); assuming they agree")
        return 0.0
    skew = round(host_now - prom_now)  # date +%s has 1s resolution
    message = f"clocks: {short(host)} is {skew:+.0f}s from Prometheus"
    if abs(skew) > MAX_SKEW_S:
        p.warn(f"{message}; run windows are shifted by it, but check NTP on the cluster")
    else:
        p.ok(message + ", run windows use the Prometheus clock")
    return skew


def _connect(d: Discovered) -> Callable[[str], Ok[PromClient]]:
    def check(raw: str) -> Ok[PromClient]:
        errors = []
        for url in candidate_urls(raw):
            client = PromClient(url, d.tls)
            try:
                version, targets, names = client.version(), client.targets(), client.metric_names()
            except PromError as e:
                errors.append(f"{url}: {e}")
                continue
            if not any(n.startswith("jvm_metrics_") for n in names):
                raise Invalid(f"{url} exports no jvm_metrics_*: is this the Ozone Prometheus?")
            d.run_dir.write_json("metrics/prom-inventory.json", {"url": url, "metric_names": names})
            up = sum(t.health == "up" for t in targets)
            jobs = ", ".join(sorted({t.job for t in targets}))
            note = f" (at {url})" if errors else ""
            return Ok(client, f"Prometheus {version}: {up}/{len(targets)} targets up ({jobs}){note}")
        raise Invalid("; ".join(errors))
    return check


def candidate_urls(raw: str) -> list[str]:
    """CM doesn't say whether the Prometheus port speaks TLS (it does under Auto-TLS): with no scheme try https
    first, and an http URL falls back to https."""
    host = raw.split("://", 1)[-1]
    if raw.startswith("https://"):
        return [raw]
    if raw.startswith("http://"):
        return [raw, f"https://{host}"]
    return [f"https://{host}", f"http://{host}"]


def _check_queries(p: Prompter, client: PromClient) -> tuple[Metric, ...]:
    """Runs every query once over the last minute; a failing or time-series query is dropped."""
    usable, no_data = [], []
    for m in CATALOG:
        try:
            samples = client.query(m.query.replace(WINDOW, CHECK_WINDOW))
        except PromError as e:
            p.fail(f"dropped '{m.name}': {e}")
            continue
        usable.append(m)
        if not samples:
            no_data.append(m.name)
    p.ok(f"{len(usable)}/{len(CATALOG)} metric queries usable")
    if no_data:
        p.warn(f"no data in the last minute (kept, shown as '-' if still empty): {', '.join(no_data)}")
    return tuple(usable)


# --- after the runs: one value per metric, series and run ---------------------------------------------------------

def collect(p: Prompter, d: Discovered, source: Source, results: list[FreonResult]) -> list[Value]:
    p.section(f"Collecting the telemetry information ({source.client.base_url})")
    wait = source.prom_time(max(r.end_epoch for r in results)) + SCRAPE_WAIT_S - _prom_now(source)
    if wait > 0:
        p.say(f"  waiting {wait:.0f}s for Prometheus to scrape the end of the last run")
        time.sleep(wait)
    values: list[Value] = []
    dropped: set[str] = set()
    for r in results:
        window, at = run_window(r), source.prom_time(r.end_epoch)
        if window < SCRAPE_WAIT_S:
            p.warn(f"{r.run}: a {window}s window holds few Prometheus samples")
        for m in source.metrics:
            if m.name in dropped:
                continue
            query = m.query.replace(WINDOW, f"{window}s")
            try:
                samples = source.client.query(query, at)
            except PromError as e:
                p.fail(f"dropped '{m.name}' ({r.run}): {e}")
                dropped.add(m.name)
                continue
            values += [Value(r.run, m.name, query, at, series_name(s), s.value) for s in samples]
    values = [v for v in values if v.metric not in dropped]  # partly collected metrics can't be compared
    d.run_dir.write_json("metrics/metrics.json", [asdict(v) for v in values])
    metrics = [m.name for m in source.metrics if m.name not in dropped]
    _print(p, by_node(values, metrics, d.discovery.om_leader), [r.run for r in results])
    p.ok(f"saved {d.run_dir.path}/metrics/metrics.json")
    return values


def run_window(r: FreonResult) -> int:
    """Freon's own 'Total execution time': the workload, without client JVM start-up and shutdown.
    Falls back to the host's start..end when Freon didn't print it."""
    return max(r.total_time_s or (r.end_epoch - r.start_epoch), 1)


def _prom_now(source: Source) -> float:
    try:
        return source.client.now()
    except PromError:
        return time.time()


def series_name(s: Sample) -> str:
    labels = s.labels
    if "processname" in labels:
        return f"{labels['processname']}@{short(labels.get('hostname') or labels.get('instance', '?'))}"
    kept = {k: v for k, v in labels.items() if k != "__name__"}
    return ",".join(f"{k}={v}" for k, v in sorted(kept.items())) or "(all)"


# --- grouping: service, then node -------------------------------------------------------------------------------

SERVICES = {"OzoneManager": "OM", "StorageContainerManager": "SCM", "HddsDatanode": "DN", "Recon": "Recon",
            "S3Gateway": "S3G"}  # processname -> title, in display order


@dataclass(frozen=True)
class NodeBlock:
    title: str  # e.g. "ve1127 (leader)"
    rows: list[tuple[str, dict[str, float]]]  # (metric, run -> value), in catalog order


@dataclass(frozen=True)
class ServiceBlock:
    title: str  # OM, SCM, DN, ...
    nodes: list[NodeBlock]


def by_node(values: list[Value], metrics: list[str], om_leader: str | None) -> list[ServiceBlock]:
    """Values grouped by service, then node (the OM leader first), each node's metrics in catalog order."""
    cells: dict[tuple[str, str], dict[str, dict[str, float]]] = {}
    for v in values:
        process, _, host = v.series.partition("@")
        cells.setdefault((process, host), {}).setdefault(v.metric, {})[v.run] = v.value
    order = list(SERVICES)
    leader = short(om_leader) if om_leader else None
    blocks = []
    for process in sorted({proc for proc, _ in cells}, key=lambda x: (order.index(x) if x in order else len(order), x)):
        hosts = sorted((h for proc, h in cells if proc == process), key=lambda h: (h != leader, h))
        nodes = [NodeBlock(_node_title(process, host, leader),
                           [(m, cells[(process, host)][m]) for m in metrics if m in cells[(process, host)]])
                 for host in hosts]
        blocks.append(ServiceBlock(SERVICES.get(process, process), nodes))
    return blocks


def _node_title(process: str, host: str, om_leader: str | None) -> str:
    if process == "OzoneManager" and om_leader:
        return f"{host} ({'leader' if host == om_leader else 'follower'})"
    return host or "(all)"


def _print(p: Prompter, blocks: list[ServiceBlock], runs: list[str]) -> None:
    for service in blocks:
        p.say(f"\n  {service.title}")
        for node in service.nodes:
            p.say(f"\n    {node.title}")
            p.table(("metric", *runs), [(metric, *(_fmt(per_run.get(r)) for r in runs)) for metric, per_run in node.rows],
                    indent=6)


def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    if math.isnan(v):
        return "NaN"
    return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
