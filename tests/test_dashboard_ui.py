"""Opens the real dashboard in Edge and clicks through the gates. Skipped when Edge or Playwright is missing."""

import base64
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import checks
import dashboard
import factory
import pipeline

sync_api = pytest.importorskip("playwright.sync_api")
EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
pytestmark = pytest.mark.skipif(not EDGE.exists(), reason="needs Edge")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(checks, "ALWAYS_DIR", tmp_path / "always")
    (tmp_path / "runs").mkdir()
    spawned = []
    monkeypatch.setattr(dashboard, "spawn_driver", lambda rid: spawned.append(rid))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    class Site:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        runs = tmp_path / "runs"
        always = tmp_path / "always"

        def make(self, mode="single", gate=None, status="gate", **over):
            rd = factory.create_run("a tiny tool", self.runs)
            s = json.loads((rd / "state.json").read_text(encoding="utf-8"))
            now = time.time()
            rail = pipeline.build_rail(s["inputs"], mode)
            s.update(status=status, title="Tiny Tool", heartbeat=now, created_at=now - 60, cost_usd=2.5, mode=mode, rail=rail, gate=gate)
            s["stages"] = {e["key"]: {"state": "done" if i < 3 else "pending", "label": e["label"], "bot": e["bot"]} for i, e in enumerate(rail)}
            s["cards"], s["events"] = [], [{"id": 1, "kind": "gate", "card": None, "msg": "x", "created_at": now}]
            s.update(over)
            (rd / "state.json").write_text(json.dumps(s), encoding="utf-8")
            return rd

        def decision(self, rd):
            p = rd / "decision.json"
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    yield Site()
    srv.shutdown()


