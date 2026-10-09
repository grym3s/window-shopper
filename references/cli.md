# CLI reference

Run `python3 scripts/window_shopper.py --help` from this repository. Each command
also has `--help`. Python 3.10+ and its standard library are sufficient.

## Index

```sh
python3 scripts/window_shopper.py index \
  --root /path/to/skills \
  --output /path/to/project/.window-shopper/catalog.json
```

Create the chosen output directory first; writes are atomic and do not create
parent directories. A failed command leaves earlier output files unchanged.
Check the exit status before loading a selection; never treat an old file as the
result of a failed new selection.

Repeat `--root` to index multiple libraries. Choose the roots explicitly rather
than scanning home. The index contains metadata, absolute local paths, file
sizes, hashes and token estimates. Treat it as local state: it can reveal skill
names and filesystem paths. Do not commit your own catalog.

The indexer reads each accepted `SKILL.md` to hash and measure it. Selection uses
names and descriptions, not the instruction bodies. It never executes a skill.
Malformed or unsupported metadata is surfaced in warnings or errors. Review
warnings: an index with skipped files is not a complete view of your library.

Routing fields support plain, single-quoted, JSON-compatible double-quoted, and
folded/literal block scalars. This is deliberately a small YAML reader; anchors,
tags, collections and multiline quoted/plain routing fields are not supported.
Unknown metadata is ignored. No YAML object is constructed or executed.

Scanning is bounded to eight directory levels, 20,000 directory entries, 4,096
skills and 256 KiB per skill. Common dependency/cache directories are skipped.
Depth skips are reported; global scan limits fail with a narrower-root request.

Linked copies of the same file are deduplicated. Distinct files with the same
skill name are ambiguous; choose a narrower root instead of relying on order.

## Select

```sh
python3 scripts/window_shopper.py select \
  --catalog /path/to/project/.window-shopper/catalog.json \
  --task "Review the database migration" \
  --budget 2000 --max-skills 3 \
  --require project-database-rules \
  --output /path/to/project/.window-shopper/selection.json
```

`--output` is optional for selection; without it the JSON goes to stdout.
`--require` may repeat. All required skills count toward both the budget and the
skill limit. A missing or oversized requirement fails selection, rather than
being silently removed. `--project /path/to/repo` optionally contributes bounded
top-level project markers; it does not run build files or read the source tree.

Lexical matching is deterministic. Scores are ranking evidence, not probabilities.
No match is a normal outcome. No required skill, online search or automatic
installation is inferred from a weak match.

Selection status distinguishes `selected`, `no_match`, `budget_exhausted` and
`limit_reached`. A budget failure for optional matches produces an empty result
and warnings. A missing or oversized explicit requirement is a command failure.

Window Shopper normally excludes its own router from the result; explicitly
requiring it includes its cost like any other skill. Existing user, project and
host instructions remain authoritative.

## Load

```sh
python3 scripts/window_shopper.py load \
  --selection /path/to/project/.window-shopper/selection.json \
  --skill project-database-rules
```

Load returns the selected skill's text. It verifies the selection's schema,
counts, budget, and current file content before returning anything. Edited files
require a new index and selection. The hash check catches stale content; it does
not establish trust in the author or provide a security boundary against a user
who controls the manifest and files.

Loading a skill is not permission to follow instructions that conflict with the
user's request. References, scripts and downloads mentioned inside it require
their own relevance and trust decisions.

## Report and budgeting

```sh
python3 scripts/window_shopper.py report \
  --selection /path/to/project/.window-shopper/selection.json
```

The estimate is `ceil(UTF-8 bytes / 4)` for each selected file. Selection must fit
the sum of these estimates and the exact byte ceiling of `budget * 4`. It never
truncates a skill to fit. This is a predictable file allowance, not an exact
model-token allowance. Non-English text and different tokenizers can vary.

The catalog baseline reports the total indexed inventory, not expected default
agent usage. No savings percentage is inferred. The CLI has no conversation
ledger: repeated loads emit text repeatedly, and rerouting does not reclaim
context. The optional [Hermes adapter](../adapters/hermes/README.md) adds session
accounting for loads through its own tools.

Preserve margin for conversation, tool schemas, this router and reference files.
Do not raise a limit automatically just to make a failing selection succeed.
