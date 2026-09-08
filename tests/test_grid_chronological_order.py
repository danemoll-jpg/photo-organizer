"""Follow-up session, direct user request ("please change the ui grid so
that it is in chronological order" -> "I am referring to the web ui"):
review_tool.py's grid/viewer walk order changed from current_path
(roughly year/month/filename, matching the on-disk folder layout) to true
chronological order by date_taken. See review_tool.py's module docstring
and _DATE_SORT_EXPR's own comment for the full design.

This is a DEDICATED test for the actual ordering behavior -- deliberately
NOT reusing the existing test_phase2*/test_phase3*.py fixtures, whose
filenames happen to already be in the same relative order as their
date_taken values (by original design, for THEIR OWN unrelated assertions
about filtering/pagination mechanics), so those wouldn't actually catch a
bug where the grid silently fell back to path order. This fixture
deliberately scrambles filename order vs. date_taken order, includes an
undated photo (must sort last, not wherever SQLite's own NULL-handling
would put it), and two photos sharing an identical date_taken (proves the
file_hash tiebreaker gives a stable, reproducible order across pages/nav
steps rather than SQLite picking an arbitrary/unstable order among ties).

Usage:
    venv\\Scripts\\python tests\\test_grid_chronological_order.py
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


def _jpeg(path: Path, color=(10, 20, 30)) -> Path:
    img = Image.new("RGB", (20, 20), color)
    img.save(path, "jpeg")
    return path


def _setup(tmp: Path):
    """Five dated photos + one undated, filenames deliberately in the
    OPPOSITE order from date_taken (so a bug that silently sorted by
    current_path instead of date_taken would produce the reverse of the
    expected sequence, not a coincidentally-correct one), plus two photos
    sharing an identical date_taken to exercise the file_hash tiebreaker."""
    import review_tool

    case = tmp / f"case_{uuid.uuid4().hex[:8]}"
    dest = case / "dest"
    d = dest / "2020" / "2020-01"
    d.mkdir(parents=True)

    # Filenames sort z, y, x, w, v, u -- i.e. exactly BACKWARDS from the
    # chronological order asserted below. A grid that's still (accidentally
    # or otherwise) walking current_path order would produce the reverse
    # sequence, which the assertions below would catch immediately.
    p_earliest = _jpeg(d / "z_earliest.jpg")
    p_second = _jpeg(d / "y_second.jpg")
    p_tie_a = _jpeg(d / "x_tie_a.jpg")
    p_tie_b = _jpeg(d / "w_tie_b.jpg")
    p_latest = _jpeg(d / "v_latest.jpg")
    p_undated = _jpeg(d / "u_undated.jpg")

    dbp = case / "review.db"
    logger = logging.getLogger("grid_chrono_order_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)

    def insert(file_hash, path, date_taken):
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at,
               phase1_verified)
               VALUES (?, ?, ?, ?, ?, 0, ?, 2020, 1, 'exif', 'sorted', '2020-01-01', 1)""",
            (file_hash, str(path), str(path), path.name, path.stat().st_size, date_taken),
        )

    # file_hash values chosen so the tie-pair's ALPHABETICAL file_hash
    # order ("h_tie_a" < "h_tie_b") is the opposite of their PATH order
    # ("x_tie_a.jpg" > "w_tie_b.jpg") -- so a passing tiebreaker test can't
    # be accidentally explained by path order sneaking back in.
    insert("h_earliest", p_earliest, "2020-01-01T09:00:00")
    insert("h_second", p_second, "2020-01-02T09:00:00")
    insert("h_tie_a", p_tie_a, "2020-01-03T09:00:00")
    insert("h_tie_b", p_tie_b, "2020-01-03T09:00:00")  # identical date_taken to h_tie_a
    insert("h_latest", p_latest, "2020-01-04T09:00:00")
    insert("h_undated", p_undated, None)  # must sort LAST, not first (SQLite's own NULL default) or mid-sequence
    conn.commit()
    conn.close()

    captions_path = case / "captions.jsonl"
    captions_path.write_text("", encoding="utf-8")

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


# The one, single, canonical expected order for this whole fixture:
# earliest -> second -> tie_a -> tie_b (file_hash tiebreak, "h_tie_a" <
# "h_tie_b") -> latest -> undated (always last).
EXPECTED_ORDER = ["h_earliest", "h_second", "h_tie_a", "h_tie_b", "h_latest", "h_undated"]


