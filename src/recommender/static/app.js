const state = {
  seeds: [],
  pair: null,
  searchResults: [],
  libraryView: "all",
  importId: null,
  importMatches: [],
  recommendationSections: {},
  recommendationCursor: null,
};

const DRAFT_SEEDS_KEY = "scene-bridge-draft-seeds";
const MIN_SEEDS = 5;
const MAX_SEEDS = 10;
const TOTAL_COMPARISONS = 10;

const elements = {
  searchForm: document.querySelector("#search-form"),
  searchResults: document.querySelector("#search-results"),
  selectedSeeds: document.querySelector("#selected-seeds"),
  startButton: document.querySelector("#start-button"),
  resetButton: document.querySelector("#reset-button"),
  pairPanel: document.querySelector("#pair-panel"),
  pairCards: document.querySelector("#pair-cards"),
  pairRound: document.querySelector("#pair-round"),
  comparisonCount: document.querySelector("#comparison-count"),
  libraryGrid: document.querySelector("#library-grid"),
  libraryViews: document.querySelector("#library-views"),
  recommendations: document.querySelector("#recommendations"),
  loadMore: document.querySelector("#load-more"),
  refreshRecommendations: document.querySelector("#refresh-recommendations"),
  contextForm: document.querySelector("#context-form"),
  status: document.querySelector("#status"),
  phaseLabel: document.querySelector("#phase-label"),
  seedCount: document.querySelector("#seed-count"),
  libraryCount: document.querySelector("#library-count"),
  eventCount: document.querySelector("#event-count"),
  tastePanel: document.querySelector("#taste-panel"),
  positiveTraits: document.querySelector("#positive-traits"),
  negativeTraits: document.querySelector("#negative-traits"),
  tasteNoteForm: document.querySelector("#taste-note-form"),
  pauseLearning: document.querySelector("#pause-learning"),
  tvtimeFile: document.querySelector("#tvtime-file"),
  tvtimeFileLabel: document.querySelector("#tvtime-file-label"),
  previewImport: document.querySelector("#preview-import"),
  importReview: document.querySelector("#import-review"),
  importMatches: document.querySelector("#import-matches"),
  commitImport: document.querySelector("#commit-import"),
};

const sectionLabels = {
  continue: "Continue",
  upcoming: "Upcoming",
  strong_matches: "Strong matches",
  adjacent_discoveries: "Adjacent discoveries",
  cross_media: "Cross-media discoveries",
  rewatch: "Rewatch",
};

function setStatus(message, kind = "info") {
  elements.status.hidden = !message;
  elements.status.dataset.kind = kind;
  elements.status.textContent = message;
}

async function requestJson(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(url, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error?.message || "The local service returned an error.");
  return payload;
}

function clearElement(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
}

function saveDraftSeeds() {
  try {
    localStorage.setItem(DRAFT_SEEDS_KEY, JSON.stringify(state.seeds));
  } catch {
    // Local storage is optional. SQLite remains authoritative after submission.
  }
}

function clearDraftSeeds() {
  try {
    localStorage.removeItem(DRAFT_SEEDS_KEY);
  } catch {
    // Keep the in-memory profile usable when storage is blocked.
  }
}

function restoreDraftSeeds() {
  try {
    const parsed = JSON.parse(localStorage.getItem(DRAFT_SEEDS_KEY) || "[]");
    if (!Array.isArray(parsed)) return;
    state.seeds = parsed
      .filter((item) => item && ["movie", "tv"].includes(item.media_type) && Number.isInteger(item.tmdb_id) && item.title)
      .slice(0, MAX_SEEDS);
  } catch {
    state.seeds = [];
  }
}

function createPoster(item, className) {
  if (item.poster_url) {
    const image = document.createElement("img");
    image.className = className;
    image.src = item.poster_url;
    image.alt = `${item.title} poster`;
    image.loading = "lazy";
    image.addEventListener("error", () => {
      const fallback = document.createElement("div");
      fallback.className = "poster-fallback";
      fallback.textContent = item.title;
      image.replaceWith(fallback);
    }, { once: true });
    return image;
  }
  const fallback = document.createElement("div");
  fallback.className = "poster-fallback";
  fallback.textContent = item.title;
  return fallback;
}

