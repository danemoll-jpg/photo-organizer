"""Date-cluster audit: detect batch-date artifacts (many photos/videos that
all landed on the same wrong date because Phase 1's date-resolution chain
fell back to an unreliable signal for a whole folder at once), investigate
them, and — for the one root-cause pattern this module knows how to fix
confidently — apply a batch correction via Phase 2f's photo_edits audit
mechanism.

Background (see CLAUDE.md/TODO.md's "Detect ALL suspicious date clusters"
entry for the full incident writeup): the user found a batch of ~3,273
photos/videos all dated 2023-10-29, which turned out to be wrong. Real
investigation against the live DB + live files (this session) found:

- Every affected file's date_source is 'filesystem' — the LEAST reliable
  step in date_resolver.py's/video_date_resolver.py's chain, used only when
  EXIF/container metadata AND filename both yield nothing. This alone is a
  strong, cheap, structural signal: a real EXIF-covered event (even a huge
  one — weddings, trips) resolves via 'exif'/'container' and spreads its
  timestamps naturally across the day; a 'filesystem'-dominated cluster
  means the file itself carried no capture-date information at all, and
  the whole cluster shares whatever administrative timestamp (import/
  download/backup time) happened to land on all of them at once.
- Confirmed directly against real files (E:\\Pics\\2023\\2023-10\\IMG_8418.JPG
  and others from the same "katya 2024" source folder): EXIF is present but
  genuinely lacks DateTimeOriginal/DateTimeDigitized — not a bad/corrupt
  value, an ABSENT one (common for images that were re-exported/converted
  by some transfer tool, e.g. an HEIC->JPEG conversion during a USB photo
  import, which can drop capture-date tags). Filenames (IMG_####.JPG) carry
  no date pattern either, so date_resolver.py correctly reached the
  filesystem fallback — this is not a bug in the resolution chain itself.
  The resulting date instead reflects whatever administrative operation
  happened to leave the SOURCE file's mtime/ctime at 2023-10-29 (evening
  hours, ~13:00-14:15, consistent with a single import/transfer session) --
  confirmed by finding the SAME files (same size) on the independent
  WD Elements backup drive's own copy of "katya 2024", carrying the exact
  same 2023-10-29 timestamps. That rules out Phase 1's own copy step as the
  cause (the backup predates Phase 1 ever touching these files) and means
  there is NO recoverable true capture date left in this batch's metadata
  — see `find_fix_candidates()`'s docstring for why this class is correctly
  left unfixed, not force-corrected to a guess.
- A SECOND, structurally different and independently confirmed pattern: a
  smaller set of files (822, confirmed this session — including a batch
  found by cross-referencing the user's pre-Phase-1 backup drive, which
  turned up a real early-library folder-naming convention, "YYYY-MM
  (MonthName)" e.g. "2001-02 (Feb)", that the first pass of this detector
  missed) were originally seen by Phase 1 already sitting inside a
  pre-existing YYYY\\MM or YYYY\\YYYY-MM[- annotation] folder the user had
  manually curated years before this project existed
  (e.g. `E:\\Pics\\2012\\12\\Portrait Studio\\10C.jpg`) — CLAUDE.md already
  documents that Phase 1 does not treat that nested structure as
  "already correct" and re-sorts it like any other implicit-source file
  (only an exact `<dest_root>\\YYYY\\YYYY-MM\\` match counts). For these,
  EXIF/filename also yielded nothing, so date_resolver.py again fell back
  to filesystem — but landed on a DIFFERENT month than the one the user had
  already filed the photo under (e.g. 2009-11-21, when `original_path` says
  `2009\\12`). Unlike the first pattern, this one HAS a recoverable,
  trustworthy signal: the user's own pre-existing year/month folder name,
  which predates this project and needed no guessing to produce. This is
  the one pattern `find_fix_candidates()` proposes an automatic correction
  for — to month precision only (day/time within that month is NOT
  recoverable from a folder name), landing on the first of the corrected
  month and clearly flagged as approximate in date_source='manual' + the
  photo_edits audit trail's edited_by value.

Different flagged dates can have different root causes — this module
doesn't assume every 'filesystem'-sourced cluster is fixable the same way,
or fixable at all; `preview_fixes()`/`apply_fixes()` only ever act on rows
matching the second, evidence-backed pattern above, and report (without
touching) everything else so the user knows what's still unresolved.
"""
from __future__ import annotations

