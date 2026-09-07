"""Exercises Phase 3e: displaying named people (not just filtering by them,
Phase 3d's own scope) directly on photos/videos in review_tool.py -- the
viewer's "People" row and the grid's per-card badges, both backed by the new
_people_for_hashes()/_row_to_dict(..., people=...) machinery. Same isolated-
temp-dir posture as test_phase3d.py -- never touches config.yaml, the real
DB, or anything under E:\\Pics.

Usage:
    venv\\Scripts\\python tests\\test_phase3e.py
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from src.db import connect, init_db


def _setup(tmp: Path):
    """Same fixture shape as test_phase3d.py's _setup, but photo rows are
    inserted directly (no real image files needed -- nothing in this test
    file ever hits /image/<hash>, only the JSON API):

        h1: face -> cluster 10 ("Alice")
        h2: faces -> cluster 10 ("Alice") AND cluster 20 ("Bob")
        h3: face -> cluster 20 ("Bob"), PLUS a second face flagged
            not_a_person=1 on cluster 10 ("Alice") -- must NOT make Alice
            show up for h3 (Phase 3c's false-positive flag must be honored
            here exactly like it already is for clustering/the filter)
        h4: face -> cluster 30 (unnamed -- assigned_name IS NULL, must never
            be displayed, same "named only" posture as Phase 3d's facets list)
        h5: no faces at all -- must render as an empty list, not an error
    """
    import review_tool

    case = tmp / f"case_{uuid.uuid4().hex[:8]}"
    dest = case / "dest"
    dest.mkdir(parents=True)

    dbp = case / "review.db"
    logger = logging.getLogger("phase3e_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)

    def insert_photo(file_hash, filename, date_taken):
        path = str(dest / "2021" / "2021-06" / filename)
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at,
               phase1_verified)
               VALUES (?, ?, ?, ?, 1000, 0, ?, 2021, 6, 'exif', 'sorted', '2021-06-01', 1)""",
            (file_hash, path, path, filename, date_taken),
        )

    insert_photo("h1", "01_alice.jpg", "2021-06-01T09:00:00")
    insert_photo("h2", "02_alice_and_bob.jpg", "2021-06-02T09:00:00")
    insert_photo("h3", "03_bob_plus_flagged_alice.jpg", "2021-06-03T09:00:00")
    insert_photo("h4", "04_unnamed_face.jpg", "2021-06-04T09:00:00")
    insert_photo("h5", "05_no_faces.jpg", "2021-06-05T09:00:00")

    conn.execute("INSERT INTO people (cluster_id, assigned_name, face_count) VALUES (10, 'Alice', 1)")
    conn.execute("INSERT INTO people (cluster_id, assigned_name, face_count) VALUES (20, 'Bob', 2)")
    conn.execute("INSERT INTO people (cluster_id, assigned_name, face_count) VALUES (30, NULL, 1)")

    def insert_face(file_hash, cluster_id, bbox=(0, 0, 50, 50), not_a_person=0):
        conn.execute(
            "INSERT INTO faces (file_hash, bbox_x, bbox_y, bbox_w, bbox_h, embedding, det_score, cluster_id, "
            "not_a_person, detected_at) VALUES (?, ?, ?, ?, ?, ?, 0.9, ?, ?, '2021-06-01T00:00:00')",
            (file_hash, *bbox, b"\x00" * 4, cluster_id, not_a_person),
        )

    insert_face("h1", 10)
    insert_face("h2", 10, bbox=(0, 0, 40, 40))
    insert_face("h2", 20, bbox=(50, 50, 40, 40))
    insert_face("h3", 20)
    insert_face("h3", 10, bbox=(60, 60, 20, 20), not_a_person=1)  # flagged -- must not display as Alice
    insert_face("h4", 30)
    # h5 deliberately gets no face row at all.

    conn.commit()
    conn.close()

    captions_path = case / "captions.jsonl"
    captions_path.write_text("", encoding="utf-8")

    cfg = Config(
        source_folders=[], dest_root=str(dest), supported_extensions=[".jpg"],
        unsorted_subfolder="_unsorted", dry_run=True, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(dbp), log_dir=str(case / "logs"),
        captions_path=str(captions_path), video_extensions=[".mp4", ".mov", ".avi"],
        review_page_size=40,
    )
    review_tool._cfg = cfg
    review_tool._video_exts = cfg.video_extensions_normalized
    review_tool._captions_cache = review_tool.CaptionsCache(cfg.captions_path_abs)
    review_tool._captions_cache.refresh()
    review_tool._random_order_cache.clear()

    return review_tool, review_tool.app.test_client(), dbp


