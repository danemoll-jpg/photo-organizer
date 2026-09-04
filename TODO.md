# TODO — Photo Organizer

**Read this before doing anything:** only work within the phase marked `ACTIVE` below. Everything under `LOCKED` is out of scope for this session — don't scaffold it, don't touch it, even if it looks quick or related. This project runs one Code session per phase; the user and Claude (chat) re-scope this file between sessions once a phase is confirmed working. If something in a locked phase blocks or informs your current work, flag it in your end-of-session summary rather than acting on it.

Check items off as completed. Add sub-tasks as needed once implementation details get decided, but stay within the active phase.

---

## ACTIVE: Phase 0 — Setup
- [x] Confirm image formats present in the actual photo library — JPG + PNG + HEIC (no RAW)
- [x] Confirm desired destination root path — `E:\Pics` (already known before this session)
- [ ] Confirm exact source folder(s) — not enumerated yet. User wants to pick them interactively rather than list them upfront; the mechanism exists (`main.py pick-sources`) but hasn't been run
- [x] Set up project structure, config file format, and dependency management (venv/requirements) — see `README.md`
- [x] Verify GPU/CUDA is usable from Python on this machine — `nvidia-smi` confirms driver + GTX 1660 Ti visible (WDDM driver, CUDA 13.3 runtime supported). Framework-level check (torch/onnxruntime `cuda.is_available()`) deferred to Phase 3 since the backend isn't chosen yet — not needed for Phase 1 (no GPU work here)
- [x] Design SQLite schema (photos, tags, faces, people tables) keyed by file hash — see `schema.sql`, full schema created now

