---
name: software-factory-idea
description: Start the Software Factory on an idea - it runs the whole pipeline of AI agents (brief, plan, build, tests, security and screenshot checks, report) in the background, with no terminal windows, and opens the live dashboard. Use when the user runs /software-factory-idea, or says "build this with the software factory", "factory: <idea>", "send this to the factory", or asks the factory to generate or recreate an app.
---

# Start a Software Factory run

The user gives an idea; you start a factory run and open its dashboard. The run goes on by itself in the background. You don't build anything yourself.

`SF` below means: `uv run --no-project python "<HOME>/.software-factory/sf.py"` (`<HOME>` = the user's home folder, absolute path, forward slashes). If that file doesn't exist, the factory isn't set up yet: tell the user to run `/software-factory-setup` first, and stop.

## 1. Turn the request into inputs
Take only what the user actually gave. Don't invent extras.
- **The idea**: their words, cleaned up but not expanded. Write it to a temporary text file and pass `--idea-file <path>` (this avoids quoting problems). A one-line idea can go straight in: `SF idea "a habit tracker for runners"`.
- **No idea, "give me ideas"** → `--generate` (the factory proposes 5; the user picks one on the dashboard).
- **"Recreate X from Y" / "copy how Z does it"** → `--reverse "<what>"`, plus `--reverse-scope app` for a whole app (default: one feature) and `--reverse-link URL` for each page about it. It builds an original version from public information.
- **A whole spec** (a long document) → save it to a file and pass `--spec-file <path>`.
- **Links to products like it** → `--link URL` each. **Reference links** → `--ref URL` each. **Design photos** (file paths) → `--photo PATH` each. **Skills they want considered** → `--skill NAME` each.
- **"Quick" / "simple" / "just one agent"** → `--mode single`. **"Do it properly" / "full pipeline"** → `--mode multi`. Otherwise leave it out: the factory decides, and the user can switch on the dashboard.
- **A spending limit in dollars** → `--budget N`.
- **"Cheap" / "just an experiment" / "quick try"** → `--quality lean`. **"Best possible" / "money no object"** → `--quality max`. Otherwise leave it out (Balanced).
- **Apps that use outside services** (an AI model, payments, email...) are fine: the factory builds a fake mode for each, and the user adds real keys later on the dashboard (**Your app** panel). Never ask the user to paste a key into the chat.

## 2. Start it
Run `SF idea ... --json`. It returns within seconds:
- It starts the dashboard in the background if needed (no terminal window), creates the run, starts the factory's background process (also hidden), and opens the dashboard in the browser at that run.
- If it reports an error, show it in plain words and stop. For "packages are not installed", tell the user to run `/software-factory-setup`.

## 3. Tell the user (short)
- The run id and the dashboard link from the output.
- "It runs in the background. No terminal windows open: the factory's output is on the dashboard (**Factory console**, and **Show live output** for each agent)."
- "When it needs you (choosing an idea, approving the plan, reviewing the finished app), it shows up under **Needs you** on the dashboard, and the browser tab title changes."
- Don't wait for the run to finish, and don't poll it.

## Later in the conversation
If the user answers a decision here instead of on the dashboard, pass it on (`<run>` = the run id):
- Approve: `SF factory approve <run>` · Send back with changes: `SF factory reject <run> "<what to change>"`
- Pick a generated idea: `SF factory decide <run> choose --index <n>` · Random pick: `SF factory decide <run> random`
- Where is it? `SF status <run>` · Dashboard again: `SF dashboard open`
Quote the user's own words in any feedback you pass on.