import bisect
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .edits import has_manual_edit, apply_edit

# Batch corrections made by this module are recorded in photo_edits with this
# edited_by value, distinguishing them at a glance from a real interactive
# review_tool.py edit (which records the actual review_users username).
BATCH_FIX_EDITOR = "date-audit-batch"
# A separate tag for find_sequence_interpolation_candidates()'s corrections
# (see its docstring) -- distinguishable in the photo_edits audit trail from
# the folder-name-based BATCH_FIX_EDITOR corrections above, since they're a
# different (day/time-precision, human-directed) basis, not the same rule.
SEQUENCE_FIX_EDITOR = "date-audit-sequence"

# Camera-roll filenames this module knows how to parse a sequence number
# out of -- IMG_####, VID_####, etc., with or without a trailing " N"/"(N)"
# duplicate-copy suffix some export tools add (e.g. "IMG_4799 3.JPG").
_IMG_NUM_RE = re.compile(r"^(?:IMG|VID|PXL|PANO)_(\d+)", re.I)


def _sequence_number(filename: str) -> int | None:
    m = _IMG_NUM_RE.match(filename)
    return int(m.group(1)) if m else None

# A cluster below this size is very unlikely to be worth a human's time to
# review as a candidate batch-date artifact — real single-day events (a
# grandkid's birthday, a handful of screenshots) produce a handful of
# photos; this project's real day-count distribution (167k rows across
# 3,442 distinct days, checked this session) has a median of 13 and the
# real bug clusters found so far were in the hundreds-to-thousands. Callers
# can always pass a lower --min-count to see further down the list.
DEFAULT_MIN_COUNT = 20
DEFAULT_TOP_N = 30


@dataclass
class ClusterInfo:
    date: str                      # YYYY-MM-DD (calendar day, not full timestamp)
    count: int
    source_counts: dict[str, int]  # date_source -> count, for this day
    distinct_timestamps: int       # how many distinct exact date_taken values make up `count`

    @property
    def filesystem_count(self) -> int:
        return self.source_counts.get("filesystem", 0)

    @property
    def filesystem_fraction(self) -> float:
        return self.filesystem_count / self.count if self.count else 0.0


def find_suspicious_clusters(
    conn: sqlite3.Connection, min_count: int = DEFAULT_MIN_COUNT, top_n: int = DEFAULT_TOP_N
) -> list[ClusterInfo]:
    """Step 0: day-level clustering, sorted by count descending — a pure
    detection aid, not an auto-decide mechanism (a genuinely busy single day
    — a wedding, a holiday — can also produce a high count; a human still
    has to judge each candidate). Grouped by calendar day (substr of the
    ISO date_taken), not the exact timestamp: grouping by the full
    timestamp instead surfaces a different, unrelated pattern (many old
    scanned photos across DIFFERENT years sharing one identical
    sub-second-precision time-of-day, presumably burned in by old scanning
    software) rather than the same-day batch-import artifact this is meant
    to catch.

    `source_counts`/`filesystem_fraction` are included because they turned
    out to be the single best discriminator found during this session's own
    investigation: real large events resolve via 'exif'/'container' and
    naturally spread across many distinct timestamps in the day, while
    every batch-date artifact found so far was 100% (or very heavily)
    'filesystem'-sourced. Still just a signal for the human reviewing the
    list, not a filter applied here — a 100%-filesystem day is not silently
    dropped or auto-flagged as wrong.
    """
    rows = conn.execute(
        """
        SELECT substr(date_taken, 1, 10) AS day, date_source, COUNT(*) as cnt
        FROM photos
        WHERE date_taken IS NOT NULL
        GROUP BY day, date_source
        """
    ).fetchall()

    by_day: dict[str, dict[str, int]] = {}
    for day, source, cnt in rows:
        by_day.setdefault(day, {})[source] = cnt

    distinct_ts_rows = conn.execute(
        """
        SELECT substr(date_taken, 1, 10) AS day, COUNT(DISTINCT date_taken) as n
        FROM photos
        WHERE date_taken IS NOT NULL
        GROUP BY day
        """
    ).fetchall()
    distinct_ts = {day: n for day, n in distinct_ts_rows}

    clusters = [
        ClusterInfo(
            date=day,
            count=sum(sources.values()),
            source_counts=sources,
            distinct_timestamps=distinct_ts.get(day, 0),
        )
        for day, sources in by_day.items()
    ]
    clusters = [c for c in clusters if c.count >= min_count]
    clusters.sort(key=lambda c: c.count, reverse=True)
    return clusters[:top_n]


