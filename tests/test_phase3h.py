"""Exercises Phase 3h -- per-user favorites in review_tool.py, with
visibility into who favorited what. Same isolated-temp-dir posture as
test_phase2f.py/test_phase3d.py/test_phase3e.py: never touches config.yaml,
the real DB, the session-secret file, or anything under E:\\Pics.

Coverage:
  - src/favorites.py in isolation: set/unset is per-(file_hash, username)
    and fully independent between users, idempotent, raises on a bad hash.
  - The full HTTP flow through TWO separately logged-in Flask test clients
    (review_tool.app.test_client() gives each its own cookie jar/session) --
    favoriting/un-favoriting as one user never touches another user's mark
    on the same photo, `favorited_by` always lists everyone, and
    `favorited_by_me` is correctly computed relative to whichever session
    is asking (dan sees True after HE favorites it even while juliana's
    mark is also present; juliana still sees False until SHE favorites it
    too).
  - CSRF rejection and unauthenticated rejection, same posture as every
    other write route (/api/edit, /api/bulk-edit).
  - The favorite_by filter's three shapes (__me__, __any__, a literal
    username) and composition with an existing filter (date_from) -- and
    that it applies to /api/photos, /api/nav, and /api/stats alike, same
    "lives in _build_filters, every route picks it up for free" pattern
    every prior filter in this project established.
  - CRITICAL, per this session's own instruction: the favorites lookup for
    a grid page is batched -- exactly ONE query per page regardless of page
    size, proven via sqlite3's trace callback (same technique
    test_phase3e.py::test_people_batched_not_per_row already used for the
    people join), not assumed.
  - /api/facets' new `review_users` list.

Usage:
    venv\\Scripts\\python tests\\test_phase3h.py
"""
from __future__ import annotations

import logging
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.auth import hash_password
from src.config import Config
from src.db import connect, init_db
from src.favorites import favorites_for_hashes, is_favorited, set_favorite


