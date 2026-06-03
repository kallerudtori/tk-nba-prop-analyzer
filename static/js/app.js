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
  finalsMode: false,       // Finals Mode toggle (Feature 2) — OFF by default
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

  // Injury override modal (Feature 1)
  document.getElementById("injuries-btn")?.addEventListener("click", openInjuryModal);
  document.getElementById("injury-modal-close")?.addEventListener("click", closeInjuryModal);
  document.getElementById("injury-modal")?.addEventListener("click", e => {
    if (e.target.id === "injury-modal") closeInjuryModal();
  });
  document.getElementById("inj-add-btn")?.addEventListener("click", submitInjuryOverride);

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

  // Finals Mode toggle (Feature 2) — only for NBA Finals games
  setupFinalsToggle(game);

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

    // Injury status badge (Feature 1)
    const injEl  = row.querySelector(".player-row-injury");
    const status = player.injury_status || "Active";
    if (injEl) {
      if (status !== "Active") {
        injEl.textContent = _injuryShort(status);
        injEl.className   = `player-row-injury ${_injuryCls(status)}`;
        injEl.title       = player.injury_desc || status;
        row.classList.add(_injuryRowCls(status));
      } else if (player.injury_boosted) {
        injEl.textContent = "⬆ INJ";
        injEl.className   = "player-row-injury boosted";
        injEl.title       = "Projected to absorb minutes from injured teammates";
        row.classList.add("row-boosted");
      } else {
        injEl.classList.add("hidden");
      }
    }

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
 *  Finals Mode toggle (Feature 2)                                            *
 * ══════════════════════════════════════════════════════════════════════════ */
function setupFinalsToggle(game) {
  const bar    = document.getElementById("finals-toggle-bar");
  const toggle = document.getElementById("finals-mode-toggle");
  const meta   = document.getElementById("finals-toggle-meta");
  if (!bar || !toggle) return;

  if (!game.is_finals) {
    bar.classList.add("hidden");
    AppState.finalsMode = false;
    toggle.checked = false;
    return;
  }

  // Finals game → reveal toggle (OFF by default each time a game is opened)
  bar.classList.remove("hidden");
  AppState.finalsMode = false;
  toggle.checked = false;
  const gnum = game.finals_game_number || 1;
  meta.textContent = `Game ${gnum} of the NBA Finals`;

  // Re-bind to avoid stacking listeners across game switches
  toggle.onchange = () => {
    AppState.finalsMode = toggle.checked;
    showToast(
      toggle.checked ? "🏆 Finals Mode ON — re-analyzing open players"
                     : "Finals Mode OFF — reverting to standard model",
      toggle.checked ? "success" : "info"
    );
    reanalyzeOpenPlayers();
  };

  // Kick off a one-time backfill of prior Finals games so in-series form is
  // available (idempotent; safe to call repeatedly).
  if (game.game_id && (game.finals_game_number || 1) > 1) {
    apiFetch("/api/finals/backfill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ game_id: game.game_id }),
    }).catch(() => { /* non-fatal */ });
  }
}

