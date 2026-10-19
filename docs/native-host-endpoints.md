# Native local receiver endpoints (0.8.3 development)

The candidate workbench can deliver committed mail notification intents directly
to an explicitly bound Hermes, DSH or candidate Codex Unix receiver. The sender uses Python's
standard library; it needs neither a per-host shell hook nor Node. Receiver
installation, permission, exact existing-session binding and live acceptance are
separate prerequisites. This is not a claim of one-click installation or Windows
support. Existing legacy hook behavior remains available when no native endpoint
is configured for that binding.

The local owner configures `<home>/wake/host-endpoints.json` (regular file, owner
only, mode 0600). A synthetic example:

```json
{"version":1,"endpoints":[{"binding_id":"binding_example","employee_id":"employee_example","host_kind":"hermes","socket_path":"/absolute/private-ipc/hermes.sock"}]}
```

The binding must exactly match the current entry in `host-bindings.json`, including
project and original host session. Rebinding creates a new identity; an old endpoint
does not silently follow it. Receivers independently check their own current
binding before admission. Endpoint directories must be owner-private, canonical
paths; sockets must be owner-owned and mode 0600. The configuration accepts no
commands, shell arguments, credentials, guessed sessions or public network address.

An exact `agent-mailbox.host-receipt.v1` receipt is required. Health responses,
wrong sessions and partial responses are not successful delivery. A total monotonic
deadline bounds response waiting, including slow byte-by-byte responses. An absent
or refused socket is definitely not received and remains pending. Once connected,
loss or timeout is unknown; the sender never falls through to a shell hook or
blindly replays the notification. A later exact receipt may reconcile that original
delivery without another host call. Host receipt is separate from reading mail,
accepting a task, submitting results and Human acceptance.

The workbench commits a durable intent with each directed message and signals its
single worker after commit. The worker still applies disable, per-employee budget,
binding, deduplication and uncertainty guards. `interval=None` in integration tests
disables recovery scans so transaction-event behavior is independently observable;
the normal bounded recovery scan is not the evidence for immediate notification.
The worker offers a pending intent only while that project's private receiver is
reachable, or its explicitly bound legacy hook is available. Hermes and DSH must
also answer their read-only health probe with the matching protocol and an available
capability flag; a merely connectable older or broken socket is not ready. The
Codex candidate must answer `agent-mailbox.receiver-health.v1` for the exact
already-loaded bound thread before an intent is offered. A different thread,
legacy listener, protocol mismatch or unavailable backend stays pending. This
read-only question neither reads mail nor starts a turn. An absent socket
keeps the database intent pending without creating a new delivery file every
recovery scan. After an explicit `not_received` receipt, that intent waits at
least 30 seconds before another attempt. A receiver that comes online is found
by the bounded recovery scan; the saved `seen` list is not the delivery authority.
The owner UI reports transport reachability separately from exact bound-thread
readiness. An available socket still allows the owner to choose an open session,
but is not reported as a ready receiver until that thread passes the health
check. A busy host can temporarily miss the two-second read-only health deadline;
that leaves mail pending and should not be interpreted as a revoked binding.

`tests/test_host_transport.py` covers real Unix IPC with synthetic receivers,
including transaction-driven assignment and reviewer notification, total timeout,
revocation, binding change, malformed receipt/configuration and no shell fallback.
Actual host-session evidence and production activation must be reported separately.

The [Codex development adapter](../integrations/codex-mailbox-host/README.md)
exposes a launcher-owned notification socket only when its mailbox home is
explicitly configured. It targets the same backend and an already-loaded bound
thread. Its source and isolated actual-CLI integration are separate from real
Desktop activation; normal-icon persistence and automatic endpoint repinning
are not implemented. Do not label a configured endpoint as a live original-chat
receiver without actual host verification.
