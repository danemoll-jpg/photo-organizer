"""Tests for src/updater.py (the packaged app's self-update). Everything runs
against an in-memory fake "GitHub" -- no network.

What matters most, and is checked below:
  * a corrupt / truncated / oversized / cancelled download installs NOTHING;
  * a hostile zip (path traversal, no exe) installs NOTHING and leaves the
    current version and state.json exactly as they were;
  * a successful install flips state.json atomically, keeps exactly the
    current + previous versions, and "restore previous" swaps them back;
  * the download URL comes from the fixed repo + validated tag/name, never
    from text in the manifest.

Usage:
    venv\\Scripts\\python tests\\test_updater.py
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import updater as u  # noqa: E402

REPO = "someone/some-releases"


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def good_zip(marker: str) -> bytes:
    return make_zip({u.APP_EXE: b"exe-" + marker.encode(), "_internal/lib.dll": b"dll"})


def manifest_for(version: str, blob: bytes, over: dict | None = None) -> bytes:
    m = {
        "schema": 1,
        "version": version,
        "tag": f"v{version}",
        "notes": "n",
        "asset": {"name": f"PhotoOrganizer-app-{version}.zip", "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()},
    }
    m.update(over or {})
    return json.dumps(m).encode()


class FakeResp:
    def __init__(self, data: bytes):
        self._b = io.BytesIO(data)

    def read(self, n=-1):
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_opener(routes: dict[str, bytes], seen: list[str] | None = None):
    def opener(url, timeout):
        if seen is not None:
            seen.append(url)
        if url not in routes:
            raise OSError(f"404 {url}")
        return FakeResp(routes[url])

    return opener


def raises(exc, fn, *a, **k) -> str:
    try:
        fn(*a, **k)
    except exc as e:
        return str(e)
    raise AssertionError(f"expected {exc.__name__}")


def test_versions() -> None:
    assert u.is_newer("1.1.0", "1.0.0")
    assert u.is_newer("1.10.0", "1.9.0"), "numeric, not string, comparison"
    assert not u.is_newer("1.0.0", "1.0.0")
    assert not u.is_newer("1.0", "1.0.0"), "missing trailing parts count as 0"
    assert u.is_newer("1.0.1", "1.0")
    raises(ValueError, u.parse_version, "1.0.0-beta")
    raises(ValueError, u.parse_version, "v1.0")


def test_manifest_validation() -> None:
    blob = good_zip("a")
    info = u.parse_manifest(manifest_for("1.2.0", blob))
    assert info.version == "1.2.0" and info.tag == "v1.2.0" and info.asset_bytes == len(blob)
    bad = [
        dict(schema=2),
        dict(version="1.x"),
        dict(tag="../evil"),
        dict(tag="a/b"),
        dict(asset={"name": "../x.zip", "bytes": 5, "sha256": "0" * 64}),
        dict(asset={"name": "x.exe", "bytes": 5, "sha256": "0" * 64}),
        dict(asset={"name": "x.zip", "bytes": 5, "sha256": "nothex"}),
        dict(asset={"name": "x.zip", "bytes": 0, "sha256": "0" * 64}),
        dict(asset={"name": "x.zip", "bytes": u.MAX_DOWNLOAD_BYTES + 1, "sha256": "0" * 64}),
    ]
    for over in bad:
        raises(u.UpdateError, u.parse_manifest, manifest_for("1.2.0", blob, over))
    raises(u.UpdateError, u.parse_manifest, b"not json")
    raises(u.UpdateError, u.parse_manifest, b"[]")


def test_check_for_update() -> None:
    blob = good_zip("a")
    routes = {u.manifest_url(REPO): manifest_for("1.1.0", blob)}
    assert u.check_for_update("1.0.0", REPO, opener=fake_opener(routes)).version == "1.1.0"
    assert u.check_for_update("1.1.0", REPO, opener=fake_opener(routes)) is None
    assert u.check_for_update("2.0.0", REPO, opener=fake_opener(routes)) is None
    msg = raises(u.UpdateError, u.check_for_update, "1.0.0", REPO, opener=fake_opener({}))
    assert "reach" in msg.lower(), "a missing release/network reads as a friendly error, not a traceback"


def test_download_good_bad_cancel() -> None:
    blob = good_zip("a")
    info = u.parse_manifest(manifest_for("1.1.0", blob))
    url = u.asset_url(REPO, info)
    assert url == f"https://github.com/{REPO}/releases/download/v1.1.0/PhotoOrganizer-app-1.1.0.zip"
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "dl"
        seen: list[str] = []
        ticks: list[tuple[int, int]] = []
        p = u.download_asset(info, REPO, dest, progress=lambda a, b: ticks.append((a, b)), opener=fake_opener({url: blob}, seen))
        assert p.read_bytes() == blob and seen == [url] and ticks[-1] == (len(blob), len(blob))
        assert not list(dest.glob("*.part"))
        for tampered, why in [
            (blob[:-1] + b"X", "sha"),
            (blob[:-5], "short"),
            (blob + b"extra", "long"),
        ]:
            d2 = Path(d) / ("bad_" + why)
            raises(u.UpdateError, u.download_asset, info, REPO, d2, opener=fake_opener({url: tampered}))
            assert not list(d2.glob("*")), f"{why}: no partial/unverified file may be left behind"
        d3 = Path(d) / "cancel"
        raises(u.UpdateCancelled, u.download_asset, info, REPO, d3, should_stop=lambda: True, opener=fake_opener({url: blob}))
        assert not list(d3.glob("*"))


def install(root: Path, version: str, blob: bytes | None = None) -> None:
    blob = blob if blob is not None else good_zip(version)
    zp = root / f"{version}.zip"
    zp.write_bytes(blob)
    u.install_zip(zp, root, version)


def test_install_flip_prune_restore() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        install(root, "1.0.0")
        assert u.read_state(root) == {"current": "1.0.0", "previous": None}
        install(root, "1.1.0")
        assert u.read_state(root) == {"current": "1.1.0", "previous": "1.0.0"}
        install(root, "1.2.0")
        assert u.read_state(root) == {"current": "1.2.0", "previous": "1.1.0"}
        assert sorted(p.name for p in (root / "versions").iterdir()) == ["1.1.0", "1.2.0"], "1.0.0 pruned"
        assert (root / "versions/1.2.0" / u.APP_EXE).read_bytes() == b"exe-1.2.0"
        assert u.restore_previous(root) == "1.1.0"
        assert u.read_state(root) == {"current": "1.1.0", "previous": "1.2.0"}
        assert u.restore_previous(root) == "1.2.0", "restore is reversible"
        assert u.installed_summary(root) == {"current": "1.2.0", "previous": "1.1.0"}
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        install(root, "1.0.0")
        raises(u.UpdateError, u.restore_previous, root)  # nothing to go back to


def test_bad_install_changes_nothing() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        install(root, "1.0.0")
        before_state = u.read_state(root)
        before_exe = (root / "versions/1.0.0" / u.APP_EXE).read_bytes()
        hostile = {
            "zip-slip": make_zip({u.APP_EXE: b"x", "../../escaped.txt": b"pwned"}),
            "absolute": make_zip({u.APP_EXE: b"x", "/abs/evil.txt": b"pwned"}),
            "no-exe": make_zip({"readme.txt": b"hi"}),
            "not-a-zip": b"this is not a zip",
        }
        for name, blob in hostile.items():
            zp = root / f"{name}.zip"
            zp.write_bytes(blob)
            raises(u.UpdateError, u.install_zip, zp, root, "1.1.0")
            assert u.read_state(root) == before_state, f"{name}: state.json must be untouched"
            assert (root / "versions/1.0.0" / u.APP_EXE).read_bytes() == before_exe
            assert not (root / "versions/1.1.0").exists() and not (root / "versions/1.1.0.staging").exists(), name
        assert not (root.parent / "escaped.txt").exists() and not (root / "escaped.txt").exists()
        raises(u.UpdateError, u.install_zip, root / "no-exe.zip", root, "1.0.0")  # same version already current


def test_end_to_end() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        install(root, "1.0.0")
        blob = good_zip("1.1.0")
        routes = {u.manifest_url(REPO): manifest_for("1.1.0", blob)}
        info = u.check_for_update("1.0.0", REPO, opener=fake_opener(routes))
        assert info is not None
        routes[u.asset_url(REPO, info)] = blob
        u.download_and_install(info, REPO, root, opener=fake_opener(routes))
        assert u.installed_summary(root) == {"current": "1.1.0", "previous": "1.0.0"}
        assert not (root / "downloads").exists(), "download dir cleaned up"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
