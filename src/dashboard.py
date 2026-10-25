"""Live dashboard for the Software Factory.

Reads each run's state.json and agent logs (written by factory.py) and streams them to the browser over
Server-Sent Events, so the page updates within ~1s of anything happening. It can also start runs, resume them,
answer the owner gates, and edit the always-use documents.

    uv run src/dashboard.py            # http://127.0.0.1:8787
    uv run src/dashboard.py --port 9000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse
import webbrowser

import awake
import checks
import common
import deploy
import factory
import keys
import qr
import remote

HERE = Path(__file__).resolve().parent   # src/
ROOT = HERE.parent                       # the folder you cloned
STATIC = ROOT / "dashboard"
STALE_AFTER = 20  # seconds without a driver heartbeat = driver is not running
CARD_STATE = {"pending": "waiting", "queued": "queued", "active": "active", "owner": "owner", "blocked": "blocked", "done": "done"}
FILE_ROOTS = ("factory/", "inputs/photos/", "reference/shots/")  # the only places the page may read images from
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


# --------------------------------------------------------------------------- data


def read_state(rd: Path) -> Optional[dict]:
    for _ in range(3):  # the driver replaces the file atomically; retry if we hit the swap
        try:
            return json.loads((rd / "state.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, PermissionError):
            time.sleep(0.05)
    return None


def list_runs(runs_dir: Path) -> list[dict]:
    out = []
    for p in sorted(runs_dir.glob("*"), reverse=True):
        s = read_state(p) if (p / "state.json").exists() else None
        if s:
            out.append({"id": s["run_id"], "title": s.get("title") or s.get("idea") or s["run_id"], "status": s["status"]})
    return out


def tail_log(rd: Path, card_id: str, max_lines: int = 80, max_bytes: int = 24000) -> list[str]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", card_id):
        return []
    f = rd / "logs" / f"{card_id}.log"
    if not f.exists():
        return []
    with open(f, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        raw = fh.read().decode("utf-8", errors="replace")
    return [l.rstrip() for l in raw.splitlines() if l.strip()][-max_lines:]


def build_state(run_id: Optional[str], runs_dir: Optional[Path] = None, now: Optional[float] = None) -> dict:
    runs_dir = runs_dir or factory.RUNS_DIR  # looked up at call time so tests and tools can point it elsewhere
    now = now or time.time()
    runs = list_runs(runs_dir)
    state: dict = {"runs": runs, "now": int(now)}
    if not runs:
        return state
    run_id = run_id if any(r["id"] == run_id for r in runs) else runs[0]["id"]
    rd = runs_dir / run_id
    s = read_state(rd)
    if s is None:
        return state
    cards = []
    for c in s["cards"]:
        cards.append({"id": c["id"], "title": c["title"], "assignee": c.get("role"), "kind": c["kind"], "stage": c["stage"],
                      "state": CARD_STATE.get(c["state"], "waiting"), "status": c.get("status") or "",
                      "attempts": c.get("attempts", 0), "reviews": c.get("reviews", 0), "summary": c.get("summary"),
                      "last_failure_error": c.get("error"), "started_at": c.get("started_at"), "completed_at": c.get("completed_at"),
                      "model": c.get("model"), "effort": c.get("effort"), "cost_usd": c.get("cost_usd", 0)})
    stages = []
    for entry in s.get("rail") or []:
        st = s["stages"].get(entry["key"], {"state": "pending"})
        ids = [c["id"] for c in cards if c["stage"] == entry["key"] and c["kind"] != "check"]
        if entry["key"] not in ("build", "reviews") and ids:
            ids = ids[-1:]
        stages.append({"key": entry["key"], "label": entry["label"], "bot": entry["bot"], "col": entry["col"],
                       "state": "waiting" if st["state"] == "pending" else CARD_STATE.get(st["state"], st["state"]),
                       "detail": st.get("detail"), "task_ids": ids, "started_at": st.get("started_at"), "completed_at": st.get("completed_at")})
    active = [dict(c, log=tail_log(rd, c["id"])) for c in cards if c["state"] == "active" and c["kind"] not in ("gate", "check")]
    beat = s.get("heartbeat") or 0
    driver = None if s["status"] in ("done", "stopped", "created") else (s["status"] in ("running", "gate") and now - beat < STALE_AFTER)
    if s["status"] == "failed":
        driver = False
    app_json = None
    try:
        app_json = json.loads((rd / "factory" / "app.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    docs = [d for d in factory.DOCS if (rd / "factory" / d).exists()]
    state.update({
        "run": {"id": s["run_id"], "idea": s.get("idea"), "title": s.get("title") or s.get("idea") or s["run_id"], "rounds": s["rounds"],
                "round_no": s.get("round_no", 0), "status": s["status"], "error": s.get("error"), "dir": factory.posix(rd),
                "mode": s.get("mode"), "waiting": s.get("waiting"), "app_type": (app_json or {}).get("type"), "deploy": s.get("deploy"), "deploys": (s.get("deploys") or [])[-6:], "cost_usd": s.get("cost_usd", 0), "budget": factory.budget_for(s), "escalated": bool(s.get("escalated")), "decision": s.get("decision"),
                "can_open": can_open(rd, app_json), "app_running": app_running(rd.name),
                "keys": keys.describe(keys.read_env_json(rd / "factory" / "env.json")[0]),
                "inputs": s.get("inputs"), "engine": s.get("engine") or {"name": "claude", "label": "Claude Code", "reports_cost": True}},
        "docs": docs, "stages": stages, "tasks": cards, "active": active, "driver": driver,
        "console": tail_log(rd, "driver", 60) + tail_log(rd, "deploy", 20),
        "events": [{"id": e["id"], "task_id": e.get("card"), "kind": e["kind"], "msg": e.get("msg", ""), "created_at": e["created_at"]}
                   for e in reversed(s["events"][-60:])],
        "gate": s.get("gate"),
        "stats": {"started_at": s.get("created_at"), "ended_at": s.get("ended_at") if s["status"] in ("done", "stopped") else None,
                  "done": sum(1 for c in cards if c["state"] == "done"), "total": len(cards), "active": len(active),
                  "reviews": sum(c["reviews"] for c in cards), "blocked": sum(1 for c in cards if c["state"] == "blocked"),
                  "cost_usd": s.get("cost_usd", 0)},
    })
    return state


def snapshot(run_id: Optional[str]) -> dict:
    try:
        return build_state(run_id)
    except Exception as e:  # never kill the stream; the next tick recovers
        return {"runs": [], "error": str(e), "now": int(time.time())}


# --------------------------------------------------------------------------- actions


def spawn_driver(run_id: str) -> None:
    """Start factory.py for a run in a detached process, so it outlives the dashboard."""
    log = open(factory.RUNS_DIR / run_id / "logs" / "driver.log", "ab")
    # CREATE_NO_WINDOW, not DETACHED_PROCESS: a process with no console makes Windows open a new visible
    # console for every child it starts (claude, git, uv). A hidden console is inherited by the children instead.
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    subprocess.Popen([sys.executable, str(HERE / "factory.py"), "run", run_id], cwd=str(ROOT), stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True, start_new_session=(os.name != "nt"),
                     env=dict(os.environ, PYTHONUNBUFFERED="1"))  # unbuffered: the Factory console shows output as it happens


def spawn_detached(args: list[str], run_id: str, log_name: str) -> None:
    """Run `factory.py <args>` for a run in a hidden, detached process (same reasoning as spawn_driver)."""
    log = open(factory.RUNS_DIR / run_id / "logs" / log_name, "ab")
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    subprocess.Popen([sys.executable, str(HERE / "factory.py"), *args], cwd=str(ROOT), stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True, start_new_session=(os.name != "nt"),
                     env=dict(os.environ, PYTHONUNBUFFERED="1"))  # unbuffered: the Factory console shows output as it happens


def open_in_os(path: str) -> None:
    """Open a file with the system's default program (Windows, macOS or Linux)."""
    if hasattr(os, "startfile"):
        os.startfile(path)  # type: ignore[attr-defined]
        return
    try:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


