"""The owner's keys for outside services (API keys, test payment keys), kept on this computer only.

Apps the factory builds may use outside services (an AI API, payments in test mode, email, maps...). Each one reads its
key from an environment variable and has a FAKE MODE used whenever the key is missing, so the app runs and every test
passes with no keys at all. The agents list those variables in ``factory/env.json``.

The owner adds keys on the dashboard (Keys panel). They are stored in ``~/.software-factory/keys.env`` (SF_HOME moves
the folder), one ``NAME=value`` per line, readable only by the owner's account on macOS and Linux.

Who sees a key:
- the app, and only when the OWNER starts it from the dashboard ("Open the app"), and only the names its env.json lists;
- never an agent (the store is outside every run folder; on Claude Code agents cannot read outside it), never a log,
  never the dashboard page (it only learns whether a key is set), never the automatic checks (they use fake mode, so
  screenshot checks cannot spend money on a paid API).
The driver also refuses a build that contains one of the stored values (a key copied into the code).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

NAME_RX = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
URL_RX = re.compile(r"https?://\S+")
MAX_VARS = 30


def home() -> Path:
    return Path(os.environ.get("SF_HOME") or (Path.home() / ".software-factory"))


def store_path() -> Path:
    return home() / "keys.env"


def load() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = store_path().read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            if NAME_RX.fullmatch(k.strip()):
                out[k.strip()] = v
    return out


def _save(data: dict[str, str]) -> None:
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "# Keys for apps built by the Software Factory. Managed from the dashboard (Keys panel). Never commit this file.\n"
    body += "".join(f"{k}={v}\n" for k, v in sorted(data.items()))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def set_key(name: str, value: str) -> None:
    """Store (or, with an empty value, remove) one key. Raises ValueError for a bad name or value."""
    if not NAME_RX.fullmatch(name or ""):
        raise ValueError("a key name looks like OPENAI_API_KEY: capital letters, digits and underscores")
    value = (value or "").strip()
    if any(c in value for c in "\r\n\0"):
        raise ValueError("a key is one line of text")
    if len(value) > 4000:
        raise ValueError("that is too long to be a key")
    if re.match(r"(?:sk|rk|pk)_live_", value):
        raise ValueError("that is a LIVE payment key: the factory only works with test mode. Use the test key (it starts with sk_test_ or pk_test_)")
    data = load()
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    _save(data)


def is_set(names: list[str]) -> dict[str, bool]:
    data = load()
    return {n: bool(data.get(n)) for n in names}


def env_for(names: list[str]) -> dict[str, str]:
    """The stored values for these names only (least privilege: an app gets the keys it declared, nothing else)."""
    data = load()
    return {n: data[n] for n in names if data.get(n)}


def leaked(root: Path, iter_files) -> list[dict]:
    """Stored key values that appear in the app's files: [{file, name}]. Values are compared in memory, never reported."""
    values = {v: k for k, v in load().items() if len(v) >= 8}
    if not values:
        return []
    out = []
    for p in iter_files(root):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for v, k in values.items():
            if v in text:
                out.append({"file": p.relative_to(root).as_posix(), "name": k})
    return out[:20]


def read_env_json(path: Path) -> tuple[list[dict], list[str]]:
    """factory/env.json: the outside services an app uses. A missing file means none. Returns (vars, problems)."""
    import json
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [], [f"factory/env.json is not valid JSON: {e}"]
    rows = data.get("vars") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return [], ['factory/env.json must be {"vars": [...]}']
    errs, out, seen = [], [], set()
    for i, v in enumerate(rows[:MAX_VARS]):
        if not isinstance(v, dict):
            errs.append(f"env.json var {i + 1} must be an object")
            continue
        name = str(v.get("name") or "")
        if not NAME_RX.fullmatch(name):
            errs.append(f"env.json var {i + 1}: name {name!r} must look like OPENAI_API_KEY")
            continue
        if name in seen:
            errs.append(f"env.json: {name} is listed twice")
        seen.add(name)
        for field in ("service", "purpose", "fake_mode"):
            if not str(v.get(field) or "").strip():
                errs.append(f"env.json {name}: '{field}' is required (fake_mode = what the app does when the key is missing)")
        if not isinstance(v.get("secret"), bool):
            errs.append(f"env.json {name}: 'secret' must be true or false")
        link = str(v.get("get_it") or "")
        if link and not URL_RX.fullmatch(link):
            errs.append(f"env.json {name}: 'get_it' must be a web address")
        out.append({"name": name, "service": str(v.get("service") or "")[:60], "purpose": str(v.get("purpose") or "")[:200],
                    "fake_mode": str(v.get("fake_mode") or "")[:200], "secret": bool(v.get("secret", True)), "get_it": link[:300]})
    if len(rows) > MAX_VARS:
        errs.append(f"env.json lists more than {MAX_VARS} variables")
    return out, errs


def example_problems(app: Path, names: list[str]) -> list[str]:
    """Every listed variable must appear in app/.env.example (with a placeholder), so a person can see what the app reads."""
    if not names:
        return []
    ex = app / ".env.example"
    if not ex.exists():
        return ["app/.env.example is missing: list every variable from factory/env.json there, with placeholder values"]
    text = ex.read_text(encoding="utf-8", errors="ignore")
    missing = [n for n in names if not re.search(rf"(?m)^\s*{re.escape(n)}\s*=", text)]
    return [f"app/.env.example does not list: {', '.join(missing)}"] if missing else []


def describe(vars_: list[dict]) -> list[dict]:
    """What the dashboard shows: the declared variables and whether each is set. Never the values."""
    state = is_set([v["name"] for v in vars_])
    return [dict(v, set=state.get(v["name"], False)) for v in vars_]
