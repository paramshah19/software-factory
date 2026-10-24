"""Tests for the deploy step. The agent is faked; the files, state and permission rules are real."""

import json
from pathlib import Path

import pytest

import deploy
import factory
from factory import AgentResult

MCP_LIST = """Checking MCP server health…

claude.ai Claude Docs: https://api.anthropic.com/v1/pages/mcp - \u2714 Connected
claude.ai Netlify: https://netlify-mcp.netlify.app/mcp - \u2714 Connected
claude.ai Canva: https://mcp.canva.com/mcp - ! Needs authentication
local-notes: node C:/tools/notes-server/index.js - \u2714 Connected
"""
STATIC = {"type": "static", "entry": "index.html", "screens": [{"name": "home", "path": "index.html"}]}


def which_all(name):
    return "/usr/bin/" + name


def which_none(name):
    return None


def finished_run(tmp_path, app_json=STATIC, files=None):
    rd = factory.create_run({"idea": "a tiny timer"}, tmp_path)
    s = json.loads((rd / "state.json").read_text(encoding="utf-8"))
    s.update(status="done", mode="single", title="Tiny Timer")
    (rd / "state.json").write_text(json.dumps(s), encoding="utf-8")
    (rd / "factory").mkdir(exist_ok=True)
    (rd / "factory" / "app.json").write_text(json.dumps(app_json), encoding="utf-8")
    for rel, text in (files or {"index.html": "<!doctype html><title>Timer</title><p>hi</p>"}).items():
        p = rd / "app" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return rd


# --------------------------------------------------------------------------- what is signed in


def test_the_connector_list_is_parsed_the_way_claude_prints_it():
    rows = deploy.parse_mcp_list(MCP_LIST)
    by = {r["name"]: r["connected"] for r in rows}
    assert by == {"claude.ai Claude Docs": True, "claude.ai Netlify": True, "claude.ai Canva": False, "local-notes": True}
    assert deploy.connector_allow("claude.ai Netlify") == "mcp__claude_ai_Netlify"


def test_options_prefer_a_connected_connector_and_explain_what_is_missing():
    rows = deploy.parse_mcp_list(MCP_LIST)
    opts = {o["id"]: o for o in deploy.options_for(STATIC, rows, which_all)}
    assert opts["netlify"]["available"] and opts["netlify"]["connector"] == "claude.ai Netlify" and "connector" in opts["netlify"]["via"]
    assert opts["vercel"]["available"] and "command line" in opts["vercel"]["via"]
    assert opts["desktop"]["available"] and not opts["desktop"]["public"]
    bare = {o["id"]: o for o in deploy.options_for(STATIC, [], which_none)}
    assert not bare["vercel"]["available"] and "Node.js" in bare["vercel"]["reason"]
    assert not bare["github-pages"]["available"] and "GitHub" in bare["github-pages"]["reason"]


def test_apps_that_need_a_server_cannot_be_deployed_to_static_hosts():
    opts = {o["id"]: o for o in deploy.options_for({"type": "server"}, deploy.parse_mcp_list(MCP_LIST), which_all)}
    assert not any(opts[t]["available"] for t in ("netlify", "vercel", "github-pages", "desktop")) and "server" in opts["netlify"]["reason"]
    assert opts["docker"]["available"] and opts["render"]["available"]
    assert not any(o["available"] for o in deploy.options_for(None, [], which_all))


def test_connector_detection_is_cached_and_survives_a_missing_claude(monkeypatch):
    deploy._cache.update(at=0.0, rows=[])
    calls = []
    rows = deploy.detect_connectors(run=lambda a, c, t: (calls.append(a) or (0, MCP_LIST)), which=which_all, fresh=True)
    assert len(rows) == 4 and deploy.detect_connectors(run=lambda a, c, t: (calls.append(a) or (0, "")), which=which_all) == rows and len(calls) == 1
    assert deploy.detect_connectors(which=which_none, fresh=True) == []


# --------------------------------------------------------------------------- the folder that ships