@dataclass
class SampleCheck:
    file_hash: str
    current_path: str
    original_path: str
    filename: str
    date_taken: str
    date_source: str
    file_missing: bool = False
    live_has_exif_datetime: bool | None = None   # None = couldn't check (file missing/unreadable)
    live_mtime_matches: bool | None = None        # live file's current mtime vs. recorded date_taken (within 5s)
    original_folder_year_month: str | None = None  # e.g. "2012-12", parsed from original_path, if any
    folder_disagrees_with_date: bool | None = None  # True if original_folder_year_month != date_taken's year-month


@dataclass
class InspectionReport:
    date: str
    total: int
    source_counts: dict[str, int]
    samples: list[SampleCheck]


def find_year_month_in_parts(parts: tuple[str, ...] | list[str]) -> tuple[int, int] | None:
    """Looks for a bare 4-digit year path component immediately followed by
    either a 'YYYY-MM' component (optionally with trailing annotation, e.g.
    'YYYY-MM (MonthName)' — see below) or a bare 1-2 digit month component,
    anywhere in a path's directory parts — the pre-existing
    `YYYY\\YYYY-MM\\` / `YYYY\\MM\\` folder convention this user has used for
    their library since before this project existed (see CLAUDE.md: "Some
    subfolders under E:\\Pics ... already had their own pre-existing nested
    YYYY\\YYYY-MM structure before the tool ever ran"). Returns (year, month)
    for the first match found, or None.

    Real finding from cross-referencing against the user's pre-Phase-1
    backup drive (this session): the exact-match 'YYYY-MM' check alone
    missed a large, real convention from the library's early years (2001-
    2008ish) — 'YYYY-MM (MonthName)', e.g. '2001-02 (Feb)' — costing 3,229
    otherwise-recoverable rows (46% of what looked "unfixable" before this
    fix). Matching now accepts any 'YYYY-MM' PREFIX of the next component
    (the month digits immediately following 'YYYY-', with whatever comes
    after them — a space, '(', end-of-string — not itself a digit, so a
    genuinely different 3+ digit suffix like 'YYYY-023' doesn't false-match
    as month '02')."""
    for i in range(len(parts) - 1):
        p = parts[i]
        nxt = parts[i + 1]
        if len(p) == 4 and p.isdigit():
            year = int(p)
            if not (1990 <= year <= 2030):
                continue
            if nxt.startswith(p + "-"):
                rest = nxt[len(p) + 1:]
                if len(rest) >= 2 and rest[:2].isdigit() and (len(rest) == 2 or not rest[2].isdigit()):
                    month = int(rest[:2])
                    if 1 <= month <= 12:
                        return year, month
            elif nxt.isdigit() and 1 <= len(nxt) <= 2 and 1 <= int(nxt) <= 12:
                return year, int(nxt)
    return None


