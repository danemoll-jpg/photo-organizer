/* Phase 2b/2c review tool — front-end. Talks to review_tool.py's read-only
 * JSON API. Two independent-but-filter-synced views:
 *   - grid: keyset-paginated browse (see /api/photos)
 *   - viewer: single-photo step navigation + auto-advance slideshow
 *     (see /api/nav) — cursor-based, so stepping past a grid page's last
 *     photo, or letting a slideshow run indefinitely, never hits a page
 *     boundary; each step is just "the next matching row after this path".
 *
 * Phase 2c additions:
 *   - Filters: tag, caption keyword, GPS/location (+ a disabled, visibly
 *     inert people/faces control — Phase 3 doesn't exist yet). Every
 *     filter (old and new) is read by currentFilterParams() and sent to
 *     every endpoint the grid AND the viewer use, so a filtered slideshow
 *     only ever shows matching photos, same as the grid.
 *   - Viewer "Random" button (one-off jump, /api/random) — independent of...
 *   - ...the viewer's order toggle (Chronological/Random), which governs
 *     manual Prev/Next and auto-advance while active. Random order uses
 *     /api/nav?mode=random&seed=&idx= — see stepRandomNav below — which
 *     gives a repeat-free pseudo-random sequence over the CURRENT filtered
 *     set without ever fetching/shuffling it client-side. A fresh seed is
 *     minted whenever order is switched to Random (or filters change while
 *     it's active), matching "fresh shuffle every time it starts".
 *
 * Phase 2e addition: video rows flow through the exact same grid/filter/
 *   nav/random/slideshow code as photos (server no longer excludes them —
 *   see review_tool.py) — the only branching here is display: grid cards
 *   show a play-icon placeholder instead of a decoded thumbnail, and the
 *   viewer swaps in a real <video> element (playing /video/<hash> — see
 *   renderViewerItem) in place of the <img> used for photos.
 *
 * Grid/Viewer nav control cleanup (this follow-up):
 *   - The viewer's top+bottom nav-control duplication (added, then found
 *     to have been built in the wrong place) is REVERTED — the viewer is
 *     back to a single set of controls, referenced by id like before that
 *     feature ever existed.
 *   - The grid's page-number/prev-next pager gets that duplication
 *     instead (top+bottom of the thumbnail grid) — see the grid-prev-btn/
 *     grid-next-btn/grid-page-info NodeLists below, same "shared classes,
 *     every copy kept in sync" pattern the viewer used to use.
 *   - Location and Tag filters are now backed by /api/facets (distinct
 *     known values from the DB): Location is a closed <select> (folds in
 *     the old standalone GPS has/no-location toggle as a "No location"
 *     option), Tag is a free-text <input> with a <datalist> of
 *     suggestions (196 distinct tags at last count — too many for a
 *     comfortable plain <select>, but exact-match filtering still works
 *     for any value typed, listed or not — see _build_extra_predicate).
 *     (Superseded below — the <datalist> was replaced with a custom
 *     dropdown once it turned out never to render on WebKit/iOS at all.)
 *   - The old free-text "Folder contains" filter is removed (redundant
 *     with the date-range pickers given the YYYY/YYYY-MM layout).
 *
 * Grid filter improvements batch (this follow-up session):
 *   - Tag suggestions moved from a native <datalist> to a hand-built
 *     dropdown (see attachSuggestionDropdown below) -- <datalist>'s
 *     suggestion popup never renders at all on WebKit/iOS (a real platform
 *     limitation: the exact-match filtering itself always worked fine
 *     there, only the suggestion UI was invisible). Built from plain DOM
 *     elements + our own JS filtering, so it works identically on every
 *     browser/platform rather than depending on any browser's native
 *     rendering of anything.
 *   - A visible match-count ("N item(s) match your filters"), shown only
 *     while a filter is active -- reuses the /api/stats call refreshStats()
 *     already makes (no new network request).
 *   - A media_type filter (All/Photos only/Videos only), same closed-
 *     <select> pattern as Location.
 *
 * Phase 2f (this follow-up session) -- the tool is no longer read-only:
 *   - Edit/pencil buttons next to Date taken/Caption/Tags/Location in the
 *     viewer open ONE shared inline edit form (openEditForm/closeEditForm/
 *     saveEdit below), fetch()-posting to /api/edit/<hash> with the same
 *     CSRF token the logout form already carries (window.REVIEW_CONFIG.
 *     csrfToken -- see templates/review.html). A video's caption button
 *     reads "+ Add caption" instead of a bare pencil when it has none yet
 *     (see renderViewerItem) -- same mechanism, just starting from empty,
 *     per spec.
 *   - "Hide from library" / un-hide (toggleHidden below) uses the exact
 *     same /api/edit/<hash> endpoint with field="excluded" -- not a
 *     separate action. Hiding the CURRENTLY OPEN item while "Show hidden"
 *     is off makes it vanish from every filtered view immediately, so the
 *     viewer closes back to the grid rather than keep displaying an item
 *     that no longer matches anything.
 *   - A new "Show hidden" checkbox (state.filters.show_hidden) reveals
 *     already-hidden items again (mixed in with visible ones, badged and
 *     dimmed -- see renderGrid) so they can be found and un-hidden.
 *
 * Phase 2h (this follow-up session):
 *   Part A -- bulk selection/editing. state.selection is a plain Set of
 *     file_hash values, populated/depleted by a checkbox drawn on every
 *     grid card (see renderGrid) -- being a Set independent of
 *     state.grid.items, it survives paging to a different page untouched
 *     (no "select all matching filter" beyond what's been visibly paged
 *     through, per spec). The bulk-action bar (#bulk-bar) appears once
 *     it's non-empty and posts to the new /api/bulk-edit endpoint (see
 *     postBulkEdit) -- Set location/Set date/Hide reuse the single-edit
 *     endpoint's full-"set" semantics per selected item, but "Add tag"
 *     uses mode="add_tag" (a union/append into each item's OWN existing
 *     tags server-side -- see src/edits.py::add_tag_bulk) specifically
 *     because a full-replace across a batch of files with different
 *     existing tags would destroy them.
 *   Part B -- on a phone-width viewport only (see review.css's <=480px
 *     block, layered on top of the existing <=800px tablet-stack rule,
 *     which is unchanged), the viewer's info panel starts collapsed and is
 *     toggled by #viewer-info-toggle (setInfoExpanded below) -- purely a
 *     show/hide of .viewer-details via a class on .viewer-panel; nothing
 *     about editing itself changes once it's expanded.
 *
 * Real-user-feedback fix (this follow-up session): Part A's "Set location"
 *   was a <select> restricted to /api/facets' existing distinct-location
 *   list, so a genuinely new place (nothing yet reverse-geocoded there)
 *   couldn't be entered at all. Now free-text + a suggestion dropdown --
 *   see attachSuggestionDropdown, factored out of the Tag filter's own
 *   fix above so this reuses that exact behavior rather than a second
 *   copy-pasted implementation. Scoped to the bulk-edit UI only -- the
 *   single-item viewer edit form's Location field was already free-text.
 *
 * Phase 3d: the grid's Person filter (added inert in Phase 2c -- disabled
 *   control, "Phase 3 doesn't exist yet") was wired to real Phase 3 data.
 *   Populated from /api/facets' new `people` list (real assigned names
 *   only), filtering via a new `person` param (review_tool.py's
 *   _build_filters resolves it to a cluster_id join -- see its docstring).
 *   THIRD reuse of attachSuggestionDropdown (Tag filter, bulk-edit
 *   Location, now this) -- per rule 7, no third copy-pasted implementation.
 *   `assigned_name` has no uniqueness constraint (unlike tags.tag_name), so
 *   allPeople below disambiguates same-named entries by appending
 *   "(#cluster_id)" only to whichever ones actually collide, so the
 *   exact-typed-name-match resolution applyFilters() does (same "value must
 *   exactly match a real known value" contract as Tag) still lands on the
 *   right cluster_id even in that rare case.
 *
 * Phase 3g (this follow-up session): the Person filter (Phase 3d, below) is
 *   generalized from a single value to a chip-list multi-select with AND
 *   semantics -- select several named people, see only photos containing
 *   every one of them (the user's own example: Dan Moll + Juliana Moll ->
 *   photos with both). state.filters.person (a single cluster_id) becomes
 *   state.filters.people (an array of cluster_ids, sent as one
 *   comma-separated `people` param -- see review_tool.py's _build_filters
 *   for the AND-query itself, which lives entirely server-side so grid/
 *   nav/random/stats all enforce it automatically same as every other
 *   filter). `peopleChips` (below) holds the currently-picked
 *   {cluster_id, label} chips; attachSuggestionDropdown gained an optional
 *   `onSelect` callback (4th param) so picking a suggestion here adds a
 *   chip instead of filling the input's own value, the way Tag/bulk-edit-
 *   Location's calls (which omit it, unchanged) still do.
 *
 * Phase 3e (this session): the per-photo DISPLAY Phase 3d deliberately left
 *   alone (the viewer's "People" row was an inert placeholder) is now real,
 *   in both places:
 *   - Viewer: renderPeopleList(item.people) -- first VIEWER_PEOPLE_VISIBLE
 *     names, folding the rest behind a "+N more" toggle that reuses the
 *     exact same show/hide-a-block-and-swap-my-own-text idiom
 *     #viewer-info-toggle/setInfoExpanded already established, rather than
 *     inventing a second collapse pattern on the same page.
 *   - Grid: peopleHtml(item) -- more aggressive collapsing (first
 *     GRID_PEOPLE_VISIBLE names + one "+N" badge whose title attribute
 *     lists everyone), reusing the same .badge look tags/location already
 *     use on a card. No new click handling needed for "click through to see
 *     everyone" -- the card's existing click-opens-viewer handler already
 *     covers these badges since they don't stop propagation.
 *   Both read item.people, a list of {cluster_id, name} review_tool.py's
 *   _row_to_dict now populates per photo (named people only, batched one
 *   query per grid page -- see _people_for_hashes) -- read-only display,
 *   same scope line Phase 3d drew for the filter (editing stays in the
 *   Tkinter labeling window only).
 *
 * Phase 3h (this session): per-user favorites.
 *   - A star badge on the grid card for favorited-by-ME only (per spec,
 *     "keep cards visually clean") -- see cardFavoriteBadge/renderGrid.
 *     The full "who favorited this" list is a viewer-only detail, same
 *     compact-grid/full-viewer split Phase 3e established for people.
 *   - Viewer: #viewer-favorite-btn toggles the CURRENT user's own favorite
 *     (toggleFavorite below, posting to /api/favorite/<hash> with the same
 *     CSRF token every other write route already uses) -- never affects
 *     another user's favorite mark on the same photo. #viewer-favorited-by
 *     shows everyone who has (item.favorited_by, a plain username list),
 *     not just yes/no.
 *   - Filter: a plain <select> (#f-favorite-by), deliberately simpler than
 *     the People filter's chip-list multi-select (spec's own explicit
 *     call) -- "Any" / "Me" (__me__) / "Anyone" (__any__) / each real
 *     review_user individually, the last populated from /api/facets'
 *     new `review_users` list in loadFacets. Sent as one `favorite_by`
 *     param, resolved entirely server-side (see review_tool.py's
 *     _build_filters) -- this file never needs to know its own username
 *     to filter "Me", it just sends the sentinel.
 */
