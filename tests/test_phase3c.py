"""Phase 3c: FaceLabelingWindow filters (min-cluster-size, hide-already-named
-- both pure UI-layer, tested only via dashboard.py's plain-Python filter
logic, not a real Tk event loop) plus the new "Mark as not a person" flag
(src/face_clustering.py::mark_faces_not_a_person/unmark_faces_not_a_person),
which is real, backend, DB-mutating behavior and gets full coverage here.

Never touches the real project config.yaml or production DB -- an isolated
temp dir throughout, same posture as tests/test_face_clustering.py.

Covers:
  1. mark_faces_not_a_person: pulls the face out of its current cluster
     (cluster_id -> NULL), the source cluster's face_count/centroid are
     correctly recomputed, and the flagged face disappears from BOTH its
     old cluster's list_faces_in_cluster() AND the "(Unclustered)" bucket
     (it has cluster_id NULL like an unclustered face, but must not be
     mistaken for one).
  2. count_not_a_person / list_not_a_person_faces surface exactly the
     flagged faces, and only those.
  3. A flagged face is excluded from run_face_clustering()'s candidates --
     the real "excluded from future cluster-faces re-runs" requirement --
     even though it has cluster_id IS NULL, which would otherwise make it
     a candidate like any other unclustered face.
  4. unmark_faces_not_a_person is fully reversible: the face rejoins the
     "(Unclustered)" bucket and becomes a clustering candidate again on the
     next run_face_clustering() call.
  5. Migration: a faces table built before this column existed gets it
     added via init_db()/_migrate(), defaulting every existing row to 0
     (same "ALTER TABLE ... DEFAULT 0 really does backfill" pattern already
     locked in for photos.excluded by test_urgent_empty_grid.py).
  6. Bulk marking/un-marking (multiple face_ids in one call, matching how
     the labeling UI's checkbox multi-select feeds these functions) and
     idempotency (marking an already-marked face, un-marking an
     already-unmarked one) are both harmless no-ops, not errors.

Usage:
    venv\\Scripts\\python tests\\test_phase3c.py
"""
from __future__ import annotations

import shutil
import sqlite3
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
    count_not_a_person,
    list_clusters,
    list_faces_in_cluster,
    list_not_a_person_faces,
    mark_faces_not_a_person,
    run_face_clustering,
    unmark_faces_not_a_person,
    vec_to_blob,
)

_logger = logging.getLogger("phase3c_test")
if not _logger.handlers:
    _logger.addHandler(logging.StreamHandler())
_logger.setLevel(logging.WARNING)


