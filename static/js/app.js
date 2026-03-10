/**
 * app.js — Main application logic & state manager
 *
 * Flow:
 *  1. On load → fetchTodayGames()
 *  2. User clicks game card → loadRosters(game)
 *  3. User clicks "Analyze" on a player → loadPlayerAnalysis(player, game)
 *  4. Analysis card rendered; charts drawn; value picks registered with parlay.js
 *  5. User interacts with prop tabs to switch the stat view
 */

"use strict";

/* ══════════════════════════════════════════════════════════════════════════ *
 *  App State                                                                 *
 * ══════════════════════════════════════════════════════════════════════════ */
const AppState = {
  games: [],
  selectedGame: null,
  rosters: { home: [], away: [] },
  activeTeamTab: "home",   // "home" | "away"
  selectedPlayers: new Set(),
  analyses: {},            // playerId → full analysis object
  activePlayerId: null,    // currently visible tab
};

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Bootstrap                                                                 *
 * ══════════════════════════════════════════════════════════════════════════ */
document.addEventListener("DOMContentLoaded", () => {
  parlayInitButtons();
  fetchTodayGames();
  fetchHealthQuota();

  document.getElementById("refresh-odds-btn").addEventListener("click", handleRefreshOdds);
  document.getElementById("close-roster-btn").addEventListener("click", closeRosterPanel);

  // Day tabs (Today / Tomorrow)
  document.querySelectorAll(".day-tab").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".day-tab").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const offset = parseInt(btn.dataset.offset, 10);
      fetchTodayGames(offset);
      // Close roster panel when switching days
      closeRosterPanel();
      AppState.selectedGame = null;
      document.querySelectorAll(".game-card.active").forEach(c => c.classList.remove("active"));
    });
  });
});

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Health / Quota                                                            *
 * ══════════════════════════════════════════════════════════════════════════ */
async function fetchHealthQuota() {
  try {
    const data = await apiFetch("/api/health");
    if (data.odds_quota) {
      const el = document.getElementById("quota-text");
      const badge = document.getElementById("quota-badge");
      el.textContent = `${data.odds_quota.requests_remaining ?? "?"} calls left`;
      badge.classList.remove("hidden");
    }
  } catch (_) { /* silent */ }
}

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Today's Games                                                             *
 * ══════════════════════════════════════════════════════════════════════════ */
async function fetchTodayGames(dayOffset = 0) {
  const container = document.getElementById("games-container");
  showSkeletons(container, 4);

  try {
    const data = await apiFetch(`/api/games/today?day_offset=${dayOffset}`);
    AppState.games = data.games || [];
    renderGames(AppState.games);
    document.getElementById("games-count").textContent = AppState.games.length;
  } catch (err) {
    container.innerHTML = `<p class="muted-text small">Failed to load games: ${err.message}</p>`;
    showToast("Could not fetch games.", "error");
  }
}

function renderGames(games) {
  const container = document.getElementById("games-container");
  if (!games.length) {
    container.innerHTML = '<p class="muted-text small">No games scheduled today.</p>';
    return;
  }

  const tpl = document.getElementById("tpl-game-card");
  container.innerHTML = "";

  games.forEach(game => {
    const card = tpl.content.cloneNode(true).firstElementChild;
    card.dataset.gameId      = game.game_id;
    card.dataset.homeId      = game.home_team.id;
    card.dataset.awayId      = game.away_team.id;
    card.dataset.homeName    = game.home_team.name;
    card.dataset.awayName    = game.away_team.name;
    card.dataset.oddsEventId = game.odds_event_id || "";

    card.querySelector(".away-abbr").textContent = game.away_team.abbreviation;
    card.querySelector(".away-name").textContent = game.away_team.name.split(" ").pop();
    card.querySelector(".home-abbr").textContent = game.home_team.abbreviation;
    card.querySelector(".home-name").textContent = game.home_team.name.split(" ").pop();
    card.querySelector(".game-time").textContent = game.game_time;
    card.querySelector(".game-status").textContent =
      game.status_code === 1 ? "" : game.status_text;

    // Show "No DK" indicator when odds API couldn't match this game
    const dkBadge = card.querySelector(".dk-badge");
    if (dkBadge) {
      if (game.odds_event_id) {
        dkBadge.textContent = "DK";
        dkBadge.classList.add("dk-badge--ok");
      } else {
        dkBadge.textContent = "No DK";
        dkBadge.classList.add("dk-badge--missing");
      }
    }

    card.addEventListener("click", () => handleGameClick(card, game));
    container.appendChild(card);
  });
}

