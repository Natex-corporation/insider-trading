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

if [[ -z "${ALPACA_API_KEY:-}" || -z "${ALPACA_SECRET_KEY:-}" ]]; then
  echo "Set ALPACA_API_KEY and ALPACA_SECRET_KEY before running the setup script." >&2
  exit 1
fi

APP_DIR="${APP_DIR:-/srv/insider-trading}"
SERVICE_NAME="${SERVICE_NAME:-insider-trading}"
SERVICE_USER="${SERVICE_USER:-insider-trading}"
BRANCH_NAME="${BRANCH_NAME:-main}"
ENV_FILE="/etc/${SERVICE_NAME}.env"
STATE_DIR="${STATE_DIR:-${APP_DIR}/data}"
LOG_DIR="${LOG_DIR:-${STATE_DIR}/logs}"

apt-get update
apt-get install -y git python3 python3-venv

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

mkdir -p "${APP_DIR}"
mkdir -p "${STATE_DIR}" "${LOG_DIR}"
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
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${STATE_DIR}" "${LOG_DIR}"

cat <<ENVFILE > "${ENV_FILE}"
SERVICE_BRANCH=${BRANCH_NAME}
ALPACA_API_KEY=${ALPACA_API_KEY}
ALPACA_SECRET_KEY=${ALPACA_SECRET_KEY}
ALPACA_BASE_URL=${ALPACA_BASE_URL:-https://paper-api.alpaca.markets}
STATE_DIR=${STATE_DIR}
LOG_DIR=${LOG_DIR}
MONITORING_ENABLED=${MONITORING_ENABLED:-true}
MONITORING_HOST=${MONITORING_HOST:-0.0.0.0}
MONITORING_PORT=${MONITORING_PORT:-8080}
ENVFILE
chmod 600 "${ENV_FILE}"

cat <<SERVICE > "/etc/systemd/system/${SERVICE_NAME}.service"
[Unit]
Description=Insider Trading Bot
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/run_service.sh
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${STATE_DIR} ${LOG_DIR}

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

echo "Deployment complete. View logs with: journalctl -fu ${SERVICE_NAME}.service"
