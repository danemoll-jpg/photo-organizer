"""Exercises Phase 2d's auth (src/auth.py), review_tool.py's login-gated
routes, and the storage abstraction (src/storage.py) against synthetic
fixtures in an isolated temp dir (same posture as test_phase2b.py/
test_phase2c.py — never touches config.yaml, the real DB, session-secret
file, or anything under E:\\Pics).

This is the security-sensitive surface of the session, so it's tested
more deliberately than "does it still work when logged in":
  - the actual login/logout HTTP flow (not just unit-testing the pieces)
  - session persistence across separate requests
  - rate limiting actually blocking after N failures, and NOT blocking a
    different (IP, username) key
  - the open-redirect guard on `next`
  - unauthenticated API calls get 401 JSON, not a redirect (review.js
    depends on this to bounce itself to /login)
  - CSRF token mismatch is rejected
  - the storage abstraction's local backend + unknown-backend error

Usage:
    venv\\Scripts\\python tests\\test_phase2d.py
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.auth import LoginRateLimiter, hash_password, verify_login, _is_safe_next
from src.config import Config
from src.storage import LocalDiskStorage, get_storage


def _csrf_from_html(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"couldn't find csrf_token in login page HTML:\n{html[:500]}"
    return m.group(1)


def _setup(tmp: Path, *, users=None, rate_limiter=None):
    """Wires review_tool.py's module globals against a throwaway config/DB
    (same pattern as test_phase2b.py/test_phase2c.py's _setup), then wires
    a FRESH auth registration on top — review_tool.app is a module-level
    singleton reused across test functions, so before_request/login/logout
    would otherwise pile up duplicate route registrations across calls.
    Rebuilding the Flask app object itself per test keeps each test's auth
    state (rate limiter, secret key) fully isolated."""
    import importlib

    import review_tool
    importlib.reload(review_tool)  # fresh Flask app + fresh before_request/login/logout registration
    from src.db import connect, init_db

    case = tmp / f"case_{uuid.uuid4().hex[:8]}"
    dest = case / "dest"
    dest.mkdir(parents=True)
    dbp = case / "review.db"
    import logging
    logger = logging.getLogger("phase2d_test")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.WARNING)
    init_db(dbp, logger=logger)
    connect(dbp).close()

    cfg = Config(
        source_folders=[], dest_root=str(dest), supported_extensions=[".jpg"],
        unsorted_subfolder="_unsorted", dry_run=True, hash_algorithm="sha256",
        collision_suffix_length=8, db_path=str(dbp), log_dir=str(case / "logs"),
        captions_path=str(case / "captions.jsonl"), video_extensions=[".mp4", ".mov", ".avi"],
        review_page_size=40,
        review_users=users if users is not None else [
            {"username": "dan", "password_hash": hash_password("correct-horse-battery")},
        ],
        session_cookie_secure=False,
        session_lifetime_days=30,
        login_rate_limit_attempts=3,
        login_rate_limit_window_seconds=900,
    )
    review_tool._cfg = cfg
    review_tool._video_exts = cfg.video_extensions_normalized
    review_tool._storage = get_storage(cfg)
    review_tool._captions_cache = review_tool.CaptionsCache(cfg.captions_path_abs)
    review_tool._captions_cache.refresh()

    limiter = rate_limiter or LoginRateLimiter(
        max_attempts=cfg.login_rate_limit_attempts,
        window_seconds=cfg.login_rate_limit_window_seconds,
    )
    review_tool.register_auth(review_tool.app, cfg, rate_limiter=limiter)
    review_tool.app.config["TESTING"] = True
    return review_tool, review_tool.app.test_client(), limiter


def test_verify_login_and_dummy_hash_path(tmp: Path) -> None:
    print("\n=== Phase 2d: verify_login (correct / wrong password / unknown user) ===")
    from src.config import Config as _Cfg
    cfg = _Cfg(
        source_folders=[], dest_root="x", supported_extensions=[".jpg"], unsorted_subfolder="u",
        dry_run=True, hash_algorithm="sha256", collision_suffix_length=8, db_path="x.db", log_dir="logs",
        review_users=[{"username": "dan", "password_hash": hash_password("s3cret-pw")}],
    )
    assert verify_login(cfg, "dan", "s3cret-pw") is True
    assert verify_login(cfg, "dan", "wrong") is False
    assert verify_login(cfg, "nobody", "whatever") is False  # must not raise/crash on an unknown username
    print("  correct password OK, wrong password rejected, unknown username handled gracefully  OK")


def test_open_redirect_guard() -> None:
    print("\n=== Phase 2d: _is_safe_next open-redirect guard ===")
    assert _is_safe_next("/") is True
    assert _is_safe_next("/some/page?x=1") is True
    assert _is_safe_next(None) is False
    assert _is_safe_next("") is False
    assert _is_safe_next("http://evil.example.com/") is False
    assert _is_safe_next("//evil.example.com/") is False  # protocol-relative
    assert _is_safe_next("\\\\evil.example.com") is False
    print("  relative paths accepted, absolute/protocol-relative URLs rejected  OK")


def test_login_flow_and_session_persistence(tmp: Path) -> None:
    print("\n=== Phase 2d: full login/logout HTTP flow + session persistence ===")
    _rt, client, _limiter = _setup(tmp)

    # Unauthenticated: a page navigation redirects to /login...
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"], resp.headers.get("Location")
    # ...but an API call gets a plain 401 JSON, not a redirect (review.js
    # needs this to detect it via fetch() and bounce itself -- see module
    # docstring / static/review.js's fetchJSON).
    resp = client.get("/api/photos")
    assert resp.status_code == 401 and resp.get_json() == {"error": "unauthorized"}
    # Phase 2e: /video/* (the new video-streaming route) must get the same
    # 401-not-redirect treatment as /image/* always has -- a <video> tag
    # can't follow a login redirect any more usefully than fetch() can.
    resp = client.get("/video/some-hash")
    assert resp.status_code == 401 and resp.get_json() == {"error": "unauthorized"}
    print("  unauthenticated page nav -> 302 to /login; unauthenticated /api/*, /video/* -> 401 JSON  OK")

    login_page = client.get("/login")
    csrf = _csrf_from_html(login_page.get_data(as_text=True))

    # Wrong password: rejected, no session established.
    resp = client.post("/login", data={"username": "dan", "password": "nope", "csrf_token": csrf})
    assert resp.status_code == 200 and "Invalid username or password" in resp.get_data(as_text=True)
    resp = client.get("/api/photos")
    assert resp.status_code == 401
    print("  wrong password rejected, no session created  OK")

    # Correct password: redirected, and the session now persists across
    # separate requests (this is what makes it "real session-based auth",
    # not just a one-off check) without needing to resend credentials.
    login_page = client.get("/login")
    csrf = _csrf_from_html(login_page.get_data(as_text=True))
    resp = client.post("/login", data={"username": "dan", "password": "correct-horse-battery", "csrf_token": csrf},
                        follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["Location"].rstrip("/") in ("", "/"), resp.headers.get("Location")
    resp = client.get("/")
    assert resp.status_code == 200 and "dan" in resp.get_data(as_text=True)
    resp = client.get("/api/photos")
    assert resp.status_code == 200
    print("  correct login -> redirected, session persists across later requests (/ and /api/photos both 200)  OK")

    # Logout actually ends the session.
    resp = client.get("/")
    logout_csrf_m = re.search(r'name="csrf_token" value="([^"]+)"', resp.get_data(as_text=True))
    assert logout_csrf_m, "expected a logout CSRF token on the authenticated index page"
    resp = client.post("/logout", data={"csrf_token": logout_csrf_m.group(1)}, follow_redirects=False)
    assert resp.status_code == 302
    resp = client.get("/api/photos")
    assert resp.status_code == 401
    print("  logout ends the session -- subsequent requests are unauthenticated again  OK")


def test_csrf_mismatch_rejected(tmp: Path) -> None:
    print("\n=== Phase 2d: a tampered/missing CSRF token is rejected on login ===")
    _rt, client, _limiter = _setup(tmp)
    client.get("/login")  # establishes the pre-login session + its real csrf token
    resp = client.post("/login", data={"username": "dan", "password": "correct-horse-battery",
                                        "csrf_token": "not-the-real-token"})
    assert resp.status_code == 200 and "expired" in resp.get_data(as_text=True).lower()
    resp = client.get("/api/photos")
    assert resp.status_code == 401, "a CSRF-mismatched POST must never establish a session, even with the right password"
    print("  wrong csrf_token rejected even with the correct password; no session created  OK")


def test_rate_limiting_blocks_after_max_attempts(tmp: Path) -> None:
    print("\n=== Phase 2d: login rate limiting (max_attempts=3 for this test) ===")
    _rt, client, limiter = _setup(tmp)  # cfg above sets login_rate_limit_attempts=3

    for i in range(3):
        login_page = client.get("/login")
        csrf = _csrf_from_html(login_page.get_data(as_text=True))
        resp = client.post("/login", data={"username": "dan", "password": "wrong", "csrf_token": csrf})
        assert "Invalid username or password" in resp.get_data(as_text=True), f"attempt {i+1} should just be 'invalid', not yet locked out"
    print("  3 failed attempts each rejected as 'invalid' (not locked out yet)  OK")

    # The 4th attempt -- even with the CORRECT password now -- must be
    # blocked by the lockout, not silently let through.
    login_page = client.get("/login")
    csrf = _csrf_from_html(login_page.get_data(as_text=True))
    resp = client.post("/login", data={"username": "dan", "password": "correct-horse-battery", "csrf_token": csrf})
    body = resp.get_data(as_text=True)
    assert "Too many failed attempts" in body, body
    resp2 = client.get("/api/photos")
    assert resp2.status_code == 401, "a lockout must not accidentally still establish a session"
    print("  4th attempt blocked by rate limit even with the correct password -- no session created  OK")

    # A DIFFERENT (ip, username) key must be unaffected -- rate limiting
    # is per-key, not a global lockout of the whole login page.
    assert limiter.seconds_until_allowed("user:someoneelse") == 0.0
    print("  a different username's rate-limit key is untouched by dan's lockout  OK")


def test_rate_limiter_unit(tmp: Path) -> None:
    print("\n=== Phase 2d: LoginRateLimiter sliding window, unit-level (injectable clock) ===")
    now = [1000.0]
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=60, clock=lambda: now[0])

    assert limiter.seconds_until_allowed("k") == 0.0
    for _ in range(3):
        limiter.record_failure("k")
    wait = limiter.seconds_until_allowed("k")
    assert wait > 0, "should be locked out after hitting max_attempts"
    print(f"  locked out after 3 failures, {wait:.0f}s remaining  OK")

    now[0] += 61  # advance the fake clock past the window
    assert limiter.seconds_until_allowed("k") == 0.0
    print("  unlocked again once the window has fully elapsed  OK")

    for _ in range(3):
        limiter.record_failure("k")
    assert limiter.seconds_until_allowed("k") > 0
    limiter.record_success("k")
    assert limiter.seconds_until_allowed("k") == 0.0, "a successful login should clear that key's failure history"
    print("  record_success clears the lockout immediately  OK")


def test_storage_abstraction(tmp: Path) -> None:
    print("\n=== Phase 2d: storage abstraction (src/storage.py) ===")
    from src.config import Config as _Cfg

    f = tmp / "photo.jpg"
    f.write_bytes(b"fake-jpeg-bytes")

    local = LocalDiskStorage()
    assert local.exists(str(f)) is True
    assert local.exists(str(tmp / "missing.jpg")) is False
    with local.open(str(f)) as fh:
        assert fh.read() == b"fake-jpeg-bytes"
    print("  LocalDiskStorage.exists/open work against a real file  OK")

    cfg_local = _Cfg(source_folders=[], dest_root="x", supported_extensions=[".jpg"], unsorted_subfolder="u",
                      dry_run=True, hash_algorithm="sha256", collision_suffix_length=8, db_path="x.db",
                      log_dir="logs", storage_backend="local")
    assert isinstance(get_storage(cfg_local), LocalDiskStorage)

    cfg_bad = _Cfg(source_folders=[], dest_root="x", supported_extensions=[".jpg"], unsorted_subfolder="u",
                    dry_run=True, hash_algorithm="sha256", collision_suffix_length=8, db_path="x.db",
                    log_dir="logs", storage_backend="s3")
    try:
        get_storage(cfg_bad)
        assert False, "an unimplemented backend must raise, not silently fall back to local"
    except ValueError as e:
        assert "s3" in str(e)
    print("  get_storage: 'local' resolves to LocalDiskStorage; an unknown backend raises clearly  OK")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="photo_organizer_phase2d_"))
    print(f"Working in {tmp}")
    try:
        test_verify_login_and_dummy_hash_path(tmp)
        test_open_redirect_guard()
        test_login_flow_and_session_persistence(tmp)
        test_csrf_mismatch_rejected(tmp)
        test_rate_limiting_blocks_after_max_attempts(tmp)
        test_rate_limiter_unit(tmp)
        test_storage_abstraction(tmp)
        print("\nALL PHASE 2D TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
