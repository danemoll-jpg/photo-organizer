# CLAUDE.md — Photo Organizer Project

## What this project is
A local tool to organize ~25 years of family photos (35k+ files, likely more now) on a Windows PC. It has three phases, each independently useful and independently testable. Build and validate them one at a time, in order — do not jump ahead to Phase 2 or 3 until Phase 1 is working and confirmed by the user.

## Environment
- Windows 11 Pro, Dell XPS 8930
- Intel i7-9700 (8 cores / 8 threads)
- NVIDIA GTX 1660 Ti, 6GB VRAM
- 64GB RAM
- GPU acceleration is available and should be used for Phase 3 (face detection/embedding). Confirm CUDA/driver setup works before writing GPU-dependent code — don't assume it's pre-configured.

## Full spec
See `photo-organizer-spec.md` in this project for the detailed phase-by-phase requirements. That file is the source of truth for scope. TODO.md tracks current status and next steps.

## Data access notes
- Plex runs on this same PC and already has remote access set up for the user's daughter. Phase 4 (daughter-facing search) builds a companion web app alongside Plex on this machine — not a cloud-hosted rebuild. Don't propose migrating photos or the DB to the cloud unless the user asks; the whole point of this design is reusing infrastructure that already works.
- The user is very comfortable with MS Access and may want to use it as an optional query front-end to the SQLite DB via ODBC linked tables. This is for the user's own use, not the daughter's.

## Non-negotiable cross-cutting rules
1. **Resumability everywhere.** Every phase must be safely re-runnable without redoing work already done. Track processed files by content hash, not filename or path.
2. **Never silently overwrite or destroy original photos.** Phase 1 uses copy-verify-delete: copy to destination, verify via hash comparison, only then delete the source. A failed verification must leave the source in place and get logged — never delete on an unverified copy. Collision handling must never overwrite an existing file.
3. **Log everything.** Every phase needs a log the user can read after the fact to answer "what happened to file X and why."
4. **Config, not hardcoding.** Source/dest paths, backend choice (local model vs Claude API for captioning), batch sizes, and GPU on/off should live in a config file.
5. **JSONL for intermediate outputs** (Phase 2 captions, Phase 3 face embeddings) — appendable, resumable. SQLite is the final data layer, loaded from JSONL.
6. **Primary key = file content hash** across all DB tables, since paths will move during Phase 1 and duplicates/re-exports are likely across 25 years of photos.

## Session model
- The user runs a **fresh Code session for each phase** (to keep individual sessions from ballooning). This means CLAUDE.md must always contain full standing context — don't assume memory of prior sessions.
- TODO.md is scoped to the current active phase only. Work only within that phase. Do not start, scaffold, or "get ahead" on later phases even if it seems efficient — the user and Claude (chat) review and re-scope TODO.md between sessions before the next phase begins.
- If something in a later phase is blocked or ambiguous because of a decision only that phase would resolve, flag it in your summary at the end of the session rather than jumping ahead to resolve it.

## Working style
- The user works in IT and is comfortable with technical detail and code — no need to oversimplify or avoid technical terms. Surface real trade-offs plainly.
- Confirm scope and design questions before writing code, especially anything involving cost (cloud API calls) or irreversible file operations.
- Update TODO.md as work progresses so the user (and Claude in chat) can track status without reading code.
