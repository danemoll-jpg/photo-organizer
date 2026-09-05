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
- [x] **Backup completed** — first backup drive (Seagate) was DOA/crashed on first use and was returned; replacement (WD Elements 2TB) arrived and a full backup copy is confirmed done by the user. This unblocks the real run from a safety standpoint.
- [x] **Full real run (copy-verify-delete) completed successfully across the entire library** — the earlier cancelled run (which had been running the old slow destination-rescan behavior) was restarted by the user once the performance fix landed, and this time ran all the way through: both the user's externally-picked `source_folders` AND the implicit `E:\Pics` destination-scan. Confirmed via the `katyas phone2` investigation below (which was itself discovered mid-way through this real run) — 8,088 files genuinely copy-verify-deleted to the canonical top level, 2,814 exact-hash duplicates correctly left in place. Photo organization is DONE, not pending.
- [x] Run on full library, review log with user — **done, see above**. Superseded the earlier "deferred" note.
- [x] **Performance: destination folder (`E:\Pics`) was getting fully re-hashed on every run — FIXED.** `photos` table gained a `file_mtime` column plus indexes on `current_path`/`original_path`. `organize.py` now checks path+size+mtime against the existing DB record before hashing; only does a full hash for new/changed files. `init_db()` auto-migrates any existing DB (adds the column, backfills existing rows via `stat()` only, no re-hash) the next time it runs — so the user's next real run against their actual 100k+-row DB will already be fast for previously-cataloged files. Tested against synthetic fixtures, including a simulated pre-fix DB to confirm the migration/backfill path itself.
  - **Residual, separate, NOT fixed by this:** exact-hash duplicates that get skipped-and-left-in-place never receive a DB row, so they still get fully re-hashed on every rescan (no path/size/mtime record exists for them to check against). Minor compared to the main fix, but a real, distinct cost — flagged for a future session, not addressed here.
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

