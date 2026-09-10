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

    # Phase 3 Session 1: face detection + embedding (src/face_detection.py,
    # CLI: `main.py detect-faces`). faces_path is the actual per-face output
    # (one line per DETECTED face, not per photo -- see that module's
    # docstring); faces_scanned_path is a separate small resumability
    # checkpoint file (one line per photo actually scanned, including
    # photos with 0 faces found) since faces_path alone can't distinguish
    # "not yet scanned" from "scanned, 0 faces" the way captions.jsonl's
    # one-line-per-photo shape can. face_model_pack is config-driven per
    # rule 4 even though buffalo_l is the only pack actually verified this
    # session -- same reasoning as ollama_model being config-driven despite
    # qwen3-vl:2b being the only model benchmarked so far.
    faces_path: str = "data/faces.jsonl"
    faces_scanned_path: str = "data/faces_scanned.jsonl"
    face_model_pack: str = "buffalo_l"

    # Phase 3 Session 2: face clustering (src/face_clustering.py, CLI:
    # `main.py cluster-faces`). All three are judgment-call defaults, not
    # yet tuned against labeled ground truth (this library has none) --
    # config-driven per rule 4 specifically so they're retunable from the
    # dashboard/config.yaml once the user has looked at real cluster
    # quality in the labeling UI, without a code change.
    #   - face_min_det_score: RetinaFace detection-confidence floor: a face
    #     below this is never clustered (stays permanently in the "Unclustered"
    #     bucket for manual handling) -- filters out the low-confidence,
    #     often-partial/blurry detections face_detection.py's own docstring
    #     flagged as worth filtering before clustering.
    #   - face_cluster_similarity_threshold: cosine similarity above which
    #     two ArcFace embeddings are treated as the same person -- used both
    #     to match a new face against an existing person's centroid AND as
    #     DBSCAN's neighbor radius (eps = 1 - threshold) when forming brand
    #     new clusters.
    #   - face_cluster_min_samples: DBSCAN's min_samples -- a person who
    #     appears in only 1 photo can't form a cluster on their own and stays
    #     in "Unclustered" until a min_samples-th matching face shows up (or
    #     the user manually creates a person from it in the labeling UI) --
    #     see src/face_clustering.py's module docstring.
    #
    #   URGENT-fix follow-up session, defaults retuned (0.45/2 -> 0.65/4) --
    #   0.45 was this project's own earlier small-sample tuning (39 groups
    #   from 767 faces, CLAUDE.md), and did NOT generalize to real
    #   full-library scale: verified directly against the real ~155k-photo/
    #   203k-face production DB that it produces a single 61,676-face
    #   "cluster" (~30% of all clustered faces) which real sampling +
    #   pairwise-similarity checks confirmed is a chained mass of unrelated
    #   people (mean pairwise cosine similarity among a random sample of its
    #   own members: ~0.07 -- indistinguishable from random, vs. 0.39-0.78
    #   for the library's actual real named people), not a clustering
    #   quirk affecting one photo here or there. Root cause: DBSCAN with a
    #   low min_samples degenerates toward single-linkage/transitive-closure
    #   chaining, and at tens of thousands of candidate faces, near-threshold
    #   false-positive pairs accumulate combinatorially -- confirmed
    #   empirically (real DB, not synthetic) that this is NOT primarily a
    #   min_samples problem: sweeping min_samples 2->8 at threshold=0.45 only
    #   shrank the runaway cluster by ~8% (10,517->9,720 faces in a 20k-face
    #   subsample test), while raising the threshold did the real work
    #   (largest cluster in the same 20k-face subsample: ~10,500 at 0.45,
    #   ~6,400 at 0.55, ~3,200 at 0.65, ~1,960 at 0.70, ~780 at 0.75) --
    #   restricting to only high-confidence (det_score>=0.7) detections did
    #   NOT meaningfully help either (still ~11,900/20,000 at threshold=0.45),
    #   ruling out "it's just blurry/low-quality detections" as the fix.
    #   0.65/4 is a deliberate middle ground, not a claim of a fully solved
    #   problem: it meaningfully shrinks (but doesn't guarantee eliminating)
    #   runaway chaining, while trying not to destroy match recall for
    #   already-named people whose OWN existing faces are less internally
    #   similar than a stranger might expect -- especially a child whose face
    #   changes a lot over 25 years (this library's own "Juliana Moll"
    #   cluster: only 45% of her own already-clustered faces would match her
    #   own centroid at threshold=0.65, dropping to just 7.9% at 0.75 -- a
    #   real, measured recall cost from going too high, not a hypothetical
    #   one). This is a real precision/recall trade-off with no single
    #   scalar answer, not something this session claims to have solved
    #   perfectly -- see TODO.md's URGENT section and CLAUDE.md for the full
    #   writeup, including why the labeling UI's own crash fix (paginated
    #   thumbnail rendering, no longer size-limited) is what makes whatever
    #   imperfect grouping remains at ANY parameter setting safe to view and
    #   correct by hand (Select All + "Remove Selected from Cluster"),
    #   rather than a hard blocker.
    face_min_det_score: float = 0.5
    face_cluster_similarity_threshold: float = 0.65
    face_cluster_min_samples: int = 4
    # Follow-up session, direct user feedback ("it should recognize us
    # better since it's identified us plenty of times"): confirmed the
    # complaint is real, not just a feeling -- the comment above already
    # documents that "Juliana Moll" (a growing child) only self-matches her
    # OWN existing centroid 45% of the time at threshold=0.65. Root cause:
    # ONE threshold was doing two different jobs with two different real
    # risk profiles -- (a) "should this new face join an EXISTING, already
    # human-confirmed person" (a false match here is low-risk: it's one bad
    # photo under a real person's name, easy to spot and fix by hand via
    # "Remove Selected from Cluster") and (b) "should DBSCAN group two
    # never-before-seen faces into a brand new person" (a false match here
    # is exactly the chaining problem the 0.45->0.65 retune above exists to
    # prevent -- a lenient value here directly regrows the mega-cluster).
    # `face_match_existing_threshold` decouples them: when set, it replaces
    # ONLY job (a) above -- run_face_clustering()'s DBSCAN eps for job (b)
    # keeps using face_cluster_similarity_threshold unchanged, so lowering
    # this can't reintroduce chaining risk for genuinely new/unmatched
    # faces. Defaults to None (falls back to face_cluster_similarity_
    # threshold, i.e. today's exact behavior) -- a real value is a
    # deliberate per-library tuning choice for the user to make after
    # seeing real match-rate numbers, not something to guess at here.
    face_match_existing_threshold: float | None = None
    # Phase 3l (URGENT follow-up session): a real incident -- creating a
    # brand-new 2-photo person ("Luna", a pet) via Split, the user couldn't
    # lock it against automatic matching before a later `cluster-faces` run
    # matched 1,947 more faces to it in one pass -- confirmed the SAME
    # weak/small-sample-centroid-plus-lenient-face_match_existing_threshold
    # mechanism already diagnosed for "Joeanne Sanders" (1 face) and
    # cluster 6367 (5 faces) in the immediately prior session. Both of
    # those were fixed reactively, one at a time, after the fact --
    # auto_match_locked (above) only helps if a user remembers to set it
    # BEFORE a clustering run happens, which is exactly the race that kept
    # losing. This is the systemic, evidence-based safeguard instead: a
    # person is EXCLUDED from _load_existing_centroids() -- cannot be an
    # automatic-matching TARGET at all -- until their face_count reaches
    # this minimum, regardless of auto_match_locked (additive, not a
    # replacement for it -- a person can still be locked forever even
    # after crossing this floor, e.g. a pet that will never legitimately
    # need more automatic matches). Manual assignment (Move/Merge Selected
    # Faces Here) is completely unaffected either way -- same "automatic
    # vs. manual" split auto_match_locked already established.
    #
    # Default of 6 is evidence-based, not guessed: checked against the real
    # production DB before picking it -- all three real incidents found so
    # far had a pre-contamination face_count of 1 (Joeanne Sanders), 5
    # (cluster 6367), and 2 (Luna) respectively, so the gate must exclude
    # at least up to 5 to have prevented every one of them; of the 150
    # currently-named real people in the DB, only 5 have face_count <= 5
    # (two of which -- "Shadow" and "Chuckles" -- read as pets themselves,
    # worth the user locking by hand too), confirming a floor around here
    # affects only a narrow, genuinely-tiny real slice rather than
    # penalizing established people. Also consistent with
    # face_cluster_min_samples=4 (DBSCAN's own evidence-tuned floor for
    # forming a brand-new cluster at all) being too low a bar on its own
    # for a *matching target*, a different, arguably higher-risk decision
    # than initial cluster formation.
    face_min_cluster_size_for_matching: int = 6

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
    def faces_path_abs(self) -> Path:
        p = Path(self.faces_path)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def faces_scanned_path_abs(self) -> Path:
        p = Path(self.faces_scanned_path)
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
        faces_path=raw.get("faces_path", "data/faces.jsonl"),
        faces_scanned_path=raw.get("faces_scanned_path", "data/faces_scanned.jsonl"),
        face_model_pack=raw.get("face_model_pack", "buffalo_l"),
        face_min_det_score=raw.get("face_min_det_score", 0.5),
        # URGENT-fix follow-up session: defaults retuned 0.45/2 -> 0.65/4 --
        # see the Config dataclass field comment above for the full,
        # real-data-backed reasoning.
        face_cluster_similarity_threshold=raw.get("face_cluster_similarity_threshold", 0.65),
        face_cluster_min_samples=raw.get("face_cluster_min_samples", 4),
        face_match_existing_threshold=raw.get("face_match_existing_threshold"),
        # Phase 3l: see the Config dataclass field comment above for the
        # real-data-backed default (6).
        face_min_cluster_size_for_matching=raw.get("face_min_cluster_size_for_matching", 6),
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
