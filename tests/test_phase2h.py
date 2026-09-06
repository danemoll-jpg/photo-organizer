"""Exercises Phase 2h -- bulk grid selection/editing in review_tool.py --
against synthetic fixtures in an isolated temp dir (same posture as
test_phase2b.py/test_phase2c.py/test_phase2d.py/test_phase2f.py: never
touches config.yaml, the real DB, the session-secret file, or anything
under E:\\Pics).

Part A (bulk editing) is the part with real behavior to verify carefully:
  - src/edits.py::add_tag_bulk() in isolation -- the UNION/append semantics
    that make it deliberately different from apply_edit("tags", ...)'s
    full-replace (the whole point of building a separate function rather
    than reusing that code path, per photo-organizer-spec.md's explicit
    warning) -- including the "tag already present" no-op-with-no-audit-row
    case, and that it goes through the exact same photo_edits audit
    mechanism (has_manual_edit) as any other edit.
  - The full HTTP /api/bulk-edit flow through a logged-in Flask test
    client: Set location / Set date / Hide applying the SAME value to every
    selected item (BULK_SET_FIELDS's "set" mode == apply_edit's existing
    semantics), Add tag unioning into each item's OWN different existing
    tags (not clobbering them), CSRF rejection, unauthenticated rejection,
    an unknown/disallowed field rejected (including "tags" with mode="set",
    which must be rejected -- bulk tagging is add_tag-only), and a mixed
    batch (one good hash + one bogus hash) producing a partial
    updated/failed summary rather than aborting the whole request.
  - edited_by on a bulk edit is the real logged-in username, same as a
    single-item edit -- not a synthetic batch-tool tag like the
    fully-automated date-audit tool uses.

Part B (mobile viewer layout) is a pure CSS/JS show-hide with no server
surface to test at the HTTP level -- verified instead via a direct
rendered-HTML/CSS check: the new #viewer-info-toggle element and
.viewer-info-toggle/<=480px rule exist, and the existing <=800px
tablet-stack rule (which must NOT change per spec) is untouched.

Usage:
    venv\\Scripts\\python tests\\test_phase2h.py
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import piexif
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.auth import hash_password
from src.config import Config
from src.db import connect, init_db
from src.edits import (
    MAX_BULK_ITEMS,
    add_tag_bulk,
    apply_edit,
    current_tags,
    has_manual_edit,
)


def _jpeg(path: Path, dt: str) -> Path:
    img = Image.new("RGB", (30, 30), (10, 20, 30))
    img.save(path, "jpeg", exif=piexif.dump({"Exif": {piexif.ExifIFD.DateTimeOriginal: dt.encode()}}))
    return path


def _csrf_from_html(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"couldn't find csrf_token in page HTML:\n{html[:500]}"
    return m.group(1)


# ---------------------------------------------------------------------------
# Part 1: src/edits.py::add_tag_bulk() in isolation (no Flask)
# ---------------------------------------------------------------------------

def test_add_tag_bulk_unit(tmp: Path) -> None:
    print("\n=== Phase 2h: src/edits.py add_tag_bulk() -- union/append, not full-replace ===")
    case = tmp / f"case_{uuid.uuid4().hex[:8]}"
    case.mkdir()
    dbp = case / "test.db"
    logger = logging.getLogger("phase2h_unit_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)

    def insert(file_hash, existing_tags):
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at, phase1_verified)
               VALUES (?, ?, ?, ?, 1, 0, '2020-05-01T09:00:00', 2020, 5, 'exif', 'sorted', '2020-05-01', 1)""",
            (file_hash, f"C:\\dest\\{file_hash}.jpg", f"C:\\src\\{file_hash}.jpg", f"{file_hash}.jpg"),
        )
        for t in existing_tags:
            conn.execute("INSERT OR IGNORE INTO tags (tag_name) VALUES (?)", (t,))
            conn.execute(
                "INSERT INTO photo_tags (file_hash, tag_id) VALUES (?, (SELECT tag_id FROM tags WHERE tag_name=?))",
                (file_hash, t),
            )

    insert("hA", ["family"])
    insert("hB", ["beach", "sunset"])
    insert("hC", [])  # no existing tags at all
    conn.commit()

    add_tag_bulk(conn, "hA", "vacation", edited_by="dan")
    add_tag_bulk(conn, "hB", "vacation", edited_by="dan")
    add_tag_bulk(conn, "hC", "vacation", edited_by="dan")
    assert sorted(current_tags(conn, "hA")) == sorted(["family", "vacation"]), current_tags(conn, "hA")
    assert sorted(current_tags(conn, "hB")) == sorted(["beach", "sunset", "vacation"]), current_tags(conn, "hB")
    assert current_tags(conn, "hC") == ["vacation"]
    print("  each file's DIFFERENT existing tags are preserved -- new tag is unioned in, not replacing them  OK")

    assert has_manual_edit(conn, "hA", "tags") is True
    audit = conn.execute(
        "SELECT old_value, new_value, edited_by FROM photo_edits WHERE file_hash='hA' AND field_name='tags'"
    ).fetchone()
    assert json.loads(audit[0]) == ["family"] and sorted(json.loads(audit[1])) == sorted(["family", "vacation"])
    assert audit[2] == "dan"
    print("  goes through the same photo_edits audit mechanism as any other edit (field_name='tags')  OK")

    # Re-adding a tag the file already has (case-insensitively) is a no-op:
    # no duplicate, and no second audit row spamming the trail.
    before_count = conn.execute("SELECT COUNT(*) FROM photo_edits WHERE file_hash='hA'").fetchone()[0]
    result = add_tag_bulk(conn, "hA", "Vacation", edited_by="dan")  # different case, already present
    assert sorted(result) == sorted(["family", "vacation"])
    after_count = conn.execute("SELECT COUNT(*) FROM photo_edits WHERE file_hash='hA'").fetchone()[0]
    assert after_count == before_count, "re-adding an already-present tag must not write a redundant audit row"
    print("  re-adding an already-present tag (case-insensitive) is a no-op -- no dup, no extra audit row  OK")

    try:
        add_tag_bulk(conn, "missing-hash", "x", edited_by="dan")
        assert False, "a nonexistent file_hash must be rejected"
    except ValueError:
        pass
    try:
        add_tag_bulk(conn, "hA", "   ", edited_by="dan")
        assert False, "a blank tag must be rejected"
    except ValueError:
        pass
    try:
        add_tag_bulk(conn, "hA", "x" * 500, edited_by="dan")
        assert False, "an over-length tag must be rejected"
    except ValueError:
        pass
    print("  validation: unknown file_hash / blank tag / over-length tag all rejected  OK")
    conn.close()


