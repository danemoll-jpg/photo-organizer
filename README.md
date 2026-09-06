# Photo Organizer

Local tool to sort ~25 years of family photos (and, since Phase 1b, videos)
into `E:\Pics\YYYY\YYYY-MM\`.
See [`photo-organizer-spec.md`](photo-organizer-spec.md) for the full design and
[`TODO.md`](TODO.md) for current phase status. This README covers Phase 0/1/1b/2/2b setup and usage.

## Setup (one-time)

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy config.example.yaml config.yaml
venv\Scripts\python main.py init-db
```

Then edit `config.yaml` (gitignored — machine-specific paths live here, never hardcoded):
- `source_folders` — folders to scan. Populate interactively:
  ```bash
  venv\Scripts\python main.py pick-sources
  ```
  This opens the native Windows folder picker with **multi-select**
  (ctrl/shift-click several sibling folders in one dialog), asks "pick
  another batch?" between rounds, and writes your selections into
  `config.yaml` — additively, on top of whatever's already there, never
  overwriting it. Each dialog opens at the *parent* of the last folder you
  picked, not inside it, so you don't have to navigate back up every round.
  If the native dialog can't be created on this machine for some reason, it
  falls back to the old single-folder-at-a-time picker automatically.
  `dest_root` (`E:\Pics`) is *always* scanned too, in addition to whatever you
  pick — it already has a mix of sorted and loose files, and this catches the
  loose ones without you having to list it separately.
- `dry_run` — leave as `true` until you've reviewed a scan's output.

## Desktop dashboard

```bash
venv\Scripts\python dashboard.py
```

Or just double-click **`Launch Dashboard.bat`** in this folder, or the
**"Photo Organizer"** shortcut on the Desktop (created once via
`New-Object -ComObject WScript.Shell` — see git history if you ever need to
recreate it on another machine). Both launch `dashboard.py` with
`pythonw.exe` — no console window.

A Tkinter GUI wrapper around the same code the CLI uses — it doesn't
duplicate any scan/date-resolve/copy-verify-delete logic, it just calls into
`src/organize.py`, `src/pick_sources.py`, `src/config.py`, etc. directly.

- **Source folders** — see/add (native multi-select picker)/remove entries
  in `source_folders`. Removing a folder here only forgets it as a scan
  source; it never touches files in it.
- **Dry Run (preview)** — same as `main.py scan`; results panel shows counts
  by outcome (would-sort, would-flag-unsorted, already-in-place no-op,
  duplicates skipped, errors).
- **Run for real...** — same as `main.py run --execute`; asks for explicit
  confirmation first (lists the source folders and dest, explains the
  copy-verify-delete safety model) before touching any files.
- **Cancel** — becomes active during a run; stops the run *between* files
  (never mid-copy/verify), so a cancelled real run can't leave a partial
  file behind. Already-processed files stay processed (hash-based
  resumability), so a cancelled run is always safe to just re-run later.
- **Progress** — live "N / total processed" bar while a run is going.
- **Log viewer** — pick any `logs/organize_*.log` from the dropdown to
  browse it, or leave "Auto-tail active run" checked to watch the current
  run's log live. Same log files the CLI writes — no separate GUI-only log.

`main.py`'s CLI commands keep working exactly as before; the dashboard is an
additional front end, not a replacement.

## Usage

```bash
# Preview only — never touches files, always safe, ignores config's dry_run
venv\Scripts\python main.py scan

# Honors config.yaml's dry_run setting
venv\Scripts\python main.py run

# Force a real run regardless of config (asks for a typed "yes" confirmation
# unless --yes is also passed)
venv\Scripts\python main.py run --execute

# Force a preview even if config.yaml has dry_run: false
venv\Scripts\python main.py run --dry-run
```

Every run writes a timestamped log to `logs/organize_<timestamp>.log` (per-file
outcome + which date source was used) and records each processed file in
`data/photo_organizer.db` (SQLite, keyed by content hash — see `schema.sql`).
Re-running is always safe: anything already recorded by hash is skipped.

