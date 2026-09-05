#!/usr/bin/env bash
# agent-mailbox → Hermes webhook bridge
# 用法: agent-mailbox watch --once 的输出 (JSON lines) 通过 stdin 传入；
# 每条 new_message 转发到 Hermes gateway 的 webhook 路由，触发 agent 唤醒。
# 配合 launchd/cron 每分钟跑: watch --once | mailbox-webhook-bridge.sh
set -euo pipefail

HERMES_WEBHOOK_URL="${HERMES_WEBHOOK_URL:-http://127.0.0.1:8377/webhooks/agent-mailbox}"
HERMES_WEBHOOK_SECRET="${HERMES_WEBHOOK_SECRET:-}"

while IFS= read -r line; do
  [ -z "$line" ] && continue
  event=$(echo "$line" | /usr/bin/python3 -c "import sys,json; print(json.load(sys.stdin).get('event',''))" 2>/dev/null || true)
  [ "$event" != "new_message" ] && continue
  payload=$(echo "$line" | /usr/bin/python3 -c "
import sys, json
d = json.load(sys.stdin)['message']
print(json.dumps({
    'event': 'agent_mailbox_new_message',
    'id': d.get('id',''),
    'from': d.get('from',''),
    'to': d.get('to',''),
    'subject': d.get('subject',''),
    'priority': d.get('priority',''),
}, ensure_ascii=False))
" 2>/dev/null) || continue
  if [ -n "$HERMES_WEBHOOK_SECRET" ]; then
    sig=$(echo -n "$payload" | openssl dgst -sha256 -hmac "$HERMES_WEBHOOK_SECRET" -hex | awk '{print $2}')
    curl -s --max-time 10 -X POST "$HERMES_WEBHOOK_URL" \
      -H "Content-Type: application/json" \
      -H "X-Hermes-Signature: sha256=$sig" \
      -d "$payload" > /dev/null || true
  else
    curl -s --max-time 10 -X POST "$HERMES_WEBHOOK_URL" \
      -H "Content-Type: application/json" \
      -d "$payload" > /dev/null || true
  fi
done
