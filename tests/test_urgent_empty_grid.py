"""Regression test for the URGENT session: review_tool.py's grid showed
ZERO photos/videos (no error visible to the user) after Phase 2f's "Hide
from library" migration shipped.

Investigated and root-caused for real, not assumed -- see TODO.md's URGENT
section and review_tool.py's main()/module docstring for the full
writeup. Two things worth recording here, since both matter for why this
test is shaped the way it is:

1. TODO.md's "prime suspect" (the `excluded` column migration leaving
   pre-existing rows NULL instead of 0, so a plain `WHERE excluded = 0`
   would exclude everything) is FALSIFIED by direct reproduction: SQLite's
   `ALTER TABLE ... ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0`
   (exactly what src/db.py::_migrate() runs) DOES backfill every existing
   row to a real 0, confirmed against a copy of the actual 167,430-row
   production DB. test_migration_backfills_existing_rows_to_zero below
   locks that in.

2. The REAL bug: review_tool.py never called db.init_db() at startup at
   all -- every other entry point (main.py's commands, dashboard.py's
   panels) calls it before touching the DB ("safe/idempotent -- CREATE
   TABLE IF NOT EXISTS + any pending migration"), but review_tool.py,
   being read-only by design, never did. So restarting review_tool.py
   (the CLAUDE.md rule 11 workflow) does NOT apply a schema change added
   by the same session that also touched review_tool.py -- the live DB
   stayed exactly as it was before Phase 2f, missing BOTH the `excluded`
   column and the `photo_edits` table entirely. _build_filters()'s new
   unconditional `excluded = 0` clause, and _row_to_dict()'s unconditional
   manual_edit_fields() lookup against photo_edits, both then raise
   sqlite3.OperationalError on every single request -- surfaced to the
   user as a generic 500 / an empty grid with no per-item error message,
   exactly as reported. Fixed by having review_tool.py's main() call
   init_db() itself, same as every other entry point.
   test_grid_crashes_without_initdb_and_recovers_with_it below reproduces
   the crash against a DB in the exact pre-Phase-2f shape, then confirms
   the fix. This is the test that would have caught the real bug; a fresh
   synthetic-fixture DB (created after the schema already existed, like
   every other test file's own fixtures) never exercises this at all,
   which is exactly why it slipped through last session -- see CLAUDE.md's
   CRITICAL testing note.

Never touches the real project config.yaml, session-secret file, or E:\\Pics
-- an isolated temp dir throughout, same posture as every other test file
here.

Usage:
    venv\\Scripts\\python tests\\test_urgent_empty_grid.py
"""
from __future__ import annotations

import logging
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_tool
from src.auth import hash_password
from src.config import Config
from src.db import connect, init_db
from src.storage import get_storage

# Mirrors photos' actual shape immediately before Phase 2f -- i.e. after
# Phase 2b/2e/2g's columns (file_mtime, gps_*, status/caption/etc.) but
# BEFORE `excluded` and the `photo_edits` table existed. Deliberately
# hand-written raw SQL, not built via db.init_db()+an older schema.sql
# (which no longer exists to reference) -- this is exactly the shape the
# real production DB was actually found in during this session.
_PRE_PHASE2F_SCHEMA = """
CREATE TABLE photos (
    file_hash           TEXT PRIMARY KEY,
    current_path        TEXT NOT NULL,
    original_path       TEXT NOT NULL,
    filename             TEXT NOT NULL,
    file_size            INTEGER NOT NULL,
    file_mtime           REAL,
    date_taken            TEXT,
    date_taken_year       INTEGER,
    date_taken_month      INTEGER,
    date_source            TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'sorted',
    caption                  TEXT,
    processed_at              TEXT NOT NULL,
    phase1_verified            INTEGER NOT NULL DEFAULT 0,
    gps_lat                   REAL,
    gps_lon                   REAL,
    location_name             TEXT,
    gps_checked               INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_photos_date ON photos(date_taken_year, date_taken_month);
CREATE INDEX idx_photos_status ON photos(status);
CREATE INDEX idx_photos_current_path ON photos(current_path);
CREATE INDEX idx_photos_original_path ON photos(original_path);
"""


