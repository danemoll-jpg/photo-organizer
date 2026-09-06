"""Harmless stand-in for review_tool.py, used ONLY by tests/test_port_check.py.

Mirrors the same "substitute a stand-in process for the real subprocess"
testing convention dashboard.py's cloudflared Start/Stop already uses (see
TODO.md's Phase 2d follow-up section) — this lets test_port_check.py
exercise the real kill-wait-relaunch state machine in
src.port_check.restart_review_tool() end to end (spawn real OS processes,
have netstat really see them, really kill them, really relaunch a new
one) without needing the real Flask app, a real DB, or a real
captions.jsonl.

Accepts the same `--port`/`--no-browser` flags real review_tool.py's CLI
does (restart_review_tool() always passes `--port`, regardless of which
script it's pointed at) so it's a drop-in stand-in. Binds with
SO_REUSEADDR before listening — this matches real behavior: Werkzeug's
dev server (what review_tool.py actually runs on) sets this too, and on
Windows (unlike POSIX) SO_REUSEADDR permits a second process to bind+
listen on a port another process is already actively listening on. That
Windows-specific quirk is exactly how this project's real incident ended
up with four separate review_tool.py processes simultaneously LISTENING
on port 5151 (see CLAUDE.md) — reproducing it here is what lets this
test's "kill more than one stale process" case be a real repro, not just
a plausible-sounding one.
"""
from __future__ import annotations

import argparse
import socket
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--no-browser", action="store_true")  # accepted, ignored — see module docstring
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", args.port))
    sock.listen(1)
    try:
        time.sleep(120)  # test kills this well before the timeout; just needs to outlive the test
    finally:
        sock.close()


if __name__ == "__main__":
    main()
