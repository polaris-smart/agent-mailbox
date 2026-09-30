#!/bin/bash
# acceptance-user-path.sh — agent-mailbox 用户路径验收脚本（0.7.6 收口批第 3 批 · G-6+G-7）
#
# 判定人 = HS（亲自跑拿输出原文）。本脚本只把「用户路径」固化成可复跑断言：
# 一行命令自造干净环境（临时 HOME + 临时信箱根，mktemp + trap 清理），
# 站在用户位置走 G-7 用户路径：装 → 配一次模型面 → 加 agent 只选名字 →
# 发测试信 → 自动处理标 done；全程 0 手写唤醒脚本 · 0 环境变量 · 0 手改文件。
#
# 设计铁律（HS 明令）：
#   1. 本脚本允许是红的——实现对不上就 FAIL 并输出原因，这是它的设计目的；
#      它要能发现真问题，不许写成永远绿。
#   2. 每条断言输出可判定结果（PASS/FAIL/SKIP + 证据），不吐槽。
#   3. 「环境装置」分界：沙箱里造的假 codebuddy CLI / ZCode.app 是「干净机器
#      上已装产品」的替身（模拟真品的成功与失败行为，含 rc=0 伪装成功），
#      不是唤醒链路的手写脚本。G-6「0 手写脚本」禁令约束的是唤醒链路
#      （wrapper / belt / plist / resolver），断言 A4 只查信箱根与 HOME 里
#      有没有手写唤醒脚本。
#   4. launchd 加载态在沙箱不可复现（--no-activate 防污染真机 launchd），
#      对应检查标 SKIP（环境限制），真机加载/跑通由 HS 在发版门复验。
#
# 用法：bash scripts/acceptance-user-path.sh
# 退出码：0 = 全 PASS（SKIP 不算失败）；1 = 存在 FAIL（红项清单见末尾）。

set -u

# ------------------------------------------------ 0. 定位被测仓库（脚本所在仓）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$REPO/src"
PY="${PYTHON:-python3}"
if [ ! -f "$SRC/agent_mailbox/cli.py" ]; then
  echo "FATAL: 找不到被测仓源码 $SRC/agent_mailbox/cli.py"
  exit 2
fi

# ------------------------------------------------ 1. 自造干净环境
SBX="$(mktemp -d "${TMPDIR:-/tmp}/agent-mailbox-accept.XXXXXX")"; SBX="$(cd "$SBX" && pwd -P)"  # 物理规格化（TMPDIR 尾斜杠会产生内部双斜杠→路径比对假红）
trap 'rm -rf "$SBX"' EXIT
HOME_SBX="${SBX%/}/home"  # 规格化（剥 SBX 尾斜杠；双斜杠会让路径比对假红）
MAIL_ROOT="$HOME_SBX/.agent-mail"
BIN_SBX="$HOME_SBX/bin"
LA_DIR="$HOME_SBX/Library/LaunchAgents"
mkdir -p "$HOME_SBX" "$BIN_SBX"

# 产品命令一律跑在净化环境里（env -i）：只有沙箱钉子（HOME/PATH/PYTHONPATH/
# TMPDIR），没有任何产品相关环境变量——「全程未设任何环境变量」由此结构性
# 成立，断言 A4 再显式复核。
CLEAN_ENV=(env -i HOME="$HOME_SBX" PATH="$BIN_SBX:/usr/bin:/bin"
  PYTHONPATH="$SRC" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-en_US.UTF-8}")

mb() { "${CLEAN_ENV[@]}" "$PY" -m agent_mailbox.cli "$@"; }      # 产品 CLI（用户位置）
mbwake() { "${CLEAN_ENV[@]}" "$PY" -m agent_mailbox.wake "$@"; } # 产品唤醒器
mbpy() { "${CLEAN_ENV[@]}" "$PY" -c "$1" "${@:2}"; }                              # 透传断言参数                      # store 断言面

