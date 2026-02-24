/**
 * charts.js — Chart.js visualisation helpers
 * All charts are built with the dark premium palette.
 * Registry pattern prevents memory leaks when cards are removed/re-rendered.
 */

"use strict";

/* ── Palette ──────────────────────────────────────────────────────────────── */
const C = {
  green:     "#00d68f",
  greenDim:  "#00a870",
  gold:      "#ffd700",
  goldDim:   "#c9a800",
  red:       "#ff4757",
  blue:      "#4dabf7",
  blueDim:   "#2980b9",
  text:      "#7a8fb5",
  textMuted: "#4a5a7a",
  border:    "#232f4a",
  bg:        "#121828",
  bgCard:    "#18213a",
};

/* Global Chart.js defaults */
Chart.defaults.color               = C.text;
Chart.defaults.borderColor         = C.border;
Chart.defaults.font.family         = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
Chart.defaults.plugins.legend.display = false;
Chart.defaults.plugins.tooltip.backgroundColor = "#18213a";
Chart.defaults.plugins.tooltip.borderColor     = C.border;
Chart.defaults.plugins.tooltip.borderWidth     = 1;
Chart.defaults.plugins.tooltip.padding         = 10;
Chart.defaults.plugins.tooltip.titleColor      = "#e4e8f5";
Chart.defaults.plugins.tooltip.bodyColor       = C.text;

/* ── Registry ─────────────────────────────────────────────────────────────── */
const ChartRegistry = {};   // playerId → { recentForm, seasonTrend, homeAway }

function destroyPlayerCharts(playerId) {
  const group = ChartRegistry[playerId];
  if (!group) return;
  Object.values(group).forEach(c => { if (c) c.destroy(); });
  delete ChartRegistry[playerId];
}

function _register(playerId, key, chart) {
  if (!ChartRegistry[playerId]) ChartRegistry[playerId] = {};
  if (ChartRegistry[playerId][key]) ChartRegistry[playerId][key].destroy();
  ChartRegistry[playerId][key] = chart;
  return chart;
}

/* ── Shared axis config ───────────────────────────────────────────────────── */
function _scaleBase() {
  return {
    grid: { color: C.border, drawBorder: false },
    ticks: { color: C.text, font: { size: 10 } },
    border: { color: "transparent" },
  };
}

/* ── Stat key → label / colour ───────────────────────────────────────────── */
const PROP_META = {
  points:   { label: "PTS", rollingKey: "pts_r5", gameKey: "pts", color: C.blue },
  rebounds: { label: "REB", rollingKey: "reb_r5", gameKey: "reb", color: C.green },
  assists:  { label: "AST", rollingKey: "ast_r5", gameKey: "ast", color: C.gold },
  pra:      { label: "PRA", rollingKey: "pra_r5", gameKey: "pra", color: "#b48eff" },
};

/* ─────────────────────────────────────────────────────────────────────────── *
 *  1. Recent Form Bar Chart — last 10 games                                  *
 * ─────────────────────────────────────────────────────────────────────────── */
