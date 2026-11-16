"""Entry point for the packaged (PyInstaller) Photo Organizer.

One executable, several jobs -- the packaged app has no python.exe or
review_tool.py on disk to spawn, so it re-launches ITSELF:

    PhotoOrganizerApp.exe                  the dashboard (normal launch)
    PhotoOrganizerApp.exe --review-tool …  the review web app (what the
                                           dashboard's restart button starts)
    PhotoOrganizerApp.exe --cli …          main.py's commands
    PhotoOrganizerApp.exe --selftest PATH  headless smoke check -> JSON file

Users normally start PhotoOrganizer.exe (the launcher, launcher/launcher.py),
which picks the current version out of state.json and runs this.

Not used when running from source (python dashboard.py etc. as before).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _selftest(out_path: str) -> int:
    """Imports everything a normal run needs and checks the bundled resources
    are where the code looks for them. Windowed builds have no console, so the
    result goes to a file."""
    result: dict = {"ok": False, "problems": []}
    try:
        from src import paths, updater, version
        from src.config import EXAMPLE_CONFIG_PATH
        from src.db import SCHEMA_PATH

        result["version"] = version.__version__
        result["frozen"] = paths.FROZEN
        result["app_root"] = str(paths.APP_ROOT)
        result["data_root"] = str(paths.DATA_ROOT)
        for label, p in {
            "schema.sql": SCHEMA_PATH,
            "config.example.yaml": EXAMPLE_CONFIG_PATH,
            "templates/review.html": paths.APP_ROOT / "templates" / "review.html",
            "static/review.js": paths.APP_ROOT / "static" / "review.js",
        }.items():
            if not p.is_file():
                result["problems"].append(f"missing bundled file: {label} ({p})")
        import dashboard  # noqa: F401  (every panel module imports at top level)
        import main as _cli  # noqa: F401
        import review_tool

        # The review tool's routes need the config/auth wiring review_tool.main()
        # does at startup, so check what packaging can actually break: that
        # Flask finds and can parse the bundled templates and static files.
        for tpl in ("login.html", "review.html"):
            try:
                review_tool.app.jinja_env.get_template(tpl)
            except Exception as e:
                result["problems"].append(f"review tool template {tpl} didn't load: {type(e).__name__}: {e}")
        static = Path(review_tool.app.static_folder)
        for name in ("review.js", "review.css"):
            if not (static / name).is_file():
                result["problems"].append(f"review tool static file missing: {name} (looked in {static})")
        result["updater_ok"] = callable(updater.check_for_update)
    except Exception as e:  # report, don't crash silently
        import traceback

        result["problems"].append(f"{type(e).__name__}: {e}")
        result["traceback"] = traceback.format_exc()
    result["ok"] = not result["problems"]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    return 0 if result["ok"] else 1


def main() -> None:
    argv = sys.argv[1:]
    if argv[:1] == ["--selftest"] and len(argv) == 2:
        sys.exit(_selftest(argv[1]))
    if argv[:1] == ["--review-tool"]:
        sys.argv = [sys.argv[0]] + argv[1:]
        import review_tool

        review_tool.main()
        return
    if argv[:1] == ["--cli"]:
        sys.argv = [sys.argv[0]] + argv[1:]
        import main as cli

        cli.main()
        return

    from src.first_run import ensure_config

    ensure_config()
    import dashboard

    dashboard.main()


if __name__ == "__main__":
    main()
