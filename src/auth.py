"""Phase 2d: session-based auth for review_tool.py — a small local list of
username+password pairs (config.yaml's review_users, managed via `main.py
review-user add/list/remove`), NOT a single shared password and NOT a full
account-management system. Every account currently gets full library
access (no per-person restriction implemented — see photo-organizer-
spec.md's Phase 2d), but nothing here assumes that so deeply it couldn't
grow a per-user scope check later (see the `user` value threaded through
session/require_login, ready for a future authorization check to key off).

Design, briefly (see the session's end-of-run summary for the full
reasoning written out):
  - Real session-based auth: a signed Flask session cookie set on
    successful login, checked on every request via a `before_request`
    hook — not HTTP Basic Auth. Sessions are `permanent` (configurable
    lifetime, default 30 days), so a login persists across browser
    restarts, not just until the tab closes.
  - Passwords are hashed with werkzeug's `generate_password_hash`
    (scrypt) — never stored or compared in plaintext. A nonexistent
    username still runs a dummy hash check so it takes the same time as a
    real-but-wrong password (avoids a timing side-channel that could be
    used to enumerate valid usernames).
  - Rate limiting (LoginRateLimiter): failed attempts are tracked in a
    sliding window, keyed by BOTH the client IP and the attempted
    username, whichever is stricter — this defends against both a single
    IP brute-forcing many passwords, and a distributed brute force
    (many IPs) targeting one known username.
  - The app is only ever meant to be reached either on localhost directly
    (the user, at their own PC) or through the Cloudflare Tunnel (which
    terminates real HTTPS at Cloudflare's edge — see spec) — cloudflared
    itself connects to this app over loopback only, so `CF-Connecting-IP`
    (which cloudflared sets on every forwarded request) is a trustworthy
    real-client-IP source here; see get_client_ip.
  - A `next` redirect target (where to send the user after login) is only
    ever honored if it's a same-site relative path — an open-redirect
    guard (see _is_safe_next), since `next` can come from a URL an
    attacker crafted and sent to a victim.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

from flask import jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

SECRET_KEY_FILENAME = ".review_session_secret"

# Precomputed once at import time so verify_login can run a hash check even
# when the username doesn't exist at all — see module docstring's timing
# note. The actual value is meaningless (never a real credential).
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))

# Endpoints reachable without a session — Flask's built-in static file
# server, plus the login page itself (has to be, or nobody could ever log
# in). Everything else goes through _enforce_auth below.
_PUBLIC_ENDPOINTS = {"login", "static"}


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def find_user(cfg, username: str) -> dict | None:
    for u in cfg.review_users:
        if u.get("username") == username:
            return u
    return None


def verify_login(cfg, username: str, password: str) -> bool:
    user = find_user(cfg, username)
    if user is None:
        check_password_hash(_DUMMY_HASH, password)  # constant-time-ish decoy, see module docstring
        return False
    return check_password_hash(user.get("password_hash", ""), password)


def ensure_secret_key(data_dir: Path) -> bytes:
    """The Flask session cookie is a signed (not encrypted) value — anyone
    who can read this key could forge a valid session cookie for any
    username, so it's generated once and persisted (not re-generated on
    every server start, which would silently log everyone out on every
    restart) under `data/`, which is already gitignored (see .gitignore's
    Project data section) and never displayed/logged anywhere."""
    data_dir.mkdir(parents=True, exist_ok=True)
    key_path = data_dir / SECRET_KEY_FILENAME
    if key_path.exists():
        return key_path.read_bytes()
    key = secrets.token_bytes(32)
    key_path.write_bytes(key)
    try:
        os.chmod(key_path, 0o600)  # best-effort; NTFS ACLs aren't chmod-controlled anyway on Windows
    except OSError:
        pass
    return key


def get_client_ip(req) -> str:
    """Best-effort real client IP for rate limiting. cloudflared sets
    CF-Connecting-IP on every request it forwards, and — per this tool's
    deployment (review_tool.py binds 127.0.0.1 only; the tunnel is the
    only path in from outside this machine, see spec) — that header can
    only have been set by cloudflared itself, so it's trustworthy here.
    Falls back to X-Forwarded-For's first hop, then the raw socket
    address, for direct localhost/LAN access when there's no tunnel in
    the picture (e.g. local testing)."""
    cf_ip = req.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    xff = req.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return req.remote_addr or "unknown"


def _is_safe_next(path: str | None) -> bool:
    """Open-redirect guard for the post-login `next` target: must be a
    same-site relative path, not `//host/...` (a protocol-relative URL
    browsers still treat as external) and not a backslash variant some
    browsers also normalize into a host separator."""
    return bool(path) and path.startswith("/") and not path.startswith("//") and "\\" not in path


class LoginRateLimiter:
    """Sliding-window failed-login tracker, keyed by an arbitrary string
    (caller decides — register_auth below uses both the client IP and the
    attempted username, taking whichever is stricter). Not persisted
    across a server restart — acceptable for a locally-hosted single-
    process tool; a restart is a rare, deliberate admin action, not
    something an attacker can trigger to reset their own lockout.

    `clock` is injectable (defaults to time.monotonic) purely so tests can
    drive it deterministically without real sleeping."""

    def __init__(self, max_attempts: int = 5, window_seconds: float = 900.0, clock=time.monotonic):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._clock = clock
        self._failures: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> None:
        dq = self._failures[key]
        while dq and now - dq[0] > self.window_seconds:
            dq.popleft()

    def seconds_until_allowed(self, key: str) -> float:
        """0.0 if an attempt against `key` is allowed right now, else how
        many seconds until the oldest failure in the window ages out."""
        now = self._clock()
        with self._lock:
            self._prune(key, now)
            dq = self._failures[key]
            if len(dq) < self.max_attempts:
                return 0.0
            return max(0.0, self.window_seconds - (now - dq[0]))

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._failures[key].append(self._clock())

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def register_auth(app, cfg, logger: logging.Logger | None = None,
                   rate_limiter: LoginRateLimiter | None = None) -> LoginRateLimiter:
    """Wires up session config, the login/logout routes, and the
    before_request auth gate on `app`. Returns the LoginRateLimiter in use
    (built from cfg unless one is passed in, e.g. by tests)."""
    log = logger or logging.getLogger("photo_organizer")

    app.secret_key = ensure_secret_key(cfg.db_path_abs.parent)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.session_cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(days=cfg.session_lifetime_days),
    )

    limiter = rate_limiter or LoginRateLimiter(
        max_attempts=cfg.login_rate_limit_attempts,
        window_seconds=cfg.login_rate_limit_window_seconds,
    )

    @app.before_request
    def _enforce_auth():
        if request.endpoint in _PUBLIC_ENDPOINTS or request.endpoint == "logout":
            return None
        if request.endpoint is None:
            return None  # let unmatched routes 404 normally rather than masking as an auth redirect
        if session.get("user"):
            return None
        if request.path.startswith("/api/") or request.path.startswith("/image/") \
                or request.path.startswith("/video/") or request.path.startswith("/thumbnail/"):
            # These are fetch()/<img>/<video> calls from review.js, not full
            # page navigations — a redirect response wouldn't be followed
            # the way a browser follows one for a normal link, and would
            # just look like a broken/empty result (a <video> element in
            # particular would just fail to load with no obvious cause).
            # 401 lets the front end notice and bounce to /login itself
            # (see fetchJSON in review.js) — the same reasoning that already
            # applied to /image/* now covers /video/* (Phase 2e) and
            # /thumbnail/* (Phase 2g) too.
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("login", next=request.path))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("user"):
            dest = request.args.get("next")
            return redirect(dest if _is_safe_next(dest) else url_for("index"))

        error = None
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            next_target = request.form.get("next") or request.args.get("next")
            ip = get_client_ip(request)
            user_key = f"user:{username.lower()}" if username else None

            if request.form.get("csrf_token") != session.get("_csrf"):
                error = "Your session expired — please try again."
            else:
                wait = limiter.seconds_until_allowed(ip)
                if user_key:
                    wait = max(wait, limiter.seconds_until_allowed(user_key))
                if wait > 0:
                    log.warning(f"LOGIN blocked (rate limit) ip={ip} user={username!r}")
                    minutes = int(wait // 60) + 1
                    error = f"Too many failed attempts. Try again in about {minutes} minute(s)."
                elif verify_login(cfg, username, password):
                    limiter.record_success(ip)
                    if user_key:
                        limiter.record_success(user_key)
                    log.info(f"LOGIN success user={username!r} ip={ip}")
                    session.clear()
                    session.permanent = True
                    session["user"] = username
                    # Fresh CSRF token for the now-authenticated session
                    # (the pre-login one was just cleared above) — used by
                    # the logout form (see register_auth's /logout route
                    # and templates/review.html).
                    session["_csrf"] = secrets.token_urlsafe(24)
                    return redirect(next_target if _is_safe_next(next_target) else url_for("index"))
                else:
                    limiter.record_failure(ip)
                    if user_key:
                        limiter.record_failure(user_key)
                    log.warning(f"LOGIN failed user={username!r} ip={ip}")
                    error = "Invalid username or password."

        # A fresh CSRF token per (unauthenticated) session, reused across
        # a GET-then-POST round trip on the same browser session.
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(24)
        next_param = request.args.get("next") or request.form.get("next")
        return render_template(
            "login.html",
            error=error,
            next=next_param if _is_safe_next(next_param) else "",
            csrf_token=session["_csrf"],
        )

    @app.route("/logout", methods=["POST"])
    def logout():
        user = session.get("user")
        if user and request.form.get("csrf_token") != session.get("_csrf"):
            # Wrong/missing token — most likely a stale page (session
            # rotated since it was rendered); just leave the session as-is
            # rather than logging out on an unverified POST (CSRF guard).
            return redirect(url_for("index"))
        session.clear()
        if user:
            log.info(f"LOGOUT user={user!r}")
        return redirect(url_for("login"))

    return limiter
