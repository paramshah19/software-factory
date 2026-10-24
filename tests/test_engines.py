"""The engine layer: Claude Code (default), Codex and any headless agent command line. No real agent is called."""

import json
import sys
from pathlib import Path

import engines
import factory
from common import Role

REPO = Path(__file__).resolve().parent.parent


def write_cfg(tmp_path, monkeypatch, cfg):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("SF_CONFIG", str(p))
    return p


def test_engine_defaults_to_claude_and_reads_the_config(tmp_path, monkeypatch):
    assert engines.engine_name() == "claude" and engines.current() is None
    assert engines.describe()["reports_cost"] is True
    write_cfg(tmp_path, monkeypatch, {"engine": "codex"})
    assert engines.engine_name() == "codex" and isinstance(engines.current(), engines.Codex)
    assert engines.describe() == {"name": "codex", "label": "Codex", "reports_cost": False, "enforces_permissions": False}
    write_cfg(tmp_path, monkeypatch, {"engine": "something-else"})
    assert engines.engine_name() == "claude"
    (tmp_path / "config.json").write_text("not json", encoding="utf-8")
    assert engines.engine_name() == "claude"


def test_claude_models_map_to_tiers_and_configured_models():
    assert [engines.tier(m) for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "")] == ["strong", "standard", "fast", "standard"]
    eng = engines.Codex({"engine": "codex", "models": {"strong": "big-model", "fast": ""}})
    assert eng.model_for("claude-opus-5-5") == "big-model"
    assert eng.model_for("claude-haiku-5-5") == "" and eng.model_label("claude-haiku-5-5") == "Codex default (fast)"


def test_host_rule_follows_the_operating_system(monkeypatch):
    for system, word in (("Windows", "Windows host"), ("Darwin", "macOS host"), ("Linux", "Linux host")):
        monkeypatch.setattr(engines.platform, "system", lambda s=system: s)
        assert engines.host_rule().startswith(word)
    text = factory.protocol_text()
    assert "{{HOST_RULE}}" not in text and "host." in text and "Param" not in text


def test_codex_command(tmp_path):
    pf = tmp_path / "p.md"
    pf.write_text("PROMPT", encoding="utf-8")
    eng = engines.Codex({"engine": "codex"})
    argv, stdin = eng.command("", "max", tmp_path / "app", tmp_path, False, pf)
    assert argv[1:4] == ["exec", "--json", "--skip-git-repo-check"] and stdin == "PROMPT" and argv[-1] == "-"
    assert "workspace-write" in argv and 'model_reasoning_effort="xhigh"' in argv and "-m" not in argv
    assert argv[argv.index("-C") + 1] == (tmp_path / "app").as_posix() and argv[argv.index("--add-dir") + 1] == tmp_path.as_posix()
    argv, _ = eng.command("my-model", "medium", tmp_path, tmp_path, True, pf)
    assert argv[argv.index("-m") + 1] == "my-model" and 'web_search="live"' in argv


def test_codex_parser_success_and_failure():
    ok = engines.CodexParser("C:/runs/r1")
    lines = []
    for ev in ({"type": "thread.started", "thread_id": "x"}, {"type": "turn.started"},
               {"type": "item.completed", "item": {"type": "command_execution", "command": "uv run pytest -q", "exit_code": 1, "aggregated_output": "1 failed"}},
               {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "C:/runs/r1/app/main.py", "kind": "add"}]}},
               {"type": "item.completed", "item": {"type": "agent_message", "text": "Wrote the app."}},
               {"type": "turn.completed", "usage": {"input_tokens": 1200, "output_tokens": 300}}):
        lines += ok.feed(json.dumps(ev))
    ok.finish(0)
    assert ok.ok and ok.text == "Wrote the app." and ok.error == ""
    assert "▸ Shell uv run pytest -q" in lines and "✗ exit 1: 1 failed" in lines and "▸ Add <run>/app/main.py" in lines
    assert lines[-1].startswith("■ finished · 1,500 tokens")
    # the real failure Codex printed on this machine (exit code 0, but the turn failed)
    bad = engines.CodexParser()
    msg = json.dumps({"type": "error", "status": 400, "error": {"type": "invalid_request_error", "message": "The 'gpt-x' model is not supported."}})
    for ev in ({"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": "Model metadata not found."}},
               {"type": "turn.started"}, {"type": "error", "message": msg}, {"type": "turn.failed", "error": {"message": msg}}):
        bad.feed(json.dumps(ev))
    bad.finish(0)
    assert not bad.ok and bad.error == "The 'gpt-x' model is not supported."
    crashed = engines.CodexParser()
    crashed.feed("not json at all")
    crashed.finish(0)
    assert not crashed.ok and "before finishing" in crashed.error


