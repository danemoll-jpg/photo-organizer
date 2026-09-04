# CLAUDE.md — Photo Organizer Project

## What this project is
A local tool to organize ~25 years of family photos on a Windows PC. It has multiple phases, each independently useful and independently testable. Build and validate them one at a time, in order — do not jump ahead to Phase 2 or 3 until Phase 1 is working and confirmed by the user.

**Structural note:** Phase 1 originally shipped as a CLI (`main.py` with subcommands). It now also has a desktop dashboard (`dashboard.py`, Tkinter) that wraps the same underlying code (`src/organize.py`, `src/pick_sources.py`, `src/config.py`, `src/db.py`, `src/logging_setup.py`) — the CLI was not replaced or removed, both remain fully usable. Launch via `Launch Dashboard.bat` (repo root) or the "Photo Organizer" Desktop shortcut — both run `dashboard.py` through `pythonw.exe` (no console window). See TODO.md for current scope and README.md for panel-by-panel usage.

## Environment
- Windows 11 Pro, Dell XPS 8930
- Intel i7-9700 (8 cores / 8 threads)
- NVIDIA GTX 1660 Ti, 6GB VRAM
- 64GB RAM
- Only Python 3.14 is installed (no other version via `py -0p`). It's new enough that GPU-framework wheels (torch, onnxruntime-gpu, insightface's deps) may lag — verify wheel availability for 3.14 early in whichever session picks the Phase 3 backend, don't assume it.
- GPU acceleration is available and should be used for Phase 3 (face detection/embedding). Driver-level check confirmed working (`nvidia-smi` shows the GTX 1660 Ti, WDDM driver, CUDA 13.3 runtime supported). Framework-level check (e.g. `torch.cuda.is_available()` or onnxruntime-gpu's equivalent) is still needed once Phase 3 picks a library — don't assume driver-level success means the framework will see the GPU too.
- `pywin32`'s shell wrapper does NOT expose `IFileDialog`/`IFileOpenDialog` for Python 3.14 — only the lower-level `IShellItem`/`IShellItemArray`. The native multi-select folder picker hand-declares the COM vtable interfaces via `comtypes` instead (see `src/folder_picker.py` docstring). Worth remembering if any future COM/shell work comes up.

## Project state
- Phase 0 and Phase 1 core pipeline, native multi-select folder picker, and the desktop dashboard are all built. See `README.md` for usage — don't re-derive commands from scratch.
- Testing so far has been against synthetic fixtures (`tests/make_sample_library.py`) plus real dry-runs against the user's actual photos. No real *execute* (copy-verify-delete) pass against real photos has happened yet.
- The native multi-select picker dialog's COM bindings are structurally verified, but a human hasn't fully click-through-tested the interactive `Show()` dialog itself in anger yet — non-blocking since there's an automatic tkinter fallback if native dialog creation fails.
- **Real dry-run result:** 7 of the user's scattered source folders — not counting `E:\Pics` itself — produced 100k+ files. Original ~35k estimate is obsolete. More source folders likely remain unpicked.
- **Destination-rescan performance issue: fixed.** `E:\Pics` no longer gets fully re-hashed every run. `photos` gained a `file_mtime` column + indexes on `current_path`/`original_path`; `organize.py` now checks path+size+mtime against the existing DB record before hashing, and only does a full hash for new/changed files. `init_db()` auto-migrates any existing DB (adds the column, backfills existing rows via `stat()` only — no re-hash) the next time it runs, so the very next real run against the user's actual 100k+-row DB will already be fast for previously-cataloged files. Tested against synthetic fixtures, including simulating a pre-fix DB to confirm the migration/backfill path itself. See TODO.md for full detail. One residual, pre-existing, unrelated-to-this-fix cost surfaced during testing: exact-hash duplicates that get skipped-and-left-in-place never receive a DB row, so they still get a full hash every rescan — flagged, not fixed, see TODO.md.
- Full-library real run is still deferred. Before it happens: (1) **backup is done** — WD Elements 2TB replacement drive, full backup copy confirmed complete. (2) user is leaning toward running the real copy-verify-delete pass folder-by-folder / in batches rather than all `source_folders` at once, given the 100k+ scale — no new code needed, just a decision. (3) A first real run was started against real source folder(s) and then deliberately cancelled (cleanly, no partial files) once the destination-rescan performance issue was identified — **the fix has now landed and been tested; the user will restart that same real run themselves through the dashboard** (Claude did not restart it — irreversible file operation, out of scope for Claude to initiate unprompted).
- Next session scope: TBD — likely candidates are a human click-through test of the native picker, and/or the actual real full-library run once the user restarts/completes their batched real runs. See TODO.md for exact status.

## Library facts (confirmed)
- Image formats actually in the library: **JPG, PNG, HEIC**. No RAW formats.
- `E:\Pics` (the destination root) is not a clean slate — it already contains a **mix** of files sorted into `YYYY/YYYY-MM` and loose/unsorted files at the top level. Any phase that touches `E:\Pics` should expect this mix, not assume it's empty or uniformly organized.
- Real scale is at least 100k+ files across just 7 of the user's scattered source folders, not the originally-estimated ~35k. Treat 35k as obsolete.

## Full spec
See `photo-organizer-spec.md` in this project for the detailed phase-by-phase requirements. That file is the source of truth for scope. TODO.md tracks current status and next steps.

## Data access notes
- Plex runs on this same PC and already has remote access set up for the user's daughter. Phase 4 (daughter-facing search) builds a companion web app alongside Plex on this machine — not a cloud-hosted rebuild. Don't propose migrating photos or the DB to the cloud unless the user asks; the whole point of this design is reusing infrastructure that already works.
- The user is very comfortable with MS Access and may want to use it as an optional query front-end to the SQLite DB via ODBC linked tables. This is for the user's own use, not the daughter's.

## Non-negotiable cross-cutting rules
1. **Resumability everywhere.** Every phase must be safely re-runnable without redoing work already done. Track processed files by content hash, not filename or path. (See the destination-rescan performance issue above — resumability shouldn't mean "safe but needlessly slow"; cheap pre-checks before a full hash are in scope for satisfying this rule well, not just technically.)
2. **Never silently overwrite or destroy original photos.** Phase 1 uses copy-verify-delete: copy to destination, verify via hash comparison, only then delete the source. A failed verification must leave the source in place and get logged — never delete on an unverified copy. Collision handling must never overwrite an existing file.
3. **Log everything.** Every phase needs a log the user can read after the fact to answer "what happened to file X and why." The dashboard's log viewer reads these same log files — don't create a second, separate logging path for the GUI.
4. **Config, not hardcoding.** Source/dest paths, backend choice (local model vs Claude API for captioning), batch sizes, and GPU on/off should live in a config file.
5. **JSONL for intermediate outputs** (Phase 2 captions, Phase 3 face embeddings) — appendable, resumable. SQLite is the final data layer, loaded from JSONL.
6. **Primary key = file content hash** across all DB tables, since paths will move during Phase 1 and duplicates/re-exports are likely across 25 years of photos.
7. **The GUI is a wrapper, not a parallel implementation.** The dashboard should call the same underlying functions/modules the CLI uses, not duplicate logic. `main.py`'s commands must keep working directly.
8. **At 100k+ files, favor batched/incremental real runs over one giant pass**, and favor cheap pre-checks (path/size/mtime) over full re-hashing wherever a file is very likely unchanged. Both the CLI and dashboard already support scanning a subset of `source_folders`, so folder-by-folder real runs are achievable without new code.

## Session model
- The user runs a **fresh Code session for each phase** (to keep individual sessions from ballooning). This means CLAUDE.md must always contain full standing context — don't assume memory of prior sessions.
- TODO.md is scoped to the current active phase only. Work only within that phase. Do not start, scaffold, or "get ahead" on later phases even if it seems efficient — the user and Claude (chat) review and re-scope TODO.md between sessions before the next phase begins.
- If something in a later phase is blocked or ambiguous because of a decision only that phase would resolve, flag it in your summary at the end of the session rather than jumping ahead to resolve it.
- **Always keep TODO.md current as work happens** — check items off as they're completed, add new sub-tasks if scope details get decided mid-session, and note any judgment calls made along the way so they're visible rather than buried in code.
- **Update CLAUDE.md too when something durable changes** — a structural decision, a new confirmed environment fact, or a new cross-cutting rule belongs here, not just in TODO.md. TODO.md is for task status; CLAUDE.md is for standing context that the next fresh session needs to know without being told again.
- End every session with a written summary: what was built/changed, what was tested and how, and anything the user or the next session should know. This is in addition to updating the files themselves, not instead of it.

## Working style
- The user works in IT and is comfortable with technical detail and code — no need to oversimplify or avoid technical terms. Surface real trade-offs plainly.
- Confirm scope and design questions before writing code, especially anything involving cost (cloud API calls) or irreversible file operations.
