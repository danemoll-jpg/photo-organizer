"""Builds the downloadable "core" Photo Organizer and stages a release.

    venv\\Scripts\\python Tools\\build_release.py [--notes "text" | --notes-file F]
                                                [--skip-build] [--not-latest]

Output (Builds/release/<tag>/, git-ignored) -- nothing is uploaded here:

    assets/PhotoOrganizer-<ver>-windows.zip     first-time download: launcher
                                                + state.json + versions/<ver>
    assets/PhotoOrganizer-app-<ver>.zip         what an in-app update fetches
    assets/update-manifest.json                 what "Check for Updates" reads
    INVENTORY.json                              sizes + SHA-256 of every asset,
                                                the contract Tools/publish_release.py
                                                re-verifies before uploading

Core edition = organizer, dashboard, review tool, GPS/thumbnail/date tools.
The GPU/face libraries (insightface, onnxruntime, faiss, scikit-learn) are
deliberately excluded; the dashboard hides those panels when they're absent.

The built app is smoke-tested headlessly (--selftest) BEFORE anything is
staged: a build that can't import its own modules or find its bundled
schema/templates never becomes a release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import updater  # noqa: E402
from src.version import RELEASES_REPO, __version__  # noqa: E402

WORK = ROOT / "build" / "pyinstaller"
DIST = ROOT / "dist"
RELEASE_ROOT = ROOT / "Builds" / "release"

EXCLUDES = [
    # GPU / face stack (a later "full" edition could add these back)
    "insightface", "onnxruntime", "onnx", "faiss", "sklearn", "skimage", "scipy.stats",
    # never needed at runtime
    "torch", "tensorflow", "matplotlib", "IPython", "pytest", "notebook", "pandas",
]

README_TEXT = """Photo Organizer {version}

1. Extract this ZIP anywhere you can write to (for example Documents\\Apps).
2. Run PhotoOrganizer.exe.

The app checks for updates when it opens and installs them only when you click
Install (Dashboard > Updates). The previous version is kept so you can go back.

Your settings, database and logs live in %LOCALAPPDATA%\\PhotoOrganizer, outside
this folder -- deleting or re-extracting this folder never touches them. Your
photos stay wherever you keep them.

First run is a safe dry run: nothing is moved until you turn dry_run off in
config.yaml (Dashboard > Updates > Open Data Folder).

Windows may show a "Windows protected your PC" notice because the app is not
code-signed: choose More info > Run anyway.
"""


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def build_app() -> Path:
    sep = os.pathsep
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
        "--name", "PhotoOrganizerApp",
        "--distpath", DIST, "--workpath", WORK / "app", "--specpath", WORK,
        "--add-data", f"{ROOT / 'schema.sql'}{sep}.",
        "--add-data", f"{ROOT / 'config.example.yaml'}{sep}.",
        "--add-data", f"{ROOT / 'templates'}{sep}templates",
        "--add-data", f"{ROOT / 'static'}{sep}static",
        "--collect-data", "reverse_geocoder",
        "--collect-submodules", "hachoir",
        "--collect-submodules", "comtypes",
        "--hidden-import", "pillow_heif",
        "--paths", ROOT,
    ]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    cmd.append(ROOT / "app_entry.py")
    run(cmd)
    out = DIST / "PhotoOrganizerApp"
    if not (out / updater.APP_EXE).is_file():
        raise SystemExit(f"Build produced no {updater.APP_EXE}")
    return out


def build_launcher() -> Path:
    run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onefile",
        "--name", "PhotoOrganizer",
        "--distpath", DIST / "launcher", "--workpath", WORK / "launcher", "--specpath", WORK,
        ROOT / "launcher" / "launcher.py",
    ])
    exe = DIST / "launcher" / updater.LAUNCHER_EXE
    if not exe.is_file():
        raise SystemExit("Build produced no launcher exe")
    return exe


def selftest(app_dir: Path) -> None:
    """Runs the BUILT exe headlessly against a throwaway data folder."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "selftest.json"
        env = dict(os.environ, PHOTO_ORGANIZER_HOME=str(Path(tmp) / "data"))
        r = subprocess.run([str(app_dir / updater.APP_EXE), "--selftest", str(out)], env=env, timeout=300)
        if not out.is_file():
            raise SystemExit(f"Self-test wrote no result (exit {r.returncode}); the built app can't start.")
        result = json.loads(out.read_text(encoding="utf-8"))
    print(json.dumps({k: v for k, v in result.items() if k != "traceback"}, indent=2))
    if not result.get("ok"):
        print(result.get("traceback", ""), file=sys.stderr)
        raise SystemExit("Self-test FAILED; nothing was staged.")
    if result.get("version") != __version__:
        raise SystemExit(f"Built app reports version {result.get('version')}, expected {__version__}.")
    if not result.get("frozen"):
        raise SystemExit("Built app doesn't think it is frozen.")


