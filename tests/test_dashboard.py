"""Tests for the dashboard state builder and its actions, using hand-built run folders."""

import json
import time

import checks
import dashboard
import factory
import pipeline


def make_run(tmp_path, mode="single", **over):
    rd = factory.create_run("a tiny tool", tmp_path)
    s = json.loads((rd / "state.json").read_text(encoding="utf-8"))
    now = time.time()
    rail = pipeline.build_rail(s["inputs"], mode)
    s.update(status="running", title="Tiny Tool", heartbeat=now, created_at=now - 100, cost_usd=1.25, mode=mode, rail=rail)
    s["stages"] = {e["key"]: {"state": "pending", "label": e["label"], "bot": e["bot"]} for e in rail}
    for k in ("intake", "decide"):
        s["stages"][k]["state"] = "done"
    if "maker" in s["stages"]:
        s["stages"]["maker"]["state"] = "active"

    def c(i, title, role, kind, stage, state, **kw):
        return {"id": i, "title": title, "role": role, "kind": kind, "stage": stage, "state": state, "status": "", "attempts": 1,
                "reviews": 0, "summary": None, "error": None, "model": "claude-opus-5-5", "effort": "medium", "cost_usd": 0.1,
                "started_at": now - 50, "completed_at": None, "depends_on": [], "round": 0, **kw}
    s["cards"] = [c("intake", "Intake", None, "stage", "intake", "done"), c("decide", "Decide", "sf-decider", "stage", "decide", "done"),
                  c("maker", "Build the app", "sf-maker", "stage", "maker", "active", status="running"),
                  c("autocheck-1", "Auto checks", None, "check", "autocheck", "done")]
    s["events"] = [{"id": 1, "kind": "started", "card": "maker", "msg": "", "created_at": now}]
    s.update(over)
    (rd / "state.json").write_text(json.dumps(s), encoding="utf-8")
    return rd


def test_build_state_follows_the_run_rail_and_shapes_cards_and_workers(tmp_path):
    rd = make_run(tmp_path)
    (rd / "logs" / "maker.log").write_text("──── sf-maker\n▸ Write <run>/app/index.html\n", encoding="utf-8")
    (rd / "factory" / "brief.md").write_text("# brief", encoding="utf-8")
    st = dashboard.build_state(None, tmp_path)
    assert [s["key"] for s in st["stages"]] == ["intake", "decide", "skills", "maker", "autocheck", "security", "report", "review"]
    by = {s["key"]: s for s in st["stages"]}
    assert by["decide"]["state"] == "done" and by["maker"]["state"] == "active" and by["security"]["state"] == "waiting"
    assert by["maker"]["col"] < by["review"]["col"] and by["maker"]["task_ids"] == ["maker"]
    assert [a["id"] for a in st["active"]] == ["maker"] and "▸ Write" in st["active"][0]["log"][1]
    assert st["docs"] == ["brief.md"] and st["run"]["mode"] == "single" and st["stats"]["cost_usd"] == 1.25 and st["driver"] is True
    assert "autocheck-1" not in by["autocheck"]["task_ids"]  # check cards are listed, but the stage opens its latest agent card
    assert st["tasks"][2]["model"] == "claude-opus-5-5"


def test_driver_flag_and_failed_runs(tmp_path):
    make_run(tmp_path, heartbeat=time.time() - 120)
    assert dashboard.build_state(None, tmp_path)["driver"] is False
    (tmp_path / "y").mkdir()
    make_run(tmp_path / "y", status="failed", error="boom")
    st = dashboard.build_state(None, tmp_path / "y")
    assert st["driver"] is False and st["run"]["error"] == "boom"
    (tmp_path / "z").mkdir()
    make_run(tmp_path / "z", status="done", ended_at=time.time())
    st = dashboard.build_state(None, tmp_path / "z")
    assert st["driver"] is None and st["stats"]["ended_at"] is not None


