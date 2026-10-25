"""Software Factory driver.

Runs a chain of Claude Code agents (see agents/*.md; each has its own model and effort) that turns an idea
into a working, tested product on this machine:

    idea -> spec -> [OWNER: approve spec] -> plan  (+ hidden acceptance tests, in parallel)
         -> build cards (parallel, each: build -> tests -> review -> merge)
         -> QA verify (fail -> fix cards -> re-verify, max 3 rounds) -> handoff docs -> [OWNER: final approval]

Every stage must leave real files on disk. The driver checks them (and runs the tests itself), so an agent
saying "done" is never enough.

Usage (from this folder):
    uv run src/factory.py check                       # are claude / git / uv available, do all models answer?
    uv run src/factory.py new [--idea "..."]          # start a run in the foreground (no idea = factory invents one)
    uv run src/factory.py run RUN                     # resume a run (after a failure or a restart)
    uv run src/factory.py status [RUN]
    uv run src/factory.py approve RUN [--note ".."]   # pass the pending owner gate
    uv run src/factory.py reject RUN "feedback"       # send the pending gate back
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
from common import (AGENTS_DIR, NO_WINDOW, WORKSPACE, Role, StageFailed, failing_acs, git, holdout_withs, load_role,  # noqa: E402
                    parse_role, posix, ready_cards, sh, slug, spec_ac_text, spec_acs, validate_cards)
import checks  # noqa: E402
import common  # noqa: E402
import engines  # noqa: E402
import kinds  # noqa: E402
from pipeline import PipelineMixin  # noqa: E402
RUNS_DIR = WORKSPACE / "runs"
ROLES = ["sf-ideator", "sf-intake", "sf-decider", "sf-maker", "sf-security", "sf-spec", "sf-researcher", "sf-mock", "sf-designer",
         "sf-planner", "sf-integrator", "sf-holdout", "sf-reverse", "sf-skills", "sf-builder", "sf-reviewer", "sf-reviewer-ui", "sf-writer", "sf-medic"]
DOCS = ["idea.md", "reverse.md", "brief.md", "skills.md", "spec.md", "research.md", "design.md", "plan.md", "report.md"]

MAX_PARALLEL = 3        # agents running at once (kept low for subscription rate limits)
MAX_CARD_ROUNDS = 3     # build/review rounds per card
MAX_HEAL = 2            # troubleshooter attempts per failed station

SHELLS = ("Bash", "PowerShell")
SHELL_OK = ("uv", "git", "python", "py", "ls", "cat", "mkdir", "dir", "cd", "pwd", "head", "tail", "grep", "wc", "sort",
            "uniq", "echo", "test", "sleep", "which", "diff", "curl http://127.0.0.1", "curl http://localhost",
            "Get-ChildItem", "Get-Content", "Set-Location", "Push-Location", "Pop-Location", "Select-Object",
            "Select-String", "Measure-Object", "Where-Object", "Sort-Object", "Write-Output", "Write-Host",
            "Test-Path", "Start-Sleep", "Out-String")
SHELL_DENY = ("git push", "git remote")
SHELL_DENY_PUSH = ("git push --force", "git push -f", "git remote remove")  # the deployer may push, never destructively




# --------------------------------------------------------------------------- roles










# --------------------------------------------------------------------------- calling claude


QUALITY_CHOICES = ("lean", "balanced", "max")


def pick_model(role: Role, escalate: bool, quality: str = "balanced") -> tuple[str, str]:
    """Model and effort for an attempt. The run's Quality picks the preset (Lean / Balanced / Max); a retry
    escalates to the role's stronger pair (every retry pair is at least as strong as the Max preset)."""
    if escalate and role.retry_model:
        return role.retry_model, role.retry_effort or role.effort
    return (role.presets or {}).get(quality) or (role.model, role.effort)


def default_quality() -> str:
    q = engines.load_config().get("quality")
    return q if q in QUALITY_CHOICES else "balanced"


def run_quality(run) -> str:
    q = ((getattr(run, "state", None) or {}).get("inputs") or {}).get("quality")
    return q if q in QUALITY_CHOICES else "balanced"


def claude_cmd(role: Role, model: str, effort: str, run_dir: Path, prompt_file: Path, extra_allow: tuple = ()) -> list[str]:
    allow = ["Read", "Write", "Edit", "Glob", "Grep", *extra_allow]
    for sh in SHELLS:
        allow += [f"{sh}({c}:*)" for c in (*SHELL_OK, *role.commands)]
    if role.web:
        allow += ["WebSearch", "WebFetch"]
    deny = [f"{sh}({c}:*)" for sh in SHELLS for c in (SHELL_DENY_PUSH if role.push else SHELL_DENY)]
    if not role.holdout:  # hidden tests: only the holdout writer and QA may touch them
        deny += [f"{t}(**/holdout/**)" for t in ("Read", "Edit", "Write")]
    return [shutil.which("claude") or "claude", "-p", "--model", model, "--effort", effort,
            "--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk",
            "--no-session-persistence", "--setting-sources", "project",
            "--add-dir", posix(run_dir), "--append-system-prompt-file", posix(prompt_file),
            "--disallowedTools", *deny, "--allowedTools", *allow]


def tool_summary(name: str, inp: dict, run_dir: str = "") -> str:
    s = ""
    for key in ("file_path", "command", "pattern", "query", "url", "path"):
        if inp.get(key):
            s = str(inp[key])
            break
    s = s.replace("\\", "/").replace(run_dir, "<run>") if run_dir else s
    return " ".join(s.split())[:140]


def format_event(ev: dict, run_dir: str = "") -> list[str]:
    """One stream-json event -> zero or more readable log lines."""
    out = []
    t = ev.get("type")
    msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
    content = [b for b in msg.get("content", []) if isinstance(b, dict)] if isinstance(msg.get("content"), list) else []
    if t == "assistant":
        for b in content:
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append("… " + " ".join(b["text"].split())[:260])
            elif b.get("type") == "tool_use":
                out.append(f"▸ {b.get('name')} {tool_summary(b.get('name', ''), b.get('input') or {}, run_dir)}".rstrip())
    elif t == "user":
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                c = b.get("content")
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else str(c)
                out.append("✗ " + " ".join(c.split())[:200])
    elif t == "result":
        out.append(f"■ finished in {round((ev.get('duration_ms') or 0) / 1000)}s · ${ev.get('total_cost_usd') or 0:.2f}")
    return out


@dataclass
class AgentResult:
    ok: bool
    text: str = ""
    cost: float = 0.0
    error: str = ""
    denied: list = field(default_factory=list)  # tool calls the permission rules refused




