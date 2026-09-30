"""t-63（判据8 + 任务书 §2.2 S1–S5）: 干净 HOME 端到端场景化验收。

每个场景独立 tmp HOME（Path.home/PATH 全部钉进 fixture 目录），零存量环境
隐式依赖——新人视角从零装、从零跑：

- **S1** 干净 HOME 单身份: setup → 发测试信 → wake run → 被叫醒（假命令真
  执行）→ 回信 → 目标 pending 归零。
- **S2** 三身份同机: setup 三次 ⇒ 互不覆盖（三段三 plist）+ 各自只被自己的
  信叫醒（触发日志 wake-attempts.jsonl + 目标 pending 下降 双证）。
- **S3** 故障注入三连: 错路径命令 / 未登录态 / 身份不在名单 ⇒ doctor 逐条
  判出 + 给修法 + 非零退出。
- **S4** LLM 缺席: 无任何 CLI 登录态 ⇒ 信件流转（digest 降级全链路），
  全程断网断 subprocess-LLM 也不炸（socket 一碰就炸的对抗断言）。
- **S5** doctor 对 S3 三故障各输出**可直接粘贴执行**的修复命令（人读面 +
  JSON 面双断言）。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agent_mailbox.cli import cli_main, doctor_report
from agent_mailbox.digest import digest_path
from agent_mailbox.store import MailStore
from agent_mailbox.wake import WAKE_ATTEMPTS_FILE, WAKE_LABEL, wake_main

AUTH_LINE = "Authentication required. Please use /login to continue"

# 假被叫醒 agent: 读投递 env → 回信给发件人 → 标 done → 落 marker（触发证据）
FAKE_AGENT_BODY = """
import os
from agent_mailbox.store import MailStore
root = os.environ["AGENT_MAIL_HOME"]
me = os.environ.get("AGENT_MAIL_AGENT_ID", "")
mid = os.environ.get("AGENT_MAIL_MSG_ID", "")
st = MailStore(root)
if mid:
    try:
        frm = st.get_letter(me, mid).get("from", "boss")
        st.send(me, frm, "Re: " + os.environ.get("AGENT_MAIL_SUBJECT", "")[:40], f"handled {mid}")
    except Exception:
        pass
    st.set_status(me, mid, "done")
marker = os.environ.get("FAKE_MARKER", "")
if marker:
    open(marker, "a").write(mid + "\\n")
print("handled", mid)
"""

FAKE_AUTH_BODY = f"""
print("{AUTH_LINE}")
"""


@pytest.fixture()
def clean_home(tmp_path, monkeypatch):
    """干净 HOME: Path.home + PATH + AGENT_MAIL_HOME 全钉进 tmp（S1–S5 共用，
    每个场景一个全新目录）。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "bin").mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("PATH", str(home / "bin"))
    root = home / ".agent-mail"
    monkeypatch.setenv("AGENT_MAIL_HOME", str(root))
    return home, root


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _setup_agent(root: Path, agent: str, command: list[str], *extra: str) -> int:
    return cli_main(
        [
            "setup",
            "--home",
            str(root),
            "--agent",
            agent,
            "--adapter",
            "local-command",
            "--command",
            json.dumps(command),
            "--no-activate",
            *extra,
        ]
    )


def _fast_retry(root: Path) -> None:
    """测试时序旋钮: 重试 1 次不等待（真实部署走默认 5×60s，语义不变）。"""
    p = root / "wake.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["retry_max"] = 1
    data["retry_interval"] = 0
    p.write_text(json.dumps(data), encoding="utf-8")


def _attempts(root: Path) -> list[dict]:
    f = root / WAKE_ATTEMPTS_FILE
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]


def _letters(root: Path, agent: str) -> list[dict]:
    box = root / "inbox" / agent
    if not box.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(box.glob("*.json"))]


# ------------------------------------------------------------- S1 单身份


def test_S1_clean_home_single_identity(clean_home, tmp_path, monkeypatch):
    """装完即用: setup 一条命令 → 发信 → wake run → 假命令真执行（回信+done）
    → pending 归零。"""
    home, root = clean_home
    marker = tmp_path / "marker-worker"
    monkeypatch.setenv("FAKE_MARKER", str(marker))
    script = _write_script(tmp_path, "fake_worker.py", FAKE_AGENT_BODY)
    rc = _setup_agent(root, "worker", [sys.executable, str(script)])
    assert rc == 0
    # 装完的面: 注册 + agents 段 + plist（WatchPaths 指该身份 inbox）
    assert "worker" in MailStore(root).registry()["agents"]
    plist = home / "Library" / "LaunchAgents" / f"{WAKE_LABEL}-worker.plist"
    assert (
        plist.exists()
        and f"{root}/inbox/worker".replace("//", "/") in plist.read_text(encoding="utf-8")
        or str(root / "inbox" / "worker") in plist.read_text(encoding="utf-8")
    )
    st = MailStore(root)
    st.register("HS", kind="owner")
    st.send("HS", "worker", "task-1", "把季度数据整理好")
    assert [m["status"] for m in _letters(root, "worker")] == ["pending"]
    wake_main(["run", "--agent", "worker", "--root", str(root), "--once"])  # rc 0 = 真投出
    letter = _letters(root, "worker")[0]
    assert letter["status"] == "done"  # 被叫醒且处理完
    assert marker.exists() and letter["id"] in marker.read_text(encoding="utf-8")  # 触发证据
    replies = [m for m in _letters(root, "HS") if m["from"] == "worker"]  # 回信到了
    assert replies and "handled" in replies[0]["body"]
    assert st.check("worker", mark=False) == []  # 目标 pending 归零


