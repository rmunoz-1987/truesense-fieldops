#!/bin/bash
# Pull a consistent Field Ops backup (SQLite + uploads) and keep 14 days.
# Reads the key from /home/box/.fieldops/backup_key and never prints it.
set -euo pipefail
KEY_FILE="${BACKUP_KEY_FILE:-/home/box/.fieldops/backup_key}"
DEST="${FIELDOPS_BACKUP_DIR:-/workspace/fieldops-backups}"
URL="${FIELDOPS_BACKUP_URL:-https://truesense-fieldops.onrender.com/api/office/backup}"
KEEP_DAYS="${FIELDOPS_BACKUP_KEEP:-14}"

if [ ! -s "$KEY_FILE" ]; then
  echo "Missing backup key file: $KEY_FILE" >&2
  exit 1
fi
mkdir -p "$DEST"
DAY="$(TZ=America/Chicago date +%F)"
OUT="$DEST/fieldops-$DAY.tgz"
TMP="$OUT.tmp.$$"
cleanup() { rm -f "$TMP"; }
trap cleanup EXIT

# The key is only passed in a header. Do not echo curl verbose output.
curl -fsS --retry 2 --retry-delay 5 \
  -H "X-Backup-Key: $(tr -d '\n' < "$KEY_FILE")" \
  -o "$TMP" \
  "$URL"

if ! tar -tzf "$TMP" >/dev/null 2>&1; then
  echo "Downloaded file is not a valid backup archive" >&2
  exit 2
fi
members=$(tar -tzf "$TMP")
if ! printf '%s
' "$members" | grep -F -x 'fieldops.db' >/dev/null; then
  echo "Backup archive is missing fieldops.db" >&2
  echo "$members" >&2
  exit 2
fi
mv "$TMP" "$OUT"
trap - EXIT
echo "Saved $OUT"

# Drop archives older than KEEP_DAYS.
find "$DEST" -maxdepth 1 -type f -name 'fieldops-*.tgz' -mtime +"$KEEP_DAYS" -delete
# If mtime prune leaves more than KEEP_DAYS files, keep the newest KEEP_DAYS.
mapfile -t extras < <(ls -1t "$DEST"/fieldops-*.tgz 2>/dev/null | tail -n +$((KEEP_DAYS + 1)))
if [ "${#extras[@]}" -gt 0 ]; then
  rm -f "${extras[@]}"
fi
