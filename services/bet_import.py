"""
DraftKings CSV Bet Import Parser

Expected DraftKings CSV columns (as exported from Account -> My Bets -> Export):
  - "Bet"        : Full bet description string (e.g. "LeBron James - Points - Over 25.5")
  - "Bet Type"   : e.g. "Single", "Parlay"
  - "Pick"       : Short pick description (e.g. "LeBron James Over 25.5 Points")
  - "Event"      : Game matchup string (e.g. "LA Lakers vs Boston Celtics")
  - "Market"     : Market/prop type (e.g. "Player Points", "Player Rebounds")
  - "Selection"  : Selected outcome (e.g. "Over 25.5")
  - "Odds"       : Odds in American format (e.g. "-110", "+120") or decimal (e.g. "1.91")
  - "Stake"      : Wager amount in dollars (e.g. "10.00")
  - "Result"     : Outcome ("Won", "Lost", "Void", "Open")
  - "Winnings"   : Amount won (e.g. "9.09")
  - "Date"       : Bet placement date (e.g. "2024-03-15" or "Mar 15, 2024")

Notes:
  - Rows with missing player_name, prop_type, or line are skipped.
  - Duplicate detection is based on (player_name, prop_type, game_date, line).
  - Decimal odds (e.g. 1.91) are converted to American format automatically.
  - Only "Single" bet types are imported by default; parlays are skipped.
"""

import csv
import io
import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Map DraftKings result strings to internal status values
_RESULT_MAP = {
    "won":   "won",
    "win":   "won",
    "lost":  "lost",
    "loss":  "lost",
    "void":  "void",
    "push":  "void",
    "open":  "pending",
    "":      "pending",
}

# Map DraftKings market/prop strings to internal prop_type values
_PROP_MAP = {
    "points":     "points",
    "pts":        "points",
    "player points": "points",
    "rebounds":   "rebounds",
    "rebs":       "rebounds",
    "reb":        "rebounds",
    "player rebounds": "rebounds",
    "assists":    "assists",
    "ast":        "assists",
    "player assists": "assists",
    "pra":        "pra",
    "pts+reb+ast": "pra",
    "points + rebounds + assists": "pra",
    "3-point field goals made": "threes",
    "three point field goals made": "threes",
    "threes":     "threes",
    "3pm":        "threes",
    "3-pointers made": "threes",
    "player threes": "threes",
}


def _normalize_prop(raw: str) -> str | None:
    """Map a DraftKings market/prop name to internal prop_type."""
    key = raw.strip().lower()
    if key in _PROP_MAP:
        return _PROP_MAP[key]
    # Fuzzy match on substrings
    for dk_key, internal in _PROP_MAP.items():
        if dk_key in key or key in dk_key:
            return internal
    return None


def _parse_line(text: str) -> float | None:
    """Extract numeric line from strings like 'Over 25.5' or '25.5'."""
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1))
    return None


def _parse_over_under(text: str) -> str:
    """Return 'over' or 'under' from a selection string."""
    tl = text.lower()
    if "under" in tl:
        return "under"
    return "over"