def test_the_gate_is_passed_through_with_options_images_and_deadline(tmp_path):
    gate = {"id": "gate-ideas", "kind": "ideas", "title": "Choose an idea", "blurb": "b", "doc": None, "accept": ["choose", "random"],
            "options": [{"title": "A", "pitch": "p"}], "deadline": None, "images": []}
    make_run(tmp_path, status="gate", gate=gate)
    g = dashboard.build_state(None, tmp_path)["gate"]
    assert g["accept"] == ["choose", "random"] and g["options"][0]["title"] == "A"


def test_the_rail_switches_with_the_mode(tmp_path):
    make_run(tmp_path, mode="multi")
    keys = [s["key"] for s in dashboard.build_state(None, tmp_path)["stages"]]
    assert "research" in keys and "maker" not in keys and keys.index("spec") < keys.index("plan") < keys.index("build")


def test_old_runs_without_a_rail_still_load(tmp_path):
    rd = factory.create_run("x", tmp_path)
    st = json.loads((rd / "state.json").read_text(encoding="utf-8"))
    del st["rail"]
    (rd / "state.json").write_text(json.dumps(st), encoding="utf-8")
    assert dashboard.build_state(None, tmp_path)["stages"] == []
    assert dashboard.build_state(None, tmp_path / "nothing")["runs"] == []


def test_files_are_only_served_from_allowed_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "RUNS_DIR", tmp_path)
    rd = make_run(tmp_path)
    (rd / "factory" / "report" / "shots").mkdir(parents=True)
    (rd / "factory" / "report" / "shots" / "home-390.png").write_bytes(b"png")
    (rd / "factory" / "mock").mkdir()
    (rd / "factory" / "mock" / "index.html").write_text("<h1>m</h1>", encoding="utf-8")
    (rd / "factory" / "notes.html").write_text("<h1>x</h1>", encoding="utf-8")
    (rd / "inputs" / "photos" / "ref.png").write_bytes(b"png")
    ok = lambda rel: dashboard.file_for(rd.name, rel)
    assert ok("factory/report/shots/home-390.png")[1] == "image/png"
    assert ok("inputs/photos/ref.png") is not None
    assert ok("factory/mock/index.html")[1].startswith("text/html")
    assert ok("factory/notes.html") is None            # only the mock may be HTML
    for bad in ("state.json", "logs/maker.log", "factory/../state.json", "../x.png", "app/index.html", "holdout/test_acceptance.py", ""):
        assert ok(bad) is None, bad
    assert dashboard.file_for("../evil", "factory/x.png") is None and dashboard.file_for("nope", "factory/x.png") is None


def test_start_a_run_needs_a_required_input_and_passes_the_extras(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "RUNS_DIR", tmp_path)
    spawned = []
    monkeypatch.setattr(dashboard, "spawn_driver", lambda rid: spawned.append(rid))
    code, body = dashboard.act("/api/new", {"product_links": ["https://a.b"], "skills": ["x"]})
    assert code == 400 and "idea" in body["error"] and not spawned
    code, body = dashboard.act("/api/new", {"reverse": {"target": ""}})
    assert code == 400 and not spawned
    code, body = dashboard.act("/api/new", {"reverse": {"target": "Notion's board view", "scope": "feature", "links": ["https://notion.so/help"]}})
    assert code == 200 and json.loads((tmp_path / body["run"] / "state.json").read_text(encoding="utf-8"))["inputs"]["reverse"]["target"] == "Notion's board view"
    code, body = dashboard.act("/api/new", {"idea": "a photo organizer", "product_links": ["https://a.b/x"], "force_mode": "single"})
    assert code == 200 and spawned[-1] == body["run"]
    st = json.loads((tmp_path / body["run"] / "state.json").read_text(encoding="utf-8"))
    assert st["inputs"]["idea"] == "a photo organizer" and st["inputs"]["product_links"] == ["https://a.b/x"] and st["inputs"]["force_mode"] == "single"
    assert dashboard.act("/api/new", {"generate": True})[0] == 200
    assert len(spawned) == 3