function handleGameClick(card, game) {
  // Deselect previous
  document.querySelectorAll(".game-card.active").forEach(c => c.classList.remove("active"));
  card.classList.add("active");

  AppState.selectedGame = game;
  loadRosters(game);
}

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Rosters                                                                   *
 * ══════════════════════════════════════════════════════════════════════════ */
async function loadRosters(game) {
  const section = document.getElementById("player-select-section");
  const roster  = document.getElementById("roster-container");
  const tabs    = document.getElementById("team-tabs");

  // Hide games list so player panel fills the sidebar from the top
  document.getElementById("games-section").style.display = "none";
  section.style.display = "block";
  showSkeletons(roster, 5);

  // Build team tabs
  tabs.innerHTML = `
    <button class="team-tab-btn active" data-team="home" onclick="switchTeamTab('home')">
      ${game.home_team.abbreviation}
    </button>
    <button class="team-tab-btn" data-team="away" onclick="switchTeamTab('away')">
      ${game.away_team.abbreviation}
    </button>`;

  // Fetch both rosters in parallel
  try {
    const [homeData, awayData] = await Promise.all([
      apiFetch(`/api/team/${game.home_team.id}/roster`),
      apiFetch(`/api/team/${game.away_team.id}/roster`),
    ]);
    AppState.rosters.home = homeData.roster || [];
    AppState.rosters.away = awayData.roster || [];
    AppState.activeTeamTab = "home";
    renderRoster("home");
  } catch (err) {
    roster.innerHTML = `<p class="muted-text small">Failed to load roster: ${err.message}</p>`;
    showToast("Roster load failed.", "error");
  }
}

function switchTeamTab(team) {
  AppState.activeTeamTab = team;
  document.querySelectorAll(".team-tab-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.team === team);
  });
  renderRoster(team);
}

function renderRoster(team) {
  const container = document.getElementById("roster-container");
  const players   = AppState.rosters[team] || [];
  const game      = AppState.selectedGame;
  const tpl       = document.getElementById("tpl-player-row");

  if (!players.length) {
    container.innerHTML = '<p class="muted-text small">No roster data.</p>';
    return;
  }

  container.innerHTML = "";
  players.forEach(player => {
    const row  = tpl.content.cloneNode(true).firstElementChild;
    row.dataset.playerId = player.player_id;

    const isHome    = team === "home";
    const oppTeamId = isHome ? game.away_team.id : game.home_team.id;

    row.dataset.isHome    = isHome ? "true" : "false";
    row.dataset.oppTeamId = oppTeamId;

    row.querySelector(".player-row-num").textContent  = player.number ? `#${player.number}` : "";
    row.querySelector(".player-row-name").textContent = player.name;
    row.querySelector(".player-row-pos").textContent  = player.position || "";

    if (AppState.selectedPlayers.has(player.player_id)) {
      row.classList.add("selected");
    }

    row.querySelector(".btn-analyze").addEventListener("click", e => {
      e.stopPropagation();
      const isSelected = AppState.selectedPlayers.has(player.player_id);
      if (isSelected) {
        removePlayerCard(player.player_id);
      } else {
        const isHomeBool = row.dataset.isHome === "true";
        loadPlayerAnalysis(player, game, oppTeamId, isHomeBool);
      }
      row.classList.toggle("selected", !isSelected);
    });

    container.appendChild(row);
  });
}

