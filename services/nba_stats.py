"""
NBA Stats Service
Fetches and caches data from the nba_api library.
Cache TTL: 1 hour for stats, 30 min for today's games/rosters.
"""

import math
import os
import time
import logging
import concurrent.futures
from datetime import date, datetime, timedelta

import re
import pandas as pd
import numpy as np
import pytz

_MOUNTAIN = pytz.timezone("America/Denver")


def _today_mt():
    """Return today's date in US/Mountain — safe on UTC-based cloud servers."""
    return datetime.now(_MOUNTAIN).date()


def _et_to_mt(status_text: str) -> str:
    """Convert NBA gameStatusText from ET to MT display (MT = ET - 2h)."""
    m = re.match(r"(\d+):(\d+)\s*(am|pm)\s*ET", status_text, re.IGNORECASE)
    if not m:
        return status_text  # "Final", "Q3 5:23", "Halftime", etc.
    h, mins, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ampm == "pm" and h != 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    h -= 2
    if h < 0:
        h += 24
    new_ampm = "pm" if h >= 12 else "am"
    if h > 12:
        h -= 12
    elif h == 0:
        h = 12
    return f"{h}:{mins:02d} {new_ampm} MT"


def _exp_decay_avg(vals, decay: float = 0.07) -> float:
    """
    Exponential-decay weighted average.
    Index 0 = most-recent game (weight 1.0); index k has weight e^(-decay*k).
    """
    if not len(vals):
        return 0.0
    weights = [math.exp(-decay * k) for k in range(len(vals))]
    return float(sum(w * v for w, v in zip(weights, vals)) / sum(weights))


logger = logging.getLogger(__name__)

def _derive_season(today: date | None = None) -> str:
    """
    The NBA season string ("2026-27") for a given date.

    A new season starts in October, so October through December belong to the
    season named for that calendar year, and January through September belong
    to the one that started the previous October. During the summer this
    correctly keeps returning the season that just finished, which is the data
    you want in the offseason.

    This used to be a hardcoded constant with a comment saying to update it
    every October. It was not updated, so the entire app silently served the
    previous season: stale rosters (traded players shown on their old teams),
    "games played this season" counts above 82, and "last 5 games" that were
    actually the previous spring's playoffs. Deriving it removes the annual
    manual step that failed.
    """
    today = today or date.today()
    start_year = today.year if today.month >= 10 else today.year - 1
    return f"{start_year}-{(start_year + 1) % 100:02d}"


# Override only to pin a historical season for backfills; normally unset.
CURRENT_SEASON = os.getenv("NBA_SEASON_OVERRIDE", "").strip() or _derive_season()

# Rate-limit pause between nba_api calls (seconds)
NBA_API_DELAY = 0.65


