#!/usr/bin/env bash
# Run this on a fresh GCE VM (Debian/Ubuntu) as the deploy user, e.g.:
#   gcloud compute ssh tds-p1-bot --zone=<zone> --command="bash -s" < deploy/setup_gce.sh
# or copy the repo up first and run it directly over SSH.
set -euo pipefail

REPO_DIR=/opt/tds-p1-telegram-bot

sudo mkdir -p "$REPO_DIR"
sudo chown "$USER":"$USER" "$REPO_DIR"

# Assumes the repo has already been git-cloned or rsynced into $REPO_DIR.
cd "$REPO_DIR"

sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip

python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "!! Edit $REPO_DIR/.env with real values before starting the service."
fi

sudo cp deploy/tds-p1-bot.service /etc/systemd/system/tds-p1-bot@"$USER".service
sudo systemctl daemon-reload
sudo systemctl enable tds-p1-bot@"$USER".service

echo "Setup done. After editing .env, start with:"
echo "  sudo systemctl start tds-p1-bot@$USER"
echo "Check logs with:"
echo "  journalctl -u tds-p1-bot@$USER -f"
