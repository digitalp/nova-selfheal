# nova-selfheal

Autonomous self-healing agent for Nova AI backend.

Watches the Nova systemd service journal for `ERROR`-level log entries, invokes Claude Code CLI to propose a fix as a unified diff, sends the proposal to you via Telegram for approval, and automatically applies the patch and restarts the service on your tap.

**Zero modifications to Nova's source code required.**

---

## How It Works

```
journalctl (nova journal)
    → JournalWatcher     — parses ERROR-level structlog JSON entries
    → ErrorDeduplicator  — suppresses repeated errors within 30 min
    → ContextBuilder     — maps logger name → source .py file
    → ClaudeAgent        — invokes `claude --print` with error + source context
    → PatchExtractor     — extracts SUMMARY + unified diff from Claude output
    → TelegramApprovalBot — sends proposal with Approve / Reject inline buttons
        [Approve] → PatchApplier — patch -p1 dry-run then apply
                              → sudo systemctl restart <service>
                              → sends result to Telegram
        [Reject]  → logged, no action
```

---

## Prerequisites

- Ubuntu/Debian host with `systemd`
- Nova AI backend running as a systemd service (default: `avatar-backend`)
- Root/sudo access on the host
- A Telegram bot token (create via [@BotFather](https://t.me/BotFather))
- Your Telegram numeric chat ID (get from [@userinfobot](https://t.me/userinfobot))
- An Anthropic API key (for Claude Code CLI)

---

## Installation

```bash
# 1. Download the installer
curl -fsSL https://raw.githubusercontent.com/digitalp/nova-selfheal/main/install.sh -o /tmp/install.sh

# 2. Run as root
sudo bash /tmp/install.sh
```

The installer will:
1. Install system dependencies (`python3`, `python3-venv`, `patch`, `git`, `curl`)
2. Install Node.js 20.x (if not present)
3. Install the Claude Code CLI (`npm install -g @anthropic-ai/claude-code`)
4. Clone this repo to `/opt/nova-selfheal/`
5. Create a Python virtual environment and install dependencies
6. Prompt for Telegram bot token and chat ID → write `/opt/nova-selfheal/.env`
7. Copy `CLAUDE.md` to `/opt/avatar-server/CLAUDE.md` (Nova's working directory)
8. Write a sudoers drop-in for passwordless service restart
9. Install, enable, and start the `nova-selfheal` systemd service

### Claude Code Authentication

After installation, you must authenticate Claude Code:

```bash
claude auth login
```

Follow the browser prompt to connect your Anthropic account. The credentials are stored per-user in `~/.claude/`.

---

## Configuration

All configuration lives in `/opt/nova-selfheal/.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | *(required)* | Your numeric Telegram chat ID |
| `NOVA_PATH` | `/opt/avatar-server` | Path to Nova's source tree |
| `WATCH_SERVICE` | `avatar-backend` | Systemd service name to watch |
| `CLAUDE_WORK_DIR` | `/opt/avatar-server` | Working directory for `claude` CLI (CLAUDE.md is read from here) |
| `APPROVAL_TIMEOUT_SECONDS` | `1800` | Auto-reject pending fixes after this many seconds (default: 30 min) |
| `DEDUP_WINDOW_SECONDS` | `1800` | Suppress duplicate `(event, exc_type)` pairs within this window |
| `CLAUDE_TIMEOUT_SECONDS` | `120` | Timeout for Claude CLI subprocess |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

After editing `.env`:

```bash
systemctl restart nova-selfheal
```

---

## Usage

### Check service status

```bash
systemctl status nova-selfheal
journalctl -u nova-selfheal -f
```

### Telegram flow

When Nova logs an ERROR, you'll receive a Telegram message like:

```
🔴 Nova ERROR detected

Event: ha_proxy.camera_error
Exception: RemoteProtocolError
File: avatar_backend/services/ha_proxy.py

Claude's analysis:
The camera fetch is timing out during high-load periods. A retry with
backoff should resolve the intermittent failures.

Proposed diff:
--- a/avatar_backend/services/ha_proxy.py
+++ b/avatar_backend/services/ha_proxy.py
@@ -42,7 +42,8 @@
...

[✅ Approve]  [❌ Reject]
```

Tap **Approve** to apply the patch and restart Nova automatically.
Tap **Reject** (or wait 30 minutes) to dismiss without any changes.

### Inject a test error

```bash
systemd-cat -t avatar-backend -p err '{"event":"test.error","level":"error","logger":"avatar_backend.services.ha_proxy","exc_type":"TestError","exc":"","timestamp":"2026-01-01T00:00:00"}'
```

You should receive a Telegram message within ~5 seconds.

---

## Uninstall

```bash
systemctl stop nova-selfheal
systemctl disable nova-selfheal
rm /etc/systemd/system/nova-selfheal.service
systemctl daemon-reload
rm -f /etc/sudoers.d/nova-selfheal
rm -rf /opt/nova-selfheal
rm -f /opt/avatar-server/CLAUDE.md
```

---

## Troubleshooting

### "claude CLI not found after install"

Claude Code requires Node.js ≥ 18. Check:

```bash
node --version
which claude
claude --version
```

If `claude` is installed but not in root's PATH:

```bash
echo $PATH
# Add /usr/local/bin or wherever npm installs globals
```

### "Telegram messages not received"

- Verify bot token and chat ID in `/opt/nova-selfheal/.env`
- Ensure the bot has been started (send `/start` to your bot)
- Check logs: `journalctl -u nova-selfheal -f`

### "Patch apply failed"

The dry-run output will appear in logs and in the Telegram reject message. Common causes:
- The error was in a file that has since been modified — the diff no longer applies cleanly
- Claude proposed a multi-file diff (only single-file patches are supported)

### "Claude invocation failed"

- Run `claude --version` as root to verify the CLI works
- Run `claude auth login` if credentials have expired
- Check `CLAUDE_TIMEOUT_SECONDS` — increase if Claude is taking too long

---

## Architecture

```
/opt/nova-selfheal/
├── .env                    # Secrets (chmod 600)
├── .venv/                  # Python virtual environment
├── dedup.db                # SQLite deduplication database
├── CLAUDE.md               # Template (also copied to Nova path)
├── requirements.txt
├── nova-selfheal.service
└── nova_selfheal/
    ├── main.py             # Asyncio entrypoint, signal handlers
    ├── config.py           # pydantic-settings
    ├── models.py           # NovaError + PendingFix dataclasses
    ├── watcher.py          # journalctl subprocess → NovaError queue
    ├── deduplicator.py     # SQLite-backed 30min dedup window
    ├── context_builder.py  # logger name → source file path
    ├── claude_agent.py     # claude --print subprocess invocation
    ├── patch_extractor.py  # SUMMARY/DIFF regex extraction + validation
    ├── patch_applier.py    # patch -p1 dry-run → apply → restart
    └── telegram_bot.py     # python-telegram-bot long-polling approval flow
```

---

## Credits

**Idea & Co-Author:** Tangu Penn

---

## License

MIT
