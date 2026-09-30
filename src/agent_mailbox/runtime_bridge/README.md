# Managed ACP runtime bridge

This directory ships with the Python wheel. Python owns project/employee admission,
tokens, UI approvals and task records. The Node process owns only managed ACP
sessions and execution. It never configures a user's MCP clients, logs in an agent,
starts a system daemon, or invokes `npx`.

## Install once into an explicit dependency directory

Requires Node.js >=22.13 and npm. Installation is a deliberate operation, separate
from run admission. `npm ci --ignore-scripts` uses the shipped integrity lockfile:

```sh
python -m agent_mailbox.runtime_bridge.install_runtime \
  --destination /absolute/mail-home/workbench/runtime/deps
```

The installer copies only `package.json` and `package-lock.json`, installs local
`node_modules`, and returns JSON with `runtime_dir`, `node_binary`, `node_version`
and `acpx_version`. It does not install global packages or move credentials.
The callable is `install_runtime(destination: Path, node_binary=None) -> dict`.

Dependencies are pinned to MIT `acpx@0.19.3`, Apache-2.0
`@agentclientprotocol/codex-acp@2.0.0`, and Apache-2.0
`@agentclientprotocol/claude-agent-acp@0.84.0`. Preserve their license notices when
redistributing the installed runtime. Adapter dependencies have their own licenses.

## Python launch and protocol

```python
from pathlib import Path
import json
import subprocess

from agent_mailbox.runtime_bridge import bridge_path

child = subprocess.Popen(
    [
        "/absolute/path/to/node",
        str(bridge_path()),
        "--state-dir",
        "/absolute/mail-home/workbench/runtime/sessions",
        "--project-root",
        "/absolute/project",
        "--runtime-dir",
        "/absolute/mail-home/workbench/runtime/deps",
    ],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
)
child.stdin.write(
    json.dumps(
        {
            "op": "run",
            "run_id": "unique-run-id",
            "session_id": "employee-project-id",
            "agent": "codex",
            "prompt": "Read the project brief and report next steps.",
            "cwd": "/absolute/project",
            "sandbox": "read-only",
            "timeout_ms": 600000,
            "mcp_servers": [
                {
                    "name": "project",
                    "command": "/absolute/path/to/python",
                    "args": ["-m", "agent_mailbox.workspace_mcp"],
                    "env": [{"name": "PROJECT_TOKEN", "value": "issued-by-parent"}],
                }
            ],
        }
    )
    + "\n"
)
child.stdin.flush()
for line in child.stdout:
    event = json.loads(line)
    # Consume every event; handle other runs concurrently in the application.
    if event["type"] == "result":
        break
child.stdin.write('{"op":"shutdown"}\n')
child.stdin.flush()
child.wait(timeout=15)
```

The parent should drain stderr concurrently. Diagnostics are intentionally generic;
actionable errors are protocol results. Supply the discovered Codex executable as
child environment `CODEX_PATH` to reuse the installed binary and existing native
authentication. Without it the installed adapter uses its bundled compatible Codex.
The Claude adapter uses the native SDK's user/project settings and authentication.
Credentials stay local. No login occurs in this bridge.

Every stdout line has `protocol: 1`, `run_id`, `session_id` and `type`:

| Type | Payload |
| --- | --- |
| `session` | `backend_session_id`, `acpx_record_id`, `native_mode`, and successfully applied optional `model` / `reasoning_effort` |
| `started` | The underlying ACP transport accepted the prompt, after queue waiting |
| `event` | `event`: the complete structured ACPX event, including output/thought/tool updates |
| `permission_required` | `request_id`, `request` (full ACP request), `options`, `tool_call`, `inferred_kind`, `timeout_ms` |
| `result` | Exactly one terminal per admitted run: `status`, complete `output_text`, `runtime_result` and/or `error` |
| `control_error` | Invalid/duplicate operation or expired permission reply; does not create a second terminal |

`output_text` concatenates output text deltas, preserves Unicode and empty output,
and excludes thought text. `status` is `completed`, `failed` or `cancelled` from the
actual ACP turn result, except that explicit provider failure events or JSON error
envelopes fail the run even when an adapter incorrectly reports completion. Only
standalone JSON with `type: "error"`, HTTP error status and typed error/message is
recognized; prose and fenced examples are not treated as failure envelopes.
The bridge does not decide whether the user's task was
semantically satisfied. An empty completed output remains empty. Partial output
accompanies failures and cancellation.