def _csrf_from_html(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"couldn't find csrf_token in page HTML:\n{html[:500]}"
    return m.group(1)


def _login(client, username: str, password: str) -> str:
    """Logs `client` in and returns the edit/favorite CSRF token minted for
    its session (same window.REVIEW_CONFIG.csrfToken every write route
    already checks -- see review_tool.py's index())."""
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
    """Two real logged-in users (dan, juliana) against one shared library of
    3 photos -- enough to exercise "favorited by nobody" / "favorited by
    one" / "favorited by both" without needing real image files (nothing
    here ever hits /image/<hash>, only the JSON API)."""
    import importlib

    import review_tool
    importlib.reload(review_tool)

    case = tmp / f"case_{uuid.uuid4().hex[:8]}"
    dest = case / "dest"
    dest.mkdir(parents=True)

    dbp = case / "review.db"
    logger = logging.getLogger("phase3h_test")
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

    insert_photo("h1", "01.jpg", "2021-06-01T09:00:00")
    insert_photo("h2", "02.jpg", "2021-06-02T09:00:00")
    insert_photo("h3", "03.jpg", "2021-06-03T09:00:00")
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
            {"username": "juliana", "password_hash": hash_password("juliana-pass-0000")},
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
    juliana = review_tool.app.test_client()
    juliana_csrf = _login(juliana, "juliana", "juliana-pass-0000")

    return review_tool, dan, dan_csrf, juliana, juliana_csrf, dbp


def _favorite(client, csrf, file_hash, favorited, csrf_token=None):
    return client.post(
        f"/api/favorite/{file_hash}",
        json={"favorited": favorited, "csrf_token": csrf_token if csrf_token is not None else csrf},
    )


# ---------------------------------------------------------------------------
# Part 1: src/favorites.py in isolation
# ---------------------------------------------------------------------------

def test_favorites_module_basic(tmp: Path) -> None:
    print("\n=== Phase 3h: src/favorites.py -- set/unset, per-user independence ===")
    _rt, _dan, _dc, _jul, _jc, dbp = _setup(tmp)
    conn = connect(dbp)

    assert is_favorited(conn, "h1", "dan") is False
    set_favorite(conn, "h1", "dan", True)
    assert is_favorited(conn, "h1", "dan") is True
    assert is_favorited(conn, "h1", "juliana") is False, "favoriting as dan must not affect juliana's mark on the same photo"
    print("  dan favorites h1 -> only dan's mark is set, juliana's is untouched  OK")

    set_favorite(conn, "h1", "juliana", True)
    assert favorites_for_hashes(conn, ["h1"])["h1"] == ["dan", "juliana"], favorites_for_hashes(conn, ["h1"])
    print("  juliana also favorites h1 -> both listed, sorted  OK")

    set_favorite(conn, "h1", "dan", False)
    assert is_favorited(conn, "h1", "dan") is False
    assert is_favorited(conn, "h1", "juliana") is True, "un-favoriting as dan must not remove juliana's mark"
    print("  dan un-favorites h1 -> only dan's mark is removed, juliana's remains  OK")

    # Idempotent: favoriting an already-favorited photo, or un-favoriting one
    # that isn't, is a harmless no-op, not an error.
    set_favorite(conn, "h1", "juliana", True)
    set_favorite(conn, "h2", "dan", False)
    assert favorites_for_hashes(conn, ["h1"])["h1"] == ["juliana"]
    assert favorites_for_hashes(conn, ["h2"]) == {}
    print("  re-favoriting/un-favoriting an already-matching state is a no-op  OK")

    try:
        set_favorite(conn, "no-such-hash", "dan", True)
        assert False, "favoriting a nonexistent file_hash must raise ValueError"
    except ValueError:
        pass
    print("  a bad file_hash raises ValueError, same posture as apply_edit()  OK")

    assert favorites_for_hashes(conn, []) == {}, "empty input must return {} without touching the DB"
    print("  favorites_for_hashes([]) short-circuits to {}  OK")
    conn.close()


# ---------------------------------------------------------------------------
# Part 2: full HTTP flow, two real logged-in users
# ---------------------------------------------------------------------------

def test_toggle_via_http_independent_per_user(tmp: Path) -> None:
    print("\n=== Phase 3h: /api/favorite/<hash> via two real logged-in sessions ===")
    _rt, dan, dan_csrf, juliana, juliana_csrf, _dbp = _setup(tmp)

    resp = _favorite(dan, dan_csrf, "h1", True)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    item = resp.get_json()["item"]
    assert item["favorited_by"] == ["dan"], item["favorited_by"]
    assert item["favorited_by_me"] is True
    print("  dan favorites h1 -> favorited_by=[dan], favorited_by_me=True (from dan's session)  OK")

    # juliana's own view of the SAME photo right now: favorited_by lists
    # dan (everyone who has), but favorited_by_me is False -- it's relative
    # to whichever session is asking, not a global flag.
    resp = juliana.get("/api/nav?dir=next")
    jul_item = resp.get_json()["item"]
    assert jul_item["file_hash"] == "h1"
    assert jul_item["favorited_by"] == ["dan"]
    assert jul_item["favorited_by_me"] is False
    print("  juliana's view of h1 -> favorited_by still shows dan, but her own favorited_by_me is False  OK")

    resp = _favorite(juliana, juliana_csrf, "h1", True)
    item = resp.get_json()["item"]
    assert item["favorited_by"] == ["dan", "juliana"], item["favorited_by"]
    assert item["favorited_by_me"] is True
    print("  juliana also favorites h1 -> favorited_by=[dan, juliana], her own favorited_by_me=True  OK")

    resp = _favorite(dan, dan_csrf, "h1", False)
    item = resp.get_json()["item"]
    assert item["favorited_by"] == ["juliana"], item["favorited_by"]
    assert item["favorited_by_me"] is False
    print("  dan un-favorites h1 -> only juliana remains, dan's own favorited_by_me=False  OK")

    # Re-fetch through the grid too (not just the favorite-toggle response
    # itself) to confirm the change is visible through the normal read path.
    resp = dan.get("/api/photos?limit=50")
    by_hash = {i["file_hash"]: i for i in resp.get_json()["items"]}
    assert by_hash["h1"]["favorited_by"] == ["juliana"]
    assert by_hash["h1"]["favorited_by_me"] is False
    assert by_hash["h2"]["favorited_by"] == []
    assert by_hash["h2"]["favorited_by_me"] is False
    print("  grid page reflects the same state -- h1 favorited by juliana only, h2 untouched  OK")


def test_csrf_and_auth(tmp: Path) -> None:
    print("\n=== Phase 3h: CSRF + auth posture, same as /api/edit and /api/bulk-edit ===")
    rt, dan, dan_csrf, _juliana, _jc, _dbp = _setup(tmp)

    resp = _favorite(dan, dan_csrf, "h1", True, csrf_token="wrong-token")
    assert resp.status_code == 403, resp.get_data(as_text=True)
    conn = connect(rt._cfg.db_path_abs)
    assert favorites_for_hashes(conn, ["h1"]) == {}, "a wrong CSRF token must not apply the favorite"
    conn.close()
    print("  wrong csrf_token -> 403, no change applied  OK")

    anon = rt.app.test_client()
    resp = anon.post("/api/favorite/h1", json={"favorited": True, "csrf_token": "irrelevant"})
    assert resp.status_code == 401, resp.status_code
    print("  no session at all -> 401 (before_request's auth gate, same as every other /api/* route)  OK")


# ---------------------------------------------------------------------------
# Part 3: the favorite_by filter
# ---------------------------------------------------------------------------

def test_favorite_by_filter(tmp: Path) -> None:
    print("\n=== Phase 3h: favorite_by filter (__me__ / __any__ / a literal username) ===")
    _rt, dan, dan_csrf, juliana, juliana_csrf, _dbp = _setup(tmp)

    _favorite(dan, dan_csrf, "h1", True)
    _favorite(juliana, juliana_csrf, "h2", True)
    # h3 is favorited by nobody.

    resp = dan.get("/api/photos?favorite_by=__me__&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1"}, hashes
    print("  favorite_by=__me__ (as dan) -> only h1 (dan's own favorite)  OK")

    resp = juliana.get("/api/photos?favorite_by=__me__&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h2"}, hashes
    print("  favorite_by=__me__ (as juliana) -> only h2 -- __me__ resolves per-session, never a shared value  OK")

    resp = dan.get("/api/photos?favorite_by=__any__&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h1", "h2"}, hashes
    print("  favorite_by=__any__ -> both h1 and h2 (favorited by someone), h3 excluded  OK")

    resp = dan.get("/api/photos?favorite_by=juliana&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h2"}, hashes
    print("  favorite_by=juliana (a literal username, requested by dan) -> only h2  OK")

    resp = dan.get("/api/photos?favorite_by=nobody-such-user&limit=50")
    assert resp.get_json()["items"] == []
    print("  favorite_by=<unknown username> -> matches nothing, not an error  OK")

    # Composition with an existing filter (date_from) -- h2 is 2021-06-02.
    resp = dan.get("/api/photos?favorite_by=__any__&date_from=2021-06-02&limit=50")
    hashes = {i["file_hash"] for i in resp.get_json()["items"]}
    assert hashes == {"h2"}, hashes
    print("  composes correctly with date_from (h1 is 06-01, excluded once date_from=06-02 is added)  OK")

    # Applies to /api/stats and /api/nav too, same free ride every other
    # _build_filters-based param already gets.
    resp = dan.get("/api/stats?favorite_by=__any__")
    assert resp.get_json()["total_photos"] == 2, resp.get_json()
    print("  /api/stats respects favorite_by too  OK")

    resp = dan.get("/api/nav?favorite_by=__me__&dir=next")
    assert resp.get_json()["item"]["file_hash"] == "h1"
    # cursor is a file_hash (see review_tool.py's module docstring on the
    # grid/viewer's chronological-order follow-up) -- "h1" itself, no
    # lookup needed.
    resp = dan.get("/api/nav?favorite_by=__me__&dir=next&cursor=h1")
    assert resp.get_json()["item"] is None, "only h1 matches dan's __me__ filter -- stepping past it must reach the end"
    print("  /api/nav respects favorite_by too (sequential stepping stops correctly at the end)  OK")


# ---------------------------------------------------------------------------
# Part 4: CRITICAL -- batched per grid page, not per row
# ---------------------------------------------------------------------------

def test_favorites_batched_not_per_row(tmp: Path) -> None:
    print("\n=== Phase 3h: grid page fetch batches the favorites lookup, not one query per row ===")
    _rt, dan, dan_csrf, _juliana, _jc, dbp = _setup(tmp)

    # A bigger fixture directly against the DB -- enough rows that an
    # accidental N+1 (one favorites query per row) would show up plainly in
    # the statement count, not just as a timing number that could be noise.
    conn = connect(dbp)
    n = 150
    photo_rows = []
    fav_rows = []
    for i in range(n):
        h = f"bulk{i:04d}"
        path = str(Path(_rt._cfg.dest_root_path) / "2022" / "2022-01" / f"{h}.jpg")
        photo_rows.append((h, path, path, f"{h}.jpg", 1000, 0, f"2022-01-01T00:00:{i % 60:02d}",
                            2022, 1, "exif", "sorted", "2022-01-01", 1))
        fav_rows.append((h, "dan", "2022-01-01T00:00:00"))
    conn.executemany(
        """INSERT INTO photos (file_hash, current_path, original_path, filename, file_size, file_mtime,
           date_taken, date_taken_year, date_taken_month, date_source, status, processed_at, phase1_verified)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        photo_rows,
    )
    conn.executemany(
        "INSERT INTO favorites (file_hash, username, favorited_at) VALUES (?, ?, ?)", fav_rows,
    )
    conn.commit()
    conn.close()

    # Count actual SQL statements for one page fetch via a fresh traced
    # connection reusing review_tool's own query path (_fetch_page) -- the
    # real proof against N+1, not just "it happened to be fast once". Note:
    # _row_to_dict also calls manual_edit_fields()/_people_for_hashes() per
    # the existing pre-Phase-3h pattern -- what THIS test must confirm is
    # that the favorites lookup specifically (identifiable by its
    # distinctive "FROM favorites WHERE file_hash IN" clause) appears
    # exactly once per page fetch, no matter how many rows are on it.
    # _row_to_dict reads Flask's `session` (to compute favorited_by_me) --
    # in real use there's always an active request (every caller here is a
    # route handler), but calling _build_filters/_fetch_page directly like
    # this needs one pushed manually, same as any other Flask internals
    # test. An empty session (no "user") is fine -- this test only checks
    # `favorited_by`, not `favorited_by_me`.
    with _rt.app.test_request_context():
        counts = {}
        for limit in (10, 150):
            conn = _rt.get_db()
            statements = []
            conn.set_trace_callback(statements.append)
            try:
                where, params = _rt._build_filters({"folder": "2022-01"})
                items, _has_next, _has_prev = _rt._fetch_page(conn, where, params, limit=limit, after=None, before=None)
                assert len(items) == min(limit, n), (limit, len(items))
            finally:
                conn.set_trace_callback(None)
                conn.close()
            favorites_query_count = sum(1 for s in statements if "FROM favorites WHERE file_hash IN" in s)
            counts[limit] = favorites_query_count
            print(f"  limit={limit}: {len(items)} rows fetched, {favorites_query_count} favorites SELECT(s) issued")

        assert counts[10] == 1, counts
        assert counts[150] == 1, counts
        print(f"  favorites-lookup query count stays exactly 1 regardless of page size ({counts[10]} vs {counts[150]}) -- no N+1  OK")

        conn = _rt.get_db()
        try:
            where, params = _rt._build_filters({"folder": "2022-01"})
            t0 = time.time()
            items, _has_next, _has_prev = _rt._fetch_page(conn, where, params, limit=150, after=None, before=None)
            elapsed = time.time() - t0
        finally:
            conn.close()
        assert elapsed < 1.0, f"a 150-row page fetch (incl. the favorites lookup) took {elapsed:.3f}s -- unexpectedly slow"
        print(f"  a 150-row page (incl. the favorites lookup) fetched in {elapsed*1000:.1f}ms  OK")

        for item in items:
            assert item["favorited_by"] == ["dan"], item["favorited_by"]
        print("  every bulk row correctly carries dan's favorite in favorited_by  OK")


# ---------------------------------------------------------------------------
# Part 5: /api/facets' new review_users list
# ---------------------------------------------------------------------------

def test_facets_review_users(tmp: Path) -> None:
    print("\n=== Phase 3h: /api/facets exposes the configured review_users list ===")
    _rt, dan, _dc, _juliana, _jc, _dbp = _setup(tmp)
    resp = dan.get("/api/facets")
    assert resp.get_json()["review_users"] == ["dan", "juliana"], resp.get_json()["review_users"]
    print("  review_users == ['dan', 'juliana'], sorted  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase3h_"))
    print(f"Working in {tmp}")
    try:
        test_favorites_module_basic(tmp)
        test_toggle_via_http_independent_per_user(tmp)
        test_csrf_and_auth(tmp)
        test_favorite_by_filter(tmp)
        test_favorites_batched_not_per_row(tmp)
        test_facets_review_users(tmp)
        print("\nALL PHASE 3H TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