function createMeta(item) {
  const meta = document.createElement("p");
  meta.className = "item-meta";
  meta.textContent = `${item.media_type === "movie" ? "Movie" : "TV show"}${item.year ? ` / ${item.year}` : ""}`;
  return meta;
}

function createTitle(item) {
  const title = document.createElement("h3");
  title.className = "item-title";
  title.textContent = item.title;
  title.title = item.title;
  return title;
}

function actionButton(label, handler, className = "item-action") {
  const button = document.createElement("button");
  button.className = className;
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function createSearchCard(item) {
  const card = document.createElement("article");
  card.className = "poster-card";
  card.append(createPoster(item, "poster-image"));
  const body = document.createElement("div");
  body.className = "poster-card-body";
  body.append(createTitle(item), createMeta(item));

  const isSelected = state.seeds.some((seed) => seed.key === item.key);
  const addButton = actionButton(isSelected ? "Added to taste set" : "Add to taste set", () => addSeed(item));
  if (isSelected) addButton.classList.add("is-added");
  addButton.disabled = isSelected || state.seeds.length >= MAX_SEEDS;
  addButton.setAttribute("aria-pressed", String(isSelected));
  body.append(addButton, actionButton("Save to library", () => updateItem(item, { status: "planned", saved: true })));
  card.append(body);
  return card;
}

function renderSearchResults(items) {
  state.searchResults = items;
  clearElement(elements.searchResults);
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = "No titles matched that search.";
    elements.searchResults.append(empty);
    return;
  }
  items.forEach((item) => elements.searchResults.append(createSearchCard(item)));
}

