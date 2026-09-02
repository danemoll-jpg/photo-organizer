# CLAUDE.md — Photo Organizer Project

## What this project is
A local tool to organize ~25 years of family photos (35k+ files, likely more now) on a Windows PC. It has multiple phases, each independently useful and independently testable. Build and validate them one at a time, in order — do not jump ahead to Phase 2 or 3 until Phase 1 is working and confirmed by the user.

**Structural note:** Phase 1 originally shipped as a CLI (`main.py` with subcommands). It now also has a desktop dashboard (`dashboard.py`, Tkinter) that wraps the same underlying code (`src/organize.py`, `src/pick_sources.py`, `src/config.py`, `src/db.py`, `src/logging_setup.py`) — the CLI was not replaced or removed, both remain fully usable. Launch with `venv\Scripts\python dashboard.py`. See TODO.md for current scope and README.md for the panel-by-panel usage.

## Environment
- Windows 11 Pro, Dell XPS 8930
- Intel i7-9700 (8 cores / 8 threads)
- NVIDIA GTX 1660 Ti, 6GB VRAM
- 64GB RAM
- Only Python 3.14 is installed (no other version via `py -0p`). It's new enough that GPU-framework wheels (torch, onnxruntime-gpu, insightface's deps) may lag — verify wheel availability for 3.14 early in whichever session picks the Phase 3 backend, don't assume it.
- GPU acceleration is available and should be used for Phase 3 (face detection/embedding). Driver-level check confirmed working (`nvidia-smi` shows the GTX 1660 Ti, WDDM driver, CUDA 13.3 runtime supported) — done in the Phase 0/1 session. Framework-level check (e.g. `torch.cuda.is_available()` or onnxruntime-gpu's equivalent) is still needed once Phase 3 picks a library — don't assume driver-level success means the framework will see the GPU too.

## Project state
- Phase 0 and Phase 1 core pipeline are built: venv + `requirements.txt`, `config.example.yaml`/`config.yaml`, `schema.sql` (full DB schema, all tables), the Phase 1 CLI (`main.py` + `src/`), and the desktop dashboard (`dashboard.py`). See `README.md` for setup/usage — don't re-derive commands from scratch, they're already documented there.
- Phase 1 (both CLI and dashboard) has been tested against synthetic fixtures only (`tests/make_sample_library.py`), not real photos yet. The full-library run against the real `E:\Pics`/`E:\Pics\fotos` is intentionally still deferred — the user wants to get comfortable with the tool via the dashboard on small/real test folders first.
- The source-folder picker (`src/pick_sources.py` + `src/folder_picker.py`) now does native multi-select via `IFileOpenDialog` (`FOS_PICKFOLDERS | FOS_ALLOWMULTISELECT`), is additive across runs, and no longer drills into the just-picked folder on reopen (defaults to its parent). **Caveat:** the COM interface bindings in `folder_picker.py` were structurally verified (interface creation, `SetOptions`/`GetOptions`/`SetFolder`/`GetFolder` round-trips all confirmed correct against the real Windows shell) but the interactive `Show()` dialog itself has not yet been clicked through by a human — do that before relying on it for a real run, and there's an automatic fallback to the old single-select tkinter dialog if native dialog creation fails outright, so it's non-blocking either way. New dependency: `comtypes` (pure-Python, no compiled-wheel version risk). `pywin32` was tried first and removed — its shell wrapper doesn't expose `IFileDialog`/`IFileOpenDialog` in the version available for Python 3.14, only the lower-level `IShellItem`/`IShellItemArray`.
- Next session scope: get the user's own hands-on confirmation of the native picker dialog and the dashboard's full flow (ideally against a small real folder, not just synthetic fixtures), then decide on the full-library run. See TODO.md for the exact checkpoint.

## Library facts (confirmed)
- Image formats actually in the library: **JPG, PNG, HEIC**. No RAW formats.
- `E:\Pics` (the destination root) is not a clean slate — it already contains a **mix** of files sorted into `YYYY/YYYY-MM` and loose/unsorted files at the top level. Any phase that touches `E:\Pics` should expect this mix, not assume it's empty or uniformly organized.
- The user's other source photos are scattered across folders/drives they haven't fully enumerated — they want to pick them interactively (folder picker, ideally multi-select) rather than list them upfront.

## Full spec
See `photo-organizer-spec.md` in this project for the detailed phase-by-phase requirements. That file is the source of truth for scope. TODO.md tracks current status and next steps.

## Data access notes
- Plex runs on this same PC and already has remote access set up for the user's daughter. Phase 4 (daughter-facing search) builds a companion web app alongside Plex on this machine — not a cloud-hosted rebuild. Don't propose migrating photos or the DB to the cloud unless the user asks; the whole point of this design is reusing infrastructure that already works.
- The user is very comfortable with MS Access and may want to use it as an optional query front-end to the SQLite DB via ODBC linked tables. This is for the user's own use, not the daughter's.

## Non-negotiable cross-cutting rules
1. **Resumability everywhere.** Every phase must be safely re-runnable without redoing work already done. Track processed files by content hash, not filename or path.
2. **Never silently overwrite or destroy original photos.** Phase 1 uses copy-verify-delete: copy to destination, verify via hash comparison, only then delete the source. A failed verification must leave the source in place and get logged — never delete on an unverified copy. Collision handling must never overwrite an existing file.
3. **Log everything.** Every phase needs a log the user can read after the fact to answer "what happened to file X and why." The dashboard's log viewer reads these same log files — don't create a second, separate logging path for the GUI.
4. **Config, not hardcoding.** Source/dest paths, backend choice (local model vs Claude API for captioning), batch sizes, and GPU on/off should live in a config file.
5. **JSONL for intermediate outputs** (Phase 2 captions, Phase 3 face embeddings) — appendable, resumable. SQLite is the final data layer, loaded from JSONL.
6. **Primary key = file content hash** across all DB tables, since paths will move during Phase 1 and duplicates/re-exports are likely across 25 years of photos.
7. **The GUI is a wrapper, not a parallel implementation.** The dashboard should call the same underlying functions/modules the CLI uses, not duplicate logic. `main.py`'s commands must keep working directly.

## Session model
- The user runs a **fresh Code session for each phase** (to keep individual sessions from ballooning). This means CLAUDE.md must always contain full standing context — don't assume memory of prior sessions.
- TODO.md is scoped to the current active phase only. Work only within that phase. Do not start, scaffold, or "get ahead" on later phases even if it seems efficient — the user and Claude (chat) review and re-scope TODO.md between sessions before the next phase begins.
- If something in a later phase is blocked or ambiguous because of a decision only that phase would resolve, flag it in your summary at the end of the session rather than jumping ahead to resolve it.
- **Always keep TODO.md current as work happens** — check items off as they're completed, add new sub-tasks if scope details get decided mid-session, and note any judgment calls made along the way (like the existing duplicate-handling note) so they're visible rather than buried in code.
- **Update CLAUDE.md too when something durable changes** — a structural decision (like the CLI→dashboard change), a new confirmed environment fact, or a new cross-cutting rule belongs here, not just in TODO.md. TODO.md is for task status; CLAUDE.md is for standing context that the next fresh session needs to know without being told again.
- End every session with a written summary: what was built/changed, what was tested and how, and anything the user or the next session should know. This is in addition to updating the files themselves, not instead of it.

## Working style
- The user works in IT and is comfortable with technical detail and code — no need to oversimplify or avoid technical terms. Surface real trade-offs plainly.
- Confirm scope and design questions before writing code, especially anything involving cost (cloud API calls) or irreversible file operations.
