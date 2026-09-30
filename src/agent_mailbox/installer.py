"""t-61（判据1+6）: 一条命令装完 — ``agent-mailbox setup`` 全包揽 installer.

产品级口径（老板 2026-09-29 原话）：「用户安装了 agent-mailbox，配置好，
所有 agent 就都能用起来，没有障碍。」反面现状 = 每家手写脚本/手填路径/
自处理登录态 ⇒ 以不同方式各自坏（codex 不在 PATH / WB CLI 未登录 / ZC
provider 死路径，三例实锤）。本模块把剩余产品面收进库内：

- **自动发现**（判据1）: setup 零输入时跑 discover 引擎（只读 L1–L3），
  CLI 形态成员直接 local-command 全自动装好（注册 + ``agents.<ID>`` 段 +
  launchd plist + 唤醒命令）；app/未识别形态给指名道姓的下一步（discover
  铁律：kind 不猜）。
- **平台细节全收进库**（判据6）: local-command 的 command 首段一律解析为
  绝对路径（launchd 环境极简、PATH 不可信）；zcode 类命令额外生成**唤醒
  命令模板**（provider env 注入内置，t-60 同款契约——caller 现有值优先 →
  仓内 resolver（存在才调）→ runtime 最新版 → app 内置副本），用户不必
  知道 provider 配置存在。
- **belt 脚本入库生成**: ``--belt`` 从包内模板渲染 per-身份 belt 脚本
  （绝对路径自动填），部署副本 = 生成产物（呼应「部署副本=install 产物」
  收口关系，HS 硬要求①的终态）。
- **向后兼容 fail-open**: 老三样（手写 wake.json / 手装 plist / 已部署
  belt）继续可用——本模块只写本身份的 ``agents.<ID>`` 段与本身份的 plist，
  存量段/未知键分毫不动；损坏 config 备份后重建，绝不静默吞、也绝不 crash。
"""

from __future__ import annotations

import dataclasses
import json
import os
import plistlib
import re
import shutil
import time
from pathlib import Path
from typing import Any

from . import discover as discover_mod
from . import wake as wake_mod
from .store import MailStore

# 唤醒命令模板（wrapper）与 belt 脚本的落点（都在信箱根内——部署副本=install 产物，
# 一个目录看全所有生成物）。
WRAPPER_PREFIX = "wake-cmd-"
BELT_PREFIX = "wake-belt-"
PROVIDER_RESOLVER_NAME = "resolve-provider-config.sh"


# ------------------------------------------------------------ 命令解析（判据6）


def resolve_command_head(cmd: list[str]) -> tuple[list[str], str]:
    """把命令首段解析为绝对路径（launchd 的 PATH 不可信，判据6）。

    已是绝对路径的原样保留（不存在也让它进配置——doctor ④ 会判「命令不
    存在」，setup 不替用户拦，fail-open）；裸命令名用 PATH 解析，解析不到
    原样保留并给 note。返回 ``(argv, note)``。"""
    argv = [str(a) for a in cmd]
    if not argv:
        return argv, "空命令"
    head = argv[0]
    if os.path.isabs(head):
        return argv, ""
    found = shutil.which(head)
    if found:
        return [found, *argv[1:]], f"{head} → {found}（PATH 解析为绝对路径）"
    return argv, f"{head} 不在 PATH（保留原样；doctor ④ 会判「不是绝对路径」）"


def is_zcode_command(cmd: list[str] | tuple[str, ...]) -> bool:
    """zcode 类命令（唤醒命令模板要内置 provider env 注入的那一类）。"""
    return bool(cmd) and "zcode" in Path(str(cmd[0])).name.lower()


def provider_resolver_path() -> str:
    """仓内 provider 解析器（scripts/resolve-provider-config.sh）的绝对路径。

    查找序：``AGENT_MAIL_PROVIDER_RESOLVER`` env → 包相对的仓 scripts/ 目录
    （editable 安装即仓内）。找不到回 ``""``——唤醒命令模板自带同契约的内
    联兜底，wheel 安装（无 scripts/）也能注入。"""
    env = os.environ.get("AGENT_MAIL_PROVIDER_RESOLVER", "")
    if env and Path(env).is_file():
        return str(Path(env).resolve())
    cand = Path(__file__).resolve().parents[2] / "scripts" / PROVIDER_RESOLVER_NAME
    if cand.is_file():
        return str(cand)
    return ""


def _sh_quote(s: str) -> str:
    """POSIX shell 单引号转义（eval/set -- 双安全的唯一写法）。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def provider_injection_bash(resolver: str) -> str:
    """provider env 注入块（t-60 契约的 shell 形态，进唤醒命令模板）。

    与 scripts/wake-zc.sh 的 t-60 块、scripts/resolve-provider-config.sh 的
    契约逐条对齐：caller 现有值优先 → 仓内 resolver（存在才调，静默不泄
    路径）→ ~/.zcode runtime 最新版（sort -V 确定性版本序 + mtime tiebreak）
    → app 内置副本；个人 provider 配置在位就一并 export。resolver 缺位时
    内联兜底，wheel 安装不缺功能。"""
    return f"""# --- provider env 注入（t-60 契约; 用户零手写, 用户不必知道 provider 配置存在）---
_ZB="${{ZCODE_BUILTIN_PROVIDER_CONFIG_FILE:-}}"
if [ -z "$_ZB" ] || [ ! -f "$_ZB" ]; then
  _RES={_sh_quote(resolver)}
  if [ -n "$_RES" ] && [ -f "$_RES" ]; then
    _ZB="$(bash "$_RES" 2>/dev/null || true)"
  else
    _ZV=$(for _d in "$HOME"/.zcode/v2/runtime/provider/darwin-*/[0-9]*/; do [ -d "$_d" ] && basename "$_d"; done 2>/dev/null | sort -V | tail -1)
    _ZB=""
    if [ -n "$_ZV" ]; then
      _ZB=$(ls -t "$HOME"/.zcode/v2/runtime/provider/darwin-*/"$_ZV"/endpoint-*/zcode-builtin.json 2>/dev/null | head -1)
    fi
    if [ -z "$_ZB" ]; then
      _APP="${{ZCODE_APP_BUNDLE_ROOT:-/Applications/ZCode.app}}"
      [ -f "$_APP/Contents/Resources/config/provider/zcode-builtin.json" ] && _ZB="$_APP/Contents/Resources/config/provider/zcode-builtin.json"
    fi
  fi
  [ -n "$_ZB" ] && export ZCODE_BUILTIN_PROVIDER_CONFIG_FILE="$_ZB"
