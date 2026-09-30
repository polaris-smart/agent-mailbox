"""Optional symbol retrieval never hands a CLI the source tree or live index."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from agent_mailbox import workbench_knowledge as knowledge
from agent_mailbox.workbench_store import WorkbenchError


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "example.py").write_text("class Example: pass\n")
    directory = root / ".codegraph"
    directory.mkdir()
    with sqlite3.connect(directory / "codegraph.db") as db:
        db.executescript(
            "CREATE TABLE nodes(id TEXT); CREATE TABLE files(path TEXT); "
            "CREATE TABLE project_metadata(key TEXT, value TEXT);"
        )
        db.execute("INSERT INTO project_metadata VALUES ('index_state', 'complete')")
    return root


@pytest.fixture
def cli(tmp_path, monkeypatch):
    path = tmp_path / "fake-codegraph"
    log = tmp_path / "calls.jsonl"

    def install(body=""):
        path.write_text(
            f"#!{sys.executable}\n"
            + f"""
import json, os, pathlib, sys, time
args = sys.argv[1:]
with open({str(log)!r}, 'a') as out:
    out.write(json.dumps({{'args': args, 'env': sorted(os.environ)}}) + '\\n')
if args == ['--version']:
    print('1.6.0')
    sys.exit(0)
{body}
assert args[0] == 'query'
assert args[-2] == '--'
root = pathlib.Path(args[args.index('--path') + 1])
assert list(root.iterdir()) == [root / '.codegraph']
assert (root / '.codegraph' / 'codegraph.db').is_file()
assert not (root / 'src').exists()
assert not (root / '.git').exists()
print(json.dumps([{{'node': {{'name': 'Example', 'kind': 'class', 'filePath': 'src/example.py', 'startLine': 1,
                              'signature': 'secret literal must not return'}}}}]))
