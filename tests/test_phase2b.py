"""Exercises Phase 2b's two new pieces against synthetic fixtures in an
isolated temp dir -- never touches config.yaml, the real DB, or anything
under E:\\Pics (same posture as tests/test_phase2_pipeline.py):

  1. GPS extraction + offline reverse geocoding (src/gps_resolver.py,
     src/gps_backfill.py) -- see test_gps_extract_and_geocode().
  2. review_tool.py's read-only JSON API (Flask test client, no real HTTP
     server started) -- pagination, filters, the live-captions-cache
     refresh, and video exclusion.

Usage:
    venv\\Scripts\\python tests\\test_phase2b.py
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

import piexif
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from src.db import connect, init_db
from src.gps_backfill import run_gps_extraction
from src.gps_resolver import extract_gps_coords, reverse_geocode, resolve_location

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None


def _deg_to_dms_rational(deg: float):
    d = int(deg)
    m_full = (deg - d) * 60
    m = int(m_full)
    s = round((m_full - m) * 60 * 10000)
    return [(d, 1), (m, 1), (s, 10000)]


def _make_gps_jpeg(path: Path, lat: float, lon: float) -> None:
    img = Image.new("RGB", (20, 20), (10, 20, 30))
    gps_ifd = {
        piexif.GPSIFD.GPSLatitudeRef: "N" if lat >= 0 else "S",
        piexif.GPSIFD.GPSLatitude: _deg_to_dms_rational(abs(lat)),
        piexif.GPSIFD.GPSLongitudeRef: "E" if lon >= 0 else "W",
        piexif.GPSIFD.GPSLongitude: _deg_to_dms_rational(abs(lon)),
    }
    img.save(path, "jpeg", exif=piexif.dump({"GPS": gps_ifd}))


def _make_plain_jpeg(path: Path, color=(200, 0, 0)) -> None:
    Image.new("RGB", (20, 20), color).save(path, "jpeg")


def test_gps_extract_and_geocode(tmp: Path) -> None:
    print("\n=== GPS extraction + offline reverse geocoding ===")
    p_gps = tmp / "with_gps.jpg"
    p_nogps = tmp / "no_gps.jpg"
    _make_gps_jpeg(p_gps, 34.0393, -84.5340)  # near Marietta/Woodstock, GA
    _make_plain_jpeg(p_nogps)

    coords = extract_gps_coords(p_gps)
    assert coords is not None, "expected GPS coords to be extracted"
    lat, lon = coords
    assert abs(lat - 34.0393) < 0.01 and abs(lon - (-84.5340)) < 0.01

    place = reverse_geocode(lat, lon)
    assert place and "GA" in place, f"expected a Georgia place name, got {place!r}"
    print(f"  reverse_geocode({lat:.4f}, {lon:.4f}) -> {place!r}  OK")

    assert extract_gps_coords(p_nogps) is None
    assert resolve_location(p_nogps) is None
    print("  no-GPS photo correctly yields no location  OK")

    # --- integration: gps_backfill against a temp DB ---
    dbp = tmp / "gps_test.db"
    logger = logging.getLogger("phase2b_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)  # quiet -- this test asserts on return values, not log lines
    init_db(dbp, logger=logger)
    conn = connect(dbp)

    def insert(file_hash: str, path: Path) -> None:
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at, phase1_verified)
               VALUES (?, ?, ?, ?, ?, 0, '2020-01-01', 2020, 1, 'exif', 'sorted', '2020-01-01', 1)""",
            (file_hash, str(path), str(path), path.name, path.stat().st_size),
        )
        conn.commit()

    insert("hash_gps", p_gps)
    insert("hash_nogps", p_nogps)

    cfg = Config(
        source_folders=[], dest_root=str(tmp), supported_extensions=[".jpg"],
        unsorted_subfolder="_unsorted", dry_run=True, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(dbp), log_dir=str(tmp / "logs"),
    )

    stats = run_gps_extraction(cfg, conn, logger)
    assert stats.scanned == 2 and stats.found == 1 and stats.not_found == 1, stats.summary()

    row_gps = conn.execute(
        "SELECT gps_lat, gps_lon, location_name, gps_checked FROM photos WHERE file_hash='hash_gps'"
    ).fetchone()
    row_nogps = conn.execute(
        "SELECT gps_lat, gps_lon, location_name, gps_checked FROM photos WHERE file_hash='hash_nogps'"
    ).fetchone()
    assert row_gps[2] and "GA" in row_gps[2] and row_gps[3] == 1
    assert row_nogps[0] is None and row_nogps[1] is None and row_nogps[3] == 1
    print("  backfill wrote gps_lat/gps_lon/location_name for the GPS photo, "
          "gps_checked=1 for both  OK")

    # Resumability: gps_checked=1 rows must never be re-examined.
    stats2 = run_gps_extraction(cfg, conn, logger)
    assert stats2.scanned == 0, "expected a fully-checked DB to be a no-op re-run"
    print("  re-run against an already-checked DB is a pure no-op  OK")

    conn.close()


