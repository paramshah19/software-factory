"""One helper for the two skills: /software-factory-setup and /software-factory-idea.

Standard library only, so any Python 3.10+ can run it (`uv run --no-project python scripts/sf.py ...` works everywhere).
Every command prints plain text, or JSON with --json, so an agent can read the result.

Setup (once):
    sf.py doctor                      what is installed: system, git, uv, agent command lines, browser for checks
    sf.py configure --engine claude   which agent runs the stations (claude | codex | custom) and which agent hosts the skills
    sf.py prepare                     install the factory's Python packages and a browser for the screenshot checks
    sf.py agents                      every station: its job, model and effort, and the model it gets on your engine
    sf.py agents set sf-planner --model claude-opus-5-5 --effort high    choose an agent's model / effort yourself
    sf.py check                       ask every model + effort the stations use to answer (proves the agents work)
    sf.py install-skills              put both skills where your agent finds them
    sf.py connections                 what deploying can use (Claude connectors, GitHub / Netlify / Vercel command lines)

Every day:
    sf.py start                       the one-click way in: install on first start, start the dashboard, open it as an app window
    sf.py start --browser --phone     ...in your normal browser, with the phone view on (same Wi-Fi, PIN)
    sf.py shortcut                    put a Software Factory icon on your Desktop (double-click = sf.py start)
    sf.py phone on|off|status         the dashboard on your phone; sf.py awake on|off|status keeps the computer awake
    sf.py idea "a habit tracker"      start a run, start the dashboard hidden if needed, open it
    sf.py dashboard start|stop|status|open
    sf.py status [RUN]                where a run is / what is waiting on you
    sf.py factory <args>              pass anything else to factory.py (approve, reject, decide, run, stats, deploy)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
HOME_DIR = Path(os.environ.get("SF_HOME") or (Path.home() / ".software-factory"))
SKILLS = ("software-factory-setup", "software-factory-idea")
DEFAULT_PORT = 8787
AGENT_CLIS = {  # command-line names of coding agents setup can look for
    "claude": "Claude Code", "codex": "Codex", "gemini": "Gemini CLI", "grok": "Grok CLI", "qwen": "Qwen Code",
    "cursor-agent": "Cursor agent", "opencode": "opencode", "aider": "Aider", "copilot": "GitHub Copilot CLI",
}
HOSTS = {  # where each agent looks for user-level skills
    "claude-code": Path.home() / ".claude" / "skills",
    "codex": Path.home() / ".agents" / "skills",
    "gemini": Path.home() / ".gemini" / "skills",
}
WIN = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- config


def config_path() -> Path:
    return Path(os.environ.get("SF_CONFIG") or (HOME_DIR / "config.json"))


def load_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def factory_dir(cfg: Optional[dict] = None) -> Path:
    cfg = cfg if cfg is not None else load_config()
    return Path(cfg.get("factory_dir") or REPO)


def port(cfg: Optional[dict] = None) -> int:
    cfg = cfg if cfg is not None else load_config()
    return int(cfg.get("port") or DEFAULT_PORT)


def out(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            print(f"{k}: {json.dumps(v) if isinstance(v, (dict, list)) else v}")
    else:
        print(obj)


# --------------------------------------------------------------------------- helpers


def venv_python(fdir: Path) -> Optional[Path]:
    p = fdir / ".venv" / ("Scripts/python.exe" if WIN else "bin/python")
    return p if p.exists() else None


def run(args: list, cwd: Path, timeout: int = 900, input_text: Optional[str] = None) -> tuple[int, str]:
    try:
        p = subprocess.run([str(a) for a in args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, input=input_text, creationflags=NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, str(e)


def version_of(exe: str) -> str:
    rc, text = run([exe, "--version"], Path.cwd(), 60)
    return (text.strip().splitlines() or [""])[0][:80] if rc == 0 else ""


def http(path: str, body: Optional[dict] = None, timeout: float = 5.0, p: Optional[int] = None):
    url = f"http://127.0.0.1:{p or port()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "X-SF-Dashboard": "1"} if data else {})  # the dashboard's CSRF guard
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except json.JSONDecodeError:
            return e.code, {"error": str(e)}
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return 0, {}


def detect_host() -> str:
    """Best guess at which agent is running this script (the one that will host the skills)."""
    env = os.environ
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"
    if any(k.startswith("CODEX_") for k in env):
        return "codex"
    if env.get("GEMINI_CLI"):
        return "gemini"
    return "other"


# --------------------------------------------------------------------------- doctor


def browser_ok(fdir: Path) -> tuple[bool, str]:
    py = venv_python(fdir)
    if not py:
        return False, "the factory's packages are not installed yet (run: sf.py prepare)"
    code = ("from playwright.sync_api import sync_playwright\n"
            "with sync_playwright() as p:\n"
            "    for ch in ('msedge', 'chrome', None):\n"
            "        try:\n"
            "            b = p.chromium.launch(channel=ch, headless=True) if ch else p.chromium.launch(headless=True)\n"
            "            b.close(); print('BROWSER', ch or 'chromium'); break\n"
            "        except Exception:\n"
            "            pass\n")
    rc, text = run([py, "-c", code], fdir, 120)
    for line in text.splitlines():
        if line.startswith("BROWSER "):
            return True, line.split(" ", 1)[1]
    return False, "no Edge, Chrome or Playwright Chromium (run: sf.py prepare)"


def cmd_doctor(a) -> int:
    cfg, fdir = load_config(), factory_dir()
    tools = {}
    for t in ("git", "uv"):
        exe = shutil.which(t)
        tools[t] = {"found": bool(exe), "version": version_of(exe) if exe else ""}
    agents = {}
    for exe_name, label in AGENT_CLIS.items():
        exe = shutil.which(exe_name)
        if exe:
            agents[exe_name] = {"label": label, "version": version_of(exe)}
    ok_browser, which_browser = browser_ok(fdir)
    status, info = http("/api/info", timeout=2)
    report = {
        "system": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": sys.version.split()[0],
        "factory_dir": fdir.as_posix(), "factory_found": (fdir / "src" / "factory.py").exists(),
        "config": config_path().as_posix() if config_path().exists() else "not configured yet",
        "engine": cfg.get("engine") or "not chosen", "host": cfg.get("host") or detect_host(),
        "tools": tools, "agent_command_lines": agents,
        "packages_installed": bool(venv_python(fdir)), "browser_for_checks": which_browser if ok_browser else f"MISSING: {which_browser}",
        "dashboard": f"running at http://127.0.0.1:{port()}" if status == 200 and info.get("app") == "software-factory" else "not running",
    }
    if report["factory_found"]:
        sys.path.insert(0, str(fdir / "src"))
        import kinds  # noqa: E402  (the factory's own list of product kinds and what each needs)
        report["can_build"] = {k: ("yes" if not why else why) for k, why in kinds.machine_kinds().items()}
    missing = [t for t, v in tools.items() if not v["found"]]
    report["ready"] = not missing and bool(agents) and report["factory_found"]
    if missing:
        report["install_steps"] = {
            "git": "https://git-scm.com/downloads" if "git" in missing else None,
            "uv": ("powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"" if WIN
                   else "curl -LsSf https://astral.sh/uv/install.sh | sh") if "uv" in missing else None}
    out(report, a.json)
    return 0 if report["ready"] else 1


# --------------------------------------------------------------------------- configure


def cmd_configure(a) -> int:
    cfg = load_config()
    cfg["factory_dir"] = Path(a.factory_dir).resolve().as_posix() if a.factory_dir else cfg.get("factory_dir") or REPO.as_posix()
    if not (Path(cfg["factory_dir"]) / "src" / "factory.py").exists():
        print(f"no src/factory.py in {cfg['factory_dir']}")
        return 1
    if a.engine:
        cfg["engine"] = a.engine
    cfg.setdefault("engine", "claude")
    cfg["host"] = a.host or cfg.get("host") or detect_host()
    if a.port:
        cfg["port"] = a.port
    if a.quality:
        cfg["quality"] = a.quality
    for mode, val in (("single", a.budget_single), ("multi", a.budget_multi)):
        if val is not None:
            cfg.setdefault("budget", {})[mode] = max(0.0, min(500.0, val))
    if a.notify:
        cfg["notify"] = a.notify == "on"
    if a.open_as:
        cfg["open_as"] = a.open_as
    if a.brand_file:  # becomes an always-use document: every agent of every run reads it
        text = Path(a.brand_file).read_text(encoding="utf-8").strip()
        dest = Path(cfg["factory_dir"]) / "workspace" / "always-use" / "brand.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("# Brand rules (every app follows these)\n\n" + text + "\n", encoding="utf-8")
        cfg["brand_doc"] = dest.as_posix()
    if a.models:
        models = dict(cfg.get("models") or {})
        for pair in a.models.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k.strip() in ("strong", "standard", "fast"):
                    models[k.strip()] = v.strip()
        cfg["models"] = models
    if cfg["engine"] == "custom":
        custom = dict(cfg.get("custom") or {})
        for key, val in (("command", a.command), ("model_args", a.model_args), ("effort_args", a.effort_args)):
            if val:
                try:
                    parsed = json.loads(val)
                except json.JSONDecodeError:
                    print(f"--{key.replace('_', '-')} must be a JSON list, like '[\"gemini\", \"--yolo\"]'")
                    return 1
                if not isinstance(parsed, list) or not parsed:
                    print(f"--{key.replace('_', '-')} must be a non-empty JSON list")
                    return 1
                custom[key] = [str(x) for x in parsed]
        if a.prompt:
            custom["prompt"] = a.prompt
        if a.label:
            custom["label"] = a.label
        if not custom.get("command"):
            print("the custom engine needs --command, a JSON list: the agent's non-interactive command, e.g. '[\"gemini\", \"--yolo\"]'")
            return 1
        cfg["custom"] = custom
    save_config(cfg)
    out({"saved": config_path().as_posix(), **cfg}, a.json)
    return 0


# --------------------------------------------------------------------------- prepare / agents / check


def cmd_prepare(a) -> int:
    fdir = factory_dir()
    if not shutil.which("uv"):
        print("uv is missing: run `sf.py doctor` for the install command")
        return 1
    rc, text = run(["uv", "sync"], fdir, 900)
    print(("ok  " if rc == 0 else "FAIL ") + "Python packages (uv sync)" + ("" if rc == 0 else ": " + text.strip()[-400:]))
    if rc:
        return 1
    ok, which = browser_ok(fdir)
    if not ok:
        print("installing Playwright's Chromium for the screenshot checks (about 150 MB, once)...")
        rc, text = run(["uv", "run", "playwright", "install", "chromium"], fdir, 1800)
        ok, which = browser_ok(fdir)
    print(("ok  " if ok else "FAIL ") + f"browser for checks: {which}")
    return 0 if ok else 1


def factory_py(args: list, timeout: int = 3600, capture: bool = True) -> tuple[int, str]:
    fdir = factory_dir()
    py = venv_python(fdir)
    if not py:
        return 1, "the factory's packages are not installed: run `sf.py prepare` first"
    env = dict(os.environ, SF_CONFIG=str(config_path()))
    if not capture:
        return subprocess.call([str(py), str(fdir / "src" / "factory.py"), *args], cwd=str(fdir), env=env), ""
    try:
        p = subprocess.run([str(py), str(fdir / "src" / "factory.py"), *args], cwd=str(fdir), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env, creationflags=NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out"


EFFORTS = ("low", "medium", "high", "xhigh", "max")


def cmd_agents(a) -> int:
    """List every agent, or set / reset the owner's own model and effort for one (saved in config.json, not agents/*.md)."""
    fdir = factory_dir()
    names = sorted(f.stem for f in (fdir / "agents").glob("sf-*.md"))
    if a.action in ("set", "reset"):
        cfg = load_config()
        mine = dict(cfg.get("agents") or {})
        if a.action == "reset":
            if a.name == "all":
                mine = {}
            elif a.name in names:
                mine.pop(a.name, None)
            else:
                print(f"unknown agent {a.name!r}: one of {', '.join(names)}, or 'all'")
                return 1
        else:
            if a.name not in names:
                print(f"unknown agent {a.name!r}: one of {', '.join(names)}")
                return 1
            if not (a.model or a.effort):
                print("give --model and/or --effort")
                return 1
            entry = dict(mine.get(a.name) or {})
            if a.model:
                entry["model"] = a.model.strip()
            if a.effort:
                entry["effort"] = a.effort
            mine[a.name] = entry
        cfg["agents"] = mine
        save_config(cfg)
    sys.path.insert(0, str(fdir / "src"))
    os.environ["SF_CONFIG"] = str(config_path())
    import common  # noqa: E402  (the factory's own role parser, which applies the overrides)
    import engines  # noqa: E402
    eng = engines.current()
    rows = []
    for n in names:
        r = common.load_role(n, fdir / "agents")
        job = next((l.strip("# ").strip() for l in r.body.splitlines() if l.startswith("# Role:")), "")
        rows.append({"agent": r.name, "job": job.replace("Role: ", ""), "model": r.model, "effort": r.effort,
                     "on_your_engine": eng.model_label(r.model) if eng else r.model, "yours": r.overridden,
                     "lean": "{} @ {}".format(*r.presets["lean"]) if (r.presets or {}).get("lean") else "",
                     "max": "{} @ {}".format(*r.presets["max"]) if (r.presets or {}).get("max") else "",
                     "retry": f"{r.retry_model} @ {r.retry_effort or r.effort}" if r.retry_model else ""})
    if a.json:
        out({"engine": engines.describe(), "agents": rows}, True)
    else:
        print(f"engine: {engines.describe()['label']} · {len(rows)} agents  (Balanced quality; * = your own setting)")
        for r in rows:
            print(f"  {r['agent']:16}{'*' if r['yours'] else ' '} {r['on_your_engine'][:34]:34} effort {r['effort']:6}  {r['job'][:56]}")
        print("change one: sf.py agents set <agent> --model <model> --effort <low|medium|high|xhigh|max>   ·   undo: sf.py agents reset <agent|all>")
    return 0


