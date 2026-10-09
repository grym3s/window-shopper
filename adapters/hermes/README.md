# Hermes adapter (experimental)

Two tools use the same offline router as the CLI:

1. `window_shop(task)` returns a small selection, without skill bodies or the full catalog.
2. `window_load(name)` reads a selected skill, after core hash and budget validation.

The adapter accounts **every successful returned SKILL.md body**, including repeat
reads, against one live-session budget. A reroute uses the remaining budget; it
cannot refill it. A failed read creates no loaded receipt. Calls without a
host-provided `session_id` fail. A thread lock serializes reads and accounting.

## Install explicitly

Keep this checkout in its permanent location. Set the following in your intended
Hermes profile's `config.yaml`, merging with existing settings:

```yaml
plugins:
  enabled:
    - window-shopper
  entries:
    window-shopper:
      settings:
        roots:
          - /absolute/path/to/your/skills
        budget: 2000
        max_skills: 3
        required: []
        guarded_tools: []
        block_native_skill_tools: true
```

Use only trusted skill roots. `required` contains exact skill names. If required
skills cannot fit, routing fails visibly. Append to your existing `plugins.enabled`
list, preserving other plugins. `guarded_tools` optionally lists exact tool names
from your Hermes version; those tools are blocked until routing has run and each
configured required body has been successfully read. Optional ranked skills do not
have to be loaded. An empty selection after a successful no-match route allows
them when required skills are satisfied. The list is empty by default.
`block_native_skill_tools` defaults to `false`; set it to `true` as above when you
want native skill tools redirected to Window Shopper.

The installed-Hermes smoke test covers `plugins.isolation: in_process`, its
default. `host` isolation has not been validated for this adapter. Preserve
existing isolation settings; use a separate test profile for the tested mode.

Install the directory plugin by symlinking the adapter into the profile's
`plugins` directory (replace both paths with your real paths):

```sh
mkdir -p /absolute/path/to/hermes-profile/plugins
ln -s /absolute/path/to/window-shopper/adapters/hermes /absolute/path/to/hermes-profile/plugins/window-shopper
```

Start a fresh Hermes process and conversation. Where your host explicitly selects
toolsets, include `window_shopper`. Verify that both tools are visible and that
`window_shop` followed by `window_load` succeeds before relying on this adapter.
These instructions do not change your existing gates automatically.

## Exact enforcement boundary

- With `block_native_skill_tools: true`, the `pre_tool_call` hook blocks built-in
  `skill_view` and `skills_list`, directing the agent to the budgeted tools. This can conflict with another plugin requiring
  those tools; configure a separate test profile before replacing existing gates.
- Only `window_load` checks selection and charges returned skill bodies. Arbitrary
  shell/file reads, references, tool output, messages, and other loaders are outside
  this boundary. This is not a filesystem sandbox or a total context-window cap.
- `guarded_tools` checks actual successful adapter reads of `required` skills, not
  attempted calls. It does not prove the model understood or followed a skill.
  Rerouting excludes previously read, unchanged bodies from new candidates; an
  unchanged required skill's earlier receipt remains valid without another read.
  A required skill changed on disk must be selected and read again.
- Token counts are `ceil(UTF-8 bytes / 4)` estimates. JSON wrapping, tool schemas,
  selection descriptions, and the rest of the conversation cost additional tokens.
- Hermes' existing skill catalog in the system prompt is **not removed**. This
  adapter uses supported tool and pre-tool hooks; it does not patch prompt internals.
- The ledger is in memory, retained across turns. Restart/reload loses it; resumed
  transcripts are therefore outside its cumulative accounting guarantee. Use a
  fresh conversation after restart. Compression does not reset the ledger.
- No session-end hook clears the ledger: some Hermes versions emit that hook after
  each turn. At 1024 session IDs per plugin instance the adapter refuses new ones
  rather than silently evicting old budgets.

API contract tests cover tool registration, session isolation, successful-read
receipts, reroutes, failed reads, and cumulative budgets. Tests against the installed
Hermes registration API are separate from a fresh model-session acceptance test;
that live model test has **not been run** for this release.