## How Phase 1 decides a photo's date

1. EXIF `DateTimeOriginal` (fallback `DateTimeDigitized`)
2. Filename pattern (`IMG_20180304_...`, `Screenshot_20200101-...`, bare `YYYYMMDD`, etc.)
3. Filesystem date (earlier of modified/created time) — least reliable, still logged as such
4. Nothing usable → routed to `<dest_root>\_unsorted\needs_review\` instead of guessed

## Phase 1b — video files (MP4, MOV, AVI)

Videos are scanned and sorted through the exact same pipeline as photos —
same hashing/dedup, copy-verify-delete, collision handling, and logging.
Two differences:

- **Date resolution** uses the video's own container creation-date metadata
  (read via [`hachoir`](https://hachoir.readthedocs.io/), no ffmpeg/MediaInfo
  install required) as the first step, since videos don't carry EXIF. The
  rest of the chain is identical to photos:
  1. Container creation-date metadata (`date_source=container`)
  2. Filename pattern (same patterns as photos — `VID_...`, `PXL_...`, etc.)
  3. Filesystem date — least reliable, still logged as such
  4. Nothing usable → `_unsorted\needs_review\`, same as photos
- **Destination**: a dated video lands in `<dest_root>\YYYY\YYYY-MM\Video\`
  — a subfolder within the month, so videos never mix with photos in the
  same folder listing. (Unsorted videos land in the same
  `_unsorted\needs_review\` folder as unsorted photos — no separate bucket.)

Extensions are configured via `video_extensions` in `config.yaml`, the same
way `supported_extensions` configures photo formats.

## Phase 2 — captioning (JPG/PNG/HEIC only, via Ollama)

Captions and tags each already-organized photo using a local vision model
served by [Ollama](https://ollama.com) — free, runs on the GTX 1660 Ti, no
cloud API involved (that option was considered and explicitly decided
against for this project; see `CLAUDE.md`). Video is out of scope for this
phase (see `photo-organizer-spec.md`).

**One-time setup:**
```bash
winget install Ollama.Ollama
ollama pull qwen3-vl:2b
```
Ollama installs as a background service (starts automatically, listens on
`http://localhost:11434`) — no need to run `ollama serve` manually.

**Usage — CLI:**
```bash
# Caption everything under dest_root not already in captions.jsonl
venv\Scripts\python main.py caption

# Try one folder first before the full library
venv\Scripts\python main.py caption --limit "E:\Pics\2024"
```

**Usage — dashboard:** open the **"Phase 2 — Captioning"** panel and click
**Start Captioning**. Same underlying function as the CLI (`run_phase2()`),
just without the `--limit` option — the dashboard always captions the
whole `dest_root` tree. Has its own progress bar and Cancel button,
independent of the Phase 1 panel above (safe to run both at once — see
below).

Output is appended to `data/captions.jsonl` (one JSON object per line:
`file_hash`, `path`, `caption`, `tags`, `date_taken`, `model_used`,
`processed_at`, plus `file_size`/`file_mtime` backing the resume fast-path
below) — not loaded into the SQLite DB yet, that's a separate future step.

- **Resumable by content hash**, same as Phase 1 — re-running only
  captions files not already in `captions.jsonl`. A path+size+mtime
  fast-path (same fix Phase 1 needed for its destination rescan, applied
  here from the start) means an unchanged file is recognized without
  being re-hashed.
- **Checkpointed**: flushed to disk every `batch_size` (config, default
  500) images captioned, so a crash loses at most that many.
- **Read-only against your photos, append-only against the JSONL** — never
  modifies, moves, or deletes an original file.