function closeRosterPanel() {
  document.getElementById("player-select-section").style.display = "none";
  document.getElementById("games-section").style.display = "block";
}

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Player Analysis                                                           *
 * ══════════════════════════════════════════════════════════════════════════ */
async function loadPlayerAnalysis(player, game, opponentTeamId, isHome) {
  AppState.selectedPlayers.add(player.player_id);
  updateEmptyState();

  // Show loading card placeholder and create tab immediately
  const placeholder = createLoadingCard(player);
  placeholder.style.display = "none";  // hidden until tab switches to it
  document.getElementById("analysis-container").appendChild(placeholder);
  createPlayerTab(player.player_id, player.name);
  switchToPlayerTab(player.player_id);
  placeholder.style.display = "block";

  const params = new URLSearchParams({
    opponent_team_id: opponentTeamId,
    is_home: isHome ? "true" : "false",
    odds_event_id: game.odds_event_id || "",
  });

  try {
    const data = await apiFetch(`/api/player/${player.player_id}/analysis?${params}`);
    AppState.analyses[player.player_id] = data.analysis;

    // Replace placeholder with full card
    placeholder.remove();
    const card = buildAnalysisCard(data.analysis);
    card.style.display = "none";
    document.getElementById("analysis-container").appendChild(card);
    switchToPlayerTab(player.player_id);

    // On mobile, jump to the Analysis tab automatically
    mobileActivatePanel("main-panel");

    // Register all props as value picks
    const a = data.analysis;
    for (const [propKey, prop] of Object.entries(a.props)) {
      if (prop.has_odds) {
        parlayRegisterValuePick({
          playerId:   a.player.id,
          playerName: a.player.name,
          teamId:     a.player.team_id,
          prop:       propKey,
          line:       prop.line,
          overOdds:   prop.over_odds,
          edge:       prop.edge,
          modelProb:  prop.model_prob_over,
          valueLabel: prop.value_label,
        });
      }
    }

  } catch (err) {
    placeholder.remove();
    removePlayerTab(player.player_id);
    AppState.selectedPlayers.delete(player.player_id);
    delete AppState.analyses[player.player_id];
    updateEmptyState();
    updateRosterSelection(player.player_id, false);
    showToast(`Error loading ${player.name}: ${err.message}`, "error");
  }
}

/* ── Build full analysis card ─────────────────────────────────────────────── */
function buildAnalysisCard(analysis) {
  const { player, stats, props } = analysis;
  const playerId = player.id;

  const tpl  = document.getElementById("tpl-analysis-card");
  const card = tpl.content.cloneNode(true).firstElementChild;
  card.dataset.playerId = playerId;
  card.id = `card-${playerId}`;

  card.querySelector(".card-player-name").textContent = player.name;
  card.querySelector(".card-player-meta").textContent = `${player.games_played} games played this season`;
  card.querySelector(".games-played-badge").textContent = `${player.games_played} GP`;

  // Headshot
  const headshot = card.querySelector(".player-headshot");
  if (headshot && player.headshot_url) {
    headshot.src = player.headshot_url;
    headshot.alt = player.name;
  }

  // Jersey number
  const jerseyEl = card.querySelector(".player-number");
  if (jerseyEl && player.jersey_number) {
    jerseyEl.textContent = `#${player.jersey_number}`;
  }

  // Close button
  card.querySelector(".btn-close-card").addEventListener("click", () => {
    removePlayerCard(playerId);
  });

  // Prop tabs
  card.querySelectorAll(".prop-tab").forEach(tab => {
    tab.addEventListener("click", () => {
      card.querySelectorAll(".prop-tab").forEach(t => t.classList.remove("active"));
      tab.classList.add("active");
      updateCardForProp(card, analysis, tab.dataset.prop);
    });
  });

  // Add-to-parlay button
  card.querySelector(".btn-add-parlay").addEventListener("click", () => {
    const activeProp = card.querySelector(".prop-tab.active")?.dataset.prop ?? "points";
    const prop = props[activeProp];
    if (!prop.has_odds) {
      showToast("No DraftKings line available for this prop.", "error");
      return;
    }
    parlayAddLeg({
      playerId,
      playerName: player.name,
      teamId:     player.team_id,
      prop:       activeProp,
      line:       prop.line,
      overOdds:   prop.over_odds,
      edge:       prop.edge,
      modelProb:  prop.model_prob_over,
      valueLabel: prop.value_label,
    });
  });

  // Initial render for "points"
  updateCardForProp(card, analysis, "points");

  return card;
}

