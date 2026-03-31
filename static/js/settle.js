"use strict";

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Utilities                                                                  *
 * ══════════════════════════════════════════════════════════════════════════ */
async function apiFetch(url, options = {}) {
  const res  = await fetch(url, options);
  const json = await res.json();
  if (!res.ok || json.success === false) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
}

function showToast(msg, type = "success") {
  const t = document.createElement("div");
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.classList.add("toast-show"), 10);
  setTimeout(() => { t.classList.remove("toast-show"); setTimeout(() => t.remove(), 300); }, 3000);
}

function fmtDate(iso) {
  if (!iso) return "—";
  return iso.slice(0, 10);
}

function propLabel(key) {
  const m = { points: "PTS", rebounds: "REB", assists: "AST", pra: "PRA", threes: "3PM" };
  return m[key] || (key || "").toUpperCase();
}

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Bootstrap                                                                  *
 * ══════════════════════════════════════════════════════════════════════════ */
document.addEventListener("DOMContentLoaded", () => {
  loadBets();
  loadWeights();

  document.getElementById("settled-toggle").addEventListener("click", () => {
    const cont = document.getElementById("settled-container");
    const isHidden = cont.classList.contains("hidden");
    cont.classList.toggle("hidden", !isHidden);
    const count = document.getElementById("settled-count").textContent;
    document.getElementById("settled-toggle").textContent =
      isHidden ? `Hide settled (${count})` : `Show settled (${count})`;
  });

  document.getElementById("run-optimizer-btn").addEventListener("click", runOptimizer);
  document.getElementById("apply-weights-btn").addEventListener("click", applyWeights);
});

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Load & Render Bets                                                         *
 * ══════════════════════════════════════════════════════════════════════════ */
let _pendingBets  = [];
let _settledBets  = [];
let _newWeights   = null;

async function loadBets() {
  try {
    const data = await apiFetch("/api/bets");
    const bets = data.bets || [];
    _pendingBets = bets.filter(b => b.status === "pending");
    _settledBets = bets.filter(b => b.status !== "pending");

    renderPending();
    renderSettled();
    loadAccuracy();
  } catch (err) {
    document.getElementById("pending-container").innerHTML =
      `<p class="muted-text small">Failed to load bets: ${err.message}</p>`;
  }
}

function renderPending() {
  const cont  = document.getElementById("pending-container");
  const count = document.getElementById("pending-count");
  count.textContent = _pendingBets.length || "";

  if (_pendingBets.length === 0) {
    cont.innerHTML = `<p class="muted-text small" style="padding:12px 0">
      No pending bets. Check off picks in the Parlay Builder or Game Lines page.
    </p>`;
    return;
  }

  cont.innerHTML = _pendingBets.map(bet => buildBetCard(bet, false)).join("");
  _wirePendingCards(cont);
}

function renderSettled() {
  const cont  = document.getElementById("settled-container");
  const count = document.getElementById("settled-count");
  count.textContent = _settledBets.length;

  if (_settledBets.length === 0) {
    cont.innerHTML = `<p class="muted-text small" style="padding:12px 0">No settled bets yet.</p>`;
    return;
  }
  cont.innerHTML = _settledBets.map(bet => buildBetCard(bet, true)).join("");
  _wireSettledCards(cont);
}

