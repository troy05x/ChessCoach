#!/usr/bin/env bash
# ==============================================================================
# install.sh - Automated Production Deployment for AI Chess Coach
# Target OS: Ubuntu 20.04 / 22.04 LTS (Optimized for 2 CPUs, 1GB RAM)
# ==============================================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# ------------------------------------------------------------------------------
# 1. Prerequisite Pre-flight Checks
# ------------------------------------------------------------------------------
log_info "Starting pre-flight system checks..."

# Check root or sudo privileges
if [ "$EUID" -ne 0 ]; then
    log_error "This script must be run as root or with sudo. Please run: sudo bash install.sh"
    exit 1
fi

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    log_info "Detected OS: $NAME $VERSION"
    if [[ "$ID" != "ubuntu" && "$ID" != "debian" ]]; then
        log_warning "This installer is optimized for Ubuntu/Debian. Continuing anyway..."
    fi
else
    log_warning "Could not detect OS release. Proceeding with caution..."
fi

# Check available RAM and warn if very low
TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
TOTAL_RAM_MB=$((TOTAL_RAM_KB / 1024))
log_info "Total System RAM: ${TOTAL_RAM_MB} MB"
if [ "$TOTAL_RAM_MB" -lt 800 ]; then
    log_warning "Detected less than 1GB RAM. Low-memory limits will keep the service stable."
fi

# Check internet connectivity
log_info "Checking internet connectivity..."
if ! ping -c 1 8.8.8.8 >/dev/null 2>&1 && ! curl -s --head https://www.google.com >/dev/null 2>&1; then
    log_error "No internet connectivity detected. Cannot download required packages."
    exit 1
fi
log_success "System prerequisites verified."

# ------------------------------------------------------------------------------
# 2. Determine Deployment Directories and Real User
# ------------------------------------------------------------------------------
TARGET_USER="${SUDO_USER:-$(whoami)}"
if [ "$TARGET_USER" = "root" ]; then
    APP_DIR="/opt/chess-coach"
else
    APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

log_info "Deploying application at: $APP_DIR"
log_info "Service run user: $TARGET_USER"

# Create application directory if needed
mkdir -p "$APP_DIR"

