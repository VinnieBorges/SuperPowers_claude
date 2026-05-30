#!/usr/bin/env bash
#
# Install Superpowers into the Antigravity CLI (`agy`).
#
# Everything ships through `agy plugin install`. We build a staging directory
# holding the manifest, a symlink to the repo's skills/, and a generated context
# file (the bootstrap), then install it. No user config file is ever edited.
#
# The bootstrap is the plugin's context file, declared by `contextFileName` in
# plugin.json. Antigravity loads that file into the model at the start of every
# session, so using-superpowers is active from the first message — no hook and no
# session-start event needed. It is generated here from the live SKILL.md and
# tool mapping so the installed bootstrap can never drift from source.
#
# agy has no Skill tool; the model loads other skills by reading their SKILL.md
# with view_file (see the tool mapping).
#
# Usage: .antigravity-plugin/install.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$REPO_ROOT/.antigravity-plugin/plugin.json"
SKILLS_DIR="$REPO_ROOT/skills"
BOOTSTRAP_SKILL="$SKILLS_DIR/using-superpowers/SKILL.md"
TOOL_MAPPING="$SKILLS_DIR/using-superpowers/references/antigravity-tools.md"

command -v agy >/dev/null 2>&1 || { echo "error: 'agy' (Antigravity CLI) is not on PATH." >&2; exit 1; }
for f in "$MANIFEST" "$BOOTSTRAP_SKILL" "$TOOL_MAPPING"; do
  [ -f "$f" ] || { echo "error: required file not found: $f" >&2; exit 1; }
done
[ -d "$SKILLS_DIR" ] || { echo "error: skills directory not found at $SKILLS_DIR" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cp "$MANIFEST" "$STAGE/plugin.json"
ln -s "$SKILLS_DIR" "$STAGE/skills"

{
  printf '<EXTREMELY_IMPORTANT>\n'
  printf 'You have superpowers.\n\n'
  printf 'The using-superpowers skill content is included below and is already loaded for this Antigravity session. Follow it now; do not try to load using-superpowers again. Antigravity has no Skill tool — load any other skill by reading its SKILL.md with view_file when it applies.\n\n'
  awk 'seen>=2{print} /^---$/{seen++}' "$BOOTSTRAP_SKILL"
  printf '\n'
  cat "$TOOL_MAPPING"
  printf '\n</EXTREMELY_IMPORTANT>\n'
} > "$STAGE/ANTIGRAVITY.md"

echo "Installing Superpowers into Antigravity (agy)…"
agy plugin install "$STAGE"

echo
echo "Done. Start a fresh 'agy' session — the bootstrap context file loads"
echo "automatically, so using-superpowers is active from the first message."
echo "To remove: agy plugin uninstall superpowers"
