"""Exercises Phase 2c's viewer v2 additions against synthetic fixtures in
an isolated temp dir (same posture as test_phase2b.py -- never touches
config.yaml, the real DB, or anything under E:\\Pics): the new tag/
caption-keyword/GPS-location filters, the "Random" one-off jump
(/api/random), and random-order slideshow navigation (/api/nav?mode=
random). Critically, per the spec's own callout, these are tested in
COMBINATION (filter + random), not just individually -- that's where the
extra_pred (cache-backed) scanning path in review_tool.py actually gets
exercised, and where subtle bugs (off-by-one in the Feistel permutation,
repeats/omissions in the shuffled-order cache) would show up.

Usage:
    venv\\Scripts\\python tests\\test_phase2c.py
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote

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
    """Builds a small, deliberately-overlapping fixture library and wires
    review_tool.py's module globals against it (same pattern as
    test_phase2b.py's test_review_tool_api) so its Flask test client can
    be exercised directly, with no real HTTP server. Each call gets its
    own unique subdirectory/DB under `tmp` -- every test function in this
    file calls _setup() independently, and file_hash is a primary key, so
    reusing one DB/dest tree across test functions would collide."""
    import review_tool

    case = tmp / f"case_{uuid.uuid4().hex[:8]}"
    dest = case / "dest"
    (dest / "2021" / "2021-06").mkdir(parents=True)
    (dest / "2021" / "2021-06" / "Video").mkdir(parents=True)
    d = dest / "2021" / "2021-06"

    # Sortable filenames -> deterministic current_path ordering, so
    # sequential-nav assertions below aren't order-flaky.
    p1 = _jpeg(d / "01_beach_sunset.jpg", "2021:06:01 09:00:00")
    p2 = _jpeg(d / "02_beach_family.jpg", "2021:06:02 09:00:00")
    p3 = _jpeg(d / "03_birthday_cake.jpg", "2021:06:03 09:00:00")
    p4 = _jpeg(d / "04_uncaptioned.jpg", "2021:06:04 09:00:00")
    p5 = _jpeg(d / "05_beach_day.jpg", "2021:06:05 09:00:00")
    video = d / "Video" / "06_clip.mp4"
    video.write_bytes(b"FAKE-MP4-BYTES")

    dbp = case / "review.db"
    logger = logging.getLogger("phase2c_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)

    def insert(file_hash, path, date_taken, is_video=False, location_name=None, gps_checked=0):
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at,
               phase1_verified, location_name, gps_checked)
               VALUES (?, ?, ?, ?, ?, 0, ?, 2021, 6, ?, 'sorted', '2021-06-01', 1, ?, ?)""",
            (file_hash, str(path), str(path), path.name, path.stat().st_size, date_taken,
             "container" if is_video else "exif", location_name, gps_checked),
        )

    insert("h1", p1, "2021-06-01T09:00:00", location_name="Miami, FL", gps_checked=1)
    insert("h2", p2, "2021-06-02T09:00:00", location_name=None, gps_checked=1)  # checked, none found
    insert("h3", p3, "2021-06-03T09:00:00", location_name="Marietta, GA", gps_checked=1)
    insert("h4", p4, "2021-06-04T09:00:00", location_name=None, gps_checked=0)  # never checked
    insert("h5", p5, "2021-06-05T09:00:00", location_name="Miami, FL", gps_checked=1)
    insert("h6_video", video, "2021-06-06T09:00:00", is_video=True)
    # Mirrors what `main.py load-captions` would have loaded from the
    # captions.jsonl below (tags: beach / birthday / cake) -- populated
    # here directly rather than actually running the loader, since only
    # /api/facets (Grid/Viewer nav cleanup) reads this table; tag
    # *filtering* itself still goes through the live captions cache, not
    # this table (see _build_extra_predicate).
    conn.execute("INSERT INTO tags (tag_name) VALUES ('beach'), ('birthday'), ('cake')")
    conn.commit()
    conn.close()

    captions_path = case / "captions.jsonl"
    captions = [
        {"file_hash": "h1", "path": str(p1), "caption": "A beautiful beach sunset over water.",
         "tags": ["beach", "sunset"], "date_taken": "2021-06-01T09:00:00", "model_used": "test",
         "processed_at": "2021-06-01T09:05:00"},
        {"file_hash": "h2", "path": str(p2), "caption": "Family playing at the beach.",
         "tags": ["beach", "family"], "date_taken": "2021-06-02T09:00:00", "model_used": "test",
         "processed_at": "2021-06-02T09:05:00"},
        {"file_hash": "h3", "path": str(p3), "caption": "Birthday cake with candles.",
         "tags": ["birthday", "cake"], "date_taken": "2021-06-03T09:00:00", "model_used": "test",
         "processed_at": "2021-06-03T09:05:00"},
        # h4 deliberately left uncaptioned -- "not yet captioned" case.
        {"file_hash": "h5", "path": str(p5), "caption": "Another beach day.",
         "tags": ["beach"], "date_taken": "2021-06-05T09:00:00", "model_used": "test",
         "processed_at": "2021-06-05T09:05:00"},
    ]
    with open(captions_path, "w", encoding="utf-8") as f:
        for c in captions:
            f.write(json.dumps(c) + "\n")

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
    # Random-order cache is process-global -- clear it so tests don't leak
    # shuffled orders from one seed value into another test.
    review_tool._random_order_cache.clear()

    return review_tool, review_tool.app.test_client()


