"""Phone view: the pieces that make the dashboard safe to open from your phone on the same Wi-Fi.

Nothing here listens on the network by itself. dashboard.py starts a SECOND server on your local network only when you
switch "Phone view" on, and stops it when you switch it off. The normal dashboard stays on 127.0.0.1 (this computer only).

Safety, in plain words:
  * Off every time the dashboard starts. You turn it on on purpose.
  * A new 6-digit PIN each time you turn it on (the QR code carries it, so scanning is enough).
  * After the PIN you get a private cookie. Wrong PINs lock that device out for a minute; too many wrong PINs anywhere
    replace the PIN, so guessing never gets far.
  * The phone can WATCH and ANSWER (approve, reject, choose, resume) and keep the computer awake. It cannot start runs,
    store keys, deploy, or edit your documents and skills: those stay on the computer.
  * Plain http on your own network. Use a network you trust (home Wi-Fi), not a cafe or a hotel.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from typing import Optional

LOCK_AFTER = 5            # wrong PINs from one device before it is locked out
LOCK_SECONDS = 60
ROTATE_AFTER = 20         # wrong PINs from anywhere before the PIN is replaced
COOKIE = "sf_remote"
COOKIE_AGE = 12 * 3600    # a phone stays signed in for 12 hours

# What a phone may do. Everything not listed is refused (and the page hides those buttons).
ALLOWED_GET = {"/", "/index.html", "/api/info", "/api/state", "/api/stream", "/api/doc", "/api/file", "/api/log", "/api/awake", "/favicon.ico"}
ALLOWED_POST = {"/api/approve", "/api/reject", "/api/decide", "/api/resume", "/api/awake", "/api/login"}


def lan_addresses() -> list[str]:
    """This computer's address(es) on your local network, the one used for the internet first. Never a public address."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))     # no packet is sent: this only asks the OS which network card it would use
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return [ip for ip in found if not ip.startswith(("127.", "169.254.", "0."))]


def tailscale_address() -> Optional[str]:
    """This computer's private Tailscale address (100.x.y.z), or None when Tailscale isn't installed or isn't connected.
    With Tailscale (free, https://tailscale.com) on this computer AND your phone, the phone view also works away from
    home, over an encrypted private network, with nothing opened to the public internet."""
    program_files = os.environ.get("ProgramFiles", "C:/Program Files")
    candidates = [shutil.which("tailscale"), os.path.join(program_files, "Tailscale", "tailscale.exe"), "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
    for exe in filter(None, candidates):
        if not os.path.exists(exe):
            continue
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=4,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            if re.fullmatch(r"100\.\d{1,3}\.\d{1,3}\.\d{1,3}", line.strip()):
                return line.strip()
    return None


class RemoteAuth:
    """The PIN, the signed-in phones and the lock-outs. Pure logic, no network: easy to test."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.pin: Optional[str] = None
        self.tokens: set[str] = set()
        self.fails: dict[str, tuple[int, float]] = {}   # device -> (wrong PINs in a row, locked until)
        self.total_fails = 0
        self.rotated_by_attack = False

    def reset(self) -> str:
        """A fresh PIN; every phone signed in with the old one is signed out."""
        with self.lock:
            self.pin = f"{secrets.randbelow(1_000_000):06d}"
            self.tokens.clear()
            self.fails.clear()
            self.total_fails = 0
            return self.pin

    def clear(self) -> None:
        with self.lock:
            self.pin = None
            self.tokens.clear()
            self.fails.clear()
            self.total_fails = 0

    def login(self, device: str, pin: str) -> tuple[Optional[str], str]:
        """Returns (token, "") when the PIN is right, else (None, why)."""
        with self.lock:
            now = self.clock()
            count, until = self.fails.get(device, (0, 0.0))
            if until > now:
                return None, f"too many wrong PINs: try again in {int(until - now) + 1} seconds"
            if self.pin and hmac.compare_digest(str(pin).strip().encode("utf-8"), self.pin.encode("ascii")):
                self.fails.pop(device, None)
                token = secrets.token_urlsafe(24)
                self.tokens.add(token)
                return token, ""
            count += 1
            self.total_fails += 1
            self.fails[device] = (0, now + LOCK_SECONDS) if count >= LOCK_AFTER else (count, 0.0)
            if self.total_fails >= ROTATE_AFTER:   # someone is guessing: the PIN they were guessing against is gone
                self.pin = f"{secrets.randbelow(1_000_000):06d}"
                self.tokens.clear()
                self.total_fails = 0
                self.rotated_by_attack = True
            return None, "wrong PIN"

    def valid(self, token: Optional[str]) -> bool:
        with self.lock:
            return bool(token) and any(hmac.compare_digest(token.encode("utf-8"), t.encode("ascii")) for t in self.tokens)

    def signed_in(self) -> int:
        with self.lock:
            return len(self.tokens)


def cookie_value(header: Optional[str], name: str = COOKIE) -> Optional[str]:
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Software Factory</title>
<style>:root{color-scheme:dark light}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0e1014;color:#f2f3f5;font:16px system-ui,sans-serif}
main{width:min(92vw,360px);padding:24px}h1{font-size:20px;margin:0 0 6px}p{color:#9aa0aa;margin:0 0 18px;line-height:1.45}
input{width:100%;box-sizing:border-box;font:600 28px ui-monospace,monospace;letter-spacing:.35em;text-align:center;padding:14px;border-radius:12px;border:1px solid #333a46;background:#171a21;color:inherit}
button{width:100%;margin-top:12px;padding:14px;border:0;border-radius:12px;background:#c8f560;color:#10130a;font:600 16px system-ui}
#e{color:#ff8a7a;min-height:1.4em;margin-top:10px}</style></head><body><main>
<h1>Software Factory</h1><p>Type the 6-digit PIN shown on your computer (Phone &amp; power).</p>
<form id="f"><input id="p" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]*" aria-label="PIN" autofocus><button>Open</button><div id="e" role="alert"></div></form>
<script>document.getElementById("f").onsubmit=async e=>{e.preventDefault();const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json","X-SF-Dashboard":"1"},
body:JSON.stringify({pin:document.getElementById("p").value})});const j=await r.json().catch(()=>({}));if(r.ok)location.replace("/");else document.getElementById("e").textContent=j.error||"Could not sign in"}</script>
</main></body></html>"""
