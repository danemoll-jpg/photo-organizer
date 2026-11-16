"""Where things live, for both "running from source" and "frozen installer".

Two roots, deliberately separate:

* APP_ROOT  -- read-only resources that ship WITH the code (schema.sql,
  templates/, static/, config.example.yaml). Source run: the repo root.
  Frozen (PyInstaller): the bundle's own extraction dir (sys._MEIPASS).
  An update replaces this wholesale, so never write here.

* DATA_ROOT -- everything the user owns (config.yaml, data/, logs/, the
  SQLite DB, thumbnails). Source run: the repo root, i.e. EXACTLY what
  every module used before this file existed, so running from source is
  unchanged. Frozen: %LOCALAPPDATA%\\PhotoOrganizer (override with the
  PHOTO_ORGANIZER_HOME env var). It lives outside the install folder so
  an update, a restore-previous, or deleting/re-extracting the install
  never touches the user's library data.

INSTALL_ROOT (frozen only) is the folder holding the launcher, versions/
and state.json -- see src/updater.py. None when running from source.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN: bool = bool(getattr(sys, "frozen", False))

_SOURCE_ROOT = Path(__file__).resolve().parent.parent


def _frozen_app_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))


def _frozen_data_root() -> Path:
    override = os.environ.get("PHOTO_ORGANIZER_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "PhotoOrganizer"


def _frozen_install_root() -> Path:
    """The launcher exports PHOTO_ORGANIZER_INSTALL_ROOT; fall back to the
    layout (<install>/versions/<ver>/PhotoOrganizerApp.exe) so a directly
    launched app exe still finds its install folder."""
    env = os.environ.get("PHOTO_ORGANIZER_INSTALL_ROOT")
    if env:
        return Path(env)
    return Path(sys.executable).resolve().parent.parent.parent


APP_ROOT: Path = _frozen_app_root() if FROZEN else _SOURCE_ROOT
DATA_ROOT: Path = _frozen_data_root() if FROZEN else _SOURCE_ROOT
INSTALL_ROOT: Path | None = _frozen_install_root() if FROZEN else None

if FROZEN:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