fi
[ -f "$HOME/.zcode/v2/provider_config.json" ] && export ZCODE_PERSONAL_PROVIDER_CONFIG_FILE="$HOME/.zcode/v2/provider_config.json"
# --- provider env 注入结束 ---
"""


def wrapper_path(root: Path | str, agent_id: str) -> Path:
    return Path(root) / f"{WRAPPER_PREFIX}{agent_id}.sh"


def wake_wrapper_body(agent_id: str, root: Path | str, argv: list[str], resolver: str) -> str:
    """唤醒命令模板（local-command + zcode 类命令时生成）。

    用户零手写：AGENT_MAIL_HOME 钉死本根、provider env 注入内置、命令以
    绝对路径 ``exec``。部署副本 = 本文件。"""
    quoted = " ".join(_sh_quote(a) for a in argv)
    return f"""#!/bin/bash
# agent-mailbox 唤醒命令模板 — `{agent_id}`（agent-mailbox setup 生成, t-61 判据1/6）
# 部署副本 = 本文件（install 产物）。信必达链路: WatchPaths → wake run → 本模板。
set -u
export AGENT_MAIL_HOME={_sh_quote(str(Path(root)))}
{provider_injection_bash(resolver)}
set -- {quoted}
exec "$@"
"""


def belt_path(root: Path | str, agent_id: str) -> Path:
    return Path(root) / f"{BELT_PREFIX}{agent_id}.sh"


def belt_script_body(
    agent_id: str,
    root: Path | str,
    python_exe: str,
    drain_cmd: str,
    watchdog_secs: float = 900.0,
) -> str:
    """per-身份 belt 脚本模板（包内渲染，绝对路径自动填）。

    单轮语义（与 wake run --once 同款）：reap → claim（t-56 认领纪律）→
    有信才 drain → 校验 done → release（信不丢）→ archive。失败必响（t-59）：
    claim 基建失败 / drain 无进展都 exit 1 + 锚A fail 行，禁 rc=0 伪装成功。
    G-4（收口批第 2 批）：drain 不再裸 ``eval``——经**纯 Python 进程组看门狗**
    （``agent_mailbox.watchdog``）执行：超时对整个进程组 SIGTERM → 宽限 →
    SIGKILL（``start_new_session`` 起组 + ``killpg``，不用 shell 拼 sleep/
    kill/pkill——那条路正是 G-4 病灶：父进程一死孙进程被 launchd 收养成
    PPID=1 孤儿）。G-5：belt 失败告警经 ``agent_mailbox.alerts`` 真投递 +
    回读确认（store.send → get_letter 回读），不再只写日志没人读。
    部署副本 = 本文件；真实 drain 语义用 ``WAKE_DRAIN_CMD`` 覆盖；看门狗
    秒数用 ``WAKE_WATCHDOG_SECS`` 覆盖。
    """
    return f"""#!/bin/bash
# agent-mailbox per-身份 wake belt — `{agent_id}`（agent-mailbox setup --belt 生成, t-61）
# 部署副本 = 本文件（生成产物）；模板见 src/agent_mailbox/installer.py::belt_script_body。
# 单轮: reap → claim → 有信才 drain → 校验 done → release → archive。
# 失败必响: claim 基建失败 / drain 无进展 exit 1 + 锚A fail 行（禁 rc=0 伪装, t-59）。
# G-4: drain 经进程组看门狗（超时 SIGTERM 整组→宽限→SIGKILL, 治孤儿）。
# G-5: belt 失败告警真投递 + 回读确认（agent_mailbox.alerts, 不再只写日志）。
# drain 语义覆盖: WAKE_DRAIN_CMD env（默认=本身份的唤醒命令）。
# 看门狗秒数覆盖: WAKE_WATCHDOG_SECS env（默认={watchdog_secs:g}s）。
set -u
MAIL_ROOT="${{MAIL_ROOT:-{_sh_quote(str(Path(root)))}}}"
export AGENT_MAIL_HOME="$MAIL_ROOT"
AGENT={_sh_quote(agent_id)}
MB_PY={_sh_quote(python_exe)}
LOG="$MAIL_ROOT/{BELT_PREFIX}{agent_id}.log"
ANCHOR="$MAIL_ROOT/{wake_mod.WAKE_ATTEMPTS_FILE}"
DRAIN_DEFAULT={_sh_quote(drain_cmd)}
DRAIN_CMD="${{WAKE_DRAIN_CMD:-$DRAIN_DEFAULT}}"
WD_SECS="${{WAKE_WATCHDOG_SECS:-{int(watchdog_secs)}}}"
LOCK="${{WAKE_LOCK:-$MAIL_ROOT/{BELT_PREFIX}{agent_id}.lock}}"
mkdir "$LOCK" 2>/dev/null || exit 0   # 已有 belt 在跑（幂等锁）
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# 锚A: 每次尝试落一行（0600, 与 daemon/belt 同形状 8 字段）
anchor_attempt() {{ # route attempt outcome error_class executor latency_ms
  local line
  line=$(printf '{{"ts":"%s","agent":"%s","route":"%s","attempt":%s,"outcome":"%s","error_class":"%s","executor":"%s","latency_ms":%s}}' \\
    "$(date -u +%FT%TZ)" "$AGENT" "$1" "$2" "$3" "$4" "$5" "$6")
  {{ umask 077; printf '%s\\n' "$line" >> "$ANCHOR"; }} 2>>"$LOG" || true
}}

# G-5: belt 失败告警（store.send + 回读确认；确定性内容走去重窗防刷屏）
belt_alert() {{
  "$MB_PY" -m agent_mailbox.alerts belt-fail --root "$MAIL_ROOT" --agent "$AGENT" \\
    --reason "$1" --claimed "${{2:-0}}" --done "${{3:-0}}" >> "$LOG" 2>&1 || true
}}

if ! "$MB_PY" -m agent_mailbox.reap --agent "$AGENT" >> "$LOG" 2>&1; then
  echo "$(date '+%F %T') reap FAILED (fail-open, continuing) agent=$AGENT" >> "$LOG"
fi

CLAIM_FILE="$MAIL_ROOT/{BELT_PREFIX}{agent_id}-claim.$$.json"
if ! "$MB_PY" -m agent_mailbox.wake claim --agent "$AGENT" --root "$MAIL_ROOT" \\
    --label "belt:$$" --out "$CLAIM_FILE" >> "$LOG" 2>&1; then
  anchor_attempt belt 1 fail claim_failed belt-claim 0
  belt_alert claim_failed
  echo "$(date '+%F %T') claim infrastructure FAILED — exit 1 (失败必响, 禁 rc=0 伪装)" >> "$LOG"
  exit 1
