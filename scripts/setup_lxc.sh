#!/bin/bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This setup script must be run as root inside the LXC container." >&2
  exit 1
fi

if [[ -z "${REPO_URL:-}" ]]; then
  echo "Set the REPO_URL environment variable to the Git repository URL before running." >&2
  exit 1
fi

APP_DIR="${APP_DIR:-/srv/insider-trading}"
SERVICE_NAME="${SERVICE_NAME:-insider-trading}"
BRANCH_NAME="${BRANCH_NAME:-main}"

apt-get update
apt-get install -y git python3 python3-venv

mkdir -p "${APP_DIR}"
if [[ ! -d "${APP_DIR}/.git" ]]; then
  git clone "${REPO_URL}" "${APP_DIR}"
else
  git -C "${APP_DIR}" fetch --all
  git -C "${APP_DIR}" reset --hard "origin/${BRANCH_NAME}"
fi

git -C "${APP_DIR}" checkout "${BRANCH_NAME}"

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

install -m 755 "${APP_DIR}/scripts/run_service.sh" "${APP_DIR}/run_service.sh"

cat <<SERVICE > "/etc/systemd/system/${SERVICE_NAME}.service"
[Unit]
Description=Insider Trading Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/run_service.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"

echo "Deployment complete. View logs with: journalctl -fu ${SERVICE_NAME}.service"