# Copy repository files into APP_DIR if installing to /opt
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$CURRENT_DIR" != "$APP_DIR" ]; then
    log_info "Copying files to $APP_DIR..."
    cp -r "$CURRENT_DIR"/* "$APP_DIR/"
fi

# ------------------------------------------------------------------------------
# 3. Setup Persistent Data Directories & Permissions (Fix Permission Issues)
# ------------------------------------------------------------------------------
DATA_DIR="/data"
log_info "Configuring persistent storage directory at $DATA_DIR..."
mkdir -p "$DATA_DIR/reports"

# Grant permissions so background cron jobs and web app can write without permission prompts
chown -R "$TARGET_USER:$TARGET_USER" "$DATA_DIR"
chmod -R 775 "$DATA_DIR"

# Also ensure application dir has proper ownership
chown -R "$TARGET_USER:$TARGET_USER" "$APP_DIR"

# ------------------------------------------------------------------------------
# 4. Install System Dependencies
# ------------------------------------------------------------------------------
log_info "Updating apt packages and installing system dependencies (python3-pip, python3-venv, stockfish, git, cron)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    stockfish \
    git \
    cron \
    curl \
    ca-certificates

# Verify stockfish binary location
STOCKFISH_BIN="$(which stockfish || true)"
if [ -z "$STOCKFISH_BIN" ]; then
    if [ -f /usr/games/stockfish ]; then
        STOCKFISH_BIN="/usr/games/stockfish"
    elif [ -f /usr/bin/stockfish ]; then
        STOCKFISH_BIN="/usr/bin/stockfish"
    fi
fi

if [ -z "$STOCKFISH_BIN" ]; then
    log_error "Stockfish installation failed or binary could not be found."
    exit 1
fi
log_success "Stockfish installed at: $STOCKFISH_BIN"

# ------------------------------------------------------------------------------
# 5. Setup Python Virtual Environment and Install Dependencies
# ------------------------------------------------------------------------------
VENV_DIR="$APP_DIR/venv"
log_info "Setting up Python virtual environment at $VENV_DIR..."
python3 -m venv "$VENV_DIR"

log_info "Installing Python dependencies inside virtual environment..."
"$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel || true

# Install requirements with fallback if any optional package has no distribution on older Python
if ! "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"; then
    log_warning "Pip encountered an issue with one or more packages in requirements.txt. Retrying core dependencies..."
    # Guarantee essential packages for Ubuntu 20.04 (Python 3.8)
    "$VENV_DIR/bin/pip" install "python-chess>=1.999" "requests>=2.25.0" "Flask>=2.0.0" "markdown>=3.3.0"
fi
chown -R "$TARGET_USER:$TARGET_USER" "$VENV_DIR"

# ------------------------------------------------------------------------------
# 6. Secure Prompt for GEMINI_API_KEY & Port Configuration
# ------------------------------------------------------------------------------
ENV_FILE="/etc/chess-coach.env"
EXISTING_KEY=""
EXISTING_PORT="5000"

if [ -f "$ENV_FILE" ]; then
    EXISTING_KEY=$(grep -E '^GEMINI_API_KEY=' "$ENV_FILE" | cut -d '=' -f2- || true)
    FOUND_PORT=$(grep -E '^PORT=' "$ENV_FILE" | cut -d '=' -f2- || true)
    if [ -n "$FOUND_PORT" ]; then
        EXISTING_PORT="$FOUND_PORT"
    fi
fi

USER_API_KEY="${GEMINI_API_KEY:-$EXISTING_KEY}"

if [ -z "$USER_API_KEY" ]; then
    echo ""
    echo -e "${YELLOW}================================================================${NC}"
    echo -e "${YELLOW}             GOOGLE GEMINI FLASH API KEY SETUP                  ${NC}"
    echo -e "${YELLOW}================================================================${NC}"
    echo "The AI Coach uses Google Gemini 2.5 Flash via Google AI Studio."
    echo "If you already have a key, paste it below. (Input is hidden)"
    echo "If you do not have one right now, press ENTER to configure it later."
    echo ""
    read -rsp "Enter GEMINI_API_KEY: " INPUT_KEY
    echo ""
    USER_API_KEY="$INPUT_KEY"
fi

# Port prompt & configuration
APP_PORT="${PORT:-$EXISTING_PORT}"
echo ""
echo -e "${YELLOW}================================================================${NC}"
echo -e "${YELLOW}             WEB DASHBOARD PORT CONFIGURATION                   ${NC}"
echo -e "${YELLOW}================================================================${NC}"
echo "Current port setting is: $APP_PORT"
read -rp "Enter web dashboard port [default: $APP_PORT]: " INPUT_PORT
if [ -n "$INPUT_PORT" ]; then
    APP_PORT="$INPUT_PORT"
fi
log_info "Configured dashboard to listen on port $APP_PORT."

cat <<EOF > "$ENV_FILE"
# AI Chess Coach Environment Configuration
CHESS_USERNAME=Troy05x
GEMINI_API_KEY=${USER_API_KEY}
DATA_DIR=${DATA_DIR}
STOCKFISH_PATH=${STOCKFISH_BIN}
PORT=${APP_PORT}
EOF

# Restrict permissions to protect API key
chmod 600 "$ENV_FILE"
chown "$TARGET_USER:$TARGET_USER" "$ENV_FILE"
log_success "Environment configuration saved securely to $ENV_FILE (chmod 600)."

# ------------------------------------------------------------------------------
# 6.1 UFW Firewall Configuration
# ------------------------------------------------------------------------------
log_info "Checking UFW (Uncomplicated Firewall)..."
if command -v ufw >/dev/null 2>&1; then
    echo ""
    echo -e "${YELLOW}================================================================${NC}"
    echo -e "${YELLOW}                  UFW FIREWALL DETECTED                         ${NC}"
    echo -e "${YELLOW}================================================================${NC}"
    read -rp "Allow incoming traffic on port $APP_PORT/tcp in UFW? [Y/n]: " ALLOW_UFW
    ALLOW_UFW="${ALLOW_UFW:-Y}"
    if [[ "$ALLOW_UFW" =~ ^[Yy]$ ]]; then
        log_info "Opening port $APP_PORT/tcp in UFW..."
        ufw allow "${APP_PORT}/tcp" comment "AI Chess Coach Web Dashboard"
        log_success "Port $APP_PORT/tcp allowed in UFW."
    else
        log_warning "Skipping UFW port rule for port $APP_PORT. Ensure your firewall permits access."
    fi
else
    log_info "UFW is not installed; skipping firewall rule creation."
fi

# ------------------------------------------------------------------------------
# 7. Configure Systemd Background Service (Web App Dashboard)
# ------------------------------------------------------------------------------
SERVICE_FILE="/etc/systemd/system/chess-coach.service"
log_info "Configuring systemd service ($SERVICE_FILE)..."

cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=AI Chess Coach Web Dashboard
After=network.target

[Service]
Type=simple
User=${TARGET_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python3 ${APP_DIR}/app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Hardened security & memory safety
MemoryMax=250M
CPUQuota=100%

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable chess-coach.service
systemctl restart chess-coach.service

log_success "Systemd service 'chess-coach.service' enabled and started."

# ------------------------------------------------------------------------------
# 8. Configure Cron Job (Every Day at 04:00 AM Server Time)
# ------------------------------------------------------------------------------
CRON_JOB="0 4 * * * /usr/bin/env bash -c 'set -a; [ -f /etc/chess-coach.env ] && . /etc/chess-coach.env; set +a; ${VENV_DIR}/bin/python3 ${APP_DIR}/engine.py >> /data/cron_engine.log 2>&1'"

CRON_FILE="/etc/cron.d/chess-coach-daily"
log_info "Setting up daily automated 04:00 AM cron trigger at $CRON_FILE..."

cat <<EOF > "$CRON_FILE"
# Trigger AI Chess Coach daily at 04:00 AM
0 4 * * * ${TARGET_USER} /usr/bin/env bash -c 'set -a; [ -f ${ENV_FILE} ] && . ${ENV_FILE}; set +a; ${VENV_DIR}/bin/python3 ${APP_DIR}/engine.py >> ${DATA_DIR}/cron_engine.log 2>&1'
EOF

chmod 644 "$CRON_FILE"
systemctl restart cron || systemctl restart crond || true
log_success "Crontab scheduled for 04:00 AM server time every morning."

# ------------------------------------------------------------------------------
# 9. Deployment Verification
# ------------------------------------------------------------------------------
sleep 2
if systemctl is-active --quiet chess-coach.service; then
    log_success "chess-coach.service is ACTIVE and running!"
else
    log_warning "chess-coach.service status check returned non-active. Checking logs:"
    journalctl -u chess-coach.service -n 10 --no-pager || true
fi

echo ""
echo -e "${GREEN}================================================================${NC}"
echo -e "${GREEN}         AI CHESS COACH DEPLOYMENT COMPLETE!                     ${NC}"
echo -e "${GREEN}================================================================${NC}"
echo "Dashboard URL: http://<YOUR_SERVER_IP>:${APP_PORT}"
echo "Data Directory: $DATA_DIR"
echo "Environment File: $ENV_FILE"
echo "Cron Schedule: Daily at 04:00 AM"
echo ""
echo "Helpful commands:"
echo "  Check dashboard service: sudo systemctl status chess-coach"
echo "  Restart dashboard:       sudo systemctl restart chess-coach"
echo "  Run analyzer manually:   sudo -u $TARGET_USER /usr/bin/env bash -c 'set -a; . /etc/chess-coach.env; set +a; $VENV_DIR/bin/python3 $APP_DIR/engine.py'"
echo "  View cron logs:          cat $DATA_DIR/cron_engine.log"
echo ""