function buildBetCard(bet, settled) {
  const statusCls = bet.status === "won"  ? "bet-won"
                  : bet.status === "lost" ? "bet-lost"
                  : bet.status === "void" ? "bet-void"
                  : "";

  const confBadge = bet.model_confidence
    ? `<span class="conf-badge conf-${bet.model_confidence.toLowerCase()}">${bet.model_confidence}</span>`
    : "";

  const projInfo = (bet.bet_type === "prop" && bet.model_projection != null)
    ? `<span class="bet-meta-item">Proj: <b>${bet.model_projection}</b></span>
       <span class="bet-meta-item">Edge: <b>${bet.model_edge != null ? (bet.model_edge >= 0 ? "+" : "") + bet.model_edge : "—"}</b></span>`
    : "";

  const actualInfo = settled && bet.actual_value != null
    ? `<span class="bet-meta-item">Actual: <b>${bet.actual_value}</b></span>`
    : "";

  const statusBadge = settled
    ? `<span class="bet-status-badge status-${bet.status}">${bet.status.toUpperCase()}</span>`
    : "";

  const settleControls = !settled ? `
    <div class="bet-settle-row" data-bet-id="${bet.id}" data-bet-type="${bet.bet_type}" data-line="${bet.line ?? ""}" data-ou="${bet.over_under ?? ""}">
      ${bet.bet_type === "prop"
        ? `<input type="number" class="settle-input" placeholder="Actual stat" step="0.5" min="0" />
           <button class="btn btn-outline btn-xs settle-submit-btn">Settle</button>`
        : `<button class="btn btn-green btn-xs settle-won-btn">Won</button>
           <button class="btn btn-outline btn-xs settle-lost-btn" style="color:var(--red);border-color:var(--red)">Lost</button>
           <button class="btn btn-outline btn-xs settle-void-btn">Void</button>`
      }
    </div>` : "";

  const removeBtn = settled
    ? `<button class="btn btn-ghost btn-xs bet-remove-btn" data-bet-id="${bet.id}" title="Remove">✕</button>`
    : "";

  return `
    <div class="bet-card ${statusCls}" data-bet-id="${bet.id}">
      <div class="bet-card-top">
        <div class="bet-card-left">
          <div class="bet-pick-label">${bet.pick_label || "—"}</div>
          <div class="bet-meta">
            <span class="bet-meta-item">${bet.game_label || ""}</span>
            <span class="bet-meta-item">${fmtDate(bet.game_date)}</span>
            <span class="bet-meta-item">${bet.odds != null ? (bet.odds > 0 ? "+" : "") + bet.odds : ""}</span>
            ${projInfo}
            ${actualInfo}
            ${confBadge}
          </div>
        </div>
        <div class="bet-card-right">
          ${statusBadge}
          ${removeBtn}
        </div>
      </div>
      ${settleControls}
    </div>`;
}

function _wirePendingCards(cont) {
  // Prop bet settle
  cont.querySelectorAll(".settle-submit-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const row     = btn.closest(".bet-settle-row");
      const betId   = row.dataset.betId;
      const input   = row.querySelector(".settle-input");
      const val     = parseFloat(input.value);
      if (isNaN(val)) { showToast("Enter a valid number", "error"); return; }
      await _settleBet(betId, { actual_value: val });
    });
  });

  // Game line Won/Lost/Void
  cont.querySelectorAll(".settle-won-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const betId = btn.closest(".bet-settle-row").dataset.betId;
      _settleBet(betId, { status: "won" });
    });
  });
  cont.querySelectorAll(".settle-lost-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const betId = btn.closest(".bet-settle-row").dataset.betId;
      _settleBet(betId, { status: "lost" });
    });
  });
  cont.querySelectorAll(".settle-void-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const betId = btn.closest(".bet-settle-row").dataset.betId;
      _settleBet(betId, { status: "void" });
    });
  });
}

function _wireSettledCards(cont) {
  cont.querySelectorAll(".bet-remove-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const betId = btn.dataset.betId;
      try {
        await apiFetch(`/api/bets/${betId}`, { method: "DELETE" });
        btn.closest(".bet-card").remove();
        _settledBets = _settledBets.filter(b => String(b.id) !== String(betId));
        document.getElementById("settled-count").textContent = _settledBets.length;
        loadAccuracy();
        showToast("Bet removed");
      } catch (err) {
        showToast(`Error: ${err.message}`, "error");
      }
    });
  });
}

async function _settleBet(betId, body) {
  try {
    const data = await apiFetch(`/api/bets/${betId}/settle`, {
      method:  "PATCH",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
    });
    showToast(`Settled: ${data.status.toUpperCase()}`);
    loadBets();
  } catch (err) {
    showToast(`Error: ${err.message}`, "error");
  }
}

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Accuracy Dashboard                                                         *
 * ══════════════════════════════════════════════════════════════════════════ */
const _accCharts = {};

