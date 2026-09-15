#!/usr/bin/env bash
# Ежедневный бэкап Postgres. Запускается systemd-таймером murcielagobot-backup.timer
# (не входит в deploy.sh — это отдельная забота по расписанию, не по пушу кода).
#
# Хранит только на диске VPS с ротацией — этого достаточно как страховки от
# "уронили миграцию / кто-то удалил не то", но НЕ спасает при потере самого
# сервера. Если появится S3-совместимое хранилище — сюда же добавить строчку
# с выгрузкой архива наружу.
set -euo pipefail

CONTAINER="murcielagobot-postgres"
DB_USER="murcielagobot"
DB_NAME="fragmentbot"
BACKUP_DIR="/opt/murcielagobot/backups"
KEEP_DAYS=14

mkdir -p "$BACKUP_DIR"

STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/${DB_NAME}-${STAMP}.sql.gz"
TMP="${OUT}.tmp"

docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" | gzip > "$TMP"
mv "$TMP" "$OUT"

# Пустой/битый дамп не должен молча остаться единственной "успешной" копией.
if [ ! -s "$OUT" ]; then
  echo "backup_db: получившийся дамп пустой — $OUT" >&2
  rm -f "$OUT"
  exit 1
fi

find "$BACKUP_DIR" -name "${DB_NAME}-*.sql.gz" -mtime "+${KEEP_DAYS}" -delete

echo "backup_db: OK -> $OUT ($(du -h "$OUT" | cut -f1))"
