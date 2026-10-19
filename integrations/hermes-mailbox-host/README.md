# Hermes Gateway receiver candidate

This standalone native Hermes plugin targets an **explicitly bound existing Gateway
conversation**. It does not create a webhook conversation, change the Human's chat
destination, launch another Agent, use Desktop authentication, or invoke a model itself.

Status: development candidate, not a completed real-Agent relay. A prior candidate
has received an exact original-session turn receipt in a live Gateway. The current
source adds version-bounded admission protection and a health diagnostic; neither
has been verified in a newly installed live receiver.
Production event-driven delivery and original-session replies remain separate gates.

## Receipt boundary

1. The owner binds a project/employee to an exact existing Gateway `session_key` in
   mailbox's `wake/host-bindings.json`, with `host_kind: hermes`.
2. The plugin additionally pins that key to the current underlying `session_id` and
   platform. It refuses CLI/TUI routing and requires Hermes's explicit injection grant.
3. The plugin verifies the installed SDK and constructs a per-request `PinnedGateway`
   adapter. It durably records the delivery identity and a random marker **before**
   scheduling through the official dispatch implementation. Adapter admission
   checks the exact pinned session ID, current binding, grant and lifecycle again.
   It never falls back to the public unpinned `ctx.inject_message` API. A `True`
   scheduling result is not a receipt.
4. Only a `pre_llm_call` observation containing that marker in the exact session and
   platform produces `received`. The hook ignores history/model/provider metadata.
   Receipt means the notification reached turn preparation, not a successful model
   response, reading the indicated mail, task acceptance, or Human acceptance.

Duplicates do not inject again, including after restart. Changed content under the
same delivery ID is rejected. Revocation prevents a later positive receipt.
After injection, errors/timeouts are **unknown**, not `not_received`. The shared
mailbox transport rejects unknown status and preserves its uncertain-delivery guard.
If a busy session starts after the six-second response window, the plugin atomically
writes a late receipt. The candidate mailbox recovery pass reconciles the unknown
claim only with a valid receipt matching the current binding, session, employee and
exact message batch. It does not call the host again. Without such evidence, unknown
stays blocked. This is recovery of receipt state; it does not prove the Gateway will
eventually start every queued event, and recovery is not necessarily instantaneous.
Session reset/compression changing the underlying session ID fails closed until rebound.
These checks protect admission and exact receipt matching, not every later step of
the host's turn. In particular, legal compression continuation and revocation after
admission still require separate execution-path validation. Do not enable this
candidate on the strength of isolated admission tests alone.

## Integration settings (synthetic example only)

Install as one Hermes native plugin directory with `plugin.yaml`, `__init__.py`
and `pinned.py`, plus the bundled `live.py` diagnostic module.
Enable via Hermes's supported plugin management. Merge settings rather than replacing
the user's configuration. All paths must be absolute and owner-private; socket/state
paths must not traverse symlinks. Unix transport only; no Windows receiver claim.

```yaml
plugins:
  entries:
    agent-mailbox-host:
      allow_gateway_injection: true
      settings:
        home: /absolute/mailbox-home
        state_dir: /absolute/private-plugin-state
        socket_path: /absolute/private-ipc/hermes.sock
        sessions:
          "agent:main:feishu:dm:SYNTHETIC":
            session_id: SYNTHETIC_EXISTING_SESSION
            platform: feishu
```

The plugin waits for Gateway publication, then opens a private local Unix socket;
it never polls mail or uses a timer to call a model. Unload closes its own socket.
A lifetime OS file lock prevents competing receivers from replacing each other.
After a crash, restart recovers an owner-private stale socket only if a bounded
connection probe explicitly reports connection refused and its inode is unchanged.
Live listeners, timeouts, permission errors, symlinks and non-socket files are refused.
The persistent `.lock` file is intentionally retained; the OS releases its lock when
the process exits. Unload removes only the socket inode this process actually bound.
User-facing one-time installation remains a subsequent integration gate.

## Existing-plugin replacement candidate

The first-install `Preparation` deliberately refuses an existing plugin. A separate
`Replacement` transaction in `setup.py` now handles only the known older local
component hash. Preview pins the current config bytes, old component hashes and
new source hashes without changing the host. Apply requires an explicit owner
confirmation **and** an independently verified stopped Gateway; it also refuses
while the configured receiver socket still exists. It changes only the plugin
directory, keeps the old directory in a private plan record, and reports
`published_runtime_unverified`. It neither reloads Gateway nor declares session
or delivery success. A stopped-host rollback checks the same config and exact
component hashes before restoring the old directory. Interrupted rename states
remain inspectable rather than being silently retried.

This transaction is still a development candidate. The caller's
`gateway_stopped=True` flag and absence of a socket are necessary safeguards, not
proof that all host work has drained; the owner integration must verify process
state and active turns before applying it. No live replacement has been run.

For a read-only local health check, send `{"operation":"probe"}` plus a newline
to the private socket. Its distinct `agent-mailbox.host-health.v1` response reports
Gateway injection availability, the number of pinned sessions, unconfirmed
deliveries and recorded turn receipts. It sends no prompt, changes no delivery
state, and exposes no session identifiers, content or configuration. Unconfirmed
means no exact turn observation yet; it does **not** mean verified host queueing.
Historical receipt counts do not prove the host is currently available. Never
use this diagnostic response as a successful delivery receipt.

The candidate workbench now includes a Python standard-library sender selected by
an explicit [native endpoint configuration](../../docs/native-host-endpoints.md).
That path needs no sender-side Node installation or shell hook. The earlier
development client remains available for isolated protocol diagnostics:

