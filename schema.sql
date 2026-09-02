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
    date_taken            TEXT,                        -- resolved date, ISO 8601 (YYYY-MM-DD[THH:MM:SS])
    date_taken_year       INTEGER,                       -- denormalized for fast filtering
    date_taken_month      INTEGER,                        -- 1-12, denormalized
    date_source            TEXT NOT NULL,                   -- 'exif' | 'filename' | 'filesystem' | 'unsorted'
    status                  TEXT NOT NULL DEFAULT 'sorted',  -- 'sorted' | 'unsorted' | 'error'
    caption                  TEXT,                              -- Phase 2 output, NULL until then
    processed_at              TEXT NOT NULL,                     -- ISO timestamp, when Phase 1 recorded this row
    phase1_verified            INTEGER NOT NULL DEFAULT 0          -- 1 once copy-verify-delete succeeded (or file confirmed already in place)
);

CREATE INDEX IF NOT EXISTS idx_photos_date ON photos(date_taken_year, date_taken_month);
CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(status);

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
