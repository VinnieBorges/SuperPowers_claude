# Superpowers on the Antigravity CLI (`agy`)

Antigravity is Google's terminal coding agent (`agy`). Superpowers installs as a
native Antigravity plugin.

## Install

From a clone of this repo:

```bash
./.antigravity-plugin/install.sh
```

That runs `agy plugin install` against a staging directory containing the plugin
manifest, the repo's `skills/`, and a generated bootstrap context file.
Everything lands in `~/.gemini/config/plugins/superpowers/`. No user config file
is touched.

Verify in a fresh `agy` session:

> What are your superpowers?

Uninstall with `agy plugin uninstall superpowers`.

## How the bootstrap works

Antigravity has no session-start hook and no `Skill` tool. Its bootstrap surface
is a **plugin context file**: the file named by `contextFileName` in `plugin.json`
(`ANTIGRAVITY.md`), which Antigravity loads into the model at the start of every
session. `install.sh` generates it from the live `using-superpowers` skill plus
the Antigravity tool mapping (wrapped in the `EXTREMELY_IMPORTANT` marker) and
ships it through `agy plugin install` — so the bootstrap is guaranteed-loaded and
can't drift from source. Nothing is injected at runtime and no config is edited.

The model loads any other skill by reading its `SKILL.md` with `view_file` — the
sanctioned skill-loading path on a harness with no `Skill` tool. The full
action→tool mapping lives in
[`skills/using-superpowers/references/antigravity-tools.md`](../skills/using-superpowers/references/antigravity-tools.md).

See [`.antigravity-plugin/INSTALL.md`](../.antigravity-plugin/INSTALL.md) for
details.
