"""Saved app presets: the "App context" and "First-open experience" you type again and again, saved under a name.

Stored in workspace/presets.json (yours, never in git). The New run form lists them and fills both boxes in one click.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

MAX_PRESETS = 50


def _file(workspace: Path) -> Path:
    return Path(workspace) / "presets.json"


def load(workspace: Path) -> list[dict]:
    try:
        data = json.loads(_file(workspace).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [p for p in data if isinstance(p, dict) and p.get("name")] if isinstance(data, list) else []


def save(workspace: Path, name: str, app_context: Optional[str], first_open: Optional[str]) -> list[dict]:
    """Add or replace a preset (same name = replace). Returns the new list."""
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:60]
    if not name:
        raise ValueError("give the preset a name")
    app_context, first_open = (str(app_context or "").strip()[:4000], str(first_open or "").strip()[:2000])
    if not (app_context or first_open):
        raise ValueError("write the app context or the first-open experience first")
    items = [p for p in load(workspace) if p["name"].lower() != name.lower()]
    items.append({"name": name, "app_context": app_context, "first_open": first_open})
    if len(items) > MAX_PRESETS:
        raise ValueError(f"at most {MAX_PRESETS} presets: delete one first")
    items.sort(key=lambda p: p["name"].lower())
    Path(workspace).mkdir(parents=True, exist_ok=True)
    _file(workspace).write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return items


def delete(workspace: Path, name: str) -> list[dict]:
    items = [p for p in load(workspace) if p["name"].lower() != str(name or "").strip().lower()]
    if _file(workspace).exists() or items:
        _file(workspace).write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return items
