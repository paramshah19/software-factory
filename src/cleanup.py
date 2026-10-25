"""Give disk space back: remove what a finished run can rebuild by itself (its virtual environment, node_modules, build
caches and agent worktrees). Never touches the app's own files, its git history, the report, screenshots or any document.

    sf.py clean            shows what would be removed and how much space it frees (changes nothing)
    sf.py clean --apply    removes it

Only runs that are finished (approved or stopped) and not running are touched. A finished app rebuilds its packages the
next time you open it (`uv run` / `npm install`), so nothing is lost: it just takes a minute again.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Optional

REBUILDABLE = ("app/.venv", "app/node_modules", "app/.worktrees", "app/.pytest_cache", "app/.expo", "app/.ruff_cache", "app/.mypy_cache")
FINISHED = ("done", "stopped")
STALE_AFTER = 20


def _size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).lstat().st_size
            except OSError:
                pass
    return total


def plan(runs_dir: Path, now: Optional[float] = None) -> list[dict]:
    """What could be removed: one row per folder, with its size in bytes."""
    now = time.time() if now is None else now
    rows = []
    for sf in sorted(Path(runs_dir).glob("*/state.json")):
        try:
            s = json.loads(sf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        alive = now - float(s.get("heartbeat") or 0) < STALE_AFTER
        if s.get("status") not in FINISHED or alive:
            continue
        run = sf.parent
        for rel in REBUILDABLE:
            p = run / rel
            if p.is_dir():
                rows.append({"run": run.name, "path": p.as_posix(), "what": rel.split("/", 1)[1], "bytes": _size(p)})
        for cache in run.glob("app/**/__pycache__"):
            if ".venv" not in cache.parts and "node_modules" not in cache.parts and cache.is_dir():
                rows.append({"run": run.name, "path": cache.as_posix(), "what": "__pycache__", "bytes": _size(cache)})
    return rows


def _force_remove(func, path, _exc) -> None:   # Windows marks some files read-only
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def apply(rows: list[dict], runs_dir: Path) -> dict:
    """Remove the rows from plan(). Refuses anything that is not inside runs_dir."""
    base = Path(runs_dir).resolve()
    freed = removed = 0
    for r in rows:
        p = Path(r["path"]).resolve()
        try:
            p.relative_to(base)
        except ValueError:
            continue
        if p.is_dir():
            shutil.rmtree(p, onerror=_force_remove)
            if not p.exists():
                removed += 1
                freed += int(r.get("bytes") or 0)
    return {"removed": removed, "freed_bytes": freed}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"
