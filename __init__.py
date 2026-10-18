"""model-router: route delegated work to owner-chosen models. No Hermes source changes.

* Routes file, KISS: ``<profile>/plugins/model-router/model-routes.yaml`` if it exists and is valid,
  otherwise the main agent's ``<hermes root>/plugins/model-router/model-routes.yaml``. Exactly one file is
  used, never merged. Every route needs ``use``, ``provider`` and ``model``; ``auto``/``default``/empty,
  unknown keys, a bad reasoning level or a missing ``default`` route reject the whole file. If no file is
  usable the plugin offers no tool and does not block ``delegate_task``. ``<profile>/model-routes.yaml``
  is not read.
* ``delegate_routed(route, goal, context | tasks)`` submits each task as a background job (``worker.py``
  running a one-shot ``hermes chat`` pinned to the route) and returns job_ids at once. Jobs are durable
  JSON records under ``plugin-data/model-router/jobs/``; completion re-enters the conversation through
  Hermes' notify_on_complete. ``action=status|result|stop|list`` inspects or cancels them.
* ``max_parallel`` is a profile-wide cap on running jobs (extra submissions are rejected, not queued).
* ``pre_tool_call`` turns ``delegate_task`` spawns into a pointer to ``delegate_routed``, so agents
  cannot quietly fall back to the single global delegation model (by design).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

ROUTES_FILE = "model-routes.yaml"
DEPTH_ENV = "MODEL_ROUTER_DEPTH"
MAX_OUTPUT = 20_000

PLUGIN_DIR = Path(__file__).resolve().parent
KNOBS = ("default", "timeout_seconds", "max_parallel")
logger = logging.getLogger(__name__)


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _root_master() -> Path:
    """The master in the root home's copy of this plugin (same folder name as ours)."""
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root()) / "plugins" / PLUGIN_DIR.name / ROUTES_FILE
    except Exception:  # older Hermes without the helper: this copy is the only master we know
        return PLUGIN_DIR / ROUTES_FILE


def _load(path: Path) -> Optional[Dict[str, Any]]:
    import hermes_yaml as yaml  # Hermes' own YAML loader (ruamel); PyYAML is not in the managed runtime
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("model-router: cannot read %s (%s)", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _own_file() -> Path:
    """This profile's own routes file: <profile>/plugins/model-router/model-routes.yaml."""
    return _home() / "plugins" / PLUGIN_DIR.name / ROUTES_FILE


ROUTE_KEYS = ("use", "provider", "model", "reasoning", "max_turns")
REQUIRED = ("use", "provider", "model")
TOP_KEYS = ("default", "timeout_seconds", "max_parallel", "routes")


def _invalid(data: Any) -> Optional[str]:
    """Why this routes file cannot be used, or None if it is fully valid. One bad entry rejects the file."""
    if not isinstance(data, dict):
        return "file is empty or not a YAML mapping"
    extra = sorted(set(data) - set(TOP_KEYS))
    if extra:
        return f"unknown top-level key(s) {extra}"
    routes = data.get("routes")
    if not isinstance(routes, dict) or not routes:
        return "no routes"
    for name, r in routes.items():
        if not isinstance(r, dict):
            return f"route {name!r} is not a mapping"
        bad = sorted(set(r) - set(ROUTE_KEYS))
        if bad:
            return f"route {name!r} has unknown key(s) {bad}"
        for k in REQUIRED:
            v = r.get(k)
            if v is None or not str(v).strip() or str(v).strip().lower() in ("auto", "default"):
                return f"route {name!r} needs a real {k!r} (auto/default/empty not allowed)"
        lvl = r.get("reasoning")
        if lvl is not None:
            try:
                from hermes_constants import VALID_REASONING_EFFORTS as valid
            except ImportError:
                valid = None
            if valid and str(lvl).strip().lower() not in (*valid, "none"):
                return f"route {name!r} reasoning {lvl!r} invalid; valid: none, {', '.join(valid)}"
    if data.get("default") not in routes:
        return f"default {data.get('default')!r} is not one of the routes"
    for k in ("timeout_seconds", "max_parallel"):
        if k in data:
            try:
                if int(data[k]) < 1:
                    raise ValueError
            except (TypeError, ValueError):
                return f"{k} must be a positive integer, got {data[k]!r}"
    return None


_cache: Dict[Any, Any] = {}


def _table() -> Optional[Dict[str, Any]]:
    """KISS: use this profile's own plugin-folder routes file if it exists and is valid, otherwise the
    main agent's file. If neither is usable, return None: the tool is not offered and delegate_task runs
    normally. No merging, no inheritance. Each outcome is logged once per file change."""
    candidates = list(dict.fromkeys((_own_file(), _root_master())))
    key = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) if p.is_file() else (str(p), None, None)
                for p in candidates)
    if key in _cache:
        return _cache[key]
    table, notes = None, []
    for path in candidates:
        if not path.is_file():
            continue
        data = _load(path)
        why = "cannot be parsed (see warning above)" if data is None else _invalid(data)
        if why:
            notes.append(f"{path}: {why}")
            logger.warning("model-router: rejected %s: %s", path, why)
            continue
        table = {k: data[k] for k in ("default", "timeout_seconds", "max_parallel") if k in data}
        table["routes"] = {n: dict(r) for n, r in data["routes"].items()}
        table["source"] = str(path)
        logger.info("model-router: using %s (%d routes, default %s)", path, len(table["routes"]),
                    table["default"])
        break
    if table is None:
        logger.warning("model-router: no usable routes file (%s); routing disabled, delegate_task runs "
                       "normally", "; ".join(notes) or "none found: " + ", ".join(map(str, candidates)))
    _cache.clear()
    _cache[key] = table
    return table


