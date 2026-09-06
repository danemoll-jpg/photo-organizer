"""Phase 2g: first-frame thumbnail extraction for video files, used by
src/thumbnail_backfill.py. Mirrors gps_resolver.py's shape (one small
module doing the actual decode work, kept separate from the batch-loop
orchestration in thumbnail_backfill.py).

Backend: OpenCV (`opencv-python-headless`) via `cv2.VideoCapture`. Chosen
over `imageio`+`imageio-ffmpeg` after checking both against this project's
existing preference for self-contained dependencies (CLAUDE.md) — the
opencv-python-headless wheel bundles its own codec backend (confirmed:
decodes MP4/MOV/AVI samples from this library with no system ffmpeg
install present on this machine), and installs cleanly as a normal wheel
on this machine's Python 3.14 venv (cp37-abi3 stable-ABI wheel, same
"verify wheel availability first" caution already applied to every other
dependency in this project). `-headless` (not the plain `opencv-python`
package) specifically because this only ever runs in a batch script /
dashboard worker thread, never needs any GUI window functions, and is a
noticeably smaller install.

Real throughput measured against this library's actual videos (see
CLAUDE.md/TODO.md): ~0.1s/file (40-file random real sample, 0 failures) —
roughly 10x slower per-file than GPS extraction's EXIF read
(~0.01s/file), but nowhere near Phase 2 captioning's ~7-9s/image. At the
real 12,436 video rows in this library's DB, a full run is on the order
of 20-25 minutes — minutes, not days, but explicitly benchmarked rather
than assumed to be as fast as GPS (per spec's own caution).

Console-output note (CLAUDE.md's recurring "does a library do its own
stdout/stderr I/O, and does it survive pythonw.exe" pattern, hit twice
already with tqdm and hachoir): OpenCV's own internal warnings (e.g. an
unusual/corrupt container logging something like "Referenced QT chapter
track not found") are written by native code straight to the OS-level
stderr file descriptor, NOT through Python's `sys.stderr` object — so
this does NOT reproduce the tqdm/hachoir crash class (confirmed: simulating
pythonw.exe by setting `sys.stdout = sys.stderr = None` and decoding a
real video that emits such a warning does not raise). Suppressed anyway,
at import time, purely for a clean console under normal CLI use — not a
crash fix, just tidiness — via cv2's own log-level API (`LOG_LEVEL_SILENT`),
the same "the library has a real suppression knob, use it" approach
gps_resolver.py's `verbose=False` already takes with reverse_geocoder.
"""
from __future__ import annotations

import io
from pathlib import Path

import cv2
from PIL import Image

cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)


def extract_first_frame_jpeg(path: Path, max_dimension: int) -> bytes | None:
    """Decodes the first frame of the video at `path`, downscales it to fit
    within max_dimension (longest edge, aspect preserved — same convention
    as caption.py's image downscaling), and returns it as JPEG bytes. Returns
    None on any failure (missing/corrupt/unsupported-codec file, zero-frame
    video, etc.) rather than raising — the caller (thumbnail_backfill.py)
    treats None as "extraction failed for this file" and moves on, same
    posture as gps_resolver.py's extract_gps_coords() returning None for an
    unreadable/GPS-less photo.

    BGR->RGB conversion: OpenCV decodes frames as BGR (its own convention,
    not a mistake) — Pillow (used here for the resize + JPEG encode, so this
    matches every other image-handling code path in this project) expects
    RGB, so the channel order is flipped before handing off.
    """
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return None
        ok, frame = cap.read()
    except Exception:
        return None
    finally:
        cap.release()
    if not ok or frame is None:
        return None

    try:
        rgb = frame[:, :, ::-1]  # BGR -> RGB
        img = Image.fromarray(rgb)
        img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return None  # decoded a frame but couldn't convert/encode it -- treat as a failure, not a crash
