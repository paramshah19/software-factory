"""Tests for the factory driver and the pipeline. The agents are faked; git, pytest and the state machine are real.
The real browser is covered in test_checks.py, so here the browser calls are replaced by a controllable fake."""

import base64
import json
import re
import sys
import threading
import time
from pathlib import Path

import pytest

import checks
import factory
import pipeline
from factory import AgentResult, Run, StageFailed

MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


# --------------------------------------------------------------------------- roles and the claude command


def test_every_agent_file_parses_with_valid_model_and_effort():
    for name in factory.ROLES:
        r = factory.load_role(name)
        assert r.model in MODELS and r.effort in EFFORTS, name
        assert r.body
        if r.retry_model:
            assert r.retry_model in MODELS and r.retry_effort in EFFORTS
    assert (factory.AGENTS_DIR / "_protocol.md").exists()
    assert not (factory.AGENTS_DIR / "sf-qa.md").exists()  # replaced by the automatic checks and the reviewers


def test_model_and_effort_choices_follow_the_cost_logic():
    ld = factory.load_role
    O, S = "claude-opus-5-5", "claude-sonnet-5-5"
    # Balanced (default): Opus only where judgement matters most; planning and design moved to Sonnet with Opus as the retry
    for name in ("sf-spec", "sf-reviewer", "sf-maker", "sf-medic"):
        assert ld(name).model == O, name
    for name in ("sf-planner", "sf-designer", "sf-mock", "sf-reviewer-ui", "sf-intake", "sf-decider", "sf-security", "sf-integrator",
                 "sf-researcher", "sf-ideator", "sf-holdout", "sf-builder"):
        assert ld(name).model == S, name
    assert ld("sf-writer").model == "claude-haiku-5-5"
    for name in ("sf-planner", "sf-designer", "sf-mock"):
        assert ld(name).retry_model == O, name
    assert factory.pick_model(ld("sf-builder"), True) == (O, "high")
    assert factory.pick_model(ld("sf-builder"), False) == (S, "medium")
    # Max = the previous all-Opus planning; Lean = Sonnet for the expensive Opus stations
    assert factory.pick_model(ld("sf-planner"), False, "max") == (O, "high")
    assert factory.pick_model(ld("sf-planner"), False, "lean") == (S, "medium")
    assert factory.pick_model(ld("sf-maker"), False, "lean") == (S, "high")
    assert factory.pick_model(ld("sf-maker"), True, "lean") == (O, "high")  # a failing lean attempt still escalates
    assert factory.pick_model(ld("sf-writer"), False, "max") == ("claude-haiku-5-5", "medium")  # no preset = same as balanced
    for name in factory.ROLES:  # every retry is at least as strong as the role's Max preset
        r = ld(name)
        if r.retry_model and (r.presets or {}).get("max"):
            assert not (r.presets["max"][0] == O and r.retry_model == S), name


def test_plans_with_too_many_cards_are_sent_back():
    card = lambda i, deps: {"id": f"c{i}", "title": "t", "body": "b", "owned_files": [f"f{i}.py"], "depends_on": deps, "acs": ["AC-1"]}
    six = {"cards": [dict(card(0, []), scaffold=True)] + [card(i, ["c0"]) for i in range(1, 6)]}
    assert not [e for e in factory.validate_cards(six, ["AC-1"], False) if "too many" in e]
    seven = {"cards": six["cards"] + [card(6, ["c0"])]}
    assert any("7 cards is too many (at most 6)" in e for e in factory.validate_cards(seven, ["AC-1"], False))
    assert any("at most 4" in e for e in factory.validate_cards({"cards": [card(i, []) for i in range(1, 6)]}, ["AC-1"], True, ""))
    ten = {"cards": [dict(card(0, []), scaffold=True)] + [card(i, ["c0"]) for i in range(1, 10)]}
    assert any("at most 6" in e for e in factory.validate_cards(ten, ["AC-1"], False))
    assert not [e for e in factory.validate_cards(ten, ["AC-1"], False, multi_service=True) if "too many" in e]  # several services: up to 10


def test_parse_role_requires_frontmatter():
    with pytest.raises(ValueError):
        factory.parse_role("no frontmatter", "x")


def cmd_for(name, tmp_path):
    role = factory.load_role(name)
    return factory.claude_cmd(role, role.model, role.effort, tmp_path, tmp_path / "p.md")


def test_only_the_hidden_test_writer_can_touch_holdout(tmp_path):
    for name in factory.ROLES:
        cmd = cmd_for(name, tmp_path)
        assert ("Read(**/holdout/**)" in cmd) is (name != "sf-holdout" and name not in ("sf-medic",)), name
        assert "Bash(git push:*)" in cmd


def test_web_access_is_limited_to_the_agents_that_read_the_outside_world(tmp_path):
    for name in factory.ROLES:
        web = "WebSearch" in cmd_for(name, tmp_path)
        assert web is (name in ("sf-ideator", "sf-intake", "sf-researcher", "sf-reverse")), name


def test_claude_cmd_uses_the_requested_model_and_effort(tmp_path):
    role = factory.load_role("sf-builder")
    cmd = factory.claude_cmd(role, role.retry_model, role.retry_effort, tmp_path, tmp_path / "p.md")
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5" and cmd[cmd.index("--effort") + 1] == "high"
    assert "stream-json" in cmd
    for needed in ("Bash(cd:*)", "Bash(head:*)", "PowerShell(Select-Object:*)"):
        assert needed in cmd


def test_format_event_scan_denials_and_odd_events():
    ev = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Writing it"},
          {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "C:/x/run/app/a.py"}}]}}
    lines = factory.format_event(ev, "C:/x/run")
    assert lines[0].startswith("… Writing") and lines[1] == "▸ Write <run>/app/a.py"
    assert factory.format_event({"type": "result", "duration_ms": 4000, "total_cost_usd": 0.5})[0].startswith("■ finished in 4s")
    pending, denied = {}, []
    factory.scan_denials({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cd x && uv run pytest | tail"}}]}}, pending, denied)
    factory.scan_denials({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                          "content": "Permission to use Bash has been denied because Claude Code is running in don't ask mode."}]}}, pending, denied)
    assert denied == ["Bash cd x && uv run pytest | tail"] and "git -C" in factory.denial_hint(denied)
    for odd in ({"type": "system", "message": "str"}, {"type": "assistant", "message": "oops"}, {"type": "user", "message": {"content": "x"}},
                {"type": "assistant", "message": {"content": [None, 3, "x"]}}, {}):
        factory.format_event(odd, "C:/x")
        factory.scan_denials(odd, {}, [], "C:/x")


def test_agent_env_finds_git_bash_for_the_bash_tool():
    env = factory._agent_env(factory.RUNS_DIR)
    if factory.os.name == "nt" and factory.shutil.which("git"):
        assert Path(env["CLAUDE_CODE_GIT_BASH_PATH"]).exists()
    assert env["FACTORY_APP_DIR"].endswith("app")


# --------------------------------------------------------------------------- pure helpers


def card(i, deps=(), acs=(), files=None, **kw):
    return {"id": i, "title": i, "body": "b", "owned_files": files if files is not None else [f"{i}.py"],
            "depends_on": list(deps), "acs": list(acs), **kw}


def test_validate_cards_accepts_good_and_catches_mistakes():
    good = [card("scaffold", scaffold=True), card("a", ["scaffold"], ["AC-1"]), card("b", ["scaffold"], ["AC-2"])]
    assert factory.validate_cards({"cards": good}, ["AC-1", "AC-2"], False) == []
    v = lambda cards, acs=("AC-1",), rework=False, **kw: factory.validate_cards({"cards": cards}, list(acs), rework, **kw)
    assert "non-empty" in v([])[0]
    assert any("scaffold" in e for e in v([card("a", acs=["AC-1"])]))
    assert any("unknown card" in e for e in v([card("scaffold", scaffold=True), card("a", ["zzz"], ["AC-1"])]))
    assert any("cycle" in e for e in v([card("scaffold", ["a"], scaffold=True), card("a", ["scaffold"], ["AC-1"])]))
    assert any("not covered" in e for e in v(good[:2], acs=("AC-1", "AC-9")))
    assert any("both" in e for e in v([card("scaffold", scaffold=True), card("a", ["scaffold"], ["AC-1"], files=["x.py"]), card("b", ["scaffold"], [], files=["x.py"])]))
    assert v([card("r1-a")], rework=True, prefix="r1-", existing={"a"}) == []
    assert any("already used" in e for e in v([card("r1-a")], rework=True, prefix="r1-", existing={"r1-a"}))
    assert any("scaffold" in e for e in v([card("r1-a", scaffold=True)], rework=True, prefix="r1-"))


def test_spec_ac_text_finds_the_criterion_in_common_formats():
    spec = "\n".join(["## AC", "- **AC-3**: Paused timers keep their time.", "1. AC-12) Titles over 120 chars are rejected", "AC-1 alpha works", ""])
    assert factory.spec_ac_text(spec, "AC-3") == "Paused timers keep their time."
    assert factory.spec_ac_text(spec, "AC-12") == "Titles over 120 chars are rejected"
    assert factory.spec_ac_text(spec, "AC-1") == "alpha works" and factory.spec_ac_text(spec, "AC-99") == ""