def cmd_check(a) -> int:
    rc, text = factory_py(["check"], 3600)
    print(text.strip())
    return rc


# --------------------------------------------------------------------------- skills


def skill_targets(host: str, custom_dir: Optional[str]) -> list[Path]:
    if custom_dir:
        return [Path(custom_dir).expanduser()]
    return [HOSTS[host]] if host in HOSTS else []


def write_shim() -> Path:
    """~/.software-factory/sf.py: a fixed path the idea skill can always call, pointing at this factory."""
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    shim = HOME_DIR / "sf.py"
    shim.write_text(
        '"""Shortcut to the Software Factory helper. Written by `sf.py install-skills`; safe to delete (setup recreates it)."""\n'
        "import json, os, runpy, sys\nfrom pathlib import Path\n\n"
        "here = Path(__file__).resolve().parent\n"
        "cfg = json.loads((here / 'config.json').read_text(encoding='utf-8'))\n"
        "os.environ.setdefault('SF_CONFIG', str(here / 'config.json'))\n"
        "target = Path(cfg['factory_dir']) / 'scripts' / 'sf.py'\n"
        "sys.argv[0] = str(target)\n"
        "runpy.run_path(str(target), run_name='__main__')\n", encoding="utf-8")
    return shim


def cmd_install_skills(a) -> int:
    cfg = load_config()
    if not cfg.get("factory_dir"):
        print("run `sf.py configure` first, so the skills know where the factory is")
        return 1
    host = a.host or cfg.get("host") or detect_host()
    targets = skill_targets(host, a.dir)
    shim = write_shim()
    if not targets:
        out({"installed": [], "shim": shim.as_posix(), "next": f"{host!r} has no known skills folder. Copy these folders into your agent's "
             f"skills folder, or pass --dir: " + ", ".join((REPO / 'skills' / s).as_posix() for s in SKILLS)}, a.json)
        return 2
    done = []
    for root in targets:
        for s in SKILLS:
            dest = root / s
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(REPO / "skills" / s, dest)
            done.append(dest.as_posix())
    cfg["host"] = host
    save_config(cfg)
    out({"installed": done, "shim": shim.as_posix()}, a.json)
    return 0


