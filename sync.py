#!/usr/bin/env python3
"""Push the model-router master install to every profile on this host. Idempotent; re-run any time.

Run it with Hermes' own interpreter (it needs ruamel.yaml), from the master copy in <root>/plugins/model-router:

  python3 sync.py --dry-run          show what would change
  python3 sync.py --only my-profile  one profile (`default` = the root home)
  python3 sync.py                    profiles/ + profiles-archive/ (if present)
  python3 sync.py --no-archive       profiles/ only
  python3 sync.py --check            which routes file each profile uses; writes nothing

Per profile:
  1. copy the plugin code (SHIP) into <home>/plugins/model-router/ (overwrite; it's ours)
  2. never creates, copies or edits any model-routes.yaml. A profile uses its own
     <home>/plugins/model-router/model-routes.yaml if you put one there, else the main agent's.
     `python3 sync.py --check` shows which file each profile uses.
  3. add model-router to plugins.enabled in <home>/config.yaml (timestamped backup first)
Logs to <root>/plugin-data/model-router/sync.log.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path

NAME = "model-router"
SRC = Path(__file__).resolve().parent
ROOT = SRC.parent.parent                      # <root hermes home>, master lives in <root>/plugins/model-router
SHIP = ("__init__.py", "worker.py", "plugin.yaml", "model-routes.example.yaml")  # code only: never routes files


def homes(archive: bool):
    for d in sorted((ROOT / "profiles").glob("*")):
        if d.is_dir() and (d / "config.yaml").is_file():
            yield "live", d
    if archive:
        for d in sorted((ROOT / "profiles-archive").glob("*")):
            if d.is_dir() and (d / "config.yaml").is_file():
                yield "archive", d


def _yaml():
    """Hermes' own round-trip editor (no line re-wrapping, same indent as Hermes writes)."""
    sys.path.insert(0, str(ROOT / "hermes-agent"))
    from hermes_yaml import roundtrip_yaml
    return roundtrip_yaml()


def _load(text: str):
    from ruamel.yaml import YAML
    return YAML(typ="safe").load(text) or {}


def _insert_line(text: str) -> str | None:
    """Add `- model-router` under plugins.enabled as one new line, matching the file's own indent.
    Returns None when the layout is not the plain block-list form (caller falls back)."""
    lines = text.splitlines(keepends=True)
    try:
        p = next(i for i, l in enumerate(lines) if re.match(r"plugins:\s*$", l))
    except StopIteration:
        return None
    for i in range(p + 1, len(lines)):
        l = lines[i]
        if l.strip() and not l.startswith((" ", "-")):
            return None                       # left the plugins: block without finding enabled:
        m = re.match(r"(\s*)enabled:\s*$", l)
        if m:
            last, indent = i, None
            for j in range(i + 1, len(lines)):
                mm = re.match(r"(\s*)- ", lines[j])
                if mm and len(mm.group(1)) >= len(m.group(1)):
                    last, indent = j, mm.group(1)
                elif lines[j].strip():
                    break
            if indent is None:
                return None
            lines.insert(last + 1, f"{indent}- {NAME}\n")
            return "".join(lines)
    return None


def _apply(base_text: str) -> str:
    """base text + model-router enabled; verified to differ only by that one entry."""
    new = _insert_line(base_text)
    if new is None:                           # unusual layout: Hermes' own round-trip editor
        y = _yaml()
        data = y.load(base_text) or {}
        data.setdefault("plugins", {}).setdefault("enabled", []).append(NAME)
        import io
        buf = io.StringIO(); y.dump(data, buf); new = buf.getvalue()
    old, chk = _load(base_text), _load(new)
    old.setdefault("plugins", {}).setdefault("enabled", []).append(NAME)
    if chk != old:
        raise RuntimeError("edit verification failed; config left untouched")
    return new


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".model-router.tmp")
    tmp.write_text(text)
    os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)


