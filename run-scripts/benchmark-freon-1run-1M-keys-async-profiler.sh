#!/usr/bin/env zsh


# keep the Mac awake (no idle or system sleep, so the VPN and ssh stay up) until this script exits
caffeinate -dims -w $$ &

PYTHONPATH=/Volumes/cloudera/ozone-perf-profiler \
/Volumes/cloudera/ozone-perf-profiler/.venv/bin/python -m ozone_perf_profiler \
  --cm-url=https://ve1125.halxg.cloudera.com:7183 \
  --cm-user=admin \
  --insecure \
  --cluster='Cluster 1' \
  --service=OZONE-1 \
  --ssh-user=root \
  --freon-host=ve1127.halxg.cloudera.com \
  --kerberos-keytab=/cdep/keytabs/om.keytab \
  --kerberos-principal=om@HALXG.CLOUDERA.COM \
  --threads=10 \
  --keys=1000000 \
  --size=1KB \
  --replication=RATIS/THREE \
  --runs=1 \
  --grace=5m \
  --warmup-keys=20000 \
  --prometheus-url=https://ve1342.halxg.cloudera.com:9096 \
  --yes
