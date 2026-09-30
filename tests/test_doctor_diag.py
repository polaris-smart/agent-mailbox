"""t-64（0.7.6 收口批第 1 批诊断面）。

- G-1: doctor ② 「装」与「加载」分开报两行；加载 = label 出现在
  launchctl/systemctl 实况（测试注入假探测，绝不真跑 launchctl）；
  加载缺失判 fail 并点名缺失 label。
- G-2: doctor ⑦ breaker 检查三态（无 / 新鲜 ≤10min / 陈旧 >10min 判
  闩死 fail，输出 latched_at/rounds/pending_before→pending_after）；
  闩死期间 ③「最近一次唤醒」不得报「正常」（顺带降级）。
- G-3: doctor ⑧ 仓内 scripts/<name> vs 部署 <root>/<name> sha256 逐对
  比对（只查不同步）；不一致/未部署判 fail 并给两侧短 sha + 行数；
  仓内基准缺失（wheel 安装）降级为跳过不误报。

实况回归锚点（HS 亲测 2026-09-29）：HS/ZC 已加载、WB 未加载 ⇒ ② 加载
必须报 fail 点名 agent-mailbox-wake-WB；仓内 wake-zc.sh（269 行）≠ 部署
（194 行）⇒ ⑧ 必须报 fail——测试按同构样态断言，不依赖真机状态。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_mailbox.cli import DOCTOR_TITLES, doctor_report

WAKE_LABEL = "com.polaris-smart.agent-mailbox-wake"
CMD = [sys.executable, "-c", "pass"]  # ④ 路由体检要求的绝对路径 command


def _mkroot3(tmp_path: Path) -> Path:
    """三身份（HS/WB/ZC）标准根：wake.json agents 段 + inbox/ZC。"""
    root = tmp_path / "mail"
    (root / "inbox" / "ZC").mkdir(parents=True)
    (root / "wake.json").write_text(
        json.dumps(
            {
                "agent_id": "ZC",
                "adapter": "local-command",
                "command": CMD,
                "agents": {
                    aid: {"adapter": "local-command", "command": CMD} for aid in ("HS", "WB", "ZC")
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def _mkla(tmp_path: Path, *ids: str) -> Path:
    la = tmp_path / "launchagents"
    la.mkdir(parents=True, exist_ok=True)
    for i in ids:
        (la / f"{WAKE_LABEL}-{i}.plist").write_text("<plist/>", encoding="utf-8")
    return la


def _probe(*ids: str):
    """假加载实况：只有列出的 id 已加载。"""
    return lambda: ({f"{WAKE_LABEL}-{i}" for i in ids}, "ok")


def _probe_status(status: str):
    return lambda: (set(), status)


def _norepo(tmp_path: Path) -> Path:
    return tmp_path / "no-repo-scripts"


def _check(report: dict, cid: str) -> dict:
    return next(c for c in report["checks"] if c["id"] == cid)


def _report(root: Path, tmp_path: Path, **kw) -> dict:
    base: dict = {
        "wb_wake_log": tmp_path / "no-such-wb.log",
        "launch_agents_dir": _mkla(tmp_path, "HS", "WB", "ZC"),
        "loaded_probe": _probe("HS", "WB", "ZC"),
        "repo_scripts_dir": _norepo(tmp_path),
    }
    base.update(kw)
    return doctor_report(root, **base)


def _mkrepo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo-scripts"
    repo.mkdir(exist_ok=True)
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    return repo


def _short_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


# ---------------------------------------------------------------- G-1


def test_doctor_g1_titles_have_installed_loaded_split():
    """② 拆两行 + ⑦⑧⑨ 就位：DOCTOR_TITLES 顺序契约（t-65 追加 ⑨ 告警可达性）。"""
    assert [i for i, _ in DOCTOR_TITLES] == [
        "root",
        "wake_installed",
        "wake_loaded",
        "last_wake",
        "routing",
        "backlog",
        "host_auth",
        "breaker",
        "wake_scripts",
        "alert_reach",
        "entries",
    ]


def test_doctor_g1_installed_and_loaded_split_wb_not_loaded(tmp_path):
    """G-1 主样态（收口批实况同构）：三身份全在盘、launchctl 只有 HS/ZC
    ⇒ ② 装载 pass 3/3、② 加载 fail 且点名 agent-mailbox-wake-WB。"""
    root = _mkroot3(tmp_path)
    report = _report(root, tmp_path, loaded_probe=_probe("HS", "ZC"))
    installed = _check(report, "wake_installed")
    loaded = _check(report, "wake_loaded")
    assert installed["ok"] is True and "3/3" in installed["detail"]
    assert loaded["ok"] is False
    assert f"{WAKE_LABEL}-WB" in loaded["detail"]
    assert "缺失" in loaded["detail"]
    assert report["healthy"] is False


def test_doctor_g1_all_loaded_pass(tmp_path):
    """三身份装+加载全齐 ⇒ ② 两行都绿、整体健康。"""
    root = _mkroot3(tmp_path)
    report = _report(root, tmp_path)
    assert _check(report, "wake_installed")["ok"] is True
    loaded = _check(report, "wake_loaded")
    assert loaded["ok"] is True and "3/3" in loaded["detail"]
    assert report["healthy"] is True


def test_doctor_g1_probe_unavailable_is_not_fail(tmp_path):
    """探测不可用（命令失败/平台不支持）→ 加载行标注「无法探测」不作判据，
    绝不误判 fail（探测不到 ≠ 没加载）。"""
    root = _mkroot3(tmp_path)
    for status in ("unavailable", "unsupported"):
        report = _report(root, tmp_path, loaded_probe=_probe_status(status))
        loaded = _check(report, "wake_loaded")
        assert loaded["ok"] is True
        assert "无法探测" in loaded["detail"] and status in loaded["detail"]


def test_doctor_g1_installed_missing_is_independent_of_loaded(tmp_path):
    """装/加载两行互相独立：WB 单元不在盘（装 fail）但假实况全加载
    （加载 pass）——两行各判各的，不串味。"""
    root = _mkroot3(tmp_path)
    la = tmp_path / "launchagents-partial"  # 只装了 ZC（独立目录防夹具串台）
    la.mkdir()
    (la / f"{WAKE_LABEL}-ZC.plist").write_text("<plist/>", encoding="utf-8")
    report = _report(root, tmp_path, launch_agents_dir=la)
    installed = _check(report, "wake_installed")
    loaded = _check(report, "wake_loaded")
    assert installed["ok"] is False and "WB" in installed["detail"] and "HS" in installed["detail"]
    assert loaded["ok"] is True and "3/3" in loaded["detail"]


# ---------------------------------------------------------------- G-2


def _mkbreaker(root: Path, *, minutes_ago: float, body: str | None = None) -> None:
    """自造闩死样本（v2.2 勘误口径：沙箱造样 → 断言 → 样本随 tmp 销毁）。

    latched_at 用本地钟面格式（与 wake-zc.sh 的 `date '+%F %T'` 同源）：
    UTC now 减偏移后 .astimezone() 挂本地时区再格式化。
    """
    text = (
        body
        if body is not None
        else (
            "latched_at="
            + (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
            + " pending_before=3 pending_after=3 rounds=5 breaker_n=5"
        )
    )
    (root / "wake-zc.breaker").write_text(text, encoding="utf-8")


def _mk_ok_attempts(root: Path) -> None:
    """③ 的「全 ok」唤醒记录——breaker 闩死时也不许它独绿。"""
    (root / "wake-attempts.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-09-29T08:00:00Z",
                "agent": "ZC",
                "route": "belt",
                "attempt": 1,
                "outcome": "ok",
                "error_class": "",
                "executor": "local-command",
                "latency_ms": 12,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_doctor_g2_breaker_absent_pass(tmp_path):
    """无闩死样本（文件不存在）⇒ ⑦ pass。"""
    root = _mkroot3(tmp_path)
    report = _report(root, tmp_path)
    breaker = _check(report, "breaker")
    assert breaker["ok"] is True and "不存在" in breaker["detail"]


def test_doctor_g2_breaker_fresh_not_latched(tmp_path):
    """新鲜样本（latched_at 距今 < 10 分钟）⇒ ⑦ pass 但点名冷却窗。"""
    root = _mkroot3(tmp_path)
    _mkbreaker(root, minutes_ago=1)
    report = _report(root, tmp_path)
    breaker = _check(report, "breaker")
    assert breaker["ok"] is True
    assert "新鲜" in breaker["detail"] and "rounds=5" in breaker["detail"]
    # ③ 不被新鲜样本按住（只有陈旧才降级）
    assert _check(report, "last_wake")["ok"] is True
    assert report["healthy"] is True


def test_doctor_g2_breaker_stale_fail_with_fields_and_downgrades_last_wake(tmp_path):
    """陈旧闩死（latched_at 距今 > 10 分钟）⇒ ⑦ fail 且原样吐
    latched_at / rounds / pending_before→pending_after；③ 即便 attempts 全
    ok 也被顺带降级为 fail（「唤醒正常」不可信）。"""
    root = _mkroot3(tmp_path)
    _mk_ok_attempts(root)
    _mkbreaker(root, minutes_ago=20)
    report = _report(root, tmp_path)
    breaker = _check(report, "breaker")
    assert breaker["ok"] is False
    assert "latched_at=" in breaker["detail"]
    assert "rounds=5" in breaker["detail"]
    assert "pending=3→3" in breaker["detail"]
    assert breaker["next_step"]
    last_wake = _check(report, "last_wake")
    assert last_wake["ok"] is False
    assert "闩死" in last_wake["detail"] and "不可信" in last_wake["detail"]
    assert report["healthy"] is False


def test_doctor_g2_breaker_mtime_fallback_when_latched_at_unparseable(tmp_path):
    """latched_at 解析不了 → age 退回文件 mtime；改老 mtime 照样判闩死
    （两类自造样本都能命中）。"""
    root = _mkroot3(tmp_path)
    _mkbreaker(root, minutes_ago=0, body="reason=no_progress unknown_key=1\n")
    old = time.time() - 1200
    os.utime(root / "wake-zc.breaker", (old, old))
    report = _report(root, tmp_path)
    breaker = _check(report, "breaker")
    assert breaker["ok"] is False and "闩死" in breaker["detail"]
    assert report["healthy"] is False


# ---------------------------------------------------------------- G-3


REPO_WAKE = "#!/bin/zsh\n# repo wake-zc.sh\n" + "echo repo-line\n" * 8  # 10 行
DEP_WAKE = "#!/bin/zsh\n# old deployed wake\n" + "echo old\n" * 3  # 5 行
REPO_RESOLVE = "#!/bin/zsh\nset -euo pipefail\necho resolve\n"  # 3 行


def test_doctor_g3_scripts_match_pass(tmp_path):
    """两侧逐字节一致 ⇒ ⑧ pass 且两对都报「一致」。"""
    root = _mkroot3(tmp_path)
    repo = _mkrepo(tmp_path, {"wake-zc.sh": REPO_WAKE, "resolve-provider-config.sh": REPO_RESOLVE})
    (root / "wake-zc.sh").write_text(REPO_WAKE, encoding="utf-8")
    (root / "resolve-provider-config.sh").write_text(REPO_RESOLVE, encoding="utf-8")
    report = _report(root, tmp_path, repo_scripts_dir=repo)
    check = _check(report, "wake_scripts")
    assert check["ok"] is True
    assert check["detail"].count("一致") == 2
    assert f"{_short_sha(root / 'wake-zc.sh')}…/10行" in check["detail"]
    assert report["healthy"] is True


def test_doctor_g3_scripts_mismatch_fail_with_both_digests(tmp_path):
    """实况同构（仓内 269 行 vs 部署 194 行的收口批样本）⇒ ⑧ fail，
    打印两侧短 sha + 两侧行数；另一对一致不连坐。"""
    root = _mkroot3(tmp_path)
    repo = _mkrepo(tmp_path, {"wake-zc.sh": REPO_WAKE, "resolve-provider-config.sh": REPO_RESOLVE})
    (root / "wake-zc.sh").write_text(DEP_WAKE, encoding="utf-8")
    (root / "resolve-provider-config.sh").write_text(REPO_RESOLVE, encoding="utf-8")
    report = _report(root, tmp_path, repo_scripts_dir=repo)
    check = _check(report, "wake_scripts")
    assert check["ok"] is False
    repo_sha = _short_sha(tmp_path / "repo-scripts" / "wake-zc.sh")
    dep_sha = _short_sha(root / "wake-zc.sh")
    assert repo_sha != dep_sha
    assert f"不一致 仓内 {repo_sha}(10行) vs 线上 {dep_sha}(5行)" in check["detail"]
    assert "resolve-provider-config.sh: 一致" in check["detail"]
    assert report["healthy"] is False


def test_doctor_g3_deployed_missing_fail(tmp_path):
    """仓内有、线上无 ⇒ ⑧ fail「线上未部署」（比「不一致」更早的一档）。"""
    root = _mkroot3(tmp_path)
    repo = _mkrepo(tmp_path, {"wake-zc.sh": REPO_WAKE, "resolve-provider-config.sh": REPO_RESOLVE})
    (root / "resolve-provider-config.sh").write_text(REPO_RESOLVE, encoding="utf-8")
    report = _report(root, tmp_path, repo_scripts_dir=repo)
    check = _check(report, "wake_scripts")
    assert check["ok"] is False
    assert "线上未部署" in check["detail"] and "wake-zc.sh" in check["detail"]


def test_doctor_g3_repo_baseline_missing_skips(tmp_path):
    """仓内基准缺失（wheel 安装没有 scripts/）⇒ 降级为跳过，不误报 fail。"""
    root = _mkroot3(tmp_path)
    report = _report(root, tmp_path, repo_scripts_dir=_norepo(tmp_path))
    check = _check(report, "wake_scripts")
    assert check["ok"] is True
    assert "跳过比对" in check["detail"] and "wake-zc.sh" in check["detail"]
    assert report["healthy"] is True
