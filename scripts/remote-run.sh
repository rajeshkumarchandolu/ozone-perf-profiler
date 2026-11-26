#!/usr/bin/env bash
# Runs an opp study on a cluster host, detached from this machine, then copies the results back here.
#
#   scripts/remote-run.sh start [--no-sync] [--module M] [-- <more opp flags>]   sync, launch, poll, fetch, print
#   scripts/remote-run.sh resume [JOB]    poll again after an interruption (default: the latest job), then fetch
#   scripts/remote-run.sh status [JOB]    one look: running or finished, and the last log line
#   scripts/remote-run.sh stop   [JOB]    kill the remote run (its whole process group)
#   scripts/remote-run.sh list            the jobs started from here
#
# The run lives on the remote host (setsid nohup), so a dropped connection or a closed laptop only stops the
# polling; `resume` picks it up again. When it ends, its output folder lands in .output/ here, the log next to it,
# and every summary.xlsx is printed.
#
#   The opp flags are in FLAGS below; edit them there. --yes and --out-dir are always added.
#   M         python module to run: usecases.tracing (default, MODULE_DEFAULT) or ozone_perf_profiler (one opp run)
#   e.g.      start -- --keys=20000 --runs=1 --profiler=none      a short smoke test with the same cluster flags
#
# Every setting is in the block below. Only the passwords come from the environment:
#   CM_PWD    CM password for opp (asked when missing)
#   SSH_PWD   root password on the cluster hosts, for this script and for opp (falls back to $SSHPW;
#             empty = key-based ssh)
# They go to the remote process on stdin and live only in its environment: never on a command line, never in a file.
set -euo pipefail

# === settings: the one place to change values ================================================================
HOST=ve1342.halxg.cloudera.com        # runs the study (the Freon host)
SSH_USER=root
POLL_S=60                             # seconds between polls
MODULE_DEFAULT=usecases.tracing       # or ozone_perf_profiler for one opp run
# Same absolute path as on this Mac: rerun.sh files and freon.py's debug log refer to it.
REMOTE_REPO=/Volumes/cloudera/ozone-perf-profiler
# opp flags for every `start`; `start -- <flags>` appends more, and for a repeated flag the last one wins.
FLAGS=(
  --cm-url=https://ve1125.halxg.cloudera.com:7183
  --cm-user=admin
  --insecure
  "--cluster=Cluster 1"
  --service=OZONE-1
  --ssh-user=root
  --freon-host=ve1128.halxg.cloudera.com  # DataNode-only host, its DataNode decommissioned for the client
  --isolate-freon-host                    # usecases.tracing only (drop it with --module ozone_perf_profiler)
  --kerberos-keytab=/cdep/keytabs/om.keytab
  --kerberos-principal=om@HALXG.CLOUDERA.COM
  --om-leader=ve1127.halxg.cloudera.com   # same OM leader for every config: ve1127 has only OM + DN
                                          # (ve1129 also has SCM + Recon; ve1342 has DN, Prometheus)
  --scm-leader=ve1126.halxg.cloudera.com  # SCM + DN only, and not the OM leader's host
  --threads=10
  --keys=1000000
  --size=1KB
  --replication=RATIS/THREE
  --runs=2
  --grace=2m
  --warmup-keys=20000
  --profiler=async-profiler
  --asprof-events=cpu,alloc
  --prometheus-url=https://ve1342.halxg.cloudera.com:9096
)
# =============================================================================================================

SSH_PWD=${SSH_PWD:-${SSHPW:-}}
LOCAL_REPO=$(cd "$(dirname "$0")/.." && pwd)
JOBS_DIR=$LOCAL_REPO/.output/remote-jobs

say() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { say "✘ $*" >&2; exit 1; }

# --- ssh: one ControlMaster connection, password through SSH_ASKPASS -------------------------------------------
SSH_TMP=$(mktemp -d /tmp/opp-rr.XXXXXX)
cleanup() {
  ssh -o ControlPath="$SSH_TMP/cm" -O exit "$SSH_USER@$HOST" >/dev/null 2>&1 || true
  rm -rf "$SSH_TMP"
}
trap cleanup EXIT
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=$SSH_TMP/cm" -o ControlPersist=10m
          -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15
          -o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o LogLevel=ERROR)