def test_new_filters_alone(tmp: Path) -> None:
    print("\n=== Phase 2c: tag / caption-keyword / GPS-location filters (individually) ===")
    _rt, client = _setup(tmp)

    resp = client.get("/api/photos?tag=beach&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1", "h2", "h5"}, hashes
    print("  tag=beach -> exactly the 3 beach-tagged photos, video/others excluded  OK")

    resp = client.get("/api/photos?caption_kw=candles&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h3"}, hashes
    print("  caption_kw=candles -> exactly the birthday-cake photo  OK")

    # Grid/Viewer nav cleanup: `location` is now an EXACT match (the client
    # only ever sends a value picked from /api/facets' real, already-stored
    # location_name values, not an arbitrary substring) -- see
    # review_tool.py's _build_filters docstring.
    resp = client.get("/api/photos?location=Miami%2C+FL&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1", "h5"}, hashes
    print("  location=Miami, FL (exact) -> exactly the 2 Miami photos  OK")

    resp = client.get("/api/photos?has_location=yes&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1", "h3", "h5"}, hashes
    resp = client.get("/api/photos?has_location=no&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    # h2/h4: "checked, none found" and "never checked" respectively. h6_video
    # (Phase 2e, included alongside photos now): video GPS is genuinely
    # unavailable, so it correctly lands here too -- not a special case.
    assert hashes == {"h2", "h4", "h6_video"}, hashes
    print("  has_location=yes/no partition correctly (never-checked rows + video, all 'no location')  OK")

    # /api/stats must reflect these new filters too, same regression class
    # as Phase 2b's date/folder fix.
    resp = client.get("/api/stats?tag=beach")
    assert resp.get_json()["total_photos"] == 3
    print("  /api/stats honors the new filters too  OK")

    # An uncaptioned photo (h4) can never match a tag/caption filter --
    # sanity-check it's simply excluded, not an error.
    resp = client.get("/api/photos?tag=nonexistent-tag&limit=50")
    assert resp.get_json()["items"] == []
    print("  a tag with zero matches returns an empty (not broken) result  OK")


def test_facets_endpoint(tmp: Path) -> None:
    print("\n=== Grid/Viewer nav cleanup: /api/facets (Location/Tag dropdown data) ===")
    _rt, client = _setup(tmp)

    resp = client.get("/api/facets")
    data = resp.get_json()
    assert data["tags"] == ["beach", "birthday", "cake"], data["tags"]
    assert data["locations"] == ["Marietta, GA", "Miami, FL"], data["locations"]
    print("  distinct tags (from `tags` table) and locations (from `photos.location_name`), both sorted  OK")

    # The dropdown's values must actually work as exact-match filters
    # against the endpoints that matter (see _build_filters' docstring).
    resp = client.get(f"/api/photos?location={quote(data['locations'][1])}&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1", "h5"}, hashes
    print("  a facet-supplied location value round-trips through /api/photos correctly  OK")


def test_media_type_filter(tmp: Path) -> None:
    print("\n=== Grid filter improvements batch: media_type filter (All/Photos only/Videos only) ===")
    _rt, client = _setup(tmp)

    resp = client.get("/api/photos?media_type=video&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h6_video"}, hashes
    print("  media_type=video -> exactly the one video row  OK")

    resp = client.get("/api/photos?media_type=photo&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1", "h2", "h3", "h4", "h5"}, hashes
    print("  media_type=photo -> all 5 photos, video excluded  OK")

    # Unset/empty (the "All" option) must be a no-op, same as before this
    # filter existed.
    resp = client.get("/api/photos?limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1", "h2", "h3", "h4", "h5", "h6_video"}, hashes
    print("  media_type unset ('All') -> no restriction, all 6 rows  OK")

    # Combines correctly with an existing filter -- the video row has no
    # tags at all (never captioned), so tag=beach + media_type=video must
    # be empty, not silently ignore one of the two filters.
    resp = client.get("/api/photos?tag=beach&media_type=video&limit=50")
    assert resp.get_json()["items"] == []
    print("  media_type combines correctly with an existing filter (tag=beach + media_type=video -> empty)  OK")

    # /api/stats must honor it too -- same regression class as every other
    # filter's own stats check above.
    resp = client.get("/api/stats?media_type=video")
    assert resp.get_json()["total_photos"] == 1
    resp = client.get("/api/stats?media_type=photo")
    assert resp.get_json()["total_photos"] == 5
    print("  /api/stats honors media_type too  OK")


def test_people_filter_is_inert(tmp: Path) -> None:
    print("\n=== Phase 2c: people/faces filter is visible-but-inert ===")
    _rt, client = _setup(tmp)
    # The front-end never sends this param (control is disabled), but the
    # backend must not choke on it or accidentally use it either, in case
    # of e.g. a hand-crafted URL.
    resp = client.get("/api/photos?people=someone&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    # Phase 2e: video is included alongside photos now, so the unfiltered
    # set is all 6 rows -- 'people' being ignored is the only thing under
    # test here.
    assert hashes == {"h1", "h2", "h3", "h4", "h5", "h6_video"}, hashes
    print("  an unrecognized 'people' param is silently ignored, not applied as a filter  OK")


def test_nav_respects_new_filters(tmp: Path) -> None:
    print("\n=== Phase 2c: /api/nav sequential stepping honors tag/caption/GPS filters ===")
    _rt, client = _setup(tmp)

    # tag=beach: h1 -> h2 -> h5 -> end, and back.
    resp = client.get("/api/nav?tag=beach&dir=next")
    first = resp.get_json()["item"]
    assert first["file_hash"] == "h1"
    resp = client.get(f"/api/nav?tag=beach&dir=next&cursor={first['current_path']}")
    second = resp.get_json()["item"]
    assert second["file_hash"] == "h2"
    resp = client.get(f"/api/nav?tag=beach&dir=next&cursor={second['current_path']}")
    third = resp.get_json()["item"]
    assert third["file_hash"] == "h5"
    resp = client.get(f"/api/nav?tag=beach&dir=next&cursor={third['current_path']}")
    assert resp.get_json()["item"] is None, "must stop at the end of the FILTERED set, not the whole library"
    resp = client.get(f"/api/nav?tag=beach&dir=prev&cursor={third['current_path']}")
    assert resp.get_json()["item"]["file_hash"] == "h2"
    print("  tag-filtered next/prev stays within {h1,h2,h5}, skipping h3/h4/video entirely  OK")

    # caption_kw should never surface the uncaptioned h4 or the video.
    resp = client.get("/api/nav?caption_kw=beach&dir=next")
    assert resp.get_json()["item"]["file_hash"] == "h1"


def test_random_button_respects_filters(tmp: Path) -> None:
    print("\n=== Phase 2c: /api/random ('surprise me') respects active filters ===")
    _rt, client = _setup(tmp)

    # Plain-filter (SQL-only) path: has_location=yes -> {h1, h3, h5}.
    seen = set()
    for _ in range(30):
        resp = client.get("/api/random?has_location=yes")
        item = resp.get_json()["item"]
        assert item is not None and item["file_hash"] in {"h1", "h3", "h5"}, item
        seen.add(item["file_hash"])
    assert len(seen) >= 2, f"expected some variety across 30 draws, got only {seen}"
    print(f"  /api/random?has_location=yes only ever returns {{'h1','h3','h5'}}, saw {sorted(seen)} across 30 draws  OK")

    # extra_pred (cache-backed) path: tag=beach -> {h1, h2, h5}.
    seen = set()
    for _ in range(30):
        resp = client.get("/api/random?tag=beach")
        item = resp.get_json()["item"]
        assert item is not None and item["file_hash"] in {"h1", "h2", "h5"}, item
        seen.add(item["file_hash"])
    assert len(seen) >= 2, f"expected some variety across 30 draws, got only {seen}"
    print(f"  /api/random?tag=beach only ever returns {{'h1','h2','h5'}}, saw {sorted(seen)} across 30 draws  OK")

    # A filter matching nothing must yield item: null, not an error.
    resp = client.get("/api/random?tag=nonexistent-tag")
    assert resp.get_json()["item"] is None
    print("  /api/random with zero matches returns item: null gracefully  OK")


def test_random_order_slideshow_no_extra_pred(tmp: Path) -> None:
    print("\n=== Phase 2c: /api/nav?mode=random -- Feistel permutation correctness (SQL-only filters) ===")
    _rt, client = _setup(tmp)

    # No tag/caption filter -> the no-extra_pred (_feistel_permute) path.
    # Phase 2e: video is included alongside photos, so the filtered set is
    # {h1..h5, h6_video}, size 6.
    all_hashes = {"h1", "h2", "h3", "h4", "h5", "h6_video"}
    seed = "test-seed-123"
    seen = []
    for idx in range(6):
        resp = client.get(f"/api/nav?mode=random&seed={seed}&idx={idx}")
        item = resp.get_json()["item"]
        assert item is not None, f"idx={idx} should still be within the 6-item permutation"
        seen.append(item["file_hash"])
    assert set(seen) == all_hashes, seen
    assert len(set(seen)) == 6, f"a true permutation must never repeat a hash across idx 0..5, got {seen}"
    print(f"  seed={seed!r} idx 0..5 visits all 6 matching rows (photos + video) exactly once: {seen}  OK")

    resp = client.get(f"/api/nav?mode=random&seed={seed}&idx=6")
    assert resp.get_json()["item"] is None, "idx past the end of the filtered set must return null"
    print("  idx=6 (one past the set size) correctly returns item: null  OK")

    resp = client.get(f"/api/nav?mode=random&seed={seed}&idx=-1")
    assert resp.get_json()["item"] is None, "a negative idx must not crash or wrap around"
    print("  idx=-1 handled gracefully (null, not an error)  OK")

    # A different seed should (almost certainly) give a different order --
    # not asserted as a hard guarantee (collisions are possible), but both
    # must independently still be valid full permutations.
    seed2 = "another-seed-456"
    seen2 = []
    for idx in range(6):
        resp = client.get(f"/api/nav?mode=random&seed={seed2}&idx={idx}")
        seen2.append(resp.get_json()["item"]["file_hash"])
    assert set(seen2) == all_hashes
    print(f"  a second seed also yields a valid full permutation: {seen2} "
          f"({'differs' if seen2 != seen else 'happens to match'} from the first)  OK")


def test_random_order_slideshow_with_tag_filter(tmp: Path) -> None:
    print("\n=== Phase 2c: /api/nav?mode=random -- shuffled-order cache correctness (tag-filtered) ===")
    _rt, client = _setup(tmp)

    # tag=beach -> extra_pred is active -> the _random_order_for cache path.
    seed = "beach-seed"
    seen = []
    for idx in range(3):
        resp = client.get(f"/api/nav?mode=random&seed={seed}&tag=beach&idx={idx}")
        item = resp.get_json()["item"]
        assert item is not None
        assert item["file_hash"] in {"h1", "h2", "h5"}, "random order must still respect the tag filter"
        seen.append(item["file_hash"])
    assert set(seen) == {"h1", "h2", "h5"} and len(set(seen)) == 3, seen
    print(f"  tag=beach random order visits exactly {{h1,h2,h5}} once each: {seen}  OK")

    resp = client.get(f"/api/nav?mode=random&seed={seed}&tag=beach&idx=3")
    assert resp.get_json()["item"] is None, "idx past the filtered-and-shuffled set must return null"
    print("  idx past the filtered set's size returns null, not an unfiltered photo  OK")

    # Re-requesting the same seed+idx must be stable (cache reuse), not a
    # fresh reshuffle on every single nav step within one slideshow.
    resp = client.get(f"/api/nav?mode=random&seed={seed}&tag=beach&idx=0")
    assert resp.get_json()["item"]["file_hash"] == seen[0], "same seed must reproduce the same order on repeat calls"
    print("  repeating idx=0 for the same seed reproduces the same photo (stable, cached order)  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase2c_"))
    print(f"Working in {tmp}")
    try:
        test_new_filters_alone(tmp)
        test_facets_endpoint(tmp)
        test_media_type_filter(tmp)
        test_people_filter_is_inert(tmp)
        test_nav_respects_new_filters(tmp)
        test_random_button_respects_filters(tmp)
        test_random_order_slideshow_no_extra_pred(tmp)
        test_random_order_slideshow_with_tag_filter(tmp)
        print("\nALL PHASE 2C TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
