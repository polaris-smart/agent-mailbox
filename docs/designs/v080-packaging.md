# v0.8.0 local application packaging

The macOS build is a PyInstaller `.app` with embedded Python, web assets, Node and
the locked ACP runtime dependency tree. The user does not need Python/npm to run
that artifact. Native agent credentials remain in the user's native agent stores;
the app neither copies them nor logs in on the user's behalf.

## Build contract

Use an isolated Python environment with this repository installed and
PyInstaller. Install the managed Node dependencies explicitly before building.
The build script itself does not install, download, update or publish software.

```sh
uv venv /tmp/mailbox-build-env --python .venv/bin/python
uv pip install --python /tmp/mailbox-build-env/bin/python pyinstaller==6.22.3 -e .
/tmp/mailbox-build-env/bin/python -m agent_mailbox.runtime_bridge.install_runtime \
  --destination /tmp/mailbox-runtime
/tmp/mailbox-build-env/bin/python scripts/build-workbench.py \
  --runtime-dir /tmp/mailbox-runtime \
  --node-binary /absolute/path/to/node \
  --output-dir /tmp/mailbox-build
```

Build independently on each target OS/architecture. This initial script supports
macOS and does not produce Windows/Linux artifacts from a Mac. Node must match the
host architecture and be >=22.13; the current local build uses Node 22.23.1.
The script verifies exact ACPX/adapter versions and records a build manifest with
the dependency-lock digest and architecture.

Output:

- `dist/Agent Mailbox.app`: self-contained app bundle.
- `build-manifest.json`: local build facts.
- `build/` and `Agent Mailbox.spec`: disposable build intermediates.

## Runtime contract

PyInstaller sets `sys._MEIPASS` to the executable's bundle resource root. Data and
binary entries may be linked across `Contents/Resources` and `Contents/Frameworks`;
the app uses `_MEIPASS/runtime/bin/node` and `_MEIPASS/runtime/deps` rather than
assuming a physical macOS directory layout. Frozen Python module `__file__` paths
are under `_MEIPASS/agent_mailbox`, so existing `Path(__file__).parent` references
find `workbench_assets` and `runtime_bridge` added with those package destinations.

`workbench_app.py` sets process-local `AGENT_MAIL_NODE_BIN` and
`AGENT_MAIL_RUNTIME_DIR` to bundle paths. It never writes dependency/runtime files
into the bundle. Managed sessions, tasks, logs and project data are kept in the
external home selected by `--home` (or the workbench's default).

Normal entry forwards `--home`, `--no-browser` and `--port` to `workbench.main`.
The frozen MCP command `"<app executable>" --workspace-mcp` runs
`workspace_mcp.main` over stdio, using the parent-issued project/employee environment.
It does not launch another browser/server. This removes the need for a system
Python when managed employees read project context through MCP.

For isolated validation:

```sh
"/tmp/mailbox-build/dist/Agent Mailbox.app/Contents/MacOS/Agent Mailbox" \
  --home /tmp/mailbox-app-smoke-home --no-browser --port 0
```

The server binds to localhost and emits its session URL. Test only the isolated
home and local HTTP health/UI/runtime readiness; do not use existing mail homes or
run a real model as part of packaging smoke tests.

## Release boundary

This build has no Developer ID signature or notarization. PyInstaller may apply
local ad-hoc signatures for Mach-O executables; that does not make it a signed,
notarized distribution. It is a local build artifact requiring a separate release
process before public distribution. The installer and build do not implement
automatic updates or rollout/rollback, and must not be described
as delivering those features. The product store performs its own additive SQLite schema migrations. A future updater must preserve the external home.