def _parse_date(text: str) -> str | None:
    """Normalise various DraftKings date formats to YYYY-MM-DD."""
    text = text.strip()
    if not text:
        return None
    # Try ISO format first
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y",
                "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(text[:len(fmt) + 4], fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Strip time component and retry
    date_part = re.split(r"[T ]", text)[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_part, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _decimal_to_american(dec: float) -> int:
    """Convert decimal odds (e.g. 1.91) to American odds (-110)."""
    if dec >= 2.0:
        return int(round((dec - 1) * 100))
    else:
        return int(round(-100 / (dec - 1)))


def _parse_odds(text: str) -> int | None:
    """Parse odds string to American integer (e.g. '-110', '+120', '1.91')."""
    text = text.strip().replace(",", "")
    if not text:
        return None
    try:
        val = float(text)
        # If no sign and < 10, it's likely decimal odds
        if "+" not in text and "-" not in text and val < 10:
            return _decimal_to_american(val)
        # Otherwise treat as American
        return int(val)
    except ValueError:
        return None


def _parse_pick_field(pick: str, market: str) -> tuple[str | None, str | None, float | None, str]:
    """
    Attempt to extract (player_name, prop_type, line, over_under) from the
    'Pick' or 'Bet' column.

    DK pick examples:
      "LeBron James Over 25.5 Points"
      "Jayson Tatum - Under 8.5 Rebounds"
    """
    player_name = None
    prop_type = _normalize_prop(market) if market else None
    line = None
    over_under = "over"

    # Remove common separators
    clean = pick.replace(" - ", " ").strip()

    # Find Over/Under keyword and line
    m = re.search(r"\b(over|under)\s+(\d+(?:\.\d+)?)\b", clean, re.IGNORECASE)
    if m:
        over_under = m.group(1).lower()
        line = float(m.group(2))
        # Player name is the text before the over/under match
        player_name = clean[:m.start()].strip()
        # Remove trailing prop-type words from player name
        prop_words = {"points", "rebounds", "assists", "threes", "3pm", "pra",
                      "pts", "reb", "ast", "made", "field", "goals"}
        parts = player_name.split()
        while parts and parts[-1].lower() in prop_words:
            parts.pop()
        player_name = " ".join(parts).strip(" -").strip() or None
    else:
        # Try to find just a line number
        m2 = re.search(r"(\d+(?:\.\d+)?)", clean)
        if m2:
            line = float(m2.group(1))

    return player_name, prop_type, line, over_under


def parse_dk_csv(file_content: str | bytes) -> list[dict]:
    """
    Parse a DraftKings CSV export and return a list of bet dicts ready
    for insertion into the bets table.

    Returns:
        list of dicts with keys matching bets table columns
    """
    if isinstance(file_content, bytes):
        # Try UTF-8 first, fall back to latin-1
        try:
            file_content = file_content.decode("utf-8-sig")
        except UnicodeDecodeError:
            file_content = file_content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(file_content))
    # Normalise header names to lowercase stripped
    rows = []
    for raw_row in reader:
        row = {k.strip().lower(): v.strip() for k, v in raw_row.items() if k}
        rows.append(row)

    parsed = []
    for i, row in enumerate(rows):
        try:
            # Skip parlays — they have multiple legs and can't be tracked individually
            bet_type = row.get("bet type", "").lower()
            if "parlay" in bet_type:
                logger.debug("Skipping parlay row %d", i)
                continue

            pick    = row.get("pick", "") or row.get("bet", "") or row.get("selection", "")
            market  = row.get("market", "")
            event   = row.get("event", "")
            odds_str = row.get("odds", "")
            stake_str = row.get("stake", "")
            result_str = row.get("result", "").lower().strip()
            date_str = row.get("date", "")

            player_name, prop_type, line, over_under = _parse_pick_field(pick, market)

            # Attempt to fill gaps from 'selection' field
            if not player_name or line is None:
                selection = row.get("selection", "")
                if selection:
                    _, _, sel_line, sel_ou = _parse_pick_field(selection, market)
                    line = line or sel_line
                    over_under = sel_ou if sel_line else over_under

            if not player_name or not prop_type or line is None:
                logger.debug("Row %d skipped — missing player/prop/line: pick=%r", i, pick)
                continue

            # Parse remaining fields
            game_date = _parse_date(date_str)
            over_odds = _parse_odds(odds_str)
            wager_amount = None
            try:
                wager_amount = float(stake_str.replace("$", "").replace(",", "")) if stake_str else None
            except ValueError:
                pass

            status = _RESULT_MAP.get(result_str, "pending")

            # Implied prob from American odds
            implied_prob_over = None
            if over_odds is not None:
                try:
                    if over_odds < 0:
                        implied_prob_over = round(-over_odds / (-over_odds + 100), 4)
                    else:
                        implied_prob_over = round(100 / (over_odds + 100), 4)
                except ZeroDivisionError:
                    pass

            # Parse opponent from event string (e.g. "LAL vs BOS" or "BOS @ LAL")
            opponent = None
            if event:
                parts = re.split(r"\s+(?:vs\.?|@)\s+", event, flags=re.IGNORECASE)
                if len(parts) == 2:
                    opponent = parts[1].strip()

            parsed.append({
                "player_name":      player_name,
                "prop_type":        prop_type,
                "line":             line,
                "over_under":       over_under,
                "model_projection": None,
                "model_edge":       None,
                "model_confidence": None,
                "model_prob_over":  None,
                "over_odds":        over_odds,
                "implied_prob_over": implied_prob_over,
                "game_date":        game_date,
                "opponent":         opponent,
                "is_home":          None,
                "wager_amount":     wager_amount,
                "status":           status,
            })

        except Exception as exc:
            logger.warning("Error parsing CSV row %d: %s", i, exc)
            continue

    return parsed