APP_PROCS: dict[str, tuple[subprocess.Popen, int]] = {}  # run id -> (the app's server process, its port)


def can_open(rd: Path, app_json: Optional[dict]) -> bool:
    if not app_json:
        return False
    if app_json.get("type") == "static":
        return (rd / "app" / str(app_json.get("entry", "index.html"))).exists()
    if app_json.get("type") == "desktop":
        return bool(app_json.get("start"))
    return app_json.get("type") == "server" and bool(app_json.get("start")) and isinstance(app_json.get("port"), int)


def app_running(run_id: str) -> bool:
    proc = APP_PROCS.get(run_id)
    return bool(proc and proc[0].poll() is None)


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def stop_app(run_id: str) -> bool:
    proc = APP_PROCS.pop(run_id, None)
    if not proc or proc[0].poll() is not None:
        return False
    factory._kill_tree(proc[0])
    return True


def open_server_app(rd: Path, aj: dict) -> tuple[int, dict]:
    """Start the finished app's server (hidden, with the owner's keys for the services it declared) and open it in the browser."""
    first = str((aj.get("screens") or [{}])[0].get("path") or "/")
    if app_running(rd.name):
        port = APP_PROCS[rd.name][1]
    else:
        port = int(aj["port"])
        if _port_open(port):  # something else has the app's usual port
            port = next((p for p in range(port + 1, port + 50) if not _port_open(p)), 0)
            if not port:
                return 409, {"error": "no free port to start the app on"}
        env_vars, _ = keys.read_env_json(rd / "factory" / "env.json")
        env = {**os.environ, "PORT": str(port), **keys.env_for([v["name"] for v in env_vars])}  # only the keys this app declared
        cmd = aj["start"]
        args = cmd if isinstance(cmd, list) else shlex.split(str(cmd), posix=(os.name != "nt"))
        log = open(rd / "logs" / "app.log", "ab")
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(args, cwd=str(rd / "app"), env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                    creationflags=flags, start_new_session=(os.name != "nt"))
        except OSError as e:
            return 500, {"error": f"the app could not start: {e}"}
        APP_PROCS[rd.name] = (proc, port)
        for _ in range(90):
            if _port_open(port) or proc.poll() is not None:
                break
            time.sleep(0.5)
        if not _port_open(port):
            stop_app(rd.name)
            return 500, {"error": "the app did not start (its output is in logs/app.log)"}
    url = f"http://127.0.0.1:{port}{first if first.startswith('/') else '/' + first}"
    webbrowser.open(url)
    return 200, {"ok": True, "url": url}


