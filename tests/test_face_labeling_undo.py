"""URGENT follow-up session: "Undo Last Action" for FaceLabelingWindow's
mutating operations (src/face_clustering.py::snapshot_faces_for_undo/
restore_faces_from_undo/snapshot_before_merge/undo_merge), plus a source-
presence check that dashboard.py's affected-face-count confirmation
(_confirm_if_multiple_faces_affected -- renamed and generalized by Phase 3o,
see below; was _confirm_if_selection_spans_pages) is actually wired into
every action that reassigns cluster_id/not_a_person.

Phase 3o update: this method used to warn ONLY when the selection reached
beyond the currently-rendered page. A real incident found that gap: a
same-page multi-face selection (e.g. two faces checked earlier and
forgotten) produced ZERO warning, so a single right-click "Mark as Not a
Person" on just ONE of those faces -- Phase 3m Part 6's own by-design "act
on the whole selection if the clicked face is part of it" convention --
silently flagged BOTH with no confirmation at all. Fixed by dropping the
off-page-only condition: it now fires whenever the actual affected count
is more than 1, period (see dashboard.py's own docstring on the renamed
method for the full writeup and the ad hoc repro that confirmed the exact
mechanism before fixing it).

Real trigger: a real incident where "Select All" (whole cluster, not just
the visible page) was mistaken for a page-scoped select, and a Move
silently reassigned all 17,764 faces in a group -- 17,738 of them never
actually looked at -- into an already-good, already-named person's
cluster. Two fixes: (1) a confirmation before any bulk action whose
selection reaches beyond the current page, (2) this real, reversible
Undo mechanism, so a mistake like that is a one-click fix, not a
half-hour forensic DB recovery.

Backend coverage here (fast, no Tk event loop needed -- the actual
confirmation-dialog/button wiring was verified via an ad hoc headless
Tk() script, same precedent as every other FaceLabelingWindow session):
  1. snapshot_faces_for_undo/restore_faces_from_undo: a plain Move-then-
     undo round-trips exactly (cluster_id/not_a_person restored, both
     endpoints' centroids/face_counts correct).
  2. The real bug this session's own testing caught and fixed: undoing an
     action that moved EVERY face out of a cluster (so
     _recompute_person_centroid's orphan-cleanup deleted that cluster's
     `people` row outright) must recreate the row with its ORIGINAL name/
     notes before reassigning faces back to it -- not fail with a FK
     error, and not silently lose the name.
  3. Split-then-undo: the brand-new person Split created is cleanly
     removed again (0 faces -> orphan-deleted), and the faces return to
     their real prior cluster.
  4. Mark-as-not-a-person-then-undo: not_a_person correctly cleared and
     faces rejoin their original cluster (not just "(Unclustered)").
  5. snapshot_before_merge/undo_merge: a merge (including the "keep
     adopts other's name" case) is fully reversible -- the deleted
     `other` row reappears with its real name/notes and its exact face
     set, and `keep`'s pre-merge name/notes are restored too.
  6. dashboard.py source check: _confirm_if_multiple_faces_affected is
     called from _on_split/_on_move/_on_remove_from_cluster/
     _on_mark_not_a_person/_on_unmark_not_a_person, its own body no longer
     contains the old off-page-only early return (the exact Phase 3o gap),
     and "Select This Page"/"Select All"/"Undo Last Action" all exist as
     real buttons -- a regression guard against silently dropping the
     safety check (or quietly reintroducing its off-page-only blind spot)
     in a future edit.

Usage:
    venv\\Scripts\\python tests\\test_face_labeling_undo.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging

from src.config import Config
from src.db import connect, init_db
from src.face_clustering import (
    EMBEDDING_DIM,
    create_person_from_faces,
    list_faces_in_cluster,
    mark_faces_not_a_person,
    merge_people,
    move_faces_to_person,
    restore_faces_from_undo,
    snapshot_before_merge,
    snapshot_faces_for_undo,
    undo_merge,
    vec_to_blob,
)

_logger = logging.getLogger("face_labeling_undo_test")
if not _logger.handlers:
    _logger.addHandler(logging.StreamHandler())
_logger.setLevel(logging.WARNING)


def _fresh_db(tmp: Path, name: str):
    dbp = tmp / name
    init_db(dbp, logger=_logger)
    return dbp, connect(dbp)


def _insert_photo(conn, file_hash: str, path: str) -> None:
    conn.execute(
        """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
           date_taken, date_taken_year, date_taken_month, date_source, status, processed_at, phase1_verified)
           VALUES (?, ?, ?, ?, 1000, 0, '2020-01-01', 2020, 1, 'exif', 'sorted', '2020-01-01', 1)""",
        (file_hash, path, path, Path(path).name),
    )
    conn.commit()


def _rand_unit(rng: np.random.Generator, base: np.ndarray, noise: float) -> np.ndarray:
    v = base + rng.normal(scale=noise, size=base.shape).astype(np.float32)
    return (v / np.linalg.norm(v)).astype(np.float32)


def _add_person(conn, rng, cluster_id: int, name: str | None, n: int, seed_dim: int) -> list[int]:
    conn.execute(
        "INSERT INTO people (cluster_id, assigned_name, notes, representative_embedding, face_count, updated_at) "
        "VALUES (?, ?, NULL, NULL, 0, '2026-01-01T00:00:00')",
        (cluster_id, name),
    )
    base = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    base[seed_dim % EMBEDDING_DIM] = 1.0
    face_ids = []
    for i in range(n):
        fh = f"c{cluster_id}_{i:04d}"
        _insert_photo(conn, fh, f"E:\\Pics\\{fh}.jpg")
        face_ids.append(_insert_face(conn, fh, _rand_unit(rng, base, 0.02), cluster_id=cluster_id))
    conn.execute("UPDATE people SET face_count = ? WHERE cluster_id = ?", (n, cluster_id))
    conn.commit()
    return face_ids


def _insert_face(conn, file_hash: str, embedding: np.ndarray, bbox=(0, 0, 50, 50), det_score=0.9, cluster_id=None) -> int:
    cur = conn.execute(
        "INSERT INTO faces (file_hash, bbox_x, bbox_y, bbox_w, bbox_h, embedding, det_score, cluster_id, detected_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00')",
        (file_hash, *bbox, vec_to_blob(embedding), det_score, cluster_id),
    )
    conn.commit()
    return cur.lastrowid


def test_move_then_undo_round_trips(tmp: Path) -> None:
    print("\n=== Move-then-undo: cluster_id/face_count round-trip exactly ===")
    dbp, conn = _fresh_db(tmp, "move_undo.db")
    rng = np.random.default_rng(1)
    a_ids = _add_person(conn, rng, 100, "Group A", n=10, seed_dim=1)
    _add_person(conn, rng, 200, "Group B", n=5, seed_dim=2)

    snapshot = snapshot_faces_for_undo(conn, a_ids[:4])
    move_faces_to_person(conn, a_ids[:4], dest_cluster_id=200)

    a_after = conn.execute("SELECT face_count FROM people WHERE cluster_id=100").fetchone()
    b_after = conn.execute("SELECT face_count FROM people WHERE cluster_id=200").fetchone()
    assert a_after == (6,) and b_after == (9,)
    print("  move applied: A=6, B=9  OK")

    restore_faces_from_undo(conn, snapshot)
    a_undone = conn.execute("SELECT face_count FROM people WHERE cluster_id=100").fetchone()
    b_undone = conn.execute("SELECT face_count FROM people WHERE cluster_id=200").fetchone()
    assert a_undone == (10,) and b_undone == (5,)
    moved_ids_now = {f.face_id for f in list_faces_in_cluster(conn, 100)}
    assert moved_ids_now == set(a_ids)
    print("  undo restored: A=10, B=5, exact face_ids back in A  OK")
    conn.close()


def test_undo_recreates_a_cluster_emptied_entirely(tmp: Path) -> None:
    """The real bug this session's own testing caught: if EVERY face in a
    cluster is moved out (the exact "Select All + Move" pattern from the
    real incident), _recompute_person_centroid's orphan-cleanup deletes
    that cluster's people row -- undoing such a move must recreate the row
    (with its real name/notes) before reassigning faces back, or it fails
    outright (an FK violation) rather than silently losing the name."""
    print("\n=== Undo recreates a source cluster the move emptied entirely (real bug found & fixed) ===")
    dbp, conn = _fresh_db(tmp, "undo_orphan.db")
    rng = np.random.default_rng(2)
    a_ids = _add_person(conn, rng, 100, "Juliana Moll", n=8, seed_dim=1)
    _add_person(conn, rng, 200, "Target Person", n=3, seed_dim=2)

    snapshot = snapshot_faces_for_undo(conn, a_ids)  # ALL of A's faces
    move_faces_to_person(conn, a_ids, dest_cluster_id=200)

    gone = conn.execute("SELECT 1 FROM people WHERE cluster_id=100").fetchone()
    assert gone is None, "emptied cluster's people row should be orphan-deleted, as designed"
    b_after = conn.execute("SELECT face_count FROM people WHERE cluster_id=200").fetchone()
    assert b_after == (11,)
    print("  move applied: A's people row deleted (0 faces), B=11  OK")

    restore_faces_from_undo(conn, snapshot)  # must NOT raise a FK error
    a_row = conn.execute("SELECT assigned_name, face_count FROM people WHERE cluster_id=100").fetchone()
    assert a_row == ("Juliana Moll", 8), f"expected A recreated with its real name and all 8 faces back, got {a_row}"
    b_row = conn.execute("SELECT face_count FROM people WHERE cluster_id=200").fetchone()
    assert b_row == (3,)
    restored_ids = {f.face_id for f in list_faces_in_cluster(conn, 100)}
    assert restored_ids == set(a_ids)
    print("  undo recreated cluster 100 with its ORIGINAL name 'Juliana Moll' and exact 8 faces, B back to 3  OK")
    conn.close()


def test_split_then_undo_removes_the_new_person(tmp: Path) -> None:
    print("\n=== Split-then-undo: the brand-new person is cleanly removed again ===")
    dbp, conn = _fresh_db(tmp, "split_undo.db")
    rng = np.random.default_rng(3)
    a_ids = _add_person(conn, rng, 100, None, n=10, seed_dim=1)
    subset = a_ids[:3]

    snapshot = snapshot_faces_for_undo(conn, subset)
    new_cid = create_person_from_faces(conn, subset, name="New Person")
    new_row = conn.execute("SELECT assigned_name, face_count FROM people WHERE cluster_id=?", (new_cid,)).fetchone()
    assert new_row == ("New Person", 3)
    a_after = conn.execute("SELECT face_count FROM people WHERE cluster_id=100").fetchone()
    assert a_after == (7,)
    print(f"  split created cluster {new_cid} with 3 faces, A down to 7  OK")

    restore_faces_from_undo(conn, snapshot)
    gone = conn.execute("SELECT 1 FROM people WHERE cluster_id=?", (new_cid,)).fetchone()
    assert gone is None, "the new person should be orphan-deleted once every face is undone out of it"
    a_restored = conn.execute("SELECT face_count FROM people WHERE cluster_id=100").fetchone()
    assert a_restored == (10,)
    restored_ids = {f.face_id for f in list_faces_in_cluster(conn, 100)}
    assert restored_ids == set(a_ids)
    print("  undo deleted the new person and restored all 10 faces to A  OK")
    conn.close()


def test_mark_not_a_person_then_undo(tmp: Path) -> None:
    print("\n=== Mark-as-not-a-person-then-undo: rejoins the ORIGINAL cluster, not just Unclustered ===")
    dbp, conn = _fresh_db(tmp, "mark_undo.db")
    rng = np.random.default_rng(4)
    a_ids = _add_person(conn, rng, 100, "Group A", n=6, seed_dim=1)
    subset = a_ids[:2]

    snapshot = snapshot_faces_for_undo(conn, subset)
    mark_faces_not_a_person(conn, subset)
    for fid in subset:
        row = conn.execute("SELECT cluster_id, not_a_person FROM faces WHERE face_id=?", (fid,)).fetchone()
        assert row == (None, 1)
    print("  marked: cluster_id=NULL, not_a_person=1  OK")

    restore_faces_from_undo(conn, snapshot)
    for fid in subset:
        row = conn.execute("SELECT cluster_id, not_a_person FROM faces WHERE face_id=?", (fid,)).fetchone()
        assert row == (100, 0), f"expected face back in cluster 100 with not_a_person=0, got {row}"
    a_restored = conn.execute("SELECT face_count FROM people WHERE cluster_id=100").fetchone()
    assert a_restored == (6,)
    print("  undo restored cluster_id=100, not_a_person=0, face_count back to 6  OK")
    conn.close()


def test_merge_then_undo_round_trips(tmp: Path) -> None:
    print("\n=== Merge-then-undo: deleted 'other' row recreated with its real name/notes, keep's name restored ===")
    dbp, conn = _fresh_db(tmp, "merge_undo.db")
    rng = np.random.default_rng(5)
    # keep starts UNNAMED, other IS named -- exercises merge_people's own
    # "keep adopts other's name" convenience, and undo_merge must reverse
    # that adoption, not just move faces back.
    keep_ids = _add_person(conn, rng, 100, None, n=4, seed_dim=1)
    other_ids = _add_person(conn, rng, 200, "Other Person", n=6, seed_dim=2)
    conn.execute("UPDATE people SET notes = ? WHERE cluster_id = 200", ("some notes",))
    conn.commit()

    snapshot = snapshot_before_merge(conn, keep_cluster_id=100, other_cluster_id=200)
    merge_people(conn, keep_cluster_id=100, other_cluster_id=200)

    keep_after = conn.execute("SELECT assigned_name, face_count FROM people WHERE cluster_id=100").fetchone()
    assert keep_after == ("Other Person", 10), f"expected keep to adopt other's name and gain its faces, got {keep_after}"
    other_gone = conn.execute("SELECT 1 FROM people WHERE cluster_id=200").fetchone()
    assert other_gone is None
    print("  merge applied: keep adopted 'Other Person', 10 faces, other's row deleted  OK")

    undo_merge(conn, snapshot)
    keep_undone = conn.execute("SELECT assigned_name, face_count FROM people WHERE cluster_id=100").fetchone()
    assert keep_undone == (None, 4), f"expected keep back to unnamed with 4 faces, got {keep_undone}"
    other_undone = conn.execute("SELECT assigned_name, notes, face_count FROM people WHERE cluster_id=200").fetchone()
    assert other_undone == ("Other Person", "some notes", 6), f"expected other recreated exactly, got {other_undone}"
    other_restored_ids = {f.face_id for f in list_faces_in_cluster(conn, 200)}
    assert other_restored_ids == set(other_ids)
    keep_restored_ids = {f.face_id for f in list_faces_in_cluster(conn, 100)}
    assert keep_restored_ids == set(keep_ids)
    print("  undo restored: keep back to unnamed/4 faces, other recreated with real name+notes+exact 6 faces  OK")
    conn.close()


def test_dashboard_wires_confirmation_and_undo_everywhere() -> None:
    """Source-presence check (no Tk event loop) -- the actual click-through
    behavior was verified via an ad hoc headless Tk() script, same
    precedent as every other FaceLabelingWindow session. This just guards
    against a future edit silently dropping the confirmation call or the
    Undo button from one of the mutating handlers."""
    print("\n=== dashboard.py: confirmation + undo wired into every mutating action ===")
    src = Path(__file__).resolve().parent.parent / "dashboard.py"
    text = src.read_text(encoding="utf-8")

    assert '"Select This Page"' in text and '"Select All"' in text and '"Undo Last Action"' in text
    print("  Select This Page / Select All / Undo Last Action buttons all present  OK")

    for handler, needs_confirm in [
        ("_on_split", True),
        ("_on_move", True),
        ("_on_remove_from_cluster", True),
        ("_on_mark_not_a_person", True),
        ("_on_unmark_not_a_person", True),
        ("_on_save_name", False),  # not selection-based -- no multi-face risk
    ]:
        start = text.index(f"def {handler}(")
        end = text.index("\n    def ", start + 1)
        body = text[start:end]
        if needs_confirm:
            assert "_confirm_if_multiple_faces_affected" in body, f"{handler} is missing the affected-count confirmation"
        assert "_push_faces_undo" in body or "_push_undo" in body, f"{handler} is missing an undo-stack push"
    print("  every selection-based action confirms multi-face effects AND pushes an undo entry  OK")
    print("  _on_save_name pushes an undo entry (no confirmation needed -- not selection-based)  OK")

    # Phase 3o: guard against the exact regression this session fixed --
    # the confirmation must fire based on the AFFECTED COUNT, not merely
    # whether part of the selection is off-page. If a future edit
    # reintroduces an early "if not off_page: return True"-style check
    # before the total>1 check, this will catch it.
    fn_start = text.index("def _confirm_if_multiple_faces_affected(")
    fn_end = text.index("\n    def ", fn_start + 1)
    fn_body = text[fn_start:fn_end]
    assert "total <= 1" in fn_body or "total > 1" in fn_body, (
        "_confirm_if_multiple_faces_affected no longer appears to gate on the total affected count"
    )
    off_page_check_pos = fn_body.find("off_page")
    total_check_pos = fn_body.find("total <= 1")
    assert total_check_pos != -1 and (off_page_check_pos == -1 or total_check_pos < off_page_check_pos), (
        "the total-affected-count check must be evaluated before any off-page-only logic, "
        "or a same-page multi-face selection could silently skip confirmation again"
    )
    print("  _confirm_if_multiple_faces_affected gates on total affected count, not just off-page  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_face_labeling_undo_"))
    print(f"Working in {tmp}")
    try:
        test_move_then_undo_round_trips(tmp)
        test_undo_recreates_a_cluster_emptied_entirely(tmp)
        test_split_then_undo_removes_the_new_person(tmp)
        test_mark_not_a_person_then_undo(tmp)
        test_merge_then_undo_round_trips(tmp)
        test_dashboard_wires_confirmation_and_undo_everywhere()
        print("\nALL FACE LABELING UNDO TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
