"""Bigger apps (outside services, keys, server apps) and cheaper runs (Quality, card cap, overrides). No real agent is called."""

import json
import socket
import sys
import time
from pathlib import Path

import pytest

import checks
import common
import dashboard
import deploy
import engines
import factory
import keys

ENV_JSON = {"vars": [
    {"name": "OPENAI_API_KEY", "service": "OpenAI", "purpose": "writes summaries", "secret": True,
     "get_it": "https://platform.openai.com/api-keys", "fake_mode": "canned summaries"},
    {"name": "STRIPE_PUBLISHABLE_KEY", "service": "Stripe", "purpose": "the checkout button", "secret": False, "fake_mode": "a demo checkout"},
]}


# --------------------------------------------------------------------------- the key store


def test_key_store_keeps_values_private_and_refuses_live_payment_keys():
    assert keys.load() == {}
    keys.set_key("OPENAI_API_KEY", "  sk-abcdefghijklmnopqrstuvwxyz  ")
    assert keys.load() == {"OPENAI_API_KEY": "sk-abcdefghijklmnopqrstuvwxyz"}
    assert keys.is_set(["OPENAI_API_KEY", "OTHER"]) == {"OPENAI_API_KEY": True, "OTHER": False}
    assert keys.env_for(["OTHER", "OPENAI_API_KEY"]) == {"OPENAI_API_KEY": "sk-abcdefghijklmnopqrstuvwxyz"}
    keys.set_key("STRIPE_SECRET_KEY", "sk_test_1234567890abcdef")
    assert keys.env_for(["OPENAI_API_KEY"]) == {"OPENAI_API_KEY": "sk-abcdefghijklmnopqrstuvwxyz"}  # only what the app declared
    for bad_name, value in (("lower_case", "x"), ("A", "x"), ("OK_NAME", "two\nlines")):
        with pytest.raises(ValueError):
            keys.set_key(bad_name, value)
    with pytest.raises(ValueError, match="LIVE payment key"):
        keys.set_key("STRIPE_SECRET_KEY", "sk_live_1234567890abcdef")
    assert keys.load()["STRIPE_SECRET_KEY"] == "sk_test_1234567890abcdef"
    keys.set_key("OPENAI_API_KEY", "")  # empty = remove
    assert "OPENAI_API_KEY" not in keys.load()
    assert "Never commit" in keys.store_path().read_text(encoding="utf-8")


def test_env_json_validation_and_placeholders(tmp_path):
    f = tmp_path / "env.json"
    assert keys.read_env_json(f) == ([], [])  # no outside services
    f.write_text(json.dumps(ENV_JSON), encoding="utf-8")
    rows, errs = keys.read_env_json(f)
    assert errs == [] and [r["name"] for r in rows] == ["OPENAI_API_KEY", "STRIPE_PUBLISHABLE_KEY"]
    f.write_text(json.dumps({"vars": [{"name": "bad name", "service": "x"}, {"name": "GOOD_KEY", "service": "x", "purpose": "y", "secret": "yes"}]}), encoding="utf-8")
    _, errs = keys.read_env_json(f)
    assert any("must look like" in e for e in errs) and any("fake_mode" in e for e in errs) and any("'secret'" in e for e in errs)
    f.write_text("{oops", encoding="utf-8")
    assert "not valid JSON" in keys.read_env_json(f)[1][0]
    app = tmp_path / "app"
    app.mkdir()
    assert "missing" in keys.example_problems(app, ["OPENAI_API_KEY"])[0]
    (app / ".env.example").write_text("OPENAI_API_KEY=your-key-here\n", encoding="utf-8")
    assert keys.example_problems(app, ["OPENAI_API_KEY"]) == []
    assert "STRIPE" in keys.example_problems(app, ["OPENAI_API_KEY", "STRIPE_PUBLISHABLE_KEY"])[0]


def test_a_stored_key_copied_into_the_code_is_found_and_secret_payment_keys_are_flagged(tmp_path):
    keys.set_key("MAPS_KEY", "AIzaVeryRealLookingValue123")
    (tmp_path / "app.py").write_text('KEY = "AIzaVeryRealLookingValue123"\n', encoding="utf-8")
    (tmp_path / "ok.py").write_text('KEY = os.environ.get("MAPS_KEY")\n', encoding="utf-8")
    assert keys.leaked(tmp_path, checks.iter_text_files) == [{"file": "app.py", "name": "MAPS_KEY"}]
    (tmp_path / "pay.js").write_text('const s = "sk_test_51Habcdefghijklmnop";\nconst p = "pk_test_51Habcdefghijklmnop";\n', encoding="utf-8")
    kinds = [(f["file"], f["line"], f["kind"]) for f in checks.scan_secrets(tmp_path)]
    assert ("pay.js", 1, "secret payment key") in kinds and not any(f == "pay.js" and l == 2 for f, l, _ in kinds)  # pk_ is publishable


