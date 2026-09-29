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
# t-50② 锚A + U2 (HS 0.7.5 收口施工单):
#   - 锚A: every drain turn appends one row to ``$MAIL_ROOT/wake-attempts.jsonl``
#     (0600, append-only, 8 fixed fields: ts/agent/route/attempt/outcome/
#     error_class/executor/latency_ms) — same shape the daemon leg writes via
#     ``record_wake_attempt()``. G1 主判据 reads this file, not process logs.
#   - U2: the first no-progress round of an episode (and the breaker latch)
#     drops an alert letter to the stuck letters' registered senders + the boss
#     box — wake health becomes readable from the mailbox face alone. Static
#     subject/body => the store's 24h semantic dedup caps alert floods.
#
# t-56 claim-first 投递修复 (HS 20260929001242 多窗重复回信·根因①):
#   - REAP FIRST, CLAIM SECOND: the loop still reaps stale-acked mail first,
#     but the old "count pending via grep + tell the session to read the whole
#     inbox" is GONE — reading the inbox directly bypasses claim entirely, so
#     two windows could both work the same letter (two done entries, four
#     replies on one thread). Instead the loop runs
#     ``python -m agent_mailbox.wake claim`` (atomic store.claim, only-scoped)
#     and hands the session the CLAIM RESULT (id/from/subject/body per letter)
#     as the delivery payload; claim 0 封 ⇒ 不拉会话、直接收工. Letters the
#     claim could not take (another window holds them in flight) are audited
#     as claim_denied by the claim action (deduped per letter, 防刷屏).
#   - 信不丢: after each turn the loop releases its still-not-done claims back
#     to pending (``wake release``), so the next round re-claims and re-drains
#     them — same recovery the old pending-count loop provided. by labels:
#     ``belt:<pid>`` lands in handled_log (J3 auditability).
#
# Environment knobs (all optional; defaults preserve historical behavior):
#   MAIL_ROOT        mail root (default ~/.agent-mail); also exported to python
#                    children as AGENT_MAIL_HOME so reap/archive/alert share it
#   WAKE_DRAIN_CMD   drain turn command override (tests/sandbox); default is the
#                    real headless zcode patrol prompt below
#   WAKE_LOCK        idempotency lockdir (default /tmp/zc-wake.lock); sandbox
#                    runs must override this or they bounce off the live belt
#
# This repo copy mirrors the deployed ~/.agent-mail/wake-zc.sh.
set -u
LOCK="${WAKE_LOCK:-/tmp/zc-wake.lock}"
mkdir "$LOCK" 2>/dev/null || exit 0   # a wake is already running (idempotency lock)
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

MAIL_ROOT="${MAIL_ROOT:-$HOME/.agent-mail}"
export AGENT_MAIL_HOME="$MAIL_ROOT"   # reap/archive/alert MailStore() root parity
LOG="$MAIL_ROOT/wake-zc.log"
INBOX="$MAIL_ROOT/inbox/ZC"
ANCHOR="$MAIL_ROOT/wake-attempts.jsonl"
WS="$HOME/wake-zc-ws"
MB_PY="/Users/interia/tools/agent-mailbox/.venv/bin/python"
REAP_AGENT="ZC"
REAP_TTL="${AGENT_MAIL_REAP_TTL:-7200}"                     # seconds; 1–2h band, < dedup 24h
BREAKER_N="${AGENT_MAIL_BREAKER_N:-5}"                      # consecutive no-progress rounds before latching
BREAKER_LATCH="$MAIL_ROOT/wake-zc.breaker"
BREAKER_COOLDOWN="${AGENT_MAIL_BREAKER_COOLDOWN:-21600}"    # latch auto-expiry (6h)

# 锚A: append one attempt row — fail-open, 0600 via umask, JSON built from
# controlled values only (ids/routes are validated upstream; error_class comes
# from the fixed vocabulary below).
anchor_attempt() { # route attempt outcome error_class executor latency_ms
  local line
  line=$(printf '{"ts":"%s","agent":"%s","route":"%s","attempt":%s,"outcome":"%s","error_class":"%s","executor":"%s","latency_ms":%s}' \
    "$(date -u +%FT%TZ)" "$REAP_AGENT" "$1" "$2" "$3" "$4" "$5" "$6")
  { umask 077; printf '%s\n' "$line" >> "$ANCHOR"; } 2>>"$LOG" || true
}

