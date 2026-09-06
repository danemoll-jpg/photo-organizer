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
    # Phase 2: local vision-model captioning via Ollama. Model name is
    # config-driven (rule 4) specifically so switching qwen3-vl:2b <->
    # minicpm-v4.6 (or anything else pulled locally) never needs a code
    # change — see src/caption.py and CLAUDE.md's Phase 2 notes.
    ollama_model: str = "qwen3-vl:2b"
    ollama_host: str = "http://localhost:11434"
    captions_path: str = "data/captions.jsonl"
    caption_max_dimension: int = 1024
    # Phase 1b: video formats, scanned and sorted alongside photos but with
    # their own date-resolution chain and destination subfolder (see organize.py).
    video_extensions: list[str] = field(default_factory=lambda: [".mp4", ".mov", ".avi"])
    # Phase 2b: standalone review/spot-check tool (review_tool.py). Not part
    # of the dashboard — see CLAUDE.md. All three are startup/UI defaults,
    # not correctness-affecting, so they get their own defaults here rather
    # than requiring the user's real config.yaml to be edited for this new
    # tool to work out of the box (this session was told not to touch
    # config.yaml — see TODO.md). review_slideshow_seconds is also always
    # user-changeable live in the tool's UI, per spec ("must be fully
    # configurable, not hardcoded") — this is just the initial value.
    review_tool_port: int = 5151
    review_page_size: int = 40
    review_slideshow_seconds: float = 5.0

    # --- Phase 2d: remote/shared access (review_tool.py auth + storage) ---
    # One username+password pair per invited person — NOT a single shared
    # password, NOT a full account-management system (see CLAUDE.md/TODO.md).
    # password_hash is a werkzeug scrypt hash (src/auth.py::hash_password),
    # never a plaintext password. Managed via `main.py review-user
    # add/list/remove` rather than hand-editing this list, though hand-
    # editing is harmless if ever needed (it's just YAML).
    review_users: list[dict] = field(default_factory=list)
    # Session cookie's Secure flag. Default False so the tool still works
    # when accessed directly over plain http://127.0.0.1 on the user's own
    # PC (a browser refuses to send a Secure cookie back over http, which
    # would otherwise silently break local login). The real HTTPS
    # protection for remote access comes from the Cloudflare Tunnel
    # terminating TLS at Cloudflare's edge (see photo-organizer-spec.md's
    # Phase 2d) — the cookie is only ever on the wire, in plaintext,
    # between the browser and Cloudflare's edge either way (that hop is
    # HTTPS regardless of this flag; cloudflared<->this app is loopback-
    # only, never leaves the machine). Set true if this tool will only
    # ever be opened via its https:// tunnel hostname, never directly.
    session_cookie_secure: bool = False
    # How long a login stays valid (browser session cookie persists this
    # long, not just until the browser closes) -- "session persistence"
    # per spec, not a re-login-every-visit experience.
    session_lifetime_days: int = 30
    # Login brute-force rate limiting (src/auth.py::LoginRateLimiter): this
    # many failed attempts within this many seconds (per client IP AND
    # per attempted username, whichever is stricter) locks out further
    # attempts against that key until the window slides past the oldest
    # failure. See src/auth.py's module docstring for the full reasoning.
    login_rate_limit_attempts: int = 5
    login_rate_limit_window_seconds: int = 900
    # Phase 2d storage-abstraction groundwork (src/storage.py): "local" is
    # the only implemented backend. Exists so a future cloud-storage swap
    # (S3/B2/etc.) is a new backend class + this one config value, not a
    # rewrite of review_tool.py — see photo-organizer-spec.md's Phase 2d.
    storage_backend: str = "local"

    # Phase 2d follow-up: dashboard.py's Remote Access panel runs
    # `cloudflared tunnel run <this>` for its Start Tunnel button. The
    # tunnel's actual name is specific to whichever Cloudflare account/
    # machine set it up (`cloudflared tunnel create <name>` — see
    # README.md's "Remote access" section), so this is config-driven per
    # rule 4 rather than hardcoded in dashboard.py. Default matches this
    # user's own already-confirmed-working tunnel (see CLAUDE.md).
    cloudflare_tunnel_name: str = "photo-viewer"

    # Phase 2g: cached video first-frame thumbnails (src/thumbnail_backfill.py,
    # served by review_tool.py's /thumbnail/<file_hash> route). One JPEG per
    # video, named <file_hash>.jpg -- disk existence IS the resumability
    # checkpoint (no DB column), per the module's own docstring. Dimension is
    # config-driven (rule 4) rather than hardcoded, same reasoning as
    # caption_max_dimension -- 480px longest edge is plenty for a grid tile
    # (photos' own grid thumbs are requested at ?max=400) with a little
    # headroom, at a fraction of the file size a full-res frame would cost
    # times 12k+ videos.
    thumbnail_dir: str = "data/thumbnails"
    thumbnail_max_dimension: int = 480

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
    def captions_path_abs(self) -> Path:
        p = Path(self.captions_path)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def log_dir_abs(self) -> Path:
        p = Path(self.log_dir)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def thumbnail_dir_abs(self) -> Path:
        p = Path(self.thumbnail_dir)
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
        review_tool_port=raw.get("review_tool_port", 5151),
        review_page_size=raw.get("review_page_size", 40),
        review_slideshow_seconds=raw.get("review_slideshow_seconds", 5.0),
        ollama_model=raw.get("ollama_model", "qwen3-vl:2b"),
        ollama_host=raw.get("ollama_host", "http://localhost:11434"),
        captions_path=raw.get("captions_path", "data/captions.jsonl"),
        caption_max_dimension=raw.get("caption_max_dimension", 1024),
        review_users=raw.get("review_users", []) or [],
        session_cookie_secure=raw.get("session_cookie_secure", False),
        session_lifetime_days=raw.get("session_lifetime_days", 30),
        login_rate_limit_attempts=raw.get("login_rate_limit_attempts", 5),
        login_rate_limit_window_seconds=raw.get("login_rate_limit_window_seconds", 900),
        storage_backend=raw.get("storage_backend", "local"),
        cloudflare_tunnel_name=raw.get("cloudflare_tunnel_name", "photo-viewer"),
        thumbnail_dir=raw.get("thumbnail_dir", "data/thumbnails"),
        thumbnail_max_dimension=raw.get("thumbnail_max_dimension", 480),
    )


