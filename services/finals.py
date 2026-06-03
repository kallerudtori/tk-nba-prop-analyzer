"""
Finals Service (Feature 2)

Detects NBA Finals games and persists per-game series state so that Finals Mode
can weight a player's *Finals-series* performance against their regular
projection as the series progresses.

Detection (auto, per spec — confirmed acceptable):
  • NBA game IDs are 10 chars: 004 + SS(season) + R(round) + matchup + game.
    For playoff games (prefix "004") the round digit sits at index 7.
    Round 4 == the Finals. Game number == last two digits.
  • Fallback: any playoff game ("004") played in June is the Finals — by then
    the Finals is the only series still running.

Persistence uses the finals_series + finals_player_stats tables (durable across
Railway redeploys, unlike the in-memory cache).
"""

import logging
from datetime import date as _date

logger = logging.getLogger(__name__)

# Best-of-7
FINALS_MAX_GAMES = 7


# ── Detection ─────────────────────────────────────────────────────────────────

def is_finals_game(game_id, game_date: str | None = None) -> bool:
    """True if this game_id is an NBA Finals game."""
    gid = str(game_id or "")
    if not gid.startswith("004"):
        return False
    # Standard 10-char playoff id → round digit at index 7
    if len(gid) >= 8 and gid[7] == "4":
        return True
    # June fallback: the only playoff series in June is the Finals
    if game_date:
        try:
            return int(str(game_date).split("-")[1]) == 6
        except (ValueError, IndexError):
            pass
    return False


def finals_game_number(game_id) -> int:
    """Game number within the series (1-7), from the last two id digits."""
    gid = str(game_id or "")
    if len(gid) >= 2 and gid[-2:].isdigit():
        n = int(gid[-2:])
        if 1 <= n <= FINALS_MAX_GAMES:
            return n
    return 1


def finals_season_year(game_id, game_date: str | None = None) -> int:
    """
    NBA-style season start year. The 2025-26 Finals (June 2026) → 2025.
    Derived from the season chars in the game id, falling back to the date.
    """
    gid = str(game_id or "")
    if len(gid) >= 5 and gid[3:5].isdigit():
        yy = int(gid[3:5])
        return 2000 + yy
    if game_date:
        try:
            y = int(str(game_date).split("-")[0])
            # June Finals belongs to the season that *started* the prior fall
            return y - 1
        except (ValueError, IndexError):
            pass
    return _date.today().year - 1


# ── Series state (SQLite) ─────────────────────────────────────────────────────

def record_finals_game(year, game_number, game_date,
                       away_team, home_team, away_score, home_score) -> None:
    """Upsert one Finals game's result (idempotent on year+game_number)."""
    from database.db import get_conn
    conn = get_conn()
    conn.execute(
        """INSERT INTO finals_series
             (year, game_number, game_date, away_team, home_team, away_score, home_score)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(year, game_number) DO UPDATE SET
             game_date=excluded.game_date,
             away_team=excluded.away_team, home_team=excluded.home_team,
             away_score=excluded.away_score, home_score=excluded.home_score""",
        (year, game_number, game_date, away_team, home_team, away_score, home_score),
    )
    conn.commit()
    conn.close()


def record_finals_player_stat(year, game_number, player_id, player_name,
                              team, pts, reb, ast, mins, fg3m, usg) -> None:
    """Upsert one player's box line for a Finals game."""
    from database.db import get_conn
    conn = get_conn()
    conn.execute(
        """INSERT INTO finals_player_stats
             (year, game_number, player_id, player_name, team, pts, reb, ast, min, fg3m, usg)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(year, game_number, player_id) DO UPDATE SET
             pts=excluded.pts, reb=excluded.reb, ast=excluded.ast,
             min=excluded.min, fg3m=excluded.fg3m, usg=excluded.usg""",
        (year, game_number, player_id, player_name, team,
         pts, reb, ast, mins, fg3m, usg),
    )
    conn.commit()
    conn.close()


def get_series_games(year: int) -> list:
    """All recorded games for a Finals year, ordered by game number."""
    from database.db import get_conn
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM finals_series WHERE year = ? ORDER BY game_number",
        (year,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_player_finals_stats(year: int, player_id: int) -> dict | None:
    """
    Average a player's recorded Finals box lines for this series so far.
    Returns {pts, reb, ast, min, fg3m, usg, pra, games} or None if no data.
    """
    from database.db import get_conn
    conn = get_conn()
    rows = conn.execute(
        "SELECT pts, reb, ast, min, fg3m, usg FROM finals_player_stats "
        "WHERE year = ? AND player_id = ?",
        (year, player_id),
    ).fetchall()
    conn.close()
    if not rows:
        return None
    n = len(rows)
    agg = {k: 0.0 for k in ("pts", "reb", "ast", "min", "fg3m", "usg")}
    for r in rows:
        for k in agg:
            agg[k] += float(r[k] or 0.0)
    avg = {k: round(v / n, 2) for k, v in agg.items()}
    avg["pra"] = round(avg["pts"] + avg["reb"] + avg["ast"], 2)
    avg["games"] = n
    return avg


def series_record(year: int) -> dict:
    """
    Compute the series win tally from recorded games.
    Returns {teams: {name: wins}, games_played, leader, summary}.
    """
    games = get_series_games(year)
    wins: dict = {}
    for g in games:
        if g["home_score"] is None or g["away_score"] is None:
            continue
        if g["home_score"] > g["away_score"]:
            winner = g["home_team"]
        else:
            winner = g["away_team"]
        if winner:
            wins[winner] = wins.get(winner, 0) + 1
    games_played = len([g for g in games if g["home_score"] is not None])
    leader = max(wins, key=wins.get) if wins else None
    if wins:
        summary = ", ".join(f"{t} {w}" for t, w in sorted(wins.items(), key=lambda kv: -kv[1]))
    else:
        summary = "Series tied 0-0"
    return {
        "teams": wins,
        "games_played": games_played,
        "leader": leader,
        "summary": summary,
    }
