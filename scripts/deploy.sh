#!/bin/bash
# Repeatable Backlogr deploy for a host without GitHub access.
# Build a bundle where you have the repo, copy it to the host:
#   git bundle create /tmp/backlogr.bundle --all --tags
#   scp /tmp/backlogr.bundle "$DEPLOY_HOST:/tmp/"
# then on the host:  scripts/deploy.sh --bundle /tmp/backlogr.bundle [--ref vX.Y.Z] [--dry-run]
# Without --bundle it fetches from origin (works only if origin is reachable).
# --dry-run fetches refs but changes no files, DB or service.
# Default ref: highest v* tag after fetch (fallback origin/main).
# Env: BACKLOGR_HOME (install dir, default ~/backlogr), BACKLOGR_SERVICE (systemd --user unit, default backlogr).
# BACKLOGR_DB / BACKLOGR_PORT come from the environment, else $BACKLOGR_HOME/.env, else defaults.
# Migrations: the server applies its schema on start, so restart = migrate.
set -euo pipefail
DIR="${BACKLOGR_HOME:-$HOME/backlogr}"; SVC="${BACKLOGR_SERVICE:-backlogr}"; cd "$DIR"
BUNDLE=""; REF=""; DRY=0
while [ $# -gt 0 ]; do case "$1" in
  --bundle) BUNDLE="$2"; shift 2;; --ref) REF="$2"; shift 2;; --dry-run|-n) DRY=1; shift;;
  -h|--help) sed -n 2,11p "$0"; exit 0;; *) echo "unknown arg $1"; exit 2;; esac; done
envval(){ [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
DB="${BACKLOGR_DB:-$(envval BACKLOGR_DB)}"; DB=${DB:-$DIR/data/backlogr.db}
PORT="${BACKLOGR_PORT:-$(envval BACKLOGR_PORT)}"; PORT=${PORT:-8796}
PREV=$(git rev-parse HEAD); TS=$(date +%Y%m%d-%H%M%S)
echo "current: $(git describe --tags --always) ($PREV)"
[ -n "$(git status --porcelain --untracked-files=no)" ] && { echo "tracked files modified; aborting"; git status --short; exit 4; }
if [ -n "$BUNDLE" ]; then git bundle verify -q "$BUNDLE"; git fetch -q --tags "$BUNDLE" '+refs/heads/*:refs/remotes/origin/*'
else git fetch -q --tags origin || { echo "origin unreachable; use --bundle"; exit 3; }; fi
[ -z "$REF" ] && REF=$(git tag -l 'v*' --sort=-v:refname | head -1); [ -z "$REF" ] && REF=origin/main
NEW=$(git rev-parse "$REF^{commit}"); echo "target: $REF ($NEW)"
B="$DIR/backups/pre-deploy-$TS.db"
if [ "$DRY" = 1 ]; then echo "DRY: backup $DB -> $B"; else
mkdir -p "$DIR/backups"
python3 -c "import sqlite3,sys;s=sqlite3.connect(sys.argv[1]);d=sqlite3.connect(sys.argv[2]);s.backup(d);d.close();s.close()" "$DB" "$B"; chmod 600 "$B"; echo "backup: $B"; fi
code(){ curl -s --connect-timeout 2 -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" || true; }
health(){ for _ in $(seq 1 "${BACKLOGR_HEALTH_TRIES:-40}"); do [ "$(code)" = 200 ] && return 0; sleep 0.5; done; return 1; }
rollback(){
  echo "$1; rolling back to $PREV (DB backup kept at $B; schema changes are not reverted automatically)"
  git checkout -q --detach "$PREV"; .venv/bin/pip install -q -e . || true; systemctl --user restart "$SVC" || true
  if health; then echo "rolled back, healthy"; else echo "ROLLBACK UNHEALTHY: restore $B manually"; fi; exit 1; }
if [ "$DRY" = 1 ]; then
  echo "DRY: git checkout -q --detach $NEW"; echo "DRY: .venv/bin/pip install -q -e ."; echo "DRY: systemctl --user restart $SVC"
  echo "DRY: wait for healthz on :$PORT (now: $(code))"; exit 0; fi
git checkout -q --detach "$NEW"
.venv/bin/pip install -q -e . || rollback "pip install FAILED"
systemctl --user restart "$SVC" || rollback "restart FAILED"
if health; then echo "OK: $(git describe --tags --always) healthy"; exit 0; fi
rollback "healthz FAILED"
