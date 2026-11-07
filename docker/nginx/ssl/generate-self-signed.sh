#!/usr/bin/env bash
# Generates self-signed TLS certificates for development or staging
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_PATH="${SCRIPT_DIR}/cert.pem"
KEY_PATH="${SCRIPT_DIR}/key.pem"

echo "Generating self-signed certificate for localhost in ${SCRIPT_DIR}..."
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout "${KEY_PATH}" \
  -out "${CERT_PATH}" \
  -subj "/C=US/ST=State/L=City/O=AIagent/CN=localhost"

chmod 600 "${KEY_PATH}"
chmod 644 "${CERT_PATH}"
echo "Certificates generated successfully: ${CERT_PATH}, ${KEY_PATH}"

