"""
Line snapshotting — the backbone of CLV.

We record our own line history rather than buying it back later: The Odds API's
historical endpoint bills at 10x, and the only lines we actually need are the
ones on markets we took a position in.

A snapshot is (market, selection, side) -> (line, price) at a timestamp, tagged
with why it was taken. `closing` is the one that matters most: it is the
benchmark every pick is ultimately judged against.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import Game, LineSnapshot

logger = logging.getLogger(__name__)

OPENING = "opening"
PICK = "pick"
PERIODIC = "periodic"
CLOSING = "closing"


def _minutes_to_kickoff(kickoff, at: datetime) -> int | None:
    if not kickoff:
        return None
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    return int((kickoff - at).total_seconds() // 60)


def parse_event_payload(event: dict, book_filter: str | None = None) -> list[dict]:
    """
    Flatten one Odds API event into snapshot-shaped dicts.

    Handles both the slate shape (h2h/spreads/totals) and the per-event prop
    shape, which distinguishes the player via `description` rather than `name`.
    """
    rows: list[dict] = []
    for book in event.get("bookmakers", []) or []:
        book_key = book.get("key", "")
        if book_filter and book_key != book_filter:
            continue
        for market in book.get("markets", []) or []:
            market_key = market.get("key", "")
            for outcome in market.get("outcomes", []) or []:
                name = outcome.get("name", "") or ""
                description = outcome.get("description")
                # Player props carry the player in `description` and the
                # over/under in `name`; game lines carry the team in `name`.
                selection = description if description else name
                side = name.lower() if name.lower() in ("over", "under") else None
                rows.append({
                    "market": market_key,
                    "selection": selection,
                    "side": side,
                    "line": outcome.get("point"),
                    "price": outcome.get("price"),
                    "book": book_key,
                })
    return rows


def record_snapshots(
    session: Session, *, sport: str, odds_event_id: str, rows: list[dict],
    snapshot_type: str, game: Game | None = None,
    api_call_id: int | None = None, captured_at: datetime | None = None,
) -> list[LineSnapshot]:
    """Persist a batch of snapshot rows for one event."""
    captured_at = captured_at or datetime.now(timezone.utc)
    mins = _minutes_to_kickoff(game.kickoff_utc if game else None, captured_at)

    snapshots = []
    for row in rows:
        snap = LineSnapshot(
            sport=sport, game_id=game.id if game else None,
            odds_event_id=odds_event_id, market=row["market"],
            selection=row["selection"][:160], side=row.get("side"),
            line=row.get("line"), price=row.get("price"), book=row["book"],
            snapshot_type=snapshot_type, captured_at=captured_at,
            minutes_to_kickoff=mins, api_call_id=api_call_id,
        )
        session.add(snap)
        snapshots.append(snap)
    session.flush()
    return snapshots


def latest_snapshot(
    session: Session, *, odds_event_id: str, market: str, selection: str,
    side: str | None = None, snapshot_type: str | None = None,
    before: datetime | None = None,
) -> LineSnapshot | None:
    """Most recent snapshot matching a market coordinate."""
    stmt = select(LineSnapshot).where(
        LineSnapshot.odds_event_id == odds_event_id,
        LineSnapshot.market == market,
        LineSnapshot.selection == selection,
    )
    stmt = stmt.where(LineSnapshot.side.is_(None)) if side is None \
        else stmt.where(LineSnapshot.side == side)
    if snapshot_type:
        stmt = stmt.where(LineSnapshot.snapshot_type == snapshot_type)
    if before:
        stmt = stmt.where(LineSnapshot.captured_at <= before)
    return session.execute(
        stmt.order_by(LineSnapshot.captured_at.desc()).limit(1)
    ).scalar_one_or_none()


def promote_closing_snapshots(
    session: Session, *, sport: str, game: Game, as_of: datetime | None = None
) -> int:
    """
    Mark the last pre-kickoff snapshot of each market coordinate as `closing`.

    Run by the per-kickoff-window job. Idempotent: re-running promotes the same
    rows and changes nothing. If the scheduler misses a window entirely, the
    latest periodic snapshot is what gets promoted — a slightly stale close is
    far more useful than no close at all, and `minutes_to_kickoff` records
    exactly how stale so the CLV can be discounted honestly.
    """
    as_of = as_of or datetime.now(timezone.utc)
    kickoff = game.kickoff_utc or as_of

    candidates = session.execute(
        select(LineSnapshot)
        .where(
            LineSnapshot.game_id == game.id,
            LineSnapshot.captured_at <= kickoff,
            LineSnapshot.snapshot_type != CLOSING,
        )
        .order_by(LineSnapshot.captured_at.desc())
    ).scalars().all()

    seen: set[tuple] = set()
    promoted = 0
    for snap in candidates:
        key = (snap.market, snap.selection, snap.side, snap.book)
        if key in seen:
            continue
        seen.add(key)
        # Snapshots are ordinary mutable rows (only predictions/picks are
        # append-only), so re-typing the latest one is safe and cheap.
        snap.snapshot_type = CLOSING
        promoted += 1

    session.flush()
    logger.info("Promoted %d closing snapshots for game %s", promoted, game.id)
    return promoted