def scan_denials(ev: dict, pending: dict, denied: list, run_dir: str = "") -> None:
    """Track tool calls and collect the ones the permission rules refused."""
    msg = ev.get("message") if ev.get("type") in ("assistant", "user") else None
    content = msg.get("content") if isinstance(msg, dict) else None
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use":
            pending[b.get("id")] = f"{b.get('name')} {tool_summary(b.get('name', ''), b.get('input') or {}, run_dir)}".strip()
        elif b.get("type") == "tool_result" and b.get("is_error"):
            c = b.get("content")
            text = " ".join(x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else str(c)
            if re.search(r"permission to use|has been denied|don't ask mode|denied by your permission", text, re.I):
                denied.append(pending.get(b.get("tool_use_id"), "a tool call")[:200])


def denial_hint(denied: list) -> str:
    if not denied:
        return ""
    shown = "\n".join(f"  - {d}" for d in denied[:6])
    return ("These tool calls were DENIED by the permission rules:\n" + shown +
            "\nRewrite them as simple single commands with full paths: `git -C <dir> ...`, `uv run --project <dir> ...`, "
            "`python ...`, `ls`, `cat`. No variable assignments, no `cd`, no `&&` chains, no `VAR=x cmd` prefixes. "
            "Use the Read, Glob and Grep tools to read files.")


def _kill_tree(proc: subprocess.Popen):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
    except Exception:
        pass


def _agent_env(run_dir: Path, connectors: bool = False) -> dict:
    env = dict(os.environ)
    env["FACTORY_APP_DIR"] = str(run_dir / "app")
    if connectors:  # remote connectors (Netlify, Vercel...) connect in the background; without this a headless agent starts
        env["MCP_CONNECTION_NONBLOCKING"] = "false"  # before they finish and never gets their tools (measured: 0 vs 12 tools)
    if os.name == "nt" and not env.get("CLAUDE_CODE_GIT_BASH_PATH"):
        git = shutil.which("git")
        if git:
            here = Path(git).resolve().parent
            for root in [here, *here.parents][:4]:  # ...\Git\mingw64\bin\git.exe -> ...\Git\bin\bash.exe
                found = next((c for c in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe") if c.exists()), None)
                if found:
                    env["CLAUDE_CODE_GIT_BASH_PATH"] = str(found)
                    break
    return env


KIND_ROLES = {"sf-maker", "sf-planner", "sf-integrator", "sf-builder", "sf-holdout", "sf-spec", "sf-decider", "sf-reviewer", "sf-deployer", "sf-medic"}
_CAPABILITIES: list = []


def protocol_text(role_name: str = "") -> str:
    """The shared protocol for one agent; the agents that choose, build or check the kind of product also get the kinds
    reference and what this computer can build (Node.js for phone apps, Godot for games...)."""
    text = (AGENTS_DIR / "_protocol.md").read_text(encoding="utf-8").replace("{{HOST_RULE}}", engines.host_rule())
    if role_name in KIND_ROLES:
        if not _CAPABILITIES:
            _CAPABILITIES.append(kinds.capability_note())
        text += "\n\n" + _CAPABILITIES[0] + "\n\n" + (AGENTS_DIR / "_kinds.md").read_text(encoding="utf-8")
    return text


def run_claude(run: "Run", card_id: str, role: Role, prompt: str, cwd: Path, escalate: bool = False) -> AgentResult:
    """Run one agent to completion on the configured engine (Claude Code unless configured otherwise).
    Streams a readable log to logs/<card>.log for the dashboard."""
    model, effort = pick_model(role, escalate, run_quality(run))
    pdir = run.rd / "logs" / "prompts"
    pdir.mkdir(parents=True, exist_ok=True)
    pfile = pdir / f"{card_id}.md"
    system_text = protocol_text(role.name) + "\n\n---\n\n" + role.body + run.system_extras(role.name)
    log = run.rd / "logs" / f"{card_id}.log"
    raw = run.rd / "logs" / f"{card_id}.jsonl"
    eng = engines.current()

    def note(line: str):
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    if eng is not None:
        pfile.write_text(engines.full_prompt(system_text, prompt), encoding="utf-8")
        return run_engine(run, card_id, role, eng, model, effort, cwd, pfile, note, raw, escalate)
    pfile.write_text(system_text, encoding="utf-8")
    run.card_update(card_id, model=model, effort=effort)
    note(f"──── {role.name} · {model} · effort {effort}" + (" · retry" if escalate else ""))
    proc = subprocess.Popen(claude_cmd(role, model, effort, run.rd, pfile, tuple(getattr(run, "extra_allow", ()))), cwd=str(cwd), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", env=_agent_env(run.rd, bool(getattr(run, "extra_allow", ()))), creationflags=NO_WINDOW)
    timer = threading.Timer(role.timeout_min * 60, lambda: (note("✗ timed out, killing"), _kill_tree(proc)))
    timer.start()
    err_buf: list[str] = []
    threading.Thread(target=lambda: err_buf.extend(proc.stderr), daemon=True).start()
    result = AgentResult(ok=False, error="no result event")
    pending: dict = {}
    denied: list = []
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        with open(raw, "a", encoding="utf-8") as rawfh:
            for line in proc.stdout:
                rawfh.write(line)
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    for l in format_event(ev, posix(run.rd)):
                        note(l)
                    scan_denials(ev, pending, denied, posix(run.rd))
                except Exception as e:  # a log-parsing problem must never kill the agent's run
                    note(f"(log parse skipped: {e})")
                if ev.get("type") == "result":
                    result = AgentResult(ok=not ev.get("is_error"), text=str(ev.get("result") or ""),
                                         cost=float(ev.get("total_cost_usd") or 0),
                                         error=str(ev.get("result") or "") if ev.get("is_error") else "")
        proc.wait()
    finally:
        timer.cancel()
    result.denied = denied
    if not result.ok and not result.error:
        result.error = "".join(err_buf)[-400:] or "agent produced no result (timeout or crash)"
    if not result.ok:
        note("✗ " + result.error[:300])
    return result


def run_engine(run: "Run", card_id: str, role: Role, eng: "engines.Engine", model: str, effort: str, cwd: Path, pfile: Path,
               note: Callable[[str], None], raw: Path, escalate: bool) -> AgentResult:
    """Run one agent on a non-Claude engine (Codex, or any headless agent CLI). Same contract as the Claude path."""
    use = eng.model_for(model)
    run.card_update(card_id, model=eng.model_label(model), effort=effort)
    note(f"──── {role.name} · {eng.label} · {use or 'default model'} · effort {effort}" + (" · retry" if escalate else ""))
    try:
        argv, stdin_text = eng.command(use, effort, cwd, run.rd, role.web, pfile)
    except ValueError as e:
        note("✗ " + str(e))
        return AgentResult(ok=False, error=str(e))
    parser = eng.parser(posix(run.rd))
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                env=_agent_env(run.rd), creationflags=NO_WINDOW)
    except OSError as e:
        note(f"✗ could not start {eng.label}: {e}")
        return AgentResult(ok=False, error=f"could not start {eng.label}: {e}")
    timer = threading.Timer(role.timeout_min * 60, lambda: (note("✗ timed out, killing"), _kill_tree(proc)))
    timer.start()
    try:
        if stdin_text is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except OSError:
                pass
        with open(raw, "a", encoding="utf-8") as rawfh:
            for line in proc.stdout:
                rawfh.write(line)
                try:
                    for l in parser.feed(line):
                        note(l)
                except Exception as e:  # a log-parsing problem must never kill the agent's run
                    note(f"(log parse skipped: {e})")
        proc.wait()
    finally:
        timer.cancel()
    parser.finish(proc.returncode)
    result = AgentResult(ok=parser.ok, text=parser.text, cost=parser.cost, error=parser.error)
    if not result.ok:
        note("✗ " + (result.error or "agent failed")[:300])
    return result


# --------------------------------------------------------------------------- usage limits, budget, notifications

LIMIT_RE = re.compile(r"session limit|usage limit|rate limit|limit reached|hit your .{0,30}limit|out of (?:extra )?usage", re.I)
LIMIT_FALLBACK_WAIT = 20 * 60   # when the message gives no reset time
LIMIT_MAX_WAIT = 8 * 3600
DEFAULT_BUDGET = {"single": 10.0, "multi": 45.0}


def budget_for(state: dict) -> float:
    """The spending cap of a run (0 = no cap): what the owner set, else the environment, else a default per path."""
    inp = state.get("budget_usd")
    if inp is None:
        inp = (state.get("inputs") or {}).get("budget_usd")
    if inp is None:
        env = os.environ.get("FACTORY_BUDGET_USD")
        try:
            inp = float(env) if env else None
        except ValueError:
            inp = None
    if inp is None:
        saved = engines.load_config().get("budget")  # the owner's default from setup
        if isinstance(saved, dict) and isinstance(saved.get(state.get("mode") or "multi"), (int, float)):
            inp = saved[state.get("mode") or "multi"]
    if inp is None:
        inp = DEFAULT_BUDGET.get(state.get("mode") or "multi", 45.0)
    return float(inp)


def is_limit_error(text: str) -> bool:
    return bool(LIMIT_RE.search(text or ""))


def parse_limit_reset(text: str, now: Optional[float] = None) -> Optional[float]:
    """'... resets 6:10pm (Europe/London)' -> the next local time that clock reads, as an epoch. None if no time is given."""
    m = re.search(r"resets?\W{0,6}(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text or "", re.I)
    if not m:
        return None
    hour, minute, ap = int(m.group(1)) % 12, int(m.group(2) or 0), m.group(3).lower()
    if ap == "pm":
        hour += 12
    now = time.time() if now is None else now
    t = _dt.datetime.fromtimestamp(now).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if t.timestamp() <= now:
        t += _dt.timedelta(days=1)
    return t.timestamp()


def notify(title: str, msg: str) -> None:
    """Best-effort desktop balloon so a long run can tell you it needs you. Never raises."""
    if os.environ.get("FACTORY_NOTIFY") == "0" or engines.load_config().get("notify") is False:
        return
    if os.name != "nt":
        clean = lambda x: str(x).replace('"', "'")[:120]
        cmd = (["osascript", "-e", f'display notification "{clean(msg)}" with title "{clean(title)}"'] if sys.platform == "darwin"
               else ["notify-send", clean(title), clean(msg)] if shutil.which("notify-send") else None)
        if cmd:
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                pass
        return
    try:
        esc = lambda x: str(x).replace("'", "")[:120]
        ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;$n=New-Object System.Windows.Forms.NotifyIcon;"
              "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
              f"$n.ShowBalloonTip(8000,'{esc(title)}','{esc(msg)}',[System.Windows.Forms.ToolTipIcon]::Info);Start-Sleep 9;$n.Dispose()")
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except Exception:
        pass


Agent = Callable[["Run", str, Role, str, Path, bool], AgentResult]


# --------------------------------------------------------------------------- pure helpers












def gate_blurb(kind: str) -> tuple[str, str]:
    return {
        "idea": ("The idea critic said STOP",
                 "Read idea.md. Approve to build it anyway, or reject to end this run."),
        "spec": ("Approve the spec",
                 "Nothing gets built until you sign off. Check the acceptance criteria: they are what the hidden tests enforce."),
        "help": ("An agent is stuck and needs your help",
                 "The troubleshooter could not fix this itself. Read the explanation, fix the cause if needed, then Approve to retry or Reject to stop the run."),
        "budget": ("Budget reached", "The run hit its spending cap. Approve to allow some more, or reject to stop here."),
        "final": ("Final approval",
                  "Read the report, then try the product. Rejecting starts a rework round using your feedback."),
    }.get(kind, ("Your decision", ""))




# --------------------------------------------------------------------------- shell helpers






# --------------------------------------------------------------------------- the run


class Run(PipelineMixin):
    """One pipeline run. All progress lives in runs/<id>/state.json so a crash can resume."""

    def __init__(self, rd: Path, agent: Agent = run_claude):
        self.rd = Path(rd)
        self.agent = agent
        self.lock = threading.RLock()
        self.git_lock = threading.Lock()
        self.test_cmd = ["uv", "run", "pytest", "-q"]
        self.holdout_cmd = ["uv", "run", "--with", "pytest", "pytest", "-q"]  # + withs + holdout dir
        self.poll = 1.0
        self.state = load_state(self.rd)
        self._beat = threading.Event()
        self.denials: dict[str, list] = {}
        self.sleep = time.sleep
        self.extra_allow: list[str] = []  # connector tools this run's agents may use (the deployer only)
        self.budget_lock = threading.Lock()
        self.notifies = agent is run_claude  # only real runs pop up desktop notices


    # ---- state
    @property
    def app(self) -> Path:
        return self.rd / "app"

    @property
    def fdir(self) -> Path:
        return self.rd / "factory"

    def save(self):
        with self.lock:
            self.state["heartbeat"] = time.time()
            tmp = self.rd / "state.json.tmp"
            tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
            for _ in range(20):
                try:
                    os.replace(tmp, self.rd / "state.json")
                    return
                except PermissionError:
                    time.sleep(0.05)

    def start_heartbeat(self):
        def beat():
            while not self._beat.wait(4):
                self.save()
        threading.Thread(target=beat, daemon=True).start()

    def stop_heartbeat(self):
        self._beat.set()

    def event(self, kind: str, card: Optional[str], msg: str = ""):
        with self.lock:
            evs = self.state["events"]
            evs.append({"id": (evs[-1]["id"] + 1) if evs else 1, "kind": kind, "card": card, "msg": msg,
                        "created_at": time.time()})
            del evs[:-200]
            self.save()
        try:  # the driver runs hidden: this is its terminal output, shown in the dashboard's Factory console
            print(f"{time.strftime('%H:%M:%S')}  {kind:<10} {card or '':<16} {' '.join(str(msg).split())[:220]}".rstrip(), flush=True)
        except (OSError, ValueError):
            pass

    def card(self, cid: str) -> dict:
        return next(c for c in self.state["cards"] if c["id"] == cid)

    def add_card(self, cid: str, title: str, role: Optional[str], kind: str, stage: str, **extra) -> dict:
        with self.lock:
            existing = [c for c in self.state["cards"] if c["id"] == cid]
            if existing:
                return existing[0]
            c = {"id": cid, "title": title, "role": role, "kind": kind, "stage": stage, "state": "pending",
                 "status": "", "attempts": 0, "reviews": 0, "summary": None, "error": None, "model": None,
                 "effort": None, "cost_usd": 0.0, "started_at": None, "completed_at": None, "depends_on": [],
                 "round": self.state["rounds"]}
            c.update(extra)
            self.state["cards"].append(c)
            self.save()
            return c

    def card_update(self, cid: str, **kw):
        with self.lock:
            self.card(cid).update(kw)
            self.save()

    def card_start(self, cid: str, status: str = "running"):
        with self.lock:
            c = self.card(cid)
            c["state"], c["status"], c["error"] = "active", status, None
            c["started_at"] = c["started_at"] or time.time()
            self.save()

    def card_done(self, cid: str, summary: str = ""):
        with self.lock:
            c = self.card(cid)
            c.update(state="done", status="done", summary=summary or c["summary"], completed_at=time.time(), error=None)
            self.save()

    def card_block(self, cid: str, error: str):
        with self.lock:
            self.card(cid).update(state="blocked", status="blocked", error=error[:400])
            self.event("blocked", cid, error[:200])

    def stage(self, key: str, st: str, detail: Optional[str] = None):
        with self.lock:
            s = self.state["stages"].setdefault(key, {"state": "pending", "label": key, "bot": None})
            s["state"] = st
            if detail is not None:
                s["detail"] = detail
            if st == "active" and not s.get("started_at"):
                s["started_at"] = time.time()
            if st == "done":
                s["completed_at"] = time.time()
            self.save()

    def stage_state(self, key: str) -> str:
        return self.state["stages"].get(key, {}).get("state", "pending")

    def set_status(self, status: str, error: Optional[str] = None):
        with self.lock:
            self.state["status"] = status
            self.state["error"] = error
            if status in ("done", "stopped"):
                self.state["ended_at"] = time.time()
            self.save()


    # ---- usage limits and budget
    def wait_for_limit(self, text: str, card_id: str):
        """The account hit its usage limit: sleep until it resets instead of burning retries, healing and fix rounds."""
        reset = parse_limit_reset(text)
        until = min((reset + 90) if reset else time.time() + LIMIT_FALLBACK_WAIT, time.time() + LIMIT_MAX_WAIT)
        when = _dt.datetime.fromtimestamp(until).strftime("%H:%M")
        with self.lock:
            self.state["waiting"] = {"until": until, "reason": "Claude usage limit reached", "card": card_id}
        self.event("limit", card_id, f"Claude usage limit reached; waiting until about {when}, then continuing on its own")
        if self.notifies:
            notify("Software Factory paused", f"Usage limit reached. Continues at about {when}.")
        while time.time() < until:
            self.sleep(min(30, max(0.0, until - time.time())))
        with self.lock:
            self.state["waiting"] = None
        self.event("limit_over", card_id, "waiting is over; retrying")

    def budget(self) -> float:
        return budget_for(self.state)

    def check_budget(self, card_id: str):
        """Over budget: stop and ask. Approve adds half the original budget; reject ends the run."""
        base = self.budget()
        if base <= 0:
            return
        with self.budget_lock:
            if self.state["cost_usd"] < self.budget():
                return
            spent, cap = self.state["cost_usd"], self.budget()
            stage_key = self.card(card_id)["stage"]
            n = self.state["budget_gates"] = self.state.get("budget_gates", 0) + 1
            dec, _ = self.gate("budget", f"gate-budget-{n}", stage_key, None, title=f"Budget reached: ${spent:.2f} of ${cap:.2f}",
                               blurb=f"This run has spent ${spent:.2f}, which is its cap. Approve to allow ${base / 2:.2f} more, or reject to stop the run here (everything built so far is kept).",
                               restore="active")
            if dec == "reject":
                raise StageFailed(f"stopped at the budget (${spent:.2f})")
            self.state["budget_usd"] = cap + base / 2
            self.save()

    # ---- running one agent stage with output checks
    def prompt(self, card_id: str, title: str, body: str) -> str:
        return f"RUN_DIR: {posix(self.rd)}\nCARD: {card_id} — {title}\n\n{body}"

    def run_agent_stage(self, card_id: str, role_name: str, prompt: str, outputs: list[Path],
                        validate: Optional[Callable[[], Optional[str]]] = None, cwd: Optional[Path] = None,
                        tries: int = 2, heal: bool = True, force_escalate: bool = False) -> None:
        """Run an agent until its output files pass the checks. If it keeps failing, ask the troubleshooter
        (which routes the fix to the right agent) before giving up."""
        role = load_role(role_name)
        problem = ""
        attempt, limit_waits = 0, 0
        while attempt < tries:
            attempt += 1
            self.check_budget(card_id)
            self.card_start(card_id)
            self.card_update(card_id, attempts=self.card(card_id)["attempts"] + 1)
            self.event("started", card_id, f"{role_name} attempt {attempt}")
            p = prompt if not problem else prompt + f"\n\nYOUR PREVIOUS ATTEMPT FAILED CHECKS. Fix this and write the files again:\n{problem}"
            res = self.agent(self, card_id, role, p, cwd or self.rd, attempt > 1 or force_escalate)
            with self.lock:
                self.card(card_id)["cost_usd"] = round(self.card(card_id)["cost_usd"] + res.cost, 4)
                self.state["cost_usd"] = round(sum(c["cost_usd"] for c in self.state["cards"]), 4)
            self.denials[card_id] = list(res.denied)
            if not res.ok and is_limit_error(res.error) and limit_waits < 6:
                limit_waits += 1
                self.wait_for_limit(res.error, card_id)
                attempt -= 1  # a usage limit is not the agent's fault: the attempt does not count
                continue
            problem = ""
            if not res.ok:
                problem = f"agent run failed: {res.error[:300]}"
            else:
                missing = [posix(o.relative_to(self.rd)) for o in outputs if not o.exists() or o.stat().st_size == 0]
                if missing:
                    problem = "these required files are missing or empty: " + ", ".join(missing)
                elif validate:
                    problem = validate() or ""
            if problem and res.denied:
                problem += "\n" + denial_hint(res.denied)
            if not problem:
                return
            self.event("retry" if attempt < tries else "failed", card_id, problem[:200])
        if heal and self.heal_stage(card_id, role_name, prompt, outputs, validate, cwd, problem):
            return
        self.card_block(card_id, problem)
        raise StageFailed(f"{card_id}: {problem}")

    def log_tail(self, card_id: str, n: int = 40) -> str:
        f = self.rd / "logs" / f"{card_id}.log"
        if not f.exists():
            return "(no log)"
        return "\n".join(f.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])

    def check_holdout_collects(self) -> Optional[str]:
        rc, out = sh(self.holdout_cmd_for() + ["--collect-only"], self.rd, 300)
        return None if rc == 0 and re.search(r"test_ac\d+", out) else f"hidden tests do not collect (need tests named test_ac<N>_...):\n{out[-500:]}"

    def heal_stage(self, card_id: str, role_name: str, prompt: str, outputs: list[Path],
                   validate: Optional[Callable[[], Optional[str]]], cwd: Optional[Path], problem: str) -> bool:
        """A station failed twice. Ask the troubleshooter who should fix it, let that agent do it, then retry the
        station with the troubleshooter's guidance. Returns True if the station finally produced valid output."""
        c = self.card(card_id)
        stage_key = c["stage"]
        for n in range(1, MAX_HEAL + 1):
            mid = f"{card_id}-heal{n}"
            self.add_card(mid, f"Diagnose: {c['title']}", "sf-medic", "heal", stage_key)
            diag = self.fdir / "cards" / mid / "diagnosis.json"
            diag.parent.mkdir(parents=True, exist_ok=True)
            diag.unlink(missing_ok=True)
            body = (f"Station `{card_id}` ({role_name}: {c['title']}) failed its checks twice.\n\nPROBLEM:\n{problem}\n\n"
                    f"DENIED TOOL CALLS: {self.denials.get(card_id) or 'none'}\n\nLAST LOG LINES:\n{self.log_tail(card_id)}\n\n"
                    f"Write your diagnosis to factory/cards/{mid}/diagnosis.json.")

            def check():
                try:
                    d = json.loads(diag.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as e:
                    return f"diagnosis.json invalid: {e}"
                if d.get("action") not in ("retry", "route", "owner"):
                    return "action must be retry, route or owner"
                if d["action"] == "route" and d.get("route_to") not in ("sf-holdout", "sf-builder"):
                    return "route_to must be sf-holdout or sf-builder"
                return None if str(d.get("guidance", "")).strip() else "guidance is required"
            try:
                self.run_agent_stage(mid, "sf-medic", self.prompt(mid, f"Diagnose {card_id}", body), [diag], check, heal=False)
            except StageFailed:
                return False
            d = json.loads(diag.read_text(encoding="utf-8"))
            guidance = str(d["guidance"])
            self.card_done(mid, f"{d['action']}: {str(d.get('cause', ''))[:150]}")
            self.event("healing", card_id, f"{d['action']}" + (f" → {d['route_to']}" if d["action"] == "route" else "") + f": {str(d.get('cause', ''))[:120]}")
            try:
                if d["action"] == "owner":
                    dec, _ = self.gate("help", f"gate-help-{card_id}-{n}", stage_key, None, title=f"Stuck: {c['title']}",
                                       blurb=f"{d.get('cause', '')} — {guidance}", restore="active")
                    if dec == "reject":
                        return False
                elif d["action"] == "route" and d["route_to"] == "sf-holdout":
                    hid = f"{card_id}-holdout{n}"
                    self.add_card(hid, "Fix hidden acceptance tests", "sf-holdout", "stage", stage_key)
                    self.run_agent_stage(hid, "sf-holdout", self.prompt(hid, "Fix hidden tests", "Fix the hidden acceptance tests in holdout/ (work only from factory/spec.md).\n" + guidance),
                                         [self.rd / "holdout" / "test_acceptance.py"], self.check_holdout_collects, heal=False)
                    self.card_done(hid, "hidden tests rewritten")
                elif d["action"] == "route" and d["route_to"] == "sf-builder":
                    bid = f"{card_id}-fix{n}"
                    self.run_cards([{"id": bid, "title": f"Fix for {c['title']}", "depends_on": [], "acs": [], "owned_files": [],
                                     "body": f"A later station could not do its job because of a product problem.\n{guidance}"}])
                    self.stage("build", "done")
            except StageFailed:
                return False
            retry_prompt = prompt + f"\n\nTROUBLESHOOTER GUIDANCE (your earlier attempts failed: {problem[:300]}):\n{guidance}"
            try:
                self.run_agent_stage(card_id, role_name, retry_prompt, outputs, validate, cwd, tries=1, heal=False, force_escalate=True)
                return True
            except StageFailed as e:
                problem = str(e)
        return False

    # ---- gates
    def gate(self, kind: str, card_id: str, stage_key: str, doc: Optional[str], title: Optional[str] = None,
             blurb: Optional[str] = None, restore: Optional[str] = None, accept: tuple = ("approve", "reject"),
             options: Optional[list] = None, auto_seconds: Optional[float] = None, extra: Optional[dict] = None,
             images: Optional[list] = None) -> tuple[str, str]:
        """Stop and wait for the owner. `accept` lists the decisions that count; with `auto_seconds` the gate approves itself."""
        t0, b0 = gate_blurb(kind)
        title, blurb = title or t0, blurb or b0
        self.add_card(card_id, f"OWNER GATE — {title}", None, "gate", stage_key)
        deadline = (time.time() + auto_seconds) if auto_seconds else None
        with self.lock:
            self.card(card_id).update(state="owner", status="waiting for you", started_at=time.time())
            self.state["gate"] = {"id": card_id, "kind": kind, "title": title, "blurb": blurb, "doc": doc, "accept": list(accept),
                                  "options": options, "deadline": deadline, "images": images or [], **(extra or {})}
            self.state["status"] = "gate"
            self.stage(stage_key, "owner")
        self.event("gate", card_id, title)
        if self.notifies:
            notify("Software Factory needs you", title)
        dec = self.rd / "decision.json"
        while True:
            if dec.exists():
                try:
                    d = json.loads(dec.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    time.sleep(0.2)
                    continue
                dec.unlink(missing_ok=True)
                if d.get("gate") == card_id and d.get("decision") in accept:
                    break
            if deadline and time.time() >= deadline:
                d = {"gate": card_id, "decision": "approve", "auto": True}
                break
            time.sleep(self.poll)
        self.last_decision = d
        with self.lock:
            self.state["gate"] = None
            self.state["status"] = "running"
            said = d.get("feedback") or d.get("note") or ""
            label = "continued automatically" if d.get("auto") else {"approve": "approved", "reject": "rejected"}.get(d["decision"], d["decision"])
            self.card_done(card_id, label + (f": {said}"[:200] if said else ""))
            self.stage(stage_key, restore or "done")
        kind_ev = {"approve": "approved", "reject": "rejected"}.get(d["decision"], "decided")
        self.event(kind_ev, card_id, said or ("" if kind_ev != "decided" else d["decision"]))
        return d["decision"], d.get("feedback") or ""

    # ---- stages


    def stage_holdout(self):
        if self.stage_state("holdout") == "done":
            return
        self.stage("holdout", "active")
        self.add_card("holdout", "Hidden acceptance tests", "sf-holdout", "stage", "holdout")
        tf = self.rd / "holdout" / "test_acceptance.py"

        self.run_agent_stage("holdout", "sf-holdout", self.prompt("holdout", "Hidden tests", "Write the hidden acceptance tests from factory/spec.md."),
                             [tf, self.rd / "holdout" / "scenarios.md"], self.check_holdout_collects)
        self.card_done("holdout", "hidden tests written")
        self.stage("holdout", "done")

    def holdout_cmd_for(self) -> list[str]:
        tf = self.rd / "holdout" / "test_acceptance.py"
        cmd = list(self.holdout_cmd)
        extra = [x for w in holdout_withs(tf) for x in ("--with", w)]
        if cmd[:2] == ["uv", "run"]:
            cmd[2:2] = extra
        return cmd + [str(self.rd / "holdout")]

    def stage_research(self):
        if self.stage_state("research") == "done":
            return
        self.stage("research", "active")
        self.add_card("research", "Research", "sf-researcher", "stage", "research")
        out = self.fdir / "research.md"

        def check():
            text = out.read_text(encoding="utf-8")
            if "NO RESEARCH NEEDED" in text or re.search(r"https?://", text):
                return None
            return "research.md must cite its sources as full https:// URLs, or say 'NO RESEARCH NEEDED: <why>'"
        self.run_agent_stage("research", "sf-researcher", self.prompt(
            "research", "Research", "Research what this build needs and write factory/research.md, using the web. Read factory/brief.md and factory/idea.md first (and factory/spec.md if it exists)."), [out], check)
        self.card_done("research", "research.md written")
        self.stage("research", "done")

    def stage_plan(self) -> list[dict]:
        r = self.state["rounds"]
        cid = "plan" if r == 0 else f"plan-r{r}"
        cj = self.fdir / "cards.json"
        if self.stage_state("plan") == "done":
            return json.loads(cj.read_text(encoding="utf-8"))["cards"]
        self.stage("plan", "active")
        self.add_card(cid, "Technical plan" if r == 0 else f"Technical plan (rework {r})", "sf-planner", "stage", "plan")
        acs = spec_acs((self.fdir / "spec.md").read_text(encoding="utf-8"))
        prefix = f"r{r}-" if r else ""
        existing = {c["id"] for c in self.state["cards"]}
        body = "Write factory/plan.md, factory/cards.json and factory/app.json from the approved factory/spec.md."
        if (self.fdir / "mock").exists():
            body += (" The design side is being planned in parallel by the design planner (an integrator merges both plans), so don't wait for it: "
                     "plan from the spec, the brief and the approved visual mock in factory/mock/ (screenshots in factory/mock/shots/).")
        if r:
            body = (f"REWORK round {r}. Card id prefix: {prefix!r}. OWNER FEEDBACK:\n{self.state.get('feedback') or '(none)'}\n"
                    "Read factory/plan.md and factory/report.md, then plan only the changes needed. Do not create a scaffold card or a new app.json.")
        outputs = [self.fdir / "plan.md", cj] + ([self.fdir / "app.json"] if r == 0 else [])
        self.run_agent_stage(cid, "sf-planner", self.prompt(cid, "Technical plan", body), outputs, lambda: self.plan_problem(acs, r, prefix, existing))
        cards = json.loads(cj.read_text(encoding="utf-8"))["cards"]
        self.card_done(cid, f"{len(cards)} build cards")
        self.stage("plan", "done")
        return cards

    # ---- build
    def init_app(self):
        self.app.mkdir(exist_ok=True)
        if (self.app / ".git").exists():
            return
        git(self.app, "init", "-b", "main")
        git(self.app, "config", "user.name", "Software Factory")
        git(self.app, "config", "user.email", "factory@localhost")
        (self.app / ".gitignore").write_text(".venv/\n__pycache__/\n*.pyc\n.pytest_cache/\n.worktrees/\n", encoding="utf-8")
        git(self.app, "add", "-A")
        git(self.app, "commit", "-m", "Initial commit")

    def run_cards(self, cards: list[dict], stage_key: str = "build"):
        """Build, review and merge a set of cards, honouring depends_on. Raises StageFailed if any card fails."""
        self.init_app()
        for c in cards:
            self.add_card(c["id"], c["title"], "sf-builder", "build", "build", depends_on=c["depends_on"])
        by = {c["id"]: c for c in cards}
        merged = {i for i in by if self.card(i)["state"] == "done"}
        started: set = set()
        failed: dict[str, str] = {}
        with ThreadPoolExecutor(MAX_PARALLEL) as ex:
            futs = {}
            while True:
                for c in ready_cards(cards, merged, started):
                    if len(futs) < MAX_PARALLEL:
                        started.add(c["id"])
                        self.card_update(c["id"], state="queued", status="queued")
                        futs[ex.submit(self.process_card, c)] = c["id"]
                self.stage(stage_key, "active", f"{len(merged)}/{len(by)} merged")
                if not futs:
                    break
                done, _ = wait(futs, return_when=FIRST_COMPLETED)
                for f in done:
                    cid = futs.pop(f)
                    try:
                        f.result()
                        merged.add(cid)
                    except Exception as e:  # StageFailed or an unexpected crash: don't hang the others
                        failed[cid] = str(e)
                        if not isinstance(e, StageFailed):
                            self.card_block(cid, f"crashed: {e}")
        if failed or len(merged) < len(by):
            raise StageFailed("build failed: " + "; ".join(f"{k}: {v}" for k, v in failed.items()) if failed else "build stalled")

    def worktree(self, cid: str) -> Path:
        wt = self.app / ".worktrees" / cid
        with self.git_lock:
            if not wt.exists():
                rc, out = git(self.app, "worktree", "add", str(wt), "-b", f"card/{cid}", "main")
                if rc != 0:  # branch left over from an earlier attempt
                    rc, out = git(self.app, "worktree", "add", str(wt), f"card/{cid}")
                if rc != 0:
                    raise StageFailed(f"cannot create worktree: {out[-300:]}")
        return wt

    def process_card(self, spec: dict):
        cid = spec["id"]
        wt = self.worktree(cid)
        cdir = self.fdir / "cards" / cid
        cdir.mkdir(parents=True, exist_ok=True)
        feedback = ""
        last_err = ""
        for rnd in range(1, MAX_CARD_ROUNDS + 1):
            body = (f"Build card `{cid}`: {spec['title']}\nWorktree (work ONLY here): {posix(wt)}\nBranch: card/{cid}\n"
                    f"Owned files: {', '.join(spec['owned_files'])}\nACs: {', '.join(spec['acs']) or 'n/a'}\n"
                    f"Write your handoff to factory/cards/{cid}/handoff.json.\n\n{spec['body']}")
            if feedback:
                body += f"\n\nFEEDBACK (round {rnd}) — fix every point on the existing branch:\n{feedback}"
            hand = cdir / "handoff.json"
            hand.unlink(missing_ok=True)
            self.card_update(cid, status="building" if rnd == 1 else f"rebuilding (round {rnd})")

            def check_build():
                if not hand.exists():
                    return f"missing factory/cards/{cid}/handoff.json"
                rc, out = git(self.app, "rev-list", "--count", f"main..card/{cid}")
                if rc != 0 or int(out.strip() or 0) == 0:
                    return f"branch card/{cid} has no commits ahead of main; commit your work"
                rc, out = sh(self.test_cmd, wt, 900)
                if rc != 0:
                    return "the tests fail in your worktree:\n" + out[-1500:]
            try:
                self.run_agent_stage(cid, "sf-builder", self.prompt(cid, spec["title"], body), [], check_build, cwd=wt,
                                     tries=2 if rnd == 1 else 1, force_escalate=bool(spec.get("escalate")))
            except StageFailed as e:
                last_err = str(e)
                feedback = f"Driver check failed: {last_err}"
                self.card_update(cid, state="active", status="rework", error=None, reviews=self.card(cid)["reviews"] + 1)
                continue
            if not self.per_card_review:
                ok, out = self.merge(cid)
                if ok:
                    summ = json.loads(hand.read_text(encoding="utf-8")).get("summary", "") if hand.exists() else ""
                    self.card_done(cid, summ)
                    self.event("merged", cid, "merged into main")
                    return
                feedback = out
                self.card_update(cid, state="active", status="merge failed", reviews=self.card(cid)["reviews"] + 1)
                continue
            # review
            self.card_update(cid, status="in review")
            self.event("review_requested", cid, f"round {rnd}")
            rv = cdir / "review.json"
            rv.unlink(missing_ok=True)
            rbody = (f"Review card `{cid}`: {spec['title']}\nWorktree: {posix(wt)}\nBranch: card/{cid} (merge target: main)\n"
                     f"ACs: {', '.join(spec['acs'])}\nOwned files: {', '.join(spec['owned_files'])}\n"
                     f"Write your verdict to factory/cards/{cid}/review.json.")

            def check_review():
                try:
                    d = json.loads(rv.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as e:
                    return f"review.json invalid: {e}"
                return None if d.get("verdict") in ("approve", "changes") else "verdict must be 'approve' or 'changes'"
            rc_id = f"{cid}-review{rnd}"
            self.add_card(rc_id, f"Review: {spec['title']} (round {rnd})", "sf-reviewer", "review", "build")
            self.run_agent_stage(rc_id, "sf-reviewer", self.prompt(rc_id, spec["title"], rbody), [rv], check_review)
            verdict = json.loads(rv.read_text(encoding="utf-8"))
            self.card_done(rc_id, verdict["verdict"])
            if verdict["verdict"] == "changes" and rnd < MAX_CARD_ROUNDS:
                feedback = "Reviewer comments:\n" + "\n".join(verdict.get("comments") or ["(no details)"])
                self.card_update(cid, state="active", status="changes requested", reviews=self.card(cid)["reviews"] + 1)
                self.event("changes_requested", cid, f"round {rnd}")
                continue
            ok, out = self.merge(cid)
            if ok:
                resid = verdict.get("residual_risk") or []
                if verdict["verdict"] == "changes":
                    resid = ["merged after the review round limit; open comments: "] + (verdict.get("comments") or [])
                summ = json.loads(hand.read_text(encoding="utf-8")).get("summary", "") if hand.exists() else ""
                self.card_done(cid, summ + (f"  [residual: {'; '.join(resid)[:200]}]" if resid else ""))
                self.event("merged", cid, "merged into main")
                return
            feedback = out
            self.card_update(cid, state="active", status="merge failed", reviews=self.card(cid)["reviews"] + 1)
        self.card_block(cid, f"not merged after {MAX_CARD_ROUNDS} rounds. {last_err or feedback[:200]}")
        raise StageFailed(f"{cid}: not merged after {MAX_CARD_ROUNDS} rounds")

    def merge(self, cid: str) -> tuple[bool, str]:
        self.card_update(cid, status="merging")
        with self.git_lock:
            rc, out = git(self.app, "merge", "--no-ff", f"card/{cid}", "-m", f"Merge card/{cid}")
            if rc != 0:
                git(self.app, "merge", "--abort")
                return False, "Merge into main conflicted. Merge `main` into your branch (git merge main), resolve every conflict, rerun the tests, commit.\n" + out[-600:]
            rc, out = sh(self.test_cmd, self.app, 900)
            if rc != 0:
                git(self.app, "reset", "--hard", "HEAD~1")
                return False, "After merging, the tests on main FAIL, so the merge was undone. Fix the integration problem:\n" + out[-1500:]
            git(self.app, "worktree", "remove", "--force", str(self.app / ".worktrees" / cid))
        return True, ""

    # ---- QA

    # ---- docs

    # ---- whole pipeline

    def reset_unfinished(self):
        """After a crash/restart: anything that was mid-flight goes back to pending."""
        with self.lock:
            for c in self.state["cards"]:
                if c["state"] in ("active", "queued", "blocked", "owner"):
                    c.update(state="pending", status="", error=None)
            for k, s in self.state["stages"].items():
                if s["state"] in ("active", "blocked", "owner"):
                    s["state"] = "pending"
            self.state["gate"] = None
            self.save()


# --------------------------------------------------------------------------- run layout / CLI


def new_state(run_id: str, inputs: dict) -> dict:
    return {"run_id": run_id, "inputs": inputs, "idea": inputs.get("idea"), "title": None, "status": "created", "error": None,
            "rounds": 0, "round_no": 0, "feedback": None, "mode": None, "decision": None, "open_issues": [],
            "created_at": time.time(), "ended_at": None, "heartbeat": 0, "cost_usd": 0.0, "gate": None,
            "rail": [], "stages": {}, "cards": [], "events": [], "engine": engines.describe()}


PHOTO_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def create_run(inputs, runs_dir: Path = RUNS_DIR) -> Path:
    """Start a run. `inputs`: an idea string, or a dict with idea / spec / generate (at least one of them is required) and the
    optional extras: product_links, ref_links, photos (paths or {name, data: base64}), skills, force_mode."""
    if isinstance(inputs, str) or inputs is None:
        inputs = {"idea": inputs}
    inputs = dict(inputs)
    err = checks.check_inputs(inputs)
    if err:
        raise ValueError(err)
    rev_in = inputs.get("reverse") if isinstance(inputs.get("reverse"), dict) else None
    label = inputs.get("idea") or (rev_in or {}).get("target") or ("spec" if inputs.get("spec") else "ideas")
    run_id = _dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slug(label)
    rd = runs_dir / run_id
    for sub in ("factory", "holdout", "logs", "inputs/photos", "inputs/skills"):
        (rd / sub).mkdir(parents=True, exist_ok=True)
    clean = {"idea": (inputs.get("idea") or "").strip() or None, "generate": bool(inputs.get("generate")),
             "product_links": checks.clean_links(inputs.get("product_links")), "ref_links": checks.clean_links(inputs.get("ref_links")),
             "reverse": ({"target": str(rev_in["target"]).strip()[:600], "scope": rev_in.get("scope") if rev_in.get("scope") in ("feature", "app") else "feature",
                          "links": checks.clean_links(rev_in.get("links"))} if rev_in and str(rev_in.get("target") or "").strip() else None),
             "app_context": checks.clean_text(inputs.get("app_context"), 4000), "first_open": checks.clean_text(inputs.get("first_open"), 2000),
             "photos": [], "skills": [], "force_mode": inputs.get("force_mode") if inputs.get("force_mode") in ("single", "multi") else None,
             "quality": inputs.get("quality") if inputs.get("quality") in QUALITY_CHOICES else default_quality()}
    try:
        if inputs.get("budget_usd") not in (None, ""):
            clean["budget_usd"] = max(0.0, min(500.0, float(inputs["budget_usd"])))
    except (TypeError, ValueError):
        pass
    if clean["app_context"] or clean["first_open"]:   # the originals, word for word, for any agent that wants to re-read them
        (rd / "inputs" / "context.md").write_text(checks.context_markdown(clean["app_context"], clean["first_open"]), encoding="utf-8")
    if str(inputs.get("spec") or "").strip():
        (rd / "inputs" / "spec.md").write_text(str(inputs["spec"]).strip() + "\n", encoding="utf-8")
    for i, ph in enumerate((inputs.get("photos") or [])[:8], 1):
        try:
            if isinstance(ph, dict):
                name, raw = ph.get("name", f"photo{i}.png"), base64.b64decode(ph.get("data", ""))
            else:
                src = Path(ph)
                name, raw = src.name, src.read_bytes()
        except (OSError, ValueError):
            continue
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:60] or f"photo{i}.png"
        if Path(name).suffix.lower() not in PHOTO_EXT or len(raw) > 8_000_000:
            continue
        (rd / "inputs" / "photos" / name).write_bytes(raw)
        clean["photos"].append(name)
    missing = []
    for sk in inputs.get("skills") or []:
        sk = str(sk).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", sk):
            continue
        if checks.find_skill(sk):
            clean["skills"].append(sk)  # only a hint: the skill agent decides whether it fits the project
        else:
            missing.append(sk)
    checks.copy_always(rd / "inputs" / "always-use", checks.ALWAYS_DIR)
    state = new_state(run_id, clean)
    state["missing_skills"] = missing
    (rd / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    return rd


def load_state(rd: Path) -> dict:
    """Read state.json; the driver swaps it atomically, which on Windows can briefly deny readers."""
    last: Exception = FileNotFoundError(rd / "state.json")
    for _ in range(40):
        try:
            return json.loads((rd / "state.json").read_text(encoding="utf-8"))
        except (PermissionError, json.JSONDecodeError) as e:
            last = e
            time.sleep(0.05)
    raise last


def write_decision(rd: Path, decision: str, feedback: str = "", note: str = "", index: Optional[int] = None):
    """Used by the CLI and the dashboard: tell the waiting driver what the owner decided."""
    gate = load_state(rd).get("gate")
    if not gate:
        raise SystemExit("this run is not waiting on you")
    if decision not in gate.get("accept", ["approve", "reject"]):
        raise SystemExit(f"this gate accepts: {', '.join(gate.get('accept', []))}")
    payload = {"gate": gate["id"], "decision": decision, "feedback": feedback, "note": note}
    if index is not None:
        payload["index"] = index
    (rd / "decision.json").write_text(json.dumps(payload), encoding="utf-8")


def find_run(name: Optional[str], runs_dir: Path = RUNS_DIR) -> Path:
    runs = sorted((p for p in runs_dir.glob("*") if (p / "state.json").exists()), reverse=True)
    if not runs:
        raise SystemExit("no runs yet: uv run src/factory.py new")
    if not name:
        return runs[0]
    for p in runs:
        if p.name == name or p.name.startswith(name):
            return p
    raise SystemExit(f"no run matching {name!r}")


def ping_engine(eng: "engines.Engine", model: str, effort: str, tmp: Path) -> tuple[bool, str]:
    """Ask a non-Claude engine for 'pong' through the same command the stations use."""
    pfile = tmp / "ping.md"
    pfile.write_text(engines.full_prompt("You are being tested by the Software Factory's setup check.", "Reply with exactly: pong"), encoding="utf-8")
    try:
        argv, stdin_text = eng.command(model, effort, tmp, tmp, False, pfile)
        p = subprocess.run(argv, cwd=str(tmp), input=stdin_text, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300, env=_agent_env(tmp), creationflags=NO_WINDOW)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    parser = eng.parser()
    for line in (p.stdout or "").splitlines():
        parser.feed(line)
    parser.finish(p.returncode)
    said = (parser.text or "") + "\n" + (p.stdout or "")
    good = parser.ok and "pong" in said.lower()
    return good, "" if good else (parser.error or (p.stdout or "") + (p.stderr or "")).strip()


def cmd_check() -> int:
    ok = True
    eng = engines.current()
    print(f"engine: {engines.describe()['label']} (config: {engines.config_path()})")
    for tool in ("git", "uv"):
        path = shutil.which(tool)
        print(f"{'ok ' if path else 'MISSING'} {tool}: {path or ''}")
        ok &= bool(path)
    exe = shutil.which("claude") if eng is None else eng.exe()
    print(f"{'ok ' if exe else 'MISSING'} {'claude' if eng is None else eng.label}: {exe or ''}")
    ok &= bool(exe)
    roles = [load_role(n) for n in ROLES]
    combos = sorted({(r.model, r.effort) for r in roles} | {(r.retry_model, r.retry_effort or r.effort) for r in roles if r.retry_model} |
                    {pair for r in roles for pair in (r.presets or {}).values()})
    tmp = RUNS_DIR / "_check"
    tmp.mkdir(parents=True, exist_ok=True)
    if eng is not None:  # other engines: one ping per distinct model + effort after mapping
        for model, effort in sorted({(eng.model_for(m), e if eng.uses_effort else "medium") for m, e in combos}):
            good, why = ping_engine(eng, model, effort, tmp)
            print(f"{'ok ' if good else 'FAIL'} {model or 'default model'} @ {effort}" + ("" if good else f": {why[:160]}"))
            ok &= good
            if not good:
                break  # the same problem would repeat for every pair
        return 0 if ok else 1
    for model, effort in combos:
        rc, out = sh([shutil.which("claude") or "claude", "-p", "Reply with exactly: pong", "--model", model, "--effort", effort,
                      "--setting-sources", "project", "--no-session-persistence"], tmp, 120, _agent_env(tmp))
        good = rc == 0 and "pong" in out.lower()
        print(f"{'ok ' if good else 'FAIL'} {model} @ {effort}" + ("" if good else f": {out.strip()[:120]}"))
        ok &= good
    return 0 if ok else 1


def cmd_status(name: Optional[str]) -> None:
    rd = find_run(name)
    s = load_state(rd)
    print(f"{s['run_id']}  [{s['status']}]  {s.get('title') or ''}  ${s['cost_usd']:.2f}")
    for c in s["cards"]:
        print(f"  {c['state']:8} {c['id']:22} {c.get('role') or 'owner':12} {c.get('model') or '':20} {c['title'][:50]}")
    if s.get("gate"):
        g = s["gate"]
        print(f"\nWAITING ON YOU: {g['title']}" + (f" -> read factory/{g['doc']}" if g.get("doc") else "") + f"\n  decisions: {', '.join(g.get('accept', []))}")
        for i, o in enumerate(g.get("options") or []):
            print(f"  [{i}] {o['title']}: {o['pitch']}")
    if s.get("error"):
        print(f"\nERROR: {s['error']}")


def cmd_deploy(name: str, target: Optional[str], list_only: bool) -> int:
    import deploy
    rd = find_run(name)
    if list_only or not target:
        app_json, _ = checks.read_app_json(rd / "factory" / "app.json", rd / "app")
        for o in deploy.options_for(app_json, deploy.detect_connectors()):
            print(f"{o['id']:13} {'ready' if o['available'] else 'not available':14} {o['title']}: " + (f"via {o['via']}" if o["available"] else o["reason"]))
        if not target:
            print("\nchoose one with: uv run src/factory.py deploy <run> --target <name>")
        return 0
    try:
        entry = deploy.deploy_run(rd, target)
    except deploy.DeployError as e:
        print(f"cannot deploy: {e}")
        return 2
    print(f"{entry['status']}: {entry['summary']}")
    for k in ("url", "path"):
        if entry[k]:
            print(f"  {k}: {entry[k]}")
    for step in entry["next_steps"]:
        print(f"  - {step}")
    return 0 if entry["status"] in ("deployed", "built") else 1


def read_metrics(path: Optional[Path] = None) -> list[dict]:
    path = path or (RUNS_DIR / "metrics.jsonl")
    rows = []
    for line in (path.read_text(encoding="utf-8").splitlines() if path.exists() else []):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def cmd_stats() -> None:
    rows = read_metrics()
    if not rows:
        print("no finished runs recorded yet")
        return
    print(f"{'run':44} {'path':6} {'outcome':10} {'$':>7} {'min':>5} {'fix rnds':>8} {'open issues':>11}")
    for r in rows:
        print(f"{r['run_id'][:44]:44} {r.get('mode') or '-':6} {r['outcome']:10} {r['cost_usd']:7.2f} {r['minutes']:5.0f} {r['fix_rounds']:8} {r['open_issues']:11}")
    ok = [r for r in rows if r["outcome"] == "done"]
    for mode in ("single", "multi"):
        m = [r for r in ok if r.get("mode") == mode]
        if m:
            clean = sum(1 for r in m if r["open_issues"] == 0)
            print(f"{mode}: {len(m)} finished, avg ${sum(r['cost_usd'] for r in m) / len(m):.2f}, avg {sum(r['minutes'] for r in m) / len(m):.0f} min, {clean} with no open issues")


def main(argv=None) -> int:
    common.migrate_legacy_layout()   # older installs kept runs/ next to the code
    ap = argparse.ArgumentParser(prog="factory.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    n = sub.add_parser("new", help="start a run (needs an idea, a spec, or --generate)")
    n.add_argument("--idea")
    n.add_argument("--spec-file", help="a file with a whole spec")
    n.add_argument("--generate", action="store_true", help="generate ideas, then choose one or random")
    n.add_argument("--reverse", help="a feature or app from another product to recreate (built clean-room from public information)")
    n.add_argument("--reverse-scope", choices=["feature", "app"], default="feature")
    n.add_argument("--reverse-link", action="append", default=[], help="a link to learn about the target from (repeatable)")
    n.add_argument("--link", action="append", default=[], help="a link to a product like it (repeatable)")
    n.add_argument("--ref", action="append", default=[], help="a reference link (repeatable)")
    n.add_argument("--photo", action="append", default=[], help="a design reference photo (repeatable)")
    n.add_argument("--skill", action="append", default=[], help="a skill to use (repeatable)")
    n.add_argument("--context", help="background on the app: who it is for, what it is for, anything it must respect")
    n.add_argument("--first-open", help="what the app's user should see and do the first time they open it (e.g. a welcome screen, a short set-up)")
    n.add_argument("--mode", choices=["single", "multi"], help="skip the decider's choice")
    n.add_argument("--quality", choices=list(QUALITY_CHOICES), default=None,
                   help="lean (cheapest, for experiments), balanced or max (strongest models everywhere); default: your setup choice")
    n.add_argument("--budget", type=float, help="spending cap in dollars (0 = no cap); default 10 single / 45 full pipeline")
    sub.add_parser("stats", help="cost, time and outcome of every past run")
    dp = sub.add_parser("deploy", help="publish or package a finished run: netlify, vercel, github-pages or desktop")
    dp.add_argument("run")
    dp.add_argument("--target", choices=["netlify", "vercel", "github-pages", "desktop", "docker", "render", "installer", "package", "expo", "godot-export"])
    dp.add_argument("--list", action="store_true", help="show what can be deployed and how")
    r = sub.add_parser("run")
    r.add_argument("run")
    st = sub.add_parser("status")
    st.add_argument("run", nargs="?")
    a = sub.add_parser("approve")
    a.add_argument("run")
    a.add_argument("--note", default="")
    rj = sub.add_parser("reject")
    rj.add_argument("run")
    rj.add_argument("feedback")
    d = sub.add_parser("decide", help="answer a gate: choose / random / switch / improve / tweak / big / escalate ...")
    d.add_argument("run")
    d.add_argument("decision")
    d.add_argument("--index", type=int)
    d.add_argument("--feedback", default="")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        return cmd_check()
    if args.cmd == "status":
        cmd_status(args.run)
        return 0
    if args.cmd == "stats":
        cmd_stats()
        return 0
    if args.cmd == "deploy":
        return cmd_deploy(args.run, args.target, args.list)
    if args.cmd in ("approve", "reject"):
        write_decision(find_run(args.run), args.cmd, getattr(args, "feedback", ""), getattr(args, "note", ""))
        print("decision recorded; the driver picks it up within a second")
        return 0
    if args.cmd == "decide":
        write_decision(find_run(args.run), args.decision, args.feedback, index=args.index)
        print("decision recorded; the driver picks it up within a second")
        return 0
    if args.cmd == "new":
        spec = Path(args.spec_file).read_text(encoding="utf-8") if args.spec_file else None
        try:
            rd = create_run({"idea": args.idea, "spec": spec, "generate": args.generate, "reverse": ({"target": args.reverse, "scope": args.reverse_scope, "links": args.reverse_link} if args.reverse else None),
                             "product_links": args.link, "ref_links": args.ref,
                             "photos": args.photo, "skills": args.skill, "app_context": args.context, "first_open": args.first_open, "force_mode": args.mode, "budget_usd": args.budget, "quality": args.quality})
        except ValueError as e:
            print(f"cannot start: {e}")
            return 2
    else:
        rd = find_run(args.run)
    print(f"run {rd.name}\nwatch it on the dashboard: uv run --no-project python scripts/sf.py dashboard open", flush=True)
    try:
        Run(rd).execute()
    except StageFailed as e:
        print(f"\nstopped: {e}\nresume with: uv run src/factory.py run {rd.name}")
        return 1
    print(f"\ndone. product: {rd / 'app'}\nreport: {rd / 'factory' / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