def test_ready_cards_spec_acs_failing_acs_holdout_withs(tmp_path):
    cards = [card("s"), card("a", ["s"]), card("b", ["s"]), card("c", ["a", "b"])]
    assert [c["id"] for c in factory.ready_cards(cards, set(), set())] == ["s"]
    assert [c["id"] for c in factory.ready_cards(cards, {"s"}, {"a"})] == ["b"]
    assert factory.spec_acs("AC-10 AC-2 AC-1 AC-2") == ["AC-1", "AC-2", "AC-10"]
    assert factory.failing_acs("FAILED holdout/t.py::test_ac3_empty - assert 0\nFAILED x.py::test_other") == [("AC-3", "test_ac3_empty")]
    f = tmp_path / "t.py"
    f.write_text("# factory-with: httpx, pillow\n", encoding="utf-8")
    assert factory.holdout_withs(f) == ["httpx", "pillow"]


def test_the_rail_has_a_reverse_engineer_station_only_when_asked():
    keys = lambda inputs: [e["key"] for e in pipeline.build_rail(inputs, None)]
    assert "reverse" not in keys({}) and keys({"reverse": {"target": "x"}}) == ["reverse", "intake", "decide"]
    assert keys({"generate": True, "reverse": {"target": "x"}}) == ["ideas", "pick", "reverse", "intake", "decide"]


def test_build_rail_shows_only_the_stages_that_apply():
    keys = lambda inputs, mode: [e["key"] for e in pipeline.build_rail(inputs, mode)]
    assert keys({}, None) == ["intake", "decide"]
    assert keys({"generate": True}, None)[:2] == ["ideas", "pick"]
    single = keys({}, "single")
    assert single == ["intake", "decide", "skills", "maker", "autocheck", "security", "report", "review"]
    multi = keys({}, "multi")
    for k in ("research", "spec", "mock", "gate_spec", "skills_design", "skills_tech", "design", "holdout", "plan", "integrate", "build", "autocheck", "reviews", "report", "review"):
        assert k in multi
    cols = {e["key"]: e["col"] for e in pipeline.build_rail({}, "multi")}
    assert cols["skills_design"] == cols["skills_tech"] == cols["holdout"]            # the three lanes start together...
    assert cols["design"] == cols["plan"] and cols["skills_design"] < cols["design"] < cols["integrate"]  # ...and the two plans run side by side
    assert cols["research"] < cols["spec"] < cols["plan"] < cols["build"]


# --------------------------------------------------------------------------- creating runs (the inputs)


def test_a_run_needs_an_idea_a_spec_or_generate_ideas(tmp_path):
    for nothing in (None, "", {}, {"idea": "  "}, {"product_links": ["https://a.b"], "skills": ["x"]}):
        with pytest.raises(ValueError):
            factory.create_run(nothing, tmp_path)
    for ok in ("an idea", {"spec": "# spec"}, {"generate": True}, {"idea": "x", "spec": "y"}):
        assert (factory.create_run(ok, tmp_path) / "state.json").exists()


def test_reverse_engineering_is_a_required_input_and_is_saved(tmp_path):
    rd = factory.create_run({"reverse": {"target": "  Trello's boards  ", "scope": "app", "links": ["https://trello.com/guide", "javascript:x"]}}, tmp_path)
    inp = json.loads((rd / "state.json").read_text(encoding="utf-8"))["inputs"]
    assert inp["reverse"] == {"target": "Trello's boards", "scope": "app", "links": ["https://trello.com/guide"]} and "trello-s-boards" in rd.name
    odd = json.loads((factory.create_run({"idea": "x", "reverse": {"target": "a feature", "scope": "weird"}}, tmp_path) / "state.json").read_text(encoding="utf-8"))
    assert odd["inputs"]["reverse"]["scope"] == "feature"
    assert json.loads((factory.create_run({"idea": "x", "reverse": {"target": " "}}, tmp_path) / "state.json").read_text(encoding="utf-8"))["inputs"]["reverse"] is None
    with pytest.raises(ValueError):
        factory.create_run({"reverse": {"target": ""}}, tmp_path)


