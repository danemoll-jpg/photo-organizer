"""Content-hash utilities. Every table's primary key is a file's content
hash, not its path — paths move during Phase 1, and duplicates/re-exports
are expected across 25 years of photos.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_SIZE = 1024 * 1024  # 1 MB


def hash_file(path: Path, algorithm: str = "sha256") -> str:
    """Return the hex digest of a file's contents, streamed in chunks so
    large files don't get fully loaded into memory."""
    h = hashlib.new(algorithm)
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()
