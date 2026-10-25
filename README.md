# Software Factory

A pipeline of AI coding agents that turns an idea into a working, tested app on your own computer, with a live dashboard. It builds command-line tools, websites and web apps (with accounts, a database, test-mode payments and AI or other paid APIs), APIs, bots, libraries, multi-service systems, phone apps, desktop apps and Godot games. You type an idea; about 20 specialist agents (spec writer, designer, planner, builders, reviewers, security checker, report writer...) each do one job, and a driver program checks every step with real tests, a real browser and screenshots before the next one starts. You step in only at a few decision points ("Needs you").

It runs on **the coding agent you already use**: Claude Code, Codex, or any agent with a non-interactive command line (Gemini CLI, Grok, opencode, Qwen Code, Cursor's agent...). It works on Windows, macOS and Linux. Nothing opens a terminal window: the agents run hidden in the background, and their output is shown on the dashboard.

```
inputs ─► [generate ideas ─► you choose 1, or Random pick] ─► intake (brief.md) ─► decider (you can override)
   single agent: skill agent ─► maker (+ screenshot critique) ─► auto checks ─► security ─► report ─► YOU REVIEW (improve / escalate)
   full pipeline: research ─► spec ─► visual mock ─► YOU CHECK ─► [design skills ─► design plan] ∥ [technical skills ─► technical plan] ∥ hidden tests ─► final plan
                  ─► builders (parallel) ─► auto checks ─► 3 reviewers ─► fix rounds ─► report ─► YOU REVIEW (tweak / big change)
```

The full flowchart is in [`docs/flowchart.pdf`](docs/flowchart.pdf) ([PNG](docs/flowchart.png)).

## Quick start

You need: **git**, **uv** ([install](https://docs.astral.sh/uv/getting-started/installation/)) and a coding agent you are signed in to.

```bash
git clone https://github.com/paramshah19/software-factory
cd software-factory
```

Open your coding agent in that folder and run the setup skill:

```
/software-factory-setup
```

(Claude Code and Codex find the skill in this folder by themselves. In another agent, say: "read `skills/software-factory-setup/SKILL.md` and follow it".)

Setup has four parts:
1. **Welcome**, while it quietly checks your computer.
2. **What it is**, including what your computer can build.
3. **Personalise it**: which agent does the work, the default quality and spending caps, each agent's model and effort, brand rules every app follows, and notifications. Then it installs everything and proves every agent answers.
4. **How to run it**, with the dashboard open.

Then, from any folder:

```
/software-factory-idea a habit tracker that shows honest streaks
```

The run starts in the background and the dashboard opens at that run. It tells you when it needs you.

## Start it with one click
Once it is cloned you don't need a terminal. The first start installs what it needs (about a minute); after that it opens at once.

| Your computer | Do this |
|---|---|
| Windows | double-click `Start Software Factory.cmd` |
| macOS | double-click `Start Software Factory.command` (the first time: right-click, Open) |
| Linux | `bash "Start Software Factory.command"` |

The dashboard opens as **its own app window** (no tabs or address bar). To use your normal browser instead: `uv run --no-project python scripts/sf.py configure --open-as browser`, or start with `--browser`. Add `--phone` to switch on the phone view and `--awake` to keep the computer awake (below).
Want an icon on your desktop? Run `uv run --no-project python scripts/sf.py shortcut` once.

## Where things are
```
software-factory/
├─ Start Software Factory.cmd / .command   one click: starts everything and opens the dashboard
├─ README.md  LICENSE  pyproject.toml      the usual project files
├─ src/          the factory's code: the driver (factory.py), the dashboard server, the checks, the phone view, keep-awake
├─ agents/       the plain-text job descriptions of the ~20 agents. Edit these to change how an agent works
├─ dashboard/    the dashboard web page
├─ skills/       the two skills (/software-factory-setup, /software-factory-idea)
├─ scripts/      sf.py, the helper both skills (and the launchers) call
├─ docs/         the flowchart
├─ tests/        the test suite
└─ workspace/    EVERYTHING THAT IS YOURS (kept out of git, safe to back up):
   ├─ runs/            one folder per run: the product, its documents, screenshots, logs
   ├─ always-use/      your standing documents (brand colours, design rules)
   ├─ skills-library/  skills you dropped into the dashboard
   └─ presets.json     your saved app presets
```
Your settings and keys live outside the folder, in `~/.software-factory/`. Updating the factory (`git pull`) never touches `workspace/` or your settings. Older installs that kept `runs/`, `always-use/` and `skills-library/` next to the code are moved into `workspace/` automatically the next time the dashboard or driver starts.

## The two skills

| Skill | What it does |
|---|---|
| `/software-factory-setup` | One time. Sets up the agents, the dashboard and your connections on your agent. Safe to run again. |
| `/software-factory-idea [idea]` | Starts a run: brief, plan, build, checks, report. Also takes links, design photos, a whole spec, "give me ideas", or "recreate this feature from that product". Starts the dashboard hidden if needed and opens it. |

Both are plain [`SKILL.md`](skills/) files (the open Agent Skills format), so any agent that reads skills can use them. They call one helper script, [`scripts/sf.py`](scripts/sf.py), which you can also use yourself: `uv run --no-project python scripts/sf.py --help`.

## What it can build
Every kind is built AND checked by the driver itself, not just generated:

| Kind | Built as | How the driver checks it |
|---|---|---|
| Web page / web app | one `index.html` | real browser screenshots at phone and desktop size, a real browser test |
| Web app with a backend | Python web server (FastAPI/Flask) | started, then the same browser checks |
| Command-line tool | Python | its tests |
| API (no screens) | Python + FastAPI | started; every declared request is made and its status and response checked |
| Bot / background worker | logic + a fake chat platform | runs in fake mode without crashing; a scripted conversation must succeed |
| Library | Python package (uv) | tests, a real package build, every README example runs |
| Multi-service system | one folder per service | all started in order and wired together (`<NAME>_URL`), each checked by its kind, the web one screenshotted |
| Phone app (iPhone + Android) | React Native with Expo (needs Node.js) | its tests; the web export screenshotted at phone sizes. On a real phone: Expo Go (your step) |
| Desktop app | Python + Qt (PySide6) | every screen rendered off-screen to a PNG (no window opens) |
| Game | Godot 4 (needs Godot installed) | imported and run headless with no script errors, plus its tests (no screenshots) |

Browser games are web apps. Unity and Unreal are not supported (huge installs and license agreements). `sf.py doctor` shows what this computer can build; the agents are told, so they never pick a kind that can't be checked here.

## Cost: Quality, and your own models
Each run has a **Quality** (New run form, `--quality`, or just say "cheap" / "best possible" to the idea skill):

| Quality | What changes | For |
|---|---|---|
| **Lean** | Sonnet for every station that would use Opus (the builder of single-agent apps, spec, reviewers, troubleshooter) | experiments and quick tries |
| **Balanced** (default) | Opus only for the spec, the single-agent builder, the technical review and the troubleshooter; planning and design on Sonnet with Opus as the automatic retry | most runs |
| **Max** | Opus for planning, design, the visual mock and the UI review too (the original settings) | when quality matters more than cost |

Plans are also capped at **6 build cards** (4 in a rework round): every card is a builder that re-reads the spec and plan, so fewer, bigger cards are the biggest saving. For reference, Opus 5.5 costs twice what Sonnet 5.5 does per token.

**Choose any agent's model and effort yourself:** setup asks, or run `sf.py agents set sf-planner --model claude-opus-5-5 --effort high` (`sf.py agents reset all` to undo). Your choices live in `~/.software-factory/config.json`, apply in every Quality, and survive updates. A failing agent still retries on its stronger model.

## Apps with outside services (AI, payments, email...) and your keys
- Agents may build real backends: a database (SQLite by default, or any Postgres through `DATABASE_URL`), real accounts (hashed passwords, secure sessions), **test-mode** payments, and outside services.
- **Every outside service has a fake mode**, used whenever its key is missing. The app runs, and every test and check passes, with no keys at all. The agents list each service in `factory/env.json` and `app/.env.example`; the driver checks both.
- **You add keys on the dashboard** (**Your app** panel): paste a key, press **Open the app**, and the factory starts your app with that key. Keys are stored only on your computer (`~/.software-factory/keys.env`). The page never shows them back. Agents never get them. The app gets only the keys it declared. The automatic checks always use fake mode, so they never spend money on a paid API. Live payment keys are refused.
- If a stored key ever shows up in the app's code, the build is sent back.

## Which agent runs the stations (the "engine")

| | Claude Code | Codex | Any other agent ("custom") |
|---|---|---|---|
| How it's called | `claude -p`, one fresh process per station | `codex exec --json` | the non-interactive command you (or setup) describe once |
| Per-station tool limits | yes (each station has an allowlist) | no (sandboxed to the run folder) | no |
| Hidden tests protected | by permission rules | by instruction only | by instruction only |
| Live output on the dashboard | every tool call | commands, file changes, messages | every output line |
| Cost | in dollars, with a spending cap | tokens only: shown as "not reported", no cap | not reported, no cap |
| Status | the original, fully tested | built from Codex's documented output, tested with recorded output | tested with a stand-in agent |

The choice lives in `~/.software-factory/config.json`, written by `sf.py configure`. Each agent file names a Claude model; on other engines it maps to a tier: Opus = `strong`, Sonnet = `standard`, Haiku = `fast` (`sf.py configure --models "strong=...,standard=...,fast=..."`; empty = the agent's default model).

## What you give it
- **Required, at least one:** an idea, a whole spec (both is fine), **Generate ideas** (the agent proposes 5; you choose one or press **Random pick**), or **Reverse engineer**: name a feature or a whole app from another product and the factory studies it from public information (docs, help pages, demos, your screenshots) and builds an original version: same behaviour, your own name, look and code (clean-room: nothing is copied, no logins or paywalls).
- **About the app (optional, worth filling in):** *App context* (who it is for, what it is for, tone, anything it must respect) and the **first-open experience** (what a brand-new user should see and do the first time they open the app, for example "a welcome screen, three quick questions, then the home screen, no sign-up"). Both are given to every agent word for word. The first-open experience becomes numbered acceptance criteria that the builder, the tests and the UI reviewer check. In the idea skill or on the command line: `--context "..."` and `--first-open "..."`. For apps you build often, **save the two boxes as a preset** on the New run form (stored in `workspace/presets.json`) and fill them again with one click.
- **Optional extras:** links to products like it, reference photos for the design, reference links, and skills you want considered. Drop skill files or whole skill folders into the dashboard's **Skills** library (or type a skill's name when starting a run).
- **Always-use documents** (optional, saved once, in `workspace/always-use/`): brand colours, design rules, preferences. Every agent in every run reads them. Edit them from the dashboard (**Always-use docs**) or just drop `.md` files in that folder.

The **intake agent** reads all of it (it opens the links and *looks* at the photos) and writes one `brief.md` that every later agent builds from. With only a simple idea it is skipped, so nothing is spent. A whole spec is respected: the spec step only *checks* it.

## Skills
A skill is a folder of expert guidance (a `SKILL.md` and optional extra files). The **skill agent** (`sf-skills`) runs in both paths. It looks at everything available: your **skills library** (dropped into the dashboard, stored in `workspace/skills-library/`) and the skills installed on this machine. For each project it judges which ones are actually useful, keeps at most 3 per side, and says why. Skills that don't fit are **not used**, including ones you asked for by name. Only the chosen skills are inserted into the agents they help (design skills go to the design, UI and build agents; technical skills to the planner, builders and reviewer; security skills to the security checker). `factory/skills.md` records what was used and what was left out, and why.

## The two paths
The **decider** reads the brief and picks a path, shows an estimate of cost and time, and continues on its own after 90 seconds unless you switch (or you chose a mode when starting).

| | Single agent | Full pipeline |
|---|---|---|
| For | small apps: a few screens, no real backend | backends, many screens, costly mistakes |
| Cost / time | about $2–5, 5–12 min | about $5–15 (Balanced, estimated), 30–60+ min |
| You see | the finished app and a screenshot report | the spec + a visual mock before anything is built, then the finished app |
| You can | approve, **improve** (notes go to the same agent), or **escalate** | approve, **small tweak** (straight to a builder), or **big change** (back to the planner) |

A failing single-agent run **escalates itself** to the full pipeline after two fix rounds. The draft is kept in `reference/prototype/` and becomes the design reference and the visual mock (no agent needed for the mock).

## Checks that don't trust agents
- **Auto checks (the driver, no agent):** the app's unit tests, a real **browser test** (Edge, Chrome or Playwright's own Chromium: does the core flow work and survive a reload?), real **screenshots** at phone and desktop size plus dark mode (flags console errors, failed requests, sideways scrolling), a **secret scan**, a dependency audit, and the hidden acceptance tests (full pipeline).
- **Security checker (both paths):** an agent reviews the code for injection, secrets, unsafe storage, server settings and supply-chain risks. Blocking findings go back to the builder. If they survive two fix rounds you are asked, and the report lists them.
- **Reviewers (full pipeline):** technical, security and UI/design run together on the integrated build (once, not per card). The UI reviewer *looks at the screenshots* against the approved design.
- **Hidden tests** are written from the spec only, never shown to builders, and run by the driver.
- A stage that writes no files, or invalid ones, is retried on a stronger model, then handed to the **troubleshooter** (`sf-medic`), which finds the cause and sends the fix to the right agent, or asks you.

## The agents: model and effort (on Claude Code)
| Agent | Model | Effort | Job |
|---|---|---|---|
| `sf-ideator` | Sonnet 5.5 | medium | Proposes 5 ideas (web search) |
| `sf-reverse` | Opus 5.5 | medium | Reverse engineer: studies a feature or app from public sources, clean-room |
| `sf-intake` | Sonnet 5.5 | medium | Reads links, photos, spec; writes `brief.md` |
| `sf-skills` | Sonnet 5.5 | medium | Skill agent: picks only the skills that fit this project (one per side in the full pipeline) |
| `sf-decider` | Sonnet 5.5 | medium | Single agent or full pipeline? |
| `sf-maker` | Opus 5.5 | medium (retry high) | Single path: builds the whole app, then critiques its own screenshots |
| `sf-security` | Sonnet 5.5 | high | Security review, both paths |
| `sf-researcher` | Sonnet 5.5 | medium | Web research before the spec |
| `sf-spec` | Opus 5.5 | high | Writes the spec (or checks yours) |
| `sf-mock` | Sonnet 5.5 (retry Opus 5.5) | high | Visual mock shown to you before building |
| `sf-designer` | Sonnet 5.5 (retry Opus 5.5) | high | Design plan + real stylesheet (goes first) |
| `sf-planner` | Sonnet 5.5 (retry Opus 5.5) | high | Technical plan, task cards, how the app is run |
| `sf-integrator` | Sonnet 5.5 | medium | Merges both plans into one final plan |
| `sf-holdout` | Sonnet 5.5 | medium | Hidden acceptance tests |
| `sf-builder` | Sonnet 5.5 (retry Opus 5.5 high) | medium | One card each, in parallel worktrees |
| `sf-reviewer` | Opus 5.5 | high | Technical review of the integrated build |
| `sf-reviewer-ui` | Sonnet 5.5 | medium | Looks at screenshots vs the design |
| `sf-writer` | Haiku 5.5 | medium | Screenshot-first report |
| `sf-medic` | Opus 5.5 | high | Troubleshooter |
| `sf-deployer` | Sonnet 5.5 | medium | Deploy step: publishes or packages the approved app with your own accounts (not part of a build run) |

These are the **Balanced** settings. They live in each `agents/sf-*.md` header (`model`, `effort`, `retry_model`, `retry_effort`, `lean_model`/`lean_effort`, `max_model`/`max_effort`, `timeout_min`); your own choices (`sf.py agents set`) override them. `uv run src/factory.py check` confirms every pair works. The skill agent's picks are inserted into the matching agents' instructions.

## Use it without the skills
```bash
uv run --no-project python scripts/sf.py idea "..."   # what the idea skill runs: hidden dashboard + run + browser
uv run src/dashboard.py                      # or just the dashboard: http://127.0.0.1:8787, press "New run"
```
Or the driver directly from a terminal:
```bash
uv run src/factory.py check                  # git / uv / your agent present, every model + effort answers
uv run src/factory.py new --idea "..."       # also: --spec-file f.md --generate --link URL --ref URL --photo img.png --skill NAME --mode single|multi --budget 20
uv run src/factory.py status [run]           # where is it / what is waiting on you
uv run src/factory.py stats                  # cost, time and outcome of every finished run
uv run src/factory.py approve <run>          # or: reject <run> "..."  /  decide <run> choose --index 2  /  decide <run> random
uv run src/factory.py run <run>              # resume after a failure or restart
```

## Dashboard
A live view of a run: the pipeline rail for the chosen path, each active agent's live feed with its model and effort, the **Factory console** (the output of the hidden background process), every card, the cost so far, documents (the report shows its screenshots), and an event stream. The gates appear in the **Needs you** panel: the idea list with **Choose this** and **Random pick**, the decider with a countdown and a **Switch** button, the spec with the mock screenshots, the final review with **Open the app**, and troubleshooter questions. **New run** has all the inputs; **Always-use docs** edits your standing documents; **Skills** is the skills library (drop skill files or folders; each run's skill agent decides which to use). A banner with **Resume run** appears if the driver stopped. The dashboard itself only listens on this computer (127.0.0.1) and refuses requests that arrive under another host name.

### Phone view and keep awake (the **Phone & power** button)
- **Phone view:** watch a run and answer its "Needs you" questions from your phone, on the same Wi-Fi. Press **Turn on phone view**, scan the QR code with the phone's camera, done. Or `uv run --no-project python scripts/sf.py phone on`.
  - It is **off every time the dashboard starts**, and you switch it on on purpose. Only then does a second server listen on your local network.
  - It needs a fresh 6-digit **PIN** each time (the QR code carries it). A wrong PIN locks that device for a minute, and many wrong PINs anywhere replace the PIN.
  - The phone can **watch and answer** (approve, reject, choose, resume) and keep the computer awake. It **cannot** start runs, store keys, deploy, open apps or edit your documents and skills: those stay on the computer.
  - It is plain http on your own network, so use home or office Wi-Fi you trust, not a cafe or hotel. If the phone can't connect, allow Python through the firewall for private networks when Windows asks.
  - **Away from home:** install [Tailscale](https://tailscale.com) (free) on this computer and your phone, sign in on both, then turn the phone view off and on again. A second, private address (100.x.x.x) appears in the panel and works on any network, over an encrypted private connection, with nothing opened to the public internet. The PIN still applies. The computer must stay on (use keep awake).
- **Keep my computer awake:** a computer that falls asleep stops your runs and drops the phone view. This holds it awake *until all runs finish* (default), or for 2 or 8 hours, at most 12 hours. It stops sleep, not the screen turning off, and a laptop with its lid closed can still sleep. `sf.py awake on|off|status`. Works on Windows, macOS (`caffeinate`) and Linux with systemd.
- **Phone notifications** (same panel, or `sf.py push on`): a buzz on your phone when a run needs you or hits a usage limit, anywhere, even on mobile data. It uses the free [ntfy](https://ntfy.sh) app: install it, press *Subscribe to topic*, and type the random topic the factory gives you. Only a generic line is sent ("needs you: Approve the spec"), never your idea, code, keys or run title. Anyone who learns the topic could read those lines, so treat it like a password, or run your own ntfy server (`sf.py push on --server https://...`). Off until you switch it on. `sf.py push test` checks it.

### Spending caps and clean-up
- **Daily and monthly caps across all runs:** `sf.py configure --daily-cap 25 --monthly-cap 200` (0 = no cap). A new run is refused with a plain message once a cap is used up; a run that is already going is never cut off (its own per-run cap covers it). The header shows *Today $x / $cap*. A run's cost counts on the day it started.
- **Free disk space:** `sf.py clean` lists what finished runs can rebuild by themselves (virtual environments, `node_modules`, build caches, agent worktrees) and how much space that is. `sf.py clean --apply` removes it. It never touches an app's own files, git history, report or screenshots, only finished runs that are not running, and a finished app simply reinstalls its packages next time you open it.

## What a run produces
`workspace/runs/<run-id>/` (git-ignored; the product has its own git repo):
- `inputs/`: your spec, photos, always-use copies, skills · `factory/`: `brief.md`, `skills.md`, `spec.md`, `research.md`, `mock/`, `design.md`, `plan.md`, `cards.json`, `app.json`, `checks/` (results + screenshots), `reviews/`, `report.md`
- `holdout/`: hidden tests · `app/`: the product · `reference/prototype/`: the single-agent draft after an escalation · `logs/`

## Deploy (after you approve)
Once a finished app is approved, a **Deploy** panel appears in the dashboard (or `uv run src/factory.py deploy <run> --list`, then `--target <name>`). It offers:

| Option | What you get | Needs |
|---|---|---|
| **Netlify** | a free public web address (`name.netlify.app`) | your Netlify connector in Claude, or the command line signed in once |
| **Vercel** | a free public web address (`name.vercel.app`) | the same idea |
| **GitHub Pages** | a site from a NEW PUBLIC GitHub repository (the code becomes public) | the GitHub command line (`gh`) signed in |
| **Desktop app (Electron)** | a program for your system (Windows, macOS or Linux) you double-click, plus a .zip to share; nothing is published, no account | Node.js (the first build downloads about 100 MB) |
| **Docker image** (server apps) | a container image of your app; runs on this computer or any host that takes containers; nothing is published, no account | Docker Desktop, running |
| **Render** (server apps) | a free public web address (`name.onrender.com`), plus a free Postgres database if the app uses one; you finish with one link and your own Render account | the GitHub command line (`gh`) signed in |

- **It uses your accounts, never your passwords.** On Claude Code the panel asks which connectors you have (`claude mcp list`) and prefers them; otherwise it uses the command-line tool you are already signed in to. If you are not signed in, the result says so and lists the steps for you to do yourself. The deployer never sees, asks for or types a password or token.
- **You confirm every time.** The first click only arms the button ("Yes, publish it"); the second does it. Public options say what becomes public.
- **Apps with a server** get two other options: **Docker image** (built on this computer, runs on any host that takes containers; no account) and **Render** (a public address on Render's free plan, with a free Postgres database if the app uses one: the code goes to a new public GitHub repository, then you open one link, sign in to Render, type any keys and press Apply).
- **Only the app ships.** The driver copies just the app's files to `deploy/site/` (single page) or `deploy/app/` (server: code without git history, `.env` files, local databases or caches) and refuses if the secret scan or the key check finds anything.
- **The deployer is its own agent** (`sf-deployer`, Sonnet 5.5). It is the only agent allowed to `git push` (never `--force`) and to run `gh`, `npx` and similar, and it gets only the connector that matches the target you chose, not every connector you have.
- Results (live address, file location, or what you need to do next) stay in the panel and in `state.json`.

## A calm dashboard
- **What needs you comes first.** When the run is waiting for a decision, a bar sticks to the top of the page ("Needs you: ...", **Take me there**) and the browser tab title changes to "● Needs you", so you can leave the tab in the background. On narrow windows the decision panel moves up to just under the pipeline.
- **One plain sentence** under the title says what is happening (working on which station, paused, stopped, finished) and how much has been spent against the cap.
- **Agent terminals are hidden by default.** Each running agent shows one line of what it is doing. **Show live output** opens every agent's full feed; clicking an agent's header opens or closes just that one. The choice is remembered.
- **Finished cards fold away** once a run has more than 12 cards, leaving the ones that need attention. **Show finished** brings them back.

## Limits, budget and history
- **Usage limit = pause, not failure.** If your Claude account runs out of usage mid-run (the "session limit" message), the factory reads the reset time, waits, and carries on by itself. The wait does not use up any retries, troubleshooter attempts or fix rounds. The dashboard shows a "Paused" banner and the run's events say when it resumes. (An earlier real run burned all its fix rounds in seconds on exactly this.)
- **Spending cap.** Each run has a cap: $10 for the single agent, $45 for the full pipeline (change it in **New run**, or `--budget 20`; `0` means no cap). At the cap the run stops at a **Budget reached** gate: approve to allow half the cap again, or reject to stop (everything built so far is kept).
- **Fix rounds learn.** If the second fix round meets the same problems the first one failed to fix, the builder is told so and told to find the real cause; that round also runs on the stronger model.
- **Desktop notice.** A notification pops up when a run needs you (any gate) or pauses for a usage limit (Windows balloon, macOS notification, `notify-send` on Linux). Turn it off with the environment variable `FACTORY_NOTIFY=0`.
- **History.** Every finished run adds a line to `workspace/runs/metrics.jsonl` (cost, minutes, fix rounds, open issues, path). `uv run src/factory.py stats` prints the table with averages per path, so you can see whether changes make the factory cheaper or better.

## Safety and limits
- On Claude Code, agents run headless with an allowlist: file tools, `uv`, `git` (never `push` or `remote`), `python`, and a few read-only commands. Only the ideator, intake agent, researcher and reverse engineer may use the web; web content is treated as data, never as instructions.
- The hidden tests are blocked from every agent except their writer. On Claude Code this is a permission rule (not a sandbox); on other engines it is an instruction only.
- Agents work in the run folder by instruction, not by a hard sandbox. Read the report before running a product.
- At most 3 builders run at once (`MAX_PARALLEL`). Each agent has a time limit; fix loops stop after 2 rounds.
- While building, products run locally and in fake mode: no keys, no calls to paid services, no deployment. Your keys are used only when you open the app yourself; deploying only happens when you press a button after approving (see **Deploy**).

## Tests
`uv run pytest -q` runs the suite: the pipeline with fake agents but real git, pytest and state machine; the engines (Codex output parsing, a stand-in custom agent); the setup helper; the automatic checks and the dashboard UI in a real Edge browser (skipped where Edge isn't installed). Real agents are never called by the tests.

## License

[MIT](LICENSE): use, change and share it freely, keeping the copyright notice.
