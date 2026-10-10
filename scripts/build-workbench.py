#!/usr/bin/env python3
"""Build a native workbench bundle from already installed, pinned local runtimes.

Run this script with an isolated Python environment containing the project and
PyInstaller. It does not install dependencies, fetch updates, sign for distribution,
or modify agent credentials/configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ── 零引擎默认（老板 2026-10-07 亲口确认 ✓）──
# 「我们给用户的里面，就是干净的工具，**不包括任何 agent cli**」✗
# 默认 False ✓；只有内部测试才用 AGENT_MAILBOX_BUILD_ENGINES=1 显式打开 ✓
INCLUDE_ENGINES = os.environ.get("AGENT_MAILBOX_BUILD_ENGINES") == "1"

# 原生可执行文件的魔数（只收集**真的是可执行**的文件，不靠文件名猜）。
EXECUTABLE_MAGIC = (
    b"\xcf\xfa\xed\xfe",  # Mach-O 64 LE
    b"\xce\xfa\xed\xfe",  # Mach-O 32 LE
    b"\xfe\xed\xfa\xcf",  # Mach-O 64 BE
    b"\xfe\xed\xfa\xce",  # Mach-O 32 BE
    b"\xca\xfe\xba\xbe",  # fat Mach-O
    b"\xbf\xba\xfe\xca",  # fat Mach-O swapped
    b"\x7fELF",  # Linux
    b"MZ",  # Windows
)


def require_desktop_shell() -> None:
    """Frozen macOS builds must ship the native window shell, or the UI lands in the browser.

    实测（2026-10-06）：构建环境缺 pyobjc ⇒ PyInstaller 收不到 ``objc``/``AppKit``
    （warn 文件明写 missing module）⇒ 冻结包里 ``workbench_desktop`` import 失败 ⇒
    ``_desktop_wanted()`` 返 False ⇒ 界面跑进默认浏览器，而 app 自己不开窗。
    """
    if sys.platform != "darwin":
        return
    missing: list[str] = []
    for module in ("objc", "AppKit", "WebKit"):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    if missing:
        raise RuntimeError(
            "Install pyobjc in the build environment (missing: "
            + ", ".join(missing)
            + "); otherwise the packaged app opens in the default browser instead of its own window"
        )


def require_static_macos_crypto(binary: Path | None = None) -> None:
    """Do not freeze conflicting Python and cryptography OpenSSL libraries together."""
    if sys.platform != "darwin":
        return
    if binary is None:
        spec = importlib.util.find_spec("cryptography.hazmat.bindings._rust")
        if spec is None or not spec.origin:
            raise RuntimeError("Cannot locate the cryptography extension for package validation")
        binary = Path(spec.origin)
    dependencies = subprocess.run(
        ["otool", "-L", str(binary)], capture_output=True, text=True, check=True
    ).stdout
    linked = [
        line.strip().split(" (", 1)[0]
        for line in dependencies.splitlines()[1:]
        if re.search(r"(?:^|/)(?:libssl|libcrypto)[^/]*\.dylib(?:\s|$)", line.strip())
    ]
    if linked:
        raise RuntimeError(
            "cryptography dynamically links OpenSSL; freezing it can mix incompatible "
            "Python and cryptography libraries. Rebuild cryptography in the isolated build "
            "environment with OPENSSL_STATIC=1, OPENSSL_DIR set to the chosen OpenSSL, "
            "and pip --force-reinstall --no-cache-dir --no-binary=cryptography. "
            f"Extension: {binary}; dependencies: {linked}"
        )


def runtime_binaries(runtime_dir: Path) -> list[tuple[str, str]]:
    """Native executables inside the pinned runtime that must ship with the bundle.

    只靠 ``datas`` 收集 ``node_modules`` 时，PyInstaller 会**静默漏掉**部分原生可执行文件：
    实测 ``@openai/codex-darwin-arm64/vendor/<target>/bin/{codex,codex-code-mode-host}``
    （228MB + 62MB）没进产物，装好后 app 直接报 ``RUNTIME_MISSING``。
    这里显式按 ``binaries=`` 收集（只看父目录名为 ``bin`` 且带可执行魔数的文件）。
    """
    if not INCLUDE_ENGINES:
        # 零引擎默认：**不收集任何引擎二进制** ✗（法律红线 + 用户选择权 ✓ 老板拍板 ✓）
        return []
    entries: list[tuple[str, str]] = []
    root = runtime_dir / "node_modules"
    if not root.is_dir():
        return entries
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.parent.name != "bin":
            continue
        try:
            with path.open("rb") as handle:
                head = handle.read(4)
        except OSError:
            continue
        if not any(head.startswith(magic) for magic in EXECUTABLE_MAGIC):
            continue
        destination = f"runtime/deps/{path.relative_to(runtime_dir).parent.as_posix()}"
        entries.append((str(path), destination))
    return entries


def expected_runtime_binaries() -> tuple[str, ...]:
    """独立于收集逻辑的**期望清单**（来自 codex_pair.mjs 的约定）。

    收集与自检若同源，系统性漏收时自检必然通过 —— 正是当初 `RUNTIME_MISSING` 的形状
    （第 10 轮 [中] ③-1）。这里从代码里读期望的二进制名。
    """
    text = (SOURCE / "runtime_bridge/codex_pair.mjs").read_text(encoding="utf-8")
    names = set(re.findall(r'"(codex(?:-code-mode-host)?)"', text))
    return tuple(sorted(names or {"codex", "codex-code-mode-host"}))


def repair_expected_binaries(resources: Path, runtime_dir: Path) -> list[str]:
    """打包后**修复**：PyInstaller 实测会漏掉大二进制（2026-10-07：`codex-code-mode-host` 65MB ✗）。

    从源运行时里搜同名**真文件**（非软链 ✓）补拷进产物 ✓；源里也找不到才失败 ✓（提示先 npm ci ✓）。
    """
    if not INCLUDE_ENGINES:
        # 零引擎默认：不含引擎 ⇒ 没有"期望二进制"可补拷 ✗
        return []
    repaired: list[str] = []

    def _real_match(name: str) -> bool:
        """与自检**同一判定** ✗：真文件 ✓ 非符号链接 ✓（否则 `.bin/codex` 软链会让补拷误判"已存在" ✗）"""
        return any(
            path.name == name and path.is_file() and not path.is_symlink()
            for path in resources.rglob(name)
        )

    for name in expected_runtime_binaries():
        if _real_match(name):
            continue
        source = next(
            (
                candidate
                for candidate in (runtime_dir / "node_modules").rglob(name)
                if candidate.is_file() and not candidate.is_symlink()
            ),
            None,
        )
        if source is None:
            raise RuntimeError(f"源运行时里也找不到 {name!r} ⇒ 无法修复（先跑 npm ci ✓）")
        destination = resources / "runtime" / "deps" / source.relative_to(runtime_dir).parent
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination / name)
        repaired.append(name)
    return repaired


def verify_expected_binaries(resources: Path) -> None:
    """期望清单核对 ✓ 且**不只看文件名** ✗（评审：只判名 ⇒ 0 字节/软链/目录全 PASS ✗）。

    每个期望二进制必须是：真文件 ✓ 非符号链接 ✓ 大小 > 0 ✓ 带可执行魔数 ✓。
    期望名来自 `codex_pair.mjs` ✓（与收集逻辑不同源 ✓ 否则系统性漏收时自检必然通过 ✗）。
    """
    if not INCLUDE_ENGINES:
        # 零引擎默认：不含引擎 ⇒ 没有"期望二进制"可校验 ✗
        return
    for name in expected_runtime_binaries():
        matches = [
            path for path in resources.rglob(name) if path.is_file() and not path.is_symlink()
        ]
        if not matches:
            raise RuntimeError(
                f"Bundle is missing expected runtime executable {name!r} "
                "(the packer dropped it; check binaries= collection)"
            )
        for path in matches:
            if path.stat().st_size == 0:
                raise RuntimeError(f"{name!r} 在产物里是**空文件** ✗（大小为 0）")
            with path.open("rb") as handle:
                head = handle.read(4)
            if not any(head.startswith(magic) for magic in EXECUTABLE_MAGIC):
                raise RuntimeError(f"{name!r} 在产物里**不是可执行文件** ✗（魔数不符）")


def verify_runtime_binaries(resources: Path, entries: list[tuple[str, str]]) -> None:
    """Fail the build loudly when a native runtime executable is missing from the bundle.

    没有这条自检，漏掉的二进制要到用户第一次派活时才以 ``RUNTIME_MISSING`` 暴露。
    """
    if not INCLUDE_ENGINES:
        # 零引擎默认：发行包不含引擎 ⇒ 没有"原生运行时"可校验 ✗
        return
    # 按**名字搜索**而不是精确前缀 ✗（2026-10-07 实测：macOS .app 现把 runtime 放
    # `Contents/Frameworks/...` ✓ 而旧代码写死 `resources/destination/...` ✗ ⇒ 布局一变就误报 ✓）
    missing = [
        source
        for source, _destination in entries
        if not any(
            candidate.name == Path(source).name
            and candidate.is_file()
            and not candidate.is_symlink()
            for candidate in resources.rglob(Path(source).name)
        )
    ]
    if missing:
        raise RuntimeError(
            "Bundle is missing native runtime executables (the packer dropped them): "
            + ", ".join(missing)
        )


def validate_runtime(runtime_dir: Path, node: Path) -> str:
    version = subprocess.run([str(node), "--version"], capture_output=True, text=True, check=True)
    node_version = version.stdout.strip()
    if tuple(int(part) for part in node_version.removeprefix("v").split(".")) < (22, 13, 0):
        raise ValueError("Node.js >=22.13 is required")
    subprocess.run([str(node), "--check", str(SOURCE / "runtime_bridge/bridge.mjs")], check=True)
    for name, expected in json.loads((SOURCE / "runtime_bridge/package.json").read_text())[
        "dependencies"
    ].items():
        filename = runtime_dir / "node_modules" / name / "package.json"
        if not filename.is_file() or json.loads(filename.read_text())["version"] != expected:
            raise ValueError(f"Install pinned {name}@{expected} before building")
    for filename in ("package.json", "package-lock.json"):
        if not (runtime_dir / filename).is_file():
            raise ValueError(f"Runtime directory must contain {filename}")
        if (runtime_dir / filename).read_bytes() != (
            SOURCE / "runtime_bridge" / filename
        ).read_bytes():
            raise ValueError(
                f"Runtime {filename} differs from the checked-in lock; reinstall explicitly"
            )
    subprocess.run(
        [
            str(node),
            "--input-type=module",
            "-e",
            "const {resolveCodexPair}=await import(process.argv[1]);await resolveCodexPair(process.argv[2]);",
            (SOURCE / "runtime_bridge/codex_pair.mjs").as_uri(),
            str(runtime_dir),
        ],
        check=True,
    )
    return node_version


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY / "src/agent_mailbox"


def build(runtime_dir: Path, node: Path, output: Path, name: str) -> dict:
    if sys.platform not in {"darwin", "win32", "linux"}:
        raise RuntimeError("Build on a supported native macOS, Windows or Linux host")
    if importlib.util.find_spec("PyInstaller") is None:
        raise RuntimeError("Install PyInstaller in the selected isolated Python environment first")
    runtime_dir, node, output = runtime_dir.resolve(), node.resolve(), output.resolve()
    require_desktop_shell()
    require_static_macos_crypto()
    # Git Bash which omits PATHEXT; Windows can execute that name but PyInstaller
    # requires the actual on-disk executable, including its .exe suffix.
    if sys.platform == "win32" and not node.is_file() and node.with_suffix(".exe").is_file():
        node = node.with_suffix(".exe")
    if not node.is_file():
        raise ValueError("Choose the existing Node.js executable file")
    if output.is_relative_to(REPOSITORY) or output in REPOSITORY.parents:
        raise ValueError("Choose an isolated build output directory")
    node_version = validate_runtime(runtime_dir, node)
    output.mkdir(parents=True, exist_ok=True)
    # Preserve dependency notices in the frozen distribution, including Python
    # distributions whose metadata PyInstaller would otherwise omit.
    notices = output / "third-party-licenses"
    notices.mkdir(exist_ok=True)
    inventory = []
    for distribution in importlib.metadata.distributions():
        copied = []
        distribution_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", distribution.metadata["Name"])
        for filename in distribution.files or []:
            source = Path(distribution.locate_file(filename))
            if not source.is_file() or not any(
                source.name.upper().startswith(prefix)
                for prefix in ("LICENSE", "COPYING", "NOTICE")
            ):
                continue
            destination = notices / distribution_name / str(len(copied)) / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            copied.append(str(destination.relative_to(notices)))
        if copied:
            inventory.append(
                {"name": distribution_name, "version": distribution.version, "notices": copied}
            )
    (notices / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    # CI installs dependencies beside the bridge. Ship its source separately,
    # keeping the locked node_modules tree in exactly one runtime location.
    bridge_source = output / "bridge-source"
    if bridge_source.exists():
        shutil.rmtree(bridge_source)
    shutil.copytree(
        SOURCE / "runtime_bridge",
        bridge_source,
        ignore=shutil.ignore_patterns("node_modules", ".cache"),
    )
    from PyInstaller.building.makespec import main as make_spec

    version = importlib.metadata.version("agent-mailbox")
    match = re.fullmatch(r"(\d+\.\d+\.\d+)([ab]\d+)?", version)
    if not match:
        raise ValueError("The macOS bundle requires a release or alpha/beta version")
    short_version = match.group(1)
    info_plist = {
        "CFBundleShortVersionString": short_version,
        "CFBundleVersion": short_version,
        "CFBundleGetInfoString": f"Agent Mailbox {version}",
        "AgentMailboxVersion": version,
    }
    if sys.platform == "darwin":
        # The native bundle is a browser launcher plus a local HTTP service; it
        # has no AppKit window or event loop. Advertising it as a foreground
        # application leaves Launch Services waiting for a windowed launch to
        # finish, so the Dock icon keeps bouncing and delayed reopens can launch
        # another browser tab. Keep it as an accessory application instead.
        info_plist["LSUIElement"] = True
    spec = Path(
        make_spec(
            [str(SOURCE / "workbench_app.py")],
            name=name,
            console=sys.platform != "darwin",
            onefile=False,
            shorthand_manifest=None,
            bundle_identifier="com.polaris-smart.agent-mailbox",
            specpath=str(output),
            pathex=[str(REPOSITORY / "src")],
            copy_metadata=["agent-mailbox"],
            collect_submodules=["mcp.server"],
            datas=[
                (str(REPOSITORY / "LICENSE"), "licenses"),
                (str(REPOSITORY / "NOTICE"), "licenses"),
                (str(REPOSITORY / "LICENSES"), "licenses/legacy"),
                (str(notices), "licenses/python-dependencies"),
                (str(bridge_source), "agent_mailbox/runtime_bridge"),
                (str(SOURCE / "workbench_assets"), "agent_mailbox/workbench_assets"),
                # 零引擎默认：**引擎依赖树整棵都不进包** ✗（老板：「干净的工具，不含任何 agent cli」✓）
                # —— 上一片只挡了 `binaries=` ✓ 引擎却仍从 `datas=` 整棵进来 ✗（实测 777M / 9 个引擎二进制 ✓）
                *(
                    [
                        (str(runtime_dir / "node_modules"), "runtime/deps/node_modules"),
                    ]
                    if INCLUDE_ENGINES
                    else []
                ),
                (str(runtime_dir / "package.json"), "runtime/deps"),
                (str(runtime_dir / "package-lock.json"), "runtime/deps"),
            ],
            binaries=[(str(node), "runtime/bin"), *runtime_binaries(runtime_dir)],
        )
    )
    content = spec.read_text()
    # Hooks can re-collect editable metadata even when initial data is sanitized.
    # Filter the final Analysis data before COLLECT/BUNDLE and signing.
    marker = "pyz = PYZ(a.pure)"
    if content.count(marker) != 1:
        raise RuntimeError("PyInstaller generated an unexpected analysis specification")
    content = content.replace(
        marker,
        "a.datas = [entry for entry in a.datas if "
        "entry[0].replace(chr(92), '/').rsplit('/', 1)[-1] not in "
        "{'direct_url.json', 'uv_build.json', 'uv_cache.json'}]\n" + marker,
    )
    spec.write_text(content)
    if sys.platform == "darwin":
        bundle_start = "app = BUNDLE(\n    coll,\n"
        if content.count(bundle_start) != 1:
            raise RuntimeError("PyInstaller generated an unexpected macOS specification")
        spec.write_text(
            content.replace(
                bundle_start,
                bundle_start + f"    version={short_version!r},\n    info_plist={info_plist!r},\n",
            )
        )
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(output / "dist"),
        "--workpath",
        str(output / "build"),
        str(spec),
    ]
    environment = {**os.environ, "PYINSTALLER_CONFIG_DIR": str(output / "cache")}
    subprocess.run(command, cwd=REPOSITORY, env=environment, check=True)
    app = output / "dist" / (f"{name}.app" if sys.platform == "darwin" else name)
    # 校验根 = **产物根**（`app` 本身 ✓）—— 2026-10-07 实测：macOS `.app` 里真二进制在
    # `Contents/Frameworks/runtime/...` ✗ 而旧代码只搜 `Contents/Resources` ✓ ⇒ 明明拷进去了却
    # 误报"被打包器丢了" ✗（同一 app 两处都有 runtime ✓ 大头在 Frameworks ✓）
    # ⇒ 对整个产物根做 rglob ✓ 两种布局（.app / onedir）都对 ✓
    resources = app
    if sys.platform == "darwin":
        extensions = {
            path.resolve()
            for path in app.rglob("_rust*.so")
            if "cryptography" in path.parts and path.is_file()
        }
        if not extensions:
            raise RuntimeError("The macOS bundle is missing the cryptography extension")
        for extension in extensions:
            require_static_macos_crypto(extension)
    entries = runtime_binaries(runtime_dir)
    if not entries and INCLUDE_ENGINES:
        raise RuntimeError(
            "No native runtime executables were collected; refusing to ship an app that "
            "would fail at the first task with RUNTIME_MISSING"
        )
    verify_runtime_binaries(resources, entries)
    for repaired in repair_expected_binaries(resources, runtime_dir):
        print(f"  · 补拷被 PyInstaller 漏掉的二进制：{repaired}")
    verify_expected_binaries(resources)
    executable = (
        app / "Contents/MacOS" / name
        if sys.platform == "darwin"
        else app / (name + (".exe" if sys.platform == "win32" else ""))
    )
    manifest = {
        "app": str(app),
        "executable": str(executable),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "version": version,
        "bundle_short_version": short_version,
        "bundle_version": short_version,
        "pyinstaller_version": importlib.metadata.version("pyinstaller"),
        "node_version": node_version,
        "runtime_dependencies": json.loads((SOURCE / "runtime_bridge/package.json").read_text())[
            "dependencies"
        ],
        "runtime_lock_sha256": hashlib.sha256(
            (runtime_dir / "package-lock.json").read_bytes()
        ).hexdigest(),
        "product_license": "Apache-2.0",
        "copyright": "2026 NoFox and contributors",
        "python_dependency_notice_inventory": inventory,
        "distribution_signed": False,
        "notarized": False,
        "automatic_updates": False,
        "macos_ui_element": sys.platform == "darwin",
    }
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


# ── 家目录两区约定（老板 2026-10-08 拍板 ✓ docs/2026-10-08-home-layout.md ✓）──
# 构建产物**只许**落开发区 ✓ 不再散落 /tmp（实测一次 2.2G ✗ 家族曾堆到 24G ✓）
DEV_DIRNAME = ".agent-mailbox-dev"


def write_build_info() -> dict:
    """写 `workbench_assets/build-info.json` ✓（AM-06：让**打包后**的 App 也能自证 revision/构建时间 ✓）。

    · 写进资源目录 ⇒ PyInstaller 收集资源时**自然带进 bundle** ✓ 无需改打包清单 ✓
    · 文件**不进仓** ✗（见 `.gitignore` ✓）：它描述的是"这一次构建"✗ 不是源码 ✓
    · 运行时 `build_info()` 优先读它 ✓ 读不到才退回 git ✓（开发态 ✓）
    """
    import datetime
    import hashlib
    import json as _json
    import pathlib as _pathlib
    import subprocess as _subprocess

    assets = (
        _pathlib.Path(__file__).resolve().parents[1] / "src" / "agent_mailbox" / "workbench_assets"
    )
    revision = "(unknown)"
    try:
        done = _subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=assets.parents[2],  # ← repo 根 ✓（parents[3] 是 tools 那一层 ✗ 取不到 git ✓）
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        revision = done.stdout.strip() or revision
    except (OSError, _subprocess.SubprocessError):
        pass
    dirty = "(unknown)"
    try:
        status = _subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=assets.parents[2],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        dirty = bool(status.stdout.strip())
    except (OSError, _subprocess.SubprocessError):
        pass
    payload = {
        "version": app_version(),
        "revision": revision,
        # **dirty** ✓：构建时工作树是否干净 —— 不记这个就会出现"标签落后一版"的含糊 ✗
        "dirty": dirty,
        "built_from": "working-tree" if dirty else "committed",
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "assets": {
            name.name: hashlib.sha256(name.read_bytes()).hexdigest()[:16]
            for name in sorted(assets.glob("*"))
            if name.suffix in {".js", ".css", ".html"}
        },
    }
    (assets / "build-info.json").write_text(
        _json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return payload


def app_version() -> str:
    """从源码读版本 ✓（唯一来源 ✓ 不写死 ✗）。"""
    import re as _re

    text = (SOURCE / "__init__.py").read_text(encoding="utf-8")  # SOURCE 已是 …/src/agent_mailbox ✓
    match = _re.search(r'^__version__\s*=\s*"([^"]+)"', text, _re.MULTILINE)
    if not match:
        raise RuntimeError("读不到 __version__ ✗（构建脚本需要它拼默认输出路径 ✓）")
    return match.group(1)


def dev_build_root(home: Path | None = None) -> Path:
    """构建根 ✓ = `~/.agent-mailbox-dev/build/`（两区约定 ✓ 不污染家目录 ✗）。"""
    return (home if home is not None else Path.home()) / DEV_DIRNAME / "build"


def default_output_dir(home: Path | None = None) -> Path:
    """默认输出 = `~/.agent-mailbox-dev/build/<版本>/` ✓（版本作子目录 ✓ 不写进活库名 ✗）。"""
    return dev_build_root(home) / app_version()


def purge_intermediates(output: Path) -> list[str]:
    """成功后清中间件 ✗（PyInstaller 的 build/ + cache/ ≈ 1.5G/次 ✓ 只留 dist/ ✓）。"""
    import shutil as _shutil

    removed: list[str] = []
    for name in ("build", "cache"):
        target = Path(output) / name
        if target.is_dir():
            _shutil.rmtree(target, ignore_errors=True)
            removed.append(name)
    return removed


def main() -> None:
    info = write_build_info()
    print(f"  · 构建溯源: {info['version']} @ {info['revision']} ({info['built_at']}) ✓")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--node-binary", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="缺省 = ~/.agent-mailbox-dev/build/<版本> ✓（两区约定）",
    )
    parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="保留 PyInstaller 中间件（默认成功后清掉 ✓）",
    )
    parser.add_argument("--name", default="Agent Mailbox")
    args = parser.parse_args()
    if args.output_dir is None:
        args.output_dir = default_output_dir()
        print(f"输出目录（两区约定默认 ✓）：{args.output_dir}")
    if (
        not args.name
        or Path(args.name).name != args.name
        or any(c in args.name for c in ("/", "\\"))
    ):
        parser.error("--name must be a single application name")
    manifest = build(args.runtime_dir, args.node_binary, args.output_dir, args.name)
    if not args.keep_intermediates:
        removed = purge_intermediates(args.output_dir)
        print("已清中间件 ✓：" + (", ".join(removed) if removed else "无 ✓"))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
