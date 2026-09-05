"""Date resolution chain for Phase 1b video files. Mirrors date_resolver.py's
shape exactly, swapping the first step (EXIF is photo-only — video's taken-date
lives in the container's own metadata instead):

  1. Container creation-date metadata (via hachoir — reads the MP4/MOV 'mvhd'
     atom or AVI header directly; no ffmpeg/MediaInfo binary needed)
  2. Filename pattern (same patterns as photos — VID_..., PXL_..., etc.;
     reused as-is from date_resolver.py, not reimplemented)
  3. File system created/modified date (least reliable — flag these; reused
     as-is from date_resolver.py)
  4. If nothing usable: 'unsorted', same as photos

Returns (datetime | None, source_str) where source_str is one of
'container' | 'filename' | 'filesystem' | 'unsorted'.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

# Reuse the filename-pattern and filesystem-date steps as-is — they're
# already format-agnostic (a Path is a Path) and already tested for photos.
# Not part of date_resolver's public surface (leading underscore), but this
# is an internal sibling module within the same package, not a reimplementation.
from .date_resolver import _from_filename, _from_filesystem, _valid

try:
    from hachoir.core import config as _hachoir_config
    from hachoir.core.log import log as _hachoir_log
    from hachoir.metadata import extractMetadata
    from hachoir.parser import createParser

    # hachoir logs its own parse warnings/errors (e.g. "Unknown MOV file
    # type" on a corrupt/truncated/unusual video — routine at this library's
    # scale) by writing straight to sys.stdout/sys.stderr. Under pythonw.exe
    # (the dashboard's launcher — see Launch Dashboard.bat / CLAUDE.md) those
    # are None, and hachoir's Log.newMessage() does an unconditional
    # `sys.stdout.flush()` before writing — the exact same crash class as the
    # tqdm/pythonw bug this project already hit once (see CLAUDE.md, TODO.md).
    # `quiet` alone doesn't cover it (still lets LOG_ERROR through to that same
    # write), so disable the print path outright; we log everything ourselves
    # anyway (CLAUDE.md rule 3 — one logging path, not a second parallel one).
    _hachoir_config.quiet = True
    _hachoir_log.use_print = False
    _HACHOIR_AVAILABLE = True
except ImportError:
    _HACHOIR_AVAILABLE = False


def _from_container(path: Path) -> datetime | None:
    if not _HACHOIR_AVAILABLE:
        return None
    try:
        parser = createParser(str(path))
        if parser is None:
            return None  # unrecognized/corrupt container — not an error, just no metadata
        with parser:
            metadata = extractMetadata(parser)
        if metadata is None:
            return None
        for key in ("creation_date", "last_modification"):
            if not metadata.has(key):
                continue
            raw = metadata.get(key)
            if isinstance(raw, datetime) and _valid(raw):
                return raw
    except Exception:
        return None  # unreadable/corrupt/unsupported container variant, etc. — same posture as _from_exif
    return None


def resolve_video_date(path: Path) -> tuple[datetime | None, str]:
    dt = _from_container(path)
    if dt:
        return dt, "container"
    dt = _from_filename(path)
    if dt:
        return dt, "filename"
    dt = _from_filesystem(path)
    if dt:
        return dt, "filesystem"
    return None, "unsorted"
