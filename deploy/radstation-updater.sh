#!/usr/bin/env bash
# =============================================================================
# radstation-updater.sh
# Installed to: /usr/local/bin/radstation-updater.sh
# Triggered by: radstation-updater.timer (every 15 minutes)
#
# Update flow:
#   git fetch → compare hashes → backup → pull → syntax check →
#   restart service → health check (45 s) → rollback on failure
#
# Rollback levels
# ───────────────
#   Level 1  syntax error detected   → git reset --hard; service NOT touched
#   Level 2  service dead after 45 s → restore backup + git reset; restart
#   Level 3  rollback service dead   → send CRITICAL Telegram alert; give up
#
# Environment variables (set in radstation-updater.service [Service] block):
#   TELEGRAM_TOKEN   bot token from @BotFather
#   CHAT_ID          numeric Telegram chat ID to receive update notifications
# =============================================================================

set -uo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
REPO_DIR="/home/navenger/radstat"
VENV_PYTHON="${REPO_DIR}/venv/bin/python3"
SCRIPT_NAME="RadStation_v3.py"
SERVICE="radstation"
BRANCH="main"
HEALTH_WAIT=45          # seconds to wait before deciding the restart succeeded
LOG_TAG="radstation-updater"

TELEGRAM_TOKEN="${TELEGRAM_TOKEN:-}"
CHAT_ID="${CHAT_ID:-}"

# ── Helpers ───────────────────────────────────────────────────────────────────
log() {
    local level="$1"; shift
    logger -t "$LOG_TAG" -p "user.${level}" "$*"
    printf '%s [%-7s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${level^^}" "$*"
}

# Non-fatal Telegram notification via curl — never blocks the update logic
tg_notify() {
    [[ -z "$TELEGRAM_TOKEN" || -z "$CHAT_ID" ]] && return 0
    curl -s --max-time 10 \
        --data-urlencode "text=$1" \
        -d "chat_id=${CHAT_ID}&parse_mode=Markdown" \
        "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
        >/dev/null 2>&1 || true
}

# Restore pre-pull state and restart the service
rollback() {
    local pre_hash="$1"
    local backup="${REPO_DIR}/${SCRIPT_NAME}.backup"

    log "err" "Initiating rollback to ${pre_hash:0:7}"

    # Restore file-level backup first (faster than git reset for the service)
    if [[ -f "$backup" ]]; then
        cp "$backup" "${REPO_DIR}/${SCRIPT_NAME}"
        log "notice" "Backup file restored."
    fi

    # Align git state with the known-good commit
    git -C "$REPO_DIR" reset --hard "$pre_hash" --quiet
    log "notice" "Git HEAD reset to ${pre_hash:0:7}."

    # Restart and give the service a moment to settle
    systemctl restart "$SERVICE"
    sleep 15

    if systemctl is-active --quiet "$SERVICE"; then
        log "notice" "Rollback SUCCESSFUL. Running ${pre_hash:0:7}."
        tg_notify "⚠️ *RadStation*: Update failed, auto-rolled back to \`${pre_hash:0:7}\`. Service is healthy."
    else
        log "crit" "CRITICAL: Rollback FAILED. Service is DOWN. Manual SSH required."
        tg_notify "🚨 *RadStation CRITICAL* 🚨\nUpdate AND rollback both failed. Service is *DOWN*. SSH intervention required immediately."
    fi
}

# ── Main ──────────────────────────────────────────────────────────────────────
cd "$REPO_DIR"

# ── Step 1: Fetch remote refs (read-only, no file changes yet) ────────────────
if ! git fetch origin "$BRANCH" --quiet 2>&1; then
    log "warning" "git fetch failed — no network? Skipping update check."
    exit 0
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/${BRANCH}")

if [[ "$LOCAL" == "$REMOTE" ]]; then
    log "info" "Already at ${LOCAL:0:7}. Nothing to do."
    exit 0
fi

log "notice" "Update detected: ${LOCAL:0:7} → ${REMOTE:0:7}"

# Determine whether the runtime Python script actually changed in this delta.
# If only the .ino or README changed, we can skip the service restart.
PYTHON_CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE" | grep -c "^${SCRIPT_NAME}$" || true)

# ── Step 2: Back up the current working script ────────────────────────────────
cp "${SCRIPT_NAME}" "${SCRIPT_NAME}.backup"
log "info" "Backup saved → ${SCRIPT_NAME}.backup"

# ── Step 3: Pull ──────────────────────────────────────────────────────────────
if ! git pull origin "$BRANCH" --quiet 2>&1; then
    log "err" "git pull failed. Restoring backup and aborting."
    cp "${SCRIPT_NAME}.backup" "${SCRIPT_NAME}"
    rm -f "${SCRIPT_NAME}.backup"
    exit 1
fi

log "info" "git pull complete → ${REMOTE:0:7}"

# ── Step 4: Syntax validation ─────────────────────────────────────────────────
# Run BEFORE restarting the service. A syntax error is caught here and the
# service is never interrupted — the Pi keeps running the previous version.
if ! "${VENV_PYTHON}" -m py_compile "${SCRIPT_NAME}" 2>&1; then
    log "err" "Syntax check FAILED on ${REMOTE:0:7}. Reverting without restart."
    cp "${SCRIPT_NAME}.backup" "${SCRIPT_NAME}"
    git reset --hard "$LOCAL" --quiet
    rm -f "${SCRIPT_NAME}.backup"
    tg_notify "⚠️ *RadStation*: Update \`${REMOTE:0:7}\` rejected — syntax error. Service untouched, still on \`${LOCAL:0:7}\`."
    exit 1
fi

log "info" "Syntax check passed."

# ── Step 5: Restart service (only if the Python script changed) ───────────────
if [[ "$PYTHON_CHANGED" -gt 0 ]]; then
    log "info" "Python script changed. Restarting service..."
    systemctl restart "$SERVICE"

    log "info" "Waiting ${HEALTH_WAIT}s for health check..."
    sleep "$HEALTH_WAIT"

    # ── Step 6: Health check ──────────────────────────────────────────────────
    if systemctl is-active --quiet "$SERVICE"; then
        log "notice" "Health check PASSED. Update ${REMOTE:0:7} deployed successfully."
        tg_notify "✅ *RadStation updated* to \`${REMOTE:0:7}\`. Service healthy."
        rm -f "${SCRIPT_NAME}.backup"
    else
        log "err" "Health check FAILED (service inactive after ${HEALTH_WAIT}s). Rolling back."
        rollback "$LOCAL"
        exit 1
    fi

else
    # Non-runtime change (firmware, docs, etc.) — no restart needed
    log "notice" "No Python changes in ${REMOTE:0:7}. Skipping service restart."
    tg_notify "✅ *RadStation*: Non-runtime update \`${REMOTE:0:7}\` applied (no restart needed)."
    rm -f "${SCRIPT_NAME}.backup"
fi

exit 0
