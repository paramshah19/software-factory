"""The kinds of product the factory builds, and the driver's own checks for each (no agent is trusted to say "it works").

factory/app.json says which kind an app is (`type`). The original three are checked in checks.py / tools_browse.py:
    static   a web page or web app with no server        -> real browser screenshots at phone and desktop size
    server   a web app with a backend (pages + server)   -> started, then the same browser screenshots
    cli      a command-line tool                         -> its tests
This module adds:
    api      a backend with no screens (a REST/JSON API) -> started, then every declared request is made and checked
    worker   a bot or background job                     -> runs in fake mode for a while without crashing, and a scripted
                                                            conversation (`simulate`) must succeed
    library  a package other programs import             -> tests, a real package build, and every README example runs
    multi    several services (web + api + worker...)    -> all started in order and wired together (<NAME>_URL), each
                                                            checked by its own kind, the web one screenshotted
    mobile   a phone app (React Native with Expo)        -> its tests, then the web export is screenshotted at phone sizes
    desktop  a native desktop app (Python + Qt)          -> its tests, then every screen is rendered off-screen to a PNG
    godot    a game in the Godot engine                  -> imported and run headless with no script errors, plus its tests
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WEB_KINDS = ("static", "server")
NEW_KINDS = ("api", "worker", "library", "multi", "mobile", "desktop", "godot")
ALL_KINDS = WEB_KINDS + ("cli",) + NEW_KINDS
PHONE_SIZES = [[390, 844], [430, 932]]
DESKTOP_SIZES = [[1280, 800], [1024, 700]]
GODOT_NAMES = ("godot", "godot4", "Godot", "godot-4")

KIND_HELP = {
    "static": "a web page or web app with no server: one self-contained index.html",
    "server": "a web app with a backend: a Python web server (FastAPI or Flask) that also serves the pages",
    "cli": "a command-line tool run in a terminal",
    "api": "a backend with no screens (a JSON API), declared requests checked by the driver",
    "worker": "a bot (Discord, Slack, Telegram...) or a background job, with a fake platform and a scripted conversation",
    "library": "a Python package other programs import, built with uv and documented with runnable examples",
    "multi": "several services (for example web + api + worker), each in its own folder",
    "mobile": "a phone app for iPhone and Android in React Native with Expo (needs Node.js)",
    "desktop": "a native desktop app in Python with Qt (PySide6)",
    "godot": "a game in the Godot 4 engine (needs Godot installed)",
}


# --------------------------------------------------------------------------- what this computer can build


def find_godot(which: Callable = shutil.which) -> Optional[str]:
    env = os.environ.get("GODOT_BIN")
    if env and Path(env).exists():
        return env
    return next((p for p in (which(n) for n in GODOT_NAMES) if p), None)


def machine_kinds(which: Callable = shutil.which) -> dict[str, str]:
    """{kind: "" if it can be built and checked here, else the plain reason it can't}."""
    out = {k: "" for k in ALL_KINDS}
    if not which("node") or not which("npx"):
        out["mobile"] = "needs Node.js (nodejs.org)"
    if not find_godot(which):
        out["godot"] = "needs Godot 4 (godotengine.org), on the PATH or in the GODOT_BIN environment variable"
    return out


def capability_note(which: Callable = shutil.which) -> str:
    """One paragraph for the agents' shared protocol: which kinds they may choose on this computer."""
    kinds = machine_kinds(which)
    ok = [k for k, why in kinds.items() if not why]
    lines = ["### What this computer can build (choose the app's `type` from these)"]
    lines += [f"- `{k}`: {KIND_HELP[k]}" for k in ok]
    missing = [f"`{k}` ({why})" for k, why in kinds.items() if why]
    if missing:
        lines.append("NOT available here, so never choose: " + "; ".join(missing) + ". If the owner asked for one of these, build the closest "
                     "available kind (for a game: a browser game as `static`) and say so in your handoff.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- app.json rules for the new kinds


def validate(data: dict, app: Path, check_files: bool = True) -> list[str]:
    """Extra app.json rules for the new kinds (the original three are validated in checks.py)."""
    typ = data.get("type")
    errs: list[str] = []
    if typ == "api":
        errs += _need(data, "start", "the command that starts the API (it must read the PORT environment variable)")
        if not isinstance(data.get("port"), int):
            errs.append("api apps need an integer 'port'")
        errs += _api_checks_problems(data.get("api_checks"), "api_checks")
    elif typ == "worker":
        errs += _need(data, "start", "the command that runs the bot or job (in fake mode when its keys are missing)")
        errs += _need(data, "simulate", "a command that drives a scripted conversation or job through the worker in fake mode and exits 0")
    elif typ == "library":
        errs += _need(data, "build", "the command that builds the package, for example uv build --directory <app>")
        if not isinstance(data.get("examples"), list) or not data["examples"]:
            errs.append("library apps need 'examples': commands that run the README examples")
    elif typ == "multi":
        svcs = data.get("services")
        if not isinstance(svcs, list) or len(svcs) < 2:
            errs.append("multi apps need 'services': at least two {name, type, dir, start, ...}")
        else:
            names = set()
            for s in svcs:
                if not isinstance(s, dict) or not str(s.get("name", "")).replace("-", "").isalnum():
                    errs.append("each service needs a simple 'name' (letters, digits, dashes)")
                    continue
                names.add(s["name"])
                if s.get("type") not in ("server", "api", "worker", "static"):
                    errs.append(f"service {s['name']}: type must be server, api, worker or static")
                if s.get("type") != "static":
                    errs += [f"service {s['name']}: {e}" for e in _need(s, "start", "its start command")]
                if s.get("type") in ("server", "api") and not isinstance(s.get("port"), int):
                    errs.append(f"service {s['name']}: needs an integer 'port'")
                if s.get("type") == "api":
                    errs += [f"service {s['name']}: {e}" for e in _api_checks_problems(s.get("api_checks"), "api_checks")]
                if check_files and s.get("dir") and not (app / str(s["dir"])).is_dir():
                    errs.append(f"service {s['name']}: folder app/{s['dir']} does not exist")
            for s in svcs:
                for d in (s.get("depends_on") or []) if isinstance(s, dict) else []:
                    if d not in names:
                        errs.append(f"service {s.get('name')}: depends on unknown service {d!r}")
            web = data.get("web")
            if web and web not in names:
                errs.append(f"'web' names an unknown service {web!r}")
            if web and not data.get("screens"):
                errs.append("multi apps with a 'web' service need 'screens' (URL paths on that service)")
    elif typ == "mobile":
        errs += _need(data, "web_export", "the command that exports the web version, for example npx expo export --platform web --output-dir dist")
        if not data.get("screens"):
            errs.append("mobile apps need 'screens': [{\"name\": \"home\", \"path\": \"/\"}] (paths in the web export)")
    elif typ == "desktop":
        errs += _need(data, "screenshot", "a command that renders every screen off-screen to PNG files in {out_dir}")
        if not data.get("screens"):
            errs.append("desktop apps need 'screens': [{\"name\": \"main\"}]")
    elif typ == "godot":
        if check_files and not (app / "project.godot").exists():
            errs.append("godot apps need app/project.godot")
    return errs


def _need(d: dict, key: str, what: str) -> list[str]:
    return [] if d.get(key) else [f"needs '{key}': {what}"]


def _api_checks_problems(rows, key: str) -> list[str]:
    if not isinstance(rows, list) or not rows:
        return [f"needs '{key}': [{{\"name\": \"health\", \"method\": \"GET\", \"path\": \"/health\", \"expect_status\": 200}}]"]
    errs = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("name") or not str(r.get("path", "")).startswith("/") or not isinstance(r.get("expect_status"), int):
            errs.append(f"each entry of {key} needs a name, a path starting with / and an integer expect_status")
    return errs


# --------------------------------------------------------------------------- running things


def split(cmd) -> list[str]:
    """A command as a list, with the program resolved on the PATH (on Windows, npm and npx are .cmd files that a bare
    name doesn't find)."""
    args = [str(x) for x in cmd] if isinstance(cmd, list) else shlex.split(str(cmd), posix=(os.name != "nt"))
    if args and not os.path.isabs(args[0]):
        args[0] = shutil.which(args[0]) or args[0]
    return args


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
        else:
            proc.kill()
    except Exception:
        pass


def run(cmd, cwd: Path, timeout: int = 600, env: Optional[dict] = None) -> tuple[int, str]:
    try:
        p = subprocess.run(split(cmd), cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env, creationflags=NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as e:
        return 127, str(e)


def start(cmd, cwd: Path, env: dict, log: Path) -> subprocess.Popen:
    fh = open(log, "ab")
    return subprocess.Popen(split(cmd), cwd=str(cwd), env=env, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)


def wait_port(port: int, proc: subprocess.Popen, seconds: float = 45) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if port_open(port):
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.3)
    return False


def tail(path: Path, n: int = 600) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""


# --------------------------------------------------------------------------- per-kind checks


def api_requests(base: str, rows: list[dict], timeout: float = 15) -> tuple[list[dict], list[str]]:
    """Make each declared request; return (results, problems)."""
    results, problems = [], []
    for r in rows:
        method = str(r.get("method") or "GET").upper()
        body = json.dumps(r["json"]).encode() if "json" in r else None
        req = urllib.request.Request(base + r["path"], data=body, method=method,
                                     headers={"Content-Type": "application/json", **{str(k): str(v) for k, v in (r.get("headers") or {}).items()}})
        status, text = 0, ""
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status, text = resp.status, resp.read(20000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read(4000).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            text = str(e)
        ok = status == r["expect_status"]
        missing = []
        if ok and r.get("expect_keys"):
            try:
                data = json.loads(text)
                obj = data[0] if isinstance(data, list) and data else data
                missing = [k for k in r["expect_keys"] if not isinstance(obj, dict) or k not in obj]
            except json.JSONDecodeError:
                missing = list(r["expect_keys"])
            ok = not missing
        results.append({"name": r["name"], "method": method, "path": r["path"], "status": status, "ok": ok})
        if not ok:
            why = f"returned {status}, expected {r['expect_status']}" if status != r["expect_status"] else f"response is missing {', '.join(missing)}"
            problems.append(f"API check '{r['name']}' ({method} {r['path']}) {why}: {' '.join(text.split())[:160]}")
    return results, problems


def check_api(app: Path, cfg: dict, logs: Path, env: dict) -> tuple[dict, list[str]]:
    port = int(cfg.get("port") or free_port())
    if port_open(port):
        port = free_port()
    log = logs / "check-api.log"
    proc = start(cfg["start"], app, {**env, "PORT": str(port)}, log)
    try:
        if not wait_port(port, proc, float(cfg.get("start_timeout", 45))):
            return {"ok": False}, [f"the API did not start on port {port}: {tail(log, 400)}"]
        results, problems = api_requests(f"http://127.0.0.1:{port}", cfg["api_checks"])
        return {"ok": not problems, "requests": results}, problems
    finally:
        kill(proc)


def check_worker(app: Path, cfg: dict, logs: Path, env: dict) -> tuple[dict, list[str]]:
    problems = []
    log = logs / "check-worker.log"
    seconds = float(cfg.get("run_seconds", 8))
    proc = start(cfg["start"], app, env, log)
    time.sleep(seconds)
    alive = proc.poll() is None
    kill(proc)
    out = tail(log, 4000)
    if not alive and proc.returncode not in (0, None):
        problems.append(f"the worker crashed within {seconds:.0f}s (exit {proc.returncode}): {out[-400:]}")
    if "Traceback (most recent call last)" in out:
        problems.append("the worker printed an error while running in fake mode: " + out[out.index("Traceback"):][:400])
    rc, sim = run(cfg["simulate"], app, 300, env)
    if rc != 0:
        problems.append(f"the scripted conversation (simulate) failed (exit {rc}): {sim[-400:]}")
    return {"ok": not problems, "stayed_up": alive, "simulate": {"rc": rc, "tail": sim[-800:]}}, problems


def check_library(app: Path, cfg: dict, logs: Path, env: dict) -> tuple[dict, list[str]]:
    problems = []
    rc, out = run(cfg["build"], app, 600, env)
    built = sorted(p.name for p in (app / "dist").glob("*")) if (app / "dist").is_dir() else []
    if rc != 0:
        problems.append(f"the package does not build (exit {rc}): {out[-400:]}")
    elif not any(n.endswith((".whl", ".tar.gz", ".tgz")) for n in built):
        problems.append("the build ran but produced no package in app/dist/")
    examples = []
    for cmd in cfg.get("examples") or []:
        erc, eout = run(cmd, app, 300, env)
        examples.append({"cmd": str(cmd)[:160], "rc": erc})
        if erc != 0:
            problems.append(f"README example fails ({cmd}): {eout[-300:]}")
    return {"ok": not problems, "dist": built, "examples": examples}, problems


def check_godot(app: Path, cfg: dict, logs: Path, env: dict, which: Callable = shutil.which) -> tuple[dict, list[str]]:
    exe = find_godot(which)
    if not exe:
        return {"ok": False}, ["Godot is not installed on this computer, so the game could not be run (install Godot 4 and add it to the PATH)"]
    problems = []
    rc, imp = run([exe, "--headless", "--path", str(app), "--import"], app, 600, env)
    frames = int(float(cfg.get("smoke_seconds", 5)) * 60)
    rc2, out = run([exe, "--headless", "--path", str(app), "--quit-after", str(frames)], app, 300, env)
    errors = [l for l in (imp + "\n" + out).splitlines() if "SCRIPT ERROR" in l or l.startswith("ERROR:") or "Parse Error" in l]
    if errors:
        problems.append("the game reports errors when run: " + " | ".join(errors[:4])[:500])
    if rc2 not in (0,) and not errors:
        problems.append(f"the game did not run cleanly (exit {rc2}): {out[-300:]}")
    return {"ok": not problems, "run_rc": rc2, "errors": errors[:10]}, problems


def mobile_screens(app: Path, cfg: dict, shots: Path, capture: Callable, env: dict) -> tuple[dict, list[str]]:
    """Export the web version of the phone app, serve it, and screenshot it at phone sizes."""
    rc, out = run(cfg["web_export"], app, 900, env)
    web = app / str(cfg.get("web_dir") or "dist")
    if rc != 0 or not (web / "index.html").exists():
        return {"ok": False, "error": f"web export failed (exit {rc}): {out[-300:]}"}, [f"the web export of the phone app failed: {out[-300:]}"]
    port = free_port()
    shot_cfg = {"type": "server", "start": [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"], "port": port,
                "screens": cfg["screens"]}
    res = capture(web, shot_cfg, shots, PHONE_SIZES)
    return res, []


def system_font_dir() -> str:
    for d in (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts", Path("/System/Library/Fonts"), Path("/Library/Fonts"),
              Path("/usr/share/fonts"), Path("/usr/local/share/fonts")):
        if d.is_dir():
            return d.as_posix()
    return ""


def desktop_screens(app: Path, cfg: dict, shots: Path, env: dict) -> tuple[dict, list[str]]:
    """Ask the app to render each screen off-screen (Qt's 'offscreen' platform), then collect the PNGs."""
    shots.mkdir(parents=True, exist_ok=True)
    sizes = ",".join(f"{w}x{h}" for w, h in DESKTOP_SIZES)
    cmd = [str(c).replace("{out_dir}", shots.as_posix()) for c in split(cfg["screenshot"])]
    qenv = {**env, "QT_QPA_PLATFORM": "offscreen", "FACTORY_SHOT_DIR": shots.as_posix(), "FACTORY_SIZES": sizes}
    if not qenv.get("QT_QPA_FONTDIR") and system_font_dir():  # off-screen Qt finds no fonts by itself: every letter becomes a box
        qenv["QT_QPA_FONTDIR"] = system_font_dir()
    rc, out = run(cmd, app, 300, qenv)
    screens, problems = [], []
    if rc != 0:
        problems.append(f"the screenshot command failed (exit {rc}): {out[-400:]}")
    for s in cfg["screens"]:
        for w, h in DESKTOP_SIZES:
            png = shots / f"{s['name']}-{w}.png"
            entry = {"name": s["name"], "size": [w, h], "png": png.name if png.exists() else None, "overflow": False,
                     "console_errors": [], "failed_requests": []}
            if not png.exists():
                entry["load_error"] = f"no screenshot {png.name} was written"
            elif png.stat().st_size < 1500:
                entry["load_error"] = "the screenshot is blank"
            screens.append(entry)
    if "Traceback (most recent call last)" in out:
        problems.append("the app printed an error while rendering: " + out[out.index("Traceback"):][:400])
    return {"ok": True, "screens": screens}, problems


def check_multi(app: Path, cfg: dict, logs: Path, env: dict, shots: Path, capture: Callable) -> tuple[dict, list[str]]:
    """Start the services in dependency order, give each the others' addresses, check each, screenshot the web one."""
    svcs = {s["name"]: dict(s) for s in cfg["services"]}
    order, seen = [], set()

    def visit(n, stack=()):
        if n in seen or n in stack:
            return
        for d in svcs[n].get("depends_on") or []:
            visit(d, stack + (n,))
        seen.add(n)
        order.append(n)
    for n in svcs:
        visit(n)
    for s in svcs.values():
        if s["type"] in ("server", "api"):
            s["port"] = s["port"] if not port_open(int(s["port"])) else free_port()
    urls = {f"{n.upper().replace('-', '_')}_URL": f"http://127.0.0.1:{s['port']}" for n, s in svcs.items() if s["type"] in ("server", "api")}
    senv = {**env, **urls}
    procs, problems, results = [], [], {}
    web = cfg.get("web")
    try:
        for n in order:
            s = svcs[n]
            if n == web or s["type"] == "static":
                continue  # the browser check starts the web service itself
            sdir = app / str(s.get("dir") or ".")
            log = logs / f"check-svc-{n}.log"
            proc = start(s["start"], sdir, {**senv, "PORT": str(s.get("port", ""))}, log)
            procs.append(proc)
            if s["type"] in ("server", "api"):
                if not wait_port(int(s["port"]), proc, float(s.get("start_timeout", 45))):
                    problems.append(f"service {n} did not start on port {s['port']}: {tail(log, 300)}")
                    continue
            if s["type"] == "api":
                rows, probs = api_requests(f"http://127.0.0.1:{s['port']}", s["api_checks"])
                results[n] = {"requests": rows}
                problems += [f"service {n}: {p}" for p in probs]
            if s["type"] == "worker":
                time.sleep(float(s.get("run_seconds", 5)))
                if proc.poll() not in (None, 0):
                    problems.append(f"service {n} (worker) crashed: {tail(log, 300)}")
        screens = {"ok": True, "screens": [], "note": "no web service"}
        if web and not problems:
            w = svcs[web]
            wcfg = {"type": "static" if w["type"] == "static" else "server", "start": w.get("start"), "port": w.get("port"),
                    "entry": w.get("entry", "index.html"), "screens": cfg["screens"]}
            screens = capture(app / str(w.get("dir") or "."), wcfg, shots, None, senv)
        return {"ok": not problems, "services": results, "screens": screens}, problems
    finally:
        for p in procs:
            kill(p)