function renderSeeds() {
  clearElement(elements.selectedSeeds);
  if (!state.seeds.length) {
    const empty = document.createElement("span");
    empty.className = "empty-note";
    empty.textContent = "Nothing selected yet.";
    elements.selectedSeeds.append(empty);
  } else {
    state.seeds.forEach((item) => {
      const chip = document.createElement("div");
      chip.className = "selected-seed";
      const label = document.createElement("span");
      label.textContent = item.title;
      const remove = document.createElement("button");
      remove.className = "remove-seed";
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove ${item.title}`);
      remove.textContent = "×";
      remove.addEventListener("click", () => {
        state.seeds = state.seeds.filter((seed) => seed.key !== item.key);
        saveDraftSeeds();
        renderSeeds();
        renderSearchResults(state.searchResults);
      });
      chip.append(label, remove);
      elements.selectedSeeds.append(chip);
    });
  }
  elements.seedCount.textContent = String(state.seeds.length);
  elements.startButton.disabled = state.seeds.length < MIN_SEEDS || state.seeds.length > MAX_SEEDS;
}

function addSeed(item) {
  if (state.seeds.length >= MAX_SEEDS || state.seeds.some((seed) => seed.key === item.key)) return;
  state.seeds.push(item);
  saveDraftSeeds();
  renderSeeds();
  renderSearchResults(state.searchResults);
  setStatus(`${item.title} added to your taste set.`);
}

function createPairCard(item) {
  const card = document.createElement("article");
  card.className = "pair-card";
  card.append(createPoster(item, "pair-image"));
  const copy = document.createElement("div");
  copy.className = "pair-card-copy";
  const heading = document.createElement("div");
  heading.append(createTitle(item), createMeta(item));
  copy.append(heading, actionButton(`Choose ${item.title}`, () => submitPair(item.key), "choose-button"));
  card.append(copy);
  return card;
}

function renderPair(pair, shouldScroll = true) {
  state.pair = pair;
  elements.pairPanel.hidden = false;
  if (elements.comparisonCount) elements.comparisonCount.textContent = String(pair.round);
  elements.pairRound.textContent = pair.total_rounds === null
    ? `Comparison ${pair.round} / ongoing`
    : `Comparison ${pair.round} of ${pair.total_rounds}`;
  clearElement(elements.pairCards);
  elements.pairCards.append(createPairCard(pair.left), createPairCard(pair.right));
  elements.phaseLabel.textContent = "Teach the profile";
  if (shouldScroll) elements.pairPanel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderComplete() {
  if (elements.comparisonCount) elements.comparisonCount.textContent = String(TOTAL_COMPARISONS);
  elements.phaseLabel.textContent = "Your profile is learning";
  elements.pairRound.textContent = "Initial comparison pass complete";
  elements.pairPanel.hidden = false;
  clearElement(elements.pairCards);
  const note = document.createElement("p");
  note.className = "empty-note pair-complete";
  note.textContent = "The comparison pass is complete. Keep using the library to sharpen the next feed.";
  elements.pairCards.append(note, actionButton("Teach me another pair", loadAdaptivePair, "button button-quiet"));
}

async function loadAdaptivePair() {
  try {
    const pair = await requestJson("/api/learning/compare");
    renderPair(pair);
    setStatus("This pair was chosen because your current model is least certain here.");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function beginComparisons() {
  elements.startButton.disabled = true;
  setStatus("Saving your taste set...");
  try {
    await requestJson("/api/profile/seeds", {
      method: "POST",
      body: JSON.stringify({ items: state.seeds.map(({ media_type, tmdb_id }) => ({ media_type, tmdb_id })) }),
    });
    clearDraftSeeds();
    const pair = await requestJson("/api/profile/pair");
    if (pair.complete) renderComplete();
    else renderPair(pair);
    setStatus("Choose the title you would pick first.");
    await refreshProfileData();
  } catch (error) {
    setStatus(error.message, "error");
    elements.startButton.disabled = false;
  }
}

async function submitPair(winnerKey) {
  if (!state.pair) return;
  const buttons = elements.pairCards.querySelectorAll("button");
  buttons.forEach((button) => { button.disabled = true; });
  setStatus("Updating your profile...");
  try {
    const next = await requestJson("/api/profile/pair", {
      method: "POST",
      body: JSON.stringify({ pair_id: state.pair.pair_id, winner_key: winnerKey }),
    });
    if (next.complete) renderComplete();
    else renderPair(next);
    setStatus(next.complete ? "Your first learning pass is complete." : "Next comparison loaded.");
    await refreshProfileData();
  } catch (error) {
    setStatus(error.message, "error");
    buttons.forEach((button) => { button.disabled = false; });
  }
}

function createReasonList(item) {
  const reasons = document.createElement("div");
  reasons.className = "reason-list";
  (item.reasons || []).forEach((reason) => {
    const tag = document.createElement("span");
    tag.className = "reason-tag";
    tag.textContent = reason;
    reasons.append(tag);
  });
  return reasons;
}

function createRecommendationCard(item) {
  const card = document.createElement("article");
  card.className = "result-card recommendation-card";
  card.append(createPoster(item, "result-image"));
  const body = document.createElement("div");
  body.className = "result-card-body";
  body.append(createTitle(item), createMeta(item), createReasonList(item));
  const actions = document.createElement("div");
  actions.className = "card-actions";
  actions.append(
    actionButton("Save", () => updateItem(item, { status: "planned", saved: true })),
    actionButton("Watched", () => updateItem(item, { status: "watched" })),
    actionButton("Like", () => updateItem(item, { sentiment: "like" })),
    actionButton("Not for me", () => updateItem(item, { sentiment: "not_for_me" })),
    actionButton("Hide", () => updateItem(item, { hidden: true })),
  );
  body.append(actions);
  card.append(body);
  return card;
}

function renderRecommendations() {
  clearElement(elements.recommendations);
  const order = ["continue", "upcoming", "strong_matches", "adjacent_discoveries", "cross_media", "rewatch"];
  const available = order.filter((section) => state.recommendationSections[section]?.length);
  if (!available.length) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = "Add a few titles or import your history to start the feed.";
    elements.recommendations.append(empty);
    return;
  }
  available.forEach((section) => {
    const group = document.createElement("section");
    group.className = "recommendation-section";
    const heading = document.createElement("div");
    heading.className = "subsection-heading";
    const title = document.createElement("h3");
    title.textContent = sectionLabels[section];
    const count = document.createElement("span");
    count.className = "subsection-count";
    count.textContent = `${state.recommendationSections[section].length} here`;
    heading.append(title, count);
    const grid = document.createElement("div");
    grid.className = "results-grid";
    state.recommendationSections[section].forEach((item) => grid.append(createRecommendationCard(item)));
    group.append(heading, grid);
    elements.recommendations.append(group);
  });
}

function sessionContext() {
  const context = {};
  const time = document.querySelector("#context-time").value;
  const mood = document.querySelector("#context-mood").value.trim();
  const company = document.querySelector("#context-company").value;
  if (time) context.minutes = Number(time);
  if (mood) context.mood = mood;
  if (company) context.company = company;
  return context;
}

async function loadRecommendations(append = false) {
  const cursor = append && state.recommendationCursor !== null ? state.recommendationCursor : 0;
  if (!append) {
    state.recommendationSections = {};
    clearElement(elements.recommendations);
    const loading = document.createElement("p");
    loading.className = "empty-note";
    loading.textContent = "Building the next watch list...";
    elements.recommendations.append(loading);
  }
  try {
    const context = encodeURIComponent(JSON.stringify(sessionContext()));
    const payload = await requestJson(`/api/recommendations?limit=20&cursor=${cursor}&context=${context}`);
    const sections = payload.sections || {};
    Object.entries(sections).forEach(([section, items]) => {
      state.recommendationSections[section] = [...(state.recommendationSections[section] || []), ...items];
    });
    state.recommendationCursor = payload.next_cursor;
    elements.loadMore.hidden = payload.next_cursor === null;
    renderRecommendations();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function createLibraryCard(item) {
  const card = document.createElement("article");
  card.className = "library-card";
  card.append(createPoster(item, "library-image"));
  const body = document.createElement("div");
  body.className = "library-card-body";
  body.append(createTitle(item), createMeta(item));
  const library = item.library || {};

  const controls = document.createElement("div");
  controls.className = "library-controls";
  const statusLabel = document.createElement("label");
  statusLabel.textContent = "Status";
  const status = document.createElement("select");
  ["planned", "watching", "watched", "dropped"].forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value.replace("_", " ");
    option.selected = value === library.status;
    status.append(option);
  });
  status.addEventListener("change", () => updateItem(item, { status: status.value }));
  statusLabel.append(status);
  controls.append(statusLabel);

  const sentimentLabel = document.createElement("label");
  sentimentLabel.textContent = "Sentiment";
  const sentiment = document.createElement("select");
  ["neutral", "love", "like", "dislike", "not_for_me"].forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value.replace("_", " ");
    option.selected = value === library.sentiment;
    sentiment.append(option);
  });
  sentiment.addEventListener("change", () => updateItem(item, { sentiment: sentiment.value }));
  sentimentLabel.append(sentiment);
  controls.append(sentimentLabel);
  body.append(controls);

  if (item.upcoming_episode) {
    const next = document.createElement("p");
    next.className = "episode-note";
    next.textContent = `S${item.upcoming_episode.season_number} E${item.upcoming_episode.episode_number} ${item.upcoming_episode.name || "upcoming"} / ${item.upcoming_episode.air_date || "date unknown"}`;
    body.append(next);
  }
  const actions = document.createElement("div");
  actions.className = "card-actions";
  actions.append(actionButton(library.saved ? "Saved" : "Save", () => updateItem(item, { saved: !library.saved })));
  actions.append(actionButton(library.hidden ? "Unhide" : "Hide", () => updateItem(item, { hidden: !library.hidden })));
  body.append(actions);
  card.append(body);
  return card;
}

function renderLibrary(items) {
  clearElement(elements.libraryGrid);
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = state.libraryView === "all" ? "Your library will appear here after you save or import a title." : "Nothing is in this view yet.";
    elements.libraryGrid.append(empty);
    return;
  }
  items.forEach((item) => elements.libraryGrid.append(createLibraryCard(item)));
}

async function loadLibrary(view = state.libraryView) {
  state.libraryView = view;
  try {
    const payload = await requestJson(`/api/library?view=${encodeURIComponent(view)}&limit=100`);
    renderLibrary(payload.items);
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function updateItem(item, changes) {
  try {
    await requestJson(`/api/library/items/${item.media_type}/${item.tmdb_id}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    });
    setStatus(`${item.title} updated. The next feed will use that signal.`);
    await refreshProfileData();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function renderTraits(target, values, emptyText) {
  clearElement(target);
  if (!values?.length) {
    const empty = document.createElement("span");
    empty.className = "empty-note";
    empty.textContent = emptyText;
    target.append(empty);
    return;
  }
  values.forEach((value) => {
    const tag = document.createElement("span");
    tag.className = "trait-tag";
    tag.textContent = value;
    target.append(tag);
  });
}

async function loadTasteProfile() {
  try {
    const payload = await requestJson("/api/profile/taste");
    renderTraits(elements.positiveTraits, payload.traits.positive, "No positive signals yet.");
    renderTraits(elements.negativeTraits, payload.traits.negative, "No negative signals yet.");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function refreshProfileData() {
  const profile = await requestJson("/api/profile");
  elements.seedCount.textContent = String(profile.items.length);
  elements.libraryCount.textContent = String(profile.library_count || 0);
  elements.eventCount.textContent = String(profile.event_count || 0);
  elements.pauseLearning.textContent = profile.learning_paused ? "Resume learning" : "Pause learning";
  await Promise.all([loadLibrary(), loadTasteProfile()]);
  if (profile.has_profile) {
    await loadRecommendations();
  } else {
    state.recommendationSections = {};
    state.recommendationCursor = null;
    elements.loadMore.hidden = true;
    renderRecommendations();
  }
}

async function searchCatalog(event) {
  event.preventDefault();
  const form = new FormData(elements.searchForm);
  const query = String(form.get("q") || "").trim();
  const mediaType = String(form.get("media_type") || "all");
  if (!query) return;
  setStatus("Searching the catalog...");
  try {
    const payload = await requestJson(`/api/search?q=${encodeURIComponent(query)}&media_type=${encodeURIComponent(mediaType)}`);
    renderSearchResults(payload.items);
    setStatus(payload.items.length ? "Choose a title to add to your taste set or library." : "No titles matched that search.");
  } catch (error) {
    renderSearchResults([]);
    setStatus(error.message, "error");
  }
}

function renderImportMatches(matches) {
  state.importMatches = matches.map((match) => ({ ...match }));
  clearElement(elements.importMatches);
  state.importMatches.forEach((match) => {
    const row = document.createElement("article");
    row.className = "import-match";
    const copy = document.createElement("div");
    const title = document.createElement("h4");
    title.textContent = `${match.title}${match.year ? ` / ${match.year}` : ""}`;
    const meta = document.createElement("p");
    meta.className = "item-meta";
    meta.textContent = `${match.media_type === "movie" ? "Movie" : "TV show"} / ${(match.confidence * 100).toFixed(0)}% match`;
    copy.append(title, meta);
    const select = document.createElement("select");
    select.setAttribute("aria-label", `TMDB match for ${match.title}`);
    const skip = document.createElement("option");
    skip.value = "";
    skip.textContent = "Skip this title";
    select.append(skip);
    match.candidate_keys.forEach((key) => {
      const option = document.createElement("option");
      option.value = key;
      option.textContent = key;
      option.selected = key === match.selected_key;
      select.append(option);
    });
    select.addEventListener("change", () => {
      match.selected_key = select.value || null;
      match.status = select.value ? "accepted" : "skipped";
    });
    const stateLabel = document.createElement("span");
    stateLabel.className = "import-state";
    stateLabel.textContent = match.status === "accepted" ? "Ready" : "Needs review";
    row.append(copy, select, stateLabel);
    elements.importMatches.append(row);
  });
  elements.importReview.hidden = !matches.length;
}

async function previewImport() {
  const file = elements.tvtimeFile.files[0];
  if (!file) {
    setStatus("Choose a TV Time ZIP or CSV first.", "error");
    return;
  }
  elements.previewImport.disabled = true;
  setStatus("Reading the export and matching titles...");
  try {
    const form = new FormData();
    form.append("file", file);
    const payload = await requestJson("/api/import/tvtime/preview", { method: "POST", body: form });
    state.importId = payload.import_id;
    renderImportMatches(payload.matches);
    setStatus(`${payload.summary.records} records found. Review the queue before adding them.`);
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    elements.previewImport.disabled = false;
  }
}

async function commitImport() {
  if (!state.importId) return;
  elements.commitImport.disabled = true;
  try {
    const matches = state.importMatches.map((match) => ({
      match_id: match.id,
      selected_key: match.selected_key,
      status: match.selected_key ? "accepted" : "skipped",
    }));
    const payload = await requestJson(`/api/import/tvtime/commit?import_id=${encodeURIComponent(state.importId)}`, {
      method: "POST",
      body: JSON.stringify({ matches }),
    });
    elements.importReview.hidden = true;
    setStatus(`${payload.summary.accepted} titles added to your local library.`);
    await refreshProfileData();
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    elements.commitImport.disabled = false;
  }
}

async function saveTasteNote(event) {
  event.preventDefault();
  const input = document.querySelector("#taste-note");
  const polarity = document.querySelector("#taste-note-polarity").value;
  try {
    await requestJson("/api/profile/taste-notes", {
      method: "POST",
      body: JSON.stringify({ text: input.value.trim(), polarity }),
    });
    input.value = "";
    setStatus("Personal taste signal saved.");
    await refreshProfileData();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function toggleLearning() {
  try {
    const profile = await requestJson("/api/profile");
    await requestJson("/api/learning/pause", {
      method: "POST",
      body: JSON.stringify({ paused: !profile.learning_paused }),
    });
    setStatus(profile.learning_paused ? "Learning resumed." : "Learning paused. Library changes will still be recorded.");
    await refreshProfileData();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function resetProfile() {
  if (!window.confirm("Reset the local library, taste profile, imports, and comparisons?")) return;
  try {
    await requestJson("/api/profile/reset", { method: "POST" });
    clearDraftSeeds();
    state.seeds = [];
    state.pair = null;
    state.recommendationSections = {};
    state.recommendationCursor = null;
    elements.pairPanel.hidden = true;
    renderSeeds();
    renderSearchResults([]);
    setStatus("Your local profile was reset. The catalog cache is still available.");
    await refreshProfileData();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function restoreProfile() {
  try {
    const profile = await requestJson("/api/profile");
    state.seeds = profile.items || [];
    renderSeeds();
    if (profile.has_profile) {
      elements.phaseLabel.textContent = state.seeds.length ? "Profile is learning" : "Library is learning";
      if (profile.comparison_count > 0) {
        const pair = await requestJson("/api/profile/pair");
        if (pair.complete) renderComplete();
        else renderPair(pair, false);
      }
      setStatus("Restored your saved local profile.");
    } else {
      restoreDraftSeeds();
      renderSeeds();
      if (state.seeds.length) setStatus("Restored your in-progress taste set.");
    }
    await refreshProfileData();
  } catch (error) {
    restoreDraftSeeds();
    renderSeeds();
    setStatus(state.seeds.length ? "Restored your in-progress taste set. Saved profile status is temporarily unavailable." : error.message, "error");
  }
}

elements.searchForm.addEventListener("submit", searchCatalog);
elements.startButton.addEventListener("click", beginComparisons);
elements.resetButton.addEventListener("click", resetProfile);
elements.refreshRecommendations.addEventListener("click", () => loadRecommendations());
elements.loadMore.addEventListener("click", () => loadRecommendations(true));
elements.contextForm.addEventListener("submit", (event) => {
  event.preventDefault();
  loadRecommendations();
  setStatus("Session context applied to the next feed.");
});
elements.libraryViews.addEventListener("click", (event) => {
  const button = event.target.closest("[data-library-view]");
  if (!button) return;
  document.querySelectorAll(".view-tab").forEach((tab) => {
    const active = tab === button;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  loadLibrary(button.dataset.libraryView);
});
elements.tasteNoteForm.addEventListener("submit", saveTasteNote);
elements.pauseLearning.addEventListener("click", toggleLearning);
elements.tvtimeFile.addEventListener("change", () => {
  elements.tvtimeFileLabel.textContent = elements.tvtimeFile.files[0]?.name || "Choose ZIP or CSV";
});
elements.previewImport.addEventListener("click", previewImport);
elements.commitImport.addEventListener("click", commitImport);
restoreProfile();
