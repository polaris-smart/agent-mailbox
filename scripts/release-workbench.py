#!/usr/bin/env python3
"""Inspect an app or generate a signing plan. Never signs or uploads by itself."""

from __future__ import annotations

import argparse
import json
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

MACH_O = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


def identity_status() -> dict:
    result = subprocess.run(
        ["security", "find-identity", "-v", "-p", "codesigning"],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = result.stdout.splitlines()
    count = next((int(line.split()[0]) for line in lines if "valid identities found" in line), None)
    developer_id = any("Developer ID Application:" in line for line in lines)
    return {
        "valid_identity_count": count,
        "developer_id_application_present": developer_id,
        "team_id_configured": any(
            "Developer ID Application:" in line and "(" in line and ")" in line for line in lines
        ),
    }


def signature(path: Path) -> dict:
    display = subprocess.run(
        ["codesign", "--display", "--verbose=4", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    details = display.stderr
    return {
        "signed": display.returncode == 0,
        "developer_id": "Authority=Developer ID Application:" in details,
        "hardened_runtime": "runtime"
        in next((line for line in details.splitlines() if line.startswith("CodeDirectory ")), ""),
        "ad_hoc": "Signature=adhoc" in details,
    }


def native_files(app: Path) -> list[Path]:
    files = []
    for candidate in app.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        with candidate.open("rb") as stream:
            if stream.read(4) in MACH_O:
                files.append(candidate)
    return sorted(files, key=lambda p: (-len(p.parts), str(p)))


def inspect(app: Path) -> dict:
    result = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, check=False
    )
    return {
        "app": str(app),
        "signature": signature(app),
        "signature_verifies": result.returncode == 0,
        "native_file_count": len(native_files(app)),
        "identity_status": identity_status(),
        "notarization_checked": False,
        "uploads_performed": False,
    }


def signing_plan(
    app: Path, output: Path, identity: str, entitlements: Path, reviewed: bool
) -> dict:
    if not reviewed:
        raise ValueError(
            "Review third-party redistribution and re-signing terms before generating a signing plan"
        )
    if not identity.startswith("Developer ID Application:"):
        raise ValueError(
            "Supply an explicit Developer ID Application identity; ad-hoc signing is not distribution signing"
        )
    with entitlements.open("rb") as stream:
        grants = plistlib.load(stream)
    if not isinstance(grants, dict) or grants.get("com.apple.security.get-task-allow"):
        raise ValueError("Use reviewed release entitlements without get-task-allow")
    target = output / app.name
    if output == app or output.is_relative_to(app) or app.is_relative_to(output) or target.exists():
        raise ValueError("Choose a fresh output directory outside the source app")
    files = native_files(app)
    commands = [["mkdir", "-p", str(output)], ["ditto", str(app), str(target)]]
    preserved = []
    for source in files:
        relative = source.relative_to(app)
        # Anthropic packages are proprietary, and their published binary must
        # remain unmodified. A copied valid vendor signature is preserved.
        proprietary = any(part.startswith("claude-agent-sdk") for part in relative.parts)
        if proprietary:
            details = signature(source)
            checked = subprocess.run(
                ["codesign", "--verify", "--strict", str(source)], capture_output=True, check=False
            )
            if not details["developer_id"] or not details["hardened_runtime"] or checked.returncode:
                raise ValueError(
                    "A proprietary Claude binary lacks a valid hardened vendor signature; release needs a separate redistribution decision"
                )
            preserved.append(str(relative))
            continue
        commands.append(
            [
                "codesign",
                "--force",
                "--sign",
                identity,
                "--timestamp",
                "--options",
                "runtime",
                "--entitlements",
                str(entitlements),
                str(target / relative),
            ]
        )
    # Seal nested frameworks/bundles after their contents, then the outer app.
    bundles = sorted(
        (
            p
            for p in app.rglob("*")
            if not p.is_symlink()
            and p.is_dir()
            and p.suffix in {".framework", ".app", ".xpc", ".appex"}
        ),
        key=lambda p: (-len(p.parts), str(p)),
    )
    for bundle in [*bundles, app]:
        if any(part.startswith("claude-agent-sdk") for part in bundle.relative_to(app).parts):
            continue
        commands.append(
            [
                "codesign",
                "--force",
                "--sign",
                identity,
                "--timestamp",
                "--options",
                "runtime",
                "--entitlements",
                str(entitlements),
                str(target / bundle.relative_to(app)),
            ]
        )
    commands.append(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(target)])
    archive = output / (app.stem + "-notarization.zip")
    future = [
        ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(target), str(archive)],
        [
            "xcrun",
            "notarytool",
            "submit",
            str(archive),
            "--keychain-profile",
            "REPLACE_WITH_EXISTING_PROFILE",
            "--wait",
            "--timeout",
            "30m",
            "--output-format",
            "json",
        ],
        ["xcrun", "stapler", "staple", str(target)],
        ["xcrun", "stapler", "validate", str(target)],
        ["spctl", "--assess", "--type", "execute", "--verbose=2", str(target)],
        [
            "ditto",
            "-c",
            "-k",
            "--sequesterRsrc",
            "--keepParent",
            str(target),
            str(output / (app.stem + "-distribution.zip")),
        ],
    ]
    return {
        "source_app": str(app),
        "target_app": str(target),
        "vendor_binaries_preserved": preserved,
        "signing_commands": [shlex.join(c) for c in commands],
        "future_notarization_commands": [shlex.join(c) for c in future],
        "notarization_requires_separate_publish_authorization": True,
        "commands_executed": False,
        "notarization_proven": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "plan"))
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--identity")
    parser.add_argument("--entitlements", type=Path)
    parser.add_argument("--third-party-terms-reviewed", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Run macOS release checks on macOS")
    app = args.app.resolve()
    if app.suffix != ".app" or not (app / "Contents/Info.plist").is_file():
        parser.error("--app must identify an existing macOS application bundle")
    if args.action == "inspect":
        result = inspect(app)
    else:
        if not args.output_dir or not args.identity or not args.entitlements:
            parser.error("plan requires --output-dir, --identity and --entitlements")
        try:
            result = signing_plan(
                app,
                args.output_dir.resolve(),
                args.identity,
                args.entitlements.resolve(),
                args.third_party_terms_reviewed,
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
