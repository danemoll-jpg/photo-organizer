"""Exercises Phase 3i -- manual person tagging in review_tool.py (the grid's
mass add/remove across the checkbox multi-select, and the viewer's
single-photo add/remove). Same isolated-temp-dir posture as
test_phase2f.py/test_phase3d.py/test_phase3e.py/test_phase3h.py: never
touches config.yaml, the real DB, the session-secret file, or anything
under E:\\Pics.

Coverage:
  - src/people_tags.py in isolation: the add/remove toggle logic (an
    override row only ever exists when it CONTRADICTS the automatic
    faces/people join -- adding someone already automatically present
    clears a stale 'remove' override rather than writing a redundant
    'add'; removing someone only present via a manual 'add' deletes that
    override outright rather than writing a meaningless 'remove'),
    find_person_by_name/create_named_person/resolve_or_create_person, and
    overrides_for_hashes' batching.
  - The full HTTP flow through POST /api/photo/<hash>/person (viewer,
    single photo) and POST /api/bulk-edit with mode="add_person"/
    "remove_person" (grid, many photos at once) -- adding by cluster_id,
    adding by name (including creating a brand-new person), removing, and
    that a bulk "add_person" by name resolves/creates the person ONCE and
    reuses it across every selected item, not once per item.
  - CRITICAL: removing a person that's automatically present via a real
    detected face must NEVER touch faces.cluster_id/not_a_person -- only
    the DISPLAYED people list for that photo changes; the underlying
    face/cluster assignment the Tkinter labeling window owns is
    completely untouched.
  - CSRF rejection and unauthenticated rejection, same posture as every
    other write route (/api/edit, /api/bulk-edit, /api/favorite).
  - Bad/stale file_hash or cluster_id in a bulk batch fails only that one
    item (recorded in `failed`), same partial-success posture as Phase 2h.
  - The override lookup for a grid page is batched -- exactly ONE query
    per page regardless of page size, same technique test_phase3e.py's/
    test_phase3h.py's own batching proofs used, not assumed.

Usage:
    venv\\Scripts\\python tests\\test_phase3i.py
"""
from __future__ import annotations

import logging
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.auth import hash_password
from src.config import Config
from src.db import connect, init_db
from src.people_tags import (
    add_person_to_photo,
    create_named_person,
    find_person_by_name,
    is_automatically_present,
    overrides_for_hashes,
    remove_person_from_photo,
    resolve_or_create_person,
)


