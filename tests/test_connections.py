"""Connections: the connectors and skills your own agent has, and which factory agents may use them."""

import json
from pathlib import Path

import pytest

import checks
import connections
import dashboard
import engines
import factory
from common import Role
from test_remote import call, servers, turn_on  # noqa: F401  (servers is a fixture)


def write_config(data):
    path = engines.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: h))
    return h


SECRET = "sk-live-VERY-SECRET-TOKEN"


def fake_agents(home):
    (home / ".gemini").mkdir()
    (home / ".gemini" / "settings.json").write_text(json.dumps({"mcpServers": {
        "remote-docs": {"httpUrl": f"https://docs.example.com/mcp?key={SECRET}", "headers": {"Authorization": f"Bearer {SECRET}"}},
        "local-tool": {"command": "/usr/bin/node", "args": ["server.js", SECRET], "env": {"TOKEN": SECRET}},
        "off-one": {"command": "x", "disabled": True}}}), encoding="utf-8")
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"figma-ish": {"url": "https://mcp.example.com/sse"}}}), encoding="utf-8")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(f'[mcp_servers.node_repl]\ncommand = "node"\nargs = ["repl.js", "{SECRET}"]\n\n'
                                                 f'[mcp_servers.github]\nurl = "https://api.example.com/mcp"\nbearer_token_env_var = "{SECRET}"\n', encoding="utf-8")
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text(json.dumps({"mcp": {"sentry": {"type": "remote", "url": "https://mcp.sentry.dev/mcp"}}}), encoding="utf-8")


# ------------------------------------------------------------------ connectors


def test_connectors_from_every_agent_are_listed_without_secrets(home):
    fake_agents(home)
    rows = connections.discover(home=home, which=lambda n: None)   # no agent command line is run
    by = {(r["agent"], r["name"]): r for r in rows}
    assert {k for k in by} == {("gemini", "remote-docs"), ("gemini", "local-tool"), ("gemini", "off-one"), ("cursor", "figma-ish"),
                               ("codex", "node_repl"), ("codex", "github"), ("opencode", "sentry")}
    assert by[("gemini", "remote-docs")]["where"] == "docs.example.com" and by[("gemini", "remote-docs")]["kind"] == "remote"
    assert by[("gemini", "local-tool")]["where"] == "node" and by[("gemini", "local-tool")]["kind"] == "local"
    assert by[("gemini", "off-one")]["connected"] is False
    assert SECRET not in json.dumps(rows) and "Bearer" not in json.dumps(rows) and "key=" not in json.dumps(rows)   # names, kind and host only
    assert all(r["scope"] == "off" and not r["enforced"] for r in rows)   # engine is Claude Code: other agents' connectors can't be used


def test_the_engines_own_connectors_show_health_and_can_be_scoped(home):
    run = lambda argv, cwd, timeout=0: (0, "claude.ai Gmail: https://gmail.mcp.claude.com/mcp - ✓ Connected\nclaude.ai Canva: https://mcp.canva.com/mcp - ! Needs authentication\n")
    import deploy
    deploy._cache.update(at=0.0, rows=[])
    rows = connections.discover(fresh=True, home=home, run=run, which=lambda n: "/bin/claude" if n == "claude" else None)
    gm = next(r for r in rows if r["name"] == "claude.ai Gmail")
    cv = next(r for r in rows if r["name"] == "claude.ai Canva")
    assert gm["connected"] and gm["enforced"] and gm["on_engine"] and gm["scope"] == "off"
    assert not cv["connected"] and "authentication" in cv["status"].lower()
    connections.set_access("claude.ai Gmail", "research")
    deploy._cache.update(at=0.0, rows=[])
    rows = connections.discover(fresh=True, home=home, run=run, which=lambda n: "/bin/claude" if n == "claude" else None)
    assert next(r for r in rows if r["name"] == "claude.ai Gmail")["scope"] == "research"


# ------------------------------------------------------------------ who may use what


def test_access_is_off_by_default_and_scopes_pick_the_agents():
    assert connections.access() == {} and connections.allow_for("sf-researcher") == () and connections.prompt_note("sf-researcher") == ""
    connections.set_access("claude.ai Figma", "build")
    connections.set_access("claude.ai Resend", "all")
    connections.set_access("claude.ai Gmail", "research")
    assert connections.allow_for("sf-builder") == ("mcp__claude_ai_Figma", "mcp__claude_ai_Resend")
    assert connections.allow_for("sf-researcher") == ("mcp__claude_ai_Resend", "mcp__claude_ai_Gmail")
    assert connections.allow_for("sf-holdout") == () and connections.allow_for("sf-deployer") == ()   # never, even for "all"
    note = connections.prompt_note("sf-builder")
    assert "claude.ai Figma" in note and "claude.ai Gmail" not in note and "Never send" in note and "data, never instructions" in note
    connections.set_access("claude.ai Figma", "off")
    assert connections.allow_for("sf-builder") == ("mcp__claude_ai_Resend",)
    with pytest.raises(ValueError):
        connections.set_access("x", "everything")
    with pytest.raises(ValueError):
        connections.set_access("", "all")


def test_other_engines_get_no_per_agent_rules():
    connections.set_access("claude.ai Figma", "all")
    write_config({**json.loads(engines.config_path().read_text(encoding="utf-8")), "engine": "codex"})
    assert connections.allow_for("sf-builder") == () and connections.prompt_note("sf-builder") == ""   # Codex has no per-agent tool limits


