"""
Structural enforcement of the anti-anchoring rule.

The rule: a projection is formed without the market line in scope. Not
"analysts are instructed not to look at it" — the line must be unreachable,
because anchoring happens whether or not anyone intends it.

These tests are the enforcement mechanism the build spec asks for. They fail
if odds data becomes reachable from the projection layer by any of the four
routes we can actually check: the import graph, function signatures, the
database schema, and the runtime feature payload.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.anchoring import (
    MARKET_MODULES, MarketDataLeak, assert_market_free, is_forbidden_key,
)

ROOT = Path(__file__).resolve().parents[1]

# Modules that form projections. Every new projection module must be added here
# — that is the point of the list, and a projection module absent from it is a
# gap in the guarantee, not a module exempt from it.
PROJECTION_MODULES = ["core.predictions", "core.anchoring"]

# Packages whose entire contents are projection code, discovered dynamically so
# a new NFL model file cannot silently escape the check by not being listed.
PROJECTION_PACKAGES = ["nfl.model", "nfl.props"]


def _module_path(dotted: str) -> Path | None:
    p = ROOT / (dotted.replace(".", "/") + ".py")
    return p if p.exists() else None


def _discover_projection_modules() -> list[str]:
    modules = list(PROJECTION_MODULES)
    for pkg in PROJECTION_PACKAGES:
        pkg_dir = ROOT / pkg.replace(".", "/")
        if pkg_dir.is_dir():
            for path in sorted(pkg_dir.rglob("*.py")):
                if path.name != "__init__.py":
                    modules.append(str(path.relative_to(ROOT)).replace("/", ".")[:-3])
    return modules


def _imports_of(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
    return found


def _transitive_imports(dotted: str, seen: set[str] | None = None) -> set[str]:
    """Walk the real import graph, following first-party modules only."""
    seen = seen if seen is not None else set()
    if dotted in seen:
        return seen
    seen.add(dotted)
    path = _module_path(dotted)
    if path is None:
        return seen
    for imported in _imports_of(path):
        if imported in seen:
            continue
        if _module_path(imported) is not None:
            _transitive_imports(imported, seen)
        else:
            seen.add(imported)
    return seen


@pytest.mark.parametrize("module", _discover_projection_modules())
def test_projection_modules_cannot_reach_market_data(module):
    """
    No projection module may import an odds module, directly or transitively.

    `core.anchoring` names the market modules as strings for exactly this
    reason: it can enforce the boundary without importing across it.
    """
    leaked = sorted(_transitive_imports(module) & set(MARKET_MODULES))
    assert not leaked, (
        f"{module} can reach market data via {leaked}. A projection must be "
        f"formed without the line in scope. Move the market join downstream "
        f"into core/picks.py."
    )


def test_store_prediction_accepts_no_market_argument():
    """The projection store must offer no channel for a line to arrive through."""
    from core.predictions import store_prediction

    params = list(inspect.signature(store_prediction).parameters)
    offenders = [p for p in params if is_forbidden_key(p)]
    assert not offenders, f"store_prediction exposes market-shaped params: {offenders}"


def test_predictions_table_has_no_market_columns():
    """
    The schema itself must have nowhere to put a line.

    Mirrors the `assert_predictions_market_free()` SQL function created by the
    initial migration, so the guarantee is checked without a live database.
    """
    from core.models import Prediction

    offenders = [c.name for c in Prediction.__table__.columns if is_forbidden_key(c.name)]
    assert not offenders, (
        f"predictions gained market columns: {offenders}. Edge belongs on picks."
    )


def test_picks_table_is_where_market_data_lives():
    """The complement: the downstream join is expected to hold market fields."""
    from core.models import Pick

    names = {c.name for c in Pick.__table__.columns}
    assert {"line_at_pick", "price_at_pick", "edge"} <= names


@pytest.mark.parametrize("payload", [
    {"dk_spread": -3.5},
    {"market_total": 44.5},
    {"nested": {"implied_prob": 0.55}},
    {"list": [{"closing_line": -3}]},
    {"consensus_pick": "KC"},
    {"vegas": {"moneyline": -150}},
])
def test_runtime_guard_rejects_market_features(payload):
    with pytest.raises(MarketDataLeak):
        assert_market_free(payload)


def test_runtime_guard_allows_genuine_model_features():
    """
    Real NFL features must survive the filter, including the ones whose names
    collide with betting vocabulary ("offensive line", "goal line").
    """
    assert_market_free({
        "epa_off_pass": 0.14, "epa_def_rush": -0.06,
        "pass_block_win_rate": 0.62, "pressure_rate_allowed": 0.31,
        "offensive_line_rank": 7, "goal_line_carries": 4,
        "explosive_play_rate": 0.11, "red_zone_td_rate": 0.58,
        "rest_days": 10, "is_short_week": False, "travel_miles": 2100,
        "altitude_ft": 5280, "wind_mph": 18, "precip_prob": 0.4,
        "is_dome": False, "snap_share": 0.81, "route_participation": 0.74,
        "target_share": 0.27,
    })


def test_explanation_cannot_be_written_without_attribution():
    """
    Prose is generated from the attribution object, so a prediction that never
    produced one cannot acquire an explanation. This is what separates analysis
    from post-hoc rationalisation.
    """
    from core.predictions import AttributionError_, validate_attribution

    with pytest.raises(AttributionError_):
        validate_attribution({})
    with pytest.raises(AttributionError_):
        validate_attribution({"baseline": 0.0})               # no factors
    with pytest.raises(AttributionError_):
        validate_attribution({"baseline": 0.0, "factors": {"x": "a lot"}})

    validate_attribution({
        "baseline": -1.0,
        "factors": {"pass_rush_mismatch": 1.2, "wind_18mph": -3.1},
    })
