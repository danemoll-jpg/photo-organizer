# Photo Organizer — Technical Spec

## Context / Goals
- ~25 years of family photos, 35k+ files (likely more now), volume has slowed recently.
- Target machine: Dell XPS 8930, Intel i7-9700 (8c/8t), NVIDIA GTX 1660 Ti (6GB VRAM), 64GB RAM, Windows 11 Pro.
- Three independent phases. Build and test them separately, in this order.

## Phase 1 — Date-Based Folder Sort

**Goal:** Move/copy every photo into `YYYY/MM/` based on when it was actually taken.

**Requirements:**
- Recursively scan one or more source folders for image files (jpg, jpeg, png, heic, tiff, and any RAW formats present — confirm with user what formats are in the library).
- Date resolution priority, in order, with the source recorded per file:
  1. EXIF `DateTimeOriginal` (or `DateTimeDigitized` as fallback)
  2. Filename pattern if it encodes a date (many phone/camera exports do, e.g. `IMG_20180304_...`)
  3. File system created/modified date (least reliable — flag these)
  4. If nothing usable: route to an `_unsorted/needs_review/` folder rather than guessing
- Destination structure: `<dest_root>/YYYY/YYYY-MM/` (matches the user's existing convention — top-level year folder containing month subfolders named `YYYY-MM`, not just `MM`). Preserve original filename; on collision, append a short hash or counter — never silently overwrite.
- **Copy-verify-delete workflow** (not a raw move): copy each file to its destination, verify the copy (hash comparison between source and destination), and only delete the source original after verification passes. If verification fails, leave the source untouched and log it as an error rather than deleting. This gets the end state the user wants (files relocated, not duplicated) without the risk of a plain move.
- Dry-run mode: print/log the planned moves without touching files first.
- Must be safely re-runnable: skip files already sorted (track by content hash, not just filename, since duplicates/re-exports are likely across 25 years and multiple devices).
- Log file: what happened to every file (destination, date source used, any errors), so mistakes are auditable/reversible.

**Out of scope for this phase:** any content analysis, dedup beyond exact-hash collisions.

## Phase 2 — Content Tagging / Description

**Goal:** For each photo, produce a caption/tags and store them in a structured, DB-loadable format.

**Requirements:**
- Two possible backends — spec should support both, chosen via config:
  - **Local vision model** (e.g. via Ollama running a vision-capable model such as LLaVA or Qwen-VL) — no per-image cost, runs on the GTX 1660 Ti, lower quality/nuance.
  - **Claude API** (vision) — higher quality descriptions, real dollar cost at scale (35k+ images), so should support batching, rate limiting, and a cost/token estimate before a full run.
- Output per photo: `{ file_hash, path, caption, tags[], date_taken, model_used, processed_at }`.
- Store as JSON Lines (one JSON object per line) — easy to append incrementally, easy to bulk-load into SQLite later. Avoid single giant JSON array (harder to append/resume safely).
- Must be resumable: skip any file hash already present in the output.
- Batch/checkpoint every N images (e.g. every 500) so a crash loses minutal work.
- Rate-limit and retry logic if using a cloud API.

## Phase 3 — Face Detection & Person Clustering

**Goal:** Detect faces, cluster them across the whole library (same person → same cluster) without pre-existing labels, then let the user assign names to clusters once.

**Requirements:**
- Face detection + embedding via `insightface` (GPU-accelerated, fits the 6GB VRAM budget) or equivalent.
- Store per detected face: `{ file_hash, path, bbox, embedding, detected_at }`.
- Clustering: use an approximate-nearest-neighbor approach (e.g. `faiss`) rather than naive all-pairs comparison — needed once face count gets into the tens of thousands. DBSCAN (or HDBSCAN) on top of the ANN index for actual grouping.
- Output: `cluster_id` per face, plus a small review UI or simple script (even just "here are 12 sample thumbnails from cluster 7, what's this person's name?") for the user to label clusters.
- Expect and design for imperfection: provide an easy way to merge two clusters or split a bad one later, rather than assuming one clustering pass is final. Kids' faces change a lot over 25 years — don't expect a toddler and teen version of the same person to auto-cluster together; that's an acceptable known limitation, not a bug to chase.
- People table: `{ cluster_id, assigned_name, notes }`, separate from the face-detection output so relabeling doesn't require re-running detection.

## Data Layer
- SQLite as the target DB. Phases 2 and 3 write JSONL as an intermediate/resumable format; a separate loader script ingests JSONL into SQLite tables (`photos`, `tags`, `faces`, `people`).
- Primary key across all tables: file content hash (not path — paths will move during Phase 1).
- **Optional front-end for the user (not for the daughter):** MS Access can connect to the SQLite DB as linked tables via ODBC, giving the user a familiar query/browse interface without SQLite's constraints ever becoming a bottleneck (Access isn't the actual data store, just a client).

## Phase 4 — Daughter-Facing Search + Plex Deep Links

**Goal:** Let the user's daughter filter/search the photo metadata (date, tags, people) and jump straight to the matching photo in Plex — without duplicating the photo library anywhere.

**Context:** Plex runs on the same PC as this whole pipeline, and the daughter already has remote access to Plex. This phase builds a small companion web app that sits alongside Plex on the same machine/network, rather than moving anything to the cloud.

**Requirements:**
- Lightweight local web app (e.g. a simple Flask/FastAPI app) that:
  - Queries the SQLite DB with filters (date range, tags, person/people present)
  - Returns matching photos with a direct link into Plex for each one
- Needs to resolve each photo's `file_hash`/path in the DB to its corresponding item in Plex's library, in order to build the deep link. Plex has a local API (via its own metadata database or the Plex Media Server API) that can be queried to map a file path to a Plex library item ID — this mapping step needs research/prototyping early in this phase, since it's the piece most likely to have surprises.
- Exposed to the daughter the same way Plex already is (same remote-access setup) — no separate hosting, no separate account system needed beyond what Plex already provides for her.
- Read-only for the daughter: she can search and view, not edit tags/people or modify the underlying DB.
- Out of scope for now: syncing/duplicating photos to any cloud storage; making this usable if the home PC is off.

## Cross-Cutting Requirements
- **Resumability is a hard requirement for every phase**, not a nice-to-have, given the volume and multi-day/multi-session nature of this project.
- Every phase logs enough to answer "what happened to file X and why" after the fact.
- Config file (not hardcoded) for: source/dest paths, which backend to use for captioning, batch sizes, GPU on/off.
- No phase should require re-processing the full library to pick up newly added photos — incremental runs only process new/changed files.

## Explicitly Out of Scope (for now)
- Automatic duplicate/near-duplicate detection beyond exact hash match.
- Editing/rotating/modifying original image files in any way.
- Any cloud upload/storage — everything stays local.