def _log(entry: Dict[str, Any]) -> None:
    try:
        log = _home() / "plugin-data" / "model-router" / "calls.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _prompt(goal: str, context: str) -> str:
    parts = [f"TASK:\n{goal.strip()}"]
    if context and context.strip():
        parts.append(f"CONTEXT:\n{context.strip()}")
    parts.append("You are a delegated worker. Do the task, then end with a concise summary of what you did "
                 "and what you found. Do not delegate further.")
    return "\n\n".join(parts)


def _hermes_exe() -> str:
    # Plugins run inside Hermes' own interpreter, so its launcher sits next to sys.executable.
    sibling = Path(sys.executable).parent / "hermes"
    return shutil.which("hermes") or (str(sibling) if sibling.is_file() else "hermes")


def _cmd(route: Dict[str, Any]) -> list:
    # --ignore-rules: no SOUL/AGENTS/memory injection and no memory provider, so workers do not write
    # into the parent's Honcho/MEMORY. Their sessions are tagged --source model-router in state.db.
    cmd = [_hermes_exe(), "chat", "--query-file", "-", "--oneshot", "-Q", "--ignore-rules",
           "--source", "model-router", "-m", str(route["model"])]
    cmd += ["--provider", str(route["provider"])]
    if route.get("reasoning") not in (None, ""):  # unset: no flag, the provider's default level applies
        cmd += ["--reasoning", str(route["reasoning"])]
    if route.get("max_turns"):
        cmd += ["--max-turns", str(int(route["max_turns"]))]
    return cmd


# ---- durable job records ---------------------------------------------------------------------------
ACTIVE = ("submitted", "running")
WORKER = PLUGIN_DIR / "worker.py"


def _jobs_dir() -> Path:
    d = _home() / "plugin-data" / "model-router" / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(job_id: str) -> Optional[Dict[str, Any]]:
    if not job_id or "/" in job_id or job_id.startswith("."):
        return None
    p = _jobs_dir() / f"{job_id}.json"
    try:
        job = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return _merge_spawn(job)


