# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.2.0] - 2026-10-18

First public release.

### Changed
- `delegate_routed` no longer blocks the parent turn. A submit returns job_ids in under 2 s; each job runs as a
  Hermes-tracked background process (`worker.py`) with notify_on_complete, so its result arrives as a new message.
- Each profile now uses exactly one routes file: its own `plugins/model-router/model-routes.yaml` if present and
  valid, otherwise the root one. Files are never merged, and a profile's `<home>/model-routes.yaml` is not read.
- Routes files are validated as a whole: every route needs a real `use`, `provider` and `model` (no
  `auto`/`default`/empty), unknown keys and invalid reasoning levels are errors, and `default` must name a route.
  One bad entry rejects the file. With no usable file the tool is not offered and `delegate_task` runs normally.
- `max_parallel` is now a profile-wide cap on running jobs; a submission over the cap is rejected, not queued.
- Workers run with `--ignore-rules` (no SOUL/AGENTS/memory injection, no memory provider writing into the
  parent's stores).
- A timeout or stop now kills the worker's whole process group, not just the `hermes` process.
- Fallback detection matches Hermes' current "Model fallback:" notice as well as the older one.
- `sync.py` copies plugin code only and never creates or edits a routes file.

### Added
- Durable job records in `plugin-data/model-router/jobs/` (submitted, running, done, failed, timeout,
  cancelled, lost).
- `action=status|result|stop|list` with `job_id`, and `dispatch_id` for idempotent retries.
- `sync.py --check`: shows which routes file each profile uses, or why none is usable.

### Removed
- Per-route inheritance between a profile file and the master table.
- `sync.py --routes keep|add|reset` and the profile route stubs it wrote.

## [0.1.0] - 2026-10-11

First version (not published).

### Added
- `delegate_routed` tool: run delegated work on owner-chosen routes (label -> provider/model/reasoning), single
  or batched in parallel, with per-call logging to `plugin-data/model-router/calls.jsonl`.
- `pre_tool_call` hook that redirects `delegate_task` spawns to `delegate_routed`.
- Master route table in the root home, read live by every profile (the profile's own copy is a fallback).
- Per-route profile overrides: an empty/`auto` model inherits the master route, and a set model runs exactly
  as written.
- `sync.py` to push the plugin to every profile and enable it, with `--routes keep|add|reset` for existing
  profile route files.
- Warning when a route's `reasoning` is not a valid Hermes level (Hermes would otherwise silently ignore it).
- A failed worker returns the end of its stderr, so the parent sees why it failed.
- Results and the call log carry `"fallback": true` when Hermes switched the worker to a fallback provider.
- Routed workers don't get `delegate_routed` in their tool list (nesting is refused anyway).
