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
"""
from __future__ import annotations

import subprocess


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
