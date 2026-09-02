#!/usr/bin/env python
"""Photo Organizer CLI.

    venv\\Scripts\\python main.py init-db
    venv\\Scripts\\python main.py pick-sources
    venv\\Scripts\\python main.py scan              # dry-run preview, always safe
    venv\\Scripts\\python main.py run                # honors config.yaml's dry_run
    venv\\Scripts\\python main.py run --execute        # forces a real run (asks to confirm)
    venv\\Scripts\\python main.py run --dry-run          # forces a preview even if config says execute
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from src.config import load_config
from src.db import connect, init_db
from src.organize import run_phase1


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"organize_{timestamp}.log"

    logger = logging.getLogger("photo_organizer")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    logger.info(f"Log file: {log_path}")
    return logger


def cmd_init_db(args) -> None:
    cfg = load_config()
    init_db(cfg.db_path_abs)
    print(f"Database ready at {cfg.db_path_abs}")


def cmd_pick_sources(args) -> None:
    from src.pick_sources import main as pick_main
    pick_main()


def cmd_scan(args) -> None:
    _do_run(force_dry_run=True, force_execute=False, skip_confirm=True)


def cmd_run(args) -> None:
    _do_run(force_dry_run=args.dry_run, force_execute=args.execute, skip_confirm=args.yes)


def _do_run(force_dry_run: bool, force_execute: bool, skip_confirm: bool) -> None:
    cfg = load_config()

    dry_run = cfg.dry_run
    if force_dry_run:
        dry_run = True
    elif force_execute:
        dry_run = False

    if not cfg.source_folders:
        print("WARNING: config.yaml has no source_folders configured.")
        print(f"Only {cfg.dest_root} itself will be scanned (for loose/unsorted files already there).")
        print("Run `python main.py pick-sources` to add more, or edit config.yaml directly.\n")

    if not dry_run and not skip_confirm:
        print(f"About to COPY-VERIFY-DELETE photos from:")
        for f in cfg.source_folders:
            print(f"  - {f}")
        print(f"  - {cfg.dest_root} (for loose/misplaced files already there)")
        print(f"into: {cfg.dest_root}\\YYYY\\YYYY-MM\\")
        print("Originals are only deleted after their copy is verified byte-for-byte.\n")
        answer = input("Type 'yes' to proceed: ").strip().lower()
        if answer != "yes":
            print("Aborted. No files were touched.")
            return

    init_db(cfg.db_path_abs)  # safe/idempotent — CREATE TABLE IF NOT EXISTS
    logger = setup_logging(cfg.log_dir_abs)
    logger.info(f"Starting Phase 1 run — dry_run={dry_run}")

    cfg.dry_run = dry_run  # CLI flags (if any) take precedence over the config.yaml value

    conn = connect(cfg.db_path_abs)
    try:
        stats = run_phase1(cfg, conn, logger)
    finally:
        conn.close()

    print("\n--- Summary ---")
    print(stats.summary())
    if dry_run:
        print("\nThis was a DRY RUN — nothing was copied, moved, or deleted.")
        print("Review the log above, then run with --execute to actually apply it.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Photo Organizer — Phase 0/1")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create the SQLite database and schema").set_defaults(func=cmd_init_db)
    sub.add_parser("pick-sources", help="Interactively choose source folders (folder picker)").set_defaults(func=cmd_pick_sources)
    sub.add_parser("scan", help="Preview Phase 1 (always dry-run, never touches files)").set_defaults(func=cmd_scan)

    run_parser = sub.add_parser("run", help="Run Phase 1 (honors config.yaml's dry_run unless overridden)")
    run_parser.add_argument("--execute", action="store_true", help="Force a real run even if config.yaml has dry_run: true")
    run_parser.add_argument("--dry-run", action="store_true", help="Force a preview even if config.yaml has dry_run: false")
    run_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt before a real run")
    run_parser.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
