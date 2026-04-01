"use strict";

const NBA_LOGO = id =>
  `https://cdn.nba.com/logos/nba/${id}/global/L/logo.svg`;

let currentOffset = 0;

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Bootstrap                                                                  *
 * ══════════════════════════════════════════════════════════════════════════ */
document.addEventListener("DOMContentLoaded", () => {
  fetchHealthQuota();
  fetchGamesWithLines(0);
  fetchTopPick(0);

  document.querySelectorAll(".day-tab").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".day-tab").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      currentOffset = parseInt(btn.dataset.offset, 10);
      fetchGamesWithLines(currentOffset);
      fetchTopPick(currentOffset);
    });
  });
});

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Health / Quota                                                             *
 * ══════════════════════════════════════════════════════════════════════════ */
async function fetchHealthQuota() {
  try {
    const data = await apiFetch("/api/health");
    const q    = data.odds_quota;
    if (!q?.requests_remaining) return;

    const remaining = q.requests_remaining;
    const resetDays = data.quota_reset_days;
    let text = `${remaining} calls left`;
    if (resetDays != null) {
      text += resetDays === 0
        ? " · resets today"
        : ` · resets in ${resetDays}d`;
    }
    document.getElementById("quota-text").textContent = text;
    document.getElementById("quota-badge").classList.remove("hidden");
  } catch (_) { /* silent */ }
}

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Top Pick Banner                                                            *
 * ══════════════════════════════════════════════════════════════════════════ */
async function fetchTopPick(dayOffset) {
  const banner  = document.getElementById("top-pick-banner");
  const loading = banner.querySelector(".tp-loading");
  const content = banner.querySelector(".tp-content");

  banner.classList.remove("hidden");
  loading.classList.remove("hidden");
  content.classList.add("hidden");

  try {
    const data = await apiFetch(`/api/games/top-pick?day_offset=${dayOffset}`);
    renderTopPick(data);
  } catch (_) {
    banner.classList.add("hidden");
  }
}

function renderTopPick(data) {
  const banner  = document.getElementById("top-pick-banner");
  const loading = banner.querySelector(".tp-loading");
  const content = banner.querySelector(".tp-content");

  loading.classList.add("hidden");

  if (!data.pick || data.pick === "—") {
    banner.classList.add("hidden");
    return;
  }

  document.getElementById("tp-pick").textContent       = data.pick;
  document.getElementById("tp-game-label").textContent = data.game || "";
  document.getElementById("tp-analysis").textContent   = data.analysis || "";

  // ALT badge in banner
  const tpAlt = document.getElementById("tp-alt-badge");
  if (tpAlt) tpAlt.classList.toggle("hidden", !data.is_alternate);

  // Alt comparison line in banner
  const tpAltComp = document.getElementById("tp-alt-comparison");
  if (tpAltComp) {
    if (data.is_alternate && data.alt_spread_comparison) {
      tpAltComp.textContent = `📊 Line comparison: ${data.alt_spread_comparison}`;
      tpAltComp.classList.remove("hidden");
    } else {
      tpAltComp.classList.add("hidden");
    }
  }

  const conf      = (data.confidence || "low").toLowerCase();
  const confBadge = document.getElementById("tp-confidence");
  confBadge.textContent = conf.charAt(0).toUpperCase() + conf.slice(1);
  confBadge.className   = `lc-confidence-badge conf-${conf}`;

  content.classList.remove("hidden");
}

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Fetch & Render Games                                                       *
 * ══════════════════════════════════════════════════════════════════════════ */
async function fetchGamesWithLines(dayOffset) {
  const container = document.getElementById("lines-container");
  showSkeletons(container);

  try {
    const data  = await apiFetch(`/api/games/lines?day_offset=${dayOffset}`);
    const games = data.games || [];
    document.getElementById("games-count-badge").textContent = `${games.length} games`;
    renderGames(games);
  } catch (err) {
    container.innerHTML =
      `<p class="muted-text small" style="padding:24px">Failed to load games: ${err.message}</p>`;
  }
}