def test_grid_carries_named_people_per_photo(tmp: Path) -> None:
    print("\n=== Phase 3e: /api/photos' `people` field is real per-photo named data ===")
    _rt, client, _dbp = _setup(tmp)

    resp = client.get("/api/photos?limit=50")
    by_hash = {i["file_hash"]: i["people"] for i in resp.get_json()["items"]}

    assert by_hash["h1"] == [{"cluster_id": 10, "name": "Alice"}], by_hash["h1"]
    print("  h1 (one Alice face) -> exactly [Alice]  OK")

    assert by_hash["h2"] == [{"cluster_id": 10, "name": "Alice"}, {"cluster_id": 20, "name": "Bob"}], by_hash["h2"]
    print("  h2 (Alice + Bob) -> both, sorted by name  OK")

    assert by_hash["h3"] == [{"cluster_id": 20, "name": "Bob"}], by_hash["h3"]
    print("  h3 (Bob + a not_a_person-flagged Alice face) -> only Bob, flagged face excluded  OK")

    assert by_hash["h4"] == [], by_hash["h4"]
    print("  h4 (unnamed/unclustered-identity face) -> empty, never shown unnamed  OK")

    assert by_hash["h5"] == [], by_hash["h5"]
    print("  h5 (no faces at all) -> empty list, not an error/None  OK")


def test_single_row_endpoints_carry_people(tmp: Path) -> None:
    print("\n=== Phase 3e: single-row endpoints (nav/random/edit) also populate `people` ===")
    _rt, client, _dbp = _setup(tmp)

    resp = client.get("/api/nav?dir=next")
    first = resp.get_json()["item"]
    assert first["file_hash"] == "h1"
    assert first["people"] == [{"cluster_id": 10, "name": "Alice"}], first["people"]
    print("  /api/nav (sequential, no filter) -> first item h1 carries [Alice]  OK")

    resp = client.get("/api/random?person=20")
    item = resp.get_json()["item"]
    assert item["file_hash"] in ("h2", "h3")
    assert {p["name"] for p in item["people"]} >= {"Bob"}
    print(f"  /api/random (filtered to Bob) -> {item['file_hash']} correctly carries Bob in `people`  OK")