def inspect_cluster(conn: sqlite3.Connection, date_str: str, sample_size: int = 10) -> InspectionReport:
    """Step 1: deep-dive investigation for one suspicious day, repeatable for
    any date the day-level list surfaces (not hardcoded to 2023-10-29).
    Re-checks live evidence for a sample of real rows rather than trusting
    only what's already in the DB, per the investigation this was modeled
    on: does the file's live EXIF actually carry a capture-date tag today?
    Does the live filesystem mtime still match what's recorded? Does the
    file's ORIGINAL path encode a year/month that disagrees with the
    resolved date (the one confirmed-fixable pattern — see module
    docstring)?
    """
    from PIL import Image
    from PIL.ExifTags import IFD

    EXIF_DATETIME_ORIGINAL = 36867
    EXIF_DATETIME_DIGITIZED = 36868

    rows = conn.execute(
        "SELECT date_source, COUNT(*) FROM photos WHERE substr(date_taken,1,10)=? GROUP BY date_source",
        (date_str,),
    ).fetchall()
    source_counts = {source: cnt for source, cnt in rows}
    total = sum(source_counts.values())

    sample_rows = conn.execute(
        """SELECT file_hash, current_path, original_path, filename, date_taken, date_source
           FROM photos WHERE substr(date_taken,1,10)=? ORDER BY file_hash LIMIT ?""",
        (date_str, sample_size),
    ).fetchall()

    samples: list[SampleCheck] = []
    for file_hash, current_path, original_path, filename, date_taken, date_source in sample_rows:
        chk = SampleCheck(
            file_hash=file_hash, current_path=current_path, original_path=original_path,
            filename=filename, date_taken=date_taken, date_source=date_source,
        )
        found = find_year_month_in_parts(Path(original_path).parts)
        if found:
            yr, mo = found
            chk.original_folder_year_month = f"{yr:04d}-{mo:02d}"
            chk.folder_disagrees_with_date = chk.original_folder_year_month != date_taken[:7]

        p = Path(current_path)
        try:
            st = p.stat()
        except OSError:
            chk.file_missing = True
            samples.append(chk)
            continue

        try:
            from datetime import datetime
            recorded = datetime.fromisoformat(date_taken)
            chk.live_mtime_matches = abs(st.st_mtime - recorded.timestamp()) < 5
        except (ValueError, OverflowError, OSError):
            chk.live_mtime_matches = None

        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".heic"):
            try:
                with Image.open(p) as img:
                    exif = img.getexif()
                    exif_ifd = exif.get_ifd(IFD.Exif) if exif else {}
                    chk.live_has_exif_datetime = bool(
                        exif_ifd.get(EXIF_DATETIME_ORIGINAL) or exif_ifd.get(EXIF_DATETIME_DIGITIZED)
                    )
            except Exception:
                chk.live_has_exif_datetime = None
        samples.append(chk)

    return InspectionReport(date=date_str, total=total, source_counts=source_counts, samples=samples)


def format_inspection_report(report: InspectionReport) -> str:
    lines = [
        f"Date: {report.date}  (total {report.total} photo(s)/video(s))",
        "By date_source: " + ", ".join(f"{k}={v}" for k, v in sorted(report.source_counts.items(), key=lambda kv: -kv[1])),
        "",
        f"Sample of {len(report.samples)} file(s), re-checked live:",
    ]
    for s in report.samples:
        lines.append(f"  {s.filename}  (hash {s.file_hash[:12]})")
        lines.append(f"    current_path:  {s.current_path}")
        lines.append(f"    original_path: {s.original_path}")
        lines.append(f"    date_taken={s.date_taken}  date_source={s.date_source}")
        if s.file_missing:
            lines.append("    LIVE FILE MISSING at current_path — can't re-check EXIF/mtime")
        else:
            if s.live_has_exif_datetime is not None:
                lines.append(f"    live EXIF has a capture-date tag today: {s.live_has_exif_datetime}")
            if s.live_mtime_matches is not None:
                lines.append(f"    live file mtime matches recorded date_taken: {s.live_mtime_matches}")
        if s.original_folder_year_month:
            verdict = "DISAGREES with date_taken -> fixable candidate" if s.folder_disagrees_with_date else "agrees, nothing to fix"
            lines.append(f"    original_path folder encodes {s.original_folder_year_month} -> {verdict}")
        else:
            lines.append("    original_path has no YYYY\\MM-style folder to cross-check against")
        lines.append("")
    return "\n".join(lines)


@dataclass
class FixCandidate:
    file_hash: str
    current_path: str
    old_date_taken: str
    old_date_source: str
    new_date_taken: str          # ISO date/datetime -- precision depends on `basis` (see below)
    corrected_year_month: str    # e.g. "2012-12", for display
    basis: str = "folder"        # "folder" (month precision, find_fix_candidates) |
                                  # "sequence-exact" | "sequence-interpolated" (day/time precision,
                                  # find_sequence_interpolation_candidates)


@dataclass
class FixPreview:
    candidates: list[FixCandidate]
    total_filesystem_rows: int
    already_agrees_count: int      # pattern found, but already matches date_taken -- nothing to do
    no_pattern_count: int          # no recoverable signal at all (e.g. the 2023-10-29/"katya 2024" case)
    already_manually_edited_count: int  # excluded from candidates -- a human already fixed date_taken here

    @property
    def unfixable_count(self) -> int:
        return self.no_pattern_count


