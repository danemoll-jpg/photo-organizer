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
 */
(() => {
  const cfg = window.REVIEW_CONFIG;

  const state = {
    filters: { date_from: "", date_to: "", folder: "", tag: "", caption_kw: "", location: "", has_location: "" },
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
  const gridEl = el("grid");
  const gridEmptyEl = el("grid-empty");
  const gridPageInfo = el("grid-page-info");
  const gridPrevBtn = el("grid-prev");
  const gridNextBtn = el("grid-next");

  const viewerEl = el("viewer");
  const viewerImg = el("viewer-img");
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
    return {
      date_from: state.filters.date_from,
      date_to: state.filters.date_to,
      folder: state.filters.folder,
      tag: state.filters.tag,
      caption_kw: state.filters.caption_kw,
      location: state.filters.location,
      has_location: state.filters.has_location,
    };
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
    if (!item.captioned) return `<span class="card-caption pending">Not yet captioned</span>`;
    return `<span class="card-caption">${escapeHtml(item.caption || "")}</span>`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ---------------------------------------------------------------------
  // Stats
  // ---------------------------------------------------------------------
  async function refreshStats() {
    try {
      const data = await fetchJSON(`/api/stats?${qs(currentFilterParams())}`);
      statsEl.textContent = `${data.captioned_so_far.toLocaleString()} captioned so far (library-wide) — ${data.total_photos.toLocaleString()} photo(s) match current filters`;
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
      card.innerHTML = `
        <div class="thumb-wrap"><img loading="lazy" src="/image/${item.file_hash}?max=400" alt=""></div>
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
    gridPrevBtn.disabled = !state.grid.hasPrev;
    gridNextBtn.disabled = !state.grid.hasNext;
    gridPageInfo.textContent = state.grid.items.length ? `Page ${state.grid.pageNum}` : "—";
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

  el("grid-next").addEventListener("click", () => {
    if (state.grid.hasNext) loadGridPage({ after: state.grid.lastCursor });
  });
  el("grid-prev").addEventListener("click", () => {
    if (state.grid.hasPrev) loadGridPage({ before: state.grid.firstCursor });
  });
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
    state.filters.folder = el("f-folder").value.trim();
    state.filters.tag = el("f-tag").value.trim();
    state.filters.caption_kw = el("f-caption-kw").value.trim();
    state.filters.location = el("f-location").value.trim();
    state.filters.has_location = el("f-has-location").value;
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
  for (const id of ["f-folder", "f-tag", "f-caption-kw", "f-location"]) {
    el(id).addEventListener("keydown", (e) => { if (e.key === "Enter") applyFilters(); });
  }
  el("f-reset").addEventListener("click", () => {
    el("f-date-from").value = "";
    el("f-date-to").value = "";
    el("f-folder").value = "";
    el("f-tag").value = "";
    el("f-caption-kw").value = "";
    el("f-location").value = "";
    el("f-has-location").value = "";
    applyFilters();
  });

  // ---------------------------------------------------------------------
  // Viewer / slideshow
  // ---------------------------------------------------------------------
  function renderViewerItem(item) {
    state.viewer.current = item;
    viewerNoMore.hidden = true;
    if (!item) return;
    viewerImg.src = `/image/${item.file_hash}?max=1800`;
    viewerImg.alt = item.relative_path;
    viewerPath.textContent = item.relative_path;
    viewerPath.title = item.current_path;
    viewerDate.textContent = `${fmtDate(item.date_taken)}${item.date_source ? " (source: " + item.date_source + ")" : ""}`;
    viewerCaption.innerHTML = item.captioned
      ? escapeHtml(item.caption || "")
      : `<span class="placeholder">Not yet captioned — the background Phase 2 run hasn't reached this photo yet.</span>`;
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
  setInterval(refreshStats, 30000); // periodic, cheap: lets "captioned so far" creep up live while the tool sits open
})();
