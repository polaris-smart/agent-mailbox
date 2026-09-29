"""t-60: resolve-provider-config.sh — deterministic provider-config resolver.

HS 硬要求②（2026-09-29 8ef90fad §三.2）：「最新版」解析必须给明确比较器
（本实现 = 版本段 sort -V 为主、mtime 为 tiebreak）并配两条用例（多版本并存 /
回退分支），禁靠 glob 目录序；env 值/路径不回显进日志。

These tests drive the real bash script against a fake HOME so the resolution
rules stay executable checks, not prose.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "resolve-provider-config.sh"


def run(home: Path, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def make_runtime(home: Path, versions: list[str], mtime_order: list[str] | None = None) -> None:
    """Create runtime endpoint copies: <home>/.zcode/v2/runtime/provider/darwin-aarch64/<v>/endpoint-x/zcode-builtin.json"""
    base = home / ".zcode" / "v2" / "runtime" / "provider" / "darwin-aarch64"
    for i, v in enumerate(versions):
        d = base / v / "endpoint-deadbeef"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "zcode-builtin.json"
        f.write_text("{}\n", encoding="utf-8")
        if mtime_order:
            # older files get older mtimes in the given order
            import os

            os.utime(f, (1000 + i * 10, 1000 + i * 10))


def test_multi_version_picks_highest_not_glob_order(tmp_path: Path) -> None:
    # 目录创建顺序故意倒着（3.14.1 先落盘）——禁 glob 序的用例
    make_runtime(tmp_path, ["3.14.1", "3.14.4", "3.14.3"])
    out = run(tmp_path)
    assert out.returncode == 0
    assert "3.14.4" in out.stdout
    assert "3.14.1" not in out.stdout and "3.14.3" not in out.stdout


def test_fallback_to_app_bundle_when_no_runtime(tmp_path: Path) -> None:
    # 干净 HOME（无 runtime 目录）→ 回退 app 内置副本（用可覆盖的假 bundle 根，
    # 机器真 /Applications 不得影响用例）；假副本也没有 → exit 1 空输出
    fake_app = tmp_path / "fakeapp"
    provider = fake_app / "Contents" / "Resources" / "config" / "provider"
    provider.mkdir(parents=True)
    (provider / "zcode-builtin.json").write_text("{}\n", encoding="utf-8")
    out = run(tmp_path, {"ZCODE_APP_BUNDLE_ROOT": str(fake_app)})
    assert out.returncode == 0 and out.stdout.strip() == str(provider / "zcode-builtin.json")

    # 连假 bundle 都没有 → exit 1 且 stdout 为空（调用方走自己的兜底）
    out2 = run(tmp_path, {"ZCODE_APP_BUNDLE_ROOT": str(tmp_path / "nowhere")})
    assert out2.returncode == 1 and out2.stdout.strip() == ""


def test_caller_env_wins_even_if_runtime_exists(tmp_path: Path) -> None:
    make_runtime(tmp_path, ["3.14.1", "3.14.4"])
    custom = tmp_path / "override.json"
    custom.write_text("{}\n", encoding="utf-8")
    out = run(tmp_path, {"ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": str(custom)})
    assert out.stdout.strip() == str(custom)


def test_silent_on_stderr(tmp_path: Path) -> None:
    # 路径按泄漏级处理：解析器自身不向 stderr 吐任何机器路径
    make_runtime(tmp_path, ["3.14.4"])
    out = run(tmp_path)
    assert out.stderr == ""
