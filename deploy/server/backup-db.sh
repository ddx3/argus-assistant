#!/bin/sh
# Backup of the Argus accounts database (emails, password hashes, balances, usage, orders).
#
# The copy is made with SQLite's own means (`backup`): it is consistent even if the
# database is being written to at that moment, unlike a "file copy on the fly". It lies on
# the server itself outside the container volume (`/opt/argus-server/backups`), so
# recreating the container or the volume does not touch it. Permissions 600: other
# people's addresses are inside. Kept for 30 days, older ones are erased.
# Cron: every day at 03:30 (`crontab -l`). Restore: `restore-db.md` in this same folder.
# WARNING: for now the copies live only on this same server; if the server is lost, they
# are lost too.
set -eu

DIR=/opt/argus-server/backups
DAY=$(date +%Y%m%d)
OUT="$DIR/argus-accounts-$DAY.sqlite"
LOG="$DIR/backup.log"
mkdir -p "$DIR"
umask 077

{
  docker exec argus-server python -c "
import sqlite3
src = sqlite3.connect('/app/data/argus-accounts.sqlite')
dst = sqlite3.connect('/tmp/backup.sqlite')
src.backup(dst)
dst.close()
src.close()
"
  docker cp argus-server:/tmp/backup.sqlite "$OUT"
  docker exec argus-server rm -f /tmp/backup.sqlite

  # Check: the copy opens, is consistent, and has accounts in it.
  COUNT=$(python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'integrity_check'
print(c.execute('SELECT COUNT(*) FROM accounts').fetchone()[0])
" "$OUT")
  gzip -f "$OUT"
  chmod 600 "$OUT.gz"
  find "$DIR" -name 'argus-accounts-*.sqlite.gz' -mtime +30 -delete
  echo "$(date -u +%FT%TZ) ok: $OUT.gz, accounts: $COUNT"
} >> "$LOG" 2>&1 || {
  echo "$(date -u +%FT%TZ) FAILED, see the lines above" >> "$LOG"
  exit 1
}