def test_people_batched_not_per_row(tmp: Path) -> None:
    print("\n=== Phase 3e: grid page fetch batches the people join, not one query per row ===")
    _rt, client, dbp = _setup(tmp)

    # Build a bigger fixture directly against the same DB -- enough rows that
    # an accidental N+1 (one people query per row) would be obvious in the
    # statement count, not just in a timing number that could be noise.
    conn = connect(dbp)
    n = 150
    photo_rows = []
    face_rows = []
    for i in range(n):
        h = f"bulk{i:04d}"
        path = str(Path(_rt._cfg.dest_root_path) / "2022" / "2022-01" / f"{h}.jpg")
        photo_rows.append((h, path, path, f"{h}.jpg", 1000, 0, f"2022-01-01T00:00:{i % 60:02d}",
                            2022, 1, "exif", "sorted", "2022-01-01", 1))
        face_rows.append((h, 0, 0, 10, 10, b"\x00" * 4, 0.9, 10, 0, "2022-01-01T00:00:00"))
    conn.executemany(
        """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
           date_taken, date_taken_year, date_taken_month, date_source, status, processed_at, phase1_verified)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        photo_rows,
    )
    conn.executemany(
        "INSERT INTO faces (file_hash, bbox_x, bbox_y, bbox_w, bbox_h, embedding, det_score, cluster_id, "
        "not_a_person, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        face_rows,
    )
    conn.commit()
    conn.close()

    # Count actual SQL statements executed for one page fetch, via a fresh
    # traced connection reusing review_tool's own query path (_fetch_page) --
    # the real proof against N+1, not just "it happened to be fast once".
    # Note: _row_to_dict also calls manual_edit_fields() (Phase 2f, pre-
    # existing, untouched this session) once per row -- that's a real O(rows)
    # query pattern already in the codebase before this session, so total
    # statement count legitimately scales with page size regardless of
    # anything added here. What THIS session's own addition must NOT do is
    # add a SECOND per-row pattern on top of that -- checked precisely below
    # by counting only statements that are _people_for_hashes' own join
    # (identifiable by its distinctive "JOIN people p" clause), which must
    # appear exactly once per page fetch no matter how many rows are on it.
    # _row_to_dict also reads Flask's `session` as of Phase 3h (to compute
    # favorited_by_me) -- in real use there's always an active request
    # (every caller here is a route handler), but calling
    # _build_filters/_fetch_page directly like this needs a request context
    # pushed manually, same as any other Flask-internals test. An empty
    # session (no "user") is fine -- this test doesn't touch favorites.
    with _rt.app.test_request_context():
        counts = {}
        for limit in (10, 150):
            conn = _rt.get_db()
            statements = []
            conn.set_trace_callback(statements.append)
            try:
                where, params = _rt._build_filters({"folder": "2022-01"})
                items, _has_next, _has_prev = _rt._fetch_page(conn, where, params, limit=limit, after=None, before=None)
                assert len(items) == min(limit, n), (limit, len(items))
            finally:
                conn.set_trace_callback(None)
                conn.close()
            people_join_count = sum(1 for s in statements if "JOIN people p" in s)
            counts[limit] = people_join_count
            print(f"  limit={limit}: {len(items)} rows fetched, {people_join_count} people-join SELECT(s) issued")

        # The whole point of batching: one people-join query per PAGE, not one
        # per ROW -- this must be exactly 1 whether the page has 10 rows or 150.
        assert counts[10] == 1, counts
        assert counts[150] == 1, counts
        print(f"  people-join query count stays exactly 1 regardless of page size ({counts[10]} vs {counts[150]}) -- no N+1  OK")

        # Also a real wall-clock number, not just a statement count, at a size in
        # the ballpark of a real page load (default review_page_size well below
        # this) -- per the session's own "verify, don't assume" instruction.
        conn = _rt.get_db()
        try:
            where, params = _rt._build_filters({"folder": "2022-01"})
            t0 = time.time()
            items, _has_next, _has_prev = _rt._fetch_page(conn, where, params, limit=150, after=None, before=None)
            elapsed = time.time() - t0
        finally:
            conn.close()
        assert elapsed < 1.0, f"a 150-row page fetch (incl. the people join) took {elapsed:.3f}s -- unexpectedly slow"
        print(f"  a 150-row page (incl. the people join) fetched in {elapsed*1000:.1f}ms  OK")

        for item in items:
            assert item["people"] == [{"cluster_id": 10, "name": "Alice"}], item["people"]
        print("  every bulk row correctly carries its Alice face in `people`  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase3e_"))
    print(f"Working in {tmp}")
    try:
        test_grid_carries_named_people_per_photo(tmp)
        test_single_row_endpoints_carry_people(tmp)
        test_people_batched_not_per_row(tmp)
        print("\nALL PHASE 3E TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