# ------------------------------------------------ 2. 断言器
FAILS=0
PASSES=0
SKIPS=0
declare -a RED_ITEMS=()
declare -a SKIP_ITEMS=()

say() { printf '%s\n' "$*"; }
pass() { PASSES=$((PASSES + 1)); say "PASS [$1] $2"; }
fail() {
  FAILS=$((FAILS + 1))
  RED_ITEMS+=("[$1] $2")
  say "FAIL [$1] $2"
}
skip() {
  SKIPS=$((SKIPS + 1))
  SKIP_ITEMS+=("[$1] $2")
  say "SKIP [$1] $2 （环境限制，真机发版门复验）"
}

# ------------------------------------------------ 3. 环境装置（「已装产品」替身）
say "== 环境装置：沙箱 HOME=${HOME_SBX}（信箱根 ${MAIL_ROOT}，trap 清理）=="

# 3a. codebuddy CLI 替身——行为忠实模拟真品（G-6 WB 病根）：
#     配置家（CODEBUDDY_CONFIG_DIR）缺失 → 落默认 ~/.codebuddy 零 auth →
#     输出真品同款 "Authentication required. Please use /login" 且 exit 0
#     （rc=0 伪装成功也要被判定面抓住）；配置家在位 → 处理信件标 done。
cat > "$BIN_SBX/codebuddy" <<'EOF'
#!/bin/bash
# agent-mailbox 验收沙箱 · 环境装置：codebuddy CLI 替身（非唤醒链路脚本）
CFG="${CODEBUDDY_CONFIG_DIR:-$HOME/.codebuddy}"
if [ ! -d "$CFG" ]; then
  echo "Authentication required. Please use /login to authenticate."
  exit 0
fi
python3 -c '
import os, pathlib
from agent_mailbox.store import MailStore
mid, ag = os.environ.get("AGENT_MAIL_MSG_ID", ""), os.environ.get("AGENT_MAIL_AGENT_ID", "")
root = os.environ.get("AGENT_MAIL_HOME") or str(pathlib.Path.home() / ".agent-mail")
if mid and ag:
    MailStore(root).set_status(ag, mid, "done")
    print("codebuddy(替身): processed", mid)
'
EOF
chmod 0755 "$BIN_SBX/codebuddy"

# 3b. ZCode.app 替身（GUI 产品的 bundle 形态）——provider 配置（模型面）缺失 →
#     输出真品同款 "无法定位 CLI ZCode Built-in Provider Config" 且 exit 0；
#     在位（ZCODE_*_PROVIDER_CONFIG_FILE 指向存在的文件）→ 处理信件标 done。
APP_DIR="$HOME_SBX/Applications/ZCode.app/Contents/MacOS"
mkdir -p "$APP_DIR"
cat > "$APP_DIR/ZCode" <<'EOF'
#!/bin/bash
# agent-mailbox 验收沙箱 · 环境装置：ZCode.app 替身（非唤醒链路脚本）
ZB="${ZCODE_BUILTIN_PROVIDER_CONFIG_FILE:-}"
ZP="${ZCODE_PERSONAL_PROVIDER_CONFIG_FILE:-}"
if { [ -z "$ZB" ] || [ ! -f "$ZB" ]; } && { [ -z "$ZP" ] || [ ! -f "$ZP" ]; }; then
  echo "无法定位 CLI ZCode Built-in Provider Config：~/fake-search-path-1, ~/fake-search-path-2"
  exit 0
fi
python3 -c '
import os, pathlib
from agent_mailbox.store import MailStore
mid, ag = os.environ.get("AGENT_MAIL_MSG_ID", ""), os.environ.get("AGENT_MAIL_AGENT_ID", "")
root = os.environ.get("AGENT_MAIL_HOME") or str(pathlib.Path.home() / ".agent-mail")
if mid and ag:
    MailStore(root).set_status(ag, mid, "done")
    print("ZCode.app(替身): processed", mid)
