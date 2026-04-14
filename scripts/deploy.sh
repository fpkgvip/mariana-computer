#!/usr/bin/env bash
# ============================================================================
# Mariana Computer — Full Server Setup & Deploy Script
# Run from your MacBook: bash scripts/deploy.sh
# Prerequisites: SSH access to 77.42.3.206 as root with key auth
# ============================================================================
set -euo pipefail

SERVER="root@77.42.3.206"
DEPLOY_DIR="/home/deploy/mariana"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Mariana Computer Deploy Script ==="
echo "Server: $SERVER"
echo "Local:  $LOCAL_DIR"
echo ""

# ----------------------------------------------------------------------------
# Phase 1: System Setup (idempotent — safe to re-run)
# ----------------------------------------------------------------------------
echo "[1/7] System hardening & essentials..."
ssh "$SERVER" bash -s << 'PHASE1'
set -euo pipefail

# Create deploy user if not exists
if ! id -u deploy &>/dev/null; then
    adduser --disabled-password --gecos "" deploy
    echo "deploy ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/deploy
    mkdir -p /home/deploy/.ssh
    cp ~/.ssh/authorized_keys /home/deploy/.ssh/
    chown -R deploy:deploy /home/deploy/.ssh
    chmod 700 /home/deploy/.ssh && chmod 600 /home/deploy/.ssh/authorized_keys
    echo "  [+] Created deploy user"
else
    echo "  [=] deploy user exists"
fi

# Firewall
if ! ufw status | grep -q "Status: active"; then
    ufw allow 22/tcp
    ufw allow 8080/tcp
    ufw --force enable
    echo "  [+] Firewall enabled"
else
    echo "  [=] Firewall already active"
fi

# Timezone
timedatectl set-timezone Asia/Hong_Kong 2>/dev/null || true

# Essentials
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq curl git htop tmux unzip jq python3-pip
echo "  [+] System essentials installed"
PHASE1

# ----------------------------------------------------------------------------
# Phase 2: Docker
# ----------------------------------------------------------------------------
echo "[2/7] Installing Docker..."
ssh "$SERVER" bash -s << 'PHASE2'
set -euo pipefail
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
    usermod -aG docker deploy
    apt-get install -y -qq docker-compose-plugin
    echo "  [+] Docker installed"
else
    echo "  [=] Docker already installed"
fi
docker --version
PHASE2

# ----------------------------------------------------------------------------
# Phase 3: Tailscale
# ----------------------------------------------------------------------------
echo "[3/7] Installing Tailscale..."
ssh "$SERVER" bash -s << 'PHASE3'
set -euo pipefail
if ! command -v tailscale &>/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "  [+] Tailscale installed"
    echo "  [!] Run 'tailscale up' manually to authenticate"
else
    echo "  [=] Tailscale already installed"
    tailscale status 2>/dev/null || echo "  [!] Tailscale not connected — run 'tailscale up'"
fi
PHASE3

# ----------------------------------------------------------------------------
# Phase 4: Project structure
# ----------------------------------------------------------------------------
echo "[4/7] Creating project structure..."
ssh "$SERVER" bash -s << 'PHASE4'
set -euo pipefail
mkdir -p /home/deploy/mariana/{mariana/{orchestrator,ai/prompts,data,connectors,tribunal,report/templates,browser},data/{checkpoints,findings,reports,pdfs,screenshots,batch_results,inbox},scripts}
chown -R deploy:deploy /home/deploy/mariana
echo "  [+] Directory structure created"
PHASE4

# ----------------------------------------------------------------------------
# Phase 5: Upload code
# ----------------------------------------------------------------------------
echo "[5/7] Uploading codebase..."
rsync -avz --delete \
    --exclude '.env' \
    --exclude 'data/' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.git' \
    "$LOCAL_DIR/" "$SERVER:$DEPLOY_DIR/"

ssh "$SERVER" "chown -R deploy:deploy $DEPLOY_DIR"
echo "  [+] Code uploaded"

