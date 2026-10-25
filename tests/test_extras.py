"""Phone notifications, spending caps, disk clean-up, saved presets and the Tailscale address."""

import json
import os
import subprocess
import time

import pytest

import cleanup
import common
import dashboard
import engines
import factory
import presets
import push
import remote
import spend
from test_remote import call, servers, turn_on  # noqa: F401  (servers is a fixture)


def write_config(data):
    path = engines.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def fake_run(runs, name, status="done", cost=1.0, created=None, heartbeat=0):
    rd = runs / name
    (rd / "app").mkdir(parents=True)
    (rd / "state.json").write_text(json.dumps({"run_id": name, "status": status, "cost_usd": cost, "heartbeat": heartbeat,
                                               "created_at": created if created is not None else time.time()}), encoding="utf-8")
    return rd


# ------------------------------------------------------------------ push


class Opener:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def __call__(self, req, timeout=0):
        self.sent.append(req)
        if not self.ok:
            raise OSError("offline")

        class R:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()


def test_push_is_off_until_enabled_and_keeps_its_topic():
    assert push.settings() == {} and push.send("t", "m") is False
    first = push.enable()
    assert first["topic"].startswith("sf-") and len(first["topic"]) > 20 and first["server"] == "https://ntfy.sh"
    push.disable()
    assert push.settings() == {}
    assert push.enable()["topic"] == first["topic"]            # same topic: the phone stays subscribed
    assert push.enable("https://ntfy.example.org/")["server"] == "https://ntfy.example.org"
    with pytest.raises(ValueError):
        push.enable("not a url")


def test_push_sends_only_short_ascii_text_and_never_raises():
    push.enable()
    op = Opener()
    assert push.send("Software Factory needs you", "Approve the spec ✓ é" + "x" * 500, urgent=True, opener=op) is True
    req = op.sent[0]
    assert req.full_url.endswith(push.settings()["topic"]) and req.get_method() == "POST"
    assert len(req.data) <= 200 and req.data.decode("ascii")
    assert req.get_header("Priority") == "high" and req.get_header("Title") == "Software Factory needs you"
    assert push.send("t", "m", opener=Opener(ok=False)) is False   # offline: quietly false


def test_notify_with_remote_sends_a_push_but_not_without_it(monkeypatch):
    write_config({"notify": False})   # no desktop balloon in a test
    seen = []
    monkeypatch.setattr(push, "send", lambda title, msg, urgent=False: seen.append((title, msg)) or True)
    factory.notify("Software Factory", "plain")
    factory.notify("Software Factory needs you", "Approve the spec", remote=True)
    for _ in range(50):
        if seen:
            break
        time.sleep(0.05)
    assert seen == [("Software Factory needs you", "Approve the spec")]


# ------------------------------------------------------------------ spend caps


def test_spend_summary_and_caps(tmp_path):
    now = time.time()
    fake_run(tmp_path, "a", cost=3.0, created=now)
    fake_run(tmp_path, "b", cost=2.5, created=now)
    fake_run(tmp_path, "old", cost=9.0, created=now - 40 * 86400)
    s = spend.summary(tmp_path, now)
    assert s["today"] == 5.5 and s["runs_today"] == 2 and s["month"] >= 5.5 and s["daily_cap"] == 0
    assert spend.check_new_run(tmp_path, now) is None                       # no cap: always fine
    write_config({"limits": {"daily": 5, "monthly": 0}})
    assert "daily cap" in spend.check_new_run(tmp_path, now) and "--daily-cap" in spend.check_new_run(tmp_path, now)
    write_config({"limits": {"daily": 50, "monthly": 5}})
    assert "monthly cap" in spend.check_new_run(tmp_path, now)
    write_config({"limits": {"daily": "junk", "monthly": None}})
    assert spend.caps() == {"daily": 0.0, "monthly": 0.0}


