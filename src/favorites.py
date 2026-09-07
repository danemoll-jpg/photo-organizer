"""Phase 3h: per-user favorites for review_tool.py. See schema.sql's
`favorites` table and photo-organizer-spec.md's Phase 3h section for the
full requirements.

Design, briefly:
- One row per (file_hash, username), PK-enforced -- this is what makes
  favorites genuinely independent per invited person (Phase 2d: every
  review_user is their own account), not one shared yes/no per photo.
  Favoriting is an INSERT, un-favoriting a DELETE of just that one row --
  neither ever touches another user's row for the same photo.
- Deliberately NOT part of src/edits.py's photo_edits audit mechanism.
  That table exists to protect a field from a future AUTOMATED overwrite
  (main.py load-captions / src/gps_backfill.py silently clobbering a human
  edit) -- nothing in this project ever automates favoriting a photo, so
  there's no future writer to protect against, and no audit trail was
  asked for (the favorites table itself, with its own favorited_at, already
  answers "who favorited what and when" -- a second parallel record of the
  same fact in photo_edits would be redundant, not more auditable).
- set_favorite() is an idempotent SET (favorited=True/False), not a
  toggle -- the caller (review_tool.py's /api/favorite/<hash> route)
  already knows the desired end state from the request body (the front
  end sends the opposite of whatever it's currently showing, same pattern
  as apply_edit("excluded", ...) for "Hide from library" -- see
  static/review.js's toggleHidden), so there's no ambiguity to resolve
  here about what "toggle" would mean against a possibly-stale client view.
- favorites_for_hashes() is the batched lookup every grid page (and every
  single-row response) goes through -- ONE query per call regardless of
  how many file_hashes are passed, the same discipline
  review_tool.py::_people_for_hashes() established for Phase 3e (proven,
  not assumed, by tests/test_phase3h.py -- never repeat the N+1 mistake
  that had to be caught there).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_favorited(conn: sqlite3.Connection, file_hash: str, username: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM favorites WHERE file_hash = ? AND username = ?", (file_hash, username)
    ).fetchone()
    return row is not None


def set_favorite(conn: sqlite3.Connection, file_hash: str, username: str, favorited: bool) -> bool:
    """Idempotent set (see module docstring) -- favoriting an already-
    favorited photo, or un-favoriting one that isn't, is a harmless no-op
    (INSERT OR IGNORE / a DELETE matching zero rows), not an error.

    Raises ValueError if file_hash names no real photo -- matching
    apply_edit()'s posture in src/edits.py (see its docstring): the foreign
    key's ON DELETE CASCADE would otherwise just silently insert nothing
    useful for a stale/bad hash, which is worth surfacing as a caller error
    rather than a quiet no-op. Returns `favorited` (the now-current state)
    for symmetry with apply_edit()'s "return what the caller should show"
    convention -- review_tool.py's route doesn't strictly need this back
    (it re-reads the whole photo via _row_to_dict right after), but it
    costs nothing to be consistent."""
    row = conn.execute("SELECT 1 FROM photos WHERE file_hash = ?", (file_hash,)).fetchone()
    if row is None:
        raise ValueError(f"No photo with file_hash={file_hash!r}.")
    if favorited:
        conn.execute(
            "INSERT OR IGNORE INTO favorites (file_hash, username, favorited_at) VALUES (?, ?, ?)",
            (file_hash, username, _now_iso()),
        )
    else:
        conn.execute("DELETE FROM favorites WHERE file_hash = ? AND username = ?", (file_hash, username))
    conn.commit()
    return favorited


def favorites_for_hashes(conn: sqlite3.Connection, file_hashes: list[str]) -> dict[str, list[str]]:
    """Batch lookup: file_hash -> sorted list of usernames who favorited it.
    ONE query for however many file_hashes are passed, never one per
    file_hash -- review_tool.py's _fetch_page calls this once per grid page
    (see _people_for_hashes' identical shape/reasoning for Phase 3e), and
    every single-row caller (nav/random/edit/favorite-toggle) just passes a
    one-element list through the same query shape. Returns {} for an empty
    input without touching the DB, same as _people_for_hashes.

    Deliberately unpacks each row positionally (not by column name) rather
    than assuming the caller's connection has row_factory set to
    sqlite3.Row -- same reasoning src/edits.py::apply_edit gives for its
    own by-name SELECTs, applied here to a query that returns two plain
    columns instead."""
    if not file_hashes:
        return {}
    placeholders = ",".join("?" * len(file_hashes))
    sql = (
        f"SELECT file_hash, username FROM favorites WHERE file_hash IN ({placeholders}) "
        f"ORDER BY username COLLATE NOCASE"
    )
    result: dict[str, list[str]] = {}
    for row in conn.execute(sql, file_hashes).fetchall():
        file_hash, username = row[0], row[1]
        result.setdefault(file_hash, []).append(username)
    return result