def enable(cfg: Path, dry: bool) -> str:
    text = cfg.read_text()
    plugins = _load(text).get("plugins") or {}
    if NAME in (plugins.get("disabled") or []):
        return "SKIP: explicitly disabled by owner"
    if NAME in (plugins.get("enabled") or []):
        return "already enabled"
    new = _apply(text)
    if dry:
        return "would enable"
    shutil.copy2(cfg, cfg.with_name(f"config.yaml.bak-model-router-{time.strftime('%Y%m%dT%H%M%S')}"))
    _write(cfg, new)
    return "enabled"


def sync(kind: str, home: Path, mode: str, dry: bool) -> str:
    dest = home / "plugins" / NAME
    acts = []
    if not dry:
        dest.mkdir(parents=True, exist_ok=True)
        for f in SHIP:
            if (SRC / f).is_file():
                shutil.copy2(SRC / f, dest / f)
    acts.append("code synced" if not dry else "would sync code")
    own = dest / "model-routes.yaml"
    acts.append(f"routes: {'own file' if own.is_file() else 'main agent file'}")
    acts.append(enable(home / "config.yaml", dry))
    return f"{kind:7} {home.name:34} " + "; ".join(acts)


def check(no_archive: bool) -> int:
    """Per profile: the one routes file the plugin will use, or why none is usable. Exit 1 on any problem."""
    sys.path.insert(0, str(ROOT / "hermes-agent"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("mr_check", SRC / "__init__.py")
    mr = importlib.util.module_from_spec(spec); spec.loader.exec_module(mr)
    main_file = ROOT / "plugins" / NAME / "model-routes.yaml"
    homes_ = [("root", ROOT)] + list(homes(not no_archive))
    bad = 0
    for kind, home in homes_:
        own = home / "plugins" / NAME / "model-routes.yaml"
        picked, why = None, []
        for path in dict.fromkeys((own, main_file)):
            if not path.is_file():
                continue
            data = mr._load(path)
            err = "unparseable" if data is None else mr._invalid(data)
            if err:
                why.append(f"{path.relative_to(ROOT)} REJECTED: {err}")
                continue
            picked = path
            break
        stray = home / "model-routes.yaml"
        note = f"  (not read, archive it: {stray.relative_to(ROOT)})" if stray.is_file() else ""
        if picked is None:
            bad += 1
        print(f"{kind:7} {home.name:34} -> {picked.relative_to(ROOT) if picked else 'DISABLED (delegate_task runs)'}"
              + ("; " + "; ".join(why) if why else "") + note)
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Push model-router to every Hermes profile on this host.")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    ap.add_argument("--no-archive", action="store_true", help="skip profiles-archive/")
    ap.add_argument("--only", nargs="*", metavar="PROFILE", help="only these profiles (`default` = root home)")
    ap.add_argument("--check", action="store_true", help="show which routes file each profile uses; write nothing")
    a = ap.parse_args()
    if a.check:
        return check(a.no_archive)
    if SRC != ROOT / "plugins" / NAME:
        print(f"run sync.py from the master copy ({ROOT}/plugins/{NAME}), not a profile copy", file=sys.stderr)
        return 2
    lines = []
    if not a.only or "default" in a.only:
        try:
            lines.append(f"root    {'default':34} main agent file; " + enable(ROOT / "config.yaml", a.dry_run))
        except Exception as exc:
            lines.append(f"root    {'default':34} ERROR {type(exc).__name__}: {exc}")
    for kind, home in homes(not a.no_archive):
        if a.only and home.name not in a.only:
            continue
        try:
            lines.append(sync(kind, home, None, a.dry_run))
        except Exception as exc:  # one bad profile must not stop the rest
            lines.append(f"{kind:7} {home.name:34} ERROR {type(exc).__name__}: {exc}")
    print("\n".join(lines))
    if not a.dry_run:
        print("\nRunning gateways keep the route menu they started with: restart them to show route changes.")
    if not a.dry_run:
        log = ROOT / "plugin-data" / NAME / "sync.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as fh:
            fh.write(f"--- {time.strftime('%FT%T%z')}\n" + "\n".join(lines) + "\n")
    return 1 if any(" ERROR " in ln for ln in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
