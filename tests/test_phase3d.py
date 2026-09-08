"""Exercises Phase 3d: wiring review_tool.py's grid people/faces filter
(added inert in Phase 2c, "Phase 3 doesn't exist yet") up to real Phase 3
data -- a `person` query param (a people.cluster_id) that _build_filters
resolves via a plain `faces`/`people` join, plus /api/facets' new `people`
list (real assigned names only). Same isolated-temp-dir posture as
test_phase2b.py/test_phase2c.py -- never touches config.yaml, the real DB,
or anything under E:\\Pics.

Usage:
    venv\\Scripts\\python tests\\test_phase3d.py
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import piexif
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from src.db import connect, init_db


def _jpeg(path: Path, dt: str, color=(10, 20, 30)) -> Path:
    img = Image.new("RGB", (30, 30), color)
    img.save(path, "jpeg", exif=piexif.dump({"Exif": {piexif.ExifIFD.DateTimeOriginal: dt.encode()}}))
    return path


def _setup(tmp: Path):
    """Same fixture-and-module-globals pattern as test_phase2c.py's _setup
    (see its docstring) -- five photos, one with two different people's
    faces in it (to prove a photo isn't double-counted, and that either
    person's filter correctly includes it), one with only an unnamed/
    unclustered face (must never appear as an /api/facets option, but a
    hand-crafted URL naming its cluster_id directly must still filter
    correctly -- see _build_filters' docstring on being "permissive"), and
    one with no face at all.

        h1: face -> cluster 10 ("Alice")
        h2: faces -> cluster 10 ("Alice") AND cluster 20 ("Bob")
        h3: face -> cluster 20 ("Bob")
        h4: face -> cluster 30 (unnamed -- assigned_name IS NULL)
        h5: no faces
    """
    import review_tool

    case = tmp / f"case_{uuid.uuid4().hex[:8]}"
    dest = case / "dest"
    d = dest / "2021" / "2021-06"
    d.mkdir(parents=True)

    p1 = _jpeg(d / "01_alice.jpg", "2021:06:01 09:00:00")
    p2 = _jpeg(d / "02_alice_and_bob.jpg", "2021:06:02 09:00:00")
    p3 = _jpeg(d / "03_bob.jpg", "2021:06:03 09:00:00")
    p4 = _jpeg(d / "04_unnamed_face.jpg", "2021:06:04 09:00:00")
    p5 = _jpeg(d / "05_no_faces.jpg", "2021:06:05 09:00:00")

    dbp = case / "review.db"
    logger = logging.getLogger("phase3d_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)

    def insert_photo(file_hash, path, date_taken):
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at,
               phase1_verified)
               VALUES (?, ?, ?, ?, ?, 0, ?, 2021, 6, 'exif', 'sorted', '2021-06-01', 1)""",
            (file_hash, str(path), str(path), path.name, path.stat().st_size, date_taken),
        )

    insert_photo("h1", p1, "2021-06-01T09:00:00")
    insert_photo("h2", p2, "2021-06-02T09:00:00")
    insert_photo("h3", p3, "2021-06-03T09:00:00")
    insert_photo("h4", p4, "2021-06-04T09:00:00")
    insert_photo("h5", p5, "2021-06-05T09:00:00")

    # people: two named (real, assignable), one unclustered/unnamed --
    # the unnamed one must never surface via /api/facets (see below).
    conn.execute("INSERT INTO people (cluster_id, assigned_name, face_count) VALUES (10, 'Alice', 1)")
    conn.execute("INSERT INTO people (cluster_id, assigned_name, face_count) VALUES (20, 'Bob', 2)")
    conn.execute("INSERT INTO people (cluster_id, assigned_name, face_count) VALUES (30, NULL, 1)")

    def insert_face(file_hash, cluster_id, bbox=(0, 0, 50, 50)):
        conn.execute(
            "INSERT INTO faces (file_hash, bbox_x, bbox_y, bbox_w, bbox_h, embedding, det_score, cluster_id, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0.9, ?, '2021-06-01T00:00:00')",
            (file_hash, *bbox, b"\x00" * 4, cluster_id),
        )

    insert_face("h1", 10)
    insert_face("h2", 10, bbox=(0, 0, 40, 40))
    insert_face("h2", 20, bbox=(50, 50, 40, 40))
    insert_face("h3", 20)
    insert_face("h4", 30)
    # h5 deliberately gets no face row at all.

    conn.commit()
    conn.close()

    captions_path = case / "captions.jsonl"
    captions_path.write_text("", encoding="utf-8")  # no live captions needed for these tests

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

    return review_tool, review_tool.app.test_client()


def test_facets_lists_named_people_only(tmp: Path) -> None:
    print("\n=== Phase 3d: /api/facets' new `people` list is named-only ===")
    _rt, client = _setup(tmp)

    resp = client.get("/api/facets")
    data = resp.get_json()
    assert data["people"] == [
        {"cluster_id": 10, "assigned_name": "Alice"},
        {"cluster_id": 20, "assigned_name": "Bob"},
    ], data["people"]
    print("  exactly the 2 named people, sorted by name; cluster 30 (unnamed) excluded  OK")


def test_person_filter_narrows_grid(tmp: Path) -> None:
    print("\n=== Phase 3d: person=<cluster_id> narrows the grid via the faces/people join ===")
    _rt, client = _setup(tmp)

    resp = client.get("/api/photos?person=10&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1", "h2"}, hashes
    print("  person=10 (Alice) -> exactly the 2 photos with an Alice face  OK")

    resp = client.get("/api/photos?person=20&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h2", "h3"}, hashes
    print("  person=20 (Bob) -> exactly the 2 photos with a Bob face (h2 correctly matches both people)  OK")

    # Permissive-by-design (see _build_filters' docstring): a hand-crafted
    # URL naming the unnamed cluster still filters correctly, it's just
    # never offered as a UI choice (see the facets test above).
    resp = client.get("/api/photos?person=30&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h4"}, hashes
    print("  person=30 (unnamed, not offered by /api/facets) still filters correctly if named directly  OK")

    resp = client.get("/api/photos?person=9999&limit=50")
    assert resp.get_json()["items"] == [], "a cluster_id with zero faces must return empty, not error"
    print("  a nonexistent cluster_id returns an empty (not broken) result  OK")

    resp = client.get("/api/photos?person=not-a-number&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1", "h2", "h3", "h4", "h5"}, hashes
    print("  a non-numeric person value is silently ignored, not applied as a filter or an error  OK")


def test_person_filter_composes_with_other_filters(tmp: Path) -> None:
    print("\n=== Phase 3d: person filter composes with date range, same as every other filter combo ===")
    _rt, client = _setup(tmp)

    # Alice appears in h1 (06-01) and h2 (06-02); restricting to 06-02
    # onward must drop h1 but keep h2 -- both filters apply TOGETHER, not
    # one overriding the other.
    resp = client.get("/api/photos?person=10&date_from=2021-06-02&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h2"}, hashes
    print("  person=10 AND date_from=2021-06-02 -> exactly {h2}, not {h1,h2} or {}  OK")

    resp = client.get("/api/stats?person=10")
    assert resp.get_json()["total_photos"] == 2
    print("  /api/stats honors the person filter too, same regression class as every other filter  OK")


def test_nav_respects_person_filter(tmp: Path) -> None:
    print("\n=== Phase 3d: /api/nav sequential stepping honors the person filter ===")
    _rt, client = _setup(tmp)

    resp = client.get("/api/nav?person=20&dir=next")
    first = resp.get_json()["item"]
    assert first["file_hash"] == "h2"
    resp = client.get(f"/api/nav?person=20&dir=next&cursor={first['file_hash']}")
    second = resp.get_json()["item"]
    assert second["file_hash"] == "h3"
    resp = client.get(f"/api/nav?person=20&dir=next&cursor={second['file_hash']}")
    assert resp.get_json()["item"] is None, "must stop at the end of the person-filtered set"
    print("  person=20 (Bob) next-stepping visits h2 -> h3 -> end, skipping h1/h4/h5  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase3d_"))
    print(f"Working in {tmp}")
    try:
        test_facets_lists_named_people_only(tmp)
        test_person_filter_narrows_grid(tmp)
        test_person_filter_composes_with_other_filters(tmp)
        test_nav_respects_person_filter(tmp)
        print("\nALL PHASE 3D TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
