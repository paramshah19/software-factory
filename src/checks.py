"""Automatic checks and input helpers for the factory. Plain functions; no dependency on factory.py."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional

from common import HERE, ROOT, WORKSPACE, posix, sh

ALWAYS_DIR = WORKSPACE / "always-use"
BROWSE_TOOL = HERE / "tools_browse.py"
SIZES = [[390, 844], [1280, 800]]  # phone, desktop

# usd_low, usd_high, minutes_low, minutes_high — from measured runs ($4.2 / 15 min single with 2 critique rounds; $10.5 / 33 agent-min multi)
ESTIMATES = {
    "single": {"small": (3, 5, 10, 16), "medium": (4, 7, 12, 22), "large": (6, 10, 15, 30)},
    "multi": {"small": (6, 10, 25, 40), "medium": (10, 16, 35, 55), "large": (16, 26, 50, 90)},
}


# The table above was measured with the strongest models (now the "max" Quality). The other presets are ESTIMATES until
# measured: balanced moves planning/design from Opus to Sonnet (half the price per token) and caps plans at 6 cards;
# lean also puts the maker, spec writer and reviewers on Sonnet. `src/factory.py stats` shows the real numbers per run.
QUALITY_FACTOR = {"lean": {"single": 0.55, "multi": 0.45}, "balanced": {"single": 1.0, "multi": 0.6}, "max": {"single": 1.0, "multi": 1.0}}


def estimate(mode: str, size: str, quality: str = "balanced") -> dict:
    lo, hi, tlo, thi = ESTIMATES[mode][size if size in ESTIMATES[mode] else "medium"]
    f = QUALITY_FACTOR.get(quality, QUALITY_FACTOR["balanced"])[mode]
    return {"usd": [max(1, round(lo * f)), max(2, round(hi * f))], "minutes": [tlo, thi]}


# --------------------------------------------------------------------------- secret + dependency scans

SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("API key (sk-)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("secret payment key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{10,}\b")),  # pk_ keys are publishable: allowed
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("hard-coded credential", re.compile(r"""(?i)\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*["'][^"'\s]{8,}["']""")),
]
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".worktrees", ".pytest_cache", "dist", "build"}
TEXT_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".md", ".txt", ".toml", ".yaml", ".yml", ".cfg",
                 ".ini", ".env", ".cmd", ".bat", ".ps1", ".sh", ""}


def iter_text_files(root: Path):
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES and p.stat().st_size < 600_000:
            yield p


def scan_secrets(root: Path) -> list[dict]:
    """Return one finding per suspicious line. `.env.example` and test fixtures are allowed to look secret-like."""
    found = []
    for p in iter_text_files(root):
        rel = p.relative_to(root).as_posix()
        if p.name == ".env":
            found.append({"file": rel, "line": 0, "kind": ".env file in the project"})
            continue
        if p.name.endswith(".example") or p.name == "uv.lock" or p.name == "package-lock.json":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for kind, rx in SECRET_PATTERNS:
                if rx.search(line):
                    if kind == "hard-coded credential" and re.search(r"(?i)example|placeholder|changeme|your[_-]|<.*>|xxx|test|fake|dummy", line):
                        continue
                    found.append({"file": rel, "line": n, "kind": kind})
                    break
    return found[:50]


def scan_external_assets(root: Path) -> list[dict]:
    """Scripts or styles loaded from the internet: not blocking, but the owner should know (offline use, supply chain)."""
    out = []
    rx = re.compile(r"""<(?:script|link)[^>]+(?:src|href)=["'](https?://[^"']+)["']""", re.I)
    for p in iter_text_files(root):
        if p.suffix.lower() != ".html":
            continue
        for m in rx.finditer(p.read_text(encoding="utf-8", errors="ignore")):
            out.append({"file": p.relative_to(root).as_posix(), "url": m.group(1)})
    return out[:20]


def dependency_audit(app: Path, run: Callable = sh) -> dict:
    """Audit Python dependencies with pip-audit when the app has a lock/pyproject. Skips (never fails) when it can't run."""
    if not (app / "pyproject.toml").exists():
        return {"status": "none", "detail": "no Python dependencies to audit"}
    rc, out = run(["uv", "export", "--project", str(app), "--no-dev", "--no-hashes", "--format", "requirements-txt"], app, 120)
    if rc != 0 or not out.strip():
        return {"status": "skipped", "detail": "could not list the dependencies"}
    reqs = [l for l in out.splitlines() if l and not l.startswith(("#", " ", "-e", "."))]
    if not reqs:
        return {"status": "none", "detail": "no third-party dependencies"}
    with tempfile.TemporaryDirectory() as td:
        rf = Path(td) / "requirements.txt"
        rf.write_text("\n".join(reqs), encoding="utf-8")
        rc, out = run(["uv", "run", "--with", "pip-audit", "pip-audit", "-r", str(rf), "--no-deps", "--disable-pip"], app, 240)
    if rc == 0:
        return {"status": "ok", "detail": f"{len(reqs)} dependencies, no known vulnerabilities"}
    if re.search(r"(?i)vulnerab|PYSEC|GHSA|CVE-", out):
        return {"status": "issues", "detail": out.strip()[-800:]}
    return {"status": "skipped", "detail": "the audit tool could not run (offline?)"}