@pytest.fixture
def page(site):
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        pg = browser.new_page(viewport={"width": 1500, "height": 1000})
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
        pg.on("response", lambda r: pg.errors.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        yield pg
        browser.close()


def gate(kind, accept, **kw):
    return {"id": f"gate-{kind}", "kind": kind, "title": f"T {kind}", "blurb": "Blurb text", "doc": kw.pop("doc", None), "accept": accept,
            "options": None, "deadline": None, "images": [], **kw}


def test_choose_an_idea_or_pick_random(site, page):
    ideas = [{"title": f"Idea {i}", "pitch": f"Pitch {i}", "size": "small", "has_ui": True} for i in range(5)]
    rd = site.make("single", gate("ideas", ["choose", "random"], options=ideas))
    page.goto(site.url)
    page.wait_for_selector(".opt")
    assert page.locator(".opt").count() == 5 and page.get_by_role("button", name="Random pick").count() == 1
    page.locator("[data-idx='2']").click()
    page.wait_for_timeout(600)
    d = site.decision(rd)
    assert d["decision"] == "choose" and d["index"] == 2 and d["gate"] == "gate-ideas"
    assert page.errors == []
    (rd / "decision.json").unlink()
    site.make("single", gate("ideas", ["choose", "random"], options=ideas), status="gate")
    page.reload()
    page.wait_for_selector(".opt")
    page.get_by_role("button", name="Random pick").click()
    page.wait_for_timeout(600)
    assert any((r / "decision.json").exists() and json.loads((r / "decision.json").read_text(encoding="utf-8"))["decision"] == "random"
               for r in site.runs.iterdir())


def test_decider_gate_shows_a_countdown_and_a_switch_button(site, page):
    decision = {"mode": "single", "size": "small", "has_ui": True, "reason": "small",
                "estimates": {"single": {"usd": [2, 4], "minutes": [5, 12]}, "multi": {"usd": [6, 10], "minutes": [25, 40]}}}
    rd = site.make("single", gate("mode", ["approve", "switch"], deadline=time.time() + 50, decision=decision, other="multi"), decision=decision)
    page.goto(site.url)
    page.wait_for_selector("#cd")
    text = page.locator("#cd").inner_text()
    assert "Continues on its own in" in text and "unless you switch" in text
    assert page.get_by_role("button", name="Switch to full pipeline").count() == 1 and page.get_by_role("button", name="Continue").count() == 1
    assert "$2–4" in page.locator("#ask").inner_text()
    page.get_by_role("button", name="Switch to full pipeline").click()
    page.wait_for_timeout(600)
    assert site.decision(rd)["decision"] == "switch" and page.errors == []


def test_review_gate_needs_a_note_before_improve_and_escalates_in_one_click(site, page):
    rd = site.make("single", gate("review", ["approve", "improve", "escalate"], doc="report.md"))
    (rd / "app").mkdir()
    (rd / "app" / "index.html").write_text("x", encoding="utf-8")
    (rd / "factory" / "app.json").write_text(json.dumps({"type": "static", "entry": "index.html", "screens": [{"name": "h", "path": "index.html"}]}), encoding="utf-8")
    page.goto(site.url)
    page.wait_for_selector("#ask:not([hidden])")
    box = page.locator("#ask")
    for name in ("Approve app", "Improve…", "Too big: use the full pipeline", "Open the app", "Read report.md"):
        assert box.get_by_role("button", name=name).count() == 1, name
    box.get_by_role("button", name="Improve…").click()
    assert page.locator("#g-rej").is_visible()
    page.locator("#g-send").click()
    assert "Write the note first" in page.locator("#g-err").inner_text() and site.decision(rd) is None
    page.locator("#g-fb").fill("make the buttons bigger")
    page.locator("#g-send").click()
    page.wait_for_timeout(600)
    d = site.decision(rd)
    assert d["decision"] == "improve" and d["feedback"] == "make the buttons bigger" and page.errors == []


def test_spec_gate_shows_the_mock_screenshots(site, page):
    rd = site.make("multi", gate("spec", ["approve", "reject"], doc="spec.md", images=["mock/shots/home-390.png", "mock/shots/home-1280.png"]))
    (rd / "factory" / "mock" / "shots").mkdir(parents=True)
    for n in ("home-390.png", "home-1280.png"):
        (rd / "factory" / "mock" / "shots" / n).write_bytes(PNG)
    page.goto(site.url)
    page.wait_for_selector(".thumb")
    assert page.locator(".thumb").count() == 2
    assert page.evaluate("[...document.querySelectorAll('.thumb')].every(i => i.complete && i.naturalWidth > 0)")
    page.locator(".thumb").first.click()
    assert page.locator("#drawer img").count() == 1 and page.errors == []


def test_the_rail_follows_the_mode_and_stacks_parallel_stages(site, page):
    site.make("multi", None, status="running")
    page.goto(site.url)
    page.wait_for_selector(".node")
    labels = page.locator(".node .lbl").all_inner_texts()
    for want in ("Intake", "Decider", "Research", "Spec", "Visual mock", "You check", "Design plan", "Hidden tests", "Technical plan", "Final plan",
                 "Build", "Auto checks", "Reviewers", "Report", "You review"):
        assert want in labels, want
    assert "Build app" not in labels
    assert page.locator(".col").count() < len(labels)  # design plan + hidden tests share a column
    page.get_by_role("button", name="New run").wait_for()
    assert page.errors == []


def test_new_run_form_validates_and_starts_a_run(site, page):
    page.goto(site.url)
    page.get_by_role("button", name="New run").click()
    page.wait_for_selector("#nr-go")
    page.locator("#nr-prod").fill("https://example.com/product")
    page.locator("#nr-go").click()
    assert "Give an idea or a whole spec" in page.locator("#nr-err").inner_text()
    page.locator("#nr-idea").fill("a habit tracker")
    page.locator("#nr-prod").fill("https://example.com/product\nhttps://another.example")
    page.locator("#nr-skills").fill("frontend-design, ui-ux-pro-max")
    page.locator("#nr-mode").select_option("single")
    page.locator("#nr-go").click()
    page.wait_for_timeout(1200)
    runs = [json.loads((r / "state.json").read_text(encoding="utf-8")) for r in site.runs.iterdir()]
    assert len(runs) == 1
    inp = runs[0]["inputs"]
    assert inp["idea"] == "a habit tracker" and inp["product_links"] == ["https://example.com/product", "https://another.example"] and inp["force_mode"] == "single"
    assert page.errors == []


def test_reverse_engineer_is_an_input_with_a_target_scope_and_links(site, page):
    page.goto(site.url)
    page.get_by_role("button", name="New run").click()
    assert not page.locator("#nr-revtarget").is_visible()
    page.locator("#nr-rev").check()
    assert page.locator("#nr-revtarget").is_visible() and "clean-room" in page.locator("#nr-revbox").inner_text()
    page.locator("#nr-go").click()
    assert "Say what to reverse engineer" in page.locator("#nr-err").inner_text()
    page.locator("#nr-revtarget").fill("Trello boards and cards")
    page.locator("#nr-revscope").select_option("app")
    page.locator("#nr-revlinks").fill("https://trello.com/guide\nnot a link")
    page.locator("#nr-go").click()
    page.wait_for_timeout(1000)
    runs = [json.loads((r / "state.json").read_text(encoding="utf-8")) for r in site.runs.iterdir()]
    assert len(runs) == 1 and runs[0]["inputs"]["reverse"] == {"target": "Trello boards and cards", "scope": "app", "links": ["https://trello.com/guide"]}
    assert page.errors == []


def test_generate_ideas_alone_is_enough_to_start(site, page):
    page.goto(site.url)
    page.get_by_role("button", name="New run").click()
    page.locator("#nr-gen").check()
    page.locator("#nr-go").click()
    page.wait_for_timeout(1000)
    assert [json.loads((r / "state.json").read_text(encoding="utf-8"))["inputs"]["generate"] for r in site.runs.iterdir()] == [True]


def test_always_use_documents_can_be_added_and_deleted(site, page):
    page.goto(site.url)
    page.get_by_role("button", name="Always-use docs").click()
    page.wait_for_selector(".adoc")
    page.locator(".adoc textarea").last.fill("Primary colour #FF5A36")
    page.locator(".adoc [data-save]").last.click()
    page.wait_for_timeout(800)
    assert (site.always / "brand-colours.md").read_text(encoding="utf-8") == "Primary colour #FF5A36"
    assert page.locator(".adoc").count() == 2
    page.locator(".adoc [data-del]").first.click()
    page.wait_for_timeout(800)
    assert not (site.always / "brand-colours.md").exists() and page.errors == []


def test_skills_library_accepts_dropped_skills_and_lists_them(site, page, tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "SKILLS_LIBRARY", tmp_path / "lib")
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: nice-skill\ndescription: Makes things nice\n---\nbody", encoding="utf-8")
    page.goto(site.url)
    page.get_by_role("button", name="Skills", exact=True).click()
    page.wait_for_selector("#sk-drop")
    assert "Nothing dropped in yet" in page.locator("#drawer").inner_text() and "uses only the ones that are useful" in page.locator("#drawer").inner_text()
    page.locator("#sk-files").set_input_files(str(skill))
    page.wait_for_selector(".skill")
    assert page.locator(".skill b").inner_text() == "nice-skill"
    assert (tmp_path / "lib").exists() and checks.list_library(tmp_path / "lib")[0]["description"] == "Makes things nice"
    page.locator(".skill [data-del]").click()
    page.wait_for_selector("text=Nothing dropped in yet")
    assert checks.list_library(tmp_path / "lib") == [] and page.errors == []


def test_the_report_shows_its_screenshots(site, page):
    rd = site.make("single", None, status="running")
    (rd / "factory" / "report" / "shots").mkdir(parents=True)
    (rd / "factory" / "report" / "shots" / "home-390.png").write_bytes(PNG)
    (rd / "factory" / "report.md").write_text("# Report\n![Home](report/shots/home-390.png)\nBuilt it.\n", encoding="utf-8")
    page.goto(site.url)
    page.get_by_role("button", name="Report", exact=False).first.click()
    page.wait_for_selector(".mdimg")
    assert page.evaluate("document.querySelector('.mdimg').naturalWidth > 0") and page.errors == []
