"""Single source of truth for the app version and where releases are published.

Bump __version__ here, then run Tools/build_release.py. Versions are plain
dotted integers ("1.2.0"); src/updater.py compares them numerically.
"""
__version__ = "1.0.0"

# Public GitHub repo whose Releases carry update-manifest.json + the app zip.
# Same shape as Chastain Rush's chastain-rush-releases repo. The repo does
# not need to exist for the app to run -- a failed check is reported, never
# fatal.
RELEASES_REPO = "danemoll-jpg/photo-organizer-releases"
