#!/usr/bin/env bash
# Start PocketBase with CallProof migrations. Creates/updates superuser from .env.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${ROOT}/.." && pwd)"
BIN="${ROOT}/pocketbase"

if [[ ! -x "$BIN" ]]; then
  echo "PocketBase binary missing. Run: ${ROOT}/download.sh" >&2
  exit 1
fi

# Load repo .env if present (does not export secrets to the shell history)
if [[ -f "${REPO}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${REPO}/.env"
  set +a
fi

: "${POCKETBASE_ADMIN_EMAIL:?Set POCKETBASE_ADMIN_EMAIL in .env}"
: "${POCKETBASE_ADMIN_PASSWORD:?Set POCKETBASE_ADMIN_PASSWORD in .env}"
HTTP_ADDR="${POCKETBASE_HTTP_ADDR:-127.0.0.1:8090}"

"$BIN" superuser upsert "$POCKETBASE_ADMIN_EMAIL" "$POCKETBASE_ADMIN_PASSWORD" \
  --dir "${ROOT}/pb_data" \
  --migrationsDir "${ROOT}/pb_migrations"

echo "Serving PocketBase on http://${HTTP_ADDR} (admin UI at /_/)"
exec "$BIN" serve \
  --http="$HTTP_ADDR" \
  --dir "${ROOT}/pb_data" \
  --migrationsDir "${ROOT}/pb_migrations"
