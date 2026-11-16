"""PhotoOrganizer.exe -- the tiny, stable launcher. Stdlib only.

It never changes between app updates: it reads state.json (next to itself),
starts versions/<current>/PhotoOrganizerApp.exe and exits. That is what lets
an update be "extract a new folder, flip state.json" with no file ever
replaced while in use.

Self-healing: if the current version dies within a few seconds of starting
(a bad release), the launcher flips back to the previous version, starts
that, and says so -- an update can't strand someone with an app that won't
open.

Keep this file independent of src/ (it is built as its own small exe). The
constants below must match src/updater.py (tests/test_updater.py checks it).
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE_FILE = "state.json"
VERSIONS_DIR = "versions"
APP_EXE = "PhotoOrganizerApp.exe"
STARTUP_WATCH_SECONDS = 8.0


def _msgbox(text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "Photo Organizer", 0x10)
    except Exception:
        pass


def install_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def read_state(root: Path) -> dict:
    try:
        data = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(root: Path, state: dict) -> None:
    tmp = root / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, root / STATE_FILE)


def app_path(root: Path, version) -> Path | None:
    if not isinstance(version, str) or not version or any(c in version for c in "/\\"):
        return None
    p = root / VERSIONS_DIR / version / APP_EXE
    return p if p.is_file() else None


def start(root: Path, exe: Path, args: list[str]) -> subprocess.Popen:
    env = dict(os.environ, PHOTO_ORGANIZER_INSTALL_ROOT=str(root))
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen([str(exe), *args], cwd=str(exe.parent), env=env, creationflags=flags, close_fds=True)


def main() -> int:
    root = install_root()
    state = read_state(root)
    current, previous = state.get("current"), state.get("previous")
    exe = app_path(root, current)
    if exe is None:
        fallback = app_path(root, previous)
        if fallback is None:
            _msgbox(
                "Photo Organizer can't find an installed version.\n\n"
                f"Looked in: {root / VERSIONS_DIR}\n\nRe-download and extract the full ZIP."
            )
            return 1
        write_state(root, {"current": previous, "previous": None})
        current, previous, exe = previous, None, fallback

    proc = start(root, exe, sys.argv[1:])
    deadline = time.monotonic() + STARTUP_WATCH_SECONDS
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is None:
            time.sleep(0.25)
            continue
        if code == 0:
            return 0
        prev_exe = app_path(root, previous)
        if prev_exe is None:
            _msgbox(f"Photo Organizer {current} closed unexpectedly (exit code {code}).")
            return code
        write_state(root, {"current": previous, "previous": None})
        _msgbox(
            f"Version {current} failed to start, so Photo Organizer went back to {previous}.\n"
            "(Your photos and settings were not touched.)"
        )
        start(root, prev_exe, sys.argv[1:])
        return 0
    return 0  # still running after the watch window: it started fine


if __name__ == "__main__":
    sys.exit(main())
