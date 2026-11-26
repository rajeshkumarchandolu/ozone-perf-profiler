"""The collated outputs of a run: report.json (everything) and summary.xlsx (to share, see summary.py)."""

from __future__ import annotations

import math
from dataclasses import asdict
from pathlib import Path

from .discover import Discovered
from .freon import Plan
from .freon_output import FreonResult
from .manifest import utc_stamp
from .profiling import ProfilePlan, Recording
from .prompts import Prompter
from .remote import short
from .telemetry import Source, Value


def write(p: Prompter, d: Discovered, plan: Plan, results: list[FreonResult], source: Source,
          values: list[Value], profile: ProfilePlan | None, recordings: list[Recording]) -> Path:
    p.section("Report")
    path = d.run_dir.write_json("report.json", _report(d, plan, results, source, values, profile, recordings))
    p.ok(f"report written: {path}")
    try:
        from . import summary  # needs pandas + openpyxl; without them the run still ends with report.json
        xlsx = summary.write(d.run_dir.path / "summary.xlsx", d, plan, results, source, values,
                             profiling_summary(profile, recordings))
        p.ok(f"summary written: {xlsx}")
    except ImportError as e:
        p.fail(f"summary.xlsx skipped: {e.name} is not installed (uv sync, or pip install pandas openpyxl)")
    return path


def profiling_summary(profile: ProfilePlan | None, recordings: list[Recording]) -> str:
    """One line for the Cluster tab, e.g. 'async-profiler 4.6 cpu,alloc,lock,wall, 600s: 15/15 recordings'."""
    if not profile:
        return "none"
    copied = sum(bool(r.local_file) for r in recordings)
    return f"{profile.settings}, {profile.period_s}s: {copied}/{len(recordings)} recordings in profiler/{profile.profiler}/run-N/"


def _report(d: Discovered, plan: Plan, results: list[FreonResult], source: Source, values: list[Value],
            profile: ProfilePlan | None, recordings: list[Recording]) -> dict[str, object]:
    found, topo = d.discovery, d.discovery.topology
    telemetry = {m.name: {"query": m.query, "values": _by_series(values, m.name)} for m in source.metrics}
    return {
        "run_name": d.run_dir.name,
        "created": utc_stamp(),
        "cluster": {"cm_url": topo.cm_url, "cluster": topo.cluster.display_name, "service": topo.service.name,
                    "ozone_version": found.ozone_version, "security": asdict(found.security),
                    "om_leader": found.om_leader, "scm_leader": found.scm_leader, "freon_host": found.freon_host,
                    "roles": _roles(d)},
        "freon": {"spec": asdict(plan.spec), "runs": plan.runs, "grace_s": plan.grace_s,
                  "warmup_keys": plan.warmup_keys, "results": [{"ok": r.ok, **asdict(r)} for r in results]},
        "telemetry": {"prometheus_url": source.client.base_url, "metrics": telemetry},
        "profiling": {"profiler": profile.profiler, "settings": profile.settings, "period_s": profile.period_s,
                      "recordings": [asdict(r) for r in recordings]} if profile else None,
    }


def _by_series(values: list[Value], metric: str) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    for v in values:
        if v.metric == metric:
            out.setdefault(v.series, {})[v.run] = None if math.isnan(v.value) else v.value
    return out


def _roles(d: Discovered) -> dict[str, list[str]]:
    roles: dict[str, list[str]] = {}
    for role in d.discovery.topology.roles:
        roles.setdefault(role.short, []).append(short(role.host))
    return roles
