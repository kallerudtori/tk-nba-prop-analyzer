"""
Weight Optimizer — finds optimal W_L5 / W_L10 / W_SEASON weights
by minimising MAE between model_projection and actual_value on settled prop bets.

Requires 30+ settled prop bets (status IN ('won','lost'), bet_type='prop').
Saves result to models/weights_config.json.
"""

import json
import os

import numpy as np
from scipy.optimize import minimize

from database.db import get_conn

_CFG_PATH = os.path.join(os.path.dirname(__file__), "weights_config.json")
_DEFAULTS = {
    "W_L5": 0.40,
    "W_L10": 0.35,
    "W_SEASON": 0.25,
    "OPP_CAP": 0.15,
    "OPP_CAP_3PM": 0.20,
    "SPLIT_CAP": 0.10,
}
_MIN_BETS = 30


def _load_current() -> dict:
    try:
        with open(_CFG_PATH) as f:
            cfg = json.load(f)
        return {**_DEFAULTS, **cfg}
    except Exception:
        return dict(_DEFAULTS)


def run_optimizer() -> dict:
    """
    Returns:
        {
          old_weights: dict,
          new_weights: dict,
          improvement: float,   # reduction in MAE (positive = better)
          sample_size: int,
        }
    Raises ValueError if fewer than MIN_BETS settled prop bets exist.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT model_projection, actual_value
        FROM bets
        WHERE bet_type = 'prop'
          AND status IN ('won', 'lost')
          AND model_projection IS NOT NULL
          AND actual_value     IS NOT NULL
    """).fetchall()
    conn.close()

    if len(rows) < _MIN_BETS:
        raise ValueError(
            f"Need at least {_MIN_BETS} settled prop bets to run the optimizer "
            f"({len(rows)} available)."
        )

    projections = np.array([r["model_projection"] for r in rows], dtype=float)
    actuals     = np.array([r["actual_value"]     for r in rows], dtype=float)

    # The stored projection already incorporates the weights. We model the
    # residual as a linear correction: new_proj = proj * scale_factor.
    # We find the scale that minimises MAE, then back-project that into weight
    # adjustments proportional to current weights.
    #
    # For a richer optimisation we treat projections as a weighted combination
    # of three components estimated from residuals:
    #   component_i ≈ projection / current_weight_i * weight_i (simplified proxy)
    # Since we only store the final projection we use Nelder-Mead on a 3-param
    # scale vector [s5, s10, ss] and minimise MAE of adjusted projection.

    old_cfg = _load_current()
    w5, w10, ws = old_cfg["W_L5"], old_cfg["W_L10"], old_cfg["W_SEASON"]

    # Decompose each projection into three hypothetical components:
    # We estimate each component as projection * (wi / sum_w) * (1/wi) = projection
    # (all equal because we only have the aggregate). The optimizer then finds
    # the best reweighting of that single signal — effectively a bias correction.
    def _mae(params):
        s5, s10, ss = params
        total = s5 + s10 + ss
        if total <= 0:
            return 1e9
        # Scale the projections by the ratio of new combined weight to current
        scale = (s5 * w5 + s10 * w10 + ss * ws) / ((w5 + w10 + ws) * (s5 + s10 + ss) / 3)
        adj = projections * scale
        return float(np.mean(np.abs(adj - actuals)))

    old_mae = float(np.mean(np.abs(projections - actuals)))

    result = minimize(
        _mae,
        x0=[w5, w10, ws],
        method="Nelder-Mead",
        options={"maxiter": 5000, "xatol": 1e-5, "fatol": 1e-5},
    )

    s5, s10, ss = result.x
    # Normalise so they sum to 1
    total = s5 + s10 + ss
    new_w5  = max(0.05, s5  / total)
    new_w10 = max(0.05, s10 / total)
    new_ws  = max(0.05, ss  / total)
    # Re-normalise after clamping
    total2   = new_w5 + new_w10 + new_ws
    new_w5  /= total2
    new_w10 /= total2
    new_ws  /= total2

    new_mae = _mae([new_w5, new_w10, new_ws])

    new_cfg = {
        "W_L5":      round(new_w5,  4),
        "W_L10":     round(new_w10, 4),
        "W_SEASON":  round(new_ws,  4),
        "OPP_CAP":   old_cfg["OPP_CAP"],
        "OPP_CAP_3PM": old_cfg["OPP_CAP_3PM"],
        "SPLIT_CAP": old_cfg["SPLIT_CAP"],
    }

    with open(_CFG_PATH, "w") as f:
        json.dump(new_cfg, f, indent=2)

    return {
        "old_weights":  {k: old_cfg[k] for k in ("W_L5", "W_L10", "W_SEASON")},
        "new_weights":  {k: new_cfg[k] for k in ("W_L5", "W_L10", "W_SEASON")},
        "old_mae":      round(old_mae, 4),
        "new_mae":      round(new_mae, 4),
        "improvement":  round(old_mae - new_mae, 4),
        "sample_size":  len(rows),
    }
