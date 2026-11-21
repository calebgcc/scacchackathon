#!/usr/bin/env bash
# Run from your laptop: copies the repo to the VM and (re)starts the app.
#   deploy/deploy.sh user@VM_IP
# Data (db, uploads, round files) lives in /opt/scacchackathon/data on the VM and is never overwritten.
set -euo pipefail
HOST="${1:?usage: deploy/deploy.sh user@host}"
cd "$(dirname "$0")/.."
rsync -az --delete --exclude data --exclude .env --exclude .git --exclude '__pycache__' ./ "$HOST:/opt/scacchackathon/"
ssh "$HOST" 'cd /opt/scacchackathon && test -f .env || { echo "missing /opt/scacchackathon/.env - copy .env.example and fill it in"; exit 1; }
  docker compose build && docker compose up -d && docker compose ps'
