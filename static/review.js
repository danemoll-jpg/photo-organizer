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
 *     dropdown (see renderTagSuggestions/hideTagSuggestions below) --
 *     <datalist>'s suggestion popup never renders at all on WebKit/iOS (a
 *     real platform limitation: the exact-match filtering itself always
 *     worked fine there, only the suggestion UI was invisible). Built from
 *     plain DOM elements + our own JS filtering, so it works identically
 *     on every browser/platform rather than depending on any browser's
 *     native rendering of anything.
 *   - A visible match-count ("N item(s) match your filters"), shown only
 *     while a filter is active -- reuses the /api/stats call refreshStats()
 *     already makes (no new network request).
 *   - A media_type filter (All/Photos only/Videos only), same closed-
 *     <select> pattern as Location.
 */
(() => {
  const cfg = window.REVIEW_CONFIG;

  // Sentinel value for the Location dropdown's "No location" option —
  // translated to has_location=no rather than sent as a literal location
  // name (see currentFilterParams).
  const NO_LOCATION_VALUE = "__no_location__";

  const state = {
    filters: { date_from: "", date_to: "", tag: "", caption_kw: "", location: "", has_location: "", media_type: "" },
    pageSize: cfg.pageSize,
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

  const viewerEl = el("viewer");
  const viewerImg = el("viewer-img");
  const viewerVideo = el("viewer-video");
  const viewerNoMore = el("viewer-nomore");
  const viewerPath = el("viewer-path");
  const viewerDate = el("viewer-date");
  const viewerCaption = el("viewer-caption");
  const viewerTags = el("viewer-tags");
  const viewerLocation = el("viewer-location");
  const viewerPlayPause = el("viewer-playpause");
  const viewerInterval = el("viewer-interval");
  const viewerRandomBtn = el("viewer-random");
  const viewerOrderSelect = el("viewer-order");

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
    // Note: no "people" param — the people/faces filter is deliberately
    // inert (disabled control, Phase 3 doesn't exist yet) and never sent.
    // Note: no "folder" param — that filter was removed from the UI.
    return {
      date_from: state.filters.date_from,
      date_to: state.filters.date_to,
      tag: state.filters.tag,
      caption_kw: state.filters.caption_kw,
      location: state.filters.location,
      has_location: state.filters.has_location,
      media_type: state.filters.media_type,
    };
  }

  function hasActiveFilters() {
    const f = state.filters;
    return Boolean(f.date_from || f.date_to || f.tag || f.caption_kw || f.location || f.has_location || f.media_type);
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
  // Facets (Location dropdown + Tag suggestion data)
  // ---------------------------------------------------------------------
  let allTags = []; // populated below, filtered client-side by the Tag suggestion dropdown

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
    } catch (e) {
      // Non-fatal: filters just show "Any"/no suggestions until reloaded.
    }
  }

  // ---------------------------------------------------------------------
  // Tag filter suggestions (custom dropdown -- see module docstring for
  // why this replaced <datalist>)
  // ---------------------------------------------------------------------
  const MAX_TAG_SUGGESTIONS = 50; // plenty for a "did you mean" list; keeps a huge tag table cheap to render
  const tagInput = el("f-tag");
  const tagSuggestionsEl = el("f-tag-suggestions");

  function renderTagSuggestions() {
    const q = tagInput.value.trim().toLowerCase();
    const matches = (q ? allTags.filter((t) => t.toLowerCase().includes(q)) : allTags).slice(0, MAX_TAG_SUGGESTIONS);
    if (!matches.length) {
      hideTagSuggestions();
      return;
    }
    tagSuggestionsEl.innerHTML = matches.map((t) => `<li>${escapeHtml(t)}</li>`).join("");
    tagSuggestionsEl.hidden = false;
  }

  function hideTagSuggestions() {
    tagSuggestionsEl.hidden = true;
    tagSuggestionsEl.innerHTML = "";
  }

  tagInput.addEventListener("input", renderTagSuggestions);
  tagInput.addEventListener("focus", renderTagSuggestions);
  tagInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideTagSuggestions();
  });
  // mousedown (not click) so this fires BEFORE the input's blur handler
  // below would otherwise hide the list first -- selecting a suggestion
  // must land its value in the field, still free-text/editable afterward
  // (same as picking a <datalist> option used to), not lock it in.
  tagSuggestionsEl.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li");
    if (!li) return;
    e.preventDefault(); // don't let the input lose focus over this
    tagInput.value = li.textContent;
    hideTagSuggestions();
  });
  tagInput.addEventListener("blur", () => {
    // Short delay so the mousedown handler above still gets to run first
    // -- blur fires the instant focus leaves the input, which a mousedown
    // on the suggestion list technically does before its own handler runs.
    setTimeout(hideTagSuggestions, 150);
  });
  // Clicking anywhere else on the page should close an open dropdown too
  // (e.g. clicking straight into another filter field without going
  // through blur-then-something-else first).
  document.addEventListener("click", (e) => {
    if (e.target !== tagInput && !tagSuggestionsEl.contains(e.target)) hideTagSuggestions();
  });

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
      card.className = "card";
      const thumb = item.is_video
        // Phase 2e: no real frame-extraction thumbnail yet -- a generic
        // play-icon placeholder tile stands in for the grid (the full
        // viewer plays the actual file, see openViewer/renderViewerItem).
        ? `<div class="thumb-video-placeholder" title="Video">&#9654;</div>`
        : `<img loading="lazy" src="/image/${item.file_hash}?max=400" alt="">`;
      card.innerHTML = `
        <div class="thumb-wrap">${thumb}</div>
        <div class="card-body">
          <div class="card-path" title="${escapeHtml(item.current_path)}">${escapeHtml(item.relative_path)}</div>
          <div class="card-date">${fmtDate(item.date_taken)}${item.date_source ? " · " + item.date_source : ""}</div>
          ${captionHtml(item)}
          <div class="card-tags">
            ${(item.tags || []).slice(0, 6).map(t => `<span class="badge">${escapeHtml(t)}</span>`).join("")}
            ${item.location_name ? `<span class="badge location">📍 ${escapeHtml(item.location_name)}</span>` : ""}
          </div>
        </div>`;
      card.addEventListener("click", () => openViewer(item));
      gridEl.appendChild(card);
    }
    forEach(gridPrevBtnEls, (btn) => { btn.disabled = !state.grid.hasPrev; });
    forEach(gridNextBtnEls, (btn) => { btn.disabled = !state.grid.hasNext; });
    const pageText = state.grid.items.length ? `Page ${state.grid.pageNum}` : "—";
    forEach(gridPageInfoEls, (span) => { span.textContent = pageText; });
  }

  async function loadGridPage({ after, before, resetPageNum } = {}) {
    const params = { ...currentFilterParams(), limit: state.pageSize };
    if (after) params.after = after;
    if (before) params.before = before;
    const data = await fetchJSON(`/api/photos?${qs(params)}`);
    state.grid.items = data.items;
    state.grid.hasNext = data.has_next;
    state.grid.hasPrev = data.has_prev;
    state.grid.firstCursor = data.items.length ? data.items[0].current_path : null;
    state.grid.lastCursor = data.items.length ? data.items[data.items.length - 1].current_path : null;
    if (resetPageNum) state.grid.pageNum = 1;
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
    hideTagSuggestions();
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
    applyFilters();
  });

  // ---------------------------------------------------------------------
  // Viewer / slideshow
  // ---------------------------------------------------------------------
  function renderViewerItem(item) {
    state.viewer.current = item;
    viewerNoMore.hidden = true;
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
    renderViewerItem(item);
  }

  function closeViewer() {
    stopSlideshow();
    viewerVideo.pause();
    viewerVideo.removeAttribute("src");
    viewerVideo.load();
    state.viewer.open = false;
    viewerEl.hidden = true;
  }
  el("viewer-close").addEventListener("click", closeViewer);
  document.addEventListener("keydown", (e) => {
    if (!state.viewer.open) return;
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
  // Init
  // ---------------------------------------------------------------------
  loadGridPage({ resetPageNum: true });
  refreshStats();
  loadFacets();
  setInterval(refreshStats, 30000); // periodic, cheap: lets "captioned so far" creep up live while the tool sits open
})();
