"""
Injury Service (Feature 1)

Two data sources, merged with manual taking precedence:
  1. ESPN public injuries JSON (free, undocumented but stable) — primary feed.
  2. Manual overrides stored in SQLite — always win, scoped per game_date.
     Critical because injury news drops 30-60 min before tip and APIs lag.

Status enum (normalized):  Active | Questionable | Doubtful | Out | GTD
"""

import logging
import unicodedata
from datetime import date as _date

import requests

logger = logging.getLogger(__name__)

ESPN_INJURIES_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
)

# Canonical statuses
ACTIVE      = "Active"
QUESTIONABLE = "Questionable"
DOUBTFUL    = "Doubtful"
OUT         = "Out"
GTD         = "GTD"

VALID_STATUSES = {ACTIVE, QUESTIONABLE, DOUBTFUL, OUT, GTD}

# Severity ranking — higher = more sidelined. Used to reconcile conflicting
# ESPN signals (top-level `status` vs `fantasyStatus`) by taking the worse one.
_SEVERITY = {ACTIVE: 0, GTD: 1, QUESTIONABLE: 2, DOUBTFUL: 3, OUT: 4}


def _more_severe(a: str, b: str) -> str:
    """Return whichever canonical status sidelines the player more."""
    return a if _SEVERITY.get(a, 0) >= _SEVERITY.get(b, 0) else b

# Statuses that fully sideline a player → excluded from suggestions
SIDELINED = {OUT, DOUBTFUL}
# Statuses that are uncertain → kept in suggestions but flagged
UNCERTAIN = {QUESTIONABLE, GTD}


def _normalize_name(s: str) -> str:
    """Lowercase, strip diacritics and periods. Mirrors OddsService logic."""
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
    return s.replace(".", "").lower().strip()


def classify_status(raw: str) -> str:
    """Map an ESPN status / fantasyStatus string to our canonical enum."""
    if not raw:
        return ACTIVE
    r = raw.strip().lower().replace("-", " ").replace("_", " ")
    if r in ("out", "o", "ofs", "out for season", "ir", "injured reserve",
             "nfi", "susp", "suspension", "suspended"):
        # OFS = out for season, IR = injured reserve, NFI = non-football/
        # basketball injury — all sideline the player.
        return OUT
    if r in ("doubtful", "d"):
        return DOUBTFUL
    if r in ("questionable", "q"):
        return QUESTIONABLE
    if r in ("gtd", "game time decision", "game-time decision"):
        return GTD
    if r in ("day to day", "dtd", "probable", "p"):
        # Day-to-day / probable = uncertain but likely; treat as GTD so it's
        # flagged without being excluded.
        return GTD
    if r in ("active", "available"):
        return ACTIVE
    return ACTIVE


# ── ESPN feed ──────────────────────────────────────────────────────────────

