"""
Statistical Projection Model

Weights:    Last-5  40% | Last-10  35% | Season  25%
Adjustments: Opponent defense · Home/Away split · Minutes trend · Back-to-Back
Confidence: based on sample size + coefficient of variation + minutes volatility
"""

import json
import math
import os
from scipy import stats as scipy_stats


# ── Edge thresholds ──────────────────────────────────────────────────────────
STRONG_VALUE = 2.5
SLIGHT_VALUE = 1.0

# ── Hardcoded defaults (overridden by weights_config.json if present) ────────
_W_L5_DEFAULT     = 0.40
_W_L10_DEFAULT    = 0.35
_W_SEASON_DEFAULT = 0.25
_OPP_CAP_DEFAULT     = 0.15
_OPP_CAP_3PM_DEFAULT = 0.20
_SPLIT_CAP_DEFAULT   = 0.10

# ── Max adjustment caps (non-weight) ─────────────────────────────────────────
MIN_CAP = 0.15       # ±15 % minutes trend factor
SPLIT_THRESHOLD = 0.05
MIN_THRESHOLD   = 0.05

_CFG_PATH = os.path.join(os.path.dirname(__file__), "weights_config.json")


def _load_weights() -> tuple:
    """Load weights from weights_config.json, falling back to defaults."""
    try:
        with open(_CFG_PATH) as f:
            cfg = json.load(f)
        return (
            float(cfg.get("W_L5",      _W_L5_DEFAULT)),
            float(cfg.get("W_L10",     _W_L10_DEFAULT)),
            float(cfg.get("W_SEASON",  _W_SEASON_DEFAULT)),
            float(cfg.get("OPP_CAP",   _OPP_CAP_DEFAULT)),
            float(cfg.get("OPP_CAP_3PM", _OPP_CAP_3PM_DEFAULT)),
            float(cfg.get("SPLIT_CAP", _SPLIT_CAP_DEFAULT)),
        )
    except Exception:
        return (_W_L5_DEFAULT, _W_L10_DEFAULT, _W_SEASON_DEFAULT,
                _OPP_CAP_DEFAULT, _OPP_CAP_3PM_DEFAULT, _SPLIT_CAP_DEFAULT)


class ProjectionModel:

    def __init__(self):
        (self.W_L5, self.W_L10, self.W_SEASON,
         self.OPP_CAP, self.OPP_CAP_3PM, self.SPLIT_CAP) = _load_weights()

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
        Returns projections for all five prop types:
            {
                'points':   { projection, std_dev, confidence, adjustments },
                'rebounds': { ... },
                'assists':  { ... },
                'pra':      { ... },
                'threes':   { ... },
            }
        """
        result = {}
        for prop_key, stat_key in [
            ("points", "pts"),
            ("rebounds", "reb"),
            ("assists", "ast"),
            ("pra", "pra"),
            ("threes", "threes"),
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
            base = l5_avg * self.W_L5 + l10_avg * self.W_L10 + season_avg * self.W_SEASON
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
            capped = max(1 - self.SPLIT_CAP, min(1 + self.SPLIT_CAP, raw))
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

        after_min = after_split * min_factor
        adj["min_factor"] = round(min_factor, 3)

        # ── Step 5: Back-to-back fatigue ─────────────────────────────────
        b2b_factor = 0.96 if ps.get("is_back_to_back", False) else 1.0
        final = after_min * b2b_factor
        adj["b2b_factor"] = round(b2b_factor, 3)

        std_dev = ps["std_devs"].get(stat_key, 0.0)
        minutes_cv = ps.get("minutes_cv", 0.0)

        return {
            "projection": round(final, 1),
            "std_dev": round(std_dev, 2),
            "confidence": self._confidence(games_played, final, std_dev, minutes_cv),
            "adjustments": adj,
        }

    def _opp_factor(self, stat_key: str, opp_def: dict, lg_avg: dict) -> float:
        """
        Compute how much the opponent allows relative to league average.
        PRA uses a weighted blend of the three sub-stats.
        Threes use a wider cap (±20%) to reflect higher variance.
        """
        if stat_key == "pra":
            pts_f = _safe_ratio(opp_def.get("opp_pts", 0), lg_avg.get("opp_pts", 1))
            reb_f = _safe_ratio(opp_def.get("opp_reb", 0), lg_avg.get("opp_reb", 1))
            ast_f = _safe_ratio(opp_def.get("opp_ast", 0), lg_avg.get("opp_ast", 1))
            raw = pts_f * 0.50 + reb_f * 0.30 + ast_f * 0.20
            return max(1 - self.OPP_CAP, min(1 + self.OPP_CAP, raw))
        elif stat_key == "threes":
            raw = _safe_ratio(opp_def.get("opp_fg3m", 0), lg_avg.get("opp_fg3m", 1))
            return max(1 - self.OPP_CAP_3PM, min(1 + self.OPP_CAP_3PM, raw))
        else:
            opp_key_map = {"pts": "opp_pts", "reb": "opp_reb", "ast": "opp_ast"}
            opp_key = opp_key_map.get(stat_key, "opp_pts")
            raw = _safe_ratio(opp_def.get(opp_key, 0), lg_avg.get(opp_key, 1))
            return max(1 - self.OPP_CAP, min(1 + self.OPP_CAP, raw))

    @staticmethod
    def _confidence(
        games_played: int,
        projection: float,
        std_dev: float,
        minutes_cv: float = 0.0,
    ) -> str:
        if games_played < 5 or projection <= 0:
            return "Low"
        cov = std_dev / projection  # coefficient of variation
        if games_played >= 20 and cov < 0.20:
            raw = "High"
        elif games_played >= 10 and cov < 0.35:
            raw = "Medium"
        else:
            raw = "Low"

        # Downgrade one tier if playing time is volatile (high CV of minutes)
        if minutes_cv > 0.25:
            if raw == "High":
                raw = "Medium"
            elif raw == "Medium":
                raw = "Low"

        return raw


# ── Utility ──────────────────────────────────────────────────────────────────

def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 1.0
    return numerator / denominator
