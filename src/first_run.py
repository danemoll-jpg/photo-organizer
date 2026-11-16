"""First-run setup for the packaged app.

config.example.yaml's dest_root is this project's author's own drive
(E:\\Pics). That is a trap for anyone else, so the packaged app creates its
config.yaml itself with a destination under the user's own Pictures folder.
dry_run stays true (the example's safe default): nothing is moved until the
user turns it off on purpose.

Only acts when running packaged; running from source keeps the manual
"copy config.example.yaml to config.yaml" workflow untouched.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import CONFIG_PATH, EXAMPLE_CONFIG_PATH
from .paths import FROZEN

_DEST_LINE = re.compile(r'^dest_root:.*$', re.MULTILINE)


def default_dest_root() -> str:
    return str(Path.home() / "Pictures" / "Photo Organizer")


def make_config_text(example_text: str, dest_root: str) -> str:
    # YAML single quotes: backslashes in a Windows path need no escaping.
    quoted = "'" + dest_root.replace("'", "''") + "'"
    out, n = _DEST_LINE.subn(lambda _m: f"dest_root: {quoted}", example_text, count=1)
    if n != 1:
        raise RuntimeError("config.example.yaml has no dest_root line to replace")
    return out


def ensure_config(config_path: Path = CONFIG_PATH, example_path: Path = EXAMPLE_CONFIG_PATH, force: bool = FROZEN) -> bool:
    """Creates config.yaml if missing. Returns True if it created one."""
    if not force or config_path.exists():
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = make_config_text(example_path.read_text(encoding="utf-8"), default_dest_root())
    config_path.write_text(text, encoding="utf-8")
    return True
