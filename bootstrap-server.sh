#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# Mariana Computer — Server Bootstrap (run once from Hetzner web console)
#
# Usage (paste this ONE line into the Hetzner web console terminal):
#
#   curl -fsSL https://raw.githubusercontent.com/fpkgvip/mariana-computer/master/bootstrap-server.sh | bash
#
# ────────────────────────────────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[bootstrap]${NC} $*"; }
warn() { echo -e "${YELLOW}[bootstrap]${NC} $*"; }
err()  { echo -e "${RED}[bootstrap]${NC} $*" >&2; exit 1; }

# ── 1. System dependencies ─────────────────────────────────────────────────
log "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq git curl docker.io docker-compose-plugin

systemctl enable --now docker
log "Docker version: $(docker --version)"

# ── 2. Create deploy user ──────────────────────────────────────────────────
if ! id deploy &>/dev/null; then
    log "Creating deploy user..."
    useradd -m -s /bin/bash -G docker,sudo deploy
    echo "deploy ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/deploy
else
    log "Deploy user already exists"
    usermod -aG docker deploy 2>/dev/null || true
fi

# ── 3. Clone repository ────────────────────────────────────────────────────
log "Cloning Mariana Computer..."
mkdir -p /opt
if [ -d /opt/mariana/.git ]; then
    log "Repo already exists, pulling latest..."
    cd /opt/mariana
    git pull origin master
else
    rm -rf /opt/mariana
    # Clone via HTTPS (no SSH key needed)
    git clone https://github.com/fpkgvip/mariana-computer.git /opt/mariana
fi
chown -R deploy:deploy /opt/mariana

# ── 4. Generate .env ───────────────────────────────────────────────────────
cd /opt/mariana
if [ ! -f .env ]; then
    PG_PASS=$(openssl rand -hex 16)
    cp .env.example .env
    sed -i "s/change_me_to_random_32_char/${PG_PASS}/g" .env

    # Update POSTGRES_DSN with the generated password
    sed -i "s|postgresql://mariana:${PG_PASS}@postgresql|postgresql://mariana:${PG_PASS}@postgresql|g" .env

    log "Generated .env with random Postgres password"
    warn ""
    warn "╔══════════════════════════════════════════════════════════════╗"
    warn "║  IMPORTANT: Edit /opt/mariana/.env and add your API keys:  ║"
    warn "║                                                            ║"
    warn "║    nano /opt/mariana/.env                                   ║"
    warn "║                                                            ║"
    warn "║  Required keys:                                            ║"
    warn "║    - LLM_GATEWAY_API_KEY                                   ║"
    warn "║    - POLYGON_API_KEY                                       ║"
    warn "║    - UNUSUAL_WHALES_API_KEY                                ║"
    warn "║    - FRED_API_KEY                                          ║"
    warn "╚══════════════════════════════════════════════════════════════╝"
    warn ""
    warn "After adding keys, run:  cd /opt/mariana && docker compose up -d"
    warn ""
else
    log "Existing .env preserved"
fi

# ── 5. Build Docker images ─────────────────────────────────────────────────
log "Building Docker images (this may take 2-3 minutes)..."
docker compose build

# ── 6. Start services ──────────────────────────────────────────────────────
log "Starting services..."
docker compose up -d

# ── 7. Wait for health ─────────────────────────────────────────────────────
log "Waiting for services to be healthy..."
sleep 10

API_UP=false
for i in $(seq 1 12); do
    if curl -sf http://localhost:8080/api/health >/dev/null 2>&1; then
        API_UP=true
        break
    fi
    echo "  Waiting for API... (attempt $i/12)"
    sleep 5
done

if [ "$API_UP" = true ]; then
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "Mariana Computer is LIVE"
    log ""
    log "  API:     http://$(hostname -I | awk '{print $1}'):8080"
    log "  Swagger: http://$(hostname -I | awk '{print $1}'):8080/docs"
    log "  Health:  http://$(hostname -I | awk '{print $1}'):8080/api/health"
    log ""
    curl -s http://localhost:8080/api/health | python3 -m json.tool 2>/dev/null || true
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
else
    warn "API not responding yet. Check logs:"
    warn "  docker compose -f /opt/mariana/docker-compose.yml logs --tail=30"
fi

log ""
log "Bootstrap complete."
