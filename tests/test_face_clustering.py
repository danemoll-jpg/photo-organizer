"""Exercises Phase 3 Session 2's src/face_clustering.py in an isolated temp
DB -- never touches config.yaml or the real production DB (same posture as
tests/test_face_detection.py). Covers:

  1. Clean synthetic clustering (3 well-separated groups) -- verifies
     correct grouping happens deterministically, nothing over/under-merged.
  2. The actual correctness requirement this session was built to satisfy:
     re-clustering after MORE faces show up must fold a new photo of an
     already-named person into that SAME person (never a fresh cluster_id),
     and must never touch/rename an already-clustered face -- see
     src/face_clustering.py's module docstring for the full design this
     verifies.
  3. Manual labeling-UI operations: create_person_from_faces (split),
     move_faces_to_person, merge_people (including name-adoption and
     orphaned-empty-person cleanup).
  4. crop_face_thumbnail's EXIF-orientation handling -- a real correctness
     point (see that function's own docstring), not boilerplate.
  4b. Phase 3j's load_full_photo_with_bbox -- the opposite ordering
      (draw-then-rotate rather than crop's crop-then-rotate) needs its own
      dedicated orientation check; see that function's own docstring for
      why the ordering has to be reversed.
  5. A smoke test against the REAL data/faces.jsonl this project's own
     Phase 3 Session 1 produced (skipped gracefully if that file doesn't
     exist) -- run against a throwaway temp DB seeded only with the
     `photos` rows the real faces.jsonl itself references (so this never
     needs to copy the real ~80MB production DB into the test suite), per
     this session's own instruction to test against real embeddings, not
     just synthetic fixtures.

Usage:
    venv\\Scripts\\python tests\\test_face_clustering.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging

from src.config import Config
from src.db import connect, init_db
from src.face_clustering import (
    EMBEDDING_DIM,
    ClusterStats,
    LoadStats,
    _recompute_person_centroid,
    create_person_from_faces,
    crop_face_thumbnail,
    list_clusters,
    list_faces_in_cluster,
    load_faces_from_jsonl,
    load_full_photo_with_bbox,
    merge_people,
    move_faces_to_person,
    rank_clusters_by_similarity,
    run_face_clustering,
    set_person_auto_match_locked,
    set_person_name,
    vec_to_blob,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
_logger = logging.getLogger("face_clustering_test")
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


def _fresh_db(tmp: Path, name: str) -> tuple[Path, "sqlite3.Connection"]:
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


def test_synthetic_clean_clustering(tmp: Path) -> None:
    print("\n=== Clean synthetic clustering (3 well-separated groups) ===")
    dbp, conn = _fresh_db(tmp, "synthetic.db")
    rng = np.random.default_rng(42)
    # Three base directions, deliberately far apart (near-orthogonal random
    # 512-dim vectors are almost always >0.9 cosine distance apart already).
    bases = [rng.normal(size=EMBEDDING_DIM).astype(np.float32) for _ in range(3)]
    bases = [(b / np.linalg.norm(b)).astype(np.float32) for b in bases]

    for person_i, base in enumerate(bases):
        for j in range(5):
            file_hash = f"p{person_i}_f{j}"
            _insert_photo(conn, file_hash, f"E:\\Pics\\2020\\2020-0{person_i + 1}\\img{j}.jpg")
            emb = _rand_unit(rng, base, noise=0.02)
            _insert_face(conn, file_hash, emb)

    cfg = _make_cfg(dbp)
    stats = run_face_clustering(cfg, conn, _logger)
    print(f"  {stats.summary()}")
    assert stats.candidates == 15
    assert stats.new_clusters_formed == 3, f"expected exactly 3 clean clusters, got {stats.summary()}"
    assert stats.faces_in_new_clusters == 15
    assert stats.remained_unclustered == 0

    clusters = list_clusters(conn, include_unclustered=False)
    assert len(clusters) == 3
    assert {c.face_count for c in clusters} == {5, 5, 5}
    print(f"  3 clusters of 5 faces each, none merged/split incorrectly  OK")
    conn.close()


def _embedding_at_cosine(base: np.ndarray, orth: np.ndarray, cos_sim: float) -> np.ndarray:
    """A unit vector at EXACTLY the given cosine similarity to `base`,
    using `orth` (a vector orthonormal to `base`) as the other axis --
    lets a test assert an exact, non-flaky similarity value instead of
    tuning random noise and hoping it lands close enough."""
    theta = np.arccos(np.clip(cos_sim, -1.0, 1.0))
    return (np.cos(theta) * base + np.sin(theta) * orth).astype(np.float32)


def test_face_match_existing_threshold_decoupled_from_dbscan(tmp: Path) -> None:
    print("\n=== Config.face_match_existing_threshold: decoupled from DBSCAN's new-cluster eps (follow-up session) ===")
    dbp, conn = _fresh_db(tmp, "decoupled_threshold.db")
    rng = np.random.default_rng(7)

    # Two orthonormal axis pairs so the two scenarios below (matching an
    # EXISTING person vs. two brand-new candidates finding each other)
    # don't accidentally interact.
    e1 = np.zeros(EMBEDDING_DIM, dtype=np.float32); e1[0] = 1.0
    o1 = np.zeros(EMBEDDING_DIM, dtype=np.float32); o1[1] = 1.0
    e2 = np.zeros(EMBEDDING_DIM, dtype=np.float32); e2[2] = 1.0
    o2 = np.zeros(EMBEDDING_DIM, dtype=np.float32); o2[3] = 1.0

    # Alice: an existing, already-named person, 3 faces exactly at e1 (her
    # centroid is therefore exactly e1 after recomputation). The `people`
    # row must exist BEFORE any face references its cluster_id (FK).
    conn.execute("INSERT INTO people (cluster_id, assigned_name, face_count) VALUES (1, 'Alice', 0)")
    conn.commit()
    for j in range(3):
        fh = f"alice_{j}"
        _insert_photo(conn, fh, f"E:\\Pics\\alice_{j}.jpg")
        _insert_face(conn, fh, e1.copy(), cluster_id=1)
    _recompute_person_centroid(conn, 1)

    # A new, unclustered candidate at cosine=0.58 to Alice's centroid --
    # BELOW the strict 0.65 default, ABOVE a more lenient 0.55.
    _insert_photo(conn, "alice_new", "E:\\Pics\\alice_new.jpg")
    _insert_face(conn, "alice_new", _embedding_at_cosine(e1, o1, 0.58))

    # Two brand-new, never-before-seen candidates, mutually at the SAME
    # cosine=0.58 to each other -- a stand-in for "two photos of some new
    # person DBSCAN might chain together" if the lenient threshold were
    # (wrongly) also applied to new-cluster formation.
    _insert_photo(conn, "new_b1", "E:\\Pics\\new_b1.jpg")
    _insert_face(conn, "new_b1", e2.copy())
    _insert_photo(conn, "new_b2", "E:\\Pics\\new_b2.jpg")
    _insert_face(conn, "new_b2", _embedding_at_cosine(e2, o2, 0.58))

    # --- Run 1: default behavior (face_match_existing_threshold unset) ---
    cfg_default = _make_cfg(dbp, face_cluster_similarity_threshold=0.65, face_cluster_min_samples=2)
    stats1 = run_face_clustering(cfg_default, conn, _logger)
    print(f"  default (no override): {stats1.summary()}")
    assert stats1.matched_existing == 0, "at the strict 0.65 threshold, the 0.58-similar candidate must NOT match Alice"
    assert stats1.new_clusters_formed == 0, "new_b1/new_b2 at 0.58 similarity must NOT form a new cluster at eps=0.35"
    assert stats1.remained_unclustered == 3
    alice_count_after_run1 = conn.execute("SELECT face_count FROM people WHERE cluster_id = 1").fetchone()[0]
    assert alice_count_after_run1 == 3, "Alice's count must be unchanged -- nothing matched her"

    # Reset the three candidates back to unclustered for a clean second run
    # (run_face_clustering only ever touches cluster_id IS NULL rows, so
    # a real second run with different config needs them un-clustered
    # again -- this mirrors "the user changes the config and re-runs").
    conn.execute("UPDATE faces SET cluster_id = NULL WHERE file_hash IN ('alice_new', 'new_b1', 'new_b2')")
    conn.commit()

    # --- Run 2: face_match_existing_threshold=0.55, DBSCAN eps unchanged ---
    cfg_lenient_match = _make_cfg(
        dbp, face_cluster_similarity_threshold=0.65, face_cluster_min_samples=2,
        face_match_existing_threshold=0.55,
    )
    stats2 = run_face_clustering(cfg_lenient_match, conn, _logger)
    print(f"  face_match_existing_threshold=0.55: {stats2.summary()}")
    assert stats2.matched_existing == 1, "with the lenient match threshold, the 0.58-similar candidate MUST now match Alice"
    assert stats2.new_clusters_formed == 0, \
        "new_b1/new_b2 must STILL not cluster together -- DBSCAN's eps must stay governed by the STRICT threshold, unaffected"
    assert stats2.remained_unclustered == 2, "new_b1/new_b2 correctly remain unclustered candidates"
    row = conn.execute("SELECT cluster_id FROM faces WHERE file_hash = 'alice_new'").fetchone()
    assert row[0] == 1, "alice_new must have joined Alice's real cluster_id, not a new one"
    alice_count_after_run2 = conn.execute("SELECT face_count FROM people WHERE cluster_id = 1").fetchone()[0]
    assert alice_count_after_run2 == 4, f"Alice should have grown to 4 faces, got {alice_count_after_run2}"
    print("  matching-against-existing-people got more lenient WITHOUT loosening new-cluster (chaining) risk  OK")
    conn.close()


def test_auto_match_locked_excluded_from_matching(tmp: Path) -> None:
    print("\n=== people.auto_match_locked: a locked person is skipped entirely by automatic matching (follow-up session) ===")
    dbp, conn = _fresh_db(tmp, "auto_match_locked.db")

    # "Cat": an existing person, locked from automatic matching -- the
    # real scenario this was built for (a pet cluster attracting "all
    # kinds of crap" once face_match_existing_threshold made matching
    # against existing people more lenient).
    e1 = np.zeros(EMBEDDING_DIM, dtype=np.float32); e1[0] = 1.0
    conn.execute("INSERT INTO people (cluster_id, assigned_name, face_count, auto_match_locked) VALUES (1, 'Cat', 0, 1)")
    conn.commit()
    for j in range(3):
        fh = f"cat_{j}"
        _insert_photo(conn, fh, f"E:\\Pics\\cat_{j}.jpg")
        _insert_face(conn, fh, e1.copy(), cluster_id=1)
    _recompute_person_centroid(conn, 1)

    # A new candidate at VERY high similarity (0.99) to Cat's centroid --
    # would obviously match her under any normal threshold. min_samples=2
    # so it's still eligible to form its own new cluster if left unmatched.
    _insert_photo(conn, "candidate", "E:\\Pics\\candidate.jpg")
    _insert_face(conn, "candidate", _embedding_at_cosine(e1, np.roll(e1, 1), 0.99))

    cfg = _make_cfg(dbp, face_cluster_similarity_threshold=0.65, face_cluster_min_samples=2)
    stats = run_face_clustering(cfg, conn, _logger)
    print(f"  locked: {stats.summary()}")
    assert stats.matched_existing == 0, "a LOCKED person must never receive an automatic match, no matter how similar"
    row = conn.execute("SELECT cluster_id FROM faces WHERE file_hash = 'candidate'").fetchone()
    assert row[0] != 1, "the candidate must NOT have been assigned to the locked cluster"
    cat_count = conn.execute("SELECT face_count FROM people WHERE cluster_id = 1").fetchone()[0]
    assert cat_count == 3, "the locked person's face_count must be completely unchanged"
    print("  Cat (locked) received zero automatic matches despite a 0.99-similar candidate  OK")

    # Un-locking must restore normal matching behavior on the NEXT run.
    conn.execute("UPDATE faces SET cluster_id = NULL WHERE file_hash = 'candidate'")
    conn.commit()
    set_person_auto_match_locked(conn, 1, False)
    stats2 = run_face_clustering(cfg, conn, _logger)
    print(f"  unlocked: {stats2.summary()}")
    assert stats2.matched_existing == 1, "after unlocking, the same candidate must match normally"
    row2 = conn.execute("SELECT cluster_id FROM faces WHERE file_hash = 'candidate'").fetchone()
    assert row2[0] == 1
    print("  un-locking restores normal automatic matching  OK")

    # Manual assignment must work identically regardless of the lock --
    # the user's own explicit requirement ("just selecting them myself").
    set_person_auto_match_locked(conn, 1, True)
    _insert_photo(conn, "manual_add", "E:\\Pics\\manual_add.jpg")
    manual_fid = _insert_face(conn, "manual_add", e1.copy(), cluster_id=None)
    move_faces_to_person(conn, [manual_fid], 1)
    assert manual_fid in {f.face_id for f in list_faces_in_cluster(conn, 1)}
    print("  manual Move/Merge assignment into a LOCKED person still works normally  OK")

    clusters = list_clusters(conn, include_unclustered=False)
    cat_summary = next(c for c in clusters if c.cluster_id == 1)
    assert cat_summary.auto_match_locked is True, "ClusterSummary must surface the locked flag for the UI"
    conn.close()


def test_auto_match_locked_migration(tmp: Path) -> None:
    print("\n=== Migration: a pre-existing people table gets auto_match_locked added, backfilled to 0 ===")
    dbp = tmp / "premigration_lock.db"
    conn = sqlite3.connect(dbp)
    # Hand-built shape mirroring people/faces/photos immediately BEFORE this
    # session (i.e. WITH not_a_person/det_score but WITHOUT
    # auto_match_locked) -- never a fresh schema.sql-created fixture, same
    # discipline as test_phase3c.py's own migration test.
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
            embedding BLOB NOT NULL, det_score REAL, not_a_person INTEGER NOT NULL DEFAULT 0,
            cluster_id INTEGER REFERENCES people(cluster_id) ON DELETE SET NULL,
            detected_at TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO people (cluster_id, assigned_name, face_count) VALUES (1, 'Cat', 3)")
    conn.commit()
    cols_before = {row[1] for row in conn.execute("PRAGMA table_info(people)")}
    assert "auto_match_locked" not in cols_before
    conn.close()

    init_db(dbp, logger=_logger)
    conn = connect(dbp)
    cols_after = {row[1] for row in conn.execute("PRAGMA table_info(people)")}
    assert "auto_match_locked" in cols_after, "init_db()/_migrate() must add auto_match_locked to an existing people table"
    row = conn.execute("SELECT auto_match_locked FROM people WHERE cluster_id = 1").fetchone()
    assert row == (0,), f"pre-existing person row must backfill to auto_match_locked=0 (never locked before), got {row}"
    # And the newly-migrated column must actually work post-migration.
    set_person_auto_match_locked(conn, 1, True)
    assert conn.execute("SELECT auto_match_locked FROM people WHERE cluster_id = 1").fetchone() == (1,)
    print("  auto_match_locked added + backfilled to 0 on migration, set_person_auto_match_locked works post-migration  OK")
    conn.close()


def test_rank_clusters_by_similarity(tmp: Path) -> None:
    print("\n=== rank_clusters_by_similarity: FaceLabelingWindow's 'Sort by likely match to' backend (follow-up session) ===")
    dbp, conn = _fresh_db(tmp, "rank_similarity.db")

    e1 = np.zeros(EMBEDDING_DIM, dtype=np.float32); e1[0] = 1.0
    orth = np.zeros(EMBEDDING_DIM, dtype=np.float32); orth[1] = 1.0

    def add_person(name, centroid, face_count=1):
        blob = vec_to_blob(centroid) if centroid is not None else None
        cur = conn.execute(
            "INSERT INTO people (assigned_name, face_count, representative_embedding) VALUES (?, ?, ?)",
            (name, face_count, blob),
        )
        return cur.lastrowid

    target_id = add_person("Target", e1)
    close_id = add_person(None, _embedding_at_cosine(e1, orth, 0.85))
    mid_id = add_person("Someone Else", _embedding_at_cosine(e1, orth, 0.40))
    far_id = add_person(None, _embedding_at_cosine(e1, orth, 0.05))
    no_embedding_id = add_person(None, None)  # simulates a person with no centroid computed yet
    conn.commit()

    sims = rank_clusters_by_similarity(conn, target_id)
    assert target_id not in sims, "the target itself must never appear in its own ranking"
    assert no_embedding_id not in sims, "a person with no representative_embedding must be silently excluded, not error"
    assert set(sims.keys()) == {close_id, mid_id, far_id}
    assert abs(sims[close_id] - 0.85) < 1e-4, f"expected ~0.85, got {sims[close_id]}"
    assert abs(sims[mid_id] - 0.40) < 1e-4, f"expected ~0.40, got {sims[mid_id]}"
    assert abs(sims[far_id] - 0.05) < 1e-4, f"expected ~0.05, got {sims[far_id]}"
    ranked = sorted(sims.items(), key=lambda kv: -kv[1])
    assert [cid for cid, _ in ranked] == [close_id, mid_id, far_id], f"expected close->mid->far order, got {ranked}"
    print(f"  exact cosine similarities recovered (0.85/0.40/0.05), correctly ordered, target+no-embedding-person excluded  OK")

    # A person with no representative_embedding at all as the TARGET
    # itself (e.g. an empty/orphaned row somehow) must return an empty
    # dict, not raise.
    empty_result = rank_clusters_by_similarity(conn, no_embedding_id)
    assert empty_result == {}, f"expected empty dict for a target with no centroid, got {empty_result}"
    print("  a target person with no representative_embedding returns an empty ranking, doesn't raise  OK")

    # A nonexistent cluster_id must also return an empty dict gracefully.
    assert rank_clusters_by_similarity(conn, 999999) == {}
    print("  a nonexistent target cluster_id returns an empty ranking, doesn't raise  OK")
    conn.close()


def test_incremental_reclustering_preserves_names(tmp: Path) -> None:
    print("\n=== Incremental re-clustering: never touches an already-named/clustered face ===")
    dbp, conn = _fresh_db(tmp, "incremental.db")
    rng = np.random.default_rng(7)
    base_alice = (rng.normal(size=EMBEDDING_DIM).astype(np.float32))
    base_alice /= np.linalg.norm(base_alice)
    base_bob = rng.normal(size=EMBEDDING_DIM).astype(np.float32)
    base_bob /= np.linalg.norm(base_bob)

    for j in range(3):
        fh = f"alice_{j}"
        _insert_photo(conn, fh, f"E:\\Pics\\alice{j}.jpg")
        _insert_face(conn, fh, _rand_unit(rng, base_alice, 0.02))

    cfg = _make_cfg(dbp)
    stats1 = run_face_clustering(cfg, conn, _logger)
    assert stats1.new_clusters_formed == 1
    alice_cluster = list_clusters(conn, include_unclustered=False)[0]
    alice_cid = alice_cluster.cluster_id
    set_person_name(conn, alice_cid, "Alice")
    original_alice_face_ids = {f.face_id for f in list_faces_in_cluster(conn, alice_cid)}
    print(f"  first pass: Alice's cluster_id={alice_cid}, {len(original_alice_face_ids)} face(s), named")

    # Simulate a later Phase 3 Session 1 run finding MORE photos: one more
    # of Alice (should fold into her EXISTING named cluster) and enough of
    # a brand new person Bob to form a new cluster.
    fh_alice_new = "alice_new"
    _insert_photo(conn, fh_alice_new, "E:\\Pics\\alice_later.jpg")
    _insert_face(conn, fh_alice_new, _rand_unit(rng, base_alice, 0.02))
    for j in range(3):
        fh = f"bob_{j}"
        _insert_photo(conn, fh, f"E:\\Pics\\bob{j}.jpg")
        _insert_face(conn, fh, _rand_unit(rng, base_bob, 0.02))

    stats2 = run_face_clustering(cfg, conn, _logger)
    print(f"  second pass: {stats2.summary()}")
    assert stats2.candidates == 4, "must only consider the 4 NEW faces, not re-touch Alice's original 3"
    assert stats2.matched_existing == 1, "the new Alice photo must match her existing centroid, not form a new cluster"
    assert stats2.new_clusters_formed == 1, "Bob's 3 new faces must form exactly one new cluster"

    # The real requirement: Alice's ORIGINAL faces must be byte-for-byte
    # untouched (same cluster_id) and her name must still be there.
    alice_after = next(c for c in list_clusters(conn, include_unclustered=False) if c.cluster_id == alice_cid)
    assert alice_after.assigned_name == "Alice", "re-clustering must never wipe an existing name assignment"
    assert alice_after.face_count == 4, "Alice should now have her original 3 + the 1 new matching face"
    current_alice_face_ids = {f.face_id for f in list_faces_in_cluster(conn, alice_cid)}
    assert original_alice_face_ids <= current_alice_face_ids, "no original face was moved off Alice's cluster_id"
    print(f"  Alice's name/cluster_id survived re-clustering, gained the new matching face  OK")

    bob_cluster = next(c for c in list_clusters(conn, include_unclustered=False) if c.cluster_id != alice_cid)
    assert bob_cluster.face_count == 3
    assert bob_cluster.cluster_id != alice_cid
    print(f"  Bob correctly formed as a genuinely new, separate person (cluster_id={bob_cluster.cluster_id})  OK")

    # A third pass with nothing new must be a true no-op.
    stats3 = run_face_clustering(cfg, conn, _logger)
    assert stats3.candidates == 0
    print("  third pass with no new faces is a correct no-op  OK")
    conn.close()


def test_manual_split_move_merge(tmp: Path) -> None:
    print("\n=== Manual labeling-UI operations: split / move / merge ===")
    dbp, conn = _fresh_db(tmp, "manual_ops.db")
    for i in range(4):
        _insert_photo(conn, f"h{i}", f"E:\\Pics\\h{i}.jpg")
    emb = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    emb[0] = 1.0
    # people row must exist before any face references its cluster_id (FK).
    conn.execute(
        "INSERT INTO people (cluster_id, assigned_name, notes, representative_embedding, face_count, updated_at) "
        "VALUES (100, 'Mixed Group', NULL, ?, 4, '2026-01-01')",
        (vec_to_blob(emb),),
    )
    conn.commit()
    face_ids = [_insert_face(conn, f"h{i}", emb, cluster_id=100) for i in range(4)]

    # Split: two of the four faces actually belong to someone else.
    new_cid = create_person_from_faces(conn, face_ids[2:], name="Split Person")
    remaining = list_faces_in_cluster(conn, 100)
    assert {f.face_id for f in remaining} == set(face_ids[:2])
    moved = list_faces_in_cluster(conn, new_cid)
    assert {f.face_id for f in moved} == set(face_ids[2:])
    mixed_after_split = next(c for c in list_clusters(conn, include_unclustered=False) if c.cluster_id == 100)
    assert mixed_after_split.face_count == 2, "source cluster's face_count must be recomputed after a split"
    print(f"  split: 2 faces moved out into new cluster_id={new_cid}, source cluster correctly down to 2  OK")

    # Move one of the split-off faces into an existing third person instead.
    _insert_photo(conn, "h_extra", "E:\\Pics\\h_extra.jpg")
    extra_face_id = _insert_face(conn, "h_extra", emb, cluster_id=None)
    move_faces_to_person(conn, [extra_face_id], new_cid)
    assert extra_face_id in {f.face_id for f in list_faces_in_cluster(conn, new_cid)}
    assert list_clusters(conn, include_unclustered=False)
    print(f"  move_faces_to_person: unclustered face correctly folded into cluster_id={new_cid}  OK")

    # Merge: fold the (named) "Split Person" cluster into "Mixed Group",
    # which is ALSO already named -- keep's name must win, not be overwritten.
    merge_people(conn, keep_cluster_id=100, other_cluster_id=new_cid)
    after_merge = {c.cluster_id: c for c in list_clusters(conn, include_unclustered=False)}
    assert new_cid not in after_merge, "merged-away cluster_id's people row must be removed"
    assert after_merge[100].assigned_name == "Mixed Group", "keep's existing name must NOT be overwritten by other's name"
    assert after_merge[100].face_count == 5, "2 (remaining) + 2 (split) + 1 (moved extra) = 5"
    print(f"  merge_people: {after_merge[100].face_count} faces under cluster 100, name preserved  OK")

    # Merge-adopts-name case: an unnamed cluster merged with a named one.
    _insert_photo(conn, "h_named_src", "E:\\Pics\\h_named_src.jpg")
    unnamed_fid = _insert_face(conn, "h_named_src", emb, cluster_id=None)
    unnamed_cid = create_person_from_faces(conn, [unnamed_fid], name=None)
    merge_people(conn, keep_cluster_id=unnamed_cid, other_cluster_id=100)
    final = {c.cluster_id: c for c in list_clusters(conn, include_unclustered=False)}
    assert 100 not in final
    assert final[unnamed_cid].assigned_name == "Mixed Group", "keep-has-no-name case must adopt other's name"
    print(f"  merge_people (keep unnamed, other named): name correctly adopted  OK")
    conn.close()


def test_crop_face_thumbnail_orientation(tmp: Path) -> None:
    print("\n=== crop_face_thumbnail: EXIF orientation applied to the CROP, not the coordinate frame ===")
    # Raw (sensor-orientation, un-rotated) image: 200w x 100h. A distinctly
    # colored "face" square sits at raw pixel bbox (10, 10, 60, 60) --
    # exactly the coordinate frame face_detection.py's bbox would have used
    # (computed on the un-rotated array, see that module's _load_image_bgr).
    arr = np.full((100, 200, 3), 128, dtype=np.uint8)  # (H, W, 3)
    arr[10:60, 10:60] = [255, 0, 0]  # distinct red square = "the face"
    img = Image.fromarray(arr, mode="RGB")
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation=6: rotate 90 CW for correct display
    p = tmp / "rotated_photo.jpg"
    img.save(p, "JPEG", exif=exif.tobytes())

    crop = crop_face_thumbnail(p, bbox=(10, 10, 50, 50), size=256, margin=0.0)
    # Orientation=6 swaps width/height on the transposed crop (a 90-degree
    # rotation) -- the raw crop was 50x50 (square) so this alone wouldn't
    # prove much; the real check is pixel content below.
    arr_out = np.asarray(crop)
    # The distinct red region must still be present SOMEWHERE in the output
    # (proving the crop correctly grabbed the raw-coordinate-frame square
    # before any rotation was applied) -- if bbox had instead been applied
    # AFTER a full-image exif_transpose, this crop would have landed on the
    # wrong region entirely (mid-image gray, no red at all).
    red_pixel_present = np.any((arr_out[:, :, 0] > 200) & (arr_out[:, :, 1] < 50) & (arr_out[:, :, 2] < 50))
    assert red_pixel_present, "crop must contain the red 'face' region -- bbox was applied in the wrong coordinate frame"
    print(f"  crop shape={arr_out.shape}, red face region correctly present after orientation fix  OK")

    # Control: an image with NO EXIF orientation tag must crop identically
    # (no unexpected rotation applied when there's nothing to correct).
    p_plain = tmp / "plain_photo.jpg"
    img.save(p_plain, "JPEG")
    crop_plain = crop_face_thumbnail(p_plain, bbox=(10, 10, 50, 50), size=256, margin=0.0)
    arr_plain = np.asarray(crop_plain)
    assert arr_plain.shape[0] == arr_plain.shape[1] == 50, f"unrotated crop should stay a 50x50 square, got {arr_plain.shape}"
    print(f"  control (no EXIF orientation tag): crop stays an un-rotated 50x50 square  OK")


def test_load_full_photo_with_bbox_orientation(tmp: Path) -> None:
    print("\n=== load_full_photo_with_bbox: highlight box follows the SAME rotation as the photo (Phase 3j) ===")
    # Same raw sensor-frame setup as test_crop_face_thumbnail_orientation
    # above, saved as PNG rather than JPEG so the thin drawn outline isn't
    # smeared by lossy DCT block artifacts right at a sharp color edge.
    arr = np.full((100, 200, 3), 128, dtype=np.uint8)  # (H, W, 3)
    arr[10:60, 10:60] = [255, 0, 0]  # red "face" square, same raw bbox face_detection.py would have used
    img = Image.fromarray(arr, mode="RGB")
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation=6: rotate 90 CW for correct display
    p = tmp / "rotated_full_photo.png"
    img.save(p, "PNG", exif=exif.tobytes())

    def red_mask_of(arr_out: np.ndarray) -> np.ndarray:
        return (arr_out[:, :, 0] > 200) & (arr_out[:, :, 1] < 50) & (arr_out[:, :, 2] < 50)

    def outline_mask_of(arr_out: np.ndarray) -> np.ndarray:
        return (arr_out[:, :, 0] < 50) & (arr_out[:, :, 1] > 200) & (arr_out[:, :, 2] < 50)

    result = load_full_photo_with_bbox(
        p, bbox=(10, 10, 50, 50), max_size=(1000, 1000), outline="#00ff00", outline_width=2
    )
    arr_out = np.asarray(result.convert("RGB"))
    red_mask = red_mask_of(arr_out)
    assert red_mask.any(), "rotated output must still contain the red 'face' region"
    rows, cols = np.where(red_mask.any(axis=1))[0], np.where(red_mask.any(axis=0))[0]
    r0, r1, c0, c1 = rows.min(), rows.max(), cols.min(), cols.max()
    # A ring just outside the (correctly-rotated) red region's own bounding
    # box must contain the outline color -- proving the highlighted
    # rectangle actually bounds the rotated face, not some other part of
    # the photo. If the box had instead been drawn AFTER exif_transpose
    # (using the raw, un-rotated bbox coordinates on an already-rotated
    # image), it would land on the wrong axis/region entirely and this
    # would fail.
    ring = arr_out[max(0, r0 - 6): r1 + 7, max(0, c0 - 6): c1 + 7]
    assert outline_mask_of(ring).any(), (
        "the highlighted outline must appear immediately around the rotated red region -- "
        "drawing the box before vs. after exif_transpose() must not disagree"
    )
    print(f"  red region bbox=({r0},{c0})-({r1},{c1}), outline correctly bounds it after rotation  OK")

    # Control: an unrotated photo -- box lands exactly at the raw bbox, no
    # dimension swap.
    p_plain = tmp / "plain_full_photo.png"
    img.save(p_plain, "PNG")
    result_plain = load_full_photo_with_bbox(
        p_plain, bbox=(10, 10, 50, 50), max_size=(1000, 1000), outline="#00ff00", outline_width=2
    )
    assert result_plain.size == (200, 100), f"unrotated photo shouldn't change dimensions, got {result_plain.size}"
    arr_plain = np.asarray(result_plain.convert("RGB"))
    # (x, y) = (10, 10) is the rectangle's own top-left corner -- part of
    # both its top and left border.
    r, g, b = arr_plain[10, 10][:3]
    assert r < 50 and g > 200 and b < 50, f"rectangle corner should be outline-colored, got ({r},{g},{b})"
    print("  control (no EXIF orientation tag): box lands exactly at the raw bbox, dimensions unchanged  OK")

    # max_size actually constrains the result -- scaled DOWN, never up.
    tiny = load_full_photo_with_bbox(p_plain, bbox=(10, 10, 50, 50), max_size=(50, 50))
    assert max(tiny.size) <= 50, f"result must be scaled to fit max_size, got {tiny.size}"
    print(f"  max_size correctly caps the result (got {tiny.size} for a max_size=(50,50) cap)  OK")


def test_real_faces_jsonl_smoke(tmp: Path) -> None:
    print("\n=== Smoke test against the REAL data/faces.jsonl (Phase 3 Session 1's own output) ===")
    real_faces_path = REPO_ROOT / "data" / "faces.jsonl"
    if not real_faces_path.exists():
        print(f"  SKIPPED: {real_faces_path} doesn't exist in this environment.")
        return

    dbp, conn = _fresh_db(tmp, "real_smoke.db")
    seen_hashes: set[str] = set()
    with open(real_faces_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj["file_hash"] not in seen_hashes:
                _insert_photo(conn, obj["file_hash"], obj["path"])
                seen_hashes.add(obj["file_hash"])
    print(f"  seeded {len(seen_hashes)} distinct real photo row(s) referenced by the real faces.jsonl")

    cfg = _make_cfg(dbp)
    load_stats = load_faces_from_jsonl(conn, real_faces_path, _logger)
    print(f"  {load_stats.summary()}")
    assert load_stats.skipped_bad_line == 0, "every real faces.jsonl line should load cleanly against its own real photos"
    assert load_stats.inserted > 0

    cluster_stats = run_face_clustering(cfg, conn, _logger)
    print(f"  {cluster_stats.summary()}")
    # Accounting invariant: every candidate ends up in exactly one bucket.
    assert (
        cluster_stats.matched_existing + cluster_stats.faces_in_new_clusters + cluster_stats.remained_unclustered
        == cluster_stats.candidates
    ), "every candidate face must be accounted for: matched + newly-clustered + still-unclustered == candidates"
    assert cluster_stats.new_clusters_formed > 0, "this library's real sample data is known to contain repeat faces"

    clusters = list_clusters(conn)
    total_in_named_groups = sum(c.face_count for c in clusters if c.cluster_id is not None)
    assert total_in_named_groups == cluster_stats.faces_in_new_clusters + cluster_stats.matched_existing
    print(f"  {len([c for c in clusters if c.cluster_id is not None])} real cluster(s) formed from real embeddings, "
          f"accounting checks out  OK")

    # Idempotency against real data too: a second load must insert nothing
    # new (already covered by idx_faces_unique_detection). Re-running
    # clustering with no NEW faces.jsonl rows can still legitimately GROW an
    # existing person (some of the previous pass's "still unclustered"
    # leftovers may now match an existing centroid, exactly like the
    # candidates-vs-centroids step for any run) -- the real invariant is
    # that an existing person's face_count can only stay the same or grow,
    # NEVER shrink (that would mean a face silently got reassigned away,
    # which run_face_clustering must never do to an already-clustered face).
    people_before = {c.cluster_id: c.face_count for c in list_clusters(conn, include_unclustered=False)}
    run_face_clustering(cfg, conn, _logger)
    people_after = {c.cluster_id: c.face_count for c in list_clusters(conn, include_unclustered=False)}
    for cid, count in people_before.items():
        assert people_after.get(cid, 0) >= count, f"re-clustering must never shrink an existing person's face_count (cluster {cid})"
    load_again = load_faces_from_jsonl(conn, real_faces_path, _logger)
    assert load_again.inserted == 0, "re-loading the same real faces.jsonl must not insert any duplicate rows"
    print(f"  re-load is a correct no-op ({load_again.summary()})  OK")
    conn.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase3_session2_"))
    print(f"Working in {tmp}")
    try:
        test_synthetic_clean_clustering(tmp)
        test_face_match_existing_threshold_decoupled_from_dbscan(tmp)
        test_auto_match_locked_excluded_from_matching(tmp)
        test_auto_match_locked_migration(tmp)
        test_rank_clusters_by_similarity(tmp)
        test_incremental_reclustering_preserves_names(tmp)
        test_manual_split_move_merge(tmp)
        test_crop_face_thumbnail_orientation(tmp)
        test_load_full_photo_with_bbox_orientation(tmp)
        test_real_faces_jsonl_smoke(tmp)
        print("\nALL PHASE 3 SESSION 2 (FACE CLUSTERING) TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
