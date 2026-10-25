"""Make the factory open by name, like any other app: a Start menu entry (Windows), an app in Applications (macOS) or an
entry in the app menu (Linux), and optionally a Desktop icon. Press the Windows key (or Cmd+Space), type "Software
Factory", press Enter: it starts the factory by itself if it isn't running and opens the dashboard as an app window.

You still clone the factory from GitHub first: the entry points at that folder, so don't move the folder afterwards
(if you do, run this again). Standard library only. Used by `sf.py install-app` and the dashboard's Phone & power panel.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

NAME = "Software Factory"
WIN = os.name == "nt"
MAC = sys.platform == "darwin"


def os_name() -> str:
    return "windows" if WIN else "mac" if MAC else "linux"


def launcher(root: Path) -> Path:
    return Path(root) / ("Start Software Factory.cmd" if WIN else "Start Software Factory.command")


def desktop_dir(custom: Optional[str] = None) -> Path:
    if custom:
        return Path(custom).expanduser()
    if WIN:   # the real Desktop folder (it moves when OneDrive backs it up)
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"], capture_output=True, text=True,
                                 timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip().splitlines()
            if out and Path(out[-1]).is_dir():
                return Path(out[-1])
        except (OSError, subprocess.SubprocessError):
            pass
    return Path.home() / "Desktop"


def menu_dir(custom: Optional[str] = None) -> Path:
    """Where the operating system looks for apps you can open by typing their name."""
    if custom:
        return Path(custom).expanduser()
    if WIN:
        return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    if MAC:
        return Path.home() / "Applications"
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "applications"


def _windows_lnk(lnk: Path, target: Path, icon: Path) -> None:
    q = lambda p: str(p).replace("'", "''")
    script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}');$s.TargetPath='{q(target)}';"
              f"$s.WorkingDirectory='{q(target.parent)}';$s.WindowStyle=7;$s.Description='Start the Software Factory';")
    if icon.exists():
        script += f"$s.IconLocation='{q(icon)}';"
    script += "$s.Save()"
    r = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 or not lnk.exists():
        raise RuntimeError("could not create the shortcut: " + (r.stdout + r.stderr).strip()[-300:])


def _mac_app(app: Path, target: Path) -> None:
    """A tiny app bundle whose only job is to run the launcher. Spotlight (Cmd+Space) finds apps in ~/Applications."""
    if app.exists():
        shutil.rmtree(app)
    (app / "Contents" / "MacOS").mkdir(parents=True)
    plist = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
             '<plist version="1.0"><dict><key>CFBundleName</key><string>Software Factory</string><key>CFBundleDisplayName</key><string>Software Factory</string>'
             '<key>CFBundleIdentifier</key><string>io.github.software-factory</string><key>CFBundleExecutable</key><string>run</string>'
             '<key>CFBundlePackageType</key><string>APPL</string><key>LSUIElement</key><true/></dict></plist>\n')
    (app / "Contents" / "Info.plist").write_text(plist, encoding="utf-8")
    run = app / "Contents" / "MacOS" / "run"
    run.write_text('#!/bin/bash\n# opened from Finder or Spotlight: no shell profile, so add the places uv usually lives\n'
                   'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"\n'
                   f'exec bash "{target}"\n', encoding="utf-8")
    run.chmod(0o755)


def create(where: str, root: Path, folder: Optional[str] = None) -> Path:
    """where: "menu" (open by typing the name) or "desktop" (an icon). Returns what was created."""
    root = Path(root).resolve()
    target = launcher(root)
    if not target.exists():
        raise FileNotFoundError(f"launcher not found: {target}")
    dest = desktop_dir(folder) if where == "desktop" else menu_dir(folder)
    if not dest.is_dir():
        if where == "menu" and folder is None:
            dest.mkdir(parents=True, exist_ok=True)   # the per-user app folder may not exist yet (macOS ~/Applications, Linux)
        else:
            raise FileNotFoundError(f"no folder at {dest}: pass a folder that exists")
    icon_ico, icon_png = root / "dashboard" / "icon.ico", root / "dashboard" / "icon.png"
    if WIN:
        made = dest / f"{NAME}.lnk"
        _windows_lnk(made, target, icon_ico)
    elif MAC:
        app = (menu_dir(folder) if where == "menu" else menu_dir()) / f"{NAME}.app"
        app.parent.mkdir(parents=True, exist_ok=True)
        _mac_app(app, target)
        if where == "desktop":
            made = dest / f"{NAME}.app"
            made.unlink(missing_ok=True)
            made.symlink_to(app)
        else:
            made = app
    else:
        made = dest / ("software-factory.desktop" if where == "menu" else f"{NAME}.desktop")
        made.write_text("[Desktop Entry]\nType=Application\nName=Software Factory\nComment=Start the Software Factory\n"
                        f'Exec=bash "{target}"\nPath={target.parent}\nTerminal=false\nCategories=Development;\n'
                        + (f"Icon={icon_png}\n" if icon_png.exists() else ""), encoding="utf-8")
        made.chmod(0o755)
    return made


def how_to_open() -> str:
    return {"windows": 'Press the Windows key, type "Software Factory" and press Enter.',
            "mac": 'Press Cmd+Space, type "Software Factory" and press Enter.',
            "linux": "Open your applications menu and search for Software Factory."}[os_name()]
