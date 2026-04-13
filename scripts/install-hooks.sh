#!/usr/bin/env bash
# install-hooks.sh — Install git hooks in the Nova source repo so CLAUDE.md
# is regenerated automatically whenever Nova is updated via git pull/merge.
#
# Run once after nova-selfheal installation:
#   sudo bash /opt/nova-selfheal/scripts/install-hooks.sh

set -euo pipefail

NOVA_PATH="${NOVA_PATH:-/opt/avatar-server}"
SELFHEAL_PATH="${SELFHEAL_PATH:-/opt/nova-selfheal}"
HOOKS_DIR="$NOVA_PATH/.git/hooks"

if [[ ! -d "$HOOKS_DIR" ]]; then
  echo "ERROR: $HOOKS_DIR not found — is $NOVA_PATH a git repo?" >&2
  exit 1
fi

HOOK="$HOOKS_DIR/post-merge"

cat > "$HOOK" << 'EOF'
#!/usr/bin/env bash
# post-merge: regenerate CLAUDE.md after every git pull/merge in Nova repo
exec bash /opt/nova-selfheal/scripts/update-context.sh
EOF

chmod +x "$HOOK"
echo "Installed post-merge hook at $HOOK"

# Run it immediately to sync current state
bash /opt/nova-selfheal/scripts/update-context.sh