def find_fix_candidates(conn: sqlite3.Connection) -> FixPreview:
    """Step 2 (dry-run half): scans every date_source='filesystem' row in
    the whole DB (not scoped to the top-N clusters Step 0 surfaces — this is
    the row-level, evidence-based rule from Step 1, which is a strictly
    higher-confidence signal than "this row happens to belong to a big
    cluster") and classifies each one:

    - FIXABLE: original_path encodes a YYYY\\MM/YYYY\\YYYY-MM folder (the
      user's own pre-existing organization, predating this project) whose
      year-month disagrees with the resolved date_taken. Proposed
      correction is `{year}-{month}-01T00:00:00` — month precision only,
      since a folder name alone doesn't tell us the exact day. This is the
      ONLY pattern this module fixes automatically.
    - Already agrees / no pattern found: not a candidate, nothing proposed.
    - Already manually edited (Phase 2f `photo_edits` has a 'date_taken'
      entry for this file): excluded even if it would otherwise match the
      fixable pattern — a human's own correction always wins and is never
      silently overwritten by this batch tool.

    Deliberately does NOT touch the 2023-10-29-style "no recoverable date"
    rows — see module docstring for why guessing at a value there would be
    worse than leaving them flagged.
    """
    rows = conn.execute(
        "SELECT file_hash, current_path, original_path, date_taken FROM photos WHERE date_source = 'filesystem'"
    ).fetchall()

    candidates: list[FixCandidate] = []
    already_agrees = 0
    no_pattern = 0
    already_edited = 0

    for file_hash, current_path, original_path, date_taken in rows:
        found = find_year_month_in_parts(Path(original_path).parts)
        if found is None:
            no_pattern += 1
            continue
        year, month = found
        corrected_ym = f"{year:04d}-{month:02d}"
        if corrected_ym == date_taken[:7]:
            already_agrees += 1
            continue
        if has_manual_edit(conn, file_hash, "date_taken"):
            already_edited += 1
            continue
        candidates.append(FixCandidate(
            file_hash=file_hash,
            current_path=current_path,
            old_date_taken=date_taken,
            old_date_source="filesystem",
            new_date_taken=f"{corrected_ym}-01T00:00:00",
            corrected_year_month=corrected_ym,
        ))

    return FixPreview(
        candidates=candidates,
        total_filesystem_rows=len(rows),
        already_agrees_count=already_agrees,
        no_pattern_count=no_pattern,
        already_manually_edited_count=already_edited,
    )


def format_fix_preview(preview: FixPreview, max_lines: int = 200) -> str:
    lines = [
        f"{len(preview.candidates)} photo(s)/video(s) would be corrected "
        f"(month precision only — see below):",
        "",
    ]
    for c in preview.candidates[:max_lines]:
        lines.append(
            f"  {c.current_path}\n"
            f"    {c.old_date_taken} (date_source=filesystem)  ->  {c.new_date_taken} "
            f"(from its original folder, {c.corrected_year_month})"
        )
    if len(preview.candidates) > max_lines:
        lines.append(f"  ... and {len(preview.candidates) - max_lines} more (truncated for display)")
    lines.append("")
    lines.append(
        f"Of {preview.total_filesystem_rows} total filesystem-sourced rows: "
        f"{len(preview.candidates)} fixable, "
        f"{preview.already_agrees_count} already agree with their original folder (nothing to do), "
        f"{preview.already_manually_edited_count} already manually edited (left alone), "
        f"{preview.no_pattern_count} have no recoverable signal at all — "
        f"these remain flagged as low-confidence 'filesystem'-sourced dates and need manual "
        f"research (e.g. checking the original phone/cloud backup) if the exact date matters."
    )
    return "\n".join(lines)


@dataclass
class ApplyFixStats:
    candidates: int = 0
    applied: int = 0
    skipped_manual_edit: int = 0  # edited between preview and apply -- re-checked at apply time too
    errors: int = 0

    def summary(self) -> str:
        return (
            f"candidates={self.candidates} applied={self.applied} "
            f"skipped_manual_edit={self.skipped_manual_edit} errors={self.errors}"
        )


