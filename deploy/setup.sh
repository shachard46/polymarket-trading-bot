#!/usr/bin/env bash
# Provision an Ubuntu 24.04 host (AWS Lightsail) for the pmbot Hub and Discord bot.
# Idempotent: safe to re-run. Usage:
#   sudo PMBOT_REPO_URL=<git url> bash deploy/setup.sh          # clone to /opt/pmbot
#   sudo bash deploy/setup.sh /path/to/checkout                  # use an existing checkout
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run with sudo" >&2
  exit 1
fi

SVC_USER=pmbot
APP_DIR="${1:-/opt/pmbot}"
ENV_DIR=/etc/pmbot
ENV_FILE="${ENV_DIR}/pmbot.env"
BACKUP_DIR=/var/backups/pmbot
SRC_DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip git ufw unattended-upgrades

# Service user (no login shell, no home directory contents needed).
if ! id -u "${SVC_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${SVC_USER}"
fi

# Repo.
if [[ -d "${APP_DIR}/.git" || -f "${APP_DIR}/main.py" ]]; then
  echo "using existing checkout at ${APP_DIR}"
else
  if [[ -z "${PMBOT_REPO_URL:-}" ]]; then
    echo "${APP_DIR} missing: set PMBOT_REPO_URL or pass an existing checkout path" >&2
    exit 1
  fi
  git clone "${PMBOT_REPO_URL}" "${APP_DIR}"
fi
chown -R "${SVC_USER}:${SVC_USER}" "${APP_DIR}"
# git refuses to run as another user in a directory it does not own; the owner is pmbot now.

# Virtualenv and dependencies (as the service user).
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  sudo -u "${SVC_USER}" python3 -m venv "${APP_DIR}/.venv"
fi
sudo -u "${SVC_USER}" "${APP_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "${SVC_USER}" "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

# Env file: created once, never overwritten (it holds secrets).
install -d -m 755 "${ENV_DIR}"
if [[ ! -f "${ENV_FILE}" ]]; then
  install -m 600 "${SRC_DEPLOY}/pmbot.env.example" "${ENV_FILE}"
  echo "created ${ENV_FILE}: fill in your keys"
fi
chown root:root "${ENV_FILE}"
chmod 600 "${ENV_FILE}"
# systemd reads EnvironmentFile as root, so the service user does not need access.

install -d -m 750 -o "${SVC_USER}" -g "${SVC_USER}" "${BACKUP_DIR}"

# systemd units. Unit files assume /opt/pmbot; rewrite when another path is used.
for unit in pmbot-hub.service pmbot-bot.service pmbot-backup.service pmbot-backup.timer; do
  sed "s#/opt/pmbot#${APP_DIR}#g" "${SRC_DEPLOY}/${unit}" > "/etc/systemd/system/${unit}"
  chmod 644 "/etc/systemd/system/${unit}"
done
chmod 755 "${APP_DIR}/deploy/backup.sh"
systemctl daemon-reload

# Hardening: firewall (SSH only) and automatic security updates.
ufw allow OpenSSH
ufw --force enable
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades

cat <<EOF

setup done. Next:
  1. sudoedit ${ENV_FILE}
  2. sudo systemctl enable --now pmbot-hub pmbot-bot pmbot-backup.timer
  3. journalctl -u pmbot-hub -f
EOF
