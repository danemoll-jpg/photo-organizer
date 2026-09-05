"""Exercises Phase 2's mechanics (JSONL schema, resume-by-hash, the
path+size+mtime fast-path, checkpointing, and video-exclusion) against a
small synthetic photo library in an isolated temp dir -- never touches
config.yaml, the real DB, or anything under E:\\Pics (see CLAUDE.md / this
session's scope note).

This is an integration test, not a mock-based unit test: it hits the real,
already-installed local Ollama server, same posture as Phase 1b's decision
to verify hachoir's "container metadata found" step against real files
rather than fake it (see tests/make_sample_library.py's docstring) -- the
whole point of Phase 2 is what a real model actually returns, so a mocked
client would prove nothing about the part most likely to break.

Requires: Ollama running locally with the configured model already pulled
(see README.md / CLAUDE.md's Phase 2 section).

Usage:
    venv\\Scripts\\python tests\\test_phase2_pipeline.py [model_name]
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

import piexif
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.caption import run_phase2
from src.config import Config

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None


def _cfg(dest_root: Path, captions_path: Path, model: str, batch_size: int = 2) -> Config:
    return Config(
        source_folders=[],
        dest_root=str(dest_root),
        supported_extensions=[".jpg", ".jpeg", ".png", ".heic"],
        unsorted_subfolder="_unsorted\\needs_review",
        dry_run=True,  # irrelevant to Phase 2 -- required field only
        hash_algorithm="sha256",
        collision_suffix_length=8,
        db_path=str(dest_root / "unused.db"),
        log_dir=str(dest_root / "logs"),
        ollama_model=model,
        captions_path=str(captions_path),
        caption_max_dimension=512,
        batch_size=batch_size,
    )


def build_library(root: Path) -> Path:
    if root.exists():
        shutil.rmtree(root)
    dest = root / "dest_pics"
    (dest / "2020" / "2020-07").mkdir(parents=True)
    (dest / "2020" / "2020-07" / "Video").mkdir(parents=True)

    def jpeg(path: Path, color, dt=None) -> None:
        img = Image.new("RGB", (60, 60), color)
        if dt:
            img.save(path, "jpeg", exif=piexif.dump({"Exif": {piexif.ExifIFD.DateTimeOriginal: dt.encode()}}))
        else:
            img.save(path, "jpeg")

    jpeg(dest / "2020" / "2020-07" / "beach_day.jpg", (30, 140, 220), "2020:07:04 15:00:00")
    Image.new("RGB", (60, 60), (20, 200, 40)).save(dest / "2020" / "2020-07" / "backyard.png", "png")
    jpeg(dest / "loose_photo.jpg", (200, 60, 60))

    if pillow_heif:
        img = Image.new("RGB", (60, 60), (180, 180, 20))
        img.save(dest / "2020" / "2020-07" / "sunset.heic", format="HEIF",
                  exif=piexif.dump({"Exif": {piexif.ExifIFD.DateTimeOriginal: b"2020:07:05 19:30:00"}}))

    # Video sitting right alongside photos -- must NOT be captioned (Phase 2
    # is photo-only, see photo-organizer-spec.md).
    (dest / "2020" / "2020-07" / "Video" / "clip.mp4").write_bytes(b"FAKE-MP4-NOT-A-REAL-CONTAINER" * 20)

    return dest


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen3-vl:2b"
    tmp_root = Path(tempfile.gettempdir()) / "photo_organizer_phase2_test"
    dest = build_library(tmp_root)
    captions_path = tmp_root / "captions.jsonl"
    cfg = _cfg(dest, captions_path, model)

    logger = logging.getLogger("phase2_test")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(sys.stdout))

    expected_photo_count = 4 if pillow_heif else 3  # beach_day, backyard, loose_photo, [sunset.heic]

    print(f"=== Run 1: fresh library ({expected_photo_count} photos + 1 video) ===")
    progress_calls = []
    t0 = time.time()
    stats1 = run_phase2(cfg, logger, progress_cb=lambda d, t: progress_calls.append((d, t)))
    elapsed1 = time.time() - t0
    print(f"stats: {stats1.summary()}  (elapsed {elapsed1:.1f}s)")

    assert stats1.scanned == expected_photo_count, f"expected to scan {expected_photo_count} photos (video excluded), got {stats1.scanned}"
    assert stats1.captioned == expected_photo_count, f"expected {expected_photo_count} newly captioned, got {stats1.captioned}"
    assert stats1.already_captioned == 0
    assert progress_calls, "progress_cb was never called"
    assert progress_calls[-1] == (expected_photo_count, expected_photo_count)

    records = _read_jsonl(captions_path)
    assert len(records) == expected_photo_count, f"expected {expected_photo_count} JSONL lines, got {len(records)}"
    required_fields = {"file_hash", "path", "caption", "tags", "date_taken", "model_used", "processed_at"}
    for r in records:
        missing = required_fields - r.keys()
        assert not missing, f"record missing required field(s) {missing}: {r}"
        assert isinstance(r["caption"], str) and r["caption"], f"empty/non-string caption: {r}"
        assert isinstance(r["tags"], list) and all(isinstance(t, str) for t in r["tags"]), f"bad tags: {r}"
        assert r["model_used"] == model
        assert "clip.mp4" not in r["path"], "video file was captioned -- Phase 2 must be photo-only"
    # the EXIF-dated file should resolve a real date_taken, not None
    beach = next(r for r in records if "beach_day" in r["path"])
    assert beach["date_taken"] and beach["date_taken"].startswith("2020-07-04"), f"bad date_taken: {beach}"
    print(f"All {len(records)} records have the required fields; sample caption: {records[0]['caption']!r} tags={records[0]['tags']}")

    print("\n=== Run 2: re-run unchanged -- should be a pure resume (fast-path, no re-hashing/no re-captioning) ===")
    stats2 = run_phase2(cfg, logger)
    print(f"stats: {stats2.summary()}")
    assert stats2.captioned == 0, "re-run should not re-caption unchanged files"
    assert stats2.already_captioned == expected_photo_count, "re-run should recognize every file via the resume fast-path"
    records2 = _read_jsonl(captions_path)
    assert len(records2) == expected_photo_count, "re-run must not duplicate JSONL records"

    print("\n=== Run 3: change one file's content -- only that file should be recaptioned ===")
    changed_path = dest / "loose_photo.jpg"
    Image.new("RGB", (60, 60), (5, 5, 5)).save(changed_path, "jpeg")  # different content -> different hash
    stats3 = run_phase2(cfg, logger)
    print(f"stats: {stats3.summary()}")
    assert stats3.captioned == 1, f"expected exactly 1 recaptioned file after content change, got {stats3.captioned}"
    assert stats3.already_captioned == expected_photo_count - 1
    records3 = _read_jsonl(captions_path)
    assert len(records3) == expected_photo_count + 1, "changed file should get a NEW record (old one left as-is, append-only JSONL)"

    print("\n=== Corrupt trailing line handling ===")
    with open(captions_path, "a", encoding="utf-8") as f:
        f.write("{not valid json\n")
    stats4 = run_phase2(cfg, logger)
    print(f"stats: {stats4.summary()}")
    assert stats4.captioned == 0 and stats4.errors == 0, "a corrupt trailing JSONL line must not break resume or crash the run"

    print("\nALL PHASE 2 PIPELINE CHECKS PASSED")
    print(f"(cleanup: rmdir /s /q \"{tmp_root}\" to remove the isolated test fixtures)")


if __name__ == "__main__":
    main()
