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

## First expanded run

[Run 38014683187](https://github.com/polaris-smart/agent-mailbox/actions/runs/38014683187)
checks commit `4c35131`. A frozen local macOS arm64 run passed 927 tests with four
skipped. Windows, Linux and macOS arm64 extracted-package checks passed before
the full matrix completed; this is package smoke evidence, not GUI acceptance.

Linux's full regression exposed the frontend syntax gate passing the entire
JavaScript bundle as one `node --eval` argument. That exceeds the Linux
per-argument limit (`E2BIG`) and can exceed Windows command-line limits. The
gate now sends source through stdin to `node --input-type=module --check`, checks
the exit status, and has explicit extra-parenthesis/unescaped-quote rejection
cases. It parses as a module without executing DOM operations.

The completed first run had four successful macOS regression jobs, three
successful package jobs, two Linux regression failures caused by the command
size limit, two Windows regression failures (39 failed assertions in each), and
one Intel package startup failure. Windows' seven new lock tests passed on both
Python versions. Its failures included POSIX-only fixtures and actual runtime
assumptions: text-mode file descriptors changed artifact bytes, session and wake
files used chmod instead of native private ACLs, and source-mode path detection
assumed forward slashes. These are addressed without weakening ACL checks or
removing delivery assertions. Two filesystem FIFO cases explicitly require
POSIX; they cannot be created with os.mkfifo on Windows.

Windows Python 3.10 took 589 seconds for the first complete run, near the former
600-second process cap. The process cap is now 900 seconds, with the individual
30-second test timeout unchanged and a 20-minute job cap.

The Intel package mixed a dynamically linked cryptography extension with
incompatible bundled OpenSSL libraries (`_SSL_get0_group_name` was missing).
Intel CI rebuilds cryptography with static OpenSSL using the
[upstream build procedure](https://cryptography.io/en/50.0.2/installation/#building-cryptography-on-macos),
without downgrading it. The public builder rejects dynamic OpenSSL dependencies
in the extension before and after packaging. Upstream cryptography dropped
macOS x86_64 support in
[49.0.0](https://cryptography.io/en/50.0.2/changelog/#v49-0-0);
this is a project-maintained source-build validation path, not an upstream
supported Intel wheel. Native package startup still has to pass after this fix.

Windows hooks now use a fixed `wake-hook.ps1` with the system PowerShell path,
separate arguments, and the machine's existing execution policy. Tests execute
real PowerShell on Windows and cover success, failure, Unicode paths and binary
output. An exit-zero hook receipt is not task completion or Human acceptance.
Wake claims/outbox directories receive their own private ACLs, including when
they already exist; state, session and marker files are protected before writing
payloads. Permission failures prevent hook execution and release owned claims.

The second-round local macOS arm64 suite passed 939 tests with six platform
skips before the final directory-permission hardening. The added permission
denial tests are targeted checks; the next full four-platform workflow must
validate the resulting commit. No Windows or Intel fix is declared accepted
solely from these local results.

## Second expanded run

[Run 38016075780](https://github.com/polaris-smart/agent-mailbox/actions/runs/38016075780)
checks branch head `f1d4961`, using GitHub's merge-test commit `2a17b47` (parents
`9cf139f` and `f1d4961`). All four extracted-package checks passed, including
Intel startup with static OpenSSL. Both Linux and all four macOS regressions
passed, as did Windows Python 3.13. Windows Python 3.10 hit the unchanged
30-second per-test limit while creating 230 separate database transactions for
a pagination fixture. That fixture now batches its synthetic rows in one real
transaction and asserts the exact set of all 230 returned message IDs.

Two older migration checks also depended on a developer's real home and were
skipped on CI. They now create only synthetic projects, tasks, active/revoked
keys and wake watermarks, exercise the schema migration branch, and check data
preservation, key validity/revocation and future-schema refusal. They never read
a real home. These checks still do not substitute for an original released-app
upgrade test. A final workflow will verify the test-only changes.
