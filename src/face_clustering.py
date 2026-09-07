"""Phase 3 Session 2: face clustering. Reads data/faces.jsonl (Session 1's
output, one line per DETECTED face) into the `faces`/`people` DB tables
(schema.sql, additive via init_db()/_migrate() per rule 12 -- both tables
have existed since Phase 0, empty until this module writes to them for the
first time), clusters embeddings by cosine similarity, and exposes the
merge/split/assign-name operations the dashboard's Face Labeling window
uses. Pure numerical work -- no `insightface` dependency here (see
CLAUDE.md rule 13 / photo-organizer-spec.md's Phase 3 section), just
`faiss`+`scikit-learn` (both verified to have official cp314-win_amd64
wheels on PyPI before adding, same caution as every other dependency in
this project -- see requirements.txt).

Photos only (faces.jsonl already excludes video, see face_detection.py).

--- Re-clustering design: the real correctness requirement -------------

The task this module exists to solve isn't just "cluster the embeddings
once" -- it's "cluster them again later, after more photos have been face-
detected, WITHOUT undoing any name/merge/split work the user already did in
the labeling UI." A naive from-scratch re-cluster (re-run DBSCAN over every
embedding every time) can't promise that: DBSCAN's own cluster *labels* are
arbitrary integers assigned by iteration order, unrelated across two runs
even over the exact same data, so there is no way to know "new cluster 3"
is the same person as "old cluster 7" without comparing them by content --
and worse, re-running from scratch would also silently undo any manual
split/merge, since DBSCAN would just re-group by raw similarity again.

The design actually used here is INCREMENTAL, not a from-scratch re-cluster:

1. `people.cluster_id` is a STABLE PERSON IDENTITY, allocated once and never
   reused or renumbered -- not a transient per-run DBSCAN label. Once a
   photo's face is assigned a cluster_id, it keeps that cluster_id forever
   unless a human explicitly moves it (merge_people/move_faces_to_person/
   create_person_from_faces below) -- clustering itself NEVER reassigns an
   already-clustered face's cluster_id. This alone guarantees a name
   assigned to a person, and any manual merge/split already done, can never
   be silently undone by running clustering again -- run_face_clustering()
   only ever touches faces where cluster_id IS NULL.
2. Each person keeps a `representative_embedding` (people.representative_
   embedding) -- a running centroid, recomputed exactly from the mean of
   every face CURRENTLY assigned to that cluster_id (_recompute_person_
   centroid, called after every mutation) rather than an incremental
   weighted-average approximation, so it can never drift out of sync with
   what's actually in the DB.
3. On each clustering run, every not-yet-clustered face is first compared
   against every EXISTING person's centroid (cosine similarity). A match
   above face_cluster_similarity_threshold reuses that person's cluster_id
   -- this is how a newly-detected photo of an already-named person gets
   folded into their existing group instead of forming a disconnected
   duplicate cluster.
4. Only the faces that don't match anyone existing get DBSCAN'd among
   themselves (cosine distance, via a faiss neighbor graph -- see
   _dbscan_cosine below) to discover BRAND NEW people the library hasn't
   seen labeled before. Each resulting cluster gets a freshly allocated
   cluster_id (max existing + 1, see _next_cluster_id) and an unnamed
   `people` row.
5. Noise points (DBSCAN label -1, or simply too few remaining faces to
   reach min_samples) stay cluster_id NULL -- a "person" seen in only one
   photo can't form a cluster with itself under the default min_samples=2.
   These are surfaced in the labeling UI as an "(Unclustered)" pseudo-
   group (list_clusters(cluster_id=None)) rather than silently hidden, so
   the user can still manually promote one into a new person
   (create_person_from_faces) or fold it into an existing one
   (move_faces_to_person) by hand.

Net effect: calling run_face_clustering() again after Session 1 detects
more photos is a pure INCREMENT -- every already-clustered face (named or
not, merged or split by hand or not) is left completely untouched; only
the new faces get processed, either joining an existing person or seeding
a new one. This is also why it's safe/cheap to call idempotently with
nothing new to do (returns immediately once the "unclustered, above
confidence" query comes back empty).

Kids' faces changing a lot over 25 years is an accepted, known limitation
per the spec (a toddler and teen photo of the same person will not
auto-cluster) -- not something this design tries to solve; a human
merging those two clusters by hand once labeled is the intended fix path.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageOps

from .config import Config

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None


EMBEDDING_DIM = 512


# --------------------------------------------------------------- helpers

def vec_to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return vec / norm


def _now_iso() -> str:
    return datetime.now().isoformat()


def _in_clause(n: int) -> str:
    return ",".join("?" * n)


# ------------------------------------------------------- loading faces.jsonl

@dataclass
class LoadStats:
    read: int = 0
    inserted: int = 0
    skipped_duplicate: int = 0
    skipped_bad_line: int = 0

    def summary(self) -> str:
        return (
            f"read={self.read} inserted={self.inserted} "
            f"skipped_duplicate={self.skipped_duplicate} skipped_bad_line={self.skipped_bad_line}"
        )


def load_faces_from_jsonl(conn: sqlite3.Connection, faces_path: Path, logger: logging.Logger) -> LoadStats:
    """Idempotent: safe to call repeatedly as face_detection.py appends more
    lines to faces.jsonl over time (e.g. a resumed Session 1 run against
    newly-organized photos). Uses INSERT OR IGNORE against
    idx_faces_unique_detection (file_hash, bbox_x, bbox_y, bbox_w, bbox_h)
    -- a face's bbox is deterministic for an unchanged photo, so this is a
    stable natural key that never double-inserts an already-loaded face."""
    stats = LoadStats()
    if not faces_path.exists():
        logger.info(f"No faces file at {faces_path} — nothing to load.")
        return stats

    with open(faces_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            stats.read += 1
            try:
                obj = json.loads(line)
                x1, y1, x2, y2 = obj["bbox"]
                embedding = np.asarray(obj["embedding"], dtype=np.float32)
                if embedding.shape != (EMBEDDING_DIM,):
                    raise ValueError(f"expected a {EMBEDDING_DIM}-dim embedding, got shape {embedding.shape}")
                bbox_x, bbox_y = int(round(x1)), int(round(y1))
                bbox_w, bbox_h = int(round(x2 - x1)), int(round(y2 - y1))
                cur = conn.execute(
                    "INSERT OR IGNORE INTO faces "
                    "(file_hash, bbox_x, bbox_y, bbox_w, bbox_h, embedding, det_score, cluster_id, detected_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    (
                        obj["file_hash"], bbox_x, bbox_y, bbox_w, bbox_h,
                        vec_to_blob(embedding), obj.get("det_score"),
                        obj.get("detected_at") or _now_iso(),
                    ),
                )
                if cur.rowcount:
                    stats.inserted += 1
                else:
                    stats.skipped_duplicate += 1
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
                stats.skipped_bad_line += 1
                logger.warning(f"Skipping bad faces.jsonl line: {e!r}")
            except sqlite3.IntegrityError as e:
                # file_hash with no matching photos row (FK) -- shouldn't
                # happen in practice (faces.jsonl is only ever written for
                # rows read straight from `photos`), but don't let one bad
                # record kill the whole load.
                stats.skipped_bad_line += 1
                logger.warning(f"Skipping faces.jsonl line with no matching photos row: {e!r}")
    conn.commit()
    logger.info(f"Loaded faces.jsonl into DB: {stats.summary()}")
    return stats


# --------------------------------------------------------------- clustering

@dataclass
class ClusterStats:
    candidates: int = 0                 # unclustered faces considered this run (above the confidence floor)
    excluded_low_confidence: int = 0    # unclustered faces below face_min_det_score -- left unclustered, not counted as candidates
    matched_existing: int = 0           # folded into an already-named/unnamed existing person
    new_clusters_formed: int = 0
    faces_in_new_clusters: int = 0
    remained_unclustered: int = 0       # DBSCAN noise, or too few candidates to reach min_samples
    people_total: int = 0

    def summary(self) -> str:
        return (
            f"candidates={self.candidates} excluded_low_confidence={self.excluded_low_confidence} "
            f"matched_existing={self.matched_existing} new_clusters_formed={self.new_clusters_formed} "
            f"faces_in_new_clusters={self.faces_in_new_clusters} "
            f"remained_unclustered={self.remained_unclustered} people_total={self.people_total}"
        )


def _next_cluster_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(cluster_id), 0) FROM people").fetchone()
    return row[0] + 1


def _recompute_person_centroid(conn: sqlite3.Connection, cluster_id: int) -> None:
    """Always recomputes EXACTLY from what's currently in the DB (mean of
    every face presently assigned to this cluster_id), rather than an
    incremental weighted-average update -- simpler and can never drift out
    of sync with reality, and cheap since per-person face counts are small.
    Deletes the `people` row outright if it ends up with zero faces (e.g.
    every face was moved away by a merge/split) -- an orphaned named person
    with no faces isn't useful to keep around in the labeling UI."""
    rows = conn.execute("SELECT embedding FROM faces WHERE cluster_id = ?", (cluster_id,)).fetchall()
    if not rows:
        conn.execute("DELETE FROM people WHERE cluster_id = ?", (cluster_id,))
        return
    embeddings = np.stack([blob_to_vec(r[0]) for r in rows])
    centroid = _normalize(embeddings.mean(axis=0))
    conn.execute(
        "UPDATE people SET representative_embedding = ?, face_count = ?, updated_at = ? WHERE cluster_id = ?",
        (vec_to_blob(centroid), len(rows), _now_iso(), cluster_id),
    )


