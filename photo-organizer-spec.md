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

## Phase 1b — Video File Organization (added after initial Phase 1 rollout)

**Context:** Photos are already organized via Phase 1. Video files (MP4, MOV, AVI) were mixed in with the photo library the whole time and were never in scope for the original scanner (image-formats-only). This adds video files to the same organizing pass, without extending content analysis (Phase 2/3) to them.

**Goal:** Move/copy every video file into the same `YYYY/YYYY-MM/` structure Phase 1 already uses, but into a `Video` subfolder within each month folder, keeping them separate from photos.

**Requirements:**
- Extend the scanner to also recognize MP4, MOV, and AVI (in addition to the existing JPG/PNG/HEIC image formats).
- Destination structure for video: `<dest_root>/YYYY/YYYY-MM/Video/` — same year/month resolution as photos, just routed into a `Video` subfolder instead of directly into the month folder.
- **Date resolution chain for video is NOT the same as EXIF** — video files carry creation dates in their own container metadata (e.g. QuickTime/MP4 "creation time" atom), which needs a different metadata reader (e.g. `hachoir`, `pymediainfo`, or `ffprobe` if available). The overall chain shape is the same as Phase 1's, just with a video-appropriate first step:
  1. Video container creation-date metadata (format-appropriate — MP4/MOV vs AVI may need different readers)
  2. Filename pattern (same logic as Phase 1, e.g. `VID_20180304_...`)
  3. File system created/modified date — least reliable, flag as such
  4. If nothing usable: route to `_unsorted/needs_review/`, same as photos
- Copy-verify-delete, hashing/dedup, collision handling, and logging all reuse the exact same logic Phase 1 already built for photos — these are format-agnostic and should NOT be reimplemented separately for video.
- Dashboard and CLI should treat video as part of the same scan/dry-run/run flow, not a separate mode — same buttons, same log, same confirmation step. Results panel should be able to break down counts by photo vs. video if that's a natural, low-effort addition; not a hard requirement.

**Explicitly out of scope:**
- Phase 2 (content tagging/captioning) does NOT extend to video. Captioning a still photo and analyzing video content are different problems (which frame(s) to even examine), and this is not being designed or guessed at here.
- Phase 3 (face detection/clustering) does NOT extend to video, for the same reason.
- Both are open questions for a possible future phase, not something to build a placeholder for now.

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

## Phase 2b — Review/Spot-Check Tool (new, added while Phase 2's full run is in progress)

**Context:** Phase 2's full captioning run takes ~8-9 days. The user wants to spot-check progress periodically — see what's been captioned so far, browse photos with their captions/tags, and catch problems early rather than only discovering issues after the run finishes.

**Goal:** A standalone local tool (opens in the browser, not a dashboard tab) for scrolling through the library and reviewing what's been captured, live, while Phase 2 is still running.

**Requirements:**
- Standalone, separate from `dashboard.py` — the user explicitly wants this as its own tool, not another dashboard panel.
- Per-photo display: the image itself, its current file path (which folder/location it lives in post-organize), date taken, caption, tags, and GPS-derived location (see below).
- **Must work against a partially-completed library.** Photos not yet captioned should display clearly as "not yet captioned" (or similar), not error out or be silently skipped. As Phase 2 continues running in the background, re-loading/refreshing the tool should surface newly-captioned photos without restarting anything.
- **Placeholder space for people/faces (Phase 3), even though empty for now.** Phase 3 doesn't exist yet — this tool should have a reserved area/field for per-photo people/face info so it doesn't need a rebuild once Phase 3 lands, but it's fine (expected) for that area to show nothing / "not yet available" until then.
- **Pagination/filtering required at this scale** — 100k+ photos means never loading everything into one page. Support browsing/paging, and at least basic filtering (by date range and/or folder) so the user can jump to a specific area of the library rather than only scrolling linearly.
- **Photo viewer / slideshow mode.** Beyond just a browsable grid, the user wants to use this as an actual photo viewer: forward/back navigation buttons to step through photos one at a time, plus an auto-advance slideshow mode that changes photos automatically every few seconds. The interval must be fully configurable by the user (not a fixed hardcoded delay), with an obvious way to start/stop auto-advance and step manually at any time (manual navigation should pause auto-advance rather than fight with it). Navigation must work seamlessly across pagination boundaries — stepping "next" past the last photo on a page should transparently load the next page, not dead-end or require the user to manually re-paginate.
- Data source: should read live from whatever's most current — likely a combination of the SQLite DB (path/date, populated by Phase 1/1b) and `captions.jsonl` (caption/tags, written incrementally by the in-progress Phase 2 run) rather than requiring the user to manually re-run the JSONL→SQLite loader every time they want to check progress.