def test_only_the_app_ships_not_its_tests_or_tooling(tmp_path):
    rd = finished_run(tmp_path, files={"index.html": "<p>hi</p>", "style.css": "p{}", "tests/test_x.py": "x", "pyproject.toml": "x",
                                       "main.py": "x", "README.md": "x", ".venv/lib.js": "x", "assets/logo.svg": "<svg/>"})
    site, count = deploy.export_site(rd, STATIC)
    shipped = sorted(p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file())
    assert shipped == ["assets/logo.svg", "index.html", "style.css"] and count == 3


def test_a_secret_in_the_app_blocks_the_deploy(tmp_path):
    rd = finished_run(tmp_path, files={"index.html": "<p>hi</p>", "app.js": 'const api_key = "sk-live-aaaaaaaaaaaaaaaaaaaaaaaa";'})
    with pytest.raises(deploy.DeployError, match="secret"):
        deploy.export_site(rd, STATIC)


def test_a_missing_entry_file_is_refused(tmp_path):
    rd = finished_run(tmp_path)
    with pytest.raises(deploy.DeployError):
        deploy.export_site(rd, {"type": "static", "entry": "../etc/passwd"})


# --------------------------------------------------------------------------- the agent's answer


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_results_are_validated(tmp_path):
    p = tmp_path / "r.json"
    ok = {"status": "deployed", "url": "https://x.netlify.app", "summary": "Live."}
    assert deploy.validate_result(write_json(p, ok), tmp_path) is None
    assert "https" in deploy.validate_result(write_json(p, {**ok, "url": "http://x"}), tmp_path)
    assert "status" in deploy.validate_result(write_json(p, {**ok, "status": "great"}), tmp_path)
    assert "summary" in deploy.validate_result(write_json(p, {"status": "failed"}), tmp_path)
    assert "next_steps" in deploy.validate_result(write_json(p, {"status": "needs_login", "summary": "s"}), tmp_path)
    assert deploy.validate_result(write_json(p, {"status": "needs_login", "summary": "s", "next_steps": ["run npx netlify-cli login"]}), tmp_path) is None
    assert "path" in deploy.validate_result(write_json(p, {"status": "built", "summary": "s", "path": "deploy/missing"}), tmp_path)
    (tmp_path / "deploy" / "desktop").mkdir(parents=True)
    assert deploy.validate_result(write_json(p, {"status": "built", "summary": "s", "path": "deploy/desktop"}), tmp_path) is None
    assert "valid JSON" in deploy.validate_result(tmp_path / "nope.json", tmp_path)


# --------------------------------------------------------------------------- the whole step


class FakeDeployer:
    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, run, cid, role, prompt, cwd, escalate):
        self.calls.append((role.name, prompt, list(run.extra_allow)))
        target = prompt.split("TARGET: ")[1].split(" ")[0]
        write_json(run.fdir / "deploy" / f"{target}.json", self.result)
        return AgentResult(ok=True, cost=0.02)


def test_a_deploy_records_the_result_and_gives_the_agent_only_its_own_connector(tmp_path):
    rd = finished_run(tmp_path)
    agent = FakeDeployer({"status": "deployed", "url": "https://tiny.netlify.app", "summary": "Your app is live."})
    entry = deploy.deploy_run(rd, "netlify", agent=agent, connectors=deploy.parse_mcp_list(MCP_LIST), which=which_all)
    assert entry["status"] == "deployed" and entry["url"] == "https://tiny.netlify.app"
    role, prompt, allow = agent.calls[0]
    assert role == "sf-deployer" and "FOLDER TO SHIP" in prompt and "claude.ai Netlify" in prompt
    assert allow == ["mcp__claude_ai_Netlify"]  # not Canva, Gmail or anything else
    s = json.loads((rd / "state.json").read_text(encoding="utf-8"))
    assert s["deploys"][0]["url"] == "https://tiny.netlify.app" and s["deploy"]["status"] == "deployed" and s["status"] == "done"
    assert (rd / "deploy" / "site" / "index.html").exists()
    assert "budget_usd" not in s  # the temporary no-cap setting is removed again


def test_an_unfinished_run_cannot_be_deployed(tmp_path):
    rd = finished_run(tmp_path)
    s = json.loads((rd / "state.json").read_text(encoding="utf-8"))
    s["status"] = "gate"
    (rd / "state.json").write_text(json.dumps(s), encoding="utf-8")
    with pytest.raises(deploy.DeployError, match="approve"):
        deploy.deploy_run(rd, "netlify", agent=FakeDeployer({}), connectors=[], which=which_all)