def open_desktop_app(rd: Path, aj: dict) -> tuple[int, dict]:
    """Start a finished desktop app (its own window is the app), with the keys it declared."""
    if app_running(rd.name):
        return 200, {"ok": True, "note": "the app is already open"}
    env_vars, _ = keys.read_env_json(rd / "factory" / "env.json")
    env = {**os.environ, **keys.env_for([v["name"] for v in env_vars])}
    cmd = aj["start"]
    args = cmd if isinstance(cmd, list) else shlex.split(str(cmd), posix=(os.name != "nt"))
    args = [shutil.which(str(args[0])) or args[0], *args[1:]]
    log = open(rd / "logs" / "app.log", "ab")
    try:
        proc = subprocess.Popen(args, cwd=str(rd / "app"), env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=(os.name != "nt"))
    except OSError as e:
        return 500, {"error": f"the app could not start: {e}"}
    APP_PROCS[rd.name] = (proc, 0)
    return 200, {"ok": True}


def driver_alive(rd: Path) -> bool:
    s = read_state(rd)
    return bool(s) and s["status"] in ("running", "gate") and time.time() - (s.get("heartbeat") or 0) < STALE_AFTER


def safe_run_dir(run: str) -> Optional[Path]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run or "") or not (factory.RUNS_DIR / run / "state.json").exists():
        return None
    return factory.RUNS_DIR / run