function renderGames(games) {
  const container = document.getElementById("lines-container");
  const tpl       = document.getElementById("tpl-lines-card");
  container.innerHTML = "";

  if (!games.length) {
    container.innerHTML = '<p class="muted-text small" style="padding:24px">No games scheduled.</p>';
    return;
  }

  games.forEach(game => {
    const card = tpl.content.cloneNode(true).firstElementChild;
    populateCard(card, game);
    container.appendChild(card);
  });
}

function populateCard(card, game) {
  const home  = game.home_team;
  const away  = game.away_team;
  const lines = game.lines;

  // Team logos
  const awayLogo = card.querySelector(".away-logo");
  const homeLogo = card.querySelector(".home-logo");
  if (away.id) { awayLogo.src = NBA_LOGO(away.id); awayLogo.alt = away.abbreviation; }
  if (home.id) { homeLogo.src = NBA_LOGO(home.id); homeLogo.alt = home.abbreviation; }

  // Names & time
  card.querySelector(".away-abbr").textContent = away.abbreviation;
  card.querySelector(".away-name").textContent = away.name;
  card.querySelector(".home-abbr").textContent = home.abbreviation;
  card.querySelector(".home-name").textContent = home.name;
  card.querySelector(".lc-time").textContent   = game.game_time || "TBD";

  // Records
  if (away.wins != null) card.querySelector(".away-record").textContent = `${away.wins}–${away.losses}`;
  if (home.wins != null) card.querySelector(".home-record").textContent = `${home.wins}–${home.losses}`;

  // No lines case
  if (!lines) {
    card.querySelector(".lc-odds-table").classList.add("hidden");
    card.querySelector(".lc-no-lines").classList.remove("hidden");
    card.querySelector(".lc-analyze-btn").disabled    = true;
    card.querySelector(".lc-analyze-btn").textContent = "No lines available";
    return;
  }

  const ml  = lines.moneyline || {};
  const sp  = lines.spread    || {};
  const tot = lines.total     || {};

  // Spread
  card.querySelector(".spread-away").textContent =
    sp.away_point != null ? `${away.abbreviation} ${fmtPoint(sp.away_point)} (${fmtOdds(sp.away_price)})` : "—";
  card.querySelector(".spread-home").textContent =
    sp.home_point != null ? `${home.abbreviation} ${fmtPoint(sp.home_point)} (${fmtOdds(sp.home_price)})` : "—";

  // Moneyline — highlight dog in gold
  const mlAway = card.querySelector(".ml-away");
  const mlHome = card.querySelector(".ml-home");
  mlAway.textContent = ml.away_price != null ? `${away.abbreviation} ${fmtOdds(ml.away_price)}` : "—";
  mlHome.textContent = ml.home_price != null ? `${home.abbreviation} ${fmtOdds(ml.home_price)}` : "—";
  if (ml.away_price > 0) mlAway.classList.add("odds-underdog");
  if (ml.home_price > 0) mlHome.classList.add("odds-underdog");

  // Total
  card.querySelector(".total-over").textContent =
    tot.point != null ? `Over  ${tot.point} (${fmtOdds(tot.over_price)})` : "—";
  card.querySelector(".total-under").textContent =
    tot.point != null ? `Under ${tot.point} (${fmtOdds(tot.under_price)})` : "—";

  // Analyze button
  const analyzeBtn   = card.querySelector(".lc-analyze-btn");
  const analysisBody = card.querySelector(".lc-analysis-body");
  const eventId      = game.odds_event_id;

  analyzeBtn.addEventListener("click", async () => {
    if (!analysisBody.classList.contains("hidden")) {
      analysisBody.classList.add("hidden");
      analyzeBtn.textContent = "✦ Get Sharp Analysis";
      return;
    }

    analyzeBtn.disabled    = true;
    analyzeBtn.textContent = "Analyzing…";
    analysisBody.classList.remove("hidden");
    card.querySelector(".lc-analysis-loading").classList.remove("hidden");
    card.querySelector(".lc-analysis-content").classList.add("hidden");

    try {
      const data = await apiFetch(`/api/game/${eventId}/analysis?day_offset=${currentOffset}`);
      renderAnalysis(card, data);
      analyzeBtn.textContent = "▲ Hide Analysis";
    } catch (err) {
      card.querySelector(".lc-analysis-text").textContent = `Error: ${err.message}`;
      card.querySelector(".lc-analysis-loading").classList.add("hidden");
      card.querySelector(".lc-analysis-content").classList.remove("hidden");
      analyzeBtn.textContent = "✦ Get Sharp Analysis";
    } finally {
      analyzeBtn.disabled = false;
    }
  });
}