'
EOF
chmod 0755 "$APP_DIR/ZCode"
printf '{\n  "CFBundleName": "ZCode",\n  "CFBundleExecutable": "ZCode",\n  "CFBundleIdentifier": "sandbox.fake.ZCode"\n}\n' > "$HOME_SBX/Applications/ZCode.app/Contents/Info.plist"

# 3c. 「别人 app 家里的凭据」替身（G-7 探测继承源）+ 「用户只配一次的模型面」
#     （G-7 用户路径第 2 步：在 /setup 配一次 provider/model/key——这里用
#     zcode 个人 provider 配置文件替身；这是**配置文件**，不是手写脚本）。
mkdir -p "$HOME_SBX/.workbuddy"
printf '{"custom_models":[{"id":"custom-local:deepseek-v4.1-flash"}]}\n' > "$HOME_SBX/.workbuddy/models.json"
mkdir -p "$HOME_SBX/.zcode/v2"
printf '{"provider":"zcode-builtin","model":"custom-local:deepseek-v4.1-flash"}\n' > "$HOME_SBX/.zcode/v2/provider_config.json"

# A4 预备：记录净化基线（产品命令可见的全部环境变量）
ENV_BASELINE="$("${CLEAN_ENV[@]}" env | cut -d= -f1 | sort | tr '\n' ' ')"

# ================================================ A1 · 装两条 agent（app/cli 双入口）
say ""
say "== A1 · setup 零输入装两条 agent（cli 入口一条 + app 入口一条）=="

SETUP_OUT="$SBX/setup.out"
mb setup --no-activate > "$SETUP_OUT" 2>&1
SETUP_RC=$?
if [ "$SETUP_RC" -eq 0 ]; then
  pass "A1.1" "agent-mailbox setup 零输入退出码 0（一行命令装完）"
else
  fail "A1.1" "setup 退出码 $SETUP_RC 非 0 | 原文尾: $(tail -3 "$SETUP_OUT" | tr '\n' ' ')"
fi

WJ="$MAIL_ROOT/wake.json"
read_entry() { # agent entry key → 原样输出值（不存在输出空 + rc 1）
  mbpy '
import json, sys
root, agent, entry, path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4].split(".")
try:
    sec = json.load(open(sys.argv[1] + "/wake.json"))["agents"][agent]["entries"][entry]
    for k in path:
        sec = sec[k]
    print(sec if not isinstance(sec, (dict, list)) else json.dumps(sec, ensure_ascii=False))
except Exception:
    sys.exit(1)
' "$MAIL_ROOT" "$1" "$2" "$3"
}

if [ -f "$WJ" ]; then
  pass "A1.2" "wake.json 已生成（${WJ}）"
else
  fail "A1.2" "wake.json 不存在——setup 未写配置 | setup 尾: $(tail -2 "$SETUP_OUT" | tr '\n' ' ')"
fi

# cli 入口（codebuddy）：入口档在 + 配置家探测在 + 模型继承在
CLI_ENTRY="$(read_entry codebuddy cli entry 2>/dev/null || true)"
if [ "$CLI_ENTRY" = "cli" ]; then
  pass "A1.3" "agents.codebuddy.entries.cli 存在（cli 入口一条）"
else
  fail "A1.3" "agents.codebuddy.entries.cli 缺失（got='$CLI_ENTRY'）——setup 未给 CLI 形态成员建入口档"
fi
CLI_HOME="$(read_entry codebuddy cli config_env.CODEBUDDY_CONFIG_DIR 2>/dev/null || true)"
if [ "$CLI_HOME" = "$HOME_SBX/.workbuddy" ]; then
  pass "A1.4" "cli 入口配置家探测正确: CODEBUDDY_CONFIG_DIR=${CLI_HOME}（探测 .workbuddy，非默认 .codebuddy）"
