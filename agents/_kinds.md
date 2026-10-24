## Kinds of product and how each is checked

`factory/app.json` names the kind in `type`. Pick the simplest kind that delivers what the owner asked for, from the list of kinds this computer can build (above). The driver runs every check below itself; a check that fails sends the work back. In every command, `<RUN_DIR>` is the real absolute path.

### static: a web page or web app with no server
One self-contained `app/index.html` (HTML, CSS and JS together, no build step, data in `localStorage`). Browser games (canvas, Phaser-style code written by you) are `static` too.
```json
{"type": "static", "entry": "index.html", "screens": [{"name": "home", "path": "index.html?demo=1"}], "smoke_test": "tests/test_smoke_browser.py", "test": null}
```

### server: a web app with a backend
A Python web server (FastAPI or Flask) that serves the pages and the data. `start` must read the `PORT` environment variable.
```json
{"type": "server", "start": "uv run --directory <RUN_DIR>/app python -m myapp", "port": 8765, "screens": [{"name": "home", "path": "/"}], "test": "uv run --directory <RUN_DIR>/app pytest -q"}
```

### cli: a command-line tool
```json
{"type": "cli", "test": "uv run --directory <RUN_DIR>/app pytest -q"}
```

### api: a backend with no screens (a JSON API, a webhook receiver)
Python + FastAPI. Include a `GET /health` endpoint. `api_checks` are real requests the driver makes against the running API; cover the main flows (create, read, the main error case). `json` is an optional request body; `expect_keys` lists keys the JSON response (or its first item) must have.
```json
{"type": "api", "start": "uv run --directory <RUN_DIR>/app uvicorn myapi.main:app --port 8765", "port": 8765,
 "test": "uv run --directory <RUN_DIR>/app pytest -q",
 "api_checks": [{"name": "health", "method": "GET", "path": "/health", "expect_status": 200},
                {"name": "create note", "method": "POST", "path": "/notes", "json": {"text": "hi"}, "expect_status": 201, "expect_keys": ["id", "text"]},
                {"name": "missing note", "method": "GET", "path": "/notes/999999", "expect_status": 404}]}
```
(Use the `PORT` environment variable rather than a fixed port when you can: the driver may pick a different free port.)

### worker: a bot (Discord, Slack, Telegram, email...) or a background job
Split it in two: the bot's LOGIC (pure functions: message in, reply out) and a thin ADAPTER for the platform. The adapter has a fake mode used when the platform's token is missing (declare the token in `factory/env.json`): it reads messages from a local file or the console instead of the real platform. `start` runs the worker in whatever mode the environment allows and must keep running (or finish its job) without errors. `simulate` drives a scripted conversation or job through the logic in fake mode, prints the transcript, and exits 0 only if every reply is right.
```json
{"type": "worker", "start": "uv run --directory <RUN_DIR>/app python -m mybot", "run_seconds": 8,
 "simulate": "uv run --directory <RUN_DIR>/app python -m mybot.simulate", "test": "uv run --directory <RUN_DIR>/app pytest -q"}
```

### library: a package other programs import
A Python package made with `uv init --lib`. A README with a quickstart, and every README example also saved as a runnable script in `examples/`. The driver builds the package and runs each example.
```json
{"type": "library", "build": "uv build --directory <RUN_DIR>/app", "test": "uv run --directory <RUN_DIR>/app pytest -q",
 "examples": ["uv run --directory <RUN_DIR>/app python examples/quickstart.py"]}
```