"""
        )
        path.chmod(0o700)
        monkeypatch.setenv("AGENT_MAIL_CODEGRAPH_BIN", str(path))
        return path, log

    return install


def test_status_missing_tool(project, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_CODEGRAPH_BIN", "/nonexistent/codegraph")
    result = knowledge.knowledge_status(project)
    assert result["available"] is False
    assert result["error_code"] == "KNOWLEDGE_TOOL_UNAVAILABLE"
    assert result["freshness"] == "unknown"
    with pytest.raises(WorkbenchError, match="unavailable") as exc:
        knowledge.knowledge_query(project, "Example")
    assert exc.value.code == "KNOWLEDGE_TOOL_UNAVAILABLE"


@pytest.mark.parametrize("value", ["relative/bin", "", "/nonexistent/tool"])
def test_explicit_invalid_binary_does_not_fall_back(project, monkeypatch, value):
    monkeypatch.setenv("AGENT_MAIL_CODEGRAPH_BIN", value)
    assert knowledge.knowledge_status(project)["available"] is False


def test_paths_and_index(project, cli, tmp_path):
    cli()
    with pytest.raises(WorkbenchError) as exc:
        knowledge.knowledge_status(tmp_path / "missing")
    assert exc.value.code == "KNOWLEDGE_INVALID_PROJECT"
    (project / ".codegraph" / "codegraph.db").unlink()
    assert knowledge.knowledge_status(project)["error_code"] == "KNOWLEDGE_INDEX_REQUIRED"
    with pytest.raises(WorkbenchError) as exc:
        knowledge.knowledge_query(project, "Example")
    assert exc.value.code == "KNOWLEDGE_INDEX_REQUIRED"


@pytest.mark.parametrize(
    "query",
    [
        "",
        "   ",
        "-p /etc",
        "--help",
        "a" * 501,
        "x\x00y",
        "../x",
        "/etc/passwd",
        "file:secret",
        "src/example.py",
        "x\ny",
        None,
    ],
)
def test_query_rejects_options_paths_and_controls(project, query):
    with pytest.raises(WorkbenchError) as exc:
        knowledge.knowledge_query(project, query)
    assert exc.value.code == "KNOWLEDGE_INVALID_QUERY"


def test_snapshot_literal_args_cleanup_no_credentials(project, cli, monkeypatch):
    _, log = cli()
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-forward")
    database = project / ".codegraph" / "codegraph.db"
    before = database.read_bytes(), database.stat().st_mtime_ns
    result = knowledge.knowledge_query(project, "Example --version")
    assert result["mode"] == "symbols_snapshot"
    assert result["project_path"] == str(project.resolve())
    assert result["symbols"] == [
        {"file_path": "src/example.py", "name": "Example", "kind": "class", "start_line": 1}
    ]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    query = calls[-1]
    assert query["args"][-2:] == ["--", "Example --version"]
    snapshot = Path(query["args"][query["args"].index("--path") + 1])
    assert snapshot != project
    assert not snapshot.exists()
    assert "OPENAI_API_KEY" not in query["env"]
    assert before == (database.read_bytes(), database.stat().st_mtime_ns)
    assert sorted(path.name for path in database.parent.iterdir()) == ["codegraph.db"]


def test_index_busy_and_invalid(project, cli):
    cli()
    wal = project / ".codegraph" / "codegraph.db-wal"
    wal.write_bytes(b"uncheckpointed")
    assert knowledge.knowledge_status(project)["error_code"] == "KNOWLEDGE_INDEX_BUSY"
    with pytest.raises(WorkbenchError) as exc:
        knowledge.knowledge_query(project, "Example")
    assert exc.value.code == "KNOWLEDGE_INDEX_BUSY"
    wal.unlink()
    (project / ".codegraph" / "codegraph.db").write_bytes(b"invalid")
    assert knowledge.knowledge_status(project)["error_code"] == "KNOWLEDGE_INDEX_REQUIRED"


def test_index_symlink_is_rejected(project, cli, tmp_path):
    cli()
    db = project / ".codegraph" / "codegraph.db"
    outside = tmp_path / "outside.db"
    db.rename(outside)
    db.symlink_to(outside)
    assert knowledge.knowledge_status(project)["error_code"] == "KNOWLEDGE_INDEX_REQUIRED"


@pytest.mark.parametrize(
    "body, code",
    [
        ("sys.stderr.write('SECRET');sys.exit(7)", "KNOWLEDGE_COMMAND_FAILED"),
        ("print('x' * 200000);sys.exit(0)", "KNOWLEDGE_OUTPUT_LIMIT"),
        ("sys.stderr.write('x' * 200000);sys.exit(0)", "KNOWLEDGE_OUTPUT_LIMIT"),
        ("print('not JSON');sys.exit(0)", "KNOWLEDGE_INVALID_OUTPUT"),
        ("time.sleep(3);sys.exit(0)", "KNOWLEDGE_TIMEOUT"),
    ],
)
def test_failures_are_bounded_and_sanitized(project, cli, monkeypatch, body, code):
    cli(body)
    monkeypatch.setattr(knowledge, "QUERY_TIMEOUT", 0.1)
    # Default _run's argument is bound at definition; patch only query runtime.
    original = knowledge._run

    def run(args, cwd, timeout=None):
        return original(args, cwd, 0.1 if timeout is None else timeout)

    monkeypatch.setattr(knowledge, "_run", run)
    with pytest.raises(WorkbenchError) as exc:
        knowledge.knowledge_query(project, "Example")
    assert exc.value.code == code
    assert "SECRET" not in str(exc.value)


def test_cross_project_paths_are_filtered(project, cli, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("private")
    (project / "link.py").symlink_to(outside)
    paths = ["/etc/passwd", "../outside.py", "link.py", "src/example.py"]
    body = f"print(json.dumps([{{'node': {{'filePath': p, 'name': 'X'}}}} for p in {paths!r}]));sys.exit(0)"
    cli(body)
    result = knowledge.knowledge_query(project, "Example")
    assert result["symbols"] == [{"file_path": "src/example.py", "name": "X"}]


def test_git_failure_is_unknown(project, cli):
    cli()
    status = knowledge.knowledge_status(project)
    assert status["revision"] is None
    assert status["working_tree_dirty"] is None