def fetch_espn_injuries(cache) -> dict:
    """
    Fetch and cache the ESPN injuries feed (10 min TTL).

    Returns dict keyed by normalized player name:
        { "lebron james": {
              "status": "Out",
              "raw_status": "Out",
              "description": "Knee — Soreness",
              "position": "F",
              "team_abbr": "LAL",
              "source": "espn",
          }, ... }
    """
    cache_key = "injuries_espn"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    result: dict = {}
    try:
        resp = requests.get(ESPN_INJURIES_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning("ESPN injuries fetch failed: %s", exc)
        cache.set(cache_key, {}, timeout=120)  # short cache so we retry soon
        return {}

    for team in data.get("injuries", []):
        team_abbr = ""
        for item in team.get("injuries", []):
            ath = item.get("athlete", {}) or {}
            name = ath.get("displayName", "")
            if not name:
                continue

            # Team abbreviation (per-athlete, falls back to team block)
            ath_team = ath.get("team", {}) or {}
            team_abbr = ath_team.get("abbreviation", "") or team_abbr

            # Position abbreviation if present
            pos = (ath.get("position", {}) or {}).get("abbreviation", "")

            # Reconcile the two ESPN signals: the top-level `status` string and
            # the finer-grained `fantasyStatus`. Take whichever is MORE severe,
            # so an unrecognized fantasy code (e.g. "OFS") can never downgrade a
            # clear top-level "Out" to Active.
            details = item.get("details", {}) or {}
            fantasy = (details.get("fantasyStatus", {}) or {}).get("abbreviation", "")
            top_status = item.get("status", "")
            status = _more_severe(classify_status(fantasy), classify_status(top_status))
            raw_status = top_status or fantasy

            # Human-readable description
            desc_parts = [p for p in (details.get("type"), details.get("detail")) if p]
            description = " — ".join(desc_parts) if desc_parts else item.get("status", "")

            result[_normalize_name(name)] = {
                "status": status,
                "raw_status": item.get("status", raw_status),
                "description": description,
                "position": pos,
                "team_abbr": team_abbr,
                "source": "espn",
            }

    cache.set(cache_key, result, timeout=600)
    logger.info("ESPN injuries: %d players flagged", len(result))
    return result


# ── Manual overrides (SQLite) ────────────────────────────────────────────────

def add_override(player_id, player_name, team_id, status, notes, game_date) -> int:
    """Insert (or replace same-day same-player) a manual injury override."""
    from database.db import get_conn
    status = status if status in VALID_STATUSES else classify_status(status)
    conn = get_conn()
    # Remove any prior same-day override for this player so we don't stack
    conn.execute(
        "DELETE FROM injury_overrides WHERE game_date = ? AND "
        "(player_id = ? OR lower(player_name) = lower(?))",
        (game_date, player_id, player_name or ""),
    )
    cur = conn.execute(
        """INSERT INTO injury_overrides
             (player_id, player_name, team_id, status, source, notes, game_date)
           VALUES (?,?,?,?,'manual',?,?)""",
        (player_id, player_name, team_id, status, notes, game_date),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def list_overrides(game_date: str | None = None) -> list:
    from database.db import get_conn
    conn = get_conn()
    if game_date:
        rows = conn.execute(
            "SELECT * FROM injury_overrides WHERE game_date = ? ORDER BY created_at DESC",
            (game_date,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM injury_overrides ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_override(override_id: int) -> None:
    from database.db import get_conn
    conn = get_conn()
    conn.execute("DELETE FROM injury_overrides WHERE id = ?", (override_id,))
    conn.commit()
    conn.close()


def get_manual_map(game_date: str) -> dict:
    """
    Return manual overrides for a date as two lookups:
        { "by_name": {normalized_name: {...}}, "by_id": {player_id: {...}} }
    """
    by_name: dict = {}
    by_id: dict = {}
    for row in list_overrides(game_date):
        entry = {
            "status": row["status"],
            "raw_status": row["status"],
            "description": row.get("notes") or "Manual override",
            "position": "",
            "team_abbr": "",
            "source": "manual",
            "override_id": row["id"],
        }
        if row.get("player_name"):
            by_name[_normalize_name(row["player_name"])] = entry
        if row.get("player_id"):
            by_id[int(row["player_id"])] = entry
    return {"by_name": by_name, "by_id": by_id}


# ── Resolution ───────────────────────────────────────────────────────────────

def get_injury_map(cache, game_date: str | None = None) -> dict:
    """
    Merge ESPN + manual overrides. Manual wins. Returns:
        { "espn": {name: entry}, "manual": {"by_name":..., "by_id":...} }
    Use status_for_player() to resolve an individual player.
    """
    game_date = game_date or _date.today().isoformat()
    return {
        "espn":   fetch_espn_injuries(cache),
        "manual": get_manual_map(game_date),
    }


def status_for_player(injury_map: dict, player_id, player_name: str) -> dict:
    """
    Resolve a single player's status. Manual override > ESPN > Active.
    Returns {status, raw_status, description, source}.
    """
    norm = _normalize_name(player_name)
    manual = injury_map.get("manual", {})
    espn = injury_map.get("espn", {})

    # 1. Manual by player_id (most precise)
    if player_id is not None and int(player_id) in manual.get("by_id", {}):
        return manual["by_id"][int(player_id)]
    # 2. Manual by name
    if norm in manual.get("by_name", {}):
        return manual["by_name"][norm]
    # 3. ESPN by name (exact)
    if norm in espn:
        return espn[norm]
    # 4. ESPN by last name + first-initial (handles "C. Cunningham" vs
    #    "Cade Cunningham"). The first initial MUST agree, otherwise two
    #    different players sharing a common surname (e.g. Tre Johnson vs
    #    Keldon Johnson) would be wrongly matched.
    p_parts = norm.split()
    p_last = p_parts[-1] if p_parts else ""
    p_first = p_parts[0] if len(p_parts) > 1 else ""
    if len(p_last) > 2 and p_first:
        for ename, entry in espn.items():
            e_parts = ename.split()
            if len(e_parts) < 2:
                continue
            if e_parts[-1] == p_last and e_parts[0][0] == p_first[0]:
                return entry
    return {
        "status": ACTIVE,
        "raw_status": "Active",
        "description": "",
        "source": "default",
    }
