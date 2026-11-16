"""Publishes a staged release (Tools/build_release.py) to the public GitHub
releases repo. A deliberate, human-run operation: the default is a DRY RUN,
and the app and the build never call this.

    venv\\Scripts\\python Tools\\publish_release.py Builds\\release\\v1.0.0
    venv\\Scripts\\python Tools\\publish_release.py Builds\\release\\v1.0.0 \\
        --publish --confirm-repository danemoll-jpg/photo-organizer-releases

What it checks before anything goes public:
  * every staged file still matches INVENTORY.json (size + SHA-256);
  * the repo exists, is public, and you have push rights (via your own
    already-configured `gh` login -- no credentials are read or stored here);
  * the release is created as a DRAFT, every asset is uploaded with
    update-manifest.json LAST, each upload's size/digest is verified, and only
    then is the draft published as Latest. Installed apps read the manifest
    from "latest", so they never see a half-uploaded release.

Needs the GitHub CLI (`gh`), logged in. One-time repo setup (your call, it is
a public repo):  gh repo create danemoll-jpg/photo-organizer-releases --public
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import updater  # noqa: E402

MANIFEST = updater.MANIFEST_NAME


import os  # noqa: E402

# The GitHub CLI: $GH if set, else `gh` on PATH.
GH = os.environ.get("GH") or "gh"


def gh(*args: str) -> str:
    try:
        return subprocess.run([GH, *args], check=True, capture_output=True, text=True).stdout
    except FileNotFoundError:
        raise SystemExit("The GitHub CLI (gh) isn't installed or isn't on PATH: https://cli.github.com/")
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"gh {' '.join(args[:3])}... failed: {(e.stderr or e.stdout).strip()}")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("release_dir", type=Path)
    ap.add_argument("--publish", action="store_true", help="actually create and publish the release (default: dry run)")
    ap.add_argument("--confirm-repository", help="must equal the repository named in INVENTORY.json")
    ap.add_argument("--resume-draft", action="store_true", help="finish an interrupted publish: reuse the unpublished draft with this tag")
    a = ap.parse_args()

    inv = json.loads((a.release_dir / "INVENTORY.json").read_text(encoding="utf-8"))
    repo, tag, latest = inv["repository"], inv["tag"], inv.get("latest", True)
    assets_dir = a.release_dir / "assets"

    for item in inv["assets"]:
        f = assets_dir / item["path"]
        if not f.is_file() or f.stat().st_size != item["bytes"]:
            raise SystemExit(f"Staged asset missing or wrong size: {item['path']}")
        if sha256_of(f) != item["sha256"]:
            raise SystemExit(f"Staged asset changed since the build: {item['path']}")
        if item["bytes"] >= 2 * 1024**3:
            raise SystemExit(f"{item['path']} is over GitHub's 2 GiB asset limit")
    # The app must be able to read the manifest we are about to make "latest".
    updater.parse_manifest((assets_dir / MANIFEST).read_bytes())

    total = sum(i["bytes"] for i in inv["assets"])
    print(json.dumps({"repository": repo, "tag": tag, "assets": len(inv["assets"]), "bytes": total, "latest": latest, "publish": a.publish}))
    if not a.publish:
        print("Dry run: all staged files verified. Nothing was uploaded.")
        return
    if a.confirm_repository != repo:
        raise SystemExit(f"Explicit confirmation required: --confirm-repository {repo}")

    info = json.loads(gh("api", f"repos/{repo}"))
    if info["private"] or not info.get("permissions", {}).get("push"):
        raise SystemExit("The releases repo must be public and you need push permission.")
    existing = json.loads(gh("api", f"repos/{repo}/releases?per_page=100"))
    matches = [r for r in existing if r["tag_name"] == tag]
    have: dict[str, dict] = {}
    if matches:
        # Only an UNPUBLISHED draft may be resumed, and only on request; a published release is never touched.
        if not a.resume_draft or len(matches) != 1 or not matches[0]["draft"]:
            raise SystemExit(f"A release tagged {tag} already exists; refusing to touch it. Bump the version instead "
                             "(or pass --resume-draft to finish an unpublished draft).")
        have = {x["name"]: x for x in matches[0]["assets"]}
        for item in inv["assets"]:
            x = have.get(item["path"])
            if x and (x["size"] != item["bytes"] or x.get("digest") not in (None, "sha256:" + item["sha256"])):
                raise SystemExit(f"The draft's {item['path']} differs from the approved inventory; nothing was overwritten.")
    elif a.resume_draft:
        raise SystemExit("--resume-draft given but no matching draft exists.")
    else:
        gh("release", "create", tag, "--repo", repo, "--draft", "--title", inv["title"], "--notes", inv["game"]["notes"],
           *([] if latest else ["--latest=false"]))
    for item in sorted(inv["assets"], key=lambda i: i["path"] == MANIFEST):  # manifest last
        if item["path"] in have:
            print(f"Already uploaded {item['path']}", flush=True)
            continue
        gh("release", "upload", tag, str(assets_dir / item["path"]), "--repo", repo)
        print(f"Uploaded {item['path']} ({item['bytes']:,} bytes)", flush=True)

    rel = next(r for r in json.loads(gh("api", f"repos/{repo}/releases?per_page=100")) if r["tag_name"] == tag and r["draft"])
    remote = {x["name"]: x for x in rel["assets"]}
    for item in inv["assets"]:
        r = remote.get(item["path"])
        if not r or r["size"] != item["bytes"] or r.get("digest") not in (None, "sha256:" + item["sha256"]):
            raise SystemExit(f"Upload verification failed for {item['path']}; the draft stays unpublished.")
    gh("release", "edit", tag, "--repo", repo, "--draft=false", "--latest" if latest else "--latest=false")
    print(f"Published {tag}. Installed apps will see it on their next update check.")


if __name__ == "__main__":
    main()
