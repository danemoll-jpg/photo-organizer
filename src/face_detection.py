"""Phase 3 Session 1: face detection + embedding extraction via insightface's
FaceAnalysis (buffalo_l model pack -- RetinaFace detection + 512-dim ArcFace
embeddings), read-only against the main SQLite DB, output as its own JSONL
file (data/faces.jsonl) -- same "JSONL for intermediate outputs, SQLite is
the final data layer" pattern as captions.jsonl (CLAUDE.md rule 5).

Architecture note (see CLAUDE.md rule 13 / photo-organizer-spec.md's Phase 3
section): this was originally planned to need a second, separate Python
3.11-3.13 environment, because insightface historically had no official
Python 3.14 wheel. Verified during this session that this premise is now
stale: insightface 1.0.1 (released 2026-05-23) ships a single
Python-version-agnostic `py3-none-any` wheel -- its optional `face3d`
Cython/C++ extension (not needed for plain FaceAnalysis detection+embedding)
is opt-in and was not installed here -- and onnxruntime-gpu 1.29.0 ships an
official cp314 win_amd64 wheel too. Both installed and were confirmed
working end to end (including real CUDAExecutionProvider use, see
build_face_analysis() below) directly in this project's existing primary
3.14 venv -- no second Python install was needed. So this module lives in
src/ and is invoked via `main.py detect-faces`, exactly like every other
phase, NOT a standalone script in a separate environment as originally
planned. Rule 13 is kept in CLAUDE.md for historical context and because a
*future* dependency could hit the same kind of wheel gap insightface used to
have -- the two-environment pattern is still the right answer if that
happens again, it just wasn't needed here. See README.md for the (now much
simpler) invocation instructions.

Scope: Session 1 ONLY (see TODO.md's Phase 3 section). This module does
detection + embedding extraction and nothing else. It does NOT cluster
faces, does NOT write to the `photos` table (or anywhere in the DB -- purely
read-only, matching gps_backfill.py's/thumbnail_backfill.py's "SELECT
existing rows, write to a separate output" shape), and does NOT build the
faces/people tables or any labeling UI -- that's Session 2+, deliberately
locked out of this session.

Photos only, no video (matching Phase 2's captioning scope decision -- see
photo-organizer-spec.md's Phase 3 section).

Output shape (data/faces.jsonl): one line per DETECTED FACE, not one line
per photo -- a photo with 0 faces contributes 0 lines, a photo with 3 faces
contributes 3 lines. Fields: file_hash, path, bbox ([x1, y1, x2, y2] in
pixel coordinates), embedding (512-dim ArcFace vector -- insightface's own
L2-normalized `normed_embedding`, the form its documentation recommends for
cosine-similarity comparison, which is what Session 2's clustering will
use), det_score (RetinaFace's own detection confidence, 0-1), detected_at.
det_score is one field beyond the spec's minimum list -- a documented
judgment call, same precedent as caption.py's extra file_size/file_mtime
fields: it's already computed for free by the same forward pass and is
genuinely useful for Session 2 to filter low-confidence detections before
clustering, so there's no reason to throw it away.

Resumability (CLAUDE.md rule 1): since a photo can produce zero face lines,
faces.jsonl's own contents can't answer "has this photo been scanned yet"
by itself -- a photo with 3 real faces and a photo never scanned at all
look identical from a *third* photo's perspective either way, but a 0-face
photo is indistinguishable from a never-scanned one purely by absence from
the file. A second, small sidecar file (data/faces_scanned.jsonl) is the
actual resumability checkpoint: one line per photo actually scanned,
whatever it found (including 0 faces). This solves the exact same "0 is a
valid, final answer, not the same as unknown" problem gps_checked solves for
GPS extraction -- just JSONL-based here instead of a DB column, since
Session 1 is deliberately not touching the DB schema yet (that's Session
2's job, see TODO.md).

Simplification worth naming explicitly: unlike thumbnail_backfill.py's
<hash>.failed marker for a permanently-undecodable video, a face-detection
error here does NOT get any kind of "permanent failure" marker -- an
unreadable/corrupt photo is simply left out of faces_scanned.jsonl and
retried on every future run, same as gps_backfill.py's plain "missing file"
case. Good enough for Session 1's benchmark-and-decide scope; worth
revisiting if a real full run turns up files that fail the same way on
every retry (see TODO.md).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image

from .config import Config

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None


DEFAULT_DET_SIZE = (640, 640)


@dataclass
class FaceDetectionStats:
    scanned: int = 0
    already_scanned: int = 0
    faces_found: int = 0
    photos_with_faces: int = 0
    photos_with_no_faces: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"scanned={self.scanned} already_scanned={self.already_scanned} "
            f"faces_found={self.faces_found} photos_with_faces={self.photos_with_faces} "
            f"photos_with_no_faces={self.photos_with_no_faces} errors={self.errors}"
        )


def _load_scanned_hashes(scanned_path: Path) -> set[str]:
    """Mirrors caption.py's _load_resume_state -- a corrupt/incomplete
    trailing line (e.g. a crash mid-write) is skipped, not fatal."""
    scanned: set[str] = set()
    if not scanned_path.exists():
        return scanned
    with open(scanned_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            file_hash = obj.get("file_hash")
            if file_hash:
                scanned.add(file_hash)
    return scanned


def _load_image_bgr(path: Path) -> np.ndarray:
    """Loads any supported format (JPG/PNG/HEIC) uniformly via Pillow, same
    reasoning as caption.py's _load_image_as_jpeg_bytes -- HEIC needs
    pillow_heif's opener, cv2.imread doesn't understand it directly. Returns
    a BGR uint8 array (the channel order insightface/OpenCV expect) --
    Pillow decodes as RGB, so the channel axis is reversed, not swapped
    byte-by-byte."""
    with Image.open(path) as img:
        img = img.convert("RGB")
        rgb = np.asarray(img)
    return np.ascontiguousarray(rgb[:, :, ::-1])


def _register_cuda_dll_dirs(logger: logging.Logger) -> None:
    """Windows-only fix for a real failure caught this session: onnxruntime-
    gpu's CUDA execution provider DLL (onnxruntime_providers_cuda.dll)
    dynamically depends on cuBLAS/cuDNN DLLs that pip's nvidia-* packages
    (onnxruntime-gpu[cuda,cudnn], see requirements.txt) install under
    site-packages/nvidia/.../bin/ -- but nothing puts those directories on
    the DLL search path automatically. Without this, onnxruntime silently
    falls back to CPUExecutionProvider with no exception raised anywhere
    (confirmed: LoadLibrary error 126, "The specified module could not be
    found", for cublasLt64_13.dll) -- insightface's own default logger
    severity swallows that underlying error entirely, so the ONLY reason
    this was caught at all is build_face_analysis()'s explicit "confirm the
    provider actually in use" check below, not an exception anywhere.

    Prepending these directories to the PATH environment variable is the
    fix that was actually verified to work this session -- NOT
    os.add_dll_directory(), which was tried first and did NOT resolve the
    load failure (onnxruntime's own DLL loading for this provider does not
    appear to use the LOAD_LIBRARY_SEARCH_* flags AddDllDirectory relies on,
    so it falls through to classic search order, which does include PATH).
    Modifying os.environ["PATH"] from within the running process is
    sufficient on Windows: the OS loader re-reads PATH at LoadLibrary time,
    it is not cached at process start.

    Paths are discovered via the `nvidia` namespace package rather than
    hardcoded, so this doesn't silently stop working if a future
    onnxruntime-gpu upgrade bumps the CUDA major version (nvidia-cublas/
    -cuda-runtime/-cufft/-curand/-cuda-nvrtc all install under a
    nvidia/cu<major>/bin/<arch>/ folder named after the CUDA major version;
    nvidia-cudnn-cu13's own DLLs install under the version-independent
    nvidia/cudnn/bin/ regardless). The glob is scoped to .../bin/* so it
    only ever matches directories (each CUDA-toolkit package's arch
    subfolder), never individual files, even though "cudnn" itself also
    matches the "cu*" prefix -- cudnn's own bin/ has no further subfolder
    nesting, so cu*/bin/* only picks up its individual DLL files there,
    which the is_dir() filter below correctly drops (cudnn/bin itself is
    added separately, explicitly, right after)."""
    if sys.platform != "win32":
        return
    try:
        import nvidia
        base = Path(next(iter(nvidia.__path__)))
    except (ImportError, StopIteration):
        return
    dirs = [d for d in base.glob("cu*/bin/*") if d.is_dir()]
    cudnn_dir = base / "cudnn" / "bin"
    if cudnn_dir.is_dir():
        dirs.append(cudnn_dir)
    if not dirs:
        logger.warning(
            "gpu_enabled but no nvidia-*/cu*/bin/<arch> or nvidia/cudnn/bin directory found -- "
            "onnxruntime-gpu[cuda,cudnn] may not be installed (see requirements.txt / README.md)."
        )
        return
    for d in dirs:
        os.add_dll_directory(str(d))  # cheap, harmless belt-and-suspenders -- PATH below is the fix that's confirmed to matter
    os.environ["PATH"] = os.pathsep.join(str(d) for d in dirs) + os.pathsep + os.environ.get("PATH", "")
    logger.info(f"Registered {len(dirs)} CUDA/cuDNN DLL director(ies) for onnxruntime (via PATH): {[str(d) for d in dirs]}")


