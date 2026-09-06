"""Tests for src/port_check.py's restart mechanism (CLAUDE.md rule 11 /
TODO.md's "One-click restart" item): kill_pids(), wait_for_port_free(),
and restart_review_tool() -- the one shared implementation "Restart Review
Tool.bat" and the dashboard's Remote Access panel's "Force Restart" button
both call.

This deliberately spawns REAL OS processes bound to a REAL (throwaway,
high, unlikely-to-collide) TCP port rather than mocking anything -- per
this session's own instruction to test the actual kill-wait-relaunch
sequence for real, not just each piece in isolation. Uses
tests/_restart_stub_server.py as a harmless stand-in for review_tool.py
itself (same "substitute a stand-in subprocess" convention as
dashboard.py's cloudflared Start/Stop tests) so this never needs the real
Flask app, DB, or captions.jsonl just to prove the restart mechanism
works.

Usage:
    venv\\Scripts\\python tests\\test_port_check.py
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config
from src.port_check import kill_pids, listening_pids, restart_review_tool, wait_for_port_free

STUB_SCRIPT = Path(__file__).resolve().parent / "_restart_stub_server.py"


def _free_port() -> int:
    """Finds a currently-unused port by binding to port 0 and reading back
    what the OS assigned, then immediately releasing it. Small race window
    between release and the caller actually using it -- fine for a test
    run on a single dev machine, not a concern in production code."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn_stub(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(STUB_SCRIPT), "--port", str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_until_listening(port: int, expected_count: int, timeout: float = 5.0) -> list[int]:
    deadline = time.monotonic() + timeout
    pids: list[int] = []
    while time.monotonic() < deadline:
        pids = listening_pids(port)
        if len(pids) >= expected_count:
            return pids
        time.sleep(0.1)
    return pids


def test_kill_pids_and_wait_for_port_free() -> None:
    # Note: the PID netstat reports as LISTENING is treated as ground
    # truth throughout this file, not Popen's own .pid -- in some
    # sandboxed/virtualized environments the two can legitimately differ
    # (e.g. a process-creation broker in between), and restart_review_tool()
    # itself never assumes they match either: it always re-derives which
    # PIDs to kill from listening_pids(), never from a Popen handle it
    # happens to be holding.
    port = _free_port()
    proc = _spawn_stub(port)
    try:
        pids = _wait_until_listening(port, 1)
        assert len(pids) == 1, f"expected exactly 1 process listening on {port}, got {pids}"

        kill_pids(pids)
        freed = wait_for_port_free(port, timeout=5.0)
        assert freed, f"port {port} did not free up after kill_pids()"
        assert listening_pids(port) == []
        print("  kill_pids() + wait_for_port_free() actually free a real bound port  OK")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_restart_review_tool_kills_all_stale_and_relaunches() -> None:
    """The real end-to-end sequence: THREE separate stale processes (the
    real incident this whole mechanism exists for found four at once —
    see CLAUDE.md) all bound to the same port, then one restart_review_tool()
    call kills every one of them, confirms the port actually freed, and
    launches exactly one fresh instance."""
    port = _free_port()
    stale_procs = [_spawn_stub(port) for _ in range(3)]
    new_proc_pid: int | None = None
    try:
        stale_pids = _wait_until_listening(port, 3)
        assert len(stale_pids) == 3, (
            f"expected 3 stale processes actually LISTENING on {port} before the test even "
            f"starts, got {stale_pids} -- can't test the real kill-multiple behavior without this"
        )

        cfg = replace(load_config(), review_tool_port=port)
        result = restart_review_tool(
            cfg,
            open_browser=False,
            python_exe=Path(sys.executable),
            script_path=STUB_SCRIPT,
        )

        assert sorted(result.killed_pids) == sorted(stale_pids), (
            f"expected all 3 stale PIDs {stale_pids} killed, restart_review_tool() reports "
            f"{result.killed_pids}"
        )
        assert result.port_freed, "restart_review_tool() reports the port never actually freed"
        assert result.launched, "restart_review_tool() should have launched a fresh instance"
        assert result.new_pid is not None
        new_proc_pid = result.new_pid

        # Confirm for real: exactly one (fresh, not a leftover stale one)
        # process ends up bound to the port -- not just that the function
        # claims it did. Not compared against result.new_pid itself (see
        # the note atop this file) since some sandboxed environments track
        # a Popen's own .pid separately from whichever PID actually ends
        # up holding the socket.
        final_pids = _wait_until_listening(port, 1)
        assert len(final_pids) == 1, f"expected exactly 1 process listening on {port} afterward, got {final_pids}"
        for stale_pid in stale_pids:
            assert stale_pid not in final_pids, f"stale PID {stale_pid} is still listening — not actually killed"

        print(
            "  restart_review_tool() kills all 3 stale processes, waits for the port to actually "
            "free, and relaunches exactly one fresh instance  OK"
        )
    finally:
        # Best-effort cleanup: kill anything from this test still alive
        # (the 3 originals should already be dead; the new one is the
        # real thing left running and needs killing here since nothing
        # else in this test suite will).
        leftover_pids = listening_pids(port)
        if leftover_pids:
            kill_pids(leftover_pids)
        for proc in stale_procs:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        wait_for_port_free(port, timeout=5.0)


def test_restart_review_tool_launches_when_nothing_was_running() -> None:
    """The "nothing stale to begin with" case -- restart_review_tool()
    should still launch a fresh instance (this is what the dashboard's
    Force Restart button does when review_tool.py just isn't running at
    all, per its own confirmation-dialog wording)."""
    port = _free_port()
    assert listening_pids(port) == []

    cfg = replace(load_config(), review_tool_port=port)
    result = restart_review_tool(
        cfg,
        open_browser=False,
        python_exe=Path(sys.executable),
        script_path=STUB_SCRIPT,
    )
    try:
        assert result.killed_pids == []
        assert result.port_freed is True
        assert result.launched is True
        assert result.new_pid is not None
        final_pids = _wait_until_listening(port, 1)
        assert len(final_pids) == 1, f"expected exactly 1 process listening on {port}, got {final_pids}"
        print("  restart_review_tool() launches fine even when nothing was previously bound  OK")
    finally:
        leftover_pids = listening_pids(port)
        if leftover_pids:
            kill_pids(leftover_pids)
        wait_for_port_free(port, timeout=5.0)


def main() -> None:
    test_kill_pids_and_wait_for_port_free()
    test_restart_review_tool_kills_all_stale_and_relaunches()
    test_restart_review_tool_launches_when_nothing_was_running()
    print("\nALL PORT_CHECK RESTART TESTS PASSED")


if __name__ == "__main__":
    main()
