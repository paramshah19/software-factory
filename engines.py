"""Which coding agent runs the factory's stations.

The factory was built on Claude Code, and that is still the default and the most complete engine. Two more are
supported so people can use the agent they already pay for:

- ``claude``: Claude Code (`claude -p`). Per-station tool allowlists, hidden tests blocked by permission rules,
  cost in dollars. Handled directly in factory.py (claude_cmd / format_event), not here.
- ``codex``: OpenAI Codex CLI (`codex exec --json`). Sandboxed to the run folder, but no per-tool allowlist, so the
  hidden tests are protected by instruction only. Reports tokens, not dollars, so the spending cap cannot apply.
- ``custom``: any agent that has a headless (non-interactive) command line: Gemini CLI, Grok CLI, Qwen Code,
  Cursor's agent, Aider... You describe the command once in the config. Its output is shown line by line on the
  dashboard and the exit code decides success. No allowlist and no cost.

The choice lives in ``~/.software-factory/config.json`` (or the file named by the SF_CONFIG environment variable).
``scripts/sf.py configure`` writes it. Example::

    {"engine": "codex", "models": {"strong": "", "standard": "", "fast": ""}}
    {"engine": "custom",
     "custom": {"label": "Gemini CLI", "command": ["gemini", "--yolo"], "prompt": "stdin", "model_args": ["-m", "{model}"]}}

Each station's file in agents/ names a Claude model. Other engines map it to a tier: Opus = ``strong``,
Sonnet = ``standard``, Haiku = ``fast``. An empty tier means "use the agent's own default model".
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path
from typing import Optional

CONFIG_ENV = "SF_CONFIG"
ENGINES = ("claude", "codex", "custom")
CODEX_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV) or (Path.home() / ".software-factory" / "config.json"))


def load_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def engine_name(cfg: Optional[dict] = None) -> str:
    name = str((cfg if cfg is not None else load_config()).get("engine") or "claude").lower()
    return name if name in ENGINES else "claude"


def tier(model: str) -> str:
    m = (model or "").lower()
    return "strong" if "opus" in m else "fast" if "haiku" in m else "standard"


def host_rule() -> str:
    """The operating-system line of the agents' shared protocol."""
    system = platform.system()
    if system == "Windows":
        return "Windows host. Use forward-slash paths like `C:/Users/...`. Prefer `git -C <path>` over `cd`."
    name = "macOS" if system == "Darwin" else "Linux" if system == "Linux" else system or "Unix-like"
    return f"{name} host. Use absolute paths like `/home/...` or `/Users/...`. Prefer `git -C <path>` over `cd`."


def full_prompt(system_text: str, prompt: str) -> str:
    """For agents without a 'system prompt' option: the standing instructions and the task in one message."""
    return ("# Your standing instructions (follow them for the whole task)\n\n" + system_text.strip() +
            "\n\n---\n\n# Your task\n\n" + prompt.strip() + "\n")


class Engine:
    name = "?"
    label = "?"
    reports_cost = False
    enforces_permissions = False
    uses_effort = True

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg if cfg is not None else load_config()

    def exe(self) -> Optional[str]:
        raise NotImplementedError

    def model_for(self, claude_model: str) -> str:
        """The model this engine should use for a station that asks for `claude_model` ('' = the agent's default).
        A name that isn't a Claude model is the owner's own choice for this engine (an override): used as it is."""
        if claude_model and not claude_model.startswith("claude-"):
            return claude_model
        return str((self.cfg.get("models") or {}).get(tier(claude_model)) or "").strip()

    def model_label(self, claude_model: str) -> str:
        return self.model_for(claude_model) or f"{self.label} default ({tier(claude_model)})"

    def command(self, model: str, effort: str, cwd: Path, run_dir: Path, web: bool, prompt_file: Path) -> tuple[list[str], Optional[str]]:
        """(argv, text for stdin or None). `prompt_file` already holds the full prompt."""
        raise NotImplementedError

    def parser(self, run_dir: str = "") -> "Parser":
        raise NotImplementedError


class Parser:
    """Turns the agent's stdout, line by line, into readable log lines and, at the end, a result."""

    def __init__(self, run_dir: str = ""):
        self.run_dir = run_dir
        self.ok = False
        self.text = ""
        self.error = ""
        self.cost = 0.0

    def short(self, s: str, n: int = 200) -> str:
        s = str(s or "").replace("\\", "/")
        if self.run_dir:
            s = s.replace(self.run_dir, "<run>")
        return " ".join(s.split())[:n]

    def feed(self, line: str) -> list[str]:
        raise NotImplementedError

    def finish(self, returncode: int) -> None:
        pass


# --------------------------------------------------------------------------- Codex


