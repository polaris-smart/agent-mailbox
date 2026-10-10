# agent-mailbox 0.8.2

0.8.2 is a platform reliability release. Existing-session project mailbox collaboration remains available to MCP-capable hosts, including DeepSeek Harness, Workbuddy, Doubao, ZCode, Claude Code, Codex and Hermes. Which hosts were rechecked is separate from which hosts can connect.

## Packages and support levels

| Package | Scope |
|---|---|
| `Agent-Mailbox-0.8.2-darwin-arm64.zip` | Established macOS Apple Silicon App and local Web distribution. |
| `Agent-Mailbox-0.8.2-darwin-x64.zip` | Additional **preview** Intel macOS App, built on a native Intel runner. |
| `Agent-Mailbox-0.8.2-win32-x64.zip` | Additional **preview** Windows x64 local service with browser UI. |
| `Agent-Mailbox-0.8.2-linux-x64.tar.gz` | Additional **preview** Linux x64 local service with browser UI, built on Ubuntu 24.04. |
| Python wheel and sdist | Python 3.10+; independent installation and runtime preparation. |

Native bundles include Python; no agent CLI engines or user accounts are bundled. Windows/Linux do not include the macOS embedded desktop shell. Preview packages have native CI coverage, but full user installation, GUI, original-release upgrade and real-agent acceptance on each new platform are not established. Older Linux distributions are not covered by an Ubuntu 24.04 build.

Extract the **whole** archive. On Windows, run `Agent Mailbox.exe` inside its extracted folder. On Linux, run `./Agent\ Mailbox` inside that folder. Retain dependencies alongside the executable. For macOS, move the extracted `Agent Mailbox.app` into Applications. Quit the existing workbench before switching versions.

macOS downloads are not Developer ID signed or notarized. Do not disable system security globally. Keep the workbench local and keep its authenticated access link private.

## What changed

- Native OS/CPU checks and four separate bundles; Python 3.10/3.13 regression on all four targets.
- Revalidate device authorization under the task-claim lock so revocation cannot race the initial claim.
- Windows-safe wake locking, process-exit recovery, protected wake directories, actual PowerShell hook tests, binary reads and path handling.
- No macOS-only service removal command on Linux. Optional CodeGraph explicitly unsupported on Windows.
- Intel macOS cryptography builds link OpenSSL statically to avoid conflicting bundled libraries.
- Version metadata is shared by the workbench UI and backend. Package version and macOS bundle metadata are 0.8.2.

The development baseline passed 12 CI jobs: eight regression jobs and four extracted native package HTTP/MCP checks. The final release is rebuilt and rechecked from its release commit. See the tag's Actions run and `release-provenance.json` for the exact revision and artifact hashes. A green CI job is not evidence of untested human interaction or new-platform feature parity.

## Upgrade and recovery

Back up the existing home and external project files, stop the old instance, replace the complete program, and continue with the **same home**. There is no new database schema migration in this patch relative to 0.8.1. Synthetic database migration and future-schema rejection are tested in CI; they do not prove every historical upgrade path. Do not delete `instance.json` as a routine upgrade step.

Restoration is manual: stop the new program and restore the compatible prior program and backup together when needed. Database backups do not include all repositories, worktrees or agent-host logins. v0.7 is not automatically migrated.

The bounded two-real-host acceptance from 0.8.1 remains documented in [its release scope](RELEASE-081.md); 0.8.2 does not claim that workflow has been repeated on every new platform. Notification, explicit acceptance, pinned-resource reading, delivery, and Human approval remain separate steps. Optional managed CLI integrations have a narrower adapter and platform verification scope than project mailbox collaboration.

## Video and npm

The [60-second narrated introduction](https://polaris-smart.github.io/agent-mailbox/#demo) uses synthetic demonstration projects and enlarged screenshots of the released 0.8.1 UI. It is not a live agent execution recording. The video is hosted with the website rather than added to the software Release assets.

`dsh-agent-mailbox` is a separate DeepSeek Harness integration package. Do not install the unrelated unscoped npm package `agent-mailbox` as this workbench. A scoped workbench launcher is only an installation option once its own package and instructions have actually been published.