def test_run_creation_saves_every_input(tmp_path, monkeypatch):
    always = tmp_path / "always"
    always.mkdir()
    (always / "brand.md").write_text("Primary colour #FF5A36", encoding="utf-8")
    monkeypatch.setattr(checks, "ALWAYS_DIR", always)
    skills = tmp_path / "skills" / "my-skill"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: my-skill\n---\nDo X well", encoding="utf-8")
    monkeypatch.setattr(checks, "skill_roots", lambda: [tmp_path / "skills"])
    monkeypatch.setattr(checks.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    photo = tmp_path / "ref.png"
    photo.write_bytes(b"\x89PNG fake")
    rd = factory.create_run({"idea": "habit app", "spec": "# My spec\nAC-1 x", "product_links": ["https://a.example/x", "javascript:no"],
                             "ref_links": ["http://b.example"], "photos": [str(photo), {"name": "up load.jpg", "data": base64.b64encode(b"jpg").decode()},
                                                                          {"name": "evil.exe", "data": "AAAA"}],
                             "skills": ["my-skill", "ghost-skill", "../bad"], "force_mode": "single"}, tmp_path / "runs")
    st = json.loads((rd / "state.json").read_text(encoding="utf-8"))
    inp = st["inputs"]
    assert inp["product_links"] == ["https://a.example/x"] and inp["ref_links"] == ["http://b.example"]
    assert sorted(inp["photos"]) == ["ref.png", "up-load.jpg"] and inp["force_mode"] == "single"
    assert inp["skills"] == ["my-skill"] and st["missing_skills"] == ["ghost-skill"]
    assert (rd / "inputs" / "spec.md").read_text(encoding="utf-8").startswith("# My spec")
    assert (rd / "inputs" / "always-use" / "brand.md").exists() and not list((rd / "inputs" / "skills").glob("*"))  # skills are only hints: the skill agent decides
    assert (rd / "inputs" / "photos" / "ref.png").exists() and not list((rd / "inputs" / "photos").glob("*.exe"))


def test_always_use_documents_reach_every_agent_and_picked_skills_only_the_right_ones(tmp_path):
    rd = factory.create_run("x", tmp_path)
    (rd / "inputs" / "always-use").mkdir(parents=True, exist_ok=True)
    (rd / "inputs" / "always-use" / "brand.md").write_text("Primary colour #FF5A36", encoding="utf-8")
    for name, body in (("design-skill", "DESIGN BODY"), ("tech-skill", "TECH BODY"), ("sec-skill", "SECURITY BODY"), ("everything", "EVERYTHING BODY")):
        (rd / "inputs" / "skills" / name).mkdir(parents=True)
        (rd / "inputs" / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}", encoding="utf-8")
    run = Run(rd)
    run.state["skills"] = {"selected": [{"name": "design-skill", "for": ["design"], "why": "a screen app", "source": "yours", "side": "design"},
                                        {"name": "tech-skill", "for": ["technical"], "why": "tests", "source": "installed", "side": "technical"},
                                        {"name": "sec-skill", "for": ["security"], "why": "xss", "source": "installed", "side": "technical"},
                                        {"name": "everything", "for": ["design", "technical", "security"], "why": "all", "source": "yours", "side": "all"}], "rejected": []}
    for role in factory.ROLES:  # every agent gets the always-use documents
        assert "#FF5A36" in run.system_extras(role), role
    has = lambda role, body: body in run.system_extras(role)
    assert has("sf-designer", "DESIGN BODY") and has("sf-mock", "DESIGN BODY") and has("sf-reviewer-ui", "DESIGN BODY") and not has("sf-designer", "TECH BODY")
    assert has("sf-planner", "TECH BODY") and has("sf-reviewer", "TECH BODY") and not has("sf-planner", "DESIGN BODY")
    assert has("sf-security", "SECURITY BODY") and not has("sf-security", "DESIGN BODY") and not has("sf-planner", "SECURITY BODY")
    assert has("sf-maker", "DESIGN BODY") and has("sf-maker", "TECH BODY") and has("sf-builder", "DESIGN BODY") and has("sf-builder", "TECH BODY")
    assert all(has(r, "EVERYTHING BODY") for r in ("sf-designer", "sf-planner", "sf-security"))
    assert "Picked by the skill agent because: a screen app" in run.system_extras("sf-designer")
    assert not has("sf-writer", "DESIGN BODY") and not has("sf-decider", "TECH BODY")  # agents with nothing to apply them to get no skills


# --------------------------------------------------------------------------- fakes


class FakeBrowser:
    """Stands in for the real browser tools: writes screenshots, reports problems when told to."""

    def __init__(self):
        self.overflow_next = 0          # the next N auto-check captures report a sideways-scroll problem
        self.bad_while_single = None    # a Run: every auto-check capture fails while that run is in single-agent mode
        self.captures = 0

    def capture(self, app, app_json, out_dir, run=None, sizes=None):
        self.captures += 1
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        is_autocheck = "checks" in out_dir.parts and out_dir.name == "shots"
        bad = is_autocheck and (self.overflow_next > 0 or (self.bad_while_single is not None and self.bad_while_single.state.get("mode") == "single"))
        if is_autocheck and self.overflow_next > 0:
            self.overflow_next -= 1
        screens = []
        for s in app_json.get("screens", []):
            for w in (390, 1280):
                (out_dir / f"{s['name']}-{w}.png").write_bytes(b"\x89PNG")
                screens.append({"name": s["name"], "size": [w, 800], "png": f"{s['name']}-{w}.png", "overflow": bad and w == 390,
                                "console_errors": [], "failed_requests": []})
            (out_dir / f"{s['name']}-dark.png").write_bytes(b"\x89PNG")
        return {"ok": True, "screens": screens}


SKILL_FILES = {"frontend-design": ("installed", "Polished UI work for screens"), "my-skill": ("yours", "Does my thing"),
               "product-launch-video": ("installed", "Makes promo videos")}


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    root = tmp_path / "catalog"
    entries = []
    for name, (source, desc) in SKILL_FILES.items():
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nBody of {name}", encoding="utf-8")
        (root / name / "extra.md").write_text("more", encoding="utf-8")
        entries.append({"name": name, "description": desc, "source": source, "path": str(root / name / "SKILL.md")})
    monkeypatch.setattr(checks, "skill_catalog", lambda: list(entries))
    monkeypatch.setattr(checks, "find_skill", lambda name: Path(next((e["path"] for e in entries if e["name"] == name.split(":")[-1]), "")) if any(e["name"] == name.split(":")[-1] for e in entries) else None)
    return entries


@pytest.fixture
def browser(monkeypatch):
    fb = FakeBrowser()
    monkeypatch.setattr(checks, "capture_screens", fb.capture)
    monkeypatch.setattr(checks, "run_smoke_test", lambda app, rel, run=None: (0, "1 passed"))
    monkeypatch.setattr(checks, "dependency_audit", lambda app, run=None: {"status": "none", "detail": ""})
    return fb


class FakeAgents:
    """Plays every role by writing the files the real agent would write."""

    def __init__(self, **cfg):
        self.calls, self.prompts = [], {}
        self.lock = threading.Lock()
        self.decision = {"mode": "single", "size": "small", "has_ui": True, "reason": "two screens, data on the device"}
        self.security_blocks = 0        # how many security runs report a blocking finding
        self.tech_blocks = 0            # how many technical reviews report a blocking finding
        self.secret_in_maker = False
        self.medic = {"action": "retry", "route_to": None, "cause": "agent gave up", "guidance": "use simple commands"}
        self.silent = set()             # roles that write nothing until they get troubleshooter guidance
        self.skill_picks = {}           # side -> [(skill name, [for...])] the skill agent will pick
        self.__dict__.update(cfg)

    def __call__(self, run, cid, role, prompt, cwd, escalate):
        with self.lock:
            self.calls.append((role.name, cid, escalate))
            self.prompts.setdefault(cid, []).append(prompt)
        if role.name in self.silent and "TROUBLESHOOTER GUIDANCE" not in prompt:
            return AgentResult(ok=True, cost=0.01)
        getattr(self, role.name.replace("-", "_"))(run, cid, prompt, cwd)
        return AgentResult(ok=True, cost=0.01)

    def names(self):
        return [c[0] for c in self.calls]

    # ---- inputs, decision
    def sf_ideator(self, run, cid, prompt, cwd):
        ideas = [{"title": f"Idea {i}", "pitch": f"Pitch {i}", "why": "because", "size": "small", "has_ui": True} for i in range(5)]
        (run.fdir / "ideas.json").write_text(json.dumps({"ideas": ideas}), encoding="utf-8")

    def sf_intake(self, run, cid, prompt, cwd):
        (run.fdir / "brief.md").write_text("# Brief: x\n\n## Summary\nbuild it\n", encoding="utf-8")

    def sf_reverse(self, run, cid, prompt, cwd):
        self.prompts.setdefault("reverse-seen", []).append(prompt)
        if getattr(self, "reverse_no_sources", False) and "PREVIOUS ATTEMPT FAILED" not in prompt:
            (run.fdir / "reverse.md").write_text("# Reverse engineering: x\n## Feature breakdown\n- boards\n", encoding="utf-8")
            return
        (run.fdir / "reverse.md").write_text("# Reverse engineering: Boards\n\nA board with lists and cards.\n\n## Feature breakdown\n- lists (core)\n- cards (core)\n\n"
                                              "## Sources\nhttps://example.com/help/boards\n", encoding="utf-8")

    def sf_skills(self, run, cid, prompt, cwd):
        out = Path(re.search(r"the file (\S+\.json) ", prompt).group(1))
        side = re.search(r"SIDE: (\w+)", prompt).group(1)
        self.prompts.setdefault("skills-seen", []).append(prompt)
        picks = self.skill_picks.get(side, [])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"selected": [{"name": n, "for": f, "why": f"fits the {side} side of this project"} for n, f in picks],
                                   "rejected": [{"name": "product-launch-video", "why": "makes videos, this is an app"}]}), encoding="utf-8")

    def sf_decider(self, run, cid, prompt, cwd):
        (run.fdir / "decision.json").write_text(json.dumps(self.decision), encoding="utf-8")

    # ---- single agent
    def sf_maker(self, run, cid, prompt, cwd):
        run.app.mkdir(exist_ok=True)
        if not (run.app / "index.html").exists():
            (run.app / "index.html").write_text("<h1>app</h1>", encoding="utf-8")
        if self.secret_in_maker and "FEEDBACK" not in prompt and not (run.app / "secrets.js").exists():
            (run.app / "secrets.js").write_text('const key = "AKIAABCDEFGHIJKLMNOP";', encoding="utf-8")
        if "FEEDBACK" in prompt:
            (run.app / "secrets.js").unlink(missing_ok=True)
            (run.app / f"feedback-{cid}.txt").write_text(prompt[-300:], encoding="utf-8")
        (run.fdir / "app.json").write_text(json.dumps({"type": "static", "entry": "index.html",
                                                       "screens": [{"name": "home", "path": "index.html?demo=1"}]}), encoding="utf-8")

    def sf_security(self, run, cid, prompt, cwd):
        out = run.rd / re.search(r"to (factory/[\w/.-]+\.json)", prompt).group(1)
        out.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            block = self.security_blocks > 0
            if block:
                self.security_blocks -= 1
        finding = [{"severity": "high", "file": "index.html", "line": 3, "issue": "names go into innerHTML", "fix": "use textContent", "blocking": True}] if block else []
        out.write_text(json.dumps({"verdict": "fail" if block else "pass", "summary": "s", "findings": finding}), encoding="utf-8")

    def sf_writer(self, run, cid, prompt, cwd):
        shots = sorted((run.fdir / "report" / "shots").glob("*-390.png"))
        img = f"![Home](report/shots/{shots[0].name})\n" if shots else ""
        (run.fdir / "report.md").write_text(f"# Report\n{img}Built it.\n", encoding="utf-8")
        (run.app / "README.md").write_text("# readme\n", encoding="utf-8")

    # ---- full pipeline
    def sf_researcher(self, run, cid, prompt, cwd):
        (run.fdir / "research.md").write_text("## Sources\nhttps://docs.python.org/3/\n", encoding="utf-8")

    def sf_spec(self, run, cid, prompt, cwd):
        note = "\n## Checker notes\nadded AC-2\n" if "CHECK mode" in prompt else ("\n(revised)\n" if "OWNER FEEDBACK" in prompt else "")
        base = (run.fdir / "spec.md").read_text(encoding="utf-8") if "CHECK mode" in prompt else "# Spec\nAC-1 alpha works\n"
        (run.fdir / "spec.md").write_text(base.rstrip() + "\nAC-2 beta works\n" + note, encoding="utf-8")

    def sf_mock(self, run, cid, prompt, cwd):
        (run.fdir / "mock").mkdir(exist_ok=True)
        (run.fdir / "mock" / "index.html").write_text("<h1>mock</h1>" + ("<!-- revised -->" if "OWNER FEEDBACK" in prompt else ""), encoding="utf-8")
        (run.fdir / "mock" / "app.json").write_text(json.dumps({"type": "static", "entry": "index.html", "screens": [{"name": "home", "path": "index.html"}]}), encoding="utf-8")

    def sf_designer(self, run, cid, prompt, cwd):
        (run.fdir / "design").mkdir(exist_ok=True)
        (run.fdir / "design.md").write_text("# Design\n", encoding="utf-8")
        (run.fdir / "design" / "tokens.css").write_text(":root { --brand: #ff5a36; }\n", encoding="utf-8")

    def sf_planner(self, run, cid, prompt, cwd):
        (run.fdir / "plan.md").write_text("plan", encoding="utf-8")
        if "REWORK" in prompt:
            cards = [card("r1-delta", [], ["AC-1"])]
        else:
            cards = [card("scaffold", acs=[], scaffold=True, files=["tests/test_smoke.py", "index.html"]), card("alpha", ["scaffold"], ["AC-1"]),
                     card("beta", ["scaffold"], ["AC-2"])]
            (run.fdir / "app.json").write_text(json.dumps({"type": "static", "entry": "index.html", "test": PYTEST,
                                                           "screens": [{"name": "home", "path": "index.html"}]}), encoding="utf-8")
        (run.fdir / "cards.json").write_text(json.dumps({"cards": cards}), encoding="utf-8")

    def sf_integrator(self, run, cid, prompt, cwd):
        with (run.fdir / "plan.md").open("a", encoding="utf-8") as fh:
            fh.write("\n## Integration notes\nnothing to fix\n")

    def sf_holdout(self, run, cid, prompt, cwd):
        (run.rd / "holdout" / "scenarios.md").write_text("scenarios", encoding="utf-8")
        (run.rd / "holdout" / "test_acceptance.py").write_text(
            "import os\nfrom pathlib import Path\nAPP = Path(os.environ.get('FACTORY_APP_DIR', '.'))\n"
            "def test_ac1_alpha():\n    assert (APP / 'alpha.py').exists()\n", encoding="utf-8")

    def sf_builder(self, run, cid, prompt, cwd):
        if cid == "scaffold":
            (cwd / "tests").mkdir(exist_ok=True)
            (cwd / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
            (cwd / "index.html").write_text("<h1>app</h1>", encoding="utf-8")
        else:
            (cwd / f"{cid}.py").write_text("VALUE = 1\n", encoding="utf-8")
            (cwd / "tests").mkdir(exist_ok=True)
            (cwd / "tests" / f"test_{cid.replace('-', '_')}.py").write_text("def test_it():\n    assert True\n", encoding="utf-8")
        factory.git(cwd, "add", "-A")
        factory.git(cwd, "commit", "-m", f"build {cid}")
        d = run.fdir / "cards" / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / "handoff.json").write_text(json.dumps({"summary": f"built {cid}"}), encoding="utf-8")

    def sf_reviewer(self, run, cid, prompt, cwd):
        out = run.rd / re.search(r"to (factory/[\w/.-]+\.json)", prompt).group(1)
        out.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            block = self.tech_blocks > 0
            if block:
                self.tech_blocks -= 1
        findings = [{"severity": "medium", "where": "alpha.py", "issue": "AC-1 edge case missing", "fix": "handle empty input", "blocking": True}] if block else []
        out.write_text(json.dumps({"verdict": "changes" if block else "approve", "summary": "s", "findings": findings}), encoding="utf-8")

    def sf_reviewer_ui(self, run, cid, prompt, cwd):
        out = run.rd / re.search(r"to (factory/[\w/.-]+\.json)", prompt).group(1)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"verdict": "approve", "summary": "looks right", "findings": []}), encoding="utf-8")

    def sf_medic(self, run, cid, prompt, cwd):
        if self.medic is None:
            return
        (run.fdir / "cards" / cid).mkdir(parents=True, exist_ok=True)
        (run.fdir / "cards" / cid / "diagnosis.json").write_text(json.dumps(self.medic), encoding="utf-8")