## ACTIVE: Phase 1 — Date-Based Folder Sort
- [x] Build file scanner (recursive, filters to supported image formats) — `src/scanner.py`
- [x] Implement date resolution chain: EXIF → filename pattern → filesystem date → flag as unsorted — `src/date_resolver.py`
- [x] Implement content-hash-based dedup/skip-if-already-processed — `src/hashing.py` + `src/db.py` (sha256, PK)
- [x] Implement copy-verify-delete: copy to destination, verify via hash comparison, delete source only after verification passes (leave source + log error if verification fails) — `src/organize.py`
- [x] Implement collision handling (never overwrite) — hash-suffixed filename on collision, `src/organize.py::_pick_available_name`
- [x] Implement dry-run mode — `main.py scan` (always) / `main.py run` (config-driven) / `run --dry-run` (forced)
- [x] Implement logging (per-file outcome + date source used) — `logs/organize_<timestamp>.log`
- [x] Test on a small sample folder first — synthetic fixtures in `tests/make_sample_library.py`, covering EXIF/filename/filesystem/unsorted date sources, HEIC, exact-hash duplicate, filename collision, an already-correctly-placed file, and a loose file at dest root. All verified working, including a simulated copy-verification failure (source left untouched, no partial file left behind) and re-run idempotency (second run = pure no-op).
- [x] `pick-sources` must be additive across runs — **confirmed already the behavior** (`pick_sources.py` merges `cfg.source_folders + picked`, de-duped, before saving) and now also covered by the dashboard's "Add folder(s)" path via the shared `merge_and_save()` helper.
- [x] Replace the current single-select folder dialog with true multi-select — done via `IFileOpenDialog` + `FOS_PICKFOLDERS | FOS_ALLOWMULTISELECT`, hand-declared through `comtypes` in `src/folder_picker.py` (pywin32's shell wrapper doesn't expose `IFileDialog`/`IFileOpenDialog` in the version available for Python 3.14, so the vtable interfaces are declared by hand — see that file's docstring). Falls back to the old tkinter single-select loop if the native dialog can't be created at all. **Note:** the vtable layout was structurally verified (interface creation + `SetOptions`/`GetOptions`/`SetFolder`/`GetFolder` round-trips all confirmed correct), but the actual `Show()` dialog needs a human to click through — not yet done, see session summary.
- [x] Reopen behavior fixed — each dialog round now opens at the PARENT of the last folder picked (tracked across rounds in one `pick-sources` run, and seeded from config.yaml's last entry on a fresh run), never inside the folder just added. Applies to both the native picker and the tkinter fallback.
- [ ] Run on full library, review log with user — **deferred, not blocking this session**. User wants the picker fixes and dashboard done first, to get comfortable with how the tool behaves, before committing to the real full-library run. Do not treat this as required to close out this session.
- [ ] **Checkpoint: confirm with user before this phase is marked fully done** — code is complete and tested against synthetic fixtures (see session summary), but the native multi-select dialog itself still needs a human to actually click through once before this is a settled milestone.

## ACTIVE: Phase 1 — Desktop Dashboard (replaces raw CLI for day-to-day use)
User wants a full simple desktop GUI rather than running CLI commands directly. Scope:
- [x] **Source folder management panel** — `dashboard.py`: Listbox of `source_folders`, "Add folder(s)..." (native multi-select picker), "Remove selected" (with confirmation, only forgets the folder as a scan source — never touches files)
- [x] **Dry-run button** — runs `organize.run_phase1()` with `dry_run=True` (same function `main.py scan` calls) in a background thread, results panel shows counts by outcome (would-sort / would-flag-unsorted / already-in-place-no-op / duplicates-skipped / errors)
- [x] **Run button** — runs `organize.run_phase1()` with `dry_run=False`, gated behind a custom confirmation dialog (lists source folders + dest, explains copy-verify-delete, requires clicking "Run for real...", not just Enter)
- [x] **Progress indicator** — determinate progress bar + "N / total processed" label, driven by a `progress_cb` callback added to `run_phase1()` (optional; `run_phase1()` itself no longer touches stdout/stderr — see bugfix note below — so both callers now pass one: the dashboard drives its Tk bar, `main.py` drives a `tqdm` bar)
- [x] **Bugfix: dashboard "Run" crashed immediately with `'NoneType' object has no attribute 'write'`** — `run_phase1()` used to wrap its file loop in `tqdm(...)` directly, and `tqdm` writes to `sys.stderr` by default. `Launch Dashboard.bat` runs `dashboard.py` via `pythonw.exe` (no console window by design), under which `sys.stdout`/`sys.stderr` are `None` — so the first `tqdm` render crashed the run. Fix: moved `tqdm` out of `src/organize.py` entirely; `run_phase1()` now only calls `progress_cb(done, total)` and is presentation-agnostic. `main.py` now builds its own `tqdm` bar off that same callback (previously it passed `progress_cb=None` and let `run_phase1()`'s internal `tqdm` render). Verified by simulating `pythonw.exe` (`sys.stdout = sys.stderr = None`) and re-running Phase 1 against the synthetic fixtures — no crash — plus a normal-stdout run confirming the CLI's `tqdm` bar still renders.
- [x] **Log viewer** — Combobox to pick any `logs/organize_*.log` file, tails the active run's log live (polls the file every 500ms), reads the exact same files `main.py` writes via the new shared `src/logging_setup.py` (no second logging path)
- [x] Framework choice: Tkinter, as expected — no change of plan needed
- [x] GUI wrapper, not a rewrite — `dashboard.py` calls `src.organize.run_phase1`, `src.pick_sources`, `src.config`, `src.db`, `src.logging_setup` directly; `main.py`'s commands are unchanged and still work standalone (re-verified this session after the shared refactor)
- [x] Also added a **Cancel** button (judgment call, not originally requested — see session summary): sets a stop flag `run_phase1()` checks *between* files, never mid-copy/verify, so cancelling a long real run can't leave a partial file behind
- [x] Test the full flow end to end — **done against synthetic fixtures**, not yet against real photos (see session summary for exactly what was and wasn't exercised)
- [x] Launcher (not originally scoped, added on request) — `Launch Dashboard.bat` in the repo root, plus a "Photo Organizer" Desktop shortcut, both run `dashboard.py` via `pythonw.exe` (no console window). The shortcut targets `venv\Scripts\pythonw.exe` directly with `dashboard.py` as its argument and the repo as its working directory.

## Open questions for the active phase (surface these, don't guess)
- Exact paths of the "other folders" scattered with photos — user wants to pick these interactively rather than list them now. Run `python main.py pick-sources`, or use the dashboard's "Add folder(s)..." button (opens the native multi-select folder-picker dialog — ctrl/shift-click several at once, asks "pick another batch?" between rounds) to populate `config.yaml`'s `source_folders`. Needs to be run at the physical machine (GUI dialog) — not something to drive remotely/headlessly.
- Real small-sample and full-library run + log review with the user — blocked on the above, and now also on the user trying the dashboard/native picker on a small real folder first (see session summary).

## Confirmed for the active phase
- Destination root: `E:\Pics`
- Folder structure: `E:\Pics\YYYY\YYYY-MM\` (year folder containing month subfolders, month subfolder named `YYYY-MM` — not a bare `MM`)
- Image formats: JPG, PNG, HEIC (no RAW)
- `E:\Pics` itself is a **mix** of already-organized (`YYYY/YYYY-MM`) and loose/unsorted files — not a clean slate. The tool always scans `dest_root` as an implicit source in addition to whatever's in `source_folders`, and recognizes files already sitting at their correct destination path (no-op, just a DB record — no copy/delete).
- Duplicate handling: an exact-hash duplicate found under a *second* path is **skipped and left in place**, not deleted. Phase 1 does not clean up duplicate originals — only the copy-verify-delete of the first-seen instance is destructive, and only after verification. This was a judgment call (see session summary) — flag if you'd rather duplicates get cleaned up too once verified against the already-sorted copy.
- Source folders don't need manual removal/pruning after they're fully processed: copy-verify-delete empties them of files as it goes, so a re-scan of an already-processed folder is just a fast no-op. This also means the user's intended long-term workflow — clear the historical backlog, then settle into one ongoing "dump new photos here" folder they periodically re-run the tool against — is already supported by the existing `source_folders` design once the list naturally narrows down to that one active folder. No special "steady-state mode" needs to be built.

---

## LOCKED — Phase 2: Content Tagging / Description
Do not start. Reference only.
- Decide captioning backend with user: local model (Ollama/LLaVA or similar) vs Claude API vs both
- If Claude API: estimate cost for full library size before running, get user sign-off
- Build captioning pipeline outputting JSONL (file_hash, path, caption, tags, date_taken, model_used, processed_at)
- Resume-by-hash, checkpointing every N images, rate limiting/retry if cloud API
- Test on sample, then run on full library

## LOCKED — Phase 3: Face Detection & Clustering
Do not start. Reference only.
- insightface (GPU) for detection + embedding, output JSONL (file_hash, path, bbox, embedding, detected_at)
- faiss ANN index + DBSCAN/HDBSCAN clustering
- Cluster-labeling review mechanism + merge/split tool
- Separate `people` table

## LOCKED — Phase 4: Daughter-Facing Search + Plex Deep Links
Do not start. Reference only.
- Map file_hash/path → Plex library item ID (prototype early once this phase starts — riskiest unknown)
- Filter/search web app (date, tags, people) against SQLite DB, read-only for daughter
- Deep-link into Plex per result
- Expose via same remote-access setup already used for Plex

## LOCKED — Data Layer, ongoing items beyond Phase 0
Do not start. Reference only.
- JSONL → SQLite loader script (needed once Phase 2 produces JSONL)
- (Optional) MS Access as ODBC-linked front end to the SQLite DB, for the user's own querying — user is proficient in Access

## Future open questions (not yet relevant — don't act on these early)
- Local model vs Claude API for captioning (cost tolerance) — Phase 2
- How the daughter currently authenticates/connects for Plex remote access — Phase 4