/* ── Update card when prop tab changes ───────────────────────────────────── */
function updateCardForProp(card, analysis, propKey) {
  const { stats, props, player } = analysis;
  const prop = props[propKey];
  if (!prop) return;

  const statKey = _propToStatKey(propKey);

  // Stats grid
  card.querySelector(".season-val").textContent = stats.season_avg[statKey] ?? "—";
  card.querySelector(".l5-val").textContent     = stats.last_5_avg[statKey] ?? "—";
  card.querySelector(".l10-val").textContent    = stats.last_10_avg[statKey] ?? "—";
  card.querySelector(".home-val").textContent   = stats.home_avg[statKey] ?? "—";
  card.querySelector(".away-val").textContent   = stats.away_avg[statKey] ?? "—";

  // Projection row
  const lineEl  = card.querySelector(".line-val");
  const projEl  = card.querySelector(".projection-val");
  const edgeEl  = card.querySelector(".edge-val");

  lineEl.textContent = prop.has_odds ? prop.line : "N/A";
  projEl.textContent = prop.projection ?? "—";

  if (prop.edge != null) {
    const sign = prop.edge >= 0 ? "+" : "";
    edgeEl.textContent = `${sign}${prop.edge}`;
    edgeEl.className   = `proj-val edge-val ${prop.edge > 0 ? "positive" : prop.edge < 0 ? "negative" : "neutral"}`;
  } else {
    edgeEl.textContent = "—";
    edgeEl.className   = "proj-val edge-val neutral";
  }

  // Value & confidence badges
  const valBadge  = card.querySelector(".value-badge");
  const confBadge = card.querySelector(".confidence-badge");

  valBadge.textContent = prop.value_label ?? "—";
  valBadge.className   = `value-badge ${_valueBadgeCls(prop.value_label)}`;
  confBadge.textContent = `${prop.confidence ?? "—"} confidence`;
  confBadge.className   = `confidence-badge ${(prop.confidence ?? "").toLowerCase()}`;

  // Odds probability row
  const oddsRow = card.querySelector(".odds-prob-row");
  if (prop.has_odds) {
    oddsRow.classList.remove("hidden");
    card.querySelector(".implied-prob").textContent  = pct(prop.implied_prob_over);
    card.querySelector(".model-prob").textContent    = pct(prop.model_prob_over);
    card.querySelector(".over-odds-val").textContent =
      (prop.over_odds >= 0 ? "+" : "") + prop.over_odds;
    card.querySelector(".no-odds-notice").classList.add("hidden");
  } else {
    oddsRow.classList.add("hidden");
    card.querySelector(".no-odds-notice").classList.remove("hidden");
  }

  // Add-to-parlay button
  const addBtn = card.querySelector(".btn-add-parlay");
  addBtn.dataset.prop = propKey;
  addBtn.textContent = prop.has_odds
    ? `+ Add Over ${prop.line} ${_propLabel(propKey)} to Parlay`
    : "No DK odds available";
  addBtn.disabled = !prop.has_odds;

  // Game log table
  const line = prop.has_odds ? prop.line : null;
  renderGameLogTable(card, analysis.chart_data.last_10_games, propKey, line);

  // Destroy old charts then re-render (prop switch)
  destroyPlayerCharts(player.id);
  // rAF ensures canvases are in DOM before Chart.js touches them
  requestAnimationFrame(() => {
    renderAllCharts(card, player.id, propKey, analysis);
  });
}

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Player Tabs                                                               *
 * ══════════════════════════════════════════════════════════════════════════ */
