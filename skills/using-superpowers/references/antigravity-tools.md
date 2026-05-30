# Antigravity CLI (`agy`) Tool Mapping

Skills speak in actions ("dispatch a subagent", "create a todo", "read a file"). On the Antigravity CLI (`agy`) these resolve to the tools below.

| Action skills request | Antigravity CLI equivalent |
|----------------------|----------------------|
| Read a file | `view_file` |
| Create a new file | `write_to_file` |
| Edit a file | `replace_file_content` |
| Edit a file in several places at once | `multi_replace_file_content` |
| Run a shell command | `run_command` |
| Search file contents | `grep_search` |
| Find files by name / list a directory | `list_dir` (no dedicated glob tool — combine `list_dir` with `grep_search`) |
| Fetch a URL | `read_url_content` |
| Search the web | `search_web` |
| Pose a structured question to your human partner | `ask_question` |
| Dispatch a subagent (`Subagent (general-purpose):` template) | `invoke_subagent` — pass the agent type via the `TypeName` property in the `Subagents` array (see [Subagent support](#subagent-support)) |
| Multiple parallel dispatches | Multiple `invoke_subagent` entries / calls in the same response |
| Task tracking ("create a todo", "mark complete") | `manage_task` |

## Invoking a skill — read its `SKILL.md`

Antigravity surfaces every installed skill's `name` + `description` to you at the
start of each session, but it has **no `Skill`/`activate_skill` tool**. To load a
skill, **read its `SKILL.md` with `view_file`** when the skill applies — e.g.
`view_file` on `.../plugins/superpowers/skills/<skill-name>/SKILL.md`.

This is the blessed skill-loading mechanism on this harness. The general rule
"never read skill files manually" means "don't bypass your platform's
skill-loading mechanism" — and on Antigravity, reading `SKILL.md` *is* that
mechanism. Reading it honors the rule rather than breaking it.

You already know which skills exist and what they're for: their names and
descriptions are in front of you at session start. When a description matches
what you're about to do, read that skill's `SKILL.md` before acting.

## Subagent support

Antigravity dispatches subagents with `invoke_subagent`. The agent type is passed
via the `TypeName` property inside the `Subagents` array. Related tools:
`define_subagent` (define a new subagent type) and `manage_subagents` (manage
running subagents).

Skills dispatch with `Subagent (general-purpose):` and either reference a
prompt-template file (e.g. `superpowers:subagent-driven-development`'s
`./implementer-prompt.md`) or supply an inline prompt. On Antigravity:

| Skill dispatch form | Antigravity equivalent |
|---------------------|----------------------|
| References a `*-prompt.md` template (implementer, spec-reviewer, code-quality-reviewer, code-reviewer, etc.) | Fill the template, then `invoke_subagent` with a general-purpose `TypeName` and the filled prompt |
| References `superpowers:requesting-code-review`'s `./code-reviewer.md` | `invoke_subagent` with a general-purpose `TypeName` and the filled review template |
| Inline prompt (no template referenced) | `invoke_subagent` with a general-purpose `TypeName` and your inline prompt |

### Prompt filling

Skills provide prompt templates with placeholders like `{WHAT_WAS_IMPLEMENTED}` or
`[FULL TEXT of task]`. Fill all placeholders before passing the complete prompt to
`invoke_subagent`. The prompt template itself contains the agent's role, review
criteria, and expected output format — the subagent will follow it.

### Parallel dispatch

Issue multiple `invoke_subagent` calls in the same response to run independent
subagent work in parallel. Keep dependent tasks sequential, but do not serialize
independent subagent tasks just to preserve a simpler history.

## Task tracking

When a skill says "create a todo" or "mark a task complete", use `manage_task`.
If no task tool is available in a given session, fall back to a plan file or
`TODO.md`.
