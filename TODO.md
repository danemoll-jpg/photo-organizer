# TODO — Photo Organizer

**Read this before doing anything:** only work within the phase marked `ACTIVE` below. Everything under `LOCKED` is out of scope for this session — don't scaffold it, don't touch it, even if it looks quick or related. This project runs one Code session per phase; the user and Claude (chat) re-scope this file between sessions once a phase is confirmed working. If something in a locked phase blocks or informs your current work, flag it in your end-of-session summary rather than acting on it.

Check items off as completed. Add sub-tasks as needed once implementation details get decided, but stay within the active phase.

---

## ACTIVE: Phase 0 — Setup
- [ ] Confirm image formats present in the actual photo library (jpg, heic, png, RAW variants?) with the user
- [ ] Confirm source folder(s) and desired destination root path
- [ ] Set up project structure, config file format, and dependency management (venv/requirements)
- [ ] Verify GPU/CUDA is usable from Python on this machine (needed later for Phase 3 — worth confirming early)
- [ ] Design SQLite schema (photos, tags, faces, people tables) keyed by file hash — full schema now, even though only `photos` is populated this early, so later phases don't require migrations

## ACTIVE: Phase 1 — Date-Based Folder Sort
- [ ] Build file scanner (recursive, filters to supported image formats)
- [ ] Implement date resolution chain: EXIF → filename pattern → filesystem date → flag as unsorted
- [ ] Implement content-hash-based dedup/skip-if-already-processed
- [ ] Implement copy-verify-delete: copy to destination, verify via hash comparison, delete source only after verification passes (leave source + log error if verification fails)
- [ ] Implement collision handling (never overwrite)
- [ ] Implement dry-run mode
- [ ] Implement logging (per-file outcome + date source used)
- [ ] Test on a small sample folder first
- [ ] Run on full library, review log with user
- [ ] **Checkpoint: confirm with user before this phase is marked done**

## Open questions for the active phase (surface these, don't guess)
- Exact photo file formats in the library

## Confirmed for the active phase
- Destination root: `E:\Pics`
- Folder structure: `E:\Pics\YYYY\YYYY-MM\` (year folder containing month subfolders, month subfolder named `YYYY-MM` — not a bare `MM`)

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