# --------------------------------------------------------------------------- app.json (how to run / open the app)


def validate_app_json(data, app: Path, check_files: bool = True) -> list[str]:
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["app.json must be an object"]
    typ = data.get("type")
    import kinds
    if typ not in kinds.ALL_KINDS:
        errs.append("type must be one of: " + ", ".join(kinds.ALL_KINDS))
        return errs
    if typ in kinds.NEW_KINDS:
        return kinds.validate(data, app, check_files)
    if typ in ("static", "server"):
        screens = data.get("screens")
        if not isinstance(screens, list) or not screens:
            errs.append("screens must list at least one screen: [{\"name\": \"home\", \"path\": \"...\"}]")
        else:
            for s in screens:
                if not isinstance(s, dict) or not re.fullmatch(r"[a-z0-9-]+", str(s.get("name", ""))) or not s.get("path"):
                    errs.append("each screen needs a lowercase-dash name and a path")
                elif check_files and typ == "static" and not (app / str(s["path"]).partition("?")[0]).exists():
                    errs.append(f"screen {s['name']}: file {str(s['path']).partition('?')[0]} does not exist in app/")
    if check_files and typ == "static" and not (app / str(data.get("entry", "index.html"))).exists():
        errs.append(f"entry file {data.get('entry', 'index.html')} does not exist in app/")
    if typ == "server":
        if not data.get("start"):
            errs.append("server apps need 'start' (the command that starts the server, using the PORT environment variable)")
        if not isinstance(data.get("port"), int):
            errs.append("server apps need an integer 'port'")
    st = data.get("smoke_test")
    if check_files and st and not (app / str(st)).exists():
        errs.append(f"smoke_test file {st} does not exist in app/")
    return errs


