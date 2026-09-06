"""Exercises Phase 2g's new pieces against synthetic fixtures in an
isolated temp dir -- never touches config.yaml, the real DB, real videos,
or anything under E:\\Pics (same posture as tests/test_phase2b.py):

  1. First-frame extraction itself (src/video_thumbnail.py) against a real
     (tiny, synthetic) decodable video and a genuinely corrupt one -- see
     test_extract_first_frame().
  2. The batch backfill loop (src/thumbnail_backfill.py) -- resumability
     via disk-cache existence, the .failed-marker-vs-missing-file
     distinction, folder_prefix limiting, and early stop -- see
     test_run_thumbnail_extraction().
  3. review_tool.py's has_thumbnail field + /thumbnail/<file_hash> route,
     via Flask's test client (no real HTTP server) -- see
     test_review_tool_thumbnail_route().

Usage:
    venv\\Scripts\\python tests\\test_phase2g.py
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from src.db import connect, init_db
from src.thumbnail_backfill import ThumbStats, run_thumbnail_extraction
from src.video_thumbnail import extract_first_frame_jpeg

cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)


def _make_synthetic_video(path: Path, frames: int = 5, size=(64, 48), colors=None) -> None:
    """A tiny, genuinely decodable MP4 -- written by cv2.VideoWriter itself
    (confirmed round-trips through cv2.VideoCapture in this environment),
    so this test exercises the real decode path, not a mock."""
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    w, h = size
    writer = cv2.VideoWriter(str(path), fourcc, 5.0, (w, h))
    try:
        for i in range(frames):
            color = colors[i] if colors else (i * 40 % 256, 100, 200)
            frame = np.full((h, w, 3), color, dtype=np.uint8)
            writer.write(frame)
    finally:
        writer.release()


def test_extract_first_frame(tmp: Path) -> None:
    print("\n=== First-frame extraction (src/video_thumbnail.py) ===")
    good = tmp / "good.mp4"
    _make_synthetic_video(good, colors=[(10, 20, 200)] * 5)  # a distinctive first-frame color

    data = extract_first_frame_jpeg(good, max_dimension=200)
    assert data is not None, "expected a real decodable video to yield thumbnail bytes"
    img = Image.open(__import__("io").BytesIO(data))
    assert img.format == "JPEG"
    assert max(img.size) <= 200
    # BGR->RGB conversion sanity check: the synthetic frame was written as
    # (B, G, R) = (10, 20, 200) via cv2 -- after the module's BGR->RGB flip
    # and JPEG's lossy encoding, the decoded pixel should read close to
    # RGB (200, 20, 10), not the un-flipped (10, 20, 200).
    px = img.convert("RGB").getpixel((img.size[0] // 2, img.size[1] // 2))
    assert px[0] > px[2], f"expected R>B after BGR->RGB conversion, got {px}"
    print(f"  real synthetic video -> {len(data)} JPEG bytes, {img.size}, BGR->RGB conversion correct  OK")

    corrupt = tmp / "corrupt.mp4"
    corrupt.write_bytes(b"NOT A REAL VIDEO FILE, JUST GARBAGE BYTES" * 20)
    assert extract_first_frame_jpeg(corrupt, max_dimension=200) is None
    print("  corrupt/unreadable file correctly yields None, not a crash  OK")

    missing = tmp / "does_not_exist.mp4"
    assert extract_first_frame_jpeg(missing, max_dimension=200) is None
    print("  nonexistent path correctly yields None  OK")


def _make_cfg(tmp: Path, dbp: Path, thumb_dir: Path) -> Config:
    return Config(
        source_folders=[], dest_root=str(tmp), supported_extensions=[".jpg"],
        unsorted_subfolder="_unsorted", dry_run=True, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(dbp), log_dir=str(tmp / "logs"),
        video_extensions=[".mp4", ".mov"],
        thumbnail_dir=str(thumb_dir), thumbnail_max_dimension=200,
    )


def test_run_thumbnail_extraction(tmp: Path) -> None:
    print("\n=== Batch backfill loop (src/thumbnail_backfill.py) ===")
    good_video = tmp / "good.mp4"
    _make_synthetic_video(good_video)
    corrupt_video = tmp / "corrupt.mov"
    corrupt_video.write_bytes(b"GARBAGE" * 50)
    missing_video = tmp / "moved_away.mp4"  # inserted into the DB, never actually created on disk
    other_folder_video = tmp / "other_folder" / "clip.mp4"
    other_folder_video.parent.mkdir(parents=True)
    _make_synthetic_video(other_folder_video)
    photo = tmp / "photo.jpg"
    Image.new("RGB", (10, 10)).save(photo, "jpeg")

    dbp = tmp / "thumbs_test.db"
    thumb_dir = tmp / "thumbnails"
    logger = logging.getLogger("phase2g_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)

    def insert(file_hash: str, path: Path, size: int = 12345) -> None:
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at, phase1_verified)
               VALUES (?, ?, ?, ?, ?, 0, '2020-01-01', 2020, 1, 'container', 'sorted', '2020-01-01', 1)""",
            (file_hash, str(path), str(path), path.name, size),
        )
        conn.commit()

    insert("hash_good", good_video, good_video.stat().st_size)
    insert("hash_corrupt", corrupt_video, corrupt_video.stat().st_size)
    insert("hash_missing", missing_video)  # never created -- size is a made-up placeholder
    insert("hash_other_folder", other_folder_video, other_folder_video.stat().st_size)
    insert("hash_photo", photo, photo.stat().st_size)  # not a video -- must never be scanned

    cfg = _make_cfg(tmp, dbp, thumb_dir)

    stats = run_thumbnail_extraction(cfg, conn, logger)
    assert stats.scanned == 4, f"expected only the 4 video rows to be scanned, got {stats.summary()}"
    assert stats.extracted == 2 and stats.errors == 2 and stats.already_cached == 0, stats.summary()
    print(f"  first run: {stats.summary()}  OK (photo row correctly excluded from scanning)")

    thumb_good = thumb_dir / "hash_good.jpg"
    thumb_other = thumb_dir / "hash_other_folder.jpg"
    failed_corrupt = thumb_dir / "hash_corrupt.failed"
    assert thumb_good.exists() and thumb_other.exists()
    assert Image.open(thumb_good).format == "JPEG"
    assert failed_corrupt.exists(), "a genuinely corrupt video must get a .failed marker so it isn't retried forever"
    assert not (thumb_dir / "hash_missing.jpg").exists() and not (thumb_dir / "hash_missing.failed").exists(), \
        "a merely-missing file must NOT get a .failed marker -- it's a transient condition, retry next run"
    print("  cached .jpg for both real videos, .failed marker for the corrupt one, "
          "no marker at all for the missing one  OK")

    # --- Resumability: a second run must not re-attempt cached or failed-marked videos ---
    stats2 = run_thumbnail_extraction(cfg, conn, logger)
    assert stats2.scanned == 4
    assert stats2.extracted == 0, "cached/failed-marked videos must not be re-extracted"
    assert stats2.already_cached == 3, \
        "hash_good + hash_other_folder (cached) and hash_corrupt (failed-marked) all count as already_cached"
    assert stats2.errors == 1, "hash_missing has no marker either way -- correctly retried, and still missing"
    print(f"  second run: {stats2.summary()}  OK (resumable — cached/failed skipped, missing file still retried)")

    # --- folder_prefix (--limit) ---
    stats3 = run_thumbnail_extraction(cfg, conn, logger, folder_prefix=str(other_folder_video.parent))
    assert stats3.scanned == 1, f"expected --limit to restrict to just the one video under other_folder, got {stats3.summary()}"
    print("  folder_prefix correctly restricts to a single subfolder  OK")

    # --- stop_check (Cancel button) ---
    thumb_dir2 = tmp / "thumbnails_stop_test"
    cfg2 = _make_cfg(tmp, dbp, thumb_dir2)
    calls = {"n": 0}

    def stop_after_one():
        calls["n"] += 1
        return calls["n"] > 1

    stats4 = run_thumbnail_extraction(cfg2, conn, logger, stop_check=stop_after_one)
    assert stats4.scanned == 1, f"expected stop_check to halt after the first file, got {stats4.summary()}"
    print("  stop_check halts the run early, mid-scan  OK")

    conn.close()