def _load_raw(path: Path | None = None) -> dict:
    cfg_path = path or CONFIG_PATH
    if not cfg_path.exists():
        print(f"ERROR: {cfg_path} not found. Copy config.example.yaml to config.yaml first.", file=sys.stderr)
        sys.exit(1)
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_raw(raw: dict, path: Path | None = None) -> None:
    cfg_path = path or CONFIG_PATH
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def save_source_folders(folders: list[str], path: Path | None = None) -> None:
    """Update just source_folders in config.yaml, preserving everything else
    and comments as best-effort (full rewrite of the mapping — YAML comments
    in the user's config.yaml will NOT be preserved by this round-trip)."""
    raw = _load_raw(path)
    raw["source_folders"] = folders
    _save_raw(raw, path)


# --- Phase 2d: review_tool.py login credentials (config.yaml's review_users) ---
# Managed here (rather than hand-editing YAML) via `main.py review-user
# add/list/remove` — see main.py and src/auth.py::hash_password. Same
# "preserve everything else, full rewrite of the mapping" caveat as
# save_source_folders above.

def add_or_update_review_user(username: str, password_hash: str, path: Path | None = None) -> None:
    raw = _load_raw(path)
    users = [u for u in (raw.get("review_users") or []) if u.get("username") != username]
    users.append({"username": username, "password_hash": password_hash})
    raw["review_users"] = users
    _save_raw(raw, path)


def remove_review_user(username: str, path: Path | None = None) -> bool:
    """Returns True if a user was actually removed, False if no such
    username existed (so the CLI can say so rather than silently no-op)."""
    raw = _load_raw(path)
    users = raw.get("review_users") or []
    remaining = [u for u in users if u.get("username") != username]
    raw["review_users"] = remaining
    _save_raw(raw, path)
    return len(remaining) != len(users)


def list_review_usernames(path: Path | None = None) -> list[str]:
    raw = _load_raw(path)
    return [u.get("username") for u in (raw.get("review_users") or [])]
