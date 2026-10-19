"""Real private Node children; synthetic SDK/config, no host or model started."""

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from agent_mailbox import workbench_dsh_process as process

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix DSH preparation adapter")
APP_NODE = Path("/Applications/Agent Mailbox.app/Contents/Frameworks/runtime/bin/node")
NODE = Path(
    os.environ.get("MAILBOX_TEST_NODE")
    or shutil.which("node")
    or (str(APP_NODE) if APP_NODE.exists() else "/unavailable-node")
)


@pytest.fixture
def case(tmp_path):
    if not NODE.is_file():
        pytest.skip("requires an explicitly available Node executable")
    root = tmp_path.resolve()
    # Hosted tool caches are not a trusted execution root for the adapter.
    # Copy the synthetic test runtime into the private fixture directory.
    node = root / "node"
    shutil.copy2(NODE, node)
    node.chmod(0o700)
    node_sha = hashlib.sha256(node.read_bytes()).hexdigest()
    profile = root / "profile"
    profile.mkdir(mode=0o700)
    source = root / "source"
    source.mkdir(mode=0o700)
    sdk = root / "sdk"
    sdk.mkdir(mode=0o700)
    for name in process.FILES:
        (source / name).write_text("// fixture\n")
    for name in process.REQUIRED_SDK:
        p = sdk / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n")
    expected_sdk = {
        n: hashlib.sha256((sdk / n).read_bytes()).hexdigest() for n in process.REQUIRED_SDK
    }
    request = {
        "version": 1,
        "operation": "status",
        "sdk": str(sdk),
        "context": {"dir": str(profile)},
        "plan_id": "plan_" + "0" * 32,
    }

    def invoke(code, *, timeout=2, mutate=None, value=None):
        (source / "prepare-bridge.mjs").write_text(code)
        pins = {n: hashlib.sha256((source / n).read_bytes()).hexdigest() for n in process.FILES}
        if mutate:
            mutate(source, sdk, pins)
        return process.run(
            node,
            profile,
            value or request,
            source=source,
            expected_node=node_sha,
            expected_files=pins,
            expected_sdk=expected_sdk,
            timeout=timeout,
        )

    return root, profile, source, sdk, request, invoke


RESULT = {
    "plan_id": "plan_" + "0" * 32,
    "state": "preview",
    "requires_confirmation": False,
    "will_activate": False,
    "session_verified": False,
    "delivery_verified": False,
}


def success(result=None):
    return (
        "console.log(JSON.stringify("
        + json.dumps({"version": 1, "ok": True, "result": result or RESULT})
        + "));\n"
    )


@pytest.mark.parametrize("temporary_alias", [False, True])
def test_child_isolated_from_node_loaders_agent_environment_and_real_home(
    case, monkeypatch, temporary_alias
):
    if temporary_alias:
        real = case[0] / "temporary"
        real.mkdir(mode=0o700)
        alias = case[0] / "temp-alias"
        alias.symlink_to(real)
        monkeypatch.setattr(process.tempfile, "tempdir", str(alias))
    for key in ["NODE_OPTIONS", "CODEX_SANDBOX", "HTTPS_PROXY", "OPENAI_API_KEY", "DSH_HOME"]:
        monkeypatch.setenv(key, "PRIVATE")
    code = """import assert from 'node:assert/strict';
import fs from 'node:fs';
// CoreFoundation may add its own text-encoding variable inside Node on macOS.
assert.deepEqual(Object.keys(process.env).filter(k=>k!=='__CF_USER_TEXT_ENCODING').sort(), ['HOME','LANG','PATH','TMPDIR'].sort());
assert.equal(process.cwd(), process.env.HOME);
assert.equal(process.cwd(), process.env.TMPDIR);
assert.equal(JSON.parse(fs.readFileSync(0,'utf8')).operation,'status');
"""
    assert case[5](code + success()) == RESULT


