"""summary.xlsx, to share: 'Results' (every value side by side, one column per run) and 'Cluster' tabs."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import telemetry
from .discover import Discovered
from .freon import Plan
from .freon_output import FreonResult
from .jfr import Recording
from .remote import short
from .telemetry import Source, Value


def write(path: Path, d: Discovered, plan: Plan, results: list[FreonResult], source: Source,
          values: list[Value], recordings: list[Recording]) -> Path:
    with pd.ExcelWriter(path) as xl:
        _results(d, results, source, values).to_excel(xl, sheet_name="Results", index=False, freeze_panes=(1, 2))
        _cluster(d, plan, source, recordings).to_excel(xl, sheet_name="Cluster", index=False, freeze_panes=(1, 1))
    return path


def _results(d: Discovered, results: list[FreonResult], source: Source, values: list[Value]) -> pd.DataFrame:
    """Freon first, then each service (OM, SCM, DN, ...) and inside it each node: a title row, its metric rows,
    a blank row between nodes and two between services."""
    runs = [r.run for r in results]
    queries = {m.name: m.query for m in source.metrics}
    blank = [""] * (len(runs) + 3)

    def row(title: str, metric: str = "", per_run: dict[str, object] | None = None, query: str = "") -> list[object]:
        return [title, metric, *(_round((per_run or {}).get(r)) for r in runs), query]

    rows = [row(f"Freon (client on {short(d.discovery.freon_host)})")]
    rows += [row("", label, {r.run: r.values().get(label) for r in results})
             for label in dict.fromkeys(label for r in results for label in r.values())]
    metrics = [m.name for m in source.metrics]
    for service in telemetry.by_node(values, metrics, d.discovery.om_leader):
        rows += [blank, blank, row(service.title)]
        for i, node in enumerate(service.nodes):
            rows += ([blank] if i else []) + [row(node.title)]
            rows += [row("", metric, per_run, queries.get(metric, "")) for metric, per_run in node.rows]
    return pd.DataFrame(rows, columns=["service / node", "metric", *runs, "PromQL"])


def _round(v: object) -> object:
    return round(v, 2) if isinstance(v, float) else v


def _cluster(d: Discovered, plan: Plan, source: Source, recordings: list[Recording]) -> pd.DataFrame:
    found, topo, spec = d.discovery, d.discovery.topology, plan.spec
    hosts: dict[str, list[str]] = {}
    for role in topo.roles:
        hosts.setdefault(f"{role.short} hosts", []).append(short(role.host))
    items = {
        "run name": d.run_dir.name, "Cloudera Manager": topo.cm_url, "cluster": topo.cluster.display_name,
        "cluster version": topo.cluster.version, "Ozone service": topo.service.name,
        "Ozone version": found.ozone_version, "security": found.security.describe(),
        "OM leader": found.om_leader or "-", "Prometheus": source.client.base_url,
        **{kind: ", ".join(names) for kind, names in hosts.items()},
        "Freon host": found.freon_host, "Freon threads": spec.threads, "keys per run": spec.keys,
        "key size bytes": spec.size_bytes, "replication": spec.replication, "volume": spec.volume,
        "runs": plan.runs, "grace period s": plan.grace_s, "warm-up keys": plan.warmup_keys,
        "extra ockg args": " ".join(spec.extra) or "-",
        "JFR recordings": (f"{sum(bool(r.local_file) for r in recordings)} in jfr/ "
                           f"({', '.join(sorted({r.service for r in recordings}))})" if recordings else "none"),
    }
    return pd.DataFrame(list(items.items()), columns=["item", "value"])
