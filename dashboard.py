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
- Log viewer — tails the active Phase 1 run's log, or browse any older
  logs/*.log (including old Phase 2 runs, if you want to review one)

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
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import messagebox, ttk

from src.caption import CaptionStats, run_phase2
from src.config import CONFIG_PATH, EXAMPLE_CONFIG_PATH, Config, load_config, save_source_folders
from src.db import connect, init_db
from src.logging_setup import setup_logging
from src.organize import RunStats, run_phase1
from src.pick_sources import merge_and_save, pick_sources_interactive


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

        self._build_ui()
        self.root.after(50, self._load_config_or_onboard)
        self.root.after(200, self._poll_queue)
        self.root.after(500, self._tail_tick)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 6}

        # --- Source folders panel ---
        src_frame = ttk.LabelFrame(self.root, text="Source folders")
        src_frame.pack(fill="x", **pad)

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
        act_frame = ttk.LabelFrame(self.root, text="Run")
        act_frame.pack(fill="x", **pad)

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
        cap_frame = ttk.LabelFrame(self.root, text="Phase 2 — Captioning (local Ollama vision model)")
        cap_frame.pack(fill="x", **pad)

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

        # --- Log viewer panel ---
        log_frame = ttk.LabelFrame(self.root, text="Log viewer")
        log_frame.pack(fill="both", expand=True, **pad)

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
        self._refresh_folder_list()

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
            logger, log_path = setup_logging(cfg.log_dir_abs, echo_to_console=False)
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
        self._set_caption_results_text("")
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
            logger, log_path = setup_logging(cfg.log_dir_abs, echo_to_console=False)
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
        self.root.after(500, self._tail_tick)

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
