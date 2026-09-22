#!/bin/bash
# Wake ZC on new mailbox mail — event-driven (launchd WatchPaths) + poll fallback.
# Idempotent via lockdir. The inner while-loop re-drains mail that arrived DURING
# a run (launchd WatchPaths coalesces/drops those events), so nothing waits on a
# missed trigger; the 5-min poll launchd is the second belt.
#
# 09-13 hardening:
#   - exponential backoff when the drain turn fails (peak-hour glm-5.3 429 was
#     causing tight 18s retry loops); caps at 600s, resets on success
#   - drain turn runs in ~/wake-zc-ws whose project config pins main to
#     deepseek-xiaoma/deepseek-v4-flash — mail draining (read/exec/receipt)
#     needs no glm-5.3 muscle and flash survives the 14-18 peak window
#
# v0.5.0 wiring (docs/v0.5.0-实施任务书.md, HS 会审已批):
#   - REAP FIRST, COUNT SECOND (缺口3): every loop iteration reclaims stale-acked
#     mail (`python -m agent_mailbox.reap`) BEFORE counting pending — an orphaned
#     acked letter otherwise keeps PENDING at zero and the loop breaks with mail
#     still hidden (2026-09-13 t-6 incident). The lockdir below already makes the
#     script single-instance, and the store-level flock inside reap keeps it safe
#     against concurrent MCP servers: reruns are idempotent. Fail-open by design:
#     a reap failure logs an explicit line but never blocks the wake itself.
#     REAP_TTL defaults to 7200s (2h, within the agreed 1–2h band) and must stay
#     strictly below the 24h dedup window (铁1, enforced in store config loading).
#   - CIRCUIT BREAKER: after N consecutive no-progress drain rounds (pending not
#     dropping), the loop latches a breaker file and STOPS launching drain turns.
#     分层: backoff = 拉长间隔, breaker = 止损停摆 (防「空转烧 3300–5000 回合/日」
#     复发). The latch auto-expires after BREAKER_COOLDOWN seconds; delete the
#     file to resume immediately. Evidence (pending counts, round no.) is in the
#     latch file and in the log line marked "CIRCUIT BREAKER".
#
# This repo copy mirrors the deployed ~/.agent-mail/wake-zc.sh.
set -u
LOCK=/tmp/zc-wake.lock
mkdir "$LOCK" 2>/dev/null || exit 0   # a wake is already running (idempotency lock)
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

LOG=~/.agent-mail/wake-zc.log
INBOX="$HOME/.agent-mail/inbox/ZC"
WS="$HOME/wake-zc-ws"
MB_PY="/Users/interia/tools/agent-mailbox/.venv/bin/python"
REAP_AGENT="ZC"
REAP_TTL="${AGENT_MAIL_REAP_TTL:-7200}"                     # seconds; 1–2h band, < dedup 24h
BREAKER_N="${AGENT_MAIL_BREAKER_N:-5}"                      # consecutive no-progress rounds before latching
BREAKER_LATCH="$HOME/.agent-mail/wake-zc.breaker"
BREAKER_COOLDOWN="${AGENT_MAIL_BREAKER_COOLDOWN:-21600}"    # latch auto-expiry (6h)

cd "$WS"   # project config here pins the lite model for the drain turn;
           # previously cd "$HOME" — default (glm-5.3) dies on 429 at peak hours

# circuit breaker: refuse to drain while latched (unless the cooldown elapsed)
if [ -f "$BREAKER_LATCH" ]; then
  LATCH_AGE=$(( $(date +%s) - $(stat -f %m "$BREAKER_LATCH" 2>/dev/null || echo 0) ))
  if [ "$LATCH_AGE" -lt "$BREAKER_COOLDOWN" ]; then
    echo "$(date '+%F %T') BREAKER latched ${LATCH_AGE}s ago — drain suppressed (rm $BREAKER_LATCH to reset)" >> "$LOG"
    exit 0
  fi
  echo "$(date '+%F %T') breaker latch expired after ${LATCH_AGE}s — resuming drain" >> "$LOG"
  rm -f "$BREAKER_LATCH"
fi

BACKOFF=30
NO_PROGRESS=0
while true; do
  # v0.5.0: reclaim stale-acked mail BEFORE counting pending (先回收后计数) —
  # order matters: counting first would see zero and break with mail hidden.
  # Fail-open: reap problems are logged loudly but never block the wake.
  if ! "$MB_PY" -m agent_mailbox.reap --agent "$REAP_AGENT" --ttl "$REAP_TTL" >> "$LOG" 2>&1; then
    echo "$(date '+%F %T') reap FAILED (fail-open, continuing) agent=$REAP_AGENT ttl=${REAP_TTL}s" >> "$LOG"
  fi

  PENDING=$(grep -l '"status": *"pending"' "$INBOX"/*.json 2>/dev/null | wc -l | tr -d ' ')
  [ "$PENDING" = "0" ] && break
  echo "$(date '+%F %T') wake: $PENDING mail files" >> "$LOG"
  /bin/zsh -lc 'AGENT_MAIL_ID=ZC /Users/interia/.local/bin/zcode -p "信箱巡检：读 ~/.agent-mail/inbox/ZC/ 下全部 status=pending 的信（JSON：from/subject/body），逐封按内容执行；完成后给每封的发件人写回执（/Users/interia/tools/agent-mailbox/.venv/bin/python 调 agent_mailbox.store.MailStore 的 send），并把信的状态更新为 done。最后一行输出：处理 N 封。"' \
    >> "$LOG" 2>&1
  # exit code is NOT trustworthy (headless zcode can exit 0 on a failed turn) —
  # judge by outcome: mail count must drop, otherwise back off (429 peak hours)
  PENDING_AFTER=$(grep -l '"status": *"pending"' "$INBOX"/*.json 2>/dev/null | wc -l | tr -d ' ')
  if [ "$PENDING_AFTER" -ge "$PENDING" ]; then
    NO_PROGRESS=$(( NO_PROGRESS + 1 ))
    echo "$(date '+%F %T') drain made NO progress ($PENDING -> $PENDING_AFTER), round $NO_PROGRESS/$BREAKER_N, backoff ${BACKOFF}s" >> "$LOG"
    if [ "$NO_PROGRESS" -ge "$BREAKER_N" ]; then
      echo "$(date '+%F %T') CIRCUIT BREAKER: $NO_PROGRESS consecutive no-progress rounds (pending stuck at $PENDING) — latching and stopping drain turns. Reset: rm $BREAKER_LATCH" >> "$LOG"
      {
        echo "latched_at=$(date '+%F %T')"
        echo "pending_before=$PENDING pending_after=$PENDING_AFTER"
        echo "rounds=$NO_PROGRESS breaker_n=$BREAKER_N"
      } > "$BREAKER_LATCH"
      exit 1
    fi
    sleep "$BACKOFF"
    BACKOFF=$(( BACKOFF * 2 )); [ "$BACKOFF" -gt 600 ] && BACKOFF=600
  else
    NO_PROGRESS=0
    BACKOFF=30
  fi
  # drain made progress: archive finished mail via the native store API so done
  # letters stop re-triggering wake scans (root cause of the idle-loop churn)
  "$MB_PY" -c \
    "from agent_mailbox.store import MailStore; print('archived:', MailStore().archive_done('ZC'))" >> "$LOG" 2>&1
  echo "$(date '+%F %T') wake done" >> "$LOG"
done
