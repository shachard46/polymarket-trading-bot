#!/usr/bin/env bash
# Daily backup of the Vault directory. Keeps the newest 14 archives.
# Optional upload: set PMBOT_BACKUP_S3_URI (for example s3://my-bucket/pmbot/) and install the aws cli.
set -euo pipefail

APP_DIR="${PMBOT_APP_DIR:-/opt/pmbot}"
VAULT_BASE="${PMBOT_VAULT_PATH:-${APP_DIR}}"
VAULT_DIR="${VAULT_BASE}/Vault"
BACKUP_DIR="${PMBOT_BACKUP_DIR:-/var/backups/pmbot}"
KEEP=14

if [[ ! -d "${VAULT_DIR}" ]]; then
  echo "vault not found: ${VAULT_DIR}" >&2
  exit 1
fi

mkdir -p "${BACKUP_DIR}"
stamp="$(date +%Y-%m-%d_%H%M%S)"
archive="${BACKUP_DIR}/vault-${stamp}.tar.gz"
tar -czf "${archive}" -C "${VAULT_BASE}" Vault
echo "wrote ${archive}"

# Retention: delete all but the newest KEEP archives.
mapfile -t old < <(ls -1t "${BACKUP_DIR}"/vault-*.tar.gz | tail -n +"$((KEEP + 1))")
if [[ ${#old[@]} -gt 0 ]]; then
  rm -f -- "${old[@]}"
fi

if [[ -n "${PMBOT_BACKUP_S3_URI:-}" ]]; then
  if command -v aws >/dev/null 2>&1; then
    aws s3 cp "${archive}" "${PMBOT_BACKUP_S3_URI%/}/$(basename "${archive}")"
  else
    echo "PMBOT_BACKUP_S3_URI set but aws cli not found; skipping upload" >&2
  fi
fi
