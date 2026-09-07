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
    venv\\Scripts\\python main.py detect-faces                # Phase 3 Session 1: face detection + embeddings -> data/faces.jsonl
    venv\\Scripts\\python main.py detect-faces --limit <folder prefix>  # just one folder first (benchmark before a full run)
    venv\\Scripts\\python main.py cluster-faces               # Phase 3 Session 2: load faces.jsonl + cluster into people (name/merge/split via the dashboard's Face Labeling window)
    venv\\Scripts\\python main.py review-user add <username>    # Phase 2d: add/update a review_tool.py login
    venv\\Scripts\\python main.py review-user list              # list configured usernames
    venv\\Scripts\\python main.py review-user remove <username> # revoke a login
    venv\\Scripts\\python main.py restart-review-tool           # kill stale review_tool.py process(es), relaunch fresh
    venv\\Scripts\\python main.py audit-dates list                  # Step 0: day-level clusters, sorted by count
    venv\\Scripts\\python main.py audit-dates inspect 2023-10-29       # Step 1: deep-dive one suspicious date
    venv\\Scripts\\python main.py audit-dates preview-fix                # Step 2: dry-run proposed corrections
    venv\\Scripts\\python main.py audit-dates apply-fix                    # Step 2: apply via photo_edits audit trail
    venv\\Scripts\\python main.py audit-dates interpolate-preview --target-prefix "E:\\katya 2024" --reference-prefix "E:\\Pics\\2020\\2020-02" --img-max 7093
    venv\\Scripts\\python main.py audit-dates interpolate-apply --target-prefix "E:\\katya 2024" --reference-prefix "E:\\Pics\\2020\\2020-02" --img-max 7093
    venv\\Scripts\\python main.py relocate-files preview          # Phase 3b: dry-run, show which files' folders disagree with their current date_taken
    venv\\Scripts\\python main.py relocate-files apply             # Phase 3b: copy-verify-delete each into its correct YYYY/YYYY-MM folder
