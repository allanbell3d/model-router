"""model-router worker: runs ONE routed job to completion and keeps its durable record current.

Launched by the plugin as a tracked background process (``terminal(background, notify_on_complete)``),
so the parent conversation never waits on it. Usage::

    python -I worker.py <job.json>

The job file is the single source of truth: status moves submitted -> running -> done | failed |
timeout | cancelled. The final record and the output file are written BEFORE the short summary is
printed, so a completion notice can never point at a result that was not saved.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

MAX_STDERR = 2_000
NOTICE_CHARS = 3_000
FALLBACK_MARKERS = ("Model fallback:", "switching to fallback")

_child: "subprocess.Popen[str] | None" = None
_job_path: Path
_job: dict


def _now() -> str:
    return time.strftime("%FT%T%z")


def _save() -> None:
    tmp = _job_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_job, ensure_ascii=False, indent=1))
    os.replace(tmp, _job_path)


def _finish(status: str, output: str = "", **extra) -> None:
    out_path = _job_path.with_suffix(".out.md")
    out_path.write_text(output)
    _job.update(status=status, finished_at=_now(), output_path=str(out_path),
                seconds=round(time.monotonic() - _t0, 1), output_chars=len(output), **extra)
    _save()
    head = output if len(output) <= NOTICE_CHARS else output[:NOTICE_CHARS] + "\n...[truncated]"
    print(f"[model-router] job {_job['job_id']} {status} (route {_job['route']}, {_job['seconds']}s)\n"
          f"Full result: delegate_routed(action='result', job_id='{_job['job_id']}') or {out_path}\n\n{head}",
          flush=True)


def _on_term(signum, _frame) -> None:
    """Stop requested: kill the whole worker tree, record it truthfully, exit."""
    if _child is not None and _child.poll() is None:
        try:
            os.killpg(_child.pid, signal.SIGTERM)
            try:
                _child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(_child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _finish("cancelled", f"cancelled by signal {signum}")
    sys.exit(143)


def main() -> int:
    global _child, _job_path, _job, _t0
    _t0 = time.monotonic()
    _job_path = Path(sys.argv[1])
    _job = json.loads(_job_path.read_text())
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    _job.update(status="running", started_at=_now(), worker_pid=os.getpid())
    _save()
    prompt = Path(_job["prompt_path"]).read_text()
    env = {**os.environ, **_job.get("env", {})}
    try:
        _child = subprocess.Popen(_job["cmd"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    except Exception as exc:  # missing executable, bad permissions: fast and truthful
        _finish("failed", f"could not start worker: {type(exc).__name__}: {exc}")
        return 1
    try:
        stdout, stderr = _child.communicate(prompt, timeout=int(_job["timeout_seconds"]))
    except subprocess.TimeoutExpired:
        os.killpg(_child.pid, signal.SIGKILL)
        stdout, stderr = _child.communicate()
        _finish("timeout", (stdout or "").strip() + f"\n\n[timed out after {_job['timeout_seconds']}s]")
        return 1

    session = next((ln.split(":", 1)[1].strip() for ln in (stderr or "").splitlines()
                    if ln.startswith("session_id:")), None)
    output = (stdout or "").strip()
    if _child.returncode != 0:
        output = f"{output}\n\n[exit {_child.returncode}] {(stderr or '').strip()[-MAX_STDERR:]}".strip()
    fallback = any(m in (stdout or "") or m in (stderr or "") for m in FALLBACK_MARKERS)
    _finish("done" if _child.returncode == 0 else "failed", output, exit_code=_child.returncode,
            child_session_id=session, fallback=fallback)
    return 0 if _child.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
