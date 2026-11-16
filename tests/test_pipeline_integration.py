"""
End-to-end checks against a real Postgres.

Skipped when TEST_DATABASE_URL is unset so the unit suite stays runnable
anywhere; CI and local development both set it.

What these prove is the thing the build spec cares most about: the pipeline
order is enforced by the database, not by the discipline of whoever writes the
next job.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import InternalError, ProgrammingError
from sqlalchemy.orm import sessionmaker

from core import credits
from core.models import Base, Game, LineSnapshot, ModelVersion, Pick, Prediction
from core.picks import MarketQuote, build_pick, compute_edge
from core.predictions import attach_explanation, store_prediction
from core.snapshots import PICK, record_snapshots

TEST_DB = os.getenv("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")

IMMUTABLE_ERRORS = (InternalError, ProgrammingError)

_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION forbid_prediction_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'predictions is append-only: row % cannot be deleted.', OLD.id;
    END IF;
    IF OLD.explanation IS NULL AND NEW.explanation IS NOT NULL THEN
        IF ROW(NEW.id, NEW.sport, NEW.game_id, NEW.model_version_id, NEW.target,
               NEW.subject, NEW.stat, NEW.projection, NEW.win_probability,
               NEW.distribution, NEW.feature_snapshot, NEW.attribution,
               NEW.is_shadow, NEW.created_at)
           IS DISTINCT FROM
           ROW(OLD.id, OLD.sport, OLD.game_id, OLD.model_version_id, OLD.target,
               OLD.subject, OLD.stat, OLD.projection, OLD.win_probability,
               OLD.distribution, OLD.feature_snapshot, OLD.attribution,
               OLD.is_shadow, OLD.created_at)
        THEN
            RAISE EXCEPTION 'predictions is append-only: row % may only have its NULL explanation filled in.', OLD.id;
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'predictions is append-only: row % cannot be updated.', OLD.id;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_predictions_immutable ON predictions;
CREATE TRIGGER trg_predictions_immutable
BEFORE UPDATE OR DELETE ON predictions
FOR EACH ROW EXECUTE FUNCTION forbid_prediction_mutation();

CREATE OR REPLACE FUNCTION forbid_pick_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'picks is append-only: row % cannot be %.', OLD.id, lower(TG_OP);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_picks_immutable ON picks;
CREATE TRIGGER trg_picks_immutable
BEFORE UPDATE OR DELETE ON picks
FOR EACH ROW EXECUTE FUNCTION forbid_pick_mutation();
"""


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB, future=True)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    # Recreate the triggers the migration installs; metadata.create_all only
    # knows about tables, so without this the immutability tests would pass
    # vacuously and tell us nothing.
    with eng.begin() as conn:
        conn.execute(text(_TRIGGER_SQL))
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    s = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def fixtures(session):
    stamp = datetime.now().timestamp()
    game = Game(
        sport="nfl", home_team="Kansas City Chiefs", away_team="Buffalo Bills",
        source_game_id=f"2026_01_BUF_KC_{stamp}",
        kickoff_utc=datetime.now(timezone.utc) + timedelta(hours=6),
        season=2026, week=1, season_type="REG",
    )
    model = ModelVersion(sport="nfl", kind="game", version=f"t{stamp}")
    session.add_all([game, model])
    session.flush()
    return game, model


FEATURES = {
    "epa_off_pass": 0.14, "epa_def_rush": -0.06, "pass_block_win_rate": 0.62,
    "rest_days": 7, "wind_mph": 4, "is_dome": False,
}
ATTRIBUTION = {
    "baseline": -1.5,
    "factors": {"pass_rush_mismatch": 1.2, "rest_advantage": 0.4, "wind_4mph": 0.0},
}


class TestPipelineOrder:
    def test_prediction_is_written_before_any_market_data_exists(self, session, fixtures):
        game, model = fixtures
        pred = store_prediction(
            session, sport="nfl", game_id=game.id, model_version_id=model.id,
            target="spread", subject="Kansas City Chiefs", projection=-3.9,
            feature_snapshot=FEATURES, attribution=ATTRIBUTION,
        )
        assert pred.id is not None
        # There is no market column to have populated.
        assert not [
            c for c in Prediction.__table__.columns
            if c.name in ("line", "price", "edge")
        ]

    def test_edge_is_projection_minus_market(self, session, fixtures):
        game, model = fixtures
        pred = store_prediction(
            session, sport="nfl", game_id=game.id, model_version_id=model.id,
            target="spread", subject="Kansas City Chiefs", projection=-6.0,
            feature_snapshot=FEATURES, attribution=ATTRIBUTION,
        )
        quote = MarketQuote("spreads", "Kansas City Chiefs", None, -3.5, -110,
                            "draftkings", opposite_price=-110)
        # Model says KC by 6, market says KC by 3.5 -> 2.5 points of edge on KC.
        assert compute_edge(pred, quote) == pytest.approx(-2.5)

    def test_pick_requires_a_stored_prediction(self, session, fixtures):
        """A pick cannot exist without the projection it came from."""
        game, _ = fixtures
        session.add(Pick(
            sport="nfl", prediction_id=999_999_999, game_id=game.id,
            market="spreads", selection="KC", book="draftkings",
        ))
        with pytest.raises(Exception):
            session.flush()
        session.rollback()


