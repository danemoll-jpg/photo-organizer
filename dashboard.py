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
       a manual "Refresh" button; dashboard.py has no code path that
       launches review_tool.py itself, so this is detection/warning only,
       not prevention (see TODO.md's follow-up note on why).
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

Every panel is collapsible — a ▼/▶ toggle in its header hides/shows its
body (see _make_collapsible()), so a section you're not using right now
(e.g. Source folders, once already configured, or Phase 1's Run panel
while doing Phase 2 work) can be tucked away to make room for whatever
you actually want to look at, especially the Log viewer at the bottom
(which expands to fill whatever vertical space is free).

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
from src.db import connect, init_db
from src.gps_backfill import GpsStats, run_gps_extraction
from src.logging_setup import setup_logging
from src.organize import RunStats, run_phase1
from src.pick_sources import merge_and_save, pick_sources_interactive
from src.port_check import listening_pids


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

        # Remote Access panel (Phase 2d follow-up): the cloudflared
        # subprocess this dashboard itself spawned, if any. None whenever
        # no tunnel was started from here (including: never started, or
        # already stopped/exited) — see _tunnel_running().
        self.tunnel_proc: subprocess.Popen | None = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._load_config_or_onboard)
        self.root.after(200, self._poll_queue)
        self.root.after(500, self._tail_tick)
        self.root.after(300, self._port_status_tick)

    # ------------------------------------------------------------------ UI
    def _make_collapsible(self, title: str, fill: str = "x", expand: bool = False,
                           start_expanded: bool = True) -> ttk.Frame:
        """Builds a section with a header (toggle triangle + title) and a
        body frame the caller packs its real widgets into; returns the
        body. Collapsing just pack_forget()s the body -- there's no
        scrollable container to fight with, so other sections (especially
        the Log viewer, which expands to fill whatever's left) simply
        reclaim the freed vertical space through normal pack layout, no
        extra bookkeeping needed."""
        outer = ttk.Frame(self.root, relief="groove", borderwidth=1)
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
                    elif kind == "tunnel_output":
                        _, proc, line = msg
                        self._append_tunnel_log(proc, line)
                    elif kind == "tunnel_exited":
                        _, proc, returncode = msg
                        self._on_tunnel_exited(proc, returncode)
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


def main() -> None:
    root = tk.Tk()
    DashboardApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
