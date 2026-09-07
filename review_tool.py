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

Read-only, deliberately (with one narrow startup-time exception — see the
URGENT-fix note below and Phase 2f's edit route further down):
- Every request-handling route's SQLite connection is opened via SQLite's
  own `mode=ro` URI (get_db()), so nothing here can write to it even by
  accident — any attempted write raises rather than silently succeeding.
- **Exception, added fixing the URGENT empty-grid bug**: main() calls
  src.db.init_db() once at startup, via a normal (non-ro) connection, the
  same "safe/idempotent — CREATE TABLE IF NOT EXISTS + any pending
  migration" call every other entry point (main.py's commands,
  dashboard.py's panels) already made before touching the DB. This file
  used to be the one entry point that never called it, on the theory that
  a read-only tool shouldn't touch schema — but that meant a real
  production DB last migrated by some other command before a schema-adding
  session (e.g. Phase 2f's `excluded` column + `photo_edits` table) never
  actually got that migration applied just by restarting review_tool.py,
  which is exactly the rule-11 restart workflow this project uses. See
  main()'s init_db() call site for the full incident writeup.
- captions.jsonl is only ever opened for reading, tailed incrementally
  (see CaptionsCache below) as the in-progress Phase 2 run appends to it.
- This tool never runs organize/caption/extract-gps itself, and never
  edits captions/tags — it only displays whatever those other steps have
  already produced, including "nothing yet", which must show clearly as
  such (e.g. "Not yet captioned"), never as an error or a blank gap.

Video files (Phase 2e): included in every query, grid, and the viewer,
same as photos — they already have real DB rows (hash/path/date) from the
shared organize.py pipeline, only display logic differed before. Since
Phase 2 never captions video by design (see caption.py), an uncaptioned
video shows plainly as "No caption" (not "Not yet captioned", which
wrongly implies a pending automated process — see _row_to_dict's
`is_video` field and review.js). GPS/location is genuinely unavailable
for video (confirmed absent from container metadata — see
gps_resolver.py) so it always renders as "no location", same as any
photo that's been checked and had none found — not a special case.
Grid thumbnails (Phase 2g): a real cached first-frame JPEG, served by
/thumbnail/<file_hash> below, when one has been extracted by
`main.py extract-thumbnails` / the dashboard's Phase 2g panel
(src/thumbnail_backfill.py) — `_row_to_dict`'s `has_thumbnail` field tells
the front end whether to request it; a video not yet processed (or one
that failed to decode) falls back to the same generic play-icon
placeholder Phase 2e originally shipped, never a broken image. The full
viewer plays the actual file via a `<video>` element backed by the
/video/<file_hash> route below, not the photo-only /image/<file_hash>
route — real thumbnails only ever affect the grid.

People/faces: originally a reserved-but-inert `people` field (always `None`,
Phase 3 didn't exist yet) — see the Phase 3d/3e notes below for how it's
now wired up for real: Phase 3d wired the grid's FILTER, Phase 3e wired the
per-photo DISPLAY (viewer "People" row + grid card badges).

Phase 3d: the grid's people/faces FILTER (added inert in Phase 2c, disabled
control, "Phase 3 doesn't exist yet") was wired to real Phase 3 data — a
`person` query param (a `people.cluster_id`) narrows every browsing route to
photos with at least one face in that cluster, via a plain `faces`/`people`
join folded straight into `_build_filters` (see its docstring): unlike
tag/caption-keyword filtering, this needs no live-cache scanning path, it's
a real committed DB table like `location`, so it stays on the cheap indexed
SQL path and every route that already calls `_build_filters` (grid, nav,
random, stats) picks it up automatically, no per-route changes needed.
`/api/facets` gained a `people` list (real assigned names only, from the
`people` table — unclustered/unnamed faces are never offered) for the front
end's dropdown — see its docstring. Phase 3d deliberately left the
per-photo `people` display field always `None` — filter/browse only, not
per-photo face display.

Phase 3g: Phase 3d's single-person filter is generalized to a multi-person
filter with AND semantics — select several named people and see only
photos containing every one of them (the user's own example: Dan Moll +
Juliana Moll -> photos with both, not either). New `people` query param
(comma-separated cluster_ids) alongside the still-supported single-value
`person` (kept as a legacy alias so nothing that already used it breaks —
see `_build_filters`' docstring for exactly how the two combine into one
code path). The query is a real relational intersection (`GROUP BY
file_hash HAVING COUNT(DISTINCT cluster_id) = N`), not an `IN (...)` — the
latter would silently produce OR semantics, matching a photo with just one
of the selected people, which is explicitly NOT what was asked for. Same
free ride as Phase 3d: this lives entirely in `_build_filters`, so every
route that already calls it (grid, nav, random, stats) enforces the AND
semantics automatically with zero per-route changes, and it composes with
every other filter (date/tag/location/media_type) exactly like `person`
already did. Front end: `static/review.js`'s Person filter is now a
chip-list (type a name, pick a suggestion, it becomes a removable chip,
repeat) built around the same `attachSuggestionDropdown` type-ahead
component Tag/bulk-edit-Location/Phase 3d's Person already used — the chip
LIST container itself is new UI, not a fourth drop-in reuse of a
single-value instance. No "any of these people" (OR) mode was requested or
built — see photo-organizer-spec.md's Phase 3g section.

Phase 3e (this session): the per-photo `people` field Phase 3d left as
`None` is now populated for real — see `_people_for_hashes`/`_row_to_dict`
below. Every photo/video response carries the named people (from the
`faces`/`people` join, `not_a_person=0`, assigned_name real — same
named-only posture as Phase 3d's `/api/facets` list) actually appearing in
THAT specific file, not just whether a filter matches. The grid's page
fetch batches this into one query per page (`_fetch_page`) rather than one
per row, so it stays cheap at 100k+-row scale — see `_people_for_hashes`'s
docstring. Still read-only, still no editing surface here (that stays in
the Tkinter `FaceLabelingWindow` only) — this is display only, same scope
line Phase 3d drew for the filter.

Phase 2c added: a Random button + random slideshow order in the viewer
(/api/random, /api/nav?mode=random — see their docstrings), and new
filters (tag, caption keyword, GPS/location, plus a visible-but-inert
people/faces control that's UI-only, never sent to the server). Every
filter — old and new — applies identically to the grid (/api/photos) and
to viewer/slideshow navigation (/api/nav), per spec. See
_build_filters/_build_extra_predicate for how the two different filter
"sources" (plain DB columns vs. the live captions cache) are combined.

Grid/Viewer nav cleanup follow-up: the Location and Tag filters became
dropdown/autocomplete controls fed by /api/facets (distinct known values
from the DB — `tags.tag_name` and `photos.location_name`), instead of
free-text search, so the user picks a real existing value instead of
guessing spelling/formatting. `location` matching switched from substring
LIKE to an exact match to match dropdown semantics. The old standalone
GPS has/no-location toggle was folded into the same Location dropdown as
a "No location" option (still `has_location=no` under the hood — see
_build_filters) since it was otherwise redundant. The old free-text
"Folder contains" filter was removed from the UI (redundant with the
date-range pickers given the YYYY/YYYY-MM layout) — the `folder` query
param itself stays supported server-side (harmless, still covered by
existing tests) even though nothing in the UI sends it anymore.

Grid filter improvements batch (this follow-up session): (1) the Tag
filter's suggestion UI moved from a `<datalist>` to a hand-built dropdown
in static/review.js (`<datalist>` never renders its suggestion popup on
WebKit/iOS at all -- a longstanding platform limitation, not a bug here;
the underlying exact-match filtering was always fine there, only the
suggestions were invisible) -- purely a templates/review.html + static/
review.js/review.css change, nothing here. (2) A visible match-count
("N item(s) match your filters") when a filter is active, in
static/review.js -- reuses /api/stats' existing filtered `total_photos`
count (already correct, already tested since Phase 2b's regression fix),
no new backend logic. (3) A `media_type` filter (All/Photos only/Videos
only) -- see _build_filters above.

Phase 2d added remote/shared access: real session-based login (see
src/auth.py — a small local username+password list in config.yaml's
review_users, rate-limited, NOT HTTP Basic Auth), meant to be reached
either directly on localhost or through a separate Cloudflare Tunnel (see
README.md/photo-organizer-spec.md — independent of Plex's own remote
access, never reusing it). Every route except /login, /logout, and
/static/* now requires a session (src/auth.py::register_auth's
before_request hook) — a browser navigation redirects to /login, an
unauthenticated /api/*, /image/*, or /video/* call gets a plain 401 (review.js
bounces itself to /login on that, since a redirect response doesn't help
a fetch() call). Phase 2d also introduced a storage-abstraction seam
(src/storage.py::PhotoStorage) between this file and "where photo bytes
actually live" — local disk today (LocalDiskStorage), groundwork only for
a possible future cloud-storage backend, not an actual migration.

Phase 2f (deliberate, significant reversal of the "read-only" framing
above): now that Phase 2d's real auth exists, every authenticated user can
edit caption/tags/location/date_taken via a new, narrowly-scoped
/api/edit/<file_hash> route (see src/edits.py) — the ONLY route in this
whole file that writes to the DB. It uses its own separate read-write
connection (get_db_write()), CSRF-protected exactly like login/logout (a
per-session token minted at login — see src/auth.py — compared against the
request body), against a server-side allow-list of field names
(src.edits.EDITABLE_FIELDS) so a client can never name an arbitrary column.
Every OTHER route above and below is completely untouched: they still all
go through get_db()'s mode=ro connection, and still cannot write even by
accident. A manual edit is permanently protected from a future automated
overwrite by main.py load-captions / src/gps_backfill.py (see src/edits.py
::has_manual_edit) — _row_to_dict below checks this per row so an edited
caption/tags value is shown instead of the live captions.jsonl cache's
value the moment it's edited, not just after some future reload.
"Hide from library" (the `excluded` column) uses this exact same edit/audit
mechanism — it's just another field name, not a separate code path — and
every browsing route (_build_filters) now excludes excluded=1 rows by
default, revealed again only via a "Show hidden" toggle (show_hidden=1).

Phase 2h added a second write route, /api/bulk-edit, for the grid's
checkbox multi-select + bulk-action bar (static/review.js): the same
auth/CSRF/allow-list posture as /api/edit/<hash>, applied to a client-
supplied list of file_hash values in one request. Set location/date/Hide
reuse apply_edit()'s existing full-"set" semantics per item; "Add tag" goes
through src.edits.add_tag_bulk() instead — a union/append into each item's
own existing tags, deliberately NOT apply_edit()'s full-replace, since
different selected files likely have different tags a full replace would
destroy. See BULK_SET_FIELDS below and api_bulk_edit()'s own docstring.

Phase 3h added per-user favorites — a third write route, /api/favorite/
<hash> (see src/favorites.py), same auth/CSRF posture as the other two
write routes but deliberately NOT built on apply_edit()/EDITABLE_FIELDS:
favorites are one row per (file_hash, username), not a single photos-table
column, and are NOT part of the photo_edits audit mechanism (nothing ever
automates favoriting a photo, so there's no future automated overwrite to
protect against — see src/favorites.py's module docstring). Every
authenticated user can favorite/unfavorite independently — marking or
unmarking a favorite for the CURRENTLY LOGGED-IN user only ever touches
that user's own row, never anyone else's. `_row_to_dict` gained
`favorited_by` (a list of usernames, batched via
src.favorites.favorites_for_hashes — same one-query-per-page discipline
Phase 3e's `_people_for_hashes` established, see _fetch_page) and
`favorited_by_me` (whether the CURRENT session's user is in that list).
The grid shows a compact star badge for favorited-by-me only (per spec,
"keep cards visually clean"); the full "who favorited this" list is a
viewer-only detail, same compact-grid/full-viewer split Phase 3e already
established for people. `_build_filters` gained `favorite_by`: `__me__`
(resolved server-side to `session["user"]`, never trusted from the
client), `__any__` (favorited by at least one user), or a literal
username — a real DB table like `location`/`people`, so (like those) it
needs no live-cache scanning fallback and every route that already calls
`_build_filters` (grid, nav, random, stats) picks it up automatically.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import mimetypes
import random
import sqlite3
import threading
import webbrowser
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file, session
from PIL import Image, ImageOps

from src.auth import register_auth
from src.config import Config, load_config
from src.db import init_db
from src.edits import EDITABLE_FIELDS, MAX_BULK_ITEMS, add_tag_bulk, apply_edit, current_tags, manual_edit_fields
from src.favorites import favorites_for_hashes, set_favorite
from src.logging_setup import setup_logging
from src.port_check import listening_pids
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
_logger: logging.Logger = logging.getLogger("photo_organizer")  # overwritten by main() with the real configured logger


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


def _thumbnail_path(file_hash: str) -> Path:
    """Phase 2g: where a cached video thumbnail lives for this file_hash, if
    src/thumbnail_backfill.py has already generated one. Deliberately NOT
    routed through the PhotoStorage abstraction (unlike serve_image/
    serve_video) — thumbnails are this tool's own local cache artifact, not
    "where the original photo/video bytes live", so they stay a plain local
    path regardless of storage_backend."""
    return _cfg.thumbnail_dir_abs / f"{file_hash}.jpg"


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


def get_db_write() -> sqlite3.Connection:
    """Phase 2f: a fresh READ-WRITE connection per call, used ONLY by the
    /api/edit/<file_hash> route below -- every other route in this file
    still exclusively uses get_db()'s mode=ro connection above, completely
    unchanged. A separate connection (not a mode=ro one reopened without
    the flag) so it's structurally obvious at every call site which code
    path is allowed to write, matching this file's existing "read-only,
    deliberately" posture for everything else (see module docstring)."""
    conn = sqlite3.connect(str(_cfg.db_path_abs))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _build_filters(args) -> tuple[str, list]:
    """Shared WHERE-clause builder for every query below: date range
    (against date_taken -- ISO8601 text sorts correctly lexicographically)
    + folder substring (against current_path) + GPS/location (Phase 2c:
    place-name filter against `location_name`, and/or a has/no-location
    toggle against the same column). All of these are plain DB columns, so
    they stay on the cheap indexed-or-LIKE SQL path. Tag and
    caption-keyword filtering (also Phase 2c) are deliberately NOT here --
    see _build_extra_predicate for why those two need a different
    mechanism. Video rows are NOT excluded here (Phase 2e reversed that
    earlier judgment call — see module docstring): they're plain `photos`
    rows like any other, filtered the same way. Returns
    (sql_fragment_after_WHERE, params).

    `location` is an EXACT match, not substring -- since the Grid/Viewer
    nav cleanup follow-up, the client only ever sends a value picked from
    /api/facets' list of real, already-existing `location_name` values, so
    there's no spelling/formatting to fuzzy-match against anymore. `folder`
    stays substring/LIKE -- it has no dropdown, and no source of truth for
    "every folder" to pick from the way tags/locations do.

    `media_type` (Grid filter improvements batch: All/Photos only/Videos
    only) belongs here rather than in _build_extra_predicate, same
    reasoning as `location` -- it's derived purely from a plain DB column
    (`filename`'s extension against the module-global `_video_exts`, the
    exact same check `_row_to_dict`'s `is_video` field already uses), no
    live-cache lookup needed, so it stays on the cheap indexed/LIKE SQL
    path rather than forcing every query through the batched-scan path
    tag/caption-keyword filtering needs.

    `person` / `people` (Phase 3d added the single-value `person` param;
    Phase 3g generalizes it to `people`, a comma-separated list of
    `people.cluster_id` values, with AND semantics -- a matching photo must
    have a face in EVERY listed cluster_id, not just one, per the user's
    explicit "Dan Moll + Juliana Moll -> photos with both" example). Both
    params funnel into one code path: `person_ids` is built from `people`
    (split on comma) with `person` folded in as a single-value legacy alias
    if `people` wasn't given -- so old links/tests using `person=<id>` keep
    working unchanged, and the AND-query below degenerates correctly to a
    plain single-person match when there's only one id (COUNT(DISTINCT
    cluster_id) = 1 is trivially satisfied by one id, no special-casing
    needed).

    The actual query is the relational "contains ALL of these people"
    intersection the spec asks for, not a plain `IN (...)` (which would
    wrongly produce OR semantics -- e.g. person A OR person B, not both):

        file_hash IN (
            SELECT file_hash FROM faces WHERE cluster_id IN (?, ?, ...)
            GROUP BY file_hash HAVING COUNT(DISTINCT cluster_id) = <N>
        )

    Grouping by file_hash and requiring exactly N distinct cluster_ids
    matched (N = the number of ids requested) means a photo only qualifies
    if every requested person has at least one face in it -- a photo with
    faces for only some of the requested people, or with duplicate faces
    for the same requested person, is still correctly excluded/not
    double-counted (COUNT(DISTINCT ...), not COUNT(...)).

    Same reasoning as `location`/`media_type` above for why this belongs
    here rather than in _build_extra_predicate: a real, already-committed
    DB table (Phase 3's `faces`/`people`), not the live captions cache, so
    it needs no scanning fallback and every route that already calls
    `_build_filters` (grid, nav, random, stats) picks up the AND semantics
    automatically, no per-route changes needed -- same free ride Phase 3d's
    single-person version already got. Deliberately permissive like
    `location` -- this only matches against whatever cluster_ids the client
    sends, it does not itself check that they resolve to NAMED people; that
    restriction lives one layer up, in which cluster_ids /api/facets'
    `people` list offers the front end to pick from (see its docstring) --
    a hand-crafted URL naming an unclustered/unnamed cluster_id still
    filters correctly (just to whatever faces share that id), it's simply
    never offered as a choice in the UI. A non-integer entry is silently
    dropped from the list (same posture as an unrecognized `media_type`),
    not an error; duplicate ids in the list are de-duped before building N,
    so `people=10,10` behaves identically to `people=10`.

    `favorite_by` (Phase 3h): filters to photos favorited by a specific
    user, or by anyone. Same reasoning as `location`/`media_type`/`people`
    above -- a real, already-committed DB table (`favorites`), so it needs
    no live-cache scanning fallback and every route that already calls
    `_build_filters` picks it up automatically. Three shapes:
      - `__me__`: resolved server-side to `session["user"]` (never trusted
        from the client -- a request can't spoof "my" favorites as
        someone else's by sending a literal username instead). If somehow
        called with no session user (shouldn't happen -- every route here
        is auth-gated), matches nothing rather than leaking anyone's data.
      - `__any__`: favorited by at least one user, regardless of who.
      - anything else: treated as a literal username (deliberately
        permissive, same posture as `location`/`person` -- the front end
        only ever offers real `review_users` entries to pick from via
        /api/facets, but a hand-crafted URL naming an unknown username
        still filters correctly, just to zero rows).

    `show_hidden` (Phase 2f "Hide from library"): by default every route
    that calls this excludes `excluded=1` rows entirely -- they're meant to
    have permanently disappeared from the tool, per spec. Passing
    show_hidden=1 (the UI's "Show hidden" checkbox) lifts that restriction
    so a hidden item can be found again and un-hidden via the same edit
    mechanism (see /api/edit/<file_hash>) -- it does NOT restrict to ONLY
    hidden items, it just stops filtering them out, so hidden and visible
    items are browsable together (the front end marks hidden ones).

    The clause below matches `excluded IS NULL` as well as `excluded = 0`,
    defensively -- NULL = 0 is never true in SQL, so a plain `excluded = 0`
    would silently exclude every row where `excluded` came out NULL rather
    than 0. Belt-and-suspenders: `_migrate()`'s actual ALTER TABLE (`... ADD
    COLUMN excluded INTEGER NOT NULL DEFAULT 0`) already backfills every
    pre-existing row to a real 0, not NULL -- confirmed by reproducing it
    against a real copy of the production DB, not assumed (see TODO.md's
    URGENT section) -- so no row should ever actually be NULL here. This
    guards against a future migration path getting that wrong without
    anyone noticing until the grid goes silently empty again.
    """
    clauses = ["1=1"]
    params: list = []
    date_from = (args.get("date_from") or "").strip()
    date_to = (args.get("date_to") or "").strip()
    folder = (args.get("folder") or "").strip()
    location = (args.get("location") or "").strip()
    has_location = (args.get("has_location") or "").strip().lower()
    media_type = (args.get("media_type") or "").strip().lower()
    person = (args.get("person") or "").strip()  # legacy single-value alias, see docstring
    people_param = (args.get("people") or "").strip()  # Phase 3g: comma-separated cluster_ids, AND semantics
    favorite_by = (args.get("favorite_by") or "").strip()  # Phase 3h: __me__ | __any__ | a literal username
    show_hidden = (args.get("show_hidden") or "").strip().lower() in ("1", "true", "yes")
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
        clauses.append("location_name = ?")
        params.append(location)
    if has_location == "yes":
        clauses.append("location_name IS NOT NULL")
    elif has_location == "no":
        # Deliberately simple binary per spec ("has/no location"): this
        # also matches rows never GPS-checked at all (gps_checked=0), not
        # just ones checked-and-found-nothing -- both cases really do have
        # "no location" to show right now, and extract-gps is a separate,
        # user-run step (see CLAUDE.md), so most of the library may not be
        # checked yet at any given time. Video rows land here too now
        # (Phase 2e) -- gps_backfill.py checks them like any other file and
        # always records no coordinates, so they correctly show "no
        # location" through this same path, not a special case.
        clauses.append("location_name IS NULL")
    if media_type in ("photo", "video"):
        if _video_exts:
            video_clause = "(" + " OR ".join(["filename LIKE ?"] * len(_video_exts)) + ")"
            like_params = [f"%{ext}" for ext in sorted(_video_exts)]
        else:
            # No video extensions configured at all -- nothing can be a
            # video, so "video only" correctly matches zero rows and
            # "photo only" correctly imposes no restriction.
            video_clause = "0"
            like_params = []
        clauses.append(video_clause if media_type == "video" else f"NOT {video_clause}")
        params.extend(like_params)
    # Phase 3g: `people` (comma-separated, AND semantics) generalizes
    # Phase 3d's single-value `person` -- see docstring above. Build one
    # de-duped list of int cluster_ids from whichever the caller provided,
    # preferring `people` when both happen to be present.
    person_ids: list[int] = []
    seen_ids: set[int] = set()
    raw_ids = people_param.split(",") if people_param else ([person] if person else [])
    for raw in raw_ids:
        raw = raw.strip()
        if not raw:
            continue
        try:
            pid = int(raw)
        except ValueError:
            continue  # silently dropped, same posture as an unrecognized media_type
        if pid not in seen_ids:
            seen_ids.add(pid)
            person_ids.append(pid)
    if person_ids:
        placeholders = ", ".join(["?"] * len(person_ids))
        clauses.append(
            f"file_hash IN (SELECT file_hash FROM faces WHERE cluster_id IN ({placeholders}) "
            f"GROUP BY file_hash HAVING COUNT(DISTINCT cluster_id) = ?)"
        )
        params.extend(person_ids)
        params.append(len(person_ids))
    if favorite_by == "__me__":
        current_user = session.get("user")
        if current_user:
            clauses.append("file_hash IN (SELECT file_hash FROM favorites WHERE username = ?)")
            params.append(current_user)
        else:
            clauses.append("0")  # no session user (shouldn't happen -- see docstring) -- match nothing
    elif favorite_by == "__any__":
        clauses.append("file_hash IN (SELECT DISTINCT file_hash FROM favorites)")
    elif favorite_by:
        clauses.append("file_hash IN (SELECT file_hash FROM favorites WHERE username = ?)")
        params.append(favorite_by)
    if not show_hidden:
        clauses.append("(excluded IS NULL OR excluded = 0)")
    sql = " AND ".join(clauses)
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


def _people_for_hashes(conn: sqlite3.Connection, file_hashes: list[str]) -> dict[str, list[dict]]:
    """Phase 3e: batch lookup of named people appearing in each of the given
    file_hashes, keyed by file_hash -> a list of {"cluster_id", "name"}
    sorted by name. Named only -- same "only offer real, already-meaningful
    values" posture as /api/facets' `people` list (Phase 3d) -- and excludes
    faces flagged not_a_person (Phase 3c: a human-confirmed false-positive
    detection isn't a person to display, same reasoning run_face_clustering()
    already applies when deciding what's a real candidate).

    Deliberately ONE query for however many file_hashes are passed, not one
    query per row -- the grid's page fetch (_fetch_page below) calls this
    once with every file_hash on the current page (at most `review_page_size`,
    capped at 200 by api_photos), never per-row, so this stays a single
    indexed IN(...) lookup against idx_faces_file_hash regardless of page
    size. Single-row callers (the viewer's /api/nav, /api/random,
    /api/edit/<hash>) just pass a one-element list -- same query shape, same
    index, negligible cost either way. Returns {} for an empty input without
    ever touching the DB (a page can't skip the query it's not on)."""
    if not file_hashes:
        return {}
    placeholders = ",".join("?" * len(file_hashes))
    sql = f"""
        SELECT DISTINCT f.file_hash, p.cluster_id, p.assigned_name
        FROM faces f
        JOIN people p ON p.cluster_id = f.cluster_id
        WHERE f.file_hash IN ({placeholders})
          AND f.not_a_person = 0
          AND p.assigned_name IS NOT NULL AND TRIM(p.assigned_name) != ''
        ORDER BY p.assigned_name COLLATE NOCASE
    """
    result: dict[str, list[dict]] = {}
    for r in conn.execute(sql, file_hashes).fetchall():
        result.setdefault(r["file_hash"], []).append({"cluster_id": r["cluster_id"], "name": r["assigned_name"]})
    return result


def _row_to_dict(row: sqlite3.Row, conn: sqlite3.Connection, people: list[dict] | None = None,
                  favorited_by: list[str] | None = None) -> dict:
    """`conn` (Phase 2f) is needed to check whether this row's caption/tags
    have ever been manually edited -- see the branch below. Every call site
    already has a live connection open (the same one used to fetch `row`),
    so this is never a new connection, just threading the existing one
    through.

    `people` (Phase 3e): pre-fetched named-people list for this exact row,
    normally supplied by a caller that already batch-fetched a whole page's
    worth via _people_for_hashes (see _fetch_page) so this function itself
    never issues a query per row. A single-row caller (nav/random/edit) can
    leave it as the default None, which does one _people_for_hashes lookup
    for just this file_hash -- same query shape either way, just batched vs.
    not depending on how many rows are being converted at once.

    `favorited_by` (Phase 3h): same batching contract as `people` above, but
    backed by src.favorites.favorites_for_hashes -- a caller with a whole
    page's worth (see _fetch_page) passes the pre-fetched list for this row;
    a single-row caller (nav/random/edit/favorite-toggle) leaves it as the
    default None for one favorites_for_hashes([file_hash]) lookup."""
    d = dict(row)
    file_hash = d["file_hash"]
    if people is None:
        people = _people_for_hashes(conn, [file_hash]).get(file_hash, [])
    if favorited_by is None:
        favorited_by = favorites_for_hashes(conn, [file_hash]).get(file_hash, [])
    edited_fields = manual_edit_fields(conn, file_hash)
    current_path = d["current_path"] or ""
    dest_root = str(_cfg.dest_root_path)
    relative_path = current_path
    if current_path.lower().startswith(dest_root.lower()):
        relative_path = current_path[len(dest_root):].lstrip("\\/")
    is_video = Path(d["filename"] or "").suffix.lower() in _video_exts
    # Phase 2g: only a video can have a cached thumbnail, so the stat() call
    # is skipped entirely for the (large majority) of rows that are photos.
    has_thumbnail = is_video and _thumbnail_path(file_hash).exists()

    # Phase 2f: caption/tags normally come from the live captions.jsonl
    # cache (see module docstring -- always-live, no reruns needed), but
    # once a human has edited either field for this file_hash, the DB
    # column (photos.caption / the tags/photo_tags join, both written by
    # apply_edit -- see src/edits.py) becomes the permanent source of truth
    # for THAT field instead, so an edit is reflected the instant it's
    # saved rather than only after some future load-captions run
    # overwrites the cache's notion of it (which it now won't -- see
    # has_manual_edit in load_captions.py/gps_backfill.py). The two fields
    # are tracked independently: editing only tags on an AI-captioned photo
    # still shows the AI caption, and vice versa.
    cap = _captions_cache.get(file_hash)
    if "caption" in edited_fields:
        caption_value = d["caption"]
        captioned = caption_value is not None
    else:
        caption_value = cap.get("caption") if cap else None
        captioned = cap is not None
    if "tags" in edited_fields:
        tags_value = current_tags(conn, file_hash)
    else:
        tags_value = (cap.get("tags") if cap else None) or []

    return {
        "file_hash": file_hash,
        "current_path": current_path,
        "relative_path": relative_path,
        "filename": d["filename"],
        "date_taken": d["date_taken"],
        "date_source": d["date_source"],
        "status": d["status"],
        "is_video": is_video,
        "has_thumbnail": has_thumbnail,
        "captioned": captioned,
        "caption": caption_value,
        "tags": tags_value,
        "location_name": d.get("location_name"),
        "gps_checked": bool(d.get("gps_checked")),
        # Phase 2f: "Hide from library" -- see _build_filters' show_hidden
        # handling. Carried on every response (not just when show_hidden is
        # active) so the front end can badge/style a hidden item whenever
        # one happens to be in view.
        "excluded": bool(d.get("excluded")),
        # Which editable fields have ever been manually touched on this row
        # -- not currently rendered distinctly by the front end, but harmless
        # to expose now rather than needing a new field later.
        "edited_fields": sorted(edited_fields),
        # Phase 3e: named people appearing in this specific photo (see
        # _people_for_hashes above) -- Phase 3d wired up the grid's people
        # FILTER, this wires up actual per-photo DISPLAY. Read-only, same as
        # everything else in this response -- no editing surface here, that
        # stays in the Tkinter labeling window only.
        "people": people,
        # Phase 3h: who has favorited this specific photo (usernames, sorted)
        # and whether the CURRENTLY LOGGED-IN user is one of them -- see
        # module docstring / src/favorites.py. Read via the request's own
        # session, same as every other per-request value here; a route with
        # no session user (shouldn't happen -- every route is auth-gated)
        # just gets favorited_by_me=False rather than raising.
        "favorited_by": favorited_by,
        "favorited_by_me": session.get("user") in favorited_by,
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
    instead of a single indexed query — see its docstring.

    Phase 3e: named-people data for the whole page is fetched in exactly ONE
    extra query (_people_for_hashes, keyed by every file_hash on this page),
    not one query per row — see that function's docstring for why this
    stays cheap at 100k+-row scale: results are already paginated (limit is
    capped at 200 by api_photos), so this join only ever runs against the
    current page's rows, never the whole filtered set.

    Phase 3h: favorites data for the whole page is batched the exact same
    way, via favorites_for_hashes — one more query per page, not one per
    row, same discipline."""
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
        hashes = [r["file_hash"] for r in rows]
        people_map = _people_for_hashes(conn, hashes)
        favorites_map = favorites_for_hashes(conn, hashes)
        return [
            _row_to_dict(r, conn, people=people_map.get(r["file_hash"], []),
                         favorited_by=favorites_map.get(r["file_hash"], []))
            for r in rows
        ], has_next, has_prev
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
    hashes = [r["file_hash"] for r in rows]
    people_map = _people_for_hashes(conn, hashes)
    favorites_map = favorites_for_hashes(conn, hashes)
    return [
        _row_to_dict(r, conn, people=people_map.get(r["file_hash"], []),
                     favorited_by=favorites_map.get(r["file_hash"], []))
        for r in rows
    ], has_next, has_prev


@app.route("/")
def index():
    return render_template(
        "review.html",
        page_size=_cfg.review_page_size,
        slideshow_seconds=_cfg.review_slideshow_seconds,
        dest_root=str(_cfg.dest_root_path),
        current_user=session.get("user"),
        logout_csrf=session.get("_csrf"),
        # Phase 2f: the exact same per-session token the logout form above
        # already uses — review.js sends it back on every /api/edit/<hash>
        # call, checked against session["_csrf"] the same way login/logout
        # already are (see api_edit's docstring).
        edit_csrf=session.get("_csrf"),
    )


@app.route("/api/facets")
def api_facets():
    """Distinct known values for the grid's Location and Tag filter
    controls (Grid/Viewer nav cleanup follow-up) -- backed by the DB
    (`tags.tag_name`, `photos.location_name`), not the live captions.jsonl
    cache, since this just needs a reasonable list to pick from, not
    up-to-the-second freshness. Actual filtering is unaffected by that lag:
    tag matching still goes through _build_extra_predicate against the live
    cache exactly as before (see its docstring) -- the Tag control is a
    free-text <input> with a <datalist> of these suggestions, not a closed
    <select>, so a brand new tag from an in-progress Phase 2 run that
    hasn't reached `main.py load-captions` yet is still filterable if typed
    exactly. Location IS a closed <select> (see _build_filters' note on why
    it can be exact-match), so a location added to the DB by extract-gps
    after the page loaded won't appear until the next page load/refresh --
    acceptable since extract-gps is a fast, user-run, occasional step (see
    CLAUDE.md), not a multi-day background process like Phase 2 captioning.

    Phase 3d added `people`: real named people from the `people` table
    (`cluster_id` + `assigned_name`), sorted by name -- deliberately
    excludes unclustered/unnamed rows (`assigned_name IS NULL` or blank),
    same "only offer real, already-meaningful values" posture as
    `locations` above. Unlike `tags`/`locations` (plain string lists, since
    a tag name IS the value filtered on), each entry here is an object --
    the Person filter needs the id (`cluster_id`, what _build_filters'
    `person` param actually matches on) alongside the display name, since
    `assigned_name` itself has no uniqueness constraint (two different real
    people could share a name) -- see static/review.js's loadFacets for how
    the front end disambiguates that rare case.

    Phase 3h added `review_users`: every configured review_users username
    (config.yaml, not a DB table -- see src/config.py), sorted, for the
    Favorites filter dropdown's "each named review_user individually"
    option per spec. Not filtered to "has favorited something" -- unlike
    `locations`/`people`/`tags` above, the point here isn't "only offer
    values that already mean something," it's "let the user pick who to
    check," and a user who hasn't favorited anything YET is still a valid,
    useful thing to pick (the result is just correctly empty).
    """
    conn = get_db()
    try:
        tags = [r[0] for r in conn.execute(
            "SELECT tag_name FROM tags ORDER BY tag_name COLLATE NOCASE"
        ).fetchall()]
        locations = [r[0] for r in conn.execute(
            "SELECT DISTINCT location_name FROM photos "
            "WHERE location_name IS NOT NULL ORDER BY location_name COLLATE NOCASE"
        ).fetchall()]
        people = [dict(r) for r in conn.execute(
            "SELECT cluster_id, assigned_name FROM people "
            "WHERE assigned_name IS NOT NULL AND TRIM(assigned_name) != '' "
            "ORDER BY assigned_name COLLATE NOCASE"
        ).fetchall()]
    finally:
        conn.close()
    review_users = sorted(
        (u.get("username") for u in _cfg.review_users if u.get("username")),
        key=str.lower,
    )
    return jsonify({"tags": tags, "locations": locations, "people": people, "review_users": review_users})


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
            return jsonify({"item": _row_to_dict(row, conn) if row is not None else None})

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
        item = _row_to_dict(row, conn) if row is not None else None
    finally:
        conn.close()
    return jsonify({"item": item})


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
        item = _row_to_dict(row, conn) if row is not None else None
    finally:
        conn.close()
    return jsonify({"item": item})


@app.route("/api/edit/<file_hash>", methods=["POST"])
def api_edit(file_hash: str):
    """Phase 2f: the ONE write path in this whole file — every other route
    above/below is still exclusively read-only (see module docstring).
    Behind the same auth gate as every non-public route already (nothing
    special needed here — src/auth.py's before_request already covers
    /api/*), PLUS its own CSRF check matching login/logout's pattern
    exactly: a per-session token minted at login (src/auth.py), compared
    against this request's body, not a header or query param — a mismatch
    (including a session with no token at all, e.g. one somehow reaching
    this route without ever having logged in through this app) is rejected
    before any DB write is attempted.

    Body: {"field": <one of src.edits.EDITABLE_FIELDS>, "value": ...,
    "csrf_token": "..."}. `field` is checked against the server-side
    allow-list before ever reaching apply_edit() — an unknown name is
    rejected outright, never trusted from the client, matching this
    project's existing "server-side allow-list, don't trust the client"
    posture elsewhere (e.g. src/port_check.py's restart only ever kills
    PIDs it looked up itself).

    Returns the freshly-updated photo (same shape /api/photos/.../api/nav
    items already have) on success, so the front end can just re-render
    from the response rather than re-fetching."""
    data = request.get_json(silent=True) or {}
    if data.get("csrf_token") != session.get("_csrf"):
        return jsonify({"error": "Your session looks stale — reload the page and try again."}), 403

    field = data.get("field")
    if field not in EDITABLE_FIELDS:
        return jsonify({"error": f"{field!r} is not an editable field."}), 400
    value = data.get("value")
    edited_by = session.get("user") or "unknown"

    write_conn = get_db_write()
    try:
        try:
            apply_edit(write_conn, file_hash, field, value, edited_by=edited_by, logger=_logger)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    finally:
        write_conn.close()

    # Re-read through the normal read-only connection (untouched by the
    # write above) so the response reflects exactly what every other route
    # would now show for this photo — same _row_to_dict path, same
    # manual-edit-aware caption/tags resolution.
    _captions_cache.refresh()
    read_conn = get_db()
    try:
        row = read_conn.execute("SELECT * FROM photos WHERE file_hash = ?", (file_hash,)).fetchone()
        if row is None:
            return jsonify({"error": "Photo not found."}), 404
        item = _row_to_dict(row, read_conn)
    finally:
        read_conn.close()
    return jsonify({"item": item})


# Phase 2h: fields the bulk-edit route may apply the SAME value to every
# selected item via apply_edit()'s existing full-"set" semantics. Caption
# is deliberately excluded (setting one identical caption across a batch of
# different photos was never asked for); "tags" is also excluded from this
# set on purpose -- bulk tagging goes through add_tag_bulk()'s union/append
# path instead (mode="add_tag" below), never apply_edit()'s full-replace,
# per photo-organizer-spec.md's explicit warning not to reuse that code
# path blindly for a batch of files with different existing tags.
BULK_SET_FIELDS = {"location", "date_taken", "excluded"}


@app.route("/api/bulk-edit", methods=["POST"])
def api_bulk_edit():
    """Phase 2h: apply one edit to many selected items at once (the grid's
    checkbox multi-select + bulk-action bar — see static/review.js). Same
    auth gate (before_request already covers /api/*), same CSRF check, and
    the same server-side field allow-list posture as the single-item
    /api/edit/<hash> route above — a client never gets to name an arbitrary
    field, and "tags" specifically is only ever reachable here in
    mode="add_tag" (union), never a full replace (see BULK_SET_FIELDS).

    Body: {"file_hashes": [...], "field": ..., "value": ..., "mode":
    "set"|"add_tag", "csrf_token": "..."}. `mode` defaults to "set".

    Every selected file_hash is applied independently (one apply_edit() or
    add_tag_bulk() call, one photo_edits audit row each, `edited_by` the
    real logged-in user per spec — not a synthetic batch-tool tag like the
    fully-automated date-audit tool uses) — a bad hash or an invalid value
    for one item is recorded in `failed` and does NOT abort the rest of the
    batch, so one typo/stale selection can't silently zero out an otherwise
    successful bulk action. Returns a summary ({updated, failed, total}) so
    the front end can show a clear "N updated, M failed" result rather than
    silently succeeding or failing (per spec)."""
    data = request.get_json(silent=True) or {}
    if data.get("csrf_token") != session.get("_csrf"):
        return jsonify({"error": "Your session looks stale — reload the page and try again."}), 403

    file_hashes = data.get("file_hashes")
    if not isinstance(file_hashes, list) or not file_hashes:
        return jsonify({"error": "No items selected."}), 400
    if len(file_hashes) > MAX_BULK_ITEMS:
        return jsonify({"error": f"Too many items selected at once (max {MAX_BULK_ITEMS})."}), 400

    field = data.get("field")
    mode = data.get("mode") or "set"
    value = data.get("value")
    if mode == "add_tag":
        if field != "tags":
            return jsonify({"error": "mode='add_tag' only applies to the tags field."}), 400
    elif mode == "set":
        if field not in BULK_SET_FIELDS:
            return jsonify({"error": f"{field!r} is not a bulk-editable field."}), 400
    else:
        return jsonify({"error": f"Unknown bulk-edit mode {mode!r}."}), 400

    edited_by = session.get("user") or "unknown"
    updated = 0
    failed: list[dict] = []
    write_conn = get_db_write()
    try:
        for file_hash in file_hashes:
            try:
                if mode == "add_tag":
                    add_tag_bulk(write_conn, file_hash, value, edited_by=edited_by, logger=_logger)
                else:
                    apply_edit(write_conn, file_hash, field, value, edited_by=edited_by, logger=_logger)
                updated += 1
            except ValueError as e:
                failed.append({"file_hash": file_hash, "error": str(e)})
    finally:
        write_conn.close()

    _captions_cache.refresh()
    return jsonify({"updated": updated, "failed": failed, "total": len(file_hashes)})


@app.route("/api/favorite/<file_hash>", methods=["POST"])
def api_favorite(file_hash: str):
    """Phase 3h: mark/unmark the photo as a favorite for the CURRENTLY
    LOGGED-IN user only — never touches another user's favorite status on
    this same photo (see src/favorites.py::set_favorite, one row per
    (file_hash, username)). Same auth gate (before_request already covers
    /api/*) and CSRF posture as /api/edit/<hash> and /api/bulk-edit above —
    a per-session token compared against the request body, checked before
    any write. Deliberately its own route/mechanism rather than another
    EDITABLE_FIELDS entry: favorites aren't a single photos-table column,
    and aren't part of the photo_edits audit trail (see src/favorites.py's
    module docstring for why that mechanism doesn't apply here).

    Body: {"favorited": true|false, "csrf_token": "..."}. Returns the
    freshly-updated photo (same shape /api/photos/.../api/nav items already
    have, now including favorited_by/favorited_by_me) so the front end can
    re-render from the response, same convention as /api/edit/<hash>."""
    data = request.get_json(silent=True) or {}
    if data.get("csrf_token") != session.get("_csrf"):
        return jsonify({"error": "Your session looks stale — reload the page and try again."}), 403
    username = session.get("user")
    if not username:
        return jsonify({"error": "Not logged in."}), 401
    favorited = bool(data.get("favorited"))

    write_conn = get_db_write()
    try:
        try:
            set_favorite(write_conn, file_hash, username, favorited)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    finally:
        write_conn.close()

    read_conn = get_db()
    try:
        row = read_conn.execute("SELECT * FROM photos WHERE file_hash = ?", (file_hash,)).fetchone()
        if row is None:
            return jsonify({"error": "Photo not found."}), 404
        item = _row_to_dict(row, read_conn)
    finally:
        read_conn.close()
    return jsonify({"item": item})


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
        # Many phones/cameras store pixel data in a fixed sensor orientation
        # and record the intended display rotation in the EXIF Orientation
        # tag rather than physically rotating pixels -- without this, those
        # photos render sideways/upside down here even though every other
        # app that reads the tag (Windows Photos, Google Photos, ...) shows
        # them correctly. exif_transpose() returns a new, already-rotated
        # in-memory image with the tag normalized to 1; it never touches the
        # file on disk (`_storage.open` is a read-only file handle, and
        # nothing here ever writes back to `path`), so the original photo's
        # bytes/EXIF are completely unaffected -- purely a serving-time
        # transform for this cropped/resized copy. A no-op (returns a copy
        # unchanged) for the common orientation=1 case.
        img = ImageOps.exif_transpose(img)
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


@app.route("/video/<file_hash>")
def serve_video(file_hash: str):
    """Streams the actual video file for the viewer's <video> element
    (Phase 2e) -- unlike serve_image, this is NOT re-encoded/downscaled:
    video files are already reasonably sized and there's no equivalent of
    PIL's cheap thumbnail() for video, so this just serves the original
    bytes. Looked up by file_hash, same as serve_image, so a stale/
    bookmarked URL can't read an arbitrary filesystem path.

    Uses send_file(..., conditional=True) so the browser can issue HTTP
    Range requests (needed for scrubbing/seeking, and for some browsers'
    initial-load behavior) -- that needs a real local path Werkzeug can
    stat() and seek(), which _storage.local_path() provides for today's
    only backend (LocalDiskStorage). A future non-local backend without a
    real path falls back to serving the whole file as one response (no
    range support) rather than failing outright -- video playback still
    works, just without scrubbing, until that backend adds proper range
    support of its own."""
    conn = get_db()
    try:
        row = conn.execute("SELECT current_path, filename FROM photos WHERE file_hash = ?", (file_hash,)).fetchone()
    finally:
        conn.close()
    if row is None:
        abort(404)
    path, filename = row["current_path"], row["filename"]
    if Path(filename or "").suffix.lower() not in _video_exts:
        abort(404)  # /video/* is for video rows only -- use /image/<hash> for photos
    if not _storage.exists(path):
        abort(404)
    mimetype = mimetypes.guess_type(filename or path)[0] or "video/mp4"
    local_path = _storage.local_path(path)
    if local_path is not None:
        return send_file(local_path, mimetype=mimetype, conditional=True)
    with _storage.open(path) as f:
        data = f.read()
    return Response(data, mimetype=mimetype)


@app.route("/thumbnail/<file_hash>")
def serve_thumbnail(file_hash: str):
    """Phase 2g: serves a cached video first-frame thumbnail, if one exists
    at cfg.thumbnail_dir_abs (see src/thumbnail_backfill.py). The grid only
    ever issues this request when `_row_to_dict`'s `has_thumbnail` field is
    true (static/review.js), so a 404 here should be rare in practice — but
    it's a real, deliberate fallback (not just an assumption the front end
    always gets it right first): review.js's `<img>` tag renders normally on
    a 404 (no onerror handler swaps in a placeholder mid-grid, per spec's
    "never show a broken image" — the front end simply never emits an <img>
    for a video without has_thumbnail in the first place, so a broken image
    never appears in the grid at all). Looked up by file_hash, same as
    serve_image/serve_video, so a stale/bookmarked URL can't read an
    arbitrary filesystem path — and unlike those two, no DB lookup is even
    needed: the cache file's existence at this deterministic path *is* the
    whole answer."""
    path = _thumbnail_path(file_hash)
    if not path.exists():
        abort(404)
    resp = send_file(path, mimetype="image/jpeg", conditional=True)
    # Safe to cache aggressively, same reasoning as /image/<hash>: file_hash
    # is content-addressed and this tool never re-generates a thumbnail for
    # an already-cached hash (see thumbnail_backfill.py's resumability).
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


def _open_browser_later(url: str) -> None:
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()


def main() -> None:
    global _cfg, _video_exts, _captions_cache, _storage, _logger

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
    _logger = logger

    # Real bug fixed here (URGENT session): this file used to never call
    # init_db() at all, on the theory that a read-only tool has no business
    # touching schema. But init_db()'s _migrate() is what actually applies a
    # pending ALTER TABLE/CREATE TABLE to an already-existing DB (see
    # src/db.py) -- main.py's commands and dashboard.py's panels all call it
    # before touching the DB themselves, but this file never did, so a real
    # 167k-row production DB last migrated before a schema-adding session
    # (e.g. Phase 2f's `excluded` column + `photo_edits` table) stayed
    # exactly as it was: restarting review_tool.py (rule 11) does NOT apply
    # a pending migration, since this was the only entry point that skipped
    # init_db() entirely. The result: _build_filters()'s unconditional
    # `excluded = 0` clause (and _row_to_dict()'s unconditional
    # manual_edit_fields() lookup against photo_edits) raised
    # sqlite3.OperationalError on every single request -- reproduced
    # directly against a real copy of the production DB predating this
    # column, not guessed. This one call, safe/idempotent like every other
    # entry point's, is the actual fix -- confirmed by reproducing the
    # failure without it and confirming it goes away with it (see TODO.md's
    # URGENT section). It uses its own normal read-write connection
    # (src.db.connect(), not get_db()'s mode=ro URI) for this one
    # startup-only step, before any request-handling route ever touches the
    # DB -- every route below is still exclusively mode=ro (or, for the one
    # Phase 2f edit route, get_db_write()), unchanged.
    init_db(_cfg.db_path_abs, logger=logger)

    register_auth(app, _cfg, logger=logger)
    if not _cfg.review_users:
        print("WARNING: no review_users configured in config.yaml — nobody can log in yet.")
        print("Add one with: venv\\Scripts\\python main.py review-user add <username>")

    port = args.port or _cfg.review_tool_port

    # Cheap warning, not a hard block (see dashboard.py's Remote Access
    # panel / CLAUDE.md for the real incident this guards against): if
    # something's already bound to this port, launching another instance
    # anyway is exactly how stale duplicate processes stack up. A genuine
    # restart-after-crash false positive (previous process still releasing
    # the socket) is possible but rare and harmless to just notice —
    # Flask's own bind attempt below will fail loudly if the port is
    # truly taken, so this is purely an early, clearer heads-up.
    existing_pids = listening_pids(port)
    if existing_pids:
        print(
            f"WARNING: {len(existing_pids)} process(es) already appear to be listening on "
            f"port {port} (PID(s): {', '.join(str(p) for p in existing_pids)}). If one of "
            "those predates a recent code change (e.g. an auth/security fix), it's still "
            "silently serving the OLD version — this exact pattern caused a real (if "
            "temporary) auth bypass before. Consider stopping the other instance(s) first "
            "(Task Manager, or PowerShell's Stop-Process -Id <pid> -Force)."
        )

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