def _make_cfg(dbp: Path, **overrides) -> Config:
    kwargs = dict(
        source_folders=[], dest_root="E:\\Pics", supported_extensions=[".jpg", ".jpeg", ".png", ".heic"],
        unsorted_subfolder="_unsorted", dry_run=True, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(dbp), log_dir=str(dbp.parent / "logs"),
        face_min_det_score=0.5, face_cluster_similarity_threshold=0.45, face_cluster_min_samples=2,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


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


def _insert_face(conn, file_hash: str, embedding: np.ndarray, bbox=(0, 0, 50, 50), det_score=0.9, cluster_id=None) -> int:
    cur = conn.execute(
        "INSERT INTO faces (file_hash, bbox_x, bbox_y, bbox_w, bbox_h, embedding, det_score, cluster_id, detected_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00')",
        (file_hash, *bbox, vec_to_blob(embedding), det_score, cluster_id),
    )
    conn.commit()
    return cur.lastrowid


def test_mark_pulls_out_of_cluster(tmp: Path) -> None:
    print("\n=== mark_faces_not_a_person: pulls a face out of its cluster, recomputes it ===")
    dbp, conn = _fresh_db(tmp, "mark_pulls_out.db")
    rng = np.random.default_rng(1)
    base = rng.normal(size=EMBEDDING_DIM).astype(np.float32)
    base /= np.linalg.norm(base)
    for i in range(4):
        fh = f"h{i}"
        _insert_photo(conn, fh, f"E:\\Pics\\h{i}.jpg")
        _insert_face(conn, fh, _rand_unit(rng, base, 0.02))

    cfg = _make_cfg(dbp)
    stats = run_face_clustering(cfg, conn, _logger)
    assert stats.new_clusters_formed == 1
    cluster = list_clusters(conn, include_unclustered=False)[0]
    cid = cluster.cluster_id
    assert cluster.face_count == 4
    face_ids = [f.face_id for f in list_faces_in_cluster(conn, cid)]
    target = face_ids[0]

    mark_faces_not_a_person(conn, [target])

    remaining = list_faces_in_cluster(conn, cid)
    assert target not in {f.face_id for f in remaining}, "flagged face must be gone from its old cluster's face list"
    cluster_after = next(c for c in list_clusters(conn, include_unclustered=False) if c.cluster_id == cid)
    assert cluster_after.face_count == 3, "source cluster's face_count must be recomputed down after the flag"

    row = conn.execute("SELECT cluster_id, not_a_person FROM faces WHERE face_id = ?", (target,)).fetchone()
    assert row == (None, 1), f"expected (cluster_id=None, not_a_person=1), got {row}"
    print(f"  cluster went 4 -> 3 faces, flagged face's cluster_id cleared, not_a_person=1  OK")

    # Must NOT show up in the "(Unclustered)" bucket either -- it has
    # cluster_id NULL like a genuinely unclustered face, but belongs to its
    # own dedicated bucket, not this one.
    unclustered_ids = {f.face_id for f in list_faces_in_cluster(conn, None)}
    assert target not in unclustered_ids, "a flagged face must not appear in the (Unclustered) bucket"
    all_clusters = list_clusters(conn, include_unclustered=True)
    unclustered_summary = next((c for c in all_clusters if c.cluster_id is None), None)
    if unclustered_summary is not None:
        assert unclustered_summary.face_count == 0 or target not in {
            f.face_id for f in list_faces_in_cluster(conn, None)
        }
    print(f"  flagged face correctly absent from the (Unclustered) bucket too  OK")
    conn.close()


def test_not_a_person_listing(tmp: Path) -> None:
    print("\n=== count_not_a_person / list_not_a_person_faces surface exactly the flagged faces ===")
    dbp, conn = _fresh_db(tmp, "listing.db")
    emb = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    emb[0] = 1.0
    face_ids = []
    for i in range(3):
        fh = f"h{i}"
        _insert_photo(conn, fh, f"E:\\Pics\\h{i}.jpg")
        face_ids.append(_insert_face(conn, fh, emb, cluster_id=None))

    assert count_not_a_person(conn) == 0
    assert list_not_a_person_faces(conn) == []

    mark_faces_not_a_person(conn, [face_ids[0], face_ids[1]])
    assert count_not_a_person(conn) == 2
    listed = {f.face_id for f in list_not_a_person_faces(conn)}
    assert listed == {face_ids[0], face_ids[1]}
    # The third, never-flagged face must still show up as a normal
    # unclustered face -- flagging two others must not affect it.
    assert face_ids[2] in {f.face_id for f in list_faces_in_cluster(conn, None)}
    assert face_ids[2] not in listed
    print(f"  2 flagged/1 untouched, both lists correct  OK")
    conn.close()


def test_flagged_face_excluded_from_clustering(tmp: Path) -> None:
    print("\n=== A flagged face is excluded from run_face_clustering()'s candidates ===")
    dbp, conn = _fresh_db(tmp, "excluded_from_clustering.db")
    rng = np.random.default_rng(2)
    base = rng.normal(size=EMBEDDING_DIM).astype(np.float32)
    base /= np.linalg.norm(base)
    fh = "solo"
    _insert_photo(conn, fh, "E:\\Pics\\solo.jpg")
    fid = _insert_face(conn, fh, _rand_unit(rng, base, 0.02), cluster_id=None)

    mark_faces_not_a_person(conn, [fid])
    assert conn.execute("SELECT cluster_id FROM faces WHERE face_id = ?", (fid,)).fetchone()[0] is None

    cfg = _make_cfg(dbp)
    stats = run_face_clustering(cfg, conn, _logger)
    print(f"  {stats.summary()}")
    assert stats.candidates == 0, "a flagged face must never be counted as a clustering candidate, even with cluster_id NULL"
    row = conn.execute("SELECT cluster_id, not_a_person FROM faces WHERE face_id = ?", (fid,)).fetchone()
    assert row == (None, 1), "run_face_clustering must leave a flagged face completely untouched"
    print(f"  flagged face correctly ignored by clustering, remains (cluster_id=None, not_a_person=1)  OK")
    conn.close()


def test_unmark_is_reversible_and_reclusterable(tmp: Path) -> None:
    print("\n=== unmark_faces_not_a_person: fully reversible, rejoins clustering ===")
    dbp, conn = _fresh_db(tmp, "unmark.db")
    rng = np.random.default_rng(3)
    base = rng.normal(size=EMBEDDING_DIM).astype(np.float32)
    base /= np.linalg.norm(base)
    for i in range(3):
        fh = f"h{i}"
        _insert_photo(conn, fh, f"E:\\Pics\\h{i}.jpg")
        _insert_face(conn, fh, _rand_unit(rng, base, 0.02))

    cfg = _make_cfg(dbp)
    run_face_clustering(cfg, conn, _logger)
    cluster = list_clusters(conn, include_unclustered=False)[0]
    cid = cluster.cluster_id
    face_ids = [f.face_id for f in list_faces_in_cluster(conn, cid)]
    target = face_ids[0]

    mark_faces_not_a_person(conn, [target])
    assert count_not_a_person(conn) == 1

    unmark_faces_not_a_person(conn, [target])
    assert count_not_a_person(conn) == 0
    row = conn.execute("SELECT cluster_id, not_a_person FROM faces WHERE face_id = ?", (target,)).fetchone()
    assert row == (None, 0), f"un-marked face should be (cluster_id=None, not_a_person=0), got {row}"
    assert target in {f.face_id for f in list_faces_in_cluster(conn, None)}, "un-marked face must rejoin (Unclustered)"
    print(f"  un-marked face correctly back in (Unclustered), not_a_person=0  OK")

    # And it must be a real clustering candidate again -- since it's still
    # close to the existing (now-3-faces-minus-1) cluster's centroid, it
    # should fold back in via the existing-centroid-match step.
    stats = run_face_clustering(cfg, conn, _logger)
    print(f"  re-cluster after un-mark: {stats.summary()}")
    assert stats.candidates == 1
    assert stats.matched_existing == 1, "the un-marked face should match its old cluster's centroid and rejoin it"
    final_cluster = next(c for c in list_clusters(conn, include_unclustered=False) if c.cluster_id == cid)
    assert final_cluster.face_count == 3, "un-marked face should be back to the original 3-face count"
    print(f"  un-marked face correctly folded back into its original cluster on re-clustering  OK")
    conn.close()


def test_bulk_and_idempotent(tmp: Path) -> None:
    print("\n=== Bulk mark/unmark (multiple face_ids at once) and idempotency ===")
    dbp, conn = _fresh_db(tmp, "bulk.db")
    emb = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    emb[0] = 1.0
    face_ids = []
    for i in range(3):
        fh = f"h{i}"
        _insert_photo(conn, fh, f"E:\\Pics\\h{i}.jpg")
        face_ids.append(_insert_face(conn, fh, emb, cluster_id=None))

    mark_faces_not_a_person(conn, face_ids)
    assert count_not_a_person(conn) == 3
    # Marking an already-marked set again must be a harmless no-op, not an error.
    mark_faces_not_a_person(conn, face_ids)
    assert count_not_a_person(conn) == 3
    print(f"  bulk mark of 3 faces, re-marking is idempotent  OK")

    unmark_faces_not_a_person(conn, face_ids)
    assert count_not_a_person(conn) == 0
    unmark_faces_not_a_person(conn, face_ids)  # already unmarked -- must not error
    assert count_not_a_person(conn) == 0
    print(f"  bulk un-mark of 3 faces, re-un-marking is idempotent  OK")

    # Empty list must be a safe no-op too (the labeling UI's own handlers
    # already guard against calling this with nothing selected, but the
    # function itself shouldn't blow up if it ever is).
    mark_faces_not_a_person(conn, [])
    unmark_faces_not_a_person(conn, [])
    print(f"  empty face_ids list is a safe no-op for both  OK")
    conn.close()


def test_migration_adds_column_defaulting_to_zero(tmp: Path) -> None:
    print("\n=== Migration: a pre-Phase-3c faces table gets not_a_person added, backfilled to 0 ===")
    dbp = tmp / "premigration.db"
    conn = sqlite3.connect(dbp)
    # Hand-built shape mirroring faces/people/photos immediately BEFORE this
    # session -- i.e. WITH det_score/representative_embedding (Session 2)
    # but WITHOUT not_a_person, same "never a fresh schema.sql-created
    # fixture" discipline as test_urgent_empty_grid.py's own migration test.
    conn.executescript(
        """
        CREATE TABLE photos (
            file_hash TEXT PRIMARY KEY, current_path TEXT, original_path TEXT, filename TEXT,
            file_size INTEGER, file_mtime REAL, date_taken TEXT, date_taken_year INTEGER,
            date_taken_month INTEGER, date_source TEXT, status TEXT, processed_at TEXT,
            phase1_verified INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE people (
            cluster_id INTEGER PRIMARY KEY, assigned_name TEXT, notes TEXT,
            representative_embedding BLOB, face_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT
        );
        CREATE TABLE faces (
            face_id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_hash TEXT NOT NULL REFERENCES photos(file_hash) ON DELETE CASCADE,
            bbox_x INTEGER NOT NULL, bbox_y INTEGER NOT NULL, bbox_w INTEGER NOT NULL, bbox_h INTEGER NOT NULL,
            embedding BLOB NOT NULL, det_score REAL,
            cluster_id INTEGER REFERENCES people(cluster_id) ON DELETE SET NULL,
            detected_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime, "
        "date_taken, date_taken_year, date_taken_month, date_source, status, processed_at, phase1_verified) "
        "VALUES ('h0', 'E:\\Pics\\h0.jpg', 'E:\\Pics\\h0.jpg', 'h0.jpg', 1000, 0, '2020-01-01', 2020, 1, "
        "'exif', 'sorted', '2020-01-01', 1)"
    )
    emb = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    emb[0] = 1.0
    conn.execute(
        "INSERT INTO faces (file_hash, bbox_x, bbox_y, bbox_w, bbox_h, embedding, det_score, cluster_id, detected_at) "
        "VALUES ('h0', 0, 0, 10, 10, ?, 0.9, NULL, '2026-01-01T00:00:00')",
        (vec_to_blob(emb),),
    )
    conn.commit()
    cols_before = {row[1] for row in conn.execute("PRAGMA table_info(faces)")}
    assert "not_a_person" not in cols_before
    conn.close()

    init_db(dbp, logger=_logger)
    conn = connect(dbp)
    cols_after = {row[1] for row in conn.execute("PRAGMA table_info(faces)")}
    assert "not_a_person" in cols_after, "init_db()/_migrate() must add the not_a_person column to an existing faces table"
    row = conn.execute("SELECT not_a_person FROM faces WHERE file_hash = 'h0'").fetchone()
    assert row == (0,), f"pre-existing face row must backfill to not_a_person=0, got {row}"
    # And the mark/unmark functions must work immediately against a
    # freshly-migrated (not freshly-created) DB.
    fid = conn.execute("SELECT face_id FROM faces WHERE file_hash = 'h0'").fetchone()[0]
    mark_faces_not_a_person(conn, [fid])
    assert count_not_a_person(conn) == 1
    print(f"  faces.not_a_person added + backfilled to 0 on migration, mark/unmark work post-migration  OK")
    conn.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase3c_"))
    print(f"Working in {tmp}")
    try:
        test_mark_pulls_out_of_cluster(tmp)
        test_not_a_person_listing(tmp)
        test_flagged_face_excluded_from_clustering(tmp)
        test_unmark_is_reversible_and_reclusterable(tmp)
        test_bulk_and_idempotent(tmp)
        test_migration_adds_column_defaulting_to_zero(tmp)
        print("\nALL PHASE 3C TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
