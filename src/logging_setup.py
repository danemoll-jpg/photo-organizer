"""Shared logging setup for every entry point (CLI `main.py` and the
desktop `dashboard.py`) so they write to — and the dashboard's log viewer
reads from — the exact same per-run log files in `logs/`. There is
deliberately no second/parallel logging path for the GUI (see CLAUDE.md
rule 3)."""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path


def new_log_path(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    # Microsecond resolution, not just seconds: the dashboard can call this
    # from two different worker threads within the same wall-clock second
    # (e.g. clicking "Start Captioning" then "Start GPS Extraction" close
    # together) -- found while fixing the "GPS logs appearing in the
    # Captioning panel" bug (TODO.md) that two setup_logging() calls in the
    # same second used to collide on an identical second-resolution
    # filename, so their two FileHandlers silently appended to the SAME
    # file regardless of any other fix. The while-loop below is a cheap
    # belt-and-suspenders guard for the (now vanishingly unlikely, but not
    # impossible) case of an exact microsecond tie too.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = log_dir / f"organize_{timestamp}.log"
    suffix = 1
    while path.exists():
        path = log_dir / f"organize_{timestamp}_{suffix}.log"
        suffix += 1
    return path


def setup_logging(
    log_dir: Path, echo_to_console: bool = True, logger_name: str = "photo_organizer"
) -> tuple[logging.Logger, Path]:
    """Creates a new timestamped log file under log_dir and wires up the
    named logger (default 'photo_organizer') to write to it (plus stdout
    when echo_to_console). Returns (logger, log_path) — the dashboard needs
    log_path to know which file to tail.

    `logger_name` matters for the dashboard specifically: it runs Phase 1
    (organize), Phase 2 (captioning), and Phase 2b (GPS extraction) each in
    their own worker thread, and per CLAUDE.md all three are safe to run
    concurrently. `logging.getLogger(name)` always returns the SAME object
    for a given name — so if every worker called this with the default
    name, they'd all be repointing the one shared logger's handler at
    whichever worker's log file was started most recently, silently
    redirecting every OTHER already-running worker's subsequent log lines
    into that file too (a real bug found and fixed this session — see
    TODO.md's "GPS logging appearing in the Captioning panel" entry:
    GPS's own lines were landing in the Captioning panel's log box because
    Captioning had been started after GPS and "stole" the shared logger).
    dashboard.py now passes a distinct logger_name per worker
    ("photo_organizer.organize"/".caption"/".gps") so each gets its own
    independent Logger object with its own handler, immune to another
    worker calling this function later. `propagate=False` below then keeps
    a child logger's messages from also leaking up into a same-named
    ancestor logger (e.g. plain "photo_organizer") that a different worker
    might be using. main.py and review_tool.py don't pass this (each runs
    one thing at a time in its own process, so there's no shared-object
    risk) and keep using the default name/behavior unchanged."""
    log_path = new_log_path(log_dir)

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    if echo_to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console_handler)

    logger.info(f"Log file: {log_path}")
    return logger, log_path