def _merge_spawn(job: Dict[str, Any]) -> Dict[str, Any]:
    try:
        side = json.loads((_jobs_dir() / f"{job['job_id']}.spawn.json").read_text())
        return {**job, **{k: v for k, v in side.items() if k not in job}}
    except (OSError, ValueError, KeyError):
        return job


def _write(job: Dict[str, Any]) -> None:
    p = _jobs_dir() / f"{job['job_id']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _refresh(job: Dict[str, Any]) -> Dict[str, Any]:
    """An active record whose worker is gone (crash, reboot) becomes `lost`, never a silent success."""
    if job.get("status") in ACTIVE:
        pid = job.get("worker_pid") or job.get("pid")
        age = time.time() - float(job.get("submitted_ts", 0))
        if (pid and not _alive(pid)) or (not pid and age > 120):
            job.update(status="lost", finished_at=time.strftime("%FT%T%z"),
                       note="worker exited without writing a final record (crash or restart); "
                            "nothing was re-run automatically")
            _write(job)
    return job


def _all_jobs() -> list:
    out = []
    for p in sorted(_jobs_dir().glob("*.json")):
        try:
            if p.name.endswith(".spawn.json"):
                continue
            out.append(_refresh(_merge_spawn(json.loads(p.read_text()))))
        except (OSError, ValueError):
            continue
    return out


def _spawn(job: Dict[str, Any], task_id: Optional[str]) -> Dict[str, Any]:
    """Launch the worker through Hermes' own background-process path: tracked in the process registry,
    killable, and its exit re-enters the conversation as a completion notice (notify_on_complete)."""
    from tools.terminal_tool import terminal_tool
    command = " ".join(shlex.quote(x) for x in (sys.executable, "-I", str(WORKER), job["path"]))
    raw = terminal_tool(command=command, background=True, notify_on_complete=True, task_id=task_id,
                        workdir=str(_home()), _host_local=True)
    try:
        return json.loads(raw)
    except ValueError:
        return {"error": f"unexpected spawn result: {raw[:300]}"}