else
  fail "A1.4" "cli 入口配置家未探测到（got='$CLI_HOME'，期望 $HOME_SBX/.workbuddy）——用户将不必知道「配置家」是 G-6 验收点"
fi
CLI_MODEL="$(read_entry codebuddy cli model 2>/dev/null || true)"
if [ "$CLI_MODEL" = "custom-local:deepseek-v4.1-flash" ]; then
  pass "A1.5" "模型面继承探测: model=${CLI_MODEL}（从 ~/.workbuddy/models.json 读到，用户零输入）"
else
  fail "A1.5" "模型未从别人家凭据继承（got='$CLI_MODEL'）——G-7 凭据归属「能读就继承」未落地"
fi

# app 入口（zcode）：入口档在 + 二进制探测在
APP_ENTRY="$(read_entry zcode app entry 2>/dev/null || true)"
if [ "$APP_ENTRY" = "app" ]; then
  pass "A1.6" "agents.zcode.entries.app 存在（app 入口一条）"
else
  fail "A1.6" "agents.zcode.entries.app 缺失（got='$APP_ENTRY'）——setup 对 app 形态成员只跳过不建档（现状），G-6 要求可注册"
fi
APP_BIN="$(read_entry zcode app binary 2>/dev/null || true)"
if [ "$APP_BIN" = "$HOME_SBX/Applications/ZCode.app" ]; then
  pass "A1.7" "app 入口二进制探测正确: $APP_BIN"
else
  fail "A1.7" "app 入口二进制未探测到（got='$APP_BIN'）"
fi

# 生成物必带配置家 env 与 --model（WB 病根断言）
CLI_WRAPPER="$MAIL_ROOT/wake-cmd-codebuddy-cli.sh"
if [ -f "$CLI_WRAPPER" ]; then
  if grep -q "CODEBUDDY_CONFIG_DIR" "$CLI_WRAPPER" && grep -q -- "--model" "$CLI_WRAPPER"; then
    pass "A1.8" "cli 唤醒命令模板（产品生成）带配置家 env 与 --model: $(grep -c 'export CODEBUDDY_CONFIG_DIR' "$CLI_WRAPPER") 处 export + $(grep -c -- '--model' "$CLI_WRAPPER") 处 --model"
  else
    fail "A1.8" "cli 唤醒命令模板缺配置家 env 或 --model（WB 病根：裸调丢 CODEBUDDY_CONFIG_DIR+--model）| 文件: $CLI_WRAPPER"
  fi
else
  fail "A1.8" "cli 唤醒命令模板未生成（$CLI_WRAPPER 不存在）"
fi
APP_WRAPPER="$MAIL_ROOT/wake-cmd-zcode-app.sh"
if [ -f "$APP_WRAPPER" ]; then
  if grep -q "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE\|ZCODE_BUILTIN_PROVIDER_CONFIG_FILE" "$APP_WRAPPER"; then
    pass "A1.9" "app 唤醒命令模板（产品生成）带 provider 配置注入（resolve-provider-config.sh 契约已收编进通用注入）"
  else
    fail "A1.9" "app 唤醒命令模板缺 provider 配置注入块（${APP_WRAPPER}）"
  fi
else
  fail "A1.9" "app 唤醒命令模板未生成（$APP_WRAPPER 不存在）"
fi

# plist 生成（唤醒单元由产品生成：每条入口一个单元）
PLIST_CLI="$LA_DIR/com.polaris-smart.agent-mailbox-wake-codebuddy-cli.plist"
PLIST_APP="$LA_DIR/com.polaris-smart.agent-mailbox-wake-zcode-app.plist"
if [ -f "$PLIST_CLI" ] && plutil -lint "$PLIST_CLI" >/dev/null 2>&1; then
  pass "A1.10" "cli 入口 launchd 单元已生成且合法: $(basename "$PLIST_CLI")"
else
  fail "A1.10" "cli 入口 plist 未生成或不合法（${PLIST_CLI}）——唤醒单元须由产品生成"