# --------------------------------------------------------------------------- connections


def cmd_connections(a) -> int:
    """What deploying can use. Reads only status lines; never prints or reads tokens."""
    rows = {}
    cfg = load_config()
    if (cfg.get("engine") or "claude") == "claude" and shutil.which("claude"):
        rc, text = run(["claude", "mcp", "list"], Path.cwd(), 120)
        conns = []
        for line in text.splitlines():
            low = line.lower()
            if any(k in low for k in ("netlify", "vercel", "github")) and ":" in line:
                name = line.split(":", 1)[0].strip()
                conns.append({"name": name, "connected": "connected" in low and "not connected" not in low and "fail" not in low})
        rows["claude_connectors"] = conns
    gh = shutil.which("gh")
    if gh:
        rc, text = run([gh, "auth", "status"], Path.cwd(), 60)
        rows["github_cli"] = "signed in" if rc == 0 else "installed, not signed in (run: gh auth login)"
    else:
        rows["github_cli"] = "not installed (only needed for GitHub Pages)"
    for name in ("netlify", "vercel"):
        rows[f"{name}_cli"] = "installed" if shutil.which(name) else "not installed (the deployer can use npx, or a connector)"
    rows["node"] = version_of(shutil.which("node")) if shutil.which("node") else "not installed (needed for Netlify/Vercel CLIs and desktop apps)"
    rows["note"] = "Deploying is optional and always asks you first. Sign-ins are yours to do; the factory never handles passwords or tokens."
    out(rows, a.json)
    return 0