def file_for(run: str, rel: str) -> Optional[tuple[Path, str]]:
    """A file the page may show: an image (or the mock page) under an allowed folder of a run. Returns (path, content type)."""
    rd = safe_run_dir(run)
    if rd is None or not rel or ".." in Path(rel).parts or not rel.startswith(FILE_ROOTS):
        return None
    p = (rd / rel).resolve()
    try:
        p.relative_to(rd.resolve())
    except ValueError:
        return None
    if not p.is_file():
        return None
    if p.suffix.lower() in IMAGE_EXT:
        return p, mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    if p.suffix.lower() == ".html" and rel.startswith("factory/mock/"):
        return p, "text/html; charset=utf-8"
    return None


def act(path: str, body: dict) -> tuple[int, dict]:
    if path == "/api/new":
        try:
            rd = factory.create_run({k: body.get(k) for k in ("idea", "spec", "generate", "reverse", "product_links", "ref_links", "photos", "skills", "app_context", "first_open", "force_mode", "budget_usd", "quality")},
                                    factory.RUNS_DIR)
        except ValueError as e:
            return 400, {"error": str(e)}
        spawn_driver(rd.name)
        return 200, {"ok": True, "run": rd.name}
    if path == "/api/always":
        name = checks.safe_doc_name(str(body.get("name", "")))
        if not name:
            return 400, {"error": "name must look like brand-colours.md (letters, numbers, spaces, dashes)"}
        checks.ALWAYS_DIR.mkdir(parents=True, exist_ok=True)
        (checks.ALWAYS_DIR / name).write_text(str(body.get("text", "")), encoding="utf-8")
        return 200, {"ok": True}
    if path == "/api/skills/upload":
        try:
            name = checks.save_skill_upload(body.get("files") or [], body.get("name") or None, checks.SKILLS_LIBRARY)
        except ValueError as e:
            return 400, {"error": str(e)}
        return 200, {"ok": True, "name": name}
    if path == "/api/skills/delete":
        if not checks.delete_library_skill(str(body.get("name", "")), checks.SKILLS_LIBRARY):
            return 404, {"error": "no such skill in your library"}
        return 200, {"ok": True}
    if path == "/api/always/delete":
        name = checks.safe_doc_name(str(body.get("name", "")))
        if not name or not (checks.ALWAYS_DIR / name).exists():
            return 404, {"error": "no such document"}
        (checks.ALWAYS_DIR / name).unlink()
        return 200, {"ok": True}
    if path == "/api/awake":   # keep this computer from sleeping while the factory works (also reachable from the phone)
        try:
            return 200, AWAKE.start(str(body.get("mode") or "runs"), float(body.get("hours") or 2)) if body.get("on") else AWAKE.stop()
        except (RuntimeError, ValueError) as e:
            return 400, {"error": str(e)}
    if path == "/api/remote":   # the phone view on/off (this computer only: the phone cannot switch itself on or rotate its own PIN)
        try:
            if body.get("on") is False:
                PHONE.stop()
            else:
                PHONE.start()
                if body.get("rotate"):
                    PHONE.auth.reset()
        except RuntimeError as e:
            return 500, {"error": str(e)}
        return 200, PHONE.status()
    run = str(body.get("run", ""))
    rd = safe_run_dir(run)
    if rd is None:
        return 404, {"error": "unknown run"}
    if path == "/api/resume":
        if driver_alive(rd):
            return 409, {"error": "the driver is already running"}
        spawn_driver(run)
        return 200, {"ok": True}
    if path == "/api/open":  # open a finished single-file app in the default browser
        st = read_state(rd) or {}
        try:
            aj = json.loads((rd / "factory" / "app.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 404, {"error": "this app has no app.json yet"}
        if aj.get("type") == "server" and can_open(rd, aj):
            return open_server_app(rd, aj)
        if aj.get("type") == "desktop" and can_open(rd, aj):
            return open_desktop_app(rd, aj)
        entry = rd / "app" / str(aj.get("entry", "index.html"))
        if aj.get("type") != "static" or not entry.exists() or ".." in Path(str(aj.get("entry", ""))).parts:
            return 404, {"error": "this app can't be opened from here (a command-line app?): see the report for how to run it"}
        open_in_os(str(entry))
        return 200, {"ok": True, "path": str(entry), "state": st.get("status")}
    if path == "/api/stop-app":
        return 200, {"ok": True, "stopped": stop_app(run)}
    if path == "/api/keys":  # the owner stores a key for an outside service (never returned to the page)
        try:
            keys.set_key(str(body.get("name", "")), str(body.get("value", "")))
        except ValueError as e:
            return 400, {"error": str(e)}
        restarted = stop_app(run)  # a running app keeps its old keys: stop it, the owner opens it again
        return 200, {"ok": True, "restart_needed": restarted}
    if path == "/api/deploy":  # publish or package a finished app, using the owner's own accounts
        rd = safe_run_dir(str(body.get("run", "")))
        target = str(body.get("target", ""))
        if rd is None or target not in deploy.TARGETS:
            return 400, {"error": "unknown run or target"}
        st = read_state(rd) or {}
        if st.get("status") != "done":
            return 409, {"error": "approve the finished app first; only a finished run can be deployed"}
        cur = st.get("deploy") or {}
        if cur.get("status") == "running" and time.time() - (st.get("heartbeat") or 0) < STALE_AFTER:
            return 409, {"error": "a deploy of this run is already in progress"}
        spawn_detached(["deploy", rd.name, "--target", target], rd.name, "deploy.log")
        return 200, {"ok": True}
    if path in ("/api/approve", "/api/reject", "/api/decide"):
        decision = {"/api/approve": "approve", "/api/reject": "reject"}.get(path) or str(body.get("decision", ""))
        fb = (body.get("feedback") or "").strip()
        if decision in ("reject", "improve", "tweak", "big") and not fb:
            return 400, {"error": "feedback is required: say what should change"}
        index = body.get("index")
        try:
            factory.write_decision(rd, decision, fb, body.get("note") or "", int(index) if index is not None else None)
        except SystemExit as e:
            return 409, {"error": str(e)}
        return 200, {"ok": True}
    return 404, {"error": "not found"}


# --------------------------------------------------------------------------- keep awake and phone view


def any_run_active() -> bool:
    """Is a driver working (or waiting for you at a gate) right now? Used to let go of 'keep awake' when everything is finished."""
    now = time.time()
    for p in sorted(factory.RUNS_DIR.glob("*"), reverse=True)[:30]:
        s = read_state(p) if (p / "state.json").exists() else None
        if s and s.get("status") in ("running", "gate") and now - (s.get("heartbeat") or 0) < STALE_AFTER:
            return True
    return False


AWAKE = awake.KeepAwake(busy=any_run_active)


class PhoneView:
    """The second, network-facing server. Off until you switch it on; see remote.py for how it stays safe."""

    def __init__(self):
        self.auth = remote.RemoteAuth()
        self.srv: Optional[ThreadingHTTPServer] = None
        self.port: Optional[int] = None
        self.base_port = 8787
        self.lock = threading.Lock()

    @property
    def on(self) -> bool:
        return self.srv is not None

    def start(self) -> None:
        with self.lock:
            if self.srv:
                return
            last: Optional[OSError] = None
            for port in range(self.base_port + 1, self.base_port + 25):
                try:
                    srv = ThreadingHTTPServer(("0.0.0.0", port), RemoteHandler)
                except OSError as e:
                    last = e
                    continue
                srv.daemon_threads = True
                self.srv, self.port = srv, port
                self.auth.reset()
                self.auth.rotated_by_attack = False
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                return
            raise RuntimeError(f"no free port to share the dashboard on ({last})")

    def stop(self) -> None:
        with self.lock:
            srv, self.srv, self.port = self.srv, None, None
            self.auth.clear()
        if srv:
            srv.shutdown()
            srv.server_close()

    def status(self) -> dict:
        if not self.on:
            return {"on": False}
        urls = [f"http://{ip}:{self.port}" for ip in remote.lan_addresses()]
        pin = self.auth.pin or ""
        link = f"{urls[0]}/?pin={pin}" if urls else ""
        return {"on": True, "urls": urls, "pin": pin, "link": link, "port": self.port, "signed_in": self.auth.signed_in(),
                "qr": qr.svg(link, dark="#10130a", light="#ffffff") if link else "", "rotated_by_attack": self.auth.rotated_by_attack,
                "note": "" if urls else "No local network found: connect this computer to Wi-Fi or a cable first."}


PHONE = PhoneView()


# --------------------------------------------------------------------------- http


class Handler(BaseHTTPRequestHandler):
    server_version = "SFDashboard/3"
    remote = False   # RemoteHandler (the phone view) sets this

    def _host_ok(self) -> bool:
        """On the loopback server only answer requests addressed to localhost: a web page cannot trick the browser into
        talking to this dashboard under another name (DNS rebinding)."""
        if self.remote or self.server.server_address[0] not in ("127.0.0.1", "::1"):
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _still_allowed(self) -> bool:
        return True

    def _refuse(self, message: str, code: int):
        """Answer a POST we will not act on. The body is read first: replying and closing with unread data makes the
        connection reset on Windows, and the caller would see a dropped connection instead of the message."""
        try:
            self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1_000_000))
        except (ValueError, OSError):
            pass
        return self._json({"error": message}, code)

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        run = qs.get("run", "")
        if not self._host_ok():
            return self._json({"error": "forbidden"}, 403)
        if url.path == "/api/awake":
            return self._json(AWAKE.status())
        if url.path == "/api/remote":
            return self._json({"error": "forbidden"}, 403) if self.remote else self._json(PHONE.status())
        if url.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if url.path == "/api/info":  # lets scripts/sf.py confirm this is the factory it expects
            return self._json({"app": "software-factory", "factory_dir": factory.posix(ROOT), "engine": factory.engines.describe(),
                               "default_quality": factory.default_quality(), "remote": self.remote})
        if url.path == "/api/state":
            return self._json(snapshot(run))
        if url.path == "/api/doc":
            name = qs.get("name", "")
            if name not in factory.DOCS or not re.fullmatch(r"[A-Za-z0-9_-]+", run):
                return self._json({"error": "bad request"}, 400)
            p = factory.RUNS_DIR / run / "factory" / name
            return self._json({"name": name, "text": p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""})
        if url.path == "/api/file":
            found = file_for(run, qs.get("path", ""))
            if not found:
                return self._send(404, b"not found", "text/plain")
            p, ctype = found
            # agent-written HTML is shown in a sandbox so it can never call this dashboard's own actions
            extra = {"Content-Security-Policy": "sandbox allow-scripts"} if ctype.startswith("text/html") else {}
            return self._send(200, p.read_bytes(), ctype, extra)
        if url.path == "/api/log":
            if not re.fullmatch(r"[A-Za-z0-9_-]+", run):
                return self._json({"error": "bad request"}, 400)
            return self._json({"lines": tail_log(factory.RUNS_DIR / run, qs.get("task", ""), 500, 120000)})
        if url.path == "/api/deploy-options":
            rd = safe_run_dir(run)
            if rd is None:
                return self._json({"error": "bad request"}, 400)
            app_json, _ = checks.read_app_json(rd / "factory" / "app.json", rd / "app")
            return self._json({"options": deploy.options_for(app_json, deploy.detect_connectors(fresh=qs.get("fresh") == "1"))})
        if url.path == "/api/skills":
            return self._json({"skills": checks.list_skills(), "library": checks.list_library(checks.SKILLS_LIBRARY)})
        if url.path == "/api/always":
            return self._json({"docs": [{"name": n, "text": t} for n, t in checks.load_always(checks.ALWAYS_DIR)]})
        if url.path == "/api/stream":
            return self._stream(run)
        self._send(404, b"not found", "text/plain")

    def _stream(self, run_id: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last, last_beat = "", 0.0
        try:
            while True:
                if self.remote and not self._still_allowed():   # the phone view was switched off, or the PIN was replaced
                    return
                data = json.dumps(snapshot(run_id))
                digest = hashlib.sha1(re.sub(r'"now": \d+', "", data).encode()).hexdigest()
                if digest != last:
                    self.wfile.write(f"event: state\ndata: {data}\n\n".encode())
                    self.wfile.flush()
                    last = digest
                elif time.time() - last_beat > 10:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def do_POST(self):
        url = urlparse(self.path)
        if not self._host_ok():
            return self._refuse("forbidden", 403)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 60_000_000:
            return self._json({"error": "too large"}, 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        if self.headers.get("X-SF-Dashboard") != "1":  # simple CSRF guard: only our page sets this
            return self._json({"error": "forbidden"}, 403)
        try:
            code, obj = act(url.path, body)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)[:300]}, 500)
        return self._json(obj, code)


