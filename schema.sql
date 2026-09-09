-- Photo Organizer — SQLite schema
-- Full schema defined up front (Phase 0) so later phases (2/3) never require
-- a migration. Only `photos` is populated during Phase 1; tags/faces/people
-- stay empty until Phase 2/3 land.
--
-- Primary key convention: file content hash (sha256 hex digest), not path.
-- Paths move during Phase 1, and duplicates/re-exports are likely across
-- 25 years of photos, so hash is the only stable identity.

PRAGMA foreign_keys = ON;

-- One row per unique photo (by content hash). Populated by Phase 1.
CREATE TABLE IF NOT EXISTS photos (
    file_hash           TEXT PRIMARY KEY,       -- sha256 hex digest of file bytes
    current_path        TEXT NOT NULL,           -- absolute path after Phase 1 sort
    original_path       TEXT NOT NULL,            -- path where this file was first seen
    filename             TEXT NOT NULL,             -- final filename (post collision handling)
    file_size            INTEGER NOT NULL,           -- bytes
    file_mtime           REAL,                        -- st_mtime at time of recording; NULL for pre-migration rows.
                                                        -- Fast-path pre-check: path+size+mtime match => skip full
                                                        -- hash on rescan. NULL/mismatch => fall back to full hash.
    date_taken            TEXT,                        -- resolved date, ISO 8601 (YYYY-MM-DD[THH:MM:SS])
    date_taken_year       INTEGER,                       -- denormalized for fast filtering
    date_taken_month      INTEGER,                        -- 1-12, denormalized
    date_source            TEXT NOT NULL,                   -- 'exif' | 'container' | 'filename' | 'filesystem' | 'unsorted'
                                                             -- ('container' = Phase 1b video container metadata, exif's video equivalent)
    status                  TEXT NOT NULL DEFAULT 'sorted',  -- 'sorted' | 'unsorted' | 'error'
    caption                  TEXT,                              -- Phase 2 output, NULL until then
    processed_at              TEXT NOT NULL,                     -- ISO timestamp, when Phase 1 recorded this row
    phase1_verified            INTEGER NOT NULL DEFAULT 0,         -- 1 once copy-verify-delete succeeded (or file confirmed already in place)
    -- Phase 2b: GPS extraction + offline reverse geocoding (src/gps_resolver.py,
    -- src/gps_backfill.py). Populated by its own standalone step (`main.py
    -- extract-gps`), not by Phase 1/1b/2 — organize.py/caption.py are untouched.
    gps_lat                   REAL,                                -- decimal degrees, NULL if no GPS EXIF found (or not yet checked)
    gps_lon                   REAL,                                -- decimal degrees
    location_name             TEXT,                                -- offline reverse-geocoded place name, e.g. "Marietta, GA"
    gps_checked               INTEGER NOT NULL DEFAULT 0,          -- 1 once GPS extraction has been attempted for this row, whether or
                                                                     -- not GPS data was actually found — lets extraction resume without
                                                                     -- ever re-reading the same file's EXIF twice (CLAUDE.md rule 1)
    -- Phase 2f: "Hide from library" — NOT a physical file delete, NOT a DB
    -- row delete either (see photo-organizer-spec.md's Phase 2f section for
    -- why both are unsafe here). 1 = permanently excluded from every
    -- review_tool.py view (grid/viewer/random/slideshow/stats) until
    -- reversed via the same "Show hidden" + un-hide mechanism. Phase 1/1b's
    -- rescan matching (by current_path/original_path/hash) never looks at
    -- this column, so the hide is safe from a future rescan silently
    -- resurrecting the row. Edited the same way as any other field below —
    -- see photo_edits, field_name='excluded'.
    excluded                  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_photos_date ON photos(date_taken_year, date_taken_month);
CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(status);
-- Support the path+size+mtime fast-path pre-check (see organize.py) without
-- a table scan: rescans look a file up by its current path first (covers
-- dest_root re-scans and unmoved already-in-place files), falling back to
-- original_path for the rare case a copy succeeded but the source delete
-- didn't (see organize.py module docstring).
CREATE INDEX IF NOT EXISTS idx_photos_current_path ON photos(current_path);
CREATE INDEX IF NOT EXISTS idx_photos_original_path ON photos(original_path);
-- review_tool.py's grid/viewer walk photos in chronological (date_taken)
-- order, not current_path order (see review_tool.py's module docstring and
-- _fetch_page/_scan_matches/etc.). date_taken is ISO 8601 TEXT, so it sorts
-- correctly as a plain string comparison; COALESCE(...) below pushes an
-- undated/unsorted row to sort LAST rather than SQLite's own
-- ASC-sorts-NULLs-first default (grouped at the end of a chronological
-- grid, not scattered randomly through it). file_hash is the tiebreaker for
-- rows sharing an identical date_taken (common for burst photos) — the two
-- columns together give a fully deterministic total order, which correct
-- keyset (cursor) pagination depends on. An EXPRESSION index: the query
-- text in review_tool.py must use this exact same COALESCE(...) expression
-- for SQLite's planner to actually use it instead of a full table scan.
CREATE INDEX IF NOT EXISTS idx_photos_date_taken_sort
    ON photos(COALESCE(date_taken, '9999-99-99T23:59:59'), file_hash);

-- Read-only view for the user's MS Access ODBC link (see README.md /
-- CLAUDE.md's Data access notes — Access as an optional query front-end,
-- for the user's own use). Access showed every row of the linked `photos`
-- table as "#Deleted": the SQLite ODBC driver doesn't reliably report
-- file_hash's PRIMARY KEY to Access as a usable unique row identifier, so
-- Access falls back to matching a row by comparing every column's value —
-- and file_mtime (REAL/floating point) doesn't round-trip byte-for-byte
-- through ODBC, so that match silently fails on every row. Casting it to
-- TEXT here sidesteps the float comparison entirely. Purely additive (a
-- view, not a copy) and always live — link this instead of `photos`
-- directly for Access browsing.
-- (gps_lat/gps_lon are also REAL and would hit the exact same #Deleted issue
-- once populated by Phase 2b's extraction step — cast the same way.)
CREATE VIEW IF NOT EXISTS photos_access AS
SELECT
    file_hash, current_path, original_path, filename, file_size,
    date_taken, date_taken_year, date_taken_month, date_source,
    status, caption, processed_at, phase1_verified,
    CAST(file_mtime AS TEXT) AS file_mtime,
    CAST(gps_lat AS TEXT) AS gps_lat, CAST(gps_lon AS TEXT) AS gps_lon,
    location_name, gps_checked, excluded
FROM photos;

-- Read-only view for browsing captions/tags together in Access, one row
-- per captioned photo (only photos with a caption -- the whole point is
-- to see what Phase 2 has produced so far, not the 165k+ not-yet-
-- captioned rows too). Tags are flattened into one comma-separated
-- column rather than linking the tags/photo_tags join tables directly,
-- for two reasons: (1) it's simpler to browse as a single flat table in
-- Access, no manual join needed there, and (2) photo_tags.confidence is
-- also a REAL column and would very likely hit the exact same #Deleted
-- issue file_mtime did on photos -- sidestepped entirely by not linking
-- that table directly. Purely additive (a view, not a copy) and always
-- live. Link this instead of tags/photo_tags for Access browsing.
CREATE VIEW IF NOT EXISTS captions_access AS
SELECT
    p.file_hash, p.filename, p.current_path,
    p.date_taken, p.date_taken_year, p.date_taken_month,
    p.caption,
    (SELECT GROUP_CONCAT(t.tag_name, ', ')
     FROM photo_tags pt JOIN tags t ON t.tag_id = pt.tag_id
     WHERE pt.file_hash = p.file_hash) AS tags,
    p.location_name  -- Phase 2b, NULL until extract-gps has run for this row
FROM photos p
WHERE p.caption IS NOT NULL;

-- Phase 2: tag vocabulary.
CREATE TABLE IF NOT EXISTS tags (
    tag_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_name    TEXT NOT NULL UNIQUE
);

-- Phase 2: many-to-many photos <-> tags.
CREATE TABLE IF NOT EXISTS photo_tags (
    file_hash    TEXT NOT NULL REFERENCES photos(file_hash) ON DELETE CASCADE,
    tag_id        INTEGER NOT NULL REFERENCES tags(tag_id) ON DELETE CASCADE,
    confidence     REAL,                                             -- model confidence, if applicable
    PRIMARY KEY (file_hash, tag_id)
);

-- Phase 3: one row per detected face. Loaded from data/faces.jsonl by
-- src/face_clustering.py::load_faces_from_jsonl (main.py cluster-faces /
-- the dashboard's Face Clustering panel) -- face_detection.py (Session 1)
-- itself never writes here, only to that JSONL file.
CREATE TABLE IF NOT EXISTS faces (
    face_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash      TEXT NOT NULL REFERENCES photos(file_hash) ON DELETE CASCADE,
    bbox_x          INTEGER NOT NULL,
    bbox_y          INTEGER NOT NULL,
    bbox_w          INTEGER NOT NULL,
    bbox_h          INTEGER NOT NULL,
    embedding        BLOB NOT NULL,                                    -- 512-dim ArcFace vector, float32.tobytes() (see src/face_clustering.py::vec_to_blob/blob_to_vec)
    det_score         REAL,                                              -- RetinaFace detection confidence (0-1), NULL for a pre-Session-2-schema row loaded without it
    cluster_id        INTEGER REFERENCES people(cluster_id) ON DELETE SET NULL, -- NULL until clustered (or permanently unclustered -- see face_min_det_score/min_samples)
    -- Phase 3c: reversible false-positive flag (same non-destructive
    -- philosophy as photos.excluded / "Hide from library" -- a flag, never
    -- a row delete). 1 = a human confirmed this detection isn't actually a
    -- face (a pattern/object/pet misdetected). Set via
    -- src/face_clustering.py::mark_faces_not_a_person(), which also clears
    -- cluster_id (pulling it out of whatever group it was in, same
    -- mechanical effect as a split). run_face_clustering()'s candidate
    -- query excludes not_a_person=1 rows so a future cluster-faces run
    -- never re-adds one to a group. Reversible via
    -- unmark_faces_not_a_person() -- see that function's docstring for why
    -- un-marking doesn't try to restore the old cluster_id.
    not_a_person      INTEGER NOT NULL DEFAULT 0,
    detected_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_faces_file_hash ON faces(file_hash);
CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);
-- Idempotent load key: one row per actual detected face. A face's bbox is
-- deterministic for a given (unchanged) photo, so (file_hash, bbox_*) is a
-- stable natural key -- load_faces_from_jsonl uses INSERT OR IGNORE against
-- this so re-running `cluster-faces` after face_detection.py appends more
-- lines to faces.jsonl (e.g. a resumed Session-1 run) never double-inserts
-- an already-loaded face.
CREATE UNIQUE INDEX IF NOT EXISTS idx_faces_unique_detection
    ON faces(file_hash, bbox_x, bbox_y, bbox_w, bbox_h);

-- Phase 3: human-assigned identity per cluster, separate from face detection
-- output so relabeling/merging/splitting never requires re-running detection.
-- cluster_id is a STABLE person identity, not a raw re-clustering-run label
-- -- see src/face_clustering.py's module docstring for the full re-cluster-
-- without-destroying-names design (representative_embedding is the key to
-- that: a running centroid a future clustering run matches new faces
-- against, so an existing person's cluster_id/assigned_name is reused
-- rather than a fresh cluster_id being minted and orphaning the old name).
CREATE TABLE IF NOT EXISTS people (
    cluster_id             INTEGER PRIMARY KEY,
    assigned_name           TEXT,
    notes                    TEXT,
    representative_embedding BLOB,                 -- running mean of every currently-assigned face's embedding, L2-renormalized (float32.tobytes())
    face_count               INTEGER NOT NULL DEFAULT 0,  -- denormalized count of faces.cluster_id = this row's cluster_id -- kept in sync by every mutating operation in src/face_clustering.py, never trust it blindly elsewhere without re-deriving if that invariant is ever in doubt
    updated_at                TEXT,                 -- ISO 8601, last time membership/centroid changed (name-only edits don't bump this)
    -- Follow-up session, direct user request: real incident naming pet
    -- clusters (cats) after seeing them attract "all kinds of crap" once
    -- face_match_existing_threshold made matching against EXISTING people
    -- more lenient -- a real, foreseeable risk that's much worse for a
    -- non-human face embedding (ArcFace was never trained on cats, so
    -- their centroid is far less discriminative and attracts more false
    -- positives at a looser threshold than a real human's does). 1 = this
    -- person's centroid is skipped ENTIRELY by run_face_clustering()'s
    -- Step 1 "match against existing people" pass (src/face_clustering.py)
    -- -- never a candidate for automatic assignment again, no matter how
    -- similar a new face looks. Does NOT affect manual assignment in the
    -- labeling UI (Move/Merge Selected Faces Here) -- the user's own
    -- words were "lock those off to automatic matching and just selecting
    -- them myself," so a locked person must stay a normal, selectable
    -- Move/Merge target. Reversible, same philosophy as faces.not_a_person/
    -- photos.excluded -- never a delete.
    auto_match_locked        INTEGER NOT NULL DEFAULT 0
);

-- Phase 2f: manual-edit audit trail for review_tool.py's new edit endpoints.
-- Append-only (never updated or deleted) -- one row per edit, not one row
-- per (file_hash, field_name). This is the actual protection mechanism
-- rule 11-adjacent automated writers must respect: `main.py load-captions`
-- and src/gps_backfill.py both check "does ANY row exist here for this
-- (file_hash, field_name)?" (see src/edits.py::has_manual_edit) before
-- applying an automated write to caption/tags/location, and skip (logging
-- it) if one does -- permanently, since a manual edit is never expired or
-- cleared automatically. Deliberately a table, not one boolean column per
-- field on `photos`, so it also gives a real audit trail (who changed what,
-- when, from what) across multiple invited editors (Phase 2d: every
-- authenticated user can edit). field_name is one of src/edits.py's
-- EDITABLE_FIELDS ('caption' | 'tags' | 'location' | 'date_taken' |
-- 'excluded') -- "excluded" (Hide from library) uses this exact same
-- mechanism, it's just another field name here, not a separate table/flag.
CREATE TABLE IF NOT EXISTS photo_edits (
    edit_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash    TEXT NOT NULL REFERENCES photos(file_hash) ON DELETE CASCADE,
    field_name   TEXT NOT NULL,
    old_value    TEXT,             -- prior value (NULL if there was none), stringified
    new_value    TEXT,             -- the value just written, stringified
    edited_by    TEXT NOT NULL,    -- config.yaml review_users username that made this edit
    edited_at    TEXT NOT NULL     -- ISO 8601 UTC timestamp
);

-- Supports has_manual_edit()'s per-(file_hash, field_name) existence check
-- without a table scan, and _row_to_dict()'s "which fields on this one row
-- were ever manually edited" lookup (both in review_tool.py/src/edits.py).
CREATE INDEX IF NOT EXISTS idx_photo_edits_hash_field ON photo_edits(file_hash, field_name);

-- Phase 3h: per-user favorites for review_tool.py. One row per (photo,
-- user) who favorited it -- PK on (file_hash, username) is what makes
-- favorites independent per invited person (Phase 2d: every review_user
-- gets their own account) rather than one shared boolean: two different
-- users can each favorite (or not) the same photo without touching each
-- other's mark, and un-favoriting is a plain DELETE of just that one row
-- (see src/favorites.py::set_favorite), never affecting anyone else's.
-- A brand-new table, not a column added to an existing one, so this needs
-- no ALTER TABLE in src/db.py::_migrate() -- CREATE TABLE IF NOT EXISTS
-- here is already applied on every init_db() call, same as how
-- photo_edits/faces/people were introduced.
-- Deliberately NOT part of the photo_edits audit mechanism: that table
-- protects a field from a future AUTOMATED overwrite (caption/tags/
-- location/date_taken/excluded), and nothing in this project ever
-- automates favoriting a photo, so there's nothing to protect against.
CREATE TABLE IF NOT EXISTS favorites (
    file_hash     TEXT NOT NULL REFERENCES photos(file_hash) ON DELETE CASCADE,
    username      TEXT NOT NULL,   -- config.yaml review_users username, not a foreign key (users aren't a DB table -- see src/config.py)
    favorited_at  TEXT NOT NULL,   -- ISO 8601 UTC timestamp
    PRIMARY KEY (file_hash, username)
);

-- Supports both the "who favorited this specific photo" viewer lookup
-- (src/favorites.py::favorites_for_hashes, batched one query per grid page
-- -- see review_tool.py's _fetch_page) and the favorite_by grid/viewer
-- filter's "does this file_hash have a favorites row [for this user]"
-- subquery in _build_filters -- both are satisfied by the PK's own
-- leading column, but an explicit index keeps that intent documented and
-- costs nothing extra.
CREATE INDEX IF NOT EXISTS idx_favorites_file_hash ON favorites(file_hash);
-- Supports the favorite_by filter's per-username lookup (a specific named
-- review_user, or "__me__" resolved server-side to the current session's
-- username) without a table scan.
CREATE INDEX IF NOT EXISTS idx_favorites_username ON favorites(username);

-- Phase 3i: manual person tagging for review_tool.py -- a narrow, deliberate
-- exception to "who's in this photo" being decided solely by face
-- detection/clustering (the faces/people join Phase 3e already displays),
-- for the two cases that join can never cover by itself: a person
-- face_detection.py never actually detected a face for (occluded, back
-- turned, low quality, or a photo predating Phase 3 entirely), and a false
-- match the automatic join shows but shouldn't (the real fix for a
-- misclustered FACE is the Tkinter FaceLabelingWindow's Remove Selected
-- from Cluster/Split, which this does NOT replace -- but a web-UI user
-- reviewing day-to-day needs a quick per-PHOTO "no, not them" too, without
-- touching the underlying faces.cluster_id data those tools own).
--
-- Deliberately layered ON TOP of the automatic faces/people join, never
-- writing to faces.cluster_id/not_a_person or people.face_count/
-- representative_embedding -- review_tool.py::_people_for_hashes computes
-- (automatic people) UNION (action='add' rows) MINUS (action='remove'
-- rows). One row per (photo, person): action is 'add' or 'remove', and
-- src/people_tags.py's add/remove functions keep this table as small as
-- possible by only ever inserting a row when it CONTRADICTS what the
-- automatic join alone would already say -- e.g. removing a manually-added
-- (never automatically-detected) person deletes the 'add' row outright
-- rather than writing a redundant 'remove' override for a fact that was
-- never automatically true in the first place.
-- A brand-new table -- no ALTER TABLE needed in src/db.py::_migrate(),
-- same as favorites above.
CREATE TABLE IF NOT EXISTS photo_people_overrides (
    file_hash   TEXT NOT NULL REFERENCES photos(file_hash) ON DELETE CASCADE,
    cluster_id  INTEGER NOT NULL REFERENCES people(cluster_id) ON DELETE CASCADE,
    action      TEXT NOT NULL CHECK (action IN ('add', 'remove')),
    edited_by   TEXT NOT NULL,   -- config.yaml review_users username that made this edit
    edited_at   TEXT NOT NULL,   -- ISO 8601 UTC timestamp
    PRIMARY KEY (file_hash, cluster_id)
);

-- Supports review_tool.py::_people_for_hashes' batched per-page override
-- lookup (src/people_tags.py::overrides_for_hashes) without a table scan --
-- same "one query per page, not one per row" discipline as
-- idx_favorites_file_hash above.
CREATE INDEX IF NOT EXISTS idx_photo_people_overrides_file_hash ON photo_people_overrides(file_hash);