@pytest.mark.parametrize(
    "code",
    [
        "console.log('PRIVATE config traceback');",
        "console.log('x'.repeat(70000));",
        "setTimeout(()=>{},10000);",
        "console.log(JSON.stringify({version:1,ok:'true'}));",
        success({**RESULT, "secret": "PRIVATE"}),
        success({**RESULT, "session_verified": True}),
        success({**RESULT, "will_activate": True}),
        success({**RESULT, "state": "enabled"}),
        success({**RESULT, "state": ["preview"]}),
        success({**RESULT, "requires_confirmation": "false"}),
        success({**RESULT, "plan_id": "plan_" + "1" * 32}),
    ],
)
def test_invalid_output_and_timeout_are_redacted_unknown(case, code):
    before = time.monotonic()
    with pytest.raises(ValueError, match="^dsh_preparation_result_unknown$"):
        case[5](code, timeout=0.2)
    assert time.monotonic() - before < 3


def test_failure_cannot_claim_success(case):
    with pytest.raises(ValueError, match="^dsh_preparation_refused_or_incomplete$"):
        case[5](success() + "process.exitCode=2;")


@pytest.mark.parametrize(
    "mutation", ["sdk_edit", "sdk_extra", "sdk_link", "source_edit", "source_link", "missing_pin"]
)
def test_changed_or_redirected_dependency_never_runs_child(case, mutation):
    marker = case[0] / "ran"

    def alter(source, sdk, pins):
        p = sdk / "js-yaml/index.js"
        if mutation == "sdk_edit":
            p.write_text("CHANGED")
        elif mutation == "sdk_extra":
            (sdk / "unexpected.js").write_text("EXTRA")
        elif mutation == "sdk_link":
            p.rename(sdk / "other")
            p.symlink_to(sdk / "other")
        elif mutation == "source_edit":
            (source / "setup.mjs").write_text("CHANGED")
        elif mutation == "source_link":
            (source / "setup.mjs").rename(source / "other")
            (source / "setup.mjs").symlink_to(source / "other")
        else:
            pins.pop("patch-plan.mjs")

    with pytest.raises(ValueError, match="dsh_(component|sdk)"):
        case[5](
            f"import fs from 'node:fs';fs.writeFileSync({json.dumps(str(marker))},'ran');\n"
            + success(),
            mutate=alter,
        )
    assert not marker.exists()


def test_profile_mismatch_and_wide_profile_are_refused(case):
    with pytest.raises(ValueError, match="dsh_profile_mismatch"):
        case[5](success(), value={**case[4], "context": {"dir": "/private/tmp"}})
    case[1].chmod(0o777)
    with pytest.raises(ValueError, match="dsh_profile_private_required"):
        case[5](success())


def test_sdk_manifest_requires_canonical_names_and_complete_closure(case):
    sdk = case[3]
    pins = {n: hashlib.sha256((sdk / n).read_bytes()).hexdigest() for n in process.REQUIRED_SDK}
    for extra in ["../escape", "/private/tmp/escape", "a/../b", "a//b"]:
        with pytest.raises(ValueError, match="dsh_sdk_pin_required"):
            process.verify_sdk(sdk, {**pins, extra: "0" * 64})
    with pytest.raises(ValueError, match="dsh_sdk_pin_required"):
        process.verify_sdk(sdk, {})


def test_timed_out_child_group_does_not_leave_descendant(case):
    marker = case[0] / "late"
    inner = f"setTimeout(()=>require('fs').writeFileSync({json.dumps(str(marker))},'late'),1000)"
    code = f"import {{spawn}} from 'node:child_process';spawn(process.execPath,['-e',{json.dumps(inner)}],{{stdio:['ignore',1,2]}});"
    with pytest.raises(ValueError, match="dsh_preparation_result_unknown"):
        case[5](code, timeout=0.2)
    time.sleep(1.1)
    assert not marker.exists()