# ------------------------------------------------------------- S2 三身份


def test_S2_three_identities_no_crosstalk(clean_home, tmp_path, monkeypatch):
    """各回各家: setup 三次互不覆盖；一封只给 beta 的信，只有 beta 被叫醒
    （wake-attempts 触发日志 + beta pending 1→0 双证；alpha/gamma 零动静）。"""
    home, root = clean_home
    script = _write_script(tmp_path, "fake_worker.py", FAKE_AGENT_BODY)
    agents = ("alpha", "beta", "gamma")
    for a in agents:
        assert _setup_agent(root, a, [sys.executable, str(script)]) == 0
    saved = json.loads((root / "wake.json").read_text(encoding="utf-8"))
    assert set(saved["agents"]) == set(agents)  # 三段共存 = 互不覆盖
    for a in agents:
        assert saved["agents"][a]["adapter"] == "local-command"
        assert saved["agents"][a]["command"][0] == sys.executable  # 各自绝对路径
        plist = home / "Library" / "LaunchAgents" / f"{WAKE_LABEL}-{a}.plist"
        assert plist.exists()  # 三份 plist 各指各的 inbox
        assert str(root / "inbox" / a) in plist.read_text(encoding="utf-8")
    st = MailStore(root)
    st.register("HS", kind="owner")
    st.send("HS", "beta", "only-beta", "这封只给 beta")
    for a in agents:  # 三身份各跑一轮自己的 drain（WatchPaths 语义）
        monkeypatch.setenv("FAKE_MARKER", str(tmp_path / f"marker-{a}"))
        wake_main(["run", "--agent", a, "--root", str(root), "--once"])
    # 证据① 触发日志: 只有 beta 落了 attempt 行（ok）
    rows = _attempts(root)
    assert [r["agent"] for r in rows] == ["beta"] and rows[0]["outcome"] == "ok"
    # 证据② 目标 pending 下降: beta 1→0；marker 只有 beta 被写
    assert st.check("beta", mark=False) == []
    assert (tmp_path / "marker-beta").exists()
    assert not (tmp_path / "marker-alpha").exists() and not (tmp_path / "marker-gamma").exists()
    assert _letters(root, "beta")[0]["status"] == "done"
    # 不允许「叫醒了别人」: alpha/gamma 收件箱始终为空
    assert _letters(root, "alpha") == [] and _letters(root, "gamma") == []


# ------------------------------------------------- S3/S5 共用的故障注入


def _inject_three_faults(tmp_path, clean_home) -> tuple[Path, MailStore]:
    _home, root = clean_home
    auth_script = _write_script(tmp_path, "fake_auth.py", FAKE_AUTH_BODY)
    st = MailStore(root)
    st.register("HS", kind="owner")
    # 故障① 错路径命令（setup 不拦——garbage in 进配置，doctor 负责判）
    assert _setup_agent(root, "brokenpath", ["/nonexistent/path/bin-agent"]) == 0
    # 故障② 宿主 CLI 未登录（exit 0 + Authentication required 现场特征）
    assert _setup_agent(root, "unauth", [sys.executable, str(auth_script)]) == 0
    _fast_retry(root)
    st.send("HS", "unauth", "needs login", "body")
    with pytest.raises(SystemExit) as ei:  # 失败必响: 非零退出
        wake_main(["run", "--agent", "unauth", "--root", str(root), "--once"])
    assert ei.value.code == 1
    # 故障③ 身份不在唤醒名单（有信、没装唤醒）
    st.register("gamma")
    st.send("HS", "gamma", "orphan mail", "nobody wakes for me")
    return root, st


