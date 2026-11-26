#!/usr/bin/env bash
# Plain `ozone freon ockg`: 1,000,000 keys of 1KB, RATIS/THREE, 10 threads (the same workload as the opp scripts),
# with no opp, no Prometheus and no profiler. Run it on the Freon host, either:
#   caffeinate -dims ssh root@ve1127.halxg.cloudera.com 'bash -s' < freon-manual-ockg-1M-keys-on-freon-host.sh
# (caffeinate keeps the Mac awake, so the VPN and ssh stay up), or copy it there and run it.
set -euo pipefail

KEYTAB=/cdep/keytabs/om.keytab
PRINCIPAL=om@HALXG.CLOUDERA.COM
NAME=manual-$(date +%Y-%b-%d-%Hh%Mm%Ss | tr '[:upper:]' '[:lower:]')   # e.g. manual-2026-nov-22-09h15m03s

export KRB5CCNAME=FILE:/tmp/manual_freon_krb5cc   # own ticket cache, the default one is left alone
kinit -kt "$KEYTAB" "$PRINCIPAL"

mkdir -p "/tmp/manual-freon/$NAME"
cd "/tmp/manual-freon/$NAME"
echo "volume $NAME, log in $PWD/freon.log"

ozone freon ockg \
  -t 10 \
  -n 1000000 \
  -s 1024 \
  -v "$NAME" \
  -b freon \
  -p "$NAME" \
  --type RATIS \
  --replication THREE \
  2>&1 | tee freon.log

# To delete the keys afterwards:
#   KRB5CCNAME=FILE:/tmp/manual_freon_krb5cc ozone sh volume delete -r -y "$NAME"