def make_run(tmp_path, agents, inputs="a habit tracker", wait=0.0):
    rd = factory.create_run(inputs, tmp_path)
    run = Run(rd, agent=agents)
    run.test_cmd = PYTEST
    run.holdout_cmd = PYTEST
    run.poll = 0.05
    run.decider_wait = wait
    return run


def drive(run, answers=None, timeout=300):
    """Run the pipeline in a thread. `answers` maps a gate kind to a list of decisions, e.g. {"review": [("improve", "bluer"), ("approve",)]};
    a decision is (name, feedback_or_index). Gates with no scripted answer get the default (approve, or choose idea 0)."""
    answers = {k: (list(v) if v is not None else None) for k, v in (answers or {}).items()}  # None = leave that gate alone
    errs = []

    def target():
        try:
            run.execute()
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    t = threading.Thread(target=target, daemon=True)
    t.start()
    done = set()
    end = time.time() + timeout
    while t.is_alive() and time.time() < end:
        g = run.state.get("gate")
        if g and g["id"] not in done:
            kind = g["kind"]
            if kind in answers and answers[kind] is None:
                time.sleep(0.03)
                continue
            dec = answers[kind][0] if answers.get(kind) else (("choose", 0) if kind == "ideas" else ("approve", ""))
            name, arg = (dec + ("",))[:2]
            try:
                factory.write_decision(run.rd, name, feedback=arg if isinstance(arg, str) else "", index=arg if isinstance(arg, int) else None)
            except SystemExit:  # the gate is not on disk yet (or just cleared): keep the scripted answer and try again
                continue
            if answers.get(kind):
                answers[kind].pop(0)
            done.add(g["id"])
        time.sleep(0.03)
    t.join(5)
    assert not t.is_alive(), "pipeline hung"
    return errs


def ids(run):
    return [c["id"] for c in run.state["cards"]]


def tree(run):
    return set(factory.git(run.app, "ls-tree", "-r", "--name-only", "main")[1].split())


# --------------------------------------------------------------------------- generate ideas, intake, decider


def test_generate_ideas_then_choose_one(tmp_path, browser):
    agents = FakeAgents()
    run = make_run(tmp_path, agents, {"generate": True})
    assert drive(run, {"ideas": [("choose", 2)]}) == []
    assert run.state["chosen_idea"]["title"] == "Idea 2" and run.state["chosen_idea"]["how"] == "chosen"
    assert "Idea 2" in (run.fdir / "idea.md").read_text(encoding="utf-8") and run.state["status"] == "done"
    assert any(e["kind"] == "idea_chosen" for e in run.state["events"])


def test_generate_ideas_random_pick_comes_from_the_list(tmp_path, browser, monkeypatch):
    monkeypatch.setattr(pipeline.random, "randrange", lambda n: 3)
    run = make_run(tmp_path, FakeAgents(), {"generate": True, "idea": "something for runners"})
    assert drive(run, {"ideas": [("random", "")]}) == []
    assert run.state["chosen_idea"]["title"] == "Idea 3" and run.state["chosen_idea"]["how"] == "random"
    assert "something for runners" in (run.fdir / "idea.md").read_text(encoding="utf-8")  # the theme is kept


def test_ideas_gate_only_accepts_choose_or_random(tmp_path, browser):
    run = make_run(tmp_path, FakeAgents(), {"generate": True})
    t = threading.Thread(target=run.execute, daemon=True)
    t.start()
    deadline = time.time() + 30
    # wait until the gate is on DISK, which is what the CLI and the dashboard (write_decision) read: the driver sets it in memory a moment earlier
    while not (factory.load_state(run.rd).get("gate") or {}).get("kind") == "ideas" and time.time() < deadline:
        time.sleep(0.05)
    with pytest.raises(SystemExit):
        factory.write_decision(run.rd, "approve")
    assert len(run.state["gate"]["options"]) == 5
    factory.write_decision(run.rd, "random")
    for _ in range(200):   # the driver picks the answer up and moves on; the rest of the run needs more answers than this test gives, so don't wait for it to finish
        if (run.state.get("gate") or {}).get("kind") != "ideas":
            break
        time.sleep(0.05)
    assert (run.state.get("gate") or {}).get("kind") != "ideas"


def test_simple_idea_skips_the_intake_agent_but_extras_use_it(tmp_path, browser):
    agents = FakeAgents()
    run = make_run(tmp_path, agents, "a tiny tool")
    assert drive(run) == []
    assert "sf-intake" not in agents.names() and "## Summary" in (run.fdir / "brief.md").read_text(encoding="utf-8")
    agents2 = FakeAgents()
    run2 = make_run(tmp_path, agents2, {"idea": "a tiny tool", "product_links": ["https://example.com/app"]})
    assert drive(run2) == []
    assert "sf-intake" in agents2.names()
    assert "https://example.com/app" in agents2.prompts["intake"][0]


def test_reverse_engineering_runs_before_intake_and_feeds_the_brief(tmp_path, browser):
    agents = FakeAgents()
    run = make_run(tmp_path, agents, {"reverse": {"target": "Trello's boards", "scope": "feature", "links": ["https://trello.com/guide"]}})
    assert drive(run) == []
    names = agents.names()
    assert names.index("sf-reverse") < names.index("sf-decider") and "sf-intake" not in names  # nothing else to read: the brief is built directly
    prompt = agents.prompts["reverse"][0]
    assert "Trello's boards" in prompt and "https://trello.com/guide" in prompt and "a feature" in prompt
    brief = (run.fdir / "brief.md").read_text(encoding="utf-8")
    assert "Analysis of the target" in brief and "lists (core)" in brief and "clean-room" in brief.lower()
    assert "own version" in run.state["title"] and "clean-room" in (run.fdir / "idea.md").read_text(encoding="utf-8")
    assert run.state["stages"]["reverse"]["state"] == "done" and "reverse.md" in factory.DOCS


