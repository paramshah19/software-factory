"""Tests for the automatic checks and input helpers. The browser tests use the real Edge when it is installed."""

import json
import shutil
from pathlib import Path

import pytest

import checks

EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
needs_browser = pytest.mark.skipif(not (EDGE.exists() and shutil.which("uv")), reason="needs Edge and uv")


# --------------------------------------------------------------------------- secrets and external assets


def test_scan_secrets_finds_real_looking_secrets_and_ignores_placeholders(tmp_path):
    (tmp_path / "a.py").write_text('KEY = "AKIAABCDEFGHIJKLMNOP"\nx = 1\n', encoding="utf-8")
    (tmp_path / "b.js").write_text('const t = "sk-abcdefghijklmnopqrstuvwx";\n', encoding="utf-8")
    (tmp_path / "c.py").write_text('password = "hunter2hunter2"\n', encoding="utf-8")
    (tmp_path / "ok.py").write_text('password = "changeme-example"\napi_key = "your_api_key_here"\n', encoding="utf-8")
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text('API_KEY="abcdefgh12345678"\n', encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text('const k = "sk-abcdefghijklmnopqrstuvwx";', encoding="utf-8")
    found = {(f["file"], f["kind"]) for f in checks.scan_secrets(tmp_path)}
    assert ("a.py", "AWS access key") in found
    assert ("b.js", "API key (sk-)") in found
    assert ("c.py", "hard-coded credential") in found
    assert (".env", ".env file in the project") in found
    assert not any(f[0] in ("ok.py", ".env.example") or f[0].startswith("node_modules") for f in found)


def test_scan_secrets_clean_project(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hi</h1><script>const n = 'streak';</script>", encoding="utf-8")
    assert checks.scan_secrets(tmp_path) == []


def test_scan_external_assets_lists_cdn_scripts(tmp_path):
    (tmp_path / "index.html").write_text('<script src="https://cdn.example.com/lib.js"></script><link href="style.css">', encoding="utf-8")
    assert checks.scan_external_assets(tmp_path) == [{"file": "index.html", "url": "https://cdn.example.com/lib.js"}]


def test_dependency_audit_skips_gracefully(tmp_path):
    assert checks.dependency_audit(tmp_path)["status"] == "none"
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert checks.dependency_audit(tmp_path, run=lambda *a, **k: (1, "boom"))["status"] == "skipped"
    reqs = "flask==3.0.0\n"
    calls = iter([(0, reqs), (1, "Found 1 known vulnerability: PYSEC-2024-1")])
    assert checks.dependency_audit(tmp_path, run=lambda *a, **k: next(calls))["status"] == "issues"
    calls = iter([(0, reqs), (0, "No known vulnerabilities found")])
    assert checks.dependency_audit(tmp_path, run=lambda *a, **k: next(calls))["status"] == "ok"
    calls = iter([(0, reqs), (1, "could not connect")])
    assert checks.dependency_audit(tmp_path, run=lambda *a, **k: next(calls))["status"] == "skipped"


# --------------------------------------------------------------------------- app.json


def test_validate_app_json(tmp_path):
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    good = {"type": "static", "entry": "index.html", "screens": [{"name": "home", "path": "index.html?demo=1"}]}
    assert checks.validate_app_json(good, tmp_path) == []
    assert checks.validate_app_json({"type": "cli"}, tmp_path) == []
    assert checks.validate_app_json({"type": "nope"}, tmp_path)
    assert any("screens" in e for e in checks.validate_app_json({"type": "static", "entry": "index.html"}, tmp_path))
    assert any("does not exist" in e for e in checks.validate_app_json(
        {"type": "static", "entry": "index.html", "screens": [{"name": "x", "path": "missing.html"}]}, tmp_path))
    assert any("start" in e for e in checks.validate_app_json({"type": "server", "port": 8000, "screens": [{"name": "h", "path": "/"}]}, tmp_path))
    assert any("port" in e for e in checks.validate_app_json({"type": "server", "start": "uv run x", "screens": [{"name": "h", "path": "/"}]}, tmp_path))
    assert any("smoke_test" in e for e in checks.validate_app_json({**good, "smoke_test": "tests/nope.py"}, tmp_path))
    data, errs = checks.read_app_json(tmp_path / "missing.json", tmp_path)
    assert data is None and errs


def test_screen_problems_reports_each_kind():
    ok = {"ok": True, "screens": [{"name": "home", "size": [390, 844], "overflow": False, "console_errors": [], "failed_requests": []}]}
    assert checks.screen_problems(ok) == []
    bad = {"ok": True, "screens": [{"name": "home", "size": [390, 844], "overflow": True, "console_errors": ["boom"],
                                    "failed_requests": ["http://x/a.png"], "load_error": "timeout"}]}
    text = " | ".join(checks.screen_problems(bad))
    for word in ("failed to load", "console errors", "failed requests", "overflows"):
        assert word in text
    assert "could not be opened" in checks.screen_problems({"ok": False, "error": "no browser"})[0]


# --------------------------------------------------------------------------- skills and always-use documents


def test_skills_are_found_listed_and_catalogued(tmp_path, monkeypatch):
    mine, installed = tmp_path / "mine", tmp_path / "installed"
    for root, n, text in ((installed, "frontend-design", "---\nname: frontend-design\ndescription: >\n  Polished UI work\n  for screens\n---\nBody of fd"),
                          (mine, "my-skill", "---\nname: my-skill\ndescription: Does my thing\n---\nBody of mine"),
                          (installed, "my-skill", "---\nname: my-skill\ndescription: the installed twin\n---\nOTHER"),
                          (installed, "plain", "# Plain\n\nNo frontmatter here.")):
        (root / n).mkdir(parents=True, exist_ok=True)
        (root / n / "SKILL.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(checks, "skill_sources", lambda: [(mine, "yours"), (installed, "installed")])
    monkeypatch.setattr(checks.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    assert checks.find_skill("my-skill").parent.parent == mine  # your library wins over an installed skill with the same name
    assert checks.find_skill("plugin:frontend-design") is not None and checks.find_skill("../etc") is None and checks.find_skill("missing") is None
    cat = {c["name"]: c for c in checks.skill_catalog()}
    assert set(cat) == {"frontend-design", "my-skill", "plain"}
    assert cat["my-skill"]["source"] == "yours" and cat["my-skill"]["description"] == "Does my thing"
    assert cat["frontend-design"]["description"] == "Polished UI work for screens"  # YAML block text is joined
    assert cat["plain"]["description"] == "No frontmatter here."
    assert checks.list_skills() == ["frontend-design", "my-skill", "plain"]
    assert checks.skill_text("my-skill") == "Body of mine"


def _b64(text):
    import base64
    return base64.b64encode(text.encode() if isinstance(text, str) else text).decode()


def test_dropping_a_skill_folder_stores_it_under_its_folder_name(tmp_path):
    files = [{"path": "pdf-tips/SKILL.md", "data": _b64("---\nname: pdf-tips\ndescription: PDF help\n---\nDo it")},
             {"path": "pdf-tips/refs/guide.md", "data": _b64("guide")}, {"path": "pdf-tips/.hidden", "data": _b64("x")},
             {"path": "pdf-tips/tool.exe", "data": _b64("x")}]
    name = checks.save_skill_upload(files, None, tmp_path / "lib")
    assert name == "pdf-tips"
    assert (tmp_path / "lib" / "pdf-tips" / "SKILL.md").exists() and (tmp_path / "lib" / "pdf-tips" / "refs" / "guide.md").read_text(encoding="utf-8") == "guide"
    assert not (tmp_path / "lib" / "pdf-tips" / ".hidden").exists() and not (tmp_path / "lib" / "pdf-tips" / "tool.exe").exists()
    assert checks.list_library(tmp_path / "lib") == [{"name": "pdf-tips", "description": "PDF help", "files": 2}]
    checks.save_skill_upload([{"path": "pdf-tips/SKILL.md", "data": _b64("# New\n\nreplaced")}], None, tmp_path / "lib")  # dropping again replaces it
    assert checks.list_library(tmp_path / "lib")[0]["files"] == 1


def test_dropping_a_single_markdown_file_makes_it_the_skill(tmp_path):
    assert checks.save_skill_upload([{"path": "Great Tips.md", "data": _b64("# Tips\n\nBe great.")}], None, tmp_path / "lib") == "Great-Tips"
    assert (tmp_path / "lib" / "Great-Tips" / "SKILL.md").read_text(encoding="utf-8").startswith("# Tips")
    assert checks.save_skill_upload([{"path": "SKILL.md", "data": _b64("---\nname: from-meta\n---\nx")}], None, tmp_path / "lib") == "from-meta"
    assert checks.save_skill_upload([{"path": "a.md", "data": _b64("x")}], "chosen-name", tmp_path / "lib") == "chosen-name"


def test_unsafe_or_unusable_skill_uploads_are_refused(tmp_path):
    lib = tmp_path / "lib"
    bad = [[], [{"path": "../evil/SKILL.md", "data": _b64("x")}], [{"path": "/abs/SKILL.md", "data": _b64("x")}], [{"path": "C:/x/SKILL.md", "data": _b64("x")}],
           [{"path": "s/SKILL.md", "data": "not base64!!"}], [{"path": "s/readme.txt", "data": _b64("x")}, {"path": "s/other.txt", "data": _b64("y")}],
           [{"path": "s/tool.exe", "data": _b64("x")}], [{"path": f"s/f{i}.md", "data": _b64("x")} for i in range(201)],
           [{"path": "s/SKILL.md", "data": _b64(b"x" * 2_100_000)}]]
    for files in bad:
        with pytest.raises(ValueError):
            checks.save_skill_upload(files, None, lib)
    assert not lib.exists() or not list(lib.iterdir())


def test_library_skills_can_be_deleted_only_by_exact_name(tmp_path):
    lib = tmp_path / "lib"
    checks.save_skill_upload([{"path": "keep-me/SKILL.md", "data": _b64("x")}], None, lib)
    for bad in ("../lib", "keep-me/..", "", "missing", "KEEP-ME "):
        assert checks.delete_library_skill(bad, lib) is False
    assert (lib / "keep-me").exists() and checks.delete_library_skill("keep-me", lib) is True and not (lib / "keep-me").exists()


def test_always_use_documents_load_copy_and_name_checks(tmp_path):
    d = tmp_path / "always"
    d.mkdir()
    (d / "brand.md").write_text("Primary colour: #FF5A36", encoding="utf-8")
    (d / "README.md").write_text("how this folder works", encoding="utf-8")
    (d / "empty.md").write_text("   ", encoding="utf-8")
    assert checks.load_always(d) == [("brand.md", "Primary colour: #FF5A36")]
    assert checks.copy_always(tmp_path / "run" / "always", d) == ["brand.md"]
    assert (tmp_path / "run" / "always" / "brand.md").exists()
    assert checks.load_always(tmp_path / "missing") == []
    assert checks.safe_doc_name("brand colours.md") == "brand colours.md"
    for bad in ("../x.md", "a/b.md", "x.txt", ".md", "a\\b.md"):
        assert checks.safe_doc_name(bad) is None


def test_duplicate_screens_are_found_per_width(tmp_path):
    for name, data in (("home-390.png", b"A"), ("running-390.png", b"A"), ("break-390.png", b"B"), ("home-1280.png", b"A"), ("running-1280.png", b"C"), ("home-dark.png", b"A")):
        (tmp_path / name).write_bytes(data)
    assert checks.duplicate_screens(tmp_path) == [("home", "running", 390)]
    assert checks.duplicate_screens(tmp_path / "missing") == []


def test_ideas_and_input_validation_and_estimates():
    ok = {"ideas": [{"title": f"t{i}", "pitch": "p"} for i in range(5)]}
    assert checks.validate_ideas(ok) == []
    assert checks.validate_ideas({"ideas": ok["ideas"][:2]})
    assert checks.validate_ideas({"ideas": [{"title": "", "pitch": ""}] * 4})
    assert checks.validate_ideas([])
    assert checks.check_inputs({}) and checks.check_inputs({"idea": "  "})
    for good in ({"idea": "x"}, {"spec": "y"}, {"generate": True}, {"idea": "x", "spec": "y"}):
        assert checks.check_inputs(good) is None
    assert checks.check_inputs({"product_links": ["https://a.b"], "skills": ["x"]})  # extras alone are not enough
    assert checks.check_inputs({"reverse": {"target": "  "}}) and checks.check_inputs({"reverse": "Notion"})
    assert checks.check_inputs({"reverse": {"target": "Notion's board view", "scope": "feature"}}) is None  # reverse engineering is a required input too
    assert checks.clean_links(["https://a.b/c", "javascript:alert(1)", "ftp://x", " http://ok.dev "]) == ["https://a.b/c", "http://ok.dev"]
    assert checks.estimate("single", "small")["usd"][0] < checks.estimate("multi", "small")["usd"][0]
    assert checks.estimate("multi", "weird") == checks.estimate("multi", "medium")


# --------------------------------------------------------------------------- real browser


PAGE = """<!doctype html><title>t</title><button id=b onclick="document.title='clicked'">go</button>
<div style="width:900px;height:20px;background:#ccc">wide</div><script>console.error('boom on purpose')</script>"""


@needs_browser
def test_capture_screens_with_the_real_browser_flags_problems_and_saves_screenshots(tmp_path):
    (tmp_path / "index.html").write_text(PAGE, encoding="utf-8")
    cfg = {"type": "static", "entry": "index.html", "screens": [{"name": "home", "path": "index.html"}]}
    res = checks.capture_screens(tmp_path, cfg, tmp_path / "shots")
    assert res["ok"], res
    phone = next(s for s in res["screens"] if s["size"][0] == 390)
    assert phone["overflow"] is True and any("boom" in e for e in phone["console_errors"])
    assert (tmp_path / "shots" / "home-390.png").stat().st_size > 1000
    assert (tmp_path / "shots" / "home-1280.png").exists() and (tmp_path / "shots" / "home-dark.png").exists()
    assert checks.screen_problems(res)


@needs_browser
def test_capture_screens_clean_page_has_no_problems(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><meta name=viewport content='width=device-width'><h1>hello</h1>", encoding="utf-8")
    cfg = {"type": "static", "entry": "index.html", "screens": [{"name": "home", "path": "index.html"}]}
    assert checks.screen_problems(checks.capture_screens(tmp_path, cfg, tmp_path / "shots")) == []


@needs_browser
def test_capture_screens_reports_a_server_that_never_starts(tmp_path):
    import socket
    with socket.socket() as sock:  # a port nobody is listening on right now
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
    cfg = {"type": "server", "start": ["python", "-c", "import time; time.sleep(1)"], "port": free_port, "start_timeout": 3,
           "screens": [{"name": "home", "path": "/"}]}
    res = checks.capture_screens(tmp_path, cfg, tmp_path / "shots")
    assert res["ok"] is False and "port" in res["error"]


@needs_browser
def test_run_smoke_test_runs_a_playwright_test_against_the_app(tmp_path):
    (tmp_path / "index.html").write_text(PAGE, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_smoke_browser.py").write_text(
        "import os\nfrom pathlib import Path\nfrom playwright.sync_api import sync_playwright\n"
        "def test_click():\n    app = Path(os.environ['FACTORY_APP_DIR'])\n    with sync_playwright() as p:\n"
        "        b = p.chromium.launch(channel='msedge')\n        pg = b.new_page()\n        pg.goto((app / 'index.html').as_uri())\n"
        "        pg.click('#b')\n        assert pg.title() == 'clicked'\n        b.close()\n", encoding="utf-8")
    rc, out = checks.run_smoke_test(tmp_path, "tests/test_smoke_browser.py")
    assert rc == 0, out
