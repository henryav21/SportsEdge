"""Start the dashboard and the remote address when you sign in to Windows.

One small file in your Startup folder does it: it runs `sportsedge share`, which starts the dashboard if it
is not running and connects the address you reach it on from anywhere. Nothing else on the computer is
changed. Delete that file, or run `sportsedge autostart off`, to undo it.
"""
from __future__ import annotations

import os
from pathlib import Path

NAME = "sportsedge-start.cmd"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if os.name != "nt" or not appdata:
        raise RuntimeError("Starting with the computer is set up for Windows only.")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def path() -> Path:
    return startup_dir() / NAME


def launcher() -> Path:
    return Path(__file__).resolve().parent.parent / "sportsedge.cmd"


def script() -> str:
    """Opens a minimised window that runs the share command from the project folder. If the dashboard is
    already running, the share command uses it instead of starting a second one."""
    root = launcher().parent
    return ("@echo off\r\n"
            "rem Starts the sportsedge dashboard and its remote address when you sign in.\r\n"
            "rem Created by:  sportsedge autostart on      Remove with:  sportsedge autostart off\r\n"
            f'start "sportsedge" /min /d "{root}" cmd /c ""{launcher()}" share"\r\n')


def enabled() -> bool:
    try:
        return path().exists()
    except RuntimeError:
        return False


def enable() -> Path:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(script(), encoding="ascii")
    return p


def disable() -> bool:
    p = path()
    if p.exists():
        p.unlink()
        return True
    return False
