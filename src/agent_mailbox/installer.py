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

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

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
) -> str:
    """per-身份 belt 脚本模板（包内渲染，绝对路径自动填）。

    单轮语义（与 wake run --once 同款）：reap → claim（t-56 认领纪律）→
    有信才 drain → 校验 done → release（信不丢）→ archive。失败必响（t-59）：
    claim 基建失败 / drain 无进展都 exit 1 + 锚A fail 行，禁 rc=0 伪装成功。
    部署副本 = 本文件；真实 drain 语义用 ``WAKE_DRAIN_CMD`` 覆盖。
    """
    return f"""#!/bin/bash
# agent-mailbox per-身份 wake belt — `{agent_id}`（agent-mailbox setup --belt 生成, t-61）
# 部署副本 = 本文件（生成产物）；模板见 src/agent_mailbox/installer.py::belt_script_body。
# 单轮: reap → claim → 有信才 drain → 校验 done → release → archive。
# 失败必响: claim 基建失败 / drain 无进展 exit 1 + 锚A fail 行（禁 rc=0 伪装, t-59）。
# drain 语义覆盖: WAKE_DRAIN_CMD env（默认=本身份的唤醒命令）。
set -u
MAIL_ROOT="${{MAIL_ROOT:-{_sh_quote(str(Path(root)))}}}"
export AGENT_MAIL_HOME="$MAIL_ROOT"
AGENT={_sh_quote(agent_id)}
MB_PY={_sh_quote(python_exe)}
LOG="$MAIL_ROOT/{BELT_PREFIX}{agent_id}.log"
ANCHOR="$MAIL_ROOT/{wake_mod.WAKE_ATTEMPTS_FILE}"
DRAIN_DEFAULT={_sh_quote(drain_cmd)}
DRAIN_CMD="${{WAKE_DRAIN_CMD:-$DRAIN_DEFAULT}}"
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

if ! "$MB_PY" -m agent_mailbox.reap --agent "$AGENT" >> "$LOG" 2>&1; then
  echo "$(date '+%F %T') reap FAILED (fail-open, continuing) agent=$AGENT" >> "$LOG"
fi

CLAIM_FILE="$MAIL_ROOT/{BELT_PREFIX}{agent_id}-claim.$$.json"
if ! "$MB_PY" -m agent_mailbox.wake claim --agent "$AGENT" --root "$MAIL_ROOT" \\
    --label "belt:$$" --out "$CLAIM_FILE" >> "$LOG" 2>&1; then
  anchor_attempt belt 1 fail claim_failed belt-claim 0
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
eval "$DRAIN_CMD" >> "$LOG" 2>&1
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
if [ -z "${{DONE_N:-}}" ] || [ "${{DONE_N:-0}}" -lt 1 ]; then
  anchor_attempt belt 1 fail no_progress belt-drain "$TURN_MS"
  echo "$(date '+%F %T') belt drain made NO progress (claimed $CLAIMED, done ${{DONE_N:-0}}) — exit 1" >> "$LOG"
  exit 1
fi
anchor_attempt belt 1 ok "" belt-drain "$TURN_MS"
"$MB_PY" -c "from agent_mailbox.store import MailStore; print('archived:', MailStore().archive_done('$AGENT'))" >> "$LOG" 2>&1 || true
echo "$(date '+%F %T') belt wake done" >> "$LOG"
"""


def _write_executable(path: Path, body: str, mode: int = 0o755) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


# ------------------------------------------------------------ config 装载（fail-open）


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
) -> dict[str, Any]:
    """一个身份一次装完（判据1/6 的最小完备单元）：

    注册名册 → 写 ``agents.<ID>`` 段（只动本身份，t-58 硬约束）→ 生成
    plist（WatchPaths 指该身份 inbox）→ launchd load / systemd enable →
    生成唤醒命令（zcode 类带 provider env 注入模板）→ 可选 belt 脚本。

    fail-open：存量 wake.json 的其他身份段/未知键分毫不动；损坏 config 备份
    后重建。返回结果 dict（files/wake_command/wrapper/belt/notes）。"""
    root = Path(root)
    agent_id = str(agent_id).strip()
    if not agent_id:
        raise SystemExit("setup: --agent 必填（身份 id）")
    adapter = str(adapter or "").strip()
    notes: list[str] = []
    py = python_exe or wake_mod.default_python()

    # ① 注册（幂等）——信箱存在 + 名册在位，WatchPaths 与告警才有落点
    st = store or MailStore(root)
    st.register(agent_id)

    # ② 解析通道
    argv: list[str] = []
    wrapper: Path | None = None
    if adapter == "local-command":
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
    if adapter == "local-command":
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
            "wake_command": list(argv),
            "wrapper": str(wrapper) if wrapper else "",
            "belt": "",
            "notes": notes,
        }
    )

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
        belt_file = belt_path(root, agent_id)
        _write_executable(belt_file, belt_script_body(agent_id, root, py, drain))
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
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """setup 零输入的自动安装面：discover 报告里的 CLI 形态成员直接装好。

    只对「有 CLI 绝对路径」的成员动手（这是唯一能不猜就接上的通道——
    discover 铁律：kind 不猜）；app/未识别/registry-only 成员跳过并给
    指名道姓的下一步。已有 ``agents.<ID>.adapter`` 的成员一律跳过（存量
    用户配置分毫不动，fail-open）。返回 ``(installed, skipped)``。"""
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
        if not cli_paths:
            skipped.append((name, "无 CLI on PATH（app/配置形态），自动接线不猜"))
            continue
        try:
            out = install_agent(
                root,
                name,
                adapter="local-command",
                command=[cli_paths[0]],
                belt=belt,
                activate=activate,
                launch_agents_dir=launch_agents_dir,
                systemd_dir=systemd_dir,
            )
        except SystemExit as exc:
            skipped.append((name, f"安装被拒: {exc}"))
            continue
        installed.append(out)
    return installed, skipped