/** Re-run analysis for every open player card (used when Finals Mode flips). */
function reanalyzeOpenPlayers() {
  const game = AppState.selectedGame;
  if (!game) return;
  const ids = Array.from(AppState.selectedPlayers);
  for (const pid of ids) {
    const a = AppState.analyses[pid];
    if (!a) continue;
    const isHome = a.player.team_id === game.home_team.id;
    const oppTeamId = isHome ? game.away_team.id : game.home_team.id;
    // Remove the existing card/tab then reload fresh
    removePlayerCard(pid);
    loadPlayerAnalysis(
      { player_id: pid, name: a.player.name },
      game, oppTeamId, isHome
    );
  }
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
    finals_mode: AppState.finalsMode ? "true" : "false",
    game_id: game.game_id || "",
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

    // Register all props as value picks.
    // OUT/Doubtful players are excluded from Value Picks & Auto-Suggest
    // (their projection is zeroed and they shouldn't be bet).
    const a = data.analysis;
    const ownStatus = a.player.injury_status || "Active";
    const excludeFromPicks = ownStatus === "Out" || ownStatus === "Doubtful";
    if (excludeFromPicks) {
      showToast(`${a.player.name} is ${ownStatus} — excluded from value picks.`, "error");
    }
    for (const [propKey, prop] of Object.entries(a.props)) {
      if (prop.has_odds && !excludeFromPicks) {
        parlayRegisterValuePick({
          playerId:   a.player.id,
          playerName: a.player.name,
          teamId:     a.player.team_id,
          prop:       propKey,
          line:       prop.line,
          overOdds:   prop.over_odds,
          edge:       prop.edge,
          modelProb:  prop.model_prob_over,
          modelProj:  prop.projection,
          modelConf:  prop.confidence,
          valueLabel: prop.value_label,
          ownStatus:  ownStatus,
          gameLabel:  `${game.away_team.abbreviation} @ ${game.home_team.abbreviation}`,
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

  // Injury banner + impact section (Feature 1)
  renderInjuryImpact(card, analysis);

  // Finals Context section (Feature 2)
  renderFinalsContext(card, analysis);

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
      showToast("No sportsbook line available for this prop.", "error");
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

  // Line label reflects the actual sportsbook (DraftKings preferred; falls back
  // to FanDuel/BetRivers/etc. when DK is absent from the odds feed).
  const lineLabelEl = lineEl.closest(".proj-block")?.querySelector(".proj-label");
  if (lineLabelEl) {
    lineLabelEl.textContent = (prop.has_odds && prop.source)
      ? `${prop.source} Line` : "DK Line";
  }

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

/* ── Injury Impact rendering (Feature 1) ──────────────────────────────────── */
function renderInjuryImpact(card, analysis) {
  const imp    = analysis.injury_impact;
  const status = analysis.player.injury_status || "Active";

  // Header badge: own status (uncertain/out) takes priority, else boost badge
  const badge = card.querySelector(".injury-badge");
  if (badge) {
    if (status !== "Active") {
      badge.textContent = _injuryShort(status);
      badge.className   = `injury-badge ${_injuryCls(status)}`;
    } else if (imp && imp.is_boosted) {
      badge.textContent = "⬆ INJ BOOST";
      badge.className   = "injury-badge boosted";
    } else {
      badge.classList.add("hidden");
    }
  }

  const section = card.querySelector(".injury-impact");
  const body    = card.querySelector(".injury-impact-body");
  if (!section || !body || !imp) return;

  const hasOwn      = status !== "Active";
  const hasBoost    = imp.is_boosted;
  const hasOuts     = (imp.out_players || []).length > 0;
  if (!hasOwn && !hasBoost && !hasOuts) {
    section.classList.add("hidden");
    return;
  }

  const rows = [];

  if (hasOwn) {
    const label = status === "Out"
      ? "Projected as <strong>OUT</strong> — projection zeroed; excluded from picks."
      : `Own status <strong>${status}</strong> — projection &amp; confidence discounted.`;
    rows.push(`<div class="inj-line ${_injuryRowCls(status)}">⚠️ ${label}</div>`);
  }

  if (hasBoost) {
    const min = imp.min_boost ? `+${imp.min_boost} min` : "";
    const usg = imp.usg_boost >= 0.01 ? `+${(imp.usg_boost * 100).toFixed(1)}% usg` : "";
    const parts = [min, usg].filter(Boolean).join(", ");
    const primary = imp.is_primary ? " <span class='inj-primary'>PRIMARY</span>" : "";
    rows.push(`<div class="inj-line boosted">⬆ Absorbing ${parts}${primary}</div>`);
    (imp.reasons || []).forEach(r => {
      rows.push(`<div class="inj-reason">• ${r}</div>`);
    });
  }

  if (hasOuts) {
    const outs = imp.out_players
      .map(o => `${o.name} (${o.status})`)
      .join(", ");
    rows.push(`<div class="inj-reason">Team absences: ${outs}</div>`);
  }

  if (imp.summary) {
    rows.push(`<div class="inj-summary">${imp.summary}</div>`);
  }

  body.innerHTML = rows.join("");
  section.classList.remove("hidden");
}

/* ── Finals Context section (Feature 2) ───────────────────────────────────── */
function renderFinalsContext(card, analysis) {
  const section = card.querySelector(".finals-context");
  const body    = card.querySelector(".finals-context-body");
  const gameEl  = card.querySelector(".finals-context-game");
  const fc      = analysis.finals_context;
  if (!section || !body) return;

  // Only render when Finals Mode is actually active for this analysis
  if (!fc || !fc.active) {
    section.classList.add("hidden");
    return;
  }

  if (gameEl) gameEl.textContent = `Game ${fc.game_number}`;

  const pct = fc.history_weight != null
    ? `${Math.round(fc.history_weight * 100)}%` : "0%";
  const rows = [];

  rows.push(
    `<div class="finals-line">Finals-form weight: <strong>${pct}</strong>` +
    (fc.has_finals_data
      ? ` (blending ${fc.finals_games || 0} prior Finals game${(fc.finals_games || 0) === 1 ? "" : "s"})`
      : " (no in-series sample yet)") +
    `</div>`
  );
  if (fc.pace_factor != null) {
    rows.push(`<div class="finals-line">Pace factor: <strong>×${fc.pace_factor}</strong> (Finals slowdown)</div>`);
  }
  if (fc.is_star) {
    rows.push(`<div class="finals-line">Focal scorer${fc.game_number >= 3 ? " — isolation premium applied (G3+)" : ""}</div>`);
  }
  if (fc.is_bench) {
    rows.push(`<div class="finals-line">Deep-bench — rotation compression applied</div>`);
  }
  if (fc.series_summary) {
    rows.push(`<div class="finals-summary">Series: ${fc.series_summary}</div>`);
  }
  (fc.notes || []).forEach(n => {
    rows.push(`<div class="finals-reason">• ${n}</div>`);
  });

  body.innerHTML = rows.join("");
  section.classList.remove("hidden");
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
 *  Injury Override Modal (Feature 1)                                          *
 * ══════════════════════════════════════════════════════════════════════════ */
function openInjuryModal() {
  const game = AppState.selectedGame;
  if (!game) {
    showToast("Select a game first.", "error");
    return;
  }
  // Populate the player dropdown from both rosters
  const select = document.getElementById("inj-player-select");
  if (select) {
    const opts = [];
    [["home", game.home_team], ["away", game.away_team]].forEach(([side, team]) => {
      (AppState.rosters[side] || []).forEach(p => {
        opts.push(
          `<option value="${p.player_id}" data-team-id="${team.id}" data-name="${p.name}">`
          + `${p.name} (${team.abbreviation})</option>`
        );
      });
    });
    select.innerHTML = opts.join("") || '<option value="">No roster loaded</option>';
  }
  document.getElementById("injury-modal")?.classList.remove("hidden");
  loadInjuryOverrides();
}

function closeInjuryModal() {
  document.getElementById("injury-modal")?.classList.add("hidden");
}

async function loadInjuryOverrides() {
  const container = document.getElementById("inj-overrides-container");
  if (!container) return;
  try {
    const data = await apiFetch("/api/injuries");
    const overrides = data.overrides || [];
    if (!overrides.length) {
      container.innerHTML = '<p class="muted-text small">No manual overrides.</p>';
      return;
    }
    container.innerHTML = overrides.map(o => `
      <div class="inj-override-row">
        <div>
          <span class="inj-override-name">${o.player_name || `#${o.player_id}`}</span>
          <span class="player-row-injury ${_injuryCls(o.status)}">${_injuryShort(o.status) || o.status}</span>
          ${o.notes ? `<span class="muted-text small"> — ${o.notes}</span>` : ""}
        </div>
        <button class="leg-remove" onclick="deleteInjuryOverride(${o.id})" title="Remove">✕</button>
      </div>
    `).join("");
  } catch (err) {
    container.innerHTML = `<p class="muted-text small">Failed to load: ${err.message}</p>`;
  }
}

async function submitInjuryOverride() {
  const select  = document.getElementById("inj-player-select");
  const opt      = select?.selectedOptions[0];
  if (!opt || !opt.value) {
    showToast("No player selected.", "error");
    return;
  }
  const status = document.getElementById("inj-status-select").value;
  const notes  = document.getElementById("inj-notes").value.trim();
  const payload = {
    player_id:   parseInt(opt.value, 10),
    player_name: opt.dataset.name,
    team_id:     parseInt(opt.dataset.teamId, 10),
    status,
    notes,
  };
  try {
    await apiFetch("/api/injuries/override", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    showToast(`Override set: ${opt.dataset.name} → ${status}`, "success");
    document.getElementById("inj-notes").value = "";
    await loadInjuryOverrides();
    await refreshRosters();
  } catch (err) {
    showToast(`Failed: ${err.message}`, "error");
  }
}

async function deleteInjuryOverride(overrideId) {
  try {
    await apiFetch(`/api/injuries/override/${overrideId}`, { method: "DELETE" });
    showToast("Override removed.", "success");
    await loadInjuryOverrides();
    await refreshRosters();
  } catch (err) {
    showToast(`Failed: ${err.message}`, "error");
  }
}

/** Re-fetch both rosters (injury statuses changed) and re-render current tab. */
async function refreshRosters() {
  const game = AppState.selectedGame;
  if (!game) return;
  try {
    const [homeData, awayData] = await Promise.all([
      apiFetch(`/api/team/${game.home_team.id}/roster`),
      apiFetch(`/api/team/${game.away_team.id}/roster`),
    ]);
    AppState.rosters.home = homeData.roster || [];
    AppState.rosters.away = awayData.roster || [];
    renderRoster(AppState.activeTeamTab);
  } catch (_) { /* silent */ }
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

/* ── Injury status helpers (Feature 1) ────────────────────────────────────── */
function _injuryShort(status) {
  const map = {
    Out: "OUT", Doubtful: "DTD", Questionable: "Q", GTD: "GTD", Active: "",
  };
  return map[status] ?? status;
}

function _injuryCls(status) {
  if (status === "Out" || status === "Doubtful") return "inj-out";
  if (status === "Questionable" || status === "GTD") return "inj-uncertain";
  return "";
}

function _injuryRowCls(status) {
  if (status === "Out" || status === "Doubtful") return "row-out";
  if (status === "Questionable" || status === "GTD") return "row-uncertain";
  return "";
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
