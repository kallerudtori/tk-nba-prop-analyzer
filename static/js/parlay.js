/**
 * parlay.js — Parlay Builder logic
 *
 * Responsibilities:
 *   • Maintain list of parlay legs
 *   • Calculate combined American odds, model probability, EV
 *   • Auto-suggest top picks (anti-correlation logic)
 *   • Render the parlay panel & "Best Value Picks" list
 */

"use strict";

/* ── State ────────────────────────────────────────────────────────────────── */
const ParlayState = {
  legs: [],          // Array of leg objects
  valuePicks: [],    // All analysed props with edge, sorted desc
};

/* ── Leg schema ──────────────────────────────────────────────────────────────
  {
    playerId    : number,
    playerName  : string,
    teamId      : number,
    prop        : 'points' | 'rebounds' | 'assists' | 'pra',
    line        : number,
    overOdds    : number,        // American
    edge        : number,
    modelProb   : number,        // 0-1
    valueLabeltext : string,
  }
─────────────────────────────────────────────────────────────────────────── */

/* ── Odds math ────────────────────────────────────────────────────────────── */
function americanToDecimal(american) {
  if (american >= 0) return american / 100 + 1;
  return 100 / Math.abs(american) + 1;
}

function decimalToAmerican(decimal) {
  if (decimal >= 2.0) return Math.round((decimal - 1) * 100);
  return Math.round(-100 / (decimal - 1));
}

function combinedDecimalOdds(legs) {
  return legs.reduce((acc, leg) => acc * americanToDecimal(leg.overOdds ?? -110), 1);
}

function combinedModelProb(legs) {
  return legs.reduce((acc, leg) => acc * (leg.modelProb ?? 0.55), 1);
}

function calcEV(legs, betAmount = 100) {
  const decOdds = combinedDecimalOdds(legs);
  const modelP  = combinedModelProb(legs);
  const payout  = betAmount * decOdds;
  return ((modelP * payout) - ((1 - modelP) * betAmount)).toFixed(2);
}

function formatAmerican(decimal) {
  const amer = decimalToAmerican(decimal);
  return amer >= 0 ? `+${amer}` : `${amer}`;
}

/* ── Public API ───────────────────────────────────────────────────────────── */

function parlayAddLeg(leg) {
  // Prevent same player appearing twice (DraftKings doesn't allow same-game same-player legs)
  const samePlayer = ParlayState.legs.find(l => l.playerId === leg.playerId);
  if (samePlayer) {
    showToast(`${leg.playerName} is already in your parlay — DraftKings doesn't allow same-player same-game legs.`, "error");
    return false;
  }
  if (ParlayState.legs.length >= 10) {
    showToast("Max 10 legs.", "error");
    return false;
  }
  ParlayState.legs.push(leg);
  parlayRender();
  showToast(`Added ${leg.playerName} ${_propLabel(leg.prop)} Over ${leg.line}`, "success");
  return true;
}

function parlayRemoveLeg(index) {
  ParlayState.legs.splice(index, 1);
  parlayRender();
}

function parlayClear() {
  ParlayState.legs = [];
  parlayRender();
}

/** Called by app.js after each player analysis completes */
function parlayRegisterValuePick(pick) {
  // pick: { playerId, playerName, teamId, prop, line, overOdds, edge, modelProb, valueLabel }
  // Remove stale entry for same player+prop, then add/re-sort
  ParlayState.valuePicks = ParlayState.valuePicks.filter(
    p => !(p.playerId === pick.playerId && p.prop === pick.prop)
  );
  if (pick.edge != null) {
    ParlayState.valuePicks.push(pick);
    ParlayState.valuePicks.sort((a, b) => (b.edge ?? -99) - (a.edge ?? -99));
  }
  renderBestPicks();
}

/** Remove all picks for a player (called when player card is removed) */
function parlayUnregisterPlayer(playerId) {
  ParlayState.valuePicks = ParlayState.valuePicks.filter(p => p.playerId !== playerId);
  ParlayState.legs = ParlayState.legs.filter(l => l.playerId !== playerId);
  renderBestPicks();
  parlayRender();
}

/** Auto-suggest: top 3-5 props by edge, anti-correlated */
function parlayAutoSuggest() {
  const eligible = ParlayState.valuePicks.filter(p => p.edge != null && p.edge >= 1.0);
  if (eligible.length === 0) {
    showToast("No value picks available yet. Analyse more players first.", "error");
    return;
  }

  const selected      = [];
  const usedTeamProp  = new Set();  // "teamId|prop" — no two players on same team with same prop
  const usedPlayerIds = new Set();  // no same player twice (DK same-game restriction)

  for (const pick of eligible) {
    if (selected.length >= 5) break;
    if (usedPlayerIds.has(pick.playerId)) continue;
    const key = `${pick.teamId}|${pick.prop}`;
    if (usedTeamProp.has(key)) continue;
    selected.push(pick);
    usedTeamProp.add(key);
    usedPlayerIds.add(pick.playerId);
  }

  if (selected.length < 2) {
    showToast("Need at least 2 non-correlated value picks.", "error");
    return;
  }

  ParlayState.legs = selected.map(p => ({ ...p }));
  parlayRender();
  showToast(`Auto-suggested ${selected.length}-leg parlay ⚡`, "success");
}

