#!/bin/bash
# resolve-provider-config.sh — locate the ZCode CLI built-in provider config.
#
# Why this exists (2026-09-29 wake-channel incident): the wake belt spawns the
# drain turn via `/bin/zsh -lc`, a login shell that does NOT inherit the two
# ZCODE_PROVIDER_* env vars (the ZCode app exports them only into interactive
# sessions). Headless zcode then falls back to its built-in default search
# paths — `/Applications/ZCode.app/Contents/Resources/glm/provider/…` and
# `/config/provider/…` — neither of which exists, so every drain turn dies
# before reading a single letter (wake-zc.log: 「无法定位 CLI ZCode Built-in
# Provider Config」, 5 rounds of no_progress, breaker latched).
#
# Contract (stdout = one path, or nothing on total failure):
#   1. Caller-provided $ZCODE_BUILTIN_PROVIDER_CONFIG_FILE wins untouched.
#   2. Otherwise the HIGHEST version directory under
#      ~/.zcode/v2/runtime/provider/darwin-*/ wins (compared with `sort -V`
#      — deterministic semver-ish order, never glob/目录序); within it the
#      newest endpoint copy wins (mtime tiebreak).
#   3. Fall back to the in-app copy shipped with ZCode.app.
#   4. Nothing found → exit 1, empty stdout.
#
# Nothing is echoed to stderr and no path/value is logged by the caller:
# machine-local paths are treated as leak-level detail (0.7.2 sdist precedent).
# The deployed belt copy (~/.agent-mail/wake-zc.sh) sources this file; the
# copy is a sync target of scripts/wake-zc.sh, not an independent artifact.

set -u

if [ -n "${ZCODE_BUILTIN_PROVIDER_CONFIG_FILE:-}" ] && [ -f "$ZCODE_BUILTIN_PROVIDER_CONFIG_FILE" ]; then
  printf '%s\n' "$ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"
  exit 0
fi

_HOMEDIR="${HOME:?HOME must be set}"

# Highest version component (sort -V), e.g. 3.14.4 > 3.14.3 > 3.14.1.
_ZV=$(for _d in "$_HOMEDIR"/.zcode/v2/runtime/provider/darwin-*/[0-9]*/; do
  [ -d "$_d" ] && basename "$_d"
done 2>/dev/null | sort -V | tail -1)

_ZB=""
if [ -n "$_ZV" ]; then
  # Newest endpoint copy within the winning version (mtime tiebreak).
  _ZB=$(ls -t "$_HOMEDIR"/.zcode/v2/runtime/provider/darwin-*/"$_ZV"/endpoint-*/zcode-builtin.json 2>/dev/null | head -1)
fi

# Fallback: the copy shipped inside the app bundle. The bundle root is
# overridable so tests can exercise this branch without a real /Applications.
if [ -z "$_ZB" ]; then
  _APP="${ZCODE_APP_BUNDLE_ROOT:-/Applications/ZCode.app}"
  [ -f "$_APP/Contents/Resources/config/provider/zcode-builtin.json" ] &&
    _ZB="$_APP/Contents/Resources/config/provider/zcode-builtin.json"
fi

if [ -n "$_ZB" ]; then
  printf '%s\n' "$_ZB"
  exit 0
fi
exit 1
