# @polaris-smart/agent-mailbox

A small official launcher for **agent-mailbox 0.8.2**, the local-first workbench for existing AI agents. This is separate from `dsh-agent-mailbox`, the DeepSeek Harness integration.

Requires Node.js 22.13+. The launcher downloads the matching **official GitHub Release** bundle on first invocation, verifies the SHA-256 pinned in this npm package, and caches the complete program. The bundle includes Python. No install-time scripts, agent engines, account credentials or global Python installation are required.

```sh
npx @polaris-smart/agent-mailbox@0.8.2 --version
npx @polaris-smart/agent-mailbox@0.8.2
```

Or install the launcher globally:

```sh
npm install -g @polaris-smart/agent-mailbox@0.8.2
agent-mailbox --version
agent-mailbox
```

If another installation already provides the `agent-mailbox` command, use the explicit `npx` command instead of overwriting it. Quit an older running workbench before switching program versions. Existing project data is preserved: this launcher forwards arguments without changing the product's default data home. If you use a custom directory, continue passing the same `--home` path.

The first invocation needs access to GitHub release downloads; later invocations use the verified local program cache. Installation diagnostics go to stderr. Arguments and stdio are forwarded to the real executable, including the MCP subcommands.

Supported targets: macOS arm64; **preview** macOS Intel x64, Windows x64 and Linux x64 (Ubuntu 24.04 build). Unsupported architectures fail explicitly. Windows/Linux use a local service and browser workbench, not the macOS embedded shell. Native extraction uses macOS `ditto`, Windows `tar.exe` or Linux `tar`, which must be available. macOS archives are not Developer ID signed/notarized. The launcher does not alter Gatekeeper settings.

Native program caches are under `~/Library/Caches/agent-mailbox/programs` on macOS, `%LOCALAPPDATA%/agent-mailbox/programs` on Windows, or `$XDG_CACHE_HOME/agent-mailbox/programs` (`~/.cache` fallback) on Linux. Project records remain in the workbench's own data home. Deleting a cache is not a data migration or backup.

[Product and installation guide](https://github.com/polaris-smart/agent-mailbox) · [Release scope and checksums](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.2) · [60-second introduction](https://polaris-smart.github.io/agent-mailbox/#demo)

Existing hosts such as DeepSeek Harness, Workbuddy, Doubao, ZCode, Claude Code, Codex and Hermes connect through project mailbox MCP. Host configuration and successful connection are required; registration alone is not connectivity. Optional managed CLI execution has separate runtime requirements. Human acceptance remains explicit.
