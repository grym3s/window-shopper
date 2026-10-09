# Evaluation

This release verifies routing, budget arithmetic, stale-file rejection and the
documented adapter behavior with isolated tests. It does not claim measured
model-quality, latency, VRAM or end-to-end token improvements.

The report's catalog baseline is the sum of indexed skill-file sizes. It is an
inventory comparison, not a realistic claim that ordinary agents would load
every file. Agent Skills already supports loading bodies on demand.

To measure practical value, run the same tasks, model, context limit, skill
library and host configuration with native skill discovery and Window Shopper.
Include easy tasks, ambiguous requests, multi-skill tasks, missing skills,
mandatory instructions, rerouting and non-English descriptions.

Count the entire input with the model's tokenizer: startup catalog, router,
tools, selections, skill bodies, retries and references. Record completion
quality, required-skill recall, latency and failure rate alongside tokens.
Publish the task corpus, settings and failures. A smaller prompt is useful only
when the agent can still complete the work.
