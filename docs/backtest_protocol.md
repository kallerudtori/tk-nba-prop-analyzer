# NFL game model — pre-registered backtest protocol

**Status: pre-registered. Committed before the first backtest was run.**

This document exists so the acceptance criteria cannot be rewritten after
seeing the results. If a criterion below is changed, the change must be a
separate commit, made before the run it applies to, with a stated reason.

---

## Why this is not the protocol originally specified

The original spec called for training on seasons ≤2023 and testing on
2024–2025. That holdout is 544 regular-season games. The standard error on a
hit rate at n=544 is ±2.1%, which puts the 52.4% break-even point 1.1σ from a
coin flip.

A single-split test at that sample size cannot distinguish a working model from
a broken one in either direction. A 55% reading would not prove an edge, and a
49% reading would not disprove one.

`core.calibration.required_sample_size(0.024)` returns **3,403 games** — roughly
twelve seasons — to detect a true 52.4% edge at 80% power. That number is
computed, not asserted, and is printed in the Performance tab beside any claim
of an edge.

## The protocol

### Primary: walk-forward validation, 2006–2025

Train on all seasons through year *N*, predict season *N+1*, roll forward.
Every game is predicted by a model that never saw it. Approximately **5,300
out-of-sample games**, standard error ±0.7%, at which point 52.4% is a ~3.5σ
result.

This also shows whether an edge is *stable across eras* or concentrated in one
lucky stretch — a question a single split cannot answer at all.

### Iteration: 2006–2019 train, 2020–2022 validate

All model iteration, feature selection, and hyperparameter choice happens
against the validation seasons. The holdout is not consulted.

### Final: 2023–2025, single touch

Read **once**, at the end, after the model is frozen. Every read is written to
`model_changelog` with `event='holdout_read'`, so the number of touches is
auditable rather than remembered.

---

## Acceptance criteria

Evaluated in this order. A model failing an earlier criterion is not rescued by
a later one.

### 1. Margin accuracy versus the closing line (gate)

Report model RMSE and MAE against actual margin, alongside the closing line's
RMSE and MAE **on the same games**.

The line is expected to win this comparison. That is not a failure — it is the
baseline. What matters is the size of the gap and whether model errors are
decorrelated from line errors in identifiable buckets.

**Gate:** if model RMSE exceeds the closing line's by more than 1.5 points, the
model is not competitive anywhere and no downstream filtering can rescue it.
Stop and fix inputs.

### 2. Edge-threshold monotonicity (the real test)

ATS hit rate as a function of |projection − closing line|, reported with n at
each threshold:

| threshold | n | ATS% | 95% CI |
|---|---|---|---|
| ≥ 1.0 | | | |
| ≥ 2.0 | | | |
| ≥ 3.0 | | | |
| ≥ 4.0 | | | |
| ≥ 6.0 | | | |

**Pass:** hit rate rises monotonically with threshold, and the rise is smooth
rather than driven by one sparse bucket.

**Fail:** jagged, non-monotone, or peaking mid-range. That shape means the
threshold was fitted to noise, and the apparent edge will not survive contact
with next season.

This curve — not the headline hit rate — sets the production pick threshold in
`core.picks.DEFAULT_EDGE_THRESHOLD`. Until it exists, those values are
placeholders and every pick they generate is flagged provisional.

### 3. Calibration

Brier score and a reliability curve over predicted win probability. Report
`calibration_error` from `core.calibration.evaluate`.

**Pass:** calibration error below 0.05 and a reliability curve that tracks the
diagonal without systematic over- or under-confidence.

### 4. ATS hit rate (reported, not optimised)

Full-slate ATS hit rate with a Wilson interval, on walk-forward.

**Pass:** lower bound of the 95% interval above 0.500, and the point estimate
above 0.524.

If the point estimate clears 52.4% but the interval's lower bound does not, the
honest statement is "consistent with an edge, not yet demonstrated" — and that
is what the report will say.

---

## Prohibited

- Trying feature sets until the holdout number improves
- Moving the holdout window after seeing results
- Discovering a filter on the holdout and applying it retroactively
- Dropping seasons (e.g. 2020 for empty stadiums) after seeing that they hurt.
  Any season exclusion is decided here, in advance, or not at all.
- Reporting a tuned number without reporting how many configurations were tried

## Season exclusions decided in advance

**None.** 2020 is included despite empty stadiums. Home-field advantage is a
fitted parameter with a time trend rather than a constant, so the model can
absorb that shift rather than needing the season removed.

---

## If the model fails

Failure is a real possibility and is reported plainly, with the monotonicity
curve and the RMSE comparison, rather than tuned away. Documented fallbacks, in
order of preference:

1. **Totals only.** Weather and pace effects on totals are more exploitable than
   sides; the market is thinner there and wind is systematically underpriced.
2. **Props only.** The prop model starts cold regardless of how the game model
   performs, and prop markets are materially less efficient than NFL sides.
3. **CLV-only shadow mode.** The model publishes projections and captures
   closing lines without recommending stakes. If it beats the close over 6–8
   weeks of live data, it earns the right to make picks.

Shadow mode is a legitimate resting state for this system, not a failure state.