class TestSelectivity:
    def test_no_edge_returns_no_pick(self, session, fixtures):
        """Returning nothing is a normal outcome, not a failure."""
        game, model = fixtures
        pred = store_prediction(
            session, sport="nfl", game_id=game.id, model_version_id=model.id,
            target="spread", subject="Kansas City Chiefs", projection=-3.6,
            feature_snapshot=FEATURES, attribution=ATTRIBUTION,
        )
        quote = MarketQuote("spreads", "Kansas City Chiefs", None, -3.5, -110,
                            "draftkings", opposite_price=-110)
        assert build_pick(session, prediction=pred, quote=quote) is None

    def test_real_edge_produces_a_provisional_pick_with_zero_stake(self, session, fixtures):
        game, model = fixtures
        pred = store_prediction(
            session, sport="nfl", game_id=game.id, model_version_id=model.id,
            target="spread", subject="Kansas City Chiefs", projection=-7.0,
            feature_snapshot=FEATURES, attribution=ATTRIBUTION,
        )
        quote = MarketQuote("spreads", "Kansas City Chiefs", None, -3.5, -110,
                            "draftkings", opposite_price=-110)
        pick = build_pick(session, prediction=pred, quote=quote,
                          model_probability=0.58, calibration_scale=0.0)
        assert pick is not None
        assert pick.edge == pytest.approx(-3.5)
        assert pick.is_provisional
        assert float(pick.kelly_units) == 0.0
        assert "calibration" in pick.kelly_scale_reason


class TestImmutability:
    def test_projection_cannot_be_updated(self, session, fixtures):
        game, model = fixtures
        pred = store_prediction(
            session, sport="nfl", game_id=game.id, model_version_id=model.id,
            target="spread", subject="KC", projection=-3.0,
            feature_snapshot=FEATURES, attribution=ATTRIBUTION,
        )
        session.commit()
        with pytest.raises(IMMUTABLE_ERRORS):
            session.execute(
                text("UPDATE predictions SET projection = -9 WHERE id = :i"),
                {"i": pred.id},
            )
        session.rollback()

    def test_feature_snapshot_cannot_be_rewritten(self, session, fixtures):
        game, model = fixtures
        pred = store_prediction(
            session, sport="nfl", game_id=game.id, model_version_id=model.id,
            target="spread", subject="KC", projection=-3.0,
            feature_snapshot=FEATURES, attribution=ATTRIBUTION,
        )
        session.commit()
        with pytest.raises(IMMUTABLE_ERRORS):
            session.execute(
                text("UPDATE predictions SET feature_snapshot = '{}' WHERE id = :i"),
                {"i": pred.id},
            )
        session.rollback()

    def test_explanation_may_be_written_once_only(self, session, fixtures):
        game, model = fixtures
        pred = store_prediction(
            session, sport="nfl", game_id=game.id, model_version_id=model.id,
            target="spread", subject="KC", projection=-3.0,
            feature_snapshot=FEATURES, attribution=ATTRIBUTION,
        )
        session.commit()

        attach_explanation(session, pred.id, {
            "read": "KC's interior pass rush against a backup right guard.",
            "counter_argument": "Buffalo's tempo can neutralise a rush-heavy front.",
            "falsifier": "If Buffalo's snap-to-throw drops below 2.4s, the read is wrong.",
        })
        session.commit()

        with pytest.raises(ValueError):
            attach_explanation(session, pred.id, {
                "read": "x", "counter_argument": "y", "falsifier": "z",
            })

    def test_explanation_requires_counter_argument_and_falsifier(self, session, fixtures):
        """
        An explanation without a counter-argument and a falsifier is a
        persuasive story, which is the exact failure mode this guards against.
        """
        game, model = fixtures
        pred = store_prediction(
            session, sport="nfl", game_id=game.id, model_version_id=model.id,
            target="spread", subject="KC", projection=-3.0,
            feature_snapshot=FEATURES, attribution=ATTRIBUTION,
        )
        session.commit()
        with pytest.raises(ValueError, match="counter_argument"):
            attach_explanation(session, pred.id, {"read": "only the flattering half"})


class TestCreditAccounting:
    def test_ledger_accumulates_across_calls(self, session):
        month = "2099-01"
        before = credits.get_budget(session, month).used
        credits.record_call(session, endpoint="/sports/x/odds", sport="nfl",
                            purpose="game_lines", credits_charged=3, month=month)
        credits.record_call(session, endpoint="/sports/x/events/1/odds", sport="nfl",
                            purpose="event_odds", credits_charged=5, month=month)
        assert credits.get_budget(session, month).used == before + 8

    def test_ceiling_blocks_a_call_before_it_is_made(self, session):
        month = "2099-02"
        credits.ensure_budget_row(session, month)
        session.execute(
            text("UPDATE credit_budget SET ceiling = 10 WHERE budget_month = :m"),
            {"m": month},
        )
        credits.record_call(session, endpoint="/x", sport="nfl", purpose="p",
                            credits_charged=8, month=month)
        credits.check_affordable(session, 2, month)          # fits exactly
        with pytest.raises(credits.BudgetExceeded):
            credits.check_affordable(session, 3, month)      # would overshoot

    def test_failed_calls_are_still_logged(self, session):
        month = "2099-03"
        credits.record_call(session, endpoint="/x", sport="nfl", purpose="p",
                            credits_charged=3, status_code=502,
                            error="bad gateway", month=month)
        assert credits.get_budget(session, month).used == 3


class TestSnapshots:
    def test_snapshot_round_trip(self, session, fixtures):
        game, _ = fixtures
        rows = record_snapshots(
            session, sport="nfl", odds_event_id="evt-1",
            rows=[{"market": "spreads", "selection": "Kansas City Chiefs",
                   "side": None, "line": -3.5, "price": -110, "book": "draftkings"}],
            snapshot_type=PICK, game=game,
        )
        assert len(rows) == 1
        assert rows[0].minutes_to_kickoff > 0
        assert float(session.get(LineSnapshot, rows[0].id).line) == -3.5
