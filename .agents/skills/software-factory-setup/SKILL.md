---
name: software-factory-setup
description: One-time setup of the Software Factory (a pipeline of AI agents that turns an idea into a tested product, with a live dashboard) on whatever coding agent the user runs (Claude Code, Codex, Gemini CLI, Grok, opencode or any agent with a non-interactive command line). Welcomes the user, explains what it is, asks a few questions to personalise it (which agent does the work, models and effort per agent, quality, spending caps, brand rules), then shows how to run it. Use when the user says "set up the software factory", "install the software factory", runs /software-factory-setup, or has just cloned the software-factory repo.
---

# Software Factory setup

You are setting up the Software Factory for the user, in four parts: **1. Welcome, 2. What it is, 3. Personalise it, 4. How to run it.** The user may not be technical: plain words, short messages, no jargon without a one-line explanation. Do the technical work yourself, quietly, and only tell the user what matters to them. Never claim a step worked unless its command said so.

`SF` below means: `uv run --no-project python "<FACTORY>/scripts/sf.py"`, where `<FACTORY>` is the factory folder (absolute path, forward slashes). Every `SF` command accepts `--json`.

---

## Part 1: Welcome

Greet the user in two or three warm, short sentences: they are about to set up their own software factory, it takes about ten minutes, and you will ask a few questions so it works the way they like.

