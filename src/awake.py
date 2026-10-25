"""Keep the computer awake while the factory works, so a run (and the phone view) isn't cut off by sleep.

Standard library only. One `KeepAwake` object lives in the dashboard process:

    awake = KeepAwake()
    awake.start(mode="runs")        # stay awake until no run is active for a couple of minutes (max 12 h)
    awake.start(mode="hours", hours=2)
    awake.status()                  # {"on": True, "mode": ..., "until": ..., "supported": True, "how": ...}
    awake.stop()

It stops the computer from going to SLEEP. The screen may still turn off. A laptop with its lid closed can still
sleep (that is a power setting only you can change). Everything is released when the dashboard stops.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

MAX_HOURS = 12                 # a safety stop: never keep a computer awake longer than this
IDLE_GRACE = 120               # mode "runs": seconds with no active run before letting go
WIN_CONTINUOUS, WIN_SYSTEM_REQUIRED = 0x80000000, 0x00000001


class KeepAwake:
    def __init__(self, busy: Optional[Callable[[], bool]] = None, tick: float = 5.0):
        self.busy = busy or (lambda: False)   # "is any run active right now?" (supplied by the dashboard)
        self.tick = tick
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self.mode: Optional[str] = None
        self.until: Optional[float] = None
        self.started: Optional[float] = None
        self.reason = ""                       # why it last switched off, shown on the page

    # ------------------------------------------------------------------ what this system can do
    @staticmethod
    def how() -> Optional[str]:
        if os.name == "nt":
            return "Windows"
        if sys.platform == "darwin":
            return "caffeinate"
        for tool in ("systemd-inhibit",):
            if any(os.access(os.path.join(p, tool), os.X_OK) for p in os.environ.get("PATH", "").split(os.pathsep)):
                return tool
        return None

    def supported(self) -> bool:
        return self.how() is not None

    # ------------------------------------------------------------------ control
    def start(self, mode: str = "runs", hours: float = 2.0) -> dict:
        if not self.supported():
            raise RuntimeError("this computer has no way for the factory to keep it awake (macOS, Windows or systemd Linux are supported)")
        if mode not in ("runs", "hours"):
            raise ValueError("mode must be 'runs' or 'hours'")
        hours = max(0.25, min(float(hours), MAX_HOURS))
        self.stop(reason="")
        with self._lock:
            now = time.time()
            self.mode, self.started, self.reason = mode, now, ""
            self.until = now + (hours if mode == "hours" else MAX_HOURS) * 3600
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True)
            self._thread.start()
        return self.status()

    def stop(self, reason: str = "turned off") -> dict:
        with self._lock:
            ev, th = self._stop, self._thread
            self._thread = None
            self.mode = self.until = self.started = None
            if reason:
                self.reason = reason
        ev.set()
        if th and th is not threading.current_thread():
            th.join(timeout=5)
        return self.status()

    def status(self) -> dict:
        on = bool(self._thread and self._thread.is_alive() and self.mode)
        return {"on": on, "mode": self.mode if on else None, "until": int(self.until) if on and self.until else None,
                "started": int(self.started) if on and self.started else None, "supported": self.supported(),
                "how": self.how(), "reason": "" if on else self.reason}

    # ------------------------------------------------------------------ the worker
    def _run(self, stop: threading.Event) -> None:
        """Holds the 'stay awake' request, and lets go when time is up, the runs are done, or stop() is called.
        On Windows the request belongs to the thread that made it, so the same thread must keep it and clear it."""
        reason = "turned off"
        try:
            self._hold()
            idle_since: Optional[float] = None
            while not stop.wait(self.tick):
                now = time.time()
                if self.until and now >= self.until:
                    reason = "time was up"
                    break
                if self.mode == "runs":
                    if self.busy():
                        idle_since = None
                    else:
                        idle_since = idle_since or now
                        if now - idle_since >= IDLE_GRACE:
                            reason = "all runs finished"
                            break
                if self._proc is not None and self._proc.poll() is not None:  # the helper died: take the request again
                    self._hold()
            else:
                reason = ""   # stop() already recorded why
        finally:
            self._release()
            with self._lock:
                if self._stop is stop:   # not already replaced by a new start()
                    self._thread = None
                    self.mode = self.until = self.started = None
                    if reason:
                        self.reason = reason

    def _hold(self) -> None:
        if os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(WIN_CONTINUOUS | WIN_SYSTEM_REQUIRED)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            self._proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        else:
            self._proc = subprocess.Popen(["systemd-inhibit", "--what=idle:sleep", "--who=Software Factory",
                                           "--why=A run is in progress", "sleep", str(MAX_HOURS * 3600 + 60)],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)

    def _release(self) -> None:
        if os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(WIN_CONTINUOUS)  # type: ignore[attr-defined]
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
