# 0.8.1 scope, installation and validation

**Release: 0.8.1. Human acceptance was confirmed by the project owner on 2026-10-10.**

## Included in this release

- macOS Apple Silicon native App, distributed as `Agent-Mailbox-0.8.1-darwin-arm64.zip`.
- Local Web interface and Python package, with macOS as the validated platform for this release.
- English and Chinese README, upgrade guidance, release notes and SHA-256 checksums.

No Windows or Linux native package is claimed for 0.8.1. Broader platform work is deferred. The separately maintained npm companion is not the workbench installer and is not automatically republished with it.

## Installation and data

The Mac App includes its Python runtime; existing-session mailbox use does not require the workbench to launch a coding runtime. Optional managed CLI execution needs its own prepared runtime and host login. Python/source installations require Node.js 22.13+ for managed execution.

For local Web, use a dedicated Python 3.10+ environment, install the matching `agent-mailbox==0.8.1` package after publication, then run:

```sh
AGENT_MAILBOX_BROWSER=1 agent-mailbox --home ~/.agent-mailbox
```

The browser flag requests a window; ordinary CLI startup may only print the local access URL. Keep that URL private. Reuse your existing data home when upgrading rather than silently starting a new one. Quit the old application/service before replacing binaries. Back up the home and external project files separately. Do not let an older binary open an upgraded database; restore a compatible pre-upgrade backup when rolling back. v0.7 data/configuration migration is not included.

The macOS App is not Developer ID signed or notarized. Checksums verify artifact bytes, not publisher identity. No automatic updater is promised.

## Verified candidate behavior

These are scoped technical results from an isolated candidate, not a statement that Human acceptance has already occurred:

- Native project switching and new-project/new-task forms, desktop default/minimum-window navigation, and narrow Web mail reading were exercised.
- A nonempty database created by the official 0.8.0 package was opened by the official old executable, upgraded by the candidate executable, and compared for preserved project/member/task/message/resource/session data.
- The automatic pre-upgrade database backup was restored together with pre-upgrade supporting files and opened again by the official old executable.
- Two real local agent hosts used project mailbox tools to send/read, explicitly accept, read pinned resource versions and submit a result. An isolated host hook connected the workbench notification to the receiving host.
- After a service crash between accepted/read and submitted states, the same recipient session resumed and submitted once. The task remained in review for Human action.

This does not cover every host, cross-machine mailbox operation, every crash point, or managed code execution. A hook delivering to a host is not proof of task completion. Mailbox submission is an agent report; patch capture belongs to the separate managed workflow. Descriptive project responsibilities do not grant management permissions.

## Publication checklist

1. Human completes candidate acceptance. Agents do not decide accept/reject on the user's behalf.
2. Freeze source and build final artifacts with version `0.8.1`; verify source revision, clean build provenance, package metadata, native bundle metadata, API version and visible App/Web version.
3. Run relevant gates and installed-artifact smoke checks. Preserve earlier candidate evidence with its original revision.
4. Remove release-preparation notices only when the publication gates have passed. Publish the tag, GitHub Release, matching artifacts/checksums and Python package; verify public downloads and installation.
5. Update the default branch README and GitHub release notes together. Public materials describe agent hosts and product behavior, not internal model selections, account identities or private logs.

Public version strings do not by themselves prove that a release has been published.

## Platform validation scope

The release and PR regression gates target macOS (Python 3.10 and 3.13 plus native packaging), matching the 0.8.1 release scope. The manually dispatched Extended platform checks workflow retains Linux and Windows coverage for follow-up work. Preliminary runs built and exercised native packages on all three systems, but Linux regression tests still contain a launchctl assumption and a Windows indexing fixture is not portable. These results do not qualify Windows or Linux for this release.