def test_decisions_are_checked_against_what_the_gate_accepts(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "RUNS_DIR", tmp_path)
    gate = {"id": "gate-review-1", "kind": "review", "title": "t", "blurb": "b", "doc": "report.md", "accept": ["approve", "improve", "escalate"],
            "options": None, "deadline": None, "images": []}
    rd = make_run(tmp_path, status="gate", gate=gate)
    assert dashboard.act("/api/decide", {"run": rd.name, "decision": "tweak", "feedback": "x"})[0] == 409   # not offered at this gate
    assert dashboard.act("/api/decide", {"run": rd.name, "decision": "improve"})[0] == 400                    # feedback required
    assert dashboard.act("/api/reject", {"run": rd.name})[0] == 400
    code, _ = dashboard.act("/api/decide", {"run": rd.name, "decision": "improve", "feedback": "bigger buttons"})
    assert code == 200
    assert json.loads((rd / "decision.json").read_text(encoding="utf-8")) == {"gate": "gate-review-1", "decision": "improve", "feedback": "bigger buttons", "note": ""}
    assert dashboard.act("/api/approve", {"run": "nope"})[0] == 404


def test_choosing_an_idea_sends_its_index(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "RUNS_DIR", tmp_path)
    gate = {"id": "gate-ideas", "kind": "ideas", "title": "t", "blurb": "b", "doc": None, "accept": ["choose", "random"],
            "options": [{"title": "A", "pitch": "p"}, {"title": "B", "pitch": "p"}], "deadline": None, "images": []}
    rd = make_run(tmp_path, status="gate", gate=gate)
    assert dashboard.act("/api/decide", {"run": rd.name, "decision": "choose", "index": 1})[0] == 200
    assert json.loads((rd / "decision.json").read_text(encoding="utf-8"))["index"] == 1
    assert dashboard.act("/api/decide", {"run": rd.name, "decision": "random"})[0] == 200


def test_resume_refuses_when_the_driver_is_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "RUNS_DIR", tmp_path)
    spawned = []
    monkeypatch.setattr(dashboard, "spawn_driver", lambda rid: spawned.append(rid))
    rd = make_run(tmp_path)
    assert dashboard.act("/api/resume", {"run": rd.name})[0] == 409 and not spawned
    st = json.loads((rd / "state.json").read_text(encoding="utf-8"))
    st["heartbeat"] = 0
    (rd / "state.json").write_text(json.dumps(st), encoding="utf-8")
    assert dashboard.act("/api/resume", {"run": rd.name})[0] == 200 and spawned == [rd.name]


def test_always_use_documents_can_be_saved_and_deleted_safely(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "ALWAYS_DIR", tmp_path / "always")
    assert dashboard.act("/api/always", {"name": "../evil.md", "text": "x"})[0] == 400
    assert dashboard.act("/api/always", {"name": "notes.txt", "text": "x"})[0] == 400
    assert dashboard.act("/api/always", {"name": "brand colours.md", "text": "Primary #FF5A36"})[0] == 200
    assert checks.load_always(tmp_path / "always") == [("brand colours.md", "Primary #FF5A36")]
    assert dashboard.act("/api/always/delete", {"name": "missing.md"})[0] == 404
    assert dashboard.act("/api/always/delete", {"name": "brand colours.md"})[0] == 200
    assert checks.load_always(tmp_path / "always") == []


def test_skill_library_upload_list_and_delete_over_the_api(tmp_path, monkeypatch):
    import base64
    monkeypatch.setattr(checks, "SKILLS_LIBRARY", tmp_path / "lib")
    b64 = lambda t: base64.b64encode(t.encode()).decode()
    code, body = dashboard.act("/api/skills/upload", {"files": [{"path": "my-skill/SKILL.md", "data": b64("---\nname: my-skill\ndescription: Does X\n---\nbody")},
                                                                {"path": "my-skill/notes.md", "data": b64("n")}]})
    assert code == 200 and body["name"] == "my-skill"
    assert checks.list_library(tmp_path / "lib") == [{"name": "my-skill", "description": "Does X", "files": 2}]
    assert dashboard.act("/api/skills/upload", {"files": [{"path": "../x/SKILL.md", "data": b64("x")}]})[0] == 400
    assert dashboard.act("/api/skills/upload", {"files": []})[0] == 400
    assert dashboard.act("/api/skills/delete", {"name": "../lib"})[0] == 404
    assert dashboard.act("/api/skills/delete", {"name": "my-skill"})[0] == 200 and checks.list_library(tmp_path / "lib") == []