# --------------------------------------------------------------------------- dashboard


def dashboard_status(p: Optional[int] = None) -> dict:
    status, info = http("/api/info", timeout=2, p=p)
    if status == 200 and info.get("app") == "software-factory":
        same = Path(info.get("factory_dir", "")).resolve() == factory_dir().resolve()
        return {"running": True, "ours": same, "factory_dir": info.get("factory_dir"), "url": f"http://127.0.0.1:{p or port()}"}
    status2, _ = http("/api/state", timeout=2, p=p)
    if status2 == 200:  # an older dashboard without /api/info
        return {"running": True, "ours": False, "factory_dir": "unknown (older dashboard)", "url": f"http://127.0.0.1:{p or port()}"}
    return {"running": False, "port_busy": port_busy(p or port()), "url": f"http://127.0.0.1:{p or port()}"}


def port_busy(p: int) -> bool:
    """Is some other program already listening on this port?"""
    import socket
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", p)) == 0


def start_hidden(args: list, cwd: Path, log: Path) -> int:
    """Start a background process with no terminal window that outlives this script. Returns its pid."""
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "ab")
    kw = dict(cwd=str(cwd), stdout=fh, stderr=fh, stdin=subprocess.DEVNULL, close_fds=True, env=dict(os.environ, PYTHONUNBUFFERED="1"))
    if WIN:
        # CREATE_NO_WINDOW (not DETACHED_PROCESS): children inherit the hidden console instead of opening new windows.
        # BREAKAWAY_FROM_JOB lets it outlive the agent's shell when that shell runs inside a job object.
        base = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(args, creationflags=base | 0x01000000, **kw).pid
        except OSError:
            return subprocess.Popen(args, creationflags=base, **kw).pid
    return subprocess.Popen(args, start_new_session=True, **kw).pid


