"""App context and first-open experience: the owner's own words reach the brief and the agents unchanged."""

import json

import checks
import factory
from test_factory import FakeAgents, browser, drive, make_run  # noqa: F401  (browser is a fixture)

CONTEXT = "For busy parents who track chores for 2 to 4 kids.\nUsed one-handed on a phone. Calm, no jargon."
FIRST_OPEN = "A welcome screen with the app name, then 3 questions (name, goal, reminder time), then the home screen. No sign-up."


def test_create_run_keeps_both_texts_and_writes_context_file(tmp_path):
    rd = factory.create_run({"idea": "chore app", "app_context": f"  {CONTEXT}  ", "first_open": FIRST_OPEN}, tmp_path)
    inputs = json.loads((rd / "state.json").read_text(encoding="utf-8"))["inputs"]
    assert inputs["app_context"] == CONTEXT and inputs["first_open"] == FIRST_OPEN
    text = (rd / "inputs" / "context.md").read_text(encoding="utf-8")
    assert "## App context (from the owner)\n" + CONTEXT in text and "## First-open experience" in text and FIRST_OPEN in text


def test_blank_and_oversized_texts(tmp_path):
    rd = factory.create_run({"idea": "x", "app_context": "   ", "first_open": "y" * 5000}, tmp_path)
    inputs = json.loads((rd / "state.json").read_text(encoding="utf-8"))["inputs"]
    assert inputs["app_context"] is None and len(inputs["first_open"]) == 2000
    assert not (rd / "inputs" / "context.md").exists() or "App context" not in (rd / "inputs" / "context.md").read_text(encoding="utf-8")
    assert checks.context_markdown(None, None) == ""


def test_texts_alone_do_not_count_as_a_request(tmp_path):
    try:
        factory.create_run({"app_context": CONTEXT, "first_open": FIRST_OPEN}, tmp_path)
    except ValueError:
        return
    raise AssertionError("an app with only context and no idea or spec must be refused")


def test_direct_brief_carries_the_owner_text_word_for_word(tmp_path, browser):
    agents = FakeAgents()
    run = make_run(tmp_path, agents, {"idea": "chore app", "app_context": CONTEXT, "first_open": FIRST_OPEN})
    assert drive(run) == []
    assert "sf-intake" not in agents.names()   # nothing to read, so no agent is spent
    brief = (run.fdir / "brief.md").read_text(encoding="utf-8")
    assert "## App context (from the owner)\n" + CONTEXT in brief
    assert "## First-open experience (the owner's requirement)" in brief and FIRST_OPEN in brief
    assert brief.index("First-open experience") < brief.index("## Constraints")


def test_intake_agent_is_told_to_copy_the_text_and_the_brief_is_checked(tmp_path, browser):
    class Intake(FakeAgents):
        def sf_intake(self, run, cid, prompt, cwd):
            self.seen = prompt
            (run.fdir / "brief.md").write_text("# Brief: x\n\n## Summary\nbuild it\n\n## First-open experience (the owner's requirement)\n" + FIRST_OPEN + "\n", encoding="utf-8")

    agents = Intake()
    run = make_run(tmp_path, agents, {"idea": "chore app", "product_links": ["https://example.com/a"], "app_context": CONTEXT, "first_open": FIRST_OPEN})
    assert drive(run) == []
    assert FIRST_OPEN in agents.seen and CONTEXT in agents.seen and "word for word" in agents.seen