def _load_existing_centroids(conn: sqlite3.Connection) -> tuple[list[int], np.ndarray]:
    rows = conn.execute(
        "SELECT cluster_id, representative_embedding FROM people WHERE representative_embedding IS NOT NULL"
    ).fetchall()
    if not rows:
        return [], np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    ids = [r[0] for r in rows]
    mat = np.stack([blob_to_vec(r[1]) for r in rows]).astype(np.float32)
    return ids, mat


def _dbscan_cosine(embeddings: np.ndarray, eps: float, min_samples: int) -> np.ndarray:
    """DBSCAN over cosine distance (1 - cosine similarity) without ever
    materializing a full n x n pairwise distance matrix. Built via faiss's
    exact inner-product neighbor search (embeddings are already
    L2-normalized -- insightface's `normed_embedding`, see
    face_detection.py -- so inner product IS cosine similarity) into a
    sparse CSR distance matrix, which sklearn's DBSCAN(metric="precomputed")
    accepts directly: entries outside eps can simply be omitted from a
    sparse precomputed matrix (see sklearn's own docs) rather than needing
    to be present-but-large.

    This is the piece that actually needs faiss: at this library's eventual
    real scale (photo-organizer-spec.md's Phase 3 section estimates
    tens-of-thousands-plus faces once a full library run happens), a dense
    n x n matrix (4 bytes x n^2) would run into tens of gigabytes and become
    infeasible, while faiss's IndexFlatIP + range_search stays roughly
    linear in the number of actual near-neighbor pairs, which stays small
    in a well-separated embedding space regardless of n. IndexFlatIP is
    exact (brute-force, BLAS-optimized), not an approximate index --
    appropriate at this project's realistic scale, not an oversight; see
    requirements.txt for why faiss (rather than a fallback) was chosen at
    all -- its Python 3.14 wheel situation was verified clean first, per
    the spec's own explicit caution.
    """
    import warnings

    import faiss
    from scipy.sparse import csr_matrix
    from sklearn.cluster import DBSCAN
    from sklearn.exceptions import EfficiencyWarning
    from sklearn.neighbors import sort_graph_by_row_values

    n = embeddings.shape[0]
    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)
    # faiss's range_search `radius` is a similarity threshold (bigger =
    # more similar/closer) -- the inverse sense of eps (bigger = more
    # permissive DISTANCE) below, hence similarity = 1 - eps.
    lims, sims, idx = index.range_search(embeddings, 1.0 - eps)
    # faiss returns lims/idx as uint64 -- np.repeat's `repeats` arg refuses
    # a uint64 array on Windows ("Cannot cast... to dtype('int64')"), so
    # cast explicitly rather than relying on an implicit/platform-dependent
    # cast that works on some numpy/OS combinations and not others.
    lims = lims.astype(np.int64)
    cols = idx.astype(np.int64)
    rows = np.repeat(np.arange(n), np.diff(lims))
    dist = np.clip(1.0 - sims, 0.0, None)  # cosine distance; clip guards tiny float noise going negative at near-identical vectors
    keep = rows != cols  # drop self-matches (always distance 0, not a meaningful neighbor)
    graph = csr_matrix((dist[keep], (rows[keep], cols[keep])), shape=(n, n))
    if graph.nnz > 0:
        # sklearn's DBSCAN wants each row's stored entries sorted by
        # DISTANCE VALUE (nearest first), not just by column index --
        # csr_matrix's own .sort_indices() sorts by column index and does
        # NOT satisfy this (confirmed: the EfficiencyWarning persisted even
        # after calling it). sort_graph_by_row_values is the documented
        # fix -- but it raises an IndexError of its own on a completely
        # empty graph (confirmed: a real run with zero within-threshold
        # neighbor pairs at all hits this, a genuine sklearn edge-case bug,
        # not a mistake here), hence the nnz guard: an empty graph needs no
        # sorting anyway (there's nothing to sort), so skip the call
        # entirely rather than working around sklearn's own bug further.
        graph = sort_graph_by_row_values(graph, warn_when_not_sorted=False)
    # Despite the above, sklearn's own internal sortedness re-check
    # (_is_sorted_by_data) still fires this EfficiencyWarning on some
    # inputs even immediately after sort_graph_by_row_values -- confirmed
    # harmless (DBSCAN's actual output was verified correct against known
    # synthetic clusters regardless, see tests/test_face_clustering.py),
    # so it's suppressed here rather than left to clutter every dashboard/
    # CLI log with a library-internal false positive.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=EfficiencyWarning)
        return DBSCAN(eps=eps, min_samples=min_samples, metric="precomputed").fit_predict(graph)


