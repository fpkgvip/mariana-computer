#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# Mariana Computer — Hetzner Deployment Script
# Target: 77.42.3.206 (AX42-U, Ryzen 7 PRO 8700GE, 64 GB DDR5, Ubuntu)
# ────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Configuration ───────────────────────────────────────────────────────────
SERVER_IP="77.42.3.206"
DEPLOY_USER="deploy"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/hetzner_mariana}"
REMOTE_DIR="/opt/mariana"
PROJECT_ARCHIVE="mariana-deploy.tar.gz"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
err()  { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

ssh_cmd() {
    ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "${DEPLOY_USER}@${SERVER_IP}" "$@"
}

scp_cmd() {
    scp -i "$SSH_KEY" -o StrictHostKeyChecking=no "$@"
}

# ── Pre-flight checks ──────────────────────────────────────────────────────
log "Pre-flight checks..."
[[ -f "$SSH_KEY" ]] || err "SSH key not found at $SSH_KEY"
command -v ssh >/dev/null || err "ssh not found"
command -v tar >/dev/null || err "tar not found"

# ── Step 1: Package project ────────────────────────────────────────────────
log "Packaging project..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

tar czf "/tmp/${PROJECT_ARCHIVE}" \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='data/' \
    --exclude='*.tar.gz' \
    --exclude='skills/' \
    -C "$(dirname "$SCRIPT_DIR")" \
    "$(basename "$SCRIPT_DIR")"

ARCHIVE_SIZE=$(du -h "/tmp/${PROJECT_ARCHIVE}" | cut -f1)
log "Archive created: ${ARCHIVE_SIZE}"

# ── Step 2: Prepare server (first-run only) ────────────────────────────────
log "Preparing server..."
ssh_cmd "bash -s" <<'REMOTE_SETUP'
set -euo pipefail

# Install Docker if missing
if ! command -v docker &>/dev/null; then
    echo "[server] Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
fi

# Install Docker Compose plugin if missing
if ! docker compose version &>/dev/null; then
    echo "[server] Installing Docker Compose plugin..."
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin
fi

# Create deploy directory
mkdir -p /opt/mariana
chown -R deploy:deploy /opt/mariana

echo "[server] Setup complete."
REMOTE_SETUP

# ── Step 3: Upload archive ─────────────────────────────────────────────────
log "Uploading archive to server..."
scp_cmd "/tmp/${PROJECT_ARCHIVE}" "${DEPLOY_USER}@${SERVER_IP}:/tmp/${PROJECT_ARCHIVE}"

# ── Step 4: Extract and configure ──────────────────────────────────────────
log "Extracting and configuring..."
ssh_cmd "bash -s" <<REMOTE_EXTRACT
set -euo pipefail

cd /opt
rm -rf mariana.bak 2>/dev/null || true

# Preserve .env and data if they exist
if [ -d mariana ]; then
    cp mariana/.env /tmp/mariana-env.bak 2>/dev/null || true
    mv mariana mariana.bak
fi

tar xzf /tmp/${PROJECT_ARCHIVE} -C /opt/
rm -f /tmp/${PROJECT_ARCHIVE}

# Restore .env if it existed
if [ -f /tmp/mariana-env.bak ]; then
    cp /tmp/mariana-env.bak /opt/mariana/.env
    rm -f /tmp/mariana-env.bak
    echo "[server] Restored existing .env"
fi

# Generate .env from template if it doesn't exist
if [ ! -f /opt/mariana/.env ]; then
    PG_PASS=\$(openssl rand -hex 16)
    cp /opt/mariana/.env.example /opt/mariana/.env
    sed -i "s/change_me_to_random_32_char/\${PG_PASS}/g" /opt/mariana/.env
    echo "[server] Generated .env with random Postgres password: \${PG_PASS}"
    echo "[server] ⚠  Edit /opt/mariana/.env to add your API keys!"
fi

chown -R deploy:deploy /opt/mariana
echo "[server] Extraction complete."
REMOTE_EXTRACT

# ── Step 5: Build and deploy ───────────────────────────────────────────────
log "Building Docker images and starting services..."
ssh_cmd "bash -s" <<'REMOTE_DEPLOY'
set -euo pipefail
cd /opt/mariana

# Build images
docker compose build --no-cache

# Stop existing services (if any)
docker compose down 2>/dev/null || true

# Start all services
docker compose up -d

# Wait for health checks
echo "[server] Waiting for services to be healthy..."
sleep 10

# Check service status
docker compose ps

# Test API health
echo ""
echo "[server] Testing API health..."
for i in {1..12}; do
    if curl -sf http://localhost:8080/api/health >/dev/null 2>&1; then
        echo "[server] ✓ API is healthy!"
        curl -s http://localhost:8080/api/health | python3 -m json.tool 2>/dev/null || \
            curl -s http://localhost:8080/api/health
        break
    fi
    echo "[server] Waiting for API... (attempt $i/12)"
    sleep 5
done

echo ""
echo "[server] Deployment complete."
echo "[server] API endpoint: http://77.42.3.206:8080"
echo "[server] Swagger docs: http://77.42.3.206:8080/docs"
REMOTE_DEPLOY

# ── Cleanup ─────────────────────────────────────────────────────────────────
rm -f "/tmp/${PROJECT_ARCHIVE}"

log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "Deployment complete!"
log ""
log "  API:     http://${SERVER_IP}:8080"
log "  Swagger: http://${SERVER_IP}:8080/docs"
log "  Health:  http://${SERVER_IP}:8080/api/health"
log ""
log "Next steps:"
log "  1. SSH in and edit API keys: ssh -i ${SSH_KEY} ${DEPLOY_USER}@${SERVER_IP}"
log "     nano /opt/mariana/.env"
log "  2. Restart after editing: cd /opt/mariana && docker compose restart"
log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
