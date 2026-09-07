"""Exercises src/relocate.py (Phase 3b: resync folder placement with each
photo/video's current date_taken) against synthetic fixtures — real files
on disk under an isolated temp dest_root, an isolated temp SQLite DB, same
"never touch the real data/photo_organizer.db" posture as every other
test in this suite (test_date_audit.py/test_phase2b.py/etc.).

Covers:
  - find_relocation_candidates(): a row whose current folder disagrees
    with what Phase 1's own convention computes from its CURRENT
    date_taken_year/month is flagged; an already-correctly-placed row
    (photo or video, including the video 'Video' subfolder) is not; a row
    with date_taken IS NULL (still genuinely unsorted) is never a
    candidate and doesn't crash the scan.
  - apply_relocations(): actually moves the real file via copy-verify-
    delete (src/organize.py, reused not reimplemented) — old path gone,
    new path holds the identical bytes, DB's current_path/filename/
    file_size/file_mtime updated, but original_path/date_taken/
    date_source/status/processed_at/phase1_verified all UNCHANGED (this
    is a physical move, not a metadata edit, and doesn't go through
    photo_edits — confirmed no row is written there).
  - Collision handling at the destination reuses Phase 1's own hash-suffix
    naming (pick_available_name) — two different files that would
    otherwise land on the same destination filename both survive, neither
    overwrites the other.
  - Safety re-checks at apply time: a candidate that's already been fixed
    by something else since the preview was taken is skipped as a no-op
    (skipped_already_correct); a candidate whose source file has vanished
    since the preview is skipped, not a crash (skipped_missing_file); a
    candidate whose live file content no longer matches its recorded
    file_hash is skipped, not silently relocated under a stale identity
    (skipped_hash_mismatch).
  - stop_check()/progress_cb behave like every other batch op in this
    project — a stopped run leaves the remaining candidates untouched
    (still real candidates on a fresh preview afterward).

Usage:
    venv\\Scripts\\python tests\\test_relocate.py
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from src.db import connect, init_db
from src.hashing import hash_file
from src.relocate import apply_relocations, find_relocation_candidates

logging.basicConfig(level=logging.CRITICAL)  # keep test output focused on our own prints
LOGGER = logging.getLogger("test_relocate")


def _make_cfg(dest_root: Path, db_path: Path) -> Config:
    return Config(
        source_folders=[], dest_root=str(dest_root), supported_extensions=[".jpg", ".png"],
        unsorted_subfolder="_unsorted", dry_run=False, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(db_path), log_dir=str(dest_root / "logs"),
        video_extensions=[".mp4", ".mov"],
    )


def _connect(db_path: Path) -> sqlite3.Connection:
    init_db(db_path, logger=LOGGER)
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_photo(conn: sqlite3.Connection, file_hash: str, current_path: Path,
                   date_taken: str | None, filename: str | None = None) -> None:
    filename = filename or current_path.name
    year = int(date_taken[:4]) if date_taken else None
    month = int(date_taken[5:7]) if date_taken else None
    status = "sorted" if date_taken else "unsorted"
    conn.execute(
        """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
                                date_taken, date_taken_year, date_taken_month, date_source,
                                status, processed_at, phase1_verified)
           VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, '2026-01-01T00:00:00', 1)""",
        (file_hash, str(current_path), str(current_path), filename,
         date_taken, year, month, "exif" if date_taken else "unsorted", status),
    )
    conn.commit()


def _write(path: Path, content: bytes) -> str:
    """Writes real content to disk and returns its real sha256 hash — every
    row's file_hash in production IS the hash of its actual bytes, and
    apply_relocations() re-hashes the live file and checks it against the
    row's own file_hash before moving anything (see module docstring), so
    a test using a made-up hash string would trip that exact safety check
    rather than exercising the happy path it's meant to test."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hash_file(path)


