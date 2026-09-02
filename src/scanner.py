"""Recursive file scanner: walks one or more source folders (which may
include dest_root itself — see organize.py for why) and yields paths whose
extension matches the configured supported list.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Iterator

# Directories never worth descending into even if they end up inside a
# source folder (version control, editor/OS cruft).
_SKIP_DIR_NAMES = {".git", "__pycache__", ".venv", "venv", "$RECYCLE.BIN", "System Volume Information"}


def scan_folders(folders: Iterable[Path], extensions: set[str]) -> Iterator[Path]:
    seen_roots: set[Path] = set()
    for folder in folders:
        folder = Path(folder).resolve()
        if not folder.exists():
            continue
        if folder in seen_roots:
            continue  # duplicate/overlapping source folder entries in config
        seen_roots.add(folder)
        yield from _walk(folder, extensions)


def _walk(root: Path, extensions: set[str]) -> Iterator[Path]:
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir():
                if entry.name in _SKIP_DIR_NAMES:
                    continue
                yield from _walk(entry, extensions)
            elif entry.is_file():
                if entry.suffix.lower() in extensions:
                    yield entry
        except OSError:
            continue  # permission error, broken symlink, etc. — skip, don't crash the scan
