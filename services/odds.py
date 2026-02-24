"""
Odds Service
Integrates with The Odds API to pull DraftKings NBA player props.
Cache TTL: 15 minutes for odds data.
"""

import os
import logging
from datetime import date, datetime, timedelta, time as dt_time, timezone

import pytz
import requests

logger = logging.getLogger(__name__)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Maps Odds API market key → our internal prop key
MARKET_MAP = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
    "player_points_rebounds_assists": "pra",
}
MARKETS_PARAM = ",".join(MARKET_MAP.keys())


class OddsService:
    def __init__(self, cache):
        self.cache = cache
        self.api_key = os.getenv("ODDS_API_KEY", "")
        self._quota: dict | None = None

    # ------------------------------------------------------------------ #
    #  Events                                                              #
    # ------------------------------------------------------------------ #

    def get_nba_events(self, day_offset: int = 0) -> list:
        """Return NBA events from The Odds API for the given day (in US/Eastern time)."""
        eastern = pytz.timezone("America/New_York")
        target_local = date.today() + timedelta(days=day_offset)

        # Build UTC window covering the full Eastern-timezone calendar day
        day_start_et = eastern.localize(datetime.combine(target_local, dt_time(0, 0)))
        day_end_et   = day_start_et + timedelta(days=1)
        commence_from = day_start_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        commence_to   = day_end_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        cache_key = f"nba_events_{target_local.isoformat()}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            resp = requests.get(
                f"{ODDS_API_BASE}/sports/basketball_nba/events",
                params={
                    "apiKey": self.api_key,
                    "dateFormat": "iso",
                    "commenceTimeFrom": commence_from,
                    "commenceTimeTo": commence_to,
                },
                timeout=10,
            )
            resp.raise_for_status()
            self._store_quota(resp.headers)

            self.cache.set(cache_key, resp.json(), timeout=900)
            return resp.json()
        except requests.RequestException as exc:
            logger.error("Odds API events error: %s", exc)
            return []

    def match_game_to_event(
        self, home_team_name: str, away_team_name: str, events: list
    ) -> str | None:
        """
        Try to match an NBA API game to an Odds API event by team names.
        Returns the event ID string or None.
        """
        for event in events:
            ev_home = event.get("home_team", "").lower()
            ev_away = event.get("away_team", "").lower()
            if self._teams_match(home_team_name.lower(), ev_home) and \
               self._teams_match(away_team_name.lower(), ev_away):
                return event["id"]
        return None

    @staticmethod
    def _teams_match(nba_name: str, odds_name: str) -> bool:
        nba_parts = nba_name.split()
        odds_parts = odds_name.split()
        # Last word is usually the franchise name (e.g. "Lakers")
        if nba_parts and odds_parts and nba_parts[-1] == odds_parts[-1]:
            return True
        # Fallback: check for any meaningful word overlap
        for word in nba_parts:
            if len(word) > 3 and word in odds_parts:
                return True
        return False

    # ------------------------------------------------------------------ #
    #  Player Props                                                        #
    # ------------------------------------------------------------------ #

    def get_player_props(
        self, event_id: str, player_name: str | None = None
    ) -> dict | None:
        """
        Fetch DraftKings player prop lines for an event.
        If player_name is given, filter to that player's lines only.
        Returns a dict keyed by prop ('points', 'rebounds', 'assists', 'pra')
        or None if nothing found.
        """
        cache_key = f"props_{event_id}"
        raw = self.cache.get(cache_key)

        if raw is None:
            try:
                resp = requests.get(
                    f"{ODDS_API_BASE}/sports/basketball_nba/events/{event_id}/odds",
                    params={
                        "apiKey": self.api_key,
                        "regions": "us",
                        "markets": MARKETS_PARAM,
                        "oddsFormat": "american",
                        "bookmakers": "draftkings",
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                self._store_quota(resp.headers)
                raw = resp.json()
                self.cache.set(cache_key, raw, timeout=900)
            except requests.RequestException as exc:
                logger.error("Odds API props error event=%s: %s", event_id, exc)
                return None

        if player_name:
            return self._extract_player(raw, player_name)
        return raw

    def _extract_player(self, event_data: dict, player_name: str) -> dict | None:
        """Pull out this player's over/under lines from raw event odds."""
        result = {}
        name_lower = player_name.lower().strip()

        for bookmaker in event_data.get("bookmakers", []):
            if bookmaker.get("key") != "draftkings":
                continue
            for market in bookmaker.get("markets", []):
                prop_key = MARKET_MAP.get(market.get("key", ""))
                if not prop_key:
                    continue

                over_out = under_out = None
                for outcome in market.get("outcomes", []):
                    desc = outcome.get("description", "").lower().strip()
                    if self._names_match(desc, name_lower):
                        if outcome["name"] == "Over":
                            over_out = outcome
                        elif outcome["name"] == "Under":
                            under_out = outcome

                if over_out:
                    line = over_out.get("point", 0.0)
                    over_odds = over_out.get("price", -110)
                    under_odds = under_out.get("price", -110) if under_out else -110
                    result[prop_key] = {
                        "line": float(line),
                        "over_odds": int(over_odds),
                        "under_odds": int(under_odds),
                        "implied_prob_over": self._to_prob(over_odds),
                        "implied_prob_under": self._to_prob(under_odds),
                    }

        return result if result else None

    @staticmethod
    def _names_match(desc: str, player: str) -> bool:
        if desc == player:
            return True
        # Last-name match
        d_parts = desc.split()
        p_parts = player.split()
        if d_parts and p_parts and d_parts[-1] == p_parts[-1]:
            return True
        return player in desc or desc in player

    @staticmethod
    def _to_prob(american_odds: int) -> float:
        """Convert American moneyline odds to implied probability."""
        if american_odds > 0:
            return round(100 / (american_odds + 100), 4)
        return round(abs(american_odds) / (abs(american_odds) + 100), 4)

    # ------------------------------------------------------------------ #
    #  Cache management & quota                                            #
    # ------------------------------------------------------------------ #

    def clear_cache(self, event_id: str | None = None):
        if event_id:
            self.cache.delete(f"props_{event_id}")
        # Clear both today and tomorrow's event caches
        self.cache.delete(f"nba_events_{date.today().isoformat()}")
        self.cache.delete(f"nba_events_{(date.today() + timedelta(days=1)).isoformat()}")

    def get_quota(self) -> dict | None:
        return self._quota

    def _store_quota(self, headers):
        self._quota = {
            "requests_remaining": headers.get("x-requests-remaining"),
            "requests_used": headers.get("x-requests-used"),
        }