class RemoteHandler(Handler):
    """What a phone talks to. Same pages, but every request needs the PIN (once) and only a short list of actions is allowed."""
    remote = True

    def end_headers(self):
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _token(self) -> Optional[str]:
        return remote.cookie_value(self.headers.get("Cookie"))

    def _still_allowed(self) -> bool:
        return PHONE.on and PHONE.auth.valid(self._token())

    def _cookie(self, token: str) -> dict:
        return {"Set-Cookie": f"{remote.COOKIE}={token}; Path=/; Max-Age={remote.COOKIE_AGE}; HttpOnly; SameSite=Strict"}

    def do_GET(self):
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        if not PHONE.on:
            return self._send(403, b"Phone view is off", "text/plain")
        if "pin" in qs and url.path in ("/", "/index.html"):   # the QR code: sign in, then drop the PIN from the address
            token, _ = PHONE.auth.login(self.client_address[0], qs["pin"])
            if token:
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                for k, v in self._cookie(token).items():
                    self.send_header(k, v)
                self.end_headers()
                return
        if not self._still_allowed():
            if url.path in ("/", "/index.html"):
                return self._send(401, remote.LOGIN_PAGE.encode(), "text/html; charset=utf-8")
            return self._json({"error": "not signed in"}, 401)
        if url.path not in remote.ALLOWED_GET:
            return self._json({"error": "this is only available on the computer"}, 403)
        return super().do_GET()

    def do_POST(self):
        url = urlparse(self.path)
        if not PHONE.on:
            return self._refuse("Phone view is off", 403)
        if url.path == "/api/login":
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 2000)) or b"{}")
            except (json.JSONDecodeError, ValueError):
                return self._json({"error": "bad json"}, 400)
            if self.headers.get("X-SF-Dashboard") != "1":
                return self._json({"error": "forbidden"}, 403)
            token, why = PHONE.auth.login(self.client_address[0], str(body.get("pin", "")))
            if not token:
                return self._json({"error": why}, 429 if "too many" in why else 401)
            return self._send(200, b'{"ok": true}', "application/json", self._cookie(token))
        if not self._still_allowed():
            return self._refuse("not signed in", 401)
        if url.path not in remote.ALLOWED_POST:
            return self._refuse("this is only available on the computer", 403)
        return super().do_POST()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--phone", action="store_true", help="switch the phone view on right away (same Wi-Fi, PIN protected)")
    ap.add_argument("--awake", action="store_true", help="keep this computer awake until all runs finish")
    a = ap.parse_args(argv)
    common.migrate_legacy_layout()
    factory.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    checks.ALWAYS_DIR.mkdir(parents=True, exist_ok=True)
    checks.SKILLS_LIBRARY.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"  # on Windows it would let two servers share a port
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    PHONE.base_port = a.port
    if a.phone:
        PHONE.start()
    if a.awake and AWAKE.supported():
        AWAKE.start("runs")
    threading.Thread(target=deploy.detect_connectors, daemon=True).start()  # warm the connector check: it takes a few seconds
    print(f"Software Factory dashboard: http://{a.host}:{a.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        AWAKE.stop()
        PHONE.stop()
        for run_id in list(APP_PROCS):
            stop_app(run_id)


if __name__ == "__main__":
    main()