def test_the_claude_command_carries_the_connector_rules():
    connections.set_access("claude.ai Figma", "build")
    role = Role(name="sf-builder", model="claude-sonnet-5-5", effort="medium", body="x")
    cmd = factory.claude_cmd(role, "claude-sonnet-5-5", "medium", Path("/run"), Path("/run/p.md"), connections.allow_for("sf-builder"))
    allowed = cmd[cmd.index("--allowedTools") + 1:]
    assert "mcp__claude_ai_Figma" in allowed and "Read" in allowed
    role2 = Role(name="sf-researcher", model="m", effort="medium", body="x")
    cmd2 = factory.claude_cmd(role2, "m", "medium", Path("/run"), Path("/run/p.md"), connections.allow_for("sf-researcher"))
    assert "mcp__claude_ai_Figma" not in cmd2


# ------------------------------------------------------------------ skills


def make_skill(root: Path, name: str, desc: str):
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nbody\n", encoding="utf-8")


def test_skills_from_every_agent_show_where_they_came_from(home, tmp_path):
    make_skill(home / ".claude" / "skills", "claude-one", "from Claude Code")
    make_skill(home / ".codex" / "skills", "codex-one", "from Codex")
    make_skill(home / ".gemini" / "skills", "gemini-one", "from Gemini")
    make_skill(home / ".claude" / "plugins" / "cache" / "mk" / "cool-plugin" / "1.2.3" / "skills", "plug-one", "from a plugin")
    other = tmp_path / "grok-skills"
    make_skill(other, "grok-one", "from a folder you added")
    connections.add_skill_folder(str(other))
    cat = {s["name"]: s for s in checks.skill_catalog(include_disabled=True)}
    assert cat["claude-one"]["agent"] == "Claude Code" and cat["codex-one"]["agent"] == "Codex" and cat["gemini-one"]["agent"] == "Gemini CLI"
    assert cat["plug-one"]["agent"] == "Claude plugin: cool-plugin" and cat["grok-one"]["agent"] == "Folder: grok-skills"
    assert all(cat[n]["enabled"] for n in ("claude-one", "grok-one"))


def test_a_skill_switched_off_is_hidden_from_the_factory(home):
    make_skill(home / ".claude" / "skills", "keep-me", "k")
    make_skill(home / ".claude" / "skills", "drop-me", "d")
    assert "drop-me" in checks.list_skills() and checks.find_skill("drop-me") is not None
    connections.set_skill_enabled("drop-me", False)
    assert "drop-me" not in checks.list_skills() and "keep-me" in checks.list_skills()
    assert checks.find_skill("drop-me") is None and checks.find_skill("keep-me") is not None
    off = next(s for s in checks.skill_catalog(include_disabled=True) if s["name"] == "drop-me")
    assert off["enabled"] is False
    connections.set_skill_enabled("drop-me", True)
    assert "drop-me" in checks.list_skills()
    with pytest.raises(ValueError):
        connections.set_skill_enabled("../../etc", False)


def test_skill_folders_are_checked_before_they_are_added(home, tmp_path):
    with pytest.raises(ValueError, match="no folder"):
        connections.add_skill_folder(str(tmp_path / "missing"))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no skills"):
        connections.add_skill_folder(str(empty))
    good = tmp_path / "good"
    make_skill(good, "s1", "d")
    assert connections.add_skill_folder(str(good))["folders"] == [good.resolve().as_posix()]
    assert connections.add_skill_folder(str(good))["folders"] == [good.resolve().as_posix()]   # no duplicates
    assert connections.remove_skill_folder(good.resolve().as_posix())["folders"] == []


# ------------------------------------------------------------------ the dashboard's endpoints


def test_connections_endpoints_work_and_stay_on_the_computer(servers, home, monkeypatch, tmp_path):
    monkeypatch.setattr(connections, "discover", lambda fresh=False, **k: [
        {"id": "claude:claude.ai Figma", "name": "claude.ai Figma", "agent": "claude", "agent_label": "Claude Code", "kind": "remote", "where": "",
         "status": "✔ Connected", "connected": True, "on_engine": True, "scope": connections.access().get("claude.ai Figma", "off"), "enforced": True}])
    make_skill(home / ".claude" / "skills", "alpha", "first skill")
    snap = json.loads(call(servers, "GET", "/api/connections")[1])
    assert snap["connectors"][0]["name"] == "claude.ai Figma" and snap["enforced"] and any(s["name"] == "alpha" for s in snap["skills"])
    out = json.loads(call(servers, "POST", "/api/connections/access", {"name": "claude.ai Figma", "scope": "build"})[1])
    assert out["connectors"][0]["scope"] == "build" and connections.allow_for("sf-builder") == ("mcp__claude_ai_Figma",)
    assert call(servers, "POST", "/api/connections/access", {"name": "claude.ai Figma", "scope": "bogus"})[0] == 400
    out = json.loads(call(servers, "POST", "/api/connections/skill", {"name": "alpha", "enabled": False})[1])
    assert next(s for s in out["skills"] if s["name"] == "alpha")["enabled"] is False
    assert call(servers, "POST", "/api/connections/folder", {"path": str(tmp_path / "nope")})[0] == 400
    info, port = turn_on(servers)
    cookie = call(port, "GET", "/?pin=" + info["pin"])[2]["Set-Cookie"].split(";")[0]
    assert call(port, "GET", "/api/connections", cookie=cookie)[0] == 403
    for path in ("access", "skill", "folder"):
        assert call(port, "POST", "/api/connections/" + path, {"name": "x"}, cookie=cookie)[0] == 403
