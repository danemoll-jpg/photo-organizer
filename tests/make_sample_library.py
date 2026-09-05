"""Builds a small synthetic photo library exercising every Phase 1 code
path (EXIF date, filename-pattern date, filesystem-date fallback, an
unsorted/no-date case, an exact-hash duplicate, a filename collision, a
file already correctly placed under dest, and a loose file sitting in
dest's root) — without touching any real photos.

Also builds the Phase 1b video-side equivalents (filename-pattern date,
filesystem-date fallback, exact-hash duplicate, filename collision,
already-correctly-placed under .../Video/, and a loose video at dest's
root) covering MP4/MOV/AVI. These use plain placeholder bytes, NOT real
video container structure — hachoir fails to parse them (gracefully,
same as an unreadable EXIF blob) and the chain falls through to
filename/filesystem, same as it would for a real corrupt/unusual file.
The container-metadata-found step (the one genuinely new piece of Phase
1b logic) is deliberately NOT faked here — it was instead verified
against real MP4/MOV files from the actual library in dry-run mode; see
the Phase 1b session summary. Synthetic container bytes would need a
real, valid MP4/MOV atom structure to mean anything, and hand-rolling
one wouldn't test anything a real file doesn't already test better.

Usage:
    venv\\Scripts\\python tests\\make_sample_library.py [output_dir]

Then point config.yaml's source_folders / dest_root at the printed paths
and run `python main.py scan` / `python main.py run --execute --yes`.

Requires piexif (dev-only, not in requirements.txt):
    venv\\Scripts\\pip install piexif
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import piexif
from PIL import Image

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None


def make_jpeg(path: Path, color=(255, 0, 0), exif_dt: str | None = None) -> None:
    img = Image.new("RGB", (40, 40), color)
    if exif_dt:
        exif_dict = {"Exif": {piexif.ExifIFD.DateTimeOriginal: exif_dt.encode()}}
        img.save(path, "jpeg", exif=piexif.dump(exif_dict))
    else:
        img.save(path, "jpeg")


def make_png(path: Path, color=(0, 255, 0)) -> None:
    Image.new("RGB", (40, 40), color).save(path, "png")


def make_fake_video(path: Path, payload: bytes) -> None:
    """Placeholder video file — plain bytes, not a real container. hachoir's
    createParser()/extractMetadata() fail gracefully on these (same posture
    as _from_exif on an unreadable image), so date resolution falls through
    to filename/filesystem — exactly what these fixtures are testing. See
    module docstring for why real container-metadata parsing is tested
    against real files instead of faked here."""
    path.write_bytes(payload)


def build(root: Path) -> tuple[Path, Path, Path]:
    src1, src2, dest = root / "source1_pictures", root / "source2_old_backup", root / "dest_pics"
    for d in (src1, src2, dest):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    make_jpeg(src1 / "DSC00123.jpg", (255, 0, 0), exif_dt="2015:06:12 14:30:00")               # 1. EXIF date
    make_jpeg(src1 / "IMG_20180304_091500.jpg", (0, 0, 255))                                    # 2. filename-pattern date
    make_jpeg(src1 / "vacation_photo.jpg", (255, 255, 0))                                        # 3. filesystem-date fallback
    make_png(src1 / "randomscreenshot.png", (0, 255, 0))                                          # 4. no date anywhere but mtime
    make_jpeg(src2 / "old_name_for_same_photo.jpg", (255, 0, 0), exif_dt="2015:06:12 14:30:00")     # 5. exact dup of #1
    make_jpeg(src2 / "IMG_20180304_091500.jpg", (128, 64, 200))                                      # 6. filename collision w/ #2
    already_dir = dest / "2020" / "2020-07"
    already_dir.mkdir(parents=True)
    make_jpeg(already_dir / "IMG_20200715_100000.jpg", (10, 200, 10))                                 # 7. already correctly placed
    make_png(dest / "loose_screenshot.png", (200, 200, 0))                                             # 8. loose file at dest root

    if pillow_heif:
        img = Image.new("RGB", (40, 40), (10, 20, 30))
        exif_dict = {"Exif": {piexif.ExifIFD.DateTimeOriginal: b"2019:11:05 08:00:00"}}
        img.save(src1 / "IMG_5566.heic", format="HEIF", exif=piexif.dump(exif_dict))                    # 9. HEIC + EXIF

    # --- Phase 1b: video fixtures (see module docstring — placeholder bytes,
    # not real containers; container-metadata step tested against real files) ---
    make_fake_video(src1 / "VID_20190815_120000.mp4", b"FAKEMP4-A" * 50)                                # 10. filename-pattern date (video)
    make_fake_video(src1 / "home_video.mov", b"FAKEMOV-B" * 50)                                          # 11. filesystem-date fallback (video)
    make_fake_video(src2 / "old_name_for_same_video.mp4", b"FAKEMP4-A" * 50)                              # 12. exact dup of #10
    make_fake_video(src2 / "VID_20190815_120000.mp4", b"FAKEMP4-C" * 50)                                   # 13. filename collision w/ #10
    already_video_dir = dest / "2021" / "2021-03" / "Video"
    already_video_dir.mkdir(parents=True)
    make_fake_video(already_video_dir / "VID_20210310_090000.mp4", b"FAKEMP4-D" * 50)                       # 14. already correctly placed (video, in .../Video/)
    make_fake_video(dest / "loose_clip.avi", b"FAKEAVI-E" * 50)                                              # 15. loose video file at dest root

    return src1, src2, dest


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / "photo_organizer_test"
    src1, src2, dest = build(out)
    print(f"Sample library created at: {out}\n")
    for p in sorted(out.rglob("*")):
        if p.is_file():
            print(" ", p.relative_to(out))
    print(f"\nsource_folders:\n  - \"{src1}\"\n  - \"{src2}\"\ndest_root: \"{dest}\"")