def test_find_relocation_candidates(tmp: Path) -> None:
    print("test_find_relocation_candidates...")
    dest = tmp / "dest"
    conn = _connect(tmp / "test.db")
    cfg = _make_cfg(dest, tmp / "test.db")

    # Mismatch: dated 2020-05, but sitting in the wrong (2019-01) folder.
    mismatched = dest / "2019" / "2019-01" / "wrong.jpg"
    h_mismatched = _write(mismatched, b"mismatched-photo")
    _insert_photo(conn, h_mismatched, mismatched, "2020-05-15T10:00:00")

    # Already correct.
    correct = dest / "2021" / "2021-03" / "right.jpg"
    h_correct = _write(correct, b"correct-photo")
    _insert_photo(conn, h_correct, correct, "2021-03-10T08:00:00")

    # Video, mismatched — expected dir must include the Video subfolder.
    mismatched_video = dest / "2018" / "2018-02" / "clip.mp4"
    h_mismatched_video = _write(mismatched_video, b"mismatched-video")
    _insert_photo(conn, h_mismatched_video, mismatched_video, "2022-07-01T00:00:00")

    # Video, already correct (sitting under .../Video/).
    correct_video = dest / "2022" / "2022-07" / "Video" / "good.mp4"
    h_correct_video = _write(correct_video, b"correct-video")
    _insert_photo(conn, h_correct_video, correct_video, "2022-07-01T00:00:00")

    # Still genuinely unsorted (no date at all) — never a candidate.
    unsorted = dest / "_unsorted" / "mystery.jpg"
    h_unsorted = _write(unsorted, b"mystery")
    _insert_photo(conn, h_unsorted, unsorted, None)

    preview = find_relocation_candidates(conn, cfg)
    by_hash = {c.file_hash: c for c in preview.candidates}

    assert h_mismatched in by_hash
    assert by_hash[h_mismatched].expected_dir == str(dest / "2020" / "2020-05")
    assert h_mismatched_video in by_hash
    assert by_hash[h_mismatched_video].expected_dir == str(dest / "2022" / "2022-07" / "Video")
    assert by_hash[h_mismatched_video].is_video is True
    assert h_correct not in by_hash
    assert h_correct_video not in by_hash
    assert h_unsorted not in by_hash

    assert preview.total_dated_rows == 4  # every row with date_taken IS NOT NULL
    assert preview.already_correct_count == 2
    assert preview.no_date_rows == 1
    print(f"  {len(preview.candidates)} candidates, {preview.already_correct_count} already correct  OK")
    conn.close()


def test_apply_relocations_moves_file_and_updates_db(tmp: Path) -> None:
    print("test_apply_relocations_moves_file_and_updates_db...")
    dest = tmp / "dest"
    dbp = tmp / "test.db"
    conn = _connect(dbp)
    cfg = _make_cfg(dest, dbp)

    src = dest / "2019" / "2019-01" / "wrong.jpg"
    h = _write(src, b"real-photo-bytes")
    _insert_photo(conn, h, src, "2020-05-15T10:00:00")

    preview = find_relocation_candidates(conn, cfg)
    assert len(preview.candidates) == 1
    stats = apply_relocations(conn, preview.candidates, cfg, LOGGER)
    assert stats.relocated == 1 and stats.errors == 0
    print(f"  {stats.summary()}  OK")

    expected_path = dest / "2020" / "2020-05" / "wrong.jpg"
    assert expected_path.exists(), "file should now exist at its corrected location"
    assert expected_path.read_bytes() == b"real-photo-bytes"
    assert not src.exists(), "old location should no longer have the file"

    row = conn.execute("SELECT * FROM photos WHERE file_hash = ?", (h,)).fetchone()
    assert row["current_path"] == str(expected_path)
    assert row["filename"] == "wrong.jpg"
    assert row["file_size"] == len(b"real-photo-bytes")
    # Untouched -- this is a physical move, not a metadata edit.
    assert row["original_path"] == str(src)
    assert row["date_taken"] == "2020-05-15T10:00:00"
    assert row["date_source"] == "exif"
    assert row["status"] == "sorted"
    assert row["processed_at"] == "2026-01-01T00:00:00"
    assert row["phase1_verified"] == 1

    edit_rows = conn.execute("SELECT * FROM photo_edits WHERE file_hash = ?", (h,)).fetchall()
    assert edit_rows == [], "a physical relocation must NOT go through the photo_edits mechanism"
    print("  DB current_path/filename/file_size updated; original_path/date fields/status untouched; no photo_edits row  OK")

    # Re-preview: nothing left to do.
    preview2 = find_relocation_candidates(conn, cfg)
    assert preview2.candidates == []
    print("  re-preview finds nothing left to relocate  OK")
    conn.close()


def test_collision_handling(tmp: Path) -> None:
    print("test_collision_handling...")
    dest = tmp / "dest"
    dbp = tmp / "test.db"
    conn = _connect(dbp)
    cfg = _make_cfg(dest, dbp)

    # Two DIFFERENT files, both misdated into 2020-05, both named "img.jpg"
    # -- the destination folder would collide if collision handling didn't
    # kick in exactly the way Phase 1's own pick_available_name does.
    src_a = dest / "2019" / "2019-01" / "img.jpg"
    h_a = _write(src_a, b"content-A")
    _insert_photo(conn, h_a, src_a, "2020-05-01T00:00:00", filename="img.jpg")

    src_b = dest / "2019" / "2019-02" / "img.jpg"
    h_b = _write(src_b, b"content-B-different")
    _insert_photo(conn, h_b, src_b, "2020-05-02T00:00:00", filename="img.jpg")

    # A file already legitimately sitting at the destination under the
    # wanted name, unrelated to either of the above (forces both movers to
    # need a collision suffix, not just the second one).
    already_there = dest / "2020" / "2020-05" / "img.jpg"
    _write(already_there, b"pre-existing-unrelated-file")

    preview = find_relocation_candidates(conn, cfg)
    assert len(preview.candidates) == 2
    stats = apply_relocations(conn, preview.candidates, cfg, LOGGER)
    assert stats.relocated == 2 and stats.errors == 0

    assert already_there.read_bytes() == b"pre-existing-unrelated-file", "pre-existing file must never be overwritten"
    dest_dir = dest / "2020" / "2020-05"
    all_bytes = {p.read_bytes() for p in dest_dir.iterdir() if p.is_file()}
    assert all_bytes == {b"pre-existing-unrelated-file", b"content-A", b"content-B-different"}, \
        "all three distinct files must survive under distinct names"
    print("  collision-safe: pre-existing file untouched, both movers renamed distinctly, no overwrite  OK")
    conn.close()


