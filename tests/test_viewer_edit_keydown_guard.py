"""Regression guard for: "editing a field in the viewer somehow ACTIVATES
the slideshow" (see TODO.md/CLAUDE.md). Real user report, confirmed this
session: the slideshow was NOT running when the user opened the Location
edit field and typed "New York" / "United States" -- it STARTED during
editing, then advanced away mid-edit.

Root cause, confirmed (not assumed) via direct Browser-pane testing against
a live throwaway instance of review_tool.py: static/review.js's single
global `document.addEventListener("keydown", ...)` for the viewer's
Escape/ArrowLeft/ArrowRight/Space shortcuts never checked where the
keystroke actually landed. The viewer's own inline edit form
(#viewer-edit-input) lives INSIDE the open viewer panel, so
state.viewer.open stays true the whole time an edit is in progress --
typing a space while entering a Location value bubbled straight up to this
listener and matched the `e.key === " "` branch, calling toggleSlideshow()
and starting it from a dead stop. Same class of bug applies to
ArrowLeft/ArrowRight (would step the viewer away mid-edit, e.g. while using
arrow keys to move the text cursor) and Escape (would close the viewer
outright), not just Space.

Fix (static/review.js): a new `isEditableTarget(target)` helper, checked
via `e.target` (not just the `editingField` flag) at the top of the
listener, before any shortcut branch -- so it also covers the
interval-seconds number input and any future editable control added
inside the viewer, not just the one field that happened to trigger the
real report. `openEditForm()` also gained a `pauseSlideshow()` call as a
belt-and-suspenders addition (a slideshow already RUNNING when the pencil
icon is clicked shouldn't keep advancing frames out from under an open
edit) -- not the actual fix, which is the keydown guard above.

This is a pure front-end (no server surface) behavioral change, so --
consistent with this project's existing convention for that case (see
test_phase2h.py's Part B docstring) -- the AUTOMATED check here is a
source-text presence/shape check, not a full browser-driven test (no JS
test runner exists in this project). The actual dynamic behavior was
verified for real this session via the Browser pane against a live
throwaway synthetic-fixture instance (not the real config.yaml/DB):
  - Pre-fix repro: dispatching a real `KeyboardEvent("keydown", {key:" "})`
    targeted at the focused #viewer-edit-input textarea while the Location
    edit form was open DID flip the Play button to "Pause"/started the
    slideshow (defaultPrevented=true) -- confirming the bug as reported.
  - Post-fix: the identical dispatch against the identical element/state
    left the Play button at "Play" (defaultPrevented=false) -- confirming
    the fix. ArrowRight dispatched the same way while editing did NOT
    navigate the viewer away from the open photo either.
  - Regression check: the same Space keydown dispatched with e.target =
    document.body (i.e. NOT an editable element) still correctly toggled
    the slideshow -- confirming the fix didn't break the legitimate
    shortcut, only gated it on where the keystroke actually landed.

Usage:
    venv\\Scripts\\python tests\\test_viewer_edit_keydown_guard.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def test_keydown_guard_present_and_checked_before_every_shortcut() -> None:
    print("\n=== Viewer edit-field keydown-collision fix: source presence/shape check ===")
    js = (REPO_ROOT / "static" / "review.js").read_text(encoding="utf-8")

    assert "function isEditableTarget(target)" in js, \
        "isEditableTarget() helper must exist -- it's the actual fix, not just a band-aid"
    assert 'tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable' in js, \
        "guard must cover every editable element shape, not just <textarea> (the one field the bug report used)"
    print("  isEditableTarget() helper present, covers INPUT/TEXTAREA/SELECT/contentEditable  OK")

    # Isolate the viewer's global keydown listener body so we can confirm
    # the guard is INSIDE it, and precedes every shortcut branch -- not
    # just present somewhere else in the file (e.g. dead code, or only
    # guarding one of the four shortcuts).
    m = re.search(
        r'document\.addEventListener\("keydown",\s*\(e\)\s*=>\s*\{(.*?)\n  \}\);',
        js, re.DOTALL,
    )
    assert m, "couldn't find the viewer's document-level keydown listener at all"
    body = m.group(1)

    guard_pos = body.find("isEditableTarget(e.target)")
    assert guard_pos != -1, "the keydown listener body must call isEditableTarget(e.target)"
    assert "return" in body[guard_pos:guard_pos + 40], \
        "isEditableTarget check must actually bail out (return), not just be evaluated"

    # Every shortcut branch must sit AFTER the guard, so a keystroke that
    # lands on an editable element bails out before any of them can fire --
    # this is what makes it a fix for Escape/ArrowLeft/ArrowRight too, not
    # just the Space/slideshow case the real report happened to hit.
    for needle, label in [
        ('e.key === "Escape"', "Escape (close viewer)"),
        ('e.key === "ArrowRight"', "ArrowRight (step next)"),
        ('e.key === "ArrowLeft"', "ArrowLeft (step prev)"),
        ('e.key === " "', "Space (toggle slideshow)"),
    ]:
        pos = body.find(needle)
        assert pos != -1, f"expected shortcut branch missing from listener: {label}"
        assert pos > guard_pos, f"{label} branch must be checked AFTER the isEditableTarget guard, not before"
    print("  guard is checked before EVERY shortcut branch (Escape/ArrowLeft/ArrowRight/Space), not just Space  OK")

    # Belt-and-suspenders: openEditForm() also pauses an already-running
    # slideshow the moment an edit starts (not the real fix -- the guard
    # above is -- but a reasonable second line of defense per the task).
    open_edit_form_m = re.search(r'function openEditForm\(field\)\s*\{(.*?)\n  \}', js, re.DOTALL)
    assert open_edit_form_m, "couldn't find openEditForm()"
    assert "pauseSlideshow()" in open_edit_form_m.group(1), \
        "openEditForm() should pause an already-running slideshow as a belt-and-suspenders measure"
    print("  openEditForm() also pauses an already-running slideshow (belt-and-suspenders)  OK")


def main() -> None:
    test_keydown_guard_present_and_checked_before_every_shortcut()
    print("\nALL VIEWER-EDIT-KEYDOWN-GUARD TESTS PASSED")


if __name__ == "__main__":
    main()
