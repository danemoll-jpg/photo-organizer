"""Date resolution chain for Phase 1, per spec:

  1. EXIF DateTimeOriginal (fallback DateTimeDigitized)
  2. Filename pattern (IMG_20180304_..., Screenshot_20200101-..., etc.)
  3. Filesystem date (earlier of mtime/ctime) — least reliable, flagged
  4. Nothing usable -> caller routes to _unsorted/needs_review

Returns (datetime | None, source_str) where source_str is one of
'exif' | 'filename' | 'filesystem' | 'unsorted'.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from PIL import Image
from PIL.ExifTags import IFD

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass  # HEIC files will just fail to open; handled as a per-file error upstream

EXIF_DATETIME_ORIGINAL = 36867
EXIF_DATETIME_DIGITIZED = 36868

MIN_YEAR = 1990
MAX_YEAR = datetime.now().year + 1


def _valid(dt: datetime) -> bool:
    return MIN_YEAR <= dt.year <= MAX_YEAR


def _from_exif(path: Path) -> datetime | None:
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            exif_ifd = exif.get_ifd(IFD.Exif)
            for tag in (EXIF_DATETIME_ORIGINAL, EXIF_DATETIME_DIGITIZED):
                raw = exif_ifd.get(tag)
                if not raw:
                    continue
                try:
                    dt = datetime.strptime(raw.strip(), "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    continue
                if _valid(dt):
                    return dt
    except Exception:
        return None  # unreadable/corrupt image, truncated file, unsupported variant, etc.
    return None


# Filename date patterns, tried in order. Each yields (year, month, day, hour, min, sec)
# with time fields optional (default to 0).
_FILENAME_PATTERNS = [
    # IMG_20180304_120000, IMG-20180304-WA0001, VID_20180304_120000, PXL_20210101_123456789
    re.compile(r"(?:IMG|VID|PXL|PANO)[_-](\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})", re.I),
    re.compile(r"(?:IMG|VID|PXL|PANO)[_-](\d{4})(\d{2})(\d{2})", re.I),
    # Screenshot_20200101-123456, Screenshot 2020-01-01
    re.compile(r"Screenshot[_ ](\d{4})-?(\d{2})-?(\d{2})[-_ ](\d{2})(\d{2})(\d{2})", re.I),
    # WhatsApp Image 2020-01-01 at 12.00.00, generic YYYY-MM-DD
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
    # generic YYYYMMDD_HHMMSS anywhere
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?!\d)"),
    # generic bare YYYYMMDD anywhere (checked last — most prone to false positives)
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"),
]


def _from_filename(path: Path) -> datetime | None:
    name = path.name
    for pattern in _FILENAME_PATTERNS:
        m = pattern.search(name)
        if not m:
            continue
        groups = m.groups()
        year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
        hour = int(groups[3]) if len(groups) > 3 and groups[3] else 0
        minute = int(groups[4]) if len(groups) > 4 and groups[4] else 0
        second = int(groups[5]) if len(groups) > 5 and groups[5] else 0
        try:
            dt = datetime(year, month, day, hour, minute, second)
        except ValueError:
            continue  # e.g. month=13, matched digits that weren't actually a date
        if _valid(dt):
            return dt
    return None


def _from_filesystem(path: Path) -> datetime | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    # mtime and ctime (Windows: creation time) can each be wrong in different
    # ways after copies/re-exports — take the earlier of the two as the best
    # available guess, per spec ("least reliable — flag these").
    candidates = [stat.st_mtime]
    if hasattr(stat, "st_ctime"):
        candidates.append(stat.st_ctime)
    try:
        dt = datetime.fromtimestamp(min(candidates))
    except (OSError, OverflowError, ValueError):
        return None
    return dt if _valid(dt) else None


def resolve_date(path: Path) -> tuple[datetime | None, str]:
    dt = _from_exif(path)
    if dt:
        return dt, "exif"
    dt = _from_filename(path)
    if dt:
        return dt, "filename"
    dt = _from_filesystem(path)
    if dt:
        return dt, "filesystem"
    return None, "unsorted"
