"""Deploy a finished app with the owner's own accounts or package it.

Single-page apps: Netlify, Vercel, GitHub Pages, or a desktop program (Electron). Apps with a server: a Docker image
(local), or Render (a public address, set up from a one-click link the owner opens with their own Render account).

This is a separate step after the owner has approved a run, started on purpose from the dashboard or `factory.py deploy`.
The factory never handles credentials: it uses whatever is already signed in on this computer (command-line tools) or connected
in Claude (connectors), and when something is not signed in it says so and tells the owner what to do.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

import checks
import engines
import kinds
from common import StageFailed, sh, slug

CONNECTOR_CACHE_SECONDS = 120
STATUSES = ("deployed", "built", "handoff", "needs_login", "needs_tools", "failed")  # handoff = the owner finishes with one link
SKIP_DIRS = {".git", ".venv", ".worktrees", "node_modules", "__pycache__", ".pytest_cache", "tests", "dist", "build", ".github"}
SKIP_SUFFIXES = {".py", ".pyc", ".toml", ".lock"}
SKIP_NAMES = {"README.md", ".gitignore", ".gitattributes", "pytest.ini", ".env"}

SERVER_KINDS = ("server", "api", "worker", "multi")
KIND_NAMES = {"static": "single-page web app", "server": "web app with a server", "cli": "command-line tool", "api": "API",
              "worker": "bot or background worker", "library": "library", "multi": "multi-service system", "mobile": "phone app",
              "desktop": "desktop app", "godot": "Godot game"}

TARGETS: dict[str, dict] = {
    "netlify": {"title": "Netlify", "public": True, "kinds": ("static",), "keywords": ("netlify",), "tools": ("node",),
                "pitch": "A free public web address like yourname.netlify.app. The simplest way to put a web app online."},
    "vercel": {"title": "Vercel", "public": True, "kinds": ("static",), "keywords": ("vercel",), "tools": ("node",),
               "pitch": "A free public web address like yourname.vercel.app. Same idea as Netlify, pick whichever account you have."},
    "github-pages": {"title": "GitHub Pages", "public": True, "kinds": ("static",), "keywords": ("github",), "tools": ("gh",),
                     "pitch": "Publishes from a NEW PUBLIC GitHub repository (yourname.github.io/...). Your app's code becomes public too."},
    "desktop": {"title": "Desktop app (Electron)", "public": False, "kinds": ("static",), "keywords": (), "tools": ("node",),
                "pitch": "A real desktop program you double-click (Windows, Mac or Linux: whichever this computer is), built here. Nothing is published and no account is needed. "
                         "The first build downloads about 100 MB."},
    "docker": {"title": "Docker image", "public": False, "kinds": SERVER_KINDS, "keywords": (), "tools": ("docker",),
               "pitch": "A container image of your app, built on this computer. Any cloud host can run it, and so can this computer. "
                        "Nothing is published and no account is needed."},
    "render": {"title": "Render", "public": True, "kinds": SERVER_KINDS, "keywords": ("render",), "tools": ("gh",),
               "pitch": "A public web address on Render's free plan, with a database if the app uses one. The code goes to a NEW PUBLIC "
                        "GitHub repository; then you open one link, sign in to Render and press Apply (you can type your keys there)."},
    "installer": {"title": "Desktop installer", "public": False, "kinds": ("desktop",), "keywords": (), "tools": ("uv",),
                  "pitch": "A program for this computer you double-click (plus a .zip to share), built with PyInstaller. Nothing is published."},
    "package": {"title": "Python package", "public": False, "kinds": ("library", "cli"), "keywords": (), "tools": ("uv",),
                "pitch": "A ready-to-install package (wheel) built on this computer, with the steps to publish it on PyPI with your own account."},
    "expo": {"title": "Phone app (Expo)", "public": False, "kinds": ("mobile",), "keywords": ("expo",), "tools": ("node",),
             "pitch": "Gets the app onto your phone: try it at once in the free Expo Go app, or build an installable Android/iPhone app with "
                      "your own free Expo account (you sign in yourself)."},
    "godot-export": {"title": "Game build (Godot)", "public": False, "kinds": ("godot",), "keywords": (), "tools": ("godot",),
                     "pitch": "A playable build of the game for this computer (plus a .zip). Needs Godot's export templates (one download)."},
}


class DeployError(Exception):
    """The deploy cannot start (not approved yet, app not compatible, nothing is signed in...). The message is for the owner."""


# --------------------------------------------------------------------------- what the owner has signed in


def parse_mcp_list(text: str) -> list[dict]:
    """`claude mcp list` prints lines like 'claude.ai Netlify: https://... - ✓ Connected'. Returns [{name, connected, status}]."""
    out = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(?P<name>\S.*?):\s+\S.*?\s+-\s+(?P<status>.+?)\s*$", line)
        if not m:
            continue
        status = m.group("status")
        out.append({"name": m.group("name").strip(), "status": status, "connected": bool(re.match(r"^\W*connected\b", status, re.I))})
    return out


_cache: dict = {"at": 0.0, "rows": []}


def parse_codex_mcp_list(text: str) -> list[dict]:
    """`codex mcp list --json` prints [{name, enabled, ...}]. Enabled servers are available to every headless Codex agent."""
    try:
        data = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    return [{"name": str(d["name"]), "status": "enabled" if d.get("enabled") else "disabled", "connected": bool(d.get("enabled"))}
            for d in data if isinstance(d, dict) and d.get("name")] if isinstance(data, list) else []


def detect_connectors(run: Callable = sh, which: Callable = shutil.which, fresh: bool = False) -> list[dict]:
    """The connectors (MCP servers) the owner's agent has. Claude Code: `claude mcp list`; Codex: `codex mcp list --json`.
    Best effort: an empty list just means the command line is used instead."""
    if not fresh and time.time() - _cache["at"] < CONNECTOR_CACHE_SECONDS:
        return _cache["rows"]
    engine = engines.engine_name()
    rows: list[dict] = []
    if engine == "claude" and which("claude"):
        rc, out = run([which("claude"), "mcp", "list"], Path.cwd(), 90)
        if rc == 0:
            rows = parse_mcp_list(out)
    elif engine == "codex" and which("codex"):
        rc, out = run([which("codex"), "mcp", "list", "--json"], Path.cwd(), 90)
        if rc == 0:
            rows = parse_codex_mcp_list(out)
    _cache.update(at=time.time(), rows=rows)
    return rows


def matching_connectors(target: str, connectors: list[dict]) -> list[dict]:
    words = TARGETS.get(target, {}).get("keywords", ())
    return [c for c in connectors if any(w in c["name"].lower() for w in words)]


def connector_allow(name: str) -> str:
    """The permission rule that allows every tool of one connector (its tools are named mcp__<server>__<tool>)."""
    return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", name)


def options_for(app_json: Optional[dict], connectors: list[dict], which: Callable = shutil.which) -> list[dict]:
    """What the owner can pick, with plain reasons for anything unavailable."""
    kind = (app_json or {}).get("type")
    rows = []
    for tid, t in TARGETS.items():
        conn = [c for c in matching_connectors(tid, connectors) if c["connected"]]
        tools_ok = all((kinds.find_godot(which) if x == "godot" else which(x)) for x in t["tools"])
        reason, via = "", ""
        if not app_json:
            reason = "This run has no finished app yet."
        elif kind not in t["kinds"]:
            fits = [TARGETS[o]["title"] for o in TARGETS if kind in TARGETS[o]["kinds"]]
            reason = (f"This option is for a {' or '.join(KIND_NAMES[k] for k in t['kinds'])}; yours is a {KIND_NAMES.get(kind, kind)}"
                      + (f": use {', '.join(fits)}." if fits else ": run it on this computer, see the report."))
        elif conn:
            via = f"your {conn[0]['name']} connector"
        elif tools_ok and t["public"]:
            via = "the command line (you may need to sign in once)"
        elif tools_ok:
            via = "this computer"
        else:
            need = {"node": "Node.js (nodejs.org)", "gh": "the GitHub command-line tool (cli.github.com)",
                    "docker": "Docker Desktop (docker.com), running", "uv": "uv (docs.astral.sh/uv)", "godot": "Godot 4 (godotengine.org)"}[t["tools"][0]]
            reason = f"Needs {need}, or a connected {t['title']} connector in Claude."
        rows.append({"id": tid, "title": t["title"], "pitch": t["pitch"], "public": t["public"], "available": not reason,
                     "reason": reason, "via": via, "connector": conn[0]["name"] if conn else None})
    return rows


# --------------------------------------------------------------------------- the folder that ships


def export_site(rd: Path, app_json: dict) -> tuple[Path, int]:
    """Copy the finished single-page app (and only that) to deploy/site/, and refuse if it contains anything secret-like."""
    app = rd / "app"
    entry = str(app_json.get("entry") or "index.html")
    if ".." in Path(entry).parts or not (app / entry).is_file():
        raise DeployError(f"the app's entry file {entry!r} does not exist")
    dest = rd / "deploy" / "site"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    count = 0
    for p in sorted(app.rglob("*")):
        rel = p.relative_to(app)
        if not p.is_file() or any(part in SKIP_DIRS for part in rel.parts) or p.suffix.lower() in SKIP_SUFFIXES or p.name in SKIP_NAMES:
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest / rel)
        count += 1
    if entry != "index.html" and not (dest / "index.html").exists():
        (dest / "index.html").write_text(f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url={entry}">\n', encoding="utf-8")
    secrets = checks.scan_secrets(dest)
    if secrets:
        raise DeployError("not deploying: something that looks like a secret is in the app (" +
                          ", ".join(f"{s['file']}:{s['line']}" for s in secrets[:3]) + "). Remove it first.")
    return dest, count


APP_SKIP_DIRS = {".git", ".venv", ".worktrees", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}


def export_app(rd: Path) -> tuple[Path, int]:
    """Copy a server app's code to deploy/app/ (no git history, virtual env, caches or .env files), refusing anything secret."""
    import keys
    app = rd / "app"
    dest = rd / "deploy" / "app"
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    count = 0
    for p in sorted(app.rglob("*")):
        rel = p.relative_to(app)
        if not p.is_file() or any(part in APP_SKIP_DIRS for part in rel.parts) or p.name == ".env" or p.suffix in (".db", ".sqlite", ".sqlite3"):
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest / rel)
        count += 1
    secrets = checks.scan_secrets(dest)
    leaks = keys.leaked(dest, checks.iter_text_files)
    if secrets or leaks:
        where = [f"{s['file']}:{s['line']}" for s in secrets[:3]] + [x["file"] for x in leaks[:3]]
        raise DeployError("not deploying: something secret is in the app (" + ", ".join(where) + "). Remove it first.")
    return dest, count


