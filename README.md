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
