"""Studies built on ozone_perf_profiler: run Freon once per cluster configuration, set up through Cloudera Manager.

utils.py has the building blocks (setup_cluster, run_freon, write_summary and the comparison STRATEGY); a study such
as tracing.py is its list of configurations and a short loop over them.
"""