fi
if [ -f "$PLIST_APP" ] && plutil -lint "$PLIST_APP" >/dev/null 2>&1; then
  pass "A1.11" "app 入口 launchd 单元已生成且合法: $(basename "$PLIST_APP")"
else
  fail "A1.11" "app 入口 plist 未生成或不合法（${PLIST_APP}）"
fi

# ================================================ A2 · 真跑：发信 → 自动处理 → done 增量
say ""
say "== A2 · 每条入口发测试信 → 触发唤醒 → 自动处理 → done 数 +1（不只看 pending）=="

done_count() { # agent → inbox done + archive 全量
  mbpy '
import json, sys
from pathlib import Path
root, agent = Path(sys.argv[1]), sys.argv[2]
n = 0
for base in (root / "inbox" / agent, root / "archive" / agent):
    if base.is_dir():
        for p in base.glob("*.json"):
            try:
                m = json.loads(p.read_text())
            except Exception:
                continue
            if m.get("status") == "done":
                n += 1
print(n)
' "$MAIL_ROOT" "$1"
}

send_letter() { # agent subject
  mbpy '
import sys
from agent_mailbox.store import MailStore
MailStore(sys.argv[1]).send("boss", sys.argv[2], sys.argv[3], "验收测试信：请自动处理并标 done。")
' "$MAIL_ROOT" "$1" "$2"
}

run_entry_trigger() { # agent entry → rc（用产品的 run 面触发，等价 launchd WatchPaths）
  mbwake run --agent "$1" --entry "$2" --once >/dev/null 2>&1
  echo $?
}

for pair in "codebuddy:cli" "zcode:app"; do
  AGENT="${pair%%:*}"
  ENTRY="${pair##*:}"
  BEFORE="$(done_count "$AGENT")"
  send_letter "$AGENT" "A2 测试信 → $AGENT/$ENTRY" >/dev/null 2>&1
  RC="$(run_entry_trigger "$AGENT" "$ENTRY")"
  AFTER="$(done_count "$AGENT")"
  if [ "$RC" = "0" ] && [ "$AFTER" -eq $((BEFORE + 1)) ]; then
    pass "A2.$AGENT/$ENTRY" "真跑 done ${BEFORE}→${AFTER}（+1，信被自动处理并标 done；触发 rc=${RC}）"
  else
    fail "A2.$AGENT/$ENTRY" "done ${BEFORE}→${AFTER}（期望 +1）触发 rc=${RC}（0=成功；非 0=失败必响路径）——该入口没有走通无人值守处理"
  fi
done

# ================================================ A3 · 负例：改错配置家 → doctor 报该条+人话 → 改回复绿
say ""
say "== A3 · 负例：把 cli 入口配置家改错 ⇒ doctor 报出该条 + 人话；改回 ⇒ 复绿 =="

doctor_json() { mb doctor --json 2>/dev/null; }
entry_check_text() { # 从 doctor --json 抠 entry 相关检查原文
  doctor_json | mbpy '
import json, sys
rep = json.load(sys.stdin)
for c in rep.get("checks", []):
    if c.get("id") in ("entry_levels", "entries"):
        print(json.dumps(c, ensure_ascii=False))
' 2>/dev/null
}

# 改错：cli 入口配置家指向不存在的目录（改配置 = 负例动作本身，非手改唤醒脚本）
mbpy '
import json, sys
p = sys.argv[1] + "/wake.json"
d = json.load(open(p))
d["agents"]["codebuddy"]["entries"]["cli"]["config_env"]["CODEBUDDY_CONFIG_DIR"] = sys.argv[2]
json.dump(d, open(p, "w"), ensure_ascii=False, indent=1)
' "$MAIL_ROOT" "$HOME_SBX/.broken-buddy" >/dev/null 2>&1

