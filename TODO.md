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
- [ ] `pick-sources` must be additive across runs: if `config.yaml` already has `source_folders` entries, running it again should add to that list (skipping exact duplicates already present), not overwrite it. User expects to run this again later as more old-photo locations are found, and eventually as their ongoing one-folder "dump and process" workflow (see note below). Confirm this is already the behavior; fix if not.
- [ ] Replace the current single-select folder dialog with true multi-select: use the `IFileOpenDialog` COM interface with `FOS_PICKFOLDERS | FOS_ALLOWMULTISELECT` (via `comtypes` or `pywin32`) so the user can ctrl/shift-click multiple sibling folders in one dialog instead of looping one-at-a-time. If that hits real friction, fall back to a custom Tkinter checkbox/tree list of a chosen parent directory's subfolders — but try native multi-select first.
- [ ] Whatever the picker mechanism ends up being, reopening it for another round should NOT default into the folder just added (forces the user to navigate back up and rescroll) — default the starting location to the PARENT of the last folder picked.
- [ ] Run on full library, review log with user — **deferred, not blocking this session**. User wants the picker fixes and dashboard done first, to get comfortable with how the tool behaves, before committing to the real full-library run. Do not treat this as required to close out this session.
- [ ] **Checkpoint: confirm with user before this phase is marked fully done** (picker fixes + dashboard can be confirmed as their own milestone even before the full run happens)

## ACTIVE: Phase 1 — Desktop Dashboard (replaces raw CLI for day-to-day use)
User wants a full simple desktop GUI rather than running CLI commands directly. Scope:
- [ ] **Source folder management panel** — view current `source_folders` list, add folders (via the multi-select picker being fixed above), remove a folder from the list
- [ ] **Dry-run button** — triggers the equivalent of `main.py scan`, displays results in the dashboard (counts by outcome: to-be-moved, already-sorted no-op, duplicates skipped, flagged-unsorted) rather than requiring the user to read raw console output
- [ ] **Run button** — triggers the real copy-verify-delete pass (`main.py run`), with a confirmation step before starting since it's a destructive-on-source operation
- [ ] **Progress indicator** — live status while a run is in progress (e.g. "1,204 / 35,000 processed"), not just a spinner, since full-library runs will take a while
- [ ] **Log viewer** — shows the current/most recent run's log inside the dashboard (tail + scrollback), so the user isn't opening log files manually in a text editor
- [ ] Framework choice: Tkinter is the natural fit (ships with Python, no extra install, matches the Windows-native picker work already in progress) — use it unless there's a strong reason not to; flag if choosing otherwise
- [ ] This is a GUI wrapper around the existing CLI/library code, not a rewrite — `main.py`'s commands should remain usable directly too, in case the user wants to script something later
- [ ] Test the full flow (add folders → dry-run → review → run → view log) end to end before reporting back

## Open questions for the active phase (surface these, don't guess)
- Exact paths of the "other folders" scattered with photos — user wants to pick these interactively rather than list them now. Run `python main.py pick-sources` (opens a native folder-picker dialog, one folder per pick, asks "add another?") to populate `config.yaml`'s `source_folders`. Needs to be run at the physical machine (GUI dialog) — not something to drive remotely/headlessly.
- Real small-sample and full-library run + log review with the user — blocked on the above.

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