def read_app_json(path: Path, app: Path) -> tuple[Optional[dict], list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, [f"factory/app.json is missing or not valid JSON: {e}"]
    return data, validate_app_json(data, app)


# --------------------------------------------------------------------------- real-browser checks


def capture_screens(app: Path, app_json: dict, out_dir: Path, run: Callable = sh, sizes=None, env: Optional[dict] = None) -> dict:
    """Screenshot every screen at phone and desktop size and collect console errors, failed requests, overflow."""
    if app_json.get("type") not in ("static", "server"):
        return {"ok": True, "screens": [], "note": f"{app_json.get('type')} app: no browser screens"}
    cfg = {"app_dir": str(app), "type": app_json["type"], "entry": app_json.get("entry"), "start": app_json.get("start"),
           "port": app_json.get("port"), "screens": app_json["screens"], "out_dir": str(out_dir), "sizes": sizes or SIZES}
    with tempfile.TemporaryDirectory() as td:
        cf = Path(td) / "browse.json"
        cf.write_text(json.dumps(cfg), encoding="utf-8")
        rc, out = run(["uv", "run", "--with", "playwright", "python", str(BROWSE_TOOL), str(cf)], ROOT, 240, *([{**os.environ, **env}] if env else []))
    m = re.search(r"@@RESULT@@ (\{.*\})", out)
    if not m:
        return {"ok": False, "error": f"browser tool failed (exit {rc}): {out.strip()[-300:]}"}
    return json.loads(m.group(1))


def duplicate_screens(shots_dir: Path) -> list[tuple[str, str, int]]:
    """Screens whose screenshots are byte-identical at the same width: the app ignores what the screen path asks for."""
    import hashlib
    groups: dict[tuple[int, str], list[str]] = {}
    for p in sorted(Path(shots_dir).glob("*.png")):
        m = re.fullmatch(r"(.+)-(\d+)\.png", p.name)
        if m:
            groups.setdefault((int(m.group(2)), hashlib.sha1(p.read_bytes()).hexdigest()), []).append(m.group(1))
    out = []
    for (w, _), names in groups.items():
        for other in names[1:]:
            out.append((names[0], other, w))
    return out


def run_smoke_test(app: Path, rel_test: str, run: Callable = sh) -> tuple[int, str]:
    env = {**os.environ, "FACTORY_APP_DIR": str(app)}
    rc, out = run(["uv", "run", "--with", "playwright", "--with", "pytest", "pytest", rel_test, "-q", "-p", "no:cacheprovider"], app, 300, env)
    return rc, out[-1500:]


def screen_problems(result: dict) -> list[str]:
    """Human-readable list of what is wrong on the screens (empty = fine)."""
    if not result.get("ok"):
        return [f"the app could not be opened in a browser: {result.get('error', 'unknown error')}"]
    probs = []
    for s in result.get("screens", []):
        tag = f"{s['name']} at {s['size'][0]}px"
        if s.get("load_error"):
            probs.append(f"{tag}: page failed to load ({s['load_error']})")
        if s.get("console_errors"):
            probs.append(f"{tag}: console errors: {'; '.join(s['console_errors'][:3])}")
        if s.get("failed_requests"):
            probs.append(f"{tag}: failed requests: {', '.join(s['failed_requests'][:3])}")
        if s.get("overflow"):
            probs.append(f"{tag}: content overflows the width (sideways scrolling)")
    return probs


def take_screens(app: Path, app_json: dict, out_dir: Path, logs: Optional[Path] = None) -> tuple[dict, list[str]]:
    """Screenshots for any kind of app: browser shots (static, server), the phone app's web export (mobile), off-screen
    renders (desktop). Kinds without screens return an empty, passing result. Returns (result, extra problems)."""
    import kinds
    capture = lambda a, c, o, sizes=None, env=None: capture_screens(a, c, o, sizes=sizes, env=env)
    typ = app_json.get("type")
    if typ in ("static", "server"):
        return capture_screens(app, app_json, out_dir), []
    if typ == "mobile":
        return kinds.mobile_screens(app, app_json, out_dir, capture, dict(os.environ))
    if typ == "desktop":
        return kinds.desktop_screens(app, app_json, out_dir, dict(os.environ))
    return {"ok": True, "screens": [], "note": f"{typ} app: no screens"}, []


def kind_checks(app: Path, app_json: dict, out_dir: Path, logs: Path) -> tuple[Optional[dict], list[str]]:
    """The driver's own checks for the kinds that have no browser screens (or several services). (None, []) for the others."""
    import kinds
    typ = app_json.get("type")
    env = dict(os.environ)
    logs.mkdir(parents=True, exist_ok=True)
    capture = lambda a, c, o, sizes=None, env=None: capture_screens(a, c, o, sizes=sizes, env=env)
    if typ == "api":
        return kinds.check_api(app, app_json, logs, env)
    if typ == "worker":
        return kinds.check_worker(app, app_json, logs, env)
    if typ == "library":
        return kinds.check_library(app, app_json, logs, env)
    if typ == "godot":
        return kinds.check_godot(app, app_json, logs, env)
    if typ == "multi":
        return kinds.check_multi(app, app_json, logs, env, out_dir, capture)
    return None, []


# --------------------------------------------------------------------------- skills and always-use documents


SKILLS_LIBRARY = WORKSPACE / "skills-library"   # skills the owner dropped into the dashboard
SKILL_EXT = {".md", ".txt", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".sh", ".ps1", ".css", ".html", ".svg", ".csv", ".png", ".jpg", ".jpeg"}
MAX_SKILL_FILES, MAX_SKILL_FILE_BYTES, MAX_SKILL_TOTAL_BYTES = 200, 2_000_000, 8_000_000


def skill_sources() -> list[tuple[Path, str]]:
    """Where skills live on this machine: your library first, then installed, workspace and project skills."""
    home = Path.home()
    return [(SKILLS_LIBRARY, "yours"), (home / ".claude" / "skills", "installed"), (home / ".agents" / "skills", "installed"),
            (home / ".codex" / "skills", "installed"), (ROOT.parent.parent / ".claude" / "skills", "workspace"),
            (ROOT / ".claude" / "skills", "project")]


def skill_roots() -> list[Path]:
    return [p for p, _ in skill_sources()]


def parse_skill_meta(text: str, fallback: str) -> tuple[str, str]:
    """(name, one-line description) from a SKILL.md, tolerant of YAML block scalars and missing frontmatter."""
    m = re.match(r"\A---\s*\n(.*?)\n---", text, re.S)
    name, desc = fallback, ""
    if m:
        block = m.group(1)
        nm = re.search(r"^name:\s*(.+)$", block, re.M)
        if nm:
            name = nm.group(1).strip().strip("'\"") or fallback
        dm = re.search(r"^description:[ \t]*(.*)$", block, re.M)
        if dm:
            val = dm.group(1).strip()
            if val in ("", ">", "|", ">-", "|-"):
                parts = []
                for line in block[dm.end():].split("\n")[1:]:
                    if line.startswith((" ", "\t")):
                        parts.append(line.strip())
                    elif line.strip():
                        break
                val = " ".join(parts)
            desc = val.strip().strip("'\"")
    if not desc:
        for line in (text[m.end():] if m else text).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                desc = line
                break
    return name, " ".join(desc.split())[:300]


def _plugin_skill_files():
    plug = Path.home() / ".claude" / "plugins"
    if plug.is_dir():
        for dirpath, dirs, files in os.walk(plug):
            if len(Path(dirpath).relative_to(plug).parts) > 8:
                dirs[:] = []
                continue
            if "SKILL.md" in files and Path(dirpath).parent.name == "skills":
                yield Path(dirpath) / "SKILL.md"


def skill_catalog() -> list[dict]:
    """Every skill the agents could use: [{name, description, source, path}]. Your library wins on a name clash."""
    seen: dict[str, dict] = {}

    def add(path: Path, source: str):
        name = path.parent.name
        if name in seen:
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        _, desc = parse_skill_meta(text, name)
        seen[name] = {"name": name, "description": desc, "source": source, "path": str(path)}
    for root, source in skill_sources():
        if root.is_dir():
            for p in sorted(root.glob("*/SKILL.md")):
                add(p, source)
    for p in _plugin_skill_files():
        add(p, "plugin")
    return list(seen.values())


def list_skills() -> list[str]:
    return sorted(c["name"] for c in skill_catalog())


def find_skill(name: str) -> Optional[Path]:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", name):
        return None
    name = name.split(":")[-1]
    for root in skill_roots():
        cand = root / name / "SKILL.md"
        if cand.exists():
            return cand
    return next((p for p in _plugin_skill_files() if p.parent.name == name), None)


def skill_text(name: str, cap: int = 14000) -> str:
    p = find_skill(name)
    if not p:
        return ""
    text = p.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"\A---.*?---\s*", "", text, count=1, flags=re.S)
    return text[:cap]


