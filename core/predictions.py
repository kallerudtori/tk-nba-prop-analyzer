"""
Prediction + feature store.

Writing a prediction is step 2 of the pipeline: the model has produced its own
number from its own inputs, and that number is committed to the database
before anything fetches a line. `store_prediction` deliberately takes no
market argument — there is no parameter you could pass one to.

Every row carries the complete feature snapshot and the model version, so any
future backtest can reconstruct exactly what the model knew. Without that, a
backtest is a story about the past rather than a measurement of it.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.anchoring import assert_market_free
from core.models import ModelVersion, Prediction

logger = logging.getLogger(__name__)

REQUIRED_ATTRIBUTION_KEYS = ("baseline", "factors")


class AttributionError_(ValueError):
    """Raised when an attribution object is missing its required structure."""


def validate_attribution(attribution: dict) -> None:
    """
    An attribution object must exist, and must decompose the projection into a
    baseline plus signed per-factor contributions, before any prose is written.

    This is what stops the explanation layer from becoming post-hoc
    rationalisation: prose is generated from these numbers, so it cannot
    invent a reason the model did not actually use.
    """
    if not isinstance(attribution, dict):
        raise AttributionError_("attribution must be a dict")
    missing = [k for k in REQUIRED_ATTRIBUTION_KEYS if k not in attribution]
    if missing:
        raise AttributionError_(
            f"attribution missing required keys: {missing}. "
            "Expected {'baseline': float, 'factors': {name: signed_contribution}}."
        )
    factors = attribution["factors"]
    if not isinstance(factors, dict):
        raise AttributionError_("attribution['factors'] must be a dict of name -> float")
    for name, value in factors.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise AttributionError_(
                f"attribution factor {name!r} must be numeric, got {type(value).__name__}"
            )


def get_active_model(
    session: Session, sport: str, kind: str, *, status: str = "champion"
) -> ModelVersion | None:
    return session.execute(
        select(ModelVersion)
        .where(
            ModelVersion.sport == sport,
            ModelVersion.kind == kind,
            ModelVersion.status == status,
        )
        .order_by(ModelVersion.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def store_prediction(
    session: Session, *, sport: str, game_id: int, model_version_id: int,
    target: str, feature_snapshot: dict[str, Any], attribution: dict[str, Any],
    subject: str | None = None, stat: str | None = None,
    projection: float | None = None, win_probability: float | None = None,
    distribution: dict | None = None, is_shadow: bool = False,
) -> Prediction:
    """
    Persist one projection with its full input snapshot.

    Note the absence of any line/price/odds parameter. That is the whole point:
    the projection layer cannot receive market data because there is no channel
    for it. Edge is computed later, in core/picks.py, against a stored row.
    """
    assert_market_free(feature_snapshot, context="prediction feature_snapshot")
    assert_market_free(attribution, context="prediction attribution")
    validate_attribution(attribution)

    prediction = Prediction(
        sport=sport, game_id=game_id, model_version_id=model_version_id,
        target=target, subject=subject, stat=stat, projection=projection,
        win_probability=win_probability, distribution=distribution,
        feature_snapshot=feature_snapshot, attribution=attribution,
        is_shadow=is_shadow,
    )
    session.add(prediction)
    session.flush()
    return prediction


def attach_explanation(
    session: Session, prediction_id: int, explanation: dict
) -> Prediction:
    """
    Write the generated prose onto a prediction, once.

    The database permits this exact transition (NULL -> value) and nothing
    else; a second call, or any attempt to alter another column alongside it,
    is rejected by the immutability trigger.
    """
    required = ("read", "counter_argument", "falsifier")
    missing = [k for k in required if not explanation.get(k)]
    if missing:
        raise ValueError(
            f"explanation missing required sections: {missing}. "
            "Every explanation must state the read, the strongest "
            "counter-argument, and the falsifier."
        )

    prediction = session.get(Prediction, prediction_id)
    if prediction is None:
        raise LookupError(f"prediction {prediction_id} not found")
    if prediction.explanation is not None:
        raise ValueError(
            f"prediction {prediction_id} already has an explanation; "
            "corrections go in a new prediction row"
        )

    prediction.explanation = explanation
    session.flush()
    return prediction


def predictions_for_game(
    session: Session, game_id: int, *, target: str | None = None
) -> list[Prediction]:
    stmt = select(Prediction).where(Prediction.game_id == game_id)
    if target:
        stmt = stmt.where(Prediction.target == target)
    return list(session.execute(stmt.order_by(Prediction.created_at)).scalars())