async function loadAccuracy() {
  try {
    const data = await apiFetch("/api/bets/accuracy");
    renderAccuracy(data);
  } catch (_) { /* silent */ }
}

function renderAccuracy(data) {
  document.getElementById("acc-total").textContent    = data.total ?? "—";
  document.getElementById("acc-hit-rate").textContent = data.hit_rate != null ? `${data.hit_rate}%` : "—";

  // Best prop type
  const byProp = data.by_prop || {};
  const bestProp = Object.entries(byProp).sort((a, b) => b[1] - a[1])[0];
  document.getElementById("acc-best-prop").textContent =
    bestProp ? `${propLabel(bestProp[0])} (${bestProp[1]}%)` : "—";

  const unlockMsg  = document.getElementById("acc-unlock-msg");
  const chartsWrap = document.getElementById("acc-charts-wrap");

  if (data.total < 5) {
    unlockMsg.classList.remove("hidden");
    chartsWrap.classList.add("hidden");
    return;
  }

  unlockMsg.classList.add("hidden");
  chartsWrap.classList.remove("hidden");

  _renderBarChart("chart-by-prop",  byProp,          "Hit Rate by Prop (%)");
  _renderBarChart("chart-by-conf",  data.by_confidence || {}, "Hit Rate by Confidence (%)");
  _renderBiasChart("chart-bias",    data.bias_by_prop || {});
  _renderRollingChart("chart-rolling", data.rolling_hit_rate || []);
}

function _destroyChart(id) {
  if (_accCharts[id]) { _accCharts[id].destroy(); delete _accCharts[id]; }
}

function _renderBarChart(id, dataObj, label) {
  _destroyChart(id);
  const labels = Object.keys(dataObj).map(propLabel);
  const values = Object.values(dataObj);
  const canvas = document.getElementById(id);
  if (!canvas) return;
  _accCharts[id] = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [{ label, data: values,
        backgroundColor: "rgba(77,171,247,0.7)", borderColor: "#4dabf7",
        borderWidth: 1, borderRadius: 4 }],
    },
    options: {
      indexAxis: "y",
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { min: 0, max: 100, grid: { color: "#232f4a" }, ticks: { color: "#7a8fb5", font: { size: 10 } }, border: { color: "transparent" } },
        y: { grid: { display: false }, ticks: { color: "#7a8fb5", font: { size: 10 } }, border: { color: "transparent" } },
      },
      plugins: { legend: { display: false } },
    },
  });
}

function _renderBiasChart(id, dataObj) {
  _destroyChart(id);
  const labels = Object.keys(dataObj).map(propLabel);
  const values = Object.values(dataObj);
  const colors = values.map(v => v > 0 ? "rgba(255,71,87,0.7)" : "rgba(0,214,143,0.7)");
  const canvas = document.getElementById(id);
  if (!canvas) return;
  _accCharts[id] = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [{ label: "Avg (Proj − Actual)", data: values,
        backgroundColor: colors, borderWidth: 0, borderRadius: 4 }],
    },
    options: {
      indexAxis: "y",
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { grid: { color: "#232f4a" }, ticks: { color: "#7a8fb5", font: { size: 10 } }, border: { color: "transparent" } },
        y: { grid: { display: false }, ticks: { color: "#7a8fb5", font: { size: 10 } }, border: { color: "transparent" } },
      },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => ` ${ctx.parsed.x > 0 ? "Over-projects" : "Under-projects"} by ${Math.abs(ctx.parsed.x).toFixed(2)}` } },
      },
    },
  });
}

function _renderRollingChart(id, rolling) {
  _destroyChart(id);
  const canvas = document.getElementById(id);
  if (!canvas) return;
  _accCharts[id] = new Chart(canvas, {
    type: "line",
    data: {
      labels: rolling.map(r => `#${r.index}`),
      datasets: [{ label: "Rolling Hit %", data: rolling.map(r => r.rate),
        borderColor: "#00d68f", borderWidth: 2, pointRadius: 0,
        tension: 0.4, fill: false }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { grid: { display: false }, ticks: { color: "#7a8fb5", font: { size: 10 }, maxTicksLimit: 8 }, border: { color: "transparent" } },
        y: { min: 0, max: 100, grid: { color: "#232f4a" }, ticks: { color: "#7a8fb5", font: { size: 10 } }, border: { color: "transparent" } },
      },
      plugins: { legend: { display: false } },
    },
  });
}