function createPlayerTab(playerId, playerName) {
  const tabs = document.getElementById("player-tabs");
  if (!tabs) return;
  // Don't create duplicate tab
  if (tabs.querySelector(`[data-player-id="${playerId}"]`)) return;
  const btn = document.createElement("button");
  btn.className = "player-tab";
  btn.dataset.playerId = playerId;
  btn.innerHTML = `<span class="tab-name">${playerName}</span><span class="tab-close" title="Remove">✕</span>`;
  btn.addEventListener("click", e => {
    if (e.target.classList.contains("tab-close")) {
      removePlayerCard(playerId);
    } else {
      switchToPlayerTab(playerId);
    }
  });
  tabs.appendChild(btn);
  tabs.classList.remove("hidden");
}

function switchToPlayerTab(playerId) {
  document.querySelectorAll(".analysis-card").forEach(c => {
    c.style.display = "none";
  });
  const card = document.getElementById(`card-${playerId}`);
  if (card) card.style.display = "block";

  document.querySelectorAll(".player-tab").forEach(t => {
    t.classList.toggle("active", String(t.dataset.playerId) === String(playerId));
  });
  AppState.activePlayerId = playerId;
}

function removePlayerTab(playerId) {
  const tab = document.querySelector(`.player-tab[data-player-id="${playerId}"]`);
  if (tab) tab.remove();

  const remaining = document.querySelectorAll(".player-tab");
  const tabs = document.getElementById("player-tabs");
  if (!remaining.length) {
    if (tabs) tabs.classList.add("hidden");
    AppState.activePlayerId = null;
  } else if (String(AppState.activePlayerId) === String(playerId)) {
    // Activate the last remaining tab
    switchToPlayerTab(remaining[remaining.length - 1].dataset.playerId);
  }
}

/* ── Game Log Table ───────────────────────────────────────────────────────── */
function renderGameLogTable(card, last10Games, propKey, line) {
  const statKey = _propToStatKey(propKey);
  const statLabels = { pts: "PTS", reb: "REB", ast: "AST", pra: "PRA", threes: "3PM" };

  // Update column header
  const colHeader = card.querySelector(".game-log-stat-col");
  if (colHeader) colHeader.textContent = statLabels[statKey] || "—";

  const tbody = card.querySelector(".game-log-body");
  if (!tbody) return;

  // Reverse so most recent game is first
  const games = [...last10Games].reverse();
  tbody.innerHTML = games.map(g => {
    const val  = statKey === "threes" ? (g.fg3m ?? 0) : (g[statKey] ?? 0);
    const parts = (g.matchup || "").split(/\s+/);
    const opp  = parts[parts.length - 1].substring(0, 3).toUpperCase();
    const ha   = (g.matchup || "").includes("vs.") ? "vs" : "@";
    const cls  = line != null ? (val > line ? "over" : "under") : "";
    const date = (g.date || "").replace(/,\s*\d{4}/, "");
    const mins = Math.round(g.min ?? 0);
    return `<tr>
      <td>${date}</td>
      <td>${ha} ${opp}</td>
      <td>${mins}</td>
      <td class="${cls}">${val}</td>
    </tr>`;
  }).join("");
}

/* ── Remove player card ───────────────────────────────────────────────────── */
function removePlayerCard(playerId) {
  const card = document.getElementById(`card-${playerId}`);
  if (card) card.remove();

  destroyPlayerCharts(playerId);
  parlayUnregisterPlayer(playerId);
  AppState.selectedPlayers.delete(playerId);
  delete AppState.analyses[playerId];

  removePlayerTab(playerId);
  updateEmptyState();
  updateRosterSelection(playerId, false);
}

