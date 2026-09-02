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
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return log_dir / f"organize_{timestamp}.log"


def setup_logging(log_dir: Path, echo_to_console: bool = True) -> tuple[logging.Logger, Path]:
    """Creates a new timestamped log file under log_dir and wires up the
    'photo_organizer' logger to write to it (plus stdout when
    echo_to_console). Returns (logger, log_path) — the dashboard needs
    log_path to know which file to tail."""
    log_path = new_log_path(log_dir)

    logger = logging.getLogger("photo_organizer")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

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