def _csrf_from_html(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"couldn't find csrf_token in page HTML:\n{html[:500]}"
    return m.group(1)


def _login(client, username: str, password: str) -> str:
    login_page = client.get("/login")
    csrf = _csrf_from_html(login_page.get_data(as_text=True))
    resp = client.post("/login", data={"username": username, "password": password, "csrf_token": csrf},
                        follow_redirects=False)
    assert resp.status_code == 302, f"test setup: login must succeed for {username!r}"
    index_html = client.get("/").get_data(as_text=True)
    m = re.search(r"csrfToken:\s*\"([^\"]+)\"", index_html)
    assert m, f"couldn't find csrfToken in index page:\n{index_html[:800]}"
    return m.group(1)


def _setup(tmp: Path):
    """5 photos: h1 has a real detected+named face (Alice, cluster 10);
    h2/h3/h4/h5 have none -- enough to exercise every add/remove
    combination (automatically-present vs. manual-only) without needing
    real image files (nothing here ever hits /image/<hash>)."""
    import importlib

    import review_tool
    importlib.reload(review_tool)

    case = tmp / f"case_{uuid.uuid4().hex[:8]}"
    dest = case / "dest"
    dest.mkdir(parents=True)

    dbp = case / "review.db"
    logger = logging.getLogger("phase3i_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    conn = connect(dbp)

    def insert_photo(file_hash, filename, date_taken):
        path = str(dest / "2021" / "2021-06" / filename)
        conn.execute(
            """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
               date_taken, date_taken_year, date_taken_month, date_source, status, processed_at,
               phase1_verified)
               VALUES (?, ?, ?, ?, 1000, 0, ?, 2021, 6, 'exif', 'sorted', '2021-06-01', 1)""",
            (file_hash, path, path, filename, date_taken),
        )

    for i in range(1, 6):
        insert_photo(f"h{i}", f"0{i}.jpg", f"2021-06-0{i}T09:00:00")

    conn.execute("INSERT INTO people (cluster_id, assigned_name, face_count) VALUES (10, 'Alice', 1)")
    conn.execute(
        "INSERT INTO faces (file_hash, bbox_x, bbox_y, bbox_w, bbox_h, embedding, det_score, cluster_id, "
        "not_a_person, detected_at) VALUES ('h1', 0, 0, 50, 50, ?, 0.9, 10, 0, '2021-06-01T00:00:00')",
        (b"\x00" * 4,),
    )
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
        review_users=[
            {"username": "dan", "password_hash": hash_password("dan-pass-0000")},
        ],
        session_cookie_secure=False, session_lifetime_days=30,
        login_rate_limit_attempts=5, login_rate_limit_window_seconds=900,
    )
    review_tool._cfg = cfg
    review_tool._video_exts = cfg.video_extensions_normalized
    from src.storage import get_storage
    review_tool._storage = get_storage(cfg)
    review_tool._captions_cache = review_tool.CaptionsCache(cfg.captions_path_abs)
    review_tool._captions_cache.refresh()
    review_tool._random_order_cache.clear()
    review_tool._logger = logger
    review_tool.register_auth(review_tool.app, cfg)
    review_tool.app.config["TESTING"] = True

    dan = review_tool.app.test_client()
    dan_csrf = _login(dan, "dan", "dan-pass-0000")

    return review_tool, dan, dan_csrf, dbp


def _person_action(client, csrf, file_hash, action, **kwargs):
    body = {"action": action, "csrf_token": csrf}
    body.update(kwargs)
    return client.post(f"/api/photo/{file_hash}/person", json=body)


# ---------------------------------------------------------------------------
# Part 1: src/people_tags.py in isolation
# ---------------------------------------------------------------------------

def test_toggle_logic_minimizes_override_rows(tmp: Path) -> None:
    print("\n=== src/people_tags.py: add/remove toggle logic keeps the override table minimal ===")
    _rt, _dan, _dc, dbp = _setup(tmp)
    conn = connect(dbp)

    # h1 automatically has Alice (cluster 10) via a real detected face.
    assert is_automatically_present(conn, "h1", 10) is True
    print("  h1 automatically has Alice (real detected face)  OK")

    # Adding Alice to h1 (already automatically true) must be a no-op --
    # no override row written at all.
    add_person_to_photo(conn, "h1", 10, edited_by="dan")
    assert overrides_for_hashes(conn, ["h1"]) == {}, "adding an already-automatic person must not write an override"
    print("  Add Alice to h1 (already automatic) -> no override row written  OK")

    # Removing Alice from h1 (automatically present) DOES need a 'remove'
    # override -- and must NEVER touch the underlying face row.
    remove_person_from_photo(conn, "h1", 10, edited_by="dan")
    overrides = overrides_for_hashes(conn, ["h1"])
    assert overrides["h1"] == [(10, "remove", "Alice")], overrides
    face_row = conn.execute("SELECT cluster_id, not_a_person FROM faces WHERE file_hash='h1'").fetchone()
    assert face_row == (10, 0), f"removing must NEVER touch the underlying face row, got {face_row}"
    print("  Remove Alice from h1 -> 'remove' override written, underlying face row UNTOUCHED  OK")

    # Re-adding Alice to h1 now clears the 'remove' override (reverts to the
    # automatic answer) rather than writing a redundant 'add'.
    add_person_to_photo(conn, "h1", 10, edited_by="dan")
    assert overrides_for_hashes(conn, ["h1"]) == {}, "re-adding must clear the stale 'remove' override"
    print("  Re-add Alice to h1 -> 'remove' override cleared, table empty again  OK")

    # h2 has NO automatic Alice -- adding her needs a real 'add' override.
    assert is_automatically_present(conn, "h2", 10) is False
    add_person_to_photo(conn, "h2", 10, edited_by="dan")
    assert overrides_for_hashes(conn, ["h2"])["h2"] == [(10, "add", "Alice")]
    print("  Add Alice to h2 (not automatic) -> 'add' override written  OK")

    # Removing her from h2 now deletes that 'add' override outright -- no
    # paradoxical 'remove' override for a fact that was never automatic.
    remove_person_from_photo(conn, "h2", 10, edited_by="dan")
    assert overrides_for_hashes(conn, ["h2"]) == {}, "removing a manual-only tag must delete the override, not flip it"
    print("  Remove Alice from h2 -> 'add' override deleted outright, table empty  OK")
    conn.close()


def test_resolve_or_create_person(tmp: Path) -> None:
    print("\n=== src/people_tags.py: find_person_by_name / create_named_person / resolve_or_create_person ===")
    _rt, _dan, _dc, dbp = _setup(tmp)
    conn = connect(dbp)

    assert find_person_by_name(conn, "alice") == 10, "case-insensitive exact match"
    assert find_person_by_name(conn, "Nobody Yet") is None

    new_cid = create_named_person(conn, "  Brand New Person  ")
    row = conn.execute("SELECT assigned_name, face_count, representative_embedding FROM people WHERE cluster_id=?",
                        (new_cid,)).fetchone()
    assert row == ("Brand New Person", 0, None), row
    print(f"  create_named_person: new cluster_id={new_cid}, name trimmed, face_count=0, no embedding  OK")

    same_cid = resolve_or_create_person(conn, "brand new person")
    assert same_cid == new_cid, "resolve_or_create_person must reuse the just-created person by case-insensitive name, not duplicate it"
    print("  resolve_or_create_person reuses the existing person (case-insensitive) instead of duplicating  OK")

    try:
        create_named_person(conn, "   ")
        assert False, "a blank name must raise ValueError"
    except ValueError:
        pass
    print("  create_named_person rejects a blank name  OK")
    conn.close()


# ---------------------------------------------------------------------------
# Part 2: the full HTTP flow
# ---------------------------------------------------------------------------

def test_single_photo_add_remove_via_http(tmp: Path) -> None:
    print("\n=== HTTP: POST /api/photo/<hash>/person -- add by cluster_id, add by name, remove ===")
    _rt, dan, csrf, dbp = _setup(tmp)

    # h2 starts with no people at all.
    resp = _person_action(dan, csrf, "h2", "add", cluster_id=10)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    people = resp.get_json()["item"]["people"]
    assert [p["name"] for p in people] == ["Alice"], people
    print("  Add Alice (cluster_id=10) to h2 -> h2.people == ['Alice']  OK")

    # Add a brand-new person by name -- never existed before.
    resp = _person_action(dan, csrf, "h2", "add", name="Charlie")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    names = sorted(p["name"] for p in resp.get_json()["item"]["people"])
    assert names == ["Alice", "Charlie"], names
    conn = connect(dbp)
    charlie_row = conn.execute("SELECT face_count FROM people WHERE assigned_name='Charlie'").fetchone()
    assert charlie_row == (0,), "a manually-tagged-only person has face_count 0"
    conn.close()
    print("  Add 'Charlie' by name (never existed) -> new person created, h2.people == ['Alice', 'Charlie']  OK")

    # Remove Alice from h2.
    resp = _person_action(dan, csrf, "h2", "remove", cluster_id=10)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    names = [p["name"] for p in resp.get_json()["item"]["people"]]
    assert names == ["Charlie"], names
    print("  Remove Alice from h2 -> h2.people == ['Charlie']  OK")

    # h1 automatically has Alice -- remove her via the API, confirm the
    # underlying face row is untouched (only the DISPLAYED list changes).
    resp = _person_action(dan, csrf, "h1", "remove", cluster_id=10)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["item"]["people"] == []
    conn = connect(dbp)
    face_row = conn.execute("SELECT cluster_id FROM faces WHERE file_hash='h1'").fetchone()
    assert face_row == (10,), "removing via the API must never touch faces.cluster_id"
    conn.close()
    print("  Remove Alice from h1 (automatically present) -> hidden from display, underlying face row UNTOUCHED  OK")


def test_bulk_add_remove_person_via_http(tmp: Path) -> None:
    print("\n=== HTTP: POST /api/bulk-edit mode=add_person/remove_person ===")
    _rt, dan, csrf, dbp = _setup(tmp)

    # Bulk-add a brand-new person "Dima" (by name) to h2, h3, h4 in one call
    # -- must resolve/create the person ONCE, not once per item.
    resp = dan.post("/api/bulk-edit", json={
        "file_hashes": ["h2", "h3", "h4"], "mode": "add_person", "name": "Dima", "csrf_token": csrf,
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["updated"] == 3 and data["failed"] == [] and data["total"] == 3, data
    conn = connect(dbp)
    dima_rows = conn.execute("SELECT cluster_id FROM people WHERE assigned_name='Dima'").fetchall()
    assert len(dima_rows) == 1, f"bulk add_person by name must create exactly ONE person, got {dima_rows}"
    dima_cid = dima_rows[0][0]
    for h in ("h2", "h3", "h4"):
        overrides = overrides_for_hashes(conn, [h])
        assert (dima_cid, "add", "Dima") in overrides.get(h, []), (h, overrides)
    conn.close()
    print(f"  Bulk add 'Dima' to h2/h3/h4 -> ONE new person (cluster_id={dima_cid}), tagged on all 3  OK")

    # A bad/stale file_hash in the batch fails only that one item.
    resp = dan.post("/api/bulk-edit", json={
        "file_hashes": ["h2", "no-such-hash"], "mode": "add_person", "cluster_id": 10, "csrf_token": csrf,
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["updated"] == 1 and len(data["failed"]) == 1 and data["failed"][0]["file_hash"] == "no-such-hash", data
    print("  A bad file_hash in a bulk batch fails only that item, the rest still succeed  OK")

    # Bulk-remove Dima from h2 and h3.
    resp = dan.post("/api/bulk-edit", json={
        "file_hashes": ["h2", "h3"], "mode": "remove_person", "cluster_id": dima_cid, "csrf_token": csrf,
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["updated"] == 2
    conn = connect(dbp)
    for h in ("h2", "h3"):
        remaining = [o for o in overrides_for_hashes(conn, [h]).get(h, []) if o[0] == dima_cid]
        assert remaining == [], f"{h} should have no Dima override left after removal, got {remaining}"
    assert overrides_for_hashes(conn, ["h4"]).get("h4"), "h4 was not in the remove batch, must still have Dima"
    conn.close()
    print("  Bulk remove Dima from h2/h3 -> both cleared, h4 (not in that batch) still has Dima  OK")


def test_csrf_and_auth(tmp: Path) -> None:
    print("\n=== Phase 3i: CSRF + auth posture, same as every other write route ===")
    rt, dan, csrf, dbp = _setup(tmp)

    resp = _person_action(dan, "wrong-token", "h2", "add", cluster_id=10)
    assert resp.status_code == 403, resp.get_data(as_text=True)
    conn = connect(dbp)
    assert overrides_for_hashes(conn, ["h2"]) == {}, "a wrong CSRF token must not apply the tag"
    conn.close()
    print("  wrong csrf_token on /api/photo/<hash>/person -> 403, no change applied  OK")

    anon = rt.app.test_client()
    resp = anon.post("/api/photo/h2/person", json={"action": "add", "cluster_id": 10, "csrf_token": "irrelevant"})
    assert resp.status_code == 401, resp.status_code
    print("  no session at all -> 401 (before_request's auth gate)  OK")

    resp = dan.post("/api/bulk-edit", json={
        "file_hashes": ["h2"], "mode": "add_person", "cluster_id": 10, "csrf_token": "wrong-token",
    })
    assert resp.status_code == 403, resp.get_data(as_text=True)
    print("  wrong csrf_token on /api/bulk-edit mode=add_person -> 403  OK")

    resp = dan.post("/api/bulk-edit", json={
        "file_hashes": ["h2"], "mode": "add_person", "csrf_token": csrf,
    })
    assert resp.status_code == 400, resp.get_data(as_text=True)
    print("  mode=add_person with neither cluster_id nor name -> 400  OK")

    resp = dan.post("/api/bulk-edit", json={
        "file_hashes": ["h2"], "mode": "remove_person", "csrf_token": csrf,
    })
    assert resp.status_code == 400, resp.get_data(as_text=True)
    print("  mode=remove_person with no cluster_id -> 400 (name doesn't apply to remove)  OK")


def test_overrides_batched_not_per_row(tmp: Path) -> None:
    print("\n=== CRITICAL: the override lookup for a grid page is ONE query, not one per row ===")
    _rt, dan, csrf, dbp = _setup(tmp)

    # Give several photos a manual override so there's something to batch.
    for h in ("h2", "h3", "h4", "h5"):
        resp = _person_action(dan, csrf, h, "add", cluster_id=10)
        assert resp.status_code == 200, resp.get_data(as_text=True)

    conn = connect(dbp)
    queries = []
    conn.set_trace_callback(lambda sql: queries.append(sql))
    result = overrides_for_hashes(conn, ["h1", "h2", "h3", "h4", "h5"])
    conn.set_trace_callback(None)
    overrides_queries = [q for q in queries if "photo_people_overrides" in q]
    assert len(overrides_queries) == 1, f"expected exactly 1 query, got {len(overrides_queries)}: {overrides_queries}"
    assert set(result.keys()) == {"h2", "h3", "h4", "h5"}
    print(f"  overrides_for_hashes issued exactly 1 SQL query for 5 file_hashes (found {len(result)} with overrides)  OK")
    conn.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase3i_"))
    print(f"Working in {tmp}")
    try:
        test_toggle_logic_minimizes_override_rows(tmp)
        test_resolve_or_create_person(tmp)
        test_single_photo_add_remove_via_http(tmp)
        test_bulk_add_remove_person_via_http(tmp)
        test_csrf_and_auth(tmp)
        test_overrides_batched_not_per_row(tmp)
        print("\nALL PHASE 3I TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