def build_face_analysis(gpu_enabled: bool, logger: logging.Logger, model_pack: str = "buffalo_l"):
    """Builds and prepares insightface's FaceAnalysis app.

    Explicitly tries CUDAExecutionProvider first when gpu_enabled, then logs
    which provider onnxruntime actually ended up using for every sub-model.
    This matters because onnxruntime's CUDA execution provider can silently
    fall back to CPU (missing DLL, VRAM exhausted, etc.) without raising --
    CLAUDE.md/TODO.md are explicit that GPU usage must be *confirmed*, never
    assumed, and "we asked for CUDA" is not the same fact as "we got CUDA".
    """
    if gpu_enabled:
        _register_cuda_dll_dirs(logger)

    from insightface.app import FaceAnalysis

    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if gpu_enabled else ["CPUExecutionProvider"]
    app = FaceAnalysis(name=model_pack, providers=providers)
    ctx_id = 0 if gpu_enabled else -1
    app.prepare(ctx_id=ctx_id, det_size=DEFAULT_DET_SIZE)

    actual_providers = {name: model.session.get_providers() for name, model in app.models.items()}
    logger.info(f"insightface FaceAnalysis providers actually in use: {actual_providers}")
    if gpu_enabled and not all("CUDAExecutionProvider" in p for p in actual_providers.values()):
        logger.warning(
            "GPU was requested but at least one insightface sub-model is NOT running on "
            "CUDAExecutionProvider -- it silently fell back to CPU. Throughput from this run "
            "does not reflect real GPU performance; do not use it for a full-run estimate."
        )
    return app