function renderAltLines(card, altSpreads) {
  const section = card.querySelector(".lc-alt-lines");
  if (!section) return;

  if (!altSpreads || altSpreads.length === 0) {
    section.classList.add("hidden");
    return;
  }

  // Determine team names from existing card text
  const awayName = card.querySelector(".away-name")?.textContent || "";
  const homeName = card.querySelector(".home-name")?.textContent || "";

  const awayLines = altSpreads.filter(a => a.team === awayName);
  const homeLines = altSpreads.filter(a => a.team === homeName);

  // Populate column headers
  section.querySelector(".away-abbr-full").textContent =
    card.querySelector(".away-abbr")?.textContent || awayName;
  section.querySelector(".home-abbr-full").textContent =
    card.querySelector(".home-abbr")?.textContent || homeName;

  const fmtOdds = p => p > 0 ? `+${p}` : `${p}`;
  const fmtPt   = p => p > 0 ? `+${p}` : `${p}`;

  const fillCol = (col, lines) => {
    // Remove previous rows (keep the header)
    col.querySelectorAll(".lc-alt-line-row").forEach(r => r.remove());
    lines.forEach(l => {
      const row = document.createElement("div");
      row.className = "lc-alt-line-row";
      row.innerHTML =
        `<span class="lc-alt-line-pt">${fmtPt(l.spread)}</span>` +
        `<span class="lc-alt-line-odds">${fmtOdds(l.odds)}</span>`;
      col.appendChild(row);
    });
  };

  fillCol(section.querySelector('[data-side="away"]'), awayLines);
  fillCol(section.querySelector('[data-side="home"]'), homeLines);

  // Update count badge
  const total = awayLines.length + homeLines.length;
  section.querySelector(".lc-alt-lines-count").textContent = `(${total})`;

  section.classList.remove("hidden");

  // Toggle expand/collapse
  const toggle = section.querySelector(".lc-alt-lines-toggle");
  const body   = section.querySelector(".lc-alt-lines-body");
  if (!toggle._bound) {
    toggle._bound = true;
    toggle.addEventListener("click", () => {
      const open = !body.classList.contains("hidden");
      body.classList.toggle("hidden", open);
      toggle.classList.toggle("open", !open);
    });
  }
}