function renderRecentFormChart(canvasEl, playerId, propKey, last10Games, line, projection) {
  const meta = PROP_META[propKey] || PROP_META.points;
  const labels = last10Games.map(g => {
    const d = g.date || "";
    return d.replace(/,\s*\d{4}/, "");          // strip year
  });
  const values = last10Games.map(g => g[meta.gameKey] ?? 0);

  const overLine = line ?? projection;

  // Colour each bar: green if above line, red if below
  const barColors = values.map(v =>
    v > overLine ? "rgba(0,214,143,0.75)" : "rgba(255,71,87,0.65)"
  );

  const lineData  = Array(values.length).fill(line ?? null);
  const projData  = Array(values.length).fill(projection);

  const chart = new Chart(canvasEl, {
    data: {
      labels,
      datasets: [
        {
          type: "bar",
          label: meta.label,
          data: values,
          backgroundColor: barColors,
          borderRadius: 3,
          order: 3,
        },
        line != null && {
          type: "line",
          label: "DK Line",
          data: lineData,
          borderColor: C.gold,
          borderWidth: 2,
          borderDash: [],
          pointRadius: 0,
          fill: false,
          order: 1,
        },
        {
          type: "line",
          label: "Projection",
          data: projData,
          borderColor: C.green,
          borderWidth: 2,
          borderDash: [5, 4],
          pointRadius: 0,
          fill: false,
          order: 2,
        },
      ].filter(Boolean),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 400 },
      scales: {
        x: { ..._scaleBase(), grid: { display: false } },
        y: { ..._scaleBase(), beginAtZero: false },
      },
      plugins: {
        legend: {
          display: true,
          position: "top",
          labels: {
            color: C.text,
            boxWidth: 14,
            padding: 10,
            font: { size: 10 },
          },
        },
        tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.dataset.label}: ${ctx.parsed.y}`,
          },
        },
      },
    },
  });

  return _register(playerId, "recentForm", chart);
}

/* ─────────────────────────────────────────────────────────────────────────── *
 *  2. Season Trend Line Chart — rolling 5-game average                       *
 * ─────────────────────────────────────────────────────────────────────────── */
function renderSeasonTrendChart(canvasEl, playerId, propKey, seasonGames) {
  const meta = PROP_META[propKey] || PROP_META.points;
  const labels = seasonGames.map(g => {
    const d = g.date || "";
    return d.replace(/,\s*\d{4}/, "");
  });
  const values = seasonGames.map(g => g[meta.rollingKey] ?? 0);

  // Gradient fill — use the wrapper's fixed height (140px) since the canvas
  // hasn't been sized by Chart.js yet at gradient-creation time.
  const ctx = canvasEl.getContext("2d");
  const gradient = ctx.createLinearGradient(0, 0, 0, 140);
  gradient.addColorStop(0,   meta.color + "55");
  gradient.addColorStop(1,   meta.color + "00");

  const chart = new Chart(canvasEl, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "5-Game Rolling Avg",
          data: values,
          borderColor: meta.color,
          borderWidth: 2,
          backgroundColor: gradient,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0.4,
          fill: true,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 400 },
      scales: {
        x: {
          ..._scaleBase(),
          grid: { display: false },
          ticks: {
            color: C.text,
            font: { size: 9 },
            maxTicksLimit: 8,
            maxRotation: 0,
          },
        },
        y: { ..._scaleBase(), beginAtZero: false },
      },
      plugins: {
        tooltip: {
          callbacks: {
            label: ctx => ` R5 Avg: ${ctx.parsed.y}`,
          },
        },
      },
    },
  });

  return _register(playerId, "seasonTrend", chart);
}

/* ─────────────────────────────────────────────────────────────────────────── *
 *  3. Home vs Away Bar Chart                                                  *
 * ─────────────────────────────────────────────────────────────────────────── */
function renderHomeAwayChart(canvasEl, playerId, propKey, homeAvg, awayAvg) {
  const meta = PROP_META[propKey] || PROP_META.points;
  const hVal = homeAvg[meta.gameKey] ?? 0;
  const aVal = awayAvg[meta.gameKey] ?? 0;

  const chart = new Chart(canvasEl, {
    type: "bar",
    data: {
      labels: ["Home", "Away"],
      datasets: [
        {
          data: [hVal, aVal],
          backgroundColor: [
            "rgba(0,214,143,0.7)",
            "rgba(77,171,247,0.7)",
          ],
          borderColor: [C.green, C.blue],
          borderWidth: 1,
          borderRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      scales: {
        x: { ..._scaleBase(), grid: { display: false } },
        y: { ..._scaleBase(), beginAtZero: false },
      },
      plugins: {
        tooltip: {
          callbacks: {
            label: ctx => ` ${meta.label}: ${ctx.parsed.y}`,
          },
        },
      },
    },
  });

  return _register(playerId, "homeAway", chart);
}

/* ─────────────────────────────────────────────────────────────────────────── *
 *  4. Value Gauge (CSS-based, not Chart.js)                                  *
 * ─────────────────────────────────────────────────────────────────────────── */
function updateValueGauge(gaugeRow, edge) {
  if (!gaugeRow || edge == null) return;

  const fill   = gaugeRow.querySelector(".gauge-fill");
  const marker = gaugeRow.querySelector(".gauge-marker");
  if (!fill) return;

  // Map edge range [-5, +6] to [0%, 100%]
  const pct = Math.max(0, Math.min(100, ((edge + 5) / 11) * 100));

  let color;
  if (edge >= 2.5)      color = C.green;
  else if (edge >= 1.0) color = C.gold;
  else                  color = C.red;

  fill.style.width           = `${pct}%`;
  fill.style.backgroundColor = color;
  gaugeRow.classList.remove("hidden");
}

/* ─────────────────────────────────────────────────────────────────────────── *
 *  5. Render all charts for a player card                                     *
 * ─────────────────────────────────────────────────────────────────────────── */
function renderAllCharts(card, playerId, propKey, analysis) {
  const chartData  = analysis.chart_data;
  const prop       = analysis.props[propKey];
  const line       = prop.has_odds ? prop.line : null;
  const projection = prop.projection;
  const stats      = analysis.stats;

  // Recent Form
  const rcCanvas = card.querySelector(".chart-recent-form");
  if (rcCanvas) {
    renderRecentFormChart(
      rcCanvas, playerId, propKey,
      chartData.last_10_games, line, projection
    );
  }

  // Season Trend
  const stCanvas = card.querySelector(".chart-season-trend");
  if (stCanvas) {
    renderSeasonTrendChart(
      stCanvas, playerId, propKey,
      chartData.season_games
    );
  }

  // Home vs Away
  const haCanvas = card.querySelector(".chart-home-away");
  if (haCanvas) {
    renderHomeAwayChart(
      haCanvas, playerId, propKey,
      stats.home_avg, stats.away_avg
    );
  }

  // Adjustments breakdown (no Chart.js needed)
  const adjContainer = card.querySelector(".adj-items");
  if (adjContainer && prop.adjustments) {
    renderAdjBreakdown(adjContainer, prop.adjustments);
  }

  // Value gauge
  const gaugeRow = card.querySelector(".gauge-row");
  if (gaugeRow) {
    updateValueGauge(gaugeRow, prop.edge);
  }
}

/* ─────────────────────────────────────────────────────────────────────────── *
 *  6. Adjustments breakdown (pure HTML)                                       *
 * ─────────────────────────────────────────────────────────────────────────── */
function renderAdjBreakdown(container, adjustments) {
  const items = [
    { label: "Base (weighted avg)", key: "base", format: v => v.toFixed(1) },
    {
      label: "Opponent D",
      key: "opp_factor",
      format: v => v === 1.0 ? "Neutral" : `${v > 1 ? "+" : ""}${((v - 1) * 100).toFixed(0)}%`,
      isFactorKey: true,
    },
    {
      label: "Home/Away",
      key: "split_factor",
      format: v => v === 1.0 ? "Neutral" : `${v > 1 ? "+" : ""}${((v - 1) * 100).toFixed(0)}%`,
      isFactorKey: true,
    },
    {
      label: "Minutes Trend",
      key: "min_factor",
      format: v => v === 1.0 ? "Neutral" : `${v > 1 ? "+" : ""}${((v - 1) * 100).toFixed(0)}%`,
      isFactorKey: true,
    },
  ];

  container.innerHTML = items
    .filter(item => adjustments[item.key] != null)
    .map(item => {
      const v = adjustments[item.key];
      const display = item.format(v);
      let cls = "flat";
      if (item.isFactorKey) {
        if (v > 1.005) cls = "up";
        else if (v < 0.995) cls = "down";
      }
      return `
        <div class="adj-item">
          <span class="adj-item-label">${item.label}</span>
          <span class="adj-item-val ${cls}">${display}</span>
        </div>`;
    })
    .join("");
}
