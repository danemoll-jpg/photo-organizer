"""Phase 2d storage-abstraction groundwork: where photo bytes actually
live, decoupled from review_tool.py's own logic. Local disk is the only
implemented backend today — this is groundwork for a *future* cloud-
storage swap (S3, Backblaze B2, etc.), not that swap itself (explicitly
out of scope for this session — see photo-organizer-spec.md's Phase 2d).

Scoped to review_tool.py only, per spec: Phase 1/1b/2 keep reading/writing
local paths directly (organize.py, caption.py, gps_backfill.py) — this
doesn't touch them, and isn't meant to yet.

The interface is deliberately small: everywhere in review_tool.py that
used to do `Path(current_path).exists()` / `Image.open(path)` now goes
through a PhotoStorage instead, keyed by the same `current_path` string
the DB already stores. A future backend (e.g. S3ObjectStorage) would
resolve that same string to a bucket key and stream bytes from there —
review_tool.py's own code wouldn't need to change, only get_storage()'s
backend selection and a new class here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO


class PhotoStorage(ABC):
    @abstractmethod
    def exists(self, path: str) -> bool:
        """Whether the photo at this path is currently readable."""

    @abstractmethod
    def open(self, path: str) -> BinaryIO:
        """A binary, seekable, readable file-like object for the photo at
        this path — used as `with storage.open(path) as f: Image.open(f)`.
        Caller is responsible for closing it (via the `with` block)."""

    def local_path(self, path: str) -> str | None:
        """A real local filesystem path for this item, if the backend has
        one — None otherwise. Added for Phase 2e's video playback: Flask's
        `send_file(..., conditional=True)` needs a real path (or file
        descriptor it can `stat()`) to serve HTTP range requests, which
        video scrubbing depends on — unlike photo serving, which already
        decodes the whole image into memory via `open()` regardless, so it
        never needed this. Every backend still works through `open()`/
        `exists()` for everything else; this is only consulted as an
        optimization for range-capable streaming, with a full-read fallback
        when it returns None (see review_tool.py's video route). The
        default (used by any future non-local backend that doesn't
        override this) is None."""
        return None


class LocalDiskStorage(PhotoStorage):
    """Today's only backend: `path` is a real local filesystem path, same
    as `photos.current_path` in the DB has always held."""

    def exists(self, path: str) -> bool:
        try:
            return Path(path).exists()
        except OSError:
            return False

    def open(self, path: str) -> BinaryIO:
        return open(path, "rb")

    def local_path(self, path: str) -> str | None:
        return path


def get_storage(cfg) -> PhotoStorage:
    backend = getattr(cfg, "storage_backend", "local")
    if backend == "local":
        return LocalDiskStorage()
    raise ValueError(
        f"Unknown storage_backend {backend!r} in config.yaml — only 'local' is "
        "implemented today (see src/storage.py's module docstring; a cloud "
        "backend is groundwork-only, not yet built)."
    )
