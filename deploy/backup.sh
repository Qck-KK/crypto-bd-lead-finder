#!/bin/sh
# Daily PostgreSQL backup for the agent stack (jobs, results, cost ledger, feedback, Payment Service).
# Keeps the last 7 dumps. The Payment Service dump contains wallet secrets encrypted with
# ENCRYPTION_KEY: store backups as carefully as the key, and never next to it.
#
#   sh deploy/backup.sh            # from the repo root on the server
#   crontab: 30 3 * * * cd /data/workspace/crypto-bd-lead-finder && sh deploy/backup.sh
set -eu
dir="${BACKUP_DIR:-/data/workspace/backups}"
mkdir -p "$dir" && chmod 700 "$dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
cd "$(dirname "$0")"
for db in payment agent_jobs; do
  docker compose exec -T postgres pg_dump -U masumi -Fc "$db" > "$dir/$db-$stamp.dump"
  chmod 600 "$dir/$db-$stamp.dump"
  ls -1t "$dir"/$db-*.dump | tail -n +8 | xargs -r rm -f
done
echo "backups written to $dir"
