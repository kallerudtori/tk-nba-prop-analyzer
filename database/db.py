"""
SQLite database — bet tracking (single bets table)
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
    """Create the bets table if it doesn't exist."""
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
    conn.commit()
    conn.close()