def run_face_clustering(
    cfg: Config,
    conn: sqlite3.Connection,
    logger: logging.Logger,
    progress_cb: Callable[[int, int], None] | None = None,
) -> ClusterStats:
    """Incremental clustering -- see module docstring for the full design.
    Only ever touches faces with cluster_id IS NULL; never reassigns an
    already-clustered face. Safe/idempotent to call with nothing new to do."""
    stats = ClusterStats()
    threshold = cfg.face_cluster_similarity_threshold
    min_samples = cfg.face_cluster_min_samples

    stats.excluded_low_confidence = conn.execute(
        "SELECT COUNT(*) FROM faces WHERE cluster_id IS NULL AND not_a_person = 0 "
        "AND det_score IS NOT NULL AND det_score < ?",
        (cfg.face_min_det_score,),
    ).fetchone()[0]

    # not_a_person = 0: a Phase 3c-flagged false positive is never a
    # clustering candidate, even after it's back to cluster_id IS NULL --
    # see mark_faces_not_a_person()'s docstring. This is the actual
    # enforcement point for "excluded from future cluster-faces re-runs".
    rows = conn.execute(
        "SELECT face_id, embedding FROM faces WHERE cluster_id IS NULL AND not_a_person = 0 "
        "AND (det_score IS NULL OR det_score >= ?)",
        (cfg.face_min_det_score,),
    ).fetchall()
    stats.candidates = len(rows)
    logger.info(
        f"Face clustering: {stats.candidates} unclustered face(s) with det_score >= {cfg.face_min_det_score} "
        f"({stats.excluded_low_confidence} excluded for low confidence, left unclustered)"
    )

    if not rows:
        stats.people_total = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        logger.info("Nothing new to cluster.")
        if progress_cb is not None:
            progress_cb(0, 0)
        return stats

    face_ids = [r[0] for r in rows]
    embeddings = np.stack([blob_to_vec(r[1]) for r in rows]).astype(np.float32)
    total = len(face_ids)
    done = 0

    # --- Step 1: match each candidate against every EXISTING person's centroid
    existing_ids, existing_mat = _load_existing_centroids(conn)
    unmatched_idx = list(range(len(face_ids)))
    if existing_ids:
        sims = embeddings @ existing_mat.T  # (n_faces, n_people), both sides already L2-normalized -> cosine similarity
        best_person_idx = np.argmax(sims, axis=1)
        best_sim = sims[np.arange(len(face_ids)), best_person_idx]
        matched_mask = best_sim >= threshold

        newly_matched: dict[int, list[int]] = {}
        unmatched_idx = []
        for i in range(len(face_ids)):
            if matched_mask[i]:
                newly_matched.setdefault(existing_ids[best_person_idx[i]], []).append(i)
            else:
                unmatched_idx.append(i)

        for cid, idxs in newly_matched.items():
            conn.executemany(
                "UPDATE faces SET cluster_id = ? WHERE face_id = ?",
                [(cid, face_ids[i]) for i in idxs],
            )
            stats.matched_existing += len(idxs)
            done += len(idxs)
            if progress_cb is not None:
                progress_cb(done, total)
        for cid in newly_matched:
            _recompute_person_centroid(conn, cid)
        conn.commit()

    # --- Step 2: DBSCAN whatever's left, forming brand new people ----------
    if len(unmatched_idx) >= min_samples:
        sub_ids = [face_ids[i] for i in unmatched_idx]
        sub_emb = embeddings[unmatched_idx]
        labels = _dbscan_cosine(sub_emb, eps=1.0 - threshold, min_samples=min_samples)

        next_cluster_id = _next_cluster_id(conn)
        for label in sorted(set(labels.tolist())):
            if label == -1:
                continue
            member_mask = labels == label
            member_face_ids = [sub_ids[i] for i in range(len(sub_ids)) if member_mask[i]]
            member_embeddings = sub_emb[member_mask]
            new_cid = next_cluster_id
            next_cluster_id += 1
            centroid = _normalize(member_embeddings.mean(axis=0))
            conn.execute(
                "INSERT INTO people (cluster_id, assigned_name, notes, representative_embedding, face_count, updated_at) "
                "VALUES (?, NULL, NULL, ?, ?, ?)",
                (new_cid, vec_to_blob(centroid), len(member_face_ids), _now_iso()),
            )
            conn.executemany(
                "UPDATE faces SET cluster_id = ? WHERE face_id = ?",
                [(new_cid, fid) for fid in member_face_ids],
            )
            stats.new_clusters_formed += 1
            stats.faces_in_new_clusters += len(member_face_ids)
            done += len(member_face_ids)
            if progress_cb is not None:
                progress_cb(done, total)
        stats.remained_unclustered = int((labels == -1).sum())
        conn.commit()
    else:
        stats.remained_unclustered = len(unmatched_idx)

    if progress_cb is not None:
        progress_cb(total, total)
    stats.people_total = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    logger.info(f"FACE CLUSTERING COMPLETE: {stats.summary()}")
    return stats


