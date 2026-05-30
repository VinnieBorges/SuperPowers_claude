#!/usr/bin/env bash
# Validate the Antigravity (agy) integration: manifest shape, tool mapping,
# and — most importantly — that the installer ships everything through
# `agy plugin install` and never edits the user's config (rule 2).
#
# CI-safe: does not require `agy` to be installed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PLUGIN_DIR="$REPO_ROOT/.antigravity-plugin"
MANIFEST="$PLUGIN_DIR/plugin.json"
INSTALLER="$PLUGIN_DIR/install.sh"
MAPPING="$REPO_ROOT/skills/using-superpowers/references/antigravity-tools.md"

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "test-plugin-install: checking Antigravity integration"

# --- Manifest ---------------------------------------------------------------
[ -f "$MANIFEST" ] || fail "manifest missing at $MANIFEST"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$MANIFEST" \
  || fail "manifest is not valid JSON"
python3 -c "import json,sys; m=json.load(open(sys.argv[1])); assert m.get('name') and m.get('version'), 'name/version required'" "$MANIFEST" \
  || fail "manifest missing name or version"
# agy auto-scans a co-located skills/ — a 'skills' path field is ignored and
# must not be present (it would mislead a reader into thinking it's honored).
python3 -c "import json,sys; assert 'skills' not in json.load(open(sys.argv[1])), 'agy ignores a skills field; omit it'" "$MANIFEST" \
  || fail "manifest declares a 'skills' field (agy ignores it — remove it)"

# --- Installer exists and uses the harness's own mechanism ------------------
[ -f "$INSTALLER" ] || fail "installer missing at $INSTALLER"
grep -q "agy plugin install" "$INSTALLER" \
  || fail "installer does not use 'agy plugin install'"

# --- Rule 2: installer must NOT edit the user's config ----------------------
# The whole point of this integration is that the bootstrap rides the install
# mechanism. Guard against regressions that *write into* user config to inject it.
# We look for write operations (redirect, tee, sed -i, cp/mv into) whose target
# names a user config path — not mere mentions (comments/echoed text are fine),
# so strip comments first, then match writes.
config_path='(\$HOME|~|\.gemini)[^ ]*(AGENTS\.md|GEMINI\.md|settings\.json|trustedFolders\.json|/config/)'
writes_to_config="$(
  sed 's/#.*//' "$INSTALLER" \
    | grep -nE "(>>?|tee|sed -i|cp |mv ).*${config_path}" \
    || true
)"
if [ -n "$writes_to_config" ]; then
  echo "$writes_to_config" >&2
  fail "installer writes into user config — rule 2 forbids editing user files"
fi

# --- Tool mapping -----------------------------------------------------------
[ -f "$MAPPING" ] || fail "tool mapping missing at $MAPPING"
grep -qiE "SKILL\.md" "$MAPPING" \
  || fail "tool mapping does not document reading SKILL.md as the skill-load path"

# --- SKILL.md points at the mapping ----------------------------------------
grep -qi "antigravity" "$REPO_ROOT/skills/using-superpowers/SKILL.md" \
  || fail "SKILL.md Platform Adaptation does not mention Antigravity"

echo "PASS: Antigravity integration valid (manifest, installer, mapping, rule-2 clean)"
