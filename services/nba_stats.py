"""
NBA Stats Service
Fetches and caches data from the nba_api library.
Cache TTL: 1 hour for stats, 30 min for today's games/rosters.
"""

import math
import time
import logging
from datetime import datetime, timedelta

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

# Current NBA season — update each October
CURRENT_SEASON = "2025-26"

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
            sb = scoreboardv3.ScoreboardV3(
                game_date=target_date,
                league_id="00",
                timeout=30,
            )
            dfs = sb.get_data_frames()
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
            raise

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

            # ---- game log ----
            time.sleep(NBA_API_DELAY)
            log_ep = playergamelog.PlayerGameLog(
                player_id=player_id, season=CURRENT_SEASON, timeout=30
            )
            df = log_ep.get_data_frames()[0]

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

            self.cache.set(cache_key, stats, timeout=3600)
            return stats
        except Exception as exc:
            logger.error("Error fetching league opponent stats: %s", exc)
            return []
