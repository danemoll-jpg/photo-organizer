# Photo Organizer

Local tool to sort ~25 years of family photos into `E:\Pics\YYYY\YYYY-MM\`.
See [`photo-organizer-spec.md`](photo-organizer-spec.md) for the full design and
[`TODO.md`](TODO.md) for current phase status. This README covers Phase 0/1 setup and usage.

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
  This opens the native folder picker repeatedly (one folder per pick, asks
  "add another?" each time) and writes your selections into `config.yaml`.
  `dest_root` (`E:\Pics`) is *always* scanned too, in addition to whatever you
  pick — it already has a mix of sorted and loose files, and this catches the
  loose ones without you having to list it separately.
- `dry_run` — leave as `true` until you've reviewed a scan's output.

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
file. Prints the `source_folders` / `dest_root` values to drop into
`config.yaml` for a dry run against it.
