"""Phase 2b: one-time-per-photo GPS extraction + offline reverse geocoding,
backfilled directly onto the existing `photos` table (gps_lat/gps_lon/
location_name/gps_checked columns — see schema.sql). This is new Phase 2b
scope, run as its own step (`main.py extract-gps`) against whatever's
already in the DB from a completed Phase 1/1b run — it does NOT touch
organize.py or caption.py, and does not rescan the filesystem: it reads
current_path straight from the DB rows Phase 1/1b already wrote.

Resumability (CLAUDE.md rule 1): gps_checked is the checkpoint. A row with
gps_checked=1 is never re-examined, regardless of whether GPS data was
actually found — "checked, nothing there" is itself a stable fact once a
file's bytes haven't changed (its file_hash wouldn't still be the same row
otherwise), so this never re-reads the same file's EXIF twice. A file
that's missing/unreadable at check time is left with gps_checked=0 so a
transient problem (e.g. checked mid-Phase-1-move) gets retried next run
rather than being silently skipped forever.

Phase 2f: a manual location edit in review_tool.py is permanently protected
from being silently overwritten here. Before writing gps_lat/gps_lon/
location_name for a row, this checks src.edits.has_manual_edit(conn,
file_hash, "location") -- if a human has ever overridden this photo's
location, the write is skipped (logged, counted in GpsStats.
skipped_manual_edit) but gps_checked is still set to 1, since a manual
override is permanent and this row will never need re-examining on that
basis alone (matching the existing "checked, nothing to do" resumability
model above, just for a different reason). Every row without a manual edit
keeps being processed exactly as before -- this is a real behavior change
to an already-shipped module, tested carefully for that (see
tests/test_phase2f.py).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .edits import has_manual_edit
from .gps_resolver import resolve_location


@dataclass
class GpsStats:
    scanned: int = 0
    found: int = 0
    not_found: int = 0
    errors: int = 0
    skipped_manual_edit: int = 0  # Phase 2f: location NOT overwritten -- a human edited it in review_tool.py

    def summary(self) -> str:
        return (
            f"scanned={self.scanned} found={self.found} not_found={self.not_found} "
            f"errors={self.errors} skipped_manual_edit={self.skipped_manual_edit}"
        )


def run_gps_extraction(
    cfg: Config,
    conn: sqlite3.Connection,
    logger: logging.Logger,
    progress_cb: Callable[[int, int], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
    folder_prefix: str | None = None,
) -> GpsStats:
    """Scans `photos` rows with gps_checked=0, extracts + reverse-geocodes
    GPS where present, and writes the result back. Mirrors run_phase1's/
    run_phase2's progress_cb/stop_check shape so any future dashboard/CLI
    caller can reuse the exact same pattern (none is wired up this session
    beyond `main.py extract-gps` — see TODO.md).

    folder_prefix (used by `--limit`, same spirit as caption.py's) restricts
    to rows whose current_path starts with the given prefix — lets a real
    run be tried against one small folder first."""
    stats = GpsStats()
    video_exts = cfg.video_extensions_normalized

    query = "SELECT file_hash, current_path FROM photos WHERE gps_checked = 0"
    params: list[str] = []
    if folder_prefix:
        query += " AND current_path LIKE ?"
        params.append(f"{folder_prefix}%")
    rows = conn.execute(query, params).fetchall()
    total = len(rows)
    logger.info(
        f"GPS extraction: {total} photo(s)/video(s) not yet checked"
        + (f" under {folder_prefix}" if folder_prefix else "")
    )

    for file_hash, current_path in rows:
        if stop_check is not None and stop_check():
            logger.info(f"STOPPED by user request after {stats.scanned}/{total}.")
            break
        stats.scanned += 1
        path = Path(current_path)
        is_video = path.suffix.lower() in video_exts

        try:
            if not path.exists():
                stats.errors += 1
                logger.error(f"ERROR (GPS): file missing at recorded path {path} — will retry next run")
            elif has_manual_edit(conn, file_hash, "location"):
                # Phase 2f: a human already set this photo's location in
                # review_tool.py -- never silently overwrite it. Still marks
                # gps_checked=1 (a manual override is permanent, so there's
                # nothing productive about re-examining this row again next
                # run either).
                stats.skipped_manual_edit += 1
                conn.execute("UPDATE photos SET gps_checked = 1 WHERE file_hash = ?", (file_hash,))
                logger.info(f"SKIP GPS (manually edited location) hash={file_hash[:12]} {path}")
            else:
                result = resolve_location(path, is_video=is_video)
                if result is None:
                    stats.not_found += 1
                    conn.execute("UPDATE photos SET gps_checked = 1 WHERE file_hash = ?", (file_hash,))
                else:
                    stats.found += 1
                    conn.execute(
                        "UPDATE photos SET gps_lat = ?, gps_lon = ?, location_name = ?, gps_checked = 1 "
                        "WHERE file_hash = ?",
                        (result["lat"], result["lon"], result["location_name"], file_hash),
                    )
                    shown = result["location_name"] or f"({result['lat']:.4f}, {result['lon']:.4f}) [geocode failed]"
                    logger.info(f"OK GPS hash={file_hash[:12]} {path} -> {shown}")
        except Exception as e:  # unreadable file, unexpected geocoder failure, etc. -- keep the run going
            stats.errors += 1
            logger.error(f"ERROR (GPS) {path}: {e!r}")

        if stats.scanned % 200 == 0:
            conn.commit()  # periodic checkpoint, same spirit as caption.py's flush cadence
        if progress_cb is not None:
            progress_cb(stats.scanned, total)

    conn.commit()
    logger.info(f"GPS EXTRACTION COMPLETE: {stats.summary()}")
    return stats
