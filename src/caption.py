"""Phase 2: content tagging/captioning via a local vision model served by
Ollama (captioning_backend="local" — Claude API was explicitly decided
against for this project, see CLAUDE.md/TODO.md, and is not implemented
here).

For each already-organized photo (JPG/PNG/HEIC — video is explicitly out
of scope, see photo-organizer-spec.md's Phase 2 section), asks the model
for a one-sentence caption plus a handful of short search tags, and
appends one JSON object per line to captions_path (JSONL — appendable,
resumable, see CLAUDE.md rule 5). SQLite is not touched here: loading
captions.jsonl into the DB is a separate, not-yet-built loader (see
TODO.md's LOCKED Data Layer section) — deliberately out of scope for this
phase.

Resumability (CLAUDE.md rule 1): captions.jsonl IS the checkpoint, no DB
needed. Mirrors organize.py's two-tier check exactly (see db.find_by_path /
organize.py::_process_one) so a re-run over an already-captioned 100k+-file
library doesn't repeat the same destination-rescan performance mistake
Phase 1 already hit once: a path+size+mtime match skips the file with no
hash computed at all; only a miss falls through to a full content hash,
which is then checked against every hash already captioned (catches a
captioned file that got renamed/moved since, so it isn't recaptioned as if
new).
"""
from __future__ import annotations

import io
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import ollama
from PIL import Image
from pydantic import BaseModel

from .config import Config
from .date_resolver import resolve_date
from .hashing import hash_file
from .scanner import scan_folders

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None


class CaptionResult(BaseModel):
    caption: str
    tags: list[str]


# Kept short and generic on purpose -- CLAUDE.md already set the user's
# quality expectations correctly (a 2B/1B local model gives simple,
# functional tags for search/filtering, not vivid captions -- see Phase 2
# notes). Not config-driven like paths/batch sizes/model name (rule 4) --
# tuning wording is a code change, same posture as date_resolver.py's
# filename patterns being hardcoded rather than user-configurable.
_PROMPT = (
    "You are captioning a personal family photo for a searchable photo "
    "library. Write ONE concise, plain-language sentence describing what "
    "is actually visible (people, setting, activity, notable objects). "
    "Then give 3 to 8 short lowercase keyword tags useful for searching "
    "this photo later (subjects, place/setting, activity, season, "
    "objects). Do not guess names, dates, or locations you can't see. "
    "Respond with JSON only."
)


@dataclass
class CaptionStats:
    scanned: int = 0
    already_captioned: int = 0   # already in captions.jsonl (fast-path or full-hash match) -- skipped
    captioned: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"scanned={self.scanned} already_captioned={self.already_captioned} "
            f"captioned={self.captioned} errors={self.errors}"
        )