function renderAnalysis(card, data) {
  card.querySelector(".lc-analysis-loading").classList.add("hidden");
  card.querySelector(".lc-analysis-content").classList.remove("hidden");

  // Alt lines table
  renderAltLines(card, data.alternate_spreads);

  if (data.pick && data.pick !== "—") {
    const pickRow = card.querySelector(".lc-pick-row");
    pickRow.classList.remove("hidden");
    card.querySelector(".lc-pick-value").textContent = data.pick;

    // ALT badge
    const altBadge = card.querySelector(".lc-alt-badge");
    if (altBadge) {
      altBadge.classList.toggle("hidden", !data.is_alternate);
    }

    // Alt comparison row
    const altComp = card.querySelector(".lc-alt-comparison");
    if (altComp && data.is_alternate && data.alt_spread_comparison) {
      altComp.querySelector(".lc-main-line-val").textContent = data.alt_spread_comparison;
      altComp.querySelector(".lc-alt-pick-val").textContent  = data.pick;
      altComp.classList.remove("hidden");
    }

    const conf      = (data.confidence || "low").toLowerCase();
    const confBadge = card.querySelector(".lc-confidence-badge");
    confBadge.textContent = conf.charAt(0).toUpperCase() + conf.slice(1);
    confBadge.className   = `lc-confidence-badge conf-${conf}`;

    // Show "I placed this bet" checkbox
    const placeRow = card.querySelector(".lc-place-row");
    const cb       = card.querySelector(".lc-placed-cb");
    const label    = card.querySelector(".lc-place-label");
    if (placeRow && cb) {
      placeRow.classList.remove("hidden");
      cb.addEventListener("change", async () => {
        if (cb.checked) {
          const today     = new Date().toISOString().slice(0, 10);
          const awayAbbr  = card.querySelector(".away-abbr")?.textContent || "";
          const homeAbbr  = card.querySelector(".home-abbr")?.textContent || "";
          const gameLabel = `${awayAbbr} @ ${homeAbbr}`;
          const pick      = data.pick || "";

          // Best-effort parse of line from pick label (e.g. "Celtics -2.5" → line=2.5, over_under=home)
          const lineMatch = pick.match(/([+-]?\d+\.?\d*)\s*$/);
          const parsedLine = lineMatch ? Math.abs(parseFloat(lineMatch[1])) : null;
          const ou = pick.toLowerCase().includes("over") ? "over"
                   : pick.toLowerCase().includes("under") ? "under"
                   : parsedLine !== null ? (parseFloat(lineMatch[1]) < 0 ? "home" : "away")
                   : null;

          try {
            const res  = await fetch("/api/bets", {
              method:  "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                bet_type:   "game_line",
                pick_label: pick,
                game_label: gameLabel,
                game_date:  today,
                line:       parsedLine,
                over_under: ou,
                odds:       -110,
              }),
            });
            const json = await res.json();
            if (!json.success) throw new Error(json.error || "API error");
            cb.dataset.betId  = json.id;
            label.textContent = "✓ Logged";
            label.classList.add("lc-place-logged");
          } catch (err) {
            cb.checked = false;
            showToast(`Failed to log bet: ${err.message}`, "error");
          }
        } else {
          // Uncheck — remove the bet
          const betId = cb.dataset.betId;
          if (betId) {
            try {
              await fetch(`/api/bets/${betId}`, { method: "DELETE" });
              delete cb.dataset.betId;
              label.textContent = "I placed this bet";
              label.classList.remove("lc-place-logged");
            } catch (err) {
              cb.checked = true;
              showToast(`Failed to remove bet: ${err.message}`, "error");
            }
          }
        }
      });
    }
  }
  card.querySelector(".lc-analysis-text").textContent = data.analysis || "";
}

function showToast(msg, type = "success") {
  const t = document.createElement("div");
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.classList.add("toast-show"), 10);
  setTimeout(() => { t.classList.remove("toast-show"); setTimeout(() => t.remove(), 300); }, 3000);
}

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Utilities                                                                  *
 * ══════════════════════════════════════════════════════════════════════════ */
function fmtOdds(price) {
  if (price == null) return "—";
  return price > 0 ? `+${price}` : String(price);
}
function fmtPoint(point) {
  if (point == null) return "—";
  return point > 0 ? `+${point}` : String(point);
}
function showSkeletons(container, count = 4) {
  container.innerHTML = Array(count).fill('<div class="skeleton-card"></div>').join("");
}
async function apiFetch(url, options = {}) {
  const res  = await fetch(url, options);
  const json = await res.json();
  if (!res.ok || json.success === false) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
}