- **Real throughput on this hardware: ~7-9 seconds/image** (`qwen3-vl:2b`).
  At 100k+ files, budget roughly 8-9 days of continuous run time — this is
  a multi-day background job, not something to expect to finish overnight.
  Safe to run alongside Phase 1/1b (they never touch the GPU — no resource
  contention), and safe to stop/resume any time (`Ctrl+C`, or the
  dashboard's Cancel button once Phase 2 gets one).
- Model is config-driven (`ollama_model` in `config.yaml`) — switching to
  `minicpm-v4.6` (also pulled, see below) if `qwen3-vl:2b`'s quality
  disappoints is a one-line change, no code edit needed.

**Quality expectations:** a small (1-2B parameter) local model gives
simple, functional captions/tags good enough for search and filtering
(e.g. "a family gathered around a table" / tags: `people, indoor, dinner,
table`) — not vivid, nuanced descriptions. On very low-detail or ambiguous
images it can occasionally hallucinate specifics that aren't there; worth
a skim of early output before trusting it at scale.

## Phase 2b — GPS locations + review tool

Two independent pieces, both new: extracting a human-readable place name
from a photo's EXIF GPS data, and a standalone browser tool for spot-checking
the in-progress Phase 2 captioning run against real, live data.

### GPS extraction (`main.py extract-gps`)

Reads EXIF GPS coordinates (where present — sparse on pre-GPS-era photos)
and reverse-geocodes them to a city-level place name (e.g. "Marietta, GA")
via [`reverse_geocoder`](https://pypi.org/project/reverse-geocoder/) — a
fully **offline** worldwide city gazetteer, no API key, no network call, no
per-lookup cost, consistent with this project's local-only approach
elsewhere. Video (MP4/MOV) doesn't currently expose GPS through the
container-metadata reader already used for its date resolution — checked
against real files from this library, not just assumed — so video rows
simply get no location, which is an accepted limitation, not a bug.

Writes directly to the SQLite DB (`photos.gps_lat`, `gps_lon`,
`location_name`) rather than through a JSONL intermediate — unlike Phase
2/3, this only ever needs to run once per file, so there's no benefit to a
separate resumable file format. Resumable by its own `gps_checked` column
instead: a row already checked (whether or not GPS data was actually found)
is never re-examined.

```bash
# Extract for the whole library not yet checked
venv\Scripts\python main.py extract-gps

# Try one folder first (matches current_path by prefix)
venv\Scripts\python main.py extract-gps --limit "E:\Pics\2025\2025-01"
```

Fast — real-world measurement on this library was roughly 100 files/second
(EXIF read + an in-memory nearest-city lookup), so a full 100k+-file library
run is a matter of minutes, not the multi-day job Phase 2 is. Safe to run
any time; doesn't touch `organize.py` or `caption.py`.

### Review tool (`review_tool.py`)

A standalone local web app — **not** a dashboard panel — for browsing the
library and spot-checking Phase 2's progress while it's still running.

```bash
venv\Scripts\python review_tool.py
```

Or just double-click **`Launch Review Tool.bat`** in this folder. Unlike
`Launch Dashboard.bat`, this one deliberately keeps its console window open
(runs via `python.exe`, not `pythonw.exe`) — it's a local web server, so the
window shows the Flask/Werkzeug request log and is how you stop it
(close the window, or `Ctrl+C` inside it).

Opens `http://127.0.0.1:5151` (configurable — `review_tool_port` in
`config.yaml`) in your browser automatically. Read-only by construction: the
SQLite DB is opened via SQLite's own `mode=ro` URI (an accidental write
would raise, not silently succeed), and `captions.jsonl` is only ever
tailed for reading. Nothing here edits captions/tags, and it never runs
organize/caption/extract-gps itself.

- **Grid/browse view**: paginated (cursor-based, not `OFFSET` — stays fast
  no matter how deep you page into 100k+ rows), with date-range, folder,
  tag, caption-keyword, and GPS/location filters (place-name substring, or
  a has/no-location toggle). Every filter applies to the viewer/slideshow
  below too, not just the grid — a tag-filtered slideshow only shows
  matching photos. A photo not yet captioned shows clearly as "Not yet
  captioned", never as an error.
- **Viewer/slideshow mode**: click any photo to open it full-size with its
  path, date, caption, tags, and location alongside. Forward/Back step one
  at a time; Play starts an auto-advance slideshow at a fully configurable
  interval (editable live, remembered per-browser). Manual navigation
  pauses the slideshow rather than fighting with it. Stepping past the
  last photo on a page — or just letting the slideshow run — transparently
  keeps going into the next page's photos; there's no page-boundary
  dead-end.
- **Random navigation**: a **Random** button jumps to a random photo within
  the currently active filters (a one-off "surprise me", separate from
  slideshow order below). The slideshow's **Order** control toggles between
  Chronological (the default above) and Random — switching to Random (or
  changing filters while it's selected) mints a fresh shuffle, so Play and
  manual Next/Prev step through a genuinely random, repeat-free sequence of
  the current filtered set rather than the same order every time.
- **People/faces**: a reserved section (and matching filter control) is
  always present, showing a "Phase 3 not built yet"/"Not available yet"
  placeholder — so this tool won't need a UI rebuild once face detection
  lands, only that field/filter needing to go live.
- **Live captions**: `captions.jsonl` is tailed incrementally on every
  request, so newly-captioned photos from the in-progress background run
  show up without restarting this tool — including for the tag/caption
  filters above, which read this same live cache rather than the DB's
  `caption`/`tags` columns (those only reflect the last `load-captions`
  run).
- Video files are excluded from every view here — Phase 2 never captions
  video by design, so showing them would mean a permanent, misleading
  "not yet captioned" that will never resolve.

## Phase 2d — remote/shared access

Lets you (or someone you invite) reach `review_tool.py` from outside your
home network, over real HTTPS, password-gated — without opening any port
on your router. Two independent pieces: login (this app) and network
exposure (a Cloudflare Tunnel). Fully separate from Plex's own remote
access — neither depends on the other, neither's uptime affects the other.

### 1. Add a login for each person

```bash
venv\Scripts\python main.py review-user add <username>
```

Prompts for a password (hidden input, not echoed, never taken as a CLI
arg so it can't land in shell history) and saves a hashed credential to
`config.yaml`'s `review_users` — never the plaintext password. One
username+password pair per person; not a shared password, not a full
signup system. Everyone with a login currently sees the whole library —
there's no per-person restriction (not asked for), but nothing assumes
that so deeply it couldn't be added later.

```bash
venv\Scripts\python main.py review-user list      # see who's configured
venv\Scripts\python main.py review-user remove <username>   # revoke access
```

Then just open `review_tool.py` (`Launch Review Tool.bat`, as before) — it
now shows a login page first. Signing in is a real session (a signed
cookie), not a re-login on every click, and persists for 30 days by
default (`session_lifetime_days` in `config.yaml`). A **Log out** button
sits in the top-right once signed in.

Repeated wrong passwords get rate-limited (5 attempts per 15 minutes by
default, per IP and per attempted username — see `login_rate_limit_*` in
`config.yaml`) — you'll see a "Too many failed attempts" message rather
than the login just silently accepting more guesses.

### 2. Expose it — Cloudflare Tunnel (not port-forwarding)

This gives `review_tool.py` a real `https://` URL without opening any
inbound port on your router, and without relying on Plex's own remote
access at all. One-time setup per machine (requires your own Cloudflare
account — a free one is fine — with a domain already using Cloudflare's
nameservers):

```bash
cloudflared tunnel login
```
Opens a browser to authenticate with your Cloudflare account and pick the
domain to use — this step needs to be done by you interactively, it's
your account.

```bash
cloudflared tunnel create photo-organizer
```
Creates the tunnel and writes a credentials file to
`%USERPROFILE%\.cloudflared\<tunnel-id>.json`. Note the tunnel ID it
prints.

Create `%USERPROFILE%\.cloudflared\config.yml` — see
**`cloudflared-config.example.yml`** in this repo for the exact template
(fill in your tunnel ID, credentials-file path, and the hostname you want,
e.g. `photos.yourdomain.com`).

```bash
cloudflared tunnel route dns photo-organizer photos.yourdomain.com
```
Adds the DNS record in Cloudflare pointing that hostname at your tunnel.

From then on, whenever you want remote access live:
1. Run `review_tool.py` as usual (`Launch Review Tool.bat`) — keep it on
   its default `127.0.0.1` binding, **don't** pass `--host 0.0.0.0` for
   this. cloudflared reaches it over loopback only, so the app itself
   never needs to listen on a LAN/router-facing interface at all.
2. Run **`Launch Review Tunnel.bat`** (or `cloudflared tunnel run
   photo-organizer`) — keep its window open too. Closing it takes the
   tunnel down; `review_tool.py` keeps running locally, just no longer
   reachable from outside until the tunnel's back up.
3. Share `https://photos.yourdomain.com` (or whatever hostname you
   routed) with whoever you added a login for in step 1.

### Storage abstraction (groundwork, not a migration)

`review_tool.py` reads photo bytes through a small `PhotoStorage`
interface (`src/storage.py`) instead of touching the filesystem directly.
Today there's only one backend — local disk, exactly as before — this is
just groundwork so a future move to cloud object storage (S3, Backblaze
B2, etc.) would mean writing one new backend class and changing
`config.yaml`'s `storage_backend`, not rewriting the app. No cloud backend
is implemented, and nothing has moved off local disk.

## Viewing the database

`data/photo_organizer.db` is a plain SQLite file — no server, no login.
Options for looking inside it, easiest first:

**Just ask** — if you're working with Claude Code, it can run read-only
queries against it directly in chat any time.

**DB Browser for SQLite** — free GUI, browse tables/run SQL without
knowing SQL well:
```bash
winget install DBBrowserForSQLite.DBBrowserForSQLite
```
Open the app → **Open Database** → point it at `data\photo_organizer.db`.

**Command line** — Python's `sqlite3` module needs no install:
```bash
venv\Scripts\python -c "import sqlite3; c=sqlite3.connect('data/photo_organizer.db'); [print(r) for r in c.execute('SELECT * FROM photos LIMIT 5')]"
```

**MS Access, via ODBC linked tables** (set up and confirmed working on
this machine):
1. Install the [SQLite ODBC Driver](http://www.ch-werner.de/sqliteodbc/)
   — download `sqliteodbc_w64.exe` (64-bit, matches 64-bit Office/Access;
   note the site serves it over plain HTTP, no HTTPS cert — it's still the
   long-standing canonical source for this driver). Run the installer;
   it needs admin/UAC approval.
2. In Access: **External Data → New Data Source → From Other Sources →
   ODBC Database → Link to the data source by creating a linked table.**
3. In the ODBC picker: **Machine Data Source** tab → **New...** →
   **System DSN** → **SQLite3 ODBC Driver**. Name the DSN (e.g.
   `PhotoOrganizer`) and browse to `data\photo_organizer.db`.
4. Pick that DSN, then link **`photos_access`** — **not** the raw
   `photos` table.

   Why: linking `photos` directly shows every row as `#Deleted` in
   Access. The SQLite ODBC driver doesn't reliably expose `photos`'
   `file_hash` PRIMARY KEY to Access as a usable row identifier, so Access
   falls back to matching a row by comparing every column's value — and
   `file_mtime` (a floating-point column) doesn't round-trip byte-for-byte
   through ODBC, so that comparison silently fails on every row.
   `photos_access` (defined in `schema.sql`) is the same data with
   `file_mtime` cast to text, which sidesteps the problem. It's a live
   view (always current, not a snapshot) — just presented as read-only in
   Access, which is also the recommended way to treat this link generally:
   `photo_organizer.db` gets written to by every real Phase 1/1b/2 run, so
   editing rows through Access while a run might be active risks a write
   conflict.
5. `tags`/`faces`/`people` are also available to link the same way, but
   `tags`/`photo_tags` stay empty until you load captions (below), and
   `faces`/`people` stay empty until Phase 3 runs. Heads up:
   `photo_tags.confidence` is also a REAL column — expect the same
   `#Deleted` issue there if you link `photo_tags` directly.

**Loading captions into the database:** Phase 2 only ever writes
`captions.jsonl` (see above) — it doesn't touch the database. To make
captions/tags show up in `photos`/`photos_access` (and thus in Access),
run the loader:
```bash
venv\Scripts\python main.py load-captions
```
Fills in `photos.caption` and populates `tags`/`photo_tags` by matching
`file_hash`. Safe to re-run any time as captioning progresses — it's a
full, idempotent reload of whatever's currently in `captions.jsonl`, not
an incremental one, so nothing to track between runs.

## Safety guarantees

- **Copy-verify-delete**, never a raw move: a file is copied to its destination,
  the copy is hashed and compared to the source, and only then is the source
  deleted. A hash mismatch leaves the source untouched and logs an error.
- **Never overwrites**: a filename collision at the destination (different
  file, same name) gets a short content-hash suffix appended instead.
- **Exact-hash duplicates are left in place, not deleted** — if a file's
  content hash is already recorded (from a different original path), Phase 1
  skips it without touching that copy. Phase 1 does not delete duplicate
  originals; that's a deliberate, conservative choice — see `TODO.md`.

## Testing without real photos

```bash
venv\Scripts\pip install -r requirements-dev.txt
venv\Scripts\python tests\make_sample_library.py
```

Builds a small synthetic library covering every date-resolution path, a
duplicate, a filename collision, an already-correctly-placed file, and a HEIC
file — plus the video-side equivalents (filename-pattern/filesystem-date
video, a video duplicate, a video collision, an already-correctly-placed
video under `.../Video/`, and a loose video at dest's root). The one video
path these fixtures deliberately don't cover is "container metadata found" —
that needs a real MP4/MOV container to mean anything, so it's verified
against real video files instead (see the Phase 1b session summary). Prints
the `source_folders` / `dest_root` values to drop into `config.yaml` for a
dry run against it.

```bash
venv\Scripts\python tests\test_phase2_pipeline.py [model_name]
```

Builds a small synthetic photo (+ one video, to confirm it's correctly
skipped) library in an isolated temp dir and exercises Phase 2 end to end:
first run captions everything with all required JSONL fields present, a
second run is a pure no-op (resume fast-path), changing one file's content
triggers exactly one new record, and a corrupted trailing JSONL line
doesn't break resume. Requires Ollama running locally with the model
already pulled — this hits the real model, not a mock, since captioning
quality/behavior is the whole point of the phase.

```bash
venv\Scripts\python tests\test_phase2b.py
```

Covers Phase 2b end to end against synthetic fixtures: GPS extraction +
offline reverse geocoding (including resumability via `gps_checked`), and
`review_tool.py`'s JSON API via Flask's test client — pagination across a
page boundary in both directions, date/folder filters, video exclusion,
`/api/stats` respecting the same filters as `/api/photos`, `/api/nav`
stepping next/prev and returning `null` gracefully past the last photo (not
an error), and the live `captions.jsonl` tail correctly picking up a
newly-appended line — as well as tolerating a corrupt/incomplete trailing
one — without restarting the tool. No Ollama/GPU dependency, unlike the
Phase 2 pipeline test above.

```bash
venv\Scripts\python tests\test_phase2c.py
```

Covers Phase 2c's viewer v2 additions: the new tag/caption-keyword/GPS-
location filters individually, the inert people/faces filter confirmed
truly ignored server-side, `/api/random` and `/api/nav?mode=random`
respecting active filters, and — the case most likely to hide a subtle bug
— filter+random *combinations*: a full random-order permutation visiting
every matching photo exactly once with no repeats (both the no-filter
Feistel-permutation path and the tag-filtered cached-shuffle path), correct
`null` handling past the end of a shuffled sequence, and same-seed
reproducibility. No Ollama/GPU dependency.
