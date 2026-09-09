#!/usr/bin/env python
"""Photo Organizer — Desktop Dashboard (Tkinter)

    venv\\Scripts\\python dashboard.py

A GUI wrapper around the same Phase 1 code main.py's CLI commands use —
it does not duplicate any of the scan/date-resolve/copy-verify-delete
logic. Source-folder edits go through src.pick_sources / src.config exactly
like `main.py pick-sources`; both Dry Run and Run for real call
src.organize.run_phase1, the same function `main.py scan` / `main.py run`
call. The log viewer reads the same logs/organize_<timestamp>.log files the
CLI writes — there's no second logging path.

Panels:
- Source folders — view / add (native multi-select picker) / remove
- Dry Run — safe preview, populates the results panel with counts by outcome
- Run for real — copy-verify-delete, gated behind a confirmation dialog
- Progress — live "N / total processed" while a run is active
- Phase 2: Captioning — runs src.caption.run_phase2 (same function
  `main.py caption` calls) against the whole dest_root tree via Ollama.
  Non-destructive (read-only against photos, append-only to
  captions.jsonl), so no confirmation gate like Phase 1's — just an FYI
  about expected duration. Independent of the Phase 1 panel above: both
  can run at once (confirmed no GPU contention, see CLAUDE.md), each with
  its own progress bar/worker thread, AND its own dedicated live log tail
  right in its own panel — it does not share/hijack the Log viewer panel
  below, which stays focused on Phase 1's (much bigger) logs and manual
  browsing. Both panels' logs are still the exact same
  logs/organize_<timestamp>.log files the CLI writes — no second logging
  path, just two independent *views* onto that shared log directory.
- GPS Extraction — Phase 2b follow-up: runs src.gps_backfill.run_gps_extraction
  (same function `main.py extract-gps` calls) against whatever's already in
  the DB from Phase 1/1b — reads EXIF GPS + offline reverse-geocodes into
  location_name, gps_lat/gps_lon directly on the `photos` table. Added
  because the user was confused why location data wasn't showing up in
  review_tool.py after captioning finished: extract-gps is a deliberately
  separate step (not automatic), and until now there was no GUI way to run
  it at all, CLI-only. Unlike Phase 2's multi-day captioning job, this runs
  at ~100 files/sec (minutes, not days, even at 100k+ files), so its
  confirmation dialog is a short FYI, not Phase 1's scary warning or Phase
  2's "expect several days" framing. Independent worker thread/state from
  every other panel, same "own progress bar + own dedicated live log tail"
  pattern as Phase 2's Captioning panel. Resource contention checked, not
  assumed: src/gps_resolver.py does no GPU/network work (Pillow EXIF read +
  reverse_geocoder's offline in-memory k-d tree lookup, mode=1
  single-threaded — see CLAUDE.md), so no GPU contention with a concurrent
  Phase 2 captioning run. The one real difference from Phase 2 (which only
  appends to captions.jsonl): GPS extraction writes to the SAME `photos`
  table Phase 1/1b's organize.py writes to, via its own separate sqlite3
  connection — verified via a synthetic concurrent-commit test (a
  Phase-1-style rapid-commit writer hammering the same temp DB file while
  GPS extraction ran against it) that this does not produce
  `sqlite3.OperationalError: database is locked` errors and costs only
  minor throughput overhead (~15% in that test) — Python's sqlite3 default
  busy-timeout retry handles the brief lock contention transparently at
  this write rate.
  **Real bug found and fixed this follow-up session, worth knowing if a
  future panel is added the same way:** Phase 1/Captioning/GPS-extraction
  each called `setup_logging()` with the same default logger name, which
  resolves to one shared `logging.Logger` object — so whichever worker
  called it most recently silently repointed that ONE logger's handler at
  its own file, redirecting every other already-running worker's log
  lines into it too (this is how GPS's lines ended up in the Captioning
  panel's box — see CLAUDE.md/TODO.md). Each worker below now passes its
  own `logger_name` to `setup_logging()` so this can't happen again — a
  future panel/worker must do the same (pick its own distinct name) rather
  than call `setup_logging()` with the default.
- Log viewer — tails the active Phase 1 run's log, or browse any older
  logs/*.log (including old Phase 2/GPS-extraction runs, if you want to
  review one)
- Review Users — Phase 2d follow-up: manages review_tool.py's login
  credentials (config.yaml's review_users). Deliberately lives HERE, not in
  review_tool.py itself — account management is an owner/admin action, and
  review_tool.py is the internet-facing surface invited guests use, so it
  shouldn't carry a way to add/remove logins. Calls the exact same
  src.config.add_or_update_review_user / remove_review_user /
  list_review_usernames functions `main.py review-user add/list/remove`
  already uses (plus src.auth.hash_password for the same werkzeug scrypt
  hash) — no duplicated validation/hashing logic. Password entry uses
  Tkinter's masked Entry (show="*"), not a terminal getpass prompt, per the
  user's explicit "no terminal" instruction for this panel. Collapsed by
  default (start_expanded=False) — this is an infrequent admin action, not
  something to leave taking up space ahead of the Log viewer.
- Remote Access — Phase 2d follow-up, added directly in response to a real
  incident (see CLAUDE.md/TODO.md): four separate stale review_tool.py
  processes were all found LISTENING on port 5151 at once, and the one
  actually serving a phone's request predated Phase 2d's login code, so it
  let the phone straight through with no auth prompt. Two independent
  pieces:
    1. review_tool.py status — NOT a simple up/down probe (a probe would
       have seen SOMETHING answering that night and reported "running",
       never surfacing that it was the wrong, stale instance). Instead
       queries src.port_check.listening_pids(cfg.review_tool_port) — the
       same `netstat -ano` approach used to diagnose the real incident.
       A proper 3-state indicator (Phase 2e), each with its own label
       style so the state reads at a glance, not just from the text:
       Running (exactly 1 PID, "Ok.TLabel", green), Not Running (zero
       PIDs, "Down.TLabel", orange — this is the state that was missing
       before Phase 2e: a user seeing a remote 502 had no dashboard-visible
       way to tell "the tunnel is fine, the local app just isn't running"),
       and Warning: multiple instances (more than 1 PID, "Warn.TLabel",
       red — the original incident). Refreshes on a background timer plus
       a manual "Refresh" button. A "Force Restart" button sits right next
       to Refresh (CLAUDE.md rule 11 / TODO.md's "One-click restart"
       item): calls src.port_check.restart_review_tool() on a background
       thread — the exact same function "Restart Review Tool.bat" shells
       out to via `main.py restart-review-tool` (rule 7: one mechanism,
       two front ends) — which kills every process currently bound to the
       configured port (not just the first; the original incident found
       four at once), waits for the port to actually free, then launches
       a fresh review_tool.py. The status label above is refreshed
       immediately once the restart worker reports back, rather than
       waiting for the next 5s tick.
    2. Cloudflare Tunnel Start/Stop — spawns/terminates
       `cloudflared tunnel run <cfg.cloudflare_tunnel_name>` as a
       subprocess (same "background worker, only touch Tk widgets from the
       main thread via self.msg_queue" pattern as the Phase 1/2 panels
       above), with its console output tailed live into its own Text
       widget (same look as the Log viewer / Phase 2 log tail, but sourced
       from the subprocess's own stdout pipe rather than a log file, since
       cloudflared writes to console, not to logs/organize_*.log). Stop
       terminates (falling back to kill after a timeout) — and so does
       closing the whole dashboard window (see _on_close), so a cloudflared
       process this dashboard started never survives the dashboard closing.
       This panel controls an ALREADY-configured tunnel (needs
       %USERPROFILE%\\.cloudflared\\config.yml to already exist, same
       one-time setup Launch Review Tunnel.bat has always needed) — it
       doesn't do first-time Cloudflare account linking.
  Collapsed by default (start_expanded=False), same reasoning as Review
  Users — but its background status-refresh timer keeps running either
  way, so reopening it always shows current state immediately, never a
  stale snapshot from before it was collapsed.
- Phase 2g — Video Thumbnails: runs src.thumbnail_backfill.run_thumbnail_extraction
  (same function `main.py extract-thumbnails` calls) — decodes and caches a
  real first-frame JPEG per video to data/thumbnails/<file_hash>.jpg, which
  review_tool.py's grid now serves in place of the old generic play-icon
  placeholder (falling back to that placeholder for any video not yet
  processed). Same "own worker thread, own progress bar/results box/
  dedicated live log tail" pattern as the GPS Extraction panel above, own
  distinct logger_name ("photo_organizer.thumbs") for the same reason (see
  setup_logging()'s docstring). Real throughput benchmarked before this was
  built, not assumed (per spec) — ~0.1s/video (a 40-file random real sample,
  0 failures), roughly 10x slower per-file than GPS's EXIF read but nowhere
  near Phase 2 captioning's per-image cost; a full run against this
  library's real ~12,400 video rows is on the order of 20-25 minutes, so
  the confirmation dialog is a short FYI like GPS's, not Phase 1/2's bigger
  warnings. Resumability is disk-based, not a DB column (a cache file
  already present at that hash's path means "done" — see
  thumbnail_backfill.py's module docstring for why that's a deliberate
  design choice, not an oversight) — so unlike GPS extraction, this never
  writes to the `photos` table at all, meaning there's no DB-write
  contention question to verify here the way GPS's panel needed to.
  Collapsed by default (start_expanded=False), placed right after GPS
  Extraction, same "occasional batch action" reasoning as that panel.
- Date Cluster Audit — detects and, for one confirmed root-cause pattern,
  fixes batch-date artifacts (a whole folder of photos/videos landing on
  one wrong date because Phase 1's date-resolution chain fell back to an
  unreliable signal for all of them at once — see src/date_audit.py's
  module docstring for the full investigation this was built from,
  triggered by a real ~3,273-photo cluster the user found dated
  2023-10-29). Calls the exact same src.date_audit functions `main.py
  audit-dates list/inspect/preview-fix/apply-fix` use — no duplicated
  query/classification logic between the CLI and this panel (rule 7).
    - "Scan for Suspicious Dates" (Step 0): fills a table of day-level
      clusters sorted by count, with a filesystem-fallback percentage per
      day — the strongest signal found for this kind of artifact, but
      surfaced for human judgment only, never auto-flagged as wrong (a
      real busy day can produce a high count too).
    - "Inspect Selected Date" (Step 1): a deep-dive report for one day —
      re-checks live EXIF/filesystem evidence for a sample of real files,
      repeatable for any date this surfaces, not hardcoded to the original
      incident.
    - "Preview Fix (Dry Run)" / "Apply Fix" (Step 2): preview shows every
      proposed old-date -> new-date correction across the whole library
      before anything changes; Apply is only enabled after a preview has
      been run in this same session (forces a look before committing) and
      writes go through src.edits.apply_edit — the same generic, audited
      path review_tool.py's own edit endpoint uses — so every correction
      lands in the photo_edits audit table and is permanently protected
      from a future automated overwrite, exactly like a real interactive
      edit. DB-metadata-only: never moves, renames, or re-sorts a file.
  Read-only until Apply is clicked, and Apply only ever touches rows this
  module has high confidence about (see src/date_audit.py) — everything
  else is left flagged, not guessed at. Own worker thread/progress bar/log
  tail for Apply (Scan/Inspect/Preview are fast, synchronous queries — no
  worker thread needed for those, same reasoning as this panel's own
  underlying queries being sub-second even at this library's 167k-row real
  scale). No schema changes — reuses Phase 2f's existing photo_edits table
  and photos.date_taken/date_source columns, so no review_tool.py/rule 12
  migration concern here. Collapsed by default, placed after Phase 2g.
  **Follow-up session (real usability feedback):** added two one-click
  shortcuts above the general Scan/Inspect/Preview/Apply workflow --
  "Apply Folder-Rule Fix (822 files)" and "Apply Sequence Fix — Feb 2020
  Vacation (1,705 files)" (see _on_date_audit_apply_known_folder_fix /
  _on_date_audit_apply_known_sequence_fix). Both corrections were already
  fully investigated and verified against a copy of the real production DB
  in a prior session (see TODO.md) -- the user shouldn't have to re-run
  Scan/Inspect/fill in prefix fields to apply a fix that isn't a mystery
  anymore. Each still runs a real preview against the live DB and requires
  a confirm click before writing (same dry-run-before-apply guarantee,
  just no manual setup); the general workflow below is unchanged and stays
  available for finding *future* new suspicious dates. Also investigated
  (headless repro, see session summary) why "Preview Fix (Dry Run)"
  appeared to do nothing when clicked without Scan/Inspect first: it has
  no such dependency and was never silently failing -- it populated
  date_audit_results_text correctly every time. The real cause was the
  scrolling gap fixed below: this panel's body alone requires ~900px,
  easily pushing its own results box (near the bottom of an already very
  tall panel) below the visible window with nothing to scroll it back into
  view. _set_date_audit_results_text() now auto-scrolls its result into
  view as an extra safety net on top of the general scrolling fix.

- Phase 3 — Face Detection (Session 1 scope only — see TODO.md/CLAUDE.md):
  runs src.face_detection.run_face_detection (same function `main.py
  detect-faces` calls) against every already-organized photo in the DB not
  yet scanned, via insightface's FaceAnalysis (buffalo_l pack). Same "own
  worker thread, own progress bar/results box/dedicated live log tail"
  pattern as every other batch panel, own distinct logger_name
  ("photo_organizer.faces"). Read-only against the DB (no init_db()-guarded
  write path needed here beyond the standard rule-12 startup call) and
  read-only against photos — output goes to data/faces.jsonl (one line per
  DETECTED FACE, not per photo) + data/faces_scanned.jsonl (its own
  resumability checkpoint — see src/face_detection.py's module docstring for
  why one file alone can't answer "already scanned?" when a photo can
  legitimately have zero faces).
  **Real GPU contention, unlike every earlier batch panel**: this is the
  first Phase-3-or-earlier panel that actually shares the GPU with Phase 2's
  Ollama captioning (insightface uses onnxruntime's CUDAExecutionProvider —
  see build_face_analysis()'s explicit "confirm, don't assume" provider
  check). The confirmation dialog says so plainly rather than reusing GPS/
  Thumbnail's "no GPU contention" framing, which would be actively wrong
  here. Collapsed by default, placed after Date Cluster Audit.
  **Architecture note worth knowing without re-deriving** (see CLAUDE.md
  rule 13): this was originally planned to require a second, separate
  Python 3.11-3.13 environment (insightface had no official Python 3.14
  wheel when that plan was written). Verified during this session that both
  insightface (1.0.1, now a version-agnostic `py3-none-any` wheel) and
  onnxruntime-gpu (1.29.0, official cp314 wheel) install and run cleanly
  directly in this project's existing primary 3.14 venv — no second Python
  install needed, so this is a normal dashboard panel like any other, not
  some special cross-environment invocation.

Every panel is collapsible — a ▼/▶ toggle in its header hides/shows its
body (see _make_collapsible()), so a section you're not using right now
(e.g. Source folders, once already configured, or Phase 1's Run panel
while doing Phase 2 work) can be tucked away to make room for whatever
you actually want to look at, especially the Log viewer at the bottom
(which expands to fill whatever vertical space is free).

**Follow-up session: the whole panel stack now lives inside a scrollable
Canvas** (self._main_canvas / self.scroll_frame, set up in _build_ui;
_make_collapsible packs into self.scroll_frame instead of self.root
directly), not because any one panel was individually broken, but because
this was a general gap -- with 9 stacked panels (up from 3 pre-Phase-2),
several expanded at once (or even just one very tall one, like Date
Cluster Audit) can exceed the window's height with no way to reach what's
below. Mouse wheel scrolls the page from anywhere except over a widget
that already scrolls itself (Text/Listbox/Treeview/Combobox -- see
_MOUSEWHEEL_PASSTHROUGH/_bind_mousewheel_recursive), so scrolling over the
Log viewer's text box or the Date Cluster Audit tree still scrolls that
widget's own content, not the whole page.

Note on _poll_queue / _tail_tick: both are perpetually-self-rescheduling
Tk `after()` callbacks, so a single unhandled exception inside either one
would silently stop ALL future progress/log updates for the rest of the
session (worse under pythonw.exe, which has no console to even show a
traceback — see CLAUDE.md's tqdm/hachoir silent-crash pattern). Each
message/tail call is individually wrapped so one bad one can't take the
whole loop down with it.
"""
from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from src.auth import hash_password
from src.caption import CaptionStats, run_phase2
from src.config import (
    CONFIG_PATH,
    EXAMPLE_CONFIG_PATH,
    Config,
    add_or_update_review_user,
    list_review_usernames,
    load_config,
    remove_review_user,
    save_source_folders,
)
from src.date_audit import (
    BATCH_FIX_EDITOR,
    DEFAULT_MIN_COUNT,
    DEFAULT_TOP_N,
    SEQUENCE_FIX_EDITOR,
    ApplyFixStats,
    FixPreview,
    apply_fixes,
    find_fix_candidates,
    find_sequence_interpolation_candidates,
    find_suspicious_clusters,
    format_fix_preview,
    format_inspection_report,
    format_interpolation_preview,
    inspect_cluster,
)
from src.db import connect, init_db
from src.face_clustering import (
    ClusterStats,
    LoadStats,
    cluster_faces_full,
    count_not_a_person,
    create_person_from_faces,
    crop_face_thumbnail,
    list_clusters,
    list_faces_in_cluster,
    list_not_a_person_faces,
    list_people_for_merge_target,
    load_full_photo_with_bbox,
    mark_faces_not_a_person,
    merge_people,
    move_faces_to_person,
    remove_faces_from_cluster,
    restore_faces_from_undo,
    set_person_auto_match_locked,
    set_person_name,
    snapshot_before_merge,
    snapshot_faces_for_undo,
    undo_merge,
    unmark_faces_not_a_person,
)
from src.face_detection import FaceDetectionStats, run_face_detection
from src.gps_backfill import GpsStats, run_gps_extraction
from src.logging_setup import setup_logging
from src.organize import RunStats, run_phase1
from src.pick_sources import merge_and_save, pick_sources_interactive
from src.port_check import RestartResult, listening_pids, restart_review_tool
from src.relocate import (
    RelocateStats,
    RelocationPreview,
    apply_relocations,
    find_relocation_candidates,
    format_relocation_preview,
)
from src.thumbnail_backfill import ThumbStats, run_thumbnail_extraction


class DashboardApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Photo Organizer — Dashboard")
        self.root.geometry("880x900")
        self.root.minsize(700, 640)

        self.cfg: Config | None = None
        self.worker: threading.Thread | None = None
        self.stop_requested = False
        self.msg_queue: "queue.Queue[tuple]" = queue.Queue()
        self.current_log_path: Path | None = None
        self._log_read_pos = 0
        self._tailing = False

        # Phase 2 (captioning) has its own independent worker/progress —
        # safe to run at the same time as Phase 1 above (no GPU contention,
        # see CLAUDE.md), so it doesn't share Phase 1's busy state.
        self.caption_worker: threading.Thread | None = None
        self.caption_stop_requested = False
        self._caption_log_path: Path | None = None
        self._caption_log_read_pos = 0

        # GPS Extraction panel (Phase 2b follow-up) — also its own
        # independent worker/progress, same reasoning as Phase 2 above (no
        # GPU contention; DB-write contention against Phase 1/1b checked —
        # see module docstring).
        self.gps_worker: threading.Thread | None = None
        self.gps_stop_requested = False
        self._gps_log_path: Path | None = None
        self._gps_log_read_pos = 0

        # Video Thumbnails panel (Phase 2g) — also its own independent
        # worker/progress. Never writes to the DB (see
        # src/thumbnail_backfill.py), so there's no write-contention
        # question to track here the way GPS's panel needed to.
        self.thumbs_worker: threading.Thread | None = None
        self.thumbs_stop_requested = False
        self._thumbs_log_path: Path | None = None
        self._thumbs_log_read_pos = 0

        # Phase 3 — Face Detection panel (Session 1 scope only). Also its
        # own independent worker/progress, but unlike GPS/Thumbnails this
        # one DOES share real GPU load with a concurrent Phase 2 captioning
        # run (insightface's CUDAExecutionProvider) — see the module
        # docstring above and this panel's confirmation dialog.
        self.faces_worker: threading.Thread | None = None
        self.faces_stop_requested = False
        self._faces_log_path: Path | None = None
        self._faces_log_read_pos = 0

        # Phase 3 Session 2 — Face Clustering panel: its own independent
        # worker/progress, same reasoning as every batch panel above. No
        # Cancel button/stop_check here (unlike every other batch panel) --
        # a deliberate difference, not an oversight: run_face_clustering()
        # is a fast in-memory numerical pass (faiss+DBSCAN over embeddings
        # already in the DB, no per-file I/O or network/GPU inference like
        # detect-faces/caption), benchmarked at a few seconds even for this
        # session's real 767-face sample -- there's no realistic multi-hour
        # run here to need interrupting. The Face Labeling window
        # (self.face_labeling_window) is a separate, non-modal Toplevel --
        # only one instance is kept open at a time (re-clicking "Open Face
        # Labeling..." re-focuses the existing window instead of opening a
        # second one against the same DB).
        self.cluster_worker: threading.Thread | None = None
        self._cluster_log_path: Path | None = None
        self._cluster_log_read_pos = 0
        self.face_labeling_window: "FaceLabelingWindow | None" = None

        # Phase 3b — Relocate Files panel: Preview is a fast synchronous
        # query (pure DB + path arithmetic, no file I/O -- see
        # src/relocate.py's module docstring), same reasoning as Date
        # Cluster Audit's own Preview above, but Apply does real per-file
        # copy-verify-delete so it gets the same "own worker thread +
        # progress bar + log tail" treatment as every other batch-writing
        # panel. _relocate_preview caches the last RelocationPreview
        # computed in THIS session so Apply always acts on exactly what was
        # shown, never a fresh (possibly different) query the user never
        # looked at -- Apply is disabled until a preview exists, and
        # cleared again after every Apply (its own re-check at apply time
        # already re-derives fresh state per candidate — see
        # apply_relocations — but the cached preview object itself is
        # stale the moment anything has actually moved).
        self.relocate_worker: threading.Thread | None = None
        self.relocate_stop_requested = False
        self._relocate_log_path: Path | None = None
        self._relocate_log_read_pos = 0
        self._relocate_preview: RelocationPreview | None = None

        # Remote Access panel (Phase 2d follow-up): the cloudflared
        # subprocess this dashboard itself spawned, if any. None whenever
        # no tunnel was started from here (including: never started, or
        # already stopped/exited) — see _tunnel_running().
        self.tunnel_proc: subprocess.Popen | None = None

        # Date Cluster Audit panel: Scan/Inspect/Preview are fast synchronous
        # queries (no worker thread needed — see docstring above), but Apply
        # writes hundreds of rows through apply_edit() so it gets the same
        # "own worker thread + progress bar + log tail" treatment as every
        # other batch-writing panel. _date_audit_preview caches the last
        # FixPreview computed in THIS session so Apply always acts on
        # exactly what was shown, never a fresh (possibly different) query
        # the user never actually looked at — Apply is disabled until a
        # preview exists.
        self.date_audit_worker: threading.Thread | None = None
        self.date_audit_stop_requested = False
        self._date_audit_log_path: Path | None = None
        self._date_audit_log_read_pos = 0
        self._date_audit_preview: FixPreview | None = None
        # Sequence Interpolation sub-section's own cached preview, same
        # "must preview in this session before Apply is enabled" contract
        # as _date_audit_preview above, just a plain candidate list (no
        # dedicated summary dataclass the way FixPreview is).
        self._date_audit_interp_candidates: list | None = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._load_config_or_onboard)
        self.root.after(200, self._poll_queue)
        self.root.after(500, self._tail_tick)
        self.root.after(300, self._port_status_tick)

    # ------------------------------------------------------------------ UI
    # Widget classes that already implement their own mouse-wheel scrolling
    # (Text/Listbox/Treeview all have built-in Tk/ttk class bindings) --
    # _bind_mousewheel_recursive skips these so scrolling over, say, the Log
    # viewer's text box scrolls *that box*, not the whole dashboard.
    _MOUSEWHEEL_PASSTHROUGH = (tk.Text, tk.Listbox, ttk.Treeview, ttk.Combobox, ttk.Scrollbar)

    # Parameters for the already-investigated Sequence Interpolation fix
    # (the Feb 2020 vacation batch, 1,705 files) — exactly the values used
    # to verify this candidate list against a copy of the real production DB
    # in the investigation session (see TODO.md's Date Cluster Audit entry
    # and main.py's own audit-dates interpolate-preview/-apply docstring
    # example, which use these same three values).
    KNOWN_SEQUENCE_TARGET_PREFIX = r"E:\katya 2024"
    KNOWN_SEQUENCE_REFERENCE_PREFIX = r"E:\Pics\2020\2020-02"
    KNOWN_SEQUENCE_IMG_MAX = 7093

    def _make_collapsible(self, title: str, fill: str = "x", expand: bool = False,
                           start_expanded: bool = True) -> ttk.Frame:
        """Builds a section with a header (toggle triangle + title) and a
        body frame the caller packs its real widgets into; returns the
        body. Collapsing just pack_forget()s the body -- other sections
        (especially the Log viewer, which expands to fill whatever's left)
        simply reclaim the freed space through normal pack layout, no extra
        bookkeeping needed. Panels are packed into self.scroll_frame (a
        frame inside a Canvas), not directly into self.root -- with 9
        stacked panels (up from 3 pre-Phase-2), the combined natural height
        can easily exceed the window even with several collapsed, and a
        single very tall panel (e.g. Date Cluster Audit, ~900px on its own
        once expanded) can by itself push content below the visible window
        with no way to reach it. See _build_ui's canvas/scrollbar setup."""
        outer = ttk.Frame(self.scroll_frame, relief="groove", borderwidth=1)
        outer.pack(fill=fill, expand=expand, padx=8, pady=6)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        toggle_btn = ttk.Button(header, width=3)
        toggle_btn.pack(side="left", padx=(2, 4), pady=2)
        ttk.Label(header, text=title, font=("Segoe UI", 9, "bold")).pack(side="left", pady=2)

        body = ttk.Frame(outer)
        state = {"expanded": start_expanded}

        def _apply() -> None:
            if state["expanded"]:
                body.pack(fill=fill, expand=expand, padx=0, pady=(0, 2))
                toggle_btn.configure(text="▼")  # ▼
            else:
                body.pack_forget()
                toggle_btn.configure(text="▶")  # ▶

        def _toggle() -> None:
            state["expanded"] = not state["expanded"]
            _apply()

        toggle_btn.configure(command=_toggle)
        _apply()
        return body

    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 6}

        # --- Scrollable page container -----------------------------------
        # The whole dashboard's content (every panel below) lives inside
        # this Canvas+Scrollbar instead of packing straight into self.root.
        # Before this, a tall expanded panel (or several expanded at once)
        # could push content below the window's visible area with no way
        # to reach it -- confirmed for real: Date Cluster Audit's body alone
        # requires ~894px, essentially the whole default 900px window
        # height, on top of whatever else is expanded above it. Scrolling
        # by mouse wheel works everywhere except over widgets that already
        # scroll themselves (Text/Listbox/Treeview/Combobox -- see
        # _MOUSEWHEEL_PASSTHROUGH), which keep their own wheel behavior.
        scroll_container = ttk.Frame(self.root)
        scroll_container.pack(fill="both", expand=True)
        self._main_canvas = tk.Canvas(scroll_container, highlightthickness=0)
        main_scroll = ttk.Scrollbar(scroll_container, orient="vertical", command=self._main_canvas.yview)
        self._main_canvas.configure(yscrollcommand=main_scroll.set)
        self._main_canvas.pack(side="left", fill="both", expand=True)
        main_scroll.pack(side="right", fill="y")

        self.scroll_frame = ttk.Frame(self._main_canvas)
        self._scroll_window_id = self._main_canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")

        def _on_scroll_frame_configure(_event=None) -> None:
            self._main_canvas.configure(scrollregion=self._main_canvas.bbox("all"))

        def _on_canvas_configure(event) -> None:
            # Keep the inner frame exactly as wide as the canvas viewport so
            # content wraps/fills correctly and nothing scrolls sideways.
            self._main_canvas.itemconfigure(self._scroll_window_id, width=event.width)

        self.scroll_frame.bind("<Configure>", _on_scroll_frame_configure)
        self._main_canvas.bind("<Configure>", _on_canvas_configure)

        # --- Source folders panel ---
        src_frame = self._make_collapsible("Source folders")

        list_row = ttk.Frame(src_frame)
        list_row.pack(fill="x", padx=6, pady=(6, 0))
        self.folder_listbox = tk.Listbox(list_row, height=6, selectmode="extended")
        self.folder_listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_row, orient="vertical", command=self.folder_listbox.yview)
        scroll.pack(side="right", fill="y")
        self.folder_listbox.configure(yscrollcommand=scroll.set)

        btn_row = ttk.Frame(src_frame)
        btn_row.pack(fill="x", padx=6, pady=6)
        ttk.Button(btn_row, text="Add folder(s)...", command=self._on_add_folders).pack(side="left")
        ttk.Button(btn_row, text="Remove selected", command=self._on_remove_folders).pack(side="left", padx=(6, 0))
        self.dest_label = ttk.Label(btn_row, text="dest_root: (loading config...)")
        self.dest_label.pack(side="right")

        # --- Actions panel ---
        act_frame = self._make_collapsible("Run (Phase 1: photo/video organize)")

        act_btn_row = ttk.Frame(act_frame)
        act_btn_row.pack(fill="x", padx=6, pady=6)
        self.dry_run_btn = ttk.Button(act_btn_row, text="Dry Run (preview)", command=self._on_dry_run)
        self.dry_run_btn.pack(side="left")
        self.run_btn = ttk.Button(act_btn_row, text="Run for real...", command=self._on_run_real)
        self.run_btn.pack(side="left", padx=(6, 0))
        self.cancel_btn = ttk.Button(act_btn_row, text="Cancel", command=self._on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))

        prog_row = ttk.Frame(act_frame)
        prog_row.pack(fill="x", padx=6, pady=(0, 6))
        self.progress = ttk.Progressbar(prog_row, mode="determinate")
        self.progress.pack(fill="x", side="left", expand=True)
        self.status_label = ttk.Label(prog_row, text="Idle", width=28, anchor="e")
        self.status_label.pack(side="right", padx=(6, 0))

        self.results_text = tk.Text(act_frame, height=6, state="disabled", wrap="word")
        self.results_text.pack(fill="x", padx=6, pady=(0, 6))

        # --- Phase 2: Captioning panel ---
        cap_frame = self._make_collapsible("Phase 2 — Captioning (local Ollama vision model)")

        cap_info_row = ttk.Frame(cap_frame)
        cap_info_row.pack(fill="x", padx=6, pady=(6, 0))
        self.caption_model_label = ttk.Label(cap_info_row, text="model: (loading config...)")
        self.caption_model_label.pack(side="left")

        cap_btn_row = ttk.Frame(cap_frame)
        cap_btn_row.pack(fill="x", padx=6, pady=6)
        self.caption_btn = ttk.Button(cap_btn_row, text="Start Captioning", command=self._on_caption_start)
        self.caption_btn.pack(side="left")
        self.caption_cancel_btn = ttk.Button(cap_btn_row, text="Cancel", command=self._on_caption_cancel, state="disabled")
        self.caption_cancel_btn.pack(side="left", padx=(6, 0))

        cap_prog_row = ttk.Frame(cap_frame)
        cap_prog_row.pack(fill="x", padx=6, pady=(0, 6))
        self.caption_progress = ttk.Progressbar(cap_prog_row, mode="determinate")
        self.caption_progress.pack(fill="x", side="left", expand=True)
        self.caption_status_label = ttk.Label(cap_prog_row, text="Idle", width=28, anchor="e")
        self.caption_status_label.pack(side="right", padx=(6, 0))

        self.caption_results_text = tk.Text(cap_frame, height=5, state="disabled", wrap="word")
        self.caption_results_text.pack(fill="x", padx=6, pady=(0, 6))

        # Dedicated live log tail for Phase 2, separate from the shared Log
        # viewer panel below (which stays focused on Phase 1's much bigger
        # logs / manual browsing of any log). Always tails whichever log
        # the current/most recent captioning run wrote to, independent of
        # whatever the shared viewer's dropdown is pointed at.
        ttk.Label(cap_frame, text="Live captioning log:").pack(anchor="w", padx=6)
        cap_log_body = ttk.Frame(cap_frame)
        cap_log_body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.caption_log_text = tk.Text(cap_log_body, height=10, state="disabled", wrap="none")
        self.caption_log_text.pack(side="left", fill="both", expand=True)
        cap_log_scroll = ttk.Scrollbar(cap_log_body, orient="vertical", command=self.caption_log_text.yview)
        cap_log_scroll.pack(side="right", fill="y")
        self.caption_log_text.configure(yscrollcommand=cap_log_scroll.set)

        # --- Phase 2b: GPS Extraction panel ---
        gps_frame = self._make_collapsible(
            "Phase 2b — GPS Extraction (EXIF location backfill)", start_expanded=False
        )

        gps_info_row = ttk.Frame(gps_frame)
        gps_info_row.pack(fill="x", padx=6, pady=(6, 0))
        self.gps_info_label = ttk.Label(gps_info_row, text="(loading config...)", wraplength=740, justify="left")
        self.gps_info_label.pack(side="left", fill="x", expand=True)

        gps_btn_row = ttk.Frame(gps_frame)
        gps_btn_row.pack(fill="x", padx=6, pady=6)
        self.gps_btn = ttk.Button(gps_btn_row, text="Start GPS Extraction", command=self._on_gps_start)
        self.gps_btn.pack(side="left")
        self.gps_cancel_btn = ttk.Button(gps_btn_row, text="Cancel", command=self._on_gps_cancel, state="disabled")
        self.gps_cancel_btn.pack(side="left", padx=(6, 0))

        gps_prog_row = ttk.Frame(gps_frame)
        gps_prog_row.pack(fill="x", padx=6, pady=(0, 6))
        self.gps_progress = ttk.Progressbar(gps_prog_row, mode="determinate")
        self.gps_progress.pack(fill="x", side="left", expand=True)
        self.gps_status_label = ttk.Label(gps_prog_row, text="Idle", width=28, anchor="e")
        self.gps_status_label.pack(side="right", padx=(6, 0))

        self.gps_results_text = tk.Text(gps_frame, height=5, state="disabled", wrap="word")
        self.gps_results_text.pack(fill="x", padx=6, pady=(0, 6))

        # Dedicated live log tail, same reasoning as Phase 2's above — its
        # own view onto whatever logs/organize_*.log the current/most
        # recent GPS extraction run wrote to, independent of the shared Log
        # viewer panel's dropdown.
        ttk.Label(gps_frame, text="Live GPS extraction log:").pack(anchor="w", padx=6)
        gps_log_body = ttk.Frame(gps_frame)
        gps_log_body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.gps_log_text = tk.Text(gps_log_body, height=8, state="disabled", wrap="none")
        self.gps_log_text.pack(side="left", fill="both", expand=True)
        gps_log_scroll = ttk.Scrollbar(gps_log_body, orient="vertical", command=self.gps_log_text.yview)
        gps_log_scroll.pack(side="right", fill="y")
        self.gps_log_text.configure(yscrollcommand=gps_log_scroll.set)

        # --- Phase 2g: Video Thumbnails panel ---
        thumbs_frame = self._make_collapsible(
            "Phase 2g — Video Thumbnails (grid preview frames)", start_expanded=False
        )

        thumbs_info_row = ttk.Frame(thumbs_frame)
        thumbs_info_row.pack(fill="x", padx=6, pady=(6, 0))
        self.thumbs_info_label = ttk.Label(thumbs_info_row, text="(loading config...)", wraplength=740, justify="left")
        self.thumbs_info_label.pack(side="left", fill="x", expand=True)

        thumbs_btn_row = ttk.Frame(thumbs_frame)
        thumbs_btn_row.pack(fill="x", padx=6, pady=6)
        self.thumbs_btn = ttk.Button(thumbs_btn_row, text="Start Thumbnail Extraction", command=self._on_thumbs_start)
        self.thumbs_btn.pack(side="left")
        self.thumbs_cancel_btn = ttk.Button(thumbs_btn_row, text="Cancel", command=self._on_thumbs_cancel, state="disabled")
        self.thumbs_cancel_btn.pack(side="left", padx=(6, 0))

        thumbs_prog_row = ttk.Frame(thumbs_frame)
        thumbs_prog_row.pack(fill="x", padx=6, pady=(0, 6))
        self.thumbs_progress = ttk.Progressbar(thumbs_prog_row, mode="determinate")
        self.thumbs_progress.pack(fill="x", side="left", expand=True)
        self.thumbs_status_label = ttk.Label(thumbs_prog_row, text="Idle", width=28, anchor="e")
        self.thumbs_status_label.pack(side="right", padx=(6, 0))

        self.thumbs_results_text = tk.Text(thumbs_frame, height=5, state="disabled", wrap="word")
        self.thumbs_results_text.pack(fill="x", padx=6, pady=(0, 6))

        # Dedicated live log tail, same reasoning as the GPS/Captioning
        # panels above — its own view onto whatever logs/organize_*.log the
        # current/most recent thumbnail extraction run wrote to.
        ttk.Label(thumbs_frame, text="Live thumbnail extraction log:").pack(anchor="w", padx=6)
        thumbs_log_body = ttk.Frame(thumbs_frame)
        thumbs_log_body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.thumbs_log_text = tk.Text(thumbs_log_body, height=8, state="disabled", wrap="none")
        self.thumbs_log_text.pack(side="left", fill="both", expand=True)
        thumbs_log_scroll = ttk.Scrollbar(thumbs_log_body, orient="vertical", command=self.thumbs_log_text.yview)
        thumbs_log_scroll.pack(side="right", fill="y")
        self.thumbs_log_text.configure(yscrollcommand=thumbs_log_scroll.set)

        # --- Date Cluster Audit panel ---
        da_frame = self._make_collapsible(
            "Date Cluster Audit (detect + fix batch-date artifacts)", start_expanded=False
        )

        ttk.Label(
            da_frame, wraplength=740, justify="left",
            text=(
                "Finds whole batches of photos/videos that landed on the same wrong date "
                "(e.g. a folder import that stamped hundreds of files with today's transfer "
                "date instead of their real capture date). Step 1: Scan lists candidate days "
                "for you to judge — a high count alone doesn't mean it's wrong (a real busy "
                "day counts too). Step 2: Inspect one date for the real evidence. Step 3: "
                "Preview shows exactly what would change before anything does; Apply only "
                "becomes available after you've looked at a preview."
            ),
        ).pack(anchor="w", padx=6, pady=(6, 6))

        # --- Known-fix one-click shortcuts -------------------------------
        # The 822-file folder-rule and 1,705-file sequence-interpolation
        # corrections were already fully investigated and verified against
        # a copy of the real production DB in a prior session (see TODO.md)
        # -- they aren't a mystery needing rediscovery through Scan/Inspect/
        # Preview every time. Each button still runs a real preview against
        # the live DB right now and requires a confirm click before writing
        # anything (never skips the dry-run-before-apply guarantee) -- it
        # just needs no scan, no inspect, and no fields filled in first.
        kf_frame = ttk.Frame(da_frame, relief="ridge", borderwidth=1)
        kf_frame.pack(fill="x", padx=6, pady=(0, 10))
        ttk.Label(
            kf_frame, text="Already investigated — one-click", font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", padx=6, pady=(6, 0))
        ttk.Label(
            kf_frame, wraplength=740, justify="left",
            text=(
                "These two corrections are already fully computed and verified (see TODO.md) — "
                "no need to Scan/Inspect/fill in fields first. Each still previews the current, "
                "live list of changes and asks you to confirm before writing anything."
            ),
        ).pack(anchor="w", padx=6, pady=(2, 6))
        kf_btn_row = ttk.Frame(kf_frame)
        kf_btn_row.pack(fill="x", padx=6, pady=(0, 8))
        self.date_audit_known_folder_btn = ttk.Button(
            kf_btn_row, text="Apply Folder-Rule Fix (822 files)",
            command=self._on_date_audit_apply_known_folder_fix,
        )
        self.date_audit_known_folder_btn.pack(side="left")
        self.date_audit_known_sequence_btn = ttk.Button(
            kf_btn_row, text="Apply Sequence Fix — Feb 2020 Vacation (1,705 files)",
            command=self._on_date_audit_apply_known_sequence_fix,
        )
        self.date_audit_known_sequence_btn.pack(side="left", padx=(8, 0))

        ttk.Separator(da_frame, orient="horizontal").pack(fill="x", padx=6, pady=(0, 8))
        ttk.Label(
            da_frame, text="General workflow — for finding future new suspicious dates:",
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", padx=6, pady=(0, 6))

        da_scan_row = ttk.Frame(da_frame)
        da_scan_row.pack(fill="x", padx=6, pady=(0, 6))
        self.date_audit_scan_btn = ttk.Button(da_scan_row, text="Scan for Suspicious Dates", command=self._on_date_audit_scan)
        self.date_audit_scan_btn.pack(side="left")
        ttk.Label(da_scan_row, text="  Min. count:").pack(side="left", padx=(12, 0))
        self.date_audit_min_count_var = tk.StringVar(value=str(DEFAULT_MIN_COUNT))
        ttk.Entry(da_scan_row, textvariable=self.date_audit_min_count_var, width=6).pack(side="left")
        ttk.Label(da_scan_row, text="Top N:").pack(side="left", padx=(12, 0))
        self.date_audit_top_n_var = tk.StringVar(value=str(DEFAULT_TOP_N))
        ttk.Entry(da_scan_row, textvariable=self.date_audit_top_n_var, width=6).pack(side="left")

        da_tree_body = ttk.Frame(da_frame)
        da_tree_body.pack(fill="both", expand=False, padx=6, pady=(0, 6))
        self.date_audit_tree = ttk.Treeview(
            da_tree_body, columns=("count", "fs_pct", "sources"), show="headings", height=8, selectmode="browse"
        )
        self.date_audit_tree.heading("count", text="Count")
        self.date_audit_tree.heading("fs_pct", text="Filesystem %")
        self.date_audit_tree.heading("sources", text="By date_source")
        self.date_audit_tree.column("count", width=70, anchor="e")
        self.date_audit_tree.column("fs_pct", width=100, anchor="e")
        self.date_audit_tree.column("sources", width=420, anchor="w")
        # Treeview's own item id doubles as the displayed date (col #0 is the
        # tree's implicit "text" column, shown via show="headings" as the
        # leftmost header-less cell) — simplest way to get the date back out
        # on selection without a parallel id->date dict.
        self.date_audit_tree.heading("#0", text="Date")
        self.date_audit_tree.column("#0", width=100, anchor="w")
        self.date_audit_tree.pack(side="left", fill="both", expand=True)
        da_tree_scroll = ttk.Scrollbar(da_tree_body, orient="vertical", command=self.date_audit_tree.yview)
        da_tree_scroll.pack(side="right", fill="y")
        self.date_audit_tree.configure(yscrollcommand=da_tree_scroll.set)
        self.date_audit_tree.bind("<<TreeviewSelect>>", self._on_date_audit_tree_select)

        da_inspect_row = ttk.Frame(da_frame)
        da_inspect_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(da_inspect_row, text="Date to inspect:").pack(side="left")
        self.date_audit_inspect_var = tk.StringVar(value="")
        ttk.Entry(da_inspect_row, textvariable=self.date_audit_inspect_var, width=12).pack(side="left", padx=(6, 6))
        ttk.Button(da_inspect_row, text="Inspect Date", command=self._on_date_audit_inspect).pack(side="left")

        da_fix_row = ttk.Frame(da_frame)
        da_fix_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(da_fix_row, text="Preview Fix (Dry Run)", command=self._on_date_audit_preview_fix).pack(side="left")
        self.date_audit_apply_btn = ttk.Button(
            da_fix_row, text="Apply Fix", command=self._on_date_audit_apply_fix, state="disabled"
        )
        self.date_audit_apply_btn.pack(side="left", padx=(6, 0))
        self.date_audit_cancel_btn = ttk.Button(
            da_fix_row, text="Cancel", command=self._on_date_audit_cancel, state="disabled"
        )
        self.date_audit_cancel_btn.pack(side="left", padx=(6, 0))

        ttk.Separator(da_frame, orient="horizontal").pack(fill="x", padx=6, pady=(4, 6))
        ttk.Label(
            da_frame, wraplength=740, justify="left",
            text=(
                "Sequence Interpolation — for when you recognize a misdated batch as a real event "
                "yourself, even though there's no folder-name signal to infer it automatically. Point "
                "this at an already-correctly-dated reference folder for the same device/event; it "
                "matches camera-roll numbers (IMG_####) between the two to recover day/time precision, "
                "not just a month guess."
            ),
        ).pack(anchor="w", padx=6, pady=(0, 6))

        da_interp_row1 = ttk.Frame(da_frame)
        da_interp_row1.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(da_interp_row1, text="Misdated batch (original_path prefix):", width=32, anchor="w").pack(side="left")
        self.date_audit_interp_target_var = tk.StringVar(value="")
        ttk.Entry(da_interp_row1, textvariable=self.date_audit_interp_target_var, width=40).pack(
            side="left", fill="x", expand=True
        )

        da_interp_row2 = ttk.Frame(da_frame)
        da_interp_row2.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(da_interp_row2, text="Reference folder (current_path prefix):", width=32, anchor="w").pack(side="left")
        self.date_audit_interp_reference_var = tk.StringVar(value="")
        ttk.Entry(da_interp_row2, textvariable=self.date_audit_interp_reference_var, width=40).pack(
            side="left", fill="x", expand=True
        )

        da_interp_row3 = ttk.Frame(da_frame)
        da_interp_row3.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(da_interp_row3, text="IMG # min:").pack(side="left")
        self.date_audit_interp_min_var = tk.StringVar(value="")
        ttk.Entry(da_interp_row3, textvariable=self.date_audit_interp_min_var, width=10).pack(side="left", padx=(4, 12))
        ttk.Label(da_interp_row3, text="IMG # max:").pack(side="left")
        self.date_audit_interp_max_var = tk.StringVar(value="")
        ttk.Entry(da_interp_row3, textvariable=self.date_audit_interp_max_var, width=10).pack(side="left", padx=(4, 0))
        ttk.Label(da_interp_row3, text="  (both optional — leave blank for no bound)").pack(side="left", padx=(6, 0))

        da_interp_btn_row = ttk.Frame(da_frame)
        da_interp_btn_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(
            da_interp_btn_row, text="Preview Sequence Fix (Dry Run)", command=self._on_date_audit_interp_preview
        ).pack(side="left")
        self.date_audit_interp_apply_btn = ttk.Button(
            da_interp_btn_row, text="Apply Sequence Fix", command=self._on_date_audit_interp_apply, state="disabled"
        )
        self.date_audit_interp_apply_btn.pack(side="left", padx=(6, 0))

        ttk.Separator(da_frame, orient="horizontal").pack(fill="x", padx=6, pady=(0, 6))

        da_prog_row = ttk.Frame(da_frame)
        da_prog_row.pack(fill="x", padx=6, pady=(0, 6))
        self.date_audit_progress = ttk.Progressbar(da_prog_row, mode="determinate")
        self.date_audit_progress.pack(fill="x", side="left", expand=True)
        self.date_audit_status_label = ttk.Label(da_prog_row, text="Idle", width=28, anchor="e")
        self.date_audit_status_label.pack(side="right", padx=(6, 0))

        ttk.Label(da_frame, text="Scan / Inspect / Preview output:").pack(anchor="w", padx=6)
        da_results_body = ttk.Frame(da_frame)
        da_results_body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.date_audit_results_text = tk.Text(da_results_body, height=12, state="disabled", wrap="word")
        self.date_audit_results_text.pack(side="left", fill="both", expand=True)
        da_results_scroll = ttk.Scrollbar(da_results_body, orient="vertical", command=self.date_audit_results_text.yview)
        da_results_scroll.pack(side="right", fill="y")
        self.date_audit_results_text.configure(yscrollcommand=da_results_scroll.set)

        ttk.Label(da_frame, text="Live Apply Fix log:").pack(anchor="w", padx=6)
        da_log_body = ttk.Frame(da_frame)
        da_log_body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.date_audit_log_text = tk.Text(da_log_body, height=6, state="disabled", wrap="none")
        self.date_audit_log_text.pack(side="left", fill="both", expand=True)
        da_log_scroll = ttk.Scrollbar(da_log_body, orient="vertical", command=self.date_audit_log_text.yview)
        da_log_scroll.pack(side="right", fill="y")
        self.date_audit_log_text.configure(yscrollcommand=da_log_scroll.set)

        # --- Phase 3 Session 1: Face Detection panel ---
        faces_frame = self._make_collapsible(
            "Phase 3 — Face Detection (Session 1: detection + benchmarking only)", start_expanded=False
        )

        faces_info_row = ttk.Frame(faces_frame)
        faces_info_row.pack(fill="x", padx=6, pady=(6, 0))
        self.faces_info_label = ttk.Label(faces_info_row, text="(loading config...)", wraplength=740, justify="left")
        self.faces_info_label.pack(side="left", fill="x", expand=True)

        faces_btn_row = ttk.Frame(faces_frame)
        faces_btn_row.pack(fill="x", padx=6, pady=6)
        self.faces_btn = ttk.Button(faces_btn_row, text="Start Face Detection", command=self._on_faces_start)
        self.faces_btn.pack(side="left")
        self.faces_cancel_btn = ttk.Button(faces_btn_row, text="Cancel", command=self._on_faces_cancel, state="disabled")
        self.faces_cancel_btn.pack(side="left", padx=(6, 0))

        faces_prog_row = ttk.Frame(faces_frame)
        faces_prog_row.pack(fill="x", padx=6, pady=(0, 6))
        self.faces_progress = ttk.Progressbar(faces_prog_row, mode="determinate")
        self.faces_progress.pack(fill="x", side="left", expand=True)
        self.faces_status_label = ttk.Label(faces_prog_row, text="Idle", width=28, anchor="e")
        self.faces_status_label.pack(side="right", padx=(6, 0))

        self.faces_results_text = tk.Text(faces_frame, height=6, state="disabled", wrap="word")
        self.faces_results_text.pack(fill="x", padx=6, pady=(0, 6))

        # Dedicated live log tail, same reasoning as every other batch
        # panel above — its own view onto whatever logs/organize_*.log the
        # current/most recent face detection run wrote to.
        ttk.Label(faces_frame, text="Live face detection log:").pack(anchor="w", padx=6)
        faces_log_body = ttk.Frame(faces_frame)
        faces_log_body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.faces_log_text = tk.Text(faces_log_body, height=8, state="disabled", wrap="none")
        self.faces_log_text.pack(side="left", fill="both", expand=True)
        faces_log_scroll = ttk.Scrollbar(faces_log_body, orient="vertical", command=self.faces_log_text.yview)
        faces_log_scroll.pack(side="right", fill="y")
        self.faces_log_text.configure(yscrollcommand=faces_log_scroll.set)

        # --- Phase 3 Session 2: Face Clustering panel ---
        cluster_frame = self._make_collapsible(
            "Phase 3 Session 2 — Face Clustering + Labeling", start_expanded=False
        )

        ttk.Label(
            cluster_frame, wraplength=740, justify="left",
            text=(
                "Loads data/faces.jsonl (Session 1's detected faces) into the database and groups them "
                "by person (cosine similarity over embeddings, via faiss + DBSCAN). Re-running later after "
                "more photos are face-detected is safe: it only ever processes faces not yet grouped, and "
                "never touches an already-grouped face — so a name, merge, or split you've already done in "
                "Face Labeling below can never be silently undone."
            ),
        ).pack(anchor="w", padx=6, pady=(6, 6))

        cluster_btn_row = ttk.Frame(cluster_frame)
        cluster_btn_row.pack(fill="x", padx=6, pady=(0, 6))
        self.cluster_btn = ttk.Button(cluster_btn_row, text="Start Face Clustering", command=self._on_cluster_start)
        self.cluster_btn.pack(side="left")
        self.cluster_label_btn = ttk.Button(
            cluster_btn_row, text="Open Face Labeling...", command=self._on_open_face_labeling
        )
        self.cluster_label_btn.pack(side="left", padx=(6, 0))

        cluster_prog_row = ttk.Frame(cluster_frame)
        cluster_prog_row.pack(fill="x", padx=6, pady=(0, 6))
        self.cluster_progress = ttk.Progressbar(cluster_prog_row, mode="determinate")
        self.cluster_progress.pack(fill="x", side="left", expand=True)
        self.cluster_status_label = ttk.Label(cluster_prog_row, text="Idle", width=28, anchor="e")
        self.cluster_status_label.pack(side="right", padx=(6, 0))

        self.cluster_results_text = tk.Text(cluster_frame, height=6, state="disabled", wrap="word")
        self.cluster_results_text.pack(fill="x", padx=6, pady=(0, 6))

        ttk.Label(cluster_frame, text="Live face clustering log:").pack(anchor="w", padx=6)
        cluster_log_body = ttk.Frame(cluster_frame)
        cluster_log_body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.cluster_log_text = tk.Text(cluster_log_body, height=6, state="disabled", wrap="none")
        self.cluster_log_text.pack(side="left", fill="both", expand=True)
        cluster_log_scroll = ttk.Scrollbar(cluster_log_body, orient="vertical", command=self.cluster_log_text.yview)
        cluster_log_scroll.pack(side="right", fill="y")
        self.cluster_log_text.configure(yscrollcommand=cluster_log_scroll.set)

        # --- Phase 3b: Relocate Files panel ---
        relocate_frame = self._make_collapsible(
            "Phase 3b — Relocate Files (resync folder placement with date_taken)", start_expanded=False
        )

        ttk.Label(
            relocate_frame, wraplength=740, justify="left",
            text=(
                "Finds any photo/video whose current folder disagrees with what Phase 1's own "
                "convention would compute from its CURRENT date_taken (e.g. after a Date Cluster Audit "
                "fix corrected the date but never moved the file — that tool is DB-only by design). "
                "General-purpose, not scoped to any one prior fix; checks photos and video alike. "
                "Relocating reuses Phase 1's own copy-verify-delete + collision handling exactly — "
                "never a raw move, never an overwrite. Preview shows every old path -> new path before "
                "anything moves; Apply only becomes available after you've looked at a preview."
            ),
        ).pack(anchor="w", padx=6, pady=(6, 6))

        relocate_btn_row = ttk.Frame(relocate_frame)
        relocate_btn_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(relocate_btn_row, text="Preview (Dry Run)", command=self._on_relocate_preview).pack(side="left")
        self.relocate_apply_btn = ttk.Button(
            relocate_btn_row, text="Apply", command=self._on_relocate_apply, state="disabled"
        )
        self.relocate_apply_btn.pack(side="left", padx=(6, 0))
        self.relocate_cancel_btn = ttk.Button(
            relocate_btn_row, text="Cancel", command=self._on_relocate_cancel, state="disabled"
        )
        self.relocate_cancel_btn.pack(side="left", padx=(6, 0))

        relocate_prog_row = ttk.Frame(relocate_frame)
        relocate_prog_row.pack(fill="x", padx=6, pady=(0, 6))
        self.relocate_progress = ttk.Progressbar(relocate_prog_row, mode="determinate")
        self.relocate_progress.pack(fill="x", side="left", expand=True)
        self.relocate_status_label = ttk.Label(relocate_prog_row, text="Idle", width=28, anchor="e")
        self.relocate_status_label.pack(side="right", padx=(6, 0))

        ttk.Label(relocate_frame, text="Preview / result output:").pack(anchor="w", padx=6)
        relocate_results_body = ttk.Frame(relocate_frame)
        relocate_results_body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.relocate_results_text = tk.Text(relocate_results_body, height=10, state="disabled", wrap="word")
        self.relocate_results_text.pack(side="left", fill="both", expand=True)
        relocate_results_scroll = ttk.Scrollbar(
            relocate_results_body, orient="vertical", command=self.relocate_results_text.yview
        )
        relocate_results_scroll.pack(side="right", fill="y")
        self.relocate_results_text.configure(yscrollcommand=relocate_results_scroll.set)

        ttk.Label(relocate_frame, text="Live relocation log:").pack(anchor="w", padx=6)
        relocate_log_body = ttk.Frame(relocate_frame)
        relocate_log_body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.relocate_log_text = tk.Text(relocate_log_body, height=6, state="disabled", wrap="none")
        self.relocate_log_text.pack(side="left", fill="both", expand=True)
        relocate_log_scroll = ttk.Scrollbar(relocate_log_body, orient="vertical", command=self.relocate_log_text.yview)
        relocate_log_scroll.pack(side="right", fill="y")
        self.relocate_log_text.configure(yscrollcommand=relocate_log_scroll.set)

        # --- Review Users panel (Phase 2d follow-up) ---
        ru_frame = self._make_collapsible("Review Users (review_tool.py logins)", start_expanded=False)

        ru_list_row = ttk.Frame(ru_frame)
        ru_list_row.pack(fill="x", padx=6, pady=(6, 0))
        self.review_user_listbox = tk.Listbox(ru_list_row, height=4, selectmode="browse")
        self.review_user_listbox.pack(side="left", fill="both", expand=True)
        ru_scroll = ttk.Scrollbar(ru_list_row, orient="vertical", command=self.review_user_listbox.yview)
        ru_scroll.pack(side="right", fill="y")
        self.review_user_listbox.configure(yscrollcommand=ru_scroll.set)

        ru_btn_row = ttk.Frame(ru_frame)
        ru_btn_row.pack(fill="x", padx=6, pady=6)
        ttk.Button(ru_btn_row, text="Remove selected...", command=self._on_remove_review_user).pack(side="left")

        ttk.Separator(ru_frame, orient="horizontal").pack(fill="x", padx=6, pady=(0, 8))

        ru_add_frame = ttk.Frame(ru_frame)
        ru_add_frame.pack(fill="x", padx=6, pady=(0, 8))

        ttk.Label(ru_add_frame, text="Username:").grid(row=0, column=0, sticky="w", pady=2)
        self.ru_username_var = tk.StringVar()
        ttk.Entry(ru_add_frame, textvariable=self.ru_username_var, width=28).grid(
            row=0, column=1, sticky="w", padx=(6, 0), pady=2
        )

        ttk.Label(ru_add_frame, text="Password (min 8 chars):").grid(row=1, column=0, sticky="w", pady=2)
        self.ru_password_var = tk.StringVar()
        ttk.Entry(ru_add_frame, textvariable=self.ru_password_var, show="*", width=28).grid(
            row=1, column=1, sticky="w", padx=(6, 0), pady=2
        )

        ttk.Label(ru_add_frame, text="Confirm password:").grid(row=2, column=0, sticky="w", pady=2)
        self.ru_confirm_var = tk.StringVar()
        ttk.Entry(ru_add_frame, textvariable=self.ru_confirm_var, show="*", width=28).grid(
            row=2, column=1, sticky="w", padx=(6, 0), pady=2
        )

        ttk.Button(ru_add_frame, text="Add / update user", command=self._on_add_review_user).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )

        # --- Remote Access panel (Phase 2d follow-up) ---
        style = ttk.Style()
        style.configure("Warn.TLabel", foreground="#b30000")
        style.configure("Down.TLabel", foreground="#b35c00")
        style.configure("Ok.TLabel", foreground="#1a7f37")

        ra_frame = self._make_collapsible("Remote Access (Cloudflare Tunnel)", start_expanded=False)

        ra_status_row = ttk.Frame(ra_frame)
        ra_status_row.pack(fill="x", padx=6, pady=(6, 0))
        self.review_tool_status_label = ttk.Label(
            ra_status_row, text="review_tool.py: (checking port status...)",
            wraplength=740, justify="left",
        )
        self.review_tool_status_label.pack(side="left", fill="x", expand=True)
        self.force_restart_btn = ttk.Button(ra_status_row, text="Force Restart", command=self._on_force_restart)
        self.force_restart_btn.pack(side="right", anchor="n", padx=(0, 6))
        ttk.Button(ra_status_row, text="Refresh", command=self._refresh_port_status).pack(side="right", anchor="n")

        ttk.Separator(ra_frame, orient="horizontal").pack(fill="x", padx=6, pady=6)

        ra_tunnel_row = ttk.Frame(ra_frame)
        ra_tunnel_row.pack(fill="x", padx=6, pady=(0, 6))
        self.tunnel_status_label = ttk.Label(ra_tunnel_row, text="Tunnel: Stopped")
        self.tunnel_status_label.pack(side="left")
        self.tunnel_start_btn = ttk.Button(ra_tunnel_row, text="Start Tunnel", command=self._on_tunnel_start)
        self.tunnel_start_btn.pack(side="right", padx=(6, 0))
        self.tunnel_stop_btn = ttk.Button(ra_tunnel_row, text="Stop Tunnel", command=self._on_tunnel_stop, state="disabled")
        self.tunnel_stop_btn.pack(side="right")

        ttk.Label(ra_frame, text="Tunnel console output:").pack(anchor="w", padx=6)
        ra_log_body = ttk.Frame(ra_frame)
        ra_log_body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tunnel_log_text = tk.Text(ra_log_body, height=8, state="disabled", wrap="none")
        self.tunnel_log_text.pack(side="left", fill="both", expand=True)
        ra_log_scroll = ttk.Scrollbar(ra_log_body, orient="vertical", command=self.tunnel_log_text.yview)
        ra_log_scroll.pack(side="right", fill="y")
        self.tunnel_log_text.configure(yscrollcommand=ra_log_scroll.set)

        # --- Log viewer panel ---
        log_frame = self._make_collapsible("Log viewer", fill="both", expand=True)

        log_top = ttk.Frame(log_frame)
        log_top.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(log_top, text="Log file:").pack(side="left")
        self.log_choice = ttk.Combobox(log_top, state="readonly", width=50)
        self.log_choice.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self.log_choice.bind("<<ComboboxSelected>>", self._on_log_choice_changed)
        ttk.Button(log_top, text="Refresh list", command=self._refresh_log_list).pack(side="left", padx=(6, 0))
        self.autotail_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(log_top, text="Auto-tail active run", variable=self.autotail_var).pack(side="left", padx=(6, 0))

        log_body = ttk.Frame(log_frame)
        log_body.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_text = tk.Text(log_body, state="disabled", wrap="none")
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_body, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self._bind_mousewheel_recursive(self.scroll_frame)

    def _bind_mousewheel_recursive(self, widget: tk.Widget) -> None:
        """Lets the mouse wheel scroll the main dashboard canvas from
        anywhere over a panel's plain background/labels/buttons. Skips
        widgets in _MOUSEWHEEL_PASSTHROUGH (Text/Listbox/Treeview/Combobox/
        Scrollbar) so hovering over, e.g., the Log viewer's text box or the
        Date Cluster Audit tree scrolls that widget's own content instead --
        binding both would fight each other."""
        if not isinstance(widget, self._MOUSEWHEEL_PASSTHROUGH):
            widget.bind("<MouseWheel>", self._on_main_mousewheel, add="+")
        for child in widget.winfo_children():
            self._bind_mousewheel_recursive(child)

    def _on_main_mousewheel(self, event) -> None:
        if event.delta:
            self._main_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _scroll_widget_into_view(self, widget: tk.Widget) -> None:
        """Scrolls the main dashboard canvas so `widget` is visible. Used
        after an action that writes its result into a text box that can sit
        far down a long panel (e.g. Date Cluster Audit's Preview Fix output)
        -- this is what made "clicking Preview Fix appeared to do nothing"
        look like a bug: the query ran and populated the result correctly
        every time, it was just rendered below the visible window with no
        scrollbar to reach it before this fix. Best-effort: never raises."""
        try:
            self.root.update_idletasks()
            bbox = self._main_canvas.bbox("all")
            if not bbox:
                return
            total_height = bbox[3] - bbox[1]
            if total_height <= 0:
                return
            y = widget.winfo_rooty() - self.scroll_frame.winfo_rooty()
            fraction = max(0.0, min(1.0, y / total_height))
            self._main_canvas.yview_moveto(fraction)
        except tk.TclError:
            pass

    # ------------------------------------------------------------- config
    def _load_config_or_onboard(self) -> None:
        if not CONFIG_PATH.exists():
            if messagebox.askyesno(
                "Photo Organizer",
                f"{CONFIG_PATH.name} doesn't exist yet.\n\nCreate it now from {EXAMPLE_CONFIG_PATH.name}?",
            ):
                shutil.copy(EXAMPLE_CONFIG_PATH, CONFIG_PATH)
            else:
                messagebox.showinfo(
                    "Photo Organizer",
                    f"Copy {EXAMPLE_CONFIG_PATH.name} to {CONFIG_PATH.name} and relaunch the dashboard.",
                )
                self.root.destroy()
                return
        self._reload_cfg()
        self._refresh_log_list()

    def _reload_cfg(self) -> None:
        self.cfg = load_config()
        self.dest_label.configure(text=f"dest_root: {self.cfg.dest_root}  (always scanned too)")
        self.caption_model_label.configure(
            text=f"model: {self.cfg.ollama_model}  (via {self.cfg.ollama_host})  —  scans {self.cfg.dest_root} for JPG/PNG/HEIC"
        )
        self.gps_info_label.configure(
            text="Reads EXIF GPS + offline reverse-geocodes into a place name (e.g. \"Woodstock, GA\"), "
            "written directly onto the photos table. Only scans DB rows not yet checked "
            "(gps_checked=0) — resumable, ~100 files/sec."
        )
        self.thumbs_info_label.configure(
            text=f"Decodes the first frame of each video and caches it as a JPEG to "
            f"{self.cfg.thumbnail_dir}\\<file_hash>.jpg, used by review_tool.py's grid "
            "instead of the generic play-icon placeholder. Never touches the database — "
            "a cache file already on disk means \"done\", so it's naturally resumable. "
            "~0.1s/video (benchmarked, not assumed) — minutes for the whole library, not days."
        )
        self.faces_info_label.configure(
            text=f"Detects faces + extracts embeddings (insightface, {self.cfg.face_model_pack} pack) for every "
            f"already-organized photo not yet scanned, writing results to {self.cfg.faces_path}. Photos only, "
            "no video. Session 1 scope: detection + benchmarking only — no clustering, no people/names yet. "
            f"gpu_enabled={self.cfg.gpu_enabled} — shares real GPU load with a concurrent Phase 2 captioning run, "
            "unlike GPS/Thumbnail extraction above."
        )
        self._refresh_folder_list()
        self._refresh_review_users()

    def _refresh_folder_list(self) -> None:
        self.folder_listbox.delete(0, "end")
        for f in self.cfg.source_folders:
            self.folder_listbox.insert("end", f)

    # ----------------------------------------------------- source folders
    def _on_add_folders(self) -> None:
        try:
            picked = pick_sources_interactive()
        except Exception as e:
            messagebox.showerror("Photo Organizer", f"Folder picker failed: {e}")
            return
        if not picked:
            return
        merged, added = merge_and_save(picked)
        self._reload_cfg()
        messagebox.showinfo(
            "Photo Organizer",
            f"{added} new folder(s) added ({len(picked) - added} already present, skipped).\n"
            f"{len(merged)} source folder(s) total.",
        )

    def _on_remove_folders(self) -> None:
        sel = list(self.folder_listbox.curselection())
        if not sel:
            return
        to_remove = {self.folder_listbox.get(i) for i in sel}
        remaining = [f for f in self.cfg.source_folders if f not in to_remove]
        if not messagebox.askyesno(
            "Photo Organizer",
            f"Remove {len(to_remove)} folder(s) from config.yaml's source_folders?\n\n"
            "This only forgets the folder as a scan source — it does NOT touch any files in it.",
        ):
            return
        save_source_folders(remaining, CONFIG_PATH)
        self._reload_cfg()

    # -------------------------------------------------------------- runs
    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _on_dry_run(self) -> None:
        if self._busy():
            return
        self._start_run(dry_run=True)

    def _on_run_real(self) -> None:
        if self._busy():
            return
        if not self._confirm_real_run():
            return
        self._start_run(dry_run=False)

    def _confirm_real_run(self) -> bool:
        dlg = tk.Toplevel(self.root)
        dlg.title("Confirm real run")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        msg = (
            "About to COPY-VERIFY-DELETE photos/videos from:\n"
            + "\n".join(f"  - {f}" for f in self.cfg.source_folders)
            + f"\n  - {self.cfg.dest_root}  (loose/misplaced files already there)\n\n"
            + f"into: {self.cfg.dest_root}\\YYYY\\YYYY-MM\\\n\n"
            + "Originals are only deleted after their copy is verified byte-for-byte.\n"
            + "A failed verification leaves the source untouched and logs an error.\n\n"
            + "This is not a dry run. Proceed?"
        )
        ttk.Label(dlg, text=msg, justify="left", padding=16).pack()

        result = {"go": False}

        def do_confirm():
            result["go"] = True
            dlg.destroy()

        def do_cancel():
            dlg.destroy()

        btn_row = ttk.Frame(dlg)
        btn_row.pack(pady=(0, 12))
        ttk.Button(btn_row, text="Cancel", command=do_cancel).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Run for real (copy-verify-delete)", command=do_confirm).pack(side="left", padx=6)

        dlg.protocol("WM_DELETE_WINDOW", do_cancel)
        self.root.wait_window(dlg)
        return result["go"]

    def _start_run(self, dry_run: bool) -> None:
        self.stop_requested = False
        self.progress.configure(value=0, maximum=1)
        self.status_label.configure(text="Starting...")
        self._set_results_text("")
        self.dry_run_btn.configure(state="disabled")
        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")

        self.worker = threading.Thread(target=self._run_worker, args=(dry_run,), daemon=True)
        self.worker.start()

    def _on_cancel(self) -> None:
        self.stop_requested = True
        self.status_label.configure(text="Stopping...")

    def _run_worker(self, dry_run: bool) -> None:
        # Runs on a background thread — never touch Tk widgets here, only
        # push messages through self.msg_queue for the main thread to apply.
        try:
            cfg = load_config()
            cfg = replace(cfg, dry_run=dry_run)  # CLI flag equivalent of --dry-run/--execute
            # logger_name distinguishes this worker's logger from the
            # Captioning/GPS workers' — see setup_logging()'s docstring for
            # why sharing the default name broke log routing when two
            # workers ran concurrently.
            logger, log_path = setup_logging(
                cfg.log_dir_abs, echo_to_console=False, logger_name="photo_organizer.organize"
            )
            self.msg_queue.put(("log_started", log_path))
            init_db(cfg.db_path_abs, logger=logger)  # safe/idempotent — CREATE TABLE IF NOT EXISTS + any pending migration
            logger.info(f"Starting Phase 1 run from dashboard — dry_run={dry_run}")

            conn = connect(cfg.db_path_abs)
            try:
                stats = run_phase1(
                    cfg, conn, logger,
                    progress_cb=lambda done, total: self.msg_queue.put(("progress", done, total)),
                    stop_check=lambda: self.stop_requested,
                )
            finally:
                conn.close()
            self.msg_queue.put(("done", stats, dry_run, self.stop_requested))
        except Exception as e:
            self.msg_queue.put(("error", str(e)))

    def _poll_queue(self) -> None:
        # Each message is handled inside its own try/except: one bad
        # message (e.g. a widget update that throws) must never silently
        # kill this polling loop for the rest of the session — that would
        # freeze every future progress/log update with no visible error
        # (worse under pythonw.exe, where there's no console to even show
        # a traceback — same class of silent-failure risk as the
        # tqdm/hachoir bugs in CLAUDE.md). Falls back to printing to stdout
        # (a no-op, safely swallowed, under pythonw.exe) rather than
        # letting `self.root.after(200, self._poll_queue)` below never run.
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                try:
                    kind = msg[0]
                    if kind == "log_started":
                        log_path = msg[1]
                        self._switch_to_log(log_path)
                    elif kind == "progress":
                        _, done, total = msg
                        self.progress.configure(maximum=max(total, 1), value=done)
                        self.status_label.configure(text=f"{done:,} / {total:,} processed")
                    elif kind == "done":
                        _, stats, dry_run, was_stopped = msg
                        self._on_run_done(stats, dry_run, was_stopped)
                    elif kind == "error":
                        _, err = msg
                        self._on_run_error(err)
                    elif kind == "caption_log_started":
                        log_path = msg[1]
                        self._start_caption_log_tail(log_path)
                    elif kind == "caption_progress":
                        _, done, total = msg
                        self.caption_progress.configure(maximum=max(total, 1), value=done)
                        self.caption_status_label.configure(text=f"{done:,} / {total:,} processed")
                    elif kind == "caption_done":
                        _, stats, was_stopped = msg
                        self._on_caption_done(stats, was_stopped)
                    elif kind == "caption_error":
                        _, err = msg
                        self._on_caption_error(err)
                    elif kind == "gps_log_started":
                        log_path = msg[1]
                        self._start_gps_log_tail(log_path)
                    elif kind == "gps_progress":
                        _, done, total = msg
                        self.gps_progress.configure(maximum=max(total, 1), value=done)
                        self.gps_status_label.configure(text=f"{done:,} / {total:,} processed")
                    elif kind == "gps_done":
                        _, stats, was_stopped = msg
                        self._on_gps_done(stats, was_stopped)
                    elif kind == "gps_error":
                        _, err = msg
                        self._on_gps_error(err)
                    elif kind == "thumbs_log_started":
                        log_path = msg[1]
                        self._start_thumbs_log_tail(log_path)
                    elif kind == "thumbs_progress":
                        _, done, total = msg
                        self.thumbs_progress.configure(maximum=max(total, 1), value=done)
                        self.thumbs_status_label.configure(text=f"{done:,} / {total:,} processed")
                    elif kind == "thumbs_done":
                        _, stats, was_stopped = msg
                        self._on_thumbs_done(stats, was_stopped)
                    elif kind == "thumbs_error":
                        _, err = msg
                        self._on_thumbs_error(err)
                    elif kind == "faces_log_started":
                        log_path = msg[1]
                        self._start_faces_log_tail(log_path)
                    elif kind == "faces_progress":
                        _, done, total = msg
                        self.faces_progress.configure(maximum=max(total, 1), value=done)
                        self.faces_status_label.configure(text=f"{done:,} / {total:,} processed")
                    elif kind == "faces_done":
                        _, stats, was_stopped = msg
                        self._on_faces_done(stats, was_stopped)
                    elif kind == "faces_error":
                        _, err = msg
                        self._on_faces_error(err)
                    elif kind == "cluster_log_started":
                        log_path = msg[1]
                        self._start_cluster_log_tail(log_path)
                    elif kind == "cluster_progress":
                        _, done, total = msg
                        self.cluster_progress.configure(maximum=max(total, 1), value=done)
                        self.cluster_status_label.configure(text=f"{done:,} / {total:,} processed")
                    elif kind == "cluster_done":
                        _, load_stats, cluster_stats = msg
                        self._on_cluster_done(load_stats, cluster_stats)
                    elif kind == "cluster_error":
                        _, err = msg
                        self._on_cluster_error(err)
                    elif kind == "tunnel_output":
                        _, proc, line = msg
                        self._append_tunnel_log(proc, line)
                    elif kind == "tunnel_exited":
                        _, proc, returncode = msg
                        self._on_tunnel_exited(proc, returncode)
                    elif kind == "force_restart_done":
                        _, result, err = msg
                        self._on_force_restart_done(result, err)
                    elif kind == "date_audit_log_started":
                        log_path = msg[1]
                        self._start_date_audit_log_tail(log_path)
                    elif kind == "date_audit_progress":
                        _, done, total = msg
                        self.date_audit_progress.configure(maximum=max(total, 1), value=done)
                        self.date_audit_status_label.configure(text=f"{done:,} / {total:,} applied")
                    elif kind == "date_audit_done":
                        _, stats, was_stopped = msg
                        self._on_date_audit_done(stats, was_stopped)
                    elif kind == "date_audit_error":
                        _, err = msg
                        self._on_date_audit_error(err)
                    elif kind == "relocate_log_started":
                        log_path = msg[1]
                        self._start_relocate_log_tail(log_path)
                    elif kind == "relocate_progress":
                        _, done, total = msg
                        self.relocate_progress.configure(maximum=max(total, 1), value=done)
                        self.relocate_status_label.configure(text=f"{done:,} / {total:,} relocated")
                    elif kind == "relocate_done":
                        _, stats, was_stopped = msg
                        self._on_relocate_done(stats, was_stopped)
                    elif kind == "relocate_error":
                        _, err = msg
                        self._on_relocate_error(err)
                except Exception:
                    pass  # see docstring above -- never let one bad message stop future polling
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    def _on_run_done(self, stats: RunStats, dry_run: bool, was_stopped: bool) -> None:
        self.dry_run_btn.configure(state="normal")
        self.run_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.status_label.configure(text="Stopped early" if was_stopped else "Done")
        self._set_results_text(self._format_stats(stats, dry_run, was_stopped))
        self._reload_cfg()  # source folders may now be empty of files, and dest_root state changed

    def _on_run_error(self, err: str) -> None:
        self.dry_run_btn.configure(state="normal")
        self.run_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.status_label.configure(text="Error")
        messagebox.showerror("Photo Organizer", f"Run failed: {err}")

    @staticmethod
    def _format_stats(stats: RunStats, dry_run: bool, was_stopped: bool) -> str:
        lines = [f"Scanned: {stats.scanned:,}" + ("  (stopped early by user)" if was_stopped else "")]
        lines.append(
            f"Duplicates skipped (hash already known): {stats.already_known:,} "
            f"(fast path, no re-hash: {stats.fast_path_hits:,})"
        )
        if dry_run:
            lines.append(f"Would stay in place (already correctly sorted): {stats.dry_run_already_in_place:,}")
            lines.append(f"Would be sorted into YYYY/YYYY-MM: {stats.dry_run_sorted:,}")
            lines.append(f"Would be flagged unsorted (needs_review): {stats.dry_run_unsorted:,}")
        else:
            lines.append(f"Already in place (no-op): {stats.already_in_place:,}")
            lines.append(f"Sorted into YYYY/YYYY-MM: {stats.sorted:,}")
            lines.append(f"Flagged unsorted (needs_review): {stats.unsorted:,}")
        lines.append(f"Errors: {stats.errors:,}" + ("  — see log for details" if stats.errors else ""))
        return "\n".join(lines)

    def _set_results_text(self, text: str) -> None:
        self.results_text.configure(state="normal")
        self.results_text.delete("1.0", "end")
        self.results_text.insert("1.0", text)
        self.results_text.configure(state="disabled")

    # ------------------------------------------------- Phase 2: captioning
    def _caption_busy(self) -> bool:
        return self.caption_worker is not None and self.caption_worker.is_alive()

    def _on_caption_start(self) -> None:
        if self._caption_busy():
            return
        if not messagebox.askyesno(
            "Photo Organizer",
            f"About to caption every JPG/PNG/HEIC under {self.cfg.dest_root} not already in "
            f"data/captions.jsonl, using Ollama model '{self.cfg.ollama_model}'.\n\n"
            "This only reads photos and appends to captions.jsonl — it never modifies, "
            "moves, or deletes any original file.\n\n"
            "At ~7-9s/image, a 100k+ file library can take several days. Safe to Cancel "
            "and resume any time — already-captioned files are skipped on the next run.\n\n"
            "Start now?",
        ):
            return
        self.caption_stop_requested = False
        self.caption_progress.configure(value=0, maximum=1)
        self.caption_status_label.configure(text="Starting...")
        # Placeholder, not blank -- the box used to sit empty for the run's
        # entire (potentially multi-day) duration until a final summary
        # landed on completion, which real usage showed reads as broken/
        # confusing (TODO.md) rather than "results not ready yet".
        self._set_caption_results_text("Running...")
        self.caption_btn.configure(state="disabled")
        self.caption_cancel_btn.configure(state="normal")

        self.caption_worker = threading.Thread(target=self._caption_worker, daemon=True)
        self.caption_worker.start()

    def _on_caption_cancel(self) -> None:
        self.caption_stop_requested = True
        self.caption_status_label.configure(text="Stopping...")

    def _caption_worker(self) -> None:
        # Runs on a background thread — never touch Tk widgets here, only
        # push messages through self.msg_queue for the main thread to apply.
        try:
            cfg = load_config()
            # Distinct logger_name (own object, own handler) so this doesn't
            # collide with the Phase 1/GPS workers' loggers if they're
            # running at the same time — see setup_logging()'s docstring.
            logger, log_path = setup_logging(
                cfg.log_dir_abs, echo_to_console=False, logger_name="photo_organizer.caption"
            )
            self.msg_queue.put(("caption_log_started", log_path))
            logger.info(f"Starting Phase 2 (captioning) run from dashboard — model={cfg.ollama_model}")
            stats = run_phase2(
                cfg, logger,
                progress_cb=lambda done, total: self.msg_queue.put(("caption_progress", done, total)),
                stop_check=lambda: self.caption_stop_requested,
            )
            self.msg_queue.put(("caption_done", stats, self.caption_stop_requested))
        except Exception as e:
            self.msg_queue.put(("caption_error", str(e)))

    def _on_caption_done(self, stats: CaptionStats, was_stopped: bool) -> None:
        self.caption_btn.configure(state="normal")
        self.caption_cancel_btn.configure(state="disabled")
        self.caption_status_label.configure(text="Stopped early" if was_stopped else "Done")
        lines = [f"Scanned: {stats.scanned:,}" + ("  (stopped early by user)" if was_stopped else "")]
        lines.append(f"Already captioned (resumed, skipped): {stats.already_captioned:,}")
        lines.append(f"Newly captioned this run: {stats.captioned:,}")
        lines.append(f"Errors: {stats.errors:,}" + ("  — see log for details" if stats.errors else ""))
        self._set_caption_results_text("\n".join(lines))

    def _on_caption_error(self, err: str) -> None:
        self.caption_btn.configure(state="normal")
        self.caption_cancel_btn.configure(state="disabled")
        self.caption_status_label.configure(text="Error")
        messagebox.showerror("Photo Organizer", f"Captioning failed: {err}")

    def _set_caption_results_text(self, text: str) -> None:
        self.caption_results_text.configure(state="normal")
        self.caption_results_text.delete("1.0", "end")
        self.caption_results_text.insert("1.0", text)
        self.caption_results_text.configure(state="disabled")

    def _start_caption_log_tail(self, log_path: Path) -> None:
        self._caption_log_path = log_path
        self._caption_log_read_pos = 0
        self.caption_log_text.configure(state="normal")
        self.caption_log_text.delete("1.0", "end")
        self.caption_log_text.configure(state="disabled")
        self._append_new_caption_log_lines()

    def _append_new_caption_log_lines(self) -> None:
        # Independent of the shared Log viewer panel's tailing state below —
        # this always shows the current/most recent captioning run's own
        # log, regardless of what that panel's dropdown is pointed at.
        if self._caption_log_path is None or not self._caption_log_path.exists():
            return
        try:
            with open(self._caption_log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._caption_log_read_pos)
                new_text = f.read()
                self._caption_log_read_pos = f.tell()
        except OSError:
            return
        if not new_text:
            return
        self.caption_log_text.configure(state="normal")
        self.caption_log_text.insert("end", new_text)
        self.caption_log_text.see("end")
        self.caption_log_text.configure(state="disabled")

    # -------------------------------------------- Phase 2b: GPS extraction
    def _gps_busy(self) -> bool:
        return self.gps_worker is not None and self.gps_worker.is_alive()

    def _on_gps_start(self) -> None:
        if self._gps_busy():
            return
        if not messagebox.askyesno(
            "Photo Organizer",
            "About to scan every photo/video already in the database for GPS EXIF data "
            "not yet checked, and offline reverse-geocode any coordinates found into a "
            "place name (e.g. \"Woodstock, GA\").\n\n"
            "This only reads files and writes gps_lat/gps_lon/location_name into the "
            "database — it never modifies, moves, or deletes any photo/video.\n\n"
            "Fast (~100 files/sec) — minutes, not days, even at 100k+ files. Safe to "
            "Cancel and resume any time; already-checked files are skipped next run.\n\n"
            "Start now?",
        ):
            return
        self.gps_stop_requested = False
        self.gps_progress.configure(value=0, maximum=1)
        self.gps_status_label.configure(text="Starting...")
        # Placeholder, not blank -- same UX fix as Captioning's panel above,
        # for the same real-usage confusion (TODO.md).
        self._set_gps_results_text("Running...")
        self.gps_btn.configure(state="disabled")
        self.gps_cancel_btn.configure(state="normal")

        self.gps_worker = threading.Thread(target=self._gps_worker, daemon=True)
        self.gps_worker.start()

    def _on_gps_cancel(self) -> None:
        self.gps_stop_requested = True
        self.gps_status_label.configure(text="Stopping...")

    def _gps_worker(self) -> None:
        # Runs on a background thread — never touch Tk widgets here, only
        # push messages through self.msg_queue for the main thread to apply.
        # Same shape as _run_worker/_caption_worker above; the only real
        # difference is this one also opens its own sqlite3 connection to
        # the SAME db file Phase 1/1b's worker may be writing to at the
        # same time (verified safe — see module docstring).
        try:
            cfg = load_config()
            # Distinct logger_name (own object, own handler) so this doesn't
            # collide with the Phase 1/Captioning workers' loggers if
            # they're running at the same time — this is the actual root
            # cause of the "GPS logs appearing in the Captioning panel" bug
            # (TODO.md) fixed this session; see setup_logging()'s docstring.
            logger, log_path = setup_logging(
                cfg.log_dir_abs, echo_to_console=False, logger_name="photo_organizer.gps"
            )
            self.msg_queue.put(("gps_log_started", log_path))
            init_db(cfg.db_path_abs, logger=logger)  # safe/idempotent — applies the gps_* column migration if needed
            logger.info("Starting Phase 2b (GPS extraction) run from dashboard")

            conn = connect(cfg.db_path_abs)
            try:
                stats = run_gps_extraction(
                    cfg, conn, logger,
                    progress_cb=lambda done, total: self.msg_queue.put(("gps_progress", done, total)),
                    stop_check=lambda: self.gps_stop_requested,
                )
            finally:
                conn.close()
            self.msg_queue.put(("gps_done", stats, self.gps_stop_requested))
        except Exception as e:
            self.msg_queue.put(("gps_error", str(e)))

    def _on_gps_done(self, stats: GpsStats, was_stopped: bool) -> None:
        self.gps_btn.configure(state="normal")
        self.gps_cancel_btn.configure(state="disabled")
        self.gps_status_label.configure(text="Stopped early" if was_stopped else "Done")
        lines = [f"Scanned: {stats.scanned:,}" + ("  (stopped early by user)" if was_stopped else "")]
        lines.append(f"GPS found + geocoded: {stats.found:,}")
        lines.append(f"No GPS data present: {stats.not_found:,}")
        lines.append(f"Errors: {stats.errors:,}" + ("  — see log for details" if stats.errors else ""))
        self._set_gps_results_text("\n".join(lines))

    def _on_gps_error(self, err: str) -> None:
        self.gps_btn.configure(state="normal")
        self.gps_cancel_btn.configure(state="disabled")
        self.gps_status_label.configure(text="Error")
        messagebox.showerror("Photo Organizer", f"GPS extraction failed: {err}")

    def _set_gps_results_text(self, text: str) -> None:
        self.gps_results_text.configure(state="normal")
        self.gps_results_text.delete("1.0", "end")
        self.gps_results_text.insert("1.0", text)
        self.gps_results_text.configure(state="disabled")

    def _start_gps_log_tail(self, log_path: Path) -> None:
        self._gps_log_path = log_path
        self._gps_log_read_pos = 0
        self.gps_log_text.configure(state="normal")
        self.gps_log_text.delete("1.0", "end")
        self.gps_log_text.configure(state="disabled")
        self._append_new_gps_log_lines()

    def _append_new_gps_log_lines(self) -> None:
        # Independent of the shared Log viewer panel and of Phase 2's own
        # tail above — always shows the current/most recent GPS extraction
        # run's own log, regardless of what else is being viewed.
        if self._gps_log_path is None or not self._gps_log_path.exists():
            return
        try:
            with open(self._gps_log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._gps_log_read_pos)
                new_text = f.read()
                self._gps_log_read_pos = f.tell()
        except OSError:
            return
        if not new_text:
            return
        self.gps_log_text.configure(state="normal")
        self.gps_log_text.insert("end", new_text)
        self.gps_log_text.see("end")
        self.gps_log_text.configure(state="disabled")

    # ---------------------------------------- Phase 2g: video thumbnails
    def _thumbs_busy(self) -> bool:
        return self.thumbs_worker is not None and self.thumbs_worker.is_alive()

    def _on_thumbs_start(self) -> None:
        if self._thumbs_busy():
            return
        if not messagebox.askyesno(
            "Photo Organizer",
            "About to decode the first frame of every video not yet cached and save it as "
            f"a JPEG under {self.cfg.thumbnail_dir}\\, for review_tool.py's grid to use "
            "instead of the generic play-icon placeholder.\n\n"
            "This only reads video files and writes new thumbnail files — it never "
            "modifies, moves, or deletes any original video, and never touches the "
            "database.\n\n"
            "~0.1s/video (benchmarked against this library's own videos) — minutes for "
            "the whole library, not days. Safe to Cancel and resume any time; "
            "already-cached (or permanently-unreadable) videos are skipped next run.\n\n"
            "Start now?",
        ):
            return
        self.thumbs_stop_requested = False
        self.thumbs_progress.configure(value=0, maximum=1)
        self.thumbs_status_label.configure(text="Starting...")
        self._set_thumbs_results_text("Running...")
        self.thumbs_btn.configure(state="disabled")
        self.thumbs_cancel_btn.configure(state="normal")

        self.thumbs_worker = threading.Thread(target=self._thumbs_worker, daemon=True)
        self.thumbs_worker.start()

    def _on_thumbs_cancel(self) -> None:
        self.thumbs_stop_requested = True
        self.thumbs_status_label.configure(text="Stopping...")

    def _thumbs_worker(self) -> None:
        # Runs on a background thread — never touch Tk widgets here, only
        # push messages through self.msg_queue for the main thread to apply.
        # Same shape as _gps_worker above, but this one never writes to the
        # DB at all (see src/thumbnail_backfill.py) — no init_db() call
        # needed, and no write-contention question to verify.
        try:
            cfg = load_config()
            # Distinct logger_name, same reasoning as every other worker
            # here — see setup_logging()'s docstring / the GPS-logging bug
            # this pattern exists to prevent.
            logger, log_path = setup_logging(
                cfg.log_dir_abs, echo_to_console=False, logger_name="photo_organizer.thumbs"
            )
            self.msg_queue.put(("thumbs_log_started", log_path))
            logger.info("Starting Phase 2g (video thumbnail extraction) run from dashboard")

            conn = connect(cfg.db_path_abs)
            try:
                stats = run_thumbnail_extraction(
                    cfg, conn, logger,
                    progress_cb=lambda done, total: self.msg_queue.put(("thumbs_progress", done, total)),
                    stop_check=lambda: self.thumbs_stop_requested,
                )
            finally:
                conn.close()
            self.msg_queue.put(("thumbs_done", stats, self.thumbs_stop_requested))
        except Exception as e:
            self.msg_queue.put(("thumbs_error", str(e)))

    def _on_thumbs_done(self, stats: ThumbStats, was_stopped: bool) -> None:
        self.thumbs_btn.configure(state="normal")
        self.thumbs_cancel_btn.configure(state="disabled")
        self.thumbs_status_label.configure(text="Stopped early" if was_stopped else "Done")
        lines = [f"Scanned: {stats.scanned:,}" + ("  (stopped early by user)" if was_stopped else "")]
        lines.append(f"Newly extracted this run: {stats.extracted:,}")
        lines.append(f"Already cached (or previously failed): {stats.already_cached:,}")
        lines.append(f"Errors: {stats.errors:,}" + ("  — see log for details" if stats.errors else ""))
        self._set_thumbs_results_text("\n".join(lines))

    def _on_thumbs_error(self, err: str) -> None:
        self.thumbs_btn.configure(state="normal")
        self.thumbs_cancel_btn.configure(state="disabled")
        self.thumbs_status_label.configure(text="Error")
        messagebox.showerror("Photo Organizer", f"Thumbnail extraction failed: {err}")

    def _set_thumbs_results_text(self, text: str) -> None:
        self.thumbs_results_text.configure(state="normal")
        self.thumbs_results_text.delete("1.0", "end")
        self.thumbs_results_text.insert("1.0", text)
        self.thumbs_results_text.configure(state="disabled")

    def _start_thumbs_log_tail(self, log_path: Path) -> None:
        self._thumbs_log_path = log_path
        self._thumbs_log_read_pos = 0
        self.thumbs_log_text.configure(state="normal")
        self.thumbs_log_text.delete("1.0", "end")
        self.thumbs_log_text.configure(state="disabled")
        self._append_new_thumbs_log_lines()

    def _append_new_thumbs_log_lines(self) -> None:
        # Independent of every other panel's log view, same reasoning as
        # GPS's own tail above — always shows the current/most recent
        # thumbnail extraction run's own log.
        if self._thumbs_log_path is None or not self._thumbs_log_path.exists():
            return
        try:
            with open(self._thumbs_log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._thumbs_log_read_pos)
                new_text = f.read()
                self._thumbs_log_read_pos = f.tell()
        except OSError:
            return
        if not new_text:
            return
        self.thumbs_log_text.configure(state="normal")
        self.thumbs_log_text.insert("end", new_text)
        self.thumbs_log_text.see("end")
        self.thumbs_log_text.configure(state="disabled")

    # ---------------------------------- Phase 3 Session 1: face detection
    def _faces_busy(self) -> bool:
        return self.faces_worker is not None and self.faces_worker.is_alive()

    def _on_faces_start(self) -> None:
        if self._faces_busy():
            return
        if not messagebox.askyesno(
            "Photo Organizer",
            "About to run face detection (insightface, buffalo_l pack) on every "
            "already-organized photo not yet scanned, writing results to "
            f"{self.cfg.faces_path} (one line per detected face, not per photo).\n\n"
            "This only reads photo files and writes to that JSONL file — it never "
            "modifies, moves, or deletes any photo, and never touches the database.\n\n"
            "Photos only, no video. Session 1 scope: this detects + benchmarks only — "
            "no clustering, no people/names yet.\n\n"
            "UNLIKE GPS/Thumbnail extraction above, this DOES use the GPU "
            "(onnxruntime's CUDA execution provider) — running it at the same time as "
            "a Phase 2 captioning run means the two will contend for the same GPU. "
            "Safe to Cancel and resume any time; already-scanned photos are skipped "
            "next run.\n\n"
            "Start now?",
        ):
            return
        self.faces_stop_requested = False
        self.faces_progress.configure(value=0, maximum=1)
        self.faces_status_label.configure(text="Starting...")
        self._set_faces_results_text("Running... (loading FaceAnalysis model may take a moment)")
        self.faces_btn.configure(state="disabled")
        self.faces_cancel_btn.configure(state="normal")

        self.faces_worker = threading.Thread(target=self._faces_worker, daemon=True)
        self.faces_worker.start()

    def _on_faces_cancel(self) -> None:
        self.faces_stop_requested = True
        self.faces_status_label.configure(text="Stopping...")

    def _faces_worker(self) -> None:
        # Runs on a background thread — never touch Tk widgets here, only
        # push messages through self.msg_queue for the main thread to apply.
        # Same shape as _gps_worker/_thumbs_worker above. Unlike those two,
        # building the FaceAnalysis app itself (model load, CUDA session
        # init) happens inside run_face_detection() on this same background
        # thread — it's slow enough (model load + CUDA context init) that it
        # must not block the Tk main loop either.
        try:
            cfg = load_config()
            logger, log_path = setup_logging(
                cfg.log_dir_abs, echo_to_console=False, logger_name="photo_organizer.faces"
            )
            self.msg_queue.put(("faces_log_started", log_path))
            init_db(cfg.db_path_abs, logger=logger)  # rule 12: read-only entry points call this too
            logger.info(f"Starting Phase 3 Session 1 (face detection) run from dashboard — model_pack={cfg.face_model_pack} gpu_enabled={cfg.gpu_enabled}")

            conn = connect(cfg.db_path_abs)
            try:
                stats = run_face_detection(
                    cfg, conn, logger, cfg.faces_path_abs, cfg.faces_scanned_path_abs,
                    progress_cb=lambda done, total: self.msg_queue.put(("faces_progress", done, total)),
                    stop_check=lambda: self.faces_stop_requested,
                )
            finally:
                conn.close()
            self.msg_queue.put(("faces_done", stats, self.faces_stop_requested))
        except Exception as e:
            self.msg_queue.put(("faces_error", str(e)))

    def _on_faces_done(self, stats: FaceDetectionStats, was_stopped: bool) -> None:
        self.faces_btn.configure(state="normal")
        self.faces_cancel_btn.configure(state="disabled")
        self.faces_status_label.configure(text="Stopped early" if was_stopped else "Done")
        lines = [f"Scanned: {stats.scanned:,}" + ("  (stopped early by user)" if was_stopped else "")]
        lines.append(f"Already scanned (skipped): {stats.already_scanned:,}")
        lines.append(f"Faces found: {stats.faces_found:,}")
        lines.append(f"Photos with 1+ face(s): {stats.photos_with_faces:,}")
        lines.append(f"Photos with 0 faces: {stats.photos_with_no_faces:,}")
        lines.append(f"Errors: {stats.errors:,}" + ("  — see log for details" if stats.errors else ""))
        self._set_faces_results_text("\n".join(lines))

    def _on_faces_error(self, err: str) -> None:
        self.faces_btn.configure(state="normal")
        self.faces_cancel_btn.configure(state="disabled")
        self.faces_status_label.configure(text="Error")
        messagebox.showerror("Photo Organizer", f"Face detection failed: {err}")

    def _set_faces_results_text(self, text: str) -> None:
        self.faces_results_text.configure(state="normal")
        self.faces_results_text.delete("1.0", "end")
        self.faces_results_text.insert("1.0", text)
        self.faces_results_text.configure(state="disabled")

    def _start_faces_log_tail(self, log_path: Path) -> None:
        self._faces_log_path = log_path
        self._faces_log_read_pos = 0
        self.faces_log_text.configure(state="normal")
        self.faces_log_text.delete("1.0", "end")
        self.faces_log_text.configure(state="disabled")
        self._append_new_faces_log_lines()

    def _append_new_faces_log_lines(self) -> None:
        if self._faces_log_path is None or not self._faces_log_path.exists():
            return
        try:
            with open(self._faces_log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._faces_log_read_pos)
                new_text = f.read()
                self._faces_log_read_pos = f.tell()
        except OSError:
            return
        if not new_text:
            return
        self.faces_log_text.configure(state="normal")
        self.faces_log_text.insert("end", new_text)
        self.faces_log_text.see("end")
        self.faces_log_text.configure(state="disabled")

    # -------------------------------------- Phase 3 Session 2: Face Clustering
    def _cluster_busy(self) -> bool:
        return self.cluster_worker is not None and self.cluster_worker.is_alive()

    def _on_cluster_start(self) -> None:
        if self._cluster_busy():
            return
        if not messagebox.askyesno(
            "Photo Organizer",
            "About to load data/faces.jsonl into the database and cluster faces by person "
            "(faiss + DBSCAN over cosine similarity).\n\n"
            "This is a fast, in-memory numerical pass — no GPU use, no network, no per-file I/O beyond "
            "the DB. Safe to run any time and safe to re-run: it only processes faces not yet grouped, "
            "and never reassigns or renames an already-grouped face — any name, merge, or split you've "
            "already done in Face Labeling is never touched.\n\n"
            "Start now?",
        ):
            return
        self.cluster_progress.configure(value=0, maximum=1)
        self.cluster_status_label.configure(text="Starting...")
        self._set_cluster_results_text("Running...")
        self.cluster_btn.configure(state="disabled")

        self.cluster_worker = threading.Thread(target=self._cluster_worker_fn, daemon=True)
        self.cluster_worker.start()

    def _cluster_worker_fn(self) -> None:
        # Runs on a background thread -- never touch Tk widgets here, only
        # push messages through self.msg_queue. Same shape as every other
        # worker above.
        try:
            cfg = load_config()
            logger, log_path = setup_logging(
                cfg.log_dir_abs, echo_to_console=False, logger_name="photo_organizer.cluster"
            )
            self.msg_queue.put(("cluster_log_started", log_path))
            init_db(cfg.db_path_abs, logger=logger)  # rule 12
            logger.info("Starting Phase 3 Session 2 (face clustering) run from dashboard")

            conn = connect(cfg.db_path_abs)
            try:
                load_stats, cluster_stats = cluster_faces_full(
                    cfg, conn, logger, cfg.faces_path_abs,
                    progress_cb=lambda done, total: self.msg_queue.put(("cluster_progress", done, total)),
                )
            finally:
                conn.close()
            self.msg_queue.put(("cluster_done", load_stats, cluster_stats))
        except Exception as e:
            self.msg_queue.put(("cluster_error", str(e)))

    def _on_cluster_done(self, load_stats: LoadStats, cluster_stats: ClusterStats) -> None:
        self.cluster_btn.configure(state="normal")
        self.cluster_status_label.configure(text="Done")
        lines = [
            f"Loaded from faces.jsonl: {load_stats.inserted:,} new, {load_stats.skipped_duplicate:,} already loaded"
            + (f", {load_stats.skipped_bad_line:,} skipped (bad line)" if load_stats.skipped_bad_line else ""),
            "",
            f"Candidates considered: {cluster_stats.candidates:,}"
            + (f"  ({cluster_stats.excluded_low_confidence:,} excluded, low confidence)" if cluster_stats.excluded_low_confidence else ""),
            f"Matched to an existing person: {cluster_stats.matched_existing:,}",
            f"New people formed: {cluster_stats.new_clusters_formed:,}  ({cluster_stats.faces_in_new_clusters:,} face(s))",
            f"Still unclustered: {cluster_stats.remained_unclustered:,}",
            f"Total people in library: {cluster_stats.people_total:,}",
        ]
        self._set_cluster_results_text("\n".join(lines))
        # Refresh an already-open Face Labeling window so newly-clustered
        # faces show up immediately, rather than only on its next manual
        # Refresh click.
        if self.face_labeling_window is not None and self.face_labeling_window.winfo_exists():
            self.face_labeling_window.reload()

    def _on_cluster_error(self, err: str) -> None:
        self.cluster_btn.configure(state="normal")
        self.cluster_status_label.configure(text="Error")
        messagebox.showerror("Photo Organizer", f"Face clustering failed: {err}")

    def _set_cluster_results_text(self, text: str) -> None:
        self.cluster_results_text.configure(state="normal")
        self.cluster_results_text.delete("1.0", "end")
        self.cluster_results_text.insert("1.0", text)
        self.cluster_results_text.configure(state="disabled")

    def _start_cluster_log_tail(self, log_path: Path) -> None:
        self._cluster_log_path = log_path
        self._cluster_log_read_pos = 0
        self.cluster_log_text.configure(state="normal")
        self.cluster_log_text.delete("1.0", "end")
        self.cluster_log_text.configure(state="disabled")
        self._append_new_cluster_log_lines()

    def _append_new_cluster_log_lines(self) -> None:
        if self._cluster_log_path is None or not self._cluster_log_path.exists():
            return
        try:
            with open(self._cluster_log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._cluster_log_read_pos)
                new_text = f.read()
                self._cluster_log_read_pos = f.tell()
        except OSError:
            return
        if not new_text:
            return
        self.cluster_log_text.configure(state="normal")
        self.cluster_log_text.insert("end", new_text)
        self.cluster_log_text.see("end")
        self.cluster_log_text.configure(state="disabled")

    def _on_open_face_labeling(self) -> None:
        if self.cfg is None:
            return
        if self.face_labeling_window is not None and self.face_labeling_window.winfo_exists():
            self.face_labeling_window.lift()
            self.face_labeling_window.focus_force()
            return
        self.face_labeling_window = FaceLabelingWindow(self.root, self.cfg)

    # ------------------------------------------------- Date Cluster Audit
    def _set_date_audit_results_text(self, text: str) -> None:
        self.date_audit_results_text.configure(state="normal")
        self.date_audit_results_text.delete("1.0", "end")
        self.date_audit_results_text.insert("1.0", text)
        self.date_audit_results_text.configure(state="disabled")
        # Every Scan/Inspect/Preview action writes its result through here --
        # auto-scroll so it's actually visible, not just written into a text
        # box that might sit off-screen below a long panel. This was the
        # real cause behind "Preview Fix appeared to do nothing" (confirmed
        # via a headless repro: the query ran and populated this box
        # correctly every time -- it was a visibility problem, not a logic
        # bug or a hidden dependency on Scan/Inspect having run first).
        self._scroll_widget_into_view(self.date_audit_results_text)

    def _on_date_audit_scan(self) -> None:
        # Step 0: a fast, synchronous, read-only query (sub-second even at
        # this library's 167k-row real scale — confirmed while building
        # this) — no worker thread needed, unlike Apply below.
        try:
            min_count = int(self.date_audit_min_count_var.get())
            top_n = int(self.date_audit_top_n_var.get())
        except ValueError:
            messagebox.showerror("Photo Organizer", "Min. count and Top N must be whole numbers.")
            return
        try:
            cfg = load_config()
            init_db(cfg.db_path_abs, logger=None)  # safe/idempotent (rule 12)
            conn = connect(cfg.db_path_abs)
            try:
                clusters = find_suspicious_clusters(conn, min_count=min_count, top_n=top_n)
            finally:
                conn.close()
        except Exception as e:
            messagebox.showerror("Photo Organizer", f"Scan failed: {e}")
            return

        for item in self.date_audit_tree.get_children():
            self.date_audit_tree.delete(item)
        for c in clusters:
            sources = ", ".join(f"{k}={v}" for k, v in sorted(c.source_counts.items(), key=lambda kv: -kv[1]))
            self.date_audit_tree.insert("", "end", iid=c.date, text=c.date,
                                         values=(f"{c.count:,}", f"{c.filesystem_fraction*100:.0f}%", sources))

        if not clusters:
            self._set_date_audit_results_text(f"No day had {min_count}+ photos/videos. Try a lower min. count.")
        else:
            self._set_date_audit_results_text(
                f"Found {len(clusters)} day(s) with {min_count}+ photos/videos. Select a row, then "
                "click Inspect Date for a deep-dive, or run Preview Fix to see proposed corrections "
                "across the whole library.\n\n"
                "Filesystem % = fraction resolved via the least-reliable fallback (no EXIF/container "
                "date, no filename date) — the strongest signal of a batch-date artifact found so far "
                "in this library. A high count with a low filesystem % is more likely a real busy day."
            )

    def _on_date_audit_tree_select(self, _event=None) -> None:
        selected = self.date_audit_tree.selection()
        if selected:
            self.date_audit_inspect_var.set(selected[0])  # the iid is the date string

    def _on_date_audit_inspect(self) -> None:
        # Step 1: also a fast, synchronous, read-only query (a bounded
        # sample of files re-checked live, not the whole cluster).
        date_str = self.date_audit_inspect_var.get().strip()
        if not date_str:
            messagebox.showinfo("Photo Organizer", "Enter a date (YYYY-MM-DD) or select one from the table above.")
            return
        try:
            cfg = load_config()
            init_db(cfg.db_path_abs, logger=None)
            conn = connect(cfg.db_path_abs)
            try:
                report = inspect_cluster(conn, date_str, sample_size=10)
            finally:
                conn.close()
        except Exception as e:
            messagebox.showerror("Photo Organizer", f"Inspect failed: {e}")
            return
        if report.total == 0:
            self._set_date_audit_results_text(f"No photos/videos found with date_taken starting {date_str}.")
            return
        self._set_date_audit_results_text(format_inspection_report(report))

    def _on_date_audit_preview_fix(self) -> None:
        # Step 2, dry run: scans the WHOLE library (not just what Scan
        # listed above — Scan is a detection aid over day-level clusters,
        # while this is the row-level, evidence-based rule from
        # src/date_audit.py that decides what's actually fixable). Fast
        # enough to run synchronously (a single-pass query over the
        # filesystem-sourced rows, ~8.5k rows in the real library).
        try:
            cfg = load_config()
            init_db(cfg.db_path_abs, logger=None)
            conn = connect(cfg.db_path_abs)
            try:
                preview = find_fix_candidates(conn)
            finally:
                conn.close()
        except Exception as e:
            messagebox.showerror("Photo Organizer", f"Preview failed: {e}")
            return

        self._date_audit_preview = preview
        self._set_date_audit_results_text(format_fix_preview(preview))
        self.date_audit_apply_btn.configure(state="normal" if preview.candidates else "disabled")

    @staticmethod
    def _parse_optional_int(text: str, field_label: str) -> int | None:
        text = text.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{field_label} must be a whole number (or left blank).")

    def _on_date_audit_interp_preview(self) -> None:
        target_prefix = self.date_audit_interp_target_var.get().strip()
        reference_prefix = self.date_audit_interp_reference_var.get().strip()
        if not target_prefix or not reference_prefix:
            messagebox.showinfo(
                "Photo Organizer",
                "Enter both the misdated batch's original_path prefix and the reference folder's "
                "current_path prefix (e.g. E:\\katya 2024 and E:\\Pics\\2020\\2020-02).",
            )
            return
        try:
            img_min = self._parse_optional_int(self.date_audit_interp_min_var.get(), "IMG # min")
            img_max = self._parse_optional_int(self.date_audit_interp_max_var.get(), "IMG # max")
        except ValueError as e:
            messagebox.showerror("Photo Organizer", str(e))
            return

        try:
            cfg = load_config()
            init_db(cfg.db_path_abs, logger=None)
            conn = connect(cfg.db_path_abs)
            try:
                candidates, stats = find_sequence_interpolation_candidates(
                    conn, target_prefix=target_prefix, reference_prefix=reference_prefix,
                    img_min=img_min, img_max=img_max,
                )
            finally:
                conn.close()
        except Exception as e:
            messagebox.showerror("Photo Organizer", f"Preview failed: {e}")
            return

        self._date_audit_interp_candidates = candidates
        self._set_date_audit_results_text(format_interpolation_preview(candidates, stats))
        self.date_audit_interp_apply_btn.configure(state="normal" if candidates else "disabled")

    def _date_audit_busy(self) -> bool:
        return self.date_audit_worker is not None and self.date_audit_worker.is_alive()

    def _on_date_audit_apply_fix(self) -> None:
        # Deliberately requires a preview to have been run in THIS session
        # (see _date_audit_preview) rather than silently re-querying —
        # forces the user to have actually looked at the list of changes
        # before anything is written, and guarantees Apply only ever acts
        # on exactly what was shown, not a possibly-different fresh query.
        preview = self._date_audit_preview
        if preview is None or not preview.candidates:
            messagebox.showinfo("Photo Organizer", "Run Preview Fix first, and confirm it found something to fix.")
            return
        self._start_date_audit_apply(
            preview.candidates, BATCH_FIX_EDITOR,
            confirm_message=(
                f"About to correct the date on {len(preview.candidates)} photo(s)/video(s), using each "
                "file's own pre-existing folder (from before this project ever sorted it) as the source "
                "of truth for the year/month — see the preview text above for the exact list.\n\n"
                "This only changes the database record (date_taken) via the same audited edit mechanism "
                "review_tool.py's manual edits use — it never moves, renames, or re-sorts any file, and "
                "every change is reversible (edit the date again in review_tool.py, or check photo_edits).\n\n"
                "Proceed?"
            ),
            disable_btns=[self.date_audit_apply_btn],
        )

    def _on_date_audit_interp_apply(self) -> None:
        candidates = self._date_audit_interp_candidates
        if not candidates:
            messagebox.showinfo("Photo Organizer", "Run Preview Sequence Fix first, and confirm it found something to fix.")
            return
        self._start_date_audit_apply(
            candidates, SEQUENCE_FIX_EDITOR,
            confirm_message=(
                f"About to correct the date on {len(candidates)} photo(s)/video(s), using camera-roll "
                "sequence numbers shared with the reference folder you named — see the preview text "
                "above for the exact list and which are exact matches vs. interpolated.\n\n"
                "This only changes the database record (date_taken) via the same audited edit mechanism "
                "review_tool.py's manual edits use — it never moves, renames, or re-sorts any file, and "
                "every change is reversible (edit the date again in review_tool.py, or check photo_edits).\n\n"
                "Proceed?"
            ),
            disable_btns=[self.date_audit_interp_apply_btn],
        )

    def _on_date_audit_apply_known_folder_fix(self) -> None:
        """One-click path for the already-investigated 822-file folder-rule
        correction (see TODO.md) — no Scan/Inspect needed, and no fields to
        fill in. Still runs a real preview against the live DB right now
        (never trusts the 822 figure blindly — the library may have
        changed) and still requires the same confirm-before-write step as
        the general Apply Fix button below; this only removes the busywork
        of re-deriving a fix that isn't a mystery anymore."""
        if self._date_audit_busy():
            return
        try:
            cfg = load_config()
            init_db(cfg.db_path_abs, logger=None)
            conn = connect(cfg.db_path_abs)
            try:
                preview = find_fix_candidates(conn)
            finally:
                conn.close()
        except Exception as e:
            messagebox.showerror("Photo Organizer", f"Preview failed: {e}")
            return

        # Keep the general workflow's own Preview/Apply state in sync too,
        # so the regular buttons below reflect exactly what this shortcut
        # just found — not a separate, hidden preview.
        self._date_audit_preview = preview
        self._set_date_audit_results_text(format_fix_preview(preview))
        self.date_audit_apply_btn.configure(state="normal" if preview.candidates else "disabled")

        if not preview.candidates:
            messagebox.showinfo(
                "Photo Organizer",
                "No folder-rule candidates found right now — this may already have been applied, "
                "or the underlying data has changed since the 822-file count was last confirmed.",
            )
            return

        self._start_date_audit_apply(
            preview.candidates, BATCH_FIX_EDITOR,
            confirm_message=(
                f"About to correct the date on {len(preview.candidates)} photo(s)/video(s) — the "
                "already-investigated folder-rule correction (each file's own pre-existing "
                "YYYY\\MM-style folder, from before this project ever sorted it, used as the source "
                "of truth for year/month). See the results box above for the full list this run found.\n\n"
                "This only changes the database record (date_taken) via the same audited edit mechanism "
                "review_tool.py's manual edits use — it never moves, renames, or re-sorts any file, and "
                "every change is reversible (edit the date again in review_tool.py, or check photo_edits).\n\n"
                "Proceed?"
            ),
            disable_btns=[self.date_audit_apply_btn, self.date_audit_known_folder_btn],
        )

    def _on_date_audit_apply_known_sequence_fix(self) -> None:
        """One-click path for the already-investigated 1,705-file Feb 2020
        vacation sequence-interpolation correction (see TODO.md) — no
        Scan/Inspect/field-filling needed. Prefills the general Sequence
        Interpolation fields with the exact parameters used (so what ran is
        visible, not hidden magic), runs a real preview against the live DB
        right now, and still requires the same confirm-before-write step as
        the general Apply Sequence Fix button below."""
        if self._date_audit_busy():
            return
        self.date_audit_interp_target_var.set(self.KNOWN_SEQUENCE_TARGET_PREFIX)
        self.date_audit_interp_reference_var.set(self.KNOWN_SEQUENCE_REFERENCE_PREFIX)
        self.date_audit_interp_min_var.set("")
        self.date_audit_interp_max_var.set(str(self.KNOWN_SEQUENCE_IMG_MAX))
        try:
            cfg = load_config()
            init_db(cfg.db_path_abs, logger=None)
            conn = connect(cfg.db_path_abs)
            try:
                candidates, stats = find_sequence_interpolation_candidates(
                    conn, target_prefix=self.KNOWN_SEQUENCE_TARGET_PREFIX,
                    reference_prefix=self.KNOWN_SEQUENCE_REFERENCE_PREFIX,
                    img_min=None, img_max=self.KNOWN_SEQUENCE_IMG_MAX,
                )
            finally:
                conn.close()
        except Exception as e:
            messagebox.showerror("Photo Organizer", f"Preview failed: {e}")
            return

        self._date_audit_interp_candidates = candidates
        self._set_date_audit_results_text(format_interpolation_preview(candidates, stats))
        self.date_audit_interp_apply_btn.configure(state="normal" if candidates else "disabled")

        if not candidates:
            messagebox.showinfo(
                "Photo Organizer",
                "No sequence-interpolation candidates found right now for the Feb 2020 vacation batch "
                "— this may already have been applied, or the underlying data has changed since the "
                "1,705-file count was last confirmed.",
            )
            return

        self._start_date_audit_apply(
            candidates, SEQUENCE_FIX_EDITOR,
            confirm_message=(
                f"About to correct the date on {len(candidates)} photo(s)/video(s) — the already-"
                "investigated Feb 2020 vacation correction (camera-roll sequence numbers matched "
                f"against {self.KNOWN_SEQUENCE_REFERENCE_PREFIX}). See the results box above for the "
                "full list this run found (exact matches vs. interpolated).\n\n"
                "This only changes the database record (date_taken) via the same audited edit mechanism "
                "review_tool.py's manual edits use — it never moves, renames, or re-sorts any file, and "
                "every change is reversible (edit the date again in review_tool.py, or check photo_edits).\n\n"
                "Proceed?"
            ),
            disable_btns=[self.date_audit_interp_apply_btn, self.date_audit_known_sequence_btn],
        )

    def _start_date_audit_apply(self, candidates: list, edited_by: str, confirm_message: str, disable_btns: list) -> None:
        if self._date_audit_busy():
            return
        if not messagebox.askyesno("Photo Organizer", confirm_message):
            return

        self.date_audit_stop_requested = False
        self.date_audit_progress.configure(value=0, maximum=max(len(candidates), 1))
        self.date_audit_status_label.configure(text="Starting...")
        self._set_date_audit_results_text("Applying fixes...")
        for btn in disable_btns:
            btn.configure(state="disabled")
        self.date_audit_scan_btn.configure(state="disabled")
        self.date_audit_cancel_btn.configure(state="normal")

        self.date_audit_worker = threading.Thread(
            target=self._date_audit_worker, args=(candidates, edited_by), daemon=True
        )
        self.date_audit_worker.start()

    def _on_date_audit_cancel(self) -> None:
        self.date_audit_stop_requested = True
        self.date_audit_status_label.configure(text="Stopping...")

    def _date_audit_worker(self, candidates: list, edited_by: str) -> None:
        # Runs on a background thread — never touch Tk widgets here, only
        # push messages through self.msg_queue, same shape as every other
        # worker in this file. Shared by both the folder-rule Apply Fix
        # button (edited_by=BATCH_FIX_EDITOR) and the Sequence Interpolation
        # Apply button (edited_by=SEQUENCE_FIX_EDITOR) — same underlying
        # apply_fixes() call either way, just a different candidate list/tag.
        try:
            cfg = load_config()
            logger, log_path = setup_logging(
                cfg.log_dir_abs, echo_to_console=False, logger_name="photo_organizer.date_audit"
            )
            self.msg_queue.put(("date_audit_log_started", log_path))
            conn = connect(cfg.db_path_abs)
            try:
                stats = apply_fixes(
                    conn, candidates, logger,
                    progress_cb=lambda done, total: self.msg_queue.put(("date_audit_progress", done, total)),
                    stop_check=lambda: self.date_audit_stop_requested,
                    edited_by=edited_by,
                )
            finally:
                conn.close()
            self.msg_queue.put(("date_audit_done", stats, self.date_audit_stop_requested))
        except Exception as e:
            self.msg_queue.put(("date_audit_error", str(e)))

    def _on_date_audit_done(self, stats: ApplyFixStats, was_stopped: bool) -> None:
        self.date_audit_apply_btn.configure(state="disabled")  # this preview is now stale/consumed
        self.date_audit_interp_apply_btn.configure(state="disabled")  # ditto for the sequence-interp preview
        # The known-fix shortcut buttons always recompute a fresh preview on
        # their own next click, so they have no "stale preview" concept —
        # just re-enable them unconditionally.
        self.date_audit_known_folder_btn.configure(state="normal")
        self.date_audit_known_sequence_btn.configure(state="normal")
        self.date_audit_scan_btn.configure(state="normal")
        self.date_audit_cancel_btn.configure(state="disabled")
        self.date_audit_status_label.configure(text="Stopped early" if was_stopped else "Done")
        self._date_audit_preview = None  # force a fresh Preview before another Apply
        self._date_audit_interp_candidates = None  # ditto for Sequence Interpolation
        lines = [f"Applied: {stats.applied:,} / {stats.candidates:,}" + ("  (stopped early by user)" if was_stopped else "")]
        lines.append(f"Skipped (manually edited since preview): {stats.skipped_manual_edit:,}")
        lines.append(f"Errors: {stats.errors:,}" + ("  — see log for details" if stats.errors else ""))
        lines.append("\nRun Scan again to see the day-level clusters reflect these corrections.")
        self._set_date_audit_results_text("\n".join(lines))

    def _on_date_audit_error(self, err: str) -> None:
        # Re-enable whichever Apply button still has a live preview behind
        # it (the failed run's own preview is still valid to retry with —
        # only a completed/partial apply invalidates it, handled in
        # _on_date_audit_done above).
        if self._date_audit_preview:
            self.date_audit_apply_btn.configure(state="normal")
        if self._date_audit_interp_candidates:
            self.date_audit_interp_apply_btn.configure(state="normal")
        self.date_audit_known_folder_btn.configure(state="normal")
        self.date_audit_known_sequence_btn.configure(state="normal")
        self.date_audit_scan_btn.configure(state="normal")
        self.date_audit_cancel_btn.configure(state="disabled")
        self.date_audit_status_label.configure(text="Error")
        messagebox.showerror("Photo Organizer", f"Date-audit fix failed: {err}")

    def _start_date_audit_log_tail(self, log_path: Path) -> None:
        self._date_audit_log_path = log_path
        self._date_audit_log_read_pos = 0
        self.date_audit_log_text.configure(state="normal")
        self.date_audit_log_text.delete("1.0", "end")
        self.date_audit_log_text.configure(state="disabled")
        self._append_new_date_audit_log_lines()

    def _append_new_date_audit_log_lines(self) -> None:
        if self._date_audit_log_path is None or not self._date_audit_log_path.exists():
            return
        try:
            with open(self._date_audit_log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._date_audit_log_read_pos)
                new_text = f.read()
                self._date_audit_log_read_pos = f.tell()
        except OSError:
            return
        if not new_text:
            return
        self.date_audit_log_text.configure(state="normal")
        self.date_audit_log_text.insert("end", new_text)
        self.date_audit_log_text.see("end")
        self.date_audit_log_text.configure(state="disabled")

    # ------------------------------------------ Phase 3b: relocate files
    def _set_relocate_results_text(self, text: str) -> None:
        self.relocate_results_text.configure(state="normal")
        self.relocate_results_text.delete("1.0", "end")
        self.relocate_results_text.insert("1.0", text)
        self.relocate_results_text.configure(state="disabled")
        # Same auto-scroll-into-view fix as Date Cluster Audit's own results
        # box (_set_date_audit_results_text) — every panel that lives this
        # far down the scrollable page needs it, not just that one.
        self._scroll_widget_into_view(self.relocate_results_text)

    def _on_relocate_preview(self) -> None:
        # Fast, synchronous, read-only query -- pure DB + path arithmetic,
        # no file I/O (see src/relocate.py's module docstring) -- no worker
        # thread needed, same reasoning as Date Cluster Audit's own Preview
        # buttons above.
        try:
            cfg = load_config()
            init_db(cfg.db_path_abs, logger=None)  # safe/idempotent (rule 12)
            conn = connect(cfg.db_path_abs)
            try:
                preview = find_relocation_candidates(conn, cfg)
            finally:
                conn.close()
        except Exception as e:
            messagebox.showerror("Photo Organizer", f"Preview failed: {e}")
            return

        self._relocate_preview = preview
        self._set_relocate_results_text(format_relocation_preview(preview))
        self.relocate_apply_btn.configure(state="normal" if preview.candidates else "disabled")

    def _relocate_busy(self) -> bool:
        return self.relocate_worker is not None and self.relocate_worker.is_alive()

    def _on_relocate_apply(self) -> None:
        # Deliberately requires a preview to have been run in THIS session
        # (see _on_date_audit_apply_fix's identical reasoning) rather than
        # silently re-querying -- forces the user to have actually looked
        # at the list of moves before anything is touched, and guarantees
        # Apply only ever acts on exactly what was shown.
        preview = self._relocate_preview
        if preview is None or not preview.candidates:
            messagebox.showinfo("Photo Organizer", "Run Preview first, and confirm it found something to relocate.")
            return
        if self._relocate_busy():
            return
        if not messagebox.askyesno(
            "Photo Organizer",
            f"About to relocate {len(preview.candidates)} photo(s)/video(s) into the folder that "
            "matches their current date_taken — see the preview text above for the exact list.\n\n"
            "Each file is copied to its correct location, verified via hash comparison, and only "
            "then deleted from its old location — never a raw move, and a naming collision at the "
            "destination is renamed, never overwritten (the identical copy-verify-delete safety "
            "Phase 1 itself uses). This is a physical file move, not a database-only edit.\n\n"
            "Proceed?",
        ):
            return

        self.relocate_stop_requested = False
        self.relocate_progress.configure(value=0, maximum=max(len(preview.candidates), 1))
        self.relocate_status_label.configure(text="Starting...")
        self._set_relocate_results_text("Relocating...")
        self.relocate_apply_btn.configure(state="disabled")
        self.relocate_cancel_btn.configure(state="normal")

        self.relocate_worker = threading.Thread(
            target=self._relocate_worker, args=(preview.candidates,), daemon=True
        )
        self.relocate_worker.start()

    def _on_relocate_cancel(self) -> None:
        self.relocate_stop_requested = True
        self.relocate_status_label.configure(text="Stopping...")

    def _relocate_worker(self, candidates: list) -> None:
        # Runs on a background thread — never touch Tk widgets here, only
        # push messages through self.msg_queue, same shape as every other
        # worker in this file.
        try:
            cfg = load_config()
            # Distinct logger_name, same reasoning as every other worker
            # here — see setup_logging()'s docstring / the GPS-logging bug
            # that pattern exists to prevent.
            logger, log_path = setup_logging(
                cfg.log_dir_abs, echo_to_console=False, logger_name="photo_organizer.relocate"
            )
            self.msg_queue.put(("relocate_log_started", log_path))
            init_db(cfg.db_path_abs, logger=logger)  # rule 12: every entry point that opens the DB calls this first
            conn = connect(cfg.db_path_abs)
            try:
                stats = apply_relocations(
                    conn, candidates, cfg, logger,
                    progress_cb=lambda done, total: self.msg_queue.put(("relocate_progress", done, total)),
                    stop_check=lambda: self.relocate_stop_requested,
                )
            finally:
                conn.close()
            self.msg_queue.put(("relocate_done", stats, self.relocate_stop_requested))
        except Exception as e:
            self.msg_queue.put(("relocate_error", str(e)))

    def _on_relocate_done(self, stats: RelocateStats, was_stopped: bool) -> None:
        self.relocate_cancel_btn.configure(state="disabled")
        self.relocate_status_label.configure(text="Stopped early" if was_stopped else "Done")
        # The preview this Apply acted on is now stale (whatever it touched
        # has moved) -- clear it so Apply requires a fresh Preview before
        # it can be clicked again, same "never act on a query the user
        # hasn't actually seen" contract as _on_relocate_apply's own check.
        self._relocate_preview = None
        self.relocate_apply_btn.configure(state="disabled")
        lines = [f"Relocated: {stats.relocated:,}" + ("  (stopped early by user)" if was_stopped else "")]
        if stats.skipped_already_correct:
            lines.append(f"Already correct (fixed by something else since the preview): {stats.skipped_already_correct:,}")
        if stats.skipped_missing_file:
            lines.append(f"Skipped, source file missing: {stats.skipped_missing_file:,}")
        if stats.skipped_hash_mismatch:
            lines.append(f"Skipped, live file content no longer matches its recorded identity: {stats.skipped_hash_mismatch:,}")
        lines.append(f"Errors: {stats.errors:,}" + ("  — see log for details" if stats.errors else ""))
        lines.append("\nRun Preview again to check for anything left (or newly created since).")
        self._set_relocate_results_text("\n".join(lines))

    def _on_relocate_error(self, err: str) -> None:
        self.relocate_apply_btn.configure(state="normal" if self._relocate_preview else "disabled")
        self.relocate_cancel_btn.configure(state="disabled")
        self.relocate_status_label.configure(text="Error")
        messagebox.showerror("Photo Organizer", f"Relocation failed: {err}")

    def _start_relocate_log_tail(self, log_path: Path) -> None:
        self._relocate_log_path = log_path
        self._relocate_log_read_pos = 0
        self.relocate_log_text.configure(state="normal")
        self.relocate_log_text.delete("1.0", "end")
        self.relocate_log_text.configure(state="disabled")
        self._append_new_relocate_log_lines()

    def _append_new_relocate_log_lines(self) -> None:
        if self._relocate_log_path is None or not self._relocate_log_path.exists():
            return
        try:
            with open(self._relocate_log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._relocate_log_read_pos)
                new_text = f.read()
                self._relocate_log_read_pos = f.tell()
        except OSError:
            return
        if not new_text:
            return
        self.relocate_log_text.configure(state="normal")
        self.relocate_log_text.insert("end", new_text)
        self.relocate_log_text.see("end")
        self.relocate_log_text.configure(state="disabled")

    # --------------------------------------------- Phase 2d: review users
    def _refresh_review_users(self) -> None:
        self.review_user_listbox.delete(0, "end")
        for u in list_review_usernames():
            self.review_user_listbox.insert("end", u)

    def _on_add_review_user(self) -> None:
        username = self.ru_username_var.get().strip()
        password = self.ru_password_var.get()
        confirm = self.ru_confirm_var.get()

        if not username:
            messagebox.showerror("Photo Organizer", "Username cannot be empty.")
            return
        if len(password) < 8:
            messagebox.showerror("Photo Organizer", "Password must be at least 8 characters.")
            return
        if password != confirm:
            messagebox.showerror("Photo Organizer", "Passwords didn't match.")
            return

        if username in list_review_usernames():
            if not messagebox.askyesno(
                "Photo Organizer",
                f"'{username}' already has a credential — overwrite its password?",
            ):
                return

        # Same hash_password() (src/auth.py) main.py's `review-user add` uses —
        # no duplicated hashing/validation logic, per CLAUDE.md rule 7.
        add_or_update_review_user(username, hash_password(password))
        self.ru_username_var.set("")
        self.ru_password_var.set("")
        self.ru_confirm_var.set("")
        self._refresh_review_users()
        messagebox.showinfo(
            "Photo Organizer", f"Saved. '{username}' can now sign in at review_tool.py's login page."
        )

    def _on_remove_review_user(self) -> None:
        sel = self.review_user_listbox.curselection()
        if not sel:
            return
        username = self.review_user_listbox.get(sel[0])
        if not messagebox.askyesno(
            "Photo Organizer",
            f"Remove '{username}'? They will no longer be able to sign in to review_tool.py.",
        ):
            return
        remove_review_user(username)
        self._refresh_review_users()

    # ---------------------------------------------- Remote Access (Phase 2d)
    def _refresh_port_status(self) -> None:
        """Queries what's actually bound to review_tool.py's configured
        port right now (src.port_check.listening_pids — a netstat-based
        check, not a "does something respond" probe — see this panel's
        docstring in the module header for why that distinction is exactly
        what a real incident hinged on) and updates the status label as a
        proper 3-state indicator (Phase 2e): Running / Not Running /
        Warning: multiple instances — each with its own label style so the
        state is visible at a glance, not just from reading the text."""
        if self.cfg is None:
            return
        port = self.cfg.review_tool_port
        try:
            pids = listening_pids(port)
        except Exception as e:
            self.review_tool_status_label.configure(
                text=f"review_tool.py: status check failed ({e})", style="TLabel"
            )
            return
        if not pids:
            self.review_tool_status_label.configure(
                text=(
                    f"● Not Running — nothing bound to port {port}. If a remote/tunnel "
                    "visit is showing an error, this is likely why: the tunnel itself "
                    "may be fine, but review_tool.py isn't running on this PC. Start it "
                    "(Launch Review Tool.bat) if it should be."
                ),
                style="Down.TLabel",
            )
        elif len(pids) == 1:
            self.review_tool_status_label.configure(
                text=f"● Running — 1 process, PID {pids[0]}, port {port}",
                style="Ok.TLabel",
            )
        else:
            pid_list = ", ".join(str(p) for p in pids)
            self.review_tool_status_label.configure(
                text=(
                    f"⚠ WARNING: {len(pids)} separate processes are bound to port {port} "
                    f"(PIDs: {pid_list}). This is the exact stale-duplicate-process pattern "
                    "that caused a real (temporary) auth bypass before — an old process "
                    "predating a security fix kept silently serving requests (see "
                    "CLAUDE.md). Stop all but one (Task Manager, or PowerShell's "
                    "Stop-Process -Id <pid> -Force) and relaunch a single fresh instance."
                ),
                style="Warn.TLabel",
            )

    def _port_status_tick(self) -> None:
        # Same self-rescheduling-loop reasoning as _poll_queue/_tail_tick
        # above: one failure here must never silently stop future checks.
        try:
            self._refresh_port_status()
        except Exception:
            pass
        self.root.after(5000, self._port_status_tick)

    def _on_force_restart(self) -> None:
        """CLAUDE.md rule 11 / TODO.md's "One-click restart" item. Confirms
        first (this kills OS processes, even if it never touches any
        project data — review_tool.py is read-only), then runs the actual
        kill-wait-relaunch on a background thread via the shared
        src.port_check.restart_review_tool() (same function "Restart
        Review Tool.bat" calls, per rule 7) so a slow port-free wait can't
        freeze the dashboard UI."""
        if self.cfg is None:
            return
        port = self.cfg.review_tool_port
        try:
            pids = listening_pids(port)
        except Exception:
            pids = []
        if pids:
            prompt = (
                f"This will force-kill {len(pids)} process(es) currently bound to port {port} "
                f"(PID(s): {', '.join(str(p) for p in pids)}) and launch a fresh review_tool.py.\n\n"
                "Continue?"
            )
        else:
            prompt = (
                f"Nothing is currently bound to port {port} — this will just launch a fresh "
                "review_tool.py.\n\nContinue?"
            )
        if not messagebox.askyesno("Photo Organizer", prompt):
            return

        self.force_restart_btn.configure(state="disabled")
        self.review_tool_status_label.configure(text="review_tool.py: restarting...", style="TLabel")
        threading.Thread(target=self._force_restart_worker, args=(self.cfg,), daemon=True).start()

    def _force_restart_worker(self, cfg: Config) -> None:
        # Runs on a background thread -- never touch Tk widgets here, only
        # push through self.msg_queue (same convention as every other
        # worker in this file).
        try:
            result = restart_review_tool(cfg, open_browser=False)
            self.msg_queue.put(("force_restart_done", result, None))
        except Exception as e:
            self.msg_queue.put(("force_restart_done", None, str(e)))

    def _on_force_restart_done(self, result: RestartResult | None, err: str | None) -> None:
        self.force_restart_btn.configure(state="normal")
        if err is not None:
            messagebox.showerror("Photo Organizer", f"Restart failed: {err}")
        elif not result.port_freed:
            port = self.cfg.review_tool_port if self.cfg else "?"
            messagebox.showerror(
                "Photo Organizer",
                f"Killed {len(result.killed_pids)} process(es), but port {port} did not free up "
                "in time — a new instance was NOT launched (launching anyway would just fail its "
                "own bind silently). Try again in a moment, or check what's still holding the "
                "port (Task Manager, or PowerShell's Stop-Process).",
            )
        # Refresh immediately rather than waiting for the next 5s tick — the whole point of this button.
        # (No separate success dialog needed — the refreshed status label says it all.)
        self._refresh_port_status()

    def _tunnel_running(self) -> bool:
        return self.tunnel_proc is not None and self.tunnel_proc.poll() is None

    def _on_tunnel_start(self) -> None:
        if self._tunnel_running():
            return
        tunnel_config = Path.home() / ".cloudflared" / "config.yml"
        if not tunnel_config.exists():
            messagebox.showerror(
                "Photo Organizer",
                f"No tunnel config found at {tunnel_config}.\n\n"
                "This panel controls an ALREADY-configured tunnel — it doesn't do the "
                "one-time Cloudflare account-linking steps (cloudflared tunnel login / "
                "create / route dns). See README.md's \"Remote access\" section.",
            )
            return

        tunnel_name = self.cfg.cloudflare_tunnel_name
        try:
            self.tunnel_proc = subprocess.Popen(
                ["cloudflared", "tunnel", "run", tunnel_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            messagebox.showerror(
                "Photo Organizer",
                "cloudflared not found on PATH. Install it first:\n"
                "  winget install --id Cloudflare.cloudflared -e\n"
                "Then relaunch the dashboard (PATH only refreshes for new processes).",
            )
            return
        except Exception as e:
            messagebox.showerror("Photo Organizer", f"Failed to start tunnel: {e}")
            return

        self._set_tunnel_log_text("")
        self.tunnel_status_label.configure(text=f"Tunnel: Running ({tunnel_name}, PID {self.tunnel_proc.pid})")
        self.tunnel_start_btn.configure(state="disabled")
        self.tunnel_stop_btn.configure(state="normal")
        threading.Thread(target=self._tunnel_reader_worker, args=(self.tunnel_proc,), daemon=True).start()

    def _tunnel_reader_worker(self, proc: subprocess.Popen) -> None:
        # Runs on a background thread — never touch Tk widgets here, only
        # push through self.msg_queue (same convention as the Phase 1/2
        # workers above). Reads until cloudflared's stdout closes (i.e. the
        # process exited, whether on its own or via terminate()/kill()
        # below), then reports the exit so the buttons/status can flip back.
        #
        # Every message carries `proc` itself so the handlers can tell a
        # fresh message apart from a STALE one — if Stop is clicked and
        # Start clicked again quickly, this worker's own "tunnel_exited"
        # for the OLD process can still be sitting in the queue behind a
        # newer "tunnel_output"/state change for the process that replaced
        # it; without the identity check, that stale exit message would
        # wipe out self.tunnel_proc for the NEW (still very much alive)
        # process, making the dashboard falsely report "stopped" while an
        # untracked cloudflared process kept running. Caught by this
        # session's own rapid-restart test, not just theoretical.
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    self.msg_queue.put(("tunnel_output", proc, line))
        except Exception:
            pass
        finally:
            returncode = proc.wait()
            self.msg_queue.put(("tunnel_exited", proc, returncode))

    def _on_tunnel_stop(self) -> None:
        if not self._tunnel_running():
            return
        self.tunnel_status_label.configure(text="Tunnel: Stopping...")
        self.tunnel_stop_btn.configure(state="disabled")
        threading.Thread(target=self._terminate_tunnel, args=(self.tunnel_proc,), daemon=True).start()

    def _terminate_tunnel(self, proc: subprocess.Popen) -> None:
        # Runs on a background thread so a slow-to-die process can't freeze
        # the UI. Doesn't push its own "done" message -- _tunnel_reader_worker
        # (already running on its own thread against this same proc) sees
        # stdout close and posts "tunnel_exited" once it's actually gone,
        # which is what flips the buttons/status back. Same termination
        # path _on_close uses at window-close time, so Stop and closing the
        # dashboard both guarantee no orphaned cloudflared process is left
        # running (see TODO.md's follow-up).
        try:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass

    def _on_tunnel_exited(self, proc: subprocess.Popen, returncode: int) -> None:
        if proc is not self.tunnel_proc:
            return  # stale message from an already-superseded process -- see _tunnel_reader_worker
        self.tunnel_proc = None
        self.tunnel_status_label.configure(text=f"Tunnel: Stopped (exit code {returncode})")
        self.tunnel_start_btn.configure(state="normal")
        self.tunnel_stop_btn.configure(state="disabled")

    def _append_tunnel_log(self, proc: subprocess.Popen, line: str) -> None:
        if proc is not self.tunnel_proc:
            return  # stale output from an already-superseded process -- see _tunnel_reader_worker
        self.tunnel_log_text.configure(state="normal")
        self.tunnel_log_text.insert("end", line)
        self.tunnel_log_text.see("end")
        self.tunnel_log_text.configure(state="disabled")

    def _set_tunnel_log_text(self, text: str) -> None:
        self.tunnel_log_text.configure(state="normal")
        self.tunnel_log_text.delete("1.0", "end")
        self.tunnel_log_text.insert("1.0", text)
        self.tunnel_log_text.configure(state="disabled")

    # -------------------------------------------------------------- logs
    def _log_dir(self) -> Path:
        return self.cfg.log_dir_abs if self.cfg else Path("logs")

    def _refresh_log_list(self) -> None:
        log_dir = self._log_dir()
        files = sorted(log_dir.glob("organize_*.log"), key=lambda p: p.stat().st_mtime, reverse=True) if log_dir.exists() else []
        names = [f.name for f in files]
        self.log_choice.configure(values=names)
        if names and not self.log_choice.get():
            self.log_choice.set(names[0])
            self._load_log_file(log_dir / names[0])

    def _on_log_choice_changed(self, _event=None) -> None:
        name = self.log_choice.get()
        if name:
            self._tailing = False  # user is browsing an older log — stop auto-tailing over it
            self._load_log_file(self._log_dir() / name)

    def _switch_to_log(self, log_path: Path) -> None:
        self._refresh_log_list()
        self.log_choice.set(log_path.name)
        self._load_log_file(log_path)
        self._tailing = True

    def _load_log_file(self, path: Path) -> None:
        self.current_log_path = path
        self._log_read_pos = 0
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._append_new_log_lines()

    def _tail_tick(self) -> None:
        # Same reasoning as _poll_queue above: a failure in either tail
        # must never skip the reschedule below, or live updates freeze
        # silently for the rest of the session.
        try:
            if self._tailing and self.autotail_var.get():
                self._append_new_log_lines()
        except Exception:
            pass
        try:
            self._append_new_caption_log_lines()  # Phase 2's own view, always live, no checkbox needed
        except Exception:
            pass
        try:
            self._append_new_gps_log_lines()  # GPS extraction's own view, same reasoning
        except Exception:
            pass
        try:
            self._append_new_thumbs_log_lines()  # Thumbnail extraction's own view, same reasoning
        except Exception:
            pass
        try:
            self._append_new_faces_log_lines()  # Face detection's own view, same reasoning
        except Exception:
            pass
        try:
            self._append_new_date_audit_log_lines()  # Date-audit Apply Fix's own view, same reasoning
        except Exception:
            pass
        try:
            self._append_new_cluster_log_lines()  # Face clustering's own view, same reasoning
        except Exception:
            pass
        try:
            self._append_new_relocate_log_lines()  # Phase 3b relocation's own view, same reasoning
        except Exception:
            pass
        self.root.after(500, self._tail_tick)

    # ------------------------------------------------------------- close
    def _on_close(self) -> None:
        # If this dashboard started the tunnel, closing the window must not
        # leave it orphaned (cloudflared is a separate OS process — a
        # Windows child process does NOT get killed just because its
        # parent Python process exits). Done synchronously (not via a
        # worker thread) since the window is closing anyway; a few seconds'
        # delay here is acceptable, unlike the Stop button's async path
        # above which shouldn't block the still-open UI.
        if self._tunnel_running():
            try:
                self.tunnel_proc.terminate()
                try:
                    self.tunnel_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.tunnel_proc.kill()
                    self.tunnel_proc.wait(timeout=5)
            except Exception:
                pass
        self.root.destroy()

    def _append_new_log_lines(self) -> None:
        if self.current_log_path is None or not self.current_log_path.exists():
            return
        try:
            with open(self.current_log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._log_read_pos)
                new_text = f.read()
                self._log_read_pos = f.tell()
        except OSError:
            return
        if not new_text:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", new_text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


class FaceLabelingWindow(tk.Toplevel):
    """Phase 3 Session 2's interactive labeling UI (photo-organizer-spec.md's
    Phase 3 section, user's explicit choice to live here rather than in
    review_tool.py) -- genuinely different from every batch-job panel above
    (no progress bar/log tail, no worker thread): browse clusters, view
    cropped face thumbnails, assign a name to a whole group, merge two
    groups, or split/move individual faces. Opens its OWN read/write DB
    connection for its lifetime (closed in _on_close) -- separate from every
    background worker's own short-lived connection, and separate from the
    dashboard's other panels, none of which hold a DB connection open.

    Not modal: the rest of the dashboard (including starting another
    clustering run) stays fully usable while this is open. If a clustering
    run finishes while this window is open, DashboardApp._on_cluster_done
    calls .reload() on it so newly-grouped faces show up without the user
    needing to close/reopen this window themselves.

    Selection model: self._selected_index indexes into self._clusters (the
    list list_clusters() returns), NOT cluster_id directly -- the
    "(Unclustered)" pseudo-group also has cluster_id=None, so tracking by
    list index is what actually distinguishes "nothing selected yet" (index
    is None) from "the Unclustered group is selected" (a real index whose
    ClusterSummary.cluster_id happens to be None).
    """

    THUMB_SIZE = 110
    THUMB_COLS = 6
    # URGENT fix (real 61,676-face cluster found at full-library scale): the
    # window used to render every face in a cluster as its own Tkinter
    # Checkbutton + PhotoImage in one shot -- fine at the sizes this was
    # designed/tested against (hundreds at most, e.g. the real 408-face
    # cluster Phase 3f's own writeup mentions), but tens of thousands of
    # live Tk widgets/decoded thumbnails at once is a hard crash (memory
    # exhaustion long before Tk itself would object), not just "slow." Fixed
    # by paginating the thumbnail grid (_render_face_page) rather than
    # rendering self._current_faces in full -- deliberately NOT hardcoded to
    # this one 61k case, so any future cluster of any size stays viewable.
    # Selection (self._selected_face_ids) is tracked by face_id regardless
    # of which page is currently rendered, so it survives paging back and
    # forth, and Select All/Clear operate on the WHOLE cluster (not just the
    # visible page) for the same reason -- see _on_select_all/_on_clear_selection.
    FACES_PER_PAGE = 500
    # URGENT follow-up session: a real incident (an off-page selection --
    # "Select All" left over from a moment earlier, mistaken for the
    # current page -- caused a Move to silently reassign 17,764 faces, most
    # never actually looked at) motivated two safety additions: a
    # confirmation before any bulk action whose selection reaches beyond
    # the current page (_confirm_if_selection_spans_pages), and a real,
    # session-only "Undo Last Action" stack (self._undo_stack) capped at
    # MAX_UNDO entries -- not persisted across window/dashboard restarts
    # (a live mistake needs a fast, no-questions-asked reversal right now,
    # not a durable audit trail; every mutation this window makes is a
    # plain DB write same as always, nothing new added there).
    MAX_UNDO = 20

    def __init__(self, master: tk.Misc, cfg: Config):
        super().__init__(master)
        self.title("Face Labeling")
        self.geometry("960x620")
        self.minsize(700, 450)
        self.cfg = cfg
        # rule 12: every entry point that opens the DB calls this first --
        # this window can be opened without ever having run the Face
        # Clustering panel's Start button first (whose own worker thread
        # would otherwise be the only thing guaranteeing a migrated schema),
        # so it must not assume that already happened.
        init_db(cfg.db_path_abs)
        self.conn = connect(cfg.db_path_abs)

        self._clusters: list = []  # list[ClusterSummary], unfiltered -- straight from list_clusters()
        # Phase 3c: self._display is what's actually shown in the listbox --
        # self._clusters with the min-size/hide-named filters applied (a
        # pure display-layer filter, never touches the DB -- see
        # _rebuild_display_list()), plus an optional trailing
        # ("not_a_person", count) entry for the "(Marked as not a person)"
        # bucket when its own toggle is on. self._selected_index indexes
        # into THIS list, not self._clusters -- see _display_key_for()'s
        # docstring for why a simple cluster_id isn't a stable enough
        # selection key on its own once this second pseudo-bucket exists.
        self._display: list[tuple[str, object]] = []
        self._not_a_person_count: int = 0
        self._current_view: str | None = None  # "cluster" | "not_a_person" | None (nothing selected)
        self._selected_index: int | None = None
        self._current_faces: list = []  # list[FaceRecord] -- the WHOLE cluster, not just the visible page
        self._face_page: int = 0  # URGENT fix: which FACES_PER_PAGE-sized slice of _current_faces is rendered
        self._undo_stack: list[dict] = []  # see MAX_UNDO comment above and _on_undo()
        self._selected_face_ids: set[int] = set()
        self._face_check_vars: dict[int, tk.BooleanVar] = {}
        # Follow-up session, direct user request ("keep the dropdown on the
        # same name when switching [between] clusters" + typeahead): the
        # Target-person field's full (unfiltered) candidate list, as
        # (cluster_id, label) pairs -- see _populate_target_combo(). The
        # selected target is resolved by exact label match against THIS
        # list (_resolve_target_cluster_id), never by list-widget index.
        self._all_targets: list[tuple[int, str]] = []
        # Second follow-up, direct user feedback that ttk.Combobox's own
        # dropdown was "a little buggy": hard to select-all the existing
        # text to retype, the filtered list never showed without manually
        # pulling the arrow down, and picking the wrong entry left you
        # stuck showing only that one choice. Replaced with a plain Entry
        # (self.target_entry, built in _build_ui) plus a custom floating
        # suggestion popup this class manages directly -- see
        # _show_target_suggestions/_hide_target_suggestions/
        # _on_target_text_changed for the real, from-scratch autocomplete
        # this project's own review_tool.py already uses in JS
        # (attachSuggestionDropdown) for the identical reason: a native
        # dropdown widget's built-in popup behavior can't be made to do
        # this reliably, a fully custom one can.
        # _target_suggestions_enabled is the mechanism that keeps
        # PROGRAMMATIC text changes (switching clusters, a local edit,
        # picking a suggestion) from re-triggering/reopening the popup the
        # same way live typing does -- both paths set the same StringVar,
        # so without this flag they'd be indistinguishable to the trace.
        self._target_suggest_items: list[tuple[int, str]] = []  # matches currently shown, parallel to the popup listbox's rows
        self._target_suggest_popup: tk.Toplevel | None = None
        self._target_suggest_listbox: tk.Listbox | None = None
        self._target_suggestions_enabled: bool = True
        # Must keep a live Python reference to every ImageTk.PhotoImage --
        # Tk itself only holds a C-level handle and the image silently goes
        # blank the moment Python garbage-collects its last reference.
        self._photo_refs: list[ImageTk.PhotoImage] = []
        # Phase 3j: hover-preview state (see _on_face_hover_enter/_leave).
        # A single pending `after()` job id and a single open tooltip
        # Toplevel at a time -- moving the mouse from one thumbnail
        # straight to another must cancel the first's pending job/close its
        # tooltip before scheduling the next, or stale previews would pile
        # up.
        self._hover_after_id: str | None = None
        self._hover_tooltip: tk.Toplevel | None = None

        # Real usability bug, this follow-up session: selecting a cluster
        # used to decode every thumbnail on the page (up to FACES_PER_PAGE,
        # real disk I/O + JPEG decode per face) SYNCHRONOUSLY on the main
        # thread before returning control to Tk -- fine for the small
        # synthetic clusters this was tested against, but on a real,
        # possibly-slow drive with hundreds of real photos, this froze the
        # whole window for minutes. Worse: while frozen, Tk cannot process
        # the mainloop at all, so any clicks the user made out of
        # impatience just queued up and fired only once the freeze ended --
        # landing the window on whatever the LAST queued click happened to
        # hit (often a small, already-named cluster nearby in the list),
        # not the one originally clicked. Fixed by decoding thumbnails on a
        # background thread (see _render_face_page/_decode_faces_worker)
        # and making cluster/page selection itself instant: the widget grid
        # (and its checkboxes -- Select All/This Page/etc. never depended
        # on images) now appears immediately, thumbnails pop in as decoded,
        # and clicking a DIFFERENT cluster/page while one is still loading
        # cleanly supersedes it rather than queuing behind it.
        #
        # self._face_load_generation is bumped every time a new page/cluster
        # load starts; a background job captures its own generation number
        # and every message it posts carries it, so _poll_face_thumb_queue
        # can silently discard results from a superseded (no-longer-
        # current) load instead of ever rendering a stale cluster's images
        # into the current view. self._face_load_stop is set to ask the
        # PREVIOUS job to bail out early between images (not mid-decode --
        # no partial/corrupt state possible) rather than let it keep doing
        # pointless disk I/O that would only slow down the new job sharing
        # the same drive.
        self._face_load_generation: int = 0
        self._face_load_stop: threading.Event = threading.Event()
        self._face_thumb_queue: "queue.Queue[tuple]" = queue.Queue()
        self._face_widgets: dict[int, tk.Checkbutton] = {}
        # Ordered face_ids actually rendered on the current page right now
        # -- kept in sync by _remove_faces_from_rendered_page() as faces
        # get resolved off of it in place, WITHOUT ever re-slicing a fresh
        # page's worth from self._current_faces (that re-slicing is exactly
        # what used to backfill the page with faces pulled up from further
        # down the list after every mutation -- see that method's docstring).
        self._page_face_ids: list[int] = []
        self._face_loading_total: int = 0
        self._face_loading_done: int = 0
        self._face_page_label_base: str = "No faces."
        self._face_poll_after_id: str | None = self.after(50, self._poll_face_thumb_queue)

        # Phase 3c filter state -- purely UI-layer, per the spec ("must
        # never affect the underlying data, clustering, or which faces are
        # assigned to which person"). Traced so typing/toggling re-renders
        # immediately without a DB round trip (_rebuild_display_list() only
        # ever reads self._clusters/self._not_a_person_count, already
        # in memory from the last reload()).
        self.min_size_var = tk.StringVar(value="3")
        self.min_size_var.trace_add("write", lambda *_: self._rebuild_display_list())
        self.hide_named_var = tk.BooleanVar(value=False)
        self.show_not_a_person_var = tk.BooleanVar(value=False)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.reload()

    def _build_ui(self) -> None:
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned)
        paned.add(left, weight=1)
        ttk.Label(left, text="People / groups:", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=6, pady=(6, 0))

        # Phase 3c: display-only filters -- never touch the DB, never alter
        # which faces belong to which person (see photo-organizer-spec.md's
        # Phase 3c section). Re-rendered live via _rebuild_display_list(),
        # no "Apply" button needed.
        filter_frame = ttk.Frame(left)
        filter_frame.pack(fill="x", padx=6, pady=(4, 0))
        size_row = ttk.Frame(filter_frame)
        size_row.pack(fill="x")
        ttk.Label(size_row, text="Hide groups smaller than").pack(side="left")
        ttk.Spinbox(size_row, from_=0, to=100000, width=5, textvariable=self.min_size_var).pack(
            side="left", padx=(4, 4)
        )
        ttk.Label(size_row, text="faces").pack(side="left")
        ttk.Checkbutton(
            filter_frame, text="Hide already-named people",
            variable=self.hide_named_var, command=self._rebuild_display_list,
        ).pack(anchor="w", pady=(2, 0))
        ttk.Checkbutton(
            filter_frame, text="Show marked-as-not-a-person",
            variable=self.show_not_a_person_var, command=self._rebuild_display_list,
        ).pack(anchor="w")
        ttk.Label(
            filter_frame,
            text='"(Unclustered)" always stays visible regardless of these filters.',
            foreground="#666666", font=("Segoe UI", 8),
            wraplength=220, justify="left",
        ).pack(anchor="w", pady=(2, 4))

        list_row = ttk.Frame(left)
        list_row.pack(fill="both", expand=True, padx=6, pady=6)
        self.cluster_listbox = tk.Listbox(list_row, exportselection=False)
        self.cluster_listbox.pack(side="left", fill="both", expand=True)
        cl_scroll = ttk.Scrollbar(list_row, orient="vertical", command=self.cluster_listbox.yview)
        cl_scroll.pack(side="right", fill="y")
        self.cluster_listbox.configure(yscrollcommand=cl_scroll.set)
        self.cluster_listbox.bind("<<ListboxSelect>>", self._on_cluster_selected)
        ttk.Button(left, text="Refresh", command=self.reload).pack(fill="x", padx=6, pady=(0, 6))

        right = ttk.Frame(paned)
        paned.add(right, weight=3)

        header = ttk.Frame(right)
        header.pack(fill="x", padx=6, pady=6)
        ttk.Label(header, text="Name:").pack(side="left")
        self.name_var = tk.StringVar()
        ttk.Entry(header, textvariable=self.name_var, width=30).pack(side="left", padx=(6, 6))
        ttk.Button(header, text="Save Name", command=self._on_save_name).pack(side="left")
        # Follow-up session, direct user request: real incident naming pet
        # ("cat") clusters and finding them attracting "all kinds of crap"
        # once face_match_existing_threshold made matching against
        # EXISTING people more lenient (much riskier for a non-human face
        # embedding than a real person's). Locking a person here removes
        # them ENTIRELY from run_face_clustering()'s automatic-matching
        # step (src/face_clustering.py::_load_existing_centroids) --
        # deliberately does NOT affect manual assignment (Move/Merge
        # Selected Faces Here), per the user's own words ("just selecting
        # them myself"). Disabled/cleared for "(Unclustered)"/the
        # not-a-person bucket/nothing-selected -- see
        # _update_lock_checkbox() -- locking only makes sense for a real
        # named/unnamed PERSON.
        self.lock_auto_match_var = tk.BooleanVar(value=False)
        self.lock_auto_match_cb = ttk.Checkbutton(
            header, text="Lock automatic matching", variable=self.lock_auto_match_var,
            command=self._on_toggle_auto_match_lock,
        )
        self.lock_auto_match_cb.pack(side="left", padx=(10, 0))
        # URGENT follow-up session: kept in the always-visible header (not
        # tucked into action_bar1 below) so it's reachable regardless of
        # which cluster/view is currently loaded -- the whole point is
        # "I just made a mistake, let me fix it immediately," not something
        # to go hunting for.
        self.undo_btn = ttk.Button(header, text="Undo Last Action", command=self._on_undo, state="disabled")
        self.undo_btn.pack(side="right")
        self.undo_label = ttk.Label(header, text="Nothing to undo yet.", foreground="#666666")
        self.undo_label.pack(side="right", padx=(0, 10))

        thumb_outer = ttk.Frame(right)
        thumb_outer.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.thumb_canvas = tk.Canvas(thumb_outer, highlightthickness=0)
        thumb_scroll = ttk.Scrollbar(thumb_outer, orient="vertical", command=self.thumb_canvas.yview)
        self.thumb_canvas.configure(yscrollcommand=thumb_scroll.set)
        self.thumb_canvas.pack(side="left", fill="both", expand=True)
        thumb_scroll.pack(side="right", fill="y")
        self.thumb_frame = ttk.Frame(self.thumb_canvas)
        thumb_window_id = self.thumb_canvas.create_window((0, 0), window=self.thumb_frame, anchor="nw")
        self.thumb_frame.bind(
            "<Configure>", lambda e: self.thumb_canvas.configure(scrollregion=self.thumb_canvas.bbox("all"))
        )
        self.thumb_canvas.bind(
            "<Configure>", lambda e: self.thumb_canvas.itemconfigure(thumb_window_id, width=e.width)
        )
        self.thumb_canvas.bind(
            "<MouseWheel>", lambda e: self.thumb_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        )

        # URGENT fix: pagination controls for _render_face_page() -- see the
        # class docstring/FACES_PER_PAGE comment above. Sits between the
        # thumbnail grid and the action buttons so it reads as "which slice
        # of this group am I looking at," not a global control.
        page_row = ttk.Frame(right)
        page_row.pack(fill="x", padx=6, pady=(0, 4))
        self.prev_page_btn = ttk.Button(page_row, text="◀ Prev page", command=self._on_prev_page)
        self.prev_page_btn.pack(side="left")
        self.page_label = ttk.Label(page_row, text="No faces.")
        self.page_label.pack(side="left", padx=(10, 10))
        self.next_page_btn = ttk.Button(page_row, text="Next page ▶", command=self._on_next_page)
        self.next_page_btn.pack(side="left")

        action_bar1 = ttk.Frame(right)
        action_bar1.pack(fill="x", padx=6, pady=(0, 4))
        # URGENT fix follow-up, same session: for an oversized cluster (the
        # exact scenario the pagination fix above was built for), Select
        # All selects the WHOLE group -- correct for resetting a confirmed-
        # bogus cluster, but the wrong tool for confirming/naming people,
        # since it forces trusting hundreds or thousands of unseen faces are
        # the same person before assigning a name. "Select This Page" is the
        # narrower, safer counterpart: review one manageable page (e.g. 200
        # faces) at a time, select just that page, then Split/Move it to a
        # person -- repeat page by page, only ever vouching for faces you've
        # actually looked at. See _on_select_page()'s docstring.
        ttk.Button(action_bar1, text="Select This Page", command=self._on_select_page).pack(side="left")
        ttk.Button(action_bar1, text="Select All", command=self._on_select_all).pack(side="left", padx=(6, 0))
        ttk.Button(action_bar1, text="Clear Selection", command=self._on_clear_selection).pack(side="left", padx=(6, 0))
        self.selection_count_label = ttk.Label(action_bar1, text="0 selected")
        self.selection_count_label.pack(side="left", padx=(10, 0))
        # Phase 3c: reversible false-positive flag -- see
        # photo-organizer-spec.md's Phase 3c section and
        # src/face_clustering.py's mark/unmark_faces_not_a_person()
        # docstrings. Both buttons stay enabled regardless of the current
        # view; the handlers themselves guard against the nonsensical
        # combination (e.g. "un-mark" while not viewing the
        # "(Marked as not a person)" bucket) with a clear message rather
        # than a disabled/greyed-out button the user has to guess why.
        ttk.Button(action_bar1, text="Mark as Not a Person", command=self._on_mark_not_a_person).pack(
            side="left", padx=(10, 0)
        )
        ttk.Button(action_bar1, text="Un-mark Selected", command=self._on_unmark_not_a_person).pack(
            side="left", padx=(6, 0)
        )
        # Phase 3f: the lighter-weight sibling of Split -- sends outliers
        # back to "(Unclustered)" without creating a new person. See
        # _on_remove_from_cluster's own docstring and
        # src/face_clustering.py::remove_faces_from_cluster().
        ttk.Button(
            action_bar1, text="Remove Selected from Cluster", command=self._on_remove_from_cluster
        ).pack(side="right", padx=(0, 6))
        ttk.Button(action_bar1, text="Split Selected Into New Person...", command=self._on_split).pack(
            side="right"
        )

        action_bar2 = ttk.Frame(right)
        action_bar2.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(action_bar2, text="Target person:").pack(side="left")
        self.target_var = tk.StringVar()
        # Second follow-up session, direct user feedback that the earlier
        # ttk.Combobox-based typeahead was "a little buggy" in three
        # specific, real ways -- fixed by replacing it with a plain Entry
        # plus a hand-built floating suggestion popup this class fully
        # controls (see the state comment in __init__ and
        # _show_target_suggestions/_on_target_text_changed below):
        #   1. "Hard to select the whole line to type" -> <FocusIn> below
        #      selects all existing text, so clicking in and just typing
        #      immediately replaces it (real Entry widgets don't do this
        #      by default; a deliberate binding is needed).
        #   2. "Don't see the filter until you pull down the dropdown" ->
        #      the popup opens/updates automatically on every keystroke,
        #      no manual pulldown needed, and also on focus-in so browsing
        #      is possible before typing anything.
        #   3. "Pick the wrong name, stuck with just that one" -> the
        #      popup always filters from self._all_targets (the FULL
        #      candidate list), never from whatever was previously shown,
        #      so clicking back in (which selects-all per #1) and typing
        #      again always re-opens the full set of real options.
        # The actual selected target is still resolved by exact label
        # match (_resolve_target_cluster_id), not by what's merely typed,
        # so a half-typed name that doesn't match anyone real is correctly
        # treated as "nothing chosen yet," not a fabricated target.
        self.target_entry = ttk.Entry(action_bar2, textvariable=self.target_var, width=34)
        self.target_entry.pack(side="left", padx=(6, 6))
        self.target_var.trace_add("write", self._on_target_text_changed)
        self.target_entry.bind("<FocusIn>", self._on_target_entry_focus_in)
        self.target_entry.bind("<FocusOut>", self._on_target_suggest_focus_out)
        self.target_entry.bind("<Down>", self._on_target_entry_arrow_down)
        self.target_entry.bind("<Escape>", lambda e: self._hide_target_suggestions())
        ttk.Button(action_bar2, text="Move Selected Faces Here", command=self._on_move).pack(side="left")
        ttk.Button(action_bar2, text="Merge This Whole Group Here", command=self._on_merge).pack(
            side="left", padx=(6, 0)
        )

    # ---------------------------------------------------------------- data
    def reload(self) -> None:
        """Refetches from the DB (list_clusters()/count_not_a_person()) and
        re-renders. Called on window open, Refresh, after every mutating
        action, and by DashboardApp when a clustering run finishes while
        this window is open. Distinct from _rebuild_display_list(), which
        only re-applies the display filters to already-fetched data --
        toggling a filter should never need to hit the DB again."""
        prev_key = self._display_key(self._selected_index)
        self._clusters = list_clusters(self.conn)
        self._not_a_person_count = count_not_a_person(self.conn)
        self._rebuild_display_list(prev_key=prev_key)

    def _display_key_for(self, kind: str, payload) -> tuple:
        """A stable identity for a display-list entry, used to re-find the
        same selection after reload()/_rebuild_display_list() rebuilds the
        list (list index alone isn't stable -- filtering or a changed face
        count can shift everything). cluster_id alone isn't quite enough
        either: the "(Unclustered)" cluster entry and the "(Marked as not a
        person)" pseudo-bucket both have no real cluster_id, so the `kind`
        tag is what actually tells them apart."""
        if kind == "cluster":
            return ("cluster", payload.cluster_id)
        return ("not_a_person", None)

    def _display_key(self, index: int | None):
        if index is None or index >= len(self._display):
            return None
        kind, payload = self._display[index]
        return self._display_key_for(kind, payload)

    def _rebuild_display_list(self, prev_key=None, reload_current_page: bool = True) -> None:
        """Phase 3c: rebuilds self._display from self._clusters/
        self._not_a_person_count (already in memory -- no DB query here)
        applying the min-size and hide-already-named filters, plus the
        optional trailing "(Marked as not a person)" entry. Purely a
        display-layer operation: never changes clustering, cluster_id
        assignment, or which faces belong to which person -- see the spec's
        own explicit requirement on this.

        reload_current_page (real usability fix, a later follow-up
        session): False skips the final _load_selected_cluster() call --
        used by _refresh_left_list_after_local_edit() to refresh the left
        list's labels/counts (and preserve/re-find the current selection
        and scroll position, all unchanged below) WITHOUT re-fetching and
        re-paginating the right-hand page, which is what used to bounce a
        big cluster back to page 1 after every single mutation. See
        _remove_faces_from_current_view's docstring for the full story.

        prev_key, when given (only by reload()), is used to try to keep the
        same logical selection across a DB refetch. A bare filter-widget
        change (no prev_key passed, e.g. the trace/command callbacks below)
        instead tries to preserve whatever's CURRENTLY selected, so typing
        into "Hide groups smaller than" doesn't blow away an in-progress
        selection every keystroke.

        Also restores the listbox's scroll position across the rebuild --
        real usability complaint: Tk's Listbox resets its scroll to the top
        on any delete(0, "end") + re-insert, regardless of whether the
        previously-selected item still exists. Barely noticeable when the
        selected cluster survives the refresh in roughly the same spot, but
        jarring for the common real workflow of working down the list and
        merging/clearing a cluster's faces entirely -- that cluster
        disappears from the list AND the view used to snap back to the top,
        losing your place after every single one."""
        if prev_key is None:
            prev_key = self._display_key(self._selected_index)
        try:
            prev_view_top = self.cluster_listbox.yview()[0]
        except tk.TclError:
            prev_view_top = 0.0

        try:
            min_size = max(0, int(self.min_size_var.get()))
        except (ValueError, tk.TclError):
            min_size = 0  # mid-edit/blank entry -- show everything rather than erroring
        hide_named = self.hide_named_var.get()

        display: list[tuple[str, object]] = []
        for c in self._clusters:
            if c.cluster_id is None:
                # "(Unclustered)" -- per spec, always visible, these filters don't apply to it.
                display.append(("cluster", c))
                continue
            if hide_named and c.assigned_name:
                continue
            if c.face_count < min_size:
                continue
            display.append(("cluster", c))

        if self.show_not_a_person_var.get() and self._not_a_person_count > 0:
            display.append(("not_a_person", self._not_a_person_count))

        self._display = display
        self.cluster_listbox.delete(0, "end")
        for kind, payload in self._display:
            if kind == "cluster":
                lock_prefix = "\U0001F512 " if payload.auto_match_locked else ""
                label = f"{lock_prefix}{payload.assigned_name or '(unnamed)'} — {payload.face_count} face(s)"
            else:
                label = f"(Marked as not a person) — {payload} face(s)"
            self.cluster_listbox.insert("end", label)

        self._selected_index = None
        if prev_key is not None:
            for i, (kind, payload) in enumerate(self._display):
                if self._display_key_for(kind, payload) == prev_key:
                    self._selected_index = i
                    self.cluster_listbox.selection_set(i)
                    break

        # Restored regardless of whether the previous selection still
        # exists -- see the docstring above. Harmless (a no-op look) when
        # nothing actually scrolled; the fix that matters is the case where
        # the selected cluster just vanished from the list entirely.
        self.cluster_listbox.yview_moveto(prev_view_top)

        if reload_current_page:
            self._load_selected_cluster()

    def _on_cluster_selected(self, event=None) -> None:
        sel = self.cluster_listbox.curselection()
        self._selected_index = sel[0] if sel else None
        self._load_selected_cluster()

    def _load_selected_cluster(self) -> None:
        """Fetches the WHOLE cluster's face list (a plain DB query -- cheap
        even at 61k+ rows, no image I/O here) and resets to page 0.
        Rendering itself is _render_face_page()'s job -- see the
        FACES_PER_PAGE comment on the class docstring for why this is split
        out rather than looping over every face here like it used to."""
        self._selected_face_ids.clear()

        idx = self._selected_index
        if idx is None or idx >= len(self._display):
            self._current_view = None
            self._current_faces = []
            self.name_var.set("")
            self._all_targets = []
            self._set_target_text_programmatic("")
            self._update_lock_checkbox(None)
            self._face_page = 0
            self._render_face_page()
            return

        kind, payload = self._display[idx]
        self._current_view = kind

        if kind == "not_a_person":
            # No name/merge-target concept for this bucket -- see the
            # _on_save_name/_on_split/_on_move/_on_merge guards below, which
            # are the actual enforcement of "un-mark first, then split/move
            # normally" rather than a disabled widget here.
            self.name_var.set("")
            self._current_faces = list_not_a_person_faces(self.conn)
            self._all_targets = []
            self._set_target_text_programmatic("")
            self._update_lock_checkbox(None)
        else:
            summary = payload
            self.name_var.set(summary.assigned_name or "" if summary.cluster_id is not None else "")
            self._current_faces = list_faces_in_cluster(self.conn, summary.cluster_id)
            # Direct user request: keep the Target dropdown on the same
            # name when switching BETWEEN clusters, not just within one --
            # _populate_target_combo() preserves whatever's currently
            # selected (by cluster_id identity) if it's still a valid
            # target here.
            self._populate_target_combo(exclude_cluster_id=summary.cluster_id)
            self._update_lock_checkbox(summary)

        self._face_page = 0
        self._render_face_page()

    def _render_face_page(self) -> None:
        """Renders only the current FACES_PER_PAGE-sized slice of
        self._current_faces -- the actual fix for the 61k-face crash (see
        FACES_PER_PAGE's comment). Only this slice gets its face cropped
        from disk/turned into a PhotoImage/given a Tkinter widget; the rest
        of the cluster sits as plain FaceRecord data (cheap) until paged
        to. Checkbox state is seeded from self._selected_face_ids (which
        persists across pages), so a selection made on one page is still
        reflected if you page away and back.

        Real usability bug, a later follow-up session: this used to decode
        (open + JPEG-decode, real disk I/O) EVERY thumbnail on the page
        synchronously, right here, before ever returning control to Tk --
        fine against the small synthetic fixtures this was built/tested
        against, but on a real slow drive with hundreds of real photos this
        froze the entire window for minutes, with NOTHING visibly updating
        in the meantime (not even the Name field or page label, despite
        those being cheap DB-only lookups already computed in
        _load_selected_cluster -- they were stuck behind this same blocked
        mainloop). Worse, while frozen, Tk cannot process input at all, so
        any further clicks made out of impatience just queued up and fired
        one-by-one, each triggering its own full synchronous re-decode,
        only settling on whatever the LAST queued click happened to land on
        -- often a different, smaller, already-named cluster nearby in the
        (size-sorted, so the biggest and slowest is always first) list.

        Fixed by making the actual pixel decoding asynchronous: this method
        now only builds the widget grid (all of it, with a plain gray
        placeholder image) and returns immediately -- the Name field,
        target combo, and page label are already showing correctly by the
        time this returns, same as clicking anywhere else in the window.
        Real thumbnails are decoded on a background thread
        (_decode_faces_worker) and streamed in one at a time via
        _poll_face_thumb_queue, which swaps each placeholder for its real
        image as it arrives. Selecting a DIFFERENT cluster/page while one is
        still streaming in cleanly supersedes it (see the
        self._face_load_generation/_face_load_stop comment in __init__)
        rather than queuing behind it -- clicking is always instant and
        always reflects whatever was clicked LAST, including the largest
        cluster in the list."""
        # A different cluster/page is being rendered now -- ask whichever
        # background job was decoding the PREVIOUS page to stop between
        # images (not mid-decode) rather than keep burning disk I/O nobody
        # will ever see, and bump the generation so any of its messages
        # still in flight get silently discarded by the poll loop below.
        self._face_load_stop.set()
        self._face_load_generation += 1
        gen = self._face_load_generation
        self._face_load_stop = threading.Event()
        stop_event = self._face_load_stop

        for widget in self.thumb_frame.winfo_children():
            widget.destroy()
        self._photo_refs.clear()
        self._face_check_vars.clear()
        self._face_widgets.clear()
        self._page_face_ids.clear()
        # Rebuilding the grid (page change, cluster switch, reload) destroys
        # every widget a pending hover job/tooltip might reference -- cancel
        # both so a stale after() callback can't fire against a dead widget.
        self._cancel_hover_preview()

        total = len(self._current_faces)
        page_count = max(1, (total + self.FACES_PER_PAGE - 1) // self.FACES_PER_PAGE)
        self._face_page = max(0, min(self._face_page, page_count - 1))
        start = self._face_page * self.FACES_PER_PAGE
        end = min(start + self.FACES_PER_PAGE, total)
        page_faces = self._current_faces[start:end]

        self._face_page_label_base = (
            "No faces." if total == 0
            else f"Showing {start + 1}-{end} of {total} face(s) — page {self._face_page + 1}/{page_count}"
        )
        self._face_loading_total = len(page_faces)
        self._face_loading_done = 0

        # One shared placeholder image for every widget on this page --
        # cheap (a single small solid-gray bitmap), and every Checkbutton
        # just displays the same one until its real thumbnail streams in.
        # Kept alive in self._photo_refs like any other PhotoImage.
        placeholder = ImageTk.PhotoImage(Image.new("RGB", (self.THUMB_SIZE, self.THUMB_SIZE), (200, 200, 200)))
        self._photo_refs.append(placeholder)

        for i, face in enumerate(page_faces):
            var = tk.BooleanVar(value=face.face_id in self._selected_face_ids)
            self._face_check_vars[face.face_id] = var
            cb = tk.Checkbutton(
                self.thumb_frame, image=placeholder, variable=var, indicatoron=False,
                selectcolor="#8fc7ff", borderwidth=2,
                command=lambda fid=face.face_id, v=var: self._on_face_toggle(fid, v),
            )
            cb.grid(row=i // self.THUMB_COLS, column=i % self.THUMB_COLS, padx=3, pady=3)
            # Phase 3j: double-click opens the full original photo with this
            # face's bbox highlighted (the actual requirement); hovering
            # shows a small delayed preview of the same thing (the
            # nice-to-have -- see _on_face_hover_enter's docstring for why
            # it's debounced rather than instant). Both work immediately,
            # even before this face's own grid thumbnail has streamed in --
            # they load their own (single-photo, already-fast) image
            # independently.
            cb.bind("<Double-Button-1>", lambda e, f=face: self._on_face_double_click(f))
            cb.bind("<Enter>", lambda e, f=face, w=cb: self._on_face_hover_enter(f, w))
            cb.bind("<Leave>", lambda e: self._on_face_hover_leave())
            self._face_widgets[face.face_id] = cb
            self._page_face_ids.append(face.face_id)

        self._update_page_status_label()
        self.prev_page_btn.configure(state="normal" if self._face_page > 0 else "disabled")
        self.next_page_btn.configure(state="normal" if self._face_page < page_count - 1 else "disabled")
        self._update_selection_count()

        if page_faces:
            threading.Thread(
                target=self._decode_faces_worker, args=(gen, page_faces, stop_event), daemon=True
            ).start()

    def _decode_faces_worker(self, gen: int, faces: list, stop_event: threading.Event) -> None:
        """Runs on a background thread -- must never touch a Tk widget
        directly, only push results through self._face_thumb_queue for
        _poll_face_thumb_queue (main thread) to apply. `gen` and
        `stop_event` are the specific values _render_face_page captured for
        THIS page load, passed in explicitly rather than read back off
        `self` later, since `self._face_load_generation`/`_face_load_stop`
        may have already moved on to a newer page/cluster by the time any
        given iteration of this loop runs."""
        for face in faces:
            if stop_event.is_set():
                return  # superseded by a newer selection -- stop doing pointless disk I/O
            try:
                crop = crop_face_thumbnail(face.current_path, face.bbox, size=self.THUMB_SIZE)
            except Exception:
                # Photo missing/moved/corrupt since detection -- a plain
                # gray placeholder rather than letting one bad file break
                # the whole page (a slightly darker gray than the
                # loading placeholder, so "failed" reads differently from
                # "still loading" if you look closely).
                crop = Image.new("RGB", (self.THUMB_SIZE, self.THUMB_SIZE), (160, 160, 160))
            self._face_thumb_queue.put(("thumb", gen, face.face_id, crop))

    def _poll_face_thumb_queue(self) -> None:
        """Drains self._face_thumb_queue and applies each decoded thumbnail
        to its widget -- the ONLY place PhotoImage objects get created for
        the grid, since that must happen on the main thread. Same
        defensive per-message try/except shape as DashboardApp._poll_queue
        (one bad message must never kill this polling loop for the rest of
        the window's life). Reschedules itself for as long as the window
        exists; cancelled in _on_close()."""
        try:
            while True:
                msg = self._face_thumb_queue.get_nowait()
                try:
                    kind, gen, face_id, crop = msg
                    if kind == "thumb" and gen == self._face_load_generation:
                        # Anything else (a stale generation from a
                        # since-superseded selection) is silently
                        # discarded -- see the generation/stop_event
                        # comment in __init__.
                        widget = self._face_widgets.get(face_id)
                        if widget is not None and widget.winfo_exists():
                            photo = ImageTk.PhotoImage(crop)
                            self._photo_refs.append(photo)
                            widget.configure(image=photo)
                        self._face_loading_done += 1
                        self._update_page_status_label()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self._face_poll_after_id = self.after(50, self._poll_face_thumb_queue)

    def _update_page_status_label(self) -> None:
        text = self._face_page_label_base
        if self._face_loading_total and self._face_loading_done < self._face_loading_total:
            text += f"  (loading {self._face_loading_done}/{self._face_loading_total} thumbnail(s)...)"
        self.page_label.configure(text=text)

    def _remove_faces_from_rendered_page(self, face_ids: set[int]) -> None:
        """The actual fix for "the page keeps getting filled up, I never
        make strides" (direct user feedback): removes exactly the given
        faces from the page ALREADY ON SCREEN, closing the gap by
        re-gridding whatever's left -- unlike _render_face_page(), this
        never re-slices a fresh FACES_PER_PAGE-sized window from
        self._current_faces, so the page only ever SHRINKS as faces are
        resolved off of it (Split/Move/Remove/Mark). New content only
        ever appears once the user explicitly turns the page, re-selects
        the cluster, or clears this page down to nothing (see below) --
        never as an unrequested side effect of taking an action.

        Called by _remove_faces_from_current_view AFTER self._current_faces
        has already been filtered -- this only touches the widget grid/
        self._page_face_ids, the DB-backed master list is handled there."""
        removed_any = False
        for fid in face_ids:
            widget = self._face_widgets.pop(fid, None)
            if widget is not None:
                widget.destroy()
                removed_any = True
            self._face_check_vars.pop(fid, None)
        if not removed_any:
            return
        self._page_face_ids = [fid for fid in self._page_face_ids if fid in self._face_widgets]

        if not self._page_face_ids:
            # This page is now fully cleared -- a natural, expected moment
            # to show whatever's next (or "No faces." if the group itself
            # is now empty too), not something to linger on. A full
            # _render_face_page() re-slice here is fine/correct: it's an
            # explicit consequence of just finishing this exact page, not
            # an unrequested backfill triggered by a partial removal.
            self._render_face_page()
            return

        # Re-grid the survivors in row-major order to close the gap left
        # behind by the removed widgets -- the actual "no backfill" fix.
        for i, fid in enumerate(self._page_face_ids):
            self._face_widgets[fid].grid(row=i // self.THUMB_COLS, column=i % self.THUMB_COLS, padx=3, pady=3)

        total = len(self._current_faces)
        page_count = max(1, (total + self.FACES_PER_PAGE - 1) // self.FACES_PER_PAGE)
        self._face_page = max(0, min(self._face_page, page_count - 1))
        self._face_page_label_base = (
            f"{len(self._page_face_ids)} face(s) remaining on this page — "
            f"page {self._face_page + 1}/{page_count} ({total} total left in this group)"
        )
        self._update_page_status_label()
        self.prev_page_btn.configure(state="normal" if self._face_page > 0 else "disabled")
        self.next_page_btn.configure(state="normal" if self._face_page < page_count - 1 else "disabled")
        self._update_selection_count()

    # ----------------------------------------------- Phase 3j: full photo
    def _popup_max_size(self) -> tuple[int, int]:
        """A reasonable window size to scale the full photo to fit within --
        not full native resolution (per spec), but not a fixed small
        constant either, since this dashboard can run on very different
        screens. 75% of the actual screen, floored so it's never
        unreasonably tiny."""
        return (
            max(500, int(self.winfo_screenwidth() * 0.75)),
            max(400, int(self.winfo_screenheight() * 0.75)),
        )

    def _on_face_double_click(self, face) -> None:
        """The actual Phase 3j requirement: opens the full original photo a
        face crop came from, in a new window, with that specific face's
        bbox highlighted -- so a too-tight crop (a baby/toddler, an awkward
        angle, a group shot) can be identified with real context. Reuses
        crop_face_thumbnail's own PIL loading approach via
        src.face_clustering.load_full_photo_with_bbox (no second
        image-loading path); purely a viewing aid, nothing here writes to
        the DB or the file."""
        try:
            img = load_full_photo_with_bbox(face.current_path, face.bbox, max_size=self._popup_max_size())
        except Exception as exc:
            messagebox.showerror(
                "Can't open photo",
                f"Couldn't load the original photo for this face:\n{face.current_path}\n\n{exc}",
                parent=self,
            )
            return

        win = tk.Toplevel(self)
        win.title(Path(face.current_path).name)
        photo = ImageTk.PhotoImage(img)
        # Same ImageTk gotcha as self._photo_refs above -- stash a reference
        # on the window itself so it survives for the window's own
        # lifetime, independent of this method's local scope.
        win._photo_ref = photo
        ttk.Label(win, image=photo).pack()
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(4, 8))
        win.transient(self)
        win.focus_set()

    def _cancel_hover_preview(self) -> None:
        if self._hover_after_id is not None:
            self.after_cancel(self._hover_after_id)
            self._hover_after_id = None
        if self._hover_tooltip is not None:
            self._hover_tooltip.destroy()
            self._hover_tooltip = None

    def _on_face_hover_enter(self, face, widget: tk.Widget) -> None:
        """Nice-to-have half of Phase 3j: after a short pause on one
        thumbnail (debounced via after() -- not instant, so sweeping the
        mouse across the grid doesn't decode a full photo per thumbnail
        passed over), pop up a small non-interactive preview of the same
        full-photo-with-highlighted-bbox double-click already opens. This
        stays cleanly separable from the double-click requirement -- if
        anything about it were fragile, it could be deleted without
        touching that code path at all."""
        self._cancel_hover_preview()
        self._hover_after_id = self.after(350, lambda: self._show_hover_preview(face, widget))

    def _on_face_hover_leave(self) -> None:
        self._cancel_hover_preview()

    def _show_hover_preview(self, face, widget: tk.Widget) -> None:
        self._hover_after_id = None
        try:
            img = load_full_photo_with_bbox(face.current_path, face.bbox, max_size=(420, 420))
        except Exception:
            return  # silent -- a hover preview failing shouldn't interrupt anything; double-click still reports the real error

        tip = tk.Toplevel(self)
        tip.overrideredirect(True)  # borderless -- reads as a tooltip, not a window
        tip.attributes("-topmost", True)
        x = widget.winfo_rootx() + widget.winfo_width() + 8
        y = widget.winfo_rooty()
        tip.geometry(f"+{x}+{y}")
        photo = ImageTk.PhotoImage(img)
        tip._photo_ref = photo
        tk.Label(tip, image=photo, borderwidth=2, relief="solid").pack()
        self._hover_tooltip = tip

    def _on_prev_page(self) -> None:
        if self._face_page > 0:
            self._face_page -= 1
            self._render_face_page()

    def _on_next_page(self) -> None:
        self._face_page += 1
        self._render_face_page()

    # ------------------------------------------------------------- actions
    def _update_selection_count(self) -> None:
        self.selection_count_label.configure(text=f"{len(self._selected_face_ids)} selected")

    def _on_face_toggle(self, face_id: int, var: tk.BooleanVar) -> None:
        if var.get():
            self._selected_face_ids.add(face_id)
        else:
            self._selected_face_ids.discard(face_id)
        self._update_selection_count()

    def _on_select_page(self) -> None:
        """Selects EXACTLY the faces on the currently-rendered page,
        replacing any prior selection -- the way to review and assign one
        trusted, manageable chunk of an oversized cluster (FACES_PER_PAGE
        faces at a time) without first vouching for the whole group,
        which might be tens of thousands of faces spanning many different
        people (see the "real 61,676/17,764-face chained cluster" writeup
        in CLAUDE.md/TODO.md). Workflow: page through a big "(Unclustered)"-
        style group, and on each page that clearly looks like one person,
        click this, uncheck any obvious outlier, then "Split Selected Into
        New Person..." (first page) or "Move Selected Faces Here" (an
        already-created person from an earlier page) -- the assigned faces
        leave this cluster, so the NEXT unassigned chunk becomes page 1
        after the window's automatic reload(), ready to repeat."""
        self._selected_face_ids = set(self._face_check_vars.keys())
        for var in self._face_check_vars.values():
            var.set(True)
        self._update_selection_count()

    def _on_select_all(self) -> None:
        """Selects every face in the WHOLE current cluster, not just the
        ones on the currently-rendered page -- important now that a cluster
        can be far bigger than one page (see FACES_PER_PAGE). Selection
        itself is just face_ids in a set (cheap even at 61k+); only the
        currently-visible checkboxes need their widget state updated."""
        for face in self._current_faces:
            self._selected_face_ids.add(face.face_id)
        for fid, var in self._face_check_vars.items():
            var.set(True)
        self._update_selection_count()

    def _on_clear_selection(self) -> None:
        self._selected_face_ids.clear()
        for var in self._face_check_vars.values():
            var.set(False)
        self._update_selection_count()

    def _current_summary(self):
        """Returns the selected ClusterSummary, or None if either nothing is
        selected OR the current view is the "(Marked as not a person)"
        bucket (which has no ClusterSummary at all -- see _display's
        ("not_a_person", count) entries). Callers that need to tell those
        two None-causes apart for a clearer error message check
        self._current_view themselves first (see _on_save_name/_on_merge
        below) rather than relying on this alone."""
        idx = self._selected_index
        if idx is None or idx >= len(self._display):
            return None
        kind, payload = self._display[idx]
        return payload if kind == "cluster" else None

    # ------------------------------------ optimistic in-place view updates
    def _refresh_left_list_after_local_edit(self) -> None:
        """Refreshes cluster summaries/counts in the left list (so a
        face_count label doesn't go stale after a mutation) WITHOUT
        touching the right-hand page/grid -- used after a mutation whose
        effect on the CURRENTLY VIEWED page has already been applied
        locally (see _remove_faces_from_current_view), so the user's
        place/page/scroll position in a large cluster is never lost to a
        mutation that only moved a handful of faces out of thousands."""
        prev_key = self._display_key(self._selected_index)
        self._clusters = list_clusters(self.conn)
        self._not_a_person_count = count_not_a_person(self.conn)
        self._rebuild_display_list(prev_key=prev_key, reload_current_page=False)

    def _resolve_target_cluster_id(self) -> int | None:
        """Resolves the Target-person field's CURRENT TEXT to a cluster_id
        by exact label match against self._all_targets (the full,
        unfiltered candidate list) -- never by list-widget index, which
        stops meaning anything the moment the suggestion popup is showing
        a filtered subset. Returns None if the text doesn't exactly match
        a real target (nothing chosen yet, or a half-typed name)."""
        text = self.target_var.get()
        for cid, label in self._all_targets:
            if label == text:
                return cid
        return None

    def _set_target_text_programmatic(self, text: str) -> None:
        """Sets the Target-person field's text as CODE, not as the user
        typing -- switching clusters, a local edit refresh, or picking a
        suggestion from the popup. self._target_suggestions_enabled is
        what tells _on_target_text_changed (the var's trace) to ignore
        this particular change rather than treating it as a fresh keystroke
        and reopening/re-filtering the popup around it -- both paths set
        the exact same StringVar, so without this flag they'd look
        identical to the trace."""
        self._target_suggestions_enabled = False
        self.target_var.set(text)
        self._target_suggestions_enabled = True
        self._hide_target_suggestions()

    def _populate_target_combo(self, exclude_cluster_id) -> None:
        """(Re)computes the Target-person field's full candidate list --
        called both when switching to look at a DIFFERENT cluster
        (_load_selected_cluster) and after a local edit changes someone's
        face count (_remove_faces_from_current_view). Either way, whichever
        target is currently selected is preserved BY CLUSTER_ID IDENTITY,
        not by label text (a label's own face count is exactly what a
        local edit just changed, and switching clusters changes which
        person is even excluded from the list) -- two real, direct user
        complaints this fixes: repeatedly moving faces to the same person
        no longer requires re-picking them after every move, AND paging
        through many small clusters while always sending outliers to the
        same target no longer requires re-picking them after every single
        cluster switch either.

        Falls back to the first available target only when there wasn't a
        previous selection, or it's no longer offered here (most commonly
        because it IS the newly-selected cluster itself, excluded from its
        own target list)."""
        prev_cid = self._resolve_target_cluster_id()
        self._all_targets = list_people_for_merge_target(self.conn, exclude_cluster_id=exclude_cluster_id)
        label_by_cid = {cid: label for cid, label in self._all_targets}
        if prev_cid is not None and prev_cid in label_by_cid:
            self._set_target_text_programmatic(label_by_cid[prev_cid])
        elif self._all_targets:
            self._set_target_text_programmatic(self._all_targets[0][1])
        else:
            self._set_target_text_programmatic("")

    # ------------------------------------ Target-person suggestion popup
    # Second follow-up session, direct user feedback that ttk.Combobox's
    # own dropdown was "a little buggy" -- see the three numbered
    # complaints in _build_ui's comment above the Entry widget, and the
    # class-level state comment in __init__. This is a small, from-scratch
    # autocomplete (an Entry + a borderless Toplevel holding a Listbox
    # this class positions/fills/shows/hides itself), the same underlying
    # idea as review_tool.py's own JS attachSuggestionDropdown -- a native
    # dropdown widget's built-in popup mechanics can't reliably be made to
    # auto-open-while-typing and stay browsable-after-a-wrong-pick; a
    # fully custom one can, because this class controls every part of it.
    def _on_target_text_changed(self, *_args) -> None:
        if not self._target_suggestions_enabled:
            return  # a programmatic set (see _set_target_text_programmatic), not the user typing
        typed = self.target_var.get().strip().lower()
        matches = self._all_targets if not typed else [
            (cid, label) for cid, label in self._all_targets if typed in label.lower()
        ]
        self._target_suggest_items = matches
        if matches:
            self._show_target_suggestions(matches)
        else:
            self._hide_target_suggestions()

    def _on_target_entry_focus_in(self, event=None) -> None:
        # Complaint #1 ("hard to select the whole line to type"): select
        # all existing text so the user can just start typing to replace
        # it, the way a real search box behaves. Scheduled via after(1,...)
        # since the click that produced this focus event would otherwise
        # place the cursor AFTER our selection ran, undoing it immediately.
        self.target_entry.after(1, lambda: self.target_entry.select_range(0, "end"))
        # Also show the current candidate list right away, not just after
        # the first keystroke -- lets the user browse by clicking in alone.
        self._on_target_text_changed()

    def _on_target_entry_arrow_down(self, event=None) -> str | None:
        """Pressing Down while typing moves keyboard focus into the
        suggestion popup itself, so it can be arrow-navigated + committed
        with Enter -- a keyboard-only fallback alongside clicking a
        suggestion with the mouse."""
        if self._target_suggest_popup is not None and self._target_suggest_popup.winfo_exists() \
                and self._target_suggest_listbox.size() > 0:
            self._target_suggest_listbox.focus_set()
            self._target_suggest_listbox.selection_clear(0, "end")
            self._target_suggest_listbox.selection_set(0)
            self._target_suggest_listbox.activate(0)
            return "break"
        return None

    def _on_target_suggest_focus_out(self, event=None) -> None:
        # Delayed so a click landing on the suggestion popup (which also
        # blurs the entry) has a chance to actually register as a pick
        # BEFORE this decides whether to close the popup -- closing
        # immediately on focus-out would eat the click that was supposed
        # to choose something (the classic dropdown-loses-focus-before-
        # the-click-lands bug).
        self.after(150, self._maybe_hide_target_suggestions)

    def _maybe_hide_target_suggestions(self) -> None:
        if self._target_suggest_popup is None or not self._target_suggest_popup.winfo_exists():
            return
        focused = self.focus_get()
        if focused is self._target_suggest_listbox:
            return  # focus moved INTO the popup (e.g. via arrow-down) -- keep it open
        self._hide_target_suggestions()

    def _show_target_suggestions(self, matches: list[tuple[int, str]]) -> None:
        if self._target_suggest_popup is None or not self._target_suggest_popup.winfo_exists():
            popup = tk.Toplevel(self)
            popup.withdraw()
            popup.overrideredirect(True)  # a borderless popup, not a real window -- reads as part of the field
            popup.attributes("-topmost", True)
            lb = tk.Listbox(popup, activestyle="dotbox", exportselection=False)
            lb.pack(fill="both", expand=True)
            lb.bind("<ButtonRelease-1>", self._on_target_suggestion_chosen)
            lb.bind("<Return>", self._on_target_suggestion_chosen)
            lb.bind("<Escape>", lambda e: (self._hide_target_suggestions(), self.target_entry.focus_set()))
            lb.bind("<FocusOut>", self._on_target_suggest_focus_out)
            self._target_suggest_popup = popup
            self._target_suggest_listbox = lb
        popup = self._target_suggest_popup
        lb = self._target_suggest_listbox
        lb.delete(0, "end")
        for _, label in matches:
            lb.insert("end", label)
        visible_rows = min(10, len(matches))
        lb.configure(height=visible_rows)
        x = self.target_entry.winfo_rootx()
        y = self.target_entry.winfo_rooty() + self.target_entry.winfo_height()
        width = max(self.target_entry.winfo_width(), 260)
        popup.geometry(f"{width}x{visible_rows * 18}+{x}+{y}")
        popup.deiconify()

    def _hide_target_suggestions(self) -> None:
        if self._target_suggest_popup is not None and self._target_suggest_popup.winfo_exists():
            self._target_suggest_popup.withdraw()

    def _on_target_suggestion_chosen(self, event=None) -> None:
        lb = self._target_suggest_listbox
        sel = lb.curselection()
        if not sel or sel[0] >= len(self._target_suggest_items):
            return
        _, label = self._target_suggest_items[sel[0]]
        self._set_target_text_programmatic(label)
        self.target_entry.focus_set()
        self.target_entry.select_range(0, "end")

    def _update_lock_checkbox(self, summary) -> None:
        """Reflects the currently selected cluster's auto_match_locked
        state in the header checkbox -- disabled entirely for
        "(Unclustered)"/the not-a-person bucket/nothing selected, since
        locking only makes sense for a real named/unnamed PERSON (a stable
        cluster_id a future automatic run could actually route new faces
        to)."""
        if summary is None or summary.cluster_id is None:
            self.lock_auto_match_var.set(False)
            self.lock_auto_match_cb.configure(state="disabled")
        else:
            self.lock_auto_match_var.set(summary.auto_match_locked)
            self.lock_auto_match_cb.configure(state="normal")

    def _on_toggle_auto_match_lock(self) -> None:
        summary = self._current_summary()
        if summary is None or summary.cluster_id is None:
            # Shouldn't be reachable -- the checkbox is disabled for these
            # cases -- but guard anyway rather than writing to a
            # nonexistent/pseudo cluster_id.
            self.lock_auto_match_var.set(False)
            return
        set_person_auto_match_locked(self.conn, summary.cluster_id, self.lock_auto_match_var.get())
        # Refreshes the left list's own label (the lock emoji prefix) --
        # doesn't touch the right-hand page, same as every other
        # local-edit refresh in this window.
        self._refresh_left_list_after_local_edit()

    def _remove_faces_from_current_view(self, face_ids: set[int]) -> None:
        """Applied after Split/Move/Remove-from-Cluster/Mark/Un-mark --
        actions that move the given faces OUT of whatever's currently
        shown (a real cluster, or a bucket).

        Real usability fix, direct user feedback: every one of these used
        to call reload(), which re-fetches the WHOLE cluster from the DB
        and resets to page 0 -- fine for a small cluster, but on a large
        one (thousands of faces) this silently backfills the current page
        with faces pulled up from further down the list and bounces the
        view back to page 1 every single time, making it feel like no
        progress is ever being made even though faces genuinely are being
        resolved one action at a time.

        This instead updates the in-memory view directly: the current
        page only ever SHRINKS as faces are resolved off of it (Split them
        out, Move them elsewhere, etc.) -- new content only ever appears
        once the user explicitly turns the page or re-selects the
        cluster, never as an unrequested side effect of taking an action.
        The left list's counts and the Target-person dropdown (whose face
        counts also just changed) are still kept fresh -- see
        _refresh_left_list_after_local_edit/_populate_target_combo -- just
        without touching the right-hand page/scroll state."""
        if not face_ids:
            return
        self._current_faces = [f for f in self._current_faces if f.face_id not in face_ids]
        self._selected_face_ids -= face_ids
        self._remove_faces_from_rendered_page(face_ids)
        if self._current_view == "cluster":
            summary = self._current_summary()
            if summary is not None:
                self._populate_target_combo(exclude_cluster_id=summary.cluster_id)
        self._refresh_left_list_after_local_edit()

    # -------------------------------------------------- safety + undo
    def _confirm_if_selection_spans_pages(self, action_desc: str) -> bool:
        """Returns True if the caller should proceed, False if the user
        backed out of a warning. URGENT follow-up session, directly from a
        real incident: the selection can include face_ids that aren't on
        the CURRENTLY RENDERED page (most commonly because "Select All" --
        whole cluster -- was clicked, but also possible via manually
        checking faces across more than one page without ever clearing the
        selection in between) -- acting on those without a beat to
        reconsider is exactly what silently reassigned 17,764 faces,
        ~17,738 of them never actually looked at, in the incident that
        motivated this. The check is deliberately about what's VISIBLE
        right now, not about which button produced the selection -- that's
        the actual risk, regardless of how the selection came to be."""
        visible = set(self._face_check_vars.keys())
        off_page = self._selected_face_ids - visible
        if not off_page:
            return True
        total = len(self._selected_face_ids)
        visible_selected = total - len(off_page)
        return messagebox.askyesno(
            "Face Labeling — confirm bulk action",
            f"You're about to {action_desc}.\n\n"
            f"That's {total} face(s) total, but only {visible_selected} are on "
            f"the page you're currently looking at — the other {len(off_page)} "
            f"are on other pages, not necessarily reviewed just now.\n\n"
            f'If this isn\'t what you meant, click "No" and use "Select This '
            f'Page" instead of "Select All." This can also be reversed '
            f'afterward with "Undo Last Action" if needed.\n\n'
            f"Continue with all {total} selected face(s)?",
            icon="warning",
        )

    def _push_faces_undo(self, label: str, face_ids: list[int]) -> None:
        """Snapshots the CURRENT (pre-mutation) cluster_id/not_a_person of
        each given face_id and pushes an undo entry — call this
        immediately BEFORE a mutation that reassigns cluster_id/
        not_a_person for these faces (Split/Move/Remove from Cluster/Mark/
        Un-mark). See restore_face_states() in src/face_clustering.py for
        the actual reversal logic."""
        if not face_ids:
            return
        snapshot = snapshot_faces_for_undo(self.conn, face_ids)
        self._undo_stack.append({"kind": "faces", "label": label, "snapshot": snapshot})
        if len(self._undo_stack) > self.MAX_UNDO:
            self._undo_stack.pop(0)
        self._refresh_undo_button()

    def _push_undo(self, entry: dict) -> None:
        """Generic push for non-"faces"-kind undo entries (name/merge) --
        _push_faces_undo above stays the dedicated helper for the common
        case since it also does the snapshot query itself."""
        self._undo_stack.append(entry)
        if len(self._undo_stack) > self.MAX_UNDO:
            self._undo_stack.pop(0)
        self._refresh_undo_button()

    def _refresh_undo_button(self) -> None:
        if self._undo_stack:
            self.undo_btn.configure(state="normal")
            self.undo_label.configure(text=f"Undo available: {self._undo_stack[-1]['label']}")
        else:
            self.undo_btn.configure(state="disabled")
            self.undo_label.configure(text="Nothing to undo yet.")

    def _on_undo(self) -> None:
        """Reverses the single most recent undoable action in this window
        (a plain stack, not a redo-capable history) — see MAX_UNDO's
        comment for why this is session-only, not a persisted log."""
        if not self._undo_stack:
            return
        entry = self._undo_stack.pop()
        kind = entry["kind"]
        if kind == "faces":
            restore_faces_from_undo(self.conn, entry["snapshot"])
        elif kind == "merge":
            undo_merge(self.conn, entry["snapshot"])
        elif kind == "name":
            set_person_name(self.conn, entry["cluster_id"], entry["old_name"], entry["old_notes"])
        self._refresh_undo_button()
        messagebox.showinfo("Face Labeling", f"Undid: {entry['label']}")
        self.reload()

    def _on_save_name(self) -> None:
        if self._current_view == "not_a_person":
            messagebox.showerror(
                "Face Labeling",
                'Can\'t name the "(Marked as not a person)" bucket -- these are flagged false-positive '
                'detections, not a person. Select some and click "Un-mark Selected" if they were flagged '
                'by mistake.',
            )
            return
        summary = self._current_summary()
        if summary is None:
            return
        if summary.cluster_id is None:
            messagebox.showerror(
                "Face Labeling",
                'Can\'t name the "(Unclustered)" group directly -- select some of its faces and use '
                '"Split Selected Into New Person..." to create a real, nameable person first.',
            )
            return
        name = self.name_var.get().strip() or None
        self._push_undo({
            "kind": "name",
            "label": f"Rename \"{summary.assigned_name or '(unnamed)'}\" back",
            "cluster_id": summary.cluster_id,
            "old_name": summary.assigned_name,
            "old_notes": summary.notes,
        })
        set_person_name(self.conn, summary.cluster_id, name, summary.notes)
        # Renaming doesn't move any faces, so the right-hand page is still
        # exactly correct as-is -- only the left list's own label (which
        # shows the name) needs refreshing. A full reload() here would
        # needlessly bounce you back to page 0 if you'd paged into a big
        # cluster before naming it.
        self._refresh_left_list_after_local_edit()

    def _on_split(self) -> None:
        if not self._selected_face_ids:
            messagebox.showinfo("Face Labeling", "Select one or more faces first (click their thumbnails).")
            return
        if self._current_view == "not_a_person":
            messagebox.showerror(
                "Face Labeling",
                'These faces are flagged as not a person. Click "Un-mark Selected" first, then split/move '
                "them normally.",
            )
            return
        if not self._confirm_if_selection_spans_pages("split the selected faces into a new person"):
            return
        from tkinter import simpledialog
        name = simpledialog.askstring(
            "New Person", "Name for the new person (optional -- you can name it later):", parent=self
        )
        # askstring returns None on Cancel -- an empty string (user cleared
        # the field but clicked OK) is a valid "leave unnamed for now".
        if name is None:
            return
        face_ids = sorted(self._selected_face_ids)
        self._push_faces_undo(f"Split {len(face_ids)} face(s) into a new person", face_ids)
        new_cid = create_person_from_faces(self.conn, face_ids, name=(name.strip() or None))
        messagebox.showinfo(
            "Face Labeling", f"Created a new person (cluster {new_cid}) with {len(face_ids)} face(s)."
        )
        self._remove_faces_from_current_view(set(face_ids))

    def _on_remove_from_cluster(self) -> None:
        """Phase 3f: "half of Split" -- sends the selected faces back to
        "(Unclustered)" WITHOUT creating a new person, unlike _on_split
        above. The right tool for a handful of miscellaneous outliers
        wrongly grouped into an otherwise-correct cluster (real example: a
        408-face cluster that was almost entirely one person, with a few
        genuinely misclustered faces mixed in) -- Split would create an
        unnecessary new person for a grab-bag of unrelated wrong matches,
        and selecting the hundreds of CORRECT faces to Move instead would
        be far more clicking than selecting the few wrong ones. Does NOT
        touch not_a_person -- these are still real, valid faces, just
        wrongly grouped (not Phase 3c's false-positive case)."""
        if not self._selected_face_ids:
            messagebox.showinfo("Face Labeling", "Select one or more faces first (click their thumbnails).")
            return
        if self._current_view == "not_a_person":
            messagebox.showerror(
                "Face Labeling",
                'These faces are flagged as not a person. Click "Un-mark Selected" first, then split/move '
                "them normally.",
            )
            return
        summary = self._current_summary()
        if summary is not None and summary.cluster_id is None:
            messagebox.showinfo(
                "Face Labeling", 'These faces are already in "(Unclustered)" -- nothing to remove them from.'
            )
            return
        if not self._confirm_if_selection_spans_pages("remove the selected faces from this cluster"):
            return
        face_ids = sorted(self._selected_face_ids)
        self._push_faces_undo(f'Remove {len(face_ids)} face(s) from this cluster', face_ids)
        remove_faces_from_cluster(self.conn, face_ids)
        messagebox.showinfo(
            "Face Labeling", f'Removed {len(face_ids)} face(s) from this cluster -- sent back to "(Unclustered)".'
        )
        self._remove_faces_from_current_view(set(face_ids))

    def _on_move(self) -> None:
        if not self._selected_face_ids:
            messagebox.showinfo("Face Labeling", "Select one or more faces first (click their thumbnails).")
            return
        if self._current_view == "not_a_person":
            messagebox.showerror(
                "Face Labeling",
                'These faces are flagged as not a person. Click "Un-mark Selected" first, then split/move '
                "them normally.",
            )
            return
        dest_cid = self._resolve_target_cluster_id()
        if dest_cid is None:
            messagebox.showinfo("Face Labeling", "Choose a target person first (type a name or pick from the dropdown).")
            return
        dest_label = self.target_var.get()
        if not self._confirm_if_selection_spans_pages(f'move the selected faces to "{dest_label}"'):
            return
        face_ids = sorted(self._selected_face_ids)
        self._push_faces_undo(f'Move {len(face_ids)} face(s) to "{dest_label}"', face_ids)
        move_faces_to_person(self.conn, face_ids, dest_cid)
        self._remove_faces_from_current_view(set(face_ids))

    def _on_merge(self) -> None:
        if self._current_view == "not_a_person":
            messagebox.showerror(
                "Face Labeling",
                'Can\'t merge the "(Marked as not a person)" bucket -- these aren\'t a person.',
            )
            return
        summary = self._current_summary()
        if summary is None:
            return
        if summary.cluster_id is None:
            messagebox.showerror(
                "Face Labeling",
                'Can\'t merge the "(Unclustered)" group as a whole -- select individual faces and use '
                '"Move Selected Faces Here" instead.',
            )
            return
        target_cid = self._resolve_target_cluster_id()
        if target_cid is None:
            messagebox.showinfo("Face Labeling", "Choose a target person first (type a name or pick from the dropdown).")
            return
        target_label = self.target_var.get()
        if not messagebox.askyesno(
            "Face Labeling",
            f"Merge this whole group ({summary.face_count} face(s)) into the selected person?\n\n"
            "Every face currently in this group moves to the target person, and this group's own "
            'entry is removed. This can be reversed afterward with "Undo Last Action" if needed.',
        ):
            return
        snapshot = snapshot_before_merge(self.conn, keep_cluster_id=target_cid, other_cluster_id=summary.cluster_id)
        self._push_undo({
            "kind": "merge",
            "label": f'Un-merge "{summary.assigned_name or "(unnamed)"}" back out of "{target_label}"',
            "snapshot": snapshot,
        })
        merge_people(self.conn, keep_cluster_id=target_cid, other_cluster_id=summary.cluster_id)
        self.reload()

    def _on_mark_not_a_person(self) -> None:
        """Phase 3c: flags the selected faces as confirmed false-positive
        detections (a pattern/object/pet misdetected as a face) -- reversible
        via _on_unmark_not_a_person below, never a delete. Pulls each face
        out of its current cluster (mark_faces_not_a_person's own job) and
        excludes it from every future `cluster-faces` run."""
        if not self._selected_face_ids:
            messagebox.showinfo("Face Labeling", "Select one or more faces first (click their thumbnails).")
            return
        if self._current_view == "not_a_person":
            messagebox.showinfo("Face Labeling", "These faces are already marked as not a person.")
            return
        if not self._confirm_if_selection_spans_pages("mark the selected faces as not a person"):
            return
        face_ids = sorted(self._selected_face_ids)
        self._push_faces_undo(f"Mark {len(face_ids)} face(s) as not a person", face_ids)
        mark_faces_not_a_person(self.conn, face_ids)
        self._remove_faces_from_current_view(set(face_ids))

    def _on_unmark_not_a_person(self) -> None:
        """Reverses a "Mark as Not a Person" flag. Only meaningful while
        viewing the "(Marked as not a person)" bucket -- guarded rather than
        silently no-op'd elsewhere, since a user clicking this from a normal
        cluster view most likely meant "Mark", not "Un-mark"."""
        if not self._selected_face_ids:
            messagebox.showinfo("Face Labeling", "Select one or more faces first (click their thumbnails).")
            return
        if self._current_view != "not_a_person":
            messagebox.showinfo(
                "Face Labeling",
                'Switch to the "(Marked as not a person)" view (check "Show marked-as-not-a-person" on '
                "the left) to un-mark faces.",
            )
            return
        if not self._confirm_if_selection_spans_pages("un-mark the selected faces"):
            return
        face_ids = sorted(self._selected_face_ids)
        self._push_faces_undo(f"Un-mark {len(face_ids)} face(s)", face_ids)
        unmark_faces_not_a_person(self.conn, face_ids)
        self._remove_faces_from_current_view(set(face_ids))

    def _on_close(self) -> None:
        self._cancel_hover_preview()
        # Ask any in-flight thumbnail-decoding thread to stop, and cancel
        # the recurring _poll_face_thumb_queue after() job -- otherwise it
        # would keep firing against a destroyed window and raise on the
        # next self.after() reschedule inside it.
        self._face_load_stop.set()
        if self._face_poll_after_id is not None:
            self.after_cancel(self._face_poll_after_id)
            self._face_poll_after_id = None
        # Explicit, even though self.destroy() below would cascade to this
        # child Toplevel anyway -- cheap and removes any doubt.
        if self._target_suggest_popup is not None and self._target_suggest_popup.winfo_exists():
            self._target_suggest_popup.destroy()
        self.conn.close()
        self.destroy()


def main() -> None:
    root = tk.Tk()
    DashboardApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
