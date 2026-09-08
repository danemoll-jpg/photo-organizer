"""Exercises Phase 3g: generalizing review_tool.py's single-person `person`
filter (Phase 3d) into a multi-person `people` filter with AND semantics --
select several named people and see only photos containing EVERY one of
them (the user's own example: Dan Moll + Juliana Moll -> photos with both,
not either). See _build_filters' docstring in review_tool.py for the exact
query (a GROUP BY/HAVING intersection, not a plain IN (...), which would
wrongly produce OR semantics).

Same isolated-temp-dir posture as test_phase3d.py -- never touches
config.yaml, the real DB, or anything under E:\\Pics. Reuses that file's
exact fixture shape (five photos, h2 has BOTH Alice and Bob) since it's
already built for exactly this AND-vs-OR distinction.

Usage:
    venv\\Scripts\\python tests\\test_phase3g.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_phase3d import _setup  # noqa: E402 -- reuse Phase 3d's exact fixture


def test_people_and_semantics_not_or(tmp: Path) -> None:
    print("\n=== Phase 3g: people=<id1>,<id2> is AND, not OR ===")
    _rt, client = _setup(tmp)

    # h1: Alice only. h2: Alice AND Bob. h3: Bob only. A naive IN (...)
    # would wrongly return {h1, h2, h3} (anyone matching either person) --
    # the whole point of this feature is that only h2 (BOTH) should match.
    resp = client.get("/api/photos?people=10,20&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h2"}, f"AND semantics violated -- got {hashes}, expected only h2 (has both Alice+Bob)"
    print("  people=10,20 (Alice+Bob) -> exactly {h2}, NOT {h1,h2,h3} -- true AND, not OR  OK")

    # Order shouldn't matter.
    resp = client.get("/api/photos?people=20,10&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h2"}, hashes
    print("  people=20,10 (reversed order) -> same result  OK")

    # A single id in the new plural param must behave identically to the
    # old singular one (degenerates to a plain single-person match).
    resp = client.get("/api/photos?people=10&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1", "h2"}, hashes
    print("  people=10 alone -> same as the old person=10  OK")

    # Nobody has all three of Alice+Bob+the-unnamed-cluster-30-face -- must
    # correctly return empty, not accidentally fall back to OR/partial match.
    resp = client.get("/api/photos?people=10,20,30&limit=50")
    assert resp.get_json()["items"] == [], "no photo has all three -- must be empty, not a partial/OR match"
    print("  people=10,20,30 (nobody has all three) -> empty  OK")

    # Duplicate ids in the list must not lower the effective N (people=10,10
    # must behave exactly like people=10, not silently match everyone with
    # cluster 10 duplicated in a face row, which doesn't exist in this
    # fixture anyway, or worse, break the COUNT(DISTINCT...)=N arithmetic).
    resp = client.get("/api/photos?people=10,10&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1", "h2"}, hashes
    print("  people=10,10 (duplicate) -> same as people=10, not broken/empty  OK")


def test_people_legacy_person_param_still_works(tmp: Path) -> None:
    print("\n=== Phase 3g: the old singular `person` param still works unchanged ===")
    _rt, client = _setup(tmp)

    resp = client.get("/api/photos?person=20&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h2", "h3"}, hashes
    print("  person=20 (legacy alias) -> exactly {h2, h3}, unchanged from Phase 3d  OK")

    # A non-integer entry anywhere in a `people` list is dropped, not an
    # error, same posture as an unrecognized media_type / the old `person`.
    resp = client.get("/api/photos?people=10,not-a-number&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1", "h2"}, f"bad entry should just be dropped, leaving people=10 -- got {hashes}"
    print("  people=10,not-a-number -> the bad entry is dropped, behaves as people=10  OK")

    resp = client.get("/api/photos?people=not-a-number&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1", "h2", "h3", "h4", "h5"}, hashes
    print("  people=not-a-number (nothing valid) -> no person filter applied at all  OK")


def test_people_composes_with_other_filters(tmp: Path) -> None:
    print("\n=== Phase 3g: people=A,B composes with date range, same as every other filter combo ===")
    _rt, client = _setup(tmp)

    # h2 (06-02) is the only photo with both Alice+Bob. Restricting to
    # 06-03 onward must correctly drop it -- both filters apply TOGETHER.
    resp = client.get("/api/photos?people=10,20&date_from=2021-06-03&limit=50")
    assert resp.get_json()["items"] == [], "h2 (06-02) must be excluded by date_from=06-03"
    print("  people=10,20 AND date_from=2021-06-03 -> empty (h2 excluded by date)  OK")

    resp = client.get("/api/photos?people=10,20&date_from=2021-06-01&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h2"}, hashes
    print("  people=10,20 AND date_from=2021-06-01 -> exactly {h2}  OK")

    resp = client.get("/api/stats?people=10,20")
    assert resp.get_json()["total_photos"] == 1
    print("  /api/stats honors the multi-person AND filter too  OK")


def test_nav_respects_people_and_filter(tmp: Path) -> None:
    print("\n=== Phase 3g: /api/nav sequential stepping honors the multi-person AND filter ===")
    _rt, client = _setup(tmp)

    # Only h2 has both Alice+Bob -- next-stepping through that filtered set
    # must visit h2 then immediately hit the end, never h1 or h3 (each has
    # only one of the two).
    resp = client.get("/api/nav?people=10,20&dir=next")
    first = resp.get_json()["item"]
    assert first["file_hash"] == "h2", first
    resp = client.get(f"/api/nav?people=10,20&dir=next&cursor={first['file_hash']}")
    assert resp.get_json()["item"] is None, "must stop at the end -- no other photo has both Alice and Bob"
    print("  people=10,20 next-stepping visits only h2, then end (never h1/h3 -- OR would have)  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase3g_"))
    print(f"Working in {tmp}")
    try:
        test_people_and_semantics_not_or(tmp)
        test_people_legacy_person_param_still_works(tmp)
        test_people_composes_with_other_filters(tmp)
        test_nav_respects_people_and_filter(tmp)
        print("\nALL PHASE 3G TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