if [[ -n $SSH_PWD ]]; then
  printf '#!/bin/sh\nprintf "%%s\\n" "$OPP_RR_PWD"\n' > "$SSH_TMP/askpass"
  chmod 700 "$SSH_TMP/askpass"
  export SSH_ASKPASS="$SSH_TMP/askpass" SSH_ASKPASS_REQUIRE=force OPP_RR_PWD="$SSH_PWD" DISPLAY=${DISPLAY:-:0}
  SSH_OPTS+=(-o NumberOfPasswordPrompts=1)
else
  SSH_OPTS+=(-o BatchMode=yes)
fi
rsh() { ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST" "$@" < /dev/null; }
rsh_in() { ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST" "$@"; }   # keeps stdin

# --- jobs -------------------------------------------------------------------------------------------------------
job_file() { echo "$JOBS_DIR/$1.env"; }
latest_job() { { ls -1t "$JOBS_DIR"/*.env 2>/dev/null || true; } | head -1 | sed -e 's#.*/##' -e 's/\.env$//'; }
load_job() {
  JOB=${1:-$(latest_job)}
  [[ -n $JOB && -f $(job_file "$JOB") ]] || die "no such job: ${1:-<none started yet>} (see: $0 list)"
  # shellcheck disable=SC1090
  source "$(job_file "$JOB")"
}

sync_code() {
  say "syncing $LOCAL_REPO to $HOST:$REMOTE_REPO"
  # COPYFILE_DISABLE: no macOS ._ files. The remote venv is created once, and dependencies installed when missing.
  COPYFILE_DISABLE=1 tar -czf - -C "$(dirname "$LOCAL_REPO")" \
      --exclude=.venv --exclude=.output --exclude=analyze-overhead --exclude='*.zip' --exclude=.git \
      --exclude=.idea --exclude=__pycache__ --exclude='*.egg-info' "$(basename "$LOCAL_REPO")" |
    rsh_in "set -e; mkdir -p $(dirname "$REMOTE_REPO"); tar -xzf - --warning=no-unknown-keyword -C $(dirname "$REMOTE_REPO")
            cd $REMOTE_REPO && mkdir -p .cursor
            [[ -x .venv/bin/python ]] || python3.12 -m venv .venv
            .venv/bin/python -c 'import pandas, openpyxl' 2>/dev/null || .venv/bin/pip install -q -e ."
  say "✔ code and venv ready on $HOST"
}

start() {
  local sync=1 module=$MODULE_DEFAULT
  while [[ $# -gt 0 ]]; do
    case $1 in
      --no-sync) sync=0; shift ;;
      --module) module=$2; shift 2 ;;
      --) shift; break ;;
      *) die "unknown option $1 (opp flags go after --)" ;;
    esac
  done
  if [[ -z ${CM_PWD:-} ]]; then read -rsp "CM password: " CM_PWD; echo; fi
  (( sync )) && sync_code
  JOB=$(date +%Y%m%d-%H%M%S)
  JOB_DIR=$REMOTE_REPO/.output/remote-jobs/$JOB
  local args
  args=$(printf '%q ' "${FLAGS[@]}" "$@")
  # setsid: its own session and process group (pgid = pid), so it survives this ssh and `stop` can kill all of it.
  PID=$(printf '%s\n%s\n' "$CM_PWD" "$SSH_PWD" | rsh_in "set -e; read -r CM_PWD; read -r SSH_PWD; export CM_PWD SSH_PWD
      cd $REMOTE_REPO; mkdir -p $JOB_DIR
      setsid nohup bash -c '.venv/bin/python -m $module $args --yes --out-dir=$JOB_DIR/out; echo \$? > $JOB_DIR/exit_code' \
          < /dev/null > $JOB_DIR/run.log 2>&1 &
      echo \$! | tee $JOB_DIR/pid")
  mkdir -p "$JOBS_DIR"
  printf 'JOB=%q\nHOST=%q\nSSH_USER=%q\nMODULE=%q\nJOB_DIR=%q\nPID=%q\n' \
      "$JOB" "$HOST" "$SSH_USER" "$module" "$JOB_DIR" "$PID" > "$(job_file "$JOB")"
  say "✔ started job $JOB on $HOST: python -m $module (pid $PID), log $JOB_DIR/run.log"
  poll_and_fetch
}

# Prints "<exit code or ->|<alive|dead>|<last log line>".
remote_state() {
  rsh "cat $JOB_DIR/exit_code 2>/dev/null || echo -; kill -0 $PID 2>/dev/null && echo alive || echo dead
       sed -e 's/\x1b\[[0-9;]*[A-Za-z]//g' $JOB_DIR/run.log 2>/dev/null | tr '\r' '\n' | grep -v '^[[:space:]]*$' | tail -1" |
    paste -sd'|' -
}

poll_and_fetch() {
  trap 'echo; say "polling stopped; the run goes on on $HOST. Continue with: $0 resume $JOB"; exit 130' INT
  say "polling every ${POLL_S}s (Ctrl-C only stops the polling)"
  local last="" state code alive line fails=0
  while :; do
    if state=$(remote_state 2>/dev/null); then
      fails=0
      IFS='|' read -r code alive line <<< "$state"
      [[ $line != "$last" ]] && { say "  $line"; last=$line; }
      if [[ $code != "-" ]]; then
        if [[ $code == 0 ]]; then say "✔ finished (exit 0)"; else say "✘ finished with exit code $code"; fi
        break
      fi
      if [[ $alive == dead ]]; then say "✘ the run died without an exit code (killed?); fetching what there is"; break; fi
    else
      fails=$((fails + 1))
      say "  $HOST not reachable (attempt $fails), retrying in ${POLL_S}s"
    fi
    sleep "$POLL_S"
  done
  trap - INT
  fetch
}

fetch() {
  mkdir -p "$LOCAL_REPO/.output"
  local folders f
  folders=$(rsh "ls -1 $JOB_DIR/out 2>/dev/null" || true)
  [[ -n $folders ]] || die "no output in $HOST:$JOB_DIR/out; see the log: $0 status $JOB"
  say "copying $HOST:$JOB_DIR/out/* to $LOCAL_REPO/.output/"
  rsh "tar -czf - -C $JOB_DIR/out ." | tar -xzf - -C "$LOCAL_REPO/.output"
  for f in $folders; do
    rsh "cat $JOB_DIR/run.log" > "$LOCAL_REPO/.output/$f/remote-run.log" || true
    say "✔ results: $LOCAL_REPO/.output/$f"
  done
  echo "FETCHED=$(printf '%q' "$folders")" >> "$(job_file "$JOB")"
  for f in $folders; do
    find "$LOCAL_REPO/.output/$f" -name summary.xlsx | sort | while read -r x; do print_summary "$x"; done
  done
}

print_summary() {
  say "summary: $1"
  local py=$LOCAL_REPO/.venv/bin/python
  [[ -x $py ]] || py=python3
  "$py" - "$1" <<'EOF'
import sys
import pandas as pd
for name, df in pd.read_excel(sys.argv[1], sheet_name=None, header=None).items():
    print(f"\n=== {name} ===")
    print(df.fillna("").to_string(index=False, header=False))
EOF
}

status() {
  load_job "${1:-}"
  local code alive line
  IFS='|' read -r code alive line <<< "$(remote_state)"
  if [[ $code != "-" ]]; then say "job $JOB on $HOST: finished, exit $code"
  else say "job $JOB on $HOST: $alive (pid $PID)"; fi
  say "  $line"
  [[ -n ${FETCHED:-} ]] && say "  fetched: $FETCHED"
  return 0
}

case ${1:-} in
  start) shift; start "$@" ;;
  resume) load_job "${2:-}"; poll_and_fetch ;;
  status) status "${2:-}" ;;
  stop) load_job "${2:-}"; rsh "kill -TERM -- -$PID" && say "✔ sent TERM to process group $PID on $HOST" ;;
  list) for f in $(ls -1t "$JOBS_DIR"/*.env 2>/dev/null || true); do (source "$f"; echo "$JOB  $HOST  $MODULE  pid $PID  ${FETCHED:+fetched: $FETCHED}"); done ;;
  *) sed -n '2,24p' "$0"; exit 2 ;;
esac
