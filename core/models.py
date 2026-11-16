"""
Shared schema. Every table carries a `sport` dimension.

Two rules are enforced structurally here rather than by convention:

1.  ANTI-ANCHORING.  `predictions` has no market columns at all — not a line,
    not a price, not an implied probability. There is nowhere to put one. The
    market is joined downstream in `picks`, which holds a foreign key to an
    already-written prediction. A pick therefore cannot exist before the
    projection it is derived from.

2.  IMMUTABILITY.  `predictions` and `picks` are append-only, enforced by a
    Postgres trigger (see the initial migration). Corrections are new rows.
    The one permitted mutation is writing `predictions.explanation` once while
    it is still NULL, so prose generation can run as a separate job without
    being able to touch a projection or a feature value.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index,
    Integer, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[int]:
    return mapped_column(BigInteger, primary_key=True, autoincrement=True)


def _created() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


# ── Reference data ───────────────────────────────────────────────────────────

class Team(Base):
    """
    Team reference, including the stadium geography the weather layer needs.

    `is_dome` covers fixed roofs and retractable roofs closed by default; games
    there skip the weather adjustment entirely rather than applying a zeroed
    one.
    """
    __tablename__ = "teams"

    id: Mapped[int] = _pk()
    sport: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(8), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    aliases: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")

    stadium_name: Mapped[str | None] = mapped_column(String(120))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    altitude_ft: Mapped[int | None] = mapped_column(Integer)
    is_dome: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_retractable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    timezone: Mapped[str | None] = mapped_column(String(48))

    __table_args__ = (
        UniqueConstraint("sport", "code", name="uq_teams_sport_code"),
        CheckConstraint("sport IN ('nba','nfl')", name="ck_teams_sport"),
    )


class Game(Base):
    """
    One scheduled game, keyed across every upstream source we join on.

    `odds_event_id` is The Odds API's id; `source_game_id` is the sport's own
    (nflverse `game_id`, NBA `game_id`). Both are nullable because a game is
    often known from one source before the other.
    """
    __tablename__ = "games"

    id: Mapped[int] = _pk()
    sport: Mapped[str] = mapped_column(String(8), nullable=False, index=True)

    season: Mapped[int | None] = mapped_column(Integer, index=True)
    week: Mapped[int | None] = mapped_column(Integer, index=True)
    season_type: Mapped[str | None] = mapped_column(String(16))  # REG | POST | PRE

    odds_event_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source_game_id: Mapped[str | None] = mapped_column(String(64), index=True)

    home_team: Mapped[str] = mapped_column(String(80), nullable=False)
    away_team: Mapped[str] = mapped_column(String(80), nullable=False)
    kickoff_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="scheduled"
    )
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("sport", "source_game_id", name="uq_games_sport_source"),
        CheckConstraint("sport IN ('nba','nfl')", name="ck_games_sport"),
        Index("ix_games_sport_season_week", "sport", "season", "week"),
    )


# ── Model registry ───────────────────────────────────────────────────────────

class ModelVersion(Base):
    """
    Every prediction points at exactly one row here, so a backtest can always
    reconstruct which model produced which number.

    `status` drives the champion/challenger setup: one champion per
    (sport, kind) predicts for real; challengers predict in shadow mode and are
    promoted only after clearing the champion on CLV and calibration.
    """
    __tablename__ = "model_versions"

    id: Mapped[int] = _pk()
    sport: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # game | prop
    version: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="challenger"
    )
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    feature_list: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = _created()
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("sport", "kind", "version", name="uq_model_version"),
        CheckConstraint("sport IN ('nba','nfl')", name="ck_model_sport"),
        CheckConstraint("kind IN ('game','prop')", name="ck_model_kind"),
        CheckConstraint(
            "status IN ('champion','challenger','retired')", name="ck_model_status"
        ),
    )


class ModelChangelog(Base):
    """Human-readable audit trail. Surfaced directly in the Performance tab."""
    __tablename__ = "model_changelog"

    id: Mapped[int] = _pk()
    sport: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    model_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("model_versions.id", ondelete="SET NULL")
    )
    event: Mapped[str] = mapped_column(String(24), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        CheckConstraint(
            "event IN ('created','refit','promoted','demoted','retired',"
            "'backtest','holdout_read','feature_change')",
            name="ck_changelog_event",
        ),
    )


# ── The projection layer (NO MARKET DATA — see module docstring) ─────────────

class Prediction(Base):
    """
    A model's own view of the world, written before any line is fetched.

    There is deliberately no line, price, implied probability, or edge column
    on this table. Adding one would break the anti-anchoring guarantee, and
    `tests/test_anti_anchoring.py` fails if one appears.
    """
    __tablename__ = "predictions"

    id: Mapped[int] = _pk()
    sport: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    game_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("games.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False
    )

    # What is being projected.
    #   target: spread | total | moneyline | prop
    #   subject: team name for game targets, player name for props
    #   stat: prop stat key (receptions, rush_yds, ...); NULL for game targets
    target: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(120), index=True)
    stat: Mapped[str | None] = mapped_column(String(48))

    # The projection itself.
    #   projection      — central estimate (margin, total points, stat mean)
    #   win_probability — for moneyline/spread targets
    #   distribution    — {"family": "negbinom", "params": {...}} so the prop
    #                     layer can model non-normal stats honestly
    projection: Mapped[float | None] = mapped_column(Numeric(10, 4))
    win_probability: Mapped[float | None] = mapped_column(Numeric(6, 5))
    distribution: Mapped[dict | None] = mapped_column(JSONB)

    # Full reconstruction material.
    feature_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    attribution: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Prose, generated from `attribution` + `feature_snapshot` only.
    # Written once while NULL; the immutability trigger blocks any later change.
    explanation: Mapped[dict | None] = mapped_column(JSONB)

    is_shadow: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_at: Mapped[datetime] = _created()

    game = relationship("Game")
    model_version = relationship("ModelVersion")

    __table_args__ = (
        CheckConstraint("sport IN ('nba','nfl')", name="ck_pred_sport"),
        CheckConstraint(
            "target IN ('spread','total','moneyline','prop')", name="ck_pred_target"
        ),
        CheckConstraint(
            "win_probability IS NULL OR (win_probability >= 0 AND win_probability <= 1)",
            name="ck_pred_winprob_range",
        ),
        Index("ix_pred_game_target", "game_id", "target"),
        Index("ix_pred_model_created", "model_version_id", "created_at"),
    )


# ── The market layer ─────────────────────────────────────────────────────────

class OddsApiCall(Base):
    """
    One row per outbound Odds API request, with its credit cost.

    This is the ledger the budget ceiling is enforced against. It lives in
    Postgres rather than memory because Gunicorn runs multiple workers, and an
    in-process counter undercounts real spend by exactly the worker count.
    """
    __tablename__ = "odds_api_calls"

    id: Mapped[int] = _pk()
    sport: Mapped[str | None] = mapped_column(String(8), index=True)
    endpoint: Mapped[str] = mapped_column(String(120), nullable=False)
    purpose: Mapped[str | None] = mapped_column(String(48), index=True)
    event_id: Mapped[str | None] = mapped_column(String(64))
    markets: Mapped[str | None] = mapped_column(Text)
    regions: Mapped[str | None] = mapped_column(String(32))
    bookmakers: Mapped[str | None] = mapped_column(String(120))

    credits_charged: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    requests_used: Mapped[int | None] = mapped_column(Integer)
    requests_remaining: Mapped[int | None] = mapped_column(Integer)

    status_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    # YYYY-MM of the billing period, so the ceiling query is a plain equality.
    budget_month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    called_at: Mapped[datetime] = _created()

    __table_args__ = (Index("ix_odds_calls_month_sport", "budget_month", "sport"),)


class CreditBudget(Base):
    """Per-month ceiling and alert state, so the 70% alert fires exactly once."""
    __tablename__ = "credit_budget"

    id: Mapped[int] = _pk()
    budget_month: Mapped[str] = mapped_column(String(7), nullable=False, unique=True)
    ceiling: Mapped[int] = mapped_column(Integer, nullable=False)
    alert_threshold_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    ceiling_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class LineSnapshot(Base):
    """
    A book's number at a point in time. The backbone of CLV.

    `snapshot_type`:
      opening  — first time we ever saw this market
      pick     — captured at the moment a pick was made
      periodic — routine polling between pick and kickoff
      closing  — last capture before kickoff; the CLV benchmark

    We capture our own rather than paying The Odds API's historical endpoint,
    which bills at 10x.
    """
    __tablename__ = "line_snapshots"

    id: Mapped[int] = _pk()
    sport: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    game_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("games.id", ondelete="CASCADE"), index=True
    )
    odds_event_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    market: Mapped[str] = mapped_column(String(48), nullable=False)
    selection: Mapped[str] = mapped_column(String(160), nullable=False)
    side: Mapped[str | None] = mapped_column(String(8))       # over | under | None
    line: Mapped[float | None] = mapped_column(Numeric(8, 2))
    price: Mapped[int | None] = mapped_column(Integer)        # American odds
    book: Mapped[str] = mapped_column(String(24), nullable=False)

    snapshot_type: Mapped[str] = mapped_column(String(12), nullable=False)
    captured_at: Mapped[datetime] = _created()
    minutes_to_kickoff: Mapped[int | None] = mapped_column(Integer)
    api_call_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("odds_api_calls.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint("sport IN ('nba','nfl')", name="ck_snap_sport"),
        CheckConstraint(
            "snapshot_type IN ('opening','pick','periodic','closing')",
            name="ck_snap_type",
        ),
        Index("ix_snap_lookup", "odds_event_id", "market", "selection", "side",
              "captured_at"),
        Index("ix_snap_closing", "sport", "snapshot_type", "captured_at"),
    )


class Pick(Base):
    """
    A prediction joined to the market — the only place edge exists.

    Immutable. `prediction_id` is NOT NULL, so the schema makes it impossible
    to record a pick that was not preceded by a stored projection.
    """
    __tablename__ = "picks"

    id: Mapped[int] = _pk()
    sport: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    prediction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("predictions.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    game_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("games.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # The bet.
    market: Mapped[str] = mapped_column(String(48), nullable=False)
    selection: Mapped[str] = mapped_column(String(160), nullable=False)
    side: Mapped[str | None] = mapped_column(String(8))

    # The market at pick time — captured downstream of the projection.
    line_at_pick: Mapped[float | None] = mapped_column(Numeric(8, 2))
    price_at_pick: Mapped[int | None] = mapped_column(Integer)
    book: Mapped[str] = mapped_column(String(24), nullable=False)
    pick_snapshot_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("line_snapshots.id", ondelete="SET NULL")
    )

    # Edge, computed as projection − market. Never the other way round.
    edge: Mapped[float | None] = mapped_column(Numeric(8, 3))
    model_probability: Mapped[float | None] = mapped_column(Numeric(6, 5))
    fair_probability: Mapped[float | None] = mapped_column(Numeric(6, 5))  # de-vigged

    confidence_tier: Mapped[str | None] = mapped_column(String(16))
    is_provisional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    kelly_units: Mapped[float | None] = mapped_column(Numeric(6, 3))
    kelly_scale_reason: Mapped[str | None] = mapped_column(Text)

    is_shadow: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_at: Mapped[datetime] = _created()

    prediction = relationship("Prediction")
    game = relationship("Game")

    __table_args__ = (
        CheckConstraint("sport IN ('nba','nfl')", name="ck_pick_sport"),
        CheckConstraint(
            "confidence_tier IS NULL OR confidence_tier IN "
            "('low','medium','high','provisional')",
            name="ck_pick_tier",
        ),
        Index("ix_picks_sport_created", "sport", "created_at"),
    )


class Grade(Base):
    """
    Result of a pick, plus its closing line value.

    CLV is the primary learning signal: at NFL sample sizes, win/loss cannot
    distinguish a 55% edge from a 52% one inside a season, but CLV resolves
    within weeks.
    """
    __tablename__ = "grades"

    id: Mapped[int] = _pk()
    sport: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    pick_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("picks.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )

    outcome: Mapped[str | None] = mapped_column(String(8))   # win | loss | push | void
    actual_value: Mapped[float | None] = mapped_column(Numeric(10, 3))

    closing_line: Mapped[float | None] = mapped_column(Numeric(8, 2))
    closing_price: Mapped[int | None] = mapped_column(Integer)
    closing_snapshot_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("line_snapshots.id", ondelete="SET NULL")
    )

    # Points of line moved in our favour (positive = we beat the close).
    clv_points: Mapped[float | None] = mapped_column(Numeric(8, 3))
    # Probability-space CLV: fair prob at close − fair prob at pick.
    clv_prob: Mapped[float | None] = mapped_column(Numeric(7, 5))
    beat_close: Mapped[bool | None] = mapped_column(Boolean)

    graded_at: Mapped[datetime] = _created()

    pick = relationship("Pick")

    __table_args__ = (
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('win','loss','push','void')",
            name="ck_grade_outcome",
        ),
    )


class CalibrationSnapshot(Base):
    """
    Point-in-time calibration of a model version, recomputed by the weekly job.

    `reliability` holds the binned reliability curve the Performance tab draws;
    `is_provisional` stays true until the model has enough graded predictions
    to show a confidence number that means anything.
    """
    __tablename__ = "calibration_snapshots"

    id: Mapped[int] = _pk()
    sport: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    model_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False
    )
    as_of: Mapped[datetime] = _created()
    window_label: Mapped[str] = mapped_column(String(32), nullable=False)

    n: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    brier: Mapped[float | None] = mapped_column(Numeric(8, 5))
    log_loss: Mapped[float | None] = mapped_column(Numeric(8, 5))
    calibration_error: Mapped[float | None] = mapped_column(Numeric(8, 5))
    mean_clv_points: Mapped[float | None] = mapped_column(Numeric(8, 4))
    beat_close_rate: Mapped[float | None] = mapped_column(Numeric(6, 5))

    reliability: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    by_bucket: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    is_provisional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )

    __table_args__ = (Index("ix_calib_model_asof", "model_version_id", "as_of"),)