# --------------------------------------------------------------------------- dashboard: keys panel and opening a server app


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def server_run(tmp_path):
    rd = factory.create_run("a notes app with AI summaries", tmp_path)
    s = json.loads((rd / "state.json").read_text(encoding="utf-8"))
    s.update(status="done", mode="single", title="Notes")
    (rd / "state.json").write_text(json.dumps(s), encoding="utf-8")
    app = rd / "app"
    app.mkdir()
    # a tiny server app that shows whether it received its key (never the value)
    (app / "server.py").write_text(
        "import os, http.server\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = ('real mode' if os.environ.get('OPENAI_API_KEY') else 'fake mode') + ' / other: ' + str(bool(os.environ.get('UNDECLARED_KEY')))\n"
        "        self.send_response(200); self.end_headers(); self.wfile.write(body.encode())\n"
        "    def log_message(self, *a): pass\n"
        "http.server.HTTPServer(('127.0.0.1', int(os.environ['PORT'])), H).serve_forever()\n", encoding="utf-8")
    (rd / "factory" / "app.json").write_text(json.dumps({"type": "server", "start": [sys.executable, "server.py"], "port": _free_port(),
                                                          "screens": [{"name": "home", "path": "/"}]}), encoding="utf-8")
    (rd / "factory" / "env.json").write_text(json.dumps(ENV_JSON), encoding="utf-8")
    return rd


def test_keys_panel_state_never_contains_values_and_the_app_gets_only_its_declared_keys(tmp_path, monkeypatch):
    import urllib.request
    monkeypatch.setattr(factory, "RUNS_DIR", tmp_path)
    opened = []
    monkeypatch.setattr(dashboard.webbrowser, "open", lambda url: opened.append(url))
    rd = server_run(tmp_path)
    keys.set_key("UNDECLARED_KEY", "should-never-reach-the-app")
    st = dashboard.build_state(rd.name, tmp_path)
    assert st["run"]["can_open"] is True and st["run"]["app_running"] is False
    assert [(k["name"], k["set"]) for k in st["run"]["keys"]] == [("OPENAI_API_KEY", False), ("STRIPE_PUBLISHABLE_KEY", False)]
    try:
        code, res = dashboard.act("/api/open", {"run": rd.name})
        assert code == 200 and opened == [res["url"]]
        assert urllib.request.urlopen(res["url"], timeout=5).read().decode() == "fake mode / other: False"
        assert dashboard.build_state(rd.name, tmp_path)["run"]["app_running"] is True
        code, res2 = dashboard.act("/api/keys", {"run": rd.name, "name": "OPENAI_API_KEY", "value": "sk-proj-abcdefghijklmnopqrstuvwxyz"})
        assert code == 200 and res2["restart_needed"] is True  # the running app is stopped so it picks up the key
        st = dashboard.build_state(rd.name, tmp_path)
        assert "sk-proj-abcdefghijklmnopqrstuvwxyz" not in json.dumps(st) and st["run"]["keys"][0]["set"] is True
        code, res = dashboard.act("/api/open", {"run": rd.name})
        assert code == 200 and urllib.request.urlopen(res["url"], timeout=5).read().decode() == "real mode / other: False"
        assert dashboard.act("/api/keys", {"run": rd.name, "name": "STRIPE_SECRET_KEY", "value": "sk_live_abcdefghijkl"})[0] == 400
    finally:
        assert dashboard.act("/api/stop-app", {"run": rd.name})[0] == 200
    assert dashboard.build_state(rd.name, tmp_path)["run"]["app_running"] is False


def test_autocheck_style_problems_for_outside_services(tmp_path):
    """The rules the driver applies after a build: valid env.json, every variable in .env.example, no stored key in the code."""
    rd = server_run(tmp_path)
    vars_, errs = keys.read_env_json(rd / "factory" / "env.json")
    problems = errs + keys.example_problems(rd / "app", [v["name"] for v in vars_])
    assert problems and ".env.example is missing" in problems[0]


# --------------------------------------------------------------------------- deploying server apps