/* ── Loading placeholder ──────────────────────────────────────────────────── */
function createLoadingCard(player) {
  const div = document.createElement("div");
  div.className = "analysis-card";
  div.id        = `card-${player.player_id}`;
  div.innerHTML = `
    <div class="card-header">
      <div class="card-player-info">
        <h3 class="card-player-name">${player.name}</h3>
      </div>
    </div>
    <div class="card-loading">
      <span class="spinner"></span> Loading analysis…
    </div>`;
  return div;
}

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Odds Refresh                                                              *
 * ══════════════════════════════════════════════════════════════════════════ */
async function handleRefreshOdds() {
  const eventId = AppState.selectedGame?.odds_event_id ?? null;
  try {
    await apiFetch("/api/odds/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event_id: eventId }),
    });
    showToast("Odds cache cleared. Re-analyse players to get fresh lines.", "success");
  } catch (err) {
    showToast("Refresh failed: " + err.message, "error");
  }
}

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Utility                                                                   *
 * ══════════════════════════════════════════════════════════════════════════ */

async function apiFetch(url, options = {}) {
  const res = await fetch(url, options);
  const json = await res.json();
  if (!res.ok || json.success === false) {
    throw new Error(json.error || `HTTP ${res.status}`);
  }
  return json;
}

function updateEmptyState() {
  const empty     = document.getElementById("empty-state");
  const container = document.getElementById("analysis-container");
  const tabs      = document.getElementById("player-tabs");
  const hasCards  = AppState.selectedPlayers.size > 0;
  empty.style.display     = hasCards ? "none" : "flex";
  container.style.display = hasCards ? "block" : "none";
  if (tabs) tabs.classList.toggle("hidden", !hasCards);
}

function updateRosterSelection(playerId, selected) {
  const row = document.querySelector(`.player-row[data-player-id="${playerId}"]`);
  if (row) row.classList.toggle("selected", selected);
}

function showSkeletons(container, count) {
  container.innerHTML =
    `<div class="skeleton-list">${Array(count).fill('<div class="skeleton-item"></div>').join("")}</div>`;
}

function pct(prob) {
  if (prob == null) return "—";
  return `${(prob * 100).toFixed(1)}%`;
}

function _propToStatKey(prop) {
  const map = { points: "pts", rebounds: "reb", assists: "ast", pra: "pra", threes: "threes" };
  return map[prop] ?? "pts";
}

function _propLabel(prop) {
  const map = { points: "PTS", rebounds: "REB", assists: "AST", pra: "PRA", threes: "3PM" };
  return map[prop] ?? prop.toUpperCase();
}

function _valueBadgeCls(label) {
  if (label === "Strong Value") return "strong";
  if (label === "Slight Value") return "slight";
  if (label === "No Odds")      return "no-odds";
  return "avoid";
}

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Mobile navigation                                                          *
 * ══════════════════════════════════════════════════════════════════════════ */
const MOBILE_PANELS = ["sidebar", "main-panel", "parlay-panel"];

function mobileActivatePanel(panelId) {
  MOBILE_PANELS.forEach(id => {
    document.getElementById(id)?.classList.remove("mobile-active");
  });
  document.getElementById(panelId)?.classList.add("mobile-active");
  document.querySelectorAll(".mobile-nav-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.panel === panelId);
  });
}

(function initMobileNav() {
  document.querySelectorAll(".mobile-nav-btn").forEach(btn => {
    btn.addEventListener("click", () => mobileActivatePanel(btn.dataset.panel));
  });
  mobileActivatePanel("sidebar");
})();

/* ── Toast notifications ─────────────────────────────────────────────────── */
let _toastContainer = null;

function showToast(message, type = "") {
  if (!_toastContainer) {
    _toastContainer = document.createElement("div");
    _toastContainer.id = "toast-container";
    document.body.appendChild(_toastContainer);
  }
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  _toastContainer.appendChild(toast);
  setTimeout(() => toast.remove(), 3500);
}
