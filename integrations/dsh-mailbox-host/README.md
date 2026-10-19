# DSH existing-session receiver (development candidate)

This optional adapter runs inside a DSH host, using its `sessionController.prompt`
service. The inspected Desktop distribution is `0.2.0-rc.2`. It queues notifications
to an explicitly bound existing session and uses a stable request ID for Host
deduplication. It does not create an Agent, configure a provider, or claim that
the Agent has accepted a mailbox task.

The existing `dsh-agent-mailbox` plugin supplies project tools. This receiver adds
the missing inbound notification transport; it does not replace that plugin or
reuse another employee's private connection file.

Required configuration: absolute `home` (mailbox store) and `socketPath` (short
Unix socket path in an owner-only directory). Register `index.mjs` as a Host-plane
DSH plugin with the `sessionController` service. No installation is implicit.
Profile changes affect all sessions using that profile; back up the manifest and
lockfile and verify the actual running plugin before binding a session.

An executable mailbox hook invokes:

```text
node /absolute/path/client.mjs HOME SOCKET EMPLOYEE_ID DELIVERY_ID
```

The last two arguments come from mailbox. The client reads the actual
`HOME/wake/wake-outbox/DELIVERY_ID.json`, verifies the response identity, then
atomically writes `HOME/wake/receipts/DELIVERY_ID`. Missing/refused sockets are
definitely not received. Exceptions after connection, lost responses and invalid
receipts are unknown; they must not be silently retried or counted as successful.
Binding revocation/replacement is checked before every Host admission.

Send `{"operation":"probe"}` followed by a newline over the private socket to
check the loaded Host service without sending a prompt. A successful probe is not
proof of session binding, model execution, task acceptance or human acceptance.

`{"operation":"list_sessions","cwd":"/absolute/workspace"}` lists only existing
top-level, nonempty sessions in that workspace, without activating them. The result
contains identifiers and live/busy flags, not transcripts or provider settings.
Discovery does not automatically choose or bind a session when multiple match.
An absent `running` flag is returned as `null`, never as idle.

`{"operation":"session_activity"}` reads the whole profile's session-list
projection once and returns only `total`, `running`, `unknown`, and
`idle_confirmed` counts/flag. Empty or untyped results fail closed. It is a
momentary observation, not a lock or permission to reload the profile; repeat
the check immediately before a controlled upgrade and preserve a rollback.

Validation:

```sh
node --test integrations/dsh-mailbox-host/test.mjs
PYTHONPATH=src python -m pytest tests/test_dsh_host_transport.py
```

These use synthetic Host services and real local transport. Real DSH installation,
correct-session routing and a complete Agent-to-reviewer round trip remain
separate acceptance gates. Windows transport is explicitly unsupported by this
candidate. The local Codex desktop receiver is not implemented by this module.
# Internal installation preparation bridge

`prepare-bridge.mjs` is a private child-process protocol for the workbench, not
a public endpoint or a manual end-user installation command. Its caller must
select and verify the installed SDK, pin the component files, resolve the launch
context, bound process time/output and authenticate the owner before invoking it.
`workbench_dsh_process.run` provides that internal parent boundary: it verifies
the selected Node executable, component files and approved configuration-module snapshot,
rejects unexpected SDK code, clears inherited environment and runs one bounded
private child. macOS root:admin application directories are treated as privileged
system locations; ordinary group/world-writable ancestors are refused. The
caller must obtain these approval pins and launch context from trusted discovery,
not from the web request. Owner API, discovery and UI wiring are not yet complete.

The bridge accepts only preview/apply/status/remove/reconcile. It uses the official
read-only profile loader and checks bundle, profile, home, launch overlay and
telemetry patch order against `readProfilePatches`. Missing launch context,
skipped bundles and unsupported operations are refused. It does not call the
initializing `loadProfile`, evaluate `!!js`, boot the host or activate the receiver.
Only fixed status fields leave the child; dependency output and error details
are suppressed. Preparing a disabled entry is not session or delivery verification.

After a lost preparation result, `status` stays observational and does not retry.
An explicitly confirmed `reconcile` may acknowledge an already complete disabled
publication, even if the original preview has expired. It rechecks the exact
patch and composition context, original directory identity and every component
file, and refuses an existing receiver endpoint or any removal intent. It only
writes the missing completion record: no patch rewrite, partial-file repair,
activation or delivery. Uncommitted/partial preparation and interrupted removal
still require inspection; this is not a general automatic rollback. Repeated
reconciliation validates the same evidence without rewriting its receipt.

The pure patch planner copies inputs before official composition: that SDK can
mutate inserted row objects when later patches override them. The caller's
launch snapshot must remain unchanged across preview and revalidation.


The preparation subprocess boundary is not an operating-system sandbox. Timeout
cleanup terminates only the process group created by the wrapper; a deliberately
detached descendant would not be covered. The approved preparation bridge does
not launch a host or spawn descendants. Component and SDK hashes must come from
trusted local release/discovery records, not from a browser request; hash checks
and environment isolation do not confine arbitrary substituted code.

`workbench_dsh_sdk.prepare` can create this snapshot directly from an audited
installed macOS DSH `app.asar`, in an owner-private cache. It accepts only known
archive hashes; a new upstream build requires compatibility verification. It
copies the packed configuration modules, not `app.asar.unpacked` native assets,
and must not be used as a replacement DSH installation or general runtime.
Returned per-file hashes are derived afresh from the pinned archive. Existing
cache generations are rechecked and refused if changed; they are never silently
overwritten. Concurrent helper instances serialize publication. The helper does
not activate plugins, boot a profile or modify the installed DSH application.
This cache helper is not yet connected to the owner installation endpoint.

The child wrapper canonicalizes its private temporary HOME and TMPDIR so macOS
`/var` versus `/private/var` aliases cannot disagree with Node's current directory.
Users do not need to override TMPDIR to make that identity check pass.
