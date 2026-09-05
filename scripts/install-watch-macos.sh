#!/usr/bin/env bash
# Install agent-mailbox watch daemon as a launchd service (macOS).
# All arguments are passed through to `agent-mailbox watch`, e.g.:
#   scripts/install-watch-macos.sh --notify HS --webhook-url http://localhost:8644/webhooks/agent-mailbox --webhook-secret SECRET
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || { echo "error: $PY not found — create the venv first (uv venv && uv pip install -e .)"; exit 1; }

LABEL="com.agent-mailbox.watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# stop any previous instance before rewriting the plist
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

ARGS=(--root "$HOME/.agent-mail" --interval 2.0 "$@")

{
  echo '<?xml version="1.0" encoding="UTF-8"?>'
  echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
  echo '<plist version="1.0"><dict>'
  echo "  <key>Label</key><string>$LABEL</string>"
  echo '  <key>ProgramArguments</key><array>'
  echo "    <string>$PY</string>"
  echo '    <string>-m</string><string>agent_mailbox.watch</string>'
  for a in "${ARGS[@]}"; do printf '    <string>%s</string>\n' "$a"; done
  echo '  </array>'
  echo '  <key>RunAtLoad</key><true/>'
  echo '  <key>KeepAlive</key><true/>'
  echo "  <key>StandardOutPath</key><string>$HOME/.agent-mail/watch.log</string>"
  echo "  <key>StandardErrorPath</key><string>$HOME/.agent-mail/watch.err</string>"
  echo '</dict></plist>'
} > "$PLIST"

launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "[ok] installed: $PLIST"
echo "     log: ~/.agent-mail/watch.log  tail -f to verify"
