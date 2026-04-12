"""
NBA Prop Analyzer — Flask Application
"""

import logging
import os

from flask import Flask, jsonify, render_template, request
from flask_caching import Cache
from dotenv import load_dotenv

load_dotenv()

# ── App & Cache ───────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["CACHE_TYPE"] = "SimpleCache"
app.config["CACHE_DEFAULT_TIMEOUT"] = 3600
app.config["SERVER_NAME"] = None  # don't restrict Host header

cache = Cache(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Services & Model (lazy-imported so Flask app object exists first) ─────────
from services.nba_stats import NBAStatsService
from services.odds import OddsService
from models.projection import ProjectionModel
from database.db import init_db

nba_svc = NBAStatsService(cache)
odds_svc = OddsService(cache)
model = ProjectionModel()

init_db()


# ── Global error handler (always return JSON, never HTML) ────────────────────
@app.errorhandler(Exception)
def handle_any_exception(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e  # Let Flask handle 404/405/etc normally
    logger.error("Unhandled exception: %s", e, exc_info=True)
    return jsonify({"success": False, "error": str(e)}), 500


# ── Views ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/lines")
def game_lines_page():
    return render_template("lines.html")


# ── Health / Quota ────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    from datetime import date as dt_date
    quota = odds_svc.get_quota()
    # Calculate days until the Odds API monthly quota resets (1st of next month)
    today = dt_date.today()
    if today.month == 12:
        reset = dt_date(today.year + 1, 1, 1)
    else:
        reset = dt_date(today.year, today.month + 1, 1)
    return jsonify({
        "status": "ok",
        "odds_quota": quota,
        "quota_reset_days": (reset - today).days,
        "quota_reset_date": reset.isoformat(),
    })


# ── Games ─────────────────────────────────────────────────────────────────────

@app.route("/api/games/today")
def today_games():
    day_offset = request.args.get("day_offset", 0, type=int)
    try:
        games = nba_svc.get_games(day_offset=day_offset)
        events = odds_svc.get_nba_events(day_offset=day_offset)
        for game in games:
            game["odds_event_id"] = odds_svc.match_game_to_event(
                game["home_team"]["name"],
                game["away_team"]["name"],
                events,
            )
        return jsonify({"success": True, "games": games, "day_offset": day_offset})
    except Exception as exc:
        logger.error("today_games error: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


# ── Rosters ───────────────────────────────────────────────────────────────────

@app.route("/api/team/<int:team_id>/roster")
def team_roster(team_id):
    try:
        roster = nba_svc.get_team_roster(team_id)
        return jsonify({"success": True, "roster": roster})
    except Exception as exc:
        logger.error("team_roster error: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


# ── Player Stats (raw) ────────────────────────────────────────────────────────

@app.route("/api/player/<int:player_id>/stats")
def player_stats(player_id):
    try:
        stats = nba_svc.get_player_stats(player_id)
        return jsonify({"success": True, "stats": stats})
    except Exception as exc:
        logger.error("player_stats error: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


# ── Full Analysis (stats + projection + odds) ─────────────────────────────────

@app.route("/api/player/<int:player_id>/analysis")
def player_analysis(player_id):
    """
    Query params:
      opponent_team_id  (int)   – opponent's team ID for defense adjustment
      is_home           (bool)  – 'true' / 'false'
      odds_event_id     (str)   – The Odds API event ID for this game
    """
    opponent_team_id = request.args.get("opponent_team_id", type=int)
    is_home = request.args.get("is_home", "false").lower() == "true"
    odds_event_id = request.args.get("odds_event_id", "").strip()

    try:
        # ── Stats ──────────────────────────────────────────────────────
        stats = nba_svc.get_player_stats(player_id)

        # ── Defense context ────────────────────────────────────────────
        opp_defense = (
            nba_svc.get_team_defense_stats(opponent_team_id)
            if opponent_team_id
            else None
        )
        league_avg = nba_svc.get_league_avg_defense()

        # ── Projections ────────────────────────────────────────────────
        projections = model.calculate_projection(stats, opp_defense, league_avg, is_home)

        # ── Odds ───────────────────────────────────────────────────────
        odds_data: dict | None = None
        has_odds = False
        if odds_event_id:
            odds_data = odds_svc.get_player_props(odds_event_id, stats["name"])
            has_odds = bool(odds_data)

        # ── Combine ────────────────────────────────────────────────────
        props = {}
        for prop_key in ("points", "rebounds", "assists", "pra", "threes"):
            proj = dict(projections[prop_key])  # copy

            if has_odds and odds_data and prop_key in odds_data:
                od = odds_data[prop_key]
                line = od["line"]
                edge = round(proj["projection"] - line, 1)

                proj.update(
                    {
                        "has_odds": True,
                        "line": line,
                        "over_odds": od["over_odds"],
                        "under_odds": od["under_odds"],
                        "edge": edge,
                        "value_label": model.calculate_value_label(edge),
                        "implied_prob_over": od["implied_prob_over"],
                        "implied_prob_under": od["implied_prob_under"],
                        "model_prob_over": model.calculate_model_probability(
                            proj["projection"], proj["std_dev"], line
                        ),
                    }
                )
            else:
                proj.update(
                    {
                        "has_odds": False,
                        "line": None,
                        "over_odds": None,
                        "under_odds": None,
                        "edge": None,
                        "value_label": "No Odds",
                        "implied_prob_over": None,
                        "implied_prob_under": None,
                        "model_prob_over": None,
                    }
                )

            props[prop_key] = proj

        analysis = {
            "player": {
                "id": player_id,
                "name": stats["name"],
                "team_id": stats.get("team_id"),
                "games_played": stats["games_played"],
                "jersey_number": stats.get("jersey_number", ""),
                "headshot_url": f"https://cdn.nba.com/headshots/nba/latest/260x190/{player_id}.png",
                "is_back_to_back": stats.get("is_back_to_back", False),
            },
            "stats": {
                "season_avg": stats["season_avg"],
                "last_5_avg": stats["last_5_avg"],
                "last_10_avg": stats["last_10_avg"],
                "home_avg": stats["home_avg"],
                "away_avg": stats["away_avg"],
                "std_devs": stats["std_devs"],
            },
            "chart_data": {
                "last_10_games": stats["last_10_games"],
                "season_games": stats["season_games"],
            },
            "props": props,
            "has_odds": has_odds,
            "opponent_team_id": opponent_team_id,
            "is_home": is_home,
        }

        return jsonify({"success": True, "analysis": analysis})

    except Exception as exc:
        logger.error("player_analysis error player=%s: %s", player_id, exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


# ── Game Lines dashboard ──────────────────────────────────────────────────────

@app.route("/api/games/lines")
def api_games_lines():
    """All games for the day with moneyline / spread / total and defense context."""
    day_offset = request.args.get("day_offset", 0, type=int)
    try:
        games   = nba_svc.get_games(day_offset=day_offset)
        events  = odds_svc.get_nba_events(day_offset=day_offset)
        all_lines = odds_svc.get_all_game_lines(day_offset=day_offset)

        all_defense   = nba_svc._get_league_team_stats()
        defense_by_id = {d["team_id"]: d for d in all_defense}

        result = []
        for game in games:
            eid  = odds_svc.match_game_to_event(
                game["home_team"]["name"], game["away_team"]["name"], events
            )
            lines    = all_lines.get(eid) if eid else None
            home_def = defense_by_id.get(game["home_team"]["id"], {})
            away_def = defense_by_id.get(game["away_team"]["id"], {})

            result.append({
                **game,
                "odds_event_id": eid,
                "lines": lines,
                "home_opp_pts": round(home_def.get("opp_pts", 0), 1) if home_def else None,
                "away_opp_pts": round(away_def.get("opp_pts", 0), 1) if away_def else None,
            })

        return jsonify({"success": True, "games": result, "day_offset": day_offset})
    except Exception as exc:
        logger.error("api_games_lines error: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/debug/lines")
def api_debug_lines():
    """Debug: show raw Odds API response and matching results."""
    day_offset = request.args.get("day_offset", 0, type=int)
    try:
        events    = odds_svc.get_nba_events(day_offset=day_offset)
        all_lines = odds_svc.get_all_game_lines(day_offset=day_offset)
        games     = nba_svc.get_games(day_offset=day_offset)
        matches   = []
        for g in games:
            eid = odds_svc.match_game_to_event(
                g["home_team"]["name"], g["away_team"]["name"], events
            )
            matches.append({
                "nba_home": g["home_team"]["name"],
                "nba_away": g["away_team"]["name"],
                "matched_event_id": eid,
                "has_lines": eid in all_lines if eid else False,
            })
        return jsonify({
            "quota": odds_svc.get_quota(),
            "events_count": len(events),
            "lines_count": len(all_lines),
            "games_count": len(games),
            "matches": matches,
        })
    except Exception as exc:
        logger.error("api_debug_lines error: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc), "quota": odds_svc.get_quota()}), 500


@app.route("/api/debug/props")
def api_debug_props():
    """Debug: check if player prop markets are accessible for the first available game."""
    try:
        events = odds_svc.get_nba_events(day_offset=0)
        if not events:
            return jsonify({"error": "no events found"})
        event_id = events[0]["id"]
        game_label = f"{events[0].get('away_team')} @ {events[0].get('home_team')}"
        import requests as _req
        from services.odds import ODDS_API_BASE, MARKETS_PARAM
        resp = _req.get(
            f"{ODDS_API_BASE}/sports/basketball_nba/events/{event_id}/odds",
            params={"apiKey": odds_svc.api_key, "regions": "us",
                    "markets": MARKETS_PARAM, "oddsFormat": "american", "bookmakers": "draftkings"},
            timeout=10,
        )
        data = resp.json()
        if resp.status_code != 200:
            return jsonify({"error": f"HTTP {resp.status_code}", "detail": data})
        bms = data.get("bookmakers", [])
        dk = next((b for b in bms if b["key"] == "draftkings"), None)
        if not dk:
            return jsonify({"game": game_label, "event_id": event_id,
                            "error": "no draftkings bookmaker", "bookmakers": [b["key"] for b in bms]})
        markets = [m["key"] for m in dk.get("markets", [])]
        sample = dk["markets"][0]["outcomes"][:2] if dk.get("markets") else []
        return jsonify({"game": game_label, "event_id": event_id,
                        "dk_markets_available": markets, "sample_outcomes": sample})
    except Exception as exc:
        return jsonify({"error": str(exc)})


@app.route("/api/game/<event_id>/analysis")
def api_game_analysis(event_id):
    """Generate a Claude write-up for a specific game (cached 1 h)."""
    day_offset = request.args.get("day_offset", 0, type=int)

    cache_key = f"game_analysis_{event_id}"
    cached = cache.get(cache_key)
    if cached:
        return jsonify({"success": True, **cached})

    try:
        from services.claude_ai import generate_game_analysis

        all_lines = odds_svc.get_all_game_lines(day_offset=day_offset)
        lines     = all_lines.get(event_id)
        if not lines:
            return jsonify({"success": False, "error": "No lines found for this event"}), 404

        games   = nba_svc.get_games(day_offset=day_offset)
        events  = odds_svc.get_nba_events(day_offset=day_offset)
        game    = next(
            (g for g in games
             if odds_svc.match_game_to_event(
                 g["home_team"]["name"], g["away_team"]["name"], events
             ) == event_id),
            None,
        )

        all_defense   = nba_svc._get_league_team_stats()
        defense_by_id = {d["team_id"]: d for d in all_defense}

        home_opp_pts = away_opp_pts = None
        home_record  = away_record  = ""
        game_time    = "TBD"
        home_b2b     = away_b2b = False

        if game:
            h_def = defense_by_id.get(game["home_team"]["id"], {})
            a_def = defense_by_id.get(game["away_team"]["id"], {})
            home_opp_pts = round(h_def.get("opp_pts", 0), 1) if h_def else None
            away_opp_pts = round(a_def.get("opp_pts", 0), 1) if a_def else None
            home_record  = (
                f"{game['home_team'].get('wins','?')}-{game['home_team'].get('losses','?')}"
            )
            away_record  = (
                f"{game['away_team'].get('wins','?')}-{game['away_team'].get('losses','?')}"
            )
            game_time = game.get("game_time", "TBD")

            # B2B detection: check if either team played yesterday
            try:
                yesterday_games = nba_svc.get_games(day_offset=day_offset - 1)
                yesterday_ids = {
                    tid
                    for yg in yesterday_games
                    for tid in (yg["home_team"]["id"], yg["away_team"]["id"])
                }
                home_b2b = game["home_team"]["id"] in yesterday_ids
                away_b2b = game["away_team"]["id"] in yesterday_ids
            except Exception:
                pass

        context = {
            **lines,
            "game_time":        game_time,
            "home_opp_pts":     home_opp_pts,
            "away_opp_pts":     away_opp_pts,
            "home_record":      home_record,
            "away_record":      away_record,
            "home_b2b":         home_b2b,
            "away_b2b":         away_b2b,
            "alternate_spreads": odds_svc.get_alternate_spreads(event_id),
        }

        alt_spreads = context.get("alternate_spreads", [])
        analysis = generate_game_analysis(context)
        payload = {**analysis, "alternate_spreads": alt_spreads}
        cache.set(cache_key, payload, timeout=3600)
        return jsonify({"success": True, **payload})

    except Exception as exc:
        logger.error("api_game_analysis error event=%s: %s", event_id, exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


# ── Top Pick ──────────────────────────────────────────────────────────────────

@app.route("/api/games/top-pick")
def api_games_top_pick():
    """Return the single best bet across today's slate (cached 1 h)."""
    day_offset = request.args.get("day_offset", 0, type=int)
    from datetime import datetime as _dt
    import pytz as _pytz
    _today = _dt.now(_pytz.timezone("America/Denver")).date()
    cache_key = f"top_pick_{day_offset}_{_today.isoformat()}"
    cached = cache.get(cache_key)
    if cached:
        return jsonify({"success": True, **cached})

    try:
        from services.claude_ai import generate_top_pick

        games    = nba_svc.get_games(day_offset=day_offset)
        events   = odds_svc.get_nba_events(day_offset=day_offset)
        all_lines = odds_svc.get_all_game_lines(day_offset=day_offset)
        all_defense   = nba_svc._get_league_team_stats()
        defense_by_id = {d["team_id"]: d for d in all_defense}

        # B2B detection for the full slate
        yesterday_ids: set = set()
        try:
            yesterday_games = nba_svc.get_games(day_offset=day_offset - 1)
            yesterday_ids = {
                tid
                for yg in yesterday_games
                for tid in (yg["home_team"]["id"], yg["away_team"]["id"])
            }
        except Exception:
            pass

        games_ctx = []
        for game in games:
            eid = odds_svc.match_game_to_event(
                game["home_team"]["name"], game["away_team"]["name"], events
            )
            lines = all_lines.get(eid) if eid else None
            if not lines:
                continue
            h_def = defense_by_id.get(game["home_team"]["id"], {})
            a_def = defense_by_id.get(game["away_team"]["id"], {})
            games_ctx.append({
                **lines,
                "game_time":        game.get("game_time", "TBD"),
                "home_opp_pts":     round(h_def.get("opp_pts", 0), 1) if h_def else None,
                "away_opp_pts":     round(a_def.get("opp_pts", 0), 1) if a_def else None,
                "home_record":      f"{game['home_team'].get('wins','?')}-{game['home_team'].get('losses','?')}",
                "away_record":      f"{game['away_team'].get('wins','?')}-{game['away_team'].get('losses','?')}",
                "home_b2b":         game["home_team"]["id"] in yesterday_ids,
                "away_b2b":         game["away_team"]["id"] in yesterday_ids,
                "alternate_spreads": odds_svc.get_alternate_spreads(eid) if eid else [],
            })

        if not games_ctx:
            return jsonify({"success": False, "error": "No games with lines"}), 404

        result = generate_top_pick(games_ctx)
        cache.set(cache_key, result, timeout=3600)
        return jsonify({"success": True, **result})

    except Exception as exc:
        logger.error("api_games_top_pick error: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


# ── Odds debug ────────────────────────────────────────────────────────────────

@app.route("/api/debug/odds")
def debug_odds():
    """Diagnostic endpoint — exposes Odds API status, event matching, and key presence."""
    result = {
        "has_api_key": bool(odds_svc.api_key),
        "api_key_prefix": odds_svc.api_key[:6] + "..." if odds_svc.api_key else None,
        "events": [],
        "error": None,
    }
    try:
        events = odds_svc.get_nba_events(0)
        result["events_count"] = len(events)

        games = nba_svc.get_games(day_offset=0)
        matched = 0
        for game in games:
            eid = odds_svc.match_game_to_event(
                game["home_team"]["name"], game["away_team"]["name"], events
            )
            result["events"].append({
                "game": f"{game['away_team']['name']} @ {game['home_team']['name']}",
                "odds_event_id": eid,
                "matched": eid is not None,
            })
            if eid:
                matched += 1

        result["games_matched"] = matched
        result["games_total"] = len(games)
    except Exception as exc:
        result["error"] = str(exc)

    return jsonify(result)


# ── Odds refresh ──────────────────────────────────────────────────────────────

@app.route("/api/odds/refresh", methods=["POST"])
def refresh_odds():
    event_id = request.json.get("event_id") if request.is_json else None
    odds_svc.clear_cache(event_id)
    return jsonify({"success": True, "message": "Odds cache cleared"})


# ── Bets views ────────────────────────────────────────────────────────────────

@app.route("/settle")
def settle_page():
    return render_template("settle.html")


# ── Bets API ──────────────────────────────────────────────────────────────────

@app.route("/api/bets", methods=["POST"])
def create_bet():
    from database.db import get_conn
    data = request.get_json(force=True)
    required = ("bet_type", "pick_label", "game_date")
    for field in required:
        if not data.get(field):
            return jsonify({"success": False, "error": f"Missing field: {field}"}), 400
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO bets
          (bet_type, player_name, prop_type, line, over_under, odds,
           model_projection, model_edge, model_confidence, model_prob_over,
           game_label, game_date, pick_label)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        data.get("bet_type", "prop"),
        data.get("player_name"),
        data.get("prop_type"),
        data.get("line"),
        data.get("over_under"),
        data.get("odds"),
        data.get("model_projection"),
        data.get("model_edge"),
        data.get("model_confidence"),
        data.get("model_prob_over"),
        data.get("game_label"),
        data.get("game_date"),
        data.get("pick_label"),
    ))
    conn.commit()
    bet_id = cur.lastrowid
    conn.close()
    return jsonify({"success": True, "id": bet_id}), 201


@app.route("/api/bets", methods=["GET"])
def list_bets():
    from database.db import get_conn
    status   = request.args.get("status")
    bet_type = request.args.get("bet_type")

    conn  = get_conn()
    query = "SELECT * FROM bets WHERE 1=1"
    params: list = []
    if status:
        query += " AND status = ?"; params.append(status)
    if bet_type:
        query += " AND bet_type = ?"; params.append(bet_type)
    query += " ORDER BY created_at DESC"

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify({"success": True, "bets": [dict(r) for r in rows]})


@app.route("/api/bets/<int:bet_id>/settle", methods=["PATCH"])
def settle_bet(bet_id):
    from database.db import get_conn
    data = request.get_json(force=True)

    conn = get_conn()
    row  = conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"success": False, "error": "Bet not found"}), 404

    # Determine status
    explicit_status = data.get("status")
    actual_value    = data.get("actual_value")

    if explicit_status in ("won", "lost", "void"):
        status = explicit_status
    elif actual_value is not None and row["line"] is not None and row["over_under"]:
        ou = row["over_under"].lower()
        if ou == "over":
            status = "won" if float(actual_value) > float(row["line"]) else "lost"
        elif ou == "under":
            status = "won" if float(actual_value) < float(row["line"]) else "lost"
        else:
            status = explicit_status or "pending"
    else:
        conn.close()
        return jsonify({"success": False, "error": "Provide actual_value or explicit status"}), 400

    conn.execute(
        "UPDATE bets SET status=?, actual_value=?, settled_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, actual_value, bet_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "status": status})


@app.route("/api/bets/<int:bet_id>", methods=["DELETE"])
def delete_bet(bet_id):
    from database.db import get_conn
    conn = get_conn()
    conn.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/bets/accuracy")
def bets_accuracy():
    from database.db import get_conn
    conn  = get_conn()
    rows  = conn.execute(
        "SELECT * FROM bets WHERE status IN ('won','lost')"
    ).fetchall()
    conn.close()

    settled = [dict(r) for r in rows]
    total   = len(settled)

    if total == 0:
        return jsonify({"success": True, "total": 0, "hit_rate": None,
                        "by_prop": {}, "by_confidence": {}, "edge_calibration": [],
                        "bias_by_prop": {}, "rolling_hit_rate": []})

    wins = sum(1 for r in settled if r["status"] == "won")
    hit_rate = round(wins / total * 100, 1)

    # By prop type (prop bets only)
    from collections import defaultdict
    by_prop: dict = defaultdict(lambda: {"wins": 0, "total": 0})
    by_conf: dict = defaultdict(lambda: {"wins": 0, "total": 0})
    edge_cal: list = []
    bias_by_prop: dict = defaultdict(list)

    for r in settled:
        hit = 1 if r["status"] == "won" else 0
        if r["prop_type"]:
            by_prop[r["prop_type"]]["wins"]  += hit
            by_prop[r["prop_type"]]["total"] += 1
        if r["model_confidence"]:
            by_conf[r["model_confidence"]]["wins"]  += hit
            by_conf[r["model_confidence"]]["total"] += 1
        if r["model_edge"] is not None:
            edge_cal.append({"edge": r["model_edge"], "hit": hit})
        if r["prop_type"] and r["model_projection"] is not None and r["actual_value"] is not None:
            bias_by_prop[r["prop_type"]].append(r["model_projection"] - r["actual_value"])

    prop_hr = {k: round(v["wins"] / v["total"] * 100, 1) for k, v in by_prop.items()}
    conf_hr = {k: round(v["wins"] / v["total"] * 100, 1) for k, v in by_conf.items()}
    bias    = {k: round(sum(v) / len(v), 2) for k, v in bias_by_prop.items()}

    # Rolling 10-bet hit rate (chronological order)
    chron   = sorted(settled, key=lambda r: r["settled_at"] or "")
    rolling: list = []
    for i in range(len(chron)):
        window = chron[max(0, i - 9): i + 1]
        rate   = sum(1 for x in window if x["status"] == "won") / len(window) * 100
        rolling.append({"index": i + 1, "rate": round(rate, 1)})

    return jsonify({
        "success":        True,
        "total":          total,
        "hit_rate":       hit_rate,
        "by_prop":        prop_hr,
        "by_confidence":  conf_hr,
        "edge_calibration": edge_cal,
        "bias_by_prop":   bias,
        "rolling_hit_rate": rolling,
    })


# ── Model weights ─────────────────────────────────────────────────────────────

@app.route("/api/model/weights")
def get_model_weights():
    import json, os
    cfg_path = os.path.join(os.path.dirname(__file__), "models", "weights_config.json")
    defaults = {"W_L5": 0.40, "W_L10": 0.35, "W_SEASON": 0.25,
                "OPP_CAP": 0.15, "OPP_CAP_3PM": 0.20, "SPLIT_CAP": 0.10}
    try:
        with open(cfg_path) as f:
            weights = json.load(f)
    except Exception:
        weights = defaults
    return jsonify({"success": True, "weights": weights})


@app.route("/api/model/optimize", methods=["POST"])
def optimize_model():
    try:
        from models.optimizer import run_optimizer
        result = run_optimizer()
        return jsonify({"success": True, **result})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as exc:
        logger.error("optimize error: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # preview_start injects PORT; fall back to FLASK_PORT then 5001
    port = int(os.getenv("PORT", os.getenv("FLASK_PORT", 5001)))
    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=port)