def test_reverse_engineering_a_whole_app_with_photos_goes_through_the_intake_agent(tmp_path, browser):
    photo = tmp_path / "shot.png"
    photo.write_bytes(b"png")
    agents = FakeAgents(decision=MULTI)
    run = make_run(tmp_path, agents, {"idea": "my own twist", "reverse": {"target": "a whole app like Trello", "scope": "app"}, "photos": [str(photo)]})
    assert drive(run) == []
    assert "a whole app or platform" in agents.prompts["reverse"][0] and "inputs/photos/" in agents.prompts["reverse"][0]
    assert "factory/reverse.md holds a reverse-engineering analysis" in agents.prompts["intake"][0]
    assert "Also recreate" in (run.fdir / "idea.md").read_text(encoding="utf-8")


def test_a_reverse_analysis_without_sources_is_sent_back(tmp_path, browser):
    agents = FakeAgents(reverse_no_sources=True)
    run = make_run(tmp_path, agents, {"reverse": {"target": "a feature"}})
    assert drive(run) == []
    assert run.card("reverse")["attempts"] == 2


def test_the_protocol_and_the_reverse_role_demand_clean_room_work():
    proto = (factory.AGENTS_DIR / "_protocol.md").read_text(encoding="utf-8")
    assert "CLEAN-ROOM" in proto and "own name" in proto
    role = factory.load_role("sf-reverse")
    assert role.web and role.model == "claude-opus-5-5" and "PUBLIC information only" in role.body and "Never log in" in role.body


def test_a_whole_spec_is_copied_and_the_spec_step_only_checks_it(tmp_path, browser):
    agents = FakeAgents(decision={"mode": "multi", "size": "medium", "has_ui": False, "reason": "big"})
    run = make_run(tmp_path, agents, {"spec": "# Owner spec\nAC-1 alpha works\n"})
    assert drive(run) == []
    assert "sf-intake" in agents.names()  # a spec is read by the intake agent
    assert "CHECK mode" in agents.prompts["spec"][0]
    text = (run.fdir / "spec.md").read_text(encoding="utf-8")
    assert text.startswith("# Owner spec") and "Checker notes" in text and run.state["spec_given"] is True


def test_decider_continues_by_itself_and_the_owner_can_switch(tmp_path, browser):
    run = make_run(tmp_path, FakeAgents(), wait=0.4)  # nobody answers: it carries on after the wait
    assert drive(run, {"mode": None}) == []
    assert run.state["mode"] == "single" and any("continued automatically" in (c["summary"] or "") for c in run.state["cards"] if c["id"] == "gate-mode")
    est = run.state["decision"]["estimates"]
    assert est["single"]["usd"][1] < est["multi"]["usd"][1]
    agents = FakeAgents()
    run2 = make_run(tmp_path, agents, wait=60)
    assert drive(run2, {"mode": [("switch", "")]}) == []
    assert run2.state["mode"] == "multi" and run2.state["decision"]["overridden"] is True


def test_forced_mode_skips_the_gate(tmp_path, browser):
    run = make_run(tmp_path, FakeAgents(), {"idea": "x", "force_mode": "multi"}, wait=60)
    assert drive(run) == []
    assert run.state["mode"] == "multi" and "gate-mode" not in ids(run)


def test_runs_from_an_older_version_are_refused_politely(tmp_path):
    rd = factory.create_run("x", tmp_path)
    st = json.loads((rd / "state.json").read_text(encoding="utf-8"))
    del st["inputs"]
    (rd / "state.json").write_text(json.dumps(st), encoding="utf-8")
    with pytest.raises(StageFailed, match="older version"):
        Run(rd, agent=FakeAgents()).execute()


# --------------------------------------------------------------------------- single-agent path


def test_single_agent_path_builds_checks_secures_reports_and_waits_for_you(tmp_path, browser):
    agents = FakeAgents()
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    s = run.state
    assert s["status"] == "done" and s["mode"] == "single"
    assert agents.names()[:4] == ["sf-decider", "sf-skills", "sf-maker", "sf-maker"]  # skill check, build, then the screenshot critique (first build: 2 rounds)
    assert agents.names().count("sf-maker") == 3 and "sf-security" in agents.names() and "sf-writer" in agents.names()
    for forbidden in ("sf-planner", "sf-reviewer", "sf-spec", "sf-builder"):
        assert forbidden not in agents.names()
    assert "index.html" in tree(run) and (run.fdir / "checks" / "autocheck.json").exists()
    report = (run.fdir / "report.md").read_text(encoding="utf-8")
    assert "report/shots/home-390.png" in report and "cost so far" in report and (run.fdir / "report" / "shots" / "home-390.png").exists()
    assert all(st["state"] == "done" for k, st in s["stages"].items() if k in ("intake", "decide", "maker", "autocheck", "security", "report", "review"))
    assert (run.fdir / "security.json").exists() and s["cost_usd"] > 0


def test_single_agent_failed_checks_are_fixed_by_the_maker(tmp_path, browser):
    browser.overflow_next = 1
    agents = FakeAgents()
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    assert "maker-fix-1" in ids(run) and run.state["status"] == "done" and run.state["mode"] == "single"
    assert "the content overflows" in agents.prompts["maker-fix-1"][0] or "overflows" in agents.prompts["maker-fix-1"][0]


def test_a_secret_found_by_the_scan_is_sent_back_to_the_maker(tmp_path, browser):
    agents = FakeAgents(secret_in_maker=True)
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    assert "secret in the code" in agents.prompts["maker-fix-1"][0]
    assert "secrets.js" not in tree(run) and run.state["status"] == "done"


def test_security_findings_block_and_get_fixed(tmp_path, browser):
    agents = FakeAgents(security_blocks=1)
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    fix = agents.prompts["maker-fix-1"][0]
    assert "security findings" in fix and "innerHTML" in fix and run.state["status"] == "done"
    assert run.state["open_issues"] == []


def test_security_still_blocking_after_two_rounds_asks_you_and_lists_it_in_the_report(tmp_path, browser):
    agents = FakeAgents(security_blocks=99)
    run = make_run(tmp_path, agents)
    assert drive(run, {"help": [("approve", "")]}) == []
    assert run.state["open_issues"] and "innerHTML" in (run.fdir / "report.md").read_text(encoding="utf-8")
    assert run.state["stages"]["security"]["detail"] == "shipped with warnings"
    run2 = make_run(tmp_path, FakeAgents(security_blocks=99))
    errs = drive(run2, {"help": [("reject", "")]})
    assert errs and isinstance(errs[0], StageFailed) and run2.state["status"] == "failed"


def test_improve_sends_your_notes_back_to_the_same_agent_and_you_review_again(tmp_path, browser):
    agents = FakeAgents()
    run = make_run(tmp_path, agents)
    assert drive(run, {"review": [("improve", "make the buttons bigger"), ("approve", "")]}) == []
    assert "maker-r1" in ids(run) and "make the buttons bigger" in agents.prompts["maker-r1"][0]
    assert "gate-review-1" in ids(run) and "gate-review-2" in ids(run) and run.state["round_no"] == 1
    assert agents.names().count("sf-writer") == 2  # a fresh report for the second review


def test_failing_checks_escalate_to_the_full_pipeline_and_the_draft_becomes_the_design_reference(tmp_path, browser):
    agents = FakeAgents()
    run = make_run(tmp_path, agents)
    browser.bad_while_single = run  # the single-agent draft can never pass its checks
    assert drive(run) == []
    assert run.state["escalated"] is True and run.state["mode"] == "multi" and run.state["status"] == "done"
    assert (run.rd / "reference" / "prototype" / "index.html").exists() and "Prototype" in (run.fdir / "brief.md").read_text(encoding="utf-8")
    assert "sf-mock" not in agents.names()  # prototype-first: the mock is the draft, no agent needed
    assert agents.names().count("sf-maker") >= 3 and "sf-planner" in agents.names()
    assert any(e["kind"] == "escalated" for e in run.state["events"])


def test_the_escalate_button_moves_a_finished_single_run_to_the_full_pipeline(tmp_path, browser):
    agents = FakeAgents()
    run = make_run(tmp_path, agents)
    assert drive(run, {"review": [("escalate", "")]}) == []
    assert run.state["mode"] == "multi" and run.state["escalated"] and "sf-planner" in agents.names()
    assert run.state["stages"]["maker"]["state"] == "pending"


# --------------------------------------------------------------------------- full pipeline


MULTI = {"mode": "multi", "size": "medium", "has_ui": True, "reason": "big"}


