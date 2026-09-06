"""Exercises src/date_audit.py against synthetic fixtures in an isolated
temp SQLite DB (never touches the real data/photo_organizer.db) --
same posture as test_phase2b.py/test_phase2c.py/test_phase2f.py.

Covers:
  - find_suspicious_clusters(): day-level grouping (not exact-timestamp),
    min_count filtering, sort order, source_counts/filesystem_fraction.
  - find_year_month_in_parts(): the "does original_path encode a YYYY\\MM
    or YYYY\\YYYY-MM folder" parser, including it correctly NOT matching
    an unrelated 4-digit path component (e.g. a "2024" that isn't a year
    folder followed by a real month).
  - find_fix_candidates(): the one root-cause pattern this module
    auto-fixes (original folder disagrees with a filesystem-fallback
    date), vs. the two patterns it must NOT touch -- no recoverable
    signal at all, and a photo a human already manually corrected via
    Phase 2f's photo_edits (has_manual_edit) -- confirmed BOTH are
    correctly excluded from candidates.
  - apply_fixes(): writes land through src.edits.apply_edit (so
    photo_edits gets a real audit row and date_source flips to 'manual',
    permanently taking the row out of future find_fix_candidates() runs),
    a manual edit made *between* preview and apply is still respected
    (re-checked at apply time, not just relying on the preview snapshot),
    and stop_check()/progress_cb behave like every other batch op in this
    project.
  - find_sequence_interpolation_candidates(): the human-directed
    "I recognize this batch as this other, already-correctly-dated
    folder" pattern -- exact camera-roll-number matches, interpolation
    between two bracketing reference numbers, and refusing to extrapolate
    past the reference set's own number range (reported, not guessed at).
    Modeled on a real production case (a Feb 2020 vacation misdated as
    2023-10-29 -- see CLAUDE.md).

Usage:
    venv\\Scripts\\python tests\\test_date_audit.py
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.date_audit import (
    SEQUENCE_FIX_EDITOR,
    apply_fixes,
    find_fix_candidates,
    find_sequence_interpolation_candidates,
    find_suspicious_clusters,
    find_year_month_in_parts,
)
from src.db import init_db
from src.edits import apply_edit, has_manual_edit

logging.basicConfig(level=logging.CRITICAL)  # keep test output focused on our own prints
LOGGER = logging.getLogger("test_date_audit")


def _connect(tmp: Path) -> sqlite3.Connection:
    db_path = tmp / "test.db"
    init_db(db_path, logger=LOGGER)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_photo(conn: sqlite3.Connection, file_hash: str, current_path: str, original_path: str,
                   date_taken: str, date_source: str) -> None:
    conn.execute(
        """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size,
                                date_taken, date_taken_year, date_taken_month, date_source,
                                status, processed_at, phase1_verified)
           VALUES (?, ?, ?, ?, 1000, ?, ?, ?, ?, 'sorted', '2026-01-01T00:00:00', 1)""",
        (file_hash, current_path, original_path, Path(current_path).name,
         date_taken, int(date_taken[:4]), int(date_taken[5:7]), date_source),
    )
    conn.commit()


def test_find_year_month_in_parts() -> None:
    print("test_find_year_month_in_parts...")
    assert find_year_month_in_parts(Path(r"E:\Pics\2012\12\Portrait Studio\10C.jpg").parts) == (2012, 12)
    assert find_year_month_in_parts(Path(r"E:\Pics\2012\2012-12\10C.jpg").parts) == (2012, 12)
    # Real early-library convention found by cross-referencing the user's
    # pre-Phase-1 backup drive this session: "YYYY-MM (MonthName)" --
    # 3,229 real rows used exactly this pattern and were wrongly bucketed
    # as "no recoverable signal" before this was added.
    assert find_year_month_in_parts(Path(r"E:\Pics\2001\2001-02 (Feb)\Second Ukraine Trip 001.jpg").parts) == (2001, 2)
    assert find_year_month_in_parts(Path(r"J:\2006\2006-07 (Jul)\photo.jpg").parts) == (2006, 7)
    # A 3+ digit suffix right after the month digits must NOT false-match
    # (e.g. a hypothetical "2001-023" folder that isn't really month "02").
    assert find_year_month_in_parts(Path(r"E:\Pics\2001\2001-023\photo.jpg").parts) is None
    # "katya 2024" is a folder NAME, not a YYYY component on its own (it's
    # one path segment, "katya 2024", not "2024") -- must not false-match.
    assert find_year_month_in_parts(Path(r"E:\katya 2024\IMG_8418.JPG").parts) is None
    # A bare year with no following month-shaped component doesn't match.
    assert find_year_month_in_parts(Path(r"E:\Pics\2020\Vacation\IMG_1.JPG").parts) is None
    # Out-of-range "month" (13) must not match.
    assert find_year_month_in_parts(Path(r"E:\Pics\2020\13\IMG_1.JPG").parts) is None
    # Out-of-range "year" must not match.
    assert find_year_month_in_parts(Path(r"E:\Pics\1899\05\IMG_1.JPG").parts) is None
    print("  OK")


def test_find_suspicious_clusters(tmp: Path) -> None:
    print("test_find_suspicious_clusters...")
    conn = _connect(tmp)

    # A big same-day artifact cluster, all filesystem-sourced.
    for i in range(25):
        _insert_photo(conn, f"bad{i}", rf"E:\Pics\2023\2023-10\IMG_{i}.JPG",
                      r"E:\dump_folder\IMG_x.JPG", f"2023-10-29T13:{i:02d}:00", "filesystem")
    # A smaller, legitimately exif-sourced day (a real event) -- should
    # rank below the bigger cluster and show 0% filesystem.
    for i in range(15):
        _insert_photo(conn, f"good{i}", rf"E:\Pics\2020\2020-06\IMG_{i}.JPG",
                      rf"E:\Pics\2020\2020-06\IMG_{i}.JPG", f"2020-06-15T{9+i:02d}:00:00", "exif")
    # Below min_count -- must not appear in a min_count=10 query.
    for i in range(3):
        _insert_photo(conn, f"tiny{i}", rf"E:\Pics\2019\2019-01\IMG_{i}.JPG",
                      rf"E:\Pics\2019\2019-01\IMG_{i}.JPG", f"2019-01-05T10:0{i}:00", "exif")

    clusters = find_suspicious_clusters(conn, min_count=10, top_n=10)
    assert [c.date for c in clusters] == ["2023-10-29", "2020-06-15"], clusters
    top = clusters[0]
    assert top.count == 25
    assert top.filesystem_fraction == 1.0
    second = clusters[1]
    assert second.filesystem_fraction == 0.0
    assert all(c.date != "2019-01-05" for c in clusters), "below min_count must be excluded"
    print(f"  top cluster: {top.date} count={top.count} fs_fraction={top.filesystem_fraction}  OK")
    conn.close()


def test_find_fix_candidates(tmp: Path) -> None:
    print("test_find_fix_candidates...")
    conn = _connect(tmp)

    # Case A: fixable -- original folder (2012-12) disagrees with the
    # filesystem-fallback date_taken (2012-11).
    _insert_photo(conn, "fixable1", r"E:\Pics\2012\2012-11\10C.jpg",
                  r"E:\Pics\2012\12\Portrait Studio\10C.jpg", "2012-11-25T15:44:07", "filesystem")

    # Case B: pattern found, but already agrees -- nothing to do.
    _insert_photo(conn, "agrees1", r"E:\Pics\2012\2012-12\11C.jpg",
                  r"E:\Pics\2012\12\Portrait Studio\11C.jpg", "2012-12-25T15:44:07", "filesystem")

    # Case C: no recoverable signal at all (flat/unsorted original_path,
    # like the real "katya 2024" incident) -- must NOT be proposed a fix.
    _insert_photo(conn, "unfixable1", r"E:\Pics\2023\2023-10\IMG_1.JPG",
                  r"E:\katya 2024\IMG_1.JPG", "2023-10-29T13:00:00", "filesystem")

    # Case D: would match the fixable pattern, and a photo_edits row already
    # exists recording a manual date_taken edit for it -- must be excluded
    # from candidates via the explicit has_manual_edit() belt-and-suspenders
    # check, never silently overwritten. (In the real apply_edit() flow this
    # can't coexist with date_source still being 'filesystem' -- editing
    # date_taken always flips date_source to 'manual' too, which already
    # excludes the row via this query's own WHERE clause; this row inserts
    # the photo_edits record directly to exercise that extra check on its
    # own, in case a future write path ever records an edit without also
    # updating date_source.)
    _insert_photo(conn, "manual1", r"E:\Pics\2012\2012-11\12C.jpg",
                  r"E:\Pics\2012\12\Portrait Studio\12C.jpg", "2012-11-25T15:44:07", "filesystem")
    conn.execute(
        "INSERT INTO photo_edits (file_hash, field_name, old_value, new_value, edited_by, edited_at) "
        "VALUES ('manual1', 'date_taken', '2012-11-25T15:44:07', '2012-12-15T00:00:00', "
        "'a_real_user', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    assert has_manual_edit(conn, "manual1", "date_taken")

    # Case E: exif-sourced -- not even considered (only filesystem-sourced
    # rows are candidates for this rule).
    _insert_photo(conn, "exif1", r"E:\Pics\2012\2012-11\13C.jpg",
                  r"E:\Pics\2012\12\Portrait Studio\13C.jpg", "2012-11-25T15:44:07", "exif")

    preview = find_fix_candidates(conn)
    hashes = {c.file_hash for c in preview.candidates}
    assert hashes == {"fixable1"}, hashes
    c = preview.candidates[0]
    assert c.new_date_taken == "2012-12-01T00:00:00", c.new_date_taken
    assert c.corrected_year_month == "2012-12"
    assert preview.already_agrees_count == 1
    assert preview.already_manually_edited_count == 1
    assert preview.no_pattern_count == 1  # unfixable1 (exif1 isn't filesystem-sourced, not counted here)
    assert preview.total_filesystem_rows == 4  # fixable1, agrees1, unfixable1, manual1 (not exif1)
    print(f"  candidates={len(preview.candidates)} agrees={preview.already_agrees_count} "
          f"manual={preview.already_manually_edited_count} no_pattern={preview.no_pattern_count}  OK")
    conn.close()


def test_apply_fixes(tmp: Path) -> None:
    print("test_apply_fixes...")
    conn = _connect(tmp)
    _insert_photo(conn, "fixA", r"E:\Pics\2009\2009-11\a.jpg",
                  r"E:\Pics\2009\12\a.jpg", "2009-11-21T17:29:16", "filesystem")
    _insert_photo(conn, "fixB", r"E:\Pics\2011\2011-07\b.jpg",
                  r"E:\Pics\2011\05\b.jpg", "2011-07-28T22:42:56", "filesystem")

    preview = find_fix_candidates(conn)
    assert len(preview.candidates) == 2

    progress_calls = []
    stats = apply_fixes(conn, preview.candidates, LOGGER,
                         progress_cb=lambda done, total: progress_calls.append((done, total)))
    assert stats.applied == 2 and stats.errors == 0 and stats.skipped_manual_edit == 0, stats.summary()
    assert progress_calls == [(1, 2), (2, 2)]

    row = conn.execute("SELECT date_taken, date_source, date_taken_year, date_taken_month "
                        "FROM photos WHERE file_hash='fixA'").fetchone()
    assert row["date_taken"] == "2009-12-01T00:00:00"
    assert row["date_source"] == "manual"
    assert row["date_taken_year"] == 2009 and row["date_taken_month"] == 12

    edit_rows = conn.execute("SELECT * FROM photo_edits WHERE file_hash='fixA'").fetchall()
    assert len(edit_rows) == 1
    assert edit_rows[0]["field_name"] == "date_taken"
    assert edit_rows[0]["edited_by"] == "date-audit-batch"
    assert edit_rows[0]["old_value"] == "2009-11-21T17:29:16"

    # Re-running the candidate finder afterward must find nothing left --
    # both rows are now date_source='manual', permanently out of scope for
    # this batch rule (and for any other automated writer, per Phase 2f).
    assert find_fix_candidates(conn).candidates == []
    print("  applied=2, date_source flipped to 'manual', photo_edits recorded, re-scan finds 0 left  OK")

    # A manual edit made between preview and apply must still be respected
    # -- apply_fixes re-checks has_manual_edit() itself, not just trusting
    # the preview snapshot handed to it.
    _insert_photo(conn, "fixC", r"E:\Pics\2015\2015-06\c.jpg",
                  r"E:\Pics\2015\05\c.jpg", "2015-06-02T21:31:57", "filesystem")
    stale_preview = find_fix_candidates(conn)
    assert len(stale_preview.candidates) == 1
    # Simulate a human editing this exact photo in review_tool.py right
    # after the preview was generated, before apply runs.
    apply_edit(conn, "fixC", "date_taken", "2015-06-10T00:00:00", "a_real_user", logger=LOGGER)
    stats2 = apply_fixes(conn, stale_preview.candidates, LOGGER)
    assert stats2.applied == 0 and stats2.skipped_manual_edit == 1, stats2.summary()
    row = conn.execute("SELECT date_taken FROM photos WHERE file_hash='fixC'").fetchone()
    assert row["date_taken"] == "2015-06-10T00:00:00", "the human's edit must win, not the stale batch value"
    print("  a manual edit made between preview and apply is respected, not clobbered  OK")

    # stop_check should halt before processing any further candidates.
    _insert_photo(conn, "fixD", r"E:\Pics\2016\2016-03\d.jpg",
                  r"E:\Pics\2016\04\d.jpg", "2016-03-29T16:35:50", "filesystem")
    _insert_photo(conn, "fixE", r"E:\Pics\2016\2016-03\e.jpg",
                  r"E:\Pics\2016\04\e.jpg", "2016-03-30T17:02:18", "filesystem")
    preview3 = find_fix_candidates(conn)
    assert len(preview3.candidates) == 2
    stats3 = apply_fixes(conn, preview3.candidates, LOGGER, stop_check=lambda: True)
    assert stats3.applied == 0, stats3.summary()
    print("  stop_check honored (stops before applying any)  OK")

    conn.close()


def test_sequence_interpolation(tmp: Path) -> None:
    print("test_sequence_interpolation...")
    conn = _connect(tmp)

    # Reference folder: real (exif-sourced) dates for a device's camera
    # roll, e.g. E:\Pics\2020\2020-02 in the real incident this models.
    _insert_photo(conn, "ref100", r"E:\Pics\2020\2020-02\IMG_0100.JPG",
                  r"E:\Pics\2020\2020-02\IMG_0100.JPG", "2020-02-01T10:00:00", "exif")
    _insert_photo(conn, "ref200", r"E:\Pics\2020\2020-02\IMG_0200.JPG",
                  r"E:\Pics\2020\2020-02\IMG_0200.JPG", "2020-02-10T10:00:00", "exif")
    _insert_photo(conn, "ref300", r"E:\Pics\2020\2020-02\IMG_0300.JPG",
                  r"E:\Pics\2020\2020-02\IMG_0300.JPG", "2020-02-20T10:00:00", "exif")
    # An edited-variant duplicate of IMG_0200 with a slightly different
    # timestamp -- exercises the median-of-variants path in _ref_date().
    _insert_photo(conn, "ref200b", r"E:\Pics\2020\2020-02\IMG_0200 2.JPG",
                  r"E:\Pics\2020\2020-02\IMG_0200 2.JPG", "2020-02-10T12:00:00", "exif")

    # Target folder: misdated files sharing the SAME camera-roll numbers.
    # IMG_0100/0200/0300 should get EXACT matches; IMG_0150 (bracketed
    # between 0100 and 0200, no exact reference) should be interpolated;
    # IMG_0400 (past the reference set's own max, 0300) must be refused
    # (extrapolation), not guessed at.
    _insert_photo(conn, "exact100", r"E:\Pics\2023\2023-10\IMG_0100.JPG",
                  r"E:\dump\IMG_0100.JPG", "2023-10-29T13:00:00", "filesystem")
    _insert_photo(conn, "exact200", r"E:\Pics\2023\2023-10\IMG_0200.JPG",
                  r"E:\dump\IMG_0200.JPG", "2023-10-29T13:00:00", "filesystem")
    _insert_photo(conn, "interp150", r"E:\Pics\2023\2023-10\IMG_0150.JPG",
                  r"E:\dump\IMG_0150.JPG", "2023-10-29T13:00:00", "filesystem")
    _insert_photo(conn, "extrap400", r"E:\Pics\2023\2023-10\IMG_0400.JPG",
                  r"E:\dump\IMG_0400.JPG", "2023-10-29T13:00:00", "filesystem")
    # Not filesystem-sourced -- must not be considered at all.
    _insert_photo(conn, "notfs250", r"E:\Pics\2023\2023-10\IMG_0250.JPG",
                  r"E:\dump\IMG_0250.JPG", "2023-10-29T13:00:00", "exif")
    # No parseable sequence number in the filename.
    _insert_photo(conn, "noseq", r"E:\Pics\2023\2023-10\Photo.JPG",
                  r"E:\dump\Photo.JPG", "2023-10-29T13:00:00", "filesystem")
    # Already manually edited -- must be excluded even though it would
    # otherwise match exactly. (Inserts the photo_edits row directly,
    # keeping date_source='filesystem', the same way test_find_fix_candidates
    # exercises this belt-and-suspenders check -- via the real apply_edit()
    # flow, editing date_taken always flips date_source to 'manual' too,
    # which would exclude the row via this query's own WHERE clause before
    # ever reaching the has_manual_edit() check.)
    _insert_photo(conn, "manual300", r"E:\Pics\2023\2023-10\IMG_0300.JPG",
                  r"E:\dump\IMG_0300.JPG", "2023-10-29T13:00:00", "filesystem")
    conn.execute(
        "INSERT INTO photo_edits (file_hash, field_name, old_value, new_value, edited_by, edited_at) "
        "VALUES ('manual300', 'date_taken', '2023-10-29T13:00:00', '2023-11-01T00:00:00', "
        "'a_real_user', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()

    candidates, stats = find_sequence_interpolation_candidates(
        conn, target_prefix=r"E:\dump" + "\\", reference_prefix=r"E:\Pics\2020\2020-02" + "\\"
    )
    by_hash = {c.file_hash: c for c in candidates}

    assert by_hash["exact100"].new_date_taken == "2020-02-01T10:00:00"
    assert by_hash["exact100"].basis == "sequence-exact"
    # _ref_date() picks dts[len(dts)//2] of the sorted variants for a given
    # number -- for the two IMG_0200 variants (10:00, 12:00) that's index 1.
    assert by_hash["exact200"].new_date_taken == "2020-02-10T12:00:00"
    assert by_hash["interp150"].basis == "sequence-interpolated"
    # IMG_0150 is exactly halfway between IMG_0100 (Feb 1) and IMG_0200 (Feb 10) by number.
    assert by_hash["interp150"].new_date_taken.startswith("2020-02-05T"), by_hash["interp150"].new_date_taken

    assert "extrap400" not in by_hash, "must not extrapolate past the reference set's own max number"
    assert "notfs250" not in by_hash, "non-filesystem-sourced rows must not be considered"
    assert "noseq" not in by_hash, "a filename with no parseable sequence number must not be considered"
    assert "manual300" not in by_hash, "a manually-edited row must never be silently overwritten"

    # exact_matches counts every number that matched exactly, including
    # manual300 (filtered out afterward by the has_manual_edit() check).
    assert stats.exact_matches == 3
    assert stats.interpolated == 1
    assert stats.extrapolated_skipped == 1
    assert stats.no_sequence_number == 1
    assert stats.already_manually_edited == 1
    print(f"  {stats.summary()}  OK")

    # Applying: writes go through the same apply_edit() audit path, tagged
    # with SEQUENCE_FIX_EDITOR (distinct from the folder-rule's BATCH_FIX_EDITOR).
    apply_stats = apply_fixes(conn, candidates, LOGGER, edited_by=SEQUENCE_FIX_EDITOR)
    assert apply_stats.applied == len(candidates) and apply_stats.errors == 0
    row = conn.execute("SELECT date_source FROM photos WHERE file_hash='exact100'").fetchone()
    assert row["date_source"] == "manual"
    edit_row = conn.execute(
        "SELECT edited_by FROM photo_edits WHERE file_hash='exact100' AND field_name='date_taken'"
    ).fetchone()
    assert edit_row["edited_by"] == SEQUENCE_FIX_EDITOR
    print("  applied via apply_edit(), tagged with SEQUENCE_FIX_EDITOR in photo_edits  OK")

    # No reference points at all -- must not crash, must propose nothing.
    empty_candidates, empty_stats = find_sequence_interpolation_candidates(
        conn, target_prefix=r"E:\dump" + "\\", reference_prefix=r"E:\NoSuchFolder" + "\\"
    )
    assert empty_candidates == [] and empty_stats.total_targets == 0
    print("  no reference points -> no candidates, no crash  OK")

    conn.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_date_audit_"))
    print(f"Working in {tmp}")
    try:
        test_find_year_month_in_parts()
        test_find_suspicious_clusters(tmp / "clusters")
        test_find_fix_candidates(tmp / "candidates")
        test_apply_fixes(tmp / "apply")
        test_sequence_interpolation(tmp / "sequence")
        print("\nALL DATE AUDIT TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
