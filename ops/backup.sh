#!/usr/bin/env sh
set -eu

APP_DIR="${TRADEBACK_APP_DIR:-/opt/tradeback/TradeBack_BE}"
BACKUP_DIR="${TRADEBACK_BACKUP_DIR:-/opt/tradeback/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
COMPOSE="docker compose -f ${APP_DIR}/docker-compose.prod.yml --project-name tradeback_be"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

$COMPOSE exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-privileges' \
  > "$BACKUP_DIR/postgres-$STAMP.dump"

docker run --rm \
  -v tradeback_be_media_data:/source:ro \
  -v "$BACKUP_DIR:/backup" \
  alpine tar -czf "/backup/media-$STAMP.tar.gz" -C /source .

find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'postgres-*.dump' -o -name 'media-*.tar.gz' \) \
  -mtime +14 -delete