def test_a_new_run_is_refused_when_the_daily_cap_is_used_up(tmp_path):
    fake_run(tmp_path, "spent", cost=7.0)
    write_config({"limits": {"daily": 5}})
    with pytest.raises(ValueError, match="daily cap"):
        factory.create_run("a tiny tool", tmp_path)
    write_config({"limits": {"daily": 0}})
    assert (factory.create_run("a tiny tool", tmp_path) / "state.json").exists()


# ------------------------------------------------------------------ clean-up


def test_cleanup_only_touches_finished_runs_and_only_rebuildable_folders(tmp_path):
    done = fake_run(tmp_path, "done-run", "done")
    (done / "app" / ".venv" / "lib").mkdir(parents=True)
    (done / "app" / ".venv" / "lib" / "big.bin").write_bytes(b"x" * 5000)
    (done / "app" / "node_modules" / "pkg").mkdir(parents=True)
    (done / "app" / "node_modules" / "pkg" / "i.js").write_text("1", encoding="utf-8")
    (done / "app" / "index.html").write_text("<html>keep me</html>", encoding="utf-8")
    (done / "app" / ".git").mkdir()
    (done / "factory").mkdir()
    (done / "factory" / "report.md").write_text("keep", encoding="utf-8")
    running = fake_run(tmp_path, "live", "running", heartbeat=time.time())
    (running / "app" / ".venv").mkdir()
    gate = fake_run(tmp_path, "waiting", "gate", heartbeat=0)
    (gate / "app" / ".venv").mkdir()

    rows = cleanup.plan(tmp_path)
    assert {r["run"] for r in rows} == {"done-run"} and {r["what"] for r in rows} == {".venv", "node_modules"}
    assert sum(r["bytes"] for r in rows) >= 5001
    res = cleanup.apply(rows, tmp_path)
    assert res["removed"] == 2 and res["freed_bytes"] >= 5001
    assert not (done / "app" / ".venv").exists() and not (done / "app" / "node_modules").exists()
    assert (done / "app" / "index.html").read_text(encoding="utf-8") == "<html>keep me</html>"
    assert (done / "app" / ".git").is_dir() and (done / "factory" / "report.md").exists()
    assert (running / "app" / ".venv").is_dir() and (gate / "app" / ".venv").is_dir()
    assert cleanup.plan(tmp_path) == []


def test_cleanup_refuses_paths_outside_the_runs_folder(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir()
    assert cleanup.apply([{"run": "x", "path": outside.as_posix(), "what": ".venv", "bytes": 1}], runs)["removed"] == 0
    assert (outside / "keep.txt").exists()
    assert cleanup.human(512) == "512 B" and cleanup.human(5 * 1024 * 1024) == "5.0 MB"


# ------------------------------------------------------------------ presets


def test_presets_save_replace_delete_and_limits(tmp_path):
    assert presets.load(tmp_path) == []
    presets.save(tmp_path, " Chores  app ", "For parents", "Welcome screen")
    items = presets.save(tmp_path, "chores app", "For parents of 3", "")          # same name: replaced
    assert items == [{"name": "chores app", "app_context": "For parents of 3", "first_open": ""}]
    presets.save(tmp_path, "Another", "", "x")
    assert [p["name"] for p in presets.load(tmp_path)] == ["Another", "chores app"]
    assert [p["name"] for p in presets.delete(tmp_path, "ANOTHER")] == ["chores app"]
    with pytest.raises(ValueError):
        presets.save(tmp_path, "", "a", "b")
    with pytest.raises(ValueError):
        presets.save(tmp_path, "empty", " ", " ")
    (tmp_path / "presets.json").write_text("{not json", encoding="utf-8")
    assert presets.load(tmp_path) == []


# ------------------------------------------------------------------ Tailscale


def test_tailscale_address_is_found_only_when_connected(monkeypatch):
    monkeypatch.setattr(remote.shutil, "which", lambda n: "/bin/tailscale")
    monkeypatch.setattr(remote.os.path, "exists", lambda p: p == "/bin/tailscale")
    run = lambda out: (lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""))
    monkeypatch.setattr(remote.subprocess, "run", run("100.101.102.103\n"))
    assert remote.tailscale_address() == "100.101.102.103"
    monkeypatch.setattr(remote.subprocess, "run", run("Tailscale is stopped.\n"))
    assert remote.tailscale_address() is None
    monkeypatch.setattr(remote.shutil, "which", lambda n: None)
    monkeypatch.setattr(remote.os.path, "exists", lambda p: False)
    assert remote.tailscale_address() is None