### multi: several services (for example a web front-end, an API and a worker)
Each service in its own folder under `app/` with its own start command. The driver starts them in `depends_on` order and gives EVERY service the address of every web/API service as `<NAME>_URL` (for a service named `api`: `API_URL=http://127.0.0.1:<port>`); read those variables instead of hard-coding addresses. Each service is checked by its own kind; the `web` service is screenshotted. A root `app/README.md` explains how to start everything; add a `docker-compose.yml` that runs all services together.
```json
{"type": "multi", "web": "web", "test": "uv run --directory <RUN_DIR>/app pytest -q",
 "services": [{"name": "api", "type": "api", "dir": "api", "start": "uv run --directory <RUN_DIR>/app/api python -m api", "port": 8801,
               "api_checks": [{"name": "health", "method": "GET", "path": "/health", "expect_status": 200}]},
              {"name": "worker", "type": "worker", "dir": "worker", "start": "uv run --directory <RUN_DIR>/app/worker python -m worker", "depends_on": ["api"]},
              {"name": "web", "type": "server", "dir": "web", "start": "uv run --directory <RUN_DIR>/app/web python -m web", "port": 8800, "depends_on": ["api"]}],
 "screens": [{"name": "home", "path": "/"}]}
```

### mobile: a phone app for iPhone and Android
React Native with Expo (`npx create-expo-app@latest app --template blank-typescript --yes`, then work inside `app/`), TypeScript. Keep navigation simple (a screen state in `App.tsx`, or React Navigation); on the web, read `?screen=<name>` and `?demo=1` from `window.location` (only when `Platform.OS === "web"`) so the driver can open each screen directly. It must also run on the web (`react-native-web`, `react-dom`, `@expo/metro-runtime` installed with `npx expo install`), because the driver checks it through its web export at phone sizes. Tests with Jest (`jest-expo`). Phone-only features (camera, push notifications, sensors) need a fallback on the web, and the report must say they were not checked on a real phone. Screens are URL paths of the web export; `?demo=1` loads sample data.
```json
{"type": "mobile", "framework": "expo", "test": "npm --prefix <RUN_DIR>/app test -- --watchAll=false",
 "web_export": "npx expo export --platform web --output-dir dist", "web_dir": "dist",
 "screens": [{"name": "home", "path": "/?demo=1"}, {"name": "settings", "path": "/?screen=settings&demo=1"}]}
```

### desktop: a native desktop app
Python + Qt (`uv add pyside6`). Besides the normal start, the app supports `--screenshot <folder>`: for each screen name in app.json, it builds that screen with realistic demo data, and for each size in the `FACTORY_SIZES` environment variable (for example `1280x800,1024x700`) resizes the window and saves `<folder>/<screen>-<width>.png` with `widget.grab().save(...)`, then exits. The driver runs it with `QT_QPA_PLATFORM=offscreen`, so no window ever opens. Off-screen Qt has no default font, so right after creating the `QApplication`, set one explicitly: the first of `"Segoe UI"`, `"Helvetica Neue"`, `"Helvetica"`, `"Arial"`, `"DejaVu Sans"`, `"Noto Sans"` found in `QFontDatabase.families()` (`app.setFont(QFont(family, 10))`), or the brand font if the design names one. Look at your own screenshots before handing off: boxes instead of letters mean no font was set. Unit tests run off-screen too (set `QT_QPA_PLATFORM=offscreen` in `tests/conftest.py`).
```json
{"type": "desktop", "framework": "qt", "start": "uv run --directory <RUN_DIR>/app python -m myapp",
 "screenshot": "uv run --directory <RUN_DIR>/app python -m myapp --screenshot {out_dir}",
 "screens": [{"name": "main"}, {"name": "settings"}], "test": "uv run --directory <RUN_DIR>/app pytest -q"}
```

### godot: a game in the Godot 4 engine
`app/project.godot` plus scenes and GDScript. Keep the game rules in plain scripts that don't need a screen, and test them with `app/tests/run_tests.gd`: a script that `extends SceneTree`, runs every `test_*` function of the test scripts, prints each result and calls `quit(0)` when all pass or `quit(1)` otherwise. The driver also imports the project and runs the main scene headless for a few seconds; any `SCRIPT ERROR` fails the check. Godot has no screenshots here: describe what the player sees in the report.
```json
{"type": "godot", "test": "godot --headless --path <RUN_DIR>/app --script res://tests/run_tests.gd", "smoke_seconds": 5}
```
