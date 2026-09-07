"""Phase 3i: manual person tagging for review_tool.py -- see schema.sql's
`photo_people_overrides` table (and its own comment) for the full design.

Layered strictly ON TOP of automatic face-detection/clustering data
(faces/people), never touching it: adding/removing a person here never
writes to faces.cluster_id/not_a_person, or to people.face_count/
representative_embedding -- those stay exclusively the Tkinter
FaceLabelingWindow's/src/face_clustering.py's domain. A row in
photo_people_overrides only ever exists when it CONTRADICTS what the
automatic faces/people join alone would say for that (photo, person) pair
-- add_person_to_photo/remove_person_from_photo below are toggle-aware:
each first checks whether the person is already automatically present and
only inserts/updates an override when that's not already the desired end
state, deleting a now-redundant override instead when it is. This keeps
the table as small and self-explaining as possible: every row it does
contain is a real, deliberate correction to the automatic answer.

This module is imported by review_tool.py's write routes only -- it is
NOT imported by main.py/dashboard.py, mirroring src/favorites.py's own
scoping (a review_tool.py-only concern, same posture as every other
review_tool.py-only feature since Phase 2c).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_automatically_present(conn: sqlite3.Connection, file_hash: str, cluster_id: int) -> bool:
    """True if a real detected+clustered (non-not_a_person) face in this
    photo is already assigned to this person, independent of any override
    row. The toggle logic below uses this to decide whether an override
    row is actually needed, or whether clearing one (reverting to the
    automatic answer) already achieves the desired end state."""
    row = conn.execute(
        "SELECT 1 FROM faces WHERE file_hash = ? AND cluster_id = ? AND not_a_person = 0 LIMIT 1",
        (file_hash, cluster_id),
    ).fetchone()
    return row is not None


def find_person_by_name(conn: sqlite3.Connection, name: str) -> int | None:
    """Case-insensitive exact match on assigned_name -- lets "Add Person"
    reuse an already-existing person's cluster_id (and therefore their
    real face/photo history) instead of minting a duplicate every time the
    same name is typed. If more than one real person happens to share a
    name (assigned_name has no uniqueness constraint -- see
    review_tool.py's /api/facets docstring), the one with the most
    detected faces wins, the same tie-break a human would probably want
    ("the real one I already know about," not an obscure duplicate)."""
    row = conn.execute(
        "SELECT cluster_id FROM people WHERE assigned_name IS NOT NULL AND LOWER(assigned_name) = LOWER(?) "
        "ORDER BY face_count DESC LIMIT 1",
        (name,),
    ).fetchone()
    return row[0] if row else None


def create_named_person(conn: sqlite3.Connection, name: str) -> int:
    """Creates a brand-new, real-face-free `people` row for a name that
    doesn't match anyone already in the DB -- e.g. tagging someone
    face_detection.py has never actually detected/clustered a face for.
    face_count starts at 0 and representative_embedding stays NULL; this
    person simply won't appear in the Tkinter labeling window's people
    list (which only shows face_count > 0 groups, see
    src/face_clustering.py::list_clusters) until a real detected face is
    later assigned to them there -- that's fine, this person's only
    "membership" so far is the manual override(s) pointing at them."""
    name = name.strip()
    if not name:
        raise ValueError("Name cannot be blank.")
    cluster_id = conn.execute("SELECT COALESCE(MAX(cluster_id), 0) FROM people").fetchone()[0] + 1
    conn.execute(
        "INSERT INTO people (cluster_id, assigned_name, notes, representative_embedding, face_count, updated_at) "
        "VALUES (?, ?, NULL, NULL, 0, ?)",
        (cluster_id, name, _now_iso()),
    )
    conn.commit()
    return cluster_id


def resolve_or_create_person(conn: sqlite3.Connection, name: str) -> int:
    """The actual entry point review_tool.py's "Add Person" controls call
    when given a typed name rather than an already-known cluster_id (the
    suggestion dropdown sends cluster_id when the typed text exactly
    matched a known person, this otherwise) -- reuses an existing person
    by exact case-insensitive name match, or creates a new one. See
    find_person_by_name/create_named_person above."""
    existing = find_person_by_name(conn, name)
    if existing is not None:
        return existing
    return create_named_person(conn, name)


def _set_override(conn: sqlite3.Connection, file_hash: str, cluster_id: int, action: str, edited_by: str) -> None:
    conn.execute(
        "INSERT INTO photo_people_overrides (file_hash, cluster_id, action, edited_by, edited_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(file_hash, cluster_id) DO UPDATE SET "
        "action = excluded.action, edited_by = excluded.edited_by, edited_at = excluded.edited_at",
        (file_hash, cluster_id, action, edited_by, _now_iso()),
    )


def _clear_override(conn: sqlite3.Connection, file_hash: str, cluster_id: int) -> None:
    conn.execute("DELETE FROM photo_people_overrides WHERE file_hash = ? AND cluster_id = ?", (file_hash, cluster_id))


def add_person_to_photo(conn: sqlite3.Connection, file_hash: str, cluster_id: int, edited_by: str) -> None:
    """Ensures this person shows up as present in this photo. If a real
    detected face already puts them there automatically, this just clears
    any stale 'remove' override that might be suppressing them (the
    intended way to undo an earlier accidental removal) rather than adding
    a redundant 'add' override on top of an already-true automatic fact.
    Otherwise, records an explicit 'add' override.

    Raises ValueError if file_hash/cluster_id don't name a real photo/
    person -- matching src/edits.py::apply_edit's and
    src/favorites.py::set_favorite's posture."""
    if conn.execute("SELECT 1 FROM photos WHERE file_hash = ?", (file_hash,)).fetchone() is None:
        raise ValueError(f"No photo with file_hash={file_hash!r}.")
    if conn.execute("SELECT 1 FROM people WHERE cluster_id = ?", (cluster_id,)).fetchone() is None:
        raise ValueError(f"No person with cluster_id={cluster_id!r}.")
    if is_automatically_present(conn, file_hash, cluster_id):
        _clear_override(conn, file_hash, cluster_id)
    else:
        _set_override(conn, file_hash, cluster_id, "add", edited_by)
    conn.commit()


def remove_person_from_photo(conn: sqlite3.Connection, file_hash: str, cluster_id: int, edited_by: str) -> None:
    """Ensures this person no longer shows up as present in this photo. If
    they're only there via an earlier manual 'add' override (never a real
    detected face), this just deletes that override outright -- there's no
    automatic fact left to suppress, so a 'remove' override would be
    meaningless. If a real detected face DOES put them there automatically,
    records an explicit 'remove' override -- this NEVER touches
    faces.cluster_id/not_a_person; the underlying face/cluster assignment
    the Tkinter labeling window manages is completely unaffected, only
    THIS photo's displayed person list is.

    Raises ValueError if file_hash doesn't name a real photo. Does NOT
    require cluster_id to already exist in `people` (a stale/deleted
    person is simply a harmless no-op override to clear, matching
    add_person_to_photo's own posture of failing loudly only where a bad
    id could otherwise cause confusion, not everywhere)."""
    if conn.execute("SELECT 1 FROM photos WHERE file_hash = ?", (file_hash,)).fetchone() is None:
        raise ValueError(f"No photo with file_hash={file_hash!r}.")
    if is_automatically_present(conn, file_hash, cluster_id):
        _set_override(conn, file_hash, cluster_id, "remove", edited_by)
    else:
        _clear_override(conn, file_hash, cluster_id)
    conn.commit()


def overrides_for_hashes(conn: sqlite3.Connection, file_hashes: list[str]) -> dict[str, list[tuple[int, str, str]]]:
    """Batch lookup: file_hash -> list of (cluster_id, action, assigned_name)
    overrides. ONE query for however many file_hashes are passed, same
    discipline as src/favorites.py::favorites_for_hashes / review_tool.py's
    own _people_for_hashes -- review_tool.py calls this once per grid page
    (or once per single-row response) and layers the result onto the
    automatic faces/people join. Returns {} for an empty input without
    touching the DB, same as those two."""
    if not file_hashes:
        return {}
    placeholders = ",".join("?" * len(file_hashes))
    sql = (
        f"SELECT po.file_hash, po.cluster_id, po.action, p.assigned_name "
        f"FROM photo_people_overrides po JOIN people p ON p.cluster_id = po.cluster_id "
        f"WHERE po.file_hash IN ({placeholders})"
    )
    result: dict[str, list[tuple[int, str, str]]] = {}
    for r in conn.execute(sql, file_hashes).fetchall():
        result.setdefault(r[0], []).append((r[1], r[2], r[3]))
    return result
