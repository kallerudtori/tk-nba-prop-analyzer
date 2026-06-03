"""
SQLite database — bet tracking, injury overrides, and Finals series state.
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "bets.db")

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the bets table (and supporting tables) if they don't exist."""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            bet_type         TEXT NOT NULL DEFAULT 'prop',
            player_name      TEXT,
            prop_type        TEXT,
            line             REAL,
            over_under       TEXT,
            odds             INTEGER,
            model_projection REAL,
            model_edge       REAL,
            model_confidence TEXT,
            model_prob_over  REAL,
            game_label       TEXT,
            game_date        TEXT,
            pick_label       TEXT,
            placed           INTEGER DEFAULT 1,
            status           TEXT NOT NULL DEFAULT 'pending',
            actual_value     REAL,
            settled_at       TIMESTAMP
        )
    """)

    # ── Manual injury overrides (Feature 1) ──────────────────────────────
    # source: 'manual' — always takes precedence over the ESPN feed.
    # status: Active | Questionable | Doubtful | Out | GTD
    # game_date scopes an override to a single slate so stale entries
    # don't leak into future days.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS injury_overrides (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id   INTEGER,
            player_name TEXT,
            team_id     INTEGER,
            status      TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'manual',
            notes       TEXT,
            game_date   TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_injury_date ON injury_overrides(game_date)"
    )

    # ── Finals series state (Feature 2) ──────────────────────────────────
    # Durable across Railway redeploys (SimpleCache is in-memory and dies).
    # One row per Finals game; weights accumulate as the series progresses.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS finals_series (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            year        INTEGER NOT NULL,
            game_number INTEGER NOT NULL,
            game_date   TEXT,
            away_team   TEXT,
            home_team   TEXT,
            away_score  INTEGER,
            home_score  INTEGER,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(year, game_number)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS finals_player_stats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            year        INTEGER NOT NULL,
            game_number INTEGER NOT NULL,
            player_id   INTEGER,
            player_name TEXT,
            team        TEXT,
            pts  REAL, reb REAL, ast REAL, min REAL, fg3m REAL, usg REAL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(year, game_number, player_id)
        )
    """)

    conn.commit()
    conn.close()