def sanitize_skill_name(name: str) -> Optional[str]:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name or "").strip()).strip("-.")
    return name[:60] if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name or "") else None


def save_skill_upload(files: list[dict], name_hint: Optional[str] = None, library: Optional[Path] = None) -> str:
    """Store a dropped skill (a folder, or just a .md file) in the library. files: [{path, data: base64}]. Returns the skill name."""
    import base64
    import shutil as _sh
    library = library or SKILLS_LIBRARY
    if not files or len(files) > MAX_SKILL_FILES:
        raise ValueError(f"drop between 1 and {MAX_SKILL_FILES} files")
    clean: dict[str, bytes] = {}
    total = 0
    for f in files:
        original = str(f.get("path") or "").replace("\\", "/")
        raw_path = original.strip("/")
        parts = [p for p in raw_path.split("/") if p not in ("", ".")]
        if not parts or ".." in parts or ":" in original or original.startswith("/"):
            raise ValueError(f"unsafe file path: {original!r}")
        if any(p.startswith(".") and p not in (".gitkeep",) for p in parts) or Path(parts[-1]).suffix.lower() not in SKILL_EXT:
            continue  # skip hidden files and file types a skill never needs
        try:
            data = base64.b64decode(str(f.get("data") or ""), validate=True)
        except (ValueError, TypeError):
            raise ValueError(f"{raw_path}: not valid file data")
        if len(data) > MAX_SKILL_FILE_BYTES:
            raise ValueError(f"{raw_path} is larger than {MAX_SKILL_FILE_BYTES // 1_000_000} MB")
        total += len(data)
        if total > MAX_SKILL_TOTAL_BYTES:
            raise ValueError(f"the skill is larger than {MAX_SKILL_TOTAL_BYTES // 1_000_000} MB in total")
        clean["/".join(parts)] = data
    if not clean:
        raise ValueError("no usable files in what you dropped")
    top = {k.split("/")[0] for k in clean}
    folder = None
    if len(top) == 1 and all("/" in k for k in clean):  # a dropped folder: its own name is the skill name; contents sit one level down
        folder = next(iter(top))
        clean = {k.split("/", 1)[1]: v for k, v in clean.items()}
    if "SKILL.md" not in clean:
        if len(clean) == 1 and next(iter(clean)).lower().endswith(".md"):  # a single markdown file is the skill itself
            only = next(iter(clean))
            folder = folder or Path(only).stem
            clean = {"SKILL.md": clean[only]}
        else:
            raise ValueError("a skill needs a SKILL.md at its top level")
    meta_name, _ = parse_skill_meta(clean["SKILL.md"].decode("utf-8", errors="replace"), folder or "skill")
    name = sanitize_skill_name(name_hint or folder or meta_name)
    if not name:
        raise ValueError("could not work out a name for this skill")
    dest = library / name
    if dest.exists():
        _sh.rmtree(dest)
    for rel, data in clean.items():
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    return name


