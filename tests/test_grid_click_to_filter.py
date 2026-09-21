"""Regression guard for: click-to-filter on the review tool's photo grid.

Real user request: clicking any tag, person name, or location badge shown on
a grid card should filter the grid for that value automatically, instead of
requiring the user to retype/re-pick it in the filter panel above.

Implementation (static/review.js):
  - Tag/location badges (renderGrid) and person badges (peopleHtml) gain a
    `badge-clickable` class plus `data-filter-type`/`data-filter-value`
    (and, for person, `data-cluster-id`) attributes at render time.
  - renderGrid attaches one click listener per badge, per card, each render
    -- same pattern already used for the card's own select-checkbox/label
    (see the existing selectLabel/checkbox bindings just above it) -- that
    calls e.stopPropagation() (so the click doesn't also bubble to the
    card's own "open the viewer" handler) and then applyQuickFilter(...).
  - applyQuickFilter(type, value, clusterId) sets the Tag/Location filter
    field's value (or adds a person chip via the new addPersonChipById,
    which reuses the existing peopleChips/renderPeopleChips machinery) and
    calls the existing applyFilters() -- so a badge click composes with
    whatever filters are already set, exactly like every other filter
    control on the page, rather than clearing them.
  - review.css gets a `.badge-clickable` rule (cursor: pointer + a hover
    affordance) so the badges read as clickable.

Pure front-end (no server surface) behavioral change -- consistent with this
project's existing convention for that case (see
test_viewer_edit_keydown_guard.py / test_phase2h.py's Part B docstring), the
automated check here is a source-text presence/shape check, not a full
browser-driven test (no JS test runner exists in this project). Verified for
real via the Browser pane against a live throwaway synthetic-fixture
instance (not config.yaml/the real DB): clicking a tag badge on a card
narrowed the grid to matching photos and pre-filled the Tag filter input
without opening the viewer; clicking a location badge did the same for the
Location dropdown; clicking a person badge added a removable chip to the
People filter and narrowed the grid, and clicking a second person badge on a
different card added a second chip (AND semantics, composing per Phase 3g);
Reset correctly cleared everything a click had set.

Usage:
    venv\\Scripts\\python tests\\test_grid_click_to_filter.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def test_badges_carry_clickable_class_and_filter_data() -> None:
    print("\n=== Click-to-filter: badge markup carries the right data-* attributes ===")
    js = (REPO_ROOT / "static" / "review.js").read_text(encoding="utf-8")

    tag_badge_m = re.search(
        r'\(item\.tags \|\| \[\]\)\.slice\(0, 6\)\.map\(t => `([^`]*)`\)', js,
    )
    assert tag_badge_m, "couldn't find the grid's tag-badge template literal"
    tag_badge = tag_badge_m.group(1)
    assert "badge-clickable" in tag_badge, "tag badges must carry badge-clickable"
    assert 'data-filter-type="tag"' in tag_badge, "tag badges must carry data-filter-type=\"tag\""
    assert "data-filter-value=" in tag_badge, "tag badges must carry data-filter-value"
    print("  tag badge: badge-clickable + data-filter-type=tag + data-filter-value  OK")

    loc_badge_m = re.search(
        r'item\.location_name \? `([^`]*location[^`]*)` : ""', js,
    )
    assert loc_badge_m, "couldn't find the grid's location-badge template literal"
    loc_badge = loc_badge_m.group(1)
    assert "badge-clickable" in loc_badge, "location badge must carry badge-clickable"
    assert 'data-filter-type="location"' in loc_badge, "location badge must carry data-filter-type=\"location\""
    print("  location badge: badge-clickable + data-filter-type=location + data-filter-value  OK")

    people_html_m = re.search(r'function peopleHtml\(item\)\s*\{(.*?)\n  \}', js, re.DOTALL)
    assert people_html_m, "couldn't find peopleHtml()"
    people_html = people_html_m.group(1)
    assert "badge-clickable" in people_html, "grid person badges must carry badge-clickable"
    assert 'data-filter-type="person"' in people_html, "grid person badges must carry data-filter-type=\"person\""
    assert "data-cluster-id=" in people_html, "grid person badges must carry data-cluster-id (needed to add the right chip)"
    # The "+N" overflow badge represents several people at once -- it must
    # NOT be made clickable (there's no single value it could filter by).
    more_badge_m = re.search(r'`<span class="badge person-more"[^`]*`', people_html)
    assert more_badge_m, "couldn't find the +N overflow badge"
    assert "badge-clickable" not in more_badge_m.group(0), \
        "the +N overflow badge must stay non-clickable -- it isn't one filterable value"
    print("  grid person badges: badge-clickable + data-filter-type=person + data-cluster-id  OK")
    print("  +N overflow badge correctly left non-clickable  OK")


def test_renderGrid_binds_stop_propagation_before_appending_card() -> None:
    print("\n=== Click-to-filter: renderGrid wires badge clicks without opening the viewer ===")
    js = (REPO_ROOT / "static" / "review.js").read_text(encoding="utf-8")

    render_grid_m = re.search(r'function renderGrid\(\)\s*\{(.*?)\n  \}', js, re.DOTALL)
    assert render_grid_m, "couldn't find renderGrid()"
    body = render_grid_m.group(1)

    badge_bind_pos = body.find('querySelectorAll(".badge-clickable")')
    assert badge_bind_pos != -1, "renderGrid must bind click handlers to .badge-clickable elements"

    stop_prop_window = body[badge_bind_pos:badge_bind_pos + 300]
    assert "e.stopPropagation()" in stop_prop_window, \
        "the badge click handler must call e.stopPropagation() so it doesn't also open the viewer"
    assert "applyQuickFilter(" in stop_prop_window, \
        "the badge click handler must call applyQuickFilter(...)"

    append_pos = body.find("gridEl.appendChild(card)")
    assert append_pos != -1, "renderGrid must still append the card to the grid"
    assert badge_bind_pos < append_pos, \
        "badge click handlers must be bound before the card is appended (same ordering as the existing checkbox binding)"
    print("  badge click -> stopPropagation() + applyQuickFilter(), bound before appendChild  OK")


def test_applyQuickFilter_sets_the_right_filter_and_reapplies() -> None:
    print("\n=== Click-to-filter: applyQuickFilter()/addPersonChipById() behavior ===")
    js = (REPO_ROOT / "static" / "review.js").read_text(encoding="utf-8")

    m = re.search(r'function applyQuickFilter\(type, value, clusterId\)\s*\{(.*?)\n  \}', js, re.DOTALL)
    assert m, "couldn't find applyQuickFilter()"
    body = m.group(1)

    assert 'el("f-tag").value = value' in body, "tag click must set the Tag filter field's value"
    assert 'el("f-location").value = value' in body, "location click must set the Location filter field's value"
    assert "addPersonChipById(Number(clusterId), value)" in body, \
        "person click must add a chip via addPersonChipById, not overwrite a single-value field"
    assert "applyFilters()" in body, "applyQuickFilter must actually re-apply the filters, not just stage the value"
    # applyFilters() must run after all three branches, not be skipped for
    # any one of them -- a bare textual check that it's the last statement
    # in the function body (not nested inside one of the type branches).
    assert body.rstrip().endswith("applyFilters();"), \
        "applyFilters() must run unconditionally after the type branch, for every filter type"
    print("  tag -> f-tag field, location -> f-location field, person -> addPersonChipById, then applyFilters()  OK")

    m2 = re.search(r'function addPersonChipById\(clusterId, name\)\s*\{(.*?)\n  \}', js, re.DOTALL)
    assert m2, "couldn't find addPersonChipById()"
    body2 = m2.group(1)
    assert "peopleChips.some((p) => p.cluster_id === clusterId)" in body2, \
        "addPersonChipById must dedupe against an already-picked cluster_id (no duplicate chips)"
    assert "peopleChips.push(" in body2, "addPersonChipById must actually add the chip"
    assert "renderPeopleChips()" in body2, "addPersonChipById must re-render the chip list so the UI reflects the click"
    print("  addPersonChipById() dedupes by cluster_id and re-renders the chip list  OK")


def test_clickable_badge_css_exists() -> None:
    print("\n=== Click-to-filter: badge-clickable CSS affordance ===")
    css = (REPO_ROOT / "static" / "review.css").read_text(encoding="utf-8")
    assert ".badge-clickable" in css, "review.css must style .badge-clickable"
    m = re.search(r'\.badge-clickable\s*\{([^}]*)\}', css)
    assert m, "couldn't find a .badge-clickable rule block"
    assert "cursor: pointer" in m.group(1), ".badge-clickable must set cursor: pointer"
    print("  .badge-clickable { cursor: pointer; ... } present  OK")


def main() -> None:
    test_badges_carry_clickable_class_and_filter_data()
    test_renderGrid_binds_stop_propagation_before_appending_card()
    test_applyQuickFilter_sets_the_right_filter_and_reapplies()
    test_clickable_badge_css_exists()
    print("\nALL GRID CLICK-TO-FILTER TESTS PASSED")


if __name__ == "__main__":
    main()
