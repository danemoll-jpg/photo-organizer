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
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import threading
import webbrowser
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request
from PIL import Image

from src.config import Config, load_config

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
    + folder substring (against current_path) + the video exclusion
    above. Returns (sql_fragment_after_WHERE, params)."""
    clauses = ["1=1"]
    params: list = []
    date_from = (args.get("date_from") or "").strip()
    date_to = (args.get("date_to") or "").strip()
    folder = (args.get("folder") or "").strip()
    if date_from:
        clauses.append("date_taken >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("date_taken < ?")
        params.append(date_to + "T23:59:59.999999")  # inclusive of the whole end day
    if folder:
        clauses.append("current_path LIKE ?")
        params.append(f"%{folder}%")
    sql = " AND ".join(clauses) + _video_exclusion_sql()
    params.extend(_video_exclusion_params())
    return sql, params


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
                 after: str | None, before: str | None) -> tuple[list[dict], bool, bool]:
    """Keyset (cursor) pagination on current_path — deliberately not
    OFFSET-based: OFFSET n costs an O(n) scan on the way to page n, which
    gets slower every page deeper into a 100k+-row table. A cursor lookup
    stays O(log n) via idx_photos_current_path regardless of how deep the
    user has paged. `after`/`before` are the current_path of the boundary
    item from the page the caller already has (exclusive)."""
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
    )


@app.route("/api/stats")
def api_stats():
    _captions_cache.refresh()
    conn = get_db()
    try:
        where, params = _build_filters(request.args)
        total = conn.execute(f"SELECT COUNT(*) FROM photos WHERE {where}", params).fetchone()[0]
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
        limit = min(max(int(request.args.get("limit", _cfg.review_page_size)), 1), 200)
        after = request.args.get("after")
        before = request.args.get("before")
        items, has_next, has_prev = _fetch_page(conn, where, params, limit, after, before)
        count = None
        if request.args.get("with_count"):
            count = conn.execute(f"SELECT COUNT(*) FROM photos WHERE {where}", params).fetchone()[0]
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
    """Viewer/slideshow single-step navigation. cursor is the current_path
    of the photo currently on screen (absent = start from the very first
    match). This is intentionally NOT page-based, so stepping past a
    grid page's last photo — or just running the slideshow continuously —
    never dead-ends at a page boundary: each step is one indexed lookup
    for "the next/previous matching row after this path", full stop."""
    _captions_cache.refresh()
    conn = get_db()
    try:
        where, params = _build_filters(request.args)
        cursor = request.args.get("cursor") or None
        direction = request.args.get("dir", "next")
        if direction == "prev":
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


def _get_image_bytes(path: Path, file_hash: str, max_dim: int) -> bytes:
    key = (file_hash, max_dim)
    with _image_cache_lock:
        cached = _image_cache.get(key)
    if cached is not None:
        return cached
    with Image.open(path) as img:
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
    path = Path(row[0])
    if not path.exists():
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
    global _cfg, _video_exts, _captions_cache

    parser = argparse.ArgumentParser(description="Phase 2b — Photo Organizer review/spot-check tool")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1, localhost-only)")
    parser.add_argument("--port", type=int, default=None, help="Override config.yaml's review_tool_port")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open a browser tab")
    args = parser.parse_args()

    _cfg = load_config()
    _video_exts = _cfg.video_extensions_normalized
    _captions_cache = CaptionsCache(_cfg.captions_path_abs)
    _captions_cache.refresh()

    if not _cfg.db_path_abs.exists():
        raise SystemExit(
            f"No database found at {_cfg.db_path_abs} — run Phase 1 (`main.py run`) first, "
            "there's nothing to review yet."
        )

    port = args.port or _cfg.review_tool_port
    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{port}/"
    print(f"Photo Organizer review tool — read-only against {_cfg.db_path_abs} and {_cfg.captions_path_abs}")
    print(f"Open: {url}")
    if not args.no_browser:
        _open_browser_later(url)

    app.run(host=args.host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
