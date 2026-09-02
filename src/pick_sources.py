"""Interactive folder picker for source_folders. Run via:
    venv\\Scripts\\python main.py pick-sources

Opens the native Windows "choose folder" dialog repeatedly (it only allows
one folder per pick) and asks after each pick whether to add another.
Cancelling the dialog (or answering "no") ends the picker and writes the
result to config.yaml.
"""
from __future__ import annotations

from pathlib import Path

from .config import CONFIG_PATH, load_config, save_source_folders


def pick_sources_interactive() -> list[str]:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()

    picked: list[str] = []
    while True:
        folder = filedialog.askdirectory(
            title="Select a photo source folder (Cancel when done)",
            mustexist=True,
        )
        if not folder:
            break
        picked.append(str(Path(folder)))
        again = messagebox.askyesno(
            "Photo Organizer",
            f"Added:\n{folder}\n\n({len(picked)} folder(s) so far)\n\nAdd another folder?",
        )
        if not again:
            break

    root.destroy()
    return picked


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

    print("\nSelected source folders:")
    for f in picked:
        print(f"  - {f}")

    if not CONFIG_PATH.exists():
        print(f"\n{CONFIG_PATH.name} doesn't exist yet — copy config.example.yaml to config.yaml first, "
              f"then re-run pick-sources.")
        return

    # Merge with any existing source_folders rather than clobbering them.
    cfg = load_config()
    merged = list(dict.fromkeys(cfg.source_folders + picked))  # de-dup, preserve order
    save_source_folders(merged, CONFIG_PATH)
    print(f"\nWrote {len(merged)} source folder(s) to {CONFIG_PATH}.")


if __name__ == "__main__":
    main()