def _submit(args: Dict[str, Any], table: Dict[str, Any], task_id: Optional[str]) -> Dict[str, Any]:
    routes, default = table["routes"], table.get("default")
    tasks = args.get("tasks") or [{"goal": args.get("goal"), "context": args.get("context"),
                                   "route": args.get("route"), "dispatch_id": args.get("dispatch_id")}]
    plan = []
    for t in tasks:
        goal = (t.get("goal") or "").strip()
        if not goal:
            return {"error": "every task needs a goal"}
        name = t.get("route") or args.get("route") or default
        if name not in routes:
            return {"error": f"unknown route {name!r}; choose one of {sorted(routes)}"}
        plan.append((name, routes[name], goal, t.get("context") or args.get("context") or "",
                     (t.get("dispatch_id") or "").strip()))

    jobs = _all_jobs()
    by_dispatch = {j.get("dispatch_id"): j for j in jobs if j.get("dispatch_id")}
    cap = max(1, int(table.get("max_parallel", 3)))
    active = sum(1 for j in jobs if j.get("status") in ACTIVE)
    from gateway.session_context import get_session_env
    owner = {"session_key": get_session_env("HERMES_SESSION_KEY", ""),
             "session_id": get_session_env("HERMES_SESSION_ID", ""),
             "platform": get_session_env("HERMES_SESSION_PLATFORM", ""),
             "chat_id": get_session_env("HERMES_SESSION_CHAT_ID", "")}
    results = []
    for name, route, goal, context, dispatch_id in plan:
        if dispatch_id and dispatch_id in by_dispatch:  # retry of the same dispatch: no duplicate worker
            j = by_dispatch[dispatch_id]
            results.append({"job_id": j["job_id"], "status": j["status"], "duplicate": True})
            continue
        if active >= cap:
            results.append({"route": name, "status": "rejected", "goal": goal[:120],
                            "error": f"capacity full: {active}/{cap} routed jobs running on this profile. "
                                     "Retry after a completion notice, or stop one."})
            continue
        job_id = f"mr_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        jd = _jobs_dir()
        prompt_path = jd / f"{job_id}.prompt.md"
        prompt_path.write_text(_prompt(goal, context))
        provider = route["provider"]
        job = {"job_id": job_id, "dispatch_id": dispatch_id or None, "status": "submitted",
               "route": name, "requested": {"provider": provider, "model": route.get("model"),
                                            "reasoning": route.get("reasoning")},
               "goal": goal[:500], "submitted_at": time.strftime("%FT%T%z"), "submitted_ts": time.time(),
               "timeout_seconds": int(table.get("timeout_seconds", 900)), "owner": owner,
               "prompt_path": str(prompt_path), "path": str(jd / f"{job_id}.json"),
               "cmd": _cmd(route), "env": {"HERMES_HOME": str(_home()), DEPTH_ENV: "1"}}
        _write(job)
        spawned = _spawn(job, task_id)
        if spawned.get("error") or not spawned.get("session_id"):
            job.update(status="failed", finished_at=time.strftime("%FT%T%z"),
                       note=f"spawn failed: {spawned.get('error') or spawned}")
            _write(job)
            results.append({"job_id": job_id, "status": "failed", "error": job["note"]})
            continue
        # The worker owns <job>.json after launch; spawn facts go to a sidecar so the parent can never
        # overwrite a newer worker status (read-modify-write race).
        job.update(process_session_id=spawned["session_id"], pid=spawned.get("pid"),
                   notify=bool(spawned.get("notify_on_complete")))
        side = _jobs_dir() / f"{job_id}.spawn.json"
        side.write_text(json.dumps({k: job[k] for k in ("process_session_id", "pid", "notify")}))
        active += 1
        _log({"ts": job["submitted_at"], "event": "submitted", "job_id": job_id, "route": name,
              "via": f"{provider}/{route.get('model')}", "goal": goal[:200]})
        results.append({"job_id": job_id, "route": name, "status": "submitted",
                        "notify": job["notify"]})
    return {"jobs": results,
            "note": "Jobs run in the background; you are free to answer the user now. Each job's completion "
                    "arrives as a new message. Use action='status' or 'result' with the job_id; 'stop' cancels."}


def _view(job: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("job_id", "status", "route", "requested", "goal", "submitted_at", "started_at", "finished_at",
            "seconds", "exit_code", "fallback", "child_session_id", "output_path", "output_chars", "note")
    return {k: job[k] for k in keep if k in job}


def _stop(job: Dict[str, Any]) -> Dict[str, Any]:
    if job.get("status") not in ACTIVE:
        return {"job_id": job["job_id"], "status": job["status"], "note": "not running; nothing to stop"}
    killed = None
    if job.get("process_session_id"):
        try:
            from tools.process_registry import process_registry
            killed = process_registry.kill_process(job["process_session_id"], source="model-router.stop")
        except Exception as exc:
            killed = {"error": f"{type(exc).__name__}: {exc}"}
    pid = job.get("worker_pid") or job.get("pid")
    if pid and _alive(pid):
        try:
            os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
        except OSError:
            pass
    for _ in range(30):  # bounded grace: the worker records `cancelled` itself on SIGTERM
        cur = _read(job["job_id"]) or job
        if cur.get("status") not in ACTIVE:
            return {**_view(cur), "kill": killed}
        time.sleep(0.5)
    cur = _refresh(_read(job["job_id"]) or job)
    if cur.get("status") in ACTIVE:
        cur.update(status="unknown", note="stop sent but the worker has not confirmed; check `status` again")
        _write(cur)
    return {**_view(cur), "kill": killed}


def delegate_routed(args: Dict[str, Any], task_id: Optional[str] = None, **_: Any) -> str:
    if os.environ.get(DEPTH_ENV):
        return json.dumps({"error": "delegate_routed is disabled inside a routed worker (no nesting)."})
    action = (args.get("action") or "submit").lower()
    if action == "list":
        return json.dumps({"jobs": [_view(j) for j in _all_jobs()[-20:]]}, ensure_ascii=False)
    if action in ("status", "result", "stop"):
        job = _read(str(args.get("job_id") or ""))
        if job is None:
            return json.dumps({"error": f"unknown job_id {args.get('job_id')!r}"})
        job = _refresh(job)
        if action == "status":
            return json.dumps(_view(job), ensure_ascii=False)
        if action == "stop":
            return json.dumps(_stop(job), ensure_ascii=False)
        out = ""
        if job.get("output_path"):
            try:
                out = Path(job["output_path"]).read_text()
            except OSError as exc:
                out = f"[result file unreadable: {exc}]"
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT] + f"\n...[truncated {len(out) - MAX_OUTPUT} chars; full: {job['output_path']}]"
        return json.dumps({**_view(job), "output": out}, ensure_ascii=False)
    if action != "submit":
        return json.dumps({"error": f"unknown action {action!r}; use submit, status, result, stop or list"})
    table = _table()
    if not table:
        return json.dumps({"error": f"No route with a model in {ROUTES_FILE}; routing is not enabled here."})
    return json.dumps(_submit(args, table, task_id), ensure_ascii=False)