def test_S3_fault_injection_triple(tmp_path, clean_home, capsys):
    """三连故障逐条判出 + 给修法 + doctor 非零退出。"""
    root, _st = _inject_three_faults(tmp_path, clean_home)
    report = doctor_report(root, wb_wake_log=tmp_path / "no-such-wb.log")
    checks = {c["id"]: c for c in report["checks"]}
    # ① 错路径命令 → ④ 路由判「不存在」
    routing = checks["routing"]
    assert routing["ok"] is False and "不存在" in routing["detail"]
    assert "which bin-agent" in routing["next_step"]
    # ② 未登录态 → ③ 判 auth_required（auth_required 行 + 人话根因）
    last_wake = checks["last_wake"]
    assert last_wake["ok"] is False and "auth_required" in last_wake["detail"]
    assert "缺配置家" in last_wake["next_step"]
    # ③ 身份不在名单 → ② 判 gamma 需补注册/补装（t-64 装/加载分行后归「装」维；
    #    加载维另行如实报未加载单元——假环境 launchctl 永无真加载，两维同真）
    wake_installed = checks["wake_installed"]
    assert wake_installed["ok"] is False and "gamma" in wake_installed["detail"]
    assert "agent-mailbox setup --agent gamma" in wake_installed["next_step"]
    assert checks["wake_loaded"]["ok"] is False and "缺失" in checks["wake_loaded"]["detail"]
    assert report["healthy"] is False
    # CLI 面: 非零退出
    assert cli_main(["doctor", "--home", str(root), "--json"]) == 1
    # digest 降级已把 unauth 的信消化（S4 行为共存: 故障看得到，信不丢）
    assert _letters(root, "unauth")[0]["status"] == "done"


def test_S5_doctor_pasteable_fix_commands(tmp_path, clean_home, capsys):
    """S5: 三故障各自的 next_step 都是可直接粘贴执行的命令行（非只报错误码）。"""
    root, _st = _inject_three_faults(tmp_path, clean_home)
    rc = cli_main(["doctor", "--home", str(root)])
    human = capsys.readouterr().out
    assert rc == 1
    report = doctor_report(root, wb_wake_log=tmp_path / "no-such-wb.log")
    checks = {c["id"]: c for c in report["checks"]}
    a_cmd = checks["routing"]["next_step"]  # which bin-agent …
    b_cmd = checks["last_wake"]["next_step"]  # agent-mailbox wake run --agent unauth --once
    c_cmd = checks["wake_installed"]["next_step"]  # agent-mailbox setup --agent gamma …
    assert a_cmd.startswith("which ")
    assert "agent-mailbox wake run --agent unauth --once" in b_cmd
    assert "agent-mailbox setup --agent gamma" in c_cmd
    # 人读输出同样携带这些命令（可从终端直接复制）
    for cmd in (
        "which bin-agent",
        "agent-mailbox wake run --agent unauth --once",
        "agent-mailbox setup --agent gamma",
    ):
        assert cmd in human, cmd


# ------------------------------------------------------------- S4 LLM 缺席


def test_S4_llm_absent_mail_still_flows(clean_home, tmp_path, monkeypatch):
    """没有任何 CLI 登录态: 信件流转（digest 纯本地全链路），不卡看门狗、
    不烧额度——零网络（socket 一碰就炸）+ 零 LLM 调用的对抗断言下跑通。"""

    def no_network(*a, **k):
        raise AssertionError("S4 不得有任何网络行为")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    real_popen = subprocess.Popen

    def guarded_popen(cmd, *a, **k):  # 只许 wake 投递的 CLI + discover 只读探针（lsof/ps），
        # 任何 LLM/网络类子进程 = 用例失败
        head = Path(str(cmd[0])).name if cmd else ""
        assert head in (
            "python",
            "python3",
            Path(sys.executable).name,
            "lsof",
            "ps",
        ), f"意外子进程: {cmd}"
        return real_popen(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    _home, root = clean_home
    auth_script = _write_script(tmp_path, "fake_auth.py", FAKE_AUTH_BODY)
    assert _setup_agent(root, "worker", [sys.executable, str(auth_script)]) == 0
    _fast_retry(root)
    st = MailStore(root)
    st.register("HS", kind="owner")
    st.send("HS", "worker", "工资条核对", "请核对 9 月工资条")
    st.send("HS", "worker", "周报提醒", "周五前交周报")
    with pytest.raises(SystemExit) as ei:  # 投递失败 → 非零退出（失败必响不回退）
        wake_main(["run", "--agent", "worker", "--root", str(root), "--once"])
    assert ei.value.code == 1
    # 信件流转: 两封都标 done（不再卡看门狗）
    statuses = [m["status"] for m in _letters(root, "worker")]
    assert statuses == ["done", "done"]
    # 摘要落盘 + 建议回复清单（纯本地，零 LLM）
    md = digest_path(root).read_text(encoding="utf-8")
    assert "工资条核对" in md and "周报提醒" in md
    assert "请核对 9 月工资条" in md  # 信不丢: 正文在摘要里
    assert "待人工判断" in md and "未调用 LLM" in md
    # 告警照发，降级事实进告警文本
    hs_letters = [m for m in _letters(root, "HS") if "[wake-fail]" in m["subject"]]
    assert hs_letters and "降级" in hs_letters[0]["body"] and "digest" in hs_letters[0]["body"]
    # handled_log 留痕（digest 动作 + auth_required 锚）
    letter = _letters(root, "worker")[0]
    digest_entries = [e for e in letter["handled_log"] if e["action"] == "digest"]
    assert digest_entries and "auth_required" in digest_entries[0]["note"]
    # 手动 digest CLI 兜底面: 无 pending 时空转不炸
    assert cli_main(["digest", "--home", str(root)]) == 0