/* ── Render — Parlay legs ─────────────────────────────────────────────────── */
function parlayRender() {
  const container  = document.getElementById("parlay-legs-container");
  const emptyMsg   = document.getElementById("parlay-empty-msg");
  const summary    = document.getElementById("parlay-summary");
  const legsCount  = document.getElementById("legs-count");

  if (!container) return;

  legsCount.textContent = ParlayState.legs.length || "";

  if (ParlayState.legs.length === 0) {
    emptyMsg && emptyMsg.classList.remove("hidden");
    container.innerHTML = "";
    summary && summary.classList.add("hidden");
    return;
  }

  emptyMsg && emptyMsg.classList.add("hidden");

  container.innerHTML = ParlayState.legs
    .map((leg, i) => `
      <div class="parlay-leg">
        <div class="leg-info">
          <div class="leg-player">${leg.playerName}</div>
          <div class="leg-prop">
            Over ${leg.line} ${_propLabel(leg.prop)}
            · Edge: <span style="color:var(--green)">${leg.edge >= 0 ? "+" : ""}${leg.edge}</span>
          </div>
        </div>
        <span class="leg-odds">${leg.overOdds >= 0 ? "+" : ""}${leg.overOdds}</span>
        <button class="leg-remove" onclick="parlayRemoveLeg(${i})" title="Remove">✕</button>
      </div>
    `)
    .join("");

  // Summary
  if (summary && ParlayState.legs.length >= 2) {
    summary.classList.remove("hidden");
    const decOdds   = combinedDecimalOdds(ParlayState.legs);
    const modelP    = combinedModelProb(ParlayState.legs);
    const ev        = calcEV(ParlayState.legs);
    const evNum     = parseFloat(ev);

    const oddsEl  = document.getElementById("summary-odds");
    const probEl  = document.getElementById("summary-prob");
    const evEl    = document.getElementById("summary-ev");

    if (oddsEl) oddsEl.textContent = formatAmerican(decOdds);
    if (probEl) probEl.textContent = `${(modelP * 100).toFixed(1)}%`;
    if (evEl) {
      evEl.textContent = `$${evNum >= 0 ? "+" : ""}${ev}`;
      evEl.className = `summary-value ${evNum >= 0 ? "positive" : "negative"}`;
    }
  } else {
    summary && summary.classList.add("hidden");
  }
}

/* ── Render — Best Value Picks panel ─────────────────────────────────────── */
function renderBestPicks() {
  const container  = document.getElementById("best-picks-container");
  const picksCount = document.getElementById("picks-count");
  if (!container) return;

  const picks = ParlayState.valuePicks.filter(p => p.edge != null && p.edge >= 0.5);
  picksCount.textContent = picks.length || "";

  if (picks.length === 0) {
    container.innerHTML = '<p class="muted-text small">Analyse players to see value picks.</p>';
    return;
  }

  container.innerHTML = picks
    .slice(0, 12)
    .map(pick => {
      const inParlay = ParlayState.legs.some(
        l => l.playerId === pick.playerId && l.prop === pick.prop
      );
      const edgeStr  = pick.edge >= 0 ? `+${pick.edge}` : `${pick.edge}`;
      const badge    = _valueBadgeClass(pick.valueLabel);

      return `
        <div class="pick-row" onclick="parlayAddLeg(${JSON.stringify(pick).replace(/"/g, "&quot;")})">
          <div class="pick-info">
            <div class="pick-name">${pick.playerName}</div>
            <div class="pick-sub">
              ${_propLabel(pick.prop)} O${pick.line}
              · <span class="${badge}" style="font-size:0.65rem">${pick.valueLabel}</span>
            </div>
          </div>
          <span class="pick-edge">${edgeStr}</span>
          <button class="pick-add-btn" ${inParlay ? "disabled" : ""}>
            ${inParlay ? "✓" : "+ Add"}
          </button>
        </div>`;
    })
    .join("");
}

/* ── Helpers ──────────────────────────────────────────────────────────────── */
function _propLabel(prop) {
  const map = { points: "PTS", rebounds: "REB", assists: "AST", pra: "PRA", threes: "3PM" };
  return map[prop] ?? prop.toUpperCase();
}

function _valueBadgeClass(label) {
  if (label === "Strong Value") return "value-badge strong";
  if (label === "Slight Value") return "value-badge slight";
  return "value-badge avoid";
}

/* ── Wire up static buttons (called from app.js after DOM ready) ─────────── */
function parlayInitButtons() {
  document.getElementById("auto-suggest-btn")?.addEventListener("click", parlayAutoSuggest);
  document.getElementById("clear-parlay-btn")?.addEventListener("click", parlayClear);
}
