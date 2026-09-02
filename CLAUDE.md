# CLAUDE.md — Photo Organizer Project

## What this project is
A local tool to organize ~25 years of family photos (35k+ files, likely more now) on a Windows PC. It has three phases, each independently useful and independently testable. Build and validate them one at a time, in order — do not jump ahead to Phase 2 or 3 until Phase 1 is working and confirmed by the user.

## Environment
- Windows 11 Pro, Dell XPS 8930
- Intel i7-9700 (8 cores / 8 threads)
- NVIDIA GTX 1660 Ti, 6GB VRAM
- 64GB RAM
- Only Python 3.14 is installed (no other version via `py -0p`). It's new enough that GPU-framework wheels (torch, onnxruntime-gpu, insightface's deps) may lag — verify wheel availability for 3.14 early in whichever session picks the Phase 3 backend, don't assume it.
- GPU acceleration is available and should be used for Phase 3 (face detection/embedding). Driver-level check confirmed working (`nvidia-smi` shows the GTX 1660 Ti, WDDM driver, CUDA 13.3 runtime supported) — done in the Phase 0/1 session. Framework-level check (e.g. `torch.cuda.is_available()` or onnxruntime-gpu's equivalent) is still needed once Phase 3 picks a library — don't assume driver-level success means the framework will see the GPU too.

## Project state
- Phase 0 and Phase 1 are built: venv + `requirements.txt`, `config.example.yaml`/`config.yaml`, `schema.sql` (full DB schema, all tables), and the Phase 1 CLI (`main.py` + `src/`). See `README.md` for setup/usage — don't re-derive commands from scratch, they're already documented there.
- Phase 1 has been tested against synthetic fixtures only (`tests/make_sample_library.py`), not real photos yet. The real small-sample and full-library run + user log review are still outstanding — see TODO.md.

## Library facts (confirmed)
- Image formats actually in the library: **JPG, PNG, HEIC**. No RAW formats.
- `E:\Pics` (the destination root) is not a clean slate — it already contains a **mix** of files sorted into `YYYY/YYYY-MM` and loose/unsorted files at the top level. Any phase that touches `E:\Pics` should expect this mix, not assume it's empty or uniformly organized.
- The user's other source photos are scattered across folders/drives they haven't fully enumerated — they want to pick them interactively (folder picker) rather than list them upfront. Don't assume a fixed source-folder list; the Phase 1 tool supports adding to it incrementally via `main.py pick-sources`.

## Full spec
See `photo-organizer-spec.md` in this project for the detailed phase-by-phase requirements. That file is the source of truth for scope. TODO.md tracks current status and next steps.

## Data access notes
- Plex runs on this same PC and already has remote access set up for the user's daughter. Phase 4 (daughter-facing search) builds a companion web app alongside Plex on this machine — not a cloud-hosted rebuild. Don't propose migrating photos or the DB to the cloud unless the user asks; the whole point of this design is reusing infrastructure that already works.
- The user is very comfortable with MS Access and may want to use it as an optional query front-end to the SQLite DB via ODBC linked tables. This is for the user's own use, not the daughter's.

## Non-negotiable cross-cutting rules
1. **Resumability everywhere.** Every phase must be safely re-runnable without redoing work already done. Track processed files by content hash, not filename or path.
2. **Never silently overwrite or destroy original photos.** Phase 1 uses copy-verify-delete: copy to destination, verify via hash comparison, only then delete the source. A failed verification must leave the source in place and get logged — never delete on an unverified copy. Collision handling must never overwrite an existing file.
3. **Log everything.** Every phase needs a log the user can read after the fact to answer "what happened to file X and why."
4. **Config, not hardcoding.** Source/dest paths, backend choice (local model vs Claude API for captioning), batch sizes, and GPU on/off should live in a config file.
5. **JSONL for intermediate outputs** (Phase 2 captions, Phase 3 face embeddings) — appendable, resumable. SQLite is the final data layer, loaded from JSONL.
6. **Primary key = file content hash** across all DB tables, since paths will move during Phase 1 and duplicates/re-exports are likely across 25 years of photos.

## Session model
- The user runs a **fresh Code session for each phase** (to keep individual sessions from ballooning). This means CLAUDE.md must always contain full standing context — don't assume memory of prior sessions.
- TODO.md is scoped to the current active phase only. Work only within that phase. Do not start, scaffold, or "get ahead" on later phases even if it seems efficient — the user and Claude (chat) review and re-scope TODO.md between sessions before the next phase begins.
- If something in a later phase is blocked or ambiguous because of a decision only that phase would resolve, flag it in your summary at the end of the session rather than jumping ahead to resolve it.

## Working style
- The user works in IT and is comfortable with technical detail and code — no need to oversimplify or avoid technical terms. Surface real trade-offs plainly.
- Confirm scope and design questions before writing code, especially anything involving cost (cloud API calls) or irreversible file operations.
- Update TODO.md as work progresses so the user (and Claude in chat) can track status without reading code.
