"""
Season derivation for the NBA data layer.

This was a hardcoded constant with a comment instructing a human to update it
every October. The update was missed, and the app served the previous season
for months: traded players appeared on their old rosters (Naz Reid shown on
Minnesota after moving to Charlotte), "games played this season" exceeded 82
because a complete regular season and playoffs were merged, and "last 5 games"
meant the previous spring's playoffs rather than recent form.

These cases pin the rollover so the same failure cannot recur silently.
"""
from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path

import pytest


def _load_derive_season():
    """
    Load the function without importing the module.

    services/nba_stats.py imports pandas, numpy and nba_api at module scope,
    so importing it makes a pure date-arithmetic test depend on the whole data
    stack being installed. Lifting the function out keeps this test runnable
    anywhere.
    """
    src = (Path(__file__).resolve().parents[1] / "services" / "nba_stats.py").read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_derive_season"
    )
    ns = {"date": date}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<ast>", "exec"), ns)
    return ns["_derive_season"]


derive_season = _load_derive_season()


@pytest.mark.parametrize("today,expected", [
    # The season flips on 1 October.
    (date(2026, 9, 30), "2025-26"),
    (date(2026, 10, 1), "2026-27"),
    # In-season months.
    (date(2026, 11, 15), "2026-27"),   # the date the bug was spotted
    (date(2026, 12, 25), "2026-27"),
    (date(2027, 1, 5),   "2026-27"),
    (date(2027, 4, 10),  "2026-27"),   # playoffs belong to that season
    # The offseason keeps returning the season that just finished, which is
    # the data you actually want in July.
    (date(2027, 7, 1),   "2026-27"),
    # Decade and century rollovers stay two digits, zero-padded.
    (date(2029, 10, 1),  "2029-30"),
    (date(2099, 11, 1),  "2099-00"),
])
def test_season_rollover(today, expected):
    assert derive_season(today) == expected


def test_derived_season_is_not_the_stale_constant():
    """The value that sat hardcoded must not be what late 2026 resolves to."""
    assert derive_season(date(2026, 11, 15)) != "2025-26"


def test_season_string_shape():
    """nba_api rejects any season string that is not exactly YYYY-YY."""
    for year in range(2020, 2035):
        for month in (1, 6, 10, 12):
            season = derive_season(date(year, month, 1))
            assert re.fullmatch(r"\d{4}-\d{2}", season), season