def _load_resume_state(captions_path: Path) -> tuple[dict[str, dict], set[str]]:
    """Reads whatever's already in captions.jsonl (if anything).

    Returns (by_path, known_hashes):
    - by_path: path string -> last-seen {file_hash, file_size, file_mtime},
      backing the fast path+size+mtime pre-check (see module docstring).
    - known_hashes: every file_hash captioned so far, checked even on a
      fast-path miss so a captioned file that got renamed/moved isn't
      recaptioned as if it were new (content is the real identity, per
      CLAUDE.md rule 6).

    A corrupt/incomplete trailing line (e.g. a crash mid-write) is skipped,
    not fatal -- every complete line before it is still honored.
    """
    by_path: dict[str, dict] = {}
    known_hashes: set[str] = set()
    if not captions_path.exists():
        return by_path, known_hashes
    with open(captions_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # last line of a prior crashed run -- ignore, not fatal
            file_hash = obj.get("file_hash")
            if not file_hash:
                continue
            known_hashes.add(file_hash)
            path = obj.get("path")
            if path and obj.get("file_size") is not None and obj.get("file_mtime") is not None:
                by_path[path] = {
                    "file_hash": file_hash,
                    "file_size": obj["file_size"],
                    "file_mtime": obj["file_mtime"],
                }
    return by_path, known_hashes


def _load_image_as_jpeg_bytes(path: Path, max_dimension: int) -> bytes:
    """Every image is normalized through Pillow to RGB JPEG bytes before
    being sent to Ollama, regardless of source format. Two reasons:
    (1) HEIC is a real format in this library (see CLAUDE.md) and vision
    models served by Ollama expect PNG/JPEG, not a HEIF container -- Pillow
    + pillow_heif (already a project dependency for Phase 1's EXIF/HEIC
    handling) decodes it uniformly like everything else.
    (2) Full-size 12MP+ phone photos are far larger than any vision
    encoder actually looks at; downscaling first (cfg.caption_max_dimension)
    keeps inference time/memory bounded without changing what the model
    can actually see."""
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()


def _preflight_check(cfg: Config, client: ollama.Client) -> None:
    """Fail fast with a clear message rather than discovering a dead
    server or a not-yet-pulled model after scanning 100k+ files."""
    try:
        local_models = {m.model for m in client.list().models}
    except Exception as e:
        raise RuntimeError(
            f"Could not reach Ollama server at {cfg.ollama_host}: {e!r}. "
            "Is Ollama installed and running? (winget install Ollama.Ollama "
            "installs it as a background service that starts automatically; "
            "if it's not running, launch the Ollama app once or run "
            "`ollama serve`.)"
        ) from e
    # Ollama always reports a tag (defaulting an untagged pull to
    # ":latest"), but a config value like "minicpm-v4.6" (no tag, same as
    # `ollama pull minicpm-v4.6`/`ollama run minicpm-v4.6`) is meant to
    # match that default -- compare with ":latest" implied on whichever
    # side lacks a tag, instead of requiring an exact string match.
    def _with_default_tag(name: str) -> str:
        return name if ":" in name else f"{name}:latest"

    wanted = _with_default_tag(cfg.ollama_model)
    if wanted not in {_with_default_tag(m) for m in local_models}:
        raise RuntimeError(
            f"Model '{cfg.ollama_model}' is not pulled locally "
            f"(found: {sorted(local_models) or 'none'}). Run: "
            f"ollama pull {cfg.ollama_model}"
        )


def _caption_one(path: Path, cfg: Config, client: ollama.Client) -> tuple[str, list[str]]:
    image_bytes = _load_image_as_jpeg_bytes(path, cfg.caption_max_dimension)
    options = {} if cfg.gpu_enabled else {"num_gpu": 0}
    response = client.chat(
        model=cfg.ollama_model,
        messages=[{"role": "user", "content": _PROMPT, "images": [image_bytes]}],
        format=CaptionResult.model_json_schema(),
        options=options,
        # qwen3-vl is a "thinking" model by default -- left enabled, it
        # burns many seconds per image reasoning silently before ever
        # emitting the JSON answer (confirmed: ~3x slower with it on, see
        # CLAUDE.md/TODO.md). Harmless no-op for non-thinking models like
        # minicpm-v4.6 (confirmed no error when passed) -- safe to always set.
        think=False,
    )
    # Quirk (confirmed against qwen3-vl:2b + Ollama 0.33.3, this project's
    # actual versions): with think=False AND a JSON `format` schema
    # together, the model's answer sometimes lands in message.thinking
    # instead of message.content, even though content is where it belongs
    # and where minicpm-v4.6 correctly puts it. Try content first (the
    # correct/expected field), fall back to thinking -- keeps this working
    # regardless of which model is configured or whether a future Ollama
    # version fixes the quirk.
    text = response["message"]["content"] or response["message"].get("thinking") or ""
    result = CaptionResult.model_validate_json(text)
    tags = [t.strip().lower() for t in result.tags if t.strip()]
    return result.caption.strip(), tags


def run_phase2(cfg: Config, logger: logging.Logger,
               progress_cb: Callable[[int, int], None] | None = None,
               stop_check: Callable[[], bool] | None = None,
               source_folders: list[Path] | None = None,
               max_retries: int = 2) -> CaptionStats:
    """Runs Phase 2 end to end: scan already-organized photos under
    source_folders (defaults to cfg.dest_root_path -- Phase 1's output),
    caption each one not already in captions.jsonl via Ollama, and append
    results. Mirrors run_phase1's progress_cb/stop_check shape (see
    organize.py) so CLI/dashboard callers can reuse the exact same pattern.

    Photo extensions only (cfg.extensions_normalized) -- video is never
    scanned here, matching photo-organizer-spec.md's explicit Phase 2
    scope. Never touches original files: read-only against the image,
    append-only against captions_path."""
    stats = CaptionStats()
    roots = source_folders if source_folders is not None else [cfg.dest_root_path]

    client = ollama.Client(host=cfg.ollama_host)
    _preflight_check(cfg, client)

    by_path, known_hashes = _load_resume_state(cfg.captions_path_abs)
    logger.info(f"Resuming: {len(known_hashes)} file(s) already captioned in {cfg.captions_path_abs}")

    logger.info("Scanning for photos to caption...")
    files = list(scan_folders(roots, cfg.extensions_normalized))
    total = len(files)
    logger.info(f"Found {total} candidate photo(s). Captioning with model={cfg.ollama_model}...")

    cfg.captions_path_abs.parent.mkdir(parents=True, exist_ok=True)
    out_f = open(cfg.captions_path_abs, "a", encoding="utf-8")
    since_flush = 0
    try:
        for path in files:
            if stop_check is not None and stop_check():
                logger.info(f"STOPPED by user request after {stats.scanned}/{total} files.")
                break
            stats.scanned += 1

            try:
                st = path.stat()
            except OSError as e:
                stats.errors += 1
                logger.error(f"ERROR statting {path}: {e}")
                if progress_cb is not None:
                    progress_cb(stats.scanned, total)
                continue

            cached = by_path.get(str(path))
            if cached and cached["file_size"] == st.st_size and cached["file_mtime"] == st.st_mtime:
                stats.already_captioned += 1
                if progress_cb is not None:
                    progress_cb(stats.scanned, total)
                continue

            try:
                file_hash = hash_file(path, cfg.hash_algorithm)
            except OSError as e:
                stats.errors += 1
                logger.error(f"ERROR hashing {path}: {e}")
                if progress_cb is not None:
                    progress_cb(stats.scanned, total)
                continue

            if file_hash in known_hashes:
                stats.already_captioned += 1
                by_path[str(path)] = {"file_hash": file_hash, "file_size": st.st_size, "file_mtime": st.st_mtime}
                if progress_cb is not None:
                    progress_cb(stats.scanned, total)
                continue

            dt, _source = resolve_date(path)

            caption, tags, model_error = None, None, None
            for attempt in range(max_retries + 1):
                try:
                    caption, tags = _caption_one(path, cfg, client)
                    break
                except Exception as e:  # Ollama down, model error, unreadable/corrupt image, etc.
                    model_error = e
                    if attempt < max_retries:
                        time.sleep(2 ** attempt)  # 1s, 2s, ... brief backoff -- local model, not a real cloud rate limiter
            if caption is None:
                stats.errors += 1
                logger.error(f"CAPTION FAILED {path}: {model_error!r} (skipped -- will retry next run, not recorded)")
                if progress_cb is not None:
                    progress_cb(stats.scanned, total)
                continue

            record = {
                "file_hash": file_hash,
                "path": str(path),
                "caption": caption,
                "tags": tags,
                "date_taken": dt.isoformat() if dt else None,
                "model_used": cfg.ollama_model,
                "processed_at": datetime.now().isoformat(),
                # Extra beyond the spec's minimum fields -- backs the
                # fast-path resume check above, same fields/reasoning as
                # photos.file_mtime in schema.sql. The future JSONL->SQLite
                # loader can ignore these; they're not part of the DB schema.
                "file_size": st.st_size,
                "file_mtime": st.st_mtime,
            }
            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            by_path[str(path)] = {"file_hash": file_hash, "file_size": st.st_size, "file_mtime": st.st_mtime}
            known_hashes.add(file_hash)
            stats.captioned += 1
            since_flush += 1
            logger.info(f"OK CAPTIONED hash={file_hash[:12]} {path} -> \"{caption}\" tags={tags}")

            if since_flush >= cfg.batch_size:
                out_f.flush()
                os.fsync(out_f.fileno())
                logger.info(
                    f"CHECKPOINT: flushed to disk after {stats.captioned} captioned this run "
                    f"({stats.scanned}/{total} scanned)"
                )
                since_flush = 0

            if progress_cb is not None:
                progress_cb(stats.scanned, total)
    finally:
        out_f.flush()
        os.fsync(out_f.fileno())
        out_f.close()

    logger.info(f"PHASE 2 RUN COMPLETE: {stats.summary()}")
    return stats