# ----------------------------------------------------------------------------
# Phase 6: Write .env (interactive — prompts for API keys if missing)
# ----------------------------------------------------------------------------
echo "[6/7] Configuring environment..."

if ssh "$SERVER" "test -f $DEPLOY_DIR/.env"; then
    echo "  [=] .env already exists — skipping"
    echo "  [!] To regenerate, delete $DEPLOY_DIR/.env on the server and re-run"
else
    echo "  [!] No .env found — creating..."
    
    # Generate random postgres password
    PG_PASS=$(openssl rand -base64 32 | tr -dc 'a-zA-Z0-9' | head -c 32)
    
    read -rp "  Enter LLM_GATEWAY_API_KEY: " LLM_KEY
    read -rp "  Enter POLYGON_API_KEY: " POLYGON_KEY
    read -rp "  Enter UNUSUAL_WHALES_API_KEY: " UW_KEY
    
    ssh "$SERVER" bash -s << ENVEOF
cat > $DEPLOY_DIR/.env << 'EOF'
# LLM Gateway
LLM_GATEWAY_API_KEY=$LLM_KEY
LLM_GATEWAY_BASE_URL=https://api.llmgateway.io/v1

# Data APIs
POLYGON_API_KEY=$POLYGON_KEY
UNUSUAL_WHALES_API_KEY=$UW_KEY

# Infrastructure
POSTGRES_USER=mariana
POSTGRES_PASSWORD=$PG_PASS
POSTGRES_DB=mariana
REDIS_URL=redis://redis:6379/0
DATABASE_URL=postgresql://mariana:$PG_PASS@postgresql:5432/mariana

# Budget defaults
BUDGET_TASK_HARD_CAP=400.0
BUDGET_BRANCH_HARD_CAP=75.0
BUDGET_BRANCH_INITIAL=5.0

# Paths
DATA_ROOT=/data/mariana
CHECKPOINT_DIR=/data/mariana/checkpoints
REPORT_DIR=/data/mariana/reports
EOF
chown deploy:deploy $DEPLOY_DIR/.env
chmod 600 $DEPLOY_DIR/.env
ENVEOF
    echo "  [+] .env created with generated postgres password"
fi

# ----------------------------------------------------------------------------
# Phase 7: Docker build & start
# ----------------------------------------------------------------------------
echo "[7/7] Building and starting Docker stack..."
ssh "$SERVER" bash -s << 'PHASE7'
set -euo pipefail
cd /home/deploy/mariana

# Build
docker compose build --no-cache 2>&1 | tail -5

# Start infrastructure first
docker compose up -d postgresql redis
echo "  [+] Postgres + Redis starting..."
sleep 10

# Start orchestrator
docker compose up -d mariana-orchestrator
echo "  [+] Orchestrator starting..."
sleep 5

# Health checks
echo ""
echo "=== Health Checks ==="
docker compose exec postgresql pg_isready -U mariana && echo "  [✓] PostgreSQL OK" || echo "  [✗] PostgreSQL FAILED"
docker compose exec redis redis-cli ping && echo "  [✓] Redis OK" || echo "  [✗] Redis FAILED"
docker compose ps

echo ""
echo "=== Mariana Computer Deployed ==="
echo "SSH:     ssh deploy@77.42.3.206"
echo "Logs:    docker compose logs -f mariana-orchestrator"
echo "Status:  docker compose exec mariana-orchestrator python -m mariana.main --status"
echo "Dry run: docker compose exec mariana-orchestrator python -m mariana.main --topic test --budget 5 --dry-run"
echo ""
echo "To run first investigation:"
echo "  docker compose exec mariana-orchestrator python -m mariana.main \\"
echo "    --topic 'Investigate Super Micro Computer (SMCI) accounting practices' \\"
echo "    --budget 50"
PHASE7

echo ""
echo "=== Deploy Complete ==="
