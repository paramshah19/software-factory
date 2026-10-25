"""The factory's pipeline: intake, decider, single-agent path and full pipeline. Mixed into Run (factory.py).

Flow (see "Software Factory Pipeline v2.pdf"):
  inputs -> [generate ideas -> you choose 1 or random] -> intake (brief.md) -> decider (you can override)
    single: maker (+ screenshot critique) -> auto checks -> security -> report -> you review (improve / escalate)
    multi : research -> spec -> visual mock -> you check -> design plan (+ hidden tests) -> technical plan -> final plan
            -> builders -> auto checks -> 3 reviewers -> fix rounds -> report -> you review (tweak / big change)
"""

from __future__ import annotations

import json
import os
import random
import re
import shlex
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import checks
import keys
import kinds
from common import StageFailed, failing_acs, git, posix, sh, spec_ac_text, spec_acs, validate_cards

MAX_FIX_ROUNDS = 2        # auto-check / security / review fix loops
MAX_CRITIQUE_ROUNDS = 2   # screenshot critique rounds for a fresh single-agent build
# which kinds of skill each agent can use (the skill agent labels every skill it picks with the sides it helps)
ROLE_SIDES = {"sf-maker": {"design", "technical"}, "sf-mock": {"design"}, "sf-designer": {"design"}, "sf-reviewer-ui": {"design"},
              "sf-builder": {"design", "technical"}, "sf-planner": {"technical"}, "sf-integrator": {"design", "technical"},
              "sf-reviewer": {"technical"}, "sf-holdout": {"technical"}, "sf-security": {"security"}}
SKILL_SIDES = {"all": ["design", "technical", "security"], "design": ["design"], "technical": ["technical", "security"]}
SKILL_CAP_TOTAL = 30000
MAX_SKILLS_PER_SIDE = 3


def build_rail(inputs: dict, mode: Optional[str]) -> list[dict]:
    """The stages shown on the dashboard, in order. Entries in the same sub-list share a column."""
    cols: list[list[tuple]] = []
    if inputs.get("generate"):
        cols += [[("ideas", "Generate ideas", "sf-ideator")], [("pick", "You choose", None)]]
    if inputs.get("reverse"):
        cols += [[("reverse", "Reverse engineer", "sf-reverse")]]
    cols += [[("intake", "Intake", "sf-intake")], [("decide", "Decider", "sf-decider")]]
    if mode == "single":
        cols += [[("skills", "Skill check", "sf-skills")], [("maker", "Build app", "sf-maker")], [("autocheck", "Auto checks", "driver")], [("security", "Security", "sf-security")],
                 [("report", "Report", "sf-writer")], [("review", "You review", None)]]
    elif mode == "multi":
        cols += [[("research", "Research", "sf-researcher")], [("spec", "Spec", "sf-spec")], [("mock", "Visual mock", "sf-mock")],
                 [("gate_spec", "You check", None)],
                 [("skills_design", "Design skills", "sf-skills"), ("skills_tech", "Technical skills", "sf-skills"), ("holdout", "Hidden tests", "sf-holdout")],
                 [("design", "Design plan", "sf-designer"), ("plan", "Technical plan", "sf-planner")], [("integrate", "Final plan", "sf-integrator")],
                 [("build", "Build", "sf-builder")],
                 [("autocheck", "Auto checks", "driver")], [("reviews", "Reviewers", "sf-reviewer")], [("report", "Report", "sf-writer")],
                 [("review", "You review", None)]]
    return [{"key": k, "label": l, "bot": b, "col": ci} for ci, col in enumerate(cols) for k, l, b in col]


def split_cmd(cmd) -> list[str]:
    return list(cmd) if isinstance(cmd, list) else shlex.split(str(cmd), posix=(os.name != "nt"))


def findings_text(findings: list[dict]) -> list[str]:
    out = []
    for f in findings:
        where = f.get("where") or f.get("file") or ""
        out.append(f"[{f.get('source', 'review')}] {where + ': ' if where else ''}{f.get('issue', '')} -> fix: {f.get('fix', 'fix it')}")
    return out


def review_json_problem(path: Path) -> Optional[str]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return f"{path.name} is missing or not valid JSON: {e}"
    if d.get("verdict") not in ("approve", "changes", "pass", "fail"):
        return "verdict must be 'approve' or 'changes' (security: 'pass' or 'fail')"
    if not isinstance(d.get("findings", []), list) or any(not isinstance(f, dict) for f in d.get("findings", [])):
        return "findings must be a list of objects"
    return None


