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
- **"Hide from library" (new, added after the user asked whether they could delete an unwanted item directly).** NOT a physical file delete, and NOT a database row delete either — both are unsafe here. A physical delete removes an irreplaceable original photo permanently. A DB-row delete is subtler but still wrong: Phase 1's re-scan logic recognizes already-processed files by path/hash match against the `photos` table, so deleting the row while leaving the actual file in place would cause it to silently reappear the next time Phase 1 rescans `E:\Pics` (which happens on every run, automatically). Instead: add an `excluded` boolean column to `photos`. `review_tool.py` filters out any excluded row from every view (grid, viewer, random, slideshow, stats) — it disappears from the tool entirely. Phase 1/1b's resumability check is unaffected by this flag (still matches by path/size/mtime as before, doesn't touch or care about `excluded`), so the hide is permanent and won't be undone by a future rescan. The original file is never touched. Must be reversible — a way to view/un-hide excluded items (e.g. a "Show hidden" toggle), not a one-way action. Uses the same edit/audit infrastructure as the other editable fields above (field_name="excluded" is just another entry in the audit log).

**Explicitly out of scope for this phase:**
- Any actual Phase 3 people/faces editing UI — Phase 3 doesn't exist yet, this only needs to not block it later.
- Per-user edit permissions/roles — every authenticated user can edit everything, no distinction between the owner and invited guests for this pass.
- Actually deleting a file or a database row — see "Hide from library" above for why this is a flag, not a deletion, and don't build a real delete action even if requested casually later without a full re-discussion of the risk.

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

## Phase 2h — Bulk selection/editing in the grid, and mobile viewer layout

**Part A: Multi-select bulk editing on the grid page.**

**Goal:** Let the user select multiple photos/videos in the grid at once and apply an edit to all of them in one action — motivated directly by GPS backfill gaps (many files missing location can share the correct one) and other batch cleanup needs.

**Requirements:**
- **Selection mechanism**: a checkbox (or click-to-toggle-select) per grid item. Track selection as a set of `file_hash` values, persisting across pagination within the same session (so selecting items on page 1, then paging to page 2, doesn't lose the earlier selections) — no need to support "select all matching current filter" beyond what's actually visible/paged through.
- **A bulk-action bar** appears once 1+ items are selected, showing the count and available actions, plus a "Clear selection" control.
- **Bulk actions to support:**
  - **Set location** — same dropdown (existing distinct-location list) as the single-item edit UI. Applies the chosen location to every selected item.
  - **Add tag** — adds ONE new tag to every selected item's EXISTING tag set (a union/append, NOT a full replace — this is different from the single-item edit's "full replace tags" semantics, since different selected files likely have different existing tags that must not be destroyed).
  - **Set date** (lower priority per the user's own hedging — "maybe") — sets `date_taken` to a chosen value for every selected item. Same caveat as any date edit: DB-metadata-only, never moves/renames/re-sorts files.
  - **Hide** — applies the existing reversible `excluded` flag to every selected item.
- **Every bulk action is still a real, audited edit** — goes through the same `photo_edits` audit mechanism as a single-item edit (so each is permanently protected from future automated overwrite, same as any manual edit), with `edited_by` reflecting the actual logged-in user (not a synthetic batch-tool tag — this is a real person interactively choosing a real batch through the UI, unlike the fully-automated date-audit tool).
- **New endpoint(s)** needed for this (a bulk-edit route accepting a list of `file_hash` values + field + value), behind the same auth + CSRF protection as the existing single-edit endpoint, using the same server-side field allow-list. Every authenticated user can bulk-edit (consistent with the existing "everyone can edit" decision) — no new permission tier.
- Show a clear success/failure summary after a bulk action (e.g. "12 updated, 0 failed") rather than silently succeeding or failing.

**Part B: Mobile phone viewer layout.**

**Context:** The viewer works fine on desktop and iPad, but on a phone-sized screen, the photo/video renders too small because the metadata/info panel (caption, tags, location, date, edit controls) takes up too much of the limited vertical space.

**Requirements:**
- On narrow (phone-width) viewports specifically, make the info panel collapsible/toggleable — collapsed or minimized by default so the photo/video itself gets the bulk of the screen, with an obvious toggle (icon/button) to expand and see/edit the metadata on demand.
- Desktop and iPad/tablet-width layouts are NOT to be changed — this is a phone-specific responsive breakpoint fix, not a redesign of the whole viewer.
- Must not break any existing editing functionality — the info panel's contents (and their edit controls) work exactly the same once expanded, this is purely a show/hide layout change for narrow screens.

**Explicitly out of scope for this phase:**
- "Select all matching the current filter" (beyond what's visibly paged through) for bulk selection — not requested.
- Any change to desktop/tablet viewer layout.

## Phase 3 — Face Detection & Clustering (real design, replacing the original rough sketch)

**Context:** This phase sat as a rough one-paragraph sketch since early in the project (insightface, faiss, DBSCAN/HDBSCAN, a labeling UI, a `people` table). Before building, real current research was done given how fast ML tooling moves and this machine's specific constraints (GTX 1660 Ti 6GB VRAM, Python 3.14).

**Key finding requiring a real decision:** official `insightface` has no official Python 3.14 wheels — only an unofficial, unbenchmarked community build exists. Given this tool now holds real production trust (167k+ real family photos, remote multi-user access), the user chose NOT to depend on that unofficial wheel.

**Resulting architecture decision — a second, separate Python environment:**
- Set up a second Python installation (3.11–3.13, whichever has the most stable current `insightface` + `onnxruntime-gpu` wheel support at the time this is built — verify at build time, don't assume a specific patch version) in its own venv, used ONLY to run the face-detection/embedding script.
- This is still fully custom code within this project — NOT the separate InsightFace desktop app/GUI (that was explicitly considered and rejected, since it would store results in its own separate database, breaking this project's "everything unified in one tool" design). Only the officially-supported library, called from a script this project owns.
- The detection script reads the list of photo paths (photos only — video is explicitly excluded from Phase 3, same posture as Phase 2's captioning) from the main SQLite DB (read-only access is enough for this step) and writes results to a JSONL file (same pattern as `captions.jsonl`): one line per DETECTED FACE (not per photo — a photo can have 0, 1, or many faces), containing `file_hash`, `path`, `bbox`, `embedding` (512-dim ArcFace vector via the `buffalo_l` model pack), `detected_at`.
- The main Python 3.14 environment (everything else in this project) reads that JSONL for the clustering step and beyond — it never needs `insightface` itself installed in the 3.14 environment.

**Detection step requirements:**
- Resumable by `file_hash`, same pattern as captioning/GPS extraction — skip already-processed photos on a re-run.
- Checkpoint/flush periodically, don't hold results in memory until the end.
- **Benchmark real throughput against a real sample of this library before committing to a full run** — same discipline already applied to captioning (multi-day estimate before running) and video thumbnails (measured, not assumed). GPU face detection throughput needs to be measured on this actual GTX 1660 Ti, not assumed from generic benchmarks.
- CLI command in the SEPARATE venv (this is inherently a different environment from the rest of the CLI/dashboard, so it can't be a `main.py` subcommand in the usual sense — a standalone script, clearly documented in README.md for how to invoke it given the two-environment setup).

**Clustering step requirements (in the main 3.14 environment — pure numerical work, no `insightface` dependency needed here):**
- Read the face-embeddings JSONL, cluster via DBSCAN or HDBSCAN on embedding similarity (cosine distance is standard for ArcFace embeddings).
- **Verify `faiss`'s own Python 3.14 wheel situation before committing to it** — same caution as everything else in this project; if it has the same kind of gap `insightface` did, a pure `scikit-learn`/`scipy` DBSCAN approach (no `faiss` needed) is an acceptable fallback given this library's realistic face-count scale, don't force `faiss` in if it's not cleanly available.
- New schema: a `faces` table (file_hash, path, bbox, embedding, cluster_id, detected_at) and a `people` table (cluster_id/person_id, name, notes) — additive via `init_db()`/`_migrate()` per rule 12, never destructive.
- Re-clustering should be re-runnable (e.g. after adding more photos later) without destroying existing person-name assignments already made — cluster IDs and name assignments need to survive a re-cluster somehow (e.g. keyed by a stable representative embedding or a merge/reconcile step) — a real design detail to work out carefully when this step is actually built, don't hand-wave it.

**Labeling UI — lives in the Tkinter dashboard (user's explicit choice), not `review_tool.py`:**
- A dedicated window/panel showing cropped face thumbnails (crop each photo to its detected bbox, resize, display via PIL/ImageTk) grouped by cluster.
- Assign a name to a whole cluster at once.
- Merge two clusters together (when the same person got split into two groups).
- Split incorrect faces out of a cluster (when two different people got merged into one group).
- This is a genuinely different UI paradigm from the existing batch-operation panels (progress bar + log tail) — it's interactive browsing/labeling, not a start/stop job. Build accordingly, don't force it into the existing panel template if it doesn't fit.

**Explicitly out of scope for Phase 3:**
- Video — photos only, matching Phase 2's captioning scope decision.
- Any face data surfaced in `review_tool.py` beyond what Phase 2c's already-built (but currently inert) people/faces filter placeholder expects — wiring that placeholder up to real data is a natural follow-up once Phase 3 has real results, but isn't automatically part of this build.
- Automatic re-clustering triggered by new photos being added — a manual "re-cluster" action is enough for now.

## Phase 3b — Resync folder placement to corrected dates

**Context:** The Date Cluster Audit tool fixed 2,527 photos' stored `date_taken` values (via the `photo_edits` audit mechanism), but this was explicitly DB-metadata-only — it never moved the physical files. Those files still sit in their old, now-wrong `YYYY/YYYY-MM` folders, disagreeing with their own corrected date. The user wants this resolved — the mismatch would otherwise nag indefinitely.

**Goal:** A general-purpose tool that finds any photo/video whose current physical location doesn't match where it *should* be based on its current `date_taken`, and safely relocates it — not narrowly hardcoded to just this one batch of 2,527, since any future date edit (via the viewer, bulk-edit, or a future audit fix) could create the same kind of mismatch.

**Requirements:**
- **Detection:** for each row, compute the "correct" destination path from its current `date_taken` (same convention as Phase 1: `<dest_root>/YYYY/YYYY-MM/` for photos, `<dest_root>/YYYY/YYYY-MM/Video/` for video), and compare against the file's actual current path. A mismatch is a candidate.
- **Reuse Phase 1's existing copy-verify-delete/collision-handling logic — do not reimplement it.** Same safety model: copy to the new correct location, verify via hash comparison, delete from the old location only after verification succeeds. Never a raw move. Collision handling (hash-suffix on a naming conflict) identical to Phase 1's existing behavior.
- **Dry-run preview required before any real move** — show old path → new path for every affected file, consistent with every other batch operation in this project.
- **Update the DB's current path** for each successfully relocated file. This is a physical reorganization, not a metadata field edit — log it clearly (old path, new path, timestamp) but it doesn't need to go through the `photo_edits` field=value audit mechanism the way caption/tag/location/date edits do; a dedicated log is more appropriate here, matching Phase 1's own `logs/organize_*.log` pattern.
- **Applies to both photos and video** — check whether any videos are also affected by a folder mismatch, don't assume it's photos-only just because the originating Date Cluster Audit investigation was framed around photos.
- CLI command + dashboard panel, per rule 10.
- Per rule 11/12 as applicable.

**Explicitly out of scope:**
- Re-running this automatically after every date edit — a manual trigger is enough for now, same posture as re-clustering in Phase 3.

## Phase 3c — Face Labeling UI: filter out tiny/junk clusters, mark false detections

**Context:** The labeling window already sorts by largest group first, so the most important people (family members appearing in hundreds of photos) surface quickly. But at full-library scale, a real risk is many small clusters (1-3 faces — blurry shots, strangers in the background, bad detections) burying the list in low-value entries the user has to click past. The small test sample already showed 71 unclustered faces out of 767 — this scales up meaningfully at full library size.

**Requirements:**
- **A minimum-cluster-size filter** in the `FaceLabelingWindow` — a simple numeric input ("Hide groups smaller than N faces," reasonable default like 3) that live-filters the left-side people list. Hidden clusters aren't deleted or altered in any way, just not shown while the filter is active — a toggle/reset to show everything again, nothing is ever lost.
- The "(Unclustered)" bucket is a single fixed entry, not a growing list of clusters — this filter doesn't need to apply to it the same way; leave it always visible and accessible as-is.
- **"Hide already-named people" toggle (promoted from optional to required)** — once you've labeled someone, they can drop off the visible list automatically so you're not re-scrolling past people you've already finished.
- **"Mark as not a person" action (new — for false-positive detections, e.g. a pattern/object/pet misdetected as a face).** Consistent with this project's established non-destructive pattern (same philosophy as "Hide from library" for photos — a reversible flag, never a deletion): add a flag on the affected `faces` row marking it as a confirmed false positive. A flagged face is:
  - Pulled out of whatever cluster it's currently in (same mechanical effect as a split, but tagged as "not a person" rather than moved to a new named person)
  - Excluded from future clustering runs (a future `cluster-faces` re-run must not re-add it to any cluster)
  - Hidden from the normal labeling view by default, but reachable via a "Show marked-as-not-a-person" toggle, with a way to un-mark it if it was flagged by mistake — fully reversible, same as every other flag in this project.
- Both size/named-people filters are UI-only conveniences — they must never affect the underlying data, clustering, or which faces are assigned to which person, aside from the explicit "mark as not a person" action above, which is a real, intentional (but reversible) data change.

**Explicitly out of scope:**
- Anything involving `review_tool.py` — that's Phase 3d, a separate session (see below).

## Phase 3d — Wire up the grid's people/faces filter to real data

**Context:** Phase 2c built a people/faces filter control in `review_tool.py`'s grid page as a visible-but-inert placeholder, explicitly anticipating Phase 3 ("Phase 3 doesn't exist yet — show it, mark 'not available yet'"). Phase 3 now exists with real detected/clustered/named people — this session wires the existing placeholder up to that real data.

**Requirements:**
- Populate the filter with actual named people from the `people` table (only people with a real assigned name — unclustered or unnamed faces should not appear as a filterable option).
- Filtering by a selected person narrows the grid to photos containing at least one face belonging to that person (a join between `photos`/`faces`/`people`).
- Reuse the existing custom suggestion-dropdown component (built for the tag filter's iOS fix, already reused once for the bulk-edit location field) for this control — don't build a third copy of the same pattern, per rule 7.
- This filter should compose correctly with the grid's other existing filters (date, tags, location, media type) — a person filter plus a date range, for example, should both apply together, same as every other filter combination already works.
- Per rule 11: restart `review_tool.py` at session end, since this session touches it directly.
- Per rule 12: `init_db()` still called as normal — no new schema expected here, `faces`/`people` already exist from Phase 3 Session 2.

**Explicitly out of scope:**
- Any editing of face/person data from `review_tool.py` — this is a filter/browse feature only, consistent with `review_tool.py`'s existing edit scope (captions/tags/location/date/hide) not being extended to faces in this pass unless separately requested.

## Phase 3e — Display named people on the individual viewer AND the grid

**Context:** Phase 3d wired up the ability to *filter* by person, but never wired up actually *displaying* who's in a photo — the viewer's "People" row was left as an inert placeholder (this was a genuine gap in the Phase 3d spec, not a scope decision Code made on its own). The user now wants this shown in both places: the full viewer AND directly on grid thumbnails.

**Requirements:**

**Individual viewer:**
- Wire up the existing "People" row/field to show the actual named people appearing in that specific photo (join `photos`/`faces`/`people` on `file_hash`, named people only — no unnamed/unclustered entries shown here, consistent with Phase 3d's filter also being named-only).
- **Collapse when there are many people in one photo** — show a reasonable number (e.g. first 3-4 names) plus a "+N more" indicator, with a way to expand and see everyone (click to expand is fine — reuse whatever collapse/expand pattern already exists elsewhere in the UI if one does, don't invent a new one if an equivalent pattern is already established; check first).

**Grid thumbnails:**
- Show named people directly on/near each grid card too, not just in the full viewer.
- Given thumbnails are much smaller than the full viewer, this needs to be compact by default — small name badges/chips is a reasonable approach, but use judgment on exact sizing/placement so it doesn't overwhelm the thumbnail itself.
- Same collapsing principle as the viewer, likely even more aggressive given the smaller space (e.g. first 1-2 names + "+N", full list available via hover or click-through to the viewer) — exact threshold is a reasonable implementation judgment call, doesn't need to match the viewer's exact number.
- This adds a join to the grid's existing query for every visible page of results — since results are already paginated, this should stay cheap (only needs data for the current page's rows, not the whole filtered set), but worth confirming this doesn't meaningfully slow down grid page loads at real scale.

**Explicitly out of scope:**
- Any editing of who's tagged in a photo from this display — this is read-only display, consistent with Phase 3d's filter-only scope. Editing face/person assignments stays in the Tkinter labeling window only.

## Phase 3f — "Remove Selected from Cluster" (send outliers back to Unclustered, without creating a new person)

**Context:** Found via real use — a real 408-face cluster was almost entirely the user's daughter, with a handful of genuinely misclustered faces mixed in. The existing "Split Selected Into New Person" action always creates a NEW cluster from the selection, which is the wrong tool here — the outliers weren't one cohesive different person, just miscellaneous wrong matches. Selecting and moving the ~400 CORRECT faces (to keep the cluster clean) is far more clicking than selecting the handful of WRONG ones.

**Goal:** A lighter-weight action: select the few outlier faces within a cluster, remove just them, sending them back to "(Unclustered)" — no new person/cluster created, the majority of the original cluster is left untouched.

**Requirements:**
- New action in `FaceLabelingWindow`, e.g. "Remove Selected from Cluster" — takes the currently-selected face thumbnails (same selection mechanism already used for Split/Merge), sets their `cluster_id` to NULL (same destination as a genuinely unclustered face), and leaves the `not_a_person` flag untouched/false (these are still real, valid faces — just wrongly grouped, not false-positive detections; don't conflate this with Phase 3c's "mark as not a person" action).
- **Reuse the existing centroid/face-count recompute logic already built for Split and "mark as not a person"** — don't reimplement it. The source cluster's centroid needs to be recomputed after faces leave it, exactly like those other two actions already do.
- Removed faces should show up back in "(Unclustered)" immediately, available for manual re-labeling, a future Split, or simply waiting for the next `cluster-faces` re-run to find them a new home.
- This is functionally "half of Split" (the removal half, without the new-cluster-creation half) — implement it as a genuinely smaller, focused action rather than a variant that still creates an empty/unnecessary cluster.

**Explicitly out of scope:**
- Any change to Split or Merge's existing behavior — this is a new, additional action alongside them, not a replacement.

## Phase 3g — Multi-person filter in review_tool.py's grid ("photos with BOTH X and Y")

**Context:** Phase 3d's people filter only supports selecting one person. The user wants to select multiple named people at once and see only photos where ALL of them appear together — their explicit example: select "Dan Moll" and "Juliana Moll" to find photos containing both, not either.

**Requirements:**
- **AND semantics, not OR** — a matching photo must contain a face for EVERY selected person, not just at least one. This is the whole point of the feature (per the user's explicit example) and must not be confused with a looser "any of these people" filter.
- **UI: a multi-select chip/tag-style input**, not the existing single-value suggestion field. As the user types and picks a suggestion, that person becomes a removable "chip," and they can keep adding more people the same way. Reuse the existing suggestion/autocomplete mechanism (the same component already used for tags, location, and Phase 3d's single-person filter) for the underlying type-ahead behavior, but the surrounding "list of selected chips" container is new UI, not a drop-in reuse of the single-value instances.
- **Backend query**: photos must be filtered to only those matching ALL selected people simultaneously — a relational "photo contains person A AND contains person B AND ..." query (e.g. `GROUP BY file_hash HAVING COUNT(DISTINCT person_id) = <number of selected people>`, or an equivalent intersection approach) — not a simple `IN (...)` which would incorrectly produce OR semantics.
- **Must compose correctly with every other existing filter** (date, tags, location, media type) — combined filtering, same as every other filter combination already works in this tool.
- **Must apply consistently to both the grid AND slideshow/viewer navigation** — same principle already established for every other filter in this project (Phase 2c's original filter work explicitly required this for tags/location/etc., and it should hold here too).
- Per rule 11/12 as applicable (this touches `review_tool.py` directly).

**Explicitly out of scope:**
- An "any of these people" (OR) mode — not requested; if wanted later, that's a separate, explicit ask.

## Phase 3h — Favorites, per-user, with "who favorited this" visibility

**Context:** Multiple people log into `review_tool.py` (Phase 2d). The user wants a favorites system where each person's favorite marks are independent and attributable to them — not a single shared boolean — with the ability to filter by "my favorites," "anyone's favorites," or a specific named person's favorites.

**Requirements:**
- **Schema**: a new `favorites` table (`file_hash`, `username`, `favorited_at`), primary key on `(file_hash, username)` — allows multiple different users to each independently favorite the same photo. Additive via `init_db()`/`_migrate()` per rule 12.
- **Toggle action**: a star/heart button in the viewer marks/unmarks the photo as a favorite for the CURRENTLY LOGGED-IN user only — never affects another user's favorite status on the same photo. CSRF-protected, behind the existing auth gate, same "every authenticated user can act" posture as editing (no new permission tier).
- **Display "favorited by" on the photo** — the viewer should show who has favorited it (a list of usernames), not just a yes/no. A simple star badge on grid cards (favorited-by-me only, to keep cards visually clean) is a reasonable default; the full "who favorited this" list belongs in the viewer, consistent with how other per-photo detail (people) already splits between compact-grid vs. full-viewer.
- **Filter, as a dropdown/selector, not a single-select-list like Phase 3g's multi-person filter**: options are "Me" (current user), "Anyone" (favorited by at least one user, any user), and each real named `review_user` individually (so the user can specifically check "what did my wife/daughter favorite"). This is intentionally simpler than Phase 3g's AND-multi-select — a single choice of *whose* favorites to view, not a combination of several people's favorites at once.
- **Must be batched, not per-row**, when fetching favorite status/who-favorited data for a grid page — same discipline Phase 3e's `_people_for_hashes()` established (one query per page regardless of page size, proven via a dedicated test, not assumed). Don't repeat the mistake that had to be caught elsewhere; build this batched from the start.
- Must compose correctly with every other existing filter, and apply to both the grid and viewer/slideshow navigation, same as every other filter in this tool.
- Per rule 11/12 as applicable.

**Explicitly out of scope:**
- Any notification/alerting when someone else favorites a photo — just visibility on the photo itself, nothing proactive.