# ---------------------------------------------------------------------------
# Part 2: full HTTP /api/bulk-edit flow through review_tool.py's test client
# ---------------------------------------------------------------------------

def _setup(tmp: Path):
    """Same shape as test_phase2f.py's _setup -- photo fixtures with
    DIFFERENT pre-existing tags per file (so Add tag's union semantics are
    actually exercised, not just three files that happen to start empty)
    plus the real register_auth + login flow, since /api/bulk-edit is both
    auth-gated and CSRF-protected exactly like /api/edit/<hash>."""
    import importlib

    import review_tool
    importlib.reload(review_tool)
    from src.storage import get_storage

    case = tmp / f"case_{uuid.uuid4().hex[:8]}"
    dest = case / "dest" / "2021" / "2021-06"
    dest.mkdir(parents=True)

    p1 = _jpeg(dest / "01_family.jpg", "2021:06:01 09:00:00")
    p2 = _jpeg(dest / "02_beach.jpg", "2021:06:02 09:00:00")
    p3 = _jpeg(dest / "03_notags.jpg", "2021:06:03 09:00:00")

    dbp = case / "review.db"
    logger = logging.getLogger("phase2h_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)

    def insert(file_hash, path, date_taken, tags):
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at, phase1_verified)
               VALUES (?, ?, ?, ?, ?, 0, ?, 2021, 6, 'exif', 'sorted', '2021-06-01', 1)""",
            (file_hash, str(path), str(path), path.name, path.stat().st_size, date_taken),
        )
        for t in tags:
            conn.execute("INSERT OR IGNORE INTO tags (tag_name) VALUES (?)", (t,))
            conn.execute(
                "INSERT INTO photo_tags (file_hash, tag_id) VALUES (?, (SELECT tag_id FROM tags WHERE tag_name=?))",
                (file_hash, t),
            )

    insert("h1", p1, "2021-06-01T09:00:00", ["family"])
    insert("h2", p2, "2021-06-02T09:00:00", ["beach", "sunset"])
    insert("h3", p3, "2021-06-03T09:00:00", [])
    conn.commit()
    conn.close()

    cfg = Config(
        source_folders=[], dest_root=str(case / "dest"), supported_extensions=[".jpg"],
        unsorted_subfolder="_unsorted", dry_run=True, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(dbp), log_dir=str(case / "logs"),
        captions_path=str(case / "captions.jsonl"), video_extensions=[".mp4", ".mov", ".avi"],
        review_page_size=40,
        review_users=[{"username": "dan", "password_hash": hash_password("correct-horse-battery")}],
        session_cookie_secure=False, session_lifetime_days=30,
        login_rate_limit_attempts=5, login_rate_limit_window_seconds=900,
    )
    review_tool._cfg = cfg
    review_tool._video_exts = cfg.video_extensions_normalized
    review_tool._storage = get_storage(cfg)
    review_tool._captions_cache = review_tool.CaptionsCache(cfg.captions_path_abs)
    review_tool._captions_cache.refresh()
    review_tool._logger = logger
    review_tool.register_auth(review_tool.app, cfg)
    review_tool.app.config["TESTING"] = True

    client = review_tool.app.test_client()
    login_page = client.get("/login")
    csrf = _csrf_from_html(login_page.get_data(as_text=True))
    resp = client.post("/login", data={"username": "dan", "password": "correct-horse-battery", "csrf_token": csrf},
                        follow_redirects=False)
    assert resp.status_code == 302, "test setup: login must succeed"

    index_html = client.get("/").get_data(as_text=True)
    m = re.search(r"csrfToken:\s*\"([^\"]+)\"", index_html)
    assert m, f"couldn't find csrfToken in index page:\n{index_html[:800]}"
    edit_csrf = m.group(1)

    return review_tool, client, edit_csrf, dbp


def _bulk_edit(client, edit_csrf, file_hashes, field, value, mode=None, csrf_token=None):
    body = {"file_hashes": file_hashes, "field": field, "value": value,
            "csrf_token": csrf_token if csrf_token is not None else edit_csrf}
    if mode is not None:
        body["mode"] = mode
    return client.post("/api/bulk-edit", json=body)


def _tags_by_hash(client):
    items = client.get("/api/photos?limit=50").get_json()["items"]
    return {i["file_hash"]: sorted(i["tags"]) for i in items}


def test_bulk_add_tag_unions_not_replaces(tmp: Path) -> None:
    print("\n=== Phase 2h: bulk 'Add tag' unions into each item's OWN different tags ===")
    _rt, client, edit_csrf, _dbp = _setup(tmp)

    resp = _bulk_edit(client, edit_csrf, ["h1", "h2", "h3"], "tags", "vacation", mode="add_tag")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data == {"updated": 3, "failed": [], "total": 3}, data

    tags = _tags_by_hash(client)
    assert tags["h1"] == sorted(["family", "vacation"]), tags["h1"]
    assert tags["h2"] == sorted(["beach", "sunset", "vacation"]), tags["h2"]
    assert tags["h3"] == ["vacation"], tags["h3"]
    print("  h1's 'family', h2's 'beach'+'sunset' both survive -- only 'vacation' was added to each  OK")


def test_bulk_set_location_and_date_and_hide(tmp: Path) -> None:
    print("\n=== Phase 2h: bulk Set location / Set date / Hide apply the same value to every selected item ===")
    _rt, client, edit_csrf, _dbp = _setup(tmp)

    resp = _bulk_edit(client, edit_csrf, ["h1", "h2"], "location", "Marietta, GA")  # mode defaults to "set"
    assert resp.status_code == 200
    assert resp.get_json() == {"updated": 2, "failed": [], "total": 2}
    items = {i["file_hash"]: i for i in client.get("/api/photos?limit=50").get_json()["items"]}
    assert items["h1"]["location_name"] == "Marietta, GA"
    assert items["h2"]["location_name"] == "Marietta, GA"
    assert items["h3"]["location_name"] is None, "an item not in the batch must be untouched"
    print("  Set location: both selected items get the same value; the unselected one is untouched  OK")

    resp = _bulk_edit(client, edit_csrf, ["h1", "h2"], "date_taken", "1999-12-25", mode="set")
    assert resp.status_code == 200
    items = {i["file_hash"]: i for i in client.get("/api/photos?limit=50").get_json()["items"]}
    assert items["h1"]["date_taken"].startswith("1999-12-25")
    assert items["h2"]["date_taken"].startswith("1999-12-25")
    assert items["h3"]["date_taken"].startswith("2021-06-03"), "an item not in the batch must be untouched"
    print("  Set date: DB-only, applied to both selected items, unselected one untouched  OK")

    stats_before = client.get("/api/stats").get_json()["total_photos"]
    resp = _bulk_edit(client, edit_csrf, ["h3"], "excluded", True)
    assert resp.status_code == 200 and resp.get_json()["updated"] == 1
    stats_after = client.get("/api/stats").get_json()["total_photos"]
    assert stats_after == stats_before - 1
    visible = {i["file_hash"] for i in client.get("/api/photos?limit=50").get_json()["items"]}
    assert "h3" not in visible
    shown_hidden = {i["file_hash"] for i in client.get("/api/photos?show_hidden=1&limit=50").get_json()["items"]}
    assert "h3" in shown_hidden
    print("  Hide: same excluded=true mechanism as a single-item hide, reversible via show_hidden  OK")

    conn = connect_ro_for_test(_dbp)
    assert has_manual_edit(conn, "h1", "location") and has_manual_edit(conn, "h1", "date_taken")
    assert has_manual_edit(conn, "h3", "excluded")
    print("  every bulk-applied field is recorded via the same has_manual_edit() audit checkpoint  OK")


def connect_ro_for_test(dbp):
    import sqlite3
    conn = sqlite3.connect(str(dbp))
    conn.row_factory = sqlite3.Row
    return conn


def test_bulk_edit_edited_by_is_real_user(tmp: Path) -> None:
    print("\n=== Phase 2h: a bulk edit's edited_by is the real logged-in username, not a synthetic tag ===")
    _rt, client, edit_csrf, dbp = _setup(tmp)
    _bulk_edit(client, edit_csrf, ["h1"], "location", "Athens, GA")
    conn = connect_ro_for_test(dbp)
    row = conn.execute(
        "SELECT edited_by FROM photo_edits WHERE file_hash='h1' AND field_name='location'"
    ).fetchone()
    assert row[0] == "dan", row[0]
    print("  photo_edits.edited_by == the real session username ('dan'), same as a single-item edit  OK")


def test_bulk_edit_mixed_batch_partial_success(tmp: Path) -> None:
    print("\n=== Phase 2h: a bad hash in the batch fails just that one item, not the whole request ===")
    _rt, client, edit_csrf, _dbp = _setup(tmp)
    resp = _bulk_edit(client, edit_csrf, ["h1", "does-not-exist", "h2"], "location", "Rome, GA")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["updated"] == 2 and data["total"] == 3
    assert len(data["failed"]) == 1 and data["failed"][0]["file_hash"] == "does-not-exist"
    items = {i["file_hash"]: i for i in client.get("/api/photos?limit=50").get_json()["items"]}
    assert items["h1"]["location_name"] == "Rome, GA" and items["h2"]["location_name"] == "Rome, GA"
    print("  2 updated, 1 failed (named), and the 2 valid items were applied despite the bad hash  OK")


def test_bulk_edit_csrf_auth_and_allowlist(tmp: Path) -> None:
    print("\n=== Phase 2h: CSRF mismatch / unauthenticated / disallowed-field rejections ===")
    _rt, client, edit_csrf, _dbp = _setup(tmp)

    resp = _bulk_edit(client, edit_csrf, ["h1"], "location", "nope", csrf_token="wrong-token")
    assert resp.status_code == 403, resp.get_data(as_text=True)
    items = {i["file_hash"]: i for i in client.get("/api/photos?limit=50").get_json()["items"]}
    assert items["h1"]["location_name"] is None, "a CSRF-mismatched bulk edit must never actually apply"
    print("  wrong csrf_token -> 403, nothing applied  OK")

    resp = _bulk_edit(client, edit_csrf, ["h1"], "caption", "nope")
    assert resp.status_code == 400, "caption is not a bulk-editable field"
    print("  field='caption' (not in BULK_SET_FIELDS) -> 400, rejected before ever reaching apply_edit()  OK")

    resp = _bulk_edit(client, edit_csrf, ["h1"], "tags", ["a", "list"])  # mode defaults to "set", not "add_tag"
    assert resp.status_code == 400, "bulk tags must only be reachable via mode='add_tag', never a full-replace 'set'"
    print("  field='tags' with the default mode='set' -> 400 (bulk tagging is add_tag-only, never full-replace)  OK")

    resp = _bulk_edit(client, edit_csrf, ["h1"], "location", "x", mode="add_tag")
    assert resp.status_code == 400, "mode='add_tag' must only apply to the tags field"
    print("  mode='add_tag' with field='location' -> 400  OK")

    resp = client.post("/api/bulk-edit", json={"file_hashes": [], "field": "location", "value": "x", "csrf_token": edit_csrf})
    assert resp.status_code == 400, "an empty selection must be rejected"
    resp = client.post("/api/bulk-edit", json={"file_hashes": ["h1"] * (MAX_BULK_ITEMS + 1), "field": "location",
                                                "value": "x", "csrf_token": edit_csrf})
    assert resp.status_code == 400, "a selection over MAX_BULK_ITEMS must be rejected"
    print("  empty selection and an over-cap selection both -> 400  OK")

    client.post("/logout", data={"csrf_token": edit_csrf})
    resp = _bulk_edit(client, edit_csrf, ["h1"], "location", "should not apply")
    assert resp.status_code == 401, resp.get_data(as_text=True)
    print("  logged out -> 401, same as every other /api/* route  OK")


# ---------------------------------------------------------------------------
# Part 3: Part B (mobile viewer layout) -- static markup/CSS presence checks
# ---------------------------------------------------------------------------

def test_mobile_viewer_layout_markup_and_css(tmp: Path) -> None:
    print("\n=== Phase 2h Part B: phone-width info-panel toggle exists; tablet rule is untouched ===")
    _rt, client, _edit_csrf, _dbp = _setup(tmp)
    html = client.get("/").get_data(as_text=True)
    assert 'id="viewer-info-toggle"' in html, "the phone-only info-panel toggle button must be in the viewer markup"

    repo_root = Path(__file__).resolve().parent.parent
    css = (repo_root / "static" / "review.css").read_text(encoding="utf-8")
    assert "@media (max-width: 480px)" in css, "a phone-specific (narrower than tablet) breakpoint must exist"
    assert ".viewer-panel.info-expanded .viewer-details" in css
    # The pre-existing tablet-stack rule (spec: "desktop and iPad/tablet-
    # width layouts must NOT change") must still be there, unmodified in
    # shape -- same selectors/declarations as before this session.
    assert "@media (max-width: 800px)" in css
    assert "grid-template-columns: 1fr; grid-template-rows: 1fr auto auto;" in css
    print("  #viewer-info-toggle present, new <=480px rule present, existing <=800px tablet rule untouched  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase2h_"))
    print(f"Working in {tmp}")
    try:
        test_add_tag_bulk_unit(tmp)
        test_bulk_add_tag_unions_not_replaces(tmp)
        test_bulk_set_location_and_date_and_hide(tmp)
        test_bulk_edit_edited_by_is_real_user(tmp)
        test_bulk_edit_mixed_batch_partial_success(tmp)
        test_bulk_edit_csrf_auth_and_allowlist(tmp)
        test_mobile_viewer_layout_markup_and_css(tmp)
        print("\nALL PHASE 2H TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