def cluster_faces_full(
    cfg: Config,
    conn: sqlite3.Connection,
    logger: logging.Logger,
    faces_path: Path,
    progress_cb: Callable[[int, int], None] | None = None,
) -> tuple[LoadStats, ClusterStats]:
    """Convenience wrapper combining the load + cluster steps -- what both
    `main.py cluster-faces` and the dashboard's Face Clustering panel call
    (rule 7: one mechanism, not duplicated between the two front ends)."""
    load_stats = load_faces_from_jsonl(conn, faces_path, logger)
    cluster_stats = run_face_clustering(cfg, conn, logger, progress_cb=progress_cb)
    return load_stats, cluster_stats


# ------------------------------------------------ labeling UI operations

def set_person_name(conn: sqlite3.Connection, cluster_id: int, name: str | None, notes: str | None = None) -> None:
    conn.execute("UPDATE people SET assigned_name = ?, notes = ? WHERE cluster_id = ?", (name, notes, cluster_id))
    conn.commit()


_SQL_VARIABLE_CHUNK = 500  # see comment below


def _source_clusters_of(conn: sqlite3.Connection, face_ids: list[int]) -> set[int]:
    """URGENT fix (real 61,676-face cluster at full-library scale): SQLite
    caps the number of bound parameters in one query (SQLITE_MAX_VARIABLE_
    NUMBER -- 999 on older builds, up to 32766 on newer ones) -- a single
    `face_id IN (...)` query with tens of thousands of placeholders raises
    `OperationalError: too many SQL variables`. Hit for real trying to
    reset cluster_id=81 to Unclustered via this exact function (called by
    remove_faces_from_cluster/create_person_from_faces/
    mark_faces_not_a_person) -- and since the labeling window's own Select
    All can now select an ENTIRE oversized cluster at once (see the
    pagination fix), a human clicking "Remove Selected from Cluster" on a
    huge group would hit this exact crash too, not just a batch script.
    Fixed by chunking into _SQL_VARIABLE_CHUNK-sized queries (comfortably
    under even the older 999 limit) and unioning the results -- correct
    for any face_ids list size, not hardcoded to this one cluster."""
    if not face_ids:
        return set()
    clusters: set[int] = set()
    for i in range(0, len(face_ids), _SQL_VARIABLE_CHUNK):
        chunk = face_ids[i : i + _SQL_VARIABLE_CHUNK]
        rows = conn.execute(
            f"SELECT DISTINCT cluster_id FROM faces WHERE face_id IN ({_in_clause(len(chunk))})", chunk
        ).fetchall()
        clusters.update(r[0] for r in rows if r[0] is not None)
    return clusters


