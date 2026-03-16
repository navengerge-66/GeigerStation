#!/usr/bin/env bash
# =============================================================================
# install.sh — One-shot RadStation setup for Raspberry Pi
#
# Run once as root (or with sudo) on a fresh Pi:
#   sudo bash install.sh
#
# What this does:
#   1. Installs system dependencies (git, python3-pip, curl)
#   2. Installs Python package dependencies
#   3. Clones (or updates) the GitHub repo to INSTALL_DIR
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
apt-get update -qq
apt-get install -y --no-install-recommends git python3-pip python3-venv curl
ok "System packages ready."

# ── Step 2: Python packages ───────────────────────────────────────────────────
step "Installing Python packages"
pip3 install --quiet --upgrade \
    pyserial schedule pandas numpy matplotlib scipy requests
ok "Python packages installed."

# ── Step 3: Clone or update repo ──────────────────────────────────────────────
step "Setting up repository at ${INSTALL_DIR}"
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    warn "Repo already exists — pulling latest."
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull --quiet
else
    sudo -u "$SERVICE_USER" git clone "$REPO_URL" "$INSTALL_DIR"
fi

# Ensure data directories exist
sudo -u "$SERVICE_USER" mkdir -p "${INSTALL_DIR}/archive"
ok "Repository ready."

# ── Step 4: Telegram credentials ─────────────────────────────────────────────
step "Telegram credentials"
read -r -p "  Enter your Telegram Bot Token (or press Enter to skip): " TG_TOKEN
read -r -p "  Enter your Telegram Chat ID   (or press Enter to skip): " TG_CHAT

if [[ -z "$TG_TOKEN" ]]; then
    warn "No token provided — notifications disabled. Edit the .service files later."
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
