"""Open an app in a real browser (Edge via Playwright), take screenshots and collect problems.

Run with:  uv run --with playwright python src/tools_browse.py <config.json>
Prints one line: @@RESULT@@ {json}. Never needs a browser download: it drives the installed Edge (or Chrome).
"""

import json
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def wait_port(port: int, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.4)
    return False


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
    except Exception:
        pass


def launch(p):
    last = None
    for channel in ("msedge", "chrome", None):  # None = Playwright's own Chromium (`playwright install chromium`)
        try:
            return p.chromium.launch(channel=channel, headless=True) if channel else p.chromium.launch(headless=True)
        except Exception as e:  # not installed
            last = e
    raise RuntimeError(f"no Edge, Chrome or Playwright Chromium found (run: uv run playwright install chromium): {last}")


def main(cfg_path: str) -> dict:
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    app = Path(cfg["app_dir"])
    out = Path(cfg["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    proc = None
    log = ""
    try:
        if cfg["type"] == "server":
            port = int(cfg["port"])
            cmd = cfg["start"]
            args = cmd if isinstance(cmd, list) else shlex.split(cmd, posix=(os.name != "nt"))
            proc = subprocess.Popen(args, cwd=str(app), env={**os.environ, "PORT": str(port)}, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
            if not wait_port(port, float(cfg.get("start_timeout", 45))):
                return {"ok": False, "error": f"server did not open port {port} within the time limit"}
            base = f"http://127.0.0.1:{port}"
        else:
            base = None

        results = []
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = launch(p)
            for i, screen in enumerate(cfg["screens"]):
                path = screen["path"]
                if base:
                    url = base + (path if path.startswith("/") else "/" + path)
                else:
                    file_part, _, query = path.partition("?")
                    url = (app / file_part).resolve().as_uri() + (("?" + query) if query else "")
                for w, h in cfg.get("sizes", [[390, 844], [1280, 800]]):
                    errors, failed = [], []
                    ctx = browser.new_context(viewport={"width": w, "height": h})
                    page = ctx.new_page()
                    # a missing tab icon (favicon) is not an app error; its 404 message doesn't name the file, its location does
                    page.on("console", lambda m, errors=errors: errors.append(m.text)
                            if m.type == "error" and "favicon" not in str((m.location or {}).get("url", "")).lower() else None)
                    page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                    page.on("requestfailed", lambda r, failed=failed: failed.append(r.url))
                    try:
                        page.goto(url, wait_until="load", timeout=20000)
                        page.wait_for_timeout(int(cfg.get("settle_ms", 1200)))
                        overflow = bool(page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"))
                        png = out / f"{screen['name']}-{w}.png"
                        page.screenshot(path=str(png))
                        entry = {"name": screen["name"], "size": [w, h], "png": png.name, "overflow": overflow}
                    except Exception as e:  # the page itself failed to load
                        entry = {"name": screen["name"], "size": [w, h], "png": None, "overflow": False, "load_error": str(e)[:200]}
                    errors = [e for e in errors if "favicon" not in e.lower()]
                    failed = [u for u in failed if "favicon" not in u.lower()]
                    entry["console_errors"], entry["failed_requests"] = errors[:8], failed[:8]
                    results.append(entry)
                    if i == 0 and w == cfg.get("sizes", [[390, 844]])[0][0]:
                        try:  # one dark-mode shot of the first screen
                            page.emulate_media(color_scheme="dark")
                            page.wait_for_timeout(400)
                            page.screenshot(path=str(out / f"{screen['name']}-dark.png"))
                        except Exception:
                            pass
                    ctx.close()
            browser.close()
        return {"ok": True, "screens": results}
    except Exception as e:
        return {"ok": False, "error": str(e)[:400]}
    finally:
        if proc:
            try:
                log = (proc.stdout.read(4000) if proc.poll() is not None and proc.stdout else "") or ""
            except Exception:
                pass
            kill_tree(proc)


if __name__ == "__main__":
    result = main(sys.argv[1])
    print("@@RESULT@@ " + json.dumps(result))
