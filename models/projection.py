"""
Statistical Projection Model

Weights:    Last-5  40% | Last-10  35% | Season  25%
Adjustments: Opponent defense · Home/Away split · Minutes trend
Confidence: based on sample size + coefficient of variation
"""

import math
from scipy import stats as scipy_stats


# ── Edge thresholds ──────────────────────────────────────────────────────────
STRONG_VALUE = 2.5
SLIGHT_VALUE = 1.0

# ── Projection weights ───────────────────────────────────────────────────────
W_L5 = 0.40
W_L10 = 0.35
W_SEASON = 0.25

# ── Max adjustment caps ──────────────────────────────────────────────────────
OPP_CAP = 0.15      # ±15 % opponent factor
SPLIT_CAP = 0.10    # ±10 % home/away factor
MIN_CAP = 0.15      # ±15 % minutes trend factor
SPLIT_THRESHOLD = 0.05   # Only apply split if |factor-1| > 5 %
MIN_THRESHOLD = 0.05     # Only apply min trend if |factor-1| > 5 %


class ProjectionModel:

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def calculate_projection(
        self,
        player_stats: dict,
        opponent_defense: dict | None,
        league_avg_defense: dict | None,
        is_home: bool,
    ) -> dict:
        """
        Returns projections for all four prop types:
            {
                'points':   { projection, std_dev, confidence, adjustments },
                'rebounds': { ... },
                'assists':  { ... },
                'pra':      { ... },
            }
        """
        result = {}
        for prop_key, stat_key in [
            ("points", "pts"),
            ("rebounds", "reb"),
            ("assists", "ast"),
            ("pra", "pra"),
        ]:
            result[prop_key] = self._project_single(
                player_stats,
                opponent_defense,
                league_avg_defense,
                is_home,
                stat_key,
            )
        return result

    def calculate_value_label(self, edge: float) -> str:
        if edge >= STRONG_VALUE:
            return "Strong Value"
        if edge >= SLIGHT_VALUE:
            return "Slight Value"
        return "Avoid"

    def calculate_model_probability(
        self, projection: float, std_dev: float, line: float
    ) -> float:
        """P(stat > line) using a Normal distribution N(projection, std_dev)."""
        if std_dev <= 0:
            return 0.55 if projection > line else 0.45
        z = (line - projection) / std_dev
        return round(float(1 - scipy_stats.norm.cdf(z)), 4)

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _project_single(
        self,
        ps: dict,
        opp_def: dict | None,
        lg_avg: dict | None,
        is_home: bool,
        stat_key: str,
    ) -> dict:
        season_avg = ps["season_avg"][stat_key]
        l5_avg = ps["last_5_avg"][stat_key]
        l10_avg = ps["last_10_avg"][stat_key]
        games_played = ps.get("games_played", 0)

        if season_avg == 0 and l5_avg == 0 and l10_avg == 0:
            return {
                "projection": 0.0,
                "std_dev": 0.0,
                "confidence": "Low",
                "adjustments": {},
            }

        # ── Step 1: Weighted base ────────────────────────────────────────
        if games_played >= 10:
            base = l5_avg * W_L5 + l10_avg * W_L10 + season_avg * W_SEASON
        elif games_played >= 5:
            base = l5_avg * 0.55 + season_avg * 0.45
        else:
            base = season_avg

        adj = {"base": round(base, 2)}

        # ── Step 2: Opponent defense adjustment ─────────────────────────
        opp_factor = 1.0
        if opp_def and lg_avg:
            opp_factor = self._opp_factor(stat_key, opp_def, lg_avg)

        after_opp = base * opp_factor
        adj["opp_factor"] = round(opp_factor, 3)

        # ── Step 3: Home / Away split ────────────────────────────────────
        split_avg = ps["home_avg"][stat_key] if is_home else ps["away_avg"][stat_key]
        split_factor = 1.0
        if season_avg > 0 and split_avg > 0:
            raw = split_avg / season_avg
            capped = max(1 - SPLIT_CAP, min(1 + SPLIT_CAP, raw))
            if abs(capped - 1.0) > SPLIT_THRESHOLD:
                split_factor = capped

        after_split = after_opp * split_factor
        adj["split_factor"] = round(split_factor, 3)

        # ── Step 4: Minutes trend ────────────────────────────────────────
        season_min = ps["season_avg"].get("min", 0)
        l5_min = ps["last_5_avg"].get("min", 0)
        min_factor = 1.0
        if season_min > 0 and l5_min > 0:
            raw = l5_min / season_min
            capped = max(1 - MIN_CAP, min(1 + MIN_CAP, raw))
            if abs(capped - 1.0) > MIN_THRESHOLD:
                min_factor = capped

        final = after_split * min_factor
        adj["min_factor"] = round(min_factor, 3)

        std_dev = ps["std_devs"].get(stat_key, 0.0)

        return {
            "projection": round(final, 1),
            "std_dev": round(std_dev, 2),
            "confidence": self._confidence(games_played, final, std_dev),
            "adjustments": adj,
        }

    @staticmethod
    def _opp_factor(stat_key: str, opp_def: dict, lg_avg: dict) -> float:
        """
        Compute how much the opponent allows relative to league average.
        PRA uses a weighted blend of the three sub-stats.
        """
        if stat_key == "pra":
            pts_f = _safe_ratio(opp_def.get("opp_pts", 0), lg_avg.get("opp_pts", 1))
            reb_f = _safe_ratio(opp_def.get("opp_reb", 0), lg_avg.get("opp_reb", 1))
            ast_f = _safe_ratio(opp_def.get("opp_ast", 0), lg_avg.get("opp_ast", 1))
            raw = pts_f * 0.50 + reb_f * 0.30 + ast_f * 0.20
        else:
            opp_key_map = {"pts": "opp_pts", "reb": "opp_reb", "ast": "opp_ast"}
            opp_key = opp_key_map.get(stat_key, "opp_pts")
            raw = _safe_ratio(opp_def.get(opp_key, 0), lg_avg.get(opp_key, 1))

        return max(1 - OPP_CAP, min(1 + OPP_CAP, raw))

    @staticmethod
    def _confidence(games_played: int, projection: float, std_dev: float) -> str:
        if games_played < 5 or projection <= 0:
            return "Low"
        cov = std_dev / projection  # coefficient of variation
        if games_played >= 20 and cov < 0.20:
            return "High"
        if games_played >= 10 and cov < 0.35:
            return "Medium"
        return "Low"


# ── Utility ──────────────────────────────────────────────────────────────────

def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 1.0
    return numerator / denominator
