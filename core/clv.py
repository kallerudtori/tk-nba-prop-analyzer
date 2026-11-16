"""
Closing line value.

CLV is the primary learning signal, and the reason is sample size. The NFL
regular season is 272 games. At that scale a genuine 55% edge and a mediocre
52% one produce win/loss records that overlap for most of a season — a system
that retrains on last week's results is fitting noise with confidence.

CLV resolves in weeks instead of seasons: it asks whether the number we took
beat where the market ended up, which is a much lower-variance question than
whether the bet won.

Two flavours are computed:
  clv_points — line movement in our favour, in points. Readable, and the right
               unit for spreads and totals.
  clv_prob   — de-vigged probability at close minus at pick. Comparable across
               market types, and the correct unit for moneylines and props
               where the price moves but the line does not.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import Grade, LineSnapshot, Pick
from core.odds_math import american_to_prob, devig_two_way
from core.snapshots import CLOSING

logger = logging.getLogger(__name__)


def points_clv(pick: Pick, closing_line: float | None) -> float | None:
    """
    Points of line movement in our favour.

    Direction matters and is easy to get backwards. Worked examples:

      We take Chiefs -3.0, it closes -3.5.  We got a better number: +0.5.
      We take Chiefs +3.0, it closes +2.5.  We got a better number: +0.5.
      We take Over 44.0,   it closes 45.0.  We got a better number: +1.0.
      We take Under 44.0,  it closes 43.0.  We got a better number: +1.0.
    """
    if closing_line is None or pick.line_at_pick is None:
        return None
    taken = float(pick.line_at_pick)
    close = float(closing_line)

    if pick.side == "over":
        return round(close - taken, 3)

    # Everything else — spreads and unders — is `taken - close`.
    #
    # For a spread this holds regardless of sign, because `line_at_pick` is
    # always the number attached to the side we actually took: laying -3.0 into
    # a -3.5 close and taking +3.0 into a +2.5 close are both +0.5 of value.
    return round(taken - close, 3)


def probability_clv(
    pick: Pick, closing_price: int | None, closing_opposite_price: int | None = None
) -> float | None:
    """
    Movement in de-vigged probability terms.

    Positive means the market came to agree with us: the thing we backed is
    priced as more likely at close than when we took it.
    """
    if closing_price is None or pick.price_at_pick is None:
        return None

    if closing_opposite_price is not None:
        close_fair, _ = devig_two_way(closing_price, closing_opposite_price)
    else:
        close_fair = american_to_prob(closing_price)

    pick_fair = (
        float(pick.fair_probability)
        if pick.fair_probability is not None
        else american_to_prob(pick.price_at_pick)
    )
    return round(close_fair - pick_fair, 5)


def find_closing_snapshot(session: Session, pick: Pick) -> LineSnapshot | None:
    """The closing snapshot matching this pick's exact market coordinate."""
    stmt = (
        select(LineSnapshot)
        .where(
            LineSnapshot.game_id == pick.game_id,
            LineSnapshot.market == pick.market,
            LineSnapshot.selection == pick.selection,
            LineSnapshot.book == pick.book,
            LineSnapshot.snapshot_type == CLOSING,
        )
        .order_by(LineSnapshot.captured_at.desc())
        .limit(1)
    )
    stmt = stmt.where(LineSnapshot.side.is_(None)) if pick.side is None \
        else stmt.where(LineSnapshot.side == pick.side)
    return session.execute(stmt).scalar_one_or_none()


def grade_pick(
    session: Session, pick: Pick, *, outcome: str | None = None,
    actual_value: float | None = None,
) -> Grade:
    """
    Write (or refresh) the grade for one pick, including its CLV.

    Grades are mutable by design — a pick is immutable, but its grade may
    legitimately be corrected when a stat is revised or a closing snapshot
    arrives late.
    """
    closing = find_closing_snapshot(session, pick)
    closing_line = float(closing.line) if closing and closing.line is not None else None
    closing_price = closing.price if closing else None

    clv_pts = points_clv(pick, closing_line)
    clv_p = probability_clv(pick, closing_price)

    grade = session.execute(
        select(Grade).where(Grade.pick_id == pick.id)
    ).scalar_one_or_none()
    if grade is None:
        grade = Grade(sport=pick.sport, pick_id=pick.id)
        session.add(grade)

    grade.outcome = outcome
    grade.actual_value = actual_value
    grade.closing_line = closing_line
    grade.closing_price = closing_price
    grade.closing_snapshot_id = closing.id if closing else None
    grade.clv_points = clv_pts
    grade.clv_prob = clv_p
    grade.beat_close = None if clv_pts is None else clv_pts > 0

    session.flush()
    return grade


def clv_summary(rows: list[Grade]) -> dict:
    """
    Aggregate CLV over a set of grades.

    `beat_close_rate` is the headline: over a meaningful sample, a model that
    beats the close more than half the time has found something real, whatever
    its win/loss record happens to say in any given month.
    """
    graded = [g for g in rows if g.clv_points is not None]
    if not graded:
        return {"n": 0, "mean_clv_points": None, "beat_close_rate": None,
                "mean_clv_prob": None}

    pts = [float(g.clv_points) for g in graded]
    beat = [1 for g in graded if g.beat_close]
    probs = [float(g.clv_prob) for g in graded if g.clv_prob is not None]

    return {
        "n": len(graded),
        "mean_clv_points": round(sum(pts) / len(pts), 4),
        "beat_close_rate": round(len(beat) / len(graded), 4),
        "mean_clv_prob": round(sum(probs) / len(probs), 5) if probs else None,
    }