NEG_ENTRY="$(entry_check_text)"
if echo "$NEG_ENTRY" | grep -q "codebuddy" && echo "$NEG_ENTRY" | grep -q '"ok": *false\|"ok":false'; then
  pass "A3.1" "doctor 逐条点名断开项: $(echo "$NEG_ENTRY" | head -c 200)"
else
  fail "A3.1" "doctor 未对改错的 codebuddy 入口报 FAIL（entry 级检查缺位或没点名）| got: $(echo "$NEG_ENTRY" | head -c 200)"
fi
if echo "$NEG_ENTRY" | grep -q "配置家\|配置页面"; then
  pass "A3.2" "doctor 报的是人话（含「配置家/配置页面」）而非内部通道错误原文"
else
  fail "A3.2" "doctor 未用人话报因（应含「配置家/配置页面」可操作指引）| got: $(echo "$NEG_ENTRY" | head -c 200)"
fi

# 负例真跑：配置家坏了 → 先发一封新信（A2 的信已 done，空箱触发本就 rc=0）→
# 替身 CLI 模拟真品输出 auth 墙（exit 0 伪装）→ 失败必响应判失败
send_letter codebuddy "A3 负例测试信（配置家已改错）" >/dev/null 2>&1
NEG_RC="$(run_entry_trigger codebuddy cli)"
if [ "$NEG_RC" != "0" ]; then
  pass "A3.3" "配置家改错后真跑非零退出（失败必响，禁 rc=0 伪装成功；rc=${NEG_RC}）"
else
  fail "A3.3" "配置家改错后真跑 rc=0（失败静默复发——auth 墙被当成功）"
fi
NEG_ERR="$(grep -c 'auth_required' "$MAIL_ROOT/wake-attempts.jsonl" 2>/dev/null)"  # 格式无关（jsonl 冒号后空格两种形态都算）
NEG_ERR="${NEG_ERR:-0}"
if [ "${NEG_ERR:-0}" -ge 1 ]; then
  pass "A3.4" "锚A 留痕 auth_required ×${NEG_ERR}（替身 CLI 的 rc=0 auth 墙被输出特征判失败）"
else
  fail "A3.4" "wake-attempts.jsonl 无 auth_required 行——rc=0 伪装成功漏判（t-59 特征判定缺位）"
fi

# 改回：只选名字 + 入口（setup 重跑 = 产品自己重探测 + 重生成单元）
mb setup --agent codebuddy --entry cli --no-activate > "$SBX/restore.out" 2>&1
FIX_RC=$?
FIX_HOME="$(read_entry codebuddy cli config_env.CODEBUDDY_CONFIG_DIR 2>/dev/null || true)"
if [ "$FIX_RC" = "0" ] && [ "$FIX_HOME" = "$HOME_SBX/.workbuddy" ]; then
  pass "A3.5" "改回复绿第一步：setup --agent codebuddy --entry cli（只选名字+入口）重新探测配置家=$FIX_HOME"
else
  fail "A3.5" "setup 按名字+入口重装未复绿（rc=$FIX_RC got_home='$FIX_HOME'）"
fi
BEFORE2="$(done_count codebuddy)"
send_letter codebuddy "A3 复绿测试信" >/dev/null 2>&1
RC2="$(run_entry_trigger codebuddy cli)"
AFTER2="$(done_count codebuddy)"
ENTRY_AFTER="$(entry_check_text)"
if [ "$RC2" = "0" ] && [ "$AFTER2" -eq $((BEFORE2 + 1)) ]; then
  if echo "$ENTRY_AFTER" | grep -q '"ok": *true\|"ok":true'; then
    pass "A3.6" "复绿闭环：重跑 done ${BEFORE2}→${AFTER2}（+1）且 doctor entry 级检查转绿"
  else
    fail "A3.6" "跑通了但 doctor entry 级检查仍红 | got: $(echo "$ENTRY_AFTER" | head -c 200)"
  fi
else
  fail "A3.6" "复绿后真跑未走通（done ${BEFORE2}→$AFTER2, rc=${RC2}）"
fi