def apply_fixes(
    conn: sqlite3.Connection,
    candidates: list[FixCandidate],
    logger: logging.Logger,
    progress_cb: Callable[[int, int], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
    edited_by: str = BATCH_FIX_EDITOR,
) -> ApplyFixStats:
    """Step 2 (apply half): writes each candidate's corrected date_taken via
    src.edits.apply_edit (field_name='date_taken'), the exact same generic,
    audited write path review_tool.py's interactive edit endpoint uses --
    this batch tool is not a second write mechanism, it's the same one
    called many times. Every write lands in `photo_edits` with
    edited_by=BATCH_FIX_EDITOR, so it's distinguishable from a real
    interactive edit in the audit trail, and apply_edit's own date_taken
    branch already sets date_source='manual', which permanently protects
    the corrected value from ever being reconsidered by a future run of
    this same batch tool (re-running find_fix_candidates() afterward will
    no longer see these rows, since they're no longer date_source=
    'filesystem').

    Re-checks has_manual_edit() at apply time (not just relying on the
    preview snapshot) in case a human edited this exact photo's date via
    review_tool.py in between preview and apply — that edit always wins.
    """
    stats = ApplyFixStats(candidates=len(candidates))
    total = len(candidates)
    for i, c in enumerate(candidates, start=1):
        if stop_check is not None and stop_check():
            logger.info(f"STOPPED by user request after {stats.applied}/{total}.")
            break
        try:
            if has_manual_edit(conn, c.file_hash, "date_taken"):
                stats.skipped_manual_edit += 1
                logger.info(f"SKIP date-audit fix (manually edited since preview) hash={c.file_hash[:12]} {c.current_path}")
            else:
                apply_edit(conn, c.file_hash, "date_taken", c.new_date_taken, edited_by, logger=logger)
                stats.applied += 1
                logger.info(
                    f"OK date-audit fix hash={c.file_hash[:12]} {c.current_path}: "
                    f"{c.old_date_taken} -> {c.new_date_taken}"
                )
        except Exception as e:
            stats.errors += 1
            logger.error(f"ERROR applying date-audit fix to {c.current_path}: {e!r}")
        if progress_cb is not None:
            progress_cb(i, total)

    logger.info(f"Date-audit batch fix complete: {stats.summary()}")
    return stats


@dataclass
class InterpolationStats:
    total_targets: int = 0        # rows in the target set within [img_min, img_max] with no filename number
    no_sequence_number: int = 0   # target filename didn't parse as IMG_####-style at all
    exact_matches: int = 0        # target's own sequence number also appears in the reference set
    interpolated: int = 0         # bracketed between two reference points, no exact match
    extrapolated_skipped: int = 0  # outside the reference set's own number range -- not proposed
    already_manually_edited: int = 0

    def summary(self) -> str:
        return (
            f"total_targets={self.total_targets} exact={self.exact_matches} "
            f"interpolated={self.interpolated} extrapolated_skipped={self.extrapolated_skipped} "
            f"no_sequence_number={self.no_sequence_number} already_manually_edited={self.already_manually_edited}"
        )


def find_sequence_interpolation_candidates(
    conn: sqlite3.Connection,
    target_prefix: str,
    reference_prefix: str,
    img_min: int | None = None,
    img_max: int | None = None,
) -> tuple[list[FixCandidate], InterpolationStats]:
    """A second, human-directed batch-fix pattern, distinct from
    find_fix_candidates()'s folder-name rule — for when the user recognizes
    a specific batch of `date_source='filesystem'` photos as belonging to a
    real event they can point to an already-correctly-dated folder for
    (e.g. "these look like the Feb 2020 trip in E:\\Pics\\2020\\2020-02"),
    even though there's no automatic signal (no matching original folder,
    no exact-hash duplicate) tying the two together.

    The insight this exploits: camera-roll filenames (`IMG_####`, etc.) are
    a monotonically-increasing sequence number for a given device — so if
    a `reference_prefix` folder full of real (exif/container-sourced)
    dates spans a range of these numbers, a `target_prefix` folder's
    misdated files sharing that SAME number range were very likely taken
    around the same time, even if the files themselves aren't byte-
    identical (e.g. a lost-EXIF re-export of the same shot — confirmed on
    a real 1,705-file case this session: 0 exact-hash duplicates existed
    between the two folders, yet 99% of the target files' sequence numbers
    matched a reference number EXACTLY, i.e. the reference set almost
    certainly has a different export of the very same photo).

    For each target row (date_source='filesystem', original_path under
    target_prefix, sequence number within [img_min, img_max] if given):
      - EXACT match: the same sequence number exists in the reference set
        (current_path under reference_prefix, date_source in
        ('exif','container')) — use that reference date directly (the
        median, if more than one reference file shares the number, e.g.
        edited variants of one shot). Day/time precision.
      - INTERPOLATED: no exact match, but the number falls between two
        reference numbers — linearly interpolate the timestamp between
        them by number distance. Still day-level precision in practice
        (photo-taking rate rarely varies enough within one trip to shift
        the interpolated day), just not as certain as an exact match.
      - EXTRAPOLATED (skipped, not proposed): the number falls outside the
        reference set's own range entirely — this function refuses to
        guess beyond the bracketed evidence; report it, don't fix it.

    This is deliberately NOT run automatically the way find_fix_candidates()
    is — it needs a human to have already recognized the target batch and
    named a specific reference folder; see main.py audit-dates
    interpolate-preview / CLAUDE.md's Date Cluster Audit entry for the real
    case this was built for.
    """
    stats = InterpolationStats()

    ref_rows = conn.execute(
        "SELECT filename, date_taken FROM photos WHERE current_path LIKE ? AND date_source IN ('exif','container')",
        (f"{reference_prefix}%",),
    ).fetchall()
    ref_dates_by_num: dict[int, list[datetime]] = {}
    for filename, date_taken in ref_rows:
        n = _sequence_number(filename)
        if n is None:
            continue
        try:
            dt = datetime.fromisoformat(date_taken)
        except ValueError:
            continue
        ref_dates_by_num.setdefault(n, []).append(dt)
    ref_nums = sorted(ref_dates_by_num.keys())

    def _ref_date(n: int) -> datetime:
        dts = sorted(ref_dates_by_num[n])
        return dts[len(dts) // 2]  # median, in case of multiple edited variants sharing one number

    candidates: list[FixCandidate] = []
    if ref_nums:  # nothing to bracket against otherwise -- every target would be "extrapolated"
        target_rows = conn.execute(
            "SELECT file_hash, filename, current_path, date_taken FROM photos "
            "WHERE original_path LIKE ? AND date_source = 'filesystem'",
            (f"{target_prefix}%",),
        ).fetchall()

        for file_hash, filename, current_path, old_date_taken in target_rows:
            n = _sequence_number(filename)
            if n is None:
                stats.no_sequence_number += 1
                continue
            if (img_min is not None and n < img_min) or (img_max is not None and n > img_max):
                continue
            stats.total_targets += 1

            if n in ref_dates_by_num:
                new_dt, basis = _ref_date(n), "sequence-exact"
                stats.exact_matches += 1
            else:
                idx = bisect.bisect_left(ref_nums, n)
                if idx == 0 or idx == len(ref_nums):
                    stats.extrapolated_skipped += 1
                    continue
                lo, hi = ref_nums[idx - 1], ref_nums[idx]
                lo_dt, hi_dt = _ref_date(lo), _ref_date(hi)
                frac = (n - lo) / (hi - lo)
                interp_ts = lo_dt.timestamp() + frac * (hi_dt.timestamp() - lo_dt.timestamp())
                new_dt, basis = datetime.fromtimestamp(interp_ts), "sequence-interpolated"
                stats.interpolated += 1

            if has_manual_edit(conn, file_hash, "date_taken"):
                stats.already_manually_edited += 1
                continue
            candidates.append(FixCandidate(
                file_hash=file_hash,
                current_path=current_path,
                old_date_taken=old_date_taken,
                old_date_source="filesystem",
                new_date_taken=new_dt.isoformat(),
                corrected_year_month=new_dt.strftime("%Y-%m"),
                basis=basis,
            ))

    return candidates, stats


def format_interpolation_preview(
    candidates: list[FixCandidate], stats: InterpolationStats, max_lines: int = 200
) -> str:
    lines = [
        f"{len(candidates)} photo(s)/video(s) would be corrected using neighboring sequence numbers "
        f"from the reference folder ({stats.exact_matches} exact matches, {stats.interpolated} interpolated):",
        "",
    ]
    for c in candidates[:max_lines]:
        basis_label = "exact match" if c.basis == "sequence-exact" else "interpolated"
        lines.append(
            f"  {c.current_path}\n"
            f"    {c.old_date_taken} (date_source=filesystem)  ->  {c.new_date_taken}  ({basis_label})"
        )
    if len(candidates) > max_lines:
        lines.append(f"  ... and {len(candidates) - max_lines} more (truncated for display)")
    lines.append("")
    lines.append(
        f"Of {stats.total_targets} target row(s) in range: {stats.exact_matches} exact + "
        f"{stats.interpolated} interpolated = {len(candidates) + stats.already_manually_edited} correctable, "
        f"{stats.extrapolated_skipped} outside the reference folder's own number range (not guessed at), "
        f"{stats.already_manually_edited} already manually edited (left alone), "
        f"{stats.no_sequence_number} had no parseable sequence number in their filename."
    )
    return "\n".join(lines)
