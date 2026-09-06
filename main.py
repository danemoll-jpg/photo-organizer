#!/usr/bin/env python
"""Photo Organizer CLI.

    venv\\Scripts\\python main.py init-db
    venv\\Scripts\\python main.py pick-sources
    venv\\Scripts\\python main.py scan              # dry-run preview, always safe
    venv\\Scripts\\python main.py run                # honors config.yaml's dry_run
    venv\\Scripts\\python main.py run --execute        # forces a real run (asks to confirm)
    venv\\Scripts\\python main.py run --dry-run          # forces a preview even if config says execute
    venv\\Scripts\\python main.py caption                # Phase 2: caption already-organized photos via Ollama
    venv\\Scripts\\python main.py caption --limit <folder>  # caption just one folder first
    venv\\Scripts\\python main.py extract-gps                # Phase 2b: EXIF GPS -> offline reverse-geocoded place name
    venv\\Scripts\\python main.py extract-gps --limit <folder prefix>  # just one folder first
    venv\\Scripts\\python main.py extract-thumbnails                # Phase 2g: cache a real first-frame thumbnail per video
    venv\\Scripts\\python main.py extract-thumbnails --limit <folder prefix>  # just one folder first
    venv\\Scripts\\python main.py review-user add <username>    # Phase 2d: add/update a review_tool.py login
    venv\\Scripts\\python main.py review-user list              # list configured usernames
    venv\\Scripts\\python main.py review-user remove <username> # revoke a login
    venv\\Scripts\\python main.py restart-review-tool           # kill stale review_tool.py process(es), relaunch fresh
"""
from __future__ import annotations

import argparse
from pathlib import Path

from tqdm import tqdm

from src.caption import run_phase2
from src.config import load_config
from src.db import connect, init_db
from src.gps_backfill import run_gps_extraction
from src.load_captions import load_captions
from src.logging_setup import setup_logging
from src.organize import run_phase1
from src.port_check import restart_review_tool
from src.thumbnail_backfill import run_thumbnail_extraction


def cmd_init_db(args) -> None:
    import logging
    cfg = load_config()
    logger = logging.getLogger("photo_organizer")
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)
    init_db(cfg.db_path_abs, logger=logger)
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
        print(f"About to COPY-VERIFY-DELETE photos/videos from:")
        for f in cfg.source_folders:
            print(f"  - {f}")
        print(f"  - {cfg.dest_root} (for loose/misplaced files already there)")
        print(f"into: {cfg.dest_root}\\YYYY\\YYYY-MM\\")
        print("Originals are only deleted after their copy is verified byte-for-byte.\n")
        answer = input("Type 'yes' to proceed: ").strip().lower()
        if answer != "yes":
            print("Aborted. No files were touched.")
            return

    logger, _log_path = setup_logging(cfg.log_dir_abs)
    init_db(cfg.db_path_abs, logger=logger)  # safe/idempotent — CREATE TABLE IF NOT EXISTS + any pending migration
    logger.info(f"Starting Phase 1 run — dry_run={dry_run}")

    cfg.dry_run = dry_run  # CLI flags (if any) take precedence over the config.yaml value

    # CLI-only progress bar, driven by the same progress_cb the dashboard
    # uses for its Tk bar — run_phase1 itself stays presentation-agnostic
    # (it must not touch stdout/stderr directly: the dashboard runs under
    # pythonw.exe, which has neither).
    pbar: tqdm | None = None

    def _progress_cb(done: int, total: int) -> None:
        nonlocal pbar
        if pbar is None:
            pbar = tqdm(total=total, desc="Phase 1", unit="file")
        pbar.n = done
        pbar.refresh()

    conn = connect(cfg.db_path_abs)
    try:
        stats = run_phase1(cfg, conn, logger, progress_cb=_progress_cb)
    finally:
        conn.close()
        if pbar is not None:
            pbar.close()

    print("\n--- Summary ---")
    print(stats.summary())
    if dry_run:
        print("\nThis was a DRY RUN — nothing was copied, moved, or deleted.")
        print("Review the log above, then run with --execute to actually apply it.")


