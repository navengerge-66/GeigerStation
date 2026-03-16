#!/usr/bin/env bash
# =============================================================================
# install.sh — One-shot RadStation setup for Raspberry Pi
#
# Run once as root (or with sudo) on a fresh Pi:
#   sudo bash install.sh
#
# What this does:
#   1. Installs system dependencies (git, python3-venv, curl)
#   2. Clones (or updates) the GitHub repo to INSTALL_DIR
#   3. Creates a Python venv at INSTALL_DIR/venv and installs packages into it
#   4. Installs the updater script to /usr/local/bin
#   5. Installs and enables systemd units for both the service and the timer
#   6. Prompts for Telegram credentials and writes them into the unit files
#
# Re-running this script is safe — it updates in place without data loss.
# =============================================================================

set -euo pipefail

# ── Configuration — adjust if your paths differ ───────────────────────────────
REPO_URL="https://github.com/navengerge-66/GeigerStation"
INSTALL_DIR="/home/navenger/radstat"
SERVICE_USER="navenger"
SYSTEMD_DIR="/etc/systemd/system"
UPDATER_BIN="/usr/local/bin/radstation-updater.sh"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { printf "${GREEN}[OK]${NC}  %s\n" "$*"; }
warn() { printf "${YELLOW}[WARN]${NC} %s\n" "$*"; }
die()  { printf "${RED}[FAIL]${NC} %s\n" "$*" >&2; exit 1; }
step() { printf "\n${YELLOW}▶ %s${NC}\n" "$*"; }

# ── Must run as root ──────────────────────────────────────────────────────────
[[ "$(id -u)" -eq 0 ]] || die "Run this script with sudo."

# ── Step 1: System packages ───────────────────────────────────────────────────
step "Installing system packages"
apt-get update --allow-releaseinfo-change -qq
# python3-venv is required to create the isolated environment (PEP 668 safe)
apt-get install -y --no-install-recommends git python3-venv curl
ok "System packages ready."

# ── Step 2: Clone or update repo ──────────────────────────────────────────────
step "Setting up repository at ${INSTALL_DIR}"
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    # Already a git repo — just pull the latest commits
    warn "Repo already initialised — pulling latest."
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull --quiet

elif [[ -d "$INSTALL_DIR" ]]; then
    # Directory exists with data but is NOT yet a git repo.
    # Initialise git in-place so existing .csv / .log files are never touched.
    # git only writes the files that are tracked in the remote (py, ino, etc.).
    warn "Directory exists with data. Initialising git repo in place..."
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" init --quiet
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" remote add origin "$REPO_URL"
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" fetch origin main --quiet
    # -f overwrites any pre-existing tracked files (e.g. an old RadStation.py)
    # with the canonical versions from GitHub; untracked data files are ignored.
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" checkout -f -b main origin/main
    ok "Git repo initialised inside existing data directory."

else
    # Fresh install — nothing in the way, plain clone
    sudo -u "$SERVICE_USER" git clone "$REPO_URL" "$INSTALL_DIR"
fi

# Ensure data directories exist
sudo -u "$SERVICE_USER" mkdir -p "${INSTALL_DIR}/archive"
ok "Repository ready."

# ── Step 3: Python virtual environment + packages ────────────────────────────
# Modern Raspberry Pi OS (Bookworm/Trixie) enforces PEP 668 which blocks
# system-wide pip installs. We create an isolated venv instead.
step "Creating Python virtual environment and installing packages"
VENV_DIR="${INSTALL_DIR}/venv"
if [[ ! -d "$VENV_DIR" ]]; then
    sudo -u "$SERVICE_USER" python3 -m venv "$VENV_DIR"
    ok "Virtual environment created at ${VENV_DIR}."
else
    warn "Virtual environment already exists — upgrading packages."
fi

sudo -u "$SERVICE_USER" "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
sudo -u "$SERVICE_USER" "${VENV_DIR}/bin/pip" install --quiet --upgrade \
    -r "${INSTALL_DIR}/requirements.txt"
ok "Python packages installed into venv."

# ── Step 4: Telegram credentials ──────────────────────────────────────────────
step "Telegram credentials"
# Always pre-initialise so set -u never sees an unbound variable.
TG_TOKEN=""
TG_CHAT=""

# When the script is piped through `curl | sudo bash`, stdin is the pipe not
# the terminal.  Redirect read to /dev/tty so the user can type interactively.
read -r -p "  Enter your Telegram Bot Token (or press Enter to skip): " TG_TOKEN </dev/tty || true
read -r -p "  Enter your Telegram Chat ID   (or press Enter to skip): " TG_CHAT  </dev/tty || true

if [[ -z "$TG_TOKEN" ]]; then
    warn "No token provided — notifications disabled. Edit the unit files later:"
    warn "  sudo systemctl edit radstation          (add TELEGRAM_TOKEN)"
    warn "  sudo systemctl edit radstation-updater  (add TELEGRAM_TOKEN + CHAT_ID)"
    TG_TOKEN="YOUR_TOKEN_HERE"
    TG_CHAT="YOUR_CHAT_ID_HERE"
fi

# ── Step 5: Write systemd unit files ─────────────────────────────────────────
step "Installing systemd units"

# Substitute credentials into the service files before copying
sed "s|YOUR_TOKEN_HERE|${TG_TOKEN}|g" \
    "${INSTALL_DIR}/deploy/radstation.service" \
    > "${SYSTEMD_DIR}/radstation.service"

sed -e "s|YOUR_TOKEN_HERE|${TG_TOKEN}|g" \
    -e "s|YOUR_CHAT_ID_HERE|${TG_CHAT}|g" \
    "${INSTALL_DIR}/deploy/radstation-updater.service" \
    > "${SYSTEMD_DIR}/radstation-updater.service"

cp "${INSTALL_DIR}/deploy/radstation-updater.timer" \
    "${SYSTEMD_DIR}/radstation-updater.timer"

ok "Unit files written to ${SYSTEMD_DIR}."

# ── Step 6: Install updater script ───────────────────────────────────────────
step "Installing updater script"
install -m 755 "${INSTALL_DIR}/deploy/radstation-updater.sh" "$UPDATER_BIN"
ok "Updater installed at ${UPDATER_BIN}."

# ── Step 7: Enable and start services ────────────────────────────────────────
step "Enabling and starting services"
systemctl daemon-reload

systemctl enable radstation.service
systemctl restart radstation.service
ok "radstation.service started."

systemctl enable radstation-updater.timer
systemctl start radstation-updater.timer
ok "radstation-updater.timer enabled."

# ── Summary ───────────────────────────────────────────────────────────────────
printf "\n${GREEN}═══════════════════════════════════════${NC}\n"
printf "${GREEN}  RadStation installation complete!${NC}\n"
printf "${GREEN}═══════════════════════════════════════${NC}\n\n"
printf "  Useful commands:\n"
printf "    %-42s %s\n" "systemctl status radstation"          "— service health"
printf "    %-42s %s\n" "systemctl status radstation-updater.timer" "— timer status"
printf "    %-42s %s\n" "journalctl -u radstation -f"          "— live service logs"
printf "    %-42s %s\n" "journalctl -u radstation-updater -f"  "— live update logs"
printf "    %-42s %s\n" "sudo systemctl start radstation-updater" "— force update check now"
printf "\n"