def create_person_from_faces(
    conn: sqlite3.Connection, face_ids: list[int], name: str | None = None, notes: str | None = None
) -> int:
    """Moves the given faces (regardless of their current cluster_id,
    including NULL/unclustered) into a brand-new person. Powers both the
    labeling UI's "Split incorrect faces out of a cluster" action and
    manually promoting selected faces out of the "(Unclustered)" bucket
    into a newly-named person."""
    if not face_ids:
        raise ValueError("face_ids must be non-empty")
    source_clusters = _source_clusters_of(conn, face_ids)
    new_cid = _next_cluster_id(conn)
    conn.execute(
        "INSERT INTO people (cluster_id, assigned_name, notes, representative_embedding, face_count, updated_at) "
        "VALUES (?, ?, ?, NULL, 0, ?)",
        (new_cid, name, notes, _now_iso()),
    )
    conn.executemany("UPDATE faces SET cluster_id = ? WHERE face_id = ?", [(new_cid, fid) for fid in face_ids])
    _recompute_person_centroid(conn, new_cid)
    for cid in source_clusters:
        _recompute_person_centroid(conn, cid)
    conn.commit()
    return new_cid


def remove_faces_from_cluster(conn: sqlite3.Connection, face_ids: list[int]) -> None:
    """Phase 3f's "Remove Selected from Cluster" action -- functionally
    "half of Split" (create_person_from_faces above): pulls the given faces
    out of whatever cluster(s) they're currently in and sends them back to
    "(Unclustered)" (cluster_id NULL), WITHOUT creating a new person the
    way create_person_from_faces does. Deliberately does NOT touch
    not_a_person -- these are real, valid faces that were just grouped
    with the wrong person, not Phase 3c's false-positive case (see
    mark_faces_not_a_person for that). Reuses the exact same
    centroid-recompute mechanism as Split/mark-not-a-person:
    _recompute_person_centroid on every source cluster affected, which
    also handles the source cluster ending up empty (deletes the orphaned
    `people` row) the same way a Split or merge already does. Removed
    faces are picked up again by the next `cluster-faces` run just like
    any other unclustered face, or can be manually re-labeled/split right
    away from the "(Unclustered)" bucket."""
    if not face_ids:
        return
    source_clusters = _source_clusters_of(conn, face_ids)
    conn.executemany(
        "UPDATE faces SET cluster_id = NULL WHERE face_id = ?",
        [(fid,) for fid in face_ids],
    )
    for cid in source_clusters:
        _recompute_person_centroid(conn, cid)
    conn.commit()


