#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${SOURCE_DATABASE_URL:-}" || -z "${TARGET_DATABASE_URL:-}" ]]; then
  echo "Defina SOURCE_DATABASE_URL (Render) e TARGET_DATABASE_URL (Neon)."
  echo "Exemplo: export SOURCE_DATABASE_URL='postgresql://...'"
  exit 1
fi

DUMP_FILE="render_backup_$(date +%Y%m%d_%H%M%S).dump"
echo "[1/3] Criando dump do Render..."
pg_dump "$SOURCE_DATABASE_URL" --format=custom --no-owner --no-acl --file="$DUMP_FILE"

echo "[2/3] Restaurando no Neon..."
pg_restore --dbname="$TARGET_DATABASE_URL" --clean --if-exists --no-owner --no-acl "$DUMP_FILE"

echo "[3/3] Validando conexão com Neon..."
psql "$TARGET_DATABASE_URL" -c "SELECT current_database() AS banco, now() AS horario;"

echo "Migração concluída. Dump local preservado em: $DUMP_FILE"
