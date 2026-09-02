"""SQLite access layer. photos.file_hash is the resumability checkpoint:
a hash already present means this file was already handled by a prior run.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema.sql"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()


def is_known_hash(conn: sqlite3.Connection, file_hash: str) -> bool:
    cur = conn.execute("SELECT 1 FROM photos WHERE file_hash = ? LIMIT 1", (file_hash,))
    return cur.fetchone() is not None


def upsert_photo(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO photos (
            file_hash, current_path, original_path, filename, file_size,
            date_taken, date_taken_year, date_taken_month, date_source,
            status, processed_at, phase1_verified
        ) VALUES (
            :file_hash, :current_path, :original_path, :filename, :file_size,
            :date_taken, :date_taken_year, :date_taken_month, :date_source,
            :status, :processed_at, :phase1_verified
        )
        ON CONFLICT(file_hash) DO UPDATE SET
            current_path = excluded.current_path,
            status = excluded.status,
            processed_at = excluded.processed_at,
            phase1_verified = excluded.phase1_verified
        """,
        row,
    )
    conn.commit()
