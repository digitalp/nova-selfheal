#!/usr/bin/env bash
# update-context.sh — Regenerate CLAUDE.md from live Nova source and deploy it.
#
# Run automatically via git post-merge hook in /opt/avatar-server/.git/hooks/
# or manually: sudo bash /opt/nova-selfheal/scripts/update-context.sh
#
# What it does:
#   1. Generates a fresh service inventory by scanning avatar_backend/
#   2. Merges it with the static sections from /opt/nova-selfheal/CLAUDE.md
#   3. Writes the result to /opt/avatar-server/CLAUDE.md (where claude CLI reads it)

set -euo pipefail

NOVA_PATH="${NOVA_PATH:-/opt/avatar-server}"
SELFHEAL_PATH="${SELFHEAL_PATH:-/opt/nova-selfheal}"
DEST="$NOVA_PATH/CLAUDE.md"
SRC="$SELFHEAL_PATH/CLAUDE.md"

if [[ ! -f "$SRC" ]]; then
  echo "ERROR: $SRC not found — is nova-selfheal installed?" >&2
  exit 1
fi

# Build dynamic service file list from live source tree
SERVICES=$(find "$NOVA_PATH/avatar_backend/services" -name "*.py" -not -name "__init__.py" \
  | sort \
  | while read -r f; do
      base=$(basename "$f" .py)
      echo "- \`services/${base}.py\`"
    done)

ROUTERS=$(find "$NOVA_PATH/avatar_backend/routers" -name "*.py" -not -name "__init__.py" \
  | sort \
  | while read -r f; do
      base=$(basename "$f" .py)
      echo "- \`routers/${base}.py\`"
    done)

# Stamp and copy base CLAUDE.md to destination, injecting dynamic file list
{
  # Header with timestamp
  echo "# Nova AI Backend — Architecture Context for Self-Heal Agent"
  echo ""
  echo "> **Auto-generated.** Last updated: $(date '+%Y-%m-%d %H:%M %Z') by update-context.sh"
  echo "> Source: \`$NOVA_PATH\`"
  echo ""

  # Emit everything from SRC after the first heading (skip original header lines)
  tail -n +3 "$SRC"

  # Append live file inventory
  echo ""
  echo "---"
  echo ""
  echo "## Live File Inventory (auto-generated)"
  echo ""
  echo "### Services (\`avatar_backend/services/\`)"
  echo "$SERVICES"
  echo ""
  echo "### Routers (\`avatar_backend/routers/\`)"
  echo "$ROUTERS"
} > "$DEST"

echo "CLAUDE.md updated at $DEST ($(wc -l < "$DEST") lines)"
