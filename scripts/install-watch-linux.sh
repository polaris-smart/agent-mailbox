#!/usr/bin/env bash
# Install agent-mailbox watch daemon as a systemd user service (Linux).
# All arguments are passed through to `agent-mailbox watch`, e.g.:
#   scripts/install-watch-linux.sh --notify HS --webhook-url http://localhost:8644/webhooks/agent-mailbox --webhook-secret SECRET
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || { echo "error: $PY not found — create the venv first (uv venv && uv pip install -e .)"; exit 1; }

systemctl --user status 2>/dev/null >/dev/null \
  || { echo "error: systemd user session unavailable (loginctl enable-linger $USER enables it without login)"; exit 1; }

UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/agent-mailbox-watch.service"
mkdir -p "$UNIT_DIR"

ARGS=(--root "$HOME/.agent-mail" --interval 2.0 "$@")
ARGS_CSV=""
for a in "${ARGS[@]}"; do ARGS_CSV+=" '$a'"; done

cat > "$UNIT" <<EOF
[Unit]
Description=agent-mailbox watch daemon (notifications + webhook wake-up)
After=network.target

[Service]
ExecStart=$PY -m agent_mailbox.watch$ARGS_CSV
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now agent-mailbox-watch.service
echo "[ok] installed: $UNIT"
echo "     verify: journalctl --user -u agent-mailbox-watch -f"
