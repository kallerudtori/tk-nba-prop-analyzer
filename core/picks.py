"""
The market join — steps 3 and 4 of the pipeline.

A pick takes an already-stored prediction and a market snapshot and computes
edge as `projection - market`, in that direction, as a separate downstream
step. This module is the only place in the projection-to-pick path allowed to
import odds math.

Selectivity lives here too. The system is built to return nothing: if no
prediction clears its edge threshold, no pick is produced, and that is a
normal, correct outcome. Nothing pads the slate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from core.config import settings
from core.models import LineSnapshot, Pick, Prediction
from core.odds_math import devig_two_way, staked_units

logger = logging.getLogger(__name__)

# Minimum edge before a projection is worth acting on. Derived from the
# backtest's edge-threshold curve rather than picked by feel — see
# docs/backtest_protocol.md. Until that curve exists these are placeholders,
# and every pick they generate is flagged provisional.
DEFAULT_EDGE_THRESHOLD = {
    "spread": 2.0,
    "total": 2.5,
    "moneyline": 0.04,   # probability points, not line points
    "prop": 0.5,
}


@dataclass
class MarketQuote:
    """A single side of a market at a point in time."""
    market: str
    selection: str
    side: str | None
    line: float | None
    price: int
    book: str
    opposite_price: int | None = None
    snapshot_id: int | None = None


def fair_probability(quote: MarketQuote) -> float | None:
    """De-vigged market probability, when we have both sides of the market."""
    if quote.opposite_price is None:
        return None
    fair, _ = devig_two_way(quote.price, quote.opposite_price)
    return fair


def compute_edge(prediction: Prediction, quote: MarketQuote) -> float | None:
    """
    Edge = projection - market. Never the reverse, and never computed inside
    the projection layer.

    For spreads and totals this is in points. For moneyline it is in
    probability, against the de-vigged market price rather than the raw implied
    probability — comparing a model probability to a vig-inclusive number
    manufactures roughly half the vig as fake edge on every bet.
    """
    if prediction.target == "moneyline":
        if prediction.win_probability is None:
            return None
        fair = fair_probability(quote)
        if fair is None:
            return None
        return float(prediction.win_probability) - fair

    if prediction.projection is None or quote.line is None:
        return None
    return float(prediction.projection) - float(quote.line)


def confidence_tier(
    edge: float, target: str, thresholds: dict | None = None
) -> str | None:
    """
    Map raw edge onto a coarse tier. Returns None below the acting threshold,
    which is how "no pick" is expressed.
    """
    thresholds = thresholds or DEFAULT_EDGE_THRESHOLD
    floor = thresholds.get(target, DEFAULT_EDGE_THRESHOLD.get(target, 1.0))
    magnitude = abs(edge)
    if magnitude < floor:
        return None
    if magnitude >= floor * 2.0:
        return "high"
    if magnitude >= floor * 1.4:
        return "medium"
    return "low"


def build_pick(
    session: Session, *, prediction: Prediction, quote: MarketQuote,
    model_probability: float | None = None, calibration_scale: float = 0.0,
    thresholds: dict | None = None, is_provisional: bool = True,
    force: bool = False,
) -> Pick | None:
    """
    Join one prediction to the market. Returns None when there is no edge worth
    acting on — the common case, by design.

    `calibration_scale` defaults to 0.0: an uncalibrated model gets a zero
    Kelly stake and a display-only confidence, because Kelly on a model whose
    probabilities are not yet trustworthy overbets badly.
    """
    edge = compute_edge(prediction, quote)
    if edge is None:
        return None

    tier = confidence_tier(edge, prediction.target, thresholds)
    if tier is None and not force:
        return None

    units = None
    scale_reason = None
    if model_probability is not None:
        units = staked_units(
            model_probability, quote.price,
            fraction=settings.kelly_fraction,
            max_units=settings.kelly_max_units,
            calibration_scale=calibration_scale,
        )
        if calibration_scale <= 0:
            scale_reason = (
                "Kelly scaled to zero: model has no calibration history yet. "
                "Unit size is display-only until it does."
            )
        elif calibration_scale < 1.0:
            scale_reason = (
                f"Kelly scaled to {calibration_scale:.2f} of "
                f"{settings.kelly_fraction:g} by current calibration quality."
            )

    pick = Pick(
        sport=prediction.sport, prediction_id=prediction.id,
        game_id=prediction.game_id, market=quote.market,
        selection=quote.selection[:160], side=quote.side,
        line_at_pick=quote.line, price_at_pick=quote.price, book=quote.book,
        pick_snapshot_id=quote.snapshot_id, edge=round(edge, 3),
        model_probability=model_probability, fair_probability=fair_probability(quote),
        confidence_tier="provisional" if is_provisional else tier,
        is_provisional=is_provisional, kelly_units=units,
        kelly_scale_reason=scale_reason, is_shadow=prediction.is_shadow,
    )
    session.add(pick)
    session.flush()
    return pick


def quote_from_snapshots(
    snapshots: list[LineSnapshot], market: str, selection: str, side: str | None = None
) -> MarketQuote | None:
    """Assemble a two-sided quote from stored snapshots so edge can be de-vigged."""
    matches = [
        s for s in snapshots
        if s.market == market and s.selection == selection and s.side == side
    ]
    if not matches:
        return None
    primary = max(matches, key=lambda s: s.captured_at)

    opposite_side = {"over": "under", "under": "over"}.get(side or "")
    if opposite_side:
        opposite = [
            s for s in snapshots
            if s.market == market and s.selection == selection
            and s.side == opposite_side
        ]
    else:
        # Game lines: the other side is the other team in the same market.
        opposite = [
            s for s in snapshots if s.market == market and s.selection != selection
        ]
    opposite_price = (
        max(opposite, key=lambda s: s.captured_at).price if opposite else None
    )

    return MarketQuote(
        market=primary.market, selection=primary.selection, side=primary.side,
        line=float(primary.line) if primary.line is not None else None,
        price=primary.price, book=primary.book,
        opposite_price=opposite_price, snapshot_id=primary.id,
    )
