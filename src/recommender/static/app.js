const state = {
  seeds: [],
  pair: null,
  searchResults: [],
};

const elements = {
  searchForm: document.querySelector("#search-form"),
  searchResults: document.querySelector("#search-results"),
  selectedSeeds: document.querySelector("#selected-seeds"),
  startButton: document.querySelector("#start-button"),
  resetButton: document.querySelector("#reset-button"),
  pairPanel: document.querySelector("#pair-panel"),
  pairCards: document.querySelector("#pair-cards"),
  pairRound: document.querySelector("#pair-round"),
  resultsPanel: document.querySelector("#results-panel"),
  recommendations: document.querySelector("#recommendations"),
  status: document.querySelector("#status"),
  phaseLabel: document.querySelector("#phase-label"),
  seedCount: document.querySelector("#seed-count"),
  comparisonCount: document.querySelector("#comparison-count"),
};

function setStatus(message, kind = "info") {
  elements.status.hidden = !message;
  elements.status.dataset.kind = kind;
  elements.status.textContent = message;
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error?.message || "The local service returned an error.");
  }
  return payload;
}

function clearElement(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
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

function createSearchCard(item) {
  const card = document.createElement("article");
  card.className = "poster-card";
  card.append(createPoster(item, "poster-image"));

  const body = document.createElement("div");
  body.className = "poster-card-body";
  body.append(createTitle(item), createMeta(item));

  const isSelected = state.seeds.some((seed) => seed.key === item.key);
  const button = document.createElement("button");
  button.className = "item-action";
  if (isSelected) button.classList.add("is-added");
  button.type = "button";
  button.textContent = isSelected ? "Added to taste set" : "Add to taste set";
  button.disabled = isSelected || state.seeds.length >= 10;
  button.setAttribute("aria-pressed", String(isSelected));
  button.addEventListener("click", () => addSeed(item));
  body.append(button);
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
        renderSeeds();
        renderSearchResults(state.searchResults);
      });
      chip.append(label, remove);
      elements.selectedSeeds.append(chip);
    });
  }
  elements.seedCount.textContent = String(state.seeds.length);
  elements.startButton.disabled = state.seeds.length !== 10;
}

function addSeed(item) {
  if (state.seeds.length >= 10 || state.seeds.some((seed) => seed.key === item.key)) return;
  state.seeds.push(item);
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
  const button = document.createElement("button");
  button.className = "choose-button";
  button.type = "button";
  button.textContent = `Choose ${item.title}`;
  button.addEventListener("click", () => submitPair(item.key));
  copy.append(heading, button);
  card.append(copy);
  return card;
}

function renderPair(pair) {
  state.pair = pair;
  elements.comparisonCount.textContent = String(pair.round);
  elements.pairRound.textContent = `Comparison ${pair.round} of ${pair.total_rounds}`;
  elements.pairPanel.hidden = false;
  clearElement(elements.pairCards);
  elements.pairCards.append(createPairCard(pair.left), createPairCard(pair.right));
  elements.phaseLabel.textContent = "Make ten preference choices";
  elements.pairPanel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderComplete() {
  elements.comparisonCount.textContent = "10";
  elements.phaseLabel.textContent = "Your list is ready";
  elements.pairRound.textContent = "Comparison round complete";
  loadRecommendations();
}

async function beginComparisons() {
  elements.startButton.disabled = true;
  setStatus("Saving your taste set...");
  try {
    await requestJson("/api/profile/seeds", {
      method: "POST",
      body: JSON.stringify({ items: state.seeds.map(({ media_type, tmdb_id }) => ({ media_type, tmdb_id })) }),
    });
    const pair = await requestJson("/api/profile/pair");
    if (pair.complete) {
      renderComplete();
    } else {
      renderPair(pair);
      setStatus("Choose the title you would pick first.");
    }
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
    if (next.complete) {
      renderComplete();
      setStatus("Your cross-media list is ready.");
    } else {
      renderPair(next);
      setStatus("Next comparison loaded.");
    }
  } catch (error) {
    setStatus(error.message, "error");
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function loadRecommendations() {
  elements.resultsPanel.hidden = false;
  clearElement(elements.recommendations);
  const loading = document.createElement("p");
  loading.className = "empty-note";
  loading.textContent = "Ranking your next ten picks...";
  elements.recommendations.append(loading);
  try {
    const payload = await requestJson("/api/recommendations?limit=10");
    clearElement(elements.recommendations);
    if (!payload.items.length) {
      const empty = document.createElement("p");
      empty.className = "empty-note";
      empty.textContent = "The catalog needs more candidates before it can make a list.";
      elements.recommendations.append(empty);
    } else {
      payload.items.forEach((item) => {
        const card = document.createElement("article");
        card.className = "result-card";
        card.append(createPoster(item, "result-image"));
        const body = document.createElement("div");
        body.className = "result-card-body";
        body.append(createTitle(item), createMeta(item));
        card.append(body);
        elements.recommendations.append(card);
      });
    }
    elements.resultsPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function searchCatalog(event) {
  event.preventDefault();
  const form = new FormData(elements.searchForm);
  const query = String(form.get("q") || "").trim();
  const mediaType = String(form.get("media_type") || "movie");
  if (!query) return;
  setStatus("Searching the catalog...");
  try {
    const payload = await requestJson(`/api/search?q=${encodeURIComponent(query)}&media_type=${encodeURIComponent(mediaType)}`);
    renderSearchResults(payload.items);
    setStatus(payload.items.length ? "Choose a title to add it to your taste set." : "No titles matched that search.");
  } catch (error) {
    renderSearchResults([]);
    setStatus(error.message, "error");
  }
}

async function resetProfile() {
  if (!window.confirm("Reset the local taste profile and comparisons?")) return;
  try {
    await requestJson("/api/profile/reset", { method: "POST" });
    state.seeds = [];
    state.pair = null;
    elements.pairPanel.hidden = true;
    elements.resultsPanel.hidden = true;
    elements.phaseLabel.textContent = "Build your taste set";
    elements.comparisonCount.textContent = "0";
    renderSeeds();
    renderSearchResults([]);
    setStatus("Your local profile was reset. The catalog cache is still available.");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

elements.searchForm.addEventListener("submit", searchCatalog);
elements.startButton.addEventListener("click", beginComparisons);
elements.resetButton.addEventListener("click", resetProfile);
renderSeeds();
