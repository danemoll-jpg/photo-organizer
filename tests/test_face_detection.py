"""Exercises Phase 3 Session 1's src/face_detection.py against synthetic
fixtures in an isolated temp dir -- never touches config.yaml, the real DB,
real photos, or anything under E:\\Pics (same posture as
tests/test_phase2b.py/test_phase2g.py).

`run_face_detection()` is tested end to end with a FAKE FaceAnalysis `app`
(a tiny stand-in returning deterministic fake Face objects keyed by
filename -- see _FakeApp/_FakeFace below), not a real insightface model.
This is deliberate, not a shortcut around real verification: loading the
real buffalo_l model pack costs several seconds and ~280MB on disk, and
GPU-provider behavior is a real-hardware/real-Windows-installation concern
that doesn't belong in a routine automated test anyway. The REAL model
(real GPU inference, real CUDAExecutionProvider confirmation, real
throughput) was verified manually this session against real library
photos -- see CLAUDE.md's Environment/Project-state notes and TODO.md's
Phase 3 Session 1 entry for that record. What this file DOES verify for
real: photo/video row filtering (including the exact AND/OR-precedence
trap thumbnail_backfill.py's own tests caught for the video-inclusion
clause -- here it's the inverse, video-exclusion), the two-file
(faces.jsonl / faces_scanned.jsonl) resumability design actually working
across a second run, folder_prefix (--limit), stop_check (Cancel), and
_load_image_bgr()'s real BGR/RGB channel-order correctness against a real
(tiny, synthetic) JPEG decoded by Pillow.

Usage:
    venv\\Scripts\\python tests\\test_face_detection.py
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from src.db import connect, init_db
from src.face_detection import FaceDetectionStats, _load_image_bgr, run_face_detection


class _FakeFace:
    def __init__(self, bbox, embedding, det_score):
        self.bbox = np.array(bbox, dtype=np.float32)
        self.normed_embedding = np.array(embedding, dtype=np.float32)
        self.det_score = np.float32(det_score)


class _FakeApp:
    """Stands in for a real insightface FaceAnalysis app -- app.get(img_bgr)
    is the only method run_face_detection() actually calls. Returns a
    per-photo fixed face list keyed by a marker byte value baked into the
    fake image's first pixel (see _make_synthetic_photo), so different
    fixture photos can deterministically produce 0/1/many faces without
    needing any real face-detection to run."""

    def get(self, img_bgr: np.ndarray):
        marker = int(img_bgr[0, 0, 0])  # blue channel of pixel (0,0) -- see _make_synthetic_photo
        if marker == 0:
            return []
        if marker == 1:
            return [_FakeFace([10, 10, 50, 50], [0.1] * 512, 0.9)]
        return [
            _FakeFace([10, 10, 50, 50], [0.1] * 512, 0.9),
            _FakeFace([60, 60, 100, 100], [0.2] * 512, 0.8),
        ]


def _make_synthetic_photo(path: Path, marker: int, size=(20, 16)) -> None:
    """A real, genuinely decodable PNG (lossless -- deliberately NOT JPEG:
    JPEG's block-based DCT compression smears a single-pixel edit across an
    8x8 block on a near-uniform image, silently corrupting the exact marker
    value this fixture depends on). `marker` (0/1/2+) is encoded into pixel
    (0,0)'s blue channel so _FakeApp.get() can decide how many fake faces
    to "detect" per fixture photo without any real face-detection running.
    Real JPEG/PNG/HEIC decoding correctness was already verified for real
    against real library photos this session (see CLAUDE.md) -- this
    fixture only needs an exact, lossless round-trip."""
    w, h = size
    arr = np.full((h, w, 3), 100, dtype=np.uint8)
    arr[0, 0] = [200, 50, marker]  # (R, G, B) -- PIL saves as RGB
    Image.fromarray(arr, mode="RGB").save(path, "PNG")


def test_load_image_bgr(tmp: Path) -> None:
    print("\n=== _load_image_bgr() real BGR/RGB channel-order check ===")
    p = tmp / "channel_check.jpg"
    arr = np.full((10, 10, 3), 0, dtype=np.uint8)
    arr[:, :] = [200, 20, 10]  # RGB -- distinctive, asymmetric across channels
    Image.fromarray(arr, mode="RGB").save(p, "JPEG")

    bgr = _load_image_bgr(p)
    assert bgr.shape == (10, 10, 3)
    # Source pixel was (R,G,B) = (200,20,10). After a real RGB->BGR
    # conversion, position 0 holds the original (small) B value and
    # position 2 holds the original (large) R value -- i.e. the channel
    # axis was actually reversed, not left alone (which would still show
    # position 0 as the large value).
    b, g, r = [int(x) for x in bgr[5, 5]]
    assert b < r, f"expected small B / large R after RGB->BGR conversion (source was R=200,B=10), got BGR={bgr[5, 5]}"
    print(f"  real JPEG round-trip -> BGR={bgr[5, 5]} (small-B/large-R confirms RGB->BGR conversion happened)  OK")


def _make_cfg(tmp: Path, dbp: Path, faces_dir: Path) -> Config:
    return Config(
        source_folders=[], dest_root=str(tmp), supported_extensions=[".jpg", ".jpeg", ".png", ".heic"],
        unsorted_subfolder="_unsorted", dry_run=True, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(dbp), log_dir=str(tmp / "logs"),
        video_extensions=[".mp4", ".mov", ".avi"],
        faces_path=str(faces_dir / "faces.jsonl"), faces_scanned_path=str(faces_dir / "faces_scanned.jsonl"),
    )


def test_run_face_detection(tmp: Path) -> None:
    print("\n=== Batch detection loop (src/face_detection.py::run_face_detection) ===")
    no_face_photo = tmp / "no_face.png"
    _make_synthetic_photo(no_face_photo, marker=0)
    one_face_photo = tmp / "one_face.png"
    _make_synthetic_photo(one_face_photo, marker=1)
    two_face_photo = tmp / "two_faces.png"
    _make_synthetic_photo(two_face_photo, marker=2)
    other_folder_photo = tmp / "other_folder" / "another.png"
    other_folder_photo.parent.mkdir(parents=True)
    _make_synthetic_photo(other_folder_photo, marker=1)
    video = tmp / "clip.mp4"  # must NEVER be scanned -- photos only, per Phase 2's scope precedent
    video.write_bytes(b"FAKE-MP4-BYTES")

    dbp = tmp / "faces_test.db"
    faces_dir = tmp / "faces_out"
    logger = logging.getLogger("face_detection_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
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

    insert("hash_no_face", no_face_photo)
    insert("hash_one_face", one_face_photo)
    insert("hash_two_faces", two_face_photo)
    insert("hash_other_folder", other_folder_photo)
    insert("hash_video", video)  # must be excluded from scanning entirely

    cfg = _make_cfg(tmp, dbp, faces_dir)
    fake_app = _FakeApp()

    stats = run_face_detection(cfg, conn, logger, cfg.faces_path_abs, cfg.faces_scanned_path_abs, app=fake_app)
    assert stats.scanned == 4, f"expected only the 4 photo rows (video excluded) to be scanned, got {stats.summary()}"
    # no_face(0) + one_face(1) + two_faces(2) + other_folder(1, same marker as one_face) = 0+1+2+1 = 4
    assert stats.faces_found == 4, stats.summary()
    assert stats.photos_with_faces == 3 and stats.photos_with_no_faces == 1, stats.summary()
    print(f"  first run: {stats.summary()}")

    faces_lines = [json.loads(l) for l in open(cfg.faces_path_abs, encoding="utf-8") if l.strip()]
    scanned_lines = [json.loads(l) for l in open(cfg.faces_scanned_path_abs, encoding="utf-8") if l.strip()]
    assert len(scanned_lines) == 4, "one scanned-checkpoint line per PHOTO scanned, including the 0-face one"
    scanned_hashes = {r["file_hash"] for r in scanned_lines}
    assert scanned_hashes == {"hash_no_face", "hash_one_face", "hash_two_faces", "hash_other_folder"}
    assert "hash_video" not in scanned_hashes, "video row must never be scanned at all -- photos only"
    print("  faces_scanned.jsonl has exactly one line per photo (including the 0-face one), video correctly absent  OK")

    by_hash_face_count = {}
    for r in faces_lines:
        by_hash_face_count[r["file_hash"]] = by_hash_face_count.get(r["file_hash"], 0) + 1
    assert "hash_no_face" not in by_hash_face_count, "a 0-face photo must contribute ZERO lines to faces.jsonl"
    assert by_hash_face_count["hash_one_face"] == 1
    assert by_hash_face_count["hash_two_faces"] == 2
    assert by_hash_face_count["hash_other_folder"] == 1
    print("  faces.jsonl has one line per DETECTED FACE, not per photo (0/1/many all correct)  OK")

    sample = next(r for r in faces_lines if r["file_hash"] == "hash_two_faces")
    assert set(sample.keys()) == {"file_hash", "path", "bbox", "embedding", "det_score", "detected_at"}
    assert len(sample["embedding"]) == 512
    assert len(sample["bbox"]) == 4
    print(f"  face record shape correct: {sorted(sample.keys())}, embedding len={len(sample['embedding'])}  OK")

    # --- Resumability: a second run must not re-scan any already-scanned photo ---
    stats2 = run_face_detection(cfg, conn, logger, cfg.faces_path_abs, cfg.faces_scanned_path_abs, app=fake_app)
    assert stats2.scanned == 4
    assert stats2.already_scanned == 4, "every photo was already scanned last run -- must all be skipped"
    assert stats2.faces_found == 0, "no NEW faces should be written on a pure resume"
    faces_lines2 = [l for l in open(cfg.faces_path_abs, encoding="utf-8") if l.strip()]
    assert len(faces_lines2) == len(faces_lines), "resumed run must not duplicate any face line"
    print(f"  second run: {stats2.summary()}  OK (fully resumable, no duplicate lines written)")

    # --- folder_prefix (--limit) ---
    faces_dir3 = tmp / "faces_out_limit"
    cfg3 = _make_cfg(tmp, dbp, faces_dir3)
    stats3 = run_face_detection(
        cfg3, conn, logger, cfg3.faces_path_abs, cfg3.faces_scanned_path_abs,
        app=fake_app, folder_prefix=str(other_folder_photo.parent),
    )
    assert stats3.scanned == 1, f"expected --limit to restrict to just the one photo under other_folder, got {stats3.summary()}"
    print("  folder_prefix correctly restricts to a single subfolder  OK")

    # --- stop_check (Cancel button) ---
    faces_dir4 = tmp / "faces_out_stop"
    cfg4 = _make_cfg(tmp, dbp, faces_dir4)
    calls = {"n": 0}

    def stop_after_one():
        calls["n"] += 1
        return calls["n"] > 1

    stats4 = run_face_detection(
        cfg4, conn, logger, cfg4.faces_path_abs, cfg4.faces_scanned_path_abs,
        app=fake_app, stop_check=stop_after_one,
    )
    assert stats4.scanned == 1, f"expected stop_check to halt after the first photo, got {stats4.summary()}"
    print("  stop_check halts the run early, mid-scan  OK")

    conn.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase3_"))
    print(f"Working in {tmp}")
    try:
        test_load_image_bgr(tmp)
        test_run_face_detection(tmp)
        print("\nALL PHASE 3 SESSION 1 TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
