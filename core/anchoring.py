"""
Anti-anchoring guards.

The market line is the aggregated opinion of everyone who has already looked at
this game. If it is in scope while a projection is formed, the projection will
drift toward it — not because anyone intends to cheat, but because that is what
anchoring is. Instructions do not prevent it; only structure does.

Three layers enforce the rule:

  1. Schema       — `predictions` has no market columns (see core/models.py).
  2. Import graph — `tests/test_anti_anchoring.py` fails if a projection module
                    can reach an odds module, transitively.
  3. Runtime      — `assert_market_free()`, called at the projection boundary,
                    rejects a feature dict containing anything market-shaped.

Layer 3 is the one that catches the realistic mistake: someone adds
`"dk_spread"` to a feature dict six months from now because it was convenient,
and the pipeline refuses it at the door instead of quietly producing a slightly
worse model forever.
"""
from __future__ import annotations

import re

# Modules that hold market data. Nothing reachable from a projection may import
# these, and the test enforces it by walking the real import graph.
MARKET_MODULES = frozenset({
    "core.odds_client",
    "core.odds_math",
    "core.snapshots",
    "core.credits",
    "core.picks",
    "services.odds",
})

# Substrings that make a feature key market-derived. Matched on word-ish
# boundaries so `pass_rush_win_rate` survives but `dk_line` does not.
FORBIDDEN_KEY_PATTERN = re.compile(
    r"(^|_)("
    r"line|lines|spread|spreads|total_line|moneyline|ml|odds|price|prices|"
    r"vig|juice|book|bookmaker|dk|draftkings|fanduel|market|implied|"
    r"closing|open_line|handle|ticket|consensus|edge|vegas"
    r")($|_)",
    re.IGNORECASE,
)

# Explicit allowances: legitimate model features whose names collide with the
# pattern above. Each one is a deliberate, reviewed exception.
ALLOWED_KEYS = frozenset({
    "offensive_line_rank",     # a unit of the team, not a betting line
    "o_line_pass_block_win",
    "d_line_pressure_rate",
    "goal_line_carries",
    "goal_line_share",
    "line_of_scrimmage_epa",
})


class MarketDataLeak(AssertionError):
    """Raised when market-derived data reaches the projection layer."""


def is_forbidden_key(key: str) -> bool:
    if key in ALLOWED_KEYS:
        return False
    return bool(FORBIDDEN_KEY_PATTERN.search(key))


def find_market_keys(obj, _path: str = "") -> list[str]:
    """Recursively collect market-shaped keys from a nested feature structure."""
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{_path}.{key}" if _path else str(key)
            if isinstance(key, str) and is_forbidden_key(key):
                found.append(path)
            found.extend(find_market_keys(value, path))
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            found.extend(find_market_keys(value, f"{_path}[{i}]"))
    return found


def assert_market_free(features, *, context: str = "projection input") -> None:
    """
    Reject a feature structure that contains market-derived data.

    Call this at the boundary of every projection function. It is cheap, it
    runs in production, and it converts a silent modelling failure into a loud
    one at the moment the bad data is introduced.
    """
    offenders = find_market_keys(features)
    if offenders:
        raise MarketDataLeak(
            f"Market-derived data reached the {context}: {', '.join(sorted(offenders))}. "
            "Projections must be formed without the line. Join the market "
            "downstream instead (see core/picks.py)."
        )
