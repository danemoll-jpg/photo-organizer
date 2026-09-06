"""Phase 2g: one-time-per-video first-frame thumbnail extraction, cached to
disk under cfg.thumbnail_dir_abs as <file_hash>.jpg — served by
review_tool.py's /thumbnail/<file_hash> route in place of the generic
play-icon placeholder. Same shape as src/gps_backfill.py (own module,
own CLI command `main.py extract-thumbnails`, own dashboard panel) — it
does NOT touch organize.py/caption.py/gps_backfill.py, and does not
rescan the filesystem: it reads current_path straight from the `photos`
rows Phase 1/1b already wrote, filtered to video extensions.

Resumability (CLAUDE.md rule 1) — deliberately DISK-based, not a DB
column: a cache file already present at <thumbnail_dir>/<hash>.jpg means
this video was already handled, full stop. This differs from GPS
extraction's `gps_checked` DB column, and that's a deliberate choice, not
an oversight — the spec's own framing ("cache extracted thumbnails to
disk keyed by file_hash") is itself a disk-cache design, and checking
"does the output file already exist" needs no schema migration at all
(no ALTER TABLE, nothing for init_db()'s _migrate() to touch). The
tradeoff: this module never touches the DB for writes, only SELECTs to
find video rows and their current_path — arguably a net simplification
over GPS's approach, not just a different one, and it means zero DB-write
contention risk with a concurrent Phase 1/1b/2/GPS-extraction run (GPS
extraction's own dashboard panel had to specifically verify sqlite
write-contention was safe; this one has none to verify).

A genuine extraction failure (corrupt file, unsupported codec, decodes to
zero frames) is recorded with a sibling <hash>.failed marker (an empty
file) so a permanently-broken video isn't silently retried on every
future run forever — the same spirit as gps_checked=1-but-nothing-found
being a stable, not-worth-re-examining outcome. A file that's simply
*missing* at check time (e.g. checked mid-Phase-1-move) does NOT get a
.failed marker — same reasoning as gps_backfill.py leaving gps_checked=0
for a missing file: this is a transient condition, not evidence the video
itself is broken, so it's retried on the next run rather than being
silently skipped forever.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .video_thumbnail import extract_first_frame_jpeg


@dataclass
class ThumbStats:
    scanned: int = 0
    extracted: int = 0
    already_cached: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"scanned={self.scanned} extracted={self.extracted} "
            f"already_cached={self.already_cached} errors={self.errors}"
        )


def _thumbnail_path(thumbnail_dir: Path, file_hash: str) -> Path:
    return thumbnail_dir / f"{file_hash}.jpg"


def _failed_marker_path(thumbnail_dir: Path, file_hash: str) -> Path:
    return thumbnail_dir / f"{file_hash}.failed"


def run_thumbnail_extraction(
    cfg: Config,
    conn: sqlite3.Connection,
    logger: logging.Logger,
    progress_cb: Callable[[int, int], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
    folder_prefix: str | None = None,
) -> ThumbStats:
    """Scans `photos` rows whose filename is a video (per
    cfg.video_extensions), extracts + caches a first-frame JPEG thumbnail
    for any not already cached (or previously marked permanently failed —
    see module docstring), and writes it to cfg.thumbnail_dir_abs.

    folder_prefix (used by `--limit`, same spirit as caption.py's/
    gps_backfill.py's) restricts to rows whose current_path starts with the
    given prefix — lets a real run be tried against one small folder first.
    """
    stats = ThumbStats()
    video_exts = cfg.video_extensions_normalized
    thumbnail_dir = cfg.thumbnail_dir_abs
    thumbnail_dir.mkdir(parents=True, exist_ok=True)

    if not video_exts:
        logger.info("No video_extensions configured — nothing to do.")
        return stats

    # Parenthesized deliberately -- SQL's AND binds tighter than OR, so an
    # unparenthesized "a OR b AND c" appended folder_prefix clause below
    # would silently apply only to the LAST extension's OR branch, letting
    # every OTHER extension match regardless of folder_prefix (a real bug
    # caught by tests/test_phase2g.py's folder_prefix test before this fix).
    ext_clause = "(" + " OR ".join(["filename LIKE ?"] * len(video_exts)) + ")"
    query = f"SELECT file_hash, current_path FROM photos WHERE {ext_clause}"
    params: list[str] = [f"%{ext}" for ext in sorted(video_exts)]
    if folder_prefix:
        query += " AND current_path LIKE ?"
        params.append(f"{folder_prefix}%")
    rows = conn.execute(query, params).fetchall()
    total = len(rows)
    logger.info(
        f"Thumbnail extraction: {total} video row(s) found"
        + (f" under {folder_prefix}" if folder_prefix else "")
    )

    for file_hash, current_path in rows:
        if stop_check is not None and stop_check():
            logger.info(f"STOPPED by user request after {stats.scanned}/{total}.")
            break
        stats.scanned += 1
        thumb_path = _thumbnail_path(thumbnail_dir, file_hash)
        failed_path = _failed_marker_path(thumbnail_dir, file_hash)

        if thumb_path.exists():
            stats.already_cached += 1
        elif failed_path.exists():
            stats.already_cached += 1  # permanently-failed, not re-attempted -- see module docstring
        else:
            path = Path(current_path)
            try:
                if not path.exists():
                    stats.errors += 1
                    logger.error(f"ERROR (thumbnail): file missing at recorded path {path} — will retry next run")
                else:
                    data = extract_first_frame_jpeg(path, cfg.thumbnail_max_dimension)
                    if data is None:
                        stats.errors += 1
                        failed_path.touch()
                        logger.error(f"ERROR (thumbnail): could not decode a frame from {path} — marked as failed, won't retry")
                    else:
                        # Write-then-rename: an interrupted run (crash, kill)
                        # never leaves a partial/corrupt .jpg sitting at the
                        # real cache path for review_tool.py to try to serve
                        # (same "never leave a half-written artifact behind"
                        # spirit as Phase 1's copy-verify-delete, even though
                        # this is a cache file, not an original photo).
                        tmp_path = thumb_path.with_suffix(".jpg.tmp")
                        tmp_path.write_bytes(data)
                        tmp_path.replace(thumb_path)
                        stats.extracted += 1
                        logger.info(f"OK thumbnail hash={file_hash[:12]} {path} -> {thumb_path.name} ({len(data)} bytes)")
            except Exception as e:  # unreadable file, unexpected decode failure, etc. -- keep the run going
                stats.errors += 1
                logger.error(f"ERROR (thumbnail) {path}: {e!r}")

        if progress_cb is not None:
            progress_cb(stats.scanned, total)

    logger.info(f"THUMBNAIL EXTRACTION COMPLETE: {stats.summary()}")
    return stats
