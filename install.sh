#!/usr/bin/env bash
# nova-selfheal installer
# Usage: sudo bash install.sh
set -euo pipefail

INSTALL_DIR="/opt/nova-selfheal"
NOVA_PATH="/opt/avatar-server"
SERVICE_NAME="nova-selfheal"
REPO_URL="https://github.com/digitalp/nova-selfheal.git"
SUDOERS_FILE="/etc/sudoers.d/nova-selfheal"

# ── 1. Root check ─────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo bash install.sh)" >&2
    exit 1
fi

echo "=== nova-selfheal installer ==="

# ── 2. System dependencies ────────────────────────────────────────────────────
echo "→ Installing system dependencies..."
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv patch git curl sudo

# ── 3. Node.js (required for Claude CLI) ──────────────────────────────────────
if ! command -v node &>/dev/null; then
    echo "→ Installing Node.js 20.x..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
else
    echo "→ Node.js already installed: $(node --version)"
fi

# ── 4. Claude Code CLI ────────────────────────────────────────────────────────
if ! command -v claude &>/dev/null; then
    echo "→ Installing Claude Code CLI..."
    npm install -g @anthropic-ai/claude-code
else
    echo "→ Claude CLI already installed: $(claude --version 2>&1 | head -1)"
fi

# Validate claude is callable
if ! claude --version &>/dev/null; then
    echo "ERROR: claude CLI installed but not callable — check PATH" >&2
    exit 1
fi

# ── 5. Clone or update repo ───────────────────────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "→ Updating existing installation..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    echo "→ Cloning nova-selfheal..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# ── 6. Python virtual environment ────────────────────────────────────────────
echo "→ Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q
echo "→ Python dependencies installed."

# ── 7. Secrets / .env ────────────────────────────────────────────────────────
if [[ -f "$INSTALL_DIR/.env" ]]; then
    echo "→ .env already exists — skipping secrets prompt."
    echo "   Edit $INSTALL_DIR/.env to update credentials."
else
    echo ""
    echo "=== Telegram Bot Setup ==="
    echo "Create a bot via @BotFather on Telegram to get a token."
    echo "To find your chat ID, message @userinfobot."
    echo ""
    read -rp "Telegram Bot Token: " TG_TOKEN
    read -rp "Telegram Chat ID (numeric): " TG_CHAT

    cat > "$INSTALL_DIR/.env" <<EOF
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_CHAT_ID=${TG_CHAT}
NOVA_PATH=/opt/avatar-server
WATCH_SERVICE=avatar-backend
CLAUDE_WORK_DIR=/opt/avatar-server
APPROVAL_TIMEOUT_SECONDS=1800
DEDUP_WINDOW_SECONDS=1800
CLAUDE_TIMEOUT_SECONDS=120
LOG_LEVEL=INFO
EOF
    chmod 600 "$INSTALL_DIR/.env"
    echo "→ Secrets written to $INSTALL_DIR/.env (chmod 600)"
fi

# ── 8. Write CLAUDE.md into Nova's working directory ─────────────────────────
if [[ -d "$NOVA_PATH" ]]; then
    cp "$INSTALL_DIR/CLAUDE.md" "$NOVA_PATH/CLAUDE.md"
    echo "→ CLAUDE.md written to $NOVA_PATH/CLAUDE.md"
else
    echo "WARNING: Nova path $NOVA_PATH not found — CLAUDE.md not installed."
    echo "         Run: cp $INSTALL_DIR/CLAUDE.md <your-nova-path>/CLAUDE.md"
fi

# ── 9. Sudoers drop-in ────────────────────────────────────────────────────────
cat > "$SUDOERS_FILE" <<'EOF'
# nova-selfheal: allow passwordless restart of avatar-backend
root ALL=(ALL) NOPASSWD: /bin/systemctl restart avatar-backend
root ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart avatar-backend
EOF
chmod 440 "$SUDOERS_FILE"
# Validate
if ! visudo -c -f "$SUDOERS_FILE" &>/dev/null; then
    echo "ERROR: sudoers file is invalid — removing it" >&2
    rm -f "$SUDOERS_FILE"
    exit 1
fi
echo "→ Sudoers drop-in written to $SUDOERS_FILE"

# ── 10. Systemd unit ──────────────────────────────────────────────────────────
cp "$INSTALL_DIR/nova-selfheal.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo ""
echo "=== Installation complete ==="
echo ""
systemctl status "$SERVICE_NAME" --no-pager -l
echo ""
echo "Useful commands:"
echo "  journalctl -u nova-selfheal -f    # Follow logs"
echo "  systemctl restart nova-selfheal   # Restart"
echo "  systemctl stop nova-selfheal      # Stop"
echo ""
echo "To test: inject a fake ERROR into the journal:"
echo "  systemd-cat -t avatar-backend -p err '{\"event\":\"test.error\",\"level\":\"error\",\"logger\":\"avatar_backend.services.ha_proxy\",\"exc_type\":\"TestError\",\"exc\":\"\",\"timestamp\":\"2026-01-01T00:00:00\"}'"
