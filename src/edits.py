"""Phase 2f: manual-edit audit log + the single generic write path for every
editable field review_tool.py's new edit endpoints expose. See schema.sql's
photo_edits table and photo-organizer-spec.md's Phase 2f section for the
full design reasoning.

Design, briefly:
- photo_edits is an append-only audit trail (file_hash, field_name,
  old_value, new_value, edited_by, edited_at) -- NOT a per-field boolean
  column on `photos`. This gives (a) a real audit trail across multiple
  invited editors (Phase 2d: every authenticated user can edit), and (b) a
  single generic checkpoint (has_manual_edit / manual_edit_fields) any
  automated writer -- main.py load-captions, src/gps_backfill.py -- checks
  before silently overwriting a field a human already touched. Once ANY
  edit is recorded for a (file_hash, field_name) pair, that field is
  permanently protected from future automated writes to it -- there's no
  "un-protect" mechanism; an editor who wants automation to resume for a
  field would just edit it again (itself a fresh, equally-permanent manual
  edit).
- EDITABLE_FIELDS is the server-side allow-list -- apply_edit() only ever
  accepts one of these, never an arbitrary client-supplied field name (see
  review_tool.py's /api/edit/<file_hash> route). "excluded" (Hide from
  library) is deliberately just another entry here, not a separate
  mechanism -- see the spec's "Hide from library" section.
- date_taken is NOT reachable by any automated writer once set: organize.py
  (Phase 1's upsert_photo) already never updates date_taken/date_source on
  a re-scan of an existing row (its ON CONFLICT clause only touches
  current_path/file_size/file_mtime/status/processed_at/phase1_verified),
  so a manual edit there is safe from Phase 1 rescans without needing a
  has_manual_edit() check anywhere -- confirmed, not assumed (see
  src/db.py::upsert_photo and this module's own tests).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

# Server-side allow-list of fields the edit endpoints may ever write --
# review_tool.py's /api/edit/<file_hash> route rejects anything else before
# ever reaching apply_edit(). Never trust a field name from the client.
EDITABLE_FIELDS = {"caption", "tags", "location", "date_taken", "excluded"}

# Generous caps on free-text fields (spec: "reasonable length limits ... to
# prevent abuse") -- comfortably larger than any real caption/place name/tag
# this project has ever produced (see caption.py's real-sample review),
# just there to stop a deliberately huge payload.
MAX_TEXT_FIELD_LENGTH = 4000
MAX_TAG_LENGTH = 100
MAX_TAGS = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def has_manual_edit(conn: sqlite3.Connection, file_hash: str, field_name: str) -> bool:
    """True if a human has ever edited this exact (file_hash, field_name)
    pair -- the checkpoint main.py load-captions / src/gps_backfill.py must
    consult before applying an automated write to caption/tags/location
    respectively. Deliberately permanent: a manual edit made once always
    protects that field, on that file, forever (no expiry, no way for an
    automated writer to reclaim it short of a human editing it again)."""
    row = conn.execute(
        "SELECT 1 FROM photo_edits WHERE file_hash = ? AND field_name = ? LIMIT 1",
        (file_hash, field_name),
    ).fetchone()
    return row is not None


def manual_edit_fields(conn: sqlite3.Connection, file_hash: str) -> set[str]:
    """Every field_name with at least one recorded edit for this file_hash,
    in one query -- used by review_tool.py's _row_to_dict so a photo with
    both an edited caption and edited tags costs one lookup, not two."""
    rows = conn.execute(
        "SELECT DISTINCT field_name FROM photo_edits WHERE file_hash = ?", (file_hash,)
    ).fetchall()
    return {r[0] for r in rows}


def current_tags(conn: sqlite3.Connection, file_hash: str) -> list[str]:
    """The live tags/photo_tags-table tag list for one photo, alphabetical.
    Used both by _set_tags (to compute old_value for the audit log) and by
    review_tool.py's _row_to_dict once a photo's tags have been manually
    edited (see that module's docstring on why unedited rows instead read
    the live captions.jsonl cache, not this table)."""
    rows = conn.execute(
        "SELECT t.tag_name FROM photo_tags pt JOIN tags t ON t.tag_id = pt.tag_id "
        "WHERE pt.file_hash = ? ORDER BY t.tag_name COLLATE NOCASE",
        (file_hash,),
    ).fetchall()
    return [r[0] for r in rows]


def _set_tags(conn: sqlite3.Connection, file_hash: str, tags: list[str]) -> None:
    """Full replace, per spec ("full-replace is fine, doesn't need granular
    add/remove") -- clears every existing photo_tags link for this hash and
    relinks exactly the given list (creating any brand-new tag_name rows as
    needed, same as load_captions.py already does)."""
    conn.execute("DELETE FROM photo_tags WHERE file_hash = ?", (file_hash,))
    seen: set[str] = set()
    for tag_name in tags:
        key = tag_name.lower()
        if key in seen:
            continue
        seen.add(key)
        conn.execute("INSERT OR IGNORE INTO tags (tag_name) VALUES (?)", (tag_name,))
        tag_row = conn.execute("SELECT tag_id FROM tags WHERE tag_name = ?", (tag_name,)).fetchone()
        if tag_row is None:
            continue  # shouldn't happen right after the insert above, but don't let one bad tag kill the whole edit
        conn.execute("INSERT OR IGNORE INTO photo_tags (file_hash, tag_id) VALUES (?, ?)", (file_hash, tag_row[0]))


def _clean_tags(value: Any) -> list[str]:
    """Validates + de-dupes (case-insensitively, keeping the first spelling
    seen) so the list this returns is exactly what ends up stored -- the
    caller echoes this value straight back to the client, so it must match
    reality, not just the raw request body."""
    if not isinstance(value, list):
        raise ValueError("Tags must be a list of strings.")
    cleaned = []
    seen: set[str] = set()
    for t in value:
        t = str(t).strip()
        if not t or t.lower() in seen:
            continue
        if len(t) > MAX_TAG_LENGTH:
            raise ValueError(f"Each tag must be {MAX_TAG_LENGTH} characters or fewer.")
        seen.add(t.lower())
        cleaned.append(t)
    if len(cleaned) > MAX_TAGS:
        raise ValueError(f"Too many tags (max {MAX_TAGS}).")
    return cleaned


def _clean_text(value: Any, field_label: str) -> str | None:
    text = ("" if value is None else str(value)).strip()
    if not text:
        return None
    if len(text) > MAX_TEXT_FIELD_LENGTH:
        raise ValueError(f"{field_label} is too long (max {MAX_TEXT_FIELD_LENGTH} characters).")
    return text


def _parse_date_taken(value: Any) -> tuple[str | None, int | None, int | None]:
    text = ("" if value is None else str(value)).strip()
    if not text:
        return None, None, None
    if len(text) > 40:
        raise ValueError("Date taken value is too long.")
    try:
        year = int(text[0:4])
        month = int(text[5:7])
        if not (1 <= month <= 12):
            raise ValueError
    except (ValueError, IndexError):
        raise ValueError("Date taken must start with YYYY-MM-DD (a full ISO timestamp is also fine).")
    return text, year, month


def apply_edit(conn: sqlite3.Connection, file_hash: str, field_name: str, new_value: Any,
               edited_by: str, logger: logging.Logger | None = None) -> Any:
    """The one generic write path for every editable field: validates
    field_name against EDITABLE_FIELDS, applies the change to
    `photos`/`tags`/`photo_tags`, and records the before/after in
    photo_edits -- all in the caller's existing transaction (commits once,
    at the end, so a mid-way validation error never leaves a half-applied
    edit and an audit row can't be written in some conn.commit() other than
    the one that also applied the change).

    Returns the new, fully-resolved value the caller should echo back to
    the client (e.g. the normalized tags list, not the raw request body).

    Raises ValueError for an unknown field_name, a missing photos row, or
    an invalid value (e.g. a malformed date, an over-length caption) --
    review_tool.py's edit route turns that into a 400.

    Deliberately fetches only the one column each branch needs (by name, in
    a dedicated SELECT) rather than `SELECT *` + row["col"] -- that way this
    works whether or not the caller's connection has row_factory set to
    sqlite3.Row, instead of silently depending on review_tool.py's
    particular connection setup."""
    if field_name not in EDITABLE_FIELDS:
        raise ValueError(f"Field {field_name!r} is not editable.")

    def _old(column: str):
        row = conn.execute(f"SELECT {column} FROM photos WHERE file_hash = ?", (file_hash,)).fetchone()
        if row is None:
            raise ValueError(f"No photo with file_hash={file_hash!r}.")
        return row[0]

    if field_name == "caption":
        old_value = _old("caption")
        result_value = _clean_text(new_value, "Caption")
        conn.execute("UPDATE photos SET caption = ? WHERE file_hash = ?", (result_value, file_hash))
        new_value_str = result_value

    elif field_name == "location":
        old_value = _old("location_name")
        result_value = _clean_text(new_value, "Location")
        conn.execute("UPDATE photos SET location_name = ? WHERE file_hash = ?", (result_value, file_hash))
        new_value_str = result_value

    elif field_name == "date_taken":
        old_value = _old("date_taken")
        result_value, year, month = _parse_date_taken(new_value)
        # UI must make (and does make -- see templates/review.html) clear
        # this ONLY changes this DB record: it does not move, rename, or
        # re-sort the actual file. Phase 1's folder placement is untouched.
        conn.execute(
            "UPDATE photos SET date_taken = ?, date_taken_year = ?, date_taken_month = ?, "
            "date_source = 'manual' WHERE file_hash = ?",
            (result_value, year, month, file_hash),
        )
        new_value_str = result_value

    elif field_name == "tags":
        _old("file_hash")  # existence check only -- tags' "old value" comes from the join below, not a photos column
        old_tags = current_tags(conn, file_hash)
        old_value = json.dumps(old_tags)
        result_value = _clean_tags(new_value)
        _set_tags(conn, file_hash, result_value)
        new_value_str = json.dumps(result_value)

    elif field_name == "excluded":
        old_value = "1" if _old("excluded") else "0"
        result_value = bool(new_value)
        conn.execute("UPDATE photos SET excluded = ? WHERE file_hash = ?", (1 if result_value else 0, file_hash))
        new_value_str = "1" if result_value else "0"

    else:  # pragma: no cover -- unreachable, guarded by the allow-list check above
        raise ValueError(f"Field {field_name!r} is not editable.")

    conn.execute(
        "INSERT INTO photo_edits (file_hash, field_name, old_value, new_value, edited_by, edited_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (file_hash, field_name, old_value, new_value_str, edited_by, _now_iso()),
    )
    conn.commit()
    if logger is not None:
        logger.info(f"EDIT field={field_name} hash={file_hash[:12]} by={edited_by!r}")
    return result_value
