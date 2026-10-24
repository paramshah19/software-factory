"""Shared basics for the factory: roles, shell helpers, plan validation. No dependency on factory.py."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
AGENTS_DIR = HERE / "agents"


class StageFailed(Exception):
    pass


@dataclass
class Role:
    name: str
    model: str
    effort: str
    body: str
    retry_model: Optional[str] = None
    retry_effort: Optional[str] = None
    timeout_min: int = 30
    web: bool = False
    holdout: bool = False
    commands: tuple = ()   # extra shell commands this role may run (only the deployer)
    push: bool = False     # may `git push` (only the deployer, and never with --force)
    presets: dict = None   # {"lean": (model, effort), "max": (model, effort)}: the run's Quality choice; absent = same as Balanced
    overridden: bool = False  # the owner set this agent's model / effort themselves (config.json)


QUALITIES = ("lean", "max")  # besides the default, "balanced" (the plain model / effort lines)


def parse_role(text: str, name: str) -> Role:
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n(.*)", text, re.S)
    if not m:
        raise ValueError(f"{name}: missing frontmatter")
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    truthy = lambda k: meta.get(k, "").lower() == "true"
    return Role(name=name, model=meta["model"], effort=meta["effort"], body=m.group(2).strip(),
                retry_model=meta.get("retry_model"), retry_effort=meta.get("retry_effort"),
                timeout_min=int(meta.get("timeout_min", 30)), web=truthy("web"), holdout=truthy("holdout"),
                commands=tuple(c.strip() for c in meta.get("commands", "").split(",") if c.strip()), push=truthy("push"),
                presets={q: (meta[f"{q}_model"], meta.get(f"{q}_effort") or meta["effort"]) for q in QUALITIES if meta.get(f"{q}_model")})


EFFORTS = ("low", "medium", "high", "xhigh", "max")


def agent_overrides() -> dict:
    """The owner's own model / effort per agent, from ~/.software-factory/config.json ("agents": {"sf-planner": {...}}).
    Kept outside agents/*.md so updating the factory never overwrites them."""
    import engines  # late: engines must stay importable without common
    data = engines.load_config().get("agents")
    return data if isinstance(data, dict) else {}


def apply_override(role: Role, override: Optional[dict]) -> Role:
    """An owner override replaces the agent's model and effort under every Quality preset; retries still escalate."""
    if not isinstance(override, dict):
        return role
    model = str(override.get("model") or "").strip()
    effort = str(override.get("effort") or "").strip()
    if model:
        role.model = model
    if effort in EFFORTS:
        role.effort = effort
    if model or effort in EFFORTS:
        role.presets = {}
        role.overridden = True
    return role


def load_role(name: str, agents_dir: Path = AGENTS_DIR, overrides: Optional[dict] = None) -> Role:
    role = parse_role((agents_dir / f"{name}.md").read_text(encoding="utf-8"), name)
    return apply_override(role, (agent_overrides() if overrides is None else overrides).get(name))


def posix(p: Path) -> str:
    return Path(p).as_posix()


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # never pop up console windows on Windows


MAX_CARDS, MAX_CARDS_REWORK = 6, 4  # build cards per plan (scaffold included) / per rework round


def spec_acs(spec_text: str) -> list[str]:
    return sorted(set(re.findall(r"\bAC-\d+\b", spec_text)), key=lambda a: int(a[3:]))


def validate_cards(data, acs: list[str], rework: bool, prefix: str = "", existing: Optional[set] = None, multi_service: bool = False) -> list[str]:
    """Return a list of problems with a planner's cards.json (empty = valid)."""
    errs: list[str] = []
    if not isinstance(data, dict) or not isinstance(data.get("cards"), list) or not data["cards"]:
        return ["cards.json must be an object with a non-empty 'cards' list"]
    cards = data["cards"]
    ids = [c.get("id") if isinstance(c, dict) else None for c in cards]
    for c in cards:
        if not isinstance(c, dict):
            return ["every card must be an object"]
        cid = c.get("id")
        if not isinstance(cid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", cid):
            errs.append(f"bad card id {cid!r} (lowercase letters, digits, dashes)")
            continue
        for k in ("title", "body"):
            if not isinstance(c.get(k), str) or not c[k].strip():
                errs.append(f"{cid}: missing {k}")
        for k in ("owned_files", "depends_on", "acs"):
            if not isinstance(c.get(k), list):
                errs.append(f"{cid}: {k} must be a list")
    if errs:
        return errs
    if len(set(ids)) != len(ids):
        errs.append("duplicate card ids")
    cap = (MAX_CARDS_REWORK if rework else MAX_CARDS) + (4 if multi_service else 0)  # several services need more slices
    if len(cards) > cap:  # every card is a builder that re-reads spec + plan: the biggest cost driver in a run
        errs.append(f"{len(cards)} cards is too many (at most {cap}): merge them into fewer, bigger vertical slices")
    by = {c["id"]: c for c in cards}
    for c in cards:
        for d in c["depends_on"]:
            if d not in by:
                errs.append(f"{c['id']}: depends on unknown card {d!r}")
    if errs:
        return errs
    # cycles + reachability
    reach: dict[str, set] = {}

    def anc(i: str, seen: tuple = ()) -> set:
        if i in seen:
            raise ValueError(i)
        if i not in reach:
            r = set()
            for d in by[i]["depends_on"]:
                r |= {d} | anc(d, seen + (i,))
            reach[i] = r
        return reach[i]

    try:
        for i in by:
            anc(i)
    except ValueError as e:
        return [f"dependency cycle through {e}"]
    scaff = [c["id"] for c in cards if c.get("scaffold")]
    if rework and scaff:
        errs.append("rework rounds must not create a scaffold card")
    if not rework:
        if len(scaff) != 1:
            errs.append("exactly one card must have \"scaffold\": true")
        else:
            for i in by:
                if i != scaff[0] and scaff[0] not in reach[i]:
                    errs.append(f"{i}: must depend (directly or not) on the scaffold card")
    if rework:
        for i in by:
            if prefix and not i.startswith(prefix):
                errs.append(f"{i}: rework card ids must start with {prefix!r}")
            if existing and i in existing:
                errs.append(f"{i}: id already used in an earlier round")
    owner: dict[str, str] = {}
    for c in cards:
        for f in c["owned_files"]:
            o = owner.get(f)
            if o and o not in reach[c["id"]] and c["id"] not in reach[o]:
                errs.append(f"file {f!r} is owned by both {o} and {c['id']}, which can run in parallel")
            owner[f] = c["id"]
    covered = {a for c in cards for a in c["acs"]}
    if not rework:
        missing = [a for a in acs if a not in covered]
        if missing:
            errs.append("acceptance criteria not covered by any card: " + ", ".join(missing))
    return errs


def ready_cards(cards: list[dict], merged: set, started: set) -> list[dict]:
    return [c for c in cards if c["id"] not in merged and c["id"] not in started
            and all(d in merged for d in c["depends_on"])]


def failing_acs(pytest_out: str) -> list[tuple[str, str]]:
    """(AC id, test name) for each failed holdout test named test_ac<N>_..."""
    out = []
    for m in re.finditer(r"FAILED\s+\S*::(test_ac(\d+)\w*)", pytest_out):
        out.append((f"AC-{m.group(2)}", m.group(1)))
    return out


def holdout_withs(test_file: Path) -> list[str]:
    if not test_file.exists():
        return []
    withs = []
    for line in test_file.read_text(encoding="utf-8", errors="replace").splitlines()[:15]:
        m = re.match(r"\s*#\s*factory-with:\s*(.+)", line)
        if m:
            withs += m.group(1).replace(",", " ").split()
    return withs


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "run"


def sh(args: list[str], cwd: Path, timeout: int = 900, env: Optional[dict] = None) -> tuple[int, str]:
    if args and os.name == "nt" and not os.path.isabs(str(args[0])):  # npm, npx... are .cmd files on Windows
        args = [shutil.which(str(args[0])) or args[0], *args[1:]]
    try:
        p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env, creationflags=NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(args)}"
    except FileNotFoundError as e:
        return 127, str(e)


def git(app: Path, *args: str) -> tuple[int, str]:
    return sh(["git", "-C", str(app), *args], app)


def spec_ac_text(spec_text: str, ac: str, cap: int = 320) -> str:
    """The spec's own wording for one acceptance criterion (e.g. 'AC-3'), so a fix can be described without leaking hidden tests."""
    lead = r"^[ \t>*-]*(?:\d+[.)]\s*)?\**"
    m = re.search(lead + re.escape(ac) + r"(?!\d)\**[:.)\s-]*(.+)$", spec_text, re.M)
    return " ".join(m.group(1).split())[:cap] if m else ""