/* ══════════════════════════════════════════════════════════════════════════ *
 *  Model Optimizer                                                            *
 * ══════════════════════════════════════════════════════════════════════════ */
async function loadWeights() {
  try {
    const data = await apiFetch("/api/model/weights");
    _displayWeights(data.weights, null);

    // Disable optimizer button if <30 settled prop bets
    const accData  = await apiFetch("/api/bets/accuracy");
    const propSettled = Object.values(accData.by_prop || {}).reduce((s, v) => s + (v > 0 ? 1 : 0), 0);
    // Re-check with raw count from accuracy total
    const total = accData.total ?? 0;
    if (total < 30) {
      document.getElementById("run-optimizer-btn").disabled = true;
      document.getElementById("opt-disabled-msg").classList.remove("hidden");
    }
  } catch (_) { /* silent */ }
}

function _displayWeights(weights, newWeights) {
  const fmt = v => v != null ? `${(v * 100).toFixed(1)}%` : "—";
  const diff = (oldV, newV) => {
    if (newV == null || oldV == null) return "";
    const d = ((newV - oldV) * 100).toFixed(1);
    const cls = d > 0 ? "up" : d < 0 ? "down" : "";
    return `<span class="opt-diff ${cls}">${d > 0 ? "+" : ""}${d}%</span>`;
  };

  document.getElementById("ow-l5").textContent    = fmt(weights?.W_L5);
  document.getElementById("ow-l10").textContent   = fmt(weights?.W_L10);
  document.getElementById("ow-season").textContent = fmt(weights?.W_SEASON);
  document.getElementById("ow-opp").textContent   = fmt(weights?.OPP_CAP);
  document.getElementById("ow-split").textContent = fmt(weights?.SPLIT_CAP);

  if (newWeights) {
    document.getElementById("od-l5").innerHTML    = diff(weights?.W_L5,     newWeights?.W_L5);
    document.getElementById("od-l10").innerHTML   = diff(weights?.W_L10,    newWeights?.W_L10);
    document.getElementById("od-season").innerHTML = diff(weights?.W_SEASON, newWeights?.W_SEASON);
  }
}

async function runOptimizer() {
  const btn     = document.getElementById("run-optimizer-btn");
  const spinner = document.getElementById("opt-spinner");
  const result  = document.getElementById("opt-result");
  const applyBtn = document.getElementById("apply-weights-btn");

  btn.disabled = true;
  spinner.classList.remove("hidden");
  result.classList.add("hidden");
  applyBtn.classList.add("hidden");

  try {
    const data = await apiFetch("/api/model/optimize", { method: "POST" });
    _newWeights = data.new_weights;

    _displayWeights(data.old_weights, data.new_weights);

    const imp = data.improvement;
    result.innerHTML = `
      <div class="opt-result-row">
        <span>MAE before: <b>${data.old_mae}</b></span>
        <span>MAE after: <b>${data.new_mae}</b></span>
        <span class="${imp > 0 ? "green" : "red"}">${imp > 0 ? "▼" : "▲"} ${Math.abs(imp).toFixed(4)} pts improvement</span>
        <span class="muted-text small">(${data.sample_size} bets)</span>
      </div>`;
    result.classList.remove("hidden");
    applyBtn.classList.remove("hidden");
    showToast("Optimizer complete — review new weights below");
  } catch (err) {
    result.innerHTML = `<p class="muted-text small" style="color:var(--red)">${err.message}</p>`;
    result.classList.remove("hidden");
    showToast(`Optimizer failed: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
    spinner.classList.add("hidden");
  }
}

async function applyWeights() {
  if (!_newWeights) return;
  showToast("Model weights updated — projections will use new weights");
  document.getElementById("apply-weights-btn").classList.add("hidden");
  _newWeights = null;
}