# U2: alert the responsible party (HS) + the registered senders of stuck mail.
# t-59（A-2）收件人收敛: boss 席一律不发——boss 仅存档语义，老板只看飞书，
# 自动告警发 boss 席等于没送达还制造噪音（09-29 任务书 §0.2）。急事走飞书。
# Static subject/body keeps the semantic hash stable — the store's 24h dedup
# window turns repeated calls into at most one live letter per recipient per day.
alert_wake_fail() { # reason
  "$MB_PY" - "$REAP_AGENT" "$MAIL_ROOT" "$1" <<'PYEOF' >> "$LOG" 2>&1
import json
import sys
from pathlib import Path

from agent_mailbox.store import MailStore

ALERT_RECIPIENT = "HS"  # t-59: 告警只发负责方；boss 席仅存档（老板只看飞书）

agent, root, reason = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
store = MailStore(str(root))
try:
    reg = json.loads((root / "registry.json").read_text(encoding="utf-8")).get("agents", {})
    registered = {k for k, v in reg.items() if isinstance(v, dict)}
except Exception:
    registered = set()
senders = set()
for p in (root / "inbox" / agent).glob("*.json"):
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        continue
    if not isinstance(m, dict) or m.get("status") not in ("pending", "acked"):
        continue
    f = str(m.get("from", ""))
    if f and f != agent and f != "boss" and f != ALERT_RECIPIENT and f in registered:
        senders.add(f)
recipients = [ALERT_RECIPIENT] + sorted(senders)
subject = f"[wake-fail] {agent} 唤醒通道异常"
body = (
    f"[wake-fail 自动告警] {agent} 的 drain 巡检无进展（{reason}）。\n"
    "你发给该 agent 的信可能未被处理；信仍在收件箱（信不丢）。\n"
    f"信箱根: {root}\n"
    "详见 wake-attempts.jsonl（锚A）与 wake-zc.log。此信为系统自动告警，无需回执。"
)
try:
    store.send(agent, recipients, subject, body)
    print(f"wake-fail alert sent to {recipients}")
except Exception as e:
    print(f"wake-fail alert failed (fail-open): {e}")
PYEOF
}

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
  # v0.5.0: reclaim stale-acked mail BEFORE claiming (先回收后认领) — order
  # matters: without it an orphaned acked letter stays invisible to claim.
  # Fail-open: reap problems are logged loudly but never block the wake.
  if ! "$MB_PY" -m agent_mailbox.reap --agent "$REAP_AGENT" --ttl "$REAP_TTL" >> "$LOG" 2>&1; then
    echo "$(date '+%F %T') reap FAILED (fail-open, continuing) agent=$REAP_AGENT ttl=${REAP_TTL}s" >> "$LOG"
  fi

  # t-56 claim-first: claim BEFORE delivering — the belt no longer counts
  # pending and tells the session to read the inbox (the multi-window
  # duplicate-handling root cause). The claim result IS the delivery payload:
  # each claimed letter's id/from/subject/body lands in the 0600 claim file
  # the drain turn processes; claim 0 封 ⇒ 不拉会话、直接收工. Letters claimed
  # in flight by another window are audited claim_denied inside the action.
  CLAIM_FILE="$MAIL_ROOT/wake-zc-claim.$$.json"
  CLAIM_FAILED=0
  if ! "$MB_PY" -m agent_mailbox.wake claim --agent "$REAP_AGENT" \
      --root "$MAIL_ROOT" --label "belt:$$" --out "$CLAIM_FILE" >> "$LOG" 2>&1; then
    CLAIM_FAILED=1
    echo "$(date '+%F %T') claim FAILED (fail-open, continuing) agent=$REAP_AGENT" >> "$LOG"
  fi
  CLAIMED=$("$MB_PY" -c 'import json,sys
try:
    print(len(json.load(open(sys.argv[1])).get("claimed", [])))
except Exception:
    print(0)' "$CLAIM_FILE" 2>>"$LOG")
  if [ "$CLAIMED" = "0" ]; then
    rm -f "$CLAIM_FILE"
    # t-59（A-2）失败必响: claim 基建失败（python/store 起不来）不是「没信
    # 可领」——按 rc=0 伪装成功退出就是 09-29 WB 那种事故，必须非零退出。
    if [ "$CLAIM_FAILED" = "1" ]; then
      anchor_attempt belt 1 fail claim_failed belt-claim 0
      echo "$(date '+%F %T') claim infrastructure FAILED — exit 1 (失败必响, 禁 rc=0 伪装)" >> "$LOG"
      exit 1
    fi
    break
  fi
  echo "$(date '+%F %T') wake: claimed $CLAIMED mail files (by belt:$$)" >> "$LOG"
  export AGENT_MAIL_CLAIM_FILE="$CLAIM_FILE"   # drain override (WAKE_DRAIN_CMD) reads this too

  # t-56: drain turn works the CLAIMED payload, never the raw inbox — the
  # prompt below forbids inbox reads (直读会撞上别的窗已认领在途的信).
  DRAIN_CMD="${WAKE_DRAIN_CMD:-/bin/zsh -lc 'AGENT_MAIL_ID=ZC /Users/interia/.local/bin/zcode -p \"信箱巡检（claim 认领投递）：本轮已认领给你的信件清单在 $AGENT_MAIL_CLAIM_FILE（JSON：claimed[]，每封含 id/from/subject/body），逐封按清单内容执行；不要自行读取 inbox 目录（信已认领给你，重复直读会造成多窗重复处理）；完成后给每封的发件人写回执（/Users/interia/tools/agent-mailbox/.venv/bin/python 调 agent_mailbox.store.MailStore 的 send），并把信的状态更新为 done（store.set_status）。最后一行输出：处理 N 封。\"'}"
  T0=$(date +%s)
  eval "$DRAIN_CMD" >> "$LOG" 2>&1
  # exit code is NOT trustworthy (headless zcode can exit 0 on a failed turn) —
  # judge by outcome: claimed letters must be done, otherwise back off (429 peak hours)
  DONE_N=$("$MB_PY" -c 'import json,sys
from agent_mailbox.store import MailStore
root, agent, cf = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    ids = [m["id"] for m in json.load(open(cf))["claimed"]]
except Exception:
    ids = []
store = MailStore(root)
done = 0
for i in ids:
    try:
        if store.get_letter(agent, i).get("status") == "done":
            done += 1
    except Exception:
        done += 1  # letter gone from inbox+archive = handled and cleaned up
print(done)' "$MAIL_ROOT" "$REAP_AGENT" "$CLAIM_FILE" 2>>"$LOG")
  TURN_MS=$(( ($(date +%s) - T0) * 1000 ))
  if [ "${DONE_N:-0}" -lt 1 ]; then
    NO_PROGRESS=$(( NO_PROGRESS + 1 ))
    anchor_attempt belt "$NO_PROGRESS" fail no_progress zcode-drain "$TURN_MS"
    echo "$(date '+%F %T') drain made NO progress (claimed $CLAIMED, done ${DONE_N:-0}), round $NO_PROGRESS/$BREAKER_N, backoff ${BACKOFF}s" >> "$LOG"
    if [ "$NO_PROGRESS" = "1" ]; then
      alert_wake_fail "round 1 no-progress"
    fi
    if [ "$NO_PROGRESS" -ge "$BREAKER_N" ]; then
      echo "$(date '+%F %T') CIRCUIT BREAKER: $NO_PROGRESS consecutive no-progress rounds (claimed $CLAIMED stuck at done ${DONE_N:-0}) — latching and stopping drain turns. Reset: rm $BREAKER_LATCH" >> "$LOG"
      {
        echo "latched_at=$(date '+%F %T')"
        echo "claimed=$CLAIMED done_after=${DONE_N:-0}"
        echo "rounds=$NO_PROGRESS breaker_n=$BREAKER_N"
      } > "$BREAKER_LATCH"
      alert_wake_fail "circuit breaker latched after $NO_PROGRESS rounds"
      exit 1
    fi
    sleep "$BACKOFF"
    BACKOFF=$(( BACKOFF * 2 )); [ "$BACKOFF" -gt 600 ] && BACKOFF=600
  else
    NO_PROGRESS=0
    BACKOFF=30
    anchor_attempt belt 1 ok "" zcode-drain "$TURN_MS"
  fi
  # t-56 信不丢: release this round's still-not-done claims back to pending so
  # the next iteration re-claims and re-drains them (done letters auto-skip;
  # only claims still owned by label belt:$$ are touched).
  if ! "$MB_PY" -m agent_mailbox.wake release --agent "$REAP_AGENT" \
      --root "$MAIL_ROOT" --label "belt:$$" --claim-file "$CLAIM_FILE" >> "$LOG" 2>&1; then
    echo "$(date '+%F %T') release FAILED (fail-open, continuing) agent=$REAP_AGENT" >> "$LOG"
  fi
  rm -f "$CLAIM_FILE"
  # drain made progress: archive finished mail via the native store API so done
  # letters stop re-triggering wake scans (root cause of the idle-loop churn)
  "$MB_PY" -c \
    "from agent_mailbox.store import MailStore; print('archived:', MailStore().archive_done('ZC'))" >> "$LOG" 2>&1
  echo "$(date '+%F %T') wake done" >> "$LOG"
done