# ================================================ A4 · 零折腾（0 env · 0 手写脚本 · 0 手改文件）
say ""
say "== A4 · 全程未设任何环境变量、未碰任何手写脚本 =="

PRODUCT_ENV_HITS="$(printf '%s' "$ENV_BASELINE" | tr ' ' '\n' | grep -cE '^(CODEBUDDY_|ZCODE_|AGENT_MAIL_)' || true)"
if [ "${PRODUCT_ENV_HITS:-0}" = "0" ]; then
  pass "A4.1" "产品命令可见环境 = 净化钉子（${ENV_BASELINE}），0 个产品相关环境变量（CODEBUDDY_*/ZCODE_*/AGENT_MAIL_*）"
else
  fail "A4.1" "净化环境里出现产品环境变量: $ENV_BASELINE"
fi
if grep -q 'CODEBUDDY_CONFIG_DIR' "$CLI_WRAPPER" 2>/dev/null && grep -qE "export CODEBUDDY_CONFIG_DIR='$HOME_SBX/.workbuddy'" "$CLI_WRAPPER" 2>/dev/null; then
  pass "A4.2" "配置家以绝对路径显式写进生成物（不指望环境继承——G-7 铁律 ②）：$(grep "export CODEBUDDY_CONFIG_DIR" "$CLI_WRAPPER" | head -1)"
else
  fail "A4.2" "生成物未把配置家绝对路径显式写死（依赖环境继承 = WB/ZC 同根因会复发）"
fi

HAND_WRITTEN="$(find "$MAIL_ROOT" -maxdepth 1 -name '*.sh' ! -name 'wake-cmd-*' ! -name 'wake-belt-*' 2>/dev/null)"
HOME_STRAY="$(find "$HOME_SBX" -maxdepth 2 \( -name 'wake.sh' -o -name 'wake-zc.sh' -o -name 'wake-*.sh' \) -not -path "$MAIL_ROOT/*" 2>/dev/null)"
if [ -z "$HAND_WRITTEN" ] && [ -z "$HOME_STRAY" ]; then
  GEN_N="$(find "$MAIL_ROOT" -maxdepth 1 -name 'wake-*.sh' 2>/dev/null | wc -l | tr -d ' ')"
  pass "A4.3" "信箱根 .sh 全部为产品生成物（wake-cmd-*/wake-belt-* ×${GEN_N}），HOME 无手写唤醒脚本"
else
  fail "A4.3" "发现手写唤醒脚本: ${HAND_WRITTEN:-} ${HOME_STRAY:-}"
fi
BIN_EXTRA="$(ls "$BIN_SBX" 2>/dev/null | grep -v '^codebuddy$' || true)"
if [ -z "$BIN_EXTRA" ]; then
  pass "A4.4" "沙箱 bin 只有环境装置（codebuddy 替身）——验收全程未夹带任何工具脚本"
else
  fail "A4.4" "沙箱 bin 出现计划外文件: $BIN_EXTRA"
fi

# ================================================ 汇总
say ""
say "== 汇总: PASS=$PASSES FAIL=$FAILS SKIP=$SKIPS =="
if [ "$FAILS" -gt 0 ]; then
  say "红项清单（第4步实机清单——逐项对应发版门）："
  i=1
  for item in "${RED_ITEMS[@]}"; do
    say "  $i. $item"
    i=$((i + 1))
  done
fi
if [ "$SKIPS" -gt 0 ]; then
  say "环境限制项（沙箱不可复现，HS 真机发版门复验）："
  for item in "${SKIP_ITEMS[@]}"; do
    say "  · $item"
  done
fi
if [ "$FAILS" -eq 0 ]; then
  say "结论：用户路径验收全部判定项通过（判定人=HS 拿本输出原文亲验）。"
  exit 0
fi
say "结论：存在红项（本脚本设计目的=发现真问题；红了逐项修，绿了交 HS 复跑）。"
exit 1
