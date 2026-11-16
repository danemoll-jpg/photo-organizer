"""The dashboard's "Updates" panel (CLAUDE.md rule 10: every user-facing
capability lives in the dashboard, not just a CLI).

A thin UI over src/updater.py -- no update logic lives here. Kept out of
dashboard.py (already 5k+ lines) as a self-contained class with its own
worker thread + queue, so the dashboard only has to build it.

Behaviour:
  * Optionally checks for an update shortly after the dashboard opens
    (config.yaml `check_for_updates`, default true). A failed check is
    quiet -- an offline PC must not nag.
  * Installing is always the user's click, never automatic: photo runs can
    take hours and an update must not interrupt one. The new version takes
    effect on the next launch ("Restart Now" does that when nothing is
    running; otherwise it warns first).
  * Running from source (no install folder): checking works, installing is
    disabled with an explanation -- source users update with git.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from . import updater
from .paths import DATA_ROOT, FROZEN, INSTALL_ROOT
from .version import RELEASES_REPO, __version__


class UpdatesPanel:
    def __init__(
        self,
        parent: ttk.Frame,
        root: tk.Misc,
        busy_reason: Callable[[], str | None],
        on_quit: Callable[[], None],
        auto_check: Callable[[], bool] = lambda: True,
    ) -> None:
        self.root = root
        self.busy_reason = busy_reason
        self.on_quit = on_quit
        self.auto_check = auto_check
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop = False
        self.info: updater.UpdateInfo | None = None
        self.installed_new = False

        style = ttk.Style()
        style.configure("Warn.TLabel", foreground="#b30000")
        style.configure("Ok.TLabel", foreground="#1a7f37")
        style.configure("Down.TLabel", foreground="#b35c00")

        row = ttk.Frame(parent)
        row.pack(fill="x", padx=6, pady=(6, 0))
        kind = "packaged app" if FROZEN else "running from source"
        ttk.Label(row, text=f"Installed version: {__version__}  ({kind})", font=("Segoe UI", 9, "bold")).pack(side="left")

        self.status = ttk.Label(parent, text="Not checked yet.", wraplength=740, justify="left")
        self.status.pack(fill="x", padx=6, pady=(4, 0))

        btns = ttk.Frame(parent)
        btns.pack(fill="x", padx=6, pady=6)
        self.check_btn = ttk.Button(btns, text="Check for Updates", command=self.on_check)
        self.check_btn.pack(side="left")
        self.install_btn = ttk.Button(btns, text="Download && Install", command=self.on_install, state="disabled")
        self.install_btn.pack(side="left", padx=(6, 0))
        self.restore_btn = ttk.Button(btns, text="Restore Previous Version", command=self.on_restore, state="disabled")
        self.restore_btn.pack(side="left", padx=(6, 0))
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        self.restart_btn = ttk.Button(btns, text="Restart Now", command=self.on_restart, state="disabled")
        self.restart_btn.pack(side="right")
        ttk.Button(btns, text="Open Data Folder", command=self.on_open_data).pack(side="right", padx=(0, 6))

        self.progress = ttk.Progressbar(parent, mode="determinate")
        self.progress.pack(fill="x", padx=6)

        ttk.Label(parent, text="What's new:").pack(anchor="w", padx=6, pady=(6, 0))
        self.notes = tk.Text(parent, height=5, wrap="word", state="disabled")
        self.notes.pack(fill="x", padx=6, pady=(0, 4))

        data_note = (
            f"Your settings and library data live in {DATA_ROOT} (outside the install folder, "
            "so updates never touch them)."
            if FROZEN
            else "Running from source: update with git; the installer-style update applies to the packaged app."
        )
        ttk.Label(parent, text=data_note, wraplength=740, justify="left", foreground="#555555").pack(
            anchor="w", padx=6, pady=(0, 6)
        )

        self._refresh_restore_button()
        self.root.after(200, self._poll)
        self.root.after(2500, self._maybe_auto_check)

    # ---------------------------------------------------------------- helpers
    def _set_status(self, text: str, style: str = "TLabel") -> None:
        self.status.configure(text=text, style=style)

    def _set_notes(self, text: str) -> None:
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", text)
        self.notes.configure(state="disabled")

    def _refresh_restore_button(self) -> None:
        prev = updater.installed_summary(INSTALL_ROOT)["previous"] if INSTALL_ROOT else None
        self.restore_btn.configure(state="normal" if prev and not self._busy() else "disabled")

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _run(self, fn: Callable[[], None]) -> None:
        self.worker = threading.Thread(target=fn, daemon=True)
        self.worker.start()

    # ---------------------------------------------------------------- check
    def _maybe_auto_check(self) -> None:
        try:
            wanted = self.auto_check()
        except Exception:
            wanted = True
        if wanted:
            self._start_check(silent=True)

    def on_check(self) -> None:
        self._start_check(silent=False)

    def _start_check(self, silent: bool) -> None:
        if self._busy():
            return
        self.check_btn.configure(state="disabled")
        if not silent:
            self._set_status("Checking for updates...")

        def work() -> None:
            try:
                info = updater.check_for_update(__version__, RELEASES_REPO)
                self.q.put(("checked", info, silent))
            except updater.UpdateError as e:
                self.q.put(("check_failed", str(e), silent))

        self._run(work)

    # -------------------------------------------------------------- install
    def on_install(self) -> None:
        if not self.info or INSTALL_ROOT is None:
            return
        reason = self.busy_reason()
        extra = f"\n\nNote: {reason} is still running; it keeps running until you restart." if reason else ""
        if not messagebox.askyesno(
            "Install update",
            f"Download and install version {self.info.version} "
            f"({self.info.asset_bytes / 2**20:.0f} MB)?\n\n"
            "Your photos, database and settings are not touched. The current version is kept so you can go back."
            + extra,
            parent=self.root,
        ):
            return
        self.stop = False
        self.install_btn.configure(state="disabled")
        self.check_btn.configure(state="disabled")
        self.restore_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self._set_status(f"Downloading {self.info.version}...")
        info = self.info

        def work() -> None:
            try:
                updater.download_and_install(
                    info,
                    RELEASES_REPO,
                    INSTALL_ROOT,
                    progress=lambda done, total: self.q.put(("progress", done, total)),
                    should_stop=lambda: self.stop,
                )
                self.q.put(("installed", info.version))
            except updater.UpdateError as e:
                self.q.put(("install_failed", str(e)))

        self._run(work)

    def on_cancel(self) -> None:
        self.stop = True
        self.cancel_btn.configure(state="disabled")

    def on_restore(self) -> None:
        if INSTALL_ROOT is None:
            return
        summary = updater.installed_summary(INSTALL_ROOT)
        if not summary["previous"]:
            return
        if not messagebox.askyesno(
            "Restore previous version",
            f"Switch back to version {summary['previous']}? It takes effect on the next launch. "
            "Your photos and settings are not touched.",
            parent=self.root,
        ):
            return
        try:
            now = updater.restore_previous(INSTALL_ROOT)
        except updater.UpdateError as e:
            messagebox.showerror("Restore previous version", str(e), parent=self.root)
            return
        self._set_status(f"Version {now} will start next time Photo Organizer is launched.", "Ok.TLabel")
        self.installed_new = True
        self.restart_btn.configure(state="normal")
        self._refresh_restore_button()

    # -------------------------------------------------------------- restart
    def on_restart(self) -> None:
        launcher = (INSTALL_ROOT / updater.LAUNCHER_EXE) if INSTALL_ROOT else None
        if launcher is None or not launcher.is_file():
            messagebox.showinfo(
                "Restart", "Close Photo Organizer and open it again to use the new version.", parent=self.root
            )
            return
        reason = self.busy_reason()
        if reason and not messagebox.askyesno(
            "Restart now?", f"{reason} is still running and would be stopped. Restart anyway?", parent=self.root
        ):
            return
        env = dict(os.environ, PHOTO_ORGANIZER_INSTALL_ROOT=str(INSTALL_ROOT))
        subprocess.Popen(
            [str(launcher)],
            cwd=str(INSTALL_ROOT),
            env=env,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self.on_quit()

    def on_open_data(self) -> None:
        try:
            DATA_ROOT.mkdir(parents=True, exist_ok=True)
            os.startfile(str(DATA_ROOT))  # type: ignore[attr-defined]  # Windows-only, like the rest of the app
        except OSError as e:
            messagebox.showerror("Open Data Folder", str(e), parent=self.root)

    # ----------------------------------------------------------------- poll
    def _poll(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                try:
                    self._handle(msg)
                except Exception as e:  # one bad message must not kill the poll loop
                    self._set_status(f"Update panel error: {e}", "Warn.TLabel")
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "progress":
            _, done, total = msg
            self.progress.configure(maximum=max(total, 1), value=done)
            self._set_status(f"Downloading {self.info.version if self.info else ''}...  {done / 2**20:.0f} / {total / 2**20:.0f} MB")
        elif kind == "checked":
            _, info, silent = msg
            self.check_btn.configure(state="normal")
            self.info = info
            if info is None:
                self._set_status(f"You're up to date ({__version__}).", "Ok.TLabel")
                self._set_notes("")
                self.install_btn.configure(state="disabled")
                return
            self._set_notes(info.notes)
            if INSTALL_ROOT is None:
                self._set_status(
                    f"Version {info.version} is available. You're running from source, so update with git instead.",
                    "Down.TLabel",
                )
                self.install_btn.configure(state="disabled")
            else:
                self._set_status(f"Version {info.version} is available (you have {__version__}).", "Down.TLabel")
                self.install_btn.configure(state="normal")
        elif kind == "check_failed":
            _, err, silent = msg
            self.check_btn.configure(state="normal")
            if not silent:
                self._set_status(err, "Warn.TLabel")
            else:
                self._set_status("Couldn't check for updates (offline?). Use Check for Updates to retry.")
        elif kind == "installed":
            _, version = msg
            self.cancel_btn.configure(state="disabled")
            self.check_btn.configure(state="normal")
            self.progress.configure(value=0)
            self.installed_new = True
            self.info = None
            self._set_status(
                f"Version {version} is installed. It starts the next time you open Photo Organizer; "
                "use Restart Now when no job is running.",
                "Ok.TLabel",
            )
            self.restart_btn.configure(state="normal")
            self._refresh_restore_button()
        elif kind == "install_failed":
            _, err = msg
            self.cancel_btn.configure(state="disabled")
            self.check_btn.configure(state="normal")
            self.progress.configure(value=0)
            self.install_btn.configure(state="normal" if self.info else "disabled")
            self._set_status(f"{err}  The installed version was not changed.", "Warn.TLabel")
            self._refresh_restore_button()
