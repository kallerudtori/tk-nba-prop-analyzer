"""
The Odds API client — sport-agnostic, credit-accounted, budget-enforced.

Every call goes through `_request`, which prices the call, checks it against
the monthly ceiling, makes it, and writes a ledger row — including on failure,
so a burst of errors is visible rather than silently free.

Deliberately absent: any historical-odds endpoint. It bills at 10x, and we get
the same information for free by snapshotting our own lines going forward and,
for backtesting, by using nflverse's historical closing lines.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests
from sqlalchemy.orm import Session

from core import credits
from core.config import BOOKMAKERS, ODDS_API_SPORT_KEY, settings

logger = logging.getLogger(__name__)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
DEFAULT_TIMEOUT = 15


class OddsApiError(RuntimeError):
    pass


class OddsApiNotConfigured(OddsApiError):
    pass


@dataclass
class OddsResponse:
    data: Any
    credits_charged: int
    call_id: int | None
    budget: credits.BudgetStatus | None


class OddsClient:
    """
    Args:
        session: SQLAlchemy session used for the credit ledger. Required —
            there is no un-metered mode, because an un-metered mode is what
            every blown API budget has in common.
    """

    def __init__(self, session: Session, *, api_key: str | None = None):
        self.session = session
        self.api_key = api_key if api_key is not None else settings.odds_api_key

    # ── Public API ───────────────────────────────────────────────────────────

    def get_events(self, sport: str, *, commence_from=None, commence_to=None) -> OddsResponse:
        """Fixtures without odds. Free — use it to discover event ids cheaply."""
        params: dict[str, Any] = {"dateFormat": "iso"}
        if commence_from:
            params["commenceTimeFrom"] = _iso_z(commence_from)
        if commence_to:
            params["commenceTimeTo"] = _iso_z(commence_to)
        return self._request(
            f"/sports/{_sport_key(sport)}/events", params, sport=sport, purpose="events"
        )

    def get_game_lines(
        self, sport: str, *, markets: str = "h2h,spreads,totals",
        commence_from=None, commence_to=None,
    ) -> OddsResponse:
        """Whole-slate game lines. 3 credits at the default market set."""
        params: dict[str, Any] = {
            "markets": markets,
            "oddsFormat": "american",
            "bookmakers": _books(sport),
            "dateFormat": "iso",
        }
        if commence_from:
            params["commenceTimeFrom"] = _iso_z(commence_from)
        if commence_to:
            params["commenceTimeTo"] = _iso_z(commence_to)
        return self._request(
            f"/sports/{_sport_key(sport)}/odds", params, sport=sport, purpose="game_lines"
        )

    def get_event_odds(self, sport: str, event_id: str, markets: str) -> OddsResponse:
        """
        Per-event odds — the player-prop path, and the expensive one.

        Billed per market per event, so callers should batch the market list
        into a single call rather than looping markets, and schedule sweeps
        deliberately rather than polling.
        """
        params = {
            "markets": markets,
            "oddsFormat": "american",
            "bookmakers": _books(sport),
            "dateFormat": "iso",
        }
        return self._request(
            f"/sports/{_sport_key(sport)}/events/{event_id}/odds",
            params, sport=sport, purpose="event_odds", event_id=event_id,
        )

    def budget(self) -> credits.BudgetStatus:
        return credits.get_budget(self.session)

    # ── Internals ────────────────────────────────────────────────────────────

    def _request(
        self, path: str, params: dict, *, sport: str | None,
        purpose: str, event_id: str | None = None,
    ) -> OddsResponse:
        if not self.api_key:
            raise OddsApiNotConfigured(
                "ODDS_API_KEY is not set. Add it to the Railway service variables."
            )

        cost = credits.estimate_credits(
            path, params.get("markets"), params.get("regions"), params.get("bookmakers")
        )
        # Raises BudgetExceeded before any money is spent.
        credits.check_affordable(self.session, cost)

        url = f"{ODDS_API_BASE}{path}"
        call_params = {**params, "apiKey": self.api_key}

        started = time.monotonic()
        status_code = used = remaining = None
        error = None
        data = None

        try:
            resp = requests.get(url, params=call_params, timeout=DEFAULT_TIMEOUT)
            status_code = resp.status_code
            used = _int_header(resp.headers, "x-requests-used")
            remaining = _int_header(resp.headers, "x-requests-remaining")
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            error = str(exc)[:2000]
            logger.error("Odds API %s failed: %s", path, error)
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            # A failed call may still have been billed, so it is always logged.
            call = credits.record_call(
                self.session, endpoint=path, sport=sport, purpose=purpose,
                credits_charged=cost, markets=params.get("markets"),
                regions=params.get("regions"), bookmakers=params.get("bookmakers"),
                event_id=event_id, status_code=status_code, requests_used=used,
                requests_remaining=remaining, duration_ms=duration_ms, error=error,
            )
            budget = credits.maybe_alert(self.session)

        if error is not None:
            raise OddsApiError(f"{path}: {error}")

        return OddsResponse(
            data=data, credits_charged=cost, call_id=call.id,
            budget=budget or credits.get_budget(self.session),
        )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _sport_key(sport: str) -> str:
    try:
        return ODDS_API_SPORT_KEY[sport]
    except KeyError:
        raise ValueError(f"unknown sport {sport!r}") from None


def _books(sport: str) -> str:
    return ",".join(BOOKMAKERS[sport])


def _iso_z(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _int_header(headers, name: str) -> int | None:
    try:
        return int(float(headers.get(name)))
    except (TypeError, ValueError):
        return None
