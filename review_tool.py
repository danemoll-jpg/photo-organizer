#!/usr/bin/env python
"""Phase 2b: standalone browser-based review/spot-check tool for the
in-progress Phase 2 captioning run. Deliberately separate from
dashboard.py (see TODO.md / CLAUDE.md) — this is its own local web app,
not a dashboard tab.

    venv\\Scripts\\python review_tool.py
    venv\\Scripts\\python review_tool.py --port 5151 --no-browser

Opens http://127.0.0.1:<port> (localhost only by default; pass --host
0.0.0.0 to reach it from another device on the LAN) and launches the
browser automatically unless --no-browser is passed.

Read-only, deliberately:
- The SQLite DB is opened via SQLite's own `mode=ro` URI, so this process
  cannot write to it even by accident — any attempted write raises rather
  than silently succeeding.
- captions.jsonl is only ever opened for reading, tailed incrementally
  (see CaptionsCache below) as the in-progress Phase 2 run appends to it.
- This tool never runs organize/caption/extract-gps itself, and never
  edits captions/tags — it only displays whatever those other steps have
  already produced, including "nothing yet", which must show clearly as
  such (e.g. "Not yet captioned"), never as an error or a blank gap.

Video files are deliberately excluded from every query here (see
_video_exclusion_sql): Phase 2 never captions video by design (see
caption.py), so a video row would otherwise show as permanently "not yet
captioned" forever, which is misleading rather than informative.

People/faces: Phase 3 doesn't exist yet. Every photo response carries a
`people` field that is always `None` right now — the template renders a
reserved section for it regardless, so this tool won't need a UI rebuild
once Phase 3 lands, only that field needing to be populated for real.

Phase 2c added: a Random button + random slideshow order in the viewer
(/api/random, /api/nav?mode=random — see their docstrings), and new
filters (tag, caption keyword, GPS/location, plus a visible-but-inert
people/faces control that's UI-only, never sent to the server). Every
filter — old and new — applies identically to the grid (/api/photos) and
to viewer/slideshow navigation (/api/nav), per spec. See
_build_filters/_build_extra_predicate for how the two different filter
"sources" (plain DB columns vs. the live captions cache) are combined.

Phase 2d added remote/shared access: real session-based login (see
src/auth.py — a small local username+password list in config.yaml's
review_users, rate-limited, NOT HTTP Basic Auth), meant to be reached
either directly on localhost or through a separate Cloudflare Tunnel (see
README.md/photo-organizer-spec.md — independent of Plex's own remote
access, never reusing it). Every route except /login, /logout, and
/static/* now requires a session (src/auth.py::register_auth's
before_request hook) — a browser navigation redirects to /login, an
unauthenticated /api/* or /image/* call gets a plain 401 (review.js
bounces itself to /login on that, since a redirect response doesn't help
a fetch() call). Phase 2d also introduced a storage-abstraction seam
(src/storage.py::PhotoStorage) between this file and "where photo bytes
actually live" — local disk today (LocalDiskStorage), groundwork only for
a possible future cloud-storage backend, not an actual migration.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import sqlite3
import threading
import webbrowser
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, session
from PIL import Image

from src.auth import register_auth
from src.config import Config, load_config
from src.logging_setup import setup_logging
from src.storage import PhotoStorage, get_storage

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None

REPO_ROOT = Path(__file__).resolve().parent

app = Flask(__name__)

# Set by main() at startup from config.yaml — read-only globals for the
# lifetime of this process (a single-user local tool, not multi-tenant).
_cfg: Config
_video_exts: set[str] = set()
_storage: PhotoStorage


class CaptionsCache:
    """In-memory file_hash -> caption record, built by incrementally
    tailing captions.jsonl (append-only — see caption.py). refresh() reads
    only the bytes written since the last call, so calling it on every
    request that needs fresh data (as every route below does) stays cheap
    even once the real file has 100k+ lines: a no-op read when nothing's
    changed, a small incremental parse when Phase 2 has appended since.

    A trailing partial line (Phase 2 caught mid-write, or a crash) is
    never parsed — the file position only advances past the last complete
    newline seen, so a half-written line is picked up whole on the next
    refresh instead of failing to parse. Same posture as caption.py's own
    resume-state reader.
    """

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._by_hash: dict[str, dict] = {}
        self._pos = 0

    def refresh(self) -> None:
        with self._lock:
            if not self._path.exists():
                return
            with open(self._path, "r", encoding="utf-8") as f:
                f.seek(self._pos)
                chunk = f.read()
            if not chunk:
                return
            last_newline = chunk.rfind("\n")
            if last_newline == -1:
                return  # only an incomplete trailing line available so far -- wait for more
            complete, consumed = chunk[:last_newline], last_newline + 1
            self._pos += consumed
            for line in complete.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # matches caption.py's own tolerance for a corrupt trailing line
                file_hash = obj.get("file_hash")
                if file_hash:
                    self._by_hash[file_hash] = obj

    def get(self, file_hash: str) -> dict | None:
        return self._by_hash.get(file_hash)

    def count(self) -> int:
        return len(self._by_hash)


_captions_cache: CaptionsCache


def get_db() -> sqlite3.Connection:
    """A fresh read-only connection per call. sqlite3 connections aren't
    safe to share across threads, and Flask's dev server is threaded --
    opening via the `mode=ro` URI (rather than just "don't call .commit()")
    means an accidental write anywhere in this file raises immediately
    instead of ever reaching the live DB, on top of every route already
    only ever using SELECT."""
    uri = f"file:{_cfg.db_path_abs.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _video_exclusion_sql() -> str:
    if not _video_exts:
        return ""
    return " AND (" + " AND ".join("filename NOT LIKE ?" for _ in _video_exts) + ")"


def _video_exclusion_params() -> list[str]:
    return [f"%{ext}" for ext in sorted(_video_exts)]


def _build_filters(args) -> tuple[str, list]:
    """Shared WHERE-clause builder for every query below: date range
    (against date_taken -- ISO8601 text sorts correctly lexicographically)
    + folder substring (against current_path) + GPS/location (Phase 2c:
    place-name substring against `location_name`, and/or a has/no-location
    toggle against the same column) + the video exclusion above. All of
    these are plain DB columns, so they stay on the cheap indexed-or-LIKE
    SQL path. Tag and caption-keyword filtering (also Phase 2c) are
    deliberately NOT here -- see _build_extra_predicate for why those two
    need a different mechanism. Returns (sql_fragment_after_WHERE, params).
    """
    clauses = ["1=1"]
    params: list = []
    date_from = (args.get("date_from") or "").strip()
    date_to = (args.get("date_to") or "").strip()
    folder = (args.get("folder") or "").strip()
    location = (args.get("location") or "").strip()
    has_location = (args.get("has_location") or "").strip().lower()
    if date_from:
        clauses.append("date_taken >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("date_taken < ?")
        params.append(date_to + "T23:59:59.999999")  # inclusive of the whole end day
    if folder:
        clauses.append("current_path LIKE ?")
        params.append(f"%{folder}%")
    if location:
        clauses.append("location_name LIKE ?")
        params.append(f"%{location}%")
    if has_location == "yes":
        clauses.append("location_name IS NOT NULL")
    elif has_location == "no":
        # Deliberately simple binary per spec ("has/no location"): this
        # also matches rows never GPS-checked at all (gps_checked=0), not
        # just ones checked-and-found-nothing -- both cases really do have
        # "no location" to show right now, and extract-gps is a separate,
        # user-run step (see CLAUDE.md), so most of the library may not be
        # checked yet at any given time.
        clauses.append("location_name IS NULL")
    sql = " AND ".join(clauses) + _video_exclusion_sql()
    params.extend(_video_exclusion_params())
    return sql, params


def _build_extra_predicate(args):
    """Phase 2c tag / caption-keyword filters can't be pushed into the SQL
    WHERE clause the way date/folder/GPS filters above are: their source
    of truth is the live captions.jsonl tail (_captions_cache), not the
    `caption`/`tags`/`photo_tags` DB columns -- those only reflect
    whatever `main.py load-captions` last loaded, which visibly lags
    behind an in-progress Phase 2 run (see CLAUDE.md/TODO.md's Data Layer
    notes). Using the live cache instead means a photo captioned moments
    ago is immediately filterable here, matching this tool's whole
    "always live, no reruns needed" design (see module docstring).

    The cost: matching can't use an index, so every route that gets a
    non-None predicate back from here has to fall back to scanning
    (batched, not all-at-once -- see _scan_matches/_count_matches/
    _all_matching_paths) instead of a single indexed SQL query. Returns a
    callable(file_hash) -> bool, or None when neither filter is active
    (the common case -- callers skip the scanning path entirely then and
    stay on the cheap indexed/LIKE SQL path)."""
    tag = (args.get("tag") or "").strip().lower()
    caption_kw = (args.get("caption_kw") or "").strip().lower()
    if not tag and not caption_kw:
        return None

    def predicate(file_hash: str) -> bool:
        cap = _captions_cache.get(file_hash)
        if cap is None:
            return False
        if tag and tag not in [str(t).lower() for t in (cap.get("tags") or [])]:
            return False
        if caption_kw and caption_kw not in (cap.get("caption") or "").lower():
            return False
        return True

    return predicate


def _scan_matches(conn, where, params, extra_pred, cursor, want, forward=True):
    """Only used when extra_pred (tag/caption-keyword) is active --
    date/folder/GPS-only filters never call this, they stay on the plain
    indexed SQL path. Walks `photos` in current_path order, batching the
    underlying SQL fetch (growing the batch size when a batch turns up
    few/no matches, e.g. a rare tag) rather than either pulling the whole
    filtered table into memory up front or doing one SQL round-trip per
    candidate row. Worst case (a match near the very end of a 100k+-row
    table, or no matches at all) still means scanning the whole thing --
    there's no way around that when the predicate lives outside SQL (see
    _build_extra_predicate) -- but it's bounded by rows actually read in
    growing batches, never by materializing them all at once.

    Returns (matches, exhausted): `exhausted` is True once the underlying
    table ran out of candidate rows in this direction, whether or not
    `want` matches were found by then -- callers use it the same way the
    plain-SQL path infers has_next/has_prev today."""
    results: list[sqlite3.Row] = []
    last_seen = cursor
    batch_size = max(want * 4, 200)
    exhausted = False
    while len(results) < want:
        sql = f"SELECT * FROM photos WHERE {where}"
        p = list(params)
        if last_seen is not None:
            sql += " AND current_path > ?" if forward else " AND current_path < ?"
            p.append(last_seen)
        sql += " ORDER BY current_path ASC LIMIT ?" if forward else " ORDER BY current_path DESC LIMIT ?"
        p.append(batch_size)
        rows = conn.execute(sql, p).fetchall()
        if not rows:
            exhausted = True
            break
        for r in rows:
            last_seen = r["current_path"]
            if extra_pred(r["file_hash"]):
                results.append(r)
                if len(results) >= want:
                    break
        if len(rows) < batch_size:
            exhausted = True
            break
        batch_size = min(batch_size * 2, 20000)
    return results, exhausted


def _count_matches(conn, where, params, extra_pred) -> int:
    """Filtered count, extra_pred-aware. The no-extra_pred case (plain
    date/folder/GPS filters) is a single indexed COUNT(*); with a tag/
    caption filter active this scans in batches instead (see
    _scan_matches's docstring for why there's no avoiding a scan there)."""
    if extra_pred is None:
        return conn.execute(f"SELECT COUNT(*) FROM photos WHERE {where}", params).fetchone()[0]
    count = 0
    last_seen = None
    batch_size = 5000
    while True:
        sql = f"SELECT file_hash, current_path FROM photos WHERE {where}"
        p = list(params)
        if last_seen is not None:
            sql += " AND current_path > ?"
            p.append(last_seen)
        sql += " ORDER BY current_path ASC LIMIT ?"
        p.append(batch_size)
        rows = conn.execute(sql, p).fetchall()
        if not rows:
            break
        for r in rows:
            last_seen = r["current_path"]
            if extra_pred(r["file_hash"]):
                count += 1
        if len(rows) < batch_size:
            break
    return count


def _all_matching_paths(conn, where, params, extra_pred) -> list[str]:
    """Full list of current_path values matching where+extra_pred, in
    current_path order. Only called when a tag/caption filter is active,
    for the two spots that fundamentally need the whole matching set at
    once: the viewer's one-off Random button (_pick_random_row) and
    building a random-order slideshow's shuffled sequence
    (_random_order_for) -- both already require a full pass in that case
    (same reasoning as _count_matches above), so this is that one pass,
    reused/cached by callers rather than repeated."""
    paths: list[str] = []
    last_seen = None
    batch_size = 5000
    while True:
        sql = f"SELECT file_hash, current_path FROM photos WHERE {where}"
        p = list(params)
        if last_seen is not None:
            sql += " AND current_path > ?"
            p.append(last_seen)
        sql += " ORDER BY current_path ASC LIMIT ?"
        p.append(batch_size)
        rows = conn.execute(sql, p).fetchall()
        if not rows:
            break
        for r in rows:
            last_seen = r["current_path"]
            if extra_pred(r["file_hash"]):
                paths.append(r["current_path"])
        if len(rows) < batch_size:
            break
    return paths


def _pick_random_row(conn, where, params, extra_pred):
    """Backs the viewer's one-off "Random" button (/api/random). The
    no-extra_pred path -- plain date/folder/GPS filters, the common case
    -- is what Phase 2c's "work efficiently at 100k+ scale, avoid loading
    the whole filtered set into memory" ask is really about: one COUNT
    and one indexed OFFSET fetch, never materializing more than the single
    returned row. The extra_pred (tag/caption) path can't avoid a full
    scan -- accepted tradeoff, see _build_extra_predicate."""
    if extra_pred is None:
        total = conn.execute(f"SELECT COUNT(*) FROM photos WHERE {where}", params).fetchone()[0]
        if total == 0:
            return None
        offset = random.randrange(total)
        sql = f"SELECT * FROM photos WHERE {where} ORDER BY current_path LIMIT 1 OFFSET ?"
        return conn.execute(sql, params + [offset]).fetchone()
    paths = _all_matching_paths(conn, where, params, extra_pred)
    if not paths:
        return None
    return conn.execute("SELECT * FROM photos WHERE current_path = ?", (random.choice(paths),)).fetchone()


def _feistel_permute(index: int, total: int, seed: str, rounds: int = 4) -> int:
    """Deterministic pseudo-random bijection from [0, total) to itself,
    keyed by `seed`. This is what makes random-order slideshow navigation
    (/api/nav?mode=random) cheap at 100k+ scale without ever shuffling or
    storing the filtered set: asking "what's the item at position i of
    THIS seed's random order" is an O(1)-memory computation (a few rounds
    of hashing, cycle-walked back into [0, total) when a round lands
    outside it) with the same no-repeats-until-the-whole-set-is-consumed
    guarantee a real Fisher-Yates shuffle gives -- for both next (i+1) and
    prev (i-1) stepping, since it's just a pure function of the index, no
    server-side history needed. A fresh `seed` (review.js mints a new
    random one whenever the viewer's order is switched to Random, or a
    slideshow (re)starts in that mode) gives a completely different
    permutation, matching the spec's "fresh shuffle every time it starts,
    not a repeated shuffle". Only used for the no-extra_pred case; see
    _random_order_for for the tag/caption-filtered equivalent."""
    if total <= 1:
        return 0
    bits = max(1, (total - 1).bit_length())
    half_bits = max(1, bits // 2)
    mask = (1 << half_bits) - 1
    full_mask = (1 << bits) - 1

    def round_fn(right: int, r: int) -> int:
        h = hashlib.sha256(f"{seed}:{r}:{right}".encode("utf-8", "replace")).digest()
        return int.from_bytes(h[:4], "big") & mask

    def feistel(x: int) -> int:
        left, right = x >> half_bits, x & mask
        for r in range(rounds):
            left, right = right, left ^ round_fn(right, r)
        return ((left << half_bits) | right) & full_mask

    y = feistel(index & full_mask)
    while y >= total:  # cycle-walking: re-permute until the result lands back in range
        y = feistel(y)
    return y


# Small bounded cache, keyed by the client-minted slideshow seed, of the
# shuffled current_path order for a tag/caption-filtered random-order
# slideshow (see /api/nav's mode=random). Only needed when extra_pred is
# active -- the plain-filter case never touches this, it uses
# _feistel_permute's O(1)-memory approach instead (see its docstring).
# Bounded/FIFO so a client minting many seeds (e.g. repeatedly toggling
# order modes) can't grow this without limit; a single-user local tool
# never needs more than a couple of these alive at once anyway.
_RANDOM_ORDER_CACHE_MAX = 8
_random_order_cache: dict[str, list[str]] = {}
_random_order_cache_lock = threading.Lock()


def _random_order_for(conn, where, params, extra_pred, seed: str) -> list[str]:
    with _random_order_cache_lock:
        cached = _random_order_cache.get(seed)
    if cached is not None:
        return cached
    paths = _all_matching_paths(conn, where, params, extra_pred)
    random.Random(seed).shuffle(paths)
    with _random_order_cache_lock:
        _random_order_cache[seed] = paths
        while len(_random_order_cache) > _RANDOM_ORDER_CACHE_MAX:
            _random_order_cache.pop(next(iter(_random_order_cache)))
    return paths


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    file_hash = d["file_hash"]
    cap = _captions_cache.get(file_hash)
    current_path = d["current_path"] or ""
    dest_root = str(_cfg.dest_root_path)
    relative_path = current_path
    if current_path.lower().startswith(dest_root.lower()):
        relative_path = current_path[len(dest_root):].lstrip("\\/")
    is_video = Path(d["filename"] or "").suffix.lower() in _video_exts
    return {
        "file_hash": file_hash,
        "current_path": current_path,
        "relative_path": relative_path,
        "filename": d["filename"],
        "date_taken": d["date_taken"],
        "date_source": d["date_source"],
        "status": d["status"],
        "is_video": is_video,
        "captioned": cap is not None,
        "caption": cap.get("caption") if cap else None,
        "tags": cap.get("tags") if cap else [],
        "location_name": d.get("location_name"),
        "gps_checked": bool(d.get("gps_checked")),
        # Phase 3 doesn't exist yet -- always None. Kept on every response
        # now (rather than added later) so the front-end's reserved
        # people/faces section never needs new wiring once it does; see
        # module docstring and templates/review.html.
        "people": None,
    }


def _fetch_page(conn: sqlite3.Connection, where: str, params: list, limit: int,
                 after: str | None, before: str | None, extra_pred=None) -> tuple[list[dict], bool, bool]:
    """Keyset (cursor) pagination on current_path — deliberately not
    OFFSET-based: OFFSET n costs an O(n) scan on the way to page n, which
    gets slower every page deeper into a 100k+-row table. A cursor lookup
    stays O(log n) via idx_photos_current_path regardless of how deep the
    user has paged. `after`/`before` are the current_path of the boundary
    item from the page the caller already has (exclusive).

    When a Phase 2c tag/caption filter is active (extra_pred not None),
    the same cursor semantics are preserved but satisfied via _scan_matches
    instead of a single indexed query — see its docstring."""
    if extra_pred is not None:
        if before is not None:
            matches, _ = _scan_matches(conn, where, params, extra_pred, cursor=before, want=limit + 1, forward=False)
            has_prev = len(matches) > limit
            rows = list(reversed(matches[:limit]))
            has_next = True
        else:
            matches, _ = _scan_matches(conn, where, params, extra_pred, cursor=after, want=limit + 1, forward=True)
            has_next = len(matches) > limit
            rows = matches[:limit]
            has_prev = after is not None
        return [_row_to_dict(r) for r in rows], has_next, has_prev
    if before is not None:
        sql = f"SELECT * FROM photos WHERE {where} AND current_path < ? ORDER BY current_path DESC LIMIT ?"
        rows = conn.execute(sql, params + [before, limit + 1]).fetchall()
        has_prev = len(rows) > limit
        rows = list(reversed(rows[:limit]))
        has_next = True  # we came from a page that exists, so there's always something after this one
    else:
        sql = f"SELECT * FROM photos WHERE {where}"
        p = list(params)
        if after is not None:
            sql += " AND current_path > ?"
            p.append(after)
        sql += " ORDER BY current_path ASC LIMIT ?"
        rows = conn.execute(sql, p + [limit + 1]).fetchall()
        has_next = len(rows) > limit
        rows = rows[:limit]
        has_prev = after is not None
    return [_row_to_dict(r) for r in rows], has_next, has_prev


@app.route("/")
def index():
    return render_template(
        "review.html",
        page_size=_cfg.review_page_size,
        slideshow_seconds=_cfg.review_slideshow_seconds,
        dest_root=str(_cfg.dest_root_path),
        current_user=session.get("user"),
        logout_csrf=session.get("_csrf"),
    )


@app.route("/api/stats")
def api_stats():
    _captions_cache.refresh()
    conn = get_db()
    try:
        where, params = _build_filters(request.args)
        extra_pred = _build_extra_predicate(request.args)
        total = _count_matches(conn, where, params, extra_pred)
    finally:
        conn.close()
    return jsonify({"total_photos": total, "captioned_so_far": _captions_cache.count()})


@app.route("/api/photos")
def api_photos():
    """Grid/browse view: one page of photos matching the current filters,
    newest-path-first is NOT assumed — ordered by current_path ascending
    (i.e. roughly by year/month/filename, matching the on-disk layout the
    user already navigates in Explorer)."""
    _captions_cache.refresh()
    conn = get_db()
    try:
        where, params = _build_filters(request.args)
        extra_pred = _build_extra_predicate(request.args)
        limit = min(max(int(request.args.get("limit", _cfg.review_page_size)), 1), 200)
        after = request.args.get("after")
        before = request.args.get("before")
        items, has_next, has_prev = _fetch_page(conn, where, params, limit, after, before, extra_pred)
        count = None
        if request.args.get("with_count"):
            count = _count_matches(conn, where, params, extra_pred)
    finally:
        conn.close()
    return jsonify({
        "items": items,
        "has_next": has_next,
        "has_prev": has_prev,
        "next_cursor": items[-1]["current_path"] if items and has_next else None,
        "prev_cursor": items[0]["current_path"] if items and has_prev else None,
        "total_matching": count,
    })


@app.route("/api/nav")
def api_nav():
    """Viewer/slideshow single-step navigation, filter-aware (Phase 2c:
    every filter that applies to the grid — date/folder/GPS via
    _build_filters, tag/caption-keyword via _build_extra_predicate — is
    applied here too, so a filtered slideshow only ever shows matching
    photos). Two navigation modes:

    - Sequential (default, `dir=next|prev`): cursor is the current_path
      of the photo currently on screen (absent = start from the very
      first match). Intentionally NOT page-based, so stepping past a
      grid page's last photo — or just running a chronological slideshow
      continuously — never dead-ends at a page boundary: each step is
      one lookup for "the next/previous matching row after this path".
    - Random (`mode=random&seed=<s>&idx=<i>`), Phase 2c: returns the
      item at position `idx` of a pseudo-random permutation of the
      filtered set keyed by `seed`. Because it's a pure function of
      (seed, idx, filters), stepping idx+1/idx-1 gives next/prev with no
      repeats until the whole filtered set is consumed — the same
      guarantee a real shuffle gives — without the server ever storing a
      per-client "current shuffle" session. review.js mints a fresh
      `seed` whenever slideshow order is (re)switched to Random, per
      spec ("fresh shuffle every time it starts, not a repeated
      shuffle"). See _feistel_permute / _random_order_for for the two
      underlying mechanisms (no-extra_pred vs tag/caption-filtered)."""
    _captions_cache.refresh()
    conn = get_db()
    try:
        where, params = _build_filters(request.args)
        extra_pred = _build_extra_predicate(request.args)

        if request.args.get("mode") == "random":
            seed = request.args.get("seed", "0")
            try:
                idx = int(request.args.get("idx", "0"))
            except ValueError:
                idx = 0
            if idx < 0:
                row = None
            elif extra_pred is not None:
                order = _random_order_for(conn, where, params, extra_pred, seed)
                row = None if idx >= len(order) else \
                    conn.execute("SELECT * FROM photos WHERE current_path = ?", (order[idx],)).fetchone()
            else:
                total = conn.execute(f"SELECT COUNT(*) FROM photos WHERE {where}", params).fetchone()[0]
                if idx >= total:
                    row = None
                else:
                    offset = _feistel_permute(idx, total, seed)
                    sql = f"SELECT * FROM photos WHERE {where} ORDER BY current_path LIMIT 1 OFFSET ?"
                    row = conn.execute(sql, params + [offset]).fetchone()
            return jsonify({"item": _row_to_dict(row) if row is not None else None})

        cursor = request.args.get("cursor") or None
        direction = request.args.get("dir", "next")
        if extra_pred is not None:
            matches, _ = _scan_matches(conn, where, params, extra_pred, cursor=cursor,
                                        want=1, forward=(direction != "prev"))
            row = matches[0] if matches else None
        elif direction == "prev":
            if cursor is None:
                row = None
            else:
                sql = f"SELECT * FROM photos WHERE {where} AND current_path < ? ORDER BY current_path DESC LIMIT 1"
                row = conn.execute(sql, params + [cursor]).fetchone()
        else:
            sql = f"SELECT * FROM photos WHERE {where}"
            p = list(params)
            if cursor is not None:
                sql += " AND current_path > ?"
                p.append(cursor)
            sql += " ORDER BY current_path ASC LIMIT 1"
            row = conn.execute(sql, p).fetchone()
    finally:
        conn.close()
    return jsonify({"item": _row_to_dict(row) if row is not None else None})


@app.route("/api/random")
def api_random():
    """Phase 2c: the viewer's one-off "Random" button — jump to a random
    photo within the current filters. Independent of any slideshow order
    mode (that's /api/nav?mode=random, a repeat-free sequence) — this is
    just "surprise me once", not part of any next/prev chain."""
    _captions_cache.refresh()
    conn = get_db()
    try:
        where, params = _build_filters(request.args)
        extra_pred = _build_extra_predicate(request.args)
        row = _pick_random_row(conn, where, params, extra_pred)
    finally:
        conn.close()
    return jsonify({"item": _row_to_dict(row) if row is not None else None})


# Small bounded in-memory cache of already-downscaled JPEG bytes, keyed by
# (file_hash, max_dim). Photos are immutable once organized (same content
# hash forever), so there's no staleness risk -- this just avoids re-
# decoding/re-resizing the same full-res file on every repeat view (e.g.
# stepping back over photos already seen, or a slideshow looping).
# Unbounded growth is avoided with a simple FIFO eviction, good enough for
# a single-user local tool that isn't trying to cache the whole library.
_IMAGE_CACHE_MAX = 128
_image_cache: dict[tuple[str, int], bytes] = {}
_image_cache_order: list[tuple[str, int]] = []
_image_cache_lock = threading.Lock()


def _get_image_bytes(path: str, file_hash: str, max_dim: int) -> bytes:
    key = (file_hash, max_dim)
    with _image_cache_lock:
        cached = _image_cache.get(key)
    if cached is not None:
        return cached
    # Routed through the storage abstraction (Phase 2d groundwork — see
    # src/storage.py) rather than opening `path` directly, so a future
    # non-local backend only needs a new PhotoStorage implementation, not
    # a change here.
    with _storage.open(path) as f, Image.open(f) as img:
        img = img.convert("RGB")
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        data = buf.getvalue()
    with _image_cache_lock:
        if key not in _image_cache:
            _image_cache[key] = data
            _image_cache_order.append(key)
            if len(_image_cache_order) > _IMAGE_CACHE_MAX:
                oldest = _image_cache_order.pop(0)
                _image_cache.pop(oldest, None)
    return data


@app.route("/image/<file_hash>")
def serve_image(file_hash: str):
    """Streams a downscaled JPEG of the photo for display in the browser
    -- full-res 12MP+ phone photos are far bigger than any browser needs
    to paint at review-tool size. HEIC decodes the same as everywhere else
    in this project (pillow_heif registered above). Looked up by
    file_hash, not a raw path, so a stale/bookmarked URL can't be used to
    read an arbitrary filesystem path."""
    conn = get_db()
    try:
        row = conn.execute("SELECT current_path FROM photos WHERE file_hash = ?", (file_hash,)).fetchone()
    finally:
        conn.close()
    if row is None:
        abort(404)
    path = row[0]
    if not _storage.exists(path):
        abort(404)
    max_dim = min(max(int(request.args.get("max", 1600)), 100), 4000)
    try:
        data = _get_image_bytes(path, file_hash, max_dim)
    except Exception:
        abort(415)
    resp = Response(data, mimetype="image/jpeg")
    # Safe to cache aggressively: file_hash is content-addressed, and this
    # tool never edits photos, so the same (hash, max) pair's bytes never
    # change for the life of this DB.
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


def _open_browser_later(url: str) -> None:
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()


def main() -> None:
    global _cfg, _video_exts, _captions_cache, _storage

    parser = argparse.ArgumentParser(description="Phase 2b/2d — Photo Organizer review/spot-check tool")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1, localhost-only)")
    parser.add_argument("--port", type=int, default=None, help="Override config.yaml's review_tool_port")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open a browser tab")
    args = parser.parse_args()

    _cfg = load_config()
    _video_exts = _cfg.video_extensions_normalized
    _storage = get_storage(_cfg)
    _captions_cache = CaptionsCache(_cfg.captions_path_abs)
    _captions_cache.refresh()

    if not _cfg.db_path_abs.exists():
        raise SystemExit(
            f"No database found at {_cfg.db_path_abs} — run Phase 1 (`main.py run`) first, "
            "there's nothing to review yet."
        )

    # Phase 2d: same shared logging path every other entry point uses (see
    # src/logging_setup.py / CLAUDE.md rule 3) — login/logout/rate-limit
    # events land in logs/organize_<timestamp>.log alongside everything
    # else, not a second parallel log.
    logger, log_path = setup_logging(_cfg.log_dir_abs)
    register_auth(app, _cfg, logger=logger)
    if not _cfg.review_users:
        print("WARNING: no review_users configured in config.yaml — nobody can log in yet.")
        print("Add one with: venv\\Scripts\\python main.py review-user add <username>")

    port = args.port or _cfg.review_tool_port
    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{port}/"
    print(f"Photo Organizer review tool — read-only against {_cfg.db_path_abs} and {_cfg.captions_path_abs}")
    print(f"Log file: {log_path}")
    print(f"Open: {url}")
    if args.host == "0.0.0.0":
        print("NOTE: for Cloudflare Tunnel access, keep the default --host 127.0.0.1 — "
              "cloudflared reaches this app over loopback, so it never needs to listen "
              "on a LAN/router-facing interface at all (see README.md's Phase 2d section).")
    if not args.no_browser:
        _open_browser_later(url)

    app.run(host=args.host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