"""
from __future__ import annotations

import argparse
from pathlib import Path

from tqdm import tqdm

from src.caption import run_phase2
from src.config import load_config
from src.date_audit import (
    DEFAULT_MIN_COUNT,
    DEFAULT_TOP_N,
    SEQUENCE_FIX_EDITOR,
    apply_fixes,
    find_fix_candidates,
    find_sequence_interpolation_candidates,
    find_suspicious_clusters,
    format_fix_preview,
    format_inspection_report,
    format_interpolation_preview,
    inspect_cluster,
)
from src.db import connect, init_db
from src.face_clustering import cluster_faces_full
from src.face_detection import run_face_detection
from src.gps_backfill import run_gps_extraction
from src.load_captions import load_captions
from src.logging_setup import setup_logging
from src.organize import run_phase1
from src.port_check import restart_review_tool
from src.relocate import apply_relocations, find_relocation_candidates, format_relocation_preview
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


def cmd_detect_faces(args) -> None:
    cfg = load_config()
    logger, _log_path = setup_logging(cfg.log_dir_abs)
    logger.info(f"Starting Phase 3 Session 1 face detection run — model_pack={cfg.face_model_pack} gpu_enabled={cfg.gpu_enabled}")
    init_db(cfg.db_path_abs, logger=logger)  # rule 12: every entry point that opens the DB calls this first, read-only or not

    pbar: tqdm | None = None

    def _progress_cb(done: int, total: int) -> None:
        nonlocal pbar
        if pbar is None:
            pbar = tqdm(total=total, desc="Face detection", unit="photo")
        pbar.n = done
        pbar.refresh()

    conn = connect(cfg.db_path_abs)
    try:
        stats = run_face_detection(
            cfg, conn, logger, cfg.faces_path_abs, cfg.faces_scanned_path_abs,
            progress_cb=_progress_cb, folder_prefix=args.limit,
        )
    finally:
        conn.close()
        if pbar is not None:
            pbar.close()

    print("\n--- Summary ---")
    print(stats.summary())
    print(f"\nFaces (one line per detected face): {cfg.faces_path_abs}")
    print(f"Scanned checkpoint (one line per photo): {cfg.faces_scanned_path_abs}")


def cmd_cluster_faces(args) -> None:
    cfg = load_config()
    logger, _log_path = setup_logging(cfg.log_dir_abs)
    logger.info(
        f"Starting Phase 3 Session 2 face clustering run — "
        f"similarity_threshold={cfg.face_cluster_similarity_threshold} min_samples={cfg.face_cluster_min_samples} "
        f"min_det_score={cfg.face_min_det_score}"
    )
    init_db(cfg.db_path_abs, logger=logger)  # rule 12: every entry point that opens the DB calls this first

    pbar: tqdm | None = None

    def _progress_cb(done: int, total: int) -> None:
        nonlocal pbar
        if total == 0:
            return
        if pbar is None:
            pbar = tqdm(total=total, desc="Face clustering", unit="face")
        pbar.n = done
        pbar.refresh()

    conn = connect(cfg.db_path_abs)
    try:
        load_stats, cluster_stats = cluster_faces_full(
            cfg, conn, logger, cfg.faces_path_abs, progress_cb=_progress_cb
        )
    finally:
        conn.close()
        if pbar is not None:
            pbar.close()

    print("\n--- Load Summary ---")
    print(load_stats.summary())
    print("\n--- Clustering Summary ---")
    print(cluster_stats.summary())
    print(
        "\nUse the dashboard's Face Labeling window to name/merge/split clusters "
        "(main.py has no CLI equivalent for that — it's inherently interactive)."
    )


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


def cmd_audit_dates_list(args) -> None:
    """Step 0: day-level clustering, sorted by count descending — surfaces
    candidate suspicious dates for human review. Not an auto-decide
    mechanism; a genuinely busy day (a wedding, a holiday) can also produce
    a high count."""
    cfg = load_config()
    init_db(cfg.db_path_abs, logger=None)  # safe/idempotent (rule 12)
    conn = connect(cfg.db_path_abs)
    try:
        clusters = find_suspicious_clusters(conn, min_count=args.min_count, top_n=args.top)
    finally:
        conn.close()

    if not clusters:
        print(f"No day had {args.min_count}+ photos/videos. Try a lower --min-count.")
        return

    print(f"Top {len(clusters)} day(s) by photo/video count (min_count={args.min_count}):\n")
    print(f"{'Date':<12} {'Count':>7}  {'FS%':>5}  By date_source")
    for c in clusters:
        sources = ", ".join(f"{k}={v}" for k, v in sorted(c.source_counts.items(), key=lambda kv: -kv[1]))
        print(f"{c.date:<12} {c.count:>7}  {c.filesystem_fraction*100:>4.0f}%  {sources}")
    print(
        "\nFS% = fraction of that day's rows resolved via the least-reliable "
        "'filesystem' fallback (no EXIF/container date, no filename date) — "
        "the strongest signal of a batch-date artifact seen so far in this "
        "library (a 100%-filesystem day with a big count is worth a closer look; "
        "a mostly-exif/container day is more likely a real busy day).\n"
        "Investigate one with: main.py audit-dates inspect YYYY-MM-DD"
    )


def cmd_audit_dates_inspect(args) -> None:
    """Step 1: deep-dive investigation for one suspicious date, repeatable
    for any date this surfaces (not hardcoded to one incident)."""
    cfg = load_config()
    init_db(cfg.db_path_abs, logger=None)
    conn = connect(cfg.db_path_abs)
    try:
        report = inspect_cluster(conn, args.date, sample_size=args.sample)
    finally:
        conn.close()
    if report.total == 0:
        print(f"No photos/videos found with date_taken starting {args.date}.")
        return
    print(format_inspection_report(report))


def cmd_audit_dates_preview_fix(args) -> None:
    """Step 2 (dry run): shows exactly what apply-fix would change, for
    every photo/video across every confirmed-bad date_source='filesystem'
    row this module knows how to fix confidently — see src/date_audit.py's
    module docstring for the root-cause evidence behind this one rule.
    Makes no changes."""
    cfg = load_config()
    init_db(cfg.db_path_abs, logger=None)
    conn = connect(cfg.db_path_abs)
    try:
        preview = find_fix_candidates(conn)
    finally:
        conn.close()
    print(format_fix_preview(preview))
    if preview.candidates:
        print("\nThis was a DRY RUN — nothing was changed. Review the list above, then run:")
        print("  main.py audit-dates apply-fix")


def cmd_audit_dates_apply_fix(args) -> None:
    """Step 2 (apply): writes the corrected date_taken for every fixable
    candidate via src.edits.apply_edit — the same generic, audited write
    path review_tool.py's interactive edit endpoint uses (Phase 2f's
    photo_edits table), so every correction is logged and permanently
    protected from a future automated overwrite."""
    cfg = load_config()
    logger, _log_path = setup_logging(cfg.log_dir_abs, logger_name="photo_organizer.date_audit")
    init_db(cfg.db_path_abs, logger=logger)
    conn = connect(cfg.db_path_abs)
    pbar: tqdm | None = None
    try:
        preview = find_fix_candidates(conn)
        if not preview.candidates:
            print("Nothing to fix — no candidates found. Run 'audit-dates preview-fix' for details.")
            return

        print(format_fix_preview(preview))
        if not args.yes:
            answer = input(
                f"\nType 'yes' to apply {len(preview.candidates)} correction(s) "
                "(recorded in photo_edits, reversible via review_tool.py like any other edit): "
            ).strip().lower()
            if answer != "yes":
                print("Aborted. No changes were made.")
                return

        def _progress_cb(done: int, total: int) -> None:
            nonlocal pbar
            if pbar is None:
                pbar = tqdm(total=total, desc="Date-audit fix", unit="file")
            pbar.n = done
            pbar.refresh()

        stats = apply_fixes(conn, preview.candidates, logger, progress_cb=_progress_cb)
    finally:
        conn.close()
        if pbar is not None:
            pbar.close()

    print("\n--- Summary ---")
    print(stats.summary())


def _interpolation_candidates_from_args(conn, args):
    return find_sequence_interpolation_candidates(
        conn,
        target_prefix=args.target_prefix,
        reference_prefix=args.reference_prefix,
        img_min=args.img_min,
        img_max=args.img_max,
    )


def cmd_audit_dates_interpolate_preview(args) -> None:
    """Step 2, sequence-interpolation variant (dry run): for a human-
    recognized batch (e.g. "these misdated photos look like the vacation
    in E:\\Pics\\2020\\2020-02"), shows the day/time each photo would be
    corrected to, using camera-roll sequence numbers shared with that
    already-correctly-dated reference folder. Makes no changes."""
    cfg = load_config()
    init_db(cfg.db_path_abs, logger=None)
    conn = connect(cfg.db_path_abs)
    try:
        candidates, stats = _interpolation_candidates_from_args(conn, args)
    finally:
        conn.close()
    print(format_interpolation_preview(candidates, stats))
    if candidates:
        print("\nThis was a DRY RUN — nothing was changed. Review the list above, then run:")
        print("  main.py audit-dates interpolate-apply (with the same --target-prefix/--reference-prefix/--img-min/--img-max)")


def cmd_audit_dates_interpolate_apply(args) -> None:
    """Step 2, sequence-interpolation variant (apply): writes the corrected
    date via the same audited src.edits.apply_edit path as apply-fix,
    tagged with SEQUENCE_FIX_EDITOR (distinct from the folder-rule's
    BATCH_FIX_EDITOR) so the audit trail shows which basis produced which
    correction."""
    cfg = load_config()
    logger, _log_path = setup_logging(cfg.log_dir_abs, logger_name="photo_organizer.date_audit")
    init_db(cfg.db_path_abs, logger=logger)
    conn = connect(cfg.db_path_abs)
    pbar: tqdm | None = None
    try:
        candidates, stats = _interpolation_candidates_from_args(conn, args)
        if not candidates:
            print("Nothing to fix — no candidates found. Run 'audit-dates interpolate-preview' for details.")
            return

        print(format_interpolation_preview(candidates, stats))
        if not args.yes:
            answer = input(
                f"\nType 'yes' to apply {len(candidates)} correction(s) "
                "(recorded in photo_edits, reversible via review_tool.py like any other edit): "
            ).strip().lower()
            if answer != "yes":
                print("Aborted. No changes were made.")
                return

        def _progress_cb(done: int, total: int) -> None:
            nonlocal pbar
            if pbar is None:
                pbar = tqdm(total=total, desc="Date-audit interpolate", unit="file")
            pbar.n = done
            pbar.refresh()

        apply_stats = apply_fixes(conn, candidates, logger, progress_cb=_progress_cb, edited_by=SEQUENCE_FIX_EDITOR)
    finally:
        conn.close()
        if pbar is not None:
            pbar.close()

    print("\n--- Summary ---")
    print(apply_stats.summary())


def cmd_relocate_preview(args) -> None:
    """Phase 3b, step 1 (dry run): shows every photo/video whose current
    folder disagrees with what Phase 1's own convention would compute from
    its CURRENT date_taken — general-purpose, not scoped to any one prior
    audit's output. Makes no changes."""
    cfg = load_config()
    init_db(cfg.db_path_abs, logger=None)  # safe/idempotent (rule 12)
    conn = connect(cfg.db_path_abs)
    try:
        preview = find_relocation_candidates(conn, cfg)
    finally:
        conn.close()
    print(format_relocation_preview(preview))
    if preview.candidates:
        print("\nThis was a DRY RUN — nothing was moved. Review the list above, then run:")
        print("  main.py relocate-files apply")


def cmd_relocate_apply(args) -> None:
    """Phase 3b, step 2 (apply): relocates each candidate via
    src.relocate.apply_relocations, which reuses Phase 1's actual
    copy-verify-delete + collision-handling primitive (src/organize.py) —
    copy to the correct YYYY/YYYY-MM folder, verify via hash comparison,
    delete the old copy only once verified. Updates the DB's current_path
    per successfully-relocated file and logs old path -> new path through
    the normal logs/organize_*.log mechanism (not the photo_edits
    mechanism — this is a physical move, not a field edit)."""
    cfg = load_config()
    logger, _log_path = setup_logging(cfg.log_dir_abs, logger_name="photo_organizer.relocate")
    init_db(cfg.db_path_abs, logger=logger)
    conn = connect(cfg.db_path_abs)
    pbar: tqdm | None = None
    try:
        preview = find_relocation_candidates(conn, cfg)
        if not preview.candidates:
            print("Nothing to relocate — no candidates found. Run 'relocate-files preview' for details.")
            return

        print(format_relocation_preview(preview))
        if not args.yes:
            answer = input(
                f"\nType 'yes' to relocate {len(preview.candidates)} file(s) "
                "(copy-verify-delete into their correct folder, per CLAUDE.md rule 2): "
            ).strip().lower()
            if answer != "yes":
                print("Aborted. No files were touched.")
                return

        def _progress_cb(done: int, total: int) -> None:
            nonlocal pbar
            if pbar is None:
                pbar = tqdm(total=total, desc="Relocate", unit="file")
            pbar.n = done
            pbar.refresh()

        stats = apply_relocations(conn, preview.candidates, cfg, logger, progress_cb=_progress_cb)
    finally:
        conn.close()
        if pbar is not None:
            pbar.close()

    print("\n--- Summary ---")
    print(stats.summary())


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
    parser = argparse.ArgumentParser(description="Photo Organizer — Phase 0/1/1b/2/2b/3")
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

    faces_parser = sub.add_parser(
        "detect-faces", help="Phase 3 Session 1: face detection + embeddings (buffalo_l) -> data/faces.jsonl"
    )
    faces_parser.add_argument("--limit", metavar="FOLDER_PREFIX",
                               help="Only process photos whose current_path starts with this prefix (try a small folder first / benchmark)")
    faces_parser.set_defaults(func=cmd_detect_faces)

    cluster_parser = sub.add_parser(
        "cluster-faces",
        help="Phase 3 Session 2: load data/faces.jsonl into the DB and cluster embeddings into people (faiss+DBSCAN)",
    )
    cluster_parser.set_defaults(func=cmd_cluster_faces)

    audit_parser = sub.add_parser(
        "audit-dates", help="Detect + fix batch-date artifacts (a whole folder landing on one wrong date)"
    )
    audit_sub = audit_parser.add_subparsers(dest="audit_dates_command", required=True)

    audit_list = audit_sub.add_parser("list", help="Step 0: day-level clustering, sorted by count — candidates for review")
    audit_list.add_argument("--top", type=int, default=DEFAULT_TOP_N, help=f"Show top N days (default {DEFAULT_TOP_N})")
    audit_list.add_argument("--min-count", type=int, default=DEFAULT_MIN_COUNT,
                             help=f"Only show days with at least this many photos/videos (default {DEFAULT_MIN_COUNT})")
    audit_list.set_defaults(func=cmd_audit_dates_list)

    audit_inspect = audit_sub.add_parser("inspect", help="Step 1: deep-dive investigation for one suspicious date")
    audit_inspect.add_argument("date", metavar="YYYY-MM-DD")
    audit_inspect.add_argument("--sample", type=int, default=10, help="Number of files to re-check live (default 10)")
    audit_inspect.set_defaults(func=cmd_audit_dates_inspect)

    audit_sub.add_parser(
        "preview-fix", help="Step 2 dry run: show proposed date corrections for every fixable photo/video"
    ).set_defaults(func=cmd_audit_dates_preview_fix)

    audit_apply = audit_sub.add_parser(
        "apply-fix", help="Step 2: apply the previewed corrections via the photo_edits audit mechanism"
    )
    audit_apply.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    audit_apply.set_defaults(func=cmd_audit_dates_apply_fix)

    def _add_interpolation_args(p):
        p.add_argument("--target-prefix", required=True, metavar="PATH",
                        help=r'original_path prefix of the misdated batch, e.g. "E:\katya 2024"')
        p.add_argument("--reference-prefix", required=True, metavar="PATH",
                        help=r'current_path prefix of an already-correctly-dated folder for the same '
                             r'event/device, e.g. "E:\Pics\2020\2020-02"')
        p.add_argument("--img-min", type=int, default=None, metavar="N",
                        help="Only consider camera-roll numbers >= N (e.g. where the recognized batch starts)")
        p.add_argument("--img-max", type=int, default=None, metavar="N",
                        help="Only consider camera-roll numbers <= N (e.g. where the recognized batch ends)")

    interp_preview = audit_sub.add_parser(
        "interpolate-preview",
        help="Step 2, human-directed variant: dry-run day/time corrections from a recognized reference folder",
    )
    _add_interpolation_args(interp_preview)
    interp_preview.set_defaults(func=cmd_audit_dates_interpolate_preview)

    interp_apply = audit_sub.add_parser(
        "interpolate-apply",
        help="Step 2, human-directed variant: apply the previewed sequence-interpolated corrections",
    )
    _add_interpolation_args(interp_apply)
    interp_apply.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    interp_apply.set_defaults(func=cmd_audit_dates_interpolate_apply)

    relocate_parser = sub.add_parser(
        "relocate-files",
        help="Phase 3b: resync folder placement with each file's current date_taken (reuses Phase 1's copy-verify-delete)",
    )
    relocate_sub = relocate_parser.add_subparsers(dest="relocate_command", required=True)

    relocate_sub.add_parser(
        "preview", help="Dry run: list every photo/video whose folder disagrees with its current date_taken"
    ).set_defaults(func=cmd_relocate_preview)

    relocate_apply = relocate_sub.add_parser(
        "apply", help="Relocate every candidate via copy-verify-delete; updates current_path in the DB"
    )
    relocate_apply.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    relocate_apply.set_defaults(func=cmd_relocate_apply)

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
