# Optional macOS release steps

GitHub source, CLI installation and GitHub Release downloads do not require an
Apple Developer account. The current `.app` is an optional unsigned test artifact;
GitHub hosting does not remove macOS Gatekeeper checks. Source/CLI remains the
primary onboarding path while the desktop build is tested.

`release-workbench.py` performs read-only inspection or prints a reviewable command
plan. It never signs, submits, staples, publishes, creates a keychain, or changes
an existing app. There is no automatic updater in this release.

```sh
uv run python scripts/release-workbench.py inspect \
  --app '/absolute/path/Agent Mailbox.app'
```

The local check on 2026-09-30 found zero valid signing identities and no Developer
ID Application identity. Team ID was not configured. The alpha1 test app has an
ad-hoc signature whose local integrity check passed; this is not a Developer ID
signature or notarization. No signing account is needed to continue development.

## Future signing plan

If a publisher later supplies an existing Developer ID Application identity,
review the entitlements and third-party redistribution/re-signing terms first.
Use a new staging directory. The original app remains untouched.

```sh
uv run python scripts/release-workbench.py plan \
  --app '/absolute/path/Agent Mailbox.app' \
  --output-dir /absolute/path/new-release-staging \
  --identity 'Developer ID Application: REPLACE WITH EXISTING IDENTITY' \
  --entitlements /absolute/path/reviewed-release.entitlements \
  --third-party-terms-reviewed > /absolute/path/signing-plan.json
```

The plan copies the app, signs individual Mach-O files from deepest paths first,
then seals nested framework/bundle containers and finally the outer app. It does
not use `codesign --deep` to sign. `--deep` is used only for final verification.
The plan is a set of commands for separate review and execution, not evidence that
signing, hardened-runtime behavior, or notarization has passed. Entitlements must
be tested with the signed Python/Node/native-agent processes; a plist file alone
cannot establish the required grants.

Claude Agent SDK and its platform binary are proprietary dependencies, separate
from the Apache-2.0 Claude ACP adapter. Their shipped LICENSE points to Anthropic's
legal terms. Anthropic requires its Claude Code binary to remain unmodified and
retains its native authentication flow. The plan preserves a valid hardened
vendor signature on these binaries and refuses an unsigned/incompatible vendor
binary instead of re-signing it. Other bundled dependencies need their own license
and notice review. The review flag records the publisher's decision; the script
cannot verify rights or agreement acceptance. Preserving a vendor signature does
not prove that the outer app will pass notarization.

## Separate notarization and publishing

The plan prints future commands using `notarytool submit` with an existing
Keychain profile; it accepts no passwords/tokens. Executing the submit command
uploads the archive to Apple and requires a separate, explicit publishing
authorization. Do not execute it as part of local development or inspection.

After an accepted submission, staple and validate the ticket, run Gatekeeper
assessment, then make a new distribution ZIP containing the stapled app. Test the
actual signed/stapled artifact in an isolated home on a fresh Mac. Do not claim
public distribution readiness from an ad-hoc signature, a printed plan, or a local
integrity check. GitHub upload is another explicit publishing step.

Sources: [Apple signing](https://developer.apple.com/documentation/xcode/creating-distribution-signed-code-for-the-mac/),
[Apple notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow),
[Anthropic legal and compliance](https://code.claude.com/docs/en/legal-and-compliance).