def browser_exe() -> Optional[str]:
    """A Chromium-family browser (Edge, Chrome, Chromium) that can open the dashboard as its own app window, or None."""
    names = ("msedge", "chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge")
    if WIN:
        roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
        for root in filter(None, roots):
            for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
                if (Path(root) / rel).exists():
                    return str(Path(root) / rel)
    elif sys.platform == "darwin":
        for app in ("Microsoft Edge", "Google Chrome", "Chromium"):
            exe = Path(f"/Applications/{app}.app/Contents/MacOS/{app}")
            if exe.exists():
                return str(exe)
    for n in names:
        exe = shutil.which(n)
        if exe:
            return exe
    return None


def open_window(url: str, mode: Optional[str] = None) -> str:
    """Open the dashboard: as its own app window (no tabs or address bar) or in your normal browser. Returns what it did.
    mode: "app" or "browser"; default from `sf.py configure --open-as`, else app (falls back to the browser when there is no Chromium browser)."""
    mode = mode or load_config().get("open_as") or "app"
    exe = browser_exe() if mode == "app" else None
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1360,900"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=not WIN)
            return "app window"
        except OSError:
            pass
    webbrowser.open(url)
    return "browser"


def ensure_dashboard(open_browser: bool = False, path: str = "/", extra_args: Optional[list] = None) -> tuple[int, dict]:
    st, note = dashboard_status(), None
    if st["running"] and not st["ours"]:
        return 1, {"error": f"port {port()} is used by another Software Factory dashboard ({st['factory_dir']}). "
                            f"Stop it, or pick another port: sf.py configure --port 8790"}
    if not st["running"] and st.get("port_busy"):  # another program has the port: move to the next free one
        cfg = load_config()
        free = next((q for q in range(port() + 1, port() + 30) if not port_busy(q)), None)
        if free is None:
            return 1, {"error": f"port {port()} and the next ones are busy: pick one with sf.py configure --port N"}
        moved_from = port()
        cfg["port"] = free
        save_config(cfg)
        st = dashboard_status()
        note = f"port {moved_from} is used by another program, so the dashboard uses port {free} (saved)"
    if not st["running"]:
        fdir = factory_dir()
        py = venv_python(fdir)
        if not py:
            return 1, {"error": "the factory's packages are not installed: run `sf.py prepare` first"}
        env_args = [str(py), str(fdir / "src" / "dashboard.py"), "--port", str(port()), *(extra_args or [])]
        os.environ["SF_CONFIG"] = str(config_path())  # the dashboard and every run it starts use this config
        pid = start_hidden(env_args, fdir, HOME_DIR / "dashboard.log")
        (HOME_DIR / "dashboard.pid").write_text(str(pid), encoding="utf-8")
        for _ in range(60):
            time.sleep(0.5)
            st = dashboard_status()
            if st["running"]:
                break
        if not st["running"]:
            tail = (HOME_DIR / "dashboard.log").read_text(encoding="utf-8", errors="replace")[-600:] if (HOME_DIR / "dashboard.log").exists() else ""
            return 1, {"error": "the dashboard did not start", "log": tail}
        st["started"] = True
    if note:
        st["note"] = note
    if open_browser:
        st["opened"] = st["url"] + path
        st["opened_in"] = open_window(st["opened"])
    return 0, st


def stop_dashboard() -> dict:
    pid_file = HOME_DIR / "dashboard.pid"
    if not pid_file.exists():
        return {"stopped": False, "why": "no dashboard started by sf.py (nothing to stop)"}
    pid = int(pid_file.read_text(encoding="utf-8").strip() or 0)
    try:
        if WIN:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    pid_file.unlink(missing_ok=True)
    time.sleep(0.5)
    return {"stopped": True, "pid": pid, "note": "runs in progress keep going: each run has its own background process"}


