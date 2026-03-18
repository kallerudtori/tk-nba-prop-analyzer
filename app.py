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

nba_svc = NBAStatsService(cache)
odds_svc = OddsService(cache)
model = ProjectionModel()


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

        context = {
            **lines,
            "game_time":     game_time,
            "home_opp_pts":  home_opp_pts,
            "away_opp_pts":  away_opp_pts,
            "home_record":   home_record,
            "away_record":   away_record,
        }

        analysis = generate_game_analysis(context)
        cache.set(cache_key, analysis, timeout=3600)
        return jsonify({"success": True, **analysis})

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
    _today = _dt.now(_pytz.timezone("America/New_York")).date()
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
                "game_time":    game.get("game_time", "TBD"),
                "home_opp_pts": round(h_def.get("opp_pts", 0), 1) if h_def else None,
                "away_opp_pts": round(a_def.get("opp_pts", 0), 1) if a_def else None,
                "home_record":  f"{game['home_team'].get('wins','?')}-{game['home_team'].get('losses','?')}",
                "away_record":  f"{game['away_team'].get('wins','?')}-{game['away_team'].get('losses','?')}",
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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # preview_start injects PORT; fall back to FLASK_PORT then 5001
    port = int(os.getenv("PORT", os.getenv("FLASK_PORT", 5001)))
    app.run(debug=True, host="0.0.0.0", port=port)
