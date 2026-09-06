"""Data Layer: loads Phase 2's captions.jsonl into the SQLite DB
(photos.caption, plus tags/photo_tags), so captions become visible via any
DB client -- including the user's MS Access ODBC link (see README.md /
CLAUDE.md's Data access notes).

Not part of Phase 2 itself -- see photo-organizer-spec.md's Data Layer
section ("a separate loader script ingests JSONL into SQLite tables").
Phase 2 only ever writes JSONL (CLAUDE.md rule 5); this is that separate,
one-way sync step, built at the user's request once they wanted to
actually see captions somewhere other than the raw JSONL file.

Idempotent -- safe to just re-run in full every time, no incremental
tracking needed:
- UPDATE photos SET caption=... WHERE file_hash=... -- same result every run
- INSERT OR IGNORE for tags/photo_tags -- duplicates are silent no-ops

A caption whose file_hash has no matching photos row (e.g. a file
captioned outside the normal Phase 1 -> Phase 2 flow, such as an ad hoc
test sample never scanned by a real Phase 1 run) is skipped and counted,
not synthesized into a fake photos row -- populating `photos` is Phase 1's
job, not this loader's (see CLAUDE.md rule 6: file_hash is the identity
across all tables, but a row still has to originate somewhere real).

Phase 2f: a manual edit in review_tool.py to a photo's caption or tags is
permanently protected from being silently overwritten by this loader. Each
record checks src.edits.has_manual_edit(conn, file_hash, "caption") /
"tags" independently BEFORE applying that half of the record -- caption and
tags are tracked (and can be skipped) separately, so an edit to one doesn't
block the other from still loading normally from an in-progress Phase 2
run. A skip is logged and counted (LoadStats.caption_skipped_manual_edit /
tags_skipped_manual_edit), never silent. Every non-edited row keeps
updating exactly as before -- this is a real behavior change to an
already-shipped module, so it was tested carefully for that (see
tests/test_phase2f.py).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .edits import has_manual_edit


@dataclass
class LoadStats:
    read: int = 0
    captions_loaded: int = 0       # photos.caption successfully updated
    skipped_no_photo_row: int = 0  # file_hash not found in photos (e.g. an ad hoc test sample)
    skipped_bad_line: int = 0      # malformed JSON / missing file_hash
    tags_linked: int = 0           # new photo_tags rows inserted (re-run no-ops via tags OR IGNORE aren't counted)
    caption_skipped_manual_edit: int = 0  # Phase 2f: caption NOT overwritten -- a human edited it in review_tool.py
    tags_skipped_manual_edit: int = 0     # Phase 2f: tags NOT re-linked -- a human edited them in review_tool.py

    def summary(self) -> str:
        return (
            f"read={self.read} captions_loaded={self.captions_loaded} "
            f"tags_linked={self.tags_linked} "
            f"skipped_no_photo_row={self.skipped_no_photo_row} "
            f"skipped_bad_line={self.skipped_bad_line} "
            f"caption_skipped_manual_edit={self.caption_skipped_manual_edit} "
            f"tags_skipped_manual_edit={self.tags_skipped_manual_edit}"
        )


def load_captions(captions_path: Path, conn: sqlite3.Connection, logger: logging.Logger,
                   commit_every: int = 500) -> LoadStats:
    """Reads captions_path line by line and applies each record to the DB.
    Commits every commit_every records (bounds how much a crash could
    re-do, not that re-doing it is unsafe either way -- see module
    docstring on idempotency)."""
    stats = LoadStats()
    if not captions_path.exists():
        logger.warning(f"No captions file at {captions_path} -- nothing to load.")
        return stats

    since_commit = 0
    with open(captions_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            stats.read += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                stats.skipped_bad_line += 1
                logger.warning(f"Skipping malformed line {line_no} in {captions_path}: {e}")
                continue

            file_hash = rec.get("file_hash")
            if not file_hash:
                stats.skipped_bad_line += 1
                logger.warning(f"Skipping line {line_no} in {captions_path}: missing file_hash")
                continue

            # Phase 2f: a photo row has to exist before either half below
            # applies to it -- checked once up front (rather than relying on
            # the caption UPDATE's rowcount, as before) so the manual-edit
            # check for tags below doesn't need its own separate existence
            # check too.
            if conn.execute("SELECT 1 FROM photos WHERE file_hash = ? LIMIT 1", (file_hash,)).fetchone() is None:
                stats.skipped_no_photo_row += 1
                continue

            if has_manual_edit(conn, file_hash, "caption"):
                stats.caption_skipped_manual_edit += 1
                logger.info(f"SKIP caption load hash={file_hash[:12]} (manually edited in review_tool.py)")
            else:
                conn.execute(
                    "UPDATE photos SET caption = ? WHERE file_hash = ?",
                    (rec.get("caption"), file_hash),
                )
                stats.captions_loaded += 1

            if has_manual_edit(conn, file_hash, "tags"):
                stats.tags_skipped_manual_edit += 1
                logger.info(f"SKIP tags load hash={file_hash[:12]} (manually edited in review_tool.py)")
            else:
                for tag_name in rec.get("tags") or []:
                    if not tag_name:
                        continue
                    conn.execute("INSERT OR IGNORE INTO tags (tag_name) VALUES (?)", (tag_name,))
                    tag_row = conn.execute("SELECT tag_id FROM tags WHERE tag_name = ?", (tag_name,)).fetchone()
                    if tag_row is None:
                        continue  # shouldn't happen right after the insert above, but don't let one bad tag kill the load
                    link_cur = conn.execute(
                        "INSERT OR IGNORE INTO photo_tags (file_hash, tag_id) VALUES (?, ?)",
                        (file_hash, tag_row[0]),
                    )
                    if link_cur.rowcount:
                        stats.tags_linked += 1

            since_commit += 1
            if since_commit >= commit_every:
                conn.commit()
                since_commit = 0

    conn.commit()
    logger.info(f"Caption load complete: {stats.summary()}")
    return stats