Operations after `run`:

```json
{"op":"permission","run_id":"unique-run-id","request_id":"from-event","decision":"allow_once"}
{"op":"permission","run_id":"unique-run-id","request_id":"from-event","decision":"deny"}
{"op":"cancel","run_id":"unique-run-id"}
{"op":"shutdown"}
```

Permissions default to deny-all. A request waits for a parent reply, then defaults
to one-time denial after 60 seconds. Configure `--permission-timeout-ms` or per-run
`permission_timeout_ms`. UUID request IDs prevent reply confusion. Cancellation,
completion and timeout revoke pending decisions; a late allow cannot authorize a
new action. No approve-all or allow-always mode is exposed.

The initialization deadline defaults to 15 seconds (`--startup-timeout-ms`), while
`timeout_ms` applies to a turn. Shutdown/EOF cancels owned work and stops agent
connections, preserving durable sessions. Reusing a run ID is rejected.

## Session restoration and boundaries

`session_id` is a mailbox managed identity, not a provider thread ID. ACPX records
and identity bindings live exclusively under `--state-dir`. `resume_session_id`
selects an existing provider session; restart also restores the saved provider ID
automatically. Unsupported/missing/corrupt sessions produce a failure, never a
silent new conversation. Authentication failures do not start browser login.

One bridge is bound to one `--project-root`. `cwd` must resolve to that directory or
a child directory; symlinks are resolved before validation. Agent and cwd
are bound to a managed identity. Native mode controls and turns are admitted in
one per-session queue, so an explicit workspace-write choice cannot change an
already running turn's permissions. MCP descriptors are supplied to session creation
and reconnection, never written to global config or session records. A live
connection retains its initial MCP configuration; changed live descriptors require
an explicit reconnect. Duplicate MCP names and unsupported transports are rejected.

Codex's native mode defaults to `read-only`; `workspace-write` must be supplied
explicitly by the authorized parent. Both are configured before prompt dispatch.
Claude's default maps to native `plan` mode. That is a permission mode, **not an OS
filesystem sandbox**; this bridge rejects Claude `workspace-write` rather than
claiming it can constrain all native SDK writes to a project. Native agents may
read user/project configuration and need the user's native credentials. ACP client
filesystem and terminal callbacks are disabled. A validated cwd is a routing
boundary, not an OS sandbox. Explicit human approval may allow an individual
action under the native agent's permission mechanism.

Failures include `RUNTIME_MISSING`, `AGENT_UNAVAILABLE`, `AUTH_REQUIRED`, `TIMEOUT`,
`SESSION_RESUME_FAILED`, `SESSION_CONTEXT_MISMATCH`, `PROJECT_BOUNDARY`,
`SANDBOX_UNSUPPORTED`, `MODEL_UNSUPPORTED`, `CONFIG_UNSUPPORTED`, `PROVIDER_ERROR`,
`INVALID_REQUEST` and `AGENT_FAILED`.

An optional run `model` selects an explicit ACP model before dispatch. Optional
`reasoning_effort` requires the adapter to advertise that configuration key and
accept the requested value. Unsupported choices fail before the prompt; the bridge
never substitutes another model or changes user configuration. Omitted settings
inherit the native adapter's current defaults, which may differ from desktop app
defaults. The `session` event records the applied values so consumers can report
whether the requested profile actually took effect.

Only Codex and Claude managed adapters are accepted. Custom GUI apps, general
agent shells and remote devices require their own supported adapters/node layer;
they are not represented as successfully connected by this component.

## Isolated process-level tests

```sh
npm install --prefix /tmp/mailbox-acpx-tests --ignore-scripts --no-audit --no-fund acpx@0.19.3
AGENT_MAILBOX_TEST_RUNTIME_DIR=/tmp/mailbox-acpx-tests uv run pytest tests/test_runtime_bridge.py
```

Tests start the real Node bridge, real pinned ACPX, and a separate fake JSON-RPC
agent process. They do not call a model. Trusted test launch arguments
`--test-mode 1 --test-agent-command-json '["/path/to/python","fake.py",...]'`
override the adapter solely for process fixtures. Never expose these arguments
through HTTP or user task input. Tests verify dynamic MCP injection, native mode,
permissions/timeouts, cancellation/late replies, queue isolation, full and empty
results, provider errors, process exits, restoration, and project boundaries.