def test_server_apps_get_docker_and_render_and_static_apps_keep_theirs():
    which = lambda name: "/usr/bin/" + name
    server = {o["id"]: o for o in deploy.options_for({"type": "server", "start": "x", "port": 1}, [], which)}
    assert server["docker"]["available"] and server["render"]["available"] and not server["netlify"]["available"]
    assert "use Docker image, Render" in server["netlify"]["reason"] and server["render"]["public"] is True
    static = {o["id"]: o for o in deploy.options_for({"type": "static", "entry": "index.html"}, [], which)}
    assert static["netlify"]["available"] and not static["docker"]["available"]
    for kind, target in (("cli", "package"), ("library", "package"), ("api", "docker"), ("worker", "render"), ("multi", "docker"),
                         ("desktop", "installer"), ("mobile", "expo")):
        opts = {o["id"]: o for o in deploy.options_for({"type": kind}, [], which)}
        assert opts[target]["available"], (kind, target)
        assert [o for o in opts.values() if o["available"]], kind
    godot = {o["id"]: o for o in deploy.options_for({"type": "godot"}, [], lambda n: None)}
    assert not godot["godot-export"]["available"] and "Godot 4" in godot["godot-export"]["reason"]
    nodocker = {o["id"]: o for o in deploy.options_for({"type": "server", "start": "x", "port": 1}, [], lambda n: None if n == "docker" else "/x")}
    assert not nodocker["docker"]["available"] and "Docker Desktop" in nodocker["docker"]["reason"]


def test_export_app_ships_code_but_not_secrets_databases_or_history(tmp_path):
    rd = server_run(tmp_path)
    for rel, text in ((".env", "OPENAI_API_KEY=x"), (".git/HEAD", "ref"), ("data.db", "x"), (".venv/x.py", "x"), ("pyproject.toml", "[project]")):
        (rd / "app" / rel).parent.mkdir(parents=True, exist_ok=True)
        (rd / "app" / rel).write_text(text, encoding="utf-8")
    dest, n = deploy.export_app(rd)
    shipped = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert shipped == ["pyproject.toml", "server.py"] and n == 2
    keys.set_key("OPENAI_API_KEY", "sk-proj-zzzzzzzzzzzzzzzzzzzzzz")
    (rd / "app" / "config.py").write_text('K = "sk-proj-zzzzzzzzzzzzzzzzzzzzzz"', encoding="utf-8")
    with pytest.raises(deploy.DeployError, match="something secret"):
        deploy.export_app(rd)


def test_a_handoff_result_needs_the_link_and_steps(tmp_path):
    out = tmp_path / "render.json"
    out.write_text(json.dumps({"status": "handoff", "summary": "Ready: open the link"}), encoding="utf-8")
    assert "next_steps" in deploy.validate_result(out, tmp_path)
    out.write_text(json.dumps({"status": "handoff", "summary": "s", "next_steps": ["open it"]}), encoding="utf-8")
    assert "https link" in deploy.validate_result(out, tmp_path)
    out.write_text(json.dumps({"status": "handoff", "summary": "s", "next_steps": ["open it"],
                               "url": "https://render.com/deploy?repo=https://github.com/me/notes"}), encoding="utf-8")
    assert deploy.validate_result(out, tmp_path) is None


# --------------------------------------------------------------------------- cheaper runs


def test_quality_reaches_the_run_and_the_estimates():
    lean = checks.estimate("multi", "medium", "lean")["usd"]
    balanced = checks.estimate("multi", "medium", "balanced")["usd"]
    mx = checks.estimate("multi", "medium", "max")["usd"]
    assert lean[1] < balanced[1] < mx[1] and checks.estimate("single", "small", "lean")["usd"][1] < checks.estimate("single", "small")["usd"][1]
    assert factory.run_quality(type("R", (), {"state": {"inputs": {"quality": "lean"}}})()) == "lean"
    assert factory.run_quality(type("R", (), {"state": {"inputs": {"quality": "silly"}}})()) == "balanced"


def test_quality_is_saved_with_a_new_run(tmp_path):
    rd = factory.create_run({"idea": "x", "quality": "lean"}, tmp_path)
    assert json.loads((rd / "state.json").read_text(encoding="utf-8"))["inputs"]["quality"] == "lean"
    rd = factory.create_run({"idea": "y", "quality": "nonsense"}, tmp_path)
    assert json.loads((rd / "state.json").read_text(encoding="utf-8"))["inputs"]["quality"] == "balanced"


def test_owner_overrides_win_in_every_quality_and_retries_still_escalate(tmp_path, monkeypatch):
    cfg = Path(tmp_path / "config.json")
    cfg.write_text(json.dumps({"agents": {"sf-planner": {"model": "claude-haiku-5-5", "effort": "low"}, "sf-writer": {"effort": "nonsense"}}}), encoding="utf-8")
    monkeypatch.setenv("SF_CONFIG", str(cfg))
    r = common.load_role("sf-planner")
    assert r.overridden and (r.model, r.effort) == ("claude-haiku-5-5", "low")
    for q in ("lean", "balanced", "max"):
        assert factory.pick_model(r, False, q) == ("claude-haiku-5-5", "low")
    assert factory.pick_model(r, True, "balanced") == ("claude-opus-5-5", "high")
    assert common.load_role("sf-writer").overridden is False  # an invalid effort is ignored
    # on another engine, an override that isn't a Claude model is that engine's own model name
    assert engines.Codex({"engine": "codex"}).model_for("my-codex-model") == "my-codex-model"