# ---------------------------------------------------------------------------
# review_tool.py's API, via Flask's test client (no real HTTP server)
# ---------------------------------------------------------------------------
def test_review_tool_api(tmp: Path) -> None:
    print("\n=== review_tool.py API (Flask test client) ===")
    import review_tool

    dest = tmp / "review_dest"
    (dest / "2020" / "2020-07").mkdir(parents=True)
    (dest / "2020" / "2020-07" / "Video").mkdir(parents=True)

    def jpeg(path: Path, dt: str, color=(10, 20, 30)) -> Path:
        img = Image.new("RGB", (30, 30), color)
        img.save(path, "jpeg", exif=piexif.dump({"Exif": {piexif.ExifIFD.DateTimeOriginal: dt.encode()}}))
        return path

    photo_captioned = jpeg(dest / "2020" / "2020-07" / "captioned.jpg", "2020:07:01 10:00:00")
    photo_pending = jpeg(dest / "2020" / "2020-07" / "pending.jpg", "2020:07:15 10:00:00")
    photo_other_folder = jpeg(dest / "2020" / "2020-07" / "third.jpg", "2020:07:20 10:00:00")
    video_path = dest / "2020" / "2020-07" / "Video" / "clip.mp4"
    video_path.write_bytes(b"FAKE-MP4-BYTES")

    dbp = tmp / "review.db"
    logger = logging.getLogger("phase2b_test_review")
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
               VALUES (?, ?, ?, ?, ?, 0, ?, 2020, 7, ?, 'sorted', '2020-07-01', 1, ?, ?)""",
            (file_hash, str(path), str(path), path.name, path.stat().st_size, date_taken,
             "container" if is_video else "exif", location_name, gps_checked),
        )

    insert("hash_captioned", photo_captioned, "2020-07-01T10:00:00", location_name="Marietta, GA", gps_checked=1)
    insert("hash_pending", photo_pending, "2020-07-15T10:00:00", gps_checked=0)
    insert("hash_third", photo_other_folder, "2020-07-20T10:00:00", gps_checked=1)
    insert("hash_video", video_path, "2020-07-05T10:00:00", is_video=True)
    conn.commit()
    conn.close()

    captions_path = tmp / "captions.jsonl"
    with open(captions_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "file_hash": "hash_captioned", "path": str(photo_captioned),
            "caption": "A test photo.", "tags": ["test", "fixture"],
            "date_taken": "2020-07-01T10:00:00", "model_used": "test-model",
            "processed_at": "2020-07-01T10:05:00",
        }) + "\n")

    cfg = Config(
        source_folders=[], dest_root=str(dest), supported_extensions=[".jpg"],
        unsorted_subfolder="_unsorted", dry_run=True, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(dbp), log_dir=str(tmp / "logs"),
        captions_path=str(captions_path), video_extensions=[".mp4", ".mov", ".avi"],
        review_page_size=2,
    )

    review_tool._cfg = cfg
    review_tool._video_exts = cfg.video_extensions_normalized
    review_tool._captions_cache = review_tool.CaptionsCache(cfg.captions_path_abs)
    review_tool._captions_cache.refresh()

    client = review_tool.app.test_client()

    # --- video exclusion + "not yet captioned" + basic fields ---
    resp = client.get("/api/photos?limit=50")
    data = resp.get_json()
    hashes = {item["file_hash"] for item in data["items"]}
    assert "hash_video" not in hashes, "video row leaked into the review tool's results"
    assert {"hash_captioned", "hash_pending", "hash_third"} == hashes
    by_hash = {item["file_hash"]: item for item in data["items"]}
    assert by_hash["hash_captioned"]["captioned"] is True
    assert by_hash["hash_captioned"]["caption"] == "A test photo."
    assert by_hash["hash_captioned"]["tags"] == ["test", "fixture"]
    assert by_hash["hash_captioned"]["location_name"] == "Marietta, GA"
    assert by_hash["hash_pending"]["captioned"] is False and by_hash["hash_pending"]["caption"] is None
    assert by_hash["hash_pending"]["location_name"] is None and by_hash["hash_pending"]["gps_checked"] is False
    assert by_hash["hash_captioned"]["people"] is None, "Phase 3 placeholder must stay None"
    print("  video excluded, captioned/pending/location fields all correct  OK")

    # --- keyset pagination: page size 2 across 3 matching rows ---
    resp = client.get("/api/photos?limit=2")
    page1 = resp.get_json()
    assert len(page1["items"]) == 2 and page1["has_next"] is True and page1["has_prev"] is False
    resp = client.get(f"/api/photos?limit=2&after={page1['next_cursor']}")
    page2 = resp.get_json()
    assert len(page2["items"]) == 1 and page2["has_next"] is False and page2["has_prev"] is True
    seen = {i["file_hash"] for i in page1["items"]} | {i["file_hash"] for i in page2["items"]}
    assert seen == {"hash_captioned", "hash_pending", "hash_third"}
    # paging back from page2 with `before` must reproduce page1 exactly
    resp = client.get(f"/api/photos?limit=2&before={page2['items'][0]['current_path']}")
    back = resp.get_json()
    assert [i["file_hash"] for i in back["items"]] == [i["file_hash"] for i in page1["items"]]
    print("  keyset pagination forward/back across a page boundary  OK")

    # --- folder + date filters ---
    resp = client.get("/api/photos?folder=captioned.jpg")
    assert [i["file_hash"] for i in resp.get_json()["items"]] == ["hash_captioned"]
    resp = client.get("/api/photos?date_from=2020-07-16")
    assert [i["file_hash"] for i in resp.get_json()["items"]] == ["hash_third"]
    print("  folder substring + date_from filters  OK")

    # --- /api/stats respects the same filters (regression: this was found
    # broken during manual testing -- api_stats() was ignoring request.args
    # entirely and always returning the unfiltered total) ---
    resp = client.get("/api/stats?folder=captioned.jpg")
    assert resp.get_json()["total_photos"] == 1, "/api/stats must apply the same filters as /api/photos"
    resp = client.get("/api/stats")
    assert resp.get_json()["total_photos"] == 3
    print("  /api/stats honors filters (regression check)  OK")

    # --- /api/nav: cursor-based step navigation, including reaching the end ---
    resp = client.get("/api/nav?dir=next")
    first = resp.get_json()["item"]
    assert first["file_hash"] == "hash_captioned"
    resp = client.get(f"/api/nav?dir=next&cursor={first['current_path']}")
    second = resp.get_json()["item"]
    assert second["file_hash"] == "hash_pending"
    resp = client.get(f"/api/nav?dir=prev&cursor={second['current_path']}")
    assert resp.get_json()["item"]["file_hash"] == "hash_captioned"
    # step past the last matching photo -> None, not an error
    resp = client.get(f"/api/nav?dir=next&cursor={second['current_path']}")
    third = resp.get_json()["item"]
    assert third["file_hash"] == "hash_third"
    resp = client.get(f"/api/nav?dir=next&cursor={third['current_path']}")
    assert resp.get_json()["item"] is None, "stepping past the last photo must return item: null, not an error"
    print("  /api/nav steps next/prev and returns null gracefully past the end  OK")

    # --- captions cache picks up a newly-appended line without restarting ---
    with open(captions_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "file_hash": "hash_pending", "path": str(photo_pending),
            "caption": "Now captioned.", "tags": ["late"],
            "date_taken": "2020-07-15T10:00:00", "model_used": "test-model",
            "processed_at": "2020-07-15T10:05:00",
        }) + "\n")
    resp = client.get("/api/photos?folder=pending.jpg")
    item = resp.get_json()["items"][0]
    assert item["captioned"] is True and item["caption"] == "Now captioned."
    print("  live captions.jsonl refresh picks up a new line without restarting  OK")

    # --- a corrupt/incomplete trailing line must not break the cache ---
    with open(captions_path, "a", encoding="utf-8") as f:
        f.write('{"file_hash": "hash_third", "incomplete')  # no closing brace, no newline
    review_tool._captions_cache.refresh()  # must not raise
    resp = client.get("/api/photos?folder=third.jpg")
    assert resp.get_json()["items"][0]["captioned"] is False, "a corrupt trailing line must not be treated as valid"
    print("  corrupt/incomplete trailing JSONL line tolerated, not treated as valid  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase2b_"))
    print(f"Working in {tmp}")
    try:
        test_gps_extract_and_geocode(tmp)
        test_review_tool_api(tmp)
        print("\nALL PHASE 2B TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