def cmd_dashboard(a) -> int:
    if a.action == "status":
        out(dashboard_status(), a.json)
        return 0
    if a.action == "stop":
        out(stop_dashboard(), a.json)
        return 0
    rc, st = ensure_dashboard(open_browser=(a.action == "open" or not a.no_open))
    out(st, a.json)
    return rc


# --------------------------------------------------------------------------- start (the one-click way in), shortcut, phone, awake


def ensure_prepared() -> tuple[bool, str]:
    """First start only: install the factory's Python packages. After that this is instant."""
    fdir = factory_dir()
    if venv_python(fdir):
        return True, ""
    if not shutil.which("uv"):
        return False, "uv is missing. Install it from https://docs.astral.sh/uv/getting-started/installation/ and start again."
    print("First start: installing the factory's packages (about a minute, once)...", flush=True)
    rc, text = run(["uv", "sync"], fdir, 900)
    if rc != 0 or not venv_python(fdir):
        return False, "could not install the packages:\n" + text.strip()[-600:]
    return True, ""


def phone_info() -> dict:
    status, r = http("/api/remote", timeout=5)
    if status != 200 or not r.get("on"):
        return {"on": False}
    return {"on": True, "addresses": r.get("urls") or [], "pin": r.get("pin"), "link": r.get("link"), "note": r.get("note") or ""}


def cmd_start(a) -> int:
    """One command (and one click, through the launchers) from nothing to the dashboard on screen."""
    ok, why = ensure_prepared()
    if not ok:
        print(why)
        return 1
    extra = (["--phone"] if a.phone else []) + (["--awake"] if a.awake else [])
    rc, st = ensure_dashboard(extra_args=extra)
    if rc:
        out(st, a.json)
        return rc
    if not st.get("started"):   # it was already running: apply the options through its own API
        if a.phone:
            http("/api/remote", {"on": True})
        if a.awake:
            http("/api/awake", {"on": True, "mode": "runs"})
    opened = None if a.no_open else open_window(st["url"], "browser" if a.browser else "app" if a.app else None)
    info = {"dashboard": st["url"], "opened_in": opened or "not opened", "started_now": bool(st.get("started"))}
    if a.phone:
        info["phone"] = phone_info()
    if a.json:
        out(info, True)
        return 0
    print(f"Software Factory is {'running' if not st.get('started') else 'started'}: {st['url']}" + (f" (opened as {opened})" if opened else ""))
    ph = info.get("phone")
    if ph and ph.get("on") and ph.get("addresses"):
        print(f"Phone view (same Wi-Fi): open {ph['addresses'][0]} and type the PIN {ph['pin']}, or scan the QR code in Phone & power.")
    elif ph and ph.get("on"):
        print(ph.get("note") or "Phone view is on, but no local network address was found.")
    return 0


def desktop_dir(custom: Optional[str] = None) -> Path:
    if custom:
        return Path(custom).expanduser()
    if WIN:   # the real Desktop folder (it moves when OneDrive backs it up)
        rc, text = run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"], Path.cwd(), 30)
        lines = text.strip().splitlines()
        if rc == 0 and lines and Path(lines[-1]).is_dir():
            return Path(lines[-1])
    return Path.home() / "Desktop"


def cmd_shortcut(a) -> int:
    """A Desktop icon that starts the factory with one double-click."""
    target = factory_dir() / ("Start Software Factory.cmd" if WIN else "Start Software Factory.command")
    desk = desktop_dir(a.dir)
    if not target.exists():
        print(f"launcher not found: {target}")
        return 1
    if not desk.is_dir():
        print(f"no Desktop folder at {desk}: pass --dir FOLDER")
        return 1
    if WIN:
        lnk = desk / "Software Factory.lnk"
        q = lambda p: str(p).replace("'", "''")
        script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}');$s.TargetPath='{q(target)}';"
                  f"$s.WorkingDirectory='{q(target.parent)}';$s.WindowStyle=7;$s.Description='Start the Software Factory';$s.Save()")
        rc, text = run(["powershell", "-NoProfile", "-Command", script], Path.cwd(), 60)
        made = lnk
        if rc != 0 or not lnk.exists():
            print("could not create the shortcut: " + text.strip()[-300:])
            return 1
    elif sys.platform == "darwin":
        made = desk / "Software Factory.command"
        made.unlink(missing_ok=True)
        made.symlink_to(target)
    else:
        made = desk / "Software Factory.desktop"
        made.write_text("[Desktop Entry]\nType=Application\nName=Software Factory\nComment=Start the Software Factory\n"
                        f'Exec=bash "{target}"\nPath={target.parent}\nTerminal=false\n', encoding="utf-8")
        made.chmod(0o755)
    out({"shortcut": made.as_posix(), "next": "Double-click it to start the Software Factory."}, a.json)
    return 0