```sh
node integrations/dsh-mailbox-host/client.mjs HOME SOCKET EMPLOYEE_ID DELIVERY_ID
```

The directory name of this diagnostic client does not restrict `host_kind`. It
checks every receipt identity and atomically writes the mailbox receipt. Only this
diagnostic client requires Node. Neither transport alone completes end-user setup.

Compatibility guards verify exact source digests and official method bindings for
the supported Hermes SDK, then read routing availability and the profile-scoped
permission check. The adapter uses private official dispatch APIs through a local
per-request facade; it does not monkeypatch the running host. Unknown SDK versions
are refused and must be revalidated before support is added.

## First-contact diagnostics

The private preparation bridge accepts `preview_discovery` with only a
server-resolved `socket_path`. It produces a reviewable plan; explicit `apply`
installs the fixed component and saves `discovery_only: true` with
`allow_gateway_injection: false`. It neither writes mailbox MCP credentials nor
enables/reloads the plugin. `status` and `remove` use the existing preparation
record. Changes outside the plan's owned keys refuse removal; a newly appearing
mailbox MCP entry is outside a diagnostic plan's ownership. The workbench owner
flow and official activation still need to be connected before this is a usable
first-install path. Do not ask end users to run this private bridge themselves.

An explicitly selected `settings.discovery_only: true` mode loads only a local
metadata service. It requires `socket_path`, not a mailbox home, session pins or
an injection grant. It registers no turn hook, creates no delivery journal, and
rejects every notification envelope even if an old grant exists. The socket
appears only when a supported running Gateway is registered; CLI/TUI, draining,
abandoned or unpublished hosts stay unavailable. Switching to normal receipt
mode still requires the ordinary complete pin/grant/binding checks.

`{"operation":"routing_context"}` obtains the plugin manager's profile and the
actual live SessionStore's routing home/scope through this owner-private socket.
It does not infer runtime state from config.yaml, read chat history or open a DB.
Only already loaded, version-checked host modules are inspected: the diagnostic
must not import the Gateway runner and trigger its bootstrap. The response is a
point-in-time private metadata snapshot with an instance identifier; neither a
session selection nor injection authorization. The workbench must revalidate it
and present bounded, redacted choices rather than expose raw paths in the UI.
First-contact installation/activation and owner UI wiring remain unfinished;
this mode alone does not complete onboarding or prove a real-Agent relay.

## Verification

Run from the mailbox repository with a source checkout explicitly supplied:

```sh
HERMES_SOURCE_ROOT=/path/to/hermes-agent PYTHONPATH=src \
  .venv/bin/python -m pytest tests/test_hermes_host_transport.py
```

Tests cover scheduling-only failure, wrong session/platform, disabled permission,
CLI/TUI refusal, duplicate races, crash recovery, revoked bindings, malformed input,
and actual Node-to-Unix-socket receipt transport. The transport suite uses an explicit
journal-only scheduling double. `tests/test_hermes_pinned.py` separately runs the
actual supported SDK dispatch, strict resolver and plugin lifecycle with synthetic
session storage and an adapter. Run it with the installed Hermes interpreter in a
private synthetic HOME; a skipped SDK test is not a pass. Neither suite starts a
model or proves a complete live Gateway turn.

## First-install preparation adapter (internal, not yet a setup wizard)

`setup.py` uses the **selected Hermes profile's interpreter and official raw-config
API**. Its `Preparation.preview/apply/status/remove` methods prepare the fixed
receiver files, its explicit Gateway injection grant, and an agent-mailbox MCP
entry. The caller must authorize the employee/project and selected existing
Gateway conversation, validate the exported session credential, and supply the
fixed approved executable before calling this internal adapter. It is not a
browser endpoint and does not discover or authorize identities on its own.

Preview preserves host configuration but creates a private, ten-minute plan and
a private full-config backup; it is **not a zero-write operation**. Apply requires
explicit confirmation and unchanged source, configuration, executable and session
file. Existing installations/entries, malformed config and managed keys are
refused. Canonical private paths, non-symlink regular files, a local operation lock,
durable pre-write intent and directory ownership records protect preparation and
cleanup. The lock serializes this adapter, not arbitrary host configuration writers;
pre/post checks do not promise to prevent every external race.

Preparation does not enable or reload Hermes, restart any host, create a binding,
call a model, or send mail. `prepared_not_activated` is only configuration and file
preparation. Session and delivery verification remain false. On interruption,
retain the private record and inspect the partial state; never blindly apply again.
Confirmed removal only cleans this unactivated preparation when its files and
entries still match. It refuses unfamiliar changes rather than restoring an old
whole-config backup. A missing publication receipt is not proof of ownership.
Keep private backups local; they may contain unrelated host configuration secrets.

This first-install adapter requires an existing private Hermes config. Upgrades,
activation through official plugin management, ordinary startup verification,
workbench UI integration and full disconnection of an already activated receiver
are separate work. Official activation can reload other profile plugins; disabling
the selection alone does not prove running callbacks were unloaded. Mailbox routing
must be revoked before separately removing an activated receiver.

The internal preparation bridge also supports read-only profile discovery through
Hermes' official profile API. The owner-only workbench catalog returns opaque
selection identifiers and profile labels, never runtime paths or configuration
contents. This first discovery source is the conventional local installation;
`runtime_not_discovered` does not rule out a custom installation. Profile
candidates are not verified live sessions or notification readiness. Session
selection, preparation, authorization and activation remain separate stages.

Disconnect revokes future mailbox deliveries first. A notification already
admitted to the host queue cannot be promised to disappear or cancel that host's
work; its message access still requires current mailbox authorization. A revoked
binding cannot gain a new transport receipt from the plugin's turn observer.
