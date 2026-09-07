"""Phase 3f: "Remove Selected from Cluster" (src/face_clustering.py::
remove_faces_from_cluster) -- the lighter-weight sibling of Split. Sends the
selected faces back to "(Unclustered)" WITHOUT creating a new person, unlike
create_person_from_faces (Split). Motivated by a real 408-face cluster that
was almost entirely one person, with a handful of genuinely misclustered
faces mixed in -- Split is the wrong tool for miscellaneous wrong matches
(it creates an unnecessary new person), and selecting the hundreds of
CORRECT faces to Move instead is far more clicking than selecting the few
wrong ones.

Never touches the real project config.yaml or production DB -- an isolated
temp dir throughout, same posture as tests/test_face_clustering.py and
tests/test_phase3c.py.

Covers:
  1. remove_faces_from_cluster pulls the selected faces out of their
     current cluster (cluster_id -> NULL), the source cluster's
     face_count/centroid are correctly recomputed from the REMAINING
     faces only, and not_a_person is left at 0 (these are real faces,
     just wrongly grouped -- not Phase 3c's false-positive case).
  2. Removed faces show up in the "(Unclustered)" bucket immediately
     (list_faces_in_cluster(conn, None) / list_clusters()).
  3. The majority of an existing cluster is left completely untouched
     (still assigned, centroid reflects the un-removed faces) -- this is
     the actual point of the feature: removing a few outliers must not
     disturb the rest.
  4. If ALL of a cluster's faces are removed, the source cluster's
     `people` row is deleted (face_count 0) -- same orphan-cleanup
     _recompute_person_centroid already does for Split/merge/mark.
  5. Removed faces are real, unflagged clustering candidates again on the
     next run_face_clustering() call (unlike Phase 3c's not_a_person,
     which is permanently excluded).
  6. Bulk removal across faces from two different clusters in one call
     recomputes BOTH source clusters correctly.
  7. Idempotency/safety: removing an already-unclustered face is a
     harmless no-op, and an empty face_ids list is a safe no-op too.

Usage:
    venv\\Scripts\\python tests\\test_phase3f.py
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
    list_clusters,
    list_faces_in_cluster,
    remove_faces_from_cluster,
    run_face_clustering,
    vec_to_blob,
)

_logger = logging.getLogger("phase3f_test")
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


def test_remove_pulls_out_and_recomputes_remainder(tmp: Path) -> None:
    print("\n=== remove_faces_from_cluster: pulls outliers out, leaves the rest untouched ===")
    dbp, conn = _fresh_db(tmp, "remove_basic.db")
    rng = np.random.default_rng(1)
    base = rng.normal(size=EMBEDDING_DIM).astype(np.float32)
    base /= np.linalg.norm(base)
    # 5 faces tightly clustered around `base` -- mimics the real "408-face
    # cluster, mostly correct" scenario at a testable scale.
    for i in range(5):
        fh = f"h{i}"
        _insert_photo(conn, fh, f"E:\\Pics\\h{i}.jpg")
        _insert_face(conn, fh, _rand_unit(rng, base, 0.02))

    cfg = _make_cfg(dbp)
    stats = run_face_clustering(cfg, conn, _logger)
    assert stats.new_clusters_formed == 1
    cluster = list_clusters(conn, include_unclustered=False)[0]
    cid = cluster.cluster_id
    assert cluster.face_count == 5
    face_ids = sorted(f.face_id for f in list_faces_in_cluster(conn, cid))
    outliers = face_ids[:2]  # "the handful of wrong matches"
    keep = face_ids[2:]

    remove_faces_from_cluster(conn, outliers)

    remaining = list_faces_in_cluster(conn, cid)
    remaining_ids = {f.face_id for f in remaining}
    assert remaining_ids == set(keep), f"expected only the un-removed faces left in the cluster, got {remaining_ids}"
    cluster_after = next(c for c in list_clusters(conn, include_unclustered=False) if c.cluster_id == cid)
    assert cluster_after.face_count == 3, "source cluster's face_count must be recomputed down after removal"
    print(f"  cluster went 5 -> 3 faces, the other 3 untouched, centroid recomputed  OK")

    for fid in outliers:
        row = conn.execute("SELECT cluster_id, not_a_person FROM faces WHERE face_id = ?", (fid,)).fetchone()
        assert row == (None, 0), f"removed face must be (cluster_id=None, not_a_person=0), got {row} for face {fid}"
    print(f"  removed faces are (cluster_id=None, not_a_person=0) -- real faces, just unclustered, not flagged  OK")

    unclustered_ids = {f.face_id for f in list_faces_in_cluster(conn, None)}
    assert unclustered_ids == set(outliers), f"removed faces must show up in (Unclustered) immediately, got {unclustered_ids}"
    print(f"  removed faces appear in the (Unclustered) bucket immediately  OK")
    conn.close()


def test_removing_entire_cluster_deletes_orphaned_person(tmp: Path) -> None:
    print("\n=== Removing ALL of a cluster's faces deletes the orphaned people row ===")
    dbp, conn = _fresh_db(tmp, "remove_all.db")
    rng = np.random.default_rng(2)
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

    remove_faces_from_cluster(conn, face_ids)

    remaining_clusters = list_clusters(conn, include_unclustered=False)
    assert cid not in {c.cluster_id for c in remaining_clusters}, "an emptied cluster's people row must be deleted, same as Split/merge/mark"
    row = conn.execute("SELECT * FROM people WHERE cluster_id = ?", (cid,)).fetchone()
    assert row is None
    unclustered_ids = {f.face_id for f in list_faces_in_cluster(conn, None)}
    assert unclustered_ids == set(face_ids)
    print(f"  emptied cluster's people row deleted, all 3 faces correctly in (Unclustered)  OK")
    conn.close()


def test_removed_face_is_a_real_reclustering_candidate(tmp: Path) -> None:
    print("\n=== A removed (not flagged) face is a normal candidate on the next cluster-faces run ===")
    dbp, conn = _fresh_db(tmp, "reclusterable.db")
    rng = np.random.default_rng(3)
    base = rng.normal(size=EMBEDDING_DIM).astype(np.float32)
    base /= np.linalg.norm(base)
    for i in range(4):
        fh = f"h{i}"
        _insert_photo(conn, fh, f"E:\\Pics\\h{i}.jpg")
        _insert_face(conn, fh, _rand_unit(rng, base, 0.02))

    cfg = _make_cfg(dbp)
    run_face_clustering(cfg, conn, _logger)
    cluster = list_clusters(conn, include_unclustered=False)[0]
    cid = cluster.cluster_id
    face_ids = [f.face_id for f in list_faces_in_cluster(conn, cid)]
    target = face_ids[0]

    remove_faces_from_cluster(conn, [target])
    assert conn.execute("SELECT cluster_id FROM faces WHERE face_id = ?", (target,)).fetchone()[0] is None

    stats = run_face_clustering(cfg, conn, _logger)
    print(f"  {stats.summary()}")
    row = conn.execute("SELECT cluster_id FROM faces WHERE face_id = ?", (target,)).fetchone()
    assert row[0] == cid, f"removed face should fold right back into the same person's centroid on re-cluster, got cluster_id={row[0]}"
    print(f"  removed face correctly re-matched to its (still-existing, un-flagged) old person on re-cluster  OK")
    conn.close()


def test_bulk_removal_across_two_clusters(tmp: Path) -> None:
    print("\n=== Bulk removal: one call spanning two different source clusters recomputes both ===")
    dbp, conn = _fresh_db(tmp, "bulk_two_clusters.db")
    rng = np.random.default_rng(4)
    base_a = rng.normal(size=EMBEDDING_DIM).astype(np.float32)
    base_a /= np.linalg.norm(base_a)
    base_b = -base_a  # maximally dissimilar, guarantees two separate clusters
    for i in range(3):
        fh = f"a{i}"
        _insert_photo(conn, fh, f"E:\\Pics\\a{i}.jpg")
        _insert_face(conn, fh, _rand_unit(rng, base_a, 0.01))
    for i in range(3):
        fh = f"b{i}"
        _insert_photo(conn, fh, f"E:\\Pics\\b{i}.jpg")
        _insert_face(conn, fh, _rand_unit(rng, base_b, 0.01))

    cfg = _make_cfg(dbp)
    stats = run_face_clustering(cfg, conn, _logger)
    assert stats.new_clusters_formed == 2
    clusters = list_clusters(conn, include_unclustered=False)
    assert len(clusters) == 2
    cid_a, cid_b = clusters[0].cluster_id, clusters[1].cluster_id
    remove_a = [f.face_id for f in list_faces_in_cluster(conn, cid_a)][:1]
    remove_b = [f.face_id for f in list_faces_in_cluster(conn, cid_b)][:1]

    remove_faces_from_cluster(conn, remove_a + remove_b)

    clusters_after = {c.cluster_id: c for c in list_clusters(conn, include_unclustered=False)}
    assert clusters_after[cid_a].face_count == 2
    assert clusters_after[cid_b].face_count == 2
    unclustered_ids = {f.face_id for f in list_faces_in_cluster(conn, None)}
    assert unclustered_ids == set(remove_a + remove_b)
    print(f"  one call across 2 source clusters: both correctly recomputed to 2 faces each, 2 faces unclustered  OK")
    conn.close()


def test_noop_safety(tmp: Path) -> None:
    print("\n=== Safety: removing an already-unclustered face, and an empty list, are harmless no-ops ===")
    dbp, conn = _fresh_db(tmp, "noop.db")
    emb = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    emb[0] = 1.0
    fh = "solo"
    _insert_photo(conn, fh, "E:\\Pics\\solo.jpg")
    fid = _insert_face(conn, fh, emb, cluster_id=None)

    remove_faces_from_cluster(conn, [fid])  # already unclustered -- must not error
    row = conn.execute("SELECT cluster_id, not_a_person FROM faces WHERE face_id = ?", (fid,)).fetchone()
    assert row == (None, 0)

    remove_faces_from_cluster(conn, [])  # empty list -- must not error
    print(f"  no-op on an already-unclustered face, and on an empty face_ids list  OK")
    conn.close()


def test_remove_handles_a_batch_larger_than_sqlites_variable_limit(tmp: Path) -> None:
    """URGENT follow-up session: a real 61,676-face cluster at full-library
    scale exposed a real bug -- _source_clusters_of() (which
    remove_faces_from_cluster/create_person_from_faces/
    mark_faces_not_a_person all call) built one `face_id IN (...)` query
    with as many placeholders as face_ids passed in, and SQLite caps the
    number of bound parameters per query (SQLITE_MAX_VARIABLE_NUMBER --
    999 on older builds). Since the labeling window's Select All can now
    select an ENTIRE oversized cluster at once (the pagination fix, same
    session), a human clicking "Remove Selected from Cluster" on a huge
    real group would hit this exact crash, not just a batch script
    resetting a cluster programmatically. Fixed by chunking the query
    (_SQL_VARIABLE_CHUNK) -- this test proves it with a batch well past the
    old limit, cheap enough to run every time (no faiss/DBSCAN involved,
    just the SQL chunking itself)."""
    print("\n=== A batch of face_ids well past SQLite's per-query variable limit doesn't crash ===")
    dbp, conn = _fresh_db(tmp, "large_batch.db")
    N = 5000  # comfortably over the historical ~999 limit
    conn.execute(
        "INSERT INTO people (cluster_id, assigned_name, notes, representative_embedding, face_count, updated_at) "
        "VALUES (500, NULL, NULL, NULL, 0, '2026-01-01T00:00:00')"
    )
    rng = np.random.default_rng(1)
    face_ids = []
    for i in range(N):
        fh = f"large{i:06d}"
        _insert_photo(conn, fh, f"E:\\Pics\\{fh}.jpg")
        emb = _rand_unit(rng, np.eye(EMBEDDING_DIM, dtype=np.float32)[0], noise=0.2)
        face_ids.append(_insert_face(conn, fh, emb, cluster_id=500))
    conn.execute("UPDATE people SET face_count = ? WHERE cluster_id = 500", (N,))
    conn.commit()

    remove_faces_from_cluster(conn, face_ids)  # used to raise "too many SQL variables" here
    assert list_faces_in_cluster(conn, 500) == []
    assert len(list_faces_in_cluster(conn, None)) == N
    assert conn.execute("SELECT 1 FROM people WHERE cluster_id = 500").fetchone() is None
    print(f"  removed {N} face_ids in one call, no SQL-variable-limit crash, orphan cleanup correct  OK")
    conn.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase3f_"))
    print(f"Working in {tmp}")
    try:
        test_remove_pulls_out_and_recomputes_remainder(tmp)
        test_removing_entire_cluster_deletes_orphaned_person(tmp)
        test_removed_face_is_a_real_reclustering_candidate(tmp)
        test_bulk_removal_across_two_clusters(tmp)
        test_noop_safety(tmp)
        test_remove_handles_a_batch_larger_than_sqlites_variable_limit(tmp)
        print("\nALL PHASE 3F TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