def cmd_phone(a) -> int:
    """Phone view on/off: the dashboard on your phone, same Wi-Fi, PIN protected."""
    if a.action == "on":
        ok, why = ensure_prepared()
        if not ok:
            print(why)
            return 1
        rc, st = ensure_dashboard(extra_args=["--phone"])
        if rc:
            out(st, a.json)
            return rc
        http("/api/remote", {"on": True})
    elif a.action == "off":
        http("/api/remote", {"on": False})
    info = phone_info()
    if a.json:
        out(info, True)
    elif not info["on"]:
        print("Phone view is off.")
    else:
        print(f"Phone view is on. Same Wi-Fi: open {(info['addresses'] or ['(no network found)'])[0]} and type the PIN {info['pin']}.")
    return 0


def cmd_awake(a) -> int:
    """Keep this computer awake while the factory works (the dashboard holds it; it lets go when runs finish)."""
    if a.action in ("on", "off") and not dashboard_status()["running"]:
        if a.action == "off":
            print("The dashboard is not running, so nothing is being held awake.")
            return 0
        rc, st = ensure_dashboard()
        if rc:
            out(st, a.json)
            return rc
    if a.action == "on":
        body = {"on": True, "mode": "hours", "hours": a.hours} if a.hours else {"on": True, "mode": "runs"}
        status, res = http("/api/awake", body)
        if status != 200:
            print(res.get("error") or "could not turn it on")
            return 1
    elif a.action == "off":
        http("/api/awake", {"on": False})
    status, res = http("/api/awake")
    out(res if a.json else ("Keeping the computer awake" + (f" until {time.strftime('%H:%M', time.localtime(res['until']))}" if res.get("mode") == "hours" else " until all runs finish")
                            if res.get("on") else "Not keeping the computer awake."), a.json)
    return 0


# --------------------------------------------------------------------------- idea


def cmd_idea(a) -> int:
    idea = " ".join(a.idea).strip()
    if a.idea_file:  # safest for long ideas or text with quotes: the agent writes it to a file first
        idea = (idea + "\n\n" + Path(a.idea_file).read_text(encoding="utf-8")).strip()
    body = {"idea": idea or None, "generate": bool(a.generate), "product_links": a.link, "ref_links": a.ref,
            "skills": a.skill, "app_context": a.context, "first_open": a.first_open, "force_mode": a.mode, "budget_usd": a.budget, "quality": a.quality}
    if a.spec_file:
        body["spec"] = Path(a.spec_file).read_text(encoding="utf-8")
    if a.reverse:
        body["reverse"] = {"target": a.reverse, "scope": a.reverse_scope, "links": a.reverse_link}
    photos = []
    for ph in a.photo:
        p = Path(ph).expanduser()
        if not p.is_file():
            print(f"photo not found: {p}")
            return 1
        photos.append({"name": p.name, "data": base64.b64encode(p.read_bytes()).decode()})
    body["photos"] = photos
    if not (idea or a.generate or a.spec_file or a.reverse):
        print('give an idea ("a habit tracker"), --spec-file, --generate or --reverse')
        return 1
    rc, st = ensure_dashboard()
    if rc:
        out(st, a.json)
        return rc
    status, res = http("/api/new", body, timeout=30)
    if status != 200 or not res.get("run"):
        out({"error": res.get("error") or f"the dashboard refused the run (HTTP {status})"}, a.json)
        return 1
    url = f"{st['url']}/?run={res['run']}"
    if not a.no_open:
        open_window(url)
    out({"run": res["run"], "dashboard": url, "opened": not a.no_open,
         "next": "Watch it on the dashboard. Decisions (pick an idea, approve the spec, review the app) appear under 'Needs you'."}, a.json)
    return 0


def cmd_status(a) -> int:
    rc, text = factory_py(["status", *([a.run] if a.run else [])], 120)
    print(text.strip())
    return rc


