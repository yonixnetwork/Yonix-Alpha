#!/bin/sh
# Wraps the stock nginx entrypoint: nginx refuses to start at all if
# `ssl_certificate`/`ssl_certificate_key` point at files that don't exist,
# which is exactly the state a brand-new droplet is in before certbot has
# ever run (see docs/DEPLOYMENT.md's TLS bootstrap section). Rather than
# ship a second, HTTP-only nginx.conf to swap in and out by hand, this
# generates a short-lived self-signed certificate at the real Let's
# Encrypt path on first boot so nginx can come up immediately — real
# HTTPS is a browser certificate-warning away until the operator runs the
# documented certbot command, at which point nginx just needs a reload to
# pick up the real files at the same path.
set -eu

DOMAIN_PRIMARY="yonixalpha.com"
CERT_DIR="/etc/letsencrypt/live/${DOMAIN_PRIMARY}"

if [ ! -f "${CERT_DIR}/fullchain.pem" ]; then
    echo "yonixalpha-entrypoint: no certificate at ${CERT_DIR} — generating a temporary self-signed one so nginx can start."
    mkdir -p "${CERT_DIR}"
    openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
        -keyout "${CERT_DIR}/privkey.pem" \
        -out "${CERT_DIR}/fullchain.pem" \
        -subj "/CN=${DOMAIN_PRIMARY}"
    echo "yonixalpha-entrypoint: temporary certificate written. Run the certbot bootstrap command in docs/DEPLOYMENT.md, then 'docker compose restart reverse-proxy' to replace it with a real one."
fi

exec "$@"