def cmd_caption(args) -> None:
    cfg = load_config()

    if args.limit:
        # --limit <folder>: scan just this one folder instead of dest_root's
        # full tree -- lets a real run be tried against one small folder
        # first, same "batched real runs, no new code needed" pattern as
        # Phase 1 (see CLAUDE.md rule 8).
        source_folders = [Path(args.limit)]
    else:
        source_folders = None  # defaults to cfg.dest_root_path inside run_phase2

    logger, _log_path = setup_logging(cfg.log_dir_abs)
    logger.info(f"Starting Phase 2 (captioning) run — model={cfg.ollama_model}")

    pbar: tqdm | None = None

    def _progress_cb(done: int, total: int) -> None:
        nonlocal pbar
        if pbar is None:
            pbar = tqdm(total=total, desc="Phase 2", unit="file")
        pbar.n = done
        pbar.refresh()

    try:
        stats = run_phase2(cfg, logger, progress_cb=_progress_cb, source_folders=source_folders)
    finally:
        if pbar is not None:
            pbar.close()

    print("\n--- Summary ---")
    print(stats.summary())
    print(f"\nOutput: {cfg.captions_path_abs}")


def cmd_extract_gps(args) -> None:
    cfg = load_config()
    logger, _log_path = setup_logging(cfg.log_dir_abs)
    logger.info("Starting Phase 2b GPS extraction run")
    init_db(cfg.db_path_abs, logger=logger)  # safe/idempotent — applies the gps_* column migration if needed

    pbar: tqdm | None = None

    def _progress_cb(done: int, total: int) -> None:
        nonlocal pbar
        if pbar is None:
            pbar = tqdm(total=total, desc="GPS extract", unit="file")
        pbar.n = done
        pbar.refresh()

    conn = connect(cfg.db_path_abs)
    try:
        stats = run_gps_extraction(cfg, conn, logger, progress_cb=_progress_cb, folder_prefix=args.limit)
    finally:
        conn.close()
        if pbar is not None:
            pbar.close()

    print("\n--- Summary ---")
    print(stats.summary())


def cmd_extract_thumbnails(args) -> None:
    cfg = load_config()
    logger, _log_path = setup_logging(cfg.log_dir_abs)
    logger.info("Starting Phase 2g video thumbnail extraction run")
    # No init_db() call needed here (unlike extract-gps) -- this module never
    # writes to the DB at all, only SELECTs existing video rows; see
    # src/thumbnail_backfill.py's module docstring.

    pbar: tqdm | None = None

    def _progress_cb(done: int, total: int) -> None:
        nonlocal pbar
        if pbar is None:
            pbar = tqdm(total=total, desc="Thumbnails", unit="file")
        pbar.n = done
        pbar.refresh()

    conn = connect(cfg.db_path_abs)
    try:
        stats = run_thumbnail_extraction(cfg, conn, logger, progress_cb=_progress_cb, folder_prefix=args.limit)
    finally:
        conn.close()
        if pbar is not None:
            pbar.close()

    print("\n--- Summary ---")
    print(stats.summary())
    print(f"\nCached to: {cfg.thumbnail_dir_abs}")


def cmd_review_user_add(args) -> None:
    """Phase 2d: add or update a review_tool.py login credential. Prompts
    for the password interactively (hidden input via getpass) rather than
    taking it as a CLI arg, so it never lands in shell history."""
    import getpass

    from src.auth import hash_password
    from src.config import add_or_update_review_user, list_review_usernames

    username = args.username.strip()
    if not username:
        print("ERROR: username cannot be empty.")
        return
    if username in list_review_usernames():
        answer = input(f"'{username}' already has a credential — overwrite its password? [y/N]: ").strip().lower()
        if answer != "y":
            print("Aborted, nothing changed.")
            return

    password = getpass.getpass(f"Password for '{username}' (min 8 chars): ")
    if len(password) < 8:
        print("ERROR: password must be at least 8 characters. Nothing changed.")
        return
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("ERROR: passwords didn't match. Nothing changed.")
        return

    add_or_update_review_user(username, hash_password(password))
    print(f"Saved. '{username}' can now sign in at review_tool.py's login page.")


def cmd_review_user_list(args) -> None:
    from src.config import list_review_usernames
    users = list_review_usernames()
    if not users:
        print("No review_tool.py users configured yet.")
        print("Add one with: main.py review-user add <username>")
        return
    print("Configured review_tool.py users:")
    for u in users:
        print(f"  - {u}")


def cmd_review_user_remove(args) -> None:
    from src.config import remove_review_user
    username = args.username.strip()
    if remove_review_user(username):
        print(f"Removed '{username}' — they can no longer sign in.")
    else:
        print(f"No user named '{username}' found. Nothing changed.")