Meanwhile, behind the scenes:
1. **Find the factory.** If the current folder (or a parent) has `src/factory.py` and `scripts/sf.py`, that is `<FACTORY>`. Otherwise clone it: `git clone https://github.com/paramshah19/software-factory "<HOME>/software-factory"` (`<HOME>` = the user's home folder) and tell them where it went.
2. **Check the computer:** `SF doctor --json`. If `git` or `uv` is missing, show the user the `install_steps` from the output, ask them to run them (installing tools is their call), run `doctor` again, and don't continue without both. Keep the rest of the output for Parts 2 and 3: the operating system, the agent command lines found (`agent_command_lines`) and what this computer can build (`can_build`).

## Part 2: What it is

Explain it in under 12 short lines, in your own words, using what `doctor` found:
- **An idea in, a tested product out.** They describe what they want; a team of about 20 specialist AI agents (researcher, spec writer, designer, planner, builders, reviewers, security checker, report writer...) each do one job.
- **It doesn't take the agents' word for it.** A driver program checks every step itself with real tests, a real browser and screenshots, and sends work back until it passes.
- **A live dashboard** shows every agent at work. Nothing opens terminal windows. When the factory needs a decision (pick an idea, approve the plan, review the app), it shows up under **Needs you**.
- **What it can build on this computer**: list the kinds `can_build` says "yes" to, in plain words (websites and web apps, command-line tools, APIs, bots, libraries, systems of several services, phone apps, desktop apps, Godot games). For each kind it can't build yet, one line on what it needs (phone apps: Node.js; Godot games: Godot 4), and that installing it is optional.
- **Costs**: it runs on their own agent's plan. A small app is a few dollars of usage, a big one more; they choose how much quality to buy (Part 3).

## Part 3: Personalise it

First run `SF agents` so you can show the agents. Then ask ALL the questions in ONE message, numbered, each with its default in [brackets], and tell the user they can answer "defaults" or only the numbers they want to change:

1. **Which agent does the work?** [the one you are, if `doctor` found its command line]. List the other agents `doctor` found. Say in one line that Claude Code is the most complete (cost in dollars, a spending cap, and safety limits per agent); on others, cost shows as "not reported" and the safety limits are weaker.
2. **Default quality of a run** [Balanced]: **Lean** (cheapest, for experiments), **Balanced** (strong where it matters), **Max** (strongest models everywhere, costs most). They can still pick per run.
3. **Spending cap per run** [single-agent $10, full pipeline $45; 0 = no cap]. Only meaningful with Claude Code.
4. **Models and effort per agent** [keep the defaults]. Show `SF agents` as a short table (agent, job, model, effort). Explain: **Model** on Claude Code is `claude-opus-5-5` (strongest, about twice the price of Sonnet), `claude-sonnet-5-5` (the everyday default) or `claude-haiku-5-5` (fastest, cheapest, for simple jobs); on another agent, its own model names. **Effort** is `low`, `medium`, `high`, `xhigh` or `max`: more thinking, better on hard work, slower and costlier. They can answer like "planner: opus, high; writer: haiku".
5. **Brand rules every app should follow** [none]: colours, fonts, tone of voice, anything they always want.
6. **Desktop notifications when a run needs you** [on].

Then apply the answers, telling the user briefly what you're doing:
- **Engine** (question 1):
  - Claude Code → `SF configure --engine claude --host claude-code`
  - Codex → `SF configure --engine codex --host codex`. Codex picks its own model; if the check below says a model isn't supported, ask which Codex models their plan has and add `--models "strong=<best>,standard=<everyday>,fast=<cheapest>"`.
  - Any other agent → `custom`. Work out YOUR OWN non-interactive command: run `<your-cli> --help` and find the mode that takes a prompt, runs without asking for approval, can edit files and run shell commands in its working folder, and exits when done. For example: `SF configure --engine custom --host other --label "Gemini CLI" --command '["gemini", "--yolo"]' --prompt stdin --model-args '["-m", "{model}"]'` (`--prompt stdin`, `arg`, or `file` with `{prompt_file}` in the command; placeholders: `{model}`, `{effort}`, `{cwd}`, `{run_dir}`, `{prompt_file}`, `{prompt}`; use `--host gemini` for Gemini CLI).
- **Defaults** (questions 2, 3, 6): `SF configure --quality <q> --budget-single <n> --budget-multi <n> --notify <on|off>` (only the options they changed).
- **Agents** (question 4): `SF agents set <agent> --model <model> --effort <effort>` for each change (either option alone is fine; `SF agents reset <agent|all>` to undo).
- **Brand** (question 5): write their rules to a temporary text file in their words, then `SF configure --brand-file <that file>`. It becomes a document every agent of every run reads.
- **Install the factory's parts:** `SF prepare` (Python packages, plus a browser for screenshot checks if they have no Edge or Chrome: about 150 MB, once). It can take a few minutes.
- **Prove every agent answers:** tell them "this sends one tiny test message per model and effort level, using a little of your plan", then `SF check`. Every line must say `ok`. For a `FAIL`, fix the cause (wrong model name → `SF agents set` or `--models`; wrong command → `--command`; not signed in → ask them to sign in to their agent themselves) and run it again.
- **Install the skills:** `SF install-skills` (add `--dir "<your skills folder>"` if it says your agent's folder is unknown). This also writes the shortcut `<HOME>/.software-factory/sf.py` that the idea skill uses.
- **Publishing options:** `SF connections`. Note which ways of putting apps online are ready (their Netlify, Vercel, GitHub or Render accounts and connectors). Never type, ask for, read or print a password, token or key; signing in is always theirs.

End Part 3 with a short summary of their factory: the agent doing the work, default quality, spending caps, any agents they changed, brand rules saved or not, and notifications on or off.

## Part 4: How to run it

1. Start the dashboard: `SF start`. It runs in the background (no terminal window) and opens as its own app window at the address it prints (`--browser` for their normal browser).
2. Show them how to use it, briefly:
   - **Start something:** `/software-factory-idea <your idea>` from any folder (or the **New run** button on the dashboard). For example: `/software-factory-idea a habit tracker that shows honest streaks`. Add "cheap" for a Lean run or "best possible" for Max. They can also give links to products they like, design photos, a whole spec, say "give me ideas", or "recreate this feature from that product".
   - **Watch it:** the dashboard opens at the run. **Show live output** shows each agent at work; **Factory console** shows the factory itself.
   - **Decide:** decisions appear under **Needs you** (the tab title changes too). They can also just answer in chat, like "approve it".
   - **Outside services:** if an app uses an AI model, payments (test mode) or email, it works in demo mode first. They add real keys later on the dashboard's **Your app** panel, never in the chat.
   - **Put it online:** after they approve a finished app, the **Deploy** panel offers what fits it, using their own accounts.
   - **Start it again later, one click:** double-click `Start Software Factory.cmd` (Windows) or `Start Software Factory.command` (macOS) in the factory folder, no terminal needed. Ask whether they want to open it by typing its name (Start menu, Spotlight or app menu); only if they say yes, run `SF install-app` and tell them the one line it prints. A Desktop icon is `SF shortcut`, also only if they ask.
   - **On your phone:** the **Phone & power** button turns on the phone view (scan the QR code, same Wi-Fi, PIN protected, off by default). The phone can watch and answer questions, not start runs, add keys or deploy. The same panel has **Keep my computer awake** so a run (and the phone view) isn't cut off by sleep. Only turn these on if they want them. Also mention the **Connections** button: it lists the connectors and skills their own agent already has, with every connector off for the factory until they choose (never switch any on for them). Also offer, without turning anything on yet: phone notifications (`SF push on`, a buzz when a run needs them), a daily spending cap (`SF configure --daily-cap N`), and `SF clean` later to free disk space.
   - **About their app:** on **New run** they can write the app's context and what a brand-new user should see and do the first time they open it; the factory builds and checks the app against that.
   - **Change any setting later:** run `/software-factory-setup` again (it keeps what they chose unless they change it).
3. Finish with one line: **What I did NOT verify:** anything that failed or was skipped during setup. Write "nothing" only if every step passed.
