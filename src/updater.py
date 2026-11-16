"""Self-update for the packaged Photo Organizer (same idea as Chastain Rush's
launcher, deliberately simpler).

How it works
------------
A public GitHub repo (src/version.py::RELEASES_REPO) publishes, per release,
two assets: ``update-manifest.json`` and the app zip it describes. The
*latest* release's manifest is always reachable at a fixed URL
(``.../releases/latest/download/update-manifest.json``), so a check is one
small unauthenticated GET -- no GitHub API, no rate limit, no token.

Install layout (INSTALL_ROOT, see src/paths.py)::

    PhotoOrganizer.exe      the tiny launcher (never replaced by an update)
    state.json              {"current": "1.1.0", "previous": "1.0.0"}
    versions/1.1.0/         one full app folder per kept version
    versions/1.0.0/         the previous one, kept so "Restore previous" works

An update never overwrites anything in place: it downloads the zip, verifies
size + SHA-256, extracts into ``versions/<ver>.staging``, checks the exe is
there, renames it to ``versions/<ver>``, and only then flips ``state.json``
(atomic replace). A failure at any earlier step leaves the installed version
exactly as it was. The launcher reads state.json on every start, so the new
version takes effect on the next launch; the running app is never touched.
User data lives elsewhere (src/paths.py::DATA_ROOT) and is never involved.

Trust model (be honest about it): the SHA-256 in the manifest catches a
corrupt or truncated download. It does NOT protect against someone who can
publish to the releases repo -- they could publish a matching manifest. The
download URL is built from the fixed repo + validated tag/name, never taken
from the manifest, so a manifest cannot redirect the app to another host.
Signing the manifest (as Chastain Rush does) is the upgrade path if this is
ever distributed beyond friends/family.

Stdlib only, so the launcher and tests can import it without the app's deps.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MANIFEST_NAME = "update-manifest.json"
STATE_FILE = "state.json"
VERSIONS_DIR = "versions"
APP_EXE = "PhotoOrganizerApp.exe"
LAUNCHER_EXE = "PhotoOrganizer.exe"

MANIFEST_SCHEMA = 1
MAX_DOWNLOAD_BYTES = int(1.5 * 1024**3)
MAX_UNPACKED_BYTES = 4 * 1024**3
USER_AGENT = "PhotoOrganizer-Updater"

_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ASSET_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\.zip$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

ProgressCb = Callable[[int, int], None]  # (bytes_done, bytes_total)
Opener = Callable[..., object]


class UpdateError(Exception):
    """Anything that stops an update; the message is shown to the user."""


class UpdateCancelled(UpdateError):
    pass


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    tag: str
    notes: str
    asset_name: str
    asset_bytes: int
    asset_sha256: str


# --- versions --------------------------------------------------------------


def parse_version(text: str) -> tuple[int, ...]:
    if not isinstance(text, str) or not _VERSION_RE.match(text):
        raise ValueError(f"bad version {text!r}")
    return tuple(int(p) for p in text.split("."))


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


# --- network ---------------------------------------------------------------


def manifest_url(repo: str) -> str:
    return f"https://github.com/{repo}/releases/latest/download/{MANIFEST_NAME}"


def asset_url(repo: str, info: UpdateInfo) -> str:
    # Built from the fixed repo + validated tag/name -- never from manifest text.
    return f"https://github.com/{repo}/releases/download/{info.tag}/{info.asset_name}"


def _open(url: str, timeout: float, opener: Opener | None):
    if opener is not None:
        return opener(url, timeout)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def parse_manifest(raw: bytes) -> UpdateInfo:
    try:
        m = json.loads(raw.decode("utf-8"))
        if m.get("schema") != MANIFEST_SCHEMA:
            raise UpdateError(f"Unsupported update manifest schema {m.get('schema')!r}; update the app manually.")
        asset = m["asset"]
        info = UpdateInfo(
            version=str(m["version"]),
            tag=str(m["tag"]),
            notes=str(m.get("notes", ""))[:4000],
            asset_name=str(asset["name"]),
            asset_bytes=int(asset["bytes"]),
            asset_sha256=str(asset["sha256"]).lower(),
        )
    except UpdateError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise UpdateError(f"Update manifest is malformed ({e}).") from e
    try:
        parse_version(info.version)
    except ValueError as e:
        raise UpdateError(f"Update manifest has a bad version: {e}") from e
    if not _TAG_RE.match(info.tag):
        raise UpdateError("Update manifest has an invalid tag.")
    if not _ASSET_RE.match(info.asset_name):
        raise UpdateError("Update manifest has an invalid asset name.")
    if not _SHA_RE.match(info.asset_sha256):
        raise UpdateError("Update manifest has an invalid SHA-256.")
    if not 0 < info.asset_bytes <= MAX_DOWNLOAD_BYTES:
        raise UpdateError("Update manifest has an unreasonable asset size.")
    return info


def fetch_latest(repo: str, timeout: float = 15, opener: Opener | None = None) -> UpdateInfo:
    try:
        with _open(manifest_url(repo), timeout, opener) as resp:
            raw = resp.read(256 * 1024 + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("No release has been published yet.") from e
        raise UpdateError(f"The update server answered HTTP {e.code}.") from e
    except Exception as e:  # network down, DNS, TLS...
        raise UpdateError(f"Couldn't reach the update server ({type(e).__name__}: {e}).") from e
    if len(raw) > 256 * 1024:
        raise UpdateError("Update manifest is unexpectedly large.")
    return parse_manifest(raw)


def check_for_update(
    current: str, repo: str, timeout: float = 15, opener: Opener | None = None
) -> UpdateInfo | None:
    """The newer release's info, or None if `current` is already up to date."""
    info = fetch_latest(repo, timeout, opener)
    return info if is_newer(info.version, current) else None


