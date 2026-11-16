"""
Credit accounting for The Odds API.

The API bills in "credits", not requests. The cost of a call is

    credits = (number of markets) x (number of regions)

with the `bookmakers` parameter counting as a single region, and the bare
`/events` endpoint costing nothing. That makes the per-event odds endpoint the
expensive path: five prop markets across a 14-game slate is 70 credits per
sweep, and polling it hourly would burn the monthly tier in days.

The ledger lives in Postgres rather than process memory because Gunicorn runs
multiple workers; an in-process counter undercounts real spend by the worker
count and is exactly the kind of accounting error that only surfaces when the
quota runs out mid-Sunday.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import settings
from core.models import CreditBudget, OddsApiCall

logger = logging.getLogger(__name__)

# The `/events` endpoint returns fixtures without odds and is not billed.
FREE_ENDPOINT_MARKERS = ("/events",)


class BudgetExceeded(RuntimeError):
    """Raised before a call that would push monthly spend past the ceiling."""


@dataclass(frozen=True)
class BudgetStatus:
    month: str
    used: int
    ceiling: int
    remaining: int
    fraction: float
    alert_threshold: float
    over_alert_threshold: bool
    at_ceiling: bool

    def as_dict(self) -> dict:
        return {
            "month": self.month,
            "used": self.used,
            "ceiling": self.ceiling,
            "remaining": self.remaining,
            "fraction_used": round(self.fraction, 4),
            "alert_threshold": self.alert_threshold,
            "over_alert_threshold": self.over_alert_threshold,
            "at_ceiling": self.at_ceiling,
        }


def current_month(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def estimate_credits(
    endpoint: str, markets: str | None, regions: str | None, bookmakers: str | None
) -> int:
    """
    Cost of a call, computed before making it so the ceiling can be enforced
    up front rather than discovered from a response header afterwards.
    """
    if any(m in endpoint for m in FREE_ENDPOINT_MARKERS) and "odds" not in endpoint:
        return 0
    n_markets = len([m for m in (markets or "").split(",") if m.strip()]) or 1
    if bookmakers:
        n_regions = 1
    else:
        n_regions = len([r for r in (regions or "").split(",") if r.strip()]) or 1
    return n_markets * n_regions


def get_budget(session: Session, month: str | None = None) -> BudgetStatus:
    month = month or current_month()
    row = session.execute(
        select(CreditBudget).where(CreditBudget.budget_month == month)
    ).scalar_one_or_none()

    ceiling = row.ceiling if row else settings.odds_monthly_credit_ceiling
    used = session.execute(
        select(func.coalesce(func.sum(OddsApiCall.credits_charged), 0)).where(
            OddsApiCall.budget_month == month
        )
    ).scalar_one()

    fraction = (used / ceiling) if ceiling else 1.0
    return BudgetStatus(
        month=month,
        used=int(used),
        ceiling=int(ceiling),
        remaining=max(0, int(ceiling) - int(used)),
        fraction=fraction,
        alert_threshold=settings.odds_credit_alert_fraction,
        over_alert_threshold=fraction >= settings.odds_credit_alert_fraction,
        at_ceiling=used >= ceiling,
    )


def ensure_budget_row(session: Session, month: str | None = None) -> CreditBudget:
    month = month or current_month()
    row = session.execute(
        select(CreditBudget).where(CreditBudget.budget_month == month)
    ).scalar_one_or_none()
    if row is None:
        row = CreditBudget(
            budget_month=month, ceiling=settings.odds_monthly_credit_ceiling
        )
        session.add(row)
        session.flush()
    return row


def check_affordable(session: Session, cost: int, month: str | None = None) -> BudgetStatus:
    """
    Gate a call against the monthly ceiling.

    Raises BudgetExceeded rather than returning a flag: a caller that forgets
    to check a boolean spends real money, while one that forgets to catch an
    exception fails loudly and gets fixed.
    """
    status = get_budget(session, month)
    if cost <= 0:
        return status
    if status.used + cost > status.ceiling:
        raise BudgetExceeded(
            f"Odds API budget for {status.month} would be exceeded: "
            f"{status.used} used + {cost} for this call > {status.ceiling} ceiling. "
            f"Raise ODDS_MONTHLY_CREDIT_CEILING or wait for the monthly reset."
        )
    return status


def record_call(
    session: Session,
    *,
    endpoint: str,
    sport: str | None,
    purpose: str | None,
    credits_charged: int,
    markets: str | None = None,
    regions: str | None = None,
    bookmakers: str | None = None,
    event_id: str | None = None,
    status_code: int | None = None,
    requests_used: int | None = None,
    requests_remaining: int | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    month: str | None = None,
) -> OddsApiCall:
    """Append one row to the credit ledger. Called for failures too."""
    call = OddsApiCall(
        sport=sport, endpoint=endpoint, purpose=purpose, event_id=event_id,
        markets=markets, regions=regions, bookmakers=bookmakers,
        credits_charged=max(0, int(credits_charged)),
        requests_used=requests_used, requests_remaining=requests_remaining,
        status_code=status_code, error=error, duration_ms=duration_ms,
        budget_month=month or current_month(),
    )
    session.add(call)
    session.flush()
    return call


def maybe_alert(session: Session, month: str | None = None) -> BudgetStatus | None:
    """
    Fire the threshold alert once per month, then record that it fired.

    Returns the status if an alert was newly raised, otherwise None, so the
    caller decides how to deliver it without this module knowing about email,
    Slack, or anything else.
    """
    status = get_budget(session, month)
    if not status.over_alert_threshold:
        return None

    row = ensure_budget_row(session, status.month)
    now = datetime.now(timezone.utc)

    newly = False
    if row.alert_threshold_sent_at is None:
        row.alert_threshold_sent_at = now
        newly = True
        logger.warning(
            "Odds API credit usage at %.1f%% of the %s ceiling (%d/%d).",
            status.fraction * 100, status.month, status.used, status.ceiling,
        )
    if status.at_ceiling and row.ceiling_hit_at is None:
        row.ceiling_hit_at = now
        newly = True
        logger.error(
            "Odds API monthly ceiling reached for %s (%d/%d). Calls are now blocked.",
            status.month, status.used, status.ceiling,
        )

    session.flush()
    return status if newly else None