def test_apply_time_safety_checks(tmp: Path) -> None:
    print("test_apply_time_safety_checks...")
    dest = tmp / "dest"
    dbp = tmp / "test.db"
    conn = _connect(dbp)
    cfg = _make_cfg(dest, dbp)

    # Case 1: fixed by something else between preview and apply.
    fixed_between = dest / "2019" / "2019-01" / "a.jpg"
    h_fixed = _write(fixed_between, b"a-bytes")
    _insert_photo(conn, h_fixed, fixed_between, "2020-05-01T00:00:00")

    # Case 2: file vanishes between preview and apply.
    vanishing = dest / "2019" / "2019-01" / "b.jpg"
    h_vanish = _write(vanishing, b"b-bytes")
    _insert_photo(conn, h_vanish, vanishing, "2020-06-01T00:00:00")

    # Case 3: live file content no longer matches its recorded identity.
    changed = dest / "2019" / "2019-01" / "c.jpg"
    h_changed = _write(changed, b"original-content")
    _insert_photo(conn, h_changed, changed, "2020-07-01T00:00:00")

    preview = find_relocation_candidates(conn, cfg)
    assert len(preview.candidates) == 3

    # Simulate each real-world race condition before actually applying.
    real_dest = dest / "2020" / "2020-05" / "a.jpg"
    real_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(fixed_between), str(real_dest))
    conn.execute("UPDATE photos SET current_path = ? WHERE file_hash = ?", (str(real_dest), h_fixed))
    conn.commit()

    vanishing.unlink()

    changed.write_bytes(b"someone-modified-this-file-since-recording")

    stats = apply_relocations(conn, preview.candidates, cfg, LOGGER)
    assert stats.relocated == 0
    assert stats.skipped_already_correct == 1
    assert stats.skipped_missing_file == 1
    assert stats.skipped_hash_mismatch == 1
    assert stats.errors == 0
    print(f"  {stats.summary()}  OK")

    # The changed file must be left exactly as found -- never relocated
    # under a stale/mismatched identity.
    assert changed.exists()
    assert changed.read_bytes() == b"someone-modified-this-file-since-recording"
    conn.close()


def test_stop_check_leaves_remainder_untouched(tmp: Path) -> None:
    print("test_stop_check_leaves_remainder_untouched...")
    dest = tmp / "dest"
    dbp = tmp / "test.db"
    conn = _connect(dbp)
    cfg = _make_cfg(dest, dbp)

    for i in range(5):
        p = dest / "2019" / "2019-01" / f"f{i}.jpg"
        h = _write(p, f"content-{i}".encode())
        _insert_photo(conn, h, p, "2020-05-01T00:00:00")

    preview = find_relocation_candidates(conn, cfg)
    assert len(preview.candidates) == 5

    calls = {"n": 0}

    def stop_after_two(done: int, total: int) -> None:
        calls["n"] = done

    stats = apply_relocations(
        conn, preview.candidates, cfg, LOGGER,
        progress_cb=stop_after_two,
        stop_check=lambda: calls["n"] >= 2,
    )
    assert stats.relocated == 2, f"expected exactly 2 relocated before stopping, got {stats.relocated}"

    preview_after = find_relocation_candidates(conn, cfg)
    assert len(preview_after.candidates) == 3, "the 3 un-processed candidates must still be real candidates"
    print(f"  stopped after {stats.relocated}/5, remaining 3 still real candidates on re-preview  OK")
    conn.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_relocate_"))
    print(f"Working in {tmp}")
    try:
        test_find_relocation_candidates(tmp / "find")
        test_apply_relocations_moves_file_and_updates_db(tmp / "apply")
        test_collision_handling(tmp / "collision")
        test_apply_time_safety_checks(tmp / "safety")
        test_stop_check_leaves_remainder_untouched(tmp / "stop")
        print("\nALL RELOCATE TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