# ------------------------------------------------------------------ the dashboard's own endpoints


def test_dashboard_presets_push_and_spend_endpoints(servers):
    assert "spend" in json.loads(call(servers, "GET", "/api/info")[1]) and json.loads(call(servers, "GET", "/api/push")[1])["on"] is False
    saved = json.loads(call(servers, "POST", "/api/presets/save", {"name": "Kids", "app_context": "ctx", "first_open": "welcome"})[1])
    assert saved["presets"][0]["name"] == "Kids" and json.loads(call(servers, "GET", "/api/presets")[1])["presets"][0]["first_open"] == "welcome"
    assert call(servers, "POST", "/api/presets/save", {"name": "", "app_context": "x"})[0] == 400
    assert json.loads(call(servers, "POST", "/api/presets/delete", {"name": "Kids"})[1])["presets"] == []
    st = json.loads(call(servers, "POST", "/api/push", {"on": True})[1])
    assert st["on"] and st["topic"].startswith("sf-") and st["subscribe"].endswith(st["topic"])
    assert call(servers, "POST", "/api/push", {"on": True, "server": "nonsense"})[0] == 400
    assert json.loads(call(servers, "POST", "/api/push", {"on": False})[1])["on"] is False
    assert call(servers, "POST", "/api/push", {"test": True})[0] == 400          # off: no test to send


def test_phone_cannot_reach_presets_or_push_settings(servers):
    info, port = turn_on(servers)
    cookie = call(port, "GET", "/?pin=" + info["pin"])[2]["Set-Cookie"].split(";")[0]
    for path in ("/api/presets", "/api/push"):
        assert call(port, "GET", path, cookie=cookie)[0] == 403
    for path in ("/api/presets/save", "/api/presets/delete", "/api/push"):
        assert call(port, "POST", path, {"name": "x"}, cookie=cookie)[0] == 403
    assert "spend" in json.loads(call(port, "GET", "/api/info", cookie=cookie)[1])   # the spend chip also shows on the phone
    assert "away" in json.loads(call(servers, "GET", "/api/remote")[1])


# ------------------------------------------------------------------ sf.py


@pytest.fixture
def sf(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    monkeypatch.setenv("SF_HOME", str(tmp_path / "sfhome"))
    monkeypatch.setenv("SF_CONFIG", str(tmp_path / "sfhome" / "config.json"))
    spec = importlib.util.spec_from_file_location("sf_extras", Path(__file__).resolve().parent.parent / "scripts" / "sf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sf_caps_push_and_clean(sf, monkeypatch, capsys):
    assert sf.main(["configure", "--daily-cap", "25", "--monthly-cap", "200"]) == 0
    assert sf.load_config()["limits"] == {"daily": 25.0, "monthly": 200.0}
    assert sf.main(["push", "test"]) == 1 and "off" in capsys.readouterr().out.lower()
    assert sf.main(["push", "on"]) == 0
    out = capsys.readouterr().out
    assert "ntfy" in out and "sf-" in out
    assert sf.load_config()["push"]["enabled"] is True
    assert sf.main(["push", "off"]) == 0 and "off" in capsys.readouterr().out.lower()
    calls = []
    monkeypatch.setattr(sf, "factory_py", lambda args, timeout=0, capture=True: calls.append(args) or (0, "3 folders, 1.0 MB could be freed."))
    assert sf.main(["clean"]) == 0 and sf.main(["clean", "--apply"]) == 0
    assert calls == [["clean"], ["clean", "--apply"]]
