#!/bin/sh
set -eu

database_path=${CONTACTBOT_DB:-/var/lib/contactbot/contactbot.sqlite3}
backup_dir=${CONTACTBOT_BACKUP_DIR:-/var/lib/contactbot/backups}

case "$database_path" in
  /*) ;;
  *) echo "CONTACTBOT_DB 必须是绝对路径" >&2; exit 2 ;;
esac
case "$backup_dir" in
  /*) ;;
  *) echo "CONTACTBOT_BACKUP_DIR 必须是绝对路径" >&2; exit 2 ;;
esac

install -d -m 0700 "$backup_dir"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$backup_dir/contactbot-$timestamp.sqlite3"
sqlite3 "$database_path" ".backup '$target'"
chmod 0600 "$target"
find "$backup_dir" -type f -name 'contactbot-*.sqlite3' -mtime +30 -delete
echo "$target"

