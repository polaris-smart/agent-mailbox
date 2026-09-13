"""B5 cleanup --dry-run: scan lists suspected test residue, deletes nothing
without --yes + confirmation."""

import json
import os
import subprocess
import sys

from agent_mailbox.cleanup import scan

ENV = {**os.environ, "PYTHONUTF8": "1"}


def _make_root(tmp_path):
    root = tmp_path / "mail"
    inbox = root / "inbox"
    (inbox / "HS").mkdir(parents=True)
    (inbox / "GHOST").mkdir()               # unregistered agent dir
    (inbox / "WBTEST").mkdir()              # unregistered, test-named
    (inbox / "HS" / "m1.json").write_text(json.dumps({"id": "m1"}), encoding="utf-8")
    (inbox / "WBTEST" / "m2.json").write_text(json.dumps({"id": "m2"}), encoding="utf-8")
    (inbox / "stray.json").write_text("{}", encoding="utf-8")  # orphan letter
    (root / "registry.json").write_text(json.dumps({"agents": {"HS": {}}}), encoding="utf-8")
    return root


def _run_cli(root, *args, stdin=""):
    return subprocess.run(
        [sys.executable, "-m", "agent_mailbox.cleanup", "--root", str(root), *args],
        capture_output=True, text=True, env=ENV, input=stdin, timeout=30,
        check=False,
    )


# ------------------------------------------------------------------- scan()

def test_scan_lists_residue(tmp_path):
    root = _make_root(tmp_path)
    paths = {f["path"] for f in scan(root)}
    assert "inbox/GHOST" in paths
    assert "inbox/WBTEST" in paths
    assert "inbox/stray.json" in paths
    assert "inbox/HS" not in paths  # registered agent untouched


def test_scan_clean_root(tmp_path):
    root = tmp_path / "mail"
    (root / "inbox" / "HS").mkdir(parents=True)
    (root / "inbox" / "HS" / "m1.json").write_text(json.dumps({"id": "m1"}), encoding="utf-8")
    (root / "registry.json").write_text(json.dumps({"agents": {"HS": {}}}), encoding="utf-8")
    assert scan(root) == []


def test_scan_test_named_registered_is_review_only(tmp_path):
    root = tmp_path / "mail"
    (root / "inbox" / "TESTDEMO").mkdir(parents=True)
    (root / "registry.json").write_text(
        json.dumps({"agents": {"TESTDEMO": {}}}), encoding="utf-8"
    )
    findings = scan(root)
    assert len(findings) == 1
    assert findings[0]["action"].startswith("review only")


def test_scan_corrupt_registry_aborts(tmp_path):
    root = tmp_path / "mail"
    root.mkdir()
    (root / "registry.json").write_text("{not json", encoding="utf-8")
    try:
        scan(root)
        raised = False
    except SystemExit:
        raised = True
    assert raised


# --------------------------------------------------------------------- CLI

def test_cli_dry_run_lists_and_deletes_nothing(tmp_path):
    root = _make_root(tmp_path)
    r = _run_cli(root, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "inbox/GHOST" in r.stdout
    assert "inbox/WBTEST" in r.stdout
    assert "inbox/stray.json" in r.stdout
    assert "nothing deleted" in r.stdout
    # zero deletion
    assert (root / "inbox" / "GHOST").exists()
    assert (root / "inbox" / "WBTEST").exists()
    assert (root / "inbox" / "stray.json").exists()


def test_cli_default_behaviour_is_dry_run(tmp_path):
    root = _make_root(tmp_path)
    r = _run_cli(root)
    assert r.returncode == 0, r.stderr
    assert "inbox/GHOST" in r.stdout and "nothing deleted" in r.stdout
    assert (root / "inbox" / "GHOST").exists()


def test_cli_yes_without_confirmation_aborts(tmp_path):
    root = _make_root(tmp_path)
    r = _run_cli(root, "--yes", stdin="no\n")
    assert r.returncode == 0, r.stderr
    assert "aborted" in r.stdout
    assert (root / "inbox" / "GHOST").exists()  # nothing deleted


def test_cli_yes_with_confirmation_deletes(tmp_path):
    root = _make_root(tmp_path)
    r = _run_cli(root, "--yes", stdin="yes\n")
    assert r.returncode == 0, r.stderr
    assert "deleted" in r.stdout
    assert not (root / "inbox" / "GHOST").exists()
    assert not (root / "inbox" / "WBTEST").exists()
    assert not (root / "inbox" / "stray.json").exists()
    assert (root / "inbox" / "HS" / "m1.json").exists()  # registered agent kept


def test_cli_clean_root_reports_nothing(tmp_path):
    root = tmp_path / "mail"
    (root / "inbox" / "HS").mkdir(parents=True)
    (root / "registry.json").write_text(json.dumps({"agents": {"HS": {}}}), encoding="utf-8")
    r = _run_cli(root, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "no suspected residue" in r.stdout


def test_cli_missing_root_fails(tmp_path):
    r = _run_cli(tmp_path / "nope", "--dry-run")
    assert r.returncode == 1
