"""
Odds arithmetic. Pure functions, no I/O, no sport knowledge.

Kept separate from the projection layer on purpose: nothing in here may ever be
imported by a module that produces projections, because every function in here
takes a market price as input.
"""
from __future__ import annotations

import math

EPS = 1e-9


# ── Conversions ──────────────────────────────────────────────────────────────

def american_to_decimal(american: int | float) -> float:
    a = float(american)
    if a >= 0:
        return a / 100.0 + 1.0
    return 100.0 / abs(a) + 1.0


def decimal_to_american(decimal: float) -> int:
    d = float(decimal)
    if d <= 1.0:
        raise ValueError(f"decimal odds must exceed 1.0, got {d}")
    if d >= 2.0:
        return round((d - 1.0) * 100.0)
    return round(-100.0 / (d - 1.0))


def american_to_prob(american: int | float) -> float:
    """Implied probability, vig included. This is NOT a fair probability."""
    a = float(american)
    if a >= 0:
        return 100.0 / (a + 100.0)
    return abs(a) / (abs(a) + 100.0)


def prob_to_american(prob: float) -> int:
    if not 0.0 < prob < 1.0:
        raise ValueError(f"probability must be in (0,1), got {prob}")
    return decimal_to_american(1.0 / prob)


# ── De-vigging ───────────────────────────────────────────────────────────────

def devig_two_way(price_a: int | float, price_b: int | float) -> tuple[float, float]:
    """
    Remove the book's margin from a two-way market, proportionally.

    The raw implied probabilities of both sides sum to more than 1; the excess
    is the vig. Comparing a model probability against a raw implied probability
    overstates edge by roughly half the vig on every single bet, which is
    enough to turn a losing model into one that looks profitable on paper.
    """
    pa = american_to_prob(price_a)
    pb = american_to_prob(price_b)
    total = pa + pb
    if total <= EPS:
        raise ValueError("degenerate two-way market")
    return pa / total, pb / total


def overround(*prices: int | float) -> float:
    """Book margin as a fraction above 1.0 (0.045 ≈ a standard -110/-110 market)."""
    return sum(american_to_prob(p) for p in prices) - 1.0


# ── Expected value ───────────────────────────────────────────────────────────

def expected_value(prob: float, american: int | float, stake: float = 1.0) -> float:
    """EV of a bet at `american` odds given a true win probability `prob`."""
    dec = american_to_decimal(american)
    return prob * (dec - 1.0) * stake - (1.0 - prob) * stake


def breakeven_prob(american: int | float) -> float:
    """Win rate needed to break even. -110 → 0.5238."""
    return american_to_prob(american)


# ── Staking ──────────────────────────────────────────────────────────────────

def kelly_fraction(prob: float, american: int | float) -> float:
    """
    Full-Kelly stake as a fraction of bankroll. Negative means no bet.

        f* = (bp - q) / b     where b = decimal odds - 1
    """
    b = american_to_decimal(american) - 1.0
    if b <= EPS:
        return 0.0
    q = 1.0 - prob
    return (b * prob - q) / b


def staked_units(
    prob: float,
    american: int | float,
    fraction: float = 0.25,
    max_units: float = 2.0,
    calibration_scale: float = 1.0,
) -> float:
    """
    Fractional Kelly, capped, and additionally scaled by how well calibrated
    the model currently is.

    Kelly assumes the probability you feed it is correct. On an uncalibrated
    model it overbets badly and ruins bankrolls faster than a flat stake would,
    so `calibration_scale` (0..1, derived from the model's own calibration
    tracking) shrinks the stake while the model has not yet earned trust. A
    brand-new model with no graded predictions should pass 0.0 here and get a
    display-only number.
    """
    f = kelly_fraction(prob, american)
    if f <= 0:
        return 0.0
    units = f * fraction * max(0.0, min(1.0, calibration_scale))
    # Express in "units" where 1 unit = 1% of bankroll, then hard-cap.
    return round(min(units * 100.0, max_units), 3)


# ── Scoring ──────────────────────────────────────────────────────────────────

def brier_score(prob: float, outcome: int) -> float:
    """Squared error of a probabilistic forecast. Lower is better; 0.25 = coin flip."""
    return (prob - outcome) ** 2


def log_loss(prob: float, outcome: int, clip: float = 1e-6) -> float:
    p = max(clip, min(1.0 - clip, prob))
    return -(math.log(p) if outcome else math.log(1.0 - p))
