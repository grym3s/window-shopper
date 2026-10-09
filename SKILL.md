---
name: window-shopper
description: Find relevant installed agent skills without loading the whole library. Use when selecting skills for a task, routing a large skill catalog, or budgeting skill instructions for a local model.
license: MIT
metadata:
  version: "0.1.0"
  python: "3.10+"
---

# Window Shopper

Select a small set of relevant skills before loading their instructions. The
helper searches metadata outside the model context; do not paste the catalog or
read every `SKILL.md` to decide. No network or separate routing model is needed.

Resolve `scripts/window_shopper.py` relative to this skill. Use an existing
catalog when available. Index only the skill roots relevant to the current agent;
the same name in different libraries can be ambiguous. Create the chosen state
directory first. Save explicit outputs in the project's ignored state directory
or task scratch, never loose in home.

```sh
python3 <skill-dir>/scripts/window_shopper.py index --root <skills-root> --output <state-dir>/catalog.json
python3 <skill-dir>/scripts/window_shopper.py select --catalog <state-dir>/catalog.json --task "<current task>" --budget 2000 --max-skills 3 --output <state-dir>/selection.json
python3 <skill-dir>/scripts/window_shopper.py load --selection <state-dir>/selection.json --skill <selected-name>
```

Use `--require <name>` for skills already required by the user or project.
Existing requirements take precedence over routing. If they do not fit, report
the budget conflict instead of silently dropping them. After a failed selection,
do not reuse an older selection file as though the new task succeeded. An explicit user request
for a particular skill also takes precedence over the lexical ranking.

Read the returned reasons. A metadata match is a suggestion, not proof of
relevance. An empty selection is valid. Refine the task description or explicitly
require a known suitable skill when needed. External discovery is optional when
the installed library lacks a match; do not install or execute unreviewed skills
merely because their descriptions match.

Load selected skills only when needed. On changed files, rebuild the catalog and
selection. When the task changes, reroute deliberately and account for instructions
already loaded; changing a JSON file cannot remove text from the conversation.

The budget covers selected `SKILL.md` bytes, with token estimates at one token per
four UTF-8 bytes. It excludes conversation, tools, this router, and referenced
files. It is not the model's actual context limit. The CLI checks each selection;
cross-load session accounting requires the optional Hermes adapter. Generic agents
follow guidance only. See [CLI details](references/cli.md) for parsing, budget and
failure behavior, and [Hermes integration](adapters/hermes/README.md) for hook scope.
