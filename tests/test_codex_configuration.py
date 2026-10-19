"""Receiver config projection: no secrets, cross-project reads, or optimistic defaults."""

import copy
import json
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from agent_mailbox import workbench_codex_connection as connection


@pytest.fixture
def result(tmp_path):
    return {
        "thread_id": "expected",
        "cwd": str(tmp_path),
        "config_digest": "a" * 64,
        "effective_servers": [{"name": "alias", "mailbox_like": True, "enabled": True}],
        "runtime_servers": [
            {"name": "other", "mailbox_like": True, "connected": False, "tools_available": False}
        ],
        "layers": [
            {
                "scope": "project",
                "disabled": True,
                "workspace_layer": True,
                "servers": [{"name": "old", "mailbox_like": True, "enabled": False}],
            }
        ],
    }


def test_projection_only_and_exact_project_thread_scope(result, tmp_path, monkeypatch):
    expected = copy.deepcopy(result)
    result["config"] = {"token": "PRIVATE"}
    result["effective_servers"][0]["args"] = ["PRIVATE"]
    result["layers"][0]["raw_config"] = {"model": "PRIVATE"}
    result["runtime_servers"][0]["description"] = "PRIVATE"
    calls = []

    def request(endpoint, operation, thread_id):
        calls.append((endpoint, operation, thread_id))
        return result

    monkeypatch.setattr(connection, "_control", request)
    actual = connection._configuration("socket", "expected", tmp_path)
    assert calls == [("socket", "configuration", "expected")]
    assert actual == {"state": "inspection_complete", **expected}
    assert "PRIVATE" not in repr(actual)


@pytest.mark.parametrize(
    "fault",
    [
        "thread",
        "cwd",
        "relative",
        "digest",
        "missing",
        "flag",
        "duplicate",
        "too_many",
        "layer_type",
        "layer_flag",
        "layer_scope",
        "too_many_layers",
        "runtime_flag",
    ],
)
def test_unknown_or_wrong_scope_is_not_no_conflict(result, tmp_path, monkeypatch, fault):
    if fault == "thread":
        result["thread_id"] = "someone-else"
    elif fault == "cwd":
        result["cwd"] = str(tmp_path.parent)
    elif fault == "relative":
        result["cwd"] = "relative"
    elif fault == "digest":
        result["config_digest"] = "g" * 64
    elif fault == "missing":
        del result["effective_servers"]
    elif fault == "flag":
        result["effective_servers"][0]["mailbox_like"] = 0
    elif fault == "duplicate":
        result["effective_servers"] *= 2
    elif fault == "too_many":
        result["effective_servers"] *= 65
    elif fault == "layer_type":
        result["layers"][0]["scope"] = {}
    elif fault == "layer_flag":
        result["layers"][0]["disabled"] = None
    elif fault == "layer_scope":
        result["layers"][0]["scope"] = "user"
    elif fault == "too_many_layers":
        result["layers"] *= 33
    elif fault == "runtime_flag":
        del result["runtime_servers"][0]["tools_available"]
    monkeypatch.setattr(connection, "_control", lambda *_: result)
    with pytest.raises(ValueError):
        connection._configuration("socket", "expected", tmp_path)


def test_no_thread_does_not_query_receiver(tmp_path, monkeypatch):
    monkeypatch.setattr(connection, "_control", lambda *_: pytest.fail("no query without thread"))
    with pytest.raises(ValueError, match="session_required"):
        connection._configuration("socket", None, tmp_path)


@pytest.mark.parametrize("digest", [None, "a" * 64, "z" * 64, {}, "a" * 65])
def test_fixed_server_digest_projection_validates_shape(result, tmp_path, monkeypatch, digest):
    result["effective_servers"][0].update(name="agent-mailbox-project", entry_digest=digest)
    monkeypatch.setattr(connection, "_control", lambda *_: result)
    if digest is None or digest == "a" * 64:
        assert (
            connection._configuration("socket", "expected", tmp_path)["effective_servers"][0][
                "entry_digest"
            ]
            == digest
        )
    else:
        with pytest.raises(ValueError, match="invalid_receiver_configuration"):
            connection._configuration("socket", "expected", tmp_path)


def test_configuration_over_real_private_socket(result, tmp_path):
    with tempfile.TemporaryDirectory(prefix="am-config-control-", dir="/private/tmp") as directory:
        endpoint = str(Path(directory) / "receiver.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(endpoint)
        listener.listen(1)
        errors = []

        def serve():
            try:
                stream, _ = listener.accept()
                with stream:
                    data = b""
                    while b"\n" not in data:
                        data += stream.recv(8192)
                    request = json.loads(data)
                    assert request["operation"] == "configuration"
                    assert request["thread_id"] == "expected" and "cwd" not in request
                    stream.sendall(
                        json.dumps(
                            {
                                **result,
                                "protocol": request["protocol"],
                                "request_id": request["request_id"],
                            }
                        ).encode()
                        + b"\n"
                    )
            except (OSError, ValueError, AssertionError, KeyError) as exc:
                errors.append(exc)
            finally:
                listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            assert (
                connection._configuration(endpoint, "expected", tmp_path)["state"]
                == "inspection_complete"
            )
        finally:
            thread.join(3)
        assert not thread.is_alive() and errors == []