fi
CLAIMED=$("$MB_PY" -c 'import json,sys
try:
    print(len(json.load(open(sys.argv[1])).get("claimed", [])))
except Exception:
    print(0)' "$CLAIM_FILE" 2>>"$LOG")
if [ -z "$CLAIMED" ] || [ "$CLAIMED" = "0" ]; then
  rm -f "$CLAIM_FILE"
  exit 0   # 无信可领: 静默收工
fi
echo "$(date '+%F %T') belt: claimed $CLAIMED mail files (by belt:$$)" >> "$LOG"
export AGENT_MAIL_CLAIM_FILE="$CLAIM_FILE"

T0=$(date +%s)
# G-4: drain 经纯 Python 进程组看门狗（--shell 把命令串当数据传入, 超时
# killpg SIGTERM→宽限→SIGKILL 整组回收；rc=124 = 看门狗超时）
"$MB_PY" -m agent_mailbox.watchdog --timeout "$WD_SECS" --shell "$DRAIN_CMD" >> "$LOG" 2>&1
DRAIN_RC=$?
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
print(done)' "$MAIL_ROOT" "$AGENT" "$CLAIM_FILE" 2>>"$LOG")
TURN_MS=$(( ($(date +%s) - T0) * 1000 ))
# 信不丢: release 本轮未 done 的认领（done 自动跳过, 只放本 belt:$$ 的认领）
"$MB_PY" -m agent_mailbox.wake release --agent "$AGENT" --root "$MAIL_ROOT" \\
  --label "belt:$$" --claim-file "$CLAIM_FILE" >> "$LOG" 2>&1 || true
rm -f "$CLAIM_FILE"
if [ "$DRAIN_RC" -eq 124 ]; then
  # G-4: 看门狗超时——进程组已回收（SIGTERM→宽限→SIGKILL），失败必响
  anchor_attempt belt 1 fail timeout belt-drain "$TURN_MS"
  belt_alert timeout "$CLAIMED" "${{DONE_N:-0}}"
  echo "$(date '+%F %T') belt drain WATCHDOG TIMEOUT (rc=124, watchdog ${{WD_SECS}}s, process group reclaimed) — exit 1" >> "$LOG"
  exit 1
fi
if [ -z "${{DONE_N:-}}" ] || [ "${{DONE_N:-0}}" -lt 1 ]; then
  anchor_attempt belt 1 fail no_progress belt-drain "$TURN_MS"
  belt_alert no_progress "$CLAIMED" "${{DONE_N:-0}}"
  echo "$(date '+%F %T') belt drain made NO progress (claimed $CLAIMED, done ${{DONE_N:-0}}) — exit 1" >> "$LOG"
  exit 1
fi
anchor_attempt belt 1 ok "" belt-drain "$TURN_MS"
"$MB_PY" -c "from agent_mailbox.store import MailStore; print('archived:', MailStore().archive_done('$AGENT'))" >> "$LOG" 2>&1 || true
echo "$(date '+%F %T') belt wake done" >> "$LOG"
"""


def _entry_plist_body(agent_id: str, entry_id: str, wrapper: str, root: Path) -> str:
    """G-6 per-entry launchd 单元：程序 = 该入口的生成物包装脚本。

    环境钉子（HOME/AGENT_MAIL_HOME/AGENT_MAIL_ID）显式写入——非交互 launchd
    环境不继承 app env 是 WB/ZC 两次事故的同款根因（G-7 铁律）。"""
    import html as _html

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key>\n  <string>{wake_mod.WAKE_LABEL}-{agent_id}-{entry_id}</string>\n"
        "  <key>ProgramArguments</key>\n  <array>\n"
        "    <string>/bin/bash</string>\n"
        f"    <string>{_html.escape(wrapper)}</string>\n"
        "  </array>\n"
        "  <key>EnvironmentVariables</key>\n  <dict>\n"
        "    <key>HOME</key>\n    <string>" + _html.escape(str(Path.home())) + "</string>\n"
        "    <key>AGENT_MAIL_HOME</key>\n    <string>"
        + _html.escape(str(Path(root)))
        + "</string>\n"
        "    <key>AGENT_MAIL_ID</key>\n    <string>" + _html.escape(agent_id) + "</string>\n"
        "  </dict>\n"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "</dict>\n</plist>\n"
    )


def _write_executable(path: Path, body: str, mode: int = 0o755) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


# ------------------------------------------------------------ 入口档（G-6/G-7）
#
# G-6 要求 1/2：注册表每 agent 增「入口档 entry profile」，唤醒单元（plist/
# 命令/看门狗/包装脚本）全由产品渲染，生成命令必带配置家 env 与 --model。
# WB 病根（收口批实测）：裸调 codebuddy 丢 CODEBUDDY_CONFIG_DIR + --model ⇒
# CLI 落 ~/.codebuddy 零 auth ⇒ 175 条 degraded。G-7 铁律：非交互 shell 一定
# 丢 app 导出的 env ⇒ 配置家与模型必须**显式写进单元**，绝不指望环境继承；
# 用户 agent 侧零配置——入口/二进制/配置家/模型由产品**探测**（能读就继承，
# 读不到在配置页面补一次）。
#
# 落点：``agents.<ID>.entries.<cli|app>`` 段（t-58 的 agents 段扩展，未知
# 子键照旧原样保留 → 旧配置 fail-open）。``scripts/resolve-provider-config.sh``
# 的 sort -V + 回退契约收编为 :func:`resolve_provider_config_path`（registry
# 驱动的通用机制），手写 resolver 退役保留兼容（部署同步面照旧比对）。

# zcode 家族（provider 配置链，t-60 契约的归属面）
ZCODE_FAMILY = frozenset({"zcode"})

# 已知家族的配置家探测表（G-7 探测三件事之一：入口 + 配置家）。候选按序
# 探测，第一个存在的目录胜出——codebuddy 的真源在 WB 宿主是 ~/.workbuddy
# （G-6 实测），裸默认 ~/.codebuddy 只作兜底。
CONFIG_HOME_DETECTION: dict[str, dict[str, Any]] = {
    "codebuddy": {"env": "CODEBUDDY_CONFIG_DIR", "config_dirs": (".workbuddy", ".codebuddy")},
    "claude": {"env": "CLAUDE_CONFIG_DIR", "config_dirs": (".claude",)},
    "codex": {"env": "CODEX_HOME", "config_dirs": (".codex",)},
}

# 已知家族的权限模式 → 固定参数（codebuddy: auto = 信任执行，G-4 现场原样）
PERMISSION_MODE_ARGS: dict[str, dict[str, tuple[str, ...]]] = {
    "codebuddy": {"auto": ("-c", "-y")},
}

# 默认提示词模板（wake-zc.sh t-56 巡检提示词的产品化泛形）：belt 认领模式
# 读 CLAIM_FILE；daemon 单信模式读 AGENT_MAIL_MSG_*。模板只引用路径与元数据，
# 信正文经 env/文件传递（LocalCommandAdapter 注入姿态不变）。``:-`` 安全展开
# 与 ``set -u`` 兼容。
DEFAULT_PROMPT_TEMPLATE = (
    "信箱巡检（claim 认领投递）：本轮已认领给你的信件清单在 ${AGENT_MAIL_CLAIM_FILE:-}"
    "（JSON：claimed[]，每封含 id/from/subject/body），逐封按清单内容执行；"
    "该变量为空时（daemon 单信模式）直接处理信件 ${AGENT_MAIL_MSG_ID:-}"
    "（主题：${AGENT_MAIL_SUBJECT:-}，正文在 env AGENT_MAIL_MSG_BODY）。"
    "不要自行读取 inbox 目录（信已认领给你，重复直读会造成多窗重复处理）；"
    "完成后给每封的发件人写回执（agent_mailbox.store 的 MailStore.send），"
    "并把信的状态更新为 done（store.set_status）。最后一行输出：处理 N 封。"
)

_ENTRY_ENV_READER_BODY = """#!/bin/bash
# agent-mailbox 入口档 env 读取器（G-6 运行时刷新）——安装生成物，非手写脚本。
# 用法: wake-cmd-entry-env.sh <mail_root> <agent> <entry>
# 输出: export 行（该入口的 config_env）；任何异常静默 exit 0（回退烘焙值）。
set -u
ROOT="${1:?}"; AGENT="${2:?}"; ENTRY="${3:?}"
[ -f "$ROOT/wake.json" ] || exit 0
python3 - "$ROOT" "$AGENT" "$ENTRY" <<'AGENT_MAIL_ENV_PY'
import json, shlex, sys
try:
    d = json.load(open(sys.argv[1] + "/wake.json"))["agents"][sys.argv[2]]
    env = d["entries"][sys.argv[3]].get("config_env", {})
    for k, v in sorted(env.items()):
        print("export %s=%s" % (k, shlex.quote(str(v))))
except Exception:
    pass
AGENT_MAIL_ENV_PY
"""


ENTRY_KINDS = ("app", "cli")
ENTRY_WAKE_MODES = ("unattended", "manual_confirm")
DEFAULT_WATCHDOG_SECS = 900.0


@dataclasses.dataclass
class EntryProfile:
    """一个 agent 的一条入口（G-6 入口档）。

    ``entry`` = app | cli；``config_env`` = 配置家 env 映射（生成单元时逐条
    ``export``，显式入单元）；``criteria`` = 跑通判据（输出非空 / done 增量），
    doctor 三级报的「跑通」级读它；``wake_mode`` = unattended | manual_confirm
    （GUI 常驻型等做不到无人值守的入口必须显式标注「可注册但需人工确认」，
    发版门⑦：不许默认跳过）。未知键进 ``extra`` 原样往返。"""

    entry: str
    binary: str = ""
    config_env: dict[str, str] = dataclasses.field(default_factory=dict)
    model: str = ""
    prompt_template: str = ""
    permission_mode: str = ""
    watchdog_secs: float = DEFAULT_WATCHDOG_SECS
    criteria: dict[str, Any] = dataclasses.field(
        default_factory=lambda: {"output_nonempty": True, "done_delta": True}
    )
    wake_mode: str = "unattended"
    note: str = ""
    args: list[str] = dataclasses.field(default_factory=list)
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "entry": self.entry,
            "binary": self.binary,
            "config_env": dict(self.config_env),
            "model": self.model,
            "prompt_template": self.prompt_template,
            "permission_mode": self.permission_mode,
            "watchdog_secs": self.watchdog_secs,
            "criteria": dict(self.criteria),
            "wake_mode": self.wake_mode,
            "note": self.note,
            "args": list(self.args),
        }
        out.update(self.extra)  # 已知键优先，extra 不覆盖
        return out

    @classmethod
    def from_dict(cls, raw: Any, entry_hint: str = "") -> EntryProfile:
        """解析并校验一个入口档 dict（形状不对抛 ValueError——调用方翻译成
        CLI SystemExit / 配置页 400，不静默吞）。未知键保进 extra（往返不丢）。"""
        if not isinstance(raw, dict):
            raise TypeError("entry profile 必须是对象（dict）")
        entry = str(raw.get("entry") or entry_hint or "").strip().lower()
        if entry not in ENTRY_KINDS:
            raise ValueError(f"entry 必须是 {' | '.join(ENTRY_KINDS)}（got {entry!r}）")
        config_env_raw = raw.get("config_env") or {}
        if not isinstance(config_env_raw, dict):
            raise TypeError("config_env 必须是对象（env 名 → 路径/值 字符串）")
        config_env = {str(k): str(v) for k, v in config_env_raw.items()}
        try:
            watchdog = float(raw.get("watchdog_secs") or DEFAULT_WATCHDOG_SECS)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"watchdog_secs 不是数字: {raw.get('watchdog_secs')!r}") from exc
        criteria_raw = raw.get("criteria")
        criteria = (
            {str(k): v for k, v in criteria_raw.items()}
            if isinstance(criteria_raw, dict)
            else dict(cls(entry=entry_hint or "cli").criteria)
        )
        template = str(raw.get("prompt_template") or "")
        if "`" in template or "$(" in template:
            # 受控展开（生成单元里 eval 展开模板引用的 AGENT_MAIL_*）的前提：
            # 模板来自配置面（token 门内 owner 输入），禁命令替换注入面。
            raise ValueError("prompt_template 不允许包含反引号或 $（ 命令替换")
        known = {
            "entry",
            "binary",
            "config_env",
            "model",
            "prompt_template",
            "permission_mode",
            "watchdog_secs",
            "criteria",
            "wake_mode",
            "note",
            "args",
        }
        extra = {k: v for k, v in raw.items() if k not in known}
        wake_mode = str(raw.get("wake_mode") or "unattended").strip().lower()
        if wake_mode not in ENTRY_WAKE_MODES:
            raise ValueError(
                f"wake_mode 必须是 {' | '.join(ENTRY_WAKE_MODES)}（got {wake_mode!r}）"
            )
        args_raw = raw.get("args") or []
        if not isinstance(args_raw, (list, tuple)) or not all(isinstance(a, str) for a in args_raw):
            raise ValueError("args 必须是字符串数组")
        return cls(
            entry=entry,
            binary=str(raw.get("binary") or ""),
            config_env=config_env,
            model=str(raw.get("model") or ""),
            prompt_template=template,
            permission_mode=str(raw.get("permission_mode") or ""),
            watchdog_secs=watchdog,
            criteria=criteria,
            wake_mode=wake_mode,
            note=str(raw.get("note") or ""),
            args=[str(a) for a in args_raw],
            extra=extra,
        )


# ------------------------------------------------ provider 配置解析（收编）


def _version_key(name: str) -> tuple[Any, ...]:
    """版本段比较键：纯数字段转 int 数值比（sort -V 语义的 Python 形态），
    混合段按 (数值, 原文) 双键，保证 3.14.4 > 3.14.3 > 3.14.1 与空段兜底。"""
    parts: list[Any] = []
    for piece in re.split(r"[._-]", str(name)):
        parts.append((0, int(piece), "") if piece.isdigit() else (1, 0, piece))
    return tuple(parts)


def resolve_provider_config_path(home: Path | None = None) -> str:
    """zcode 内置 provider 配置解析（t-60 契约的 Python 收编形态）。

    契约（与 scripts/resolve-provider-config.sh 逐条对齐，脚本退役保留兼容）：
      1. caller 的 $ZCODE_BUILTIN_PROVIDER_CONFIG_FILE 已指存在文件 → 原样采信；
      2. ~/.zcode/v2/runtime/provider/darwin-*/<版本>/endpoint-*/zcode-builtin.json
         里**最高版本**胜出（版本段 sort -V 语义，禁 glob 序；同版本内 mtime
         新者胜）；
      3. 回退 app 内置副本（ZCODE_APP_BUNDLE_ROOT 可覆盖，测试用）；
      4. 全空 → ``""``（调用方按「缺 provider 配置」人话报，绝不猜路径）。
    """
    env_val = os.environ.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", "")
    if env_val and Path(env_val).is_file():
        return str(env_val)
    home = Path(home or Path.home())
    runtime_root = home / ".zcode" / "v2" / "runtime" / "provider"
    candidates: list[tuple[tuple[Any, ...], float, str]] = []
    try:
        for plat_dir in runtime_root.glob("darwin-*"):
            for ver_dir in plat_dir.iterdir():
                if not ver_dir.is_dir():
                    continue
                for f in ver_dir.glob("endpoint-*/zcode-builtin.json"):
                    if f.is_file():
                        try:
                            mtime = f.stat().st_mtime
                        except OSError:
                            mtime = 0.0
                        candidates.append((_version_key(ver_dir.name), mtime, str(f)))
    except OSError:
        candidates = []
    if candidates:
        candidates.sort(key=lambda t: (t[0], t[1]))
        return candidates[-1][2]
    app_root = Path(os.environ.get("ZCODE_APP_BUNDLE_ROOT", "/Applications/ZCode.app"))
    in_app = app_root / "Contents" / "Resources" / "config" / "provider" / "zcode-builtin.json"
    if in_app.is_file():
        return str(in_app)
    return ""


def provider_env_for_zcode(home: Path | None = None) -> dict[str, str]:
    """zcode 家族的配置家 env（探测继承：能读到就显式写进单元）。"""
    out: dict[str, str] = {}
    resolved = resolve_provider_config_path(home)
    if resolved:
        out["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = resolved
    personal = Path(home or Path.home()) / ".zcode" / "v2" / "provider_config.json"
    if personal.is_file():
        out["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = str(personal)
    return out


def detect_config_env(member: str, home: Path | None = None) -> dict[str, str]:
    """按家族探测配置家 env（G-7 探测三件事之一；fail-open：探不到 = 空映射，
    由配置页面补一次，绝不猜）。"""
    home = Path(home or Path.home())
    name = str(member).strip().lower()
    if name in ZCODE_FAMILY:
        return provider_env_for_zcode(home)
    spec = CONFIG_HOME_DETECTION.get(name)
    if not spec:
        return {}
    for rel in spec["config_dirs"]:
        cand = home / rel
        if cand.is_dir():
            return {str(spec["env"]): str(cand)}
    return {}


def detect_model(member: str, config_home: str | Path) -> str:
    """模型面探测（G-7 凭据归属：别人 app 的 key/模型在别人家里，能读就继承）。

    best-effort 读 <config_home>/models.json 的常见形状（{"custom_models":
    [{"id":..}]} / {"models":[{"id"|"name":..}]} / {"models":["id"]} /
    {"model": "id"}），读到第一个字符串即返回；读不到/形状不认识 = ``""``
    （配置页面让用户填一次），绝不抛错、绝不改别人的文件。"""
    path = Path(config_home) / "models.json" if config_home else None
    if not path or not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return ""

    def _first_model(node: Any) -> str:
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            for item in node:
                got = _first_model(item)
                if got:
                    return got
            return ""
        if isinstance(node, dict):
            for key in ("id", "name", "model"):
                val = node.get(key)
                if isinstance(val, str) and val:
                    return val
        return ""

    if isinstance(data, dict):
        for key in ("custom_models", "models", "model"):
            if key in data:
                got = _first_model(data[key])
                if got:
                    return got
    return ""


def detect_app_binary(member: str, home: Path | None = None) -> str:
    """app 入口二进制探测：已知别名逐个查 ~/Applications 与 /Applications
    （bundle 名精确匹配，不猜）；探不到 = ``""``。"""
    home = Path(home or Path.home())
    spec = getattr(discover_mod, "CATALOG", {}).get(str(member).strip().lower(), {})
    aliases = list(spec.get("apps") or ()) + [str(member)]
    for base in (home / "Applications", Path("/Applications")):
        for alias in aliases:
            cand = base / f"{alias}.app"
            if cand.is_dir():
                return str(cand)
    return ""


def app_executable(binary: str, home: Path | None = None) -> str:
    """.app bundle → 可执行文件绝对路径（Contents/MacOS/<CFBundleExecutable 或
    bundle 名>）。解析不到 = ``""``（入口标 manual_confirm，显式结论不猜）。"""
    b = Path(binary)
    if b.suffix != ".app" or not b.is_dir():
        return str(b) if b.is_file() else ""
    exe_name = ""
    info = b / "Contents" / "Info.plist"
    try:
        if info.is_file():
            data = plistlib.loads(info.read_bytes())
            exe_name = str(data.get("CFBundleExecutable") or "")
    except (OSError, ValueError, plistlib.InvalidFileException):
        exe_name = ""
    for cand in (b / "Contents" / "MacOS" / exe_name, b / "Contents" / "MacOS" / b.stem):
        if exe_name and cand.is_file():
            return str(cand)
    macos = b / "Contents" / "MacOS"
    if macos.is_dir():
        exe = next((p for p in sorted(macos.iterdir()) if p.is_file()), None)
        if exe:
            return str(exe)
    return ""


def entry_wrapper_path(root: Path | str, agent_id: str, entry_id: str) -> Path:
    return Path(root) / f"{WRAPPER_PREFIX}{agent_id}-{entry_id}.sh"


def entry_wrapper_body(
    agent_id: str,
    entry_id: str,
    root: Path | str,
    profile: EntryProfile,
    member: str = "",
) -> str:
    """入口档唤醒命令模板（G-6 生成物：配置家 env 与 --model 显式写进单元）。

    WB 病根断言点：裸调 codebuddy 丢 CODEBUDDY_CONFIG_DIR + --model ⇒ 零
    auth。本模板逐条 ``export`` 配置家 env（绝对路径解析自探测/配置页），
    --model / 权限参数 / 提示词按入口档组装；zcode 家族追加 provider 契约
    注入块（resolve-provider-config.sh 契约的收编形态，caller 现有值优先）。
    提示词模板里引用的 ``$AGENT_MAIL_*`` 在运行时受控展开（解析时已禁命令
    替换）；信正文经 env/文件传递，argv 注入姿态与 LocalCommandAdapter 一致。
    """
    member_key = str(member or agent_id).strip().lower()
    lines = [
        "#!/bin/bash",
        (
            f"# agent-mailbox 唤醒命令模板 — `{agent_id}` 入口 `{entry_id}`"
            "（agent-mailbox setup/config 生成, G-6/G-7）"
        ),
        "# 生成物 = 部署副本。配置家 env 与 --model 显式写进单元（G-7 铁律：",
        "# 非交互 shell 一定丢 app 导出的 env，绝不指望环境继承）。",
        "set -u",
        f"export AGENT_MAIL_HOME={_sh_quote(str(Path(root)))}",
        f"export AGENT_MAIL_ID={_sh_quote(agent_id)}",
    ]
    if profile.config_env:
        lines.append("# --- 入口档 config_env（配置家显式入单元, G-6/G-7）---")
        for k in sorted(profile.config_env):
            lines.append(f"export {_sh_key(k)}={_sh_quote(profile.config_env[k])}")
    lines.append(
        "# --- 入口档 config_env 运行时刷新（配置页/手改 wake.json 即生效, G-6）---\n"
        "# wake-cmd-entry-env.sh = 安装生成的读取器；读不到静默回退上面的烘焙值。"
    )
    lines.append(
        '_ovr="$(bash "$AGENT_MAIL_HOME/wake-cmd-entry-env.sh" "$AGENT_MAIL_HOME" '
        + _sh_quote(agent_id)
        + " "
        + _sh_quote(entry_id)
        + ' 2>/dev/null)" '
        '&& [ -n "$_ovr" ] && eval "$_ovr"'
    )
    if member_key in ZCODE_FAMILY:
        lines.append(provider_injection_bash(""))
    if profile.prompt_template:
        lines += [
            "# --- 提示词模板（模板引用的 $AGENT_MAIL_* 运行时展开; 已禁命令替换）---",
            "ENTRY_PROMPT_TEMPLATE=$(cat <<'AGENT_MAIL_PROMPT_EOF'",
            profile.prompt_template,
            "AGENT_MAIL_PROMPT_EOF",
            ")",
            'eval "ENTRY_PROMPT=\\"$ENTRY_PROMPT_TEMPLATE\\""',
        ]
    argv = [_sh_quote(profile.binary)] if profile.binary else []
    if profile.prompt_template:
        argv.append('-p "$ENTRY_PROMPT"')
    if profile.model:
        argv += ["--model", _sh_quote(profile.model)]
    perm_args = PERMISSION_MODE_ARGS.get(member_key, {}).get(profile.permission_mode, ())
    argv += [_sh_quote(a) for a in perm_args]
    argv += [_sh_quote(a) for a in profile.args]
    lines.append("set -- " + " ".join(argv) if argv else "set --")
    lines.append('exec "$@"')
    return "\n".join(lines) + "\n"


def _sh_key(k: str) -> str:
    """env 名只允许 [A-Za-z_][A-Za-z0-9_]*（配置页写入前已校验，这里兜底）。"""
    k = str(k).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
        raise ValueError(f"非法环境变量名: {k!r}")
    return k


def install_entry_unit(
    root: Path | str,
    agent_id: str,
    entry_id: str,
    profile: EntryProfile,
    member: str = "",
) -> dict[str, Any]:
    """渲染并落盘一条入口的唤醒单元（G-6：命令/包装脚本由产品生成）。

    返回 ``{"wrapper", "command", "notes"}``；二进制缺位/不可执行时按
    wake_mode 给显式结论（manual_confirm = 「可注册但需人工确认」，发版门⑦
    不许默认跳过），不生成文件也不抛错（fail-open：注册面照常落）。"""
    notes: list[str] = []
    out: dict[str, Any] = {
        "wrapper": "",
        "command": [],
        "notes": notes,
        "wake_mode": profile.wake_mode,
    }
    binary = profile.binary
    if profile.entry == "app" and binary:
        exe = app_executable(binary)
        if exe:
            binary = exe
            profile.binary = binary  # 解析结果回写：wrapper 用的是 profile.binary
        else:
            notes.append(
                f"app 入口 {entry_id}: bundle 可执行文件解析不到（{binary}）→ "
                "wake_mode=manual_confirm（可注册但需人工确认，发版门⑦显式结论）"
            )
            out["wake_mode"] = "manual_confirm"
            return out
    if not binary:
        notes.append(
            f"入口 {entry_id}: 二进制缺位 → wake_mode=manual_confirm"
            "（可注册但需人工确认；配置页面补 binary 后重装）"
        )
        out["wake_mode"] = "manual_confirm"
        return out
    wrapper = entry_wrapper_path(root, agent_id, entry_id)
    _write_executable(wrapper, entry_wrapper_body(agent_id, entry_id, root, profile, member=member))
    # 入口档 env 读取器（G-6 运行时刷新）：配置页/手改 wake.json 即生效，
    # 读不到回退包装脚本里的烘焙值——两份都是生成物，非手写。
    reader = Path(root) / "wake-cmd-entry-env.sh"
    if not reader.exists():
        _write_executable(reader, _ENTRY_ENV_READER_BODY)
    out["wrapper"] = str(wrapper)
    out["command"] = ["/bin/bash", str(wrapper)]
    out["wake_mode"] = profile.wake_mode
    return out


def load_or_rebuild(root: Path) -> tuple[wake_mod.WakeConfig, str]:
    """装 wake.json；损坏时**备份后重建**（不 crash、不静默吞）。

    返回 ``(cfg, note)``；note 非空 = 发生了重建（备份路径在里面）。健康
    配置原样装载，未知键/存量段由 WakeConfig 的往返语义保全（t-37/t-58）。"""
    cfg = wake_mod.WakeConfig.load(root)
    if cfg is not None:
        return cfg, ""
    raw = root / "wake.json"
    note = ""
    if raw.exists():
        stamp = time.strftime("%Y%m%dT%H%M%S")
        backup = root / f"wake.json.corrupt-{stamp}.bak"
        try:
            os.replace(raw, backup)
            note = f"wake.json 损坏，已备份到 {backup.name} 后重建"
        except OSError as exc:
            note = f"wake.json 损坏且备份失败（{exc}），按全新配置继续"
    return wake_mod.WakeConfig({}, root), note


# ------------------------------------------------------------ 单身份安装（一条命令装完）


def install_agent(
    root: Path | str,
    agent_id: str,
    *,
    adapter: str = "",
    command: list[str] | None = None,
    webhook_url: str = "",
    webhook_secret: str = "",
    belt: bool = False,
    activate: bool = True,
    python_exe: str | None = None,
    launch_agents_dir: Path | str | None = None,
    systemd_dir: Path | str | None = None,
    store: MailStore | None = None,
    entry_id: str = "",
    entry: dict[str, Any] | None = None,
    binary: str = "",
    config_env: dict[str, str] | None = None,
    model: str = "",
    prompt_template: str = "",
    permission_mode: str = "",
    watchdog_secs: float | None = None,
    home: Path | str | None = None,
) -> dict[str, Any]:
    """一个身份一次装完（判据1/6 的最小完备单元）：

    注册名册 → 写 ``agents.<ID>`` 段（只动本身份，t-58 硬约束）→ 生成
    plist（WatchPaths 指该身份 inbox）→ launchd load / systemd enable →
    生成唤醒命令（zcode 类带 provider env 注入模板）→ 可选 belt 脚本。

    G-6 入口档（``entry_id`` = cli|app）：入口档先探测补齐（二进制/配置家/
    模型——显式传值优先，空字段按家族探测），再落 ``agents.<ID>.entries.<entry>``
    段并渲染唤醒单元（配置家 env 与 --model 显式写进生成物）；per-entry
    plist 由 :func:`wake_mod.install` 按入口档枚举生成。``agent_id`` 与已知
    家族同名时探测表生效（codebuddy/zcode/claude/codex）。

    fail-open：存量 wake.json 的其他身份段/未知键分毫不动；损坏 config 备份
    后重建。返回结果 dict（files/wake_command/wrapper/belt/notes）。"""
    root = Path(root)
    home_p = Path(home) if home else None
    agent_id = str(agent_id).strip()
    if not agent_id:
        raise SystemExit("setup: --agent 必填（身份 id）")
    adapter = str(adapter or "").strip()
    entry_id = str(entry_id or "").strip().lower()
    notes: list[str] = []
    py = python_exe or wake_mod.default_python()
    if entry_id and adapter and adapter != "local-command":
        raise SystemExit("setup: --entry 入口档固定 local-command，与 --adapter 冲突")

    # ① 注册（幂等）——信箱存在 + 名册在位，WatchPaths 与告警才有落点
    st = store or MailStore(root)
    st.register(agent_id)

    # ② 解析通道
    argv: list[str] = []
    wrapper: Path | None = None
    entry_section: dict[str, Any] | None = None
    if entry_id:
        # G-6 入口档流程: 探测补齐（显式值优先）→ 落档 → 渲染唤醒单元。
        raw = dict(entry) if isinstance(entry, dict) else {}
        raw.setdefault("entry", entry_id)
        if binary and not raw.get("binary"):
            raw["binary"] = str(binary)
        if config_env and not raw.get("config_env"):
            raw["config_env"] = {str(k): str(v) for k, v in config_env.items()}
        if model and not raw.get("model"):
            raw["model"] = str(model)
        if prompt_template and not raw.get("prompt_template"):
            raw["prompt_template"] = str(prompt_template)
        if permission_mode and not raw.get("permission_mode"):
            raw["permission_mode"] = str(permission_mode)
        if watchdog_secs is not None and not raw.get("watchdog_secs"):
            raw["watchdog_secs"] = float(watchdog_secs)
        if not raw.get("binary"):
            if entry_id == "cli":
                found = shutil.which(agent_id) or ""
                if found:
                    notes.append(f"CLI 二进制探测: {agent_id} → {found}")
            else:
                found = detect_app_binary(agent_id, home_p)
                if found:
                    notes.append(f"app bundle 探测: {found}")
            raw["binary"] = found
        if not raw.get("config_env"):
            raw["config_env"] = detect_config_env(agent_id, home_p)
            if raw["config_env"]:
                notes.append(f"配置家探测（显式入单元）: {raw['config_env']}")
        if not raw.get("model") and isinstance(raw.get("config_env"), dict):
            first_home = next(iter(raw["config_env"].values()), "")
            raw["model"] = detect_model(agent_id, first_home)
            if raw["model"]:
                notes.append(
                    f"模型面继承探测: {raw['model']}（别人家 models.json 能读就继承, G-7）"
                )
        if not raw.get("prompt_template"):
            raw["prompt_template"] = DEFAULT_PROMPT_TEMPLATE
        try:
            profile = EntryProfile.from_dict(raw, entry_hint=entry_id)
        except ValueError as exc:
            raise SystemExit(f"setup: 入口档无效（{agent_id}/{entry_id}）: {exc}") from exc
        entry_section = profile.to_dict()
        unit = install_entry_unit(root, agent_id, entry_id, profile, member=agent_id)
        notes.extend(unit.get("notes") or [])
        argv = [str(a) for a in unit.get("command") or []]
        wrapper = Path(unit["wrapper"]) if unit.get("wrapper") else None
        if wrapper is not None:
            notes.append(f"唤醒单元已生成（配置家 env 与 --model 显式入单元）: {wrapper}")
        if not adapter:
            adapter = "local-command"
    elif adapter == "local-command":
        if not command:
            raise SystemExit(
                "setup: --adapter local-command 需要 --command（JSON argv 列表，"
                '如 \'["codex","--pull"]\'；首段自动解析为绝对路径）'
            )
        argv, note = resolve_command_head([str(a) for a in command])
        if note:
            notes.append(note)
        if is_zcode_command(argv):
            # zcode 类命令 → 唤醒命令模板（provider env 注入内置, 判据6）
            wrapper = wrapper_path(root, agent_id)
            _write_executable(
                wrapper, wake_wrapper_body(agent_id, root, argv, provider_resolver_path())
            )
            argv = ["/bin/bash", str(wrapper)]
            notes.append("zcode 类命令: 唤醒命令模板已生成（provider env 注入内置）")
    elif adapter in ("hermes", "generic-webhook"):
        if not webhook_url:
            raise SystemExit(f"setup: --adapter {adapter} 需要 --webhook-url")
    elif adapter == "claude-code":
        pass  # 终端响铃 + 桌面通知，无额外参数
    else:
        raise SystemExit(
            "setup: --adapter 必须是 hermes | generic-webhook | local-command | claude-code"
        )

    # ③ 写 agents.<ID> 段（只动本身份；存量段/未知键由 set_agent_route 保全）
    cfg, rebuild_note = load_or_rebuild(root)
    if rebuild_note:
        notes.append(rebuild_note)
    cfg.agent_id = agent_id  # plist 的 --agent 用；同时是该根的默认身份
    if entry_id and entry_section is not None:
        # G-6: 入口档落 agents.<ID>.entries.<entry>（只动自己的键，sampling
        # 等存量子键原样保留）；主唤醒路由 cli 入口优先接管，其他入口仅在
        # 尚无路由时兜底——per-entry plist 各自触发自己的入口单元。
        cfg.set_agent_entry(agent_id, entry_id, entry_section)
        if argv and (entry_id == "cli" or not cfg.agent_section(agent_id).get("command")):
            cfg.set_agent_route(agent_id, adapter="local-command", command=argv)
    elif adapter == "local-command":
        cfg.set_agent_route(agent_id, adapter=adapter, command=argv)
    elif adapter == "claude-code":
        cfg.set_agent_route(agent_id, adapter=adapter)
    else:
        cfg.set_agent_route(
            agent_id, adapter=adapter, webhook_url=webhook_url, webhook_secret=webhook_secret
        )
        # 兜底：本段没 url 时沿用 webhook.json 的全局默认（与 wake install 同款）
        try:
            wh = json.loads((root / "webhook.json").read_text(encoding="utf-8"))
            if not webhook_url and str(wh.get("url", "")):
                cfg.webhook_url = str(wh.get("url", ""))
                notes.append("webhook url 沿用 <root>/webhook.json 全局默认")
        except (OSError, json.JSONDecodeError):
            pass

    # ④ plist + launchd load（activate=False 时只生成文件）
    out = wake_mod.install(
        cfg,
        python_exe=py,
        launch_agents_dir=Path(launch_agents_dir) if launch_agents_dir else None,
        systemd_dir=Path(systemd_dir) if systemd_dir else None,
        activate=activate,
    )
    out.update(
        {
            "agent": agent_id,
            "adapter": adapter,
            "entry": entry_id,
            "entry_profile": dict(entry_section) if entry_section else {},
            "wake_command": list(argv),
            "wrapper": str(wrapper) if wrapper else "",
            "belt": "",
            "notes": notes,
        }
    )
    if entry_id and entry_section is not None and out.get("files"):
        # G-6: per-entry 唤醒单元 plist（每条入口一个 launchd 单元，程序=包装
        # 脚本；主 plist 仍跑 wake run 按身份认领，per-entry 单元供定点触发/
        # 体检）。activate 语义与主 plist 一致（False=只生成文件）。
        main_plist = Path(out["files"][0])
        entry_plist = main_plist.parent / f"{wake_mod.WAKE_LABEL}-{agent_id}-{entry_id}.plist"
        entry_wrapper = out.get("wrapper") or ""
        if entry_wrapper:
            entry_plist.write_text(
                _entry_plist_body(agent_id, entry_id, entry_wrapper, root),
                encoding="utf-8",
            )
            out["files"].append(str(entry_plist))
            out["entry_plist"] = str(entry_plist)
            if activate:
                out["entry_activated"] = wake_mod._launchctl("load", str(entry_plist))

    # ⑤ belt 脚本（可选；部署副本 = 生成产物）
    if belt:
        if adapter == "local-command":
            raw_argv = [str(a) for a in command] if command else []
            resolved, _ = resolve_command_head(raw_argv)
            if wrapper is not None:
                drain = f"bash {_sh_quote(str(wrapper))}"
            else:
                drain = " ".join(_sh_quote(a) for a in resolved)
        else:
            drain = (
                f"{_sh_quote(py)} -m agent_mailbox.wake run "
                f"--root {_sh_quote(str(root))} --agent {_sh_quote(agent_id)} --once"
            )
        # G-4: 看门狗秒数 = 入口档 watchdog_secs > agents.<ID> 段 timeout
        # （未配回 300s 默认）；belt 现场可用 WAKE_WATCHDOG_SECS env 再覆盖。
        watchdog_secs = wake_mod.DEFAULT_COMMAND_TIMEOUT
        try:
            sec = cfg.agent_section(agent_id) or {}
            entry_prof = (
                (sec.get("entries") or {}).get(entry_id)
                if entry_id and isinstance(sec.get("entries"), dict)
                else None
            )
            watchdog_secs = float(
                (entry_prof or {}).get("watchdog_secs")
                or sec.get("timeout")
                or wake_mod.DEFAULT_COMMAND_TIMEOUT
            )
        except (TypeError, ValueError):
            pass
        belt_file = belt_path(root, agent_id)
        _write_executable(
            belt_file, belt_script_body(agent_id, root, py, drain, watchdog_secs=watchdog_secs)
        )
        out["belt"] = str(belt_file)
    return out


# ------------------------------------------------- 零输入自动安装（判据1）


def auto_install(
    root: Path | str,
    report: dict[str, Any],
    *,
    belt: bool = False,
    activate: bool = True,
    launch_agents_dir: Path | str | None = None,
    systemd_dir: Path | str | None = None,
    home: Path | str | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """setup 零输入的自动安装面：discover 报告里的成员直接装好（G-7 只选名字）。

    G-6/G-7 入口档形态：有 CLI 绝对路径的成员装 **cli 入口**；有 app bundle
    证据的成员装 **app 入口**（发版门①：每 agent 的 app/cli 双入口各一条，
    证据齐就都装）。入口档字段（二进制/配置家/模型）由 :func:`install_agent`
    探测补齐——用户只选名字。已有 ``agents.<ID>.adapter`` 的成员一律跳过
    （存量用户配置分毫不动，fail-open）。app bundle 可执行文件解析不到的
    入口按 manual_confirm 显式落档（发版门⑦：可注册但需人工确认，不许默认
    跳过）。返回 ``(installed, skipped)``。"""
    root = Path(root)
    installed: list[dict[str, Any]] = []
    skipped: list[tuple[str, str]] = []
    existing = wake_mod.WakeConfig.load(root)
    for m in report.get("members", []):
        name = str(m.get("member", ""))
        if not name or m.get("kind") == "unknown":
            skipped.append((name or "?", "registry-only/未识别形态，不猜通道"))
            continue
        if existing and str((existing.agent_section(name) or {}).get("adapter", "")):
            skipped.append((name, "agents 段已有唤醒配置（存量不动）"))
            continue
        cli_paths = [
            str(ev.get("detail", ""))
            for ev in m.get("evidence", [])
            if ev.get("layer") == "L1" and ev.get("type") == "cli" and ev.get("detail")
        ]
        app_paths = [
            str(ev.get("source", ""))
            for ev in m.get("evidence", [])
            if ev.get("layer") == "L1" and ev.get("type") == "app" and ev.get("source")
        ]
        if not cli_paths and not app_paths:
            skipped.append((name, "无 CLI/app 入口证据（配置形态），自动接线不猜"))
            continue
        wanted: list[tuple[str, str]] = []
        if cli_paths:
            wanted.append(("cli", cli_paths[0]))
        if app_paths:
            wanted.append(("app", app_paths[0]))
        for entry_id, bin_path in wanted:
            try:
                out = install_agent(
                    root,
                    name,
                    entry_id=entry_id,
                    binary=bin_path,
                    belt=belt,
                    activate=activate,
                    launch_agents_dir=launch_agents_dir,
                    systemd_dir=systemd_dir,
                    home=home,
                )
            except SystemExit as exc:
                skipped.append((name, f"安装被拒: {exc}"))
                continue
            installed.append(out)
    return installed, skipped