class CodexParser(Parser):
    def __init__(self, run_dir: str = ""):
        super().__init__(run_dir)
        self.done = False
        self.tokens = 0

    @staticmethod
    def _message(raw) -> str:
        """Codex wraps API errors as JSON inside the message string; dig out the human part."""
        text = str(raw or "")
        for _ in range(3):
            try:
                inner = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                break
            if isinstance(inner, dict):
                text = str((inner.get("error") or {}).get("message") if isinstance(inner.get("error"), dict) else inner.get("message") or text)
            else:
                break
        return text

    def feed(self, line: str) -> list[str]:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return [self.short(line, 260)] if line.strip() else []
        if not isinstance(ev, dict):
            return []
        t, out = ev.get("type"), []
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if t == "item.completed":
            kind = item.get("type")
            if kind == "agent_message" and str(item.get("text", "")).strip():
                self.text = str(item["text"])
                out.append("… " + self.short(item["text"], 260))
            elif kind == "command_execution":
                out.append("▸ Shell " + self.short(item.get("command"), 140))
                if item.get("exit_code") not in (None, 0):
                    out.append(f"✗ exit {item.get('exit_code')}: " + self.short(item.get("aggregated_output"), 200))
            elif kind == "file_change":
                for ch in item.get("changes") or []:
                    if isinstance(ch, dict):
                        out.append(f"▸ {str(ch.get('kind') or 'edit').title()} " + self.short(ch.get("path"), 140))
            elif kind == "web_search":
                out.append("▸ WebSearch " + self.short(item.get("query"), 140))
            elif kind == "mcp_tool_call":
                out.append(f"▸ {item.get('server')}.{item.get('tool')}")
            elif kind == "error":
                out.append("! " + self.short(item.get("message"), 200))
        elif t == "turn.completed":
            self.done, self.ok = True, True
            usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            self.tokens += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            out.append(f"■ finished · {self.tokens:,} tokens (Codex reports tokens, not dollars)")
        elif t == "turn.failed":
            self.done, self.ok = True, False
            self.error = self._message((ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else ev.get("error"))
        elif t == "error":
            self.error = self._message(ev.get("message"))
        return out

    def finish(self, returncode: int) -> None:
        if not self.done:  # no turn.completed: crashed, killed or timed out
            self.ok = False
            self.error = self.error or f"codex exited with code {returncode} before finishing"
        elif not self.ok and not self.error:
            self.error = "codex reported the turn as failed"


class Codex(Engine):
    name, label = "codex", "Codex"

    def exe(self) -> Optional[str]:
        return shutil.which("codex")

    def command(self, model, effort, cwd, run_dir, web, prompt_file):
        argv = [self.exe() or "codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-s", "workspace-write",
                "-C", Path(cwd).as_posix(), "--add-dir", Path(run_dir).as_posix(),
                "-c", "sandbox_workspace_write.network_access=true",  # uv and pip need to download packages
                "-c", f'model_reasoning_effort="{CODEX_EFFORT.get(effort, "medium")}"']
        if model:
            argv += ["-m", model]
        if web:
            argv += ["-c", 'web_search="live"']
        return argv + ["-"], Path(prompt_file).read_text(encoding="utf-8")

    def parser(self, run_dir: str = "") -> Parser:
        return CodexParser(run_dir)


# --------------------------------------------------------------------------- any other agent


class CustomParser(Parser):
    def __init__(self, run_dir: str = ""):
        super().__init__(run_dir)
        self.tail: list[str] = []

    def feed(self, line: str) -> list[str]:
        line = line.rstrip()
        if not line.strip():
            return []
        self.tail = (self.tail + [line])[-80:]
        return ["  " + self.short(line, 260)]

    def finish(self, returncode: int) -> None:
        self.text = "\n".join(self.tail)[-4000:]
        self.ok = returncode == 0
        if not self.ok:
            self.error = (f"exit code {returncode}: " + "\n".join(self.tail[-6:]))[-600:]


class Custom(Engine):
    name = "custom"

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)
        self.spec = self.cfg.get("custom") if isinstance(self.cfg.get("custom"), dict) else {}
        self.label = str(self.spec.get("label") or "custom agent")
        self.uses_effort = bool(self.spec.get("effort_args"))

    def exe(self) -> Optional[str]:
        cmd = self.spec.get("command") or []
        return shutil.which(str(cmd[0])) if cmd else None

    def command(self, model, effort, cwd, run_dir, web, prompt_file):
        text = Path(prompt_file).read_text(encoding="utf-8")
        values = {"model": model, "effort": effort, "cwd": Path(cwd).as_posix(), "run_dir": Path(run_dir).as_posix(),
                  "prompt_file": Path(prompt_file).as_posix(), "prompt": text}
        fill = lambda parts: [str(p).format(**values) for p in parts]
        cmd = [str(c) for c in (self.spec.get("command") or [])]
        if not cmd:
            raise ValueError("the custom engine has no command: run scripts/sf.py configure")
        argv = [self.exe() or cmd[0], *fill(cmd[1:])]
        if model and self.spec.get("model_args"):
            argv += fill(self.spec["model_args"])
        if self.spec.get("effort_args"):
            argv += fill(self.spec["effort_args"])
        mode = self.spec.get("prompt", "stdin")
        if mode == "arg" and "{prompt}" not in " ".join(cmd):
            argv.append(text)
        return argv, (text if mode == "stdin" else None)

    def parser(self, run_dir: str = "") -> Parser:
        return CustomParser(run_dir)


def current(cfg: Optional[dict] = None) -> Optional[Engine]:
    """The configured non-Claude engine, or None when Claude Code runs the stations."""
    cfg = cfg if cfg is not None else load_config()
    return {"codex": Codex, "custom": Custom}.get(engine_name(cfg), lambda c: None)(cfg)


def describe(cfg: Optional[dict] = None) -> dict:
    """What the dashboard and `check` say about the engine."""
    cfg = cfg if cfg is not None else load_config()
    eng = current(cfg)
    if eng is None:
        return {"name": "claude", "label": "Claude Code", "reports_cost": True, "enforces_permissions": True}
    return {"name": eng.name, "label": eng.label, "reports_cost": eng.reports_cost, "enforces_permissions": eng.enforces_permissions}