def test_open_works_for_apps_with_screens_only(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "RUNS_DIR", tmp_path)
    opened = []
    monkeypatch.setattr(dashboard.os, "startfile", lambda p: opened.append(p), raising=False)
    rd = make_run(tmp_path)
    assert dashboard.act("/api/open", {"run": rd.name})[0] == 404
    (rd / "app").mkdir()
    (rd / "app" / "index.html").write_text("x", encoding="utf-8")
    (rd / "factory" / "app.json").write_text(json.dumps({"type": "static", "entry": "index.html", "screens": [{"name": "h", "path": "index.html"}]}), encoding="utf-8")
    assert dashboard.act("/api/open", {"run": rd.name})[0] == 200 and opened[0].endswith("index.html")
    assert dashboard.build_state(rd.name, tmp_path)["run"]["can_open"] is True
    # a server app is started (here its start command doesn't exist, so it fails with a clear message); a CLI can't be opened
    (rd / "factory" / "app.json").write_text(json.dumps({"type": "server", "start": "definitely-not-a-command", "port": 1, "screens": [{"name": "h", "path": "/"}]}), encoding="utf-8")
    code, res = dashboard.act("/api/open", {"run": rd.name})
    assert code == 500 and "could not start" in res["error"]
    (rd / "factory" / "app.json").write_text(json.dumps({"type": "cli", "test": "x"}), encoding="utf-8")
    assert dashboard.act("/api/open", {"run": rd.name})[0] == 404
    (rd / "factory" / "app.json").write_text(json.dumps({"type": "static", "entry": "../../x.html", "screens": []}), encoding="utf-8")
    assert dashboard.act("/api/open", {"run": rd.name})[0] == 404


def test_answering_a_gate_restarts_a_driver_that_is_not_running(tmp_path, monkeypatch):
    """The click used to be saved and then nothing happened (the run's driver had died). Now the answer starts it again."""
    monkeypatch.setattr(factory, "RUNS_DIR", tmp_path)
    started = []
    monkeypatch.setattr(dashboard, "spawn_driver", lambda run_id: started.append(run_id))
    dashboard.RESUMED_AT.clear()
    gate = {"id": "gate-review-1", "kind": "review", "title": "Review", "blurb": "", "doc": None, "accept": ["approve", "improve"], "options": None, "deadline": None, "images": []}
    rd = make_run(tmp_path, status="gate", gate=gate, heartbeat=time.time() - 3600)      # the driver stopped an hour ago
    code, body = dashboard.act("/api/decide", {"run": rd.name, "decision": "approve"})
    assert code == 200 and body == {"ok": True, "resumed": True} and started == [rd.name]
    assert json.loads((rd / "decision.json").read_text(encoding="utf-8"))["decision"] == "approve"
    code, body = dashboard.act("/api/decide", {"run": rd.name, "decision": "approve"})     # a second click does not start a second driver
    assert code == 200 and body["resumed"] is False and started == [rd.name]


def test_answering_a_gate_does_not_restart_a_driver_that_is_running(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "RUNS_DIR", tmp_path)
    started = []
    monkeypatch.setattr(dashboard, "spawn_driver", lambda run_id: started.append(run_id))
    dashboard.RESUMED_AT.clear()
    gate = {"id": "gate-review-1", "kind": "review", "title": "Review", "blurb": "", "doc": None, "accept": ["approve"], "options": None, "deadline": None, "images": []}
    rd = make_run(tmp_path, status="gate", gate=gate, heartbeat=time.time())
    assert dashboard.act("/api/approve", {"run": rd.name}) == (200, {"ok": True, "resumed": False}) and started == []
    assert dashboard.act("/api/decide", {"run": "nope", "decision": "approve"})[0] == 404
