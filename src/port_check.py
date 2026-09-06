"""Windows-only helper for the Remote Access dashboard panel's status check
(Phase 2d follow-up — see CLAUDE.md/TODO.md).

Answers "what's actually bound to this port right now", via `netstat -ano`
— the exact tool used to diagnose the real incident this exists to catch:
four separate stale `review_tool.py` processes were all simultaneously
LISTENING on port 5151, and the one actually serving a phone's request
predated Phase 2d's login code entirely (a phone got straight through with
no auth prompt at all). A simple "does something respond on this port?"
probe would NOT have caught that — something was indeed responding, just
the wrong (stale) something. Counting distinct PIDs bound to LISTENING on
the port is what actually surfaces "more than one process is involved
here", which is the warning signal that matters.

This deliberately doesn't try to confirm the listening process actually
*is* review_tool.py (e.g. by checking its command line) — on this
single-purpose machine, anything listening on the configured
review_tool_port is for all practical purposes review_tool.py, and
`netstat`'s own PID column is enough to answer "how many, and which PIDs"
without pulling in a new dependency (no psutil in requirements.txt) just
for this.

Also holds `restart_review_tool()` (CLAUDE.md rule 11 / TODO.md's
"One-click restart" item) — the one kill-stale-processes-then-relaunch
mechanism shared by "Restart Review Tool.bat" (via `main.py
restart-review-tool`) and the dashboard's Remote Access panel's "Force
Restart" button, so that logic exists in exactly one place (rule 7) rather
than being reimplemented by each of those two consumers.
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import Config


def listening_pids(port: int) -> list[int]:
    """Returns the distinct PIDs currently LISTENING on `port` (empty list
    if none, or if the check itself fails for any reason — e.g. `netstat`
    not found, which shouldn't happen on Windows but shouldn't crash the
    dashboard's status panel either way). More than one PID means stacked/
    stale processes — exactly the pattern the incident above hit."""
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=10,
            # Without this, spawning a console app (netstat.exe) from a
            # process with no console of its own -- exactly how dashboard.py
            # runs, via pythonw.exe -- makes Windows create a brand new
            # console window for the child. dashboard.py's Remote Access
            # panel calls this every 5s for as long as the dashboard is
            # open (self._port_status_tick), so without this flag that's a
            # console window flashing open/closed every 5 seconds for the
            # dashboard's entire lifetime, stealing focus each time -- this
            # was mistaken for a GPS-extraction-specific bug (PRIORITY BUG
            # #1 in TODO.md) since it's a recent addition the user started
            # exercising around the same time as the new GPS panel, but the
            # 5s status tick runs unconditionally regardless of what else
            # is running. dashboard.py's own cloudflared Popen call already
            # uses this same CREATE_NO_WINDOW flag for the identical
            # reason -- this call was just missed. getattr guards this
            # module importing cleanly on a non-Windows platform, where the
            # attribute doesn't exist (though `netstat -p TCP`'s syntax is
            # Windows-specific anyway, so this module has no real non-
            # Windows use).
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []

    needle = f":{port}"
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        # Expected shape: Proto  Local Address  Foreign Address  State  PID
        # e.g. "TCP    0.0.0.0:5151    0.0.0.0:0    LISTENING    12345"
        if len(parts) < 5 or parts[0] != "TCP" or parts[3] != "LISTENING":
            continue
        local_addr = parts[1]
        if not local_addr.endswith(needle):
            continue  # exact suffix match — ":5151" must not match ":51515" etc.
        try:
            pids.add(int(parts[-1]))
        except ValueError:
            continue
    return sorted(pids)


def kill_pids(pids: list[int], timeout: float = 5.0) -> None:
    """Force-kills every PID in `pids` via Windows' `taskkill /F /PID` —
    the same action this project's own incident write-ups have always
    pointed the user at doing manually (Task Manager, or PowerShell's
    `Stop-Process -Id <pid> -Force`). Deliberately kills ALL given PIDs,
    not just the first — the real incident this module exists for found
    FOUR stale review_tool.py processes stacked on one port at once.

    Best-effort per PID: a PID that's already gone (e.g. it exited on its
    own between the caller's `listening_pids()` call and this one) or a
    `taskkill` failure for any other reason is swallowed here rather than
    raised — `wait_for_port_free()` below is what actually confirms
    whether the kill(s) worked, not this function's return value.
    """
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=timeout,
                # Same reasoning as listening_pids() above -- avoid a
                # flashing console window if this ever runs under
                # pythonw.exe (it does: the dashboard's Force Restart
                # button calls this on a background thread).
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass


def wait_for_port_free(port: int, timeout: float = 10.0, poll_interval: float = 0.25) -> bool:
    """Polls `listening_pids(port)` until it comes back empty or `timeout`
    seconds have elapsed. Returns whether the port was actually free by
    the time this returned.

    This exists instead of a fixed `time.sleep()` because a fixed sleep is
    exactly the kind of race this restart mechanism needs to avoid: launch
    the new review_tool.py before Windows has actually released the old
    one's socket, and the new process fails to bind instead of cleanly
    taking over — silently, since the new process's own stdout/stderr are
    redirected away (see restart_review_tool() below). Polling for the
    real condition (nothing listening) is the only way to know for sure.
    """
    deadline = time.monotonic() + timeout
    while True:
        if not listening_pids(port):
            return True
        if time.monotonic() >= deadline:
            return not listening_pids(port)  # one last check right at the deadline
        time.sleep(poll_interval)


@dataclass
class RestartResult:
    """What actually happened during one restart_review_tool() call —
    both consumers (the .bat's CLI command and the dashboard's Force
    Restart button) report this back to the user rather than assuming
    success."""
    killed_pids: list[int]      # PIDs that were found bound to the port and killed (possibly empty)
    port_freed: bool            # did the port actually become free before we tried to relaunch?
    launched: bool              # did we actually spawn a fresh review_tool.py?
    new_pid: int | None         # its PID, if launched


def restart_review_tool(
    cfg: "Config",
    *,
    open_browser: bool = True,
    kill_timeout: float = 10.0,
    python_exe: Path | None = None,
    script_path: Path | None = None,
) -> RestartResult:
    """Kills every process bound to `cfg.review_tool_port`, waits for the
    port to actually free, then launches a fresh review_tool.py — the one
    mechanism CLAUDE.md rule 11 depends on. Reads the port from config,
    never hardcodes 5151.

    `python_exe`/`script_path` default to this repo's own venv interpreter
    and review_tool.py, but are overridable so tests can point this at a
    harmless stand-in process instead of spinning up the real Flask app —
    same "substitute a stand-in for the real subprocess" testing
    convention dashboard.py's cloudflared Start/Stop already uses (see
    tests/test_port_check.py).

    If something was listening and the port never actually frees up
    within `kill_timeout`, this deliberately does NOT launch a new
    instance anyway — doing so would just spawn a second process doomed
    to fail its own bind (silently, since its stdout/stderr are
    redirected away below) rather than surfacing a clear "didn't work"
    result the caller can show the user.
    """
    port = cfg.review_tool_port
    killed = listening_pids(port)
    if killed:
        kill_pids(killed, timeout=kill_timeout)
        port_freed = wait_for_port_free(port, timeout=kill_timeout)
    else:
        port_freed = True

    if not port_freed:
        return RestartResult(killed_pids=killed, port_freed=False, launched=False, new_pid=None)

    from src.config import REPO_ROOT  # local import: keep listening_pids() usable with zero src.config dependency

    if python_exe is None:
        python_exe = REPO_ROOT / "venv" / "Scripts" / "python.exe"
        if not python_exe.exists():
            python_exe = Path(sys.executable)  # fallback, e.g. a differently-named/located venv
    if script_path is None:
        script_path = REPO_ROOT / "review_tool.py"

    # --port is passed explicitly (even though a freshly-launched
    # review_tool.py would read the same value from config.yaml on its
    # own) so there's no ambiguity between "the port this caller checked"
    # and "whatever the new process happens to load" -- they're
    # guaranteed to be the same value.
    args = [str(python_exe), str(script_path), "--port", str(port)]
    if not open_browser:
        args.append("--no-browser")

    proc = subprocess.Popen(
        args,
        cwd=str(REPO_ROOT),
        # Redirected (not inherited) so this works identically whether the
        # caller is main.py (a console python.exe) or dashboard.py (a
        # console-less pythonw.exe) -- see CLAUDE.md's recurring
        # "something doing its own console I/O misbehaves under
        # pythonw.exe" pattern. With these redirected, review_tool.py's
        # sys.stdout/sys.stderr are real (if discarded) file objects in
        # either case, never None.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return RestartResult(killed_pids=killed, port_freed=True, launched=True, new_pid=proc.pid)