def test_not_signed_in_is_reported_with_steps_not_as_a_crash(tmp_path):
    rd = finished_run(tmp_path)
    agent = FakeDeployer({"status": "needs_login", "summary": "You are not signed in to Vercel.", "next_steps": ["Run npx vercel login", "Press Deploy again"]})
    entry = deploy.deploy_run(rd, "vercel", agent=agent, connectors=[], which=which_all)
    assert entry["status"] == "needs_login" and entry["next_steps"][0].startswith("Run npx")
    assert agent.calls[0][2] == []  # no connector rule when none is connected


def test_an_agent_that_writes_nothing_gives_a_failed_entry(tmp_path):
    rd = finished_run(tmp_path)
    silent = lambda run, cid, role, prompt, cwd, escalate: AgentResult(ok=True)
    entry = deploy.deploy_run(rd, "desktop", agent=silent, connectors=[], which=which_all)
    assert entry["status"] == "failed" and "missing" in entry["details"]


def test_deploy_refuses_an_unavailable_target(tmp_path):
    rd = finished_run(tmp_path, app_json={"type": "server", "start": "x", "port": 1, "screens": [{"name": "home", "path": "/"}]})
    with pytest.raises(deploy.DeployError, match="server"):
        deploy.deploy_run(rd, "netlify", agent=FakeDeployer({}), connectors=[], which=which_all)
    with pytest.raises(deploy.DeployError, match="unknown"):
        deploy.deploy_run(rd, "heroku", agent=FakeDeployer({}), connectors=[], which=which_all)


# --------------------------------------------------------------------------- what the deployer may do


def cmd_for(name, tmp_path, extra=()):
    role = factory.load_role(name)
    return factory.claude_cmd(role, role.model, role.effort, tmp_path, tmp_path / "p.md", tuple(extra))


def test_only_the_deployer_may_push_and_never_with_force(tmp_path):
    dep = cmd_for("sf-deployer", tmp_path, ["mcp__claude_ai_Netlify"])
    deny = dep[dep.index("--disallowedTools") + 1: dep.index("--allowedTools")]
    allow = dep[dep.index("--allowedTools") + 1:]
    assert "Bash(git push:*)" not in deny and "Bash(git push --force:*)" in deny and "Bash(git push -f:*)" in deny
    assert "Bash(gh:*)" in allow and "Bash(npx:*)" in allow and "mcp__claude_ai_Netlify" in allow
    assert "WebSearch" not in allow  # the deployer uses its tools, not the open web
    for name in factory.ROLES:  # everyone else still cannot push and has no deploy tools
        other = cmd_for(name, tmp_path)
        assert "Bash(git push:*)" in other and "Bash(gh:*)" not in other and "Bash(npx:*)" not in other


def test_the_deployer_role_parses_and_is_not_part_of_the_pipeline():
    r = factory.load_role("sf-deployer")
    assert r.push and "gh" in r.commands and "npx" in r.commands and r.model in ("claude-sonnet-5-5", "claude-opus-5-5")
    assert "sf-deployer" not in factory.ROLES


def test_codex_connectors_are_read_from_its_json_list(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"engine": "codex"}', encoding="utf-8")
    monkeypatch.setenv("SF_CONFIG", str(cfg))
    listing = '[{"name": "netlify", "enabled": true}, {"name": "vercel", "enabled": false}, {"oops": 1}]'
    calls = []
    rows = deploy.detect_connectors(run=lambda a, c, t: (calls.append(a) or (0, listing)), which=which_all, fresh=True)
    assert calls[0][1:] == ["mcp", "list", "--json"]
    assert rows == [{"name": "netlify", "status": "enabled", "connected": True}, {"name": "vercel", "status": "disabled", "connected": False}]
    assert deploy.matching_connectors("netlify", rows)[0]["name"] == "netlify"
    assert deploy.parse_codex_mcp_list("not json") == [] and deploy.parse_codex_mcp_list('{"a": 1}') == []
