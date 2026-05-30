# Installing Superpowers for Antigravity (`agy`)

## Prerequisites

- The [Antigravity CLI](https://antigravity.google) (`agy`) installed and signed in.

## Installation

From a clone of this repository, run:

```bash
./.antigravity-plugin/install.sh
```

This runs Antigravity's own installer (`agy plugin install`) against a staging
directory containing this plugin's `plugin.json`, the repo's `skills/`, and a
freshly generated bootstrap context file. Everything is copied into
`~/.gemini/config/plugins/superpowers/`. **No user config file is edited.**

Start a fresh `agy` session and verify:

> What are your superpowers?

The model should know it has the Superpowers skills.

## How the bootstrap works here

Antigravity has no session-start hook and no `Skill` tool. Its bootstrap surface
is a **plugin context file**: a file named by `contextFileName` in `plugin.json`
(here, `ANTIGRAVITY.md`) that Antigravity loads into the model at the start of
every session.

`install.sh` generates that context file from the live
`skills/using-superpowers/SKILL.md` and the Antigravity tool mapping, wraps it in
the skill system's `EXTREMELY_IMPORTANT` marker, and ships it through
`agy plugin install` (which reports `✔ context : ANTIGRAVITY.md`). Because it is
generated at install time, the installed bootstrap never drifts from the source
skill. The result: `using-superpowers` is active from the very first message — no
hook, no runtime injection, no config edit.

Other skills are loaded by reading their `SKILL.md` with the `view_file` tool (the
blessed mechanism on a harness with no `Skill` tool). See
`skills/using-superpowers/references/antigravity-tools.md` for the full tool
mapping.

## Uninstall

```bash
agy plugin uninstall superpowers
```

## Other harnesses

Antigravity uses its own plugin install. If you also use Claude Code, Codex,
Gemini CLI, OpenCode, or pi, install Superpowers separately for each one.
