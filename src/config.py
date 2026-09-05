"""Load and validate config.yaml. No hardcoded paths — everything the tool
needs to know about this machine's layout lives in config.yaml (gitignored).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config.example.yaml"


@dataclass
class Config:
    source_folders: list[str]
    dest_root: str
    supported_extensions: list[str]
    unsorted_subfolder: str
    dry_run: bool
    hash_algorithm: str
    collision_suffix_length: int
    db_path: str
    log_dir: str
    captioning_backend: str = "local"
    gpu_enabled: bool = True
    batch_size: int = 500
    # Phase 1b: video formats, scanned and sorted alongside photos but with
    # their own date-resolution chain and destination subfolder (see organize.py).
    video_extensions: list[str] = field(default_factory=lambda: [".mp4", ".mov", ".avi"])

    # --- derived, absolute paths ---
    @property
    def dest_root_path(self) -> Path:
        return Path(self.dest_root)

    @property
    def unsorted_path(self) -> Path:
        return self.dest_root_path / self.unsorted_subfolder

    @property
    def db_path_abs(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def log_dir_abs(self) -> Path:
        p = Path(self.log_dir)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def source_folder_paths(self) -> list[Path]:
        return [Path(s) for s in self.source_folders]

    @property
    def extensions_normalized(self) -> set[str]:
        return {e.lower() if e.startswith(".") else f".{e.lower()}" for e in self.supported_extensions}

    @property
    def video_extensions_normalized(self) -> set[str]:
        return {e.lower() if e.startswith(".") else f".{e.lower()}" for e in self.video_extensions}

    @property
    def all_extensions_normalized(self) -> set[str]:
        """Photo + video extensions combined — what the scanner filters on.
        Media-type dispatch (which date resolver, which dest subfolder) happens
        per-file in organize.py via video_extensions_normalized, not here."""
        return self.extensions_normalized | self.video_extensions_normalized


def load_config(path: Path | None = None) -> Config:
    cfg_path = path or CONFIG_PATH
    if not cfg_path.exists():
        print(f"ERROR: {cfg_path} not found.", file=sys.stderr)
        print(f"Copy {EXAMPLE_CONFIG_PATH.name} to {cfg_path.name} and edit it first:", file=sys.stderr)
        print(f"  copy {EXAMPLE_CONFIG_PATH.name} {cfg_path.name}", file=sys.stderr)
        sys.exit(1)

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    required = ["dest_root", "supported_extensions", "unsorted_subfolder", "db_path", "log_dir"]
    missing = [k for k in required if k not in raw]
    if missing:
        print(f"ERROR: {cfg_path} is missing required keys: {missing}", file=sys.stderr)
        sys.exit(1)

    return Config(
        source_folders=raw.get("source_folders", []) or [],
        dest_root=raw["dest_root"],
        supported_extensions=raw["supported_extensions"],
        unsorted_subfolder=raw["unsorted_subfolder"],
        dry_run=raw.get("dry_run", True),
        hash_algorithm=raw.get("hash_algorithm", "sha256"),
        collision_suffix_length=raw.get("collision_suffix_length", 8),
        db_path=raw["db_path"],
        log_dir=raw["log_dir"],
        captioning_backend=raw.get("captioning_backend", "local"),
        gpu_enabled=raw.get("gpu_enabled", True),
        batch_size=raw.get("batch_size", 500),
        video_extensions=raw.get("video_extensions", [".mp4", ".mov", ".avi"]),
    )


def save_source_folders(folders: list[str], path: Path | None = None) -> None:
    """Update just source_folders in config.yaml, preserving everything else
    and comments as best-effort (full rewrite of the mapping — YAML comments
    in the user's config.yaml will NOT be preserved by this round-trip)."""
    cfg_path = path or CONFIG_PATH
    if not cfg_path.exists():
        print(f"ERROR: {cfg_path} not found. Copy config.example.yaml to config.yaml first.", file=sys.stderr)
        sys.exit(1)
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw["source_folders"] = folders
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