def _schema(table: Dict[str, Any]) -> Dict[str, Any]:
    menu = "\n".join(f"- {k}: {v.get('use', '')}" for k, v in table["routes"].items())
    labels = sorted(table["routes"])
    task = {"type": "object", "properties": {
        "goal": {"type": "string", "description": "What the worker must accomplish. Self-contained."},
        "context": {"type": "string", "description": "Everything the worker needs; it knows nothing else."},
        "route": {"type": "string", "enum": labels, "description": "Route label for this task."}},
        "required": ["goal"]}
    return {
        "name": "delegate_routed",
        "description": ("Delegate work to an isolated background worker on the owner's model for that kind of "
                        "task. Pick the route that fits the task:\n" + menu +
                        f"\nOmitted route = '{table.get('default')}'. Submit returns job_ids immediately and you "
                        "keep talking to the user; each result arrives later as a completion message. "
                        "action=status|result|stop|list with job_id to inspect or cancel. Never sleep or poll "
                        "waiting for a job."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["submit", "status", "result", "stop", "list"],
                       "description": "Default submit."},
            "job_id": {"type": "string", "description": "For status/result/stop."},
            "dispatch_id": {"type": "string", "description": "Optional idempotency key: a retry with the "
                            "same id returns the existing job instead of starting a duplicate."},
            "route": task["properties"]["route"], "goal": task["properties"]["goal"],
            "context": task["properties"]["context"],
            "tasks": {"type": "array", "items": task, "description": "Batch form; one entry per worker."}}},
    }


def _redirect_delegate_task(tool_name: str = "", args: Any = None, **_: Any) -> Optional[Dict[str, str]]:
    if tool_name != "delegate_task" or not _table():
        return None
    if isinstance(args, dict) and (args.get("action") or "spawn").lower() != "spawn":
        return None  # list/steer/stop of already-running children stay allowed
    return {"action": "block", "message": "delegate_task is routed on this profile. Call delegate_routed "
            "with the same goal/context and a route label from its description."}


def register(ctx) -> None:
    table = _table()
    if not table:
        return
    # Routed workers inherit the profile; they may not delegate, so don't spend their context on the schema.
    if not os.environ.get(DEPTH_ENV):
        ctx.register_tool(name="delegate_routed", toolset="delegation", schema=_schema(table),
                          handler=delegate_routed, emoji="🧭")
    ctx.register_hook("pre_tool_call", _redirect_delegate_task)
