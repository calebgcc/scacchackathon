#!/usr/bin/env bash
# Upload a chess engine to the Scacchackathon server.
#   SCACC_TOKEN=scc_... ./submit.sh [engine-folder]
set -euo pipefail

URL="${SCACC_URL:-__SCACC_URL__}"
DIR="${1:-.}"
TOKEN="${SCACC_TOKEN:-}"
if [[ -z "$TOKEN" && -f "$HOME/.scacc-token" ]]; then TOKEN="$(cat "$HOME/.scacc-token")"; fi
if [[ -z "$TOKEN" ]]; then
  echo "error: set SCACC_TOKEN=<your token> (or put it in ~/.scacc-token)" >&2; exit 1
fi
if [[ ! -f "$DIR/Dockerfile" ]]; then
  echo "error: $DIR/Dockerfile not found - your engine folder needs a Dockerfile at the top level" >&2; exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ARCHIVE="$TMP/engine.tgz"
# COPYFILE_DISABLE stops macOS tar from adding ._ resource files.
COPYFILE_DISABLE=1 tar czf "$ARCHIVE" \
  --exclude .git --exclude node_modules --exclude target --exclude build --exclude __pycache__ \
  --exclude .venv --exclude venv --exclude '*.o' --exclude .DS_Store \
  -C "$DIR" .
SIZE=$(du -h "$ARCHIVE" | cut -f1)
echo "==> uploading $DIR ($SIZE) to $URL"

RESP="$(curl -sS -w '\n%{http_code}' -H "Authorization: Bearer $TOKEN" -F "engine=@$ARCHIVE" "$URL/api/submit")"
CODE="${RESP##*$'\n'}"
BODY="${RESP%$'\n'*}"
if [[ "$CODE" != "200" ]]; then
  echo "error ($CODE): $BODY" >&2; exit 1
fi
ID="$(printf '%s' "$BODY" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')"
VERSION="$(printf '%s' "$BODY" | sed -n 's/.*"version":\([0-9]*\).*/\1/p')"
echo "==> submission #$ID (version v$VERSION) queued, following the build log..."

OFFSET=0
STATUS=queued
while :; do
  curl -sS -D "$TMP/h" -o "$TMP/b" -H "Authorization: Bearer $TOKEN" "$URL/api/submissions/$ID/log?offset=$OFFSET"
  cat "$TMP/b"
  STATUS="$(tr -d '\r' < "$TMP/h" | sed -n 's/^[xX]-[sS]tatus: //p')"
  NEXT="$(tr -d '\r' < "$TMP/h" | sed -n 's/^[xX]-[nN]ext-[oO]ffset: //p')"
  OFFSET="${NEXT:-$OFFSET}"
  case "$STATUS" in
    ready)  echo; echo "==> v$VERSION is READY and will play in the next round"; exit 0 ;;
    failed) echo; echo "==> v$VERSION FAILED - see the log above, fix it and submit again" >&2; exit 2 ;;
  esac
  sleep 2
done
