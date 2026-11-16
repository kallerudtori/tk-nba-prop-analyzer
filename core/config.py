"""
Shared configuration.

Everything is read from the environment so Railway stays the single source of
truth for secrets. Nothing here holds a default that would silently work in
production with the wrong value.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


# ── Sports ───────────────────────────────────────────────────────────────────
NBA = "nba"
NFL = "nfl"
SPORTS = (NBA, NFL)

# The Odds API sport keys, per sport.
ODDS_API_SPORT_KEY = {
    NBA: "basketball_nba",
    NFL: "americanfootball_nfl",
}

# Books we will accept a line from, per sport.
#
# NFL is DraftKings-only and deliberately so: closing line value is only
# meaningful when the book identity is constant between the pick snapshot and
# the closing snapshot. NBA keeps its historical multi-book fallback so the
# existing in-season behaviour does not regress.
BOOKMAKERS = {
    NBA: ("draftkings", "fanduel", "betmgm", "betrivers", "caesars"),
    NFL: ("draftkings",),
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str
    odds_api_key: str
    anthropic_api_key: str

    # Credit budget — The Odds API bills in "credits"; the paid tier here is
    # 100K/month. The ceiling is enforced by the client, not by hope.
    odds_monthly_credit_ceiling: int
    odds_credit_alert_fraction: float

    # Staking
    kelly_fraction: float
    kelly_max_units: float

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)


def _normalise_db_url(raw: str) -> str:
    """
    Railway hands out `postgres://...`; SQLAlchemy 2 wants `postgresql+psycopg2://`.
    Normalising here means the rest of the codebase never thinks about it.
    """
    if not raw:
        return ""
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://"):]
    if raw.startswith("postgresql://"):
        raw = "postgresql+psycopg2://" + raw[len("postgresql://"):]
    return raw


def load_settings() -> Settings:
    return Settings(
        database_url=_normalise_db_url(os.getenv("DATABASE_URL", "")),
        odds_api_key=os.getenv("ODDS_API_KEY", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        odds_monthly_credit_ceiling=_env_int("ODDS_MONTHLY_CREDIT_CEILING", 100_000),
        odds_credit_alert_fraction=_env_float("ODDS_CREDIT_ALERT_FRACTION", 0.70),
        kelly_fraction=_env_float("KELLY_FRACTION", 0.25),
        kelly_max_units=_env_float("KELLY_MAX_UNITS", 2.0),
    )


settings = load_settings()
