"""Interactive folder picker for source_folders. Run via:
    venv\\Scripts\\python main.py pick-sources

Opens the native Windows folder picker with multi-select
(FOS_PICKFOLDERS | FOS_ALLOWMULTISELECT via IFileOpenDialog — see
folder_picker.py) so the user can ctrl/shift-click several sibling folders
in one dialog. After each batch it asks whether to pick another batch (e.g.
from a different drive). Cancelling the dialog (or answering "no") ends the
picker and writes the merged result to config.yaml.

If the native multi-select dialog can't be used for any reason (missing
comtypes, COM setup failure, etc.), this falls back to the old tkinter
single-folder-at-a-time loop rather than leaving the user stuck.

Additive across runs: existing source_folders in config.yaml are kept and
newly picked folders are appended (exact-duplicate paths skipped), never
overwritten wholesale. The user expects to re-run this as more old-photo
locations are found, and eventually as an ongoing "dump new photos here"
workflow — see TODO.md.

Reopen behavior: each dialog defaults to the PARENT of the last folder
picked (across this run, or from config.yaml's last entry on a fresh run),
never into the folder just added — avoids forcing the user to navigate back
up and rescroll every round.
"""
from __future__ import annotations

from pathlib import Path

from .config import CONFIG_PATH, load_config, save_source_folders


def _initial_dir_from(folders: list[str]) -> str | None:
    """Parent of the last folder in `folders`, if any — never the folder
    itself, so reopening the dialog doesn't drill back into it."""
    if not folders:
        return None
    return str(Path(folders[-1]).parent)


def _pick_native(existing: list[str]) -> list[str]:
    from .folder_picker import FolderPickerCancelled, FolderPickerUnavailable, pick_folders_native

    picked: list[str] = []
    initial_dir = _initial_dir_from(existing)
    round_num = 1
    while True:
        title = "Select photo source folder(s) — ctrl/shift-click to select multiple, Cancel when done"
        try:
            batch = pick_folders_native(initial_dir, title)
        except FolderPickerCancelled:
            batch = []
        # FolderPickerUnavailable propagates — caller falls back to tkinter for the whole picker

        if not batch:
            break

        print(f"\nBatch {round_num}: added {len(batch)} folder(s):")
        for f in batch:
            print(f"  - {f}")
        picked.extend(batch)
        initial_dir = _initial_dir_from(batch)  # next dialog opens at the parent of the last pick, not inside it

        again = _ask_yes_no(f"{len(picked)} folder(s) selected so far. Pick another batch?")
        if not again:
            break
        round_num += 1

    return picked


def _ask_yes_no(question: str) -> bool:
    # Deliberately does NOT create its own tk.Tk() root: when called from
    # the CLI (no root exists yet), tkinter's dialog helpers transparently
    # create a hidden default root on first use. When called from inside
    # dashboard.py (a root + mainloop already exist), creating a second,
    # independent Tk() interpreter here would fight the running one — this
    # reuses the existing default root instead.
    from tkinter import messagebox

    return messagebox.askyesno("Photo Organizer", question)


def _pick_tkinter_fallback(existing: list[str]) -> list[str]:
    """Old single-folder-at-a-time loop — used only if the native
    multi-select dialog is unavailable on this machine. See _ask_yes_no
    above for why no tk.Tk() root is created here."""
    from tkinter import filedialog, messagebox

    picked: list[str] = []
    initial_dir = _initial_dir_from(existing)
    while True:
        folder = filedialog.askdirectory(
            title="Select a photo source folder (Cancel when done)",
            mustexist=True,
            initialdir=initial_dir,
        )
        if not folder:
            break
        picked.append(str(Path(folder)))
        initial_dir = _initial_dir_from(picked)  # next open defaults to the parent, not the folder just picked
        again = messagebox.askyesno(
            "Photo Organizer",
            f"Added:\n{folder}\n\n({len(picked)} folder(s) so far)\n\nAdd another folder?",
        )
        if not again:
            break

    return picked


def merge_and_save(picked: list[str], path=None) -> tuple[list[str], int]:
    """Merges newly picked folders into config.yaml's existing
    source_folders (additive, de-duped, order-preserving) and saves.
    Returns (merged_list, number_actually_added). Shared by the CLI
    (`main()` below) and the dashboard's source-folder panel."""
    cfg = load_config(path)
    merged = list(dict.fromkeys(cfg.source_folders + picked))
    save_source_folders(merged, path or CONFIG_PATH)
    added = len(merged) - len(cfg.source_folders)
    return merged, added


def pick_sources_interactive() -> list[str]:
    existing = []
    if CONFIG_PATH.exists():
        try:
            existing = load_config().source_folders
        except SystemExit:
            existing = []

    try:
        return _pick_native(existing)
    except Exception as e:
        print(f"Native multi-select folder picker unavailable ({e}); "
              f"falling back to the single-folder picker.")
        return _pick_tkinter_fallback(existing)


def main() -> None:
    print("Opening folder picker... (switch to the dialog window if it doesn't come to front)")
    try:
        picked = pick_sources_interactive()
    except Exception as e:
        print(f"Could not open the folder picker (no display / tkinter unavailable?): {e}")
        print("Edit source_folders in config.yaml by hand instead.")
        return

    if not picked:
        print("No folders selected. config.yaml unchanged.")
        return

    print("\nSelected source folders (this run):")
    for f in picked:
        print(f"  - {f}")

    if not CONFIG_PATH.exists():
        print(f"\n{CONFIG_PATH.name} doesn't exist yet — copy config.example.yaml to config.yaml first, "
              f"then re-run pick-sources.")
        return

    merged, added = merge_and_save(picked)
    print(f"\nWrote {len(merged)} source folder(s) to {CONFIG_PATH} ({added} new, "
          f"{len(picked) - added} already present and skipped).")


if __name__ == "__main__":
    main()
