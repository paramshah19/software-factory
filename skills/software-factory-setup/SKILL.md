---
name: software-factory-setup
description: One-time setup of the Software Factory (a pipeline of AI agents that turns an idea into a tested app, with a live dashboard) on whatever coding agent the user runs (Claude Code, Codex, Gemini CLI, Grok, opencode or any agent with a non-interactive command line). Use when the user says "set up the software factory", "install the software factory", runs /software-factory-setup, or has just cloned the software-factory repo.
---

# Software Factory setup

You are setting up the Software Factory for the user, on the agent you are running in. When you finish, the user has: the factory's 20 agents checked and working on their agent, the dashboard running, and the `/software-factory-idea` skill installed. Do the steps in order. Explain what you are doing in plain words (the user may not be technical). Never claim a step worked unless its command said so.

`SF` below means: `uv run --no-project python "<FACTORY>/scripts/sf.py"`, where `<FACTORY>` is the factory folder (absolute path, forward slashes). Every `SF` command accepts `--json`.

## 1. Find or get the factory
- If the current folder (or a parent) contains `factory.py` and `scripts/sf.py`, that is `<FACTORY>`.
- Otherwise clone it: `git clone https://github.com/paramshah19/software-factory "<HOME>/software-factory"` (`<HOME>` = the user's home folder). Tell the user where it went.

## 2. Check the machine
Run `SF doctor --json`.
- `git` or `uv` missing: show the user the `install_steps` from the output and ask them to run them (installing system tools is their call), then run `doctor` again. Don't continue without both.
- Note the operating system and which agent command lines were found.
- Show what this computer can build (`can_build`): web pages and web apps, command-line tools, APIs, bots and background workers, libraries, multi-service systems, phone apps, desktop apps and Godot games. For each one marked as missing, say in one line what it needs (phone apps: Node.js; Godot games: Godot 4) and that installing it is optional and their call. The factory only offers the kinds this computer can build.

## 3. Choose the engine (the agent that runs the factory's stations)
Use the agent you are running in, unless the user asked for a different one:
- **You are Claude Code** → `SF configure --engine claude --host claude-code`
- **You are Codex** → `SF configure --engine codex --host codex`. Codex picks its own model by default. If `check` (step 6) says the model is not supported, ask the user which Codex models their plan has and set them: `--models "strong=<best>,standard=<everyday>,fast=<cheapest>"`.
- **Any other agent** (Gemini CLI, Grok, opencode, Qwen Code, Cursor's agent, Aider...) → `custom`. Work out YOUR OWN non-interactive command: run `<your-cli> --help` and find the mode that takes a prompt, works without asking for approval, can read/write files and run shell commands in its working folder, and exits when done. Then, for example:
  `SF configure --engine custom --host other --label "Gemini CLI" --command '["gemini", "--yolo"]' --prompt stdin --model-args '["-m", "{model}"]'`
  - `--prompt stdin` (the prompt is piped in), `arg` (added as the last argument) or `file` (put `{prompt_file}` inside `--command`).
  - Placeholders you can use inside the lists: `{model}`, `{effort}`, `{cwd}`, `{run_dir}`, `{prompt_file}`, `{prompt}`.
  - If your agent has known skills folders, use `--host gemini` (for Gemini CLI) or pass `--dir` in step 7.
- Tell the user, in one line, what this choice means: Claude Code is the most complete (each station has its own tool limits, and costs are shown in dollars). On Codex and other agents the stations are not limited per tool, the hidden tests are protected by instruction only, and cost shows as "not reported".

## 4. Install the factory's parts
Run `SF prepare` (installs the Python packages, plus a browser for the screenshot checks if the machine has no Edge or Chrome: about 150 MB, once). It can take a few minutes.

## 5. Show the agents, and let the user choose their models
Run `SF agents` and show the user the list as a table: each agent's job, model and effort on their engine. Explain in one line: each agent is a set of instructions in `agents/<name>.md`. The factory starts a fresh one for each step and checks its work.

Then ask ONE question: "These defaults balance cost and quality. Do you want to change the model or effort of any agent?" Explain the choices simply:
- **Model** (on Claude Code): `claude-opus-5-5` (strongest, about twice the price of Sonnet), `claude-sonnet-5-5` (the everyday default), `claude-haiku-5-5` (fastest and cheapest, for simple jobs). On another engine, use that agent's own model names.
- **Effort**: `low`, `medium`, `high`, `xhigh`, `max`. Higher means more thinking: better on hard work, slower, and more expensive.
- Each run also has a **Quality** choice (Lean / Balanced / Max) that shifts the defaults. An agent the user sets here keeps their choice in every Quality.

For each change the user asks for, run `SF agents set <agent> --model <model> --effort <effort>` (either option alone is fine). To undo: `SF agents reset <agent>`, or `SF agents reset all`. Show the final table. If they don't want changes, move on. Their choices are saved in their own config, so updating the factory never overwrites them, and they can rerun this command any time.

## 6. Prove every agent answers
Tell the user: "This sends one tiny test message per model and effort level, using a little of your plan." Then run `SF check`.
- Every line must say `ok`. For a `FAIL`, read the message, fix the cause (wrong model name → `configure --models`; wrong command → `configure --command`; not signed in → ask the user to sign in to their agent themselves) and run `check` again.
- Don't continue until it passes, unless the user says so. If they do, record it in your summary.

## 7. Install the skills
Run `SF install-skills` (add `--dir "<your skills folder>"` if your agent's skills folder is not known). It copies `software-factory-setup` and `software-factory-idea` into the agent's skills folder and writes the shortcut `<HOME>/.software-factory/sf.py`, which the idea skill uses. If it says the folder is unknown, find where your agent loads skills from and rerun it with `--dir`.

## 8. Connections (optional, for publishing apps later)
Run `SF connections` and explain the result simply: which publishing options are ready (Netlify, Vercel, GitHub Pages, a desktop app) and what the user would need to do for the others. Signing in is always the user's job. Never type, ask for, read or print a password, token or key.

## 9. Start the dashboard
Run `SF dashboard open`. It starts the dashboard in the background with no terminal window and opens it in the browser at `http://127.0.0.1:8787`. Tell the user: the factory's own output is under **Factory console**, and each agent's output under **Show live output**. Nothing opens a terminal window.

## 10. Hand off
Short summary for the user:
- What is set up (engine, number of agents checked, where the skills went, the dashboard address).
- How to use it: `/software-factory-idea <your idea>` (or however their agent invokes skills).
- **What I did NOT verify:** anything that failed or was skipped. Write "nothing" only if every step passed.
