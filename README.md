# model-router

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets you choose which model
handles each kind of delegated work.

Out of the box, Hermes sends every `delegate_task` to the single model set under `delegation:` in
`config.yaml`. model-router replaces that with a table of **routes**: labels such as `quick`, `code` or `review`,
each pinned to a provider, model and reasoning level you choose. Agents pick the route that fits the task, the
work runs in the background on that model, and the result comes back to the conversation when it is done.

No Hermes source changes are needed.

## How it works

- **`delegate_routed` tool.** The agent submits a `goal`, a `context` and a `route` label, or a `tasks` list
  for several jobs at once. Each job starts as a background process and the tool returns its `job_id` straight
  away, so the agent can keep talking to the user. When a job finishes, its result arrives as a new message.
- **Workers.** Each job runs `worker.py`, which starts a one-shot `hermes chat` pinned to the route's provider,
  model and reasoning level, in the same profile. Workers run with `--ignore-rules` (no SOUL/AGENTS/memory
  injection and no memory provider, so they don't write into the parent's memory) and are tagged
  `--source model-router`.
- **Managing jobs.** `action=status|result|stop|list` with a `job_id` shows a job's state, returns its full
  output, stops it (the worker and everything it started) or lists recent jobs. A `dispatch_id` makes a submit
  idempotent: retrying with the same id returns the existing job instead of starting a second one.
- **`pre_tool_call` hook.** Blocks new `delegate_task` spawns and tells the agent to use `delegate_routed`
  instead, so work can't quietly fall back to the global delegation model. `delegate_task` list/steer/stop
  of already-running children still work.
- **No nesting.** Workers can't delegate further: inside a worker the tool isn't offered and calls are refused.

### Job records

Every job is a durable record in `$HERMES_HOME/plugin-data/model-router/jobs/`:

| File | Contents |
|---|---|
| `<job_id>.json` | Route, requested model, status, timings, exit code, fallback flag |
| `<job_id>.prompt.md` | The prompt sent to the worker |
| `<job_id>.out.md` | The worker's full output |

A job moves `submitted` → `running` → `done`, `failed`, `timeout` or `cancelled`. A job whose worker
disappeared without a final record (crash, reboot) is marked `lost`; nothing is re-run automatically.
Submissions are also logged to `$HERMES_HOME/plugin-data/model-router/calls.jsonl`.

## Install

```sh
cd ~/.hermes/plugins
git clone https://github.com/allanbell3d/model-router.git
cd model-router
cp model-routes.example.yaml model-routes.yaml   # then fill in every route you keep
```

Then enable the plugin in `config.yaml`:

```yaml
plugins:
  enabled:
    - model-router
```

Start a new session; `delegate_routed` should now appear in the tool list. For several profiles, see
[Profiles](#profiles).

## The routes file

```yaml
default: general         # route used when the agent doesn't name one
timeout_seconds: 900     # per job
max_parallel: 3          # routed jobs running at once on this profile

routes:
  code:
    use: Write, refactor or debug code across one or more files.
    provider: <provider name>
    model: <model id>
    reasoning: medium    # optional; leave out for the provider's default level
    max_turns: 60        # optional
```

- **`use`** is shown to agents word for word and is how they choose. Keep it to one line, "what - when",
  and make neighbouring routes clearly distinct.
- **`max_parallel`** caps running jobs across the whole profile. A submission over the cap is rejected with a
  message, not queued.

The file is checked as a whole, and **one bad entry rejects the whole file**:

- every route needs a real `use`, `provider` and `model`; empty, `auto` and `default` are not accepted;
- route keys are limited to `use`, `provider`, `model`, `reasoning` and `max_turns`, and top-level keys to
  `default`, `timeout_seconds`, `max_parallel` and `routes`;
- `reasoning`, if set, must be a level Hermes knows (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`,
  `max`, `ultra` on current versions);
- `default` must name one of the routes; `timeout_seconds` and `max_parallel` must be positive integers.

The reason a file was rejected is written to the Hermes log.

## Profiles

Each profile uses **exactly one** routes file:

1. its own `<profile>/plugins/model-router/model-routes.yaml`, if that exists and is valid;
2. otherwise the main agent's `<root>/plugins/model-router/model-routes.yaml`.

The two files are never merged. To give a profile different models, put a complete routes file in that
profile's plugin folder. If neither file is usable, the profile gets no `delegate_routed` tool and
`delegate_task` works normally. A `model-routes.yaml` directly in a profile's home is not read.

### sync.py

A profile loads plugins from its own `plugins/` folder, so each one needs its own copy of the plugin code.
`sync.py` pushes it to every profile. Run it with Hermes' interpreter, from the root copy in
`<root>/plugins/model-router`:

```sh
python3 sync.py --dry-run           # show what would change
python3 sync.py                     # all profiles (profiles/ and profiles-archive/ if present)
python3 sync.py --no-archive        # profiles/ only
python3 sync.py --only work         # a single profile; `default` = the root home
python3 sync.py --check             # which routes file each profile uses, or why none is usable
```

For each profile, `sync.py`:

1. copies the plugin code (`__init__.py`, `worker.py`, `plugin.yaml`, `model-routes.example.yaml`);
2. adds `model-router` to `plugins.enabled`, after taking a timestamped backup of `config.yaml`.

It never creates, copies or edits a routes file, and it skips any profile that lists the plugin under
`plugins.disabled`. Runs are logged to `<root>/plugin-data/model-router/sync.log`.

Editing a routes file doesn't need a sync. Re-run `sync.py` when the plugin code changes.

### After changing routes

Hermes builds the tool list once, when a process starts. A new CLI session sees route changes straight away,
but a **running gateway keeps the route menu it started with until you restart it**. New jobs still use the
current file.

## Requirements

- Hermes Agent with plugin support and background processes with `notify_on_complete`.
- Linux or macOS: stopping and timing out jobs uses process groups.
- Any provider you route to must already be configured and authenticated in Hermes.

## Known limitations

- **Provider fallback.** If a route's provider fails (auth, quota, rate limit), Hermes moves the worker to the
  profile's `fallback_providers` and still reports success. model-router can't turn this off for a single run.
  It marks such jobs with `"fallback": true` in the job record and in the result.
- **Hermes internals.** Background jobs use Hermes' terminal tool, process registry and session context
  directly. A Hermes update that changes those can break job submission until the plugin is updated.