# --------------------------------------------------------------------------- the agent's answer


def validate_result(path: Path, rd: Path) -> Optional[str]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return f"{path.name} is missing or not valid JSON: {e}"
    if not isinstance(d, dict) or d.get("status") not in STATUSES:
        return f"status must be one of {', '.join(STATUSES)}"
    if not str(d.get("summary") or "").strip():
        return "summary (one plain sentence) is required"
    if d["status"] == "deployed" and not re.match(r"^https://\S+$", str(d.get("url") or "")):
        return "a deployed result needs an https url"
    if d["status"] == "built":
        p = str(d.get("path") or "")
        if not p or ".." in Path(p).parts or not (rd / p).exists():
            return "a built result needs a path (relative to the run folder) that exists"
    if d["status"] in ("needs_login", "needs_tools", "handoff") and not [x for x in (d.get("next_steps") or []) if str(x).strip()]:
        return "needs_login, needs_tools and handoff must give next_steps the owner can follow"
    if d["status"] == "handoff" and not re.match(r"^https://\S+$", str(d.get("url") or "")):
        return "a handoff result needs the https link the owner opens to finish"
    return None


def deploy_prompt(rd: Path, target: str, site: Path, name: str, connectors: list[dict], env_names: Optional[list[str]] = None) -> str:
    t = TARGETS[target]
    conn = [c["name"] for c in connectors if c["connected"]]
    lines = [f"RUN_DIR: {rd.as_posix()}", f"CARD: deploy-{target} — Deploy: {t['title']}", "",
             f"TARGET: {target} ({t['title']})", f"FOLDER TO SHIP: {site.as_posix()}", f"SUGGESTED NAME: {name}",
             f"Write your result to factory/deploy/{target}.json."]
    if env_names is not None:
        lines.append("APP START (from factory/app.json, for a container rewrite paths to the container): see factory/app.json")
        lines.append("ENVIRONMENT VARIABLES the app reads (names only; values are the owner's): " + (", ".join(env_names) or "none"))
    if conn:
        how = ("their tool names start with mcp__" if engines.engine_name() == "claude" else "use that connector's tools")
        lines.append(f"CONNECTOR TOOLS you may use for this target ({how}): " + ", ".join(conn) +
                     ". Prefer the connector over the command line: it is already signed in to the owner's account.")
    else:
        lines.append("No connector is connected for this target: use the command line.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- the whole step


def deploy_run(rd: Path, target: str, agent=None, connectors: Optional[list[dict]] = None, which: Callable = shutil.which) -> dict:
    """Deploy one finished run. Returns the entry recorded in state['deploys']. Raises DeployError if it cannot start."""
    import factory  # late: factory imports pipeline, which this module must not import at load time

    if target not in TARGETS:
        raise DeployError(f"unknown target {target!r}; choose one of {', '.join(TARGETS)}")
    run = factory.Run(rd, agent=agent or factory.run_claude)
    st = run.state
    if st.get("status") != "done":
        raise DeployError("approve the finished app first; a run can only be deployed once it is done")
    cur = st.get("deploy") or {}
    if cur.get("status") == "running" and time.time() - (st.get("heartbeat") or 0) < 30:
        raise DeployError("a deploy of this run is already in progress")
    app_json, problems = checks.read_app_json(rd / "factory" / "app.json", rd / "app")
    if connectors is None:
        connectors = detect_connectors()
    opt = next(o for o in options_for(app_json, connectors, which) if o["id"] == target)
    if not opt["available"]:
        raise DeployError(opt["reason"])
    import keys
    server = app_json.get("type") != "static"
    site, _count = export_app(rd) if server else export_site(rd, app_json)
    env_names = [v["name"] for v in keys.read_env_json(rd / "factory" / "env.json")[0]] if server else None
    mine = matching_connectors(target, connectors)
    run.extra_allow = [connector_allow(c["name"]) for c in mine if c["connected"]]
    n = len(st.get("deploys") or []) + 1
    name = (slug(st.get("title") or st.get("idea") or "app")[:24].strip("-") or "app") + "-" + time.strftime("%m%d")
    out = rd / "factory" / "deploy" / f"{target}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    cid = f"deploy-{target}-{n}"
    prior_budget = st.get("budget_usd")
    with run.lock:
        st["budget_usd"] = 0  # the build's budget gate does not apply to this separate, owner-requested step
        st["deploy"] = {"target": target, "status": "running", "started_at": time.time()}
        run.save()
    run.start_heartbeat()
    run.event("deploy_started", cid, f"deploying to {TARGETS[target]['title']}")
    result: dict
    try:
        run.add_card(cid, f"Deploy: {TARGETS[target]['title']}", "sf-deployer", "stage", "deploy")
        prompt = deploy_prompt(rd, target, site, name, [c for c in mine], env_names)
        try:
            run.run_agent_stage(cid, "sf-deployer", prompt, [out], lambda: validate_result(out, rd), tries=1, heal=False)
            result = json.loads(out.read_text(encoding="utf-8"))
            run.card_done(cid, str(result.get("summary", ""))[:200])
        except StageFailed as e:
            result = {"status": "failed", "summary": "The deploy did not finish.", "details": str(e)[:600], "next_steps": []}
    finally:
        run.stop_heartbeat()
    entry = {"target": target, "title": TARGETS[target]["title"], "status": result["status"], "url": result.get("url") or "",
             "path": result.get("path") or "", "summary": result.get("summary") or "", "details": str(result.get("details") or "")[:1200],
             "next_steps": [str(x) for x in (result.get("next_steps") or [])][:8], "at": time.time()}
    with run.lock:
        st.setdefault("deploys", []).append(entry)
        st["deploy"] = {"target": target, "status": entry["status"], "finished_at": entry["at"]}
        if prior_budget is None:
            st.pop("budget_usd", None)
        else:
            st["budget_usd"] = prior_budget
        run.save()
    run.event("deployed" if entry["status"] in ("deployed", "built", "handoff") else "deploy_" + entry["status"], cid, entry["summary"][:200])
    if run.notifies:
        factory.notify("Software Factory", f"{TARGETS[target]['title']}: {entry['summary'][:100]}")
    return entry