**New scope: GPS-based location extraction (not previously part of any phase).**
- Extract GPS EXIF coordinates from photos where present (most modern phone photos; expect very sparse-to-absent coverage on older/pre-GPS-era photos, e.g. 2004-era digicam shots).
- Convert raw coordinates to a human-readable place name via **offline/free reverse-geocoding** (no API key, no per-lookup cost, no internet dependency) — consistent with this project's existing local-only, free-tooling approach (Ollama for captioning, no cloud costs). A library-based offline solution (e.g. a city-level offline geocoding database) is preferred over a paid/online geocoding API.
- Video GPS support is a genuine open question — whether the existing video metadata reader reliably exposes GPS data from MOV/MP4 containers is unconfirmed. Investigate, but don't let this block the rest of the tool; graceful "no location available" for video is an acceptable fallback if it doesn't pan out easily.
- This needs a new DB field/column (or equivalent) to store extracted location data per photo, separate from the existing caption/tags fields.

**Explicitly out of scope for this addition:**
- Editing captions/tags from within this tool — read-only review, not an editing interface (not requested, don't build it unasked).
- Any daughter-facing considerations — this is the user's own personal review tool, unrelated to Phase 4's Plex-integrated daughter search feature, even though both will eventually browse similar data.

## Phase 2c — Viewer v2: Random Navigation + Filters (added after using Phase 2b)

**Context:** The user has been using the Phase 2b review tool and likes the viewer/slideshow. This adds real navigation and filtering depth now that tags/captions/GPS data actually exist to filter on.

**Requirements:**
- **Random button** in the photo viewer — jumps to a random photo within the currently active filtered set (not the whole library if a filter is applied).
- **Slideshow order toggle**: chronological (existing behavior, by `date_taken`) vs. random. Random mode should not simply repeat the same shuffled order every time it's started — a fresh randomization each time slideshow mode starts is the expected behavior unless the user says otherwise.
- **New filters**, in addition to the existing date-range/folder filters:
  - Tag filter (filter to photos having a specific tag or tags)
  - Caption keyword search (substring/keyword match against caption text)
  - GPS/location filter (e.g. by place name, or "has location" vs "no location")
  - Faces/people filter — Phase 3 doesn't exist yet, so this should be a visible-but-inert filter control (present in the UI, but yields no results / is clearly marked "not available yet") rather than omitted entirely, matching the existing "reserved placeholder" pattern already used for the per-photo people/faces display field.
- **Filters must apply consistently to BOTH the grid view AND slideshow/viewer navigation** — this is the important architectural point. `/api/nav`'s "next/prev matching row" logic must respect whatever filters (including the new ones) are currently active, the same way the grid's query already does. A user filtering to a specific tag and starting the slideshow should only see photos matching that tag, in either random or chronological order.
- Random navigation (both the button and random slideshow order) needs to work efficiently at 100k+ scale — avoid an approach that requires loading/shuffling the entire filtered result set into memory if it can be avoided (e.g. via an efficient random-row-matching-current-filter query), though a working-but-simple approach is acceptable to start if a more efficient one isn't obvious.

## Phase 2d — Remote/Shared Access (new — supersedes the original Plex-integration plan below)

**Context:** The user has been using `review_tool.py` and prefers it over Plex's own interface. Rather than building Phase 4 as originally scoped (a companion app deep-linking into Plex), the user wants `review_tool.py` itself to be the shared surface — password-gated, accessible from outside the home network, hosted from the user's own PC (with explicit awareness that if that PC is down, remote access is down too). **This section supersedes Phase 4's Plex-deep-link approach — Phase 4 below should be treated as superseded, not built as originally written, unless the user says otherwise.**

**Goal:** Let the user grant specific other people password-protected remote access to `review_tool.py` over the internet, safely, while keeping photos hosted locally on the user's own PC for now — but built in a way that doesn't lock out moving photo storage to the cloud later.

**Requirements:**

**Authentication:**
- A small local list of username+password pairs (stored in config — not a full signup/account-management system, not a database of users). Each person gets their own credential, not one shared password.
- Real session-based auth (login page, session cookie) — not HTTP Basic Auth stuffed in front of everything.
- Rate-limit login attempts (protect against brute-forcing a weak password).
- All of this must ride over HTTPS — see Cloudflare Tunnel below, which provides this.
- Every account currently gets full access to the whole library — no per-person restriction of *which* photos someone can see is being asked for right now; don't build that unasked, but keep the door open (e.g. don't hardcode an assumption that all users see everything, if it's cheap not to).

**Network exposure — Cloudflare Tunnel, NOT port-forwarding:**
- Set up a separate, independent Cloudflare Tunnel for this tool. Explicitly NOT reusing or depending on Plex's own remote-access mechanism (Plex Relay is proprietary/Plex-only; raw port-forwarding is the insecure pattern being deliberately avoided here). Fully independent of Plex either way — Plex's own setup is untouched, and neither tool's uptime affects the other.
- This gives a real HTTPS URL without opening any inbound port on the user's router.

**Storage abstraction (groundwork for future cloud migration, not a cloud migration itself):**
- Introduce a small storage-backend abstraction between the app and "where photo bytes actually live" — local disk today, but designed so a future swap to cloud object storage (S3, Backblaze B2, etc.) doesn't require a rewrite of the app logic. This is groundwork only — do NOT actually implement or integrate any cloud storage backend now, just don't hardcode local file-path assumptions throughout the app in a way that would make a future swap painful.
- This applies to `review_tool.py` primarily; if it naturally extends to how other phases reference photo paths without extra cost, fine, but don't go out of your way to refactor Phase 1/1b/2 for this — scope it to what Phase 2c/2d actually touch.

**Explicitly out of scope for this phase:**
- Actually migrating to cloud storage — that's a future, separate decision, not part of this build.
- Per-person visibility restrictions (see above) — full access for every account for now.
- Any onboarding/installer/packaging work related to the "give this to friends/family" longer-term direction — that's a separate future concern, not part of making remote access work for the user's own invited people today.

## Phase 2e — Small fixes: dashboard health indicator + video visibility in review_tool.py

**Dashboard "is the local app down" indicator:** The Remote Access panel's existing `review_tool.py` status check emphasizes warning about *multiple* stale processes, but doesn't clearly surface a simple "not running at all" state. Make it a proper three-state indicator: Running (exactly 1 process bound to the configured port) / Not Running (zero) / Warning: multiple instances (more than 1, existing behavior). This directly addresses a real support need — the user discovered via a live 502 error that "the site is down" actually means "the local app on my PC isn't running," and there was no dashboard-visible way to know that at a glance.

**Video visibility in `review_tool.py`:** Video rows already exist in the DB (same `organize.py`/`_process_one` pipeline as photos — hash, path, date, all populated) but are currently excluded from every review-tool view (grid, viewer, stats) by a judgment call made when video had zero chance of ever having a caption. Reverse this: include video rows in the grid/viewer/stats.
- Caption field for a video with none: show plainly as "No caption" rather than "Not yet captioned" (the latter wrongly implies a pending automated process that will eventually complete).
- Location: video GPS remains genuinely unavailable (confirmed absent from container metadata) — videos will always show "no location," which is correct/expected, not a bug to chase.
- The viewer needs to actually play video (a `<video>` element), not just display an `<img>` — the display area must switch based on file type. Existing nav/filter/random/slideshow mechanics should work uniformly across photos and video in the same filtered set.
- Grid thumbnails: real video-frame thumbnail extraction is a nice-to-have, not required for this pass — a generic video/play-icon placeholder in the grid is an acceptable v1; the real `<video>` player only needs to appear in the full viewer.
- Since this is the same `review_tool.py` the remote-access tunnel already serves, videos become part of the online/remote version automatically — no separate work needed for that.

## Phase 2f — Editable metadata in review_tool.py (breaks the tool's prior read-only guarantee, on purpose)

**Context:** `review_tool.py` has been structurally read-only this entire project (SQLite opened via `mode=ro`, enforced at the connection level, not just convention) — that was the right call before real authentication existed. Now that Phase 2d's login system is in place, the user wants real editing: captions, tags, location, date, and (once Phase 3 exists) people/faces — including creating captions for video, which never had an automated one to begin with. Every invited/authenticated user (not just the owner) can edit.

**Requirements:**

- **A separate, write-capable DB connection**, used ONLY by new edit endpoints. The existing read-only connection stays exactly as-is for every browsing/GET path — this is additive, not a relaxation of the existing read guarantee for normal use.
- **Editable fields, to start:** caption, tags (full replace-the-list is fine, doesn't need granular add/remove), location (a free-text override of `location_name`), date taken (with an explicit, visible caveat in the UI: editing this only changes the DB record — it does NOT move, rename, or re-sort the actual file; Phase 1's folder placement is untouched). Design the underlying mechanism generically enough that adding an edit path for people/faces once Phase 3 exists doesn't require re-architecting this — don't build the people/faces UI now (Phase 3 doesn't exist), just don't paint this into a corner.
- **Video captions are "create," not "edit"** — same mechanism, just starting from an empty/null value instead of overwriting an AI-generated one. Surface an "Add caption" affordance for videos using the same UI/endpoint as editing a photo's caption.
- **Manual edits must be permanently protected from future automated overwrites.** Once a user edits a field, no automated process should ever silently overwrite it again. Recommended mechanism: a dedicated audit-log table (e.g. `photo_edits`: file_hash, field_name, old_value, new_value, edited_by, edited_at) rather than one boolean-per-field column on `photos` — this both gives a genuine audit trail (useful with multiple invited editors) and gives any automated writer a single, generic place to check "has a human touched this specific field on this file" before writing.
- **`main.py load-captions` and `src/gps_backfill.py` must both be updated** to check this audit log before applying an automated write to caption/tags or location fields respectively, skipping (and logging as skipped-due-to-manual-edit) any field that already has a manual edit on record. This is a real behavior change to two already-shipped, already-tested pieces of code — test carefully that normal (non-edited) rows continue updating exactly as before, and only edited fields get skipped.
- **Security, since this is internet-facing and every invited user can now write, not just read:** new edit endpoints must sit behind the existing auth gate (already true for all non-login/logout/static routes) AND get CSRF protection matching the existing login/logout pattern. Validate against an explicit allow-list of editable field names server-side — never accept an arbitrary field name from the client. Reasonable length limits on free-text fields (caption, location override) to prevent abuse.
- **UI:** add real edit controls (e.g. an edit/pencil affordance next to caption/tags/location/date in the viewer) that submit via `fetch()` to the new endpoints and update the displayed value in place on success, with a clear error state on failure (don't silently swallow a failed edit).

**Explicitly out of scope for this phase:**
- Any actual Phase 3 people/faces editing UI — Phase 3 doesn't exist yet, this only needs to not block it later.
- Per-user edit permissions/roles — every authenticated user can edit everything, no distinction between the owner and invited guests for this pass.

## Phase 2g — Real video thumbnails in the grid (deferred nice-to-have, now requested)

**Context:** Phase 2e's video-in-grid work used a generic play-icon placeholder for video thumbnails, explicitly deferring real frame extraction as a nice-to-have. The user has now requested the real thing — a first-frame preview, matching what the viewer already shows "for free" via the native `<video>` element.

**Goal:** Extract and cache a real thumbnail image (first frame, or an early keyframe) for each video, served in the grid instead of the placeholder icon.

**Requirements:**
- **New dependency needed for actual frame decoding** — `hachoir` (already used for video date-metadata) does not decode frames, only metadata. Investigate `opencv-python` or `imageio`+`imageio-ffmpeg` as candidates — prefer whichever installs cleanly with bundled codec support and doesn't require a separately-installed system ffmpeg binary, consistent with this project's existing preference for self-contained Python dependencies. Verify Python 3.14 wheel availability before committing to one, same caution already applied to every other dependency in this project.
- **Follow the established backfill pattern** (same shape as GPS extraction and captioning): a batch operation that processes videos once, extracts a thumbnail, and caches it to disk (e.g. a `thumbnails/` directory, filename keyed by `file_hash`) — NOT a live per-request extraction on every grid page load. Real throughput should be benchmarked before running against the full library, same as Phase 2's captioning threughput was measured before committing to a full run — video frame decoding is likely slower than GPS's EXIF-read throughput, don't assume it'll be similarly fast.
- **CLI command + dashboard panel**, per rule 10 — same posture as the GPS Extraction panel (progress bar, results summary, log tail, resumable/safe to cancel).
- **Graceful fallback**: the grid must continue showing the existing play-icon placeholder for any video whose thumbnail hasn't been generated yet — same "handle partial completion gracefully" principle already applied to captions and GPS data. Never error or show a broken image for an unprocessed video.
- **A route (or extension of an existing one) in `review_tool.py`** to serve the cached thumbnail file for a given video's `file_hash`, falling back to the placeholder response if no cached thumbnail exists yet.
- Per rule 11: any session touching `review_tool.py` for this must restart it itself before ending.

**Explicitly out of scope:**
- Any thumbnail regeneration/re-extraction logic beyond a first pass (e.g. re-extracting if a video file changes) — not requested, don't build it.
- Extracting anything beyond a single representative frame (e.g. no animated/multi-frame preview) — a static first-frame image only.