(() => {
  const cfg = window.REVIEW_CONFIG;

  // Sentinel value for the Location dropdown's "No location" option —
  // translated to has_location=no rather than sent as a literal location
  // name (see currentFilterParams).
  const NO_LOCATION_VALUE = "__no_location__";

  const state = {
    // Phase 3g: `person` (single cluster_id) -> `people` (array of
    // cluster_ids, AND semantics -- see module docstring).
    filters: { date_from: "", date_to: "", tag: "", caption_kw: "", location: "", has_location: "", media_type: "", people: [], favorite_by: "", show_hidden: false },
    pageSize: cfg.pageSize,
    // Phase 2h Part A: file_hash values checked in the grid, independent of
    // which page is currently loaded -- see renderGrid/toggleSelect.
    selection: new Set(),
    grid: { items: [], firstCursor: null, lastCursor: null, hasNext: false, hasPrev: false, pageNum: 1 },
    viewer: {
      open: false,
      current: null,        // full photo dict currently shown
      playing: false,
      intervalMs: Math.max(1, cfg.slideshowSeconds) * 1000,
      timer: null,
      order: "chronological",  // Phase 2c: "chronological" | "random"
      randomSeed: null,        // minted fresh whenever random order (re)starts
      randomIdx: 0,
    },
  };

  // ---- DOM refs ----
  const el = (id) => document.getElementById(id);
  const statsEl = el("stats");
  const matchCountEl = el("filter-match-count");
  const gridEl = el("grid");
  const gridEmptyEl = el("grid-empty");

  // Grid pager is duplicated top+bottom in the HTML (same classes, no ids
  // -- ids must be unique) so every group below is a NodeList of BOTH
  // copies. Every handler operates on the whole list so the two copies can
  // never show conflicting state (e.g. one page ahead of the other).
  const els = (cls) => document.querySelectorAll("." + cls);
  const forEach = (list, fn) => list.forEach(fn);
  const gridPageInfoEls = els("grid-page-info");
  const gridPrevBtnEls = els("grid-prev-btn");
  const gridNextBtnEls = els("grid-next-btn");

  // Phase 2h Part A: bulk selection/editing bar
  const bulkBarEl = el("bulk-bar");
  const bulkCountEl = el("bulk-count");
  const bulkResultEl = el("bulk-result");
  // Real-user-feedback fix: free-text + suggestion dropdown, replacing the
  // old closed <select> that couldn't accept a genuinely new place -- see
  // attachSuggestionDropdown below.
  const bulkLocationInput = el("bulk-location-input");
  const bulkLocationSuggestionsEl = el("bulk-location-suggestions");

  const viewerEl = el("viewer");
  const viewerPanelEl = document.querySelector(".viewer-panel");
  const viewerInfoToggle = el("viewer-info-toggle");
  const viewerImg = el("viewer-img");
  const viewerVideo = el("viewer-video");
  const viewerNoMore = el("viewer-nomore");
  const viewerPath = el("viewer-path");
  const viewerDate = el("viewer-date");
  const viewerCaption = el("viewer-caption");
  const viewerTags = el("viewer-tags");
  const viewerLocation = el("viewer-location");
  const viewerPeople = el("viewer-people"); // Phase 3e
  const viewerPersonInput = el("viewer-person-input"); // Phase 3i
  const viewerPersonAddBtn = el("viewer-person-add"); // Phase 3i
  const viewerFavoriteBtn = el("viewer-favorite-btn"); // Phase 3h
  const viewerFavoritedBy = el("viewer-favorited-by"); // Phase 3h
  const viewerPlayPause = el("viewer-playpause");
  const viewerInterval = el("viewer-interval");
  const viewerRandomBtn = el("viewer-random");
  const viewerOrderSelect = el("viewer-order");

  // Phase 2f: editable metadata + "Hide from library"
  const editFormEl = el("viewer-edit-form");
  const editLabelEl = el("viewer-edit-label");
  const editInputEl = el("viewer-edit-input");
  const editHintEl = el("viewer-edit-hint");
  const editErrorEl = el("viewer-edit-error");
  const editSaveBtn = el("viewer-edit-save");
  const editCancelBtn = el("viewer-edit-cancel");
  const hideBtn = el("viewer-hide-btn");
  const captionEditBtn = document.querySelector('.edit-btn[data-field="caption"]');

  // ---- persisted slideshow interval (per-browser convenience only) ----
  try {
    const saved = localStorage.getItem("review_slideshow_seconds");
    if (saved) {
      const secs = parseFloat(saved);
      if (secs > 0) {
        state.viewer.intervalMs = secs * 1000;
        viewerInterval.value = secs;
      }
    }
  } catch (e) { /* localStorage unavailable -- fine, just use the config default */ }

  function qs(params) {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== null && v !== undefined && v !== "") p.set(k, v);
    }
    return p.toString();
  }

  function currentFilterParams() {
    // Note: no "folder" param — that filter was removed from the UI.
    // Phase 3g: "people" is a comma-separated list of people.cluster_id
    // values (see applyFilters/peopleChips), not raw typed names — so the
    // same-name-different-person edge case (see loadFacets) is never
    // ambiguous server-side either. An empty array joins to "", which qs()
    // already omits, same as any other blank filter.
    return {
      date_from: state.filters.date_from,
      date_to: state.filters.date_to,
      tag: state.filters.tag,
      caption_kw: state.filters.caption_kw,
      location: state.filters.location,
      has_location: state.filters.has_location,
      media_type: state.filters.media_type,
      people: state.filters.people.join(","),
      favorite_by: state.filters.favorite_by,
      show_hidden: state.filters.show_hidden ? "1" : "",
    };
  }

  function hasActiveFilters() {
    const f = state.filters;
    return Boolean(f.date_from || f.date_to || f.tag || f.caption_kw || f.location || f.has_location || f.media_type || f.people.length || f.favorite_by);
  }

  // Random 31-bit int as a string — used to seed a fresh random-order
  // permutation server-side (see review_tool.py's _feistel_permute /
  // _random_order_for). Doesn't need to be cryptographically random, just
  // different each time random order (re)starts.
  function mintSeed() {
    return String((Math.random() * 0x7fffffff) | 0);
  }

  async function fetchJSON(url) {
    const res = await fetch(url);
    if (res.status === 401) {
      // Phase 2d: session expired/logged out mid-use. The page itself is
      // already gated server-side (a fresh page load would've redirected
      // to /login), so this only fires for a fetch() made from a page
      // that was open before the session ended -- bounce there now.
      window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
      throw new Error("Unauthorized — redirecting to login");
    }
    if (!res.ok) throw new Error(`Request failed: ${res.status}`);
    return res.json();
  }

  function fmtDate(iso) {
    if (!iso) return "Unknown date";
    return iso.slice(0, 10);
  }

  function captionHtml(item) {
    if (!item.captioned) {
      // Video never gets an automated caption (Phase 2 doesn't caption
      // video by design) -- "Not yet captioned" would wrongly imply a
      // pending process that will eventually get to it.
      const text = item.is_video ? "No caption" : "Not yet captioned";
      return `<span class="card-caption pending">${text}</span>`;
    }
    return `<span class="card-caption">${escapeHtml(item.caption || "")}</span>`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ---------------------------------------------------------------------
  // Phase 3e: named-people display (viewer "People" row + grid card badges)
  // ---------------------------------------------------------------------
  // Viewer: show the first VIEWER_PEOPLE_VISIBLE names, fold the rest behind
  // a "+N more" toggle -- reuses the exact same idiom #viewer-info-toggle/
  // setInfoExpanded already established (a button that toggles a hidden
  // block and swaps its own text), rather than inventing a second collapse
  // pattern in the same page.
  const VIEWER_PEOPLE_VISIBLE = 4;

  // Phase 3i: each name badge gets a small "×" (reusing the People filter's
  // own .chip-remove look, see review.css) to remove that person from
  // THIS photo -- see removePersonFromCurrent below.
  function personBadge(p) {
    return `<span class="badge person" data-cluster-id="${p.cluster_id}">${escapeHtml(p.name)}` +
      `<button type="button" class="chip-remove person-remove" data-cluster-id="${p.cluster_id}" ` +
      `title="Remove ${escapeHtml(p.name)} from this photo">&times;</button></span>`;
  }

  function renderPeopleList(people) {
    if (!people || !people.length) {
      return `<span class="placeholder">No named people identified in this photo yet.</span>`;
    }
    const shown = people.slice(0, VIEWER_PEOPLE_VISIBLE);
    const rest = people.slice(VIEWER_PEOPLE_VISIBLE);
    let html = shown.map(personBadge).join(" ");
    if (rest.length) {
      const restHtml = rest.map(personBadge).join(" ");
      html += ` <button type="button" class="people-more-toggle" data-count="${rest.length}">+${rest.length} more</button>` +
        `<span class="people-rest" hidden> ${restHtml}</span>`;
    }
    return html;
  }

  // Delegated (not re-bound on every render, since #viewer-people's own
  // innerHTML is replaced wholesale every time renderViewerItem runs) --
  // same reasoning review.js already uses elsewhere for content that's
  // rebuilt from scratch per item.
  viewerPeople.addEventListener("click", (e) => {
    const removeBtn = e.target.closest(".person-remove");
    if (removeBtn) {
      removePersonFromCurrent(Number(removeBtn.dataset.clusterId));
      return;
    }
    const btn = e.target.closest(".people-more-toggle");
    if (!btn) return;
    const restEl = viewerPeople.querySelector(".people-rest");
    if (!restEl) return;
    restEl.hidden = !restEl.hidden;
    btn.textContent = restEl.hidden ? `+${btn.dataset.count} more` : "Show fewer";
  });

  // Grid: much more compact than the viewer (small cards, per spec) -- only
  // the first GRID_PEOPLE_VISIBLE names get their own badge, the rest fold
  // into one "+N" badge whose title (native browser tooltip) lists everyone,
  // so hovering still surfaces the full list without a click. The card's own
  // click-to-open-viewer handler already covers every element inside it
  // (these badges aren't given their own stopPropagation), so clicking
  // through to the full, expandable viewer list also just works for free.
  const GRID_PEOPLE_VISIBLE = 2;

  function peopleHtml(item) {
    const people = item.people || [];
    if (!people.length) return "";
    const shown = people.slice(0, GRID_PEOPLE_VISIBLE);
    const rest = people.slice(GRID_PEOPLE_VISIBLE);
    let html = shown.map(p => `<span class="badge person">${escapeHtml(p.name)}</span>`).join("");
    if (rest.length) {
      const allNames = people.map(p => p.name).join(", ");
      html += `<span class="badge person-more" title="${escapeHtml(allNames)}">+${rest.length}</span>`;
    }
    return `<div class="card-people">${html}</div>`;
  }

  // ---------------------------------------------------------------------
  // Facets (Location dropdown + Tag/Person suggestion data)
  // ---------------------------------------------------------------------
  let allTags = []; // populated below, filtered client-side by the Tag suggestion dropdown
  let allLocations = []; // ditto, for the bulk-edit Location suggestion dropdown below
  let allPeople = []; // Phase 3d: [{cluster_id, label}] for the Person filter's suggestion dropdown, see below

  async function loadFacets() {
    try {
      const data = await fetchJSON("/api/facets");
      const locSel = el("f-location");
      for (const name of data.locations || []) {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        locSel.appendChild(opt);
      }
      allTags = data.tags || [];
      // Real-user-feedback fix: the bulk-edit Location field used to clone
      // these same <option>s into a second <select>, which meant it could
      // only ever bulk-set a location some OTHER photo already had. It's
      // now free-text (see attachSuggestionDropdown below) -- this list is
      // just quick-pick suggestions, not a hard constraint on what can be
      // typed/submitted.
      allLocations = data.locations || [];
      // Phase 3d: /api/facets' `people` list is real named people only
      // (see review_tool.py). Unlike tags.tag_name, `assigned_name` has no
      // uniqueness constraint -- two different real people COULD share a
      // name -- so build a `label` that's only disambiguated with
      // "(#cluster_id)" for whichever names actually collide, keeping the
      // common case's label identical to the plain assigned_name (so
      // applyFilters' exact-label lookup below still "just works" the same
      // way Tag's exact-match-against-a-known-value already does).
      const nameCounts = {};
      for (const p of data.people || []) nameCounts[p.assigned_name] = (nameCounts[p.assigned_name] || 0) + 1;
      allPeople = (data.people || []).map((p) => ({
        cluster_id: p.cluster_id,
        label: nameCounts[p.assigned_name] > 1 ? `${p.assigned_name} (#${p.cluster_id})` : p.assigned_name,
      }));
      // Phase 3h: one <option> per real configured review_user, in addition
      // to the "Me"/"Anyone" sentinels already in the markup (see
      // templates/review.html) -- the option's own value IS the username
      // (not a sentinel), since _build_filters treats anything that isn't
      // __me__/__any__ as a literal username to match.
      const favSel = el("f-favorite-by");
      for (const username of data.review_users || []) {
        const opt = document.createElement("option");
        opt.value = username;
        opt.textContent = username;
        favSel.appendChild(opt);
      }
    } catch (e) {
      // Non-fatal: filters just show "Any"/no suggestions until reloaded.
    }
  }

  // ---------------------------------------------------------------------
  // Hand-built suggestion dropdown (custom, not <datalist> -- see module
  // docstring: <datalist>'s popup never renders at all on WebKit/iOS).
  // Originally built for the Tag filter only; factored out here so the
  // bulk-edit Location field (real-user-feedback fix, below) can reuse the
  // exact same behavior instead of a second copy-pasted implementation.
  // `getItems()` returns the full current candidate list (already-loaded
  // facet data) each time a suggestion list is rendered.
  //
  // Phase 3g: optional 4th param `onSelect(text)` -- when given, picking a
  // suggestion calls it and clears the input instead of filling the
  // input's own value with the picked text. This is what lets the People
  // filter turn a pick into a removable chip (see addPersonChip below)
  // while Tag/bulk-edit-Location's existing calls (which don't pass it)
  // keep their original "fill the field" behavior unchanged.
  // ---------------------------------------------------------------------
  const MAX_SUGGESTIONS = 50; // plenty for a "did you mean" list; keeps a huge facet table cheap to render

  function attachSuggestionDropdown(inputEl, listEl, getItems, onSelect) {
    function render() {
      const q = inputEl.value.trim().toLowerCase();
      const items = getItems();
      const matches = (q ? items.filter((t) => t.toLowerCase().includes(q)) : items).slice(0, MAX_SUGGESTIONS);
      if (!matches.length) {
        hide();
        return;
      }
      listEl.innerHTML = matches.map((t) => `<li>${escapeHtml(t)}</li>`).join("");
      listEl.hidden = false;
    }
    function hide() {
      listEl.hidden = true;
      listEl.innerHTML = "";
    }
    inputEl.addEventListener("input", render);
    inputEl.addEventListener("focus", render);
    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Escape") hide();
    });
    // mousedown (not click) so this fires BEFORE the input's blur handler
    // below would otherwise hide the list first -- selecting a suggestion
    // must land its value in the field, still free-text/editable afterward
    // (same as picking a <datalist> option used to), not lock it in.
    listEl.addEventListener("mousedown", (e) => {
      const li = e.target.closest("li");
      if (!li) return;
      e.preventDefault(); // don't let the input lose focus over this
      if (onSelect) {
        onSelect(li.textContent);
        inputEl.value = ""; // cleared, ready to type the next one (Phase 3g)
      } else {
        inputEl.value = li.textContent;
      }
      hide();
    });
    inputEl.addEventListener("blur", () => {
      // Short delay so the mousedown handler above still gets to run first
      // -- blur fires the instant focus leaves the input, which a
      // mousedown on the suggestion list technically does before its own
      // handler runs.
      setTimeout(hide, 150);
    });
    // Clicking anywhere else on the page should close an open dropdown too
    // (e.g. clicking straight into another field without going through
    // blur-then-something-else first).
    document.addEventListener("click", (e) => {
      if (e.target !== inputEl && !listEl.contains(e.target)) hide();
    });
    return hide;
  }

  const tagInput = el("f-tag");
  const hideTagSuggestions = attachSuggestionDropdown(tagInput, el("f-tag-suggestions"), () => allTags);
  // Real-user-feedback fix: bulk-edit Location, same pattern, suggestions
  // drawn from allLocations instead of allTags.
  attachSuggestionDropdown(bulkLocationInput, bulkLocationSuggestionsEl, () => allLocations);
  // Phase 3d/3g: grid People filter, fourth reuse of the same type-ahead
  // component -- see module docstring. Suggestions are display labels
  // (allPeople's disambiguated `label`, not raw assigned_name); picking one
  // resolves it back to a cluster_id and adds a chip (Phase 3g), rather
  // than filling the input's own value the way Tag/bulk-edit-Location do.
  const personInput = el("f-person");
  const peopleChipsEl = el("f-people-chips");
  let peopleChips = []; // [{cluster_id, label}], the currently-selected chips

  function renderPeopleChips() {
    peopleChipsEl.innerHTML = peopleChips.map((p) =>
      `<span class="chip" data-cluster-id="${p.cluster_id}">${escapeHtml(p.label)}<button type="button" class="chip-remove" aria-label="Remove ${escapeHtml(p.label)}">&times;</button></span>`
    ).join("");
  }
  peopleChipsEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".chip-remove");
    if (!btn) return;
    const clusterId = Number(btn.closest(".chip").dataset.clusterId);
    peopleChips = peopleChips.filter((p) => p.cluster_id !== clusterId);
    renderPeopleChips();
  });

  // Resolves typed/picked text to a real allPeople entry (same "must
  // exactly match a real known value" contract Tag already has) and adds
  // it as a chip, ignoring anything that doesn't match and any person
  // already picked (no duplicate chips for the same cluster_id).
  function addPersonChip(label) {
    const match = allPeople.find((p) => p.label.toLowerCase() === label.toLowerCase());
    if (!match) return;
    if (peopleChips.some((p) => p.cluster_id === match.cluster_id)) return;
    peopleChips.push({ cluster_id: match.cluster_id, label: match.label });
    renderPeopleChips();
  }

  const hidePersonSuggestions = attachSuggestionDropdown(
    personInput, el("f-person-suggestions"), () => allPeople.map((p) => p.label), addPersonChip
  );
  // Enter also commits the currently-typed name as a chip (if it matches a
  // real suggestion) rather than triggering Apply directly -- unlike Tag/
  // Caption's Enter bindings below, since here the point is usually to add
  // several names in a row before applying, not search on the first one.
  personInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addPersonChip(personInput.value.trim());
      personInput.value = "";
      hidePersonSuggestions();
    }
  });

  // Phase 3i: three more reuses of attachSuggestionDropdown (fifth/sixth/
  // seventh overall, per rule 7) -- unlike the People FILTER above (which
  // requires an exact match against a real known person), these three
  // deliberately accept a genuinely new typed name too, same "type freely,
  // suggestions are just quick-picks" contract the bulk-edit Location field
  // already established. Suggestions are plain labels here (not
  // disambiguated the way the filter's chips are), since typing an exact
  // existing name is enough for the add/remove routes to resolve it
  // server-side (src.people_tags.find_person_by_name/resolve_or_create_person).
  attachSuggestionDropdown(viewerPersonInput, el("viewer-person-suggestions"), () => allPeople.map((p) => p.label));
  attachSuggestionDropdown(el("bulk-person-add-input"), el("bulk-person-add-suggestions"), () => allPeople.map((p) => p.label));
  attachSuggestionDropdown(el("bulk-person-remove-input"), el("bulk-person-remove-suggestions"), () => allPeople.map((p) => p.label));

  // ---------------------------------------------------------------------
  // Stats
  // ---------------------------------------------------------------------
  async function refreshStats() {
    try {
      // total_photos already reflects every active filter, media_type
      // included (see review_tool.py's _build_filters/_count_matches) --
      // one call serves both the always-on topbar line and the
      // Grid-filter-improvements-batch match-count below, no extra request.
      const data = await fetchJSON(`/api/stats?${qs(currentFilterParams())}`);
      statsEl.textContent = `${data.captioned_so_far.toLocaleString()} captioned so far (library-wide) — ${data.total_photos.toLocaleString()} item(s) match current filters`;
      if (hasActiveFilters()) {
        const noun = state.filters.media_type === "photo" ? "photo"
          : state.filters.media_type === "video" ? "video"
          : "item";
        const plural = noun + (data.total_photos === 1 ? "" : "s");
        matchCountEl.textContent = `🔎 ${data.total_photos.toLocaleString()} ${plural} match your filters`;
        matchCountEl.hidden = false;
      } else {
        matchCountEl.hidden = true;
        matchCountEl.textContent = "";
      }
    } catch (e) {
      statsEl.textContent = "Stats unavailable";
    }
  }

  // ---------------------------------------------------------------------
  // Grid view
  // ---------------------------------------------------------------------
  function renderGrid() {
    gridEl.innerHTML = "";
    gridEmptyEl.hidden = state.grid.items.length > 0;
    for (const item of state.grid.items) {
      const card = document.createElement("div");
      const isSelected = state.selection.has(item.file_hash);
      // Phase 2f: a hidden item is only ever IN this list at all when
      // "Show hidden" is checked (see _build_filters' show_hidden on the
      // server) -- dim + badge it so it's obviously the hidden one, not a
      // silently-missing distinction.
      card.className = "card"
        + (item.excluded ? " card-hidden" : "")
        + (isSelected ? " card-selected" : ""); // Phase 2h Part A
      const thumb = !item.is_video
        ? `<img loading="lazy" src="/image/${item.file_hash}?max=400" alt="">`
        // Phase 2g: a real cached first-frame thumbnail when one exists
        // (has_thumbnail, from _row_to_dict) -- a small play-icon badge
        // overlaid so it's still obviously a video, not a photo. Falls back
        // to Phase 2e's generic placeholder tile for a video not yet
        // processed (or one that failed to decode) -- never a broken image,
        // since we only ever request /thumbnail/<hash> when has_thumbnail
        // is true in the first place.
        : item.has_thumbnail
        ? `<img loading="lazy" src="/thumbnail/${item.file_hash}" alt="">
           <div class="thumb-video-badge" title="Video">&#9654;</div>`
        : `<div class="thumb-video-placeholder" title="Video">&#9654;</div>`;
      card.innerHTML = `
        <div class="thumb-wrap">
          ${thumb}
          <label class="card-select" title="Select for bulk actions">
            <input type="checkbox" class="card-select-checkbox" ${isSelected ? "checked" : ""}>
          </label>
          ${item.favorited_by_me ? `<div class="thumb-favorite-badge" title="You favorited this">&#9733;</div>` : ""}
        </div>
        <div class="card-body">
          <div class="card-path" title="${escapeHtml(item.current_path)}">${escapeHtml(item.relative_path)}</div>
          <div class="card-date">${fmtDate(item.date_taken)}${item.date_source ? " · " + item.date_source : ""}</div>
          ${captionHtml(item)}
          <div class="card-tags">
            ${item.excluded ? `<span class="badge hidden-flag">Hidden</span>` : ""}
            ${(item.tags || []).slice(0, 6).map(t => `<span class="badge">${escapeHtml(t)}</span>`).join("")}
            ${item.location_name ? `<span class="badge location">📍 ${escapeHtml(item.location_name)}</span>` : ""}
          </div>
          ${peopleHtml(item)}
        </div>`;
      card.addEventListener("click", () => openViewer(item));
      // Phase 2h Part A: the checkbox (and its wrapping label, so tapping
      // the label's larger hit-area also works) must never also trigger
      // the card's own click-to-open-viewer handler above.
      const selectLabel = card.querySelector(".card-select");
      selectLabel.addEventListener("click", (e) => e.stopPropagation());
      const checkbox = card.querySelector(".card-select-checkbox");
      checkbox.addEventListener("change", () => toggleSelect(item.file_hash, checkbox.checked, card));
      gridEl.appendChild(card);
    }
    forEach(gridPrevBtnEls, (btn) => { btn.disabled = !state.grid.hasPrev; });
    forEach(gridNextBtnEls, (btn) => { btn.disabled = !state.grid.hasNext; });
    const pageText = state.grid.items.length ? `Page ${state.grid.pageNum}` : "—";
    forEach(gridPageInfoEls, (span) => { span.textContent = pageText; });
  }

  // Phase 2f: keepPageNum re-fetches whatever page is CURRENTLY on screen
  // (using the exact same after/before cursor that produced it) without
  // treating it as a forward/backward step -- used after an edit/hide/
  // un-hide so the grid reflects the change without silently jumping back
  // to page 1 while still showing a stale "Page N" label.
  async function loadGridPage({ after, before, resetPageNum, keepPageNum } = {}) {
    if (keepPageNum) {
      after = state.grid.lastAfter;
      before = state.grid.lastBefore;
    }
    const params = { ...currentFilterParams(), limit: state.pageSize };
    if (after) params.after = after;
    if (before) params.before = before;
    const data = await fetchJSON(`/api/photos?${qs(params)}`);
    state.grid.items = data.items;
    state.grid.hasNext = data.has_next;
    state.grid.hasPrev = data.has_prev;
    state.grid.firstCursor = data.items.length ? data.items[0].current_path : null;
    state.grid.lastCursor = data.items.length ? data.items[data.items.length - 1].current_path : null;
    state.grid.lastAfter = after || null;
    state.grid.lastBefore = before || null;
    if (resetPageNum) state.grid.pageNum = 1;
    else if (keepPageNum) { /* re-fetched the same page in place -- leave pageNum exactly as it was */ }
    else if (after) state.grid.pageNum += 1;
    else if (before) state.grid.pageNum = Math.max(1, state.grid.pageNum - 1);
    renderGrid();
  }

  forEach(gridNextBtnEls, (btn) => btn.addEventListener("click", () => {
    if (state.grid.hasNext) loadGridPage({ after: state.grid.lastCursor });
  }));
  forEach(gridPrevBtnEls, (btn) => btn.addEventListener("click", () => {
    if (state.grid.hasPrev) loadGridPage({ before: state.grid.firstCursor });
  }));
  el("f-pagesize").addEventListener("change", (e) => {
    state.pageSize = parseInt(e.target.value, 10);
    loadGridPage({ resetPageNum: true });
  });

  // ---------------------------------------------------------------------
  // Phase 2h Part A: bulk selection/editing
  // ---------------------------------------------------------------------
  function updateBulkBar() {
    const n = state.selection.size;
    bulkBarEl.hidden = n === 0;
    bulkCountEl.textContent = String(n);
    if (n === 0) { bulkResultEl.hidden = true; bulkResultEl.textContent = ""; }
  }

  // `card` is passed so the just-toggled checkbox's own card gets its
  // selected-outline updated immediately, without waiting for (or forcing)
  // a full grid re-render.
  function toggleSelect(fileHash, checked, card) {
    if (checked) state.selection.add(fileHash);
    else state.selection.delete(fileHash);
    if (card) card.classList.toggle("card-selected", checked);
    updateBulkBar();
  }

  el("bulk-clear").addEventListener("click", () => {
    state.selection.clear();
    updateBulkBar();
    renderGrid(); // uncheck/un-outline whatever's currently visible
  });

  function showBulkResult(data, actionLabel) {
    const failedCount = (data.failed || []).length;
    bulkResultEl.hidden = false;
    bulkResultEl.className = "bulk-result" + (failedCount ? " has-errors" : "");
    bulkResultEl.textContent = failedCount
      ? `${actionLabel}: ${data.updated} updated, ${failedCount} failed.`
      : `${actionLabel}: ${data.updated} updated.`;
  }

  // Every bulk action goes through this one call — same CSRF token the
  // single-item edit form already uses (window.REVIEW_CONFIG.csrfToken).
  // `mode` defaults server-side to "set" (apply the same value to every
  // selected item via apply_edit's existing semantics); "add_tag" is one
  // exception (union into each item's own existing tags — see
  // src/edits.py::add_tag_bulk / review_tool.py's api_bulk_edit).
  // Phase 3i: "add_person"/"remove_person" are a second exception — they
  // don't use field/value at all, just `extra` (cluster_id or name, see
  // the two handlers below), which this just spreads into the request body.
  async function postBulkEdit(field, value, mode, extra) {
    const res = await fetch("/api/bulk-edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file_hashes: Array.from(state.selection), field, value, mode, ...(extra || {}),
        csrf_token: cfg.csrfToken,
      }),
    });
    if (res.status === 401) {
      window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
      throw new Error("Unauthorized — redirecting to login");
    }
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status}).`);
    return data;
  }

  // After any bulk action: re-fetch the grid page currently on screen
  // (same keepPageNum mode Phase 2f's single-item edits already use, so a
  // bulk action doesn't silently jump the user back to page 1) and the
  // topbar stats, so caption/tag/location/hidden-count changes show up
  // immediately, same posture as a single-item edit.
  async function afterBulkAction() {
    await loadGridPage({ keepPageNum: true });
    refreshStats();
  }

  el("bulk-location-apply").addEventListener("click", async () => {
    // Real-user-feedback fix: free-text now, so any typed value (not just
    // one already in allLocations) is accepted and submitted.
    const value = bulkLocationInput.value.trim();
    if (!value) { alert("Type a location first."); return; }
    try {
      const data = await postBulkEdit("location", value, "set");
      showBulkResult(data, "Set location");
      bulkLocationInput.value = "";
      await afterBulkAction();
    } catch (e) { alert(e.message); }
  });

  el("bulk-tag-apply").addEventListener("click", async () => {
    const input = el("bulk-tag-input");
    const tag = input.value.trim();
    if (!tag) { alert("Type a tag first."); return; }
    try {
      const data = await postBulkEdit("tags", tag, "add_tag");
      showBulkResult(data, `Add tag "${tag}"`);
      input.value = "";
      await afterBulkAction();
    } catch (e) { alert(e.message); }
  });

  el("bulk-date-apply").addEventListener("click", async () => {
    const value = el("bulk-date-input").value; // "" if left blank
    if (!value) { alert("Choose a date first."); return; }
    try {
      const data = await postBulkEdit("date_taken", value, "set");
      showBulkResult(data, "Set date taken");
      await afterBulkAction();
    } catch (e) { alert(e.message); }
  });

  el("bulk-hide-apply").addEventListener("click", async () => {
    const n = state.selection.size;
    if (!confirm(`Hide ${n} selected item(s) from the library? This is reversible via "Show hidden".`)) return;
    try {
      const data = await postBulkEdit("excluded", true, "set");
      showBulkResult(data, "Hide from library");
      await afterBulkAction();
    } catch (e) { alert(e.message); }
  });

  // Phase 3i: mass add/remove a person across every selected item. "Add"
  // resolves the typed name to an existing person (exact match against
  // allPeople) if there is one, otherwise sends the raw name so the server
  // creates a brand-new person ONCE for the whole batch (see
  // review_tool.py's api_bulk_edit) -- same "type freely" contract as the
  // bulk-edit Location field. "Remove" only makes sense for a name that
  // already exists (there's nothing to remove from one that was never
  // tagged anywhere), so an unmatched name is rejected client-side with a
  // clear message rather than silently doing nothing.
  el("bulk-person-add-apply").addEventListener("click", async () => {
    const input = el("bulk-person-add-input");
    const name = input.value.trim();
    if (!name) { alert("Type a person's name first."); return; }
    const match = allPeople.find((p) => p.label.toLowerCase() === name.toLowerCase());
    const extra = match ? { cluster_id: match.cluster_id } : { name };
    try {
      const data = await postBulkEdit(null, null, "add_person", extra);
      showBulkResult(data, `Add person "${name}"`);
      input.value = "";
      if (!match) await loadFacets(); // a brand-new person needs to show up in future suggestions/filters
      await afterBulkAction();
    } catch (e) { alert(e.message); }
  });

  el("bulk-person-remove-apply").addEventListener("click", async () => {
    const input = el("bulk-person-remove-input");
    const name = input.value.trim();
    if (!name) { alert("Type a person's name first."); return; }
    const match = allPeople.find((p) => p.label.toLowerCase() === name.toLowerCase());
    if (!match) { alert(`No known person named "${name}".`); return; }
    try {
      const data = await postBulkEdit(null, null, "remove_person", { cluster_id: match.cluster_id });
      showBulkResult(data, `Remove person "${name}"`);
      input.value = "";
      await afterBulkAction();
    } catch (e) { alert(e.message); }
  });

  // ---------------------------------------------------------------------
  // Filters
  // ---------------------------------------------------------------------
  function applyFilters() {
    state.filters.date_from = el("f-date-from").value;
    state.filters.date_to = el("f-date-to").value;
    state.filters.tag = el("f-tag").value.trim();
    state.filters.caption_kw = el("f-caption-kw").value.trim();
    // Location dropdown: the "No location" option reproduces the old
    // standalone GPS toggle's has_location=no semantics (matches both
    // "checked, nothing found" and "never checked" rows) instead of
    // sending a literal location name.
    const locValue = el("f-location").value;
    if (locValue === NO_LOCATION_VALUE) {
      state.filters.location = "";
      state.filters.has_location = "no";
    } else {
      state.filters.location = locValue;
      state.filters.has_location = "";
    }
    state.filters.media_type = el("f-media-type").value;
    // Phase 3g: the committed filter value is whatever chips have already
    // been added (see addPersonChip) -- text still sitting in the input
    // but never turned into a chip is deliberately NOT included, same
    // "must exactly match a real known value, no silent partial match"
    // contract Tag already has for tags.
    state.filters.people = peopleChips.map((p) => p.cluster_id);
    state.filters.favorite_by = el("f-favorite-by").value;
    state.filters.show_hidden = el("f-show-hidden").checked;
    hideTagSuggestions();
    hidePersonSuggestions();
    loadGridPage({ resetPageNum: true });
    refreshStats();
    // The filtered set just changed size/membership -- any in-progress
    // random-order slideshow's seed/idx no longer means anything
    // meaningful against it, so start it fresh (matches "fresh shuffle
    // every time it starts" -- a filter change is effectively a restart).
    if (state.viewer.open && state.viewer.order === "random") startRandomOrder();
    else { state.viewer.randomSeed = null; state.viewer.randomIdx = 0; }
  }
  el("f-apply").addEventListener("click", applyFilters);
  // Note: "f-person" is deliberately NOT in this list -- its own keydown
  // handler above (addPersonChip) handles Enter, since here Enter commits
  // a chip rather than applying the filters directly.
  for (const id of ["f-tag", "f-caption-kw"]) {
    el(id).addEventListener("keydown", (e) => { if (e.key === "Enter") applyFilters(); });
  }
  el("f-reset").addEventListener("click", () => {
    el("f-date-from").value = "";
    el("f-date-to").value = "";
    el("f-tag").value = "";
    el("f-caption-kw").value = "";
    el("f-location").value = "";
    el("f-media-type").value = "";
    el("f-person").value = "";
    peopleChips = []; // Phase 3g
    renderPeopleChips();
    el("f-favorite-by").value = "";
    el("f-show-hidden").checked = false;
    applyFilters();
  });

  // ---------------------------------------------------------------------
  // Viewer / slideshow
  // ---------------------------------------------------------------------
  function renderViewerItem(item) {
    state.viewer.current = item;
    viewerNoMore.hidden = true;
    closeEditForm(); // Phase 2f: never leave a stale edit form open across a nav step
    if (!item) return;
    // Phase 2e: the viewer plays actual video via <video>, switching away
    // from the <img> used for photos -- always pause+clear the video
    // element when it's not the one showing, so a paused/still-loading
    // video doesn't keep decoding/downloading in the background while a
    // photo (or a different video) is on screen.
    if (item.is_video) {
      viewerImg.hidden = true;
      viewerImg.removeAttribute("src");
      viewerVideo.hidden = false;
      viewerVideo.src = `/video/${item.file_hash}`;
    } else {
      viewerVideo.pause();
      viewerVideo.removeAttribute("src");
      viewerVideo.load();
      viewerVideo.hidden = true;
      viewerImg.hidden = false;
      viewerImg.src = `/image/${item.file_hash}?max=1800`;
    }
    viewerImg.alt = item.relative_path;
    viewerPath.textContent = item.relative_path;
    viewerPath.title = item.current_path;
    viewerDate.textContent = `${fmtDate(item.date_taken)}${item.date_source ? " (source: " + item.date_source + ")" : ""}`;
    viewerCaption.innerHTML = item.captioned
      ? escapeHtml(item.caption || "")
      : (item.is_video
          ? `<span class="placeholder">No caption</span>`
          : `<span class="placeholder">Not yet captioned — the background Phase 2 run hasn't reached this photo yet.</span>`);
    viewerTags.innerHTML = (item.tags && item.tags.length)
      ? item.tags.map(t => `<span class="badge">${escapeHtml(t)}</span>`).join(" ")
      : `<span class="placeholder">—</span>`;
    viewerLocation.innerHTML = item.location_name
      ? `📍 ${escapeHtml(item.location_name)}`
      : (item.gps_checked
          ? `<span class="placeholder">No location available</span>`
          : `<span class="placeholder">Not yet checked for GPS data</span>`);
    viewerPeople.innerHTML = renderPeopleList(item.people);

    // Phase 3h
    viewerFavoriteBtn.textContent = item.favorited_by_me ? "★ Favorited" : "☆ Favorite";
    viewerFavoriteBtn.classList.toggle("is-favorited", Boolean(item.favorited_by_me));
    viewerFavoritedBy.textContent = (item.favorited_by && item.favorited_by.length)
      ? `Favorited by: ${item.favorited_by.join(", ")}`
      : "Not favorited by anyone yet.";

    // Phase 2f: an uncaptioned item (any not-yet-captioned photo, or ANY
    // video — which never gets an automated caption at all) shows "+ Add
    // caption" instead of a bare pencil, since there's nothing to "edit"
    // yet — same underlying mechanism either way, just starting from empty.
    if (captionEditBtn) captionEditBtn.textContent = item.captioned ? "✎" : "+ Add caption";

    hideBtn.textContent = item.excluded ? "Un-hide from library" : "🚫 Hide from library";
    hideBtn.classList.toggle("is-hidden", Boolean(item.excluded));
  }

  function openViewer(item) {
    state.viewer.open = true;
    viewerEl.hidden = false;
    // A fresh grid click is a deliberate jump to *this* photo -- always
    // show it, regardless of the sticky order-mode preference. Reset the
    // random walk so the next Next/Play under random order starts a new
    // shuffle from idx 0 rather than resuming mid-walk from wherever a
    // previous viewer session left off (which would land on an unrelated
    // photo with no connection to the one just clicked).
    state.viewer.randomSeed = null;
    state.viewer.randomIdx = 0;
    // Phase 2h Part B: collapsed by default every time the viewer is
    // (re)opened fresh from the grid -- on desktop/tablet this class does
    // nothing at all (see review.css, scoped to <=480px), so this is a
    // no-op there.
    setInfoExpanded(false);
    renderViewerItem(item);
  }

  // Phase 2h Part B: phone-width-only info-panel show/hide. A no-op in
  // effect above 480px wide -- the CSS rule this class depends on only
  // exists inside that media query (see review.css) -- so this never
  // touches desktop/iPad layout.
  function setInfoExpanded(expanded) {
    viewerPanelEl.classList.toggle("info-expanded", expanded);
    viewerInfoToggle.textContent = expanded ? "✕ Hide info" : "ℹ Info";
  }
  viewerInfoToggle.addEventListener("click", () => {
    setInfoExpanded(!viewerPanelEl.classList.contains("info-expanded"));
  });

  function closeViewer() {
    stopSlideshow();
    viewerVideo.pause();
    viewerVideo.removeAttribute("src");
    viewerVideo.load();
    state.viewer.open = false;
    viewerEl.hidden = true;
  }
  el("viewer-close").addEventListener("click", closeViewer);
  // Bug fix (see TODO.md "Editing a field in the viewer somehow ACTIVATES
  // the slideshow"): this listener is on `document`, so any keystroke typed
  // into the viewer's own edit form (#viewer-edit-input) or interval field
  // bubbles straight up to it -- the viewer stays "open" (state.viewer.open)
  // the whole time an edit is in progress, since editing happens inside the
  // viewer panel, not a separate dialog. A space typed while entering e.g.
  // "New York" or "United States" into the Location field was matching the
  // ' ' case below and calling toggleSlideshow() -- starting the slideshow
  // from a dead stop mid-edit, not just failing to pause an already-running
  // one. Fixed at the source: bail out of every shortcut here (not just
  // Space) whenever the key actually landed on an editable element, so this
  // can't happen for Escape/ArrowLeft/ArrowRight either (e.g. arrow keys
  // moving the text cursor inside the edit textarea would otherwise also
  // navigate the viewer away mid-edit). Checked via e.target, not the
  // `editingField` flag alone, so it also covers the interval-seconds
  // number input and any future editable control added inside the viewer.
  function isEditableTarget(target) {
    if (!target) return false;
    const tag = target.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
  }
  document.addEventListener("keydown", (e) => {
    if (!state.viewer.open) return;
    if (isEditableTarget(e.target)) return;
    if (e.key === "Escape") closeViewer();
    else if (e.key === "ArrowRight") {
      if (state.viewer.order === "random") stepRandomNav("next", { manual: true });
      else stepViewer("next", { manual: true });
    } else if (e.key === "ArrowLeft") {
      if (state.viewer.order === "random") stepRandomNav("prev", { manual: true });
      else stepViewer("prev", { manual: true });
    } else if (e.key === " ") { e.preventDefault(); toggleSlideshow(); }
  });

  async function stepViewer(dir, { manual = false } = {}) {
    if (manual) pauseSlideshow();
    const cursor = state.viewer.current ? state.viewer.current.current_path : "";
    const params = { ...currentFilterParams(), dir };
    if (cursor) params.cursor = cursor;
    const data = await fetchJSON(`/api/nav?${qs(params)}`);
    if (data.item === null) {
      viewerNoMore.hidden = false;
      viewerNoMore.textContent = dir === "next" ? "Reached the end of matching photos." : "Reached the beginning of matching photos.";
      pauseSlideshow();
      return;
    }
    renderViewerItem(data.item);
  }

  // ---------------------------------------------------------------------
  // Random navigation (Phase 2c)
  // ---------------------------------------------------------------------

  // One-off "surprise me" jump, independent of the order toggle below.
  async function jumpRandom() {
    pauseSlideshow();
    const data = await fetchJSON(`/api/random?${qs(currentFilterParams())}`);
    if (data.item === null) {
      viewerNoMore.hidden = false;
      viewerNoMore.textContent = "No photos match the current filters.";
      return;
    }
    renderViewerItem(data.item);
  }
  viewerRandomBtn.addEventListener("click", jumpRandom);

  // Fetches idx 0 of a brand-new random-order seed and shows it -- used
  // both when the order toggle is switched to Random and when the active
  // filters change while it's already selected (see applyFilters above).
  async function startRandomOrder() {
    state.viewer.randomSeed = mintSeed();
    state.viewer.randomIdx = 0;
    const params = { ...currentFilterParams(), mode: "random", seed: state.viewer.randomSeed, idx: 0 };
    const data = await fetchJSON(`/api/nav?${qs(params)}`);
    viewerNoMore.hidden = true;
    if (data.item === null) {
      viewerNoMore.hidden = false;
      viewerNoMore.textContent = "No photos match the current filters.";
      return;
    }
    renderViewerItem(data.item);
  }

  // Step the random-order sequence by +1/-1. Unlike stepViewer, "prev" is
  // just idx-1 -- a pure function of the seed, so no server-side history
  // is needed to step backward through the same shuffle.
  async function stepRandomNav(dir, { manual = false } = {}) {
    if (manual) pauseSlideshow();
    if (state.viewer.randomSeed === null) return startRandomOrder();
    state.viewer.randomIdx = dir === "next"
      ? state.viewer.randomIdx + 1
      : Math.max(0, state.viewer.randomIdx - 1);
    const params = {
      ...currentFilterParams(), mode: "random",
      seed: state.viewer.randomSeed, idx: state.viewer.randomIdx,
    };
    const data = await fetchJSON(`/api/nav?${qs(params)}`);
    if (data.item === null) {
      viewerNoMore.hidden = false;
      viewerNoMore.textContent = "Reached the end of the shuffled order — no more matching photos.";
      pauseSlideshow();
      return;
    }
    renderViewerItem(data.item);
  }

  viewerOrderSelect.addEventListener("change", (e) => {
    state.viewer.order = e.target.value;
    if (state.viewer.order === "random") startRandomOrder();
  });

  el("viewer-next").addEventListener("click", () => {
    if (state.viewer.order === "random") stepRandomNav("next", { manual: true });
    else stepViewer("next", { manual: true });
  });
  el("viewer-prev").addEventListener("click", () => {
    if (state.viewer.order === "random") stepRandomNav("prev", { manual: true });
    else stepViewer("prev", { manual: true });
  });

  function autoStep() {
    if (state.viewer.order === "random") stepRandomNav("next");
    else stepViewer("next");
  }

  function startSlideshow() {
    state.viewer.playing = true;
    viewerPlayPause.textContent = "⏸ Pause";
    viewerPlayPause.classList.add("playing");
    clearInterval(state.viewer.timer);
    state.viewer.timer = setInterval(autoStep, state.viewer.intervalMs);
  }
  function pauseSlideshow() {
    state.viewer.playing = false;
    viewerPlayPause.textContent = "▶ Play";
    viewerPlayPause.classList.remove("playing");
    clearInterval(state.viewer.timer);
    state.viewer.timer = null;
  }
  function stopSlideshow() { pauseSlideshow(); }
  function toggleSlideshow() {
    if (state.viewer.playing) pauseSlideshow();
    else startSlideshow();
  }
  viewerPlayPause.addEventListener("click", toggleSlideshow);

  viewerInterval.addEventListener("change", () => {
    const secs = Math.max(0.5, parseFloat(viewerInterval.value) || cfg.slideshowSeconds);
    state.viewer.intervalMs = secs * 1000;
    try { localStorage.setItem("review_slideshow_seconds", String(secs)); } catch (e) { /* ignore */ }
    if (state.viewer.playing) startSlideshow(); // restart with the new interval immediately
  });

  // ---------------------------------------------------------------------
  // Phase 2f: editable metadata (caption / tags / location / date taken)
  // ---------------------------------------------------------------------
  const EDIT_FIELD_LABELS = {
    caption: "Caption",
    tags: "Tags (comma-separated)",
    location: "Location",
    date_taken: "Date taken (YYYY-MM-DD, or a full ISO timestamp)",
  };
  // Only date_taken gets an explicit caveat -- per spec, editing it must be
  // clearly stated as changing only this tool's DB record, never the file.
  const EDIT_FIELD_HINTS = {
    date_taken: "This only changes this tool's record — it does NOT move, rename, or re-sort the actual file. Phase 1's folder placement is unaffected.",
  };
  let editingField = null;

  function valueForField(item, field) {
    if (field === "caption") return item.caption || "";
    if (field === "tags") return (item.tags || []).join(", ");
    if (field === "location") return item.location_name || "";
    if (field === "date_taken") return (item.date_taken || "").slice(0, 10);
    return "";
  }

  function openEditForm(field) {
    if (!state.viewer.current) return;
    pauseSlideshow(); // belt-and-suspenders: the real fix is the keydown
    // guard above (a slideshow that's already paused should just stay
    // paused, not get toggled back on by a stray space), but a slideshow
    // that happens to already be RUNNING when the pencil icon is clicked
    // shouldn't keep advancing frames out from under an open edit either.
    editingField = field;
    editLabelEl.textContent = EDIT_FIELD_LABELS[field] || field;
    editInputEl.value = valueForField(state.viewer.current, field);
    editHintEl.textContent = EDIT_FIELD_HINTS[field] || "";
    editHintEl.hidden = !EDIT_FIELD_HINTS[field];
    editErrorEl.hidden = true;
    editErrorEl.textContent = "";
    editFormEl.hidden = false;
    editInputEl.focus();
  }

  function closeEditForm() {
    editingField = null;
    editFormEl.hidden = true;
    editErrorEl.hidden = true;
    editErrorEl.textContent = "";
  }

  document.querySelectorAll(".edit-btn").forEach((btn) => {
    btn.addEventListener("click", () => openEditForm(btn.dataset.field));
  });
  editCancelBtn.addEventListener("click", closeEditForm);

  async function postEdit(fileHash, field, value) {
    return fetch(`/api/edit/${fileHash}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ field, value, csrf_token: cfg.csrfToken }),
    });
  }

  async function saveEdit() {
    if (!editingField || !state.viewer.current) return;
    let value = editInputEl.value;
    if (editingField === "tags") {
      value = value.split(",").map((t) => t.trim()).filter(Boolean);
    }
    editSaveBtn.disabled = true;
    editErrorEl.hidden = true;
    try {
      const res = await postEdit(state.viewer.current.file_hash, editingField, value);
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
        return;
      }
      const data = await res.json();
      if (!res.ok) {
        editErrorEl.textContent = data.error || `Save failed (${res.status}).`;
        editErrorEl.hidden = false;
        return;
      }
      renderViewerItem(data.item); // also closes the edit form, via closeEditForm() at the top
      loadGridPage({ keepPageNum: true }); // grid card for this item may show a different caption/tags/location now
      refreshStats();
    } catch (e) {
      editErrorEl.textContent = "Save failed — network error. Try again.";
      editErrorEl.hidden = false;
    } finally {
      editSaveBtn.disabled = false;
    }
  }
  editSaveBtn.addEventListener("click", saveEdit);

  // ---------------------------------------------------------------------
  // Phase 2f: "Hide from library" (reversible -- see "Show hidden" filter)
  // ---------------------------------------------------------------------
  async function toggleHidden() {
    if (!state.viewer.current) return;
    const newVal = !state.viewer.current.excluded;
    hideBtn.disabled = true;
    try {
      const res = await postEdit(state.viewer.current.file_hash, "excluded", newVal);
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
        return;
      }
      const data = await res.json();
      if (!res.ok) {
        alert(data.error || "Could not update — try again.");
        return;
      }
      if (newVal && !state.filters.show_hidden) {
        // Just hidden, and the current view doesn't show hidden items --
        // it no longer matches anything currently on screen (grid or
        // viewer), per spec. Close back to the grid rather than keep
        // displaying an item that's supposed to have disappeared.
        closeViewer();
      } else {
        renderViewerItem(data.item);
      }
      loadGridPage({ keepPageNum: true });
      refreshStats();
    } finally {
      hideBtn.disabled = false;
    }
  }
  hideBtn.addEventListener("click", toggleHidden);

  // ---------------------------------------------------------------------
  // Phase 3h: per-user favorites
  // ---------------------------------------------------------------------
  async function postFavorite(fileHash, favorited) {
    return fetch(`/api/favorite/${fileHash}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ favorited, csrf_token: cfg.csrfToken }),
    });
  }

  async function toggleFavorite() {
    if (!state.viewer.current) return;
    const newVal = !state.viewer.current.favorited_by_me;
    viewerFavoriteBtn.disabled = true;
    try {
      const res = await postFavorite(state.viewer.current.file_hash, newVal);
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
        return;
      }
      const data = await res.json();
      if (!res.ok) {
        alert(data.error || "Could not update — try again.");
        return;
      }
      // Same "close back to the grid rather than keep showing an item that
      // no longer matches" reasoning toggleHidden already applies for
      // "Hide from library": un-favoriting while viewing "Me"'s own
      // favorites means this item just fell out of the active filter.
      if (!newVal && state.filters.favorite_by === "__me__") {
        closeViewer();
      } else {
        renderViewerItem(data.item);
      }
      loadGridPage({ keepPageNum: true });
      refreshStats();
    } finally {
      viewerFavoriteBtn.disabled = false;
    }
  }
  viewerFavoriteBtn.addEventListener("click", toggleFavorite);

  // ---------------------------------------------------------------------
  // Phase 3i: manual person tagging for the CURRENT photo (add/remove) --
  // grid's mass add/remove is the bulk-edit handlers above; this is the
  // single-photo counterpart. See review_tool.py's api_photo_person.
  // ---------------------------------------------------------------------
  async function postPersonAction(fileHash, action, extra) {
    return fetch(`/api/photo/${fileHash}/person`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ...extra, csrf_token: cfg.csrfToken }),
    });
  }

  async function addPersonToCurrent() {
    if (!state.viewer.current) return;
    const name = viewerPersonInput.value.trim();
    if (!name) return;
    // Same "type freely" contract as the bulk-edit Location field: an exact
    // match against an already-known person reuses them, anything else is
    // sent as a raw name for the server to create a brand-new person from
    // (src.people_tags.resolve_or_create_person) -- tagging someone never
    // detected/named before is exactly the point of this control.
    const match = allPeople.find((p) => p.label.toLowerCase() === name.toLowerCase());
    const extra = match ? { cluster_id: match.cluster_id } : { name };
    viewerPersonAddBtn.disabled = true;
    try {
      const res = await postPersonAction(state.viewer.current.file_hash, "add", extra);
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
        return;
      }
      const data = await res.json();
      if (!res.ok) { alert(data.error || "Could not add — try again."); return; }
      renderViewerItem(data.item);
      viewerPersonInput.value = "";
      if (!match) await loadFacets(); // a brand-new person needs to show up in future suggestions/filters
      loadGridPage({ keepPageNum: true }); // this card's people badges may have changed
    } catch (e) {
      alert("Could not add — network error.");
    } finally {
      viewerPersonAddBtn.disabled = false;
    }
  }
  viewerPersonAddBtn.addEventListener("click", addPersonToCurrent);
  viewerPersonInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addPersonToCurrent(); }
  });

  async function removePersonFromCurrent(clusterId) {
    if (!state.viewer.current) return;
    try {
      const res = await postPersonAction(state.viewer.current.file_hash, "remove", { cluster_id: clusterId });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
        return;
      }
      const data = await res.json();
      if (!res.ok) { alert(data.error || "Could not remove — try again."); return; }
      renderViewerItem(data.item);
      loadGridPage({ keepPageNum: true });
    } catch (e) {
      alert("Could not remove — network error.");
    }
  }

  // ---------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------
  loadGridPage({ resetPageNum: true });
  refreshStats();
  loadFacets();
  setInterval(refreshStats, 30000); // periodic, cheap: lets "captioned so far" creep up live while the tool sits open
})();