class PipelineMixin:
    decider_wait = 90.0          # seconds the owner has to override the decider before it continues
    per_card_review = False      # the full pipeline reviews the integrated build once, not every card
    last_decision: dict = {}

    # ------------------------------------------------------------------ small helpers
    def uid(self, prefix: str) -> str:
        with self.lock:
            n = sum(1 for c in self.state["cards"] if c["id"].startswith(prefix + "-")) + 1
        return f"{prefix}-{n}"

    @property
    def mode(self) -> Optional[str]:
        return self.state.get("mode")

    def has_ui(self) -> bool:
        return bool((self.state.get("decision") or {}).get("has_ui", True))

    def ensure_rail(self):
        rail = build_rail(self.state["inputs"], self.state.get("mode"))
        with self.lock:
            self.state["rail"] = rail
            for e in rail:
                self.state["stages"].setdefault(e["key"], {"state": "pending", "label": e["label"], "bot": e["bot"]})
            self.save()

    def fix_round(self, key: str, value: Optional[int] = None) -> int:
        """Fix rounds survive a restart, so resuming a run never gives the fix loops a fresh set of rounds."""
        with self.lock:
            if value is not None:
                self.state[key] = value
                self.save()
            return self.state.get(key, 0)

    def reset_stages(self, keys):
        with self.lock:
            for k in keys:
                if k in self.state["stages"]:
                    self.state["stages"][k].update(state="pending", detail=None, started_at=None, completed_at=None)
            self.save()

    def commit_app(self, msg: str):
        git(self.app, "add", "-A")
        rc, out = git(self.app, "status", "--porcelain")
        if out.strip():
            git(self.app, "commit", "-m", msg)

    def valid_app_json(self, check_files: bool = True) -> Optional[str]:
        data, errs = checks.read_app_json(self.fdir / "app.json", self.app) if check_files else self._read_app_json_loose()
        return "; ".join(errs) if errs else None

    def _read_app_json_loose(self):
        try:
            data = json.loads((self.fdir / "app.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return None, [f"factory/app.json is missing or not valid JSON: {e}"]
        return data, checks.validate_app_json(data, self.app, check_files=False)

    def screenshot_names(self, rel_dir: str, limit: int = 8) -> list[str]:
        d = self.fdir / rel_dir
        if not d.is_dir():
            return []
        names = sorted(p.name for p in d.glob("*.png") if not p.name.endswith("-dark.png"))
        return [f"{rel_dir}/{n}" for n in names[:limit]]

    # ------------------------------------------------------------------ what every agent is given on top of its role
    def system_extras(self, role_name: str) -> str:
        parts = []
        always = checks.load_always(self.rd / "inputs" / "always-use")
        if always:
            parts.append("## Always-use documents (the owner's standing rules: they apply to everything)\n\n" +
                         "\n\n".join(f"### {n}\n{t}" for n, t in always)[:20000])
        sides = ROLE_SIDES.get(role_name, set())
        blocks, used, seen = [], 0, set()
        for it in (self.state.get("skills") or {}).get("selected", []):
            if not (sides & set(it.get("for", []))) or it["name"] in seen:
                continue
            f = self.rd / "inputs" / "skills" / it["name"] / "SKILL.md"
            if not f.exists():
                continue
            text = re.sub(r"\A---.*?---\s*", "", f.read_text(encoding="utf-8", errors="replace"), count=1, flags=re.S)[:14000]
            if used + len(text) > SKILL_CAP_TOTAL:
                break
            used += len(text)
            seen.add(it["name"])
            blocks.append(f"### Skill: {it['name']}\n(Picked by the skill agent because: {it.get('why', '')}. Its other files are in inputs/skills/{it['name']}/.)\n{text}")
        if blocks:
            parts.append("## Skills to apply (the skill agent judged these useful for this project: follow them where they fit the task)\n\n" + "\n\n".join(blocks))
        return ("\n\n---\n\n" + "\n\n---\n\n".join(parts)) if parts else ""

    # ------------------------------------------------------------------ generate ideas -> choose / random
    def stage_ideas(self):
        inp = self.state["inputs"]
        if not inp.get("generate") or self.state.get("chosen_idea"):
            return
        path = self.fdir / "ideas.json"
        if self.stage_state("ideas") != "done" or not path.exists():
            self.stage("ideas", "active")
            self.add_card("ideas", "Generate ideas", "sf-ideator", "stage", "ideas")
            theme = (inp.get("idea") or "").strip()
            body = "Generate 5 ideas and write factory/ideas.json."
            if theme:
                body = f"THEME from the owner (every idea must fit it): {theme}\n" + body

            def check():
                try:
                    errs = checks.validate_ideas(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError) as e:
                    return f"ideas.json is missing or not valid JSON: {e}"
                return "; ".join(errs) if errs else None
            self.run_agent_stage("ideas", "sf-ideator", self.prompt("ideas", "Generate ideas", body), [path], check)
            self.card_done("ideas", "ideas ready")
            self.stage("ideas", "done")
        options = json.loads(path.read_text(encoding="utf-8"))["ideas"]
        dec, _ = self.gate("ideas", "gate-ideas", "pick", None, title="Choose an idea",
                           blurb="Pick one of the ideas, or press Random pick and the factory chooses one for you.",
                           accept=("choose", "random"), options=options)
        d = self.last_decision
        if dec == "random":
            idx, how = random.randrange(len(options)), "random"
        else:
            try:
                idx = max(0, min(len(options) - 1, int(d.get("index", 0))))
            except (TypeError, ValueError):
                idx = 0
            how = "chosen"
        with self.lock:
            self.state["chosen_idea"] = {**options[idx], "how": how, "index": idx}
            self.save()
        self.event("idea_chosen", "gate-ideas", f"{how}: {options[idx]['title']}")

    # ------------------------------------------------------------------ reverse engineer a feature or app (public information only)
    def stage_reverse(self):
        rev = self.state["inputs"].get("reverse")
        if not rev or self.stage_state("reverse") == "done":
            return
        self.stage("reverse", "active")
        self.add_card("reverse", f"Reverse engineer: {rev['target'][:40]}", "sf-reverse", "stage", "reverse")
        out = self.fdir / "reverse.md"
        lines = [f"TARGET ({'a whole app or platform' if rev['scope'] == 'app' else 'a feature'}): {rev['target']}"]
        if rev.get("links"):
            lines.append("LINKS the owner gave to learn from:\n" + "\n".join(f"- {u}" for u in rev["links"]))
        if self.state["inputs"].get("photos"):
            lines.append("The owner's screenshots are in inputs/photos/ (open them with Read).")
        lines.append("Write factory/reverse.md.")

        def check():
            text = out.read_text(encoding="utf-8")
            if "## Feature breakdown" not in text:
                return "reverse.md needs a '## Feature breakdown' section"
            if "NO PUBLIC INFORMATION FOUND" in text or re.search(r"https?://", text):
                return None
            return "reverse.md must cite its sources as full https:// URLs, or say NO PUBLIC INFORMATION FOUND"
        self.run_agent_stage("reverse", "sf-reverse", self.prompt("reverse", "Reverse engineer", "\n\n".join(lines)), [out], check)
        self.card_done("reverse", "analysis written")
        self.stage("reverse", "done")

    # ------------------------------------------------------------------ intake -> brief.md
    def stage_intake(self):
        if self.stage_state("intake") == "done":
            return
        inp = self.state["inputs"]
        self.stage("intake", "active")
        chosen = self.state.get("chosen_idea")
        idea = (inp.get("idea") or "").strip()
        rev = inp.get("reverse")
        spec_in = self.rd / "inputs" / "spec.md"
        has_spec = spec_in.exists()
        if rev and not chosen and not idea:
            what = "a whole app" if rev["scope"] == "app" else "a feature"
            title = f"{rev['target'][:48]} (own version)"
            idea_md = (f"# {title}\n\nA clean-room, original implementation of {what} from another product: {rev['target']}. "
                       "Same behaviour, our own name, look and code. The analysis is in factory/reverse.md.\n")
        elif chosen:
            title = chosen["title"]
            idea_md = f"# {title}: {chosen['pitch']}\n\n{chosen.get('why', '')}\n" + (f"\nThe owner's theme: {idea}\n" if idea else "")
        elif idea:
            title = idea.splitlines()[0][:60]
            idea_md = f"# {title}\n\n{idea}\n" + (f"\nAlso recreate (clean-room, see factory/reverse.md): {rev['target']}\n" if rev else "")
        else:
            first = next((l.lstrip("# ").strip() for l in spec_in.read_text(encoding="utf-8").splitlines() if l.strip()), "App")
            title = first[:60]
            idea_md = f"# {title}\n\nThe owner supplied a whole spec: see inputs/spec.md.\n"
        (self.fdir / "idea.md").write_text(idea_md, encoding="utf-8")
        if has_spec:
            shutil.copyfile(spec_in, self.fdir / "spec.md")
        with self.lock:
            self.state["title"] = title
            self.state["spec_given"] = has_spec
        for name in self.state.get("missing_skills") or []:
            self.event("skill_missing", None, f"skill not found: {name}")

        brief = self.fdir / "brief.md"
        owner_text = checks.context_markdown(inp.get("app_context"), inp.get("first_open"))  # the owner's own words: they go into the brief unchanged
        needs_agent = bool(inp.get("product_links") or inp.get("ref_links") or inp.get("photos") or has_spec)
        self.add_card("intake", "Intake: build the brief", "sf-intake" if needs_agent else None, "stage", "intake")
        if needs_agent:
            lines = ["Read the owner's inputs and write factory/brief.md."]
            if inp.get("product_links"):
                lines.append("PRODUCT LINKS (products like the one they want):\n" + "\n".join(f"- {u}" for u in inp["product_links"]))
            if inp.get("ref_links"):
                lines.append("REFERENCE LINKS:\n" + "\n".join(f"- {u}" for u in inp["ref_links"]))
            if inp.get("photos"):
                lines.append("REFERENCE PHOTOS (open each with Read and look at it):\n" + "\n".join(f"- inputs/photos/{n}" for n in inp["photos"]))
            if has_spec:
                lines.append("The owner supplied a whole spec: inputs/spec.md (also copied to factory/spec.md).")
            if owner_text:
                lines.append("THE OWNER WROTE THESE SECTIONS THEMSELVES (also in inputs/context.md). Copy each one into brief.md word for word "
                             "under the same heading; never shorten or reword it. The first-open experience is a hard requirement.\n\n" + owner_text)
            if inp.get("skills"):
                lines.append("SKILLS the owner asked for: " + ", ".join(inp["skills"]))
            if rev:
                lines.append("factory/reverse.md holds a reverse-engineering analysis of " + rev["target"] + ". Fold its key points into the brief under 'Analysis of the target' and keep the clean-room rule: same behaviour, our own name, look and code.")

            def check():
                text = brief.read_text(encoding="utf-8")
                if "## Summary" not in text:
                    return "brief.md needs a '## Summary' section"
                if inp.get("first_open") and "## First-open experience" not in text:
                    return "brief.md needs the owner's '## First-open experience' section, copied word for word"
                return None
            self.run_agent_stage("intake", "sf-intake", self.prompt("intake", "Intake", "\n\n".join(lines)), [brief], check)
        else:
            always = [n for n, _ in checks.load_always(self.rd / "inputs" / "always-use")]
            summary = chosen["pitch"] if chosen else (idea or (f"Build an original implementation of {'the whole app' if rev['scope'] == 'app' else 'the feature'}: {rev['target']}." if rev else ""))
            analysis = ""
            if rev and (self.fdir / "reverse.md").exists():
                analysis = "## Analysis of the target (reverse engineering; clean-room: same behaviour, our own name, look and code)\n" + \
                           (self.fdir / "reverse.md").read_text(encoding="utf-8").split("\n", 1)[-1].strip() + "\n\n"
            brief.write_text(
                f"# Brief: {title}\n\n## Summary\n{summary}\n\n## From the owner\n{idea or summary}\n\n{analysis}"
                "## Product references\nnone given\n\n## Design references\nnone given: follow the always-use documents and make it look like a polished, designed product\n\n"
                f"{owner_text + chr(10) if owner_text else ''}"
                f"## Constraints\n{('Always-use documents: ' + ', '.join(always)) if always else 'none'}\n\n"
                f"## Skills to apply\n{', '.join(inp.get('skills') or []) or 'none'}\n\n## Assumptions and open questions\nnone\n", encoding="utf-8")
            self.card_start("intake")
        self.card_done("intake", f"brief ready ({'agent' if needs_agent else 'built directly: nothing to read'})")
        self.stage("intake", "done")

    # ------------------------------------------------------------------ decider (you can override)
    def stage_decide(self):
        if self.state.get("mode") and self.stage_state("decide") == "done":
            return
        self.stage("decide", "active")
        inp = self.state["inputs"]
        forced = inp.get("force_mode") if inp.get("force_mode") in ("single", "multi") else None
        out = self.fdir / "decision.json"
        self.add_card("decide", "Decide: single agent or full pipeline", "sf-decider", "stage", "decide")

        def check():
            try:
                d = json.loads(out.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                return f"decision.json is missing or not valid JSON: {e}"
            if d.get("mode") not in ("single", "multi") or d.get("size") not in ("small", "medium", "large") or not isinstance(d.get("has_ui"), bool):
                return "decision.json needs mode (single|multi), size (small|medium|large) and has_ui (true|false)"
            return None
        self.run_agent_stage("decide", "sf-decider", self.prompt("decide", "Decide", "Read factory/brief.md and write factory/decision.json."), [out], check)
        d = json.loads(out.read_text(encoding="utf-8"))
        d["recommended"] = d["mode"]
        if forced:
            names = {"single": "the single agent", "multi": "the full pipeline"}
            note = f"You chose {names[forced]} when you started the run."
            if forced != d["mode"]:
                note += f" The decider would have recommended {names[d['mode']]}: {d.get('reason', '')}"
            d["mode"], d["reason"] = forced, note
        quality = (self.state.get("inputs") or {}).get("quality") or "balanced"
        d["estimates"] = {m: checks.estimate(m, d["size"], quality) for m in ("single", "multi")}
        with self.lock:
            self.state["decision"] = d
            self.save()
        self.card_done("decide", f"{d['mode']}: {d.get('reason', '')}"[:200])
        wait = 0 if forced else self.decider_wait
        if wait > 0:
            other = "multi" if d["mode"] == "single" else "single"
            names = {"single": "the single agent", "multi": "the full pipeline"}
            est = lambda m: f"${d['estimates'][m]['usd'][0]}–{d['estimates'][m]['usd'][1]}, about {d['estimates'][m]['minutes'][0]}–{d['estimates'][m]['minutes'][1]} min"
            blurb = (f"{d.get('reason', '')} Recommended: {names[d['mode']]} ({est(d['mode'])}). "
                     f"The other option is {names[other]} ({est(other)}). It continues on its own unless you switch.")
            dec, _ = self.gate("mode", "gate-mode", "decide", None, title=f"Recommended: {names[d['mode']]}", blurb=blurb,
                               accept=("approve", "switch"), auto_seconds=wait, extra={"decision": d, "other": other},
                               restore="active")
            if dec == "switch":
                d["mode"], d["overridden"] = other, True
        with self.lock:
            self.state["mode"] = d["mode"]
            self.state["decision"] = d
            self.save()
        self.stage("decide", "done", d["mode"])
        self.ensure_rail()
        self.event("mode", "decide", f"{d['mode']}{' (you switched it)' if d.get('overridden') else ''}")

    # ------------------------------------------------------------------ the skill agent (both paths)
    def stage_skills(self, side: str, key: str):
        """Looks at every skill the environment offers (installed ones and the ones you dropped into the dashboard) and keeps only
        the ones this project can really use. Skills that don't fit are not used."""
        if self.stage_state(key) == "done":
            return
        if side == "design" and not self.has_ui():
            self.stage(key, "done", "no screens")
            return
        self.stage(key, "active")
        catalog = checks.skill_catalog()
        allowed = SKILL_SIDES[side]
        out = self.fdir / f"skill-choice-{side}.json"  # not inside a folder called "skills": agents are not allowed to write there
        if not catalog:
            out.write_text(json.dumps({"selected": [], "rejected": []}), encoding="utf-8")
            self._apply_skills(side, {"selected": [], "rejected": []}, catalog, allowed)
            self.stage(key, "done", "no skills available")
            return
        hints = list(self.state["inputs"].get("skills") or [])
        lines = ["# Skills available", ""] + [f"- **{c['name']}** ({c['source']}): {c['description']}" + (" [the owner asked for this one]" if c["name"] in hints else "") for c in catalog]
        (self.fdir / "skills-catalog.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        cdir = self.rd / "inputs" / "skill-candidates"
        cdir.mkdir(parents=True, exist_ok=True)
        for c in catalog:
            f = cdir / f"{c['name']}.md"
            if not f.exists():
                f.write_text(Path(c["path"]).read_text(encoding="utf-8", errors="replace")[:40000], encoding="utf-8")
        names = {c["name"] for c in catalog}
        cid = f"skills-{side}"
        self.add_card(cid, {"all": "Skill check", "design": "Skill check: design side", "technical": "Skill check: technical side"}[side],
                      "sf-skills", "stage", key)

        def check():
            try:
                d = json.loads(out.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                return f"{out.name} is missing or not valid JSON: {e}"
            sel, rej = d.get("selected"), d.get("rejected", [])
            if not isinstance(sel, list) or not isinstance(rej, list):
                return "'selected' and 'rejected' must be lists"
            if len(sel) > MAX_SKILLS_PER_SIDE:
                return f"select at most {MAX_SKILLS_PER_SIDE} skills: pick only the ones that clearly help"
            for it in sel:
                if not isinstance(it, dict) or it.get("name") not in names or not str(it.get("why", "")).strip():
                    return "every selected skill needs a name from the catalog and a 'why'"
            return None
        body = (f"SIDE: {side} (you choose skills for: {', '.join(allowed)}).\nRead factory/brief.md" + (", factory/spec.md" if (self.fdir / "spec.md").exists() else "") +
                (", and the approved mock in factory/mock/" if (self.fdir / "mock").exists() else "") + ", then factory/skills-catalog.md. "
                "Candidate skills are in inputs/skill-candidates/<name>.md. Write your decision to the file " + posix(out) + " (use exactly this full path).")
        if hints:
            body += "\nThe owner asked for: " + ", ".join(hints) + " (these are candidates, not orders: use one only if it genuinely helps THIS project, otherwise reject it and say why)."
        self.run_agent_stage(cid, "sf-skills", self.prompt(cid, "Skill check", body), [out], check)
        n = self._apply_skills(side, json.loads(out.read_text(encoding="utf-8")), catalog, allowed)
        self.card_done(cid, f"{n} of {len(catalog)} skills selected")
        self.stage(key, "done", f"{n} selected" if n else "none needed")

    def _apply_skills(self, side: str, data: dict, catalog: list, allowed: list) -> int:
        by = {c["name"]: c for c in catalog}
        selected = []
        for it in data.get("selected", [])[:MAX_SKILLS_PER_SIDE]:
            name = it["name"]
            fors = [f for f in (it.get("for") or allowed) if f in allowed] or list(allowed)
            selected.append({"name": name, "for": fors, "why": str(it.get("why", ""))[:300], "source": by[name]["source"], "side": side})
            src, dst = Path(by[name]["path"]).parent, self.rd / "inputs" / "skills" / name
            shutil.rmtree(dst, ignore_errors=True)
            try:
                if sum(f.stat().st_size for f in src.rglob("*") if f.is_file()) > 6_000_000:
                    raise OSError("too large: copy only SKILL.md")
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"))
            except OSError:
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src / "SKILL.md", dst / "SKILL.md")
        rejected = [{"name": str(r.get("name", "")), "why": str(r.get("why", ""))[:300], "side": side}
                    for r in data.get("rejected", []) if isinstance(r, dict)][:60]
        with self.lock:
            sk = self.state.setdefault("skills", {"selected": [], "rejected": []})
            sk["selected"] = [x for x in sk["selected"] if x["side"] != side] + selected
            sk["rejected"] = [x for x in sk["rejected"] if x["side"] != side] + rejected
            self.save()
        self.write_skills_doc()
        for it in selected:
            self.event("skill_used", f"skills-{side}", f"{it['name']}: {it['why'][:100]}")
        return len(selected)

    def write_skills_doc(self):
        sk = self.state.get("skills") or {"selected": [], "rejected": []}
        used = {x["name"] for x in sk["selected"]}
        lines = ["# Skills", "", "The skill agent looked at every skill available in this environment (installed ones and the ones you added in the dashboard) "
                 "and kept only the ones that fit this project.", "", "## Used"]
        lines += [f"- **{x['name']}** ({x['source']}, for {', '.join(x['for'])}): {x['why']}" for x in sk["selected"]] or ["- none: nothing available fit this project"]
        rej = [x for x in sk["rejected"] if x["name"] not in used]
        if rej:
            lines += ["", "## Looked at and not used"] + [f"- {x['name']}: {x['why']}" for x in rej[:40]]
        (self.fdir / "skills.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------ automatic checks (driver, no agent)
    def autocheck(self) -> dict:
        cid = self.uid("autocheck")
        self.add_card(cid, "Auto checks", None, "check", "autocheck")
        self.card_start(cid, "checking")
        self.stage("autocheck", "active")
        cdir = self.fdir / "checks"
        cdir.mkdir(exist_ok=True)
        problems: list[str] = []
        result: dict = {"tests": None, "smoke": None, "hidden": None, "screens": None, "secrets": [], "external": [], "deps": None}
        app_json, errs = checks.read_app_json(self.fdir / "app.json", self.app)
        if errs:
            problems += [f"app.json: {e}" for e in errs]
        else:
            if app_json.get("test"):
                cmd = split_cmd(app_json["test"])
                if app_json["type"] == "godot" and cmd and cmd[0] in kinds.GODOT_NAMES and kinds.find_godot():
                    cmd[0] = kinds.find_godot()  # Godot may live outside the PATH (GODOT_BIN)
                rc, out = sh(cmd, self.app, 900)
                result["tests"] = {"rc": rc, "tail": out[-800:]}
                if rc != 0:
                    problems.append("the unit tests fail:\n" + out[-600:])
            if app_json.get("smoke_test"):
                rc, out = checks.run_smoke_test(self.app, app_json["smoke_test"])
                result["smoke"] = {"rc": rc, "tail": out[-800:]}
                if rc != 0:
                    problems.append("the browser smoke test fails:\n" + out[-600:])
            shots = cdir / "shots"
            shutil.rmtree(shots, ignore_errors=True)
            kind_res, kind_probs = checks.kind_checks(self.app, app_json, shots, self.rd / "logs")
            if kind_res is not None:  # api, worker, library, godot, multi: the driver's own checks for that kind
                result["kind"] = kind_res
                problems += kind_probs
            if app_json["type"] == "multi":
                res = (kind_res or {}).get("screens") or {"ok": True, "screens": []}
            else:
                res, extra = checks.take_screens(self.app, app_json, shots)
                problems += extra
            if not res.get("note"):  # kinds without screens (cli, api, worker, library, godot) say so in a note
                result["screens"] = res
                problems += checks.screen_problems(res)
                if res.get("ok"):
                    problems += [f"screens '{a}' and '{b}' look identical at {w}px: the app ignores what each screen path asks for "
                                 "(for example the ?demo=... parameter), so that screen cannot be shown or checked" for a, b, w in checks.duplicate_screens(shots)]
            hidden = self.rd / "holdout" / "test_acceptance.py"
            if self.mode == "multi" and hidden.exists():
                rc, out = sh(self.holdout_cmd_for(), self.rd, 900, env={**os.environ, "FACTORY_APP_DIR": str(self.app)})
                result["hidden"] = {"rc": rc, "tail": out[-600:]}
                if rc != 0:
                    bad = failing_acs(out)
                    spec_text = (self.fdir / "spec.md").read_text(encoding="utf-8") if (self.fdir / "spec.md").exists() else ""
                    problems += ([f"hidden acceptance test for {a} fails ({t}). The spec says: \"{spec_ac_text(spec_text, a) or 'see the acceptance criterion in factory/spec.md'}\". "
                                  "Use the app as a user would, find where it does not behave that way, and fix the cause." for a, t in bad]
                                 or ["the hidden acceptance tests fail:\n" + out[-400:]])
        result["secrets"] = checks.scan_secrets(self.app)
        problems += [f"secret in the code: {s['kind']} at {s['file']}:{s['line']}" for s in result["secrets"]]
        env_vars, env_errs = keys.read_env_json(self.fdir / "env.json")  # outside services: each needs a fake mode and a placeholder
        result["env"] = [v["name"] for v in env_vars]
        problems += env_errs + keys.example_problems(self.app, result["env"])
        problems += [f"a key from the owner's key store ({x['name']}) was copied into app/{x['file']}: read it from the environment instead"
                     for x in keys.leaked(self.app, checks.iter_text_files)]
        result["external"] = checks.scan_external_assets(self.app)
        result["deps"] = checks.dependency_audit(self.app)
        result["ok"], result["problems"] = not problems, problems
        (cdir / "autocheck.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        self.card_done(cid, "all checks pass" if not problems else f"{len(problems)} problem(s): {problems[0][:100]}")
        self.event("autocheck", cid, "pass" if not problems else f"{len(problems)} problem(s)")
        return result

    # ------------------------------------------------------------------ security checker (both paths)
    def run_security(self, out: Path, card_id: str, stage_key: str) -> dict:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        self.add_card(card_id, "Security check", "sf-security", "stage", stage_key)
        rel = posix(out.relative_to(self.rd))
        body = f"Security-review the app in app/. The automatic scans are in factory/checks/autocheck.json. Write your result to {rel}."
        self.run_agent_stage(card_id, "sf-security", self.prompt(card_id, "Security check", body), [out], lambda: review_json_problem(out))
        data = json.loads(out.read_text(encoding="utf-8"))
        data["findings"] = [dict(f, source="security") for f in data.get("findings", [])]
        blocking = [f for f in data["findings"] if f.get("blocking")]
        data["verdict"] = "fail" if blocking else "pass"
        out.write_text(json.dumps(data, indent=1), encoding="utf-8")
        self.card_done(card_id, f"{data['verdict']}: {len(data['findings'])} finding(s), {len(blocking)} blocking")
        return data

    # ------------------------------------------------------------------ single-agent path
    def run_single(self) -> str:
        self.ensure_rail()
        self.stage_skills("all", "skills")
        while True:
            n = self.state.get("round_no", 0)
            if self.stage_state("maker") != "done":
                self.stage_maker(self.state.get("single_feedback"), n)
            if self.stage_state("security") != "done":
                if self.verify_single() == "escalate":
                    self.escalate()
                    return "escalated"
            self.stage_report()
            dec, feedback = self.review_gate(("approve", "improve", "escalate"), n)
            if dec == "approve":
                return "done"
            if dec == "escalate":
                self.escalate()
                return "escalated"
            with self.lock:
                self.state["round_no"] = n + 1
                self.state["single_feedback"] = feedback
                self.save()
            self.reset_stages(["maker", "autocheck", "security", "report", "review"])

    def stage_maker(self, feedback: Optional[str], n: int):
        self.init_app()
        cid = "maker" if n == 0 else f"maker-r{n}"
        self.stage("maker", "active")
        self.add_card(cid, "Build the app" if n == 0 else f"Improve the app (round {n})", "sf-maker", "stage", "maker")
        d = self.state.get("decision") or {}
        if feedback:
            body = (f"The app in app/ is finished and the owner reviewed it. OWNER FEEDBACK: fix every point in the existing app, keep "
                    f"everything else working, keep factory/app.json and your tests correct, commit.\n{feedback}")
        else:
            body = (f"Build the whole app in app/ following your role instructions. has_ui={d.get('has_ui', True)}, size={d.get('size', 'medium')}. "
                    "Read factory/brief.md first." + (" The owner's whole spec is factory/spec.md: follow it exactly." if self.state.get("spec_given") else ""))
        self.run_agent_stage(cid, "sf-maker", self.prompt(cid, "Maker", body), [self.fdir / "app.json"], self._maker_check)
        self.commit_app(f"{cid}: build")
        self.card_done(cid, "app built")
        self.critique(first_build=(n == 0 and not feedback))
        self.stage("maker", "done")

    def _maker_check(self) -> Optional[str]:
        content = [p for p in self.app.iterdir() if p.name not in (".git", ".gitignore")] if self.app.exists() else []
        if not content:
            return "app/ is empty: build the app there"
        return self.valid_app_json()

    def critique(self, first_build: bool):
        """The screenshot self-critique loop: the driver photographs the app, the maker looks and fixes."""
        app_json, errs = checks.read_app_json(self.fdir / "app.json", self.app)
        if errs or app_json["type"] == "cli" or not self.has_ui():
            return
        for k in range(1, (MAX_CRITIQUE_ROUNDS if first_build else 1) + 1):
            d = self.fdir / "checks" / "critique" / str(k)
            shutil.rmtree(d, ignore_errors=True)
            res, _ = checks.take_screens(self.app, app_json, d)
            if not res.get("ok"):
                return  # the auto checks will report why
            imgs = sorted(d.glob("*.png"))
            if not imgs:
                return
            rid = self.uid("maker-refine")
            self.add_card(rid, f"Screenshot critique {k}", "sf-maker", "stage", "maker")
            body = ("Here are real browser screenshots of your app (phone 390px, desktop 1280px, and one dark-mode shot):\n" +
                    "\n".join(f"- {posix(p)}" for p in imgs) + "\nOpen every one with the Read tool and LOOK at them. Critique like a demanding product "
                    "designer, then fix the 6 most important problems in the app. Keep all features and tests working, keep factory/app.json correct, commit.")
            self.run_agent_stage(rid, "sf-maker", self.prompt(rid, "Critique", body), [self.fdir / "app.json"], self._maker_check)
            self.commit_app(f"{rid}: design critique fixes")
            self.card_done(rid, "refined after looking at screenshots")

    def maker_fix(self, items: list[str], why: str):
        cid = self.uid("maker-fix")
        self.add_card(cid, f"Fix: {why}", "sf-maker", "stage", "maker")
        body = (f"FEEDBACK ({why}). Fix every point in the existing app, don't start over. Re-run your tests, keep factory/app.json correct, commit.\n" +
                "\n".join(f"{i}. {t}" for i, t in enumerate(items, 1)))
        self.run_agent_stage(cid, "sf-maker", self.prompt(cid, "Fix", body), [self.fdir / "app.json"], self._maker_check, force_escalate=True)
        self.commit_app(f"{cid}: {why}")
        self.card_done(cid, f"{len(items)} problem(s) fixed")

    def verify_single(self) -> str:
        sec_path = self.fdir / "security.json"
        for rnd in range(self.fix_round("verify_round_single"), MAX_FIX_ROUNDS + 1):
            res = self.autocheck()
            if not res["ok"]:
                if rnd == MAX_FIX_ROUNDS:
                    self.event("escalate", "autocheck", "checks still failing after the fix rounds: escalating to the full pipeline")
                    self.fix_round("verify_round_single", 0)
                    return "escalate"
                self.maker_fix(res["problems"], "automatic checks failed")
                self.fix_round("verify_round_single", rnd + 1)
                continue
            self.stage("autocheck", "done")
            self.stage("security", "active")
            sec = self.run_security(sec_path, self.uid("security"), "security")
            blocking = [f for f in sec["findings"] if f.get("blocking")]
            if not blocking:
                self.stage("security", "done", "pass")
                self.fix_round("verify_round_single", 0)
                return "ok"
            if rnd == MAX_FIX_ROUNDS:
                dec, _ = self.gate("help", self.uid("gate-help-security"), "security", None, title="Security problems remain",
                                   blurb="The security checker still blocks this app after the fix rounds: " + " | ".join(findings_text(blocking))[:600] +
                                         " Approve to ship it anyway (the report will list them) or reject to stop.",
                                   accept=("approve", "reject"), restore="active")
                if dec == "reject":
                    raise StageFailed("security problems remain; stopped by the owner")
                with self.lock:
                    self.state["open_issues"] = blocking
                self.stage("security", "done", "shipped with warnings")
                self.fix_round("verify_round_single", 0)
                return "ok"
            self.maker_fix(findings_text(blocking), "security findings")
            self.fix_round("verify_round_single", rnd + 1)
        return "ok"

    # ------------------------------------------------------------------ report + your review (both paths)
    def stage_report(self):
        if self.stage_state("report") == "done":
            return
        self.stage("report", "active")
        cid = self.uid("report")
        self.add_card(cid, "Report", "sf-writer", "stage", "report")
        sdir = self.fdir / "report" / "shots"
        shutil.rmtree(sdir, ignore_errors=True)
        sdir.mkdir(parents=True)
        src = self.fdir / "checks" / "shots"
        copied = []
        if src.is_dir():
            for p in sorted(src.glob("*.png")):
                shutil.copyfile(p, sdir / p.name)
                copied.append(p.name)
        report = self.fdir / "report.md"
        report.unlink(missing_ok=True)

        def check():
            text = report.read_text(encoding="utf-8")
            if copied and "report/shots/" not in text:
                return "report.md must show the screenshots as images, e.g. ![Home](report/shots/home-390.png)"
            return None
        self.run_agent_stage(cid, "sf-writer", self.prompt(cid, "Report", "Write factory/report.md (and app/README.md if missing). Screenshots: " + (", ".join(copied) or "none (command-line app)")),
                             [report, self.app / "README.md"], check)
        self.commit_app("Add README")
        cost = self.state.get("cost_usd", 0)
        issues = self.state.get("open_issues") or []
        with report.open("a", encoding="utf-8") as fh:
            fh.write(f"\n\n---\n_Built by the {'single agent' if self.mode == 'single' else 'full pipeline'} · cost so far ${cost:.2f}_\n")
            if issues:
                fh.write("\n**Open issues the checks flagged and nobody fixed:**\n" + "\n".join(f"- {t}" for t in findings_text(issues)) + "\n")
        self.card_done(cid, "report written")
        self.stage("report", "done")

    def review_gate(self, accept: tuple, n: int) -> tuple[str, str]:
        gid = self.uid("gate-review")  # a fresh id every time, so a repeated review (or one after escalating) is a new gate
        images = [f"report/shots/{p.name}" for p in sorted((self.fdir / "report" / "shots").glob("*.png")) if not p.name.endswith("-dark.png")][:6]
        if self.mode == "single":
            blurb = ("Open the app and look at the report. Approve if it's good. Improve sends your notes back to the same agent. "
                     "Too big for one agent? Escalate to the full pipeline: the draft becomes its design reference.")
        else:
            blurb = ("Open the app and look at the report. Approve if it's good. A small tweak goes straight to a builder; "
                     "a big change goes back to the planner.")
        return self.gate("review", gid, "review", "report.md", title="Review the app", blurb=blurb, accept=accept, images=images)

    def escalate(self):
        """Single agent -> full pipeline. The draft is kept as the design reference."""
        ref = self.rd / "reference"
        shutil.rmtree(ref, ignore_errors=True)
        ref.mkdir(parents=True)
        if self.app.exists():
            shutil.move(str(self.app), str(ref / "prototype"))
        shots = self.fdir / "checks" / "shots"
        if shots.is_dir():
            shutil.copytree(shots, ref / "shots", dirs_exist_ok=True)
        with (self.fdir / "brief.md").open("a", encoding="utf-8") as fh:
            fh.write("\n\n## Prototype (from the single-agent attempt)\nA draft exists in reference/prototype/ with screenshots in reference/shots/. "
                     "Use its look, layout and flows as the design reference; the full pipeline rebuilds it properly.\n")
        with self.lock:
            self.state["mode"] = "multi"
            self.state["escalated"] = True
            self.state["round_no"] = 0
            self.state["single_feedback"] = None
            self.state["open_issues"] = []
            self.save()
        self.reset_stages(["skills", "maker", "autocheck", "security", "report", "review"])
        with self.lock:
            if self.state.get("skills"):
                self.state["skills"]["selected"] = [x for x in self.state["skills"]["selected"] if x["side"] != "all"]
                self.state["skills"]["rejected"] = [x for x in self.state["skills"]["rejected"] if x["side"] != "all"]
            self.save()
        self.ensure_rail()
        self.event("escalated", None, "now running the full pipeline; the single-agent draft is the design reference")

    # ------------------------------------------------------------------ full pipeline
    def run_multi(self) -> str:
        self.ensure_rail()
        self.stage_research()
        self.stage_spec_gate()
        while True:
            n = self.state.get("round_no", 0)
            cards = self.plan_phase()
            if self.stage_state("build") != "done":
                self.run_cards(cards)
                self.stage("build", "done")
            if self.stage_state("reviews") != "done":
                self.verify_multi()
            self.stage_report()
            dec, feedback = self.review_gate(("approve", "tweak", "big"), n)
            if dec == "approve":
                return "done"
            with self.lock:
                self.state["round_no"] = n + 1
                self.save()
            if dec == "tweak":
                self.tweak(feedback, n + 1)
                self.reset_stages(["report", "review"])
            else:
                with self.lock:
                    self.state["rounds"] += 1
                    self.state["feedback"] = feedback
                    self.state["open_issues"] = []
                    self.save()
                self.event("rework", None, f"big change: rework round {self.state['rounds']}")
                self.reset_stages(["plan", "integrate", "build", "autocheck", "reviews", "report", "review"])

    def stage_spec_gate(self):
        feedback, n = None, 1
        while True:
            if self.stage_state("spec") != "done":
                self.stage("spec", "active")
                cid = "spec" if n == 1 else f"spec-r{n}"
                self.add_card(cid, "Spec" if n == 1 else f"Spec (revision {n})", "sf-spec", "stage", "spec")
                out = self.fdir / "spec.md"
                if feedback:
                    body = f"OWNER FEEDBACK on the previous spec (revise factory/spec.md to address every point):\n{feedback}"
                elif self.state.get("spec_given"):
                    body = "CHECK mode: the owner supplied a whole spec, already at factory/spec.md. Do not rewrite it; follow your CHECK-mode instructions."
                else:
                    body = "WRITE mode: write factory/spec.md from factory/brief.md (and factory/research.md if present)."
                self.run_agent_stage(cid, "sf-spec", self.prompt(cid, "Spec", body), [out],
                                     lambda: None if spec_acs(out.read_text(encoding="utf-8")) else "spec.md has no acceptance criteria numbered AC-1, AC-2, ...")
                self.card_done(cid, f"{len(spec_acs(out.read_text(encoding='utf-8')))} acceptance criteria")
                self.stage("spec", "done")
            self.stage_mock(feedback if n > 1 else None)
            if self.stage_state("gate_spec") == "done":
                return
            dec, fb = self.gate("spec", "gate-spec" if n == 1 else f"gate-spec-r{n}", "gate_spec", "spec.md",
                                images=self.screenshot_names("mock/shots", 6), accept=("approve", "reject"))
            if dec == "approve":
                return
            feedback, n = fb, n + 1
            self.reset_stages(["spec", "mock", "gate_spec"])

    def stage_mock(self, feedback: Optional[str]):
        if self.stage_state("mock") == "done":
            return
        if not self.has_ui():
            self.stage("mock", "done", "no screens")
            return
        self.stage("mock", "active")
        mock = self.fdir / "mock"
        shots = mock / "shots"
        proto = self.rd / "reference" / "prototype"
        proto_json = self.fdir / "app.json"
        if proto.exists() and not feedback and proto_json.exists():  # prototype-first: reuse the single-agent draft's look
            try:
                pj = json.loads(proto_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pj = {}
            if pj.get("type") == "static":
                shutil.rmtree(mock, ignore_errors=True)
                shutil.copytree(proto, mock, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".worktrees", "tests"))
                (mock / "app.json").write_text(json.dumps({k: pj[k] for k in ("type", "entry", "screens") if k in pj}), encoding="utf-8")
                res = checks.capture_screens(mock, json.loads((mock / "app.json").read_text(encoding="utf-8")), shots)
                if res.get("ok"):
                    self.add_card("mock", "Visual mock (from the prototype)", None, "stage", "mock")
                    self.card_start("mock")
                    self.card_done("mock", "reused the single-agent prototype")
                    self.stage("mock", "done", "from prototype")
                    return

        cid = self.uid("mock")
        self.add_card(cid, "Visual mock", "sf-mock", "stage", "mock")
        mj = mock / "app.json"

        def check():
            data, errs = None, []
            try:
                data = json.loads(mj.read_text(encoding="utf-8"))
                errs = checks.validate_app_json(data, mock)
            except (OSError, json.JSONDecodeError) as e:
                errs = [f"factory/mock/app.json is missing or not valid JSON: {e}"]
            if errs:
                return "; ".join(errs)
            shutil.rmtree(shots, ignore_errors=True)
            res = checks.capture_screens(mock, data, shots)
            probs = checks.screen_problems(res)
            return ("the mock has problems when opened in a browser: " + "; ".join(probs[:4])) if probs else None
        body = "Build the visual mock of the approved-to-be spec in factory/mock/ (see your role)."
        if feedback:
            body = f"OWNER FEEDBACK on the previous mock and spec (change the mock to match it):\n{feedback}"
        self.run_agent_stage(cid, "sf-mock", self.prompt(cid, "Visual mock", body), [mock / "index.html", mj], check)
        self.card_done(cid, "mock built and photographed")
        self.stage("mock", "done")

    def plan_phase(self) -> list[dict]:
        """Three lanes run at the same time: the design side (skill check, then the design plan), the technical side (skill check,
        then the technical plan) and the hidden tests. The integrator then merges the two plans into one."""
        errs: list = []
        lanes = [lambda: (self.stage_skills("design", "skills_design"), self.stage_design()),
                 lambda: (self.stage_skills("technical", "skills_tech"), self.stage_plan())]
        if self.stage_state("holdout") != "done":
            lanes.append(self.stage_holdout)

        def guarded(fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        threads = [threading.Thread(target=guarded, args=(fn,)) for fn in lanes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errs:
            raise errs[0]
        self.stage_integrate()
        return json.loads((self.fdir / "cards.json").read_text(encoding="utf-8"))["cards"]

    def stage_design(self):
        if self.stage_state("design") == "done":
            return
        if not self.has_ui():
            self.stage("design", "done", "no screens")
            return
        self.stage("design", "active")
        self.add_card("design", "Design plan", "sf-designer", "stage", "design")
        dm, css = self.fdir / "design.md", self.fdir / "design" / "tokens.css"
        css.parent.mkdir(exist_ok=True)
        self.run_agent_stage("design", "sf-designer", self.prompt("design", "Design plan", "Write factory/design.md and factory/design/tokens.css from the approved mock."),
                             [dm, css], lambda: None if ":root" in css.read_text(encoding="utf-8") else "tokens.css must define the design tokens in :root")
        self.card_done("design", "design plan + stylesheet written")
        self.stage("design", "done")

    def stage_integrate(self):
        if self.stage_state("integrate") == "done":
            return
        if not self.has_ui() or self.state["rounds"] > 0:
            self.stage("integrate", "done", "nothing to merge")
            return
        self.stage("integrate", "active")
        self.add_card("integrate", "Final plan (integrator)", "sf-integrator", "stage", "integrate")
        acs = spec_acs((self.fdir / "spec.md").read_text(encoding="utf-8"))
        self.run_agent_stage("integrate", "sf-integrator", self.prompt("integrate", "Final plan", "Merge the design plan and the technical plan into one final plan."),
                             [self.fdir / "plan.md", self.fdir / "cards.json"], lambda: self.plan_problem(acs, 0, "", set()))
        self.card_done("integrate", "one final plan")
        self.stage("integrate", "done")

    def plan_problem(self, acs, r, prefix, existing) -> Optional[str]:
        try:
            data = json.loads((self.fdir / "cards.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return f"cards.json is missing or not valid JSON: {e}"
        try:
            multi_service = json.loads((self.fdir / "app.json").read_text(encoding="utf-8")).get("type") == "multi"
        except (OSError, json.JSONDecodeError, AttributeError):
            multi_service = False
        errs = validate_cards(data, acs, r > 0, prefix, existing, multi_service)
        if r == 0:
            aj = self.valid_app_json(check_files=False)
            if aj:
                errs.append(aj)
        return "\n".join(errs) if errs else None

    def verify_multi(self, light: bool = False):
        """Auto checks, then the reviewers; blocking findings become fix cards (max 2 rounds)."""
        self.stage("reviews", "active")
        prev_sig = None
        for rnd in range(self.fix_round("verify_round"), MAX_FIX_ROUNDS + 1):
            res = self.autocheck()
            if not res["ok"]:
                findings = [{"source": "auto checks", "issue": p, "fix": "make this check pass", "blocking": True} for p in res["problems"]]
            else:
                findings = [f for f in self.run_reviewers(light) if f.get("blocking")]
            if not findings:
                self.stage("autocheck", "done")
                self.stage("reviews", "done", "approved")
                self.fix_round("verify_round", 0)
                return
            sig = sorted(str(f.get("issue") or f)[:120] for f in findings)
            stuck = prev_sig is not None and sig == prev_sig  # the last fix round changed nothing: tell the next one to try something else
            prev_sig = sig
            if rnd == MAX_FIX_ROUNDS:
                if not res["ok"]:  # the reviewers never saw this build: run them once so nothing (security above all) goes unchecked
                    findings += [f for f in self.run_reviewers(light) if f.get("blocking")]
                with self.lock:
                    self.state["open_issues"] = findings
                self.event("open_issues", None, f"{len(findings)} blocking issue(s) left after {MAX_FIX_ROUNDS} fix rounds: listed in the report")
                self.stage("autocheck", "done")
                self.stage("reviews", "done", "open issues")
                self.fix_round("verify_round", 0)
                return
            self.plan_improvements(findings, rnd + 1, stuck)
            self.fix_round("verify_round", rnd + 1)

    def run_reviewers(self, light: bool) -> list[dict]:
        jobs = []
        sec = lambda: self.run_security(self.fdir / "reviews" / "security.json", self.uid("review-security"), "reviews")["findings"]
        jobs.append(sec)
        if not light:
            jobs.append(lambda: self._review("sf-reviewer", "technical", "tech.json", "Technical review"))
            if self.has_ui():
                jobs.append(lambda: self._review("sf-reviewer-ui", "UI", "ui.json", "UI and design review"))
        with ThreadPoolExecutor(len(jobs)) as ex:
            futs = [ex.submit(j) for j in jobs]
            return [f for fut in futs for f in fut.result()]

    def _review(self, role: str, source: str, filename: str, title: str) -> list[dict]:
        out = self.fdir / "reviews" / filename
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        cid = self.uid("review-" + source.lower())
        self.add_card(cid, title, role, "review", "reviews")
        body = f"Review the integrated app in app/. Write your result to {posix(out.relative_to(self.rd))}."
        self.run_agent_stage(cid, role, self.prompt(cid, title, body), [out], lambda: review_json_problem(out))
        data = json.loads(out.read_text(encoding="utf-8"))
        findings = [dict(f, source=source) for f in data.get("findings", [])]
        n_block = sum(1 for f in findings if f.get("blocking"))
        self.card_done(cid, f"{data.get('verdict')}: {len(findings)} finding(s), {n_block} blocking")
        return findings

    def plan_improvements(self, findings: list[dict], rnd: int, stuck: bool = False):
        """The 'plan for improvements' step: blocking findings become a short chain of fix cards."""
        groups: dict[str, list] = {}
        for f in findings:
            where = str(f.get("where") or f.get("file") or "general")
            key = re.split(r"[\s:]", where.strip())[0] or "general"
            groups.setdefault(key if len(groups) < 4 or key in groups else "general", []).append(f)
        prefix = f"r{self.state['rounds']}-" if self.state["rounds"] else ""
        cards, prev = [], None
        for key, items in groups.items():
            cid = self.uid(f"{prefix}imp{rnd}")
            cards.append({"id": cid, "title": f"Fix: {key}"[:60], "depends_on": [prev] if prev else [], "acs": [], "owned_files": [],
                          "escalate": rnd >= 2, "body": "Reviewers and the automatic checks found problems in the integrated app. Fix every one, add or update tests where relevant:\n" +
                                  ("THE LAST FIX ROUND DID NOT RESOLVE THESE: the same problems came back. Do not repeat that approach; run the failing check yourself, find the real cause, then fix it.\n" if stuck else "") +
                                  "\n".join(f"- {t}" for t in findings_text(items))})
            prev = cid
        self.event("improvements", None, f"plan for improvements: {len(cards)} fix card(s) for {len(findings)} finding(s)")
        self.stage("build", "active")
        self.run_cards(cards)
        self.stage("build", "done")

    def record_metrics(self):
        """One line per finished run in runs/metrics.jsonl, so cost and quality can be compared over time (`src/factory.py stats`)."""
        st = self.state["status"]
        if st not in ("done", "failed"):
            return
        try:
            row = {"run_id": self.state["run_id"], "mode": self.state.get("mode"), "outcome": st, "cost_usd": self.state.get("cost_usd", 0),
                   "minutes": round(((self.state.get("ended_at") or time.time()) - self.state.get("created_at", time.time())) / 60, 1),
                   "fix_rounds": sum(1 for e in self.state["events"] if e["kind"] == "improvements"),
                   "open_issues": len(self.state.get("open_issues") or []), "escalated": bool(self.state.get("escalated")),
                   "cards": len(self.state["cards"]), "limit_waits": sum(1 for e in self.state["events"] if e["kind"] == "limit"),
                   "at": time.time()}
            path = self.rd.parent / "metrics.jsonl"
            rows = [l for l in (path.read_text(encoding="utf-8").splitlines() if path.exists() else []) if f'"{row["run_id"]}"' not in l]
            path.write_text("\n".join(rows + [json.dumps(row)]) + "\n", encoding="utf-8")
        except Exception:  # bookkeeping must never fail a run
            pass

    def tweak(self, feedback: str, n: int):
        """A small change from you goes straight to a builder, then a quick check (auto checks + security)."""
        self.stage("build", "active")
        self.run_cards([{"id": f"tweak-{n}", "title": "Small tweak", "depends_on": [], "acs": [], "owned_files": [],
                         "body": f"The owner reviewed the finished app and asked for a small change:\n{feedback}\nMake exactly that change, keep everything else working, add or update tests if relevant."}])
        self.stage("build", "done")
        self.reset_stages(["autocheck", "reviews"])
        self.verify_multi(light=True)

    # ------------------------------------------------------------------ the whole run
    def execute(self):
        self.start_heartbeat()
        try:
            self.set_status("running")
            for sub in ("factory", "holdout", "logs"):
                (self.rd / sub).mkdir(exist_ok=True)
            if "inputs" not in self.state:
                raise StageFailed("this run was made by an older version of the factory: start a new run")
            self.reset_unfinished()
            self.ensure_rail()
            self.stage_ideas()
            self.stage_reverse()
            self.stage_intake()
            self.stage_decide()
            while True:
                outcome = self.run_single() if self.state["mode"] == "single" else self.run_multi()
                if outcome != "escalated":
                    break
            self.set_status("done")
            self.event("done", None, "run complete")
        except StageFailed as e:
            if self.state["status"] != "stopped":
                self.set_status("failed", str(e)[:400])
            raise
        except Exception as e:  # noqa: BLE001
            self.set_status("failed", f"crashed: {e}"[:400])
            raise
        finally:
            self.save()
            self.stop_heartbeat()
            self.record_metrics()
