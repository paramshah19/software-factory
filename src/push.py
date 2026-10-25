"""Phone notifications: a short push to your phone when a run needs you, through ntfy (https://ntfy.sh, free).

Off until you switch it on (`sf.py push on`, or the Phone & power panel). How it works, in plain words:
  * You install the free "ntfy" app on your phone and subscribe to YOUR topic: a random name only you know.
  * When a run needs you (or hits a usage limit), the factory sends one short line to that topic. It works anywhere,
    on mobile data too, because ntfy is on the internet.
  * Only a generic line is sent, such as "Software Factory needs you: Approve the spec". Never your idea, your code,
    keys or the run's title. Anyone who learns the topic name could read those lines, so treat it like a password.
  * Prefer not to use a public server? Run your own ntfy and set its address with `sf.py push on --server URL`.

Standard library only. `send` never raises: a failed push must never stop a run.
"""

from __future__ import annotations

import json
import re
import secrets
import urllib.request
from typing import Optional

import engines

DEFAULT_SERVER = "https://ntfy.sh"


def settings() -> dict:
    cfg = engines.load_config().get("push")
    if not isinstance(cfg, dict) or not cfg.get("topic") or cfg.get("enabled") is False:
        return {}
    return {"topic": str(cfg["topic"]), "server": str(cfg.get("server") or DEFAULT_SERVER).rstrip("/")}


def new_topic() -> str:
    """Long and random: the topic name is the only thing protecting your notifications on a public server."""
    return "sf-" + secrets.token_urlsafe(16).lower().replace("_", "x").replace("-", "y")


def _ascii(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", str(text).encode("ascii", "ignore").decode()).strip()[:limit]


def send(title: str, message: str, urgent: bool = False, opener=urllib.request.urlopen) -> bool:
    """One short notification. Returns whether it was sent. Never raises."""
    cfg = settings()
    if not cfg:
        return False
    try:
        req = urllib.request.Request(f"{cfg['server']}/{cfg['topic']}", data=_ascii(message, 200).encode("utf-8") or b"ping", method="POST",
                                     headers={"Title": _ascii(title, 80) or "Software Factory", "Priority": "high" if urgent else "default",
                                              "Tags": "factory"})
        with opener(req, timeout=6) as r:
            return 200 <= getattr(r, "status", 200) < 300
    except Exception:  # noqa: BLE001  (offline, server down, bad address: all the same to a best-effort push)
        return False


def _save(push_cfg: dict) -> None:
    path = engines.config_path()
    cfg = engines.load_config()
    cfg["push"] = push_cfg
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + chr(10), encoding="utf-8")


def enable(server: Optional[str] = None) -> dict:
    """Switch push on. Keeps your topic if you had one, so the phone stays subscribed."""
    cur = engines.load_config().get("push")
    cur = cur if isinstance(cur, dict) else {}
    new = {"topic": cur.get("topic") or new_topic(), "server": (server or cur.get("server") or DEFAULT_SERVER).rstrip("/"), "enabled": True}
    if not re.fullmatch(r"https?://[^\s/]+(:\d+)?", new["server"]):
        raise ValueError("the server must look like https://ntfy.sh")
    _save(new)
    return settings()


def disable() -> None:
    cur = engines.load_config().get("push")
    if isinstance(cur, dict):
        _save({**cur, "enabled": False})


def subscribe_url(cfg: Optional[dict] = None) -> str:
    cfg = cfg or settings()
    return f"{cfg['server']}/{cfg['topic']}" if cfg else ""
