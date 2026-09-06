"""Regression test for the "photos display upside down/rotated in
review_tool.py" fix (see CLAUDE.md/TODO.md).

Many phones/cameras store pixel data in a fixed sensor orientation and
record the intended display rotation in the EXIF `Orientation` tag rather
than physically rotating pixels. review_tool.py's `_get_image_bytes()`
(shared by both the grid thumbnail route and the full-size viewer route --
both hit `/image/<hash>`, just with a different `max` query param) now
calls `PIL.ImageOps.exif_transpose()` before resizing/encoding.

This test builds a synthetic JPEG with a non-1 EXIF Orientation tag and a
two-color pattern (so both the dimension swap AND the actual rotation
*direction* are checked, not just "the size changed") and confirms:
  1. `/image/<hash>` returns pixel data already rotated upright, for both
     the small (grid) and large (viewer) `max` values -- i.e. the single
     shared function fixes both call sites, not just one.
  2. A normal (orientation=1) photo is served byte-for-byte-equivalent in
     shape/content -- unaffected by the fix.
  3. The original file on disk is untouched (identical bytes) after being
     served -- this must be a purely serving-time transform, never a
     write-back to the source file.

Usage:
    venv\\Scripts\\python tests\\test_orientation_fix.py
"""
from __future__ import annotations

import hashlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

import piexif
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from src.db import connect, init_db
from src.storage import get_storage


def _make_two_color_jpeg(path: Path, orientation: int | None) -> None:
    """A 40x20 (landscape) raw image: left half red, right half blue. With
    orientation=6 (the most common real-world case -- phone held rotated
    90 deg CW from "normal" -- confirmed against real examples in the
    user's own library during this fix), the correct upright display is a
    20x40 portrait image. Left as an exercise for exif_transpose() to prove
    it's actually wired up, not just that *some* size changed."""
    img = Image.new("RGB", (40, 20), (0, 0, 0))
    for x in range(40):
        for y in range(20):
            img.putpixel((x, y), (200, 0, 0) if x < 20 else (0, 0, 200))
    exif_bytes = None
    if orientation is not None:
        exif_bytes = piexif.dump({"0th": {piexif.ImageIFD.Orientation: orientation}})
    if exif_bytes:
        img.save(path, "jpeg", quality=95, exif=exif_bytes)
    else:
        img.save(path, "jpeg", quality=95)


def _avg_color(img: Image.Image, box) -> tuple[int, int, int]:
    # Downscaling a crop to 1x1 makes PIL average every pixel in the region
    # for us -- avoids the deprecated Image.getdata() API for the same result.
    return img.crop(box).resize((1, 1), Image.BOX).getpixel((0, 0))


def test_image_orientation(tmp: Path) -> None:
    print("\n=== review_tool.py /image/<hash> applies EXIF orientation (grid + viewer) ===")
    import review_tool

    dest = tmp / "dest"
    (dest / "2020" / "2020-07").mkdir(parents=True)

    rotated_path = dest / "2020" / "2020-07" / "rotated.jpg"
    normal_path = dest / "2020" / "2020-07" / "normal.jpg"
    _make_two_color_jpeg(rotated_path, orientation=6)
    _make_two_color_jpeg(normal_path, orientation=1)

    rotated_bytes_before = rotated_path.read_bytes()
    normal_bytes_before = normal_path.read_bytes()

    dbp = tmp / "orientation.db"
    import logging
    logger = logging.getLogger("orientation_fix_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)
    for h, p in (("hash_rotated", rotated_path), ("hash_normal", normal_path)):
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at,
               phase1_verified) VALUES (?, ?, ?, ?, ?, 0, '2020-07-01T00:00:00', 2020, 7, 'exif', 'sorted',
               '2020-07-01', 1)""",
            (h, str(p), str(p), p.name, p.stat().st_size),
        )
    conn.commit()
    conn.close()

    cfg = Config(
        source_folders=[], dest_root=str(dest), supported_extensions=[".jpg"],
        unsorted_subfolder="_unsorted", dry_run=True, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(dbp), log_dir=str(tmp / "logs"),
        captions_path=str(tmp / "captions.jsonl"), video_extensions=[".mp4", ".mov", ".avi"],
    )
    review_tool._cfg = cfg
    review_tool._video_exts = cfg.video_extensions_normalized
    review_tool._storage = get_storage(cfg)
    review_tool._captions_cache = review_tool.CaptionsCache(cfg.captions_path_abs)
    review_tool._captions_cache.refresh()
    # Bypass the image-bytes cache between the two `max` requests below --
    # it's keyed by (hash, max) so this isn't strictly required, but keeps
    # this test isolated from cache state if it's ever re-run in-process.
    review_tool._image_cache.clear()
    review_tool._image_cache_order.clear()

    client = review_tool.app.test_client()

    # --- rotated photo: both the small (grid) and large (viewer) `max`
    # values must come back upright, proving the ONE shared function fixes
    # both call sites (see static/review.js: grid uses ?max=400, viewer
    # uses ?max=1800) ---
    for max_dim, label in ((400, "grid-sized"), (1800, "viewer-sized")):
        resp = client.get(f"/image/hash_rotated?max={max_dim}")
        assert resp.status_code == 200
        img = Image.open(io.BytesIO(resp.data))
        assert img.size == (20, 40), (
            f"{label} request: expected upright portrait (20, 40) after exif_transpose, got {img.size}"
        )
        # Direction check, not just dimension swap: for orientation=6, the
        # raw left-half-red/right-half-blue image must become top-half-red/
        # bottom-half-blue once upright (red was closer to the "up" edge
        # when the phone was rotated 90 deg CW to take the raw shot).
        top = _avg_color(img, (0, 0, 20, 20))
        bottom = _avg_color(img, (0, 20, 20, 40))
        assert top[0] > 100 and top[2] < 100, f"{label}: top region should be red-dominant, got {top}"
        assert bottom[2] > 100 and bottom[0] < 100, f"{label}: bottom region should be blue-dominant, got {bottom}"
    print("  rotated (orientation=6) photo served upright at both grid and viewer sizes  OK")

    # --- normal (orientation=1) photo: unaffected, still landscape, still
    # left-red/right-blue exactly as stored ---
    resp = client.get("/image/hash_normal?max=400")
    img = Image.open(io.BytesIO(resp.data))
    assert img.size == (40, 20), f"orientation=1 photo must be unaffected, got {img.size}"
    left = _avg_color(img, (0, 0, 20, 20))
    right = _avg_color(img, (20, 0, 40, 20))
    assert left[0] > 100 and left[2] < 100, f"normal photo: left region should still be red, got {left}"
    assert right[2] > 100 and right[0] < 100, f"normal photo: right region should still be blue, got {right}"
    print("  normal (orientation=1) photo served unchanged  OK")

    # --- the original files on disk must be byte-for-byte untouched ---
    assert rotated_path.read_bytes() == rotated_bytes_before, "serving must never modify the original file"
    assert normal_path.read_bytes() == normal_bytes_before, "serving must never modify the original file"
    assert hashlib.sha256(rotated_path.read_bytes()).hexdigest() == hashlib.sha256(rotated_bytes_before).hexdigest()
    print("  original files on disk untouched (byte-for-byte) after being served  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_orientation_"))
    print(f"Working in {tmp}")
    try:
        test_image_orientation(tmp)
        print("\nALL ORIENTATION FIX TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
