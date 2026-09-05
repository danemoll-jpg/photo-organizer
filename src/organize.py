"""Phase 1 core: scan -> resolve date -> copy-verify-delete into
<dest_root>/YYYY/YYYY-MM/, or flag as unsorted. Every file is handled
exactly once per content hash; skipping/resuming is done by checking the
DB before touching anything.

dest_root is always scanned as an implicit source (in addition to whatever
is in config's source_folders), since it's known to already contain a mix
of correctly-placed files and loose/unsorted ones. A file whose computed
destination is its own current location is recognized and left alone —
no copy, no delete, just a DB record so future runs don't re-check it.

Exact-content duplicates found under a second, different path are SKIPPED
(left in place, not deleted) once their hash is already known — Phase 1
does not delete duplicate originals. See TODO.md / session summary.

Phase 1b: video files (per cfg.video_extensions) are scanned and sorted
alongside photos through this exact same pipeline — same hashing, copy-
verify-delete, collision handling, and logging. The only two differences
are (1) date resolution uses resolve_video_date's container-metadata-first
chain instead of resolve_date's EXIF-first chain, and (2) a dated video's
destination gets an extra 'Video' subfolder within its YYYY-MM month
folder, so photos and videos never share a directory. See
video_date_resolver.py.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .date_resolver import resolve_date
from .db import find_by_path, is_known_hash, upsert_photo
from .hashing import hash_file
from .scanner import scan_folders
from .video_date_resolver import resolve_video_date


@dataclass
class RunStats:
    scanned: int = 0
    already_known: int = 0       # hash already in DB from a prior run -> skipped entirely (duplicate)
    fast_path_hits: int = 0       # subset of already_known resolved via path+size+mtime, no hash computed
    already_in_place: int = 0     # correctly located already, DB record added, no file op
    sorted: int = 0                # copy-verify-delete succeeded, dated file
    unsorted: int = 0               # copy-verify-delete succeeded, routed to needs_review
    errors: int = 0                  # verify failed / copy failed / unexpected — source untouched
    dry_run_already_in_place: int = 0  # [dry-run] would be a no-op, already correctly placed
    dry_run_sorted: int = 0             # [dry-run] would be moved into YYYY/YYYY-MM
    dry_run_unsorted: int = 0            # [dry-run] would be flagged into _unsorted/needs_review

    @property
    def dry_run_planned(self) -> int:
        """Total planned actions under dry-run (back-compat combined count)."""
        return self.dry_run_already_in_place + self.dry_run_sorted + self.dry_run_unsorted

    def summary(self) -> str:
        return (
            f"scanned={self.scanned} already_known={self.already_known} "
            f"(fast_path={self.fast_path_hits} full_hash_confirmed={self.already_known - self.fast_path_hits}) "
            f"already_in_place={self.already_in_place} sorted={self.sorted} "
            f"unsorted={self.unsorted} errors={self.errors} "
            f"dry_run_planned={self.dry_run_planned} "
            f"(would_sort={self.dry_run_sorted} would_flag_unsorted={self.dry_run_unsorted} "
            f"would_noop_in_place={self.dry_run_already_in_place})"
        )


def _dir_claimed_names(dest_dir: Path, cache: dict[Path, set[str]]) -> set[str]:
    if dest_dir not in cache:
        names: set[str] = set()
        if dest_dir.exists():
            try:
                names = {e.name for e in dest_dir.iterdir() if e.is_file()}
            except OSError:
                pass
        cache[dest_dir] = names
    return cache[dest_dir]


def _pick_available_name(dest_dir: Path, wanted_name: str, file_hash: str,
                          cache: dict[Path, set[str]], suffix_len: int) -> str:
    claimed = _dir_claimed_names(dest_dir, cache)
    if wanted_name not in claimed:
        claimed.add(wanted_name)
        return wanted_name
    stem, ext = Path(wanted_name).stem, Path(wanted_name).suffix
    candidate = f"{stem}_{file_hash[:suffix_len]}{ext}"
    counter = 1
    while candidate in claimed:
        candidate = f"{stem}_{file_hash[:suffix_len]}_{counter}{ext}"
        counter += 1
    claimed.add(candidate)
    return candidate


def _cleanup_partial(tmp_path: Path, logger: logging.Logger) -> None:
    try:
        if tmp_path.exists():
            tmp_path.unlink()
    except OSError as e:
        logger.error(f"Could not clean up leftover partial file {tmp_path}: {e}")


def _process_one(path: Path, cfg: Config, conn, logger: logging.Logger,
                  name_cache: dict[Path, set[str]], stats: RunStats) -> None:
    # Fast-path pre-check: if this exact path was already recorded with the
    # same size and mtime, it's overwhelmingly likely to be byte-identical
    # to what a full hash would find anyway — skip re-reading its content.
    # Any actual change (including one that happens to preserve size but
    # not mtime, or vice versa) fails this check and falls through to the
    # full hash below, which stays the ground truth. See db.find_by_path.
    try:
        st = path.stat()
    except OSError as e:
        stats.errors += 1
        logger.error(f"ERROR statting {path}: {e}")
        return

    existing = find_by_path(conn, str(path))
    if (
        existing is not None
        and existing["file_mtime"] is not None
        and existing["file_size"] == st.st_size
        and existing["file_mtime"] == st.st_mtime
    ):
        stats.already_known += 1
        stats.fast_path_hits += 1
        logger.info(
            f"SKIP already-processed (fast path: path+size+mtime match, no hash) "
            f"hash={existing['file_hash'][:12]} path={path}"
        )
        return

    try:
        file_hash = hash_file(path, cfg.hash_algorithm)
    except OSError as e:
        stats.errors += 1
        logger.error(f"ERROR hashing {path}: {e}")
        return

    if is_known_hash(conn, file_hash):
        stats.already_known += 1
        logger.info(f"SKIP already-processed hash={file_hash[:12]} path={path}")
        return

    is_video = path.suffix.lower() in cfg.video_extensions_normalized
    dt, source = resolve_video_date(path) if is_video else resolve_date(path)
    if source == "unsorted":
        dest_dir = cfg.unsorted_path
    else:
        dest_dir = cfg.dest_root_path / f"{dt.year:04d}" / f"{dt.year:04d}-{dt.month:02d}"
        if is_video:
            dest_dir = dest_dir / "Video"

    wanted_name = path.name
    candidate_dest = dest_dir / wanted_name
    resolved_path = path.resolve()

    try:
        already_in_place = candidate_dest.exists() and candidate_dest.resolve() == resolved_path
    except OSError:
        already_in_place = False

    if already_in_place:
        if cfg.dry_run:
            stats.dry_run_already_in_place += 1
            logger.info(f"[DRY RUN] already in place, would record: {path} (date_source={source})")
            return
        stats.already_in_place += 1
        logger.info(f"ALREADY IN PLACE hash={file_hash[:12]} path={path} (date_source={source})")
        _record(conn, file_hash, path, path, path.name, dt, source,
                "unsorted" if source == "unsorted" else "sorted", verified=1)
        return

    dest_name = _pick_available_name(dest_dir, wanted_name, file_hash, name_cache, cfg.collision_suffix_length)
    dest_path = dest_dir / dest_name

    if cfg.dry_run:
        if source == "unsorted":
            stats.dry_run_unsorted += 1
            action = "flag as unsorted"
        else:
            stats.dry_run_sorted += 1
            action = "sort"
        logger.info(f"[DRY RUN] would {action}: {path} -> {dest_path} (date_source={source})")
        return

    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_dir / (dest_name + ".partial")

    try:
        shutil.copy2(path, tmp_path)
    except OSError as e:
        stats.errors += 1
        logger.error(f"COPY FAILED {path} -> {tmp_path}: {e} (source untouched)")
        return

    try:
        copy_hash = hash_file(tmp_path, cfg.hash_algorithm)
    except OSError as e:
        stats.errors += 1
        logger.error(f"VERIFY ERROR hashing {tmp_path}: {e} (source untouched)")
        _cleanup_partial(tmp_path, logger)
        return

    if copy_hash != file_hash:
        stats.errors += 1
        logger.error(f"VERIFY FAILED {path} -> {tmp_path}: hash mismatch, source untouched")
        _cleanup_partial(tmp_path, logger)
        return

    try:
        os.replace(tmp_path, dest_path)
    except OSError as e:
        stats.errors += 1
        logger.error(f"FINALIZE FAILED {tmp_path} -> {dest_path}: {e} (source untouched)")
        _cleanup_partial(tmp_path, logger)
        return

    try:
        path.unlink()
    except OSError as e:
        logger.warning(f"Copied+verified but could not delete source {path}: {e} (dest is safe at {dest_path})")

    status = "unsorted" if source == "unsorted" else "sorted"
    if status == "sorted":
        stats.sorted += 1
    else:
        stats.unsorted += 1
    logger.info(f"OK {status.upper()} hash={file_hash[:12]} {path} -> {dest_path} (date_source={source})")
    _record(conn, file_hash, dest_path, path, dest_name, dt, source, status, verified=1)


def _record(conn, file_hash: str, current_path: Path, original_path: Path, filename: str,
            dt, date_source: str, status: str, verified: int) -> None:
    from datetime import datetime
    try:
        st = current_path.stat()
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, None
    upsert_photo(conn, {
        "file_hash": file_hash,
        "current_path": str(current_path),
        "original_path": str(original_path),
        "filename": filename,
        "file_size": size,
        "file_mtime": mtime,
        "date_taken": dt.isoformat() if dt else None,
        "date_taken_year": dt.year if dt else None,
        "date_taken_month": dt.month if dt else None,
        "date_source": date_source,
        "status": status,
        "processed_at": datetime.now().isoformat(),
        "phase1_verified": verified,
    })


def run_phase1(cfg: Config, conn, logger: logging.Logger,
                progress_cb: Callable[[int, int], None] | None = None,
                stop_check: Callable[[], bool] | None = None) -> RunStats:
    """Runs Phase 1 end to end. `progress_cb(scanned, total)` — if given —
    is called after every file, so a caller can drive its own progress
    display (a tqdm bar for the CLI, a Tk progress bar for the dashboard)
    without duplicating this loop or this module owning any particular
    output stream — important since the dashboard runs under pythonw.exe,
    which has no stdout/stderr to write to. `stop_check()` — if given and
    it returns True — stops the run *between* files (never mid-copy/verify),
    so a cancel can't leave a partial file behind; already-processed files
    stay processed since resumability is by content hash."""
    stats = RunStats()
    name_cache: dict[Path, set[str]] = {}

    roots = list(cfg.source_folder_paths)
    if cfg.dest_root_path not in roots:
        roots.append(cfg.dest_root_path)  # dest_root always scanned too — see module docstring

    # Snapshot the full file list before processing anything. dest_root is
    # one of the scan roots and also where files get written *during* this
    # run — walking it lazily while mutating it has undefined iteration
    # behavior (and would rescan freshly-copied files this same run).
    logger.info("Scanning source folders...")
    files = list(scan_folders(roots, cfg.all_extensions_normalized))
    total = len(files)
    logger.info(f"Found {total} candidate files. Processing...")

    for path in files:
        if stop_check is not None and stop_check():
            logger.info(f"STOPPED by user request after {stats.scanned}/{total} files.")
            break
        stats.scanned += 1
        try:
            _process_one(path, cfg, conn, logger, name_cache, stats)
        except Exception as e:  # keep the run alive for a single bad file
            stats.errors += 1
            logger.error(f"UNEXPECTED ERROR on {path}: {e!r}")
        if progress_cb is not None:
            progress_cb(stats.scanned, total)

    logger.info(f"RUN COMPLETE: {stats.summary()}")
    return stats