def run_face_detection(
    cfg: Config,
    conn: sqlite3.Connection,
    logger: logging.Logger,
    faces_path: Path,
    scanned_path: Path,
    progress_cb: Callable[[int, int], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
    folder_prefix: str | None = None,
    app: Any = None,
) -> FaceDetectionStats:
    """Scans `photos` rows (photos only -- video excluded, see module
    docstring) not yet recorded in scanned_path, runs face detection +
    embedding extraction on each, and appends one faces_path line per
    detected face plus one scanned_path line per photo scanned.

    folder_prefix (used by `--limit`, same spirit as caption.py's/
    gps_backfill.py's) restricts to rows whose current_path starts with the
    given prefix -- lets a real run (or a benchmark) be tried against one
    small folder first.

    app: an already-built FaceAnalysis instance (see build_face_analysis) --
    built here if not supplied. Exposed as a parameter so a benchmark script
    or test can build it once and reuse it across multiple calls/samples
    without repeating the (comparatively slow) model-load step each time.
    """
    stats = FaceDetectionStats()
    video_exts = cfg.video_extensions_normalized

    if app is None:
        app = build_face_analysis(cfg.gpu_enabled, logger, cfg.face_model_pack)

    scanned_hashes = _load_scanned_hashes(scanned_path)
    logger.info(f"Resuming: {len(scanned_hashes)} photo(s) already scanned in {scanned_path}")

    # Parenthesized deliberately -- SQL's AND binds tighter than OR, so an
    # unparenthesized "NOT a OR b OR c AND d" would silently apply the
    # folder_prefix restriction to only the last OR branch (the exact bug
    # thumbnail_backfill.py's own tests caught for the inverse video-
    # inclusion clause -- see that module's docstring). Here it's the
    # inverse of that clause: exclude video extensions instead of requiring
    # one.
    if video_exts:
        exclude_clause = "NOT (" + " OR ".join(["filename LIKE ?"] * len(video_exts)) + ")"
        params: list[str] = [f"%{ext}" for ext in sorted(video_exts)]
    else:
        exclude_clause = "1=1"
        params = []
    query = f"SELECT file_hash, current_path FROM photos WHERE {exclude_clause}"
    if folder_prefix:
        query += " AND current_path LIKE ?"
        params.append(f"{folder_prefix}%")
    rows = conn.execute(query, params).fetchall()
    total = len(rows)
    logger.info(
        f"Face detection: {total} photo(s) found"
        + (f" under {folder_prefix}" if folder_prefix else "")
    )

    faces_path.parent.mkdir(parents=True, exist_ok=True)
    scanned_path.parent.mkdir(parents=True, exist_ok=True)
    faces_f = open(faces_path, "a", encoding="utf-8")
    scanned_f = open(scanned_path, "a", encoding="utf-8")
    since_flush = 0
    try:
        for file_hash, current_path in rows:
            if stop_check is not None and stop_check():
                logger.info(f"STOPPED by user request after {stats.scanned}/{total}.")
                break
            stats.scanned += 1

            if file_hash in scanned_hashes:
                stats.already_scanned += 1
                if progress_cb is not None:
                    progress_cb(stats.scanned, total)
                continue

            path = Path(current_path)
            try:
                if not path.exists():
                    stats.errors += 1
                    logger.error(f"ERROR (face detection): file missing at recorded path {path} — will retry next run")
                    if progress_cb is not None:
                        progress_cb(stats.scanned, total)
                    continue

                img_bgr = _load_image_bgr(path)
                faces = app.get(img_bgr)
                detected_at = datetime.now().isoformat()
                for face in faces:
                    record = {
                        "file_hash": file_hash,
                        "path": str(path),
                        "bbox": [float(x) for x in face.bbox],
                        "embedding": face.normed_embedding.tolist(),
                        "det_score": float(face.det_score),
                        "detected_at": detected_at,
                    }
                    faces_f.write(json.dumps(record, ensure_ascii=False) + "\n")

                stats.faces_found += len(faces)
                if faces:
                    stats.photos_with_faces += 1
                else:
                    stats.photos_with_no_faces += 1

                scanned_f.write(json.dumps({
                    "file_hash": file_hash,
                    "path": str(path),
                    "faces_found": len(faces),
                    "scanned_at": detected_at,
                }, ensure_ascii=False) + "\n")
                scanned_hashes.add(file_hash)
                logger.info(f"OK FACE-SCAN hash={file_hash[:12]} {path} -> {len(faces)} face(s)")
            except Exception as e:  # unreadable/corrupt image, unexpected decode/inference failure, etc.
                stats.errors += 1
                logger.error(f"ERROR (face detection) {path}: {e!r}")

            since_flush += 1
            if since_flush >= cfg.batch_size:
                faces_f.flush()
                os.fsync(faces_f.fileno())
                scanned_f.flush()
                os.fsync(scanned_f.fileno())
                logger.info(f"CHECKPOINT: flushed to disk after {stats.scanned}/{total} scanned this run")
                since_flush = 0

            if progress_cb is not None:
                progress_cb(stats.scanned, total)
    finally:
        faces_f.flush()
        os.fsync(faces_f.fileno())
        faces_f.close()
        scanned_f.flush()
        os.fsync(scanned_f.fileno())
        scanned_f.close()

    logger.info(f"FACE DETECTION RUN COMPLETE: {stats.summary()}")
    return stats
