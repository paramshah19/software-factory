"""scripts/sf.py, the helper behind both skills. No real agent or dashboard is started."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def sf(tmp_path, monkeypatch):
    monkeypatch.setenv("SF_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SF_CONFIG", str(tmp_path / "home" / "config.json"))
    spec = importlib.util.spec_from_file_location("sf_under_test", REPO / "scripts" / "sf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_configure_saves_engine_and_rejects_bad_custom_commands(sf, capsys):
    assert sf.main(["configure", "--engine", "codex", "--host", "codex", "--models", "strong=big,fast=small,bogus=x"]) == 0
    cfg = sf.load_config()
    assert cfg["engine"] == "codex" and cfg["host"] == "codex" and cfg["models"] == {"strong": "big", "fast": "small"}
    assert Path(cfg["factory_dir"]) == REPO
    assert sf.main(["configure", "--engine", "custom"]) == 1  # no command
    assert sf.main(["configure", "--engine", "custom", "--command", "gemini --yolo"]) == 1  # not a JSON list
    assert sf.main(["configure", "--engine", "custom", "--command", '["gemini", "--yolo"]', "--label", "Gemini CLI", "--prompt", "stdin"]) == 0
    assert sf.load_config()["custom"] == {"command": ["gemini", "--yolo"], "prompt": "stdin", "label": "Gemini CLI"}
    assert sf.main(["configure", "--factory-dir", str(REPO / "docs")]) == 1  # not a factory


def test_install_skills_copies_both_skills_and_a_working_shortcut(sf, tmp_path, capsys):
    assert sf.main(["install-skills", "--dir", str(tmp_path / "x")]) == 1  # configure first
    sf.main(["configure", "--engine", "claude"])
    target = tmp_path / "skills"
    assert sf.main(["install-skills", "--dir", str(target)]) == 0
    for name in sf.SKILLS:
        assert (target / name / "SKILL.md").read_text(encoding="utf-8") == (REPO / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    shim = tmp_path / "home" / "sf.py"
    p = subprocess.run([sys.executable, str(shim), "configure", "--json"], capture_output=True, text=True,
                       env={**__import__("os").environ, "SF_HOME": str(tmp_path / "home"), "SF_CONFIG": str(tmp_path / "home" / "config.json")})
    assert p.returncode == 0 and json.loads(p.stdout)["engine"] == "claude"
    assert sf.main(["install-skills", "--host", "some-unknown-agent"]) == 2  # unknown folder: says where the skills are


def test_idea_builds_the_run_inputs_and_opens_the_run(sf, tmp_path, monkeypatch, capsys):
    sf.main(["configure", "--engine", "claude"])
    sent, opened = [], []
    monkeypatch.setattr(sf, "ensure_dashboard", lambda **kw: (0, {"running": True, "ours": True, "url": "http://127.0.0.1:8787"}))
    monkeypatch.setattr(sf, "http", lambda path, body=None, **kw: (sent.append((path, body)) or (200, {"ok": True, "run": "20261024-r1"})))
    monkeypatch.setattr(sf.webbrowser, "open", lambda url: opened.append(url))
    photo = tmp_path / "ref.png"
    photo.write_bytes(b"\x89PNG fake")
    idea_file = tmp_path / "idea.txt"
    idea_file.write_text('a "quoted" idea with $symbols', encoding="utf-8")
    capsys.readouterr()
    assert sf.main(["idea", "--idea-file", str(idea_file), "--link", "https://a.example", "--photo", str(photo), "--mode", "single", "--budget", "5", "--json"]) == 0
    path, body = sent[0]
    assert path == "/api/new" and body["idea"] == 'a "quoted" idea with $symbols' and body["product_links"] == ["https://a.example"]
    assert body["force_mode"] == "single" and body["budget_usd"] == 5 and body["photos"][0]["name"] == "ref.png"
    assert opened == ["http://127.0.0.1:8787/?run=20261024-r1"]
    assert json.loads(capsys.readouterr().out)["run"] == "20261024-r1"
    assert sf.main(["idea", "--generate", "--no-open"]) == 0 and sent[-1][1]["generate"] is True and len(opened) == 1
    assert sf.main(["idea"]) == 1  # nothing to build
    assert sf.main(["idea", "x", "--photo", str(tmp_path / "missing.png")]) == 1


def test_dashboard_status_tells_our_dashboard_from_another_one(sf, monkeypatch):
    replies = {"/api/info": (200, {"app": "software-factory", "factory_dir": REPO.as_posix()})}
    monkeypatch.setattr(sf, "http", lambda path, body=None, **kw: replies.get(path, (0, {})))
    assert sf.dashboard_status() == {"running": True, "ours": True, "factory_dir": REPO.as_posix(), "url": "http://127.0.0.1:8787"}
    replies["/api/info"] = (200, {"app": "software-factory", "factory_dir": "C:/somewhere/else"})
    assert sf.dashboard_status()["ours"] is False
    rc, st = sf.ensure_dashboard()
    assert rc == 1 and "another Software Factory dashboard" in st["error"]
    replies.clear()
    assert sf.dashboard_status()["running"] is False


def test_agents_lists_every_station(sf, capsys):
    sf.main(["configure", "--engine", "claude"])
    capsys.readouterr()
    assert sf.main(["agents", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    names = {a["agent"] for a in data["agents"]}
    assert len(names) == len(list((REPO / "agents").glob("sf-*.md"))) >= 20 and "sf-maker" in names
    assert all(a["model"].startswith("claude-") and a["effort"] for a in data["agents"])


def test_agents_set_and_reset_the_owners_own_model_and_effort(sf, capsys):
    sf.main(["configure", "--engine", "claude"])
    assert sf.main(["agents", "set", "sf-planner", "--model", "claude-opus-5-5", "--effort", "xhigh"]) == 0
    assert sf.load_config()["agents"] == {"sf-planner": {"model": "claude-opus-5-5", "effort": "xhigh"}}
    capsys.readouterr()
    assert sf.main(["agents", "--json"]) == 0
    row = next(a for a in json.loads(capsys.readouterr().out)["agents"] if a["agent"] == "sf-planner")
    assert row["yours"] is True and (row["model"], row["effort"]) == ("claude-opus-5-5", "xhigh")
    assert sf.main(["agents", "set", "sf-planner", "--effort", "low"]) == 0  # one option alone keeps the other
    assert sf.load_config()["agents"]["sf-planner"] == {"model": "claude-opus-5-5", "effort": "low"}
    assert sf.main(["agents", "set", "sf-unknown", "--effort", "low"]) == 1
    assert sf.main(["agents", "set", "sf-planner"]) == 1  # nothing to set
    assert sf.main(["agents", "reset", "all"]) == 0 and sf.load_config()["agents"] == {}


def test_doctor_reports_what_this_computer_can_build(sf, capsys, monkeypatch):
    capsys.readouterr()
    sf.main(["doctor", "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert set(rep["can_build"]) >= {"static", "server", "cli", "api", "worker", "library", "multi", "mobile", "desktop", "godot"}
    assert rep["can_build"]["api"] == "yes"


def test_setup_answers_become_saved_defaults(sf, tmp_path, monkeypatch):
    fdir = tmp_path / "factory"
    fdir.mkdir()
    (fdir / "factory.py").write_text("# stand-in", encoding="utf-8")
    brand = tmp_path / "brand.txt"
    brand.write_text("Colours: deep green #0b6e4f and cream. Font: Inter. Tone: warm, short sentences.", encoding="utf-8")
    assert sf.main(["configure", "--factory-dir", str(fdir), "--engine", "claude", "--quality", "lean", "--budget-single", "3",
                    "--budget-multi", "900", "--notify", "off", "--brand-file", str(brand)]) == 0
    cfg = sf.load_config()
    assert cfg["quality"] == "lean" and cfg["budget"] == {"single": 3.0, "multi": 500.0} and cfg["notify"] is False
    doc = (fdir / "always-use" / "brand.md").read_text(encoding="utf-8")
    assert doc.startswith("# Brand rules") and "#0b6e4f" in doc
    assert sf.main(["configure", "--quality", "max"]) == 0 and sf.load_config()["budget"]["single"] == 3.0  # other answers are kept