def list_library(library: Optional[Path] = None) -> list[dict]:
    library = library or SKILLS_LIBRARY
    out = []
    if library.is_dir():
        for d in sorted(p for p in library.iterdir() if (p / "SKILL.md").is_file()):
            _, desc = parse_skill_meta((d / "SKILL.md").read_text(encoding="utf-8", errors="replace"), d.name)
            out.append({"name": d.name, "description": desc, "files": sum(1 for f in d.rglob("*") if f.is_file())})
    return out


def delete_library_skill(name: str, library: Optional[Path] = None) -> bool:
    import shutil as _sh
    library = library or SKILLS_LIBRARY
    safe = sanitize_skill_name(name)
    if not safe or safe != name or not (library / safe).is_dir():
        return False
    _sh.rmtree(library / safe)
    return True


def load_always(directory: Path = ALWAYS_DIR) -> list[tuple[str, str]]:
    if not directory.is_dir():
        return []
    out = []
    for p in sorted(directory.glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        text = p.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            out.append((p.name, text))
    return out


def safe_doc_name(name: str) -> Optional[str]:
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,60}\.md", name) or ".." in name:
        return None
    return name


def copy_always(dst: Path, directory: Path = ALWAYS_DIR) -> list[str]:
    dst.mkdir(parents=True, exist_ok=True)
    names = []
    for name, text in load_always(directory):
        (dst / name).write_text(text, encoding="utf-8")
        names.append(name)
    return names


# --------------------------------------------------------------------------- ideas, inputs


def validate_ideas(data) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("ideas"), list):
        return ["ideas.json must be {\"ideas\": [ ... ]}"]
    ideas = data["ideas"]
    if not 3 <= len(ideas) <= 8:
        return ["give between 3 and 8 ideas"]
    errs = []
    for i, it in enumerate(ideas):
        if not isinstance(it, dict) or not str(it.get("title", "")).strip() or not str(it.get("pitch", "")).strip():
            errs.append(f"idea {i + 1} needs a title and a pitch")
    return errs


def check_inputs(inputs: dict) -> Optional[str]:
    """At least one required input: an idea, a whole spec, generate-ideas, or something to reverse engineer."""
    rev = inputs.get("reverse")
    has_reverse = isinstance(rev, dict) and bool(str(rev.get("target") or "").strip())
    if not (str(inputs.get("idea") or "").strip() or str(inputs.get("spec") or "").strip() or inputs.get("generate") or has_reverse):
        return "give an idea or a whole spec, choose 'generate ideas', or say what to reverse engineer"
    return None


def clean_text(value, limit: int) -> Optional[str]:
    """Free text the owner typed (context for the app, the first-open experience): trimmed, capped, or None when empty."""
    text = str(value or "").replace("\r\n", "\n").strip()
    return text[:limit].rstrip() or None


def context_markdown(app_context: Optional[str], first_open: Optional[str]) -> str:
    """The two owner-written sections, as they appear in inputs/context.md and (verbatim) in the brief."""
    parts = []
    if app_context:
        parts.append("## App context (from the owner)\n" + app_context)
    if first_open:
        parts.append("## First-open experience (the owner's requirement)\nWhat the app's user should see and do the first time they open it:\n" + first_open)
    return "\n\n".join(parts) + ("\n" if parts else "")


def clean_links(links) -> list[str]:
    out = []
    for l in links or []:
        l = str(l).strip()
        if re.match(r"https?://\S+$", l):
            out.append(l)
    return out[:12]
