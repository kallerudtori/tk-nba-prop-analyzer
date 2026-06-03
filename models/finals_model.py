"""
Finals Adjustment Model (Feature 2)

A *clean overlay* applied on top of the standard projection ONLY when Finals
Mode is enabled. When the toggle is off this module is never invoked, so the
base model is mathematically unchanged (no regressions).

Three-tier weighting (per spec):
  • Tier 1 — Finals-specific: the player's performance in THIS Finals series so
    far. Its influence grows with the game number (G1 ≈ 0% → G5+ ≈ 85%).
  • Tier 2 — Playoff context: already baked into the base projection (the stats
    service blends Playoffs/Play-In game logs).
  • Tier 3 — Structural baseline: the base projection itself. Injury impact —
    which the base projection already applied — remains the #1 structural input.

Prop-specific Finals adjustments:
  • Pace penalty       — Finals run 2-4 fewer possessions; trims volume stats.
  • Star isolation (G3+) — elite scorers get more late-clock isolation looks.
  • Bench compression  — rotations shorten; deep-bench volume is trimmed.
  • First-Finals regression (optional) — inexperienced players regress slightly.
"""

# ── Tier-1 dynamic weight ─────────────────────────────────────────────────────
FINALS_HISTORY_CAP = 0.85


def finals_history_weight(game_number: int) -> float:
    """
    How much the player's in-series Finals average overrides the base
    projection. G1 has no series history yet → 0. Ramps ~0.21/game to the cap.
    """
    g = max(1, int(game_number or 1))
    if g <= 1:
        return 0.0
    return round(min(FINALS_HISTORY_CAP, (g - 1) * 0.21), 3)


# ── Prop adjustment constants ─────────────────────────────────────────────────
# Pace penalty grows as the series tightens (more deliberate offense).
def _pace_factor(game_number: int) -> float:
    g = max(1, int(game_number or 1))
    if g <= 2:
        return 0.98   # ~ -2 possessions
    if g <= 4:
        return 0.97   # ~ -3 possessions
    return 0.96       # ~ -4 possessions


STAR_PTS_THRESHOLD   = 22.0     # base points projection that marks a focal scorer
STAR_ISO_PREMIUM     = 1.05     # +5% scoring for stars in G3+
BENCH_MIN_THRESHOLD  = 15.0     # below this baseline = deep bench
BENCH_COMPRESSION    = 0.90     # -10% volume for deep bench
FIRST_FINALS_REGRESS = 0.95     # -5% for first-Finals players (optional flag)

# Which stats each adjustment touches
_VOLUME_PROPS = ("points", "pra", "threes")        # most pace-sensitive
_ALL_PROPS    = ("points", "rebounds", "assists", "pra", "threes")
_STAT_KEY     = {"points": "pts", "rebounds": "reb", "assists": "ast",
                 "pra": "pra", "threes": "fg3m"}


class FinalsAdjuster:
    """Overlay Finals adjustments onto an already-computed projection dict."""

    def adjust(self, projections: dict, ctx: dict) -> dict:
        """
        Args:
            projections: the standard model output
                         {prop: {projection, std_dev, confidence, ...}}
            ctx: {
              "game_number":   int,
              "finals_stats":  dict | None,   # player's series avg (per finals svc)
              "base_minutes":  float,         # season baseline minutes
              "first_finals":  bool,          # optional, default False
            }

        Returns a NEW dict (does not mutate input) with adjusted projections and
        a per-prop "finals" block describing what changed, plus a top-level
        "_finals_meta" summary.
        """
        game_number = int(ctx.get("game_number", 1) or 1)
        finals_stats = ctx.get("finals_stats") or None
        base_minutes = float(ctx.get("base_minutes", 0.0) or 0.0)
        first_finals = bool(ctx.get("first_finals", False))

        w_hist   = finals_history_weight(game_number)
        pace     = _pace_factor(game_number)
        is_bench = 0 < base_minutes < BENCH_MIN_THRESHOLD
        base_pts = float(projections.get("points", {}).get("projection", 0.0) or 0.0)
        is_star  = base_pts >= STAR_PTS_THRESHOLD

        out = {}
        notes_global = []

        for prop in _ALL_PROPS:
            src = projections.get(prop)
            if not src:
                continue
            new = dict(src)  # copy; never mutate caller's dict
            base_val = float(src.get("projection", 0.0) or 0.0)
            adj_notes = []
            val = base_val

            # ── Tier 1: blend in the player's in-series Finals average ──────
            blended_from = None
            if finals_stats and w_hist > 0:
                fkey = _STAT_KEY[prop]
                fstat = finals_stats.get(fkey)
                if fstat is not None and fstat > 0:
                    blended_from = round(fstat, 1)
                    val = base_val * (1 - w_hist) + fstat * w_hist
                    adj_notes.append(
                        f"Blended {int(w_hist*100)}% Finals form ({blended_from})"
                    )

            # ── Pace penalty (volume props) ─────────────────────────────────
            if prop in _VOLUME_PROPS:
                val *= pace
                adj_notes.append(f"Pace ×{pace}")

            # ── Star isolation premium (G3+) ────────────────────────────────
            if is_star and game_number >= 3 and prop in ("points", "pra"):
                val *= STAR_ISO_PREMIUM
                adj_notes.append(f"Star iso ×{STAR_ISO_PREMIUM}")

            # ── Bench compression ───────────────────────────────────────────
            if is_bench and prop in _VOLUME_PROPS:
                val *= BENCH_COMPRESSION
                adj_notes.append(f"Bench compression ×{BENCH_COMPRESSION}")

            # ── First-Finals regression (optional) ──────────────────────────
            if first_finals and prop in _VOLUME_PROPS:
                val *= FIRST_FINALS_REGRESS
                adj_notes.append(f"First-Finals ×{FIRST_FINALS_REGRESS}")

            new["projection"] = round(val, 1)
            new["finals"] = {
                "base":         round(base_val, 1),
                "adjusted":     round(val, 1),
                "delta":        round(val - base_val, 1),
                "history_wt":   w_hist,
                "blended_from": blended_from,
                "notes":        adj_notes,
            }
            out[prop] = new

        notes_global.append(f"Game {game_number}: Finals-form weight {int(w_hist*100)}%")
        notes_global.append(f"Pace factor ×{pace}")
        if is_star:
            notes_global.append("Focal scorer — isolation premium" if game_number >= 3
                                else "Focal scorer")
        if is_bench:
            notes_global.append("Deep-bench — rotation compression applied")
        if first_finals:
            notes_global.append("First Finals — regression applied")

        out["_finals_meta"] = {
            "game_number":     game_number,
            "history_weight":  w_hist,
            "pace_factor":     pace,
            "is_star":         is_star,
            "is_bench":        is_bench,
            "has_finals_data": bool(finals_stats),
            "finals_games":    (finals_stats or {}).get("games", 0),
            "notes":           notes_global,
        }
        return out
