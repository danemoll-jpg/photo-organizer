# Photo Organizer

Local tool to sort ~25 years of family photos (and, since Phase 1b, videos)
into `E:\Pics\YYYY\YYYY-MM\`.
See [`photo-organizer-spec.md`](photo-organizer-spec.md) for the full design and
[`TODO.md`](TODO.md) for current phase status. This README covers Phase 0/1/1b setup and usage.

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

**Usage:**
```bash
# Caption everything under dest_root not already in captions.jsonl
venv\Scripts\python main.py caption

# Try one folder first before the full library
venv\Scripts\python main.py caption --limit "E:\Pics\2024"
```

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
   stay empty until Phase 2's JSONL is loaded into the DB and Phase 3 runs.

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
