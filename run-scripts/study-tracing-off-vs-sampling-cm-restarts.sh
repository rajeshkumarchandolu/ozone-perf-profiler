#!/usr/bin/zsh


# keep the Mac awake (no idle or system sleep, so the VPN and ssh stay up) until this script exits
caffeinate -dims -w $$ &

# wait before starting (default 2h); START_DELAY_S=0 zsh study-tracing-off-vs-sampling-cm-restarts.sh starts right away
# START_DELAY_S=${START_DELAY_S:-7200}
# echo "waiting $((START_DELAY_S / 60)) min, the study starts at $(date -v+${START_DELAY_S}S +%H:%M)"
# sleep "$START_DELAY_S"

PYTHONPATH=/Volumes/cloudera/ozone-perf-profiler \
/Volumes/cloudera/ozone-perf-profiler/.venv/bin/python -m usecases.tracing \
  --cm-url=https://ve1125.halxg.cloudera.com:7183 \
  --cm-user=admin \
  --insecure \
  --cluster='Cluster 1' \
  --service=OZONE-1 \
  --ssh-user=root \
  --freon-host=ve1127.halxg.cloudera.com \
  --kerberos-keytab=/cdep/keytabs/om.keytab \
  --kerberos-principal=om@HALXG.CLOUDERA.COM \
  --om-leader=ve1128.halxg.cloudera.com \
  --scm-leader=ve1126.halxg.cloudera.com \
  --threads=10 \
  --keys=1000000 \
  --size=1KB \
  --replication=RATIS/THREE \
  --runs=1 \
  --grace=2m \
  --config-pause=5m \
  --warmup-keys=20000 \
  --prometheus-url=https://ve1342.halxg.cloudera.com:9096 \
  --yes
