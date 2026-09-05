"""Phase 2b: GPS extraction + offline reverse geocoding.

Two independent steps, both used by src/gps_backfill.py:

  1. extract_gps_coords() — reads EXIF GPSInfo from a photo (JPG/PNG/HEIC)
     via Pillow, the same library date_resolver.py already uses for
     DateTimeOriginal. Expect very sparse coverage on pre-GPS-era photos
     (see CLAUDE.md/TODO.md) — returning None here is a normal, common
     outcome, not an error.

  2. reverse_geocode() — converts decimal-degree coordinates to a
     human-readable "City, State"/"City, Country" string via the
     `reverse_geocoder` package: a fully offline, bundled worldwide city
     gazetteer (k-d tree nearest-neighbor lookup), no API key, no
     per-lookup cost, no internet dependency — consistent with this
     project's local-only, free-tooling approach elsewhere (Ollama for
     captioning). City-level precision only, per spec — this is not meant
     to be more precise than "Marietta, GA".

Video (MP4/MOV): investigated for this session (see TODO.md/CLAUDE.md) —
hachoir's metadata extraction (already used for video_date_resolver.py's
container creation-date) does not expose any GPS/location field for any
real MOV/MP4 sample checked from this library; only duration/dimensions/
creation-date/comments come through. extract_gps_coords_video() always
returns None for now — an acceptable fallback per spec, not a blocker.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image
from PIL.ExifTags import IFD

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass  # HEIC files will just fail to open here too; caller treats as "no GPS found"

# GPS IFD tag numbers (see PIL.ExifTags.GPS / EXIF spec) — used directly
# rather than via the GPS enum so this module has no import-time dependency
# on exactly which Pillow version's enum names are spelled how.
_GPS_LAT_REF = 1
_GPS_LAT = 2
_GPS_LON_REF = 3
_GPS_LON = 4

# US state full name -> two-letter postal abbreviation, for the "City, GA"
# formatting shown in the spec/CLAUDE.md's example. reverse_geocoder reports
# admin1 as the full state name ("Georgia"), not the abbreviation, for US
# results. Non-US results fall back to "City, <admin1 as reported>" or
# "City, <country code>" — see reverse_geocode() below.
_US_STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "Puerto Rico": "PR", "Guam": "GU",
}

# Lazy singleton: reverse_geocoder loads its ~30k-city worldwide gazetteer
# into memory (a k-d tree) on first use, which measured ~1-2s. Paying that
# once per process (not once per photo) is the entire point of this module
# being import-light at module load time and only pulling reverse_geocoder
# in on first actual lookup.
_rg_module = None


def _get_rg():
    global _rg_module
    if _rg_module is None:
        import reverse_geocoder as rg
        _rg_module = rg
    return _rg_module


def _dms_to_dd(dms, ref: str) -> float:
    d, m, s = (float(x) for x in dms)
    dd = d + m / 60.0 + s / 3600.0
    return -dd if ref in ("S", "W") else dd


def extract_gps_coords(path: Path) -> tuple[float, float] | None:
    """(lat, lon) in decimal degrees from a photo's EXIF GPSInfo, or None
    if absent/unreadable/out-of-range. Photos only (JPG/PNG/HEIC) — see
    extract_gps_coords_video() for the video (non-)equivalent."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            gps_ifd = exif.get_ifd(IFD.GPSInfo)
            if not gps_ifd:
                return None
            lat, lat_ref = gps_ifd.get(_GPS_LAT), gps_ifd.get(_GPS_LAT_REF)
            lon, lon_ref = gps_ifd.get(_GPS_LON), gps_ifd.get(_GPS_LON_REF)
            if not (lat and lat_ref and lon and lon_ref):
                return None
            dd_lat = _dms_to_dd(lat, lat_ref)
            dd_lon = _dms_to_dd(lon, lon_ref)
    except Exception:
        return None  # unreadable/corrupt image, truncated file, unsupported variant, etc.
    if not (-90.0 <= dd_lat <= 90.0 and -180.0 <= dd_lon <= 180.0):
        return None  # malformed GPS tag (e.g. all-zero placeholder some devices write)
    if dd_lat == 0.0 and dd_lon == 0.0:
        return None  # (0, 0) is open ocean off West Africa — in practice always a
                      # missing-GPS placeholder, never a real family-photo location
    return dd_lat, dd_lon


def extract_gps_coords_video(path: Path) -> tuple[float, float] | None:
    """Always None for now — see module docstring. Kept as its own function
    (not just inlined as "always None" at the call site in gps_backfill.py)
    so a future hachoir version, or a different container-metadata library,
    has one obvious place to land without touching any call site."""
    return None


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Offline reverse geocoding via `reverse_geocoder` — no network, no
    API key. Returns e.g. "Marietta, GA" (US) or "Kyiv, Ukraine" /
    "Kyiv, UA" style fallbacks elsewhere, or None if the lookup itself
    fails (should not happen for any valid lat/lon — the gazetteer always
    returns its single nearest city — but guarded defensively since this
    runs against 100k+ real files of varying provenance)."""
    try:
        rg = _get_rg()
        # mode=1 forces single-threaded lookup. reverse_geocoder's default
        # (mode=2) spawns a multiprocessing pool per call -- fine for a
        # one-off batch script, but wrong here: gps_backfill.py calls this
        # once per file in a tight loop, and review_tool.py may call it
        # from a threaded Flask server -- neither wants a process pool
        # spun up per lookup (confirmed misbehaving under this project's
        # own ad hoc test script on Windows: repeated multiprocessing
        # spawns re-imported and re-ran the calling script itself, since
        # Windows has no fork() and the caller isn't guaranteed a
        # `if __name__ == "__main__":` guard). verbose=False suppresses
        # reverse_geocoder's own startup print -- same posture as the
        # hachoir stdout/stderr fix elsewhere in this project (CLAUDE.md).
        result = rg.search([(lat, lon)], mode=1, verbose=False)[0]
    except Exception:
        return None
    city = (result.get("name") or "").strip()
    admin1 = (result.get("admin1") or "").strip()
    cc = (result.get("cc") or "").strip()
    if not city:
        return None
    if cc == "US":
        return f"{city}, {_US_STATE_ABBR.get(admin1, admin1)}" if admin1 else f"{city}, US"
    if admin1:
        return f"{city}, {admin1}"
    return f"{city}, {cc}" if cc else city


def resolve_location(path: Path, is_video: bool = False) -> dict | None:
    """Convenience wrapper used by gps_backfill.py: extract + geocode in
    one call. Returns {"lat", "lon", "location_name"} — location_name may
    itself be None if geocoding failed even though coordinates were found
    (still worth storing the raw lat/lon in that case) — or None outright
    if no GPS coordinates were found at all."""
    coords = extract_gps_coords_video(path) if is_video else extract_gps_coords(path)
    if coords is None:
        return None
    lat, lon = coords
    return {"lat": lat, "lon": lon, "location_name": reverse_geocode(lat, lon)}
