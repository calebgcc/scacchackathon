#!/usr/bin/env bash
# Run ONCE on a fresh Ubuntu VM (as a sudo-capable user): installs Docker and prepares the app folder.
set -euo pipefail
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER"
sudo mkdir -p /opt/scacchackathon && sudo chown "$USER" /opt/scacchackathon
# Pre-pull common base images so players' first builds are fast.
for img in python:3.12-slim debian:bookworm-slim gcc:14 rust:1.83 golang:1.23 eclipse-temurin:21 node:22-slim; do
  sudo docker pull -q "$img" || true
done
echo "done - log out and back in so the docker group applies"
