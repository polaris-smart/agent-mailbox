# Platform validation after v0.8.1

This is development work toward the next patch release. It does not change the
immutable v0.8.1 tag, its archives, or its supported release scope (macOS Apple
Silicon desktop app and local Web).

## Evidence baseline

On 2026-10-10, main was `9cf139f84c621ba4553b5f9dcdbf98d733930502`.
[Main CI](https://github.com/polaris-smart/agent-mailbox/actions/runs/38004786156)
and [Pages deployment](https://github.com/polaris-smart/agent-mailbox/actions/runs/38004785701)
passed. The reusable check workflow's default covered only `macos-latest`.

The earlier [cross-platform failure](https://github.com/polaris-smart/agent-mailbox/actions/runs/37975440891)
was from before the release. Its logs show a Python test fixture executed as a
POSIX executable on Windows (`WinError 193`) and an unconditional `launchctl`
call during wake uninstall on Linux. These are not new failures on current main.

## Candidate validation matrix

| Runner | Native architecture | Python regression | Extracted native package |
| --- | --- | --- | --- |
| macos-15 | arm64 | 3.10, 3.13 | Python 3.13 build |
| macos-15-intel | x64 | 3.10, 3.13 | Python 3.13 build |
| windows-2025 | x64 | 3.10, 3.13 | Python 3.13 build |
| ubuntu-24.04 | x64 | 3.10, 3.13 | Python 3.13 build |

Each job checks Python, Node and runner architectures before testing. Native
package jobs also check the build manifest against the expected OS and CPU,
then extract and launch the built archive and exercise HTTP/MCP with synthetic
data. The checks record diagnostics as workflow artifacts. No provider calls or
real project messages are part of these checks.

## What a green workflow does not establish

- Native package HTTP/MCP checks do not prove desktop-window interaction. The
  current embedded WebKit shell is macOS-specific; browser-based use on another
  OS must not be advertised as an independently accepted native window shell.
- Intel runs must execute on an Intel runner. Rosetta on Apple Silicon does not
  substitute for that evidence.
- Windows wake locking requires actual Windows process contention and crash
  recovery tests; substituting a POSIX lock or mocking success is insufficient.
- Clean installation, upgrade from a released package, interrupted execution,
  default/minimum-window interaction, and Human acceptance remain separate
  release gates. No new platform is promised before its gates pass.

Version numbers and release materials will be synchronized when candidate scope
and artifacts are frozen. This change does not publish a new version.