# --------------------------------------------------------------------------- main


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="sf.py", description=__doc__.split("\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", parents=[common])
    c = sub.add_parser("configure", parents=[common])
    c.add_argument("--engine", choices=["claude", "codex", "custom"])
    c.add_argument("--host", help="the agent that hosts the skills: claude-code, codex, gemini or other")
    c.add_argument("--factory-dir")
    c.add_argument("--port", type=int)
    c.add_argument("--quality", choices=["lean", "balanced", "max"], help="the default Quality of new runs")
    c.add_argument("--budget-single", type=float, help="default spending cap in dollars for single-agent runs (0 = no cap)")
    c.add_argument("--budget-multi", type=float, help="default spending cap in dollars for full-pipeline runs (0 = no cap)")
    c.add_argument("--notify", choices=["on", "off"], help="desktop notifications when a run needs you")
    c.add_argument("--open-as", choices=["app", "browser"], help="open the dashboard as its own app window (default) or in your normal browser")
    c.add_argument("--brand-file", help="a text file with brand rules (colours, fonts, tone) every app must follow")
    c.add_argument("--models", help="strong=MODEL,standard=MODEL,fast=MODEL (non-Claude engines; empty = the agent's default)")
    c.add_argument("--command", help='custom engine: JSON list, the non-interactive command, e.g. \'["gemini", "--yolo"]\'')
    c.add_argument("--prompt", choices=["stdin", "arg", "file"], help="custom engine: how the prompt is passed (file = {prompt_file} in --command)")
    c.add_argument("--model-args", help='custom engine: JSON list added when a model is set, e.g. \'["-m", "{model}"]\'')
    c.add_argument("--effort-args", help="custom engine: JSON list for the effort level, using {effort}")
    c.add_argument("--label", help="custom engine: a display name, e.g. Gemini CLI")
    sub.add_parser("prepare", parents=[common])
    g = sub.add_parser("agents", parents=[common])
    g.add_argument("action", nargs="?", choices=["list", "set", "reset"], default="list")
    g.add_argument("name", nargs="?")
    g.add_argument("--model", help="a Claude model id (claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5) or your engine's own model name")
    g.add_argument("--effort", choices=EFFORTS)
    sub.add_parser("check", parents=[common])
    s = sub.add_parser("install-skills", parents=[common])
    s.add_argument("--host")
    s.add_argument("--dir", help="install into this skills folder instead")
    sub.add_parser("connections", parents=[common])
    s0 = sub.add_parser("start", parents=[common], help="the one-click way in: install on first start, start the dashboard, open it")
    s0.add_argument("--app", action="store_true", help="open it as its own app window (default)")
    s0.add_argument("--browser", action="store_true", help="open it in your normal browser instead")
    s0.add_argument("--phone", action="store_true", help="also switch on the phone view (same Wi-Fi, PIN)")
    s0.add_argument("--awake", action="store_true", help="also keep this computer awake until all runs finish")
    s0.add_argument("--no-open", action="store_true")
    sc = sub.add_parser("shortcut", parents=[common], help="put a Software Factory icon on your Desktop")
    sc.add_argument("--dir", help="put it in this folder instead of the Desktop")
    ph = sub.add_parser("phone", parents=[common], help="phone view: on, off or status")
    ph.add_argument("action", nargs="?", choices=["on", "off", "status"], default="status")
    aw = sub.add_parser("awake", parents=[common], help="keep this computer awake: on, off or status")
    aw.add_argument("action", nargs="?", choices=["on", "off", "status"], default="status")
    aw.add_argument("--hours", type=float, help="stay awake this many hours (default: until all runs finish)")
    d = sub.add_parser("dashboard", parents=[common])
    d.add_argument("action", choices=["start", "stop", "status", "open"])
    d.add_argument("--no-open", action="store_true")
    i = sub.add_parser("idea", parents=[common])
    i.add_argument("idea", nargs="*")
    i.add_argument("--idea-file", help="read the idea from a text file (avoids quoting problems)")
    i.add_argument("--spec-file")
    i.add_argument("--generate", action="store_true")
    i.add_argument("--reverse")
    i.add_argument("--reverse-scope", choices=["feature", "app"], default="feature")
    i.add_argument("--reverse-link", action="append", default=[])
    i.add_argument("--link", action="append", default=[])
    i.add_argument("--ref", action="append", default=[])
    i.add_argument("--photo", action="append", default=[])
    i.add_argument("--skill", action="append", default=[])
    i.add_argument("--context", help="background on the app: who it is for, what it is for, what it must respect")
    i.add_argument("--first-open", help="what the app's user should see and do the first time they open it")
    i.add_argument("--mode", choices=["single", "multi"])
    i.add_argument("--budget", type=float)
    i.add_argument("--quality", choices=["lean", "balanced", "max"], default=None,
                   help="lean = cheapest (experiments), balanced, max = strongest models everywhere; default: your setup choice")
    i.add_argument("--no-open", action="store_true")
    st = sub.add_parser("status", parents=[common])
    st.add_argument("run", nargs="?")
    f = sub.add_parser("factory", parents=[common])
    f.add_argument("rest", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    if a.cmd == "factory":
        rc, _ = factory_py(a.rest, capture=False)
        return rc
    return {"doctor": cmd_doctor, "configure": cmd_configure, "prepare": cmd_prepare, "agents": cmd_agents, "check": cmd_check,
            "install-skills": cmd_install_skills, "connections": cmd_connections, "dashboard": cmd_dashboard, "idea": cmd_idea,
            "status": cmd_status, "start": cmd_start, "shortcut": cmd_shortcut, "phone": cmd_phone, "awake": cmd_awake}[a.cmd](a)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