def cmd_restart_review_tool(args) -> None:
    """CLAUDE.md rule 11 / TODO.md's "One-click restart" item: the CLI
    consumer of src.port_check.restart_review_tool() that "Restart Review
    Tool.bat" shells out to, so the .bat and the dashboard's Force Restart
    button both call the exact same underlying mechanism (rule 7) rather
    than each reimplementing kill-wait-relaunch."""
    cfg = load_config()
    port = cfg.review_tool_port
    result = restart_review_tool(cfg, open_browser=not args.no_browser)

    if result.killed_pids:
        print(f"Killed {len(result.killed_pids)} process(es) previously bound to port {port}: "
              f"{', '.join(str(p) for p in result.killed_pids)}")
    else:
        print(f"Nothing was bound to port {port} — nothing to kill.")

    if not result.port_freed:
        print(f"ERROR: port {port} did not free up in time — a new instance was NOT launched. "
              f"Check what's still holding it (Task Manager, or `netstat -ano | findstr :{port}`) "
              "and try again.")
        raise SystemExit(1)

    print(f"Launched a fresh review_tool.py (PID {result.new_pid}) on port {port}.")


def cmd_load_captions(args) -> None:
    cfg = load_config()
    logger, _log_path = setup_logging(cfg.log_dir_abs)
    init_db(cfg.db_path_abs, logger=logger)  # safe/idempotent
    conn = connect(cfg.db_path_abs)
    try:
        stats = load_captions(cfg.captions_path_abs, conn, logger)
    finally:
        conn.close()
    print("\n--- Summary ---")
    print(stats.summary())


def main() -> None:
    parser = argparse.ArgumentParser(description="Photo Organizer — Phase 0/1/1b/2/2b")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create the SQLite database and schema").set_defaults(func=cmd_init_db)
    sub.add_parser("pick-sources", help="Interactively choose source folders (folder picker)").set_defaults(func=cmd_pick_sources)
    sub.add_parser("scan", help="Preview Phase 1 (always dry-run, never touches files)").set_defaults(func=cmd_scan)

    run_parser = sub.add_parser("run", help="Run Phase 1 (honors config.yaml's dry_run unless overridden)")
    run_parser.add_argument("--execute", action="store_true", help="Force a real run even if config.yaml has dry_run: true")
    run_parser.add_argument("--dry-run", action="store_true", help="Force a preview even if config.yaml has dry_run: false")
    run_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt before a real run")
    run_parser.set_defaults(func=cmd_run)

    caption_parser = sub.add_parser("caption", help="Run Phase 2 (captioning via local Ollama vision model)")
    caption_parser.add_argument("--limit", metavar="FOLDER",
                                 help="Caption only this one folder instead of the full dest_root tree (try a small batch first)")
    caption_parser.set_defaults(func=cmd_caption)

    sub.add_parser("load-captions", help="Load data/captions.jsonl into the SQLite DB (photos.caption + tags)").set_defaults(func=cmd_load_captions)

    gps_parser = sub.add_parser("extract-gps", help="Phase 2b: extract EXIF GPS + offline reverse-geocode into the DB")
    gps_parser.add_argument("--limit", metavar="FOLDER_PREFIX",
                             help="Only process photos whose current_path starts with this prefix (try a small folder first)")
    gps_parser.set_defaults(func=cmd_extract_gps)

    thumbs_parser = sub.add_parser(
        "extract-thumbnails", help="Phase 2g: cache a real first-frame thumbnail per video for review_tool.py's grid"
    )
    thumbs_parser.add_argument("--limit", metavar="FOLDER_PREFIX",
                                help="Only process videos whose current_path starts with this prefix (try a small folder first)")
    thumbs_parser.set_defaults(func=cmd_extract_thumbnails)

    ru_parser = sub.add_parser("review-user", help="Phase 2d: manage review_tool.py login credentials (config.yaml's review_users)")
    ru_sub = ru_parser.add_subparsers(dest="review_user_command", required=True)
    ru_add = ru_sub.add_parser("add", help="Add a new user or change an existing user's password (prompts, hidden input)")
    ru_add.add_argument("username")
    ru_add.set_defaults(func=cmd_review_user_add)
    ru_sub.add_parser("list", help="List configured usernames").set_defaults(func=cmd_review_user_list)
    ru_remove = ru_sub.add_parser("remove", help="Remove a user's credential")
    ru_remove.add_argument("username")
    ru_remove.set_defaults(func=cmd_review_user_remove)

    restart_parser = sub.add_parser(
        "restart-review-tool",
        help="Kill any stale review_tool.py process(es) bound to review_tool_port and relaunch fresh",
    )
    restart_parser.add_argument("--no-browser", action="store_true", help="Don't auto-open a browser tab after relaunching")
    restart_parser.set_defaults(func=cmd_restart_review_tool)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