def test_grid_pagination_is_chronological(tmp: Path) -> None:
    print("\n=== /api/photos: grid pagination walks true chronological (date_taken) order ===")
    _rt, client = _setup(tmp)

    resp = client.get("/api/photos?limit=50")
    data = resp.get_json()
    seen = [i["file_hash"] for i in data["items"]]
    assert seen == EXPECTED_ORDER, f"expected {EXPECTED_ORDER}, got {seen}"
    print(f"  single-page fetch (limit=50) returns exactly {EXPECTED_ORDER}  OK")

    # Paginate 2-at-a-time across the whole set forward, then confirm
    # paging backward from the end reproduces the same sequence exactly.
    forward: list[str] = []
    after = None
    while True:
        params = f"?limit=2&after={after}" if after else "?limit=2"
        resp = client.get(f"/api/photos{params}")
        page = resp.get_json()
        forward.extend(i["file_hash"] for i in page["items"])
        if not page["has_next"]:
            break
        after = page["next_cursor"]
    assert forward == EXPECTED_ORDER, f"forward pagination should reconstruct {EXPECTED_ORDER}, got {forward}"
    print("  forward keyset pagination (limit=2 per page) reconstructs the exact same order  OK")

    # Page forward again, this time keeping every page, so the LAST page is
    # a real page already in hand (mirrors how the grid UI always pages
    # backward FROM a page it's currently showing, never from a made-up
    # cursor) -- then page backward from there and confirm it reconstructs
    # the exact same full sequence.
    pages_forward = []
    after = None
    while True:
        params = f"?limit=2&after={after}" if after else "?limit=2"
        page = client.get(f"/api/photos{params}").get_json()
        pages_forward.append(page)
        if not page["has_next"]:
            break
        after = page["next_cursor"]

    last_page = pages_forward[-1]
    backward = [i["file_hash"] for i in last_page["items"]]
    before = last_page["items"][0]["file_hash"] if last_page["has_prev"] else None
    while before is not None:
        page = client.get(f"/api/photos?limit=2&before={before}").get_json()
        backward = [i["file_hash"] for i in page["items"]] + backward
        before = page["items"][0]["file_hash"] if page["has_prev"] else None
    assert backward == EXPECTED_ORDER, f"backward pagination should reconstruct {EXPECTED_ORDER}, got {backward}"
    print("  backward keyset pagination (before= cursor) reconstructs the exact same order  OK")


def test_nav_sequential_stepping_is_chronological(tmp: Path) -> None:
    print("\n=== /api/nav: sequential dir=next/prev stepping matches the same chronological order ===")
    _rt, client = _setup(tmp)

    cursor = None
    seen = []
    for _ in range(len(EXPECTED_ORDER)):
        params = f"?dir=next&cursor={cursor}" if cursor else "?dir=next"
        resp = client.get(f"/api/nav{params}")
        item = resp.get_json()["item"]
        assert item is not None
        seen.append(item["file_hash"])
        cursor = item["file_hash"]
    assert seen == EXPECTED_ORDER, f"expected {EXPECTED_ORDER}, got {seen}"
    resp = client.get(f"/api/nav?dir=next&cursor={cursor}")
    assert resp.get_json()["item"] is None, "stepping past the last (undated) item must return null"
    print(f"  stepping dir=next from the start visits exactly {EXPECTED_ORDER}, then null  OK")

    # Walk all the way back from the end -- must reproduce the exact
    # reverse sequence, including the tie-pair in the correct order.
    # `cursor` is currently the LAST real item (h_undated) from the forward
    # walk above -- already known, so it seeds seen_back directly; dir=prev
    # from it yields the remaining len(EXPECTED_ORDER)-1 items.
    seen_back = [cursor]
    for _ in range(len(EXPECTED_ORDER) - 1):
        resp = client.get(f"/api/nav?dir=prev&cursor={cursor}")
        item = resp.get_json()["item"]
        assert item is not None
        seen_back.append(item["file_hash"])
        cursor = item["file_hash"]
    assert seen_back == list(reversed(EXPECTED_ORDER)), \
        f"expected reversed {EXPECTED_ORDER}, got {seen_back}"
    resp = client.get(f"/api/nav?dir=prev&cursor={cursor}")
    assert resp.get_json()["item"] is None, "stepping before the first item must return null"
    print(f"  stepping dir=prev from the end reproduces the exact reverse sequence, then null  OK")


def test_stale_cursor_restarts_gracefully(tmp: Path) -> None:
    print("\n=== A cursor pointing at a since-vanished file_hash restarts the walk, doesn't 500 ===")
    _rt, client = _setup(tmp)

    resp = client.get("/api/nav?dir=next&cursor=this-hash-does-not-exist-anymore")
    assert resp.status_code == 200
    item = resp.get_json()["item"]
    assert item is not None and item["file_hash"] == "h_earliest", \
        "an unresolvable cursor should restart the walk from the beginning, not error or silently return null"
    print("  stale/unknown-hash cursor treated as 'no cursor' -- restarts at h_earliest, no 500  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_grid_chrono_"))
    print(f"Working in {tmp}")
    try:
        test_grid_pagination_is_chronological(tmp)
        test_nav_sequential_stepping_is_chronological(tmp)
        test_stale_cursor_restarts_gracefully(tmp)
        print("\nALL GRID-CHRONOLOGICAL-ORDER TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