## ACTIVE: Phase 1b — Video File Organization (new scope, added after photos already organized)
User's photos are now organized via Phase 1 (full real run completed successfully, see above). Video files (MP4, MOV, AVI) were mixed into the same source folders the whole time and were never in scope for the original image-only scanner. See `photo-organizer-spec.md`'s "Phase 1b" section for full requirements. **Code complete and tested — awaiting user checkpoint confirmation, and a real run WITH video support is currently in progress as of this note (user is running it now; outcome not yet known).**
- [x] Extend scanner to recognize MP4, MOV, AVI alongside existing JPG/PNG/HEIC — new `video_extensions` config key (`src/config.py`), combined with `supported_extensions` via `Config.all_extensions_normalized` for the scan itself; per-file media-type dispatch (which resolver, which dest subfolder) happens in `src/organize.py::_process_one` via `video_extensions_normalized`. `scan_folders()` itself needed no change — already extension-set-driven, not format-aware.
- [x] Video destination: `<dest_root>/YYYY/YYYY-MM/Video/` — implemented in `organize.py`: dest_dir gets `/ "Video"` appended for a dated (non-unsorted) video. Unsorted videos share the same `_unsorted/needs_review` folder as unsorted photos (no separate bucket — not spec'd, kept simple).
- [x] Video date resolution — new `src/video_date_resolver.py`, chain = container creation-date metadata (`date_source=container`) → filename pattern → filesystem date → unsorted. **Backend: `hachoir`** (pure Python, no ffmpeg/MediaInfo binary needed, installs cleanly on Python 3.14). Filename-pattern and filesystem-date steps reused directly from `date_resolver.py` (imported, not reimplemented). Verified against real MOV/MP4 files from `E:\Pics\katyas phone2` — correctly extracted container creation dates matching hachoir's own plaintext metadata dump.
- [x] Reuse existing copy-verify-delete, hashing/dedup, collision handling, and logging as-is — confirmed zero changes needed to that logic; verified end-to-end that dedup, collision hash-suffixing, already-in-place recognition (now `Video`-subfolder-aware), and the fast-path pre-check all work correctly for video files.
- [x] Dashboard/CLI: same scan/dry-run/run flow, not a separate mode — confirmed, only the real-run confirmation dialog's wording changed ("photos" → "photos/videos", both main.py and dashboard.py). Photo-vs-video breakdown in results panel: skipped, as explicitly flagged nice-to-have/not-required.
- [x] **Explicitly do NOT extend Phase 2 (captioning) or Phase 3 (face detection) to video** — confirmed not touched, no placeholders added.
- [x] Test against real video files — done, see Testing notes below.
- [ ] **Checkpoint: confirm with user before this is marked done** — code complete and tested (synthetic fixtures + real container-metadata files + a real pythonw.exe-crash fix), needs the user's go-ahead. **A real run with video support is in progress right now (user-initiated) — its outcome is effectively the real-world confirmation this checkpoint needs.**

**New dependency:** `hachoir>=3.3` added to `requirements.txt` (runtime dependency, not dev-only). Installed and confirmed working on this machine's Python 3.14 venv.

**Bug caught and fixed during testing — same class as the earlier tqdm/pythonw crash:** hachoir's own internal warning/error logging writes directly to `sys.stdout`/`sys.stderr` unconditionally. Under `pythonw.exe` (how the dashboard launches), both are `None`, so ANY hachoir warning (routine — e.g. an unusual/corrupt video container, expected at this library's real-world scale) would have crashed the dashboard identically to the earlier tqdm bug. Fixed in `video_date_resolver.py` at import time: `hachoir.core.config.quiet = True` plus `hachoir.core.log.log.use_print = False` (quiet alone doesn't suppress LOG_ERROR-level messages, which hit the same crash path). Verified by simulating `pythonw.exe` against both a garbage-bytes fixture and real MOV/MP4 files — no crash either way, correct dates still returned. This would very likely have been hit on the first real dashboard run touching video, given how common "unusual container" warnings are across 25 years / many devices. **General pattern to check for with any future third-party library:** does it print/log to stdout/stderr on its own, and does it guard against those being `None`?

**Testing:**
- Extended `tests/make_sample_library.py` with video-side fixtures mirroring the existing photo cases (filename-pattern date, filesystem-date fallback, exact-hash duplicate, filename collision, already-correctly-placed video, loose video at dest root).
- Deliberately did NOT fake "container metadata found" synthetically — verified directly against 2 real MOV/MP4 samples from `E:\Pics\katyas phone2` instead, both under normal conditions and the simulated-pythonw.exe crash-fix check.
- Ran the full pipeline through dry-run, then a real copy-verify-delete run, then a second real run to confirm idempotency, against 15 synthetic fixtures (9 photo + 6 video): all outcomes matched expectations exactly, including the second run being a pure no-op with the fast-path pre-check correctly engaging for previously-sorted video files too.
- AVI: extension routing and graceful-fallback-on-unparseable-container confirmed via synthetic fixture. Did not find a real `.avi` file to test container-metadata extraction against specifically — not blocking, since hachoir dispatches by file signature internally, not extension, and MOV/MP4 already confirm that path works.

## RESOLVED — Investigate: was `E:\Pics\katyas phone2` ever actually processed?
This subfolder of `E:\Pics` already had its own pre-existing `YYYY\YYYY-MM` structure inside it BEFORE the tool ever touched it (confirmed by the user checking their backup — NOT something the organizer created, not a path-resolution bug). The question was whether this folder's files were ever scanned/organized by a real run.

**Answer: yes, and it worked correctly — discovered mid-way through the user's full real run (the one that ultimately completed successfully across the whole library, see Phase 1 above).**
- [x] Searched all `logs/organize_*.log` files for `katyas phone2`. First appeared in a dry run (`organize_20260902_010525.log`), which correctly planned to sort nearly everything in it out to the canonical top level — confirming the tool does NOT treat a pre-existing nested `YYYY-MM` structure as already-correct (only an exact match to the canonical `<dest_root>\YYYY\YYYY-MM\<filename>` path counts).
  - First **real** run to touch it: `organize_20260904_151258.log`. Cross-checked against the DB: **8,088 files** with `original_path` under `katyas phone2` are recorded `status='sorted'` with a `current_path` no longer under that folder — genuinely copy-verify-deleted out to `E:\Pics\YYYY\YYYY-MM\`. Confirmed on disk (0 rows still have `current_path` under `katyas phone2`).
  - **2,814 files** (2,813 JPG + 1 PNG) were logged `SKIP already-processed hash=...` — exact-hash duplicates of photos already sorted from elsewhere. Per existing policy (duplicates left in place, not deleted), these correctly stayed put — confirmed still on disk at `katyas phone2\YYYY-MM\...`, expected, not a bug. Per the known "duplicates never get a DB row" residual performance cost, these will keep getting rescanned-and-skipped on every future run.
  - Four later real runs (`185710`, `190534`, `191025`, `200218`) each re-found only that same residual 2,814-file set and correctly skipped them again — nothing regressed. (Minor incidental finding: in the `190534` run, each of those 2,814 files got logged twice at two timestamps within that one run — almost certainly `katyas phone2` was briefly listed as an explicit `source_folders` entry in addition to the always-implicit `dest_root` scan at that moment, causing an overlapping double-walk. Harmless — hash-based dedup makes it a no-op either way.)
- [x] Checked `config.yaml`: `katyas phone2` is **not** currently in `source_folders` — only ever reached via the always-implicit `dest_root` scan, as designed.
- [x] Read-only throughout — nothing moved, reorganized, or touched as part of investigating.
- **Bonus finding directly motivating Phase 1b**: a plain directory listing of `E:\Pics\katyas phone2` showed **8,650 files still sitting there** at investigation time: 2,813 JPG + 1 PNG (the known duplicates, expected), and **5,694 MOV + 4 MP4 + 138 AAE** — the video files Phase 1 was never scoped to touch (now handled by Phase 1b above), plus 138 iOS Photos `.aae` edit-sidecar files, which are neither a photo nor video format and aren't in scope for Phase 1b either. **Flagged, not yet decided:** does the user want `.aae` sidecar handling addressed in a future session (preserve alongside their photos? clean up?), or is it fine to just leave them untouched indefinitely?

## Open questions for the active phase (surface these, don't guess)
- Exact paths of the "other folders" scattered with photos — user wants to pick these interactively rather than list them now. Run `python main.py pick-sources`, or use the dashboard's "Add folder(s)..." button (opens the native multi-select folder-picker dialog — ctrl/shift-click several at once, asks "pick another batch?" between rounds) to populate `config.yaml`'s `source_folders`. Needs to be run at the physical machine (GUI dialog) — not something to drive remotely/headlessly.
- Real small-sample and full-library run + log review with the user — blocked on the above, and now also on the user trying the dashboard/native picker on a small real folder first (see session summary).

## Confirmed for the active phase
- Destination root: `E:\Pics`
- Folder structure: `E:\Pics\YYYY\YYYY-MM\` (year folder containing month subfolders, month subfolder named `YYYY-MM` — not a bare `MM`)
- Image formats: JPG, PNG, HEIC (no RAW). Video formats present: MP4, MOV (AVI supported defensively by Phase 1b, not yet confirmed present in real data) — Phase 1b code complete and tested; user is running a real pass with video support live right now, outcome not yet confirmed
- `E:\Pics` itself is a **mix** of already-organized (`YYYY/YYYY-MM`) and loose/unsorted files — not a clean slate. The tool always scans `dest_root` as an implicit source in addition to whatever's in `source_folders`, and recognizes files already sitting at their correct destination path (no-op, just a DB record — no copy/delete). **This full destination re-scan is expensive at 100k+ files and needs the path+size+mtime fast-path described above — flagged as a real performance issue, not just an implementation detail.**
- Duplicate handling: an exact-hash duplicate found under a *second* path is **skipped and left in place**, not deleted. Phase 1 does not clean up duplicate originals — only the copy-verify-delete of the first-seen instance is destructive, and only after verification. This was a judgment call (see session summary) — flag if you'd rather duplicates get cleaned up too once verified against the already-sorted copy.
- Source folders don't need manual removal/pruning after they're fully processed: copy-verify-delete empties them of files as it goes, so a re-scan of an already-processed folder is just a fast no-op (once the performance fix above lands — currently it's a *slow* no-op). This also means the user's intended long-term workflow — clear the historical backlog, then settle into one ongoing "dump new photos here" folder they periodically re-run the tool against — is already supported by the existing `source_folders` design once the list naturally narrows down to that one active folder. No special "steady-state mode" needs to be built.

---

## LOCKED — Phase 2: Content Tagging / Description
Do not start. Reference only.
- [x] **Backend decision made:** local model via Ollama, NOT Claude API. User chose free/slower/lower-quality over the paid options after seeing real cost estimates (~$100-560 depending on model, at 100k+ scale via Batch API). This is settled, not still open.
- [x] **Specific model recommendation, pending real-world confirmation once this phase starts:** `qwen3-vl:2b` (~1.9GB) as primary pick for the GTX 1660 Ti's 6GB VRAM, leaving real headroom for the vision encoder/processing overhead. `minicpm-v4.6` (~1.6GB) as a close alternative if qwen3-vl:2b underperforms in testing. Explicitly AVOID `llava:7b` despite it showing up in a lot of older guides/tutorials — it's stale/unmaintained at this point (no meaningful update in ~2 years) and offers a much smaller context window than current alternatives at a similar footprint. Whoever starts this phase should re-verify current Ollama library tags before pulling anything, since this space moves fast and this recommendation has a shelf life.
- [x] **Expected quality tradeoff, set correctly with the user already:** small local models will produce simple, functional tags (e.g. "beach, people, sunset") — good enough for search/filtering, not vivid/nuanced captions. This matches the user's actual use case (searchable tags via the Phase 4 dashboard/Plex integration), not a real loss for what this is for.
- Build captioning pipeline outputting JSONL (file_hash, path, caption, tags, date_taken, model_used, processed_at)
- Resume-by-hash, checkpointing every N images
- Test on sample, then run on full library
- Given local model + GPU: consider whether Ollama serving and Phase 1/1b's file operations can run concurrently on this machine without resource contention, or whether they should be sequenced — worth deciding once this phase actually starts, not guessed at now

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
- How the daughter currently authenticates/connects for Plex remote access — Phase 4
