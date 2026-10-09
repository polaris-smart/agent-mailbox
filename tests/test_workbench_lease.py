"""Path leases: matcher, conflicts, TTL, release, and the pre-commit guard (T4)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_mailbox import workbench_lease as wl
from agent_mailbox.workbench_store import WorkbenchStore

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "lease-guard.py"


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    bob = store.create_employee("Bob", "codex", project["id"])
    return store, project, alice, bob


# ── 匹配器（最小 gitwildmatch 子集）─────────────────────────────────────
def test_glob_semantics():
    assert wl.matches("src/**", "src/a/b.py")
    assert wl.matches("src/*.py", "src/a.py")
    assert not wl.matches("src/*.py", "src/deep/a.py")  # * 不跨 /
    assert not wl.matches("src/**", "srcx/a.py")
    assert wl.matches("docs/", "docs/guide/x.md")  # 尾斜杠 = 整个目录
    assert wl.matches("a?c.txt", "abc.txt")
    assert not wl.matches("a?c.txt", "a/c.txt")


def test_gitignore_depth_semantics():
    """未锚定模式应在任意深度生效（真 gitignore 语义，独立审查指出的缺陷）。"""
    assert wl.matches("tests/", "pkg/tests/test_x.py")  # 目录名模式不再漏
    assert wl.matches("docs/", "a/b/docs/spec.md")
    assert wl.matches("*.md", "docs/a.md")
    assert not wl.matches("src/*.py", "src/deep/a.py")  # 含斜杠 ⇒ 锚定根
    assert not wl.matches("docs/*.md", "pkg/docs/a.md")
    assert wl.matches("src/**/x.py", "src/x.py")  # **/ = 零个或多个目录
    assert wl.matches("src/**/x.py", "src/a/b/x.py")
    assert not wl.matches("src/**/x.py", "src/abx.py")  # 不是"贪婪吞掉"
    assert wl.conflicts_with("tests/", "pkg/tests/test_x.py")


def test_conflicts_are_conservative():
    assert wl.conflicts_with("src/**", "src/app.py")
    assert wl.conflicts_with("src/**", "src/deep/**")
    assert not wl.conflicts_with("src/**", "docs/**")
    assert wl.conflicts_with("**", "anything/at/all.py")  # 过宽 ⇒ 保守判冲突


# ── 认领 / 冲突 / 释放 / 过期 ────────────────────────────────────────────
def test_claim_blocks_another_holder_with_reasons(scene):
    store, project, alice, bob = scene
    granted = wl.claim_paths(store, project["id"], alice["id"], ["src/**"], reason="重构模块")
    assert granted["granted"] and not granted["conflicts"]

    blocked = wl.claim_paths(store, project["id"], bob["id"], ["src/app.py"])
    assert blocked["granted"] == []
    assert blocked["conflicts"][0]["held_by"] == alice["id"]
    assert blocked["conflicts"][0]["reason"] == "重构模块"

    # 自己重复认领不会被自己拦住
    assert wl.claim_paths(store, project["id"], alice["id"], ["src/other.py"])["granted"]


def test_shared_claim_does_not_block_simple_overlap(scene):
    store, project, alice, bob = scene
    wl.claim_paths(store, project["id"], alice["id"], ["docs/*.md"], mode="shared")
    assert wl.check_conflicts(store, project["id"], bob["id"], ["docs/a.md"]) == []


def test_release_frees_the_paths(scene):
    store, project, alice, bob = scene
    wl.claim_paths(store, project["id"], alice["id"], ["src/**"])
    assert wl.check_conflicts(store, project["id"], bob["id"], ["src/app.py"])
    assert wl.release_paths(store, project["id"], alice["id"])["released"]
    assert wl.check_conflicts(store, project["id"], bob["id"], ["src/app.py"]) == []


def test_ttl_expires_without_any_background_reaper(scene):
    from datetime import datetime, timedelta, timezone

    store, project, alice, _bob = scene
    wl.claim_paths(store, project["id"], alice["id"], ["src/**"], ttl_seconds=60)
    assert len(wl.active_claims(store, project["id"])) == 1
    later = datetime.now(timezone.utc) + timedelta(seconds=61)
    assert wl.active_claims(store, project["id"], now=later) == []  # 派生 + TTL，无需回收器


def test_claim_never_touches_tasks_or_messages(scene):
    store, project, alice, _bob = scene
    with store._transaction() as db:
        before = (
            db.execute("SELECT count(*) AS c FROM tasks").fetchone()["c"],
            db.execute("SELECT count(*) AS c FROM messages").fetchone()["c"],
        )
    wl.claim_paths(store, project["id"], alice["id"], ["src/**"])
    with store._transaction() as db:
        after = (
            db.execute("SELECT count(*) AS c FROM tasks").fetchone()["c"],
            db.execute("SELECT count(*) AS c FROM messages").fetchone()["c"],
        )
    assert before == after


# ── 拦截器端到端 ────────────────────────────────────────────────────────
def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    for cmd in (
        ["init", "-q"],
        ["add", "src/app.py"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)
    (repo / "src" / "app.py").write_text("print(2)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "src/app.py"], check=True, capture_output=True)
    return repo


def _run_guard(repo: Path, store, project_id: str, employee: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "AGENT_MAIL_HOME": str(store.root),
        "AGENT_MAIL_PROJECT": project_id,
        "AGENT_MAIL_EMPLOYEE": employee,
    }
    return subprocess.run(
        [sys.executable, str(GUARD)], cwd=repo, capture_output=True, text=True, env=env, check=False
    )


def test_guard_blocks_a_contested_commit_and_explains_how_to_resolve(scene, tmp_path):
    store, project, alice, bob = scene
    repo = _git_repo(tmp_path)
    env_free = _run_guard(repo, store, project["id"], bob["id"])
    assert env_free.returncode == 0  # 无人认领 ⇒ 放行

    wl.claim_paths(store, project["id"], alice["id"], ["src/**"], reason="Alice 在改")
    blocked = _run_guard(repo, store, project["id"], bob["id"])
    assert blocked.returncode == 1
    assert "正被别的员工认领" in blocked.stderr and alice["id"] in blocked.stderr
    assert "--no-verify" in blocked.stderr  # 给了人工越权路径（留痕在人）

    # 认领者自己提交不被拦
    assert _run_guard(repo, store, project["id"], alice["id"]).returncode == 0


def test_guard_catches_non_ascii_paths_and_deletions(scene, tmp_path):
    """独立审查指出的两条绕过：非 ASCII 被 git 转义、删除文件不在 ACMR 里。"""
    store, project, alice, bob = scene
    repo = _git_repo(tmp_path)
    (repo / "src" / "中文文档.txt").write_text("内容\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "src/中文文档.txt"], check=True, capture_output=True
    )

    wl.claim_paths(store, project["id"], alice["id"], ["src/**"], reason="Alice 在改")
    blocked = _run_guard(repo, store, project["id"], bob["id"])
    assert blocked.returncode == 1, "非 ASCII 路径未被拦下（git 转义）"

    subprocess.run(["git", "-C", str(repo), "reset", "-q"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "rm", "-q", "--cached", "src/app.py"],
        check=True,
        capture_output=True,
    )
    removed = _run_guard(repo, store, project["id"], bob["id"])
    assert removed.returncode == 1, "删除他人认领文件未被拦下（缺 D 过滤）"


def test_guard_requires_project_configuration(scene, tmp_path):
    store, _project, _alice, _bob = scene
    repo = _git_repo(tmp_path)
    env = {**os.environ, "AGENT_MAIL_HOME": str(store.root)}
    env.pop("AGENT_MAIL_PROJECT", None)
    result = subprocess.run(
        [sys.executable, str(GUARD)], cwd=repo, capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 2
    assert "AGENT_MAIL_PROJECT" in result.stderr