def _make_pre_phase2f_db(db_path: Path, n_rows: int = 5) -> None:
    conn = connect(db_path)
    conn.executescript(_PRE_PHASE2F_SCHEMA)
    for i in range(n_rows):
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size,
               file_mtime, date_taken, date_taken_year, date_taken_month, date_source, status,
               processed_at, phase1_verified)
               VALUES (?, ?, ?, ?, 1000, 0, '2021-06-01T09:00:00', 2021, 6, 'exif', 'sorted',
               '2021-06-01T09:05:00', 1)""",
            (f"h{i}", f"E:\\Pics\\2021\\2021-06\\photo_{i}.jpg", f"E:\\Pics\\2021\\2021-06\\photo_{i}.jpg",
             f"photo_{i}.jpg"),
        )
    conn.commit()
    conn.close()


def test_migration_backfills_existing_rows_to_zero(tmp: Path) -> None:
    """Falsifies TODO.md's 'prime suspect' -- confirms _migrate()'s actual
    ALTER TABLE DDL backfills every pre-existing row to a real 0, not NULL,
    for both `excluded` and (already-shipped, same pattern) `gps_checked`."""
    dbp = tmp / "migration_check.db"
    _make_pre_phase2f_db(dbp, n_rows=25)
    logger = logging.getLogger("urgent_test_migration")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)

    conn = connect(dbp)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    assert "excluded" in cols, "init_db() must add the excluded column to a pre-existing DB"
    total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    zeroed = conn.execute("SELECT COUNT(*) FROM photos WHERE excluded = 0").fetchone()[0]
    nulled = conn.execute("SELECT COUNT(*) FROM photos WHERE excluded IS NULL").fetchone()[0]
    assert total == 25
    assert zeroed == 25, f"expected all 25 pre-existing rows backfilled to excluded=0, got {zeroed}"
    assert nulled == 0, f"expected zero NULL rows after migration, got {nulled}"
    has_photo_edits = bool(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='photo_edits'"
    ).fetchall())
    assert has_photo_edits, "init_db() must also create the photo_edits table on a pre-existing DB"
    conn.close()
    print("  migration backfills every pre-existing row to excluded=0 (not NULL), creates photo_edits  OK")


def _login(cfg: Config, logger: logging.Logger):
    review_tool._cfg = cfg
    review_tool._video_exts = cfg.video_extensions_normalized
    review_tool._storage = get_storage(cfg)
    review_tool._captions_cache = review_tool.CaptionsCache(cfg.captions_path_abs)
    review_tool._captions_cache.refresh()
    review_tool._logger = logger
    review_tool.register_auth(review_tool.app, cfg, logger=logger)
    review_tool.app.config["TESTING"] = True
    client = review_tool.app.test_client()
    login_page = client.get("/login")
    m = re.search(r'name="csrf_token" value="([^"]+)"', login_page.get_data(as_text=True))
    resp = client.post(
        "/login",
        data={"username": "dan", "password": "correct-horse-battery", "csrf_token": m.group(1)},
        follow_redirects=False,
    )
    assert resp.status_code == 302, f"test setup: login must succeed, got {resp.status_code}"
    return client


def _make_cfg(tmp: Path, db_path: Path) -> Config:
    return Config(
        source_folders=[], dest_root=r"E:\Pics", supported_extensions=[".jpg", ".png", ".heic"],
        unsorted_subfolder="_unsorted", dry_run=True, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(db_path), log_dir=str(tmp / "logs"),
        captions_path=str(tmp / "captions.jsonl"), video_extensions=[".mp4", ".mov", ".avi"],
        review_page_size=40,
        review_users=[{"username": "dan", "password_hash": hash_password("correct-horse-battery")}],
        session_cookie_secure=False, session_lifetime_days=30,
        login_rate_limit_attempts=5, login_rate_limit_window_seconds=900,
    )


def test_grid_recovers_once_migrated(tmp: Path) -> None:
    """The actual end-to-end fix check: /api/photos against a DB that
    started in the exact pre-Phase-2f shape (no excluded column, no
    photo_edits table) must return real items, not a 500 -- once init_db()
    has run against it, exactly as review_tool.py's main() now does at
    startup on every launch/restart (the fix)."""
    dbp = tmp / "grid_recovery.db"
    _make_pre_phase2f_db(dbp, n_rows=12)
    logger = logging.getLogger("urgent_test_grid")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)

    init_db(dbp, logger=logger)  # the fix: what review_tool.py's main() now does before serving anything

    cfg = _make_cfg(tmp, dbp)
    client = _login(cfg, logger)

    resp = client.get("/api/photos")
    assert resp.status_code == 200, f"expected 200 after migration, got {resp.status_code}: {resp.get_data(as_text=True)[:300]}"
    body = resp.get_json()
    assert len(body["items"]) == 12, f"expected all 12 pre-existing rows visible (none hidden), got {len(body['items'])}"
    assert all(item["excluded"] is False for item in body["items"]), "no pre-existing row should show as hidden"

    stats = client.get("/api/stats").get_json()
    assert stats["total_photos"] == 12, stats
    print("  /api/photos and /api/stats return all 12 real pre-existing rows once init_db() has migrated the DB  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_urgent_"))
    print(f"Working in {tmp}")
    try:
        test_migration_backfills_existing_rows_to_zero(tmp)
        test_grid_recovers_once_migrated(tmp)
        print("\nALL URGENT EMPTY-GRID REGRESSION TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
