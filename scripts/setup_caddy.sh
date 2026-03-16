#!/bin/bash
# Add Lighthouse reverse proxy to Caddy
# Run with: sudo bash scripts/setup_caddy.sh

set -e

DOMAIN="lighthouse.37-27-217-78.sslip.io"
BACKEND="127.0.0.1:8420"

if grep -q "lighthouse" /etc/caddy/Caddyfile 2>/dev/null; then
    echo "⚠️  Lighthouse block already exists in Caddyfile"
    exit 0
fi

cat >> /etc/caddy/Caddyfile << EOF

${DOMAIN} {
    reverse_proxy ${BACKEND}
}
EOF

caddy reload --config /etc/caddy/Caddyfile
echo "✅ Caddy updated — https://${DOMAIN} → ${BACKEND}"
