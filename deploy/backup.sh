#!/bin/sh
# Daily PostgreSQL backup for the agent stack (jobs, results, cost ledger, feedback, Payment Service).
# Keeps the last 7 dumps. The Payment Service dump contains wallet secrets encrypted with
# ENCRYPTION_KEY: store backups as carefully as the key, and never next to it.
#
# Runs every 24 h in the compose `backup` service (PGHOST set: pg_dump talks to postgres directly).
# By hand on the server, from the repo root (uses `docker compose exec`):
#   sh deploy/backup.sh
set -eu
dir="${BACKUP_DIR:-/data/workspace/backups}"
mkdir -p "$dir" && chmod 700 "$dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
cd "$(dirname "$0")"
rm -f "$dir"/*.dump.tmp  # leftovers of a failed run

dump() {
  if [ -n "${PGHOST:-}" ]; then
    pg_dump -Fc "$1"
  else
    docker compose exec -T postgres pg_dump -U masumi -Fc "$1"
  fi
}

for db in payment agent_jobs; do
  # Write to a temp file first so a failed dump never counts as one of the 7 kept.
  dump "$db" > "$dir/$db-$stamp.dump.tmp"
  chmod 600 "$dir/$db-$stamp.dump.tmp"
  mv "$dir/$db-$stamp.dump.tmp" "$dir/$db-$stamp.dump"
  ls -1t "$dir"/$db-*.dump | tail -n +8 | xargs -r rm -f
done
echo "$stamp backups written to $dir"