def move_faces_to_person(conn: sqlite3.Connection, face_ids: list[int], dest_cluster_id: int) -> None:
    """Moves faces into an ALREADY-EXISTING person -- e.g. "these clearly
    belong with person X" without merging two whole clusters at once.
    dest_cluster_id must already exist in `people` (create_person_from_faces
    above covers "no such person yet")."""
    if not face_ids:
        return
    if conn.execute("SELECT 1 FROM people WHERE cluster_id = ?", (dest_cluster_id,)).fetchone() is None:
        raise ValueError(f"no existing person with cluster_id={dest_cluster_id}")
    source_clusters = _source_clusters_of(conn, face_ids) - {dest_cluster_id}
    conn.executemany(
        "UPDATE faces SET cluster_id = ? WHERE face_id = ?", [(dest_cluster_id, fid) for fid in face_ids]
    )
    _recompute_person_centroid(conn, dest_cluster_id)
    for cid in source_clusters:
        _recompute_person_centroid(conn, cid)
    conn.commit()


def merge_people(conn: sqlite3.Connection, keep_cluster_id: int, other_cluster_id: int) -> None:
    """Merges `other` entirely into `keep`: every face reassigned, `other`'s
    people row removed. If `keep` has no assigned_name yet but `other` does,
    adopts `other`'s name/notes -- a convenience, since the cluster_id the
    user picked to "keep" isn't necessarily the one that happened to get
    named first."""
    if keep_cluster_id == other_cluster_id:
        return
    keep_row = conn.execute(
        "SELECT assigned_name, notes FROM people WHERE cluster_id = ?", (keep_cluster_id,)
    ).fetchone()
    other_row = conn.execute(
        "SELECT assigned_name, notes FROM people WHERE cluster_id = ?", (other_cluster_id,)
    ).fetchone()
    if keep_row is None or other_row is None:
        raise ValueError("both cluster_id values must already exist in people")
    conn.execute("UPDATE faces SET cluster_id = ? WHERE cluster_id = ?", (keep_cluster_id, other_cluster_id))
    if not keep_row[0] and other_row[0]:
        conn.execute(
            "UPDATE people SET assigned_name = ?, notes = COALESCE(notes, ?) WHERE cluster_id = ?",
            (other_row[0], other_row[1], keep_cluster_id),
        )
    conn.execute("DELETE FROM people WHERE cluster_id = ?", (other_cluster_id,))
    _recompute_person_centroid(conn, keep_cluster_id)
    conn.commit()


# ------------------------------------------------------- labeling UI reads

@dataclass
class ClusterSummary:
    cluster_id: int | None  # None = the "(Unclustered)" pseudo-group
    assigned_name: str | None
    notes: str | None
    face_count: int
    sample_face_id: int | None
    sample_file_hash: str | None
    sample_bbox: tuple[int, int, int, int] | None
    sample_path: str | None


