"""What the factory has cost you today and this month, and the optional daily / monthly caps.

Each run already has its own cap. These are caps across ALL runs, checked when a new run starts (a run that is already
going is never cut off by them: its own cap handles that). A run's cost counts on the day it started.
Set them with `sf.py configure --daily-cap 25 --monthly-cap 200` (0 = no cap).
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path
from typing import Optional

import engines


def caps() -> dict:
    cfg = engines.load_config().get("limits")
    out = {"daily": 0.0, "monthly": 0.0}
    if isinstance(cfg, dict):
        for k in out:
            try:
                out[k] = max(0.0, float(cfg.get(k) or 0))
            except (TypeError, ValueError):
                pass
    return out


def summary(runs_dir: Path, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    today = _dt.datetime.fromtimestamp(now).date()
    day = month = 0.0
    runs_today = 0
    for sf in Path(runs_dir).glob("*/state.json"):
        try:
            s = json.loads(sf.read_text(encoding="utf-8"))
            cost, started = float(s.get("cost_usd") or 0), float(s.get("created_at") or 0)
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            continue
        d = _dt.datetime.fromtimestamp(started).date()
        if d == today:
            day += cost
            runs_today += 1
        if (d.year, d.month) == (today.year, today.month):
            month += cost
    c = caps()
    return {"today": round(day, 2), "month": round(month, 2), "runs_today": runs_today, "daily_cap": c["daily"], "monthly_cap": c["monthly"]}


def check_new_run(runs_dir: Path, now: Optional[float] = None) -> Optional[str]:
    """None when a new run may start, else a plain sentence saying which cap is used up and how to change it."""
    s = summary(runs_dir, now)
    if s["daily_cap"] and s["today"] >= s["daily_cap"]:
        return (f"your daily cap is used up (${s['today']:.2f} of ${s['daily_cap']:.2f} today). "
                "Wait for tomorrow, or raise it: sf.py configure --daily-cap 40 (0 = no cap)")
    if s["monthly_cap"] and s["month"] >= s["monthly_cap"]:
        return (f"your monthly cap is used up (${s['month']:.2f} of ${s['monthly_cap']:.2f} this month). "
                "Raise it: sf.py configure --monthly-cap 300 (0 = no cap)")
    return None
