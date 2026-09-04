"""SQLite access layer. photos.file_hash is the resumability checkpoint:
a hash already present means this file was already handled by a prior run.

photos.current_path / original_path (indexed) back a cheaper pre-check used
by organize.py before it resorts to a full content hash: if a scanned path
matches an existing row and that row's file_size/file_mtime match what's on
disk right now, the file is treated as already-known without re-reading its
bytes. Full hash stays the ground truth for actual dedup/copy-verify — the
pre-check only skips redundant hashing of files nothing has touched.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema.sql"

_module_logger = logging.getLogger("photo_organizer")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path, logger: logging.Logger | None = None) -> None:
    conn = connect(db_path)
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
        _migrate(conn, logger or _module_logger)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection, logger: logging.Logger) -> None:
    """Schema additions that CREATE TABLE IF NOT EXISTS can't apply to an
    already-existing table (schema.sql's CREATE INDEX statements above are
    already idempotent and re-run every init_db() call unconditionally)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    if "file_mtime" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN file_mtime REAL")
        conn.commit()
        _backfill_file_mtime(conn, logger)


def _backfill_file_mtime(conn: sqlite3.Connection, logger: logging.Logger) -> None:
    """One-time backfill for rows recorded before file_mtime existed.

    Uses a stat() per existing row (metadata only, no file bytes read) so
    this migration itself doesn't cost a full re-hash pass — it's the whole
    point of the fix. A row is backfilled only if current_path still exists
    and its on-disk size still matches the recorded file_size; otherwise
    file_mtime is left NULL and organize.py's fast-path pre-check will fall
    back to a full hash for that one file next time it's scanned (safe, just
    not fast — see CLAUDE.md rule 1 on resumability never trading away
    correctness for speed).
    """
    rows = conn.execute(
        "SELECT file_hash, current_path, file_size FROM photos WHERE file_mtime IS NULL"
    ).fetchall()
    updated = 0
    for file_hash, current_path, recorded_size in rows:
        try:
            st = Path(current_path).stat()
        except OSError:
            continue  # file missing/moved since it was recorded -- leave NULL
        if st.st_size != recorded_size:
            continue  # something changed on disk -- don't backfill a stale mtime
        conn.execute("UPDATE photos SET file_mtime = ? WHERE file_hash = ?", (st.st_mtime, file_hash))
        updated += 1
    conn.commit()
    logger.info(
        f"Migration: added file_mtime column, backfilled {updated}/{len(rows)} existing "
        f"photo records via stat() (no re-hash needed). "
        f"{len(rows) - updated} record(s) will get a full hash on next scan "
        f"(missing file or size changed since last run)."
    )


def is_known_hash(conn: sqlite3.Connection, file_hash: str) -> bool:
    cur = conn.execute("SELECT 1 FROM photos WHERE file_hash = ? LIMIT 1", (file_hash,))
    return cur.fetchone() is not None


def find_by_path(conn: sqlite3.Connection, path: str) -> dict | None:
    """Look up a photo record by where it currently sits on disk. Checked
    against current_path first (covers dest_root rescans and untouched
    already-in-place files), then original_path (covers the rare case a
    copy succeeded but the source delete failed, leaving the file behind
    at its original location too). Returns file_hash/file_size/file_mtime,
    or None if this path isn't recorded under either column."""
    cur = conn.execute(
        "SELECT file_hash, file_size, file_mtime FROM photos WHERE current_path = ? LIMIT 1",
        (path,),
    )
    row = cur.fetchone()
    if row is None:
        cur = conn.execute(
            "SELECT file_hash, file_size, file_mtime FROM photos WHERE original_path = ? LIMIT 1",
            (path,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"file_hash": row[0], "file_size": row[1], "file_mtime": row[2]}


def upsert_photo(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO photos (
            file_hash, current_path, original_path, filename, file_size, file_mtime,
            date_taken, date_taken_year, date_taken_month, date_source,
            status, processed_at, phase1_verified
        ) VALUES (
            :file_hash, :current_path, :original_path, :filename, :file_size, :file_mtime,
            :date_taken, :date_taken_year, :date_taken_month, :date_source,
            :status, :processed_at, :phase1_verified
        )
        ON CONFLICT(file_hash) DO UPDATE SET
            current_path = excluded.current_path,
            file_size = excluded.file_size,
            file_mtime = excluded.file_mtime,
            status = excluded.status,
            processed_at = excluded.processed_at,
            phase1_verified = excluded.phase1_verified
        """,
        row,
    )
    conn.commit()