def test_custom_command_placeholders_and_prompt_modes(tmp_path):
    pf = tmp_path / "p.md"
    pf.write_text("THE PROMPT", encoding="utf-8")
    base = {"label": "Gemini CLI", "command": ["python", "--cwd", "{cwd}"], "model_args": ["-m", "{model}"]}
    eng = engines.Custom({"engine": "custom", "custom": base})
    argv, stdin = eng.command("", "high", tmp_path, tmp_path, False, pf)
    assert argv[1:] == ["--cwd", tmp_path.as_posix()] and stdin == "THE PROMPT" and eng.label == "Gemini CLI" and not eng.uses_effort
    argv, _ = eng.command("m1", "high", tmp_path, tmp_path, False, pf)
    assert argv[-2:] == ["-m", "m1"]
    eng = engines.Custom({"engine": "custom", "custom": {**base, "prompt": "arg"}})
    argv, stdin = eng.command("", "high", tmp_path, tmp_path, False, pf)
    assert argv[-1] == "THE PROMPT" and stdin is None
    eng = engines.Custom({"engine": "custom", "custom": {"command": ["python", "-f", "{prompt_file}"], "prompt": "file", "effort_args": ["--effort", "{effort}"]}})
    argv, stdin = eng.command("", "low", tmp_path, tmp_path, False, pf)
    assert argv[1:] == ["-f", pf.as_posix(), "--effort", "low"] and stdin is None and eng.uses_effort


FAKE_AGENT = r'''
import sys, pathlib
prompt = sys.stdin.read()
if "FAIL" in prompt:
    print("something went wrong"); sys.exit(3)
if "pong" in prompt:
    print("pong"); sys.exit(0)
pathlib.Path("made-by-agent.txt").write_text("hello", encoding="utf-8")
print("working on it")
print("done: wrote made-by-agent.txt")
'''


class StubRun:
    def __init__(self, rd):
        self.rd = rd
        self.updates = {}

    def card_update(self, card_id, **kw):
        self.updates[card_id] = kw

    def system_extras(self, role):
        return ""


def fake_custom(tmp_path, monkeypatch):
    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE_AGENT, encoding="utf-8")
    write_cfg(tmp_path, monkeypatch, {"engine": "custom", "custom": {"label": "Fake agent", "command": [sys.executable, str(script)], "prompt": "stdin"}})


def test_a_custom_agent_runs_a_station_and_its_output_reaches_the_log(tmp_path, monkeypatch):
    fake_custom(tmp_path, monkeypatch)
    rd = tmp_path / "run"
    (rd / "logs").mkdir(parents=True)
    (rd / "app").mkdir()
    run = StubRun(rd)
    role = Role(name="sf-maker", model="claude-opus-5-5", effort="medium", body="# Role: Maker\nBuild it.")
    res = factory.run_claude(run, "make", role, "Build the app.", rd / "app")
    assert res.ok and "done: wrote made-by-agent.txt" in res.text and res.cost == 0
    assert (rd / "app" / "made-by-agent.txt").read_text(encoding="utf-8") == "hello"
    log = (rd / "logs" / "make.log").read_text(encoding="utf-8")
    assert "sf-maker · Fake agent · default model · effort medium" in log and "working on it" in log
    prompt = (rd / "logs" / "prompts" / "make.md").read_text(encoding="utf-8")
    assert "shared protocol" in prompt and "# Role: Maker" in prompt and "Build the app." in prompt  # instructions + task in one message
    assert run.updates["make"]["model"] == "Fake agent default (strong)"
    res = factory.run_claude(run, "fail", role, "FAIL please", rd / "app")
    assert not res.ok and "exit code 3" in res.error and "something went wrong" in res.error


def test_check_pings_a_custom_agent(tmp_path, monkeypatch, capsys):
    fake_custom(tmp_path, monkeypatch)
    monkeypatch.setattr(factory, "RUNS_DIR", tmp_path / "runs")
    assert factory.cmd_check() == 0
    printed = capsys.readouterr().out
    assert "engine: Fake agent" in printed and "ok  default model @ medium" in printed
    assert printed.count("default model @") == 1  # no effort settings: one ping is enough


def test_a_custom_agent_that_cannot_start_fails_cleanly(tmp_path, monkeypatch):
    write_cfg(tmp_path, monkeypatch, {"engine": "custom", "custom": {"command": ["definitely-not-a-real-agent-cli"]}})
    rd = tmp_path / "run"
    (rd / "logs").mkdir(parents=True)
    res = factory.run_claude(StubRun(rd), "x", Role(name="sf-writer", model="claude-haiku-5-5", effort="medium", body="b"), "p", rd)
    assert not res.ok and "could not start" in res.error


def test_skill_copies_match_the_source():
    for name in ("software-factory-setup", "software-factory-idea"):
        src = (REPO / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert src.startswith(f"---\nname: {name}\ndescription: ")
        for copy in (".claude/skills", ".agents/skills"):
            assert (REPO / copy / name / "SKILL.md").read_text(encoding="utf-8") == src, f"{copy}/{name} is out of date: copy skills/{name} again"