def list_clusters(conn: sqlite3.Connection, include_unclustered: bool = True) -> list[ClusterSummary]:
    """Named/unnamed real people first (largest group first -- the ones
    most worth a labeler's time), an "(Unclustered)" pseudo-group last."""
    out: list[ClusterSummary] = []
    for cluster_id, name, notes, face_count in conn.execute(
        "SELECT cluster_id, assigned_name, notes, face_count FROM people "
        "WHERE face_count > 0 ORDER BY face_count DESC"
    ).fetchall():
        sample = conn.execute(
            "SELECT f.face_id, f.file_hash, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, ph.current_path "
            "FROM faces f JOIN photos ph ON ph.file_hash = f.file_hash "
            "WHERE f.cluster_id = ? LIMIT 1",
            (cluster_id,),
        ).fetchone()
        out.append(_cluster_summary(cluster_id, name, notes, face_count, sample))

    if include_unclustered:
        # not_a_person = 0: a Phase 3c-flagged face is pulled out of the
        # "(Unclustered)" bucket too, not just out of a real cluster -- it
        # has its own dedicated bucket (see list_not_a_person_faces /
        # count_not_a_person), which FaceLabelingWindow shows separately
        # behind its "Show marked-as-not-a-person" toggle, off by default.
        count = conn.execute(
            "SELECT COUNT(*) FROM faces WHERE cluster_id IS NULL AND not_a_person = 0"
        ).fetchone()[0]
        if count:
            sample = conn.execute(
                "SELECT f.face_id, f.file_hash, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, ph.current_path "
                "FROM faces f JOIN photos ph ON ph.file_hash = f.file_hash "
                "WHERE f.cluster_id IS NULL AND f.not_a_person = 0 LIMIT 1"
            ).fetchone()
            out.append(_cluster_summary(None, "(Unclustered)", None, count, sample))
    return out


def _cluster_summary(cluster_id, name, notes, face_count, sample) -> ClusterSummary:
    return ClusterSummary(
        cluster_id=cluster_id,
        assigned_name=name,
        notes=notes,
        face_count=face_count,
        sample_face_id=sample[0] if sample else None,
        sample_file_hash=sample[1] if sample else None,
        sample_bbox=(sample[2], sample[3], sample[4], sample[5]) if sample else None,
        sample_path=sample[6] if sample else None,
    )


@dataclass
class FaceRecord:
    face_id: int
    file_hash: str
    bbox: tuple[int, int, int, int]
    current_path: str
    det_score: float | None


def list_faces_in_cluster(conn: sqlite3.Connection, cluster_id: int | None) -> list[FaceRecord]:
    """cluster_id=None lists the "(Unclustered)" bucket. A face flagged
    not_a_person is excluded here too (it has cluster_id IS NULL like any
    other unclustered face, but belongs to its own dedicated bucket -- see
    list_not_a_person_faces) -- filtered unconditionally, not just for the
    cluster_id IS NULL branch, as a defensive guard in case a real cluster_id
    row is ever left inconsistent."""
    where = "f.cluster_id IS NULL" if cluster_id is None else "f.cluster_id = ?"
    params = () if cluster_id is None else (cluster_id,)
    rows = conn.execute(
        "SELECT f.face_id, f.file_hash, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, ph.current_path, f.det_score "
        f"FROM faces f JOIN photos ph ON ph.file_hash = f.file_hash WHERE {where} AND f.not_a_person = 0 "
        "ORDER BY f.face_id",
        params,
    ).fetchall()
    return [
        FaceRecord(face_id=r[0], file_hash=r[1], bbox=(r[2], r[3], r[4], r[5]), current_path=r[6], det_score=r[7])
        for r in rows
    ]


