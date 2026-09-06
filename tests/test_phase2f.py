"""Exercises Phase 2f -- editable metadata + "Hide from library" in
review_tool.py -- against synthetic fixtures in an isolated temp dir (same
posture as test_phase2b.py/test_phase2c.py/test_phase2d.py: never touches
config.yaml, the real DB, the session-secret file, or anything under
E:\\Pics).

This is the security- and data-integrity-sensitive surface of the session,
so it's tested deliberately, not just "does an edit work":
  - src/edits.py's apply_edit()/has_manual_edit()/manual_edit_fields() in
    isolation (allow-list rejection, validation, the audit row itself).
  - The full HTTP edit flow through a logged-in Flask test client: caption/
    tags/location/date_taken edits, a video's caption "created" from empty,
    CSRF rejection, unauthenticated rejection, an unknown field rejected.
  - _row_to_dict correctly prefers a manually-edited caption/tags value
    over the live captions.jsonl cache the INSTANT it's edited (not just
    after some future reload) -- this is the subtle part: caption/tags
    normally come from the cache, but Phase 2f's edits land in the DB.
  - "Hide from library": excluded by default from /api/photos, revealed by
    show_hidden=1, reversible.
  - The real behavior change to two already-shipped modules: main.py
    load-captions and src/gps_backfill.py must both skip a manually-edited
    field (logging it, counting it) while continuing to update every
    non-edited row exactly as before -- tested for both halves explicitly,
    not just "it doesn't crash".

Usage:
    venv\\Scripts\\python tests\\test_phase2f.py
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
    EDITABLE_FIELDS,
    apply_edit,
    current_tags,
    has_manual_edit,
    manual_edit_fields,
)
from src.gps_backfill import run_gps_extraction
from src.load_captions import load_captions


def _jpeg(path: Path, dt: str) -> Path:
    img = Image.new("RGB", (30, 30), (10, 20, 30))
    img.save(path, "jpeg", exif=piexif.dump({"Exif": {piexif.ExifIFD.DateTimeOriginal: dt.encode()}}))
    return path


def _csrf_from_html(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"couldn't find csrf_token in page HTML:\n{html[:500]}"
    return m.group(1)


# ---------------------------------------------------------------------------
# Part 1: src/edits.py in isolation (no Flask, no review_tool.py)
# ---------------------------------------------------------------------------

def test_apply_edit_unit(tmp: Path) -> None:
    print("\n=== Phase 2f: src/edits.py apply_edit()/has_manual_edit() in isolation ===")
    case = tmp / f"case_{uuid.uuid4().hex[:8]}"
    case.mkdir()
    dbp = case / "test.db"
    logger = logging.getLogger("phase2f_unit_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)
    conn.execute(
        """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
           date_taken, date_taken_year, date_taken_month, date_source, status, processed_at, phase1_verified)
           VALUES ('h1', 'C:\\dest\\2020\\2020-05\\a.jpg', 'C:\\src\\a.jpg', 'a.jpg', 100, 0,
           '2020-05-01T09:00:00', 2020, 5, 'exif', 'sorted', '2020-05-01', 1)"""
    )
    conn.execute("INSERT INTO tags (tag_name) VALUES ('old_tag')")
    conn.execute(
        "INSERT INTO photo_tags (file_hash, tag_id) VALUES ('h1', (SELECT tag_id FROM tags WHERE tag_name='old_tag'))"
    )
    conn.commit()

    assert not has_manual_edit(conn, "h1", "caption")
    result = apply_edit(conn, "h1", "caption", "A lovely photo.", edited_by="dan")
    assert result == "A lovely photo."
    assert has_manual_edit(conn, "h1", "caption") is True
    assert has_manual_edit(conn, "h1", "tags") is False
    row = conn.execute("SELECT caption FROM photos WHERE file_hash='h1'").fetchone()
    assert row[0] == "A lovely photo."
    audit = conn.execute(
        "SELECT old_value, new_value, edited_by FROM photo_edits WHERE file_hash='h1' AND field_name='caption'"
    ).fetchone()
    assert audit[0] is None and audit[1] == "A lovely photo." and audit[2] == "dan"
    print("  caption edit: DB updated, has_manual_edit flips true, audit row records old=None/new/editor  OK")

    old_tags = current_tags(conn, "h1")
    assert old_tags == ["old_tag"]
    result = apply_edit(conn, "h1", "tags", ["Beach", "sunset", "beach"], edited_by="dan")  # dup (case-insensitive) collapsed
    assert result == ["Beach", "sunset"], result
    assert current_tags(conn, "h1") == ["Beach", "sunset"] or sorted(current_tags(conn, "h1")) == sorted(["Beach", "sunset"])
    assert manual_edit_fields(conn, "h1") == {"caption", "tags"}
    print("  tags edit: full replace, case-insensitive de-dup, manual_edit_fields reflects both  OK")

    apply_edit(conn, "h1", "location", "Marietta, GA", edited_by="dan")
    assert conn.execute("SELECT location_name FROM photos WHERE file_hash='h1'").fetchone()[0] == "Marietta, GA"
    apply_edit(conn, "h1", "location", "", edited_by="dan")  # clearing back to empty -- must store NULL, not ""
    assert conn.execute("SELECT location_name FROM photos WHERE file_hash='h1'").fetchone()[0] is None
    print("  location edit: free-text override, clearing to blank stores NULL  OK")

    apply_edit(conn, "h1", "date_taken", "1999-12-31", edited_by="dan")
    row = conn.execute(
        "SELECT date_taken, date_taken_year, date_taken_month, date_source, current_path FROM photos WHERE file_hash='h1'"
    ).fetchone()
    assert row[0] == "1999-12-31" and row[1] == 1999 and row[2] == 12 and row[3] == "manual"
    assert row[4] == "C:\\dest\\2020\\2020-05\\a.jpg", "date_taken edit must NEVER move/rename the actual file"
    print("  date_taken edit: date/year/month/source updated, current_path (the actual file) untouched  OK")

    assert not conn.execute("SELECT excluded FROM photos WHERE file_hash='h1'").fetchone()[0]
    apply_edit(conn, "h1", "excluded", True, edited_by="dan")
    assert conn.execute("SELECT excluded FROM photos WHERE file_hash='h1'").fetchone()[0] == 1
    apply_edit(conn, "h1", "excluded", False, edited_by="dan")
    assert conn.execute("SELECT excluded FROM photos WHERE file_hash='h1'").fetchone()[0] == 0
    print("  excluded (Hide from library) edit: reversible via the same mechanism  OK")

    try:
        apply_edit(conn, "h1", "not_a_real_field", "x", edited_by="dan")
        assert False, "an unknown field name must be rejected, never trusted from the caller"
    except ValueError as e:
        assert "not_a_real_field" in str(e)
    try:
        apply_edit(conn, "missing-hash", "caption", "x", edited_by="dan")
        assert False, "a nonexistent file_hash must be rejected"
    except ValueError:
        pass
    try:
        apply_edit(conn, "h1", "tags", "not-a-list", edited_by="dan")
        assert False, "tags must be a list, not a bare string"
    except ValueError:
        pass
    try:
        apply_edit(conn, "h1", "date_taken", "not-a-date", edited_by="dan")
        assert False, "a malformed date must be rejected"
    except ValueError:
        pass
    try:
        apply_edit(conn, "h1", "caption", "x" * 5000, edited_by="dan")
        assert False, "an over-length caption must be rejected"
    except ValueError:
        pass
    print("  validation: unknown field / missing photo / bad tags type / bad date / over-length caption all rejected  OK")
    conn.close()


# ---------------------------------------------------------------------------
# Part 2: full HTTP edit flow through review_tool.py's Flask test client
# ---------------------------------------------------------------------------

def _setup(tmp: Path):
    """Same shape as test_phase2c.py's _setup (photo/video fixtures + a
    captions.jsonl the live cache reads) PLUS test_phase2d.py's real
    register_auth + login flow, since Phase 2f's edit endpoint is both
    auth-gated and CSRF-protected."""
    import importlib

    import review_tool
    importlib.reload(review_tool)
    from src.storage import get_storage

    case = tmp / f"case_{uuid.uuid4().hex[:8]}"
    dest = case / "dest" / "2021" / "2021-06"
    (dest / "Video").mkdir(parents=True)

    p1 = _jpeg(dest / "01_captioned.jpg", "2021:06:01 09:00:00")
    p2 = _jpeg(dest / "02_uncaptioned.jpg", "2021:06:02 09:00:00")
    video = dest / "Video" / "03_clip.mp4"
    video.write_bytes(b"FAKE-MP4-BYTES")

    dbp = case / "review.db"
    logger = logging.getLogger("phase2f_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)

    def insert(file_hash, path, date_taken, is_video=False):
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at, phase1_verified)
               VALUES (?, ?, ?, ?, ?, 0, ?, 2021, 6, ?, 'sorted', '2021-06-01', 1)""",
            (file_hash, str(path), str(path), path.name, path.stat().st_size, date_taken,
             "container" if is_video else "exif"),
        )

    insert("h1", p1, "2021-06-01T09:00:00")
    insert("h2", p2, "2021-06-02T09:00:00")
    insert("h3_video", video, "2021-06-03T09:00:00", is_video=True)
    conn.commit()
    conn.close()

    captions_path = case / "captions.jsonl"
    with open(captions_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "file_hash": "h1", "path": str(p1), "caption": "An AI-written caption.",
            "tags": ["ai_tag_one", "ai_tag_two"], "date_taken": "2021-06-01T09:00:00",
            "model_used": "test", "processed_at": "2021-06-01T09:05:00",
        }) + "\n")
        # h2/h3_video deliberately left uncaptioned.

    cfg = Config(
        source_folders=[], dest_root=str(case / "dest"), supported_extensions=[".jpg"],
        unsorted_subfolder="_unsorted", dry_run=True, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(dbp), log_dir=str(case / "logs"),
        captions_path=str(captions_path), video_extensions=[".mp4", ".mov", ".avi"],
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

    # The real edit-CSRF token, minted fresh at login (same session["_csrf"]
    # the logout form / REVIEW_CONFIG.csrfToken use) -- grab it the same way
    # the front end would, off the rendered index page.
    index_html = client.get("/").get_data(as_text=True)
    m = re.search(r"csrfToken:\s*\"([^\"]+)\"", index_html)
    assert m, f"couldn't find csrfToken in index page:\n{index_html[:800]}"
    edit_csrf = m.group(1)

    return review_tool, client, edit_csrf, dbp


def _edit(client, edit_csrf, file_hash, field, value, csrf_token=None):
    return client.post(
        f"/api/edit/{file_hash}",
        json={"field": field, "value": value, "csrf_token": csrf_token if csrf_token is not None else edit_csrf},
    )


def test_edit_caption_overrides_live_cache(tmp: Path) -> None:
    print("\n=== Phase 2f: editing a caption overrides the live captions.jsonl cache immediately ===")
    _rt, client, edit_csrf, _dbp = _setup(tmp)

    before = client.get("/api/photos?limit=50").get_json()["items"]
    h1_before = next(i for i in before if i["file_hash"] == "h1")
    assert h1_before["caption"] == "An AI-written caption." and h1_before["captioned"] is True
    print("  before editing: h1 shows the live-cache (AI) caption, as always  OK")

    resp = _edit(client, edit_csrf, "h1", "caption", "A human-corrected caption.")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["item"]["caption"] == "A human-corrected caption."

    after = client.get("/api/photos?limit=50").get_json()["items"]
    h1_after = next(i for i in after if i["file_hash"] == "h1")
    assert h1_after["caption"] == "A human-corrected caption.", h1_after
    assert h1_after["captioned"] is True
    assert "caption" in h1_after["edited_fields"]
    print("  after editing: /api/photos immediately shows the edited caption, NOT the stale cache value  OK")

    nav = client.get("/api/nav?cursor=&dir=next").get_json()["item"]
    assert nav["file_hash"] == "h1" and nav["caption"] == "A human-corrected caption."
    print("  /api/nav (viewer navigation) reflects the same edited value  OK")


def test_edit_tags_full_replace(tmp: Path) -> None:
    print("\n=== Phase 2f: editing tags is a full replace, independent of the caption ===")
    _rt, client, edit_csrf, _dbp = _setup(tmp)

    resp = _edit(client, edit_csrf, "h1", "tags", ["family", "beach"])
    assert resp.status_code == 200
    item = resp.get_json()["item"]
    assert sorted(item["tags"]) == ["beach", "family"]
    assert item["caption"] == "An AI-written caption.", "editing tags must not touch the caption"
    assert item["edited_fields"] == ["tags"]
    print("  tags fully replaced; caption (not edited) still shows its live-cache AI value  OK")


def test_video_add_caption_from_empty(tmp: Path) -> None:
    print("\n=== Phase 2f: a video's caption is CREATED from empty, same mechanism as editing ===")
    _rt, client, edit_csrf, _dbp = _setup(tmp)

    before = client.get("/api/photos?media_type=video&limit=50").get_json()["items"]
    v_before = before[0]
    assert v_before["captioned"] is False and v_before["caption"] is None
    print("  before: video has no caption at all (never automated -- Phase 2 doesn't caption video)  OK")

    resp = _edit(client, edit_csrf, "h3_video", "caption", "Grandma's birthday party, filmed on the porch.")
    assert resp.status_code == 200
    item = resp.get_json()["item"]
    assert item["captioned"] is True
    assert item["caption"] == "Grandma's birthday party, filmed on the porch."
    print("  after: caption created via the exact same /api/edit endpoint as any photo edit  OK")


def test_edit_location_and_date_taken(tmp: Path) -> None:
    print("\n=== Phase 2f: location free-text override + date_taken (DB-only, file untouched) ===")
    _rt, client, edit_csrf, _dbp = _setup(tmp)

    resp = _edit(client, edit_csrf, "h2", "location", "Marietta, GA")
    assert resp.status_code == 200 and resp.get_json()["item"]["location_name"] == "Marietta, GA"
    print("  location override applied  OK")

    before_path = client.get("/api/photos?limit=50").get_json()["items"]
    h2_path_before = next(i for i in before_path if i["file_hash"] == "h2")["current_path"]
    resp = _edit(client, edit_csrf, "h2", "date_taken", "1999-12-25")
    assert resp.status_code == 200
    item = resp.get_json()["item"]
    assert item["date_taken"].startswith("1999-12-25")
    after_path = client.get("/api/photos?limit=50").get_json()["items"]
    h2_path_after = next(i for i in after_path if i["file_hash"] == "h2")["current_path"]
    assert h2_path_after == h2_path_before, "editing date_taken must NEVER move/rename/re-sort the actual file"
    print("  date_taken edited; current_path (the real file location) is unchanged  OK")


def test_csrf_and_auth_and_allowlist(tmp: Path) -> None:
    print("\n=== Phase 2f: CSRF mismatch / unauthenticated / unknown-field rejections ===")
    _rt, client, edit_csrf, _dbp = _setup(tmp)

    resp = _edit(client, edit_csrf, "h1", "caption", "nope", csrf_token="wrong-token")
    assert resp.status_code == 403, resp.get_data(as_text=True)
    still = client.get("/api/photos?limit=50").get_json()["items"]
    h1 = next(i for i in still if i["file_hash"] == "h1")
    assert h1["caption"] == "An AI-written caption.", "a CSRF-mismatched edit must never actually apply"
    print("  wrong csrf_token -> 403, no change applied  OK")

    resp = _edit(client, edit_csrf, "h1", "not_a_real_field", "x")
    assert resp.status_code == 400
    print("  an unknown field name -> 400, rejected before ever reaching apply_edit()  OK")

    client.post("/logout", data={"csrf_token": edit_csrf})
    resp = _edit(client, edit_csrf, "h1", "caption", "should not apply")
    assert resp.status_code == 401, resp.get_data(as_text=True)
    print("  logged out -> 401, same as every other /api/* route  OK")


def test_hide_from_library(tmp: Path) -> None:
    print("\n=== Phase 2f: \"Hide from library\" -- excluded by default, reversible via show_hidden ===")
    _rt, client, edit_csrf, _dbp = _setup(tmp)

    all_before = {i["file_hash"] for i in client.get("/api/photos?limit=50").get_json()["items"]}
    assert all_before == {"h1", "h2", "h3_video"}
    stats_before = client.get("/api/stats").get_json()
    assert stats_before["total_photos"] == 3

    resp = _edit(client, edit_csrf, "h2", "excluded", True)
    assert resp.status_code == 200 and resp.get_json()["item"]["excluded"] is True

    visible = {i["file_hash"] for i in client.get("/api/photos?limit=50").get_json()["items"]}
    assert visible == {"h1", "h3_video"}, "hidden item must disappear from the default (non-show_hidden) grid"
    stats_after = client.get("/api/stats").get_json()
    assert stats_after["total_photos"] == 2, "hidden item must also disappear from /api/stats' count"
    nav_seq = []
    cursor = ""
    for _ in range(5):
        item = client.get(f"/api/nav?cursor={cursor}&dir=next").get_json()["item"]
        if item is None:
            break
        nav_seq.append(item["file_hash"])
        cursor = item["current_path"]
    assert "h2" not in nav_seq, "hidden item must not appear in sequential viewer navigation either"
    print("  hidden item vanishes from /api/photos, /api/stats, and /api/nav  OK")

    shown_hidden = {i["file_hash"] for i in client.get("/api/photos?show_hidden=1&limit=50").get_json()["items"]}
    assert shown_hidden == {"h1", "h2", "h3_video"}
    h2_shown = next(i for i in client.get("/api/photos?show_hidden=1&limit=50").get_json()["items"] if i["file_hash"] == "h2")
    assert h2_shown["excluded"] is True
    print("  show_hidden=1 reveals it again (badged via excluded=true), mixed in with visible items  OK")

    resp = _edit(client, edit_csrf, "h2", "excluded", False)
    assert resp.status_code == 200 and resp.get_json()["item"]["excluded"] is False
    visible_again = {i["file_hash"] for i in client.get("/api/photos?limit=50").get_json()["items"]}
    assert visible_again == {"h1", "h2", "h3_video"}, "un-hiding must be fully reversible"
    print("  un-hiding restores it to the default view -- fully reversible, not one-way  OK")


# ---------------------------------------------------------------------------
# Part 3: main.py load-captions / src/gps_backfill.py -- manual-edit skip
# ---------------------------------------------------------------------------

def test_load_captions_skips_manually_edited_fields(tmp: Path) -> None:
    print("\n=== Phase 2f: main.py load-captions skips a manually-edited caption/tags, per-field ===")
    case = tmp / f"case_{uuid.uuid4().hex[:8]}"
    case.mkdir()
    dbp = case / "test.db"
    logger = logging.getLogger("phase2f_loadcap_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)

    def insert(file_hash):
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at, phase1_verified)
               VALUES (?, ?, ?, ?, 1, 0, '2021-01-01', 2021, 1, 'exif', 'sorted', '2021-01-01', 1)""",
            (file_hash, f"C:\\dest\\{file_hash}.jpg", f"C:\\src\\{file_hash}.jpg", f"{file_hash}.jpg"),
        )

    insert("h_normal")     # never manually touched -- must keep updating normally
    insert("h_cap_edited")  # caption manually edited -- caption load must skip, tags must NOT
    insert("h_tags_edited")  # tags manually edited -- tags load must skip, caption must NOT
    conn.commit()

    apply_edit(conn, "h_cap_edited", "caption", "Human-written caption, do not overwrite.", edited_by="dan")
    apply_edit(conn, "h_tags_edited", "tags", ["human_tag"], edited_by="dan")
    conn.commit()
    conn.close()

    captions_path = case / "captions.jsonl"
    with open(captions_path, "w", encoding="utf-8") as f:
        for fh in ("h_normal", "h_cap_edited", "h_tags_edited"):
            f.write(json.dumps({
                "file_hash": fh, "path": f"C:\\dest\\{fh}.jpg", "caption": f"AI caption for {fh}.",
                "tags": ["ai_tag"], "date_taken": "2021-01-01T00:00:00", "model_used": "test",
                "processed_at": "2021-01-01T00:05:00",
            }) + "\n")

    conn = connect(dbp)
    stats = load_captions(captions_path, conn, logger)
    assert stats.caption_skipped_manual_edit == 1, stats.summary()
    assert stats.tags_skipped_manual_edit == 1, stats.summary()
    assert stats.captions_loaded == 2, stats.summary()  # h_normal + h_tags_edited (caption not edited there)
    print(f"  stats: {stats.summary()}")

    normal = conn.execute("SELECT caption FROM photos WHERE file_hash='h_normal'").fetchone()[0]
    assert normal == "AI caption for h_normal.", "a non-edited row must keep updating exactly as before"
    assert sorted(current_tags(conn, "h_normal")) == ["ai_tag"]
    print("  h_normal (no manual edits at all): caption AND tags loaded normally, unaffected  OK")

    cap_edited_caption = conn.execute("SELECT caption FROM photos WHERE file_hash='h_cap_edited'").fetchone()[0]
    assert cap_edited_caption == "Human-written caption, do not overwrite.", "manual caption edit must survive a load-captions run"
    assert sorted(current_tags(conn, "h_cap_edited")) == ["ai_tag"], "tags on this row were NOT manually edited -- must still load normally"
    print("  h_cap_edited: caption protected (unchanged), but its tags still loaded normally (independent per-field)  OK")

    tags_edited_tags = current_tags(conn, "h_tags_edited")
    assert tags_edited_tags == ["human_tag"], "manual tags edit must survive a load-captions run"
    tags_edited_caption = conn.execute("SELECT caption FROM photos WHERE file_hash='h_tags_edited'").fetchone()[0]
    assert tags_edited_caption == "AI caption for h_tags_edited.", "caption on this row was NOT manually edited -- must still load normally"
    print("  h_tags_edited: tags protected (unchanged), but its caption still loaded normally (independent per-field)  OK")
    conn.close()


def test_gps_backfill_skips_manually_edited_location(tmp: Path) -> None:
    print("\n=== Phase 2f: src/gps_backfill.py skips a manually-edited location ===")
    case = tmp / f"case_{uuid.uuid4().hex[:8]}"
    case.mkdir()

    p_normal = case / "normal.jpg"
    p_edited = case / "edited.jpg"
    Image.new("RGB", (20, 20), (200, 0, 0)).save(p_normal, "jpeg")  # no GPS EXIF -- resolves to "not found"
    Image.new("RGB", (20, 20), (0, 200, 0)).save(p_edited, "jpeg")  # no GPS EXIF either

    dbp = case / "test.db"
    logger = logging.getLogger("phase2f_gps_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)

    def insert(file_hash, path):
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at, phase1_verified)
               VALUES (?, ?, ?, ?, ?, 0, '2021-01-01', 2021, 1, 'exif', 'sorted', '2021-01-01', 1)""",
            (file_hash, str(path), str(path), path.name, path.stat().st_size),
        )

    insert("h_normal", p_normal)
    insert("h_edited", p_edited)
    conn.commit()

    apply_edit(conn, "h_edited", "location", "Manually Chosen Place, GA", edited_by="dan")
    conn.commit()
    # Sanity: gps_checked must still be 0 pre-run for both -- the manual
    # edit alone doesn't mark a row checked; only gps_backfill running does.
    assert conn.execute("SELECT gps_checked FROM photos WHERE file_hash='h_edited'").fetchone()[0] == 0

    cfg = Config(
        source_folders=[], dest_root=str(case), supported_extensions=[".jpg"],
        unsorted_subfolder="_unsorted", dry_run=True, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(dbp), log_dir=str(case / "logs"),
    )
    stats = run_gps_extraction(cfg, conn, logger)
    assert stats.skipped_manual_edit == 1, stats.summary()
    assert stats.scanned == 2, stats.summary()
    print(f"  stats: {stats.summary()}")

    edited_row = conn.execute("SELECT location_name, gps_checked FROM photos WHERE file_hash='h_edited'").fetchone()
    assert edited_row[0] == "Manually Chosen Place, GA", "manual location edit must survive a gps_backfill run"
    assert edited_row[1] == 1, "the row should still be marked gps_checked=1 so it isn't rescanned forever"
    print("  h_edited: location protected (unchanged), but marked gps_checked=1 (won't be rescanned pointlessly)  OK")

    normal_row = conn.execute("SELECT location_name, gps_checked FROM photos WHERE file_hash='h_normal'").fetchone()
    assert normal_row[1] == 1, "a non-edited row must still be processed normally"
    print("  h_normal (no manual edit): processed normally, gps_checked=1  OK")
    conn.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase2f_"))
    print(f"Working in {tmp}")
    try:
        test_apply_edit_unit(tmp)
        test_edit_caption_overrides_live_cache(tmp)
        test_edit_tags_full_replace(tmp)
        test_video_add_caption_from_empty(tmp)
        test_edit_location_and_date_taken(tmp)
        test_csrf_and_auth_and_allowlist(tmp)
        test_hide_from_library(tmp)
        test_load_captions_skips_manually_edited_fields(tmp)
        test_gps_backfill_skips_manually_edited_location(tmp)
        print("\nALL PHASE 2F TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
