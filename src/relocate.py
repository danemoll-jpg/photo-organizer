"""Phase 3b: resync a photo/video's physical folder placement with its
current date_taken.

Context: the Date Cluster Audit tool (src/date_audit.py) corrected 2,527
photos' stored date_taken values via the photo_edits audit mechanism, but
that was explicitly DB-metadata-only — it never moved the physical files.
Those files still sit in their old, now-wrong YYYY/YYYY-MM folder,
disagreeing with their own corrected date. This module is deliberately
general-purpose, not hardcoded to that one batch: ANY row whose current
physical location doesn't match where Phase 1's own convention would put
it, given its CURRENT date_taken, is a candidate — a future date edit (via
the viewer, bulk-edit, or a future audit fix) can produce the exact same
kind of mismatch, and this tool finds it the same way.

Detection is pure DB + path arithmetic, no file I/O: for every row with a
non-NULL date_taken, compute the expected directory from date_taken_year/
date_taken_month (the same denormalized columns Phase 1 itself writes —
reusing them instead of re-parsing date_taken text avoids any ISO-format
edge cases) using Phase 1's own convention (<dest_root>/YYYY/YYYY-MM/, plus
a /Video subfolder for a video file — same is_video test organize.py
uses: extension against cfg.video_extensions_normalized), and compare
against current_path's actual parent directory. A mismatch is a candidate.
A row with date_taken IS NULL (still genuinely unsorted — Phase 1 never
resolved any date for it) has no "correct" dated folder to compare
against and is never a candidate.

Relocation REUSES Phase 1's actual copy-verify-delete + collision-handling
primitive (src/organize.py::copy_verify_delete / pick_available_name) —
not a reimplementation. Same safety model: copy to the new correct
location, verify via hash comparison, delete from the old location only
after verification succeeds; a naming collision at the destination gets
the identical hash-suffix treatment Phase 1 already uses. Never a raw
move.

This is a physical reorganization, not a metadata field edit, so it does
NOT go through Phase 2f's photo_edits audit mechanism (that's for
caption/tags/location/date_taken *field values*, not for where the
underlying file happens to sit on disk) — instead it's logged through the
normal logs/organize_*.log mechanism (src/logging_setup.py), same pattern
as Phase 1 itself, and the DB is updated via a dedicated, narrow helper
(src/db.py::update_relocated_path) that touches only current_path/
filename/file_size/file_mtime, leaving date_taken/date_source/status/
original_path/processed_at/phase1_verified untouched.

Safety details worth knowing without re-deriving:
- apply_relocations() re-derives each candidate's expected directory fresh
  from the DB (not trusting a possibly-stale preview snapshot) immediately
  before touching it, and skips as a no-op if something else already fixed
  it since the preview was shown.
- Before copying, the source file is re-hashed and compared against the
  row's own file_hash (its primary key / identity). A mismatch means the
  file's content has changed since Phase 1 (or a prior relocation) last
  touched it — relocating it under an identity that no longer matches its
  actual bytes would misrepresent the DB, so this is treated as an error
  and skipped, never silently relocated anyway.
- A missing source file (moved/deleted by something outside this project
  since it was last recorded) is likewise an error, not a crash — the run
  continues with the next candidate, matching Phase 1's own per-file
  error-isolation.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .db import update_relocated_path
from .hashing import hash_file
from .organize import CopyVerifyDeleteError, copy_verify_delete, pick_available_name


def _is_video(filename: str, cfg: Config) -> bool:
    return Path(filename).suffix.lower() in cfg.video_extensions_normalized


def expected_dir(cfg: Config, year: int, month: int, is_video: bool) -> Path:
    """Phase 1's own destination convention, reused verbatim: <dest_root>/
    YYYY/YYYY-MM/, plus a Video subfolder for video files. See
    organize.py::_process_one — this must always match that function's own
    logic exactly, or this tool would flag correctly-placed files as
    mismatched (or vice versa)."""
    d = cfg.dest_root_path / f"{year:04d}" / f"{year:04d}-{month:02d}"
    return (d / "Video") if is_video else d


def _same_dir(a: Path, b: Path) -> bool:
    # Case-insensitive, separator-normalized comparison that never touches
    # the filesystem (no .resolve()) -- a row's current_path may point at a
    # file that no longer exists, and this comparison must still work.
    import os
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


@dataclass
class RelocateCandidate:
    file_hash: str
    filename: str
    current_path: str
    expected_dir: str    # display string; actual destination filename is decided at apply time (collision-safe)
    is_video: bool
    date_taken: str


@dataclass
class RelocationPreview:
    candidates: list[RelocateCandidate]
    total_dated_rows: int       # rows with date_taken IS NOT NULL -- the whole population considered
    already_correct_count: int  # dated rows whose current folder already matches -- nothing to do
    no_date_rows: int           # rows with date_taken IS NULL -- not applicable, no folder to compare against


def find_relocation_candidates(conn: sqlite3.Connection, cfg: Config) -> RelocationPreview:
    """Step 1 (dry-run half): scans every row with a resolved date_taken —
    not scoped to any one prior audit's output — and flags any whose
    current_path's directory disagrees with what Phase 1's own convention
    would compute from that row's CURRENT date_taken_year/month. Read-only,
    no file I/O (see module docstring)."""
    rows = conn.execute(
        "SELECT file_hash, filename, current_path, date_taken, date_taken_year, date_taken_month "
        "FROM photos WHERE date_taken IS NOT NULL"
    ).fetchall()
    no_date_rows = conn.execute("SELECT COUNT(*) FROM photos WHERE date_taken IS NULL").fetchone()[0]

    candidates: list[RelocateCandidate] = []
    already_correct = 0
    for file_hash, filename, current_path, date_taken, year, month in rows:
        if year is None or month is None:
            # Defensive only -- every writer that sets date_taken also sets
            # these two denormalized columns in the same statement, so this
            # shouldn't happen in practice. Treat as "can't compute an
            # expected folder" rather than guessing or crashing.
            continue
        is_video = _is_video(filename, cfg)
        want_dir = expected_dir(cfg, year, month, is_video)
        actual_dir = Path(current_path).parent
        if _same_dir(actual_dir, want_dir):
            already_correct += 1
            continue
        candidates.append(RelocateCandidate(
            file_hash=file_hash,
            filename=filename,
            current_path=current_path,
            expected_dir=str(want_dir),
            is_video=is_video,
            date_taken=date_taken,
        ))

    return RelocationPreview(
        candidates=candidates,
        total_dated_rows=len(rows),
        already_correct_count=already_correct,
        no_date_rows=no_date_rows,
    )


def format_relocation_preview(preview: RelocationPreview, max_lines: int = 200) -> str:
    lines = [
        f"{len(preview.candidates)} photo(s)/video(s) would be relocated to match their current date_taken:",
        "",
    ]
    for c in preview.candidates[:max_lines]:
        lines.append(
            f"  {c.current_path}\n"
            f"    -> {Path(c.expected_dir) / c.filename}  "
            f"(date_taken={c.date_taken}{', video' if c.is_video else ''})"
        )
    if len(preview.candidates) > max_lines:
        lines.append(f"  ... and {len(preview.candidates) - max_lines} more (truncated for display)")
    lines.append("")
    lines.append(
        f"Of {preview.total_dated_rows} dated row(s): {len(preview.candidates)} need relocating, "
        f"{preview.already_correct_count} already correctly placed (nothing to do). "
        f"{preview.no_date_rows} additional row(s) have no resolved date at all (still genuinely "
        f"unsorted) and are not applicable here."
    )
    return "\n".join(lines)


@dataclass
class RelocateStats:
    candidates: int = 0
    relocated: int = 0
    skipped_already_correct: int = 0  # re-verified at apply time -- fixed by something else since preview
    skipped_missing_file: int = 0
    skipped_hash_mismatch: int = 0    # live file's content no longer matches its recorded file_hash
    errors: int = 0

    def summary(self) -> str:
        return (
            f"candidates={self.candidates} relocated={self.relocated} "
            f"skipped_already_correct={self.skipped_already_correct} "
            f"skipped_missing_file={self.skipped_missing_file} "
            f"skipped_hash_mismatch={self.skipped_hash_mismatch} errors={self.errors}"
        )


def apply_relocations(
    conn: sqlite3.Connection,
    candidates: list[RelocateCandidate],
    cfg: Config,
    logger: logging.Logger,
    progress_cb: Callable[[int, int], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> RelocateStats:
    """Step 2 (apply half): for each candidate, re-derives the expected
    directory fresh from the DB (never trusts the preview snapshot alone —
    something could have changed the row, or fixed the file, in between),
    re-hashes the live source file and confirms it still matches the row's
    own file_hash (see module docstring), then relocates it via
    src.organize.copy_verify_delete + pick_available_name — the exact same
    primitive Phase 1 uses, not a reimplementation. On success, updates
    photos.current_path/filename/file_size/file_mtime via
    src.db.update_relocated_path (NOT the photo_edits mechanism — this is a
    physical move, not a field edit) and logs old path -> new path.

    A single bad candidate (missing file, hash mismatch, copy/verify
    failure) is logged and counted, never lets the whole run crash — same
    per-file error isolation as Phase 1's run_phase1().
    """
    stats = RelocateStats(candidates=len(candidates))
    total = len(candidates)
    name_cache: dict[Path, set[str]] = {}

    for i, c in enumerate(candidates, start=1):
        if stop_check is not None and stop_check():
            logger.info(f"STOPPED by user request after {stats.relocated}/{total}.")
            break
        try:
            row = conn.execute(
                "SELECT current_path, filename, date_taken_year, date_taken_month FROM photos WHERE file_hash = ?",
                (c.file_hash,),
            ).fetchone()
            if row is None:
                stats.errors += 1
                logger.error(f"ERROR relocating hash={c.file_hash[:12]}: no longer in the database, skipped")
                continue
            current_path, filename, year, month = row
            if year is None or month is None:
                stats.errors += 1
                logger.error(f"ERROR relocating hash={c.file_hash[:12]} {current_path}: no resolved date, skipped")
                continue

            is_video = _is_video(filename, cfg)
            want_dir = expected_dir(cfg, year, month, is_video)
            src_path = Path(current_path)

            if _same_dir(src_path.parent, want_dir):
                stats.skipped_already_correct += 1
                logger.info(f"SKIP already correct (fixed since preview) hash={c.file_hash[:12]} {current_path}")
                continue

            if not src_path.exists():
                stats.skipped_missing_file += 1
                logger.error(f"SKIP missing source file hash={c.file_hash[:12]} {current_path}")
                continue

            try:
                live_hash = hash_file(src_path, cfg.hash_algorithm)
            except OSError as e:
                stats.errors += 1
                logger.error(f"ERROR hashing {src_path}: {e}")
                continue

            if live_hash != c.file_hash:
                stats.skipped_hash_mismatch += 1
                logger.error(
                    f"SKIP hash mismatch hash={c.file_hash[:12]} {current_path}: "
                    f"live file content no longer matches its recorded identity, not relocating"
                )
                continue

            dest_name = pick_available_name(want_dir, filename, c.file_hash, name_cache, cfg.collision_suffix_length)
            dest_path = want_dir / dest_name

            try:
                dest_path = copy_verify_delete(src_path, dest_path, c.file_hash, cfg, logger)
            except CopyVerifyDeleteError as e:
                stats.errors += 1
                logger.error(str(e))
                continue

            try:
                st = dest_path.stat()
                new_size, new_mtime = st.st_size, st.st_mtime
            except OSError:
                new_size, new_mtime = 0, None
            update_relocated_path(conn, c.file_hash, str(dest_path), dest_path.name, new_size, new_mtime)
            stats.relocated += 1
            logger.info(f"RELOCATED hash={c.file_hash[:12]} {current_path} -> {dest_path}")
        except Exception as e:  # keep the run alive for a single bad candidate
            stats.errors += 1
            logger.error(f"UNEXPECTED ERROR relocating hash={c.file_hash[:12]}: {e!r}")
        if progress_cb is not None:
            progress_cb(i, total)

    logger.info(f"Phase 3b relocation complete: {stats.summary()}")
    return stats