def test_full_pipeline_runs_every_station_in_the_order_of_the_flowchart(tmp_path, browser):
    agents = FakeAgents(decision=MULTI)
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    names = agents.names()
    first = lambda n: names.index(n)
    assert first("sf-decider") < first("sf-researcher") < first("sf-spec") < first("sf-mock") < first("sf-skills") < first("sf-designer") \
        < first("sf-integrator") < first("sf-builder") < first("sf-reviewer") < first("sf-writer")
    assert {"sf-reviewer", "sf-reviewer-ui", "sf-security", "sf-holdout"} <= set(names)
    assert names.count("sf-builder") == 3 and "sf-maker" not in names
    assert {"alpha.py", "beta.py", "index.html", "README.md"} <= tree(run)
    assert factory.git(run.app, "log", "--oneline", "main")[1].count("Merge card/") == 3
    assert run.state["status"] == "done" and all(c["state"] == "done" for c in run.state["cards"])
    for key in ("research", "spec", "mock", "gate_spec", "design", "plan", "integrate", "holdout", "build", "autocheck", "reviews", "report", "review"):
        assert run.state["stages"][key]["state"] == "done", key
    assert (run.fdir / "reviews" / "tech.json").exists() and (run.fdir / "reviews" / "ui.json").exists() and (run.fdir / "reviews" / "security.json").exists()
    assert "Integration notes" in (run.fdir / "plan.md").read_text(encoding="utf-8")


def test_you_check_the_spec_with_the_mock_before_anything_is_built(tmp_path, browser):
    agents = FakeAgents(decision=MULTI)
    run = make_run(tmp_path, agents)
    seen = {}

    def spy():
        while run.state.get("status") != "done":
            g = run.state.get("gate")
            if g and g["kind"] == "spec":
                seen.update(g)
            time.sleep(0.02)
    threading.Thread(target=spy, daemon=True).start()
    assert drive(run) == []
    assert seen["doc"] == "spec.md" and any("mock/shots/home-390.png" in i for i in seen["images"])
    names = agents.names()
    assert "sf-planner" not in names[: names.index("sf-mock") + 1]


def test_rejecting_the_spec_revises_the_spec_and_the_mock(tmp_path, browser):
    agents = FakeAgents(decision=MULTI)
    run = make_run(tmp_path, agents)
    assert drive(run, {"spec": [("reject", "add an export button"), ("approve", "")]}) == []
    assert "spec-r2" in ids(run) and "gate-spec-r2" in ids(run) and "export button" in agents.prompts["spec-r2"][0]
    assert any("export button" in p for pl in agents.prompts.values() for p in pl if "Visual mock" in p or "mock" in p.lower())
    assert "(revised)" in (run.fdir / "spec.md").read_text(encoding="utf-8")
    assert "<!-- revised -->" in (run.fdir / "mock" / "index.html").read_text(encoding="utf-8")


def test_an_app_without_screens_skips_the_mock_design_and_ui_review(tmp_path, browser):
    agents = FakeAgents(decision={**MULTI, "has_ui": False})
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    for gone in ("sf-mock", "sf-designer", "sf-integrator", "sf-reviewer-ui"):
        assert gone not in agents.names()
    assert {"sf-reviewer", "sf-security"} <= set(agents.names())
    assert run.state["stages"]["design"]["detail"] == "no screens"


def test_blocking_review_findings_become_fix_cards_and_get_rechecked(tmp_path, browser):
    agents = FakeAgents(decision=MULTI, tech_blocks=1)
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    imp = [i for i in ids(run) if i.startswith("imp1-")]
    assert imp and "AC-1 edge case missing" in agents.prompts[imp[0]][0]
    assert agents.names().count("sf-reviewer") == 2  # reviewed again after the fix
    assert run.state["open_issues"] == [] and any(e["kind"] == "improvements" for e in run.state["events"])


def test_reviews_stop_after_two_fix_rounds_and_list_what_is_left(tmp_path, browser):
    agents = FakeAgents(decision=MULTI, tech_blocks=99)
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    assert agents.names().count("sf-reviewer") == 3  # first review + 2 rounds, then it stops looping
    assert run.state["open_issues"] and "AC-1 edge case missing" in (run.fdir / "report.md").read_text(encoding="utf-8")
    assert any(e["kind"] == "open_issues" for e in run.state["events"])


def test_reviewers_still_run_once_when_the_auto_checks_never_pass(tmp_path, browser):
    browser.overflow_next = 99  # the auto checks can never pass
    agents = FakeAgents(decision=MULTI)
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    assert {"sf-reviewer", "sf-security", "sf-reviewer-ui"} <= set(agents.names())  # nothing goes unchecked
    assert agents.names().count("sf-reviewer") == 1
    assert run.state["open_issues"] and any("overflows" in f["issue"] for f in run.state["open_issues"])
    assert run.state["status"] == "done"


def test_screens_that_look_identical_are_reported(tmp_path, browser, monkeypatch):
    def capture(app, app_json, out_dir, run=None, sizes=None):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for s in ("home", "running"):  # the app ignores ?demo=running: both screens come out the same
            for w in (390, 1280):
                (out_dir / f"{s}-{w}.png").write_bytes(f"same-{w}".encode())
        return {"ok": True, "screens": []}
    monkeypatch.setattr(checks, "capture_screens", capture)
    agents = FakeAgents()
    run = make_run(tmp_path, agents)
    run.init_app()
    (run.fdir / "app.json").write_text(json.dumps({"type": "static", "entry": "index.html", "screens": [{"name": "home", "path": "index.html"}]}), encoding="utf-8")
    (run.app / "index.html").write_text("x", encoding="utf-8")
    res = run.autocheck()
    assert not res["ok"] and any("'home' and 'running' look identical at 390px" in p for p in res["problems"])


def test_failing_hidden_tests_are_fixed_before_the_reviewers_run(tmp_path, browser):
    class NeedsGamma(FakeAgents):
        def sf_holdout(self, run, cid, prompt, cwd):
            super().sf_holdout(run, cid, prompt, cwd)
            with (run.rd / "holdout" / "test_acceptance.py").open("a", encoding="utf-8") as fh:
                fh.write("def test_ac2_gamma():\n    assert (APP / 'gamma.py').exists()\n")

        def sf_builder(self, run, cid, prompt, cwd):
            super().sf_builder(run, cid, prompt, cwd)
            if cid.startswith("imp"):
                (cwd / "gamma.py").write_text("V = 3\n", encoding="utf-8")
                factory.git(cwd, "add", "-A")
                factory.git(cwd, "commit", "-m", "gamma")
    agents = NeedsGamma(decision=MULTI)
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    first_review = agents.names().index("sf-reviewer")
    assert any(i.startswith("imp1-") for i in ids(run)) and agents.names().index("sf-builder") < first_review
    fix_prompt = [p for c, ps in agents.prompts.items() if c.startswith("imp1") for p in ps][0]
    assert "hidden acceptance test for AC-2 fails" in fix_prompt and "The spec says" in fix_prompt and "beta works" in fix_prompt
    assert "test_ac2_gamma" in fix_prompt  # only the test's name, never its code
    assert "gamma.py" in tree(run)


def test_a_small_tweak_goes_straight_to_a_builder_with_a_quick_check(tmp_path, browser):
    agents = FakeAgents(decision=MULTI)
    run = make_run(tmp_path, agents)
    assert drive(run, {"review": [("tweak", "make the title bigger"), ("approve", "")]}) == []
    assert "tweak-1" in ids(run) and "make the title bigger" in agents.prompts["tweak-1"][0]
    assert agents.names().count("sf-planner") == 1  # not re-planned
    assert agents.names().count("sf-reviewer") == 1 and agents.names().count("sf-security") == 2  # quick check: security only
    assert agents.names().count("sf-writer") == 2 and run.state["rounds"] == 0


def test_a_big_change_goes_back_to_the_planner(tmp_path, browser):
    agents = FakeAgents(decision=MULTI)
    run = make_run(tmp_path, agents)
    assert drive(run, {"review": [("big", "add a whole stats screen"), ("approve", "")]}) == []
    assert run.state["rounds"] == 1 and "plan-r1" in ids(run) and "r1-delta" in ids(run)
    assert "add a whole stats screen" in agents.prompts["plan-r1"][0] and "r1-delta.py" in tree(run)
    assert agents.names().count("sf-designer") == 1 and agents.names().count("sf-mock") == 1  # design kept, only the plan redone


def test_resume_skips_finished_stages(tmp_path, browser):
    agents = FakeAgents(decision=MULTI)
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    before = len(agents.calls)
    run2 = Run(run.rd, agent=agents)
    run2.test_cmd, run2.holdout_cmd, run2.poll, run2.decider_wait = PYTEST, PYTEST, 0.05, 0
    run2.reset_stages(["review"])
    assert drive(run2) == []
    assert len(agents.calls) == before and run2.state["status"] == "done"


def test_resuming_a_run_made_before_the_skills_step_goes_straight_to_the_gate(tmp_path, browser):
    """A run an older version made has no skills step. Resuming it (to read the owner's saved answer) must not add that step:
    it runs an agent and spends money before the run ever reaches the gate it was waiting at."""
    agents = FakeAgents()
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    assert run.state["stages"]["skills"]["state"] == "done"
    state = json.loads((run.rd / "state.json").read_text(encoding="utf-8"))
    state["stages"].pop("skills")                                   # as the older version left it
    state["rail"] = [e for e in state["rail"] if e["key"] != "skills"]
    (run.rd / "state.json").write_text(json.dumps(state), encoding="utf-8")
    before = len(agents.calls)
    run2 = Run(run.rd, agent=agents)
    run2.test_cmd, run2.holdout_cmd, run2.poll, run2.decider_wait = PYTEST, PYTEST, 0.05, 0
    run2.reset_stages(["review"])
    assert drive(run2) == []
    assert len(agents.calls) == before, f"resume ran agents: {agents.names()[before:]}"
    assert run2.state["stages"]["skills"]["state"] == "done" and "added later" in run2.state["stages"]["skills"]["detail"]
    assert run2.state["status"] == "done"