def zip_tree(src: Path, dest: Path, prefix: str = "") -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                zf.write(p, (Path(prefix) / p.relative_to(src)).as_posix())


def stage(app_dir: Path, launcher_exe: Path, notes: str, latest: bool) -> Path:
    tag = f"v{__version__}"
    rel = RELEASE_ROOT / tag
    shutil.rmtree(rel, ignore_errors=True)
    assets = rel / "assets"
    assets.mkdir(parents=True)

    update_zip = assets / f"PhotoOrganizer-app-{__version__}.zip"
    zip_tree(app_dir, update_zip)

    # The first-time download is the launcher + an install laid out exactly as
    # an update would leave it, so the first run and every update are one path.
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "PhotoOrganizer"
        shutil.copytree(app_dir, pkg / updater.VERSIONS_DIR / __version__)
        shutil.copy2(launcher_exe, pkg / updater.LAUNCHER_EXE)
        (pkg / updater.STATE_FILE).write_text(json.dumps({"current": __version__, "previous": None}, indent=2))
        (pkg / "README.txt").write_text(README_TEXT.format(version=__version__), encoding="utf-8")
        full_zip = assets / f"PhotoOrganizer-{__version__}-windows.zip"
        zip_tree(Path(tmp), full_zip)

    manifest = {
        "schema": updater.MANIFEST_SCHEMA,
        "version": __version__,
        "tag": tag,
        "notes": notes,
        "asset": {"name": update_zip.name, "bytes": update_zip.stat().st_size, "sha256": sha256_of(update_zip)},
    }
    mpath = assets / updater.MANIFEST_NAME
    mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    updater.parse_manifest(mpath.read_bytes())  # the app must be able to read what we ship

    inventory = {
        "repository": RELEASES_REPO,
        "tag": tag,
        "title": f"Photo Organizer {__version__}",
        "latest": latest,
        "game": {"version": __version__, "notes": notes},
        "assets": [
            {"path": p.name, "bytes": p.stat().st_size, "sha256": sha256_of(p)} for p in sorted(assets.iterdir())
        ],
    }
    (rel / "INVENTORY.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    return rel


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notes", default="")
    ap.add_argument("--notes-file", type=Path)
    ap.add_argument("--skip-build", action="store_true", help="re-stage from the existing dist/ output")
    ap.add_argument("--not-latest", action="store_true", help="stage without moving 'latest' (installed apps won't see it)")
    a = ap.parse_args()
    notes = a.notes_file.read_text(encoding="utf-8").strip() if a.notes_file else a.notes
    if not notes:
        notes = f"Photo Organizer {__version__}."

    if a.skip_build:
        app_dir, launcher_exe = DIST / "PhotoOrganizerApp", DIST / "launcher" / updater.LAUNCHER_EXE
    else:
        shutil.rmtree(DIST, ignore_errors=True)
        app_dir, launcher_exe = build_app(), build_launcher()
    selftest(app_dir)
    rel = stage(app_dir, launcher_exe, notes, latest=not a.not_latest)
    print(f"\nStaged {rel}")
    for p in sorted((rel / "assets").iterdir()):
        print(f"  {p.name}  {p.stat().st_size / 2**20:.1f} MB")
    print("\nNothing was uploaded. To publish: Tools/publish_release.py (dry run by default).")


if __name__ == "__main__":
    main()
