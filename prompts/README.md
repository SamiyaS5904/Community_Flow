# prompts/

Every prompt in the system. Python parameterises and assembles these files; it
never authors prompt text.

**Rule: no prompt string may be written inside `dashboard/`, `engine/` or
`services/`.** A code path that needs prompt text loads it from here by name.

## How to use one

```python
from engine.prompts import render, load_agent

text  = render("tasks/poll_format")
brief = render("tasks/asset_content_brief", topic=..., instruction=..., research=...)
agent = writer_agent(group)          # -> load_agent("writer", ...) under the hood
```

Placeholders are single-braced (`{topic}`) and filled by keyword argument. A
missing one raises `PromptError` naming it, rather than sending a literal
`{topic}` to the model. Because `str.format` is used, any **literal** brace in a
prompt body must be doubled — `{{` and `}}`. This matters most in the agent
prompts that specify JSON output shapes.

Agent files carry YAML front matter for `role`, `goal`, `expected_output` and
`agent_type`; the body becomes `instructions`.

`engine.health` calls `prompts.check_all()` at startup, so a malformed prompt
fails the boot rather than three LLM calls into a batch run.

## Layout

### `system/` — the landlord voice, shared by every group

| File | Used by | Purpose |
|---|---|---|
| `content_voice.md` | `PromptBuilder` (content tier) | Identity and tone, built from the tenant's config |
| `audience.md` | `PromptBuilder` (content tier) | Who the post is for |
| `format_rules.md` | `PromptBuilder` (content tier) | Telegram formatting and word-count ceiling |
| `hard_rules.md` | `PromptBuilder` (content tier) | The six anti-AI-slop rules |
| `content_strategy.md` | `PromptBuilder` (content tier) | "Every message must earn its place" |
| `content_voice_fallback.md` | `PromptBuilder` | Used only when no tenant resolved — a bug, not a mode |
| `structural.md` | `PromptBuilder` (structural tier) | Minimal prompt for JSON-emitting agents |
| `json_output.md` | `PromptBuilder` | Appended whenever `is_json=True` |

Content agents (writer, qa, research, pdf_writer) get the full voice block.
Structural agents (planner, asset_planner, asset_mapper) get `structural.md`
only: injecting audience psychology into a JSON-emitting step wastes tokens and
creates instruction conflicts.

### `agents/` — one file per pipeline step

`planner`, `research`, `writer`, `qa`, `pdf_writer`, `asset_planner`,
`asset_mapper`. Each is loaded by the matching function in
`agents/definitions.py`, which supplies the tenant values it needs.

### `tasks/` — short prompts for one job

| File | Called from |
|---|---|
| `topic_invention.md` | `workflow.generate_single_content` when a slot has no topic |
| `planner_day_context.md` | `workflow.plan_daily_queue` (markdown-strategy groups) |
| `asset_content_brief.md` | `workflow.generate_single_content` before writing, for asset posts |
| `asset_planner_context.md` | `workflow.generate_assets` — template selection |
| `asset_mapper_context.md` | `workflow.generate_assets` — placeholder mapping |
| `draft_context.md` | `workflow.generate_single_content` — the Writer's input |
| `asset_caption_note.md` | Appended to the draft context when a graphic carries the detail |
| `poll_format.md` | Any path producing a Telegram poll |

### `bootstrap/` — prompts that generate config for new groups

Used when onboarding a community, not when publishing to one.

## Editing

Prompts are cached after first read. `engine.prompts.clear_cache()` drops them;
a process restart also works. Changing a prompt changes every group that uses
it — that is the point of the landlord/tenant split, so check whether the change
belongs here or in a single group's `config.yaml`.