def test_an_answer_saved_while_the_driver_was_dead_still_counts_after_a_restart(tmp_path, browser):
    """The owner clicked Approve, the click was saved, but the run's driver had died. Starting the driver again must pick up that
    same gate and that answer (it used to open a new gate and throw the answer away, so the owner had to click again)."""
    agents = FakeAgents()
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    state = json.loads((run.rd / "state.json").read_text(encoding="utf-8"))
    gate_card = next(c for c in state["cards"] if c["kind"] == "gate" and c["stage"] == "review")
    gate_card.update(state="owner", status="waiting for you")
    state["stages"]["review"]["state"] = "owner"
    state["status"], state["heartbeat"] = "gate", time.time() - 3600
    state["gate"] = {"id": gate_card["id"], "kind": "review", "title": "Review the app", "blurb": "", "doc": "report.md", "accept": ["approve", "improve", "escalate"],
                     "options": None, "deadline": None, "images": []}
    (run.rd / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (run.rd / "decision.json").write_text(json.dumps({"gate": gate_card["id"], "decision": "approve", "feedback": "", "note": ""}), encoding="utf-8")
    before = len(agents.calls)
    run2 = Run(run.rd, agent=agents)
    run2.test_cmd, run2.holdout_cmd, run2.poll, run2.decider_wait = PYTEST, PYTEST, 0.05, 0
    assert drive(run2, {"review": None}) == []           # nobody answers during this restart: the saved answer has to be enough
    assert run2.state["status"] == "done" and run2.state["gate"] is None
    assert len(agents.calls) == before, f"the restart ran agents: {agents.names()[before:]}"
    assert [c["id"] for c in run2.state["cards"] if c["kind"] == "gate" and c["stage"] == "review"] == [gate_card["id"]]   # no second review gate
    assert not (run.rd / "decision.json").exists()


# --------------------------------------------------------------------------- the troubleshooter


def test_a_stuck_station_is_diagnosed_and_retried_with_guidance(tmp_path, browser):
    agents = FakeAgents(silent={"sf-security"})
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    sec = [i for i in ids(run) if i.startswith("security-")][0]
    assert f"{sec}-heal1" in ids(run) and run.card(sec)["attempts"] == 3 and run.state["status"] == "done"
    assert any(e["kind"] == "healing" for e in run.state["events"])


def test_troubleshooter_can_ask_you_and_you_can_stop_it(tmp_path, browser):
    agents = FakeAgents(silent={"sf-security"}, medic={"action": "owner", "cause": "unclear", "guidance": "decide"})
    run = make_run(tmp_path, agents)
    assert drive(run, {"help": [("approve", "")]}) == []
    assert any(i.startswith("gate-help-") for i in ids(run))
    run2 = make_run(tmp_path, FakeAgents(silent={"sf-security"}, medic={"action": "owner", "cause": "x", "guidance": "y"}))
    errs = drive(run2, {"help": [("reject", "")]})
    assert errs and isinstance(errs[0], StageFailed)


def test_a_stage_that_writes_nothing_fails_instead_of_passing(tmp_path, browser):
    agents = FakeAgents(silent={"sf-decider"}, medic=None)
    run = make_run(tmp_path, agents)
    errs = drive(run)
    assert errs and isinstance(errs[0], StageFailed) and run.state["status"] == "failed" and run.card("decide")["state"] == "blocked"


def test_odd_agent_output_is_caught(tmp_path, browser):
    class BadDecision(FakeAgents):
        def sf_decider(self, run, cid, prompt, cwd):
            (run.fdir / "decision.json").write_text(json.dumps({"mode": "maybe"}), encoding="utf-8")
    agents = BadDecision(medic=None)
    run = make_run(tmp_path, agents)
    errs = drive(run)
    assert errs and "needs mode" in run.state["error"]


# --------------------------------------------------------------------------- the skill agent


def test_the_single_agent_gets_only_the_skills_the_skill_agent_judged_useful(tmp_path, browser, catalog):
    agents = FakeAgents(skill_picks={"all": [("frontend-design", ["design"]), ("my-skill", ["technical", "security"])]})
    run = make_run(tmp_path, agents, {"idea": "habit app", "skills": ["my-skill"]})
    assert drive(run) == []
    names = agents.names()
    assert names.index("sf-decider") < names.index("sf-skills") < names.index("sf-maker") and names.count("sf-skills") == 1
    prompt = agents.prompts["skills-all"][0]
    assert "SIDE: all" in prompt and "The owner asked for: my-skill" in prompt and "skill-choice-all.json" in prompt
    cat_md = (run.fdir / "skills-catalog.md").read_text(encoding="utf-8")
    assert "frontend-design" in cat_md and "(yours): Does my thing" in cat_md and "[the owner asked for this one]" in cat_md
    assert (run.rd / "inputs" / "skill-candidates" / "my-skill.md").exists()
    assert (run.rd / "inputs" / "skills" / "frontend-design" / "extra.md").exists()  # the whole skill folder is copied for the agents to read
    assert not (run.rd / "inputs" / "skills" / "product-launch-video").exists()
    doc = (run.fdir / "skills.md").read_text(encoding="utf-8")
    assert "**frontend-design**" in doc and "fits the all side" in doc and "product-launch-video: makes videos, this is an app" in doc
    # the maker (design + technical) got both skills; the security checker only the one labelled security
    assert "Body of frontend-design" in run.system_extras("sf-maker") and "Body of my-skill" in run.system_extras("sf-maker")
    assert "Body of my-skill" in run.system_extras("sf-security") and "Body of frontend-design" not in run.system_extras("sf-security")
    assert any(e["kind"] == "skill_used" for e in run.state["events"]) and run.state["stages"]["skills"]["state"] == "done"


def test_skills_that_dont_fit_are_not_used(tmp_path, browser, catalog):
    agents = FakeAgents()  # the skill agent picks nothing
    run = make_run(tmp_path, agents, {"idea": "habit app", "skills": ["my-skill"]})
    assert drive(run) == []
    assert run.state["skills"]["selected"] == [] and not list((run.rd / "inputs" / "skills").glob("*"))
    assert "none: nothing available fit this project" in (run.fdir / "skills.md").read_text(encoding="utf-8")
    assert "Body of my-skill" not in run.system_extras("sf-maker")  # asked for by the owner, judged not useful, so not used
    assert run.state["stages"]["skills"]["detail"] == "none needed"


def test_an_environment_with_no_skills_skips_the_skill_agent(tmp_path, browser, monkeypatch):
    monkeypatch.setattr(checks, "skill_catalog", lambda: [])
    agents = FakeAgents()
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    assert "sf-skills" not in agents.names() and run.state["stages"]["skills"]["detail"] == "no skills available"


def test_a_skill_choice_with_unknown_names_or_too_many_picks_is_sent_back(tmp_path, browser, catalog):
    class Sloppy(FakeAgents):
        def sf_skills(self, run, cid, prompt, cwd):
            out = Path(re.search(r"the file (\S+\.json) ", prompt).group(1))
            if "PREVIOUS ATTEMPT FAILED" in prompt:
                out.write_text(json.dumps({"selected": [{"name": "my-skill", "for": ["technical"], "why": "ok"}], "rejected": []}), encoding="utf-8")
            else:
                out.write_text(json.dumps({"selected": [{"name": "made-up-skill", "for": ["design"], "why": "sounds good"}], "rejected": []}), encoding="utf-8")
    run = make_run(tmp_path, Sloppy())
    assert drive(run) == []
    assert run.card("skills-all")["attempts"] == 2 and [s["name"] for s in run.state["skills"]["selected"]] == ["my-skill"]


def test_design_and_technical_lanes_run_in_parallel_each_with_its_own_skill_check(tmp_path, browser, catalog):
    started = {"skills-design": threading.Event(), "skills-technical": threading.Event(), "designer": threading.Event(), "planner": threading.Event()}
    overlap = {}

    class Lanes(FakeAgents):
        def meet(self, mine, other, label):
            started[mine].set()
            overlap[label] = started[other].wait(timeout=20)  # sequential lanes would never see the other lane start

        def sf_skills(self, run, cid, prompt, cwd):
            super().sf_skills(run, cid, prompt, cwd)
            side = re.search(r"SIDE: (\w+)", prompt).group(1)
            if side in ("design", "technical"):
                self.meet(f"skills-{side}", "skills-technical" if side == "design" else "skills-design", f"skills-{side}")

        def sf_designer(self, run, cid, prompt, cwd):
            super().sf_designer(run, cid, prompt, cwd)
            self.meet("designer", "planner", "designer")

        def sf_planner(self, run, cid, prompt, cwd):
            super().sf_planner(run, cid, prompt, cwd)
            if "REWORK" not in prompt:
                self.meet("planner", "designer", "planner")
    agents = Lanes(decision=MULTI, skill_picks={"design": [("frontend-design", ["design"])], "technical": [("my-skill", ["technical", "security"])]})
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    assert overlap == {"skills-design": True, "skills-technical": True, "designer": True, "planner": True}
    names = agents.names()
    assert names.count("sf-skills") == 2 and names.index("sf-integrator") > max(names.index("sf-designer"), names.index("sf-planner"))
    assert "SIDE: design" in agents.prompts["skills-design"][0] and "SIDE: technical" in agents.prompts["skills-technical"][0]
    assert {(s["name"], s["side"]) for s in run.state["skills"]["selected"]} == {("frontend-design", "design"), ("my-skill", "technical")}
    assert run.state["stages"]["skills_design"]["state"] == "done" and run.state["stages"]["skills_tech"]["state"] == "done"
    # the technical planner no longer waits for the design plan
    assert "factory/design.md" not in agents.prompts["plan"][0] and "in parallel" in agents.prompts["plan"][0]
    # sides are enforced: the design lane can only pick design skills, the technical lane technical/security ones
    assert [s["for"] for s in run.state["skills"]["selected"] if s["side"] == "design"] == [["design"]]
    assert [s["for"] for s in run.state["skills"]["selected"] if s["side"] == "technical"] == [["technical", "security"]]


def test_an_app_without_screens_has_no_design_skill_check(tmp_path, browser, catalog):
    agents = FakeAgents(decision={**MULTI, "has_ui": False}, skill_picks={"technical": [("my-skill", ["technical"])]})
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    assert agents.names().count("sf-skills") == 1 and run.state["stages"]["skills_design"]["detail"] == "no screens"


def test_a_failure_in_one_lane_stops_the_run_instead_of_hanging(tmp_path, browser, catalog):
    class DesignerBroken(FakeAgents):
        def sf_designer(self, run, cid, prompt, cwd):
            pass  # writes nothing
    agents = DesignerBroken(decision=MULTI, medic=None)
    run = make_run(tmp_path, agents)
    errs = drive(run)
    assert errs and isinstance(errs[0], StageFailed) and run.state["status"] == "failed"


def test_escalating_drops_the_single_agents_skills_and_picks_again_per_side(tmp_path, browser, catalog):
    agents = FakeAgents(skill_picks={"all": [("frontend-design", ["design"])], "design": [("frontend-design", ["design"])], "technical": [("my-skill", ["technical"])]})
    run = make_run(tmp_path, agents)
    assert drive(run, {"review": [("escalate", "")]}) == []
    assert {s["side"] for s in run.state["skills"]["selected"]} == {"design", "technical"}  # nothing left over from the single agent's pick
    assert agents.names().count("sf-skills") == 3  # one for the single agent, then one per side


def test_skills_doc_is_in_the_documents_list():
    assert "skills.md" in factory.DOCS and "sf-skills" in factory.ROLES
    role = factory.load_role("sf-skills")
    assert not role.web and role.model == "claude-sonnet-5-5" and "USEFUL FOR THIS PROJECT" in role.body and "Choosing nothing is a perfectly good answer" in role.body


# --------------------------------------------------------------------------- usage limits, budget, history


def test_limit_reset_time_is_read_from_the_message():
    now = time.mktime((2026, 10, 23, 17, 0, 0, 0, 0, -1))
    t = factory.parse_limit_reset("You've hit your session limit · resets 6:10pm (Europe/London)", now)
    assert t - now == 70 * 60                                    # later today
    assert factory.parse_limit_reset("limit reached, resets 9am", now) - now == 16 * 3600  # already past today: tomorrow
    assert factory.parse_limit_reset("resets 6pm", now) - now == 3600
    assert factory.parse_limit_reset("rate limited, try later", now) is None
    assert factory.is_limit_error("You've hit your session limit") and not factory.is_limit_error("tests failed")


class LimitOnce:
    """Wraps the fake agents: the first call of one role answers like an account that ran out of usage."""

    def __init__(self, inner, role, times=1):
        self.inner, self.role, self.left = inner, role, times

    def __call__(self, run, cid, role, prompt, cwd, escalate):
        if role.name == self.role and self.left > 0:
            self.left -= 1
            return AgentResult(ok=False, error="You've hit your session limit · resets 6:10pm (Europe/London)")
        return self.inner(run, cid, role, prompt, cwd, escalate)


def test_a_usage_limit_pauses_the_run_instead_of_burning_retries(tmp_path, browser, monkeypatch):
    monkeypatch.setattr(factory, "parse_limit_reset", lambda text, now=None: time.time() - 89.5)  # reset + 90 s buffer = half a second away
    inner = FakeAgents(decision=MULTI)
    run = make_run(tmp_path, LimitOnce(inner, "sf-spec", times=2))
    assert drive(run) == []
    kinds = [e["kind"] for e in run.state["events"]]
    assert kinds.count("limit") == 2 and kinds.count("limit_over") == 2
    assert "failed" not in kinds and "healing" not in kinds       # no retries, no troubleshooter, no escalation
    assert inner.names().count("sf-spec") == 1 and inner.calls[[c[0] for c in inner.calls].index("sf-spec")][2] is False
    assert run.state["waiting"] is None and run.state["status"] == "done"


def test_a_run_pauses_at_its_budget_and_one_approval_gives_it_more(tmp_path, browser):
    agents = FakeAgents()
    run = make_run(tmp_path, agents, {"idea": "a habit tracker", "budget_usd": 0.015})
    assert drive(run, {"budget": [("approve", "")]}) == []
    assert any(e["kind"] == "gate" and "Budget reached" in e["msg"] for e in run.state["events"])
    assert run.state["budget_usd"] > 0.015 and run.state["status"] == "done"  # each approval raised the cap by half of the original


def test_rejecting_the_budget_gate_stops_the_run_and_keeps_the_work(tmp_path, browser):
    run = make_run(tmp_path, FakeAgents(), {"idea": "a habit tracker", "budget_usd": 0.015})
    errs = drive(run, {"budget": [("reject", "")]})
    assert errs and isinstance(errs[0], StageFailed) and "budget" in str(errs[0])
    assert run.state["status"] == "failed" and (run.app / ".git").exists()


def test_no_cap_means_the_budget_gate_never_appears(tmp_path, browser):
    run = make_run(tmp_path, FakeAgents(), {"idea": "a habit tracker", "budget_usd": 0})
    assert drive(run) == []
    assert not any(e["kind"] == "gate" and "Budget" in e["msg"] for e in run.state["events"])


def test_default_budgets_differ_by_path(tmp_path):
    run = make_run(tmp_path, FakeAgents())
    run.state["mode"] = "single"
    assert run.budget() == 10.0
    run.state["mode"] = "multi"
    assert run.budget() == 45.0


def test_finished_runs_are_recorded_for_the_stats_command(tmp_path, browser, monkeypatch, capsys):
    run = make_run(tmp_path, FakeAgents(decision=MULTI, tech_blocks=99))
    assert drive(run) == []
    rows = factory.read_metrics(tmp_path / "metrics.jsonl")
    assert len(rows) == 1 and rows[0]["run_id"] == run.rd.name and rows[0]["outcome"] == "done"
    assert rows[0]["mode"] == "multi" and rows[0]["fix_rounds"] == 2 and rows[0]["open_issues"] >= 1
    monkeypatch.setattr(factory, "RUNS_DIR", tmp_path)
    factory.cmd_stats()
    out = capsys.readouterr().out
    assert run.rd.name[:30] in out and "multi: 1 finished" in out


def test_a_fix_round_that_changed_nothing_is_told_so_and_uses_the_stronger_model(tmp_path, browser):
    agents = FakeAgents(decision=MULTI, tech_blocks=99)
    run = make_run(tmp_path, agents)
    assert drive(run) == []
    first = next(i for i in ids(run) if i.startswith("imp1-"))
    second = next(i for i in ids(run) if i.startswith("imp2-"))
    assert "DID NOT RESOLVE" not in agents.prompts[first][0] and "DID NOT RESOLVE" in agents.prompts[second][0]
    esc = {cid: e for (r, cid, e) in agents.calls if r == "sf-builder"}
    assert esc[first] is False and esc[second] is True


def test_a_forced_path_still_shows_what_the_decider_recommended(tmp_path, browser):
    agents = FakeAgents(decision={"mode": "single", "size": "small", "has_ui": True, "reason": "one small page"})
    run = make_run(tmp_path, agents, {"idea": "a timer", "force_mode": "multi"})
    assert drive(run) == []
    d = run.state["decision"]
    assert d["mode"] == "multi" and d["recommended"] == "single"
    assert "You chose the full pipeline" in d["reason"] and "would have recommended the single agent: one small page" in d["reason"]
