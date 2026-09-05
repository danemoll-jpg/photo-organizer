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
    gps_checked               INTEGER NOT NULL DEFAULT 0           -- 1 once GPS extraction has been attempted for this row, whether or
                                                                     -- not GPS data was actually found — lets extraction resume without
                                                                     -- ever re-reading the same file's EXIF twice (CLAUDE.md rule 1)
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
    location_name, gps_checked
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

-- Phase 3: one row per detected face.
CREATE TABLE IF NOT EXISTS faces (
    face_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash      TEXT NOT NULL REFERENCES photos(file_hash) ON DELETE CASCADE,
    bbox_x          INTEGER NOT NULL,
    bbox_y          INTEGER NOT NULL,
    bbox_w          INTEGER NOT NULL,
    bbox_h          INTEGER NOT NULL,
    embedding        BLOB NOT NULL,                                    -- serialized float vector
    cluster_id        INTEGER,                                           -- NULL until clustering runs; FK-ish to people.cluster_id
    detected_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_faces_file_hash ON faces(file_hash);
CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);

-- Phase 3: human-assigned identity per cluster, separate from face detection
-- output so relabeling/merging/splitting never requires re-running detection.
CREATE TABLE IF NOT EXISTS people (
    cluster_id       INTEGER PRIMARY KEY,
    assigned_name     TEXT,
    notes              TEXT
);
