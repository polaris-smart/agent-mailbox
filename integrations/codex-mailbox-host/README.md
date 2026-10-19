# Codex existing-thread receiver (development candidate)

This adapter gives mailbox a private notification connection to the **same**
app-server process used by Codex Desktop. It does not start a replacement chat,
resume an unloaded thread, switch accounts, or change model, permission, approval
or sandbox settings. Windows is not implemented by this candidate.

## Transport

Desktop supports a `CODEX_CLI_PATH` executable override. A launcher forwards
non-server commands to the original CLI unchanged. For the normal stdio
app-server command it preserves all arguments and environment, changes the
listen transport to an owner-private Unix WebSocket, and bridges Desktop's
JSONL stdin/stdout to that socket. App shutdown also terminates this backend and
removes its socket and manifest.

The original CLI is required as an absolute `AGENT_MAILBOX_CODEX_BINARY` path.
`AGENT_MAILBOX_CODEX_SOCKET_DIR` defaults to `~/.codex/mailbox-host`. The directory
must belong to the current user and have mode 0700. Sockets and manifests use
0600. No TCP endpoint is created. The launcher requires Node and `ws` 8.22.0.

`codex app-server proxy` is **not** a substitute for this bridge: the inspected
CLI proxies raw WebSocket traffic, not JSONL. Feeding it Desktop JSONL fails the
HTTP upgrade handshake.

## Admission

The hook command is:

```text
node /absolute/path/client.mjs MAILBOX_HOME SOCKET_DIRECTORY EMPLOYEE_ID DELIVERY_ID
```

It reads mailbox's outbox and configured binding. Exactly one live backend must
report that the bound original thread is already loaded. Admission uses
`turn/start` with an automated notification containing only project, employee,
message and delivery identifiers. It does not invoke `thread/start` or
`thread/resume`. The Agent must read the message through its own mailbox tools;
the notification grants no new authority.

An acknowledged turn ID produces a strict host receipt. That means **host
admission**, not Agent read, task acceptance, completion or Human acceptance.
An absent/ambiguous loaded thread or revoked binding is not received. A lost
response after admission is unknown and must not be silently retried. The
notification client never answers approval, tool or authentication requests.

## Native workbench endpoint (development)

An explicit `AGENT_MAILBOX_CODEX_MAILBOX_HOME` makes this same launcher expose
a second, owner-private Unix socket using mailbox's standard JSON-line protocol.
Its `notification_socket` path is recorded in the launcher manifest. The owner
can pin that path and the current binding in `wake/host-endpoints.json` with
`host_kind: codex`. The workbench sender then uses its Python standard-library
transport without a shell hook. No independent daemon or mail polling is added.
Both sockets close with the launcher; the notification endpoint is unique to
that launch. A new candidate supports an explicitly configured
`launcher_directory` in place of `socket_path` for Codex endpoints. The launcher
records its canonical `mailbox_home`; the sender discovers exactly one live,
owner-private `.mail.sock` matching that home without rewriting endpoint config.
Before offering a pending intent, the sender asks that receiver for a read-only
`agent-mailbox.receiver-health.v1` response for the **exact bound loaded thread**.
Only a positive, matching response counts as ready; a socket that merely accepts
connections, an older listener and a missing thread leave the intent pending.
The health request makes no `turn/start` call and carries no mail content.
Zero matches stay pending; ambiguity, unsafe paths and uncertain liveness fail
closed without falling back to shell hooks. Old manifests without `mailbox_home`
are not adopted. This requires the updated launcher and sender; it is not yet
installed or verified with a real Desktop restart. At most 16 manifests are
examined; excess entries require inspection, never automatic deletion.

The receiver matches the request against the actual configured home's outbox
and current binding, then asks only its own backend for the already-loaded
original thread. It durably records uncertainty before admission; duplicate or
concurrent requests and a receiver restart cannot blindly replay an uncertain
delivery. Successful results also write an exact late receipt for sender-side
reconciliation. The journal is per mailbox home and delivery ID, retained under
the private socket directory's `deliveries` subdirectory. Revocation is checked
before returning even a cached positive result. Do not erase that journal as a
retry mechanism.

The launcher, client, native receiver and shared `socket.mjs` must be installed
together with the pinned dependency. No actual Desktop restart, normal-icon
persistence, or production Codex activation is established by these source changes.

## Verification boundary

The current regression suite additionally includes `busy.test.mjs`: the actual
isolated CLI is held in a local fixed response while the standard Python Unix
sender submits a native notification twice. The original work remains active;
after release its output is retained and the notification occurs once in the
next request. This CLI consumes that input within the existing turn, so admission
does not imply a separate turn ID. No remote model or account is used. This is
protocol evidence, not an actual Desktop UI or real-model completion claim.

`crash.test.mjs` kills disposable Node receiver processes with SIGKILL after
synthetic admission starts and after a receipt is durable. A fresh process keeps
unknown admission blocked or returns the exact saved positive receipt; neither
case calls the synthetic backend again. This tests process-crash persistence,
not physical power loss, host crash recovery, or end-to-end task completion.

```sh
TEST_CODEX_BINARY=/absolute/path/to/codex TEST_MAILBOX_PYTHON=/absolute/path/to/python \
  node --test integrations/codex-mailbox-host/busy.test.mjs
node --test integrations/codex-mailbox-host/crash.test.mjs
```

Tested transport: bundled Codex CLI `0.162.0-alpha.17.2` and
`0.163.0-alpha.10.2` on macOS. The installer pins the wrapper to the exact
detected tested version; a later CLI version runs without the receiver until
revalidated. The receiver now requires an idle original thread for admission:
a busy thread keeps the mail pending, preserving its in-flight model request.
In an empty
isolated CODEX_HOME, a synthetic Desktop connection creates an empty ephemeral
thread and the second connection sees that exact loaded thread. No model is
called by this test. Both stdin closure and SIGTERM remove the endpoint and
terminate its backend.

```sh
node --test integrations/codex-mailbox-host/test.mjs
TEST_CODEX_BINARY=/absolute/path/to/codex node --test integrations/codex-mailbox-host/test.mjs
```

This does not establish real Desktop tool/approval routing, busy-turn behavior,
Agent read/reply, or automatic A-to-B-to-A delivery. Those require a normal
Desktop restart with the override, exact original-thread binding, and a real
mailbox round trip. Do not label this candidate production-ready before those
checks. Install no global shell/profile overrides; a scoped `open --env` launch
is sufficient. Quit and launch Desktop normally to roll back.

`routing.test.mjs` adds an actual-CLI protocol test with an empty HOME/CODEX_HOME
and a loopback-only deterministic Responses fixture (no model or account).
The second notification connection admits a turn and disconnects; the original
Desktop-shaped connection must still receive the resulting command approval.
The test never approves or executes that command. This verifies backend routing,
not the actual Desktop UI or its real plugin/tool execution.

```sh
TEST_CODEX_BINARY=/absolute/path/to/codex node --test integrations/codex-mailbox-host/routing.test.mjs
```

The scoped launch override does not persist when the ordinary Codex icon is used
on the next launch. It is a development activation method, not a completed
one-time end-user installation. Do not change a signed application or global
launch environment to conceal this limitation.

`native.test.mjs` covers durable duplicate, concurrent, unknown, revoked and
outbox-conflict cases. The native variant of `routing.test.mjs` uses the actual
Python sender and actual isolated Codex CLI with the loopback fixture; set
`TEST_MAILBOX_PYTHON` to a Python interpreter that can import this checkout
(`PYTHONPATH` is set by the test). It verifies sender-to-backend admission and
approval routing, not real Desktop UI or real-model mail processing.
