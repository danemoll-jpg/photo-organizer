"""Tests for the packaging pieces: launcher/launcher.py, src/first_run.py,
src/paths.py. (The updater itself is tests/test_updater.py.)

Checks:
  * the launcher's constants match src/updater.py (they're deliberately
    duplicated -- the launcher is its own tiny exe -- so drift must be caught);
  * the launcher starts state.json's current version; with a missing/broken
    current it falls back to previous; with nothing it reports and stops;
  * a version that dies right after start is rolled back to previous;
  * first-run config gets a per-user dest_root, keeps dry_run true, and never
    overwrites an existing config;
  * running from source resolves paths exactly as before (repo root).

Usage:
    venv\\Scripts\\python tests\\test_packaging.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from src import first_run, paths, updater  # noqa: E402


def load_launcher():
    spec = importlib.util.spec_from_file_location("po_launcher", ROOT / "launcher" / "launcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_constants_match_updater() -> None:
    L = load_launcher()
    assert (L.STATE_FILE, L.VERSIONS_DIR, L.APP_EXE) == (updater.STATE_FILE, updater.VERSIONS_DIR, updater.APP_EXE)


class FakeProc:
    def __init__(self, code):
        self._code = code

    def poll(self):
        return self._code


def make_root(versions: list[str], state: dict | None) -> Path:
    root = Path(tempfile.mkdtemp())
    for v in versions:
        d = root / "versions" / v
        d.mkdir(parents=True)
        (d / "PhotoOrganizerApp.exe").write_bytes(b"x")
    if state is not None:
        (root / "state.json").write_text(json.dumps(state))
    return root


def run_launcher(root: Path, codes: dict[str, int | None]):
    """codes: version -> what poll() returns (None = still running).
    Returns (started versions in order, message boxes shown, final state)."""
    L = load_launcher()
    started: list[str] = []
    boxes: list[str] = []
    L.install_root = lambda: root
    L._msgbox = lambda t: boxes.append(t)
    L.STARTUP_WATCH_SECONDS = 1.0

    def fake_start(r, exe, args):
        v = exe.parent.name
        started.append(v)
        assert r == root
        return FakeProc(codes[v])

    L.start = fake_start
    old_argv = sys.argv
    sys.argv = ["PhotoOrganizer.exe"]
    try:
        rc = L.main()
    finally:
        sys.argv = old_argv
    return rc, started, boxes, json.loads((root / "state.json").read_text()) if (root / "state.json").exists() else None


def test_launcher_starts_current() -> None:
    root = make_root(["1.0.0", "1.1.0"], {"current": "1.1.0", "previous": "1.0.0"})
    rc, started, boxes, state = run_launcher(root, {"1.1.0": None, "1.0.0": None})
    assert (rc, started, boxes) == (0, ["1.1.0"], [])
    assert state == {"current": "1.1.0", "previous": "1.0.0"}, "state untouched on a healthy start"


def test_launcher_falls_back_when_current_missing() -> None:
    root = make_root(["1.0.0"], {"current": "1.1.0", "previous": "1.0.0"})  # 1.1.0 folder gone
    rc, started, boxes, state = run_launcher(root, {"1.0.0": None})
    assert started == ["1.0.0"] and state == {"current": "1.0.0", "previous": None}


def test_launcher_nothing_installed() -> None:
    root = make_root([], {"current": "1.1.0", "previous": None})
    rc, started, boxes, _ = run_launcher(root, {})
    assert rc == 1 and started == [] and len(boxes) == 1


def test_launcher_rolls_back_a_crashing_version() -> None:
    root = make_root(["1.0.0", "1.1.0"], {"current": "1.1.0", "previous": "1.0.0"})
    rc, started, boxes, state = run_launcher(root, {"1.1.0": 3, "1.0.0": None})
    assert rc == 0 and started == ["1.1.0", "1.0.0"]
    assert state == {"current": "1.0.0", "previous": None}
    assert len(boxes) == 1 and "1.1.0" in boxes[0] and "1.0.0" in boxes[0]


def test_launcher_crash_with_no_previous_reports() -> None:
    root = make_root(["1.0.0"], {"current": "1.0.0", "previous": None})
    rc, started, boxes, _ = run_launcher(root, {"1.0.0": 5})
    assert rc == 5 and started == ["1.0.0"] and len(boxes) == 1


def test_launcher_rejects_path_tricks_in_state() -> None:
    L = load_launcher()
    root = make_root(["1.0.0"], None)
    assert L.app_path(root, "1.0.0") is not None
    for bad in ["..", "../1.0.0", "1.0.0\\..", "", None, 5]:
        assert L.app_path(root, bad) is None, bad


def test_first_run_config() -> None:
    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as d:
        cfg_path = Path(d) / "sub" / "config.yaml"
        assert first_run.ensure_config(cfg_path, ROOT / "config.example.yaml", force=True) is True
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert cfg["dest_root"] == first_run.default_dest_root() and "E:" not in cfg["dest_root"]
        assert cfg["dry_run"] is True, "first run must stay in the safe dry-run default"
        orig = yaml.safe_load(example)
        assert {k: v for k, v in cfg.items() if k != "dest_root"} == {k: v for k, v in orig.items() if k != "dest_root"}
        cfg_path.write_text("dest_root: mine\n")
        assert first_run.ensure_config(cfg_path, ROOT / "config.example.yaml", force=True) is False
        assert cfg_path.read_text() == "dest_root: mine\n", "never overwrite an existing config"
        other = Path(d) / "o" / "config.yaml"
        assert first_run.ensure_config(other, ROOT / "config.example.yaml", force=False) is False, "source runs are untouched"
    tricky = first_run.make_config_text('x: 1\ndest_root: "E:\\\\Pics"\ny: 2\n', "C:\\Users\\O'Brien\\Pictures")
    assert yaml.safe_load(tricky) == {"x": 1, "dest_root": "C:\\Users\\O'Brien\\Pictures", "y": 2}


def test_source_paths_unchanged() -> None:
    assert not paths.FROZEN
    assert paths.APP_ROOT == paths.DATA_ROOT == ROOT and paths.INSTALL_ROOT is None
    from src import config, db

    assert config.CONFIG_PATH == ROOT / "config.yaml" and config.REPO_ROOT == ROOT
    assert db.SCHEMA_PATH == ROOT / "schema.sql" and db.SCHEMA_PATH.is_file()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