def download_asset(
    info: UpdateInfo,
    repo: str,
    dest_dir: Path,
    progress: ProgressCb | None = None,
    should_stop: Callable[[], bool] | None = None,
    timeout: float = 30,
    opener: Opener | None = None,
) -> Path:
    """Streams the zip into dest_dir, verifying size and SHA-256. Returns the
    verified file's path; on any failure the partial file is removed."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dest_dir).free
    if free < info.asset_bytes * 3:
        raise UpdateError(
            f"Not enough free disk space for the update (need ~{info.asset_bytes * 3 // 2**20} MB, "
            f"have {free // 2**20} MB)."
        )
    final = dest_dir / info.asset_name
    part = dest_dir / (info.asset_name + ".part")
    sha = hashlib.sha256()
    done = 0
    try:
        with _open(asset_url(repo, info), timeout, opener) as resp, open(part, "wb") as out:
            while True:
                if should_stop and should_stop():
                    raise UpdateCancelled("Update download cancelled.")
                block = resp.read(1024 * 256)
                if not block:
                    break
                done += len(block)
                if done > info.asset_bytes:
                    raise UpdateError("Download is larger than the manifest says; aborted.")
                sha.update(block)
                out.write(block)
                if progress:
                    progress(done, info.asset_bytes)
        if done != info.asset_bytes:
            raise UpdateError(f"Download is incomplete ({done} of {info.asset_bytes} bytes).")
        if sha.hexdigest() != info.asset_sha256:
            raise UpdateError("Download failed its SHA-256 check; nothing was installed.")
        os.replace(part, final)
        return final
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    except Exception as e:
        part.unlink(missing_ok=True)
        raise UpdateError(f"Download failed ({type(e).__name__}: {e}).") from e


# --- install state ---------------------------------------------------------


def read_state(install_root: Path) -> dict:
    try:
        data = json.loads((install_root / STATE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(install_root: Path, state: dict) -> None:
    tmp = install_root / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, install_root / STATE_FILE)  # atomic: a crash never leaves a half-written state


def version_dir(install_root: Path, version: str) -> Path:
    return install_root / VERSIONS_DIR / version


def _valid_version_dir(install_root: Path, version: str | None) -> bool:
    if not version or not _VERSION_RE.match(version):
        return False
    return (version_dir(install_root, version) / APP_EXE).is_file()


def _prune(install_root: Path, keep: set[str]) -> None:
    vroot = install_root / VERSIONS_DIR
    if not vroot.is_dir():
        return
    for child in vroot.iterdir():
        if child.name not in keep:
            shutil.rmtree(child, ignore_errors=True)


# --- install ---------------------------------------------------------------


def _extract_safely(zip_path: Path, target: Path) -> None:
    root = target.resolve()
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise UpdateError("Downloaded update isn't a valid zip file.") from e
    with zf:
        total = 0
        for member in zf.infolist():
            total += member.file_size
            if total > MAX_UNPACKED_BYTES:
                raise UpdateError("Update expands to an unreasonable size; aborted.")
            dest = (target / member.filename).resolve()
            if dest != root and root not in dest.parents:
                raise UpdateError("Update zip contains an unsafe path; aborted.")
        zf.extractall(target)


def install_zip(zip_path: Path, install_root: Path, version: str) -> Path:
    """Extracts an already-verified zip as versions/<version> and makes it the
    current version. The previous current becomes `previous`; anything older
    is removed. Returns the new version's folder."""
    parse_version(version)
    vroot = install_root / VERSIONS_DIR
    vroot.mkdir(parents=True, exist_ok=True)
    staging = vroot / (version + ".staging")
    final = vroot / version
    shutil.rmtree(staging, ignore_errors=True)
    try:
        _extract_safely(zip_path, staging)
        if not (staging / APP_EXE).is_file():
            raise UpdateError(f"Update zip doesn't contain {APP_EXE}; nothing was installed.")
        state = read_state(install_root)
        old_current = state.get("current")
        if final.exists():
            if old_current == version:
                raise UpdateError(f"Version {version} is already installed.")
            shutil.rmtree(final)  # a stale copy of this version, not the running one
        os.replace(staging, final)
    except UpdateError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"Couldn't install the update ({e}).") from e

    new_state = {"current": version, "previous": old_current if old_current != version else None}
    write_state(install_root, new_state)
    _prune(install_root, {v for v in (new_state["current"], new_state["previous"]) if v})
    return final


def download_and_install(
    info: UpdateInfo,
    repo: str,
    install_root: Path,
    progress: ProgressCb | None = None,
    should_stop: Callable[[], bool] | None = None,
    opener: Opener | None = None,
) -> Path:
    downloads = install_root / "downloads"
    zip_path = download_asset(info, repo, downloads, progress, should_stop, opener=opener)
    try:
        return install_zip(zip_path, install_root, info.version)
    finally:
        shutil.rmtree(downloads, ignore_errors=True)


def restore_previous(install_root: Path) -> str:
    """Swap current <-> previous. Returns the version now current."""
    state = read_state(install_root)
    prev, cur = state.get("previous"), state.get("current")
    if not _valid_version_dir(install_root, prev):
        raise UpdateError("There's no previous version to restore.")
    write_state(install_root, {"current": prev, "previous": cur if _valid_version_dir(install_root, cur) else None})
    return prev


def installed_summary(install_root: Path) -> dict:
    s = read_state(install_root)
    return {
        "current": s.get("current") if _valid_version_dir(install_root, s.get("current")) else None,
        "previous": s.get("previous") if _valid_version_dir(install_root, s.get("previous")) else None,
    }
