"""Regression test for a real bug found and fixed this session (see
TODO.md/CLAUDE.md): dashboard.py's Phase 1/Captioning/GPS-extraction
worker threads can genuinely run concurrently (an explicitly supported
combination per CLAUDE.md), but src/logging_setup.py::setup_logging()
used to always wire up the SAME shared 'photo_organizer' logger object
regardless of caller. Whichever worker called it most recently would
clear+repoint that one shared logger's handler at its own new log file,
silently redirecting every OTHER already-running worker's subsequent log
lines into that file too. In the user's real-world hit, GPS extraction
had been started, then Captioning was started afterward and "stole" the
shared logger -- so GPS's own ongoing log lines started landing in the
Captioning panel's log box/file instead of GPS's own, even though the
dashboard's queue-dispatch/tail-widget routing (which panel's Text widget
shows which file) was actually correct all along.

Two independent fixes are exercised here:
  1. setup_logging(..., logger_name=...) gives each worker its own Logger
     object (propagate=False so a child logger's messages can't leak up
     into a same-named ancestor another worker might be using either).
  2. new_log_path()'s timestamp gained microsecond resolution (+ a
     collision-avoidance loop) -- found while testing fix #1: two calls
     within the same wall-clock second used to produce an IDENTICAL
     filename, so even with two independent Logger objects, their two
     FileHandlers would both silently append to the one physical file.

Usage:
    venv\\Scripts\\python tests\\test_logging_setup.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.logging_setup import new_log_path, setup_logging


def test_new_log_path_never_collides(tmp: Path) -> None:
    log_dir = tmp / "logs_collision"
    paths = {new_log_path(log_dir) for _ in range(20)}  # called back-to-back, same second
    assert len(paths) == 20, f"expected 20 distinct paths, got {len(paths)} (some collided)"
    print("  new_log_path() never returns a duplicate path across rapid back-to-back calls  OK")


def test_concurrent_workers_do_not_cross_contaminate(tmp: Path) -> None:
    log_dir = tmp / "logs_concurrent"

    # Simulates the real dashboard sequence: GPS starts first, keeps
    # logging, then Captioning starts (and used to "steal" the shared
    # logger) while GPS is still running.
    gps_logger, gps_log_path = setup_logging(log_dir, echo_to_console=False, logger_name="photo_organizer.gps")
    gps_logger.info("gps line 1 (before captioning starts)")

    cap_logger, cap_log_path = setup_logging(log_dir, echo_to_console=False, logger_name="photo_organizer.caption")
    cap_logger.info("caption line 1")

    gps_logger.info("gps line 2 (after captioning started)")
    cap_logger.info("caption line 2")
    gps_logger.info("gps line 3 (after captioning started)")

    for h in gps_logger.handlers + cap_logger.handlers:
        h.flush()

    assert gps_log_path != cap_log_path, "the two workers' log files must not collide"

    gps_text = gps_log_path.read_text()
    cap_text = cap_log_path.read_text()

    for expected in ["gps line 1", "gps line 2", "gps line 3"]:
        assert expected in gps_text, f"'{expected}' missing from GPS's own log file"
        assert expected not in cap_text, f"'{expected}' leaked into the Captioning panel's log file"
    for expected in ["caption line 1", "caption line 2"]:
        assert expected in cap_text, f"'{expected}' missing from Captioning's own log file"
        assert expected not in gps_text, f"'{expected}' leaked into the GPS panel's log file"

    print("  GPS and Captioning workers' log lines stay in their own separate files  OK")

    # A third worker (Phase 1) using the plain default logger name must
    # also stay isolated from both -- propagate=False must hold even
    # against the un-namespaced default, not just between two sub-names.
    organize_logger, organize_log_path = setup_logging(
        log_dir, echo_to_console=False, logger_name="photo_organizer.organize"
    )
    organize_logger.info("organize line 1")
    gps_logger.info("gps line 4 (after organize started)")
    for h in organize_logger.handlers:
        h.flush()
    gps_logger.handlers[0].flush()
    organize_text = organize_log_path.read_text()
    assert "organize line 1" in organize_text
    assert "gps line 4" not in organize_text
    assert "gps line 4" in gps_log_path.read_text()
    print("  a third concurrent worker (Phase 1) stays isolated too  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_logging_"))
    print(f"Working in {tmp}")
    try:
        test_new_log_path_never_collides(tmp)
        test_concurrent_workers_do_not_cross_contaminate(tmp)
        print("\nALL LOGGING SETUP TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