def test_review_tool_thumbnail_route(tmp: Path) -> None:
    print("\n=== review_tool.py has_thumbnail field + /thumbnail/<hash> route ===")
    import review_tool
    from src.storage import get_storage

    tmp = tmp / "review_route_test"  # own subtree -- other tests in this file share the parent tmp dir
    tmp.mkdir()
    dest = tmp / "review_dest"
    (dest / "2021" / "2021-03" / "Video").mkdir(parents=True)
    video_with_thumb = dest / "2021" / "2021-03" / "Video" / "with_thumb.mp4"
    video_with_thumb.write_bytes(b"FAKE-MP4-BYTES")
    video_no_thumb = dest / "2021" / "2021-03" / "Video" / "no_thumb.mp4"
    video_no_thumb.write_bytes(b"FAKE-MP4-BYTES-2")

    dbp = tmp / "review_thumb.db"
    logger = logging.getLogger("phase2g_test_review")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)

    def insert(file_hash, path):
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at, phase1_verified)
               VALUES (?, ?, ?, ?, ?, 0, '2021-03-01T00:00:00', 2021, 3, 'container', 'sorted', '2021-03-01', 1)""",
            (file_hash, str(path), str(path), path.name, path.stat().st_size),
        )
    insert("hash_with_thumb", video_with_thumb)
    insert("hash_no_thumb", video_no_thumb)
    conn.commit()
    conn.close()

    thumb_dir = tmp / "thumbnails"
    thumb_dir.mkdir()
    thumb_bytes = b"\xff\xd8\xff\xe0FAKE-JPEG-BYTES-FOR-TEST"
    (thumb_dir / "hash_with_thumb.jpg").write_bytes(thumb_bytes)

    cfg = Config(
        source_folders=[], dest_root=str(dest), supported_extensions=[".jpg"],
        unsorted_subfolder="_unsorted", dry_run=True, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(dbp), log_dir=str(tmp / "logs"),
        captions_path=str(tmp / "captions.jsonl"), video_extensions=[".mp4", ".mov", ".avi"],
        thumbnail_dir=str(thumb_dir),
    )
    review_tool._cfg = cfg
    review_tool._video_exts = cfg.video_extensions_normalized
    review_tool._storage = get_storage(cfg)
    review_tool._captions_cache = review_tool.CaptionsCache(cfg.captions_path_abs)
    review_tool._captions_cache.refresh()

    client = review_tool.app.test_client()

    resp = client.get("/api/photos?limit=10")
    by_hash = {item["file_hash"]: item for item in resp.get_json()["items"]}
    assert by_hash["hash_with_thumb"]["has_thumbnail"] is True
    assert by_hash["hash_no_thumb"]["has_thumbnail"] is False
    print("  has_thumbnail field correctly reflects disk-cache presence  OK")

    resp = client.get("/thumbnail/hash_with_thumb")
    assert resp.status_code == 200
    assert resp.data == thumb_bytes
    assert resp.mimetype == "image/jpeg"
    print("  /thumbnail/<hash> serves the cached bytes for a processed video  OK")

    resp = client.get("/thumbnail/hash_no_thumb")
    assert resp.status_code == 404
    print("  /thumbnail/<hash> 404s gracefully for a not-yet-processed video (front end falls back to placeholder)  OK")

    resp = client.get("/thumbnail/hash_that_does_not_exist_at_all")
    assert resp.status_code == 404
    print("  /thumbnail/<hash> 404s for an unknown hash too  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase2g_"))
    print(f"Working in {tmp}")
    try:
        test_extract_first_frame(tmp)
        test_run_thumbnail_extraction(tmp)
        test_review_tool_thumbnail_route(tmp)
        print("\nALL PHASE 2G TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
