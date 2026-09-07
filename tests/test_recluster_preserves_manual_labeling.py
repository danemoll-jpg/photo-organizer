"""Dedicated verification: does a FULL cluster-faces re-cluster ever disturb
manual labeling work already done? This is the real question the user asked
before investing real time in labeling their actual library -- Session 2's
own test (test_face_clustering.py::test_incremental_reclustering_preserves_
names) only proved this for the single, narrow case of a NEW face joining an
already-named person's group. It never exercised move_faces_to_person,
merge_people, or (added later, in Phase 3f) remove_faces_from_cluster --
and it never ran a re-cluster where BOTH new faces AND manually-touched
faces were present together, the actual real-world sequence.

This test builds a realistic session of manual labeling work --

  1. Assign/rename people (set_person_name)
  2. Move a face to a different person (move_faces_to_person)
  3. Merge two people (merge_people)
  4. Remove outliers from a cluster (Phase 3f's remove_faces_from_cluster)

-- takes a full byte-for-byte snapshot of every already-clustered face's
cluster_id (and its embedding, untouched by definition since clustering only
ever writes the cluster_id column), THEN adds a batch of brand new faces
(some that should join existing named people, some that should form a
genuinely new person) and runs a single FULL run_face_clustering() pass --
exactly what `main.py cluster-faces` / the dashboard's Face Clustering panel
invoke (cluster_faces_full() is a thin load-then-cluster wrapper around this
same function; loading data/faces.jsonl is separately covered by
test_face_clustering.py and orthogonal to what's being verified here).

Verifies:
  - Every face that was already clustered (cluster_id NOT NULL) before the
    re-cluster -- whether its assignment came from the ORIGINAL auto-cluster
    or from a manual move/merge afterward -- has the IDENTICAL cluster_id
    afterward, and its embedding bytes are unchanged (nothing overwrote it).
  - The re-cluster's own `candidates` count matches exactly the number of
    cluster_id IS NULL faces going in (the newly-added faces plus whatever
    Phase 3f freed) -- proving the algorithm's eligibility set really is
    "unclustered only," not something broader that happened to leave
    already-assigned faces alone by luck.
  - Names/labels survive intact: not reset to NULL, not duplicated onto a
    second people row, and the "merge keeps its own name" rule still holds
    inside this larger end-to-end scenario.
  - A face freed by "Remove Selected from Cluster" is a live, eligible
    candidate again -- confirmed by observing it actually gets reprocessed
    (re-matched or newly clustered), not left behind or double-counted.
  - A subsequent no-op pass (nothing new) doesn't change anything further.

Never touches config.yaml or the real production DB -- an isolated temp DB
throughout, same posture as test_face_clustering.py / test_phase3f.py.

Usage:
    venv\\Scripts\\python tests\\test_recluster_preserves_manual_labeling.py
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
    merge_people,
    move_faces_to_person,
    remove_faces_from_cluster,
    run_face_clustering,
    set_person_name,
    vec_to_blob,
)

_logger = logging.getLogger("recluster_manual_labeling_test")
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


def _snapshot_clustered(conn) -> dict[int, tuple[int, bytes]]:
    """face_id -> (cluster_id, raw embedding bytes) for every currently-
    clustered face -- the exact "must be byte-for-byte unchanged" baseline."""
    rows = conn.execute("SELECT face_id, cluster_id, embedding FROM faces WHERE cluster_id IS NOT NULL").fetchall()
    return {r[0]: (r[1], bytes(r[2])) for r in rows}


def test_full_recluster_preserves_manual_labeling(tmp: Path) -> None:
    print("\n=== FULL re-cluster must never disturb manually-corrected labeling work ===")
    dbp, conn = _fresh_db(tmp, "manual_then_recluster.db")
    rng = np.random.default_rng(99)

    def base():
        v = rng.normal(size=EMBEDDING_DIM).astype(np.float32)
        return v / np.linalg.norm(v)

    base_alice, base_bob, base_carol, base_dave = base(), base(), base(), base()

    # --- Step 0: original auto-clustering, 4 well-separated people --------
    for person, b in [("alice", base_alice), ("bob", base_bob), ("carol", base_carol), ("dave", base_dave)]:
        for j in range(4):
            fh = f"{person}_{j}"
            _insert_photo(conn, fh, f"E:\\Pics\\{person}{j}.jpg")
            _insert_face(conn, fh, _rand_unit(rng, b, 0.02))

    cfg = _make_cfg(dbp)
    stats0 = run_face_clustering(cfg, conn, _logger)
    assert stats0.new_clusters_formed == 4, f"expected 4 clean initial clusters, got {stats0.summary()}"
    clusters0 = {c.sample_file_hash.split("_")[0]: c.cluster_id for c in list_clusters(conn, include_unclustered=False)}
    alice_cid, bob_cid, carol_cid, dave_cid = (
        clusters0["alice"], clusters0["bob"], clusters0["carol"], clusters0["dave"],
    )
    print(f"  initial auto-cluster: alice={alice_cid} bob={bob_cid} carol={carol_cid} dave={dave_cid}")

    # --- Step 1: assign/rename people (labeling) ---------------------------
    set_person_name(conn, alice_cid, "Alice")
    set_person_name(conn, bob_cid, "Bob")
    set_person_name(conn, carol_cid, "Carol Original")
    set_person_name(conn, dave_cid, "Dave")
    print("  named all 4 people")

    # --- Step 2: move a face to a different person --------------------------
    # One of Carol's photos is recognized as actually being Dave; move it.
    carol_faces = sorted(f.face_id for f in list_faces_in_cluster(conn, carol_cid))
    moved_face_id = carol_faces[0]
    move_faces_to_person(conn, [moved_face_id], dave_cid)
    assert conn.execute("SELECT cluster_id FROM faces WHERE face_id = ?", (moved_face_id,)).fetchone()[0] == dave_cid
    print(f"  moved face {moved_face_id} from Carol's cluster into Dave's (cluster_id={dave_cid})")

    # --- Step 3: merge two people --------------------------------------------
    # The rest of "Carol Original" turns out to be the same person as Dave
    # too (a real labeling correction) -- merge Carol into Dave. Dave already
    # has a name, so the merge must NOT overwrite it with Carol's.
    merge_people(conn, keep_cluster_id=dave_cid, other_cluster_id=carol_cid)
    assert conn.execute("SELECT 1 FROM people WHERE cluster_id = ?", (carol_cid,)).fetchone() is None, (
        "merged-away cluster_id's people row must be gone"
    )
    dave_after_merge = next(c for c in list_clusters(conn, include_unclustered=False) if c.cluster_id == dave_cid)
    assert dave_after_merge.assigned_name == "Dave", "merge must not overwrite keep's existing name"
    assert dave_after_merge.face_count == 8, f"expected Dave = 4 original + 1 moved + 3 remaining Carol = 8, got {dave_after_merge.face_count}"
    print(f"  merged Carol's remaining faces into Dave -> {dave_after_merge.face_count} faces, name still 'Dave'")

    # --- Step 4: Phase 3f "Remove Selected from Cluster" ----------------------
    # Two of Bob's faces turn out to be misclustered outliers -- remove them
    # (NOT a Split, NOT a not_a_person flag -- just back to Unclustered).
    bob_faces = sorted(f.face_id for f in list_faces_in_cluster(conn, bob_cid))
    removed_face_ids = bob_faces[:2]
    remove_faces_from_cluster(conn, removed_face_ids)
    for fid in removed_face_ids:
        row = conn.execute("SELECT cluster_id, not_a_person FROM faces WHERE face_id = ?", (fid,)).fetchone()
        assert row == (None, 0), f"removed face {fid} must be (cluster_id=None, not_a_person=0), got {row}"
    bob_after_remove = next(c for c in list_clusters(conn, include_unclustered=False) if c.cluster_id == bob_cid)
    assert bob_after_remove.face_count == 2
    print(f"  removed 2 outlier faces from Bob's cluster -> Bob down to {bob_after_remove.face_count}, now Unclustered")

    # --- Snapshot: every currently-clustered face, byte-for-byte -----------
    snapshot_before = _snapshot_clustered(conn)
    # alice=4, bob=2 (after removal), dave=8 (merged, absorbing carol's faces
    # too -- carol's people row is gone but her faces live on under dave_cid).
    # Total = 4 + 2 + 8 = 14.
    assert len(snapshot_before) == 14, f"expected 14 clustered faces before the full re-cluster, got {len(snapshot_before)}"
    unclustered_before = {
        r[0] for r in conn.execute("SELECT face_id FROM faces WHERE cluster_id IS NULL AND not_a_person = 0").fetchall()
    }
    assert unclustered_before == set(removed_face_ids), (
        f"only the 2 removed faces should be unclustered before new faces are added, got {unclustered_before}"
    )
    print(f"  snapshot taken: {len(snapshot_before)} clustered faces, {len(unclustered_before)} unclustered "
          f"(the 2 just-removed outliers)")

    # --- Step 5: NEW faces arrive (a later face-detection run) ---------------
    # Two more real Alice photos (should fold into her existing cluster);
    # one more real Bob photo (should fold into his now-2-face cluster); and
    # a genuinely brand-new person, Eve, with enough faces to form a cluster.
    new_face_ids = []
    for j in range(2):
        fh = f"alice_new_{j}"
        _insert_photo(conn, fh, f"E:\\Pics\\alice_new{j}.jpg")
        new_face_ids.append(_insert_face(conn, fh, _rand_unit(rng, base_alice, 0.02)))
    fh = "bob_new_0"
    _insert_photo(conn, fh, "E:\\Pics\\bob_new0.jpg")
    new_face_ids.append(_insert_face(conn, fh, _rand_unit(rng, base_bob, 0.02)))
    base_eve = base()
    for j in range(3):
        fh = f"eve_{j}"
        _insert_photo(conn, fh, f"E:\\Pics\\eve{j}.jpg")
        new_face_ids.append(_insert_face(conn, fh, _rand_unit(rng, base_eve, 0.02)))
    print(f"  added {len(new_face_ids)} new faces (2 Alice, 1 Bob, 3 Eve)")

    expected_candidates = len(new_face_ids) + len(removed_face_ids)  # new faces + the freed Bob outliers

    # --- Step 6: the FULL re-cluster ------------------------------------------
    # This is exactly what `main.py cluster-faces` / the dashboard panel run
    # (cluster_faces_full() is just load_faces_from_jsonl + this call).
    stats = run_face_clustering(cfg, conn, _logger)
    print(f"  full re-cluster: {stats.summary()}")

    assert stats.candidates == expected_candidates, (
        f"candidate set must be EXACTLY the unclustered faces (new + freed-by-removal), "
        f"got {stats.candidates}, expected {expected_candidates} -- an already-clustered face "
        f"must never be counted as a candidate"
    )

    # --- The real requirement: every PREVIOUSLY-clustered face is untouched ---
    snapshot_after = _snapshot_clustered(conn)
    for face_id, (cid_before, emb_before) in snapshot_before.items():
        assert face_id in snapshot_after, f"face {face_id} vanished from the clustered set entirely -- data loss"
        cid_after, emb_after = snapshot_after[face_id]
        assert cid_after == cid_before, (
            f"face {face_id}'s cluster_id changed from {cid_before} to {cid_after} during a full re-cluster -- "
            f"a manually-corrected (or original) assignment must NEVER be reassigned by clustering"
        )
        assert emb_after == emb_before, f"face {face_id}'s embedding bytes changed -- clustering must never touch this column"
    print(f"  all {len(snapshot_before)} previously-clustered faces have byte-for-byte identical "
          f"cluster_id and embedding after the full re-cluster  OK")

    # --- Names/labels survive intact, not reset, not duplicated --------------
    people_after = {c.cluster_id: c for c in list_clusters(conn, include_unclustered=False)}
    assert people_after[alice_cid].assigned_name == "Alice"
    assert people_after[bob_cid].assigned_name == "Bob"
    assert people_after[dave_cid].assigned_name == "Dave"
    assert carol_cid not in people_after, "Carol's merged-away cluster_id must not reappear"
    # No duplicate people row for the same identity (exactly one row per cluster_id, enforced by the
    # `people` table's own PRIMARY KEY -- verified behaviorally: querying by cluster_id returns a
    # single named row, not a second "Alice"/"Bob"/"Dave" under some other cluster_id).
    name_counts: dict[str, int] = {}
    for c in people_after.values():
        if c.assigned_name:
            name_counts[c.assigned_name] = name_counts.get(c.assigned_name, 0) + 1
    for name in ("Alice", "Bob", "Dave"):
        assert name_counts.get(name) == 1, f"expected exactly one person named {name!r} after re-cluster, got {name_counts.get(name)}"
    print("  names survived intact: Alice/Bob/Dave present exactly once each, Carol correctly gone (merged)")

    # --- Growth happened where expected, on top of the untouched originals ---
    assert people_after[alice_cid].face_count == 4 + 2, (
        f"Alice should have grown by her 2 new matching faces on top of her original 4 untouched ones, "
        f"got {people_after[alice_cid].face_count}"
    )
    assert people_after[bob_cid].face_count == 2 + 1 + 2, (
        "Bob should have his 2 remaining original faces + the 1 new matching face + the 2 removed "
        f"outliers re-matching back to him (all similar to base_bob), got {people_after[bob_cid].face_count}"
    )
    assert people_after[dave_cid].face_count == 8, "Dave's merged 8 faces must be completely untouched"
    print(f"  Alice={people_after[alice_cid].face_count}, Bob={people_after[bob_cid].face_count} "
          f"(regrew via the freed outliers re-matching + the new photo), Dave={dave_after_merge.face_count} unchanged")

    # --- The freed (removed) faces were genuinely ELIGIBLE and got processed -
    for fid in removed_face_ids:
        row = conn.execute("SELECT cluster_id FROM faces WHERE face_id = ?", (fid,)).fetchone()
        assert row[0] == bob_cid, (
            f"removed-then-freed face {fid} should have re-matched back to Bob's centroid "
            f"(it's still a base_bob-like embedding), got cluster_id={row[0]}"
        )
    print("  the 2 faces freed by 'Remove Selected from Cluster' were correctly re-processed as eligible candidates")

    # --- Eve formed as a genuinely new, separate, unnamed person -------------
    eve_candidates = [c for c in people_after.values() if c.cluster_id not in (alice_cid, bob_cid, dave_cid)]
    assert len(eve_candidates) == 1, f"expected exactly one new person (Eve) formed, got {len(eve_candidates)}"
    eve_cluster = eve_candidates[0]
    assert eve_cluster.face_count == 3
    assert eve_cluster.assigned_name is None, "a brand-new auto-formed cluster must start unnamed"
    print(f"  Eve correctly formed as a genuinely new, separate, unnamed person (cluster_id={eve_cluster.cluster_id})")

    # --- A further no-op pass changes nothing at all --------------------------
    snapshot_before_noop = _snapshot_clustered(conn)
    stats_noop = run_face_clustering(cfg, conn, _logger)
    assert stats_noop.candidates == 0
    snapshot_after_noop = _snapshot_clustered(conn)
    assert snapshot_before_noop == snapshot_after_noop, "a no-op re-cluster pass must not change anything"
    print("  a further no-op re-cluster pass changes nothing  OK")
    conn.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_recluster_manual_labeling_"))
    print(f"Working in {tmp}")
    try:
        test_full_recluster_preserves_manual_labeling(tmp)
        print("\nALL RE-CLUSTER-PRESERVES-MANUAL-LABELING TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