def count_not_a_person(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM faces WHERE not_a_person = 1").fetchone()[0]


def list_not_a_person_faces(conn: sqlite3.Connection) -> list[FaceRecord]:
    """The dedicated bucket for Phase 3c-flagged false positives -- shown by
    FaceLabelingWindow only behind its "Show marked-as-not-a-person" toggle,
    off by default, per the spec's "hidden from view by default" requirement."""
    rows = conn.execute(
        "SELECT f.face_id, f.file_hash, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, ph.current_path, f.det_score "
        "FROM faces f JOIN photos ph ON ph.file_hash = f.file_hash "
        "WHERE f.not_a_person = 1 ORDER BY f.face_id"
    ).fetchall()
    return [
        FaceRecord(face_id=r[0], file_hash=r[1], bbox=(r[2], r[3], r[4], r[5]), current_path=r[6], det_score=r[7])
        for r in rows
    ]


def mark_faces_not_a_person(conn: sqlite3.Connection, face_ids: list[int]) -> None:
    """Phase 3c's "Mark as not a person" action -- a reversible flag (same
    philosophy as photos.excluded / "Hide from library"), never a face row
    delete. Pulls each face out of whatever cluster it's currently in (same
    mechanical effect as a split, but tagged not_a_person rather than moved
    to a new named person) and recomputes that cluster's centroid so it
    doesn't keep counting a face that no longer belongs to it. Idempotent --
    marking an already-marked face (cluster_id already NULL) is a harmless
    no-op recompute-of-nothing."""
    if not face_ids:
        return
    source_clusters = _source_clusters_of(conn, face_ids)
    conn.executemany(
        "UPDATE faces SET not_a_person = 1, cluster_id = NULL WHERE face_id = ?",
        [(fid,) for fid in face_ids],
    )
    for cid in source_clusters:
        _recompute_person_centroid(conn, cid)
    conn.commit()


def unmark_faces_not_a_person(conn: sqlite3.Connection, face_ids: list[int]) -> None:
    """Reverses mark_faces_not_a_person(). Deliberately does NOT try to
    restore whatever cluster_id the face had before it was marked -- that
    prior grouping is stale the moment the face left it (the cluster's
    centroid has already been recomputed without it, and time may have
    passed), and DBSCAN cluster_ids aren't meaningfully "the same" across
    runs anyway (see this module's own re-clustering design note). An
    un-marked face simply rejoins the "(Unclustered)" bucket with
    cluster_id NULL, same as a never-flagged, never-clustered face --
    the next `cluster-faces` run picks it back up like any other
    unclustered candidate (matching an existing person's centroid, or
    seeding a new one), and the user can also manually move/split it
    from the Unclustered bucket right away without waiting for that."""
    if not face_ids:
        return
    conn.executemany(
        "UPDATE faces SET not_a_person = 0 WHERE face_id = ?",
        [(fid,) for fid in face_ids],
    )
    conn.commit()


def list_people_for_merge_target(conn: sqlite3.Connection, exclude_cluster_id: int | None = None) -> list[tuple[int, str]]:
    """(cluster_id, display label) pairs for a "merge into..." / "move to
    existing person..." picker, largest group first."""
    rows = conn.execute(
        "SELECT cluster_id, assigned_name, face_count FROM people WHERE face_count > 0 ORDER BY face_count DESC"
    ).fetchall()
    out = []
    for cid, name, count in rows:
        if cid == exclude_cluster_id:
            continue
        label = f"{name or '(unnamed)'} — {count} face(s) [cluster {cid}]"
        out.append((cid, label))
    return out


# --------------------------------------------------------- thumbnail crop

def crop_face_thumbnail(path: str | Path, bbox: tuple[int, int, int, int], size: int = 128, margin: float = 0.25) -> Image.Image:
    """Crops a face out of the ORIGINAL photo file for display in the
    labeling UI.

    Coordinate-frame note (a real correctness point, not boilerplate):
    bbox was computed by face_detection.py against the RAW decoded pixel
    array, with NO EXIF-orientation correction applied (see that module's
    _load_image_bgr — insightface needs pixel data in whatever orientation
    the sensor actually recorded, same reasoning). Calling
    ImageOps.exif_transpose() on the FULL image before cropping would
    rotate the pixel grid and invalidate bbox's coordinates. So this crops
    FIRST, in the exact same raw orientation face_detection.py saw, THEN
    applies exif_transpose() to just the small resulting crop to make the
    face right-side-up for a human labeler. Pillow's Image.crop() copies
    the source image's `.info` dict (including the raw 'exif' bytes), so
    exif_transpose() on the crop still reads the correct orientation tag.
    This mirrors review_tool.py's own exif_transpose fix (CLAUDE.md) but
    applied to a crop rather than a whole image, for the coordinate-frame
    reason above.

    margin pads the tight detection bbox a bit (as a fraction of its own
    width/height) so a labeler sees a little context around the face,
    clamped to the photo's actual bounds.
    """
    with Image.open(path) as img:
        img = img.convert("RGB")
        x, y, w, h = bbox
        pad_x, pad_y = int(w * margin), int(h * margin)
        left = max(0, x - pad_x)
        top = max(0, y - pad_y)
        right = min(img.width, x + w + pad_x)
        bottom = min(img.height, y + h + pad_y)
        crop = img.crop((left, top, right, bottom))
    crop = ImageOps.exif_transpose(crop)
    crop.thumbnail((size, size))
    return crop
