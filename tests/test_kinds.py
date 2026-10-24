"""The new kinds of product: each check is run against a small REAL sample app (servers really start, packages really build,
the browser really takes screenshots). Only Godot is replaced by a stand-in program, because it isn't installed here."""

import json
import os
import struct
import sys
import zlib
from pathlib import Path

import pytest

import checks
import factory
import kinds

PY = sys.executable.replace("\\", "/")
EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
needs_browser = pytest.mark.skipif(not EDGE.exists(), reason="needs Microsoft Edge")


def write(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


API = r'''
import json, os, http.server
NOTES = {}
class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path == "/health": return self._send(200, {"ok": True})
        if self.path.startswith("/notes/"):
            n = NOTES.get(self.path.split("/")[-1]); return self._send(200, n) if n else self._send(404, {"error": "not found"})
        self._send(404, {"error": "no route"})
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        nid = str(len(NOTES) + 1); NOTES[nid] = {"id": nid, "text": data.get("text", "")}; self._send(201, NOTES[nid])
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''
API_CHECKS = [{"name": "health", "method": "GET", "path": "/health", "expect_status": 200},
              {"name": "create note", "method": "POST", "path": "/notes", "json": {"text": "hi"}, "expect_status": 201, "expect_keys": ["id", "text"]},
              {"name": "missing note", "method": "GET", "path": "/notes/999", "expect_status": 404}]


# --------------------------------------------------------------------------- what this computer can build


def test_capabilities_tell_agents_what_they_may_choose():
    none = kinds.capability_note(which=lambda n: None)
    assert "NOT available here" in none and "`mobile` (needs Node.js" in none and "`godot` (needs Godot 4" in none
    allk = kinds.capability_note(which=lambda n: "/usr/bin/" + n)
    assert "NOT available" not in allk and "- `godot`:" in allk and "- `api`:" in allk
    maker, writer = factory.protocol_text("sf-maker"), factory.protocol_text("sf-writer")
    assert "### What this computer can build" in maker and "### api: a backend with no screens" in maker
    assert "### What this computer can build" not in writer  # agents that don't choose the kind don't pay to read it


def test_app_json_rules_for_every_new_kind(tmp_path):
    v = lambda d: checks.validate_app_json(d, tmp_path)
    assert "type must be one of" in v({"type": "spaceship"})[0]
    assert any("api_checks" in e for e in v({"type": "api", "start": "x", "port": 1}))
    assert v({"type": "api", "start": "x", "port": 1, "api_checks": API_CHECKS}) == []
    assert any("simulate" in e for e in v({"type": "worker", "start": "x"}))
    assert any("examples" in e for e in v({"type": "library", "build": "uv build"}))
    assert any("at least two" in e for e in v({"type": "multi", "services": [{"name": "a", "type": "api"}]}))
    bad_multi = {"type": "multi", "services": [{"name": "api", "type": "api", "start": "x", "port": 1, "api_checks": API_CHECKS},
                                               {"name": "web", "type": "server", "start": "y", "port": 2, "depends_on": ["db"]}], "web": "web"}
    errs = v(bad_multi)
    assert any("unknown service 'db'" in e for e in errs) and any("need 'screens'" in e for e in errs)
    assert any("web_export" in e for e in v({"type": "mobile", "screens": [{"name": "h", "path": "/"}]}))
    assert any("screenshot" in e for e in v({"type": "desktop", "screens": [{"name": "main"}]}))
    assert any("project.godot" in e for e in v({"type": "godot"}))


# --------------------------------------------------------------------------- api


def test_api_kind_starts_the_server_and_checks_every_request(tmp_path):
    app = write(tmp_path / "app", {"api.py": API})
    cfg = {"type": "api", "start": [PY, "api.py"], "port": kinds.free_port(), "api_checks": API_CHECKS}
    res, problems = checks.kind_checks(app, cfg, tmp_path / "shots", tmp_path / "logs")
    assert problems == [] and [r["status"] for r in res["requests"]] == [200, 201, 404]
    wrong = dict(cfg, api_checks=[{"name": "created", "method": "POST", "path": "/notes", "json": {}, "expect_status": 201, "expect_keys": ["owner"]},
                                  {"name": "list", "method": "GET", "path": "/notes", "expect_status": 200}])
    _, problems = checks.kind_checks(app, wrong, tmp_path / "shots", tmp_path / "logs")
    assert "missing owner" in problems[0] and "returned 404, expected 200" in problems[1]
    _, problems = checks.kind_checks(write(tmp_path / "broken", {"api.py": "raise SystemExit(3)"}), cfg, tmp_path / "s", tmp_path / "l")
    assert "did not start" in problems[0]


# --------------------------------------------------------------------------- worker / bot


BOT = r'''
import os, sys, time
from logic import reply
mode = "real" if os.environ.get("CHAT_TOKEN") else "fake"
print(f"bot running in {mode} mode", flush=True)
while True:
    time.sleep(0.2)
'''
LOGIC = "def reply(text):\n    return 'pong' if text == 'ping' else 'say ping'\n"
SIM = "from logic import reply\nassert reply('ping') == 'pong'\nassert reply('hi') == 'say ping'\nprint('user: ping -> bot: pong')\n"


def test_worker_kind_runs_the_bot_in_fake_mode_and_its_scripted_conversation(tmp_path):
    app = write(tmp_path / "app", {"bot.py": BOT, "logic.py": LOGIC, "simulate.py": SIM})
    cfg = {"type": "worker", "start": [PY, "bot.py"], "simulate": [PY, "simulate.py"], "run_seconds": 2}
    res, problems = checks.kind_checks(app, cfg, tmp_path / "shots", tmp_path / "logs")
    assert problems == [] and res["stayed_up"] and "pong" in res["simulate"]["tail"]
    assert "fake mode" in (tmp_path / "logs" / "check-worker.log").read_text(encoding="utf-8")
    write(app, {"logic.py": "def reply(text):\n    return 'pong'\n", "crash.py": "import time\ntime.sleep(0.3)\nraise RuntimeError('boom')\n"})
    _, problems = checks.kind_checks(app, dict(cfg, start=[PY, "crash.py"]), tmp_path / "shots", tmp_path / "logs2")
    assert any("crashed" in p for p in problems) and any("simulate) failed" in p for p in problems)


# --------------------------------------------------------------------------- library


def test_library_kind_really_builds_the_package_and_runs_the_examples(tmp_path):
    app = write(tmp_path / "app", {
        "pyproject.toml": '[project]\nname = "tinylib"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n[build-system]\nrequires = ["uv_build>=0.8,<0.20"]\nbuild-backend = "uv_build"\n',
        "src/tinylib/__init__.py": "def add(a, b):\n    return a + b\n",
        "examples/quickstart.py": "import sys\nsys.path.insert(0, 'src')\nfrom tinylib import add\nassert add(2, 3) == 5\nprint('2 + 3 =', add(2, 3))\n",
        "examples/broken.py": "raise SystemExit('this example is wrong')\n", "README.md": "# tinylib\n"})
    cfg = {"type": "library", "build": ["uv", "build", "--directory", app.as_posix()], "examples": [[PY, "examples/quickstart.py"]]}
    res, problems = checks.kind_checks(app, cfg, tmp_path / "shots", tmp_path / "logs")
    assert problems == [], problems
    assert any(n.endswith(".whl") for n in res["dist"]) and any(n.endswith(".tar.gz") for n in res["dist"])
    _, problems = checks.kind_checks(app, dict(cfg, examples=[[PY, "examples/broken.py"]]), tmp_path / "s", tmp_path / "l")
    assert "README example fails" in problems[0]


# --------------------------------------------------------------------------- multi-service


WEB = r'''
import os, http.server
API = os.environ["API_URL"]
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"<!doctype html><meta name=viewport content='width=device-width'><title>Board</title><h1>Board</h1><p id=api>talks to {API}</p>".encode()
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''


@needs_browser
def test_multi_kind_starts_services_in_order_wires_addresses_and_screenshots_the_web_one(tmp_path):
    app = write(tmp_path / "app", {"api/api.py": API, "web/web.py": WEB, "worker/w.py": "import time, os\nprint('worker sees', os.environ['API_URL'], flush=True)\ntime.sleep(30)\n"})
    cfg = {"type": "multi", "web": "web", "screens": [{"name": "home", "path": "/"}],
           "services": [{"name": "web", "type": "server", "dir": "web", "start": [PY, "web.py"], "port": kinds.free_port(), "depends_on": ["api"]},
                        {"name": "api", "type": "api", "dir": "api", "start": [PY, "api.py"], "port": kinds.free_port(), "api_checks": API_CHECKS},
                        {"name": "worker", "type": "worker", "dir": "worker", "start": [PY, "w.py"], "depends_on": ["api"], "run_seconds": 1}]}
    assert checks.validate_app_json(cfg, app) == []
    res, problems = checks.kind_checks(app, cfg, tmp_path / "shots", tmp_path / "logs")
    assert problems == [], problems
    assert [r["status"] for r in res["services"]["api"]["requests"]] == [200, 201, 404]
    assert res["screens"]["ok"] and {s["png"] for s in res["screens"]["screens"]} == {"home-390.png", "home-1280.png"}
    assert "worker sees http://127.0.0.1:" in (tmp_path / "logs" / "check-svc-worker.log").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- mobile (web export at phone sizes)


@needs_browser
def test_mobile_kind_screenshots_the_web_export_at_phone_sizes(tmp_path):
    export = (f"import pathlib; d = pathlib.Path('dist'); d.mkdir(exist_ok=True); "
              "(d / 'index.html').write_text(\"<!doctype html><meta name=viewport content='width=device-width'><h1>Phone app</h1>\", encoding='utf-8')")
    app = write(tmp_path / "app", {"package.json": "{}"})
    cfg = {"type": "mobile", "web_export": [PY, "-c", export], "web_dir": "dist", "screens": [{"name": "home", "path": "/"}]}
    res, extra = checks.take_screens(app, cfg, tmp_path / "shots")
    assert extra == [] and res["ok"], res
    assert sorted(s["png"] for s in res["screens"]) == ["home-390.png", "home-430.png"] and checks.screen_problems(res) == []
    res, extra = checks.take_screens(app, dict(cfg, web_export=[PY, "-c", "raise SystemExit(1)"], web_dir="nothing"), tmp_path / "s2")
    assert not res["ok"] and "web export" in extra[0]


# --------------------------------------------------------------------------- desktop (off-screen renders)


def png_bytes(w: int, h: int, seed: int) -> bytes:
    """A real PNG of noise (so it is not 'blank'), written with the standard library only."""
    import random
    rnd = random.Random(seed)
    raw = b"".join(b"\x00" + bytes(rnd.randrange(256) for _ in range(w * 3)) for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def test_desktop_kind_collects_off_screen_renders_and_flags_missing_ones(tmp_path):
    script = ("import os, sys, pathlib\n"
              "assert os.environ['QT_QPA_PLATFORM'] == 'offscreen'\n"
              "out = pathlib.Path(sys.argv[-1])\n"
              f"png = {png_bytes(60, 40, 1)!r}\n"
              "for size in os.environ['FACTORY_SIZES'].split(','):\n"
              "    (out / f\"main-{size.split('x')[0]}.png\").write_bytes(png)\n")
    app = write(tmp_path / "app", {"shoot.py": script})
    cfg = {"type": "desktop", "screenshot": [PY, "shoot.py", "{out_dir}"], "screens": [{"name": "main"}, {"name": "settings"}]}
    res, extra = checks.take_screens(app, cfg, tmp_path / "shots")
    assert extra == []
    got = {(s["name"], s["size"][0]): s for s in res["screens"]}
    assert got[("main", 1280)]["png"] == "main-1280.png" and not got[("main", 1024)].get("load_error")
    assert "no screenshot settings-1280.png" in got[("settings", 1280)]["load_error"]
    assert any("settings at 1280px: page failed to load" in p for p in checks.screen_problems(res))


# --------------------------------------------------------------------------- godot (stand-in program)


def fake_godot(tmp_path: Path, output: str, code: int = 0) -> Path:
    script = tmp_path / "fake_godot.py"
    script.write_text(f"import sys\nprint({output!r})\nsys.exit({code})\n", encoding="utf-8")
    if os.name == "nt":
        exe = tmp_path / "godot.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        exe = tmp_path / "godot"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    return exe


def test_godot_kind_runs_the_game_headless_and_catches_script_errors(tmp_path, monkeypatch):
    app = write(tmp_path / "app", {"project.godot": "[application]\nconfig/name=\"Game\"\n"})
    monkeypatch.setenv("GODOT_BIN", str(fake_godot(tmp_path, "Godot Engine v4 - running")))
    res, problems = checks.kind_checks(app, {"type": "godot"}, tmp_path / "shots", tmp_path / "logs")
    assert problems == [] and res["ok"]
    monkeypatch.setenv("GODOT_BIN", str(fake_godot(tmp_path, "SCRIPT ERROR: Invalid call. Nonexistent function 'jump'")))
    _, problems = checks.kind_checks(app, {"type": "godot"}, tmp_path / "shots", tmp_path / "logs")
    assert "SCRIPT ERROR" in problems[0]
    monkeypatch.delenv("GODOT_BIN")
    monkeypatch.setattr(kinds.shutil, "which", lambda n: None)
    _, problems = checks.kind_checks(app, {"type": "godot"}, tmp_path / "shots", tmp_path / "logs")
    assert "Godot is not installed" in problems[0]
