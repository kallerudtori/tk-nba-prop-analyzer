"""
Odds Service
Integrates with The Odds API to pull DraftKings NBA player props.
Cache TTL: 15 minutes for odds data.
"""

import os
import logging
import unicodedata
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
    "player_threes": "threes",
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
        today_et = datetime.now(eastern).date()
        target_local = today_et + timedelta(days=day_offset)

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
    def _normalize_name(s: str) -> str:
        """Lowercase, strip diacritics (e.g. Vučević → vucevic) and periods (Jr.)."""
        s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
        return s.replace(".", "").lower().strip()

    # Suffixes that should be skipped when doing last-name matching
    _NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

    @staticmethod
    def _names_match(desc: str, player: str) -> bool:
        desc_n   = OddsService._normalize_name(desc)
        player_n = OddsService._normalize_name(player)
        if desc_n == player_n:
            return True
        # Last-name match — skip trailing suffixes (Jr., II, etc.)
        d_parts = desc_n.split()
        p_parts = player_n.split()
        d_last = d_parts[-1] if d_parts else ""
        p_last = p_parts[-1] if p_parts else ""
        if d_last in OddsService._NAME_SUFFIXES and len(d_parts) >= 2:
            d_last = d_parts[-2]
        if p_last in OddsService._NAME_SUFFIXES and len(p_parts) >= 2:
            p_last = p_parts[-2]
        if d_last and p_last and d_last == p_last and len(d_last) > 2:
            return True
        return player_n in desc_n or desc_n in player_n

    @staticmethod
    def _to_prob(american_odds: int) -> float:
        """Convert American moneyline odds to implied probability."""
        if american_odds > 0:
            return round(100 / (american_odds + 100), 4)
        return round(abs(american_odds) / (abs(american_odds) + 100), 4)

    # ------------------------------------------------------------------ #
    #  Game Lines (moneyline / spread / total)                            #
    # ------------------------------------------------------------------ #

    def get_all_game_lines(self, day_offset: int = 0) -> dict:
        """
        Fetch DraftKings moneyline, spread, and total for all games in one call.
        Returns a dict keyed by event_id.
        """
        eastern = pytz.timezone("America/New_York")
        today_et = datetime.now(eastern).date()
        target_local = today_et + timedelta(days=day_offset)

        day_start_et = eastern.localize(datetime.combine(target_local, dt_time(0, 0)))
        day_end_et   = day_start_et + timedelta(days=1)
        commence_from = day_start_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        commence_to   = day_end_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        cache_key = f"game_lines_{target_local.isoformat()}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            resp = requests.get(
                f"{ODDS_API_BASE}/sports/basketball_nba/odds",
                params={
                    "apiKey": self.api_key,
                    "regions": "us",
                    "markets": "h2h,spreads,totals",
                    "oddsFormat": "american",
                    "bookmakers": "draftkings",
                    "commenceTimeFrom": commence_from,
                    "commenceTimeTo": commence_to,
                },
                timeout=10,
            )
            resp.raise_for_status()
            self._store_quota(resp.headers)

            result = {e["id"]: self._parse_game_lines(e) for e in resp.json()}
            self.cache.set(cache_key, result, timeout=900)
            return result
        except requests.RequestException as exc:
            logger.error("Odds API game lines error: %s", exc)
            return {}

    def _parse_game_lines(self, event: dict) -> dict:
        """Extract moneyline, spread, and total from a raw event with odds."""
        home_team = event.get("home_team", "")
        away_team = event.get("away_team", "")
        result = {
            "home_team": home_team,
            "away_team": away_team,
            "moneyline": None,
            "spread": None,
            "total": None,
        }
        dk = next((b for b in event.get("bookmakers", []) if b["key"] == "draftkings"), None)
        if not dk:
            return result

        for market in dk.get("markets", []):
            key      = market["key"]
            outcomes = market.get("outcomes", [])

            if key == "h2h":
                ml = {}
                for o in outcomes:
                    if o["name"] == home_team:
                        ml["home_price"] = o["price"]
                        ml["home_prob"]  = self._to_prob(o["price"])
                    elif o["name"] == away_team:
                        ml["away_price"] = o["price"]
                        ml["away_prob"]  = self._to_prob(o["price"])
                if ml:
                    result["moneyline"] = ml

            elif key == "spreads":
                sp = {}
                for o in outcomes:
                    if o["name"] == home_team:
                        sp["home_point"] = o.get("point", 0)
                        sp["home_price"] = o["price"]
                    elif o["name"] == away_team:
                        sp["away_point"] = o.get("point", 0)
                        sp["away_price"] = o["price"]
                if sp:
                    result["spread"] = sp

            elif key == "totals":
                over  = next((o for o in outcomes if o["name"] == "Over"),  None)
                under = next((o for o in outcomes if o["name"] == "Under"), None)
                if over:
                    result["total"] = {
                        "point":       over.get("point", 0),
                        "over_price":  over["price"],
                        "under_price": under["price"] if under else -110,
                    }
        return result

    # ------------------------------------------------------------------ #
    #  Alternate Spreads (lazy per-event, cached 1 h)                    #
    # ------------------------------------------------------------------ #

    def get_alternate_spreads(self, event_id: str) -> list:
        """
        Fetch DraftKings alternate spreads for a single event.
        Returns a list of dicts: [{team, spread, odds, implied_prob}, ...]
        sorted by absolute spread value (tightest first).
        Returns [] gracefully if the market is unavailable.
        """
        cache_key = f"alt_spreads_{event_id}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            resp = requests.get(
                f"{ODDS_API_BASE}/sports/basketball_nba/events/{event_id}/odds",
                params={
                    "apiKey":      self.api_key,
                    "regions":     "us",
                    "markets":     "alternate_spreads",
                    "oddsFormat":  "american",
                    "bookmakers":  "draftkings",
                },
                timeout=10,
            )
            resp.raise_for_status()
            self._store_quota(resp.headers)
            data = resp.json()
        except requests.RequestException as exc:
            logger.warning("Alt spreads fetch failed event=%s: %s", event_id, exc)
            return []

        result = []
        seen   = set()

        dk = next(
            (b for b in data.get("bookmakers", []) if b["key"] == "draftkings"),
            None,
        )
        if not dk:
            logger.warning("No DraftKings alternate_spreads for event %s", event_id)
            self.cache.set(cache_key, [], timeout=3600)
            return []

        for market in dk.get("markets", []):
            if market.get("key") != "alternate_spreads":
                continue
            for outcome in market.get("outcomes", []):
                team  = outcome.get("name", "")
                point = outcome.get("point")
                price = outcome.get("price")
                if team and point is not None and price is not None:
                    key = (team, point)
                    if key not in seen:
                        seen.add(key)
                        result.append({
                            "team":         team,
                            "spread":       float(point),
                            "odds":         int(price),
                            "implied_prob": self._to_prob(int(price)),
                        })

        # Sort tightest spread first (smallest absolute value)
        result.sort(key=lambda x: abs(x["spread"]))
        self.cache.set(cache_key, result, timeout=3600)
        logger.info("Fetched %d alt spread outcomes for event %s", len(result), event_id)
        return result

    # ------------------------------------------------------------------ #
    #  Cache management & quota                                            #
    # ------------------------------------------------------------------ #

    def clear_cache(self, event_id: str | None = None):
        if event_id:
            self.cache.delete(f"props_{event_id}")
        # Clear both today and tomorrow's event caches (use ET date)
        today_et = datetime.now(pytz.timezone("America/New_York")).date()
        self.cache.delete(f"nba_events_{today_et.isoformat()}")
        self.cache.delete(f"nba_events_{(today_et + timedelta(days=1)).isoformat()}")

    def get_quota(self) -> dict | None:
        return self._quota

    def _store_quota(self, headers):
        self._quota = {
            "requests_remaining": headers.get("x-requests-remaining"),
            "requests_used": headers.get("x-requests-used"),
        }