def _parse_minutes(val) -> float:
    """Parse a MIN value that may be float or 'MM:SS' string."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        if ":" in val:
            parts = val.split(":")
            return float(parts[0]) + float(parts[1]) / 60
        try:
            return float(val)
        except ValueError:
            return 0.0
    return 0.0


class NBAStatsService:
    def __init__(self, cache):
        self.cache = cache

    # ------------------------------------------------------------------ #
    #  Today's Games                                                       #
    # ------------------------------------------------------------------ #

    def get_games(self, day_offset: int = 0) -> list:
        """
        Fetch games for today (day_offset=0) or any offset (1=tomorrow, -1=yesterday).
        Uses ScoreboardV3 (ScoreboardV2 is deprecated for 2025-26 season).
        """
        today_mt = _today_mt()
        cache_key = f"games_{day_offset}_{today_mt.isoformat()}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        from nba_api.stats.endpoints import scoreboardv3
        from nba_api.stats.static import teams as nba_teams_static

        try:
            time.sleep(NBA_API_DELAY)
            target_date = (today_mt + timedelta(days=day_offset)).isoformat()

            def _fetch():
                return scoreboardv3.ScoreboardV3(
                    game_date=target_date,
                    league_id="00",
                    timeout=10,
                ).get_data_frames()

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
                _future = _pool.submit(_fetch)
                try:
                    dfs = _future.result(timeout=15)
                except concurrent.futures.TimeoutError:
                    logger.error("NBA API timed out after 15s (offset=%s)", day_offset)
                    return []
            games_df = dfs[1]   # one row per game
            teams_df = dfs[2]   # two rows per game (home + away teams)

            # Build tricode→team-info lookup from static list
            all_teams = nba_teams_static.get_teams()
            team_by_tri = {t["abbreviation"].upper(): t for t in all_teams}

            # Build wins/losses lookup: (gameId, teamId) → {wins, losses}
            team_record: dict = {}
            for _, row in teams_df.iterrows():
                key = (str(row["gameId"]), int(row["teamId"]))
                team_record[key] = {
                    "wins":   int(row["wins"])   if pd.notna(row["wins"])   else None,
                    "losses": int(row["losses"]) if pd.notna(row["losses"]) else None,
                }

            games = []
            for _, row in games_df.iterrows():
                game_id = str(row["gameId"])
                # gameCode format: "20260318/GSWBOS" — last 6 chars = away(3)+home(3)
                code     = str(row.get("gameCode", "")).split("/")[-1]
                away_tri = code[:3].upper()
                home_tri = code[3:].upper()

                home_t  = team_by_tri.get(home_tri, {})
                away_t  = team_by_tri.get(away_tri, {})
                home_id = home_t.get("id")
                away_id = away_t.get("id")

                home_rec = team_record.get((game_id, home_id), {}) if home_id else {}
                away_rec = team_record.get((game_id, away_id), {}) if away_id else {}

                status_text = str(row.get("gameStatusText", "")).strip()
                game_time   = _et_to_mt(status_text) if status_text else "TBD"

                games.append({
                    "game_id": game_id,
                    "home_team": {
                        "id":           home_id,
                        "name":         home_t.get("full_name", home_tri),
                        "abbreviation": home_t.get("abbreviation", home_tri),
                        "wins":         home_rec.get("wins"),
                        "losses":       home_rec.get("losses"),
                    },
                    "away_team": {
                        "id":           away_id,
                        "name":         away_t.get("full_name", away_tri),
                        "abbreviation": away_t.get("abbreviation", away_tri),
                        "wins":         away_rec.get("wins"),
                        "losses":       away_rec.get("losses"),
                    },
                    "game_time":   game_time,
                    "status_text": status_text,
                    "status_code": int(row.get("gameStatus", 1)),
                })

            self.cache.set(cache_key, games, timeout=1800)
            return games
        except Exception as exc:
            logger.error("Error fetching games (offset=%s): %s", day_offset, exc, exc_info=True)
            return []

    def build_games_from_events(self, events: list) -> list:
        """
        Fallback schedule builder used when get_games() comes back empty —
        typically because stats.nba.com is blocking Railway's datacenter IP.

        Synthesizes the same game shape from The Odds API events (which are
        reachable from cloud hosts). Team IDs/abbreviations are resolved from
        nba_api's *static* team list — a local lookup, no network call — so the
        rest of the app (rosters, injuries, props) keeps working.

        Limitations vs the real scoreboard: no NBA game_id, and no win/loss
        records. game_id is left empty; callers should use a date-based Finals
        check for these entries.
        """
        if not events:
            return []

        from nba_api.stats.static import teams as nba_teams_static

        all_teams = nba_teams_static.get_teams()
        # Lookup by full name and by nickname (last word), both normalized.
        by_full = {t["full_name"].lower(): t for t in all_teams}
        by_nick = {t["nickname"].lower(): t for t in all_teams}

        def _resolve(name: str) -> dict:
            if not name:
                return {}
            n = name.lower().strip()
            if n in by_full:
                return by_full[n]
            last = n.split()[-1] if n.split() else ""
            return by_nick.get(last, {})

        def _commence_to_mt(iso_utc: str) -> str:
            try:
                dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
                local = dt.astimezone(_MOUNTAIN)
                h = local.hour
                ampm = "pm" if h >= 12 else "am"
                if h > 12:
                    h -= 12
                elif h == 0:
                    h = 12
                return f"{h}:{local.minute:02d} {ampm} MT"
            except (ValueError, AttributeError):
                return "TBD"

        games = []
        for ev in events:
            home_t = _resolve(ev.get("home_team", ""))
            away_t = _resolve(ev.get("away_team", ""))
            game_time = _commence_to_mt(ev.get("commence_time", ""))
            games.append({
                "game_id": "",  # not available from the Odds API
                "home_team": {
                    "id":           home_t.get("id"),
                    "name":         home_t.get("full_name", ev.get("home_team", "")),
                    "abbreviation": home_t.get("abbreviation", ""),
                    "wins":         None,
                    "losses":       None,
                },
                "away_team": {
                    "id":           away_t.get("id"),
                    "name":         away_t.get("full_name", ev.get("away_team", "")),
                    "abbreviation": away_t.get("abbreviation", ""),
                    "wins":         None,
                    "losses":       None,
                },
                "game_time":   game_time,
                "status_text": game_time,
                "status_code": 1,  # scheduled
                "_from_odds":  True,
            })
        return games

    # Keep old name as an alias so nothing else breaks
    def get_today_games(self) -> list:
        return self.get_games(day_offset=0)

    # ------------------------------------------------------------------ #
    #  Team Roster                                                         #
    # ------------------------------------------------------------------ #

    def get_team_roster(self, team_id: int) -> list:
        cache_key = f"roster_{team_id}_{_today_mt().isoformat()}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        from nba_api.stats.endpoints import commonteamroster

        try:
            time.sleep(NBA_API_DELAY)
            ep = commonteamroster.CommonTeamRoster(
                team_id=team_id, season=CURRENT_SEASON, timeout=30
            )
            df = ep.get_data_frames()[0]

            players = [
                {
                    "player_id": int(row["PLAYER_ID"]),
                    "name": row["PLAYER"],
                    "position": row.get("POSITION", ""),
                    "number": str(row.get("NUM", "")),
                }
                for _, row in df.iterrows()
            ]

            self.cache.set(cache_key, players, timeout=3600)
            return players
        except Exception as exc:
            logger.error("Error fetching roster team=%s: %s", team_id, exc)
            raise

    # ------------------------------------------------------------------ #
    #  Player Stats                                                        #
    # ------------------------------------------------------------------ #

    def get_player_stats(self, player_id: int) -> dict:
        cache_key = f"player_stats_{player_id}_{_today_mt().isoformat()}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        from nba_api.stats.endpoints import commonplayerinfo, playergamelog

        try:
            # ---- player name + jersey number ----
            time.sleep(NBA_API_DELAY)
            info_ep = commonplayerinfo.CommonPlayerInfo(
                player_id=player_id, timeout=30
            )
            info_df = info_ep.get_data_frames()[0]
            player_name = (
                info_df["DISPLAY_FIRST_LAST"].iloc[0]
                if len(info_df) > 0
                else "Unknown"
            )
            team_id = int(info_df["TEAM_ID"].iloc[0]) if len(info_df) > 0 else 0
            jersey_number = str(info_df["JERSEY"].iloc[0]).strip() if len(info_df) > 0 else ""

            # ---- game log (regular season + playoffs merged) ----
            time.sleep(NBA_API_DELAY)
            log_ep = playergamelog.PlayerGameLog(
                player_id=player_id, season=CURRENT_SEASON, timeout=30
            )
            df_reg = log_ep.get_data_frames()[0]

            # Also fetch play-in and playoff games if we're in postseason
            df_playoff = pd.DataFrame()
            try:
                time.sleep(NBA_API_DELAY)
                log_po = playergamelog.PlayerGameLog(
                    player_id=player_id,
                    season=CURRENT_SEASON,
                    season_type_all_star="Playoffs",
                    timeout=30,
                )
                df_playoff = log_po.get_data_frames()[0]
            except Exception:
                pass  # Playoffs not started yet or endpoint unavailable

            df_playin = pd.DataFrame()
            try:
                time.sleep(NBA_API_DELAY)
                log_pi = playergamelog.PlayerGameLog(
                    player_id=player_id,
                    season=CURRENT_SEASON,
                    season_type_all_star="PlayIn",
                    timeout=30,
                )
                df_playin = log_pi.get_data_frames()[0]
            except Exception:
                pass  # Play-In not active or endpoint unavailable

            post_parts = [d for d in (df_playoff, df_playin) if not d.empty]
            if post_parts:
                df = pd.concat(post_parts + [df_reg], ignore_index=True)
                # Re-sort most-recent first using parsed dates
                df["_d"] = pd.to_datetime(
                    df["GAME_DATE"].apply(lambda x: str(x).title()),
                    format="%b %d, %Y", errors="coerce"
                )
                df = df.sort_values("_d", ascending=False).drop(columns=["_d"]).reset_index(drop=True)
            else:
                df = df_reg

            if df.empty:
                raise ValueError(f"No game log for player {player_id}")

            # Numeric coercion — FG3M added for threes metric
            for col in ["PTS", "REB", "AST", "OREB", "DREB", "FGA", "FG3A", "FG3M"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

            df["MIN"] = df["MIN"].apply(_parse_minutes)
            df["PRA"] = df["PTS"] + df["REB"] + df["AST"]

            # df is already sorted most-recent first
            season_avg  = self._calc_avg(df)
            last_5_avg  = self._calc_decay_avg(df.head(5))
            last_10_avg = self._calc_decay_avg(df.head(10))

            home_df = df[df["MATCHUP"].str.contains(r"vs\.", na=False)]
            away_df = df[df["MATCHUP"].str.contains("@", na=False)]
            home_avg = self._calc_avg(home_df) if len(home_df) > 0 else season_avg
            away_avg = self._calc_avg(away_df) if len(away_df) > 0 else season_avg

            # ── Back-to-back detection: did player play yesterday (ET)? ──
            is_back_to_back = False
            if not df.empty:
                try:
                    most_recent = datetime.strptime(
                        df["GAME_DATE"].iloc[0].title(), "%b %d, %Y"
                    ).date()
                    is_back_to_back = (most_recent == (_today_mt() - timedelta(days=1)))
                except (ValueError, TypeError):
                    is_back_to_back = False

            # ── Minutes volatility (CV of last 10 games) ─────────────────
            l10_min = df.head(10)["MIN"]
            minutes_cv = (
                round(float(l10_min.std() / l10_min.mean()), 3)
                if l10_min.mean() > 0 else 0.0
            )

            # Last 10 games data for charts (oldest→newest for x-axis)
            last_10_games = [
                {
                    "date":    row["GAME_DATE"],
                    "matchup": row["MATCHUP"],
                    "pts":  float(row["PTS"]),
                    "reb":  float(row["REB"]),
                    "ast":  float(row["AST"]),
                    "pra":  float(row["PRA"]),
                    "min":  float(row["MIN"]),
                    "fg3m": float(row["FG3M"]),
                }
                for _, row in df.head(10).iloc[::-1].iterrows()
            ]

            # Season rolling-5 trend (oldest→newest)
            chron = df.iloc[::-1].reset_index(drop=True)
            season_games = []
            for i, row in chron.iterrows():
                w = chron.iloc[max(0, i - 4) : i + 1]
                season_games.append(
                    {
                        "date":     row["GAME_DATE"],
                        "pts_r5":   round(float(w["PTS"].mean()), 1),
                        "reb_r5":   round(float(w["REB"].mean()), 1),
                        "ast_r5":   round(float(w["AST"].mean()), 1),
                        "pra_r5":   round(float(w["PRA"].mean()), 1),
                        "fg3m_r5":  round(float(w["FG3M"].mean()), 1),
                    }
                )

            std_devs = {
                "pts":    round(float(df["PTS"].std()), 2),
                "reb":    round(float(df["REB"].std()), 2),
                "ast":    round(float(df["AST"].std()), 2),
                "pra":    round(float(df["PRA"].std()), 2),
                "threes": round(float(df["FG3M"].std()), 2),
            }

            result = {
                "player_id":       player_id,
                "team_id":         team_id,
                "name":            player_name,
                "jersey_number":   jersey_number,
                "games_played":    len(df),
                "is_back_to_back": is_back_to_back,
                "minutes_cv":      minutes_cv,
                "season_avg":      season_avg,
                "last_5_avg":      last_5_avg,
                "last_10_avg":     last_10_avg,
                "home_avg":        home_avg,
                "away_avg":        away_avg,
                "last_10_games":   last_10_games,
                "season_games":    season_games,
                "std_devs":        std_devs,
            }

            self.cache.set(cache_key, result, timeout=3600)
            return result
        except Exception as exc:
            logger.error("Error fetching stats player=%s: %s", player_id, exc)
            raise

    @staticmethod
    def _calc_avg(df: pd.DataFrame) -> dict:
        if df is None or df.empty:
            return {"pts": 0.0, "reb": 0.0, "ast": 0.0, "pra": 0.0, "min": 0.0, "threes": 0.0}
        return {
            "pts":    round(float(df["PTS"].mean()), 1),
            "reb":    round(float(df["REB"].mean()), 1),
            "ast":    round(float(df["AST"].mean()), 1),
            "pra":    round(float(df["PRA"].mean()), 1),
            "min":    round(float(df["MIN"].mean()), 1),
            "threes": round(float(df["FG3M"].mean()), 1),
        }

    @staticmethod
    def _calc_decay_avg(df: pd.DataFrame, decay: float = 0.07) -> dict:
        """
        Exponential-decay weighted average. Most-recent game (index 0) is weighted highest.
        Recency bias: game from 1 day ago weight ≈0.93, 5 games ago ≈0.70, 10 games ago ≈0.50.
        """
        if df is None or df.empty:
            return {"pts": 0.0, "reb": 0.0, "ast": 0.0, "pra": 0.0, "min": 0.0, "threes": 0.0}
        return {
            "pts":    round(_exp_decay_avg(df["PTS"].values, decay), 1),
            "reb":    round(_exp_decay_avg(df["REB"].values, decay), 1),
            "ast":    round(_exp_decay_avg(df["AST"].values, decay), 1),
            "pra":    round(_exp_decay_avg(df["PRA"].values, decay), 1),
            "min":    round(_exp_decay_avg(df["MIN"].values, decay), 1),
            "threes": round(_exp_decay_avg(df["FG3M"].values, decay), 1),
        }

    # ------------------------------------------------------------------ #
    #  Player Usage / Minutes (for injury redistribution — Feature 1)      #
    # ------------------------------------------------------------------ #

    def _get_league_player_usage(self) -> dict:
        """
        League-wide per-player MIN + USG_PCT (Advanced box) in one call.
        Cached 1 h. Returns {player_id: {name, team_id, min, usg}}.
        """
        cache_key = f"league_player_usage_{_today_mt().isoformat()}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        from nba_api.stats.endpoints import leaguedashplayerstats

        def _fetch(season_type):
            return leaguedashplayerstats.LeagueDashPlayerStats(
                season=CURRENT_SEASON,
                measure_type_detailed_defense="Advanced",
                per_mode_detailed="PerGame",
                season_type_all_star=season_type,
                timeout=30,
            ).get_data_frames()[0]

        usage: dict = {}
        try:
            time.sleep(NBA_API_DELAY)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                df = pool.submit(_fetch, "Regular Season").result(timeout=20)
            for _, row in df.iterrows():
                usage[int(row["PLAYER_ID"])] = {
                    "name":    row.get("PLAYER_NAME", ""),
                    "team_id": int(row["TEAM_ID"]),
                    "min":     float(row.get("MIN", 0.0)),
                    "usg":     float(row.get("USG_PCT", 0.0)),
                }
        except Exception as exc:
            logger.error("Error fetching league player usage: %s", exc)
            return {}

        # Overlay postseason usage where available (playoff rotations tighten)
        for post_type in ("Playoffs", "PlayIn"):
            try:
                time.sleep(NBA_API_DELAY)
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    df_post = pool.submit(_fetch, post_type).result(timeout=20)
                if df_post is None or df_post.empty:
                    continue
                for _, row in df_post.iterrows():
                    pid = int(row["PLAYER_ID"])
                    if pid in usage:
                        # Postseason minutes/usage reflect current role better
                        usage[pid]["min"] = round(
                            usage[pid]["min"] * 0.4 + float(row.get("MIN", 0.0)) * 0.6, 1
                        )
                        usage[pid]["usg"] = round(
                            usage[pid]["usg"] * 0.4 + float(row.get("USG_PCT", 0.0)) * 0.6, 3
                        )
            except Exception:
                continue

        self.cache.set(cache_key, usage, timeout=3600)
        return usage

    def get_team_player_usage(self, team_id: int) -> dict:
        """Per-player MIN + USG_PCT for one team. {player_id: {name, min, usg}}."""
        league = self._get_league_player_usage()
        return {
            pid: {"name": v["name"], "min": v["min"], "usg": v["usg"]}
            for pid, v in league.items()
            if v["team_id"] == team_id
        }

    def get_finals_boxscore(self, game_id: str) -> list:
        """
        Fetch a single game's traditional box score for Finals backfill
        (Feature 2). Returns a list of per-player rows:
            [{player_id, name, team_abbr, pts, reb, ast, min, fg3m, usg}, ...]
        USG is not in the traditional box score, so it's left at 0.0 (the
        finals model treats missing usg gracefully).
        """
        from nba_api.stats.endpoints import boxscoretraditionalv3

        def _fetch():
            return boxscoretraditionalv3.BoxScoreTraditionalV3(
                game_id=game_id, timeout=20
            ).get_data_frames()[0]

        try:
            time.sleep(NBA_API_DELAY)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                df = pool.submit(_fetch).result(timeout=20)
        except Exception as exc:
            logger.warning("Finals boxscore fetch failed game=%s: %s", game_id, exc)
            return []

        if df is None or df.empty:
            return []

        def _minutes(val) -> float:
            # nba_api minutes can be "34:12" or "34.000000" or float
            if val is None:
                return 0.0
            s = str(val)
            if ":" in s:
                mm, ss = s.split(":")[:2]
                try:
                    return round(int(mm) + int(ss) / 60.0, 1)
                except ValueError:
                    return 0.0
            try:
                return round(float(s), 1)
            except ValueError:
                return 0.0

        rows = []
        for _, r in df.iterrows():
            try:
                first = r.get("firstName", "") or ""
                family = r.get("familyName", "") or ""
                name = (f"{first} {family}".strip()
                        or r.get("nameI", "") or r.get("playerName", ""))
                rows.append({
                    "player_id": int(r.get("personId", 0) or 0),
                    "name":      name,
                    "team_abbr": r.get("teamTricode", "") or "",
                    "pts":       float(r.get("points", 0) or 0),
                    "reb":       float(r.get("reboundsTotal", 0) or 0),
                    "ast":       float(r.get("assists", 0) or 0),
                    "min":       _minutes(r.get("minutes")),
                    "fg3m":      float(r.get("threePointersMade", 0) or 0),
                    "usg":       0.0,
                })
            except Exception:
                continue
        return rows

    # ------------------------------------------------------------------ #
    #  Team / League Defense                                               #
    # ------------------------------------------------------------------ #

    def get_team_defense_stats(self, team_id: int) -> dict | None:
        all_stats = self._get_league_team_stats()
        return next((t for t in all_stats if t["team_id"] == team_id), None)

    def get_league_avg_defense(self) -> dict:
        all_stats = self._get_league_team_stats()
        if not all_stats:
            return {"opp_pts": 115.0, "opp_reb": 44.5, "opp_ast": 25.5, "opp_fg3m": 8.5}
        return {
            "opp_pts":  round(sum(t["opp_pts"]  for t in all_stats) / len(all_stats), 1),
            "opp_reb":  round(sum(t["opp_reb"]  for t in all_stats) / len(all_stats), 1),
            "opp_ast":  round(sum(t["opp_ast"]  for t in all_stats) / len(all_stats), 1),
            "opp_fg3m": round(sum(t["opp_fg3m"] for t in all_stats) / len(all_stats), 1),
        }

    def _get_league_team_stats(self) -> list:
        cache_key = f"league_opp_stats_{_today_mt().isoformat()}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        from nba_api.stats.endpoints import leaguedashteamstats

        try:
            time.sleep(NBA_API_DELAY)
            ep = leaguedashteamstats.LeagueDashTeamStats(
                season=CURRENT_SEASON,
                measure_type_detailed_defense="Opponent",
                per_mode_detailed="PerGame",
                timeout=30,
            )
            df = ep.get_data_frames()[0]

            stats = [
                {
                    "team_id":   int(row["TEAM_ID"]),
                    "team_name": row["TEAM_NAME"],
                    "opp_pts":   float(row.get("OPP_PTS",  115.0)),
                    "opp_reb":   float(row.get("OPP_REB",   44.5)),
                    "opp_ast":   float(row.get("OPP_AST",   25.5)),
                    "opp_fg3m":  float(row.get("OPP_FG3M",   8.5)),
                }
                for _, row in df.iterrows()
            ]

            # Blend in postseason defensive data if available (40% weight total)
            # Try Playoffs first, then Play-In — whichever is available gets blended in.
            for post_type in ("Playoffs", "PlayIn"):
                try:
                    time.sleep(NBA_API_DELAY)
                    ep_post = leaguedashteamstats.LeagueDashTeamStats(
                        season=CURRENT_SEASON,
                        measure_type_detailed_defense="Opponent",
                        per_mode_detailed="PerGame",
                        season_type_all_star=post_type,
                        timeout=30,
                    )
                    df_post = ep_post.get_data_frames()[0]
                    if df_post.empty:
                        continue
                    post_lookup = {int(r["TEAM_ID"]): r for _, r in df_post.iterrows()}
                    for s in stats:
                        if s["team_id"] in post_lookup:
                            r = post_lookup[s["team_id"]]
                            for key, col in [("opp_pts", "OPP_PTS"), ("opp_reb", "OPP_REB"),
                                             ("opp_ast", "OPP_AST"), ("opp_fg3m", "OPP_FG3M")]:
                                post_val = float(r.get(col, s[key]))
                                s[key] = round(s[key] * 0.6 + post_val * 0.4, 1)
                except Exception:
                    continue  # That postseason bucket not available — try next

            self.cache.set(cache_key, stats, timeout=3600)
            return stats
        except Exception as exc:
            logger.error("Error fetching league opponent stats: %s", exc)
            return []
