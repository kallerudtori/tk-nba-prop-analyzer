"""
Claude AI Service
Generates NBA game analysis write-ups using the Anthropic API.
Falls back to a rule-based analysis if ANTHROPIC_API_KEY is not set.
"""

import json
import logging
import os
from datetime import date as _date

logger = logging.getLogger(__name__)


def _is_playoffs() -> bool:
    """Returns True if today's date falls in NBA playoff season (mid-Apr through June)."""
    today = _date.today()
    return (today.month == 4 and today.day >= 13) or today.month in (5, 6)

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            return None
        try:
            import anthropic
            _client = anthropic.Anthropic(api_key=api_key)
        except ImportError:
            logger.warning("anthropic package not installed; using rule-based fallback")
            return None
    return _client


def generate_game_analysis(context: dict) -> dict:
    """
    Generate a betting write-up for a game.

    Args:
        context: dict with keys:
            home_team, away_team, game_time,
            moneyline {home_price, away_price, home_prob, away_prob},
            spread    {home_point, home_price, away_point, away_price},
            total     {point, over_price, under_price},
            home_opp_pts, away_opp_pts,   (defense stats)
            home_record, away_record,      (W-L strings)
            home_b2b, away_b2b             (back-to-back bools, optional)

    Returns:
        {"analysis": str, "pick": str, "confidence": "low"|"medium"|"high"}
    """
    client = _get_client()
    if client is None:
        return _rule_based_analysis(context)

    prompt = _build_prompt(context)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # Pull JSON out of the response
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])
            # Ensure new fields have defaults if Claude omits them
            result.setdefault("is_alternate", False)
            result.setdefault("alt_spread_comparison", None)
            return result
        return {"analysis": text, "pick": "—", "confidence": "low",
                "is_alternate": False, "alt_spread_comparison": None}
    except Exception as exc:
        logger.error("Claude API error: %s", exc)
        return _rule_based_analysis(context)


def generate_top_pick(games: list) -> dict:
    """
    Analyze all games and return the single best bet of the day.

    Args:
        games: list of context dicts (same shape as generate_game_analysis context),
               each also has 'home_team' and 'away_team' string keys.

    Returns:
        {"game": str, "pick": str, "confidence": str, "analysis": str}
    """
    client = _get_client()
    if client is None:
        return _rule_based_top_pick(games)

    # Condensed multi-game summary for Claude
    lines_text = ""
    for i, g in enumerate(games, 1):
        ml  = g.get("moneyline") or {}
        sp  = g.get("spread")    or {}
        tot = g.get("total")     or {}
        h_prob = round((ml.get("home_prob") or 0.5) * 100, 1)
        a_prob = round((ml.get("away_prob") or 0.5) * 100, 1)
        b2b_flags = []
        if g.get("home_b2b"):
            b2b_flags.append(f"{g['home_team']} B2B")
        if g.get("away_b2b"):
            b2b_flags.append(f"{g['away_team']} B2B")
        b2b_note = f"  💤 {', '.join(b2b_flags)}" if b2b_flags else ""

        # Alternate spreads summary for this game
        alt_text = ""
        alt_spreads = g.get("alternate_spreads") or []
        if alt_spreads:
            home = g["home_team"]
            away = g["away_team"]
            home_alts = [a for a in alt_spreads if a["team"] == home][:3]
            away_alts = [a for a in alt_spreads if a["team"] == away][:3]
            if home_alts or away_alts:
                alt_lines = []
                for a in home_alts:
                    alt_lines.append(f"{home} {_fmt_point(a['spread'])} ({_fmt_odds(a['odds'])})")
                for a in away_alts:
                    alt_lines.append(f"{away} {_fmt_point(a['spread'])} ({_fmt_odds(a['odds'])})")
                alt_text = f"   Alt spreads: {' | '.join(alt_lines)}\n"

        lines_text += (
            f"{i}. {g['away_team']} ({g.get('away_record','')}) @ "
            f"{g['home_team']} ({g.get('home_record','')})  {g.get('game_time','')}{b2b_note}\n"
            f"   ML: {g['away_team']} {_fmt_odds(ml.get('away_price'))} ({a_prob}%) | "
            f"{g['home_team']} {_fmt_odds(ml.get('home_price'))} ({h_prob}%)\n"
            f"   Spread: {g['away_team']} {_fmt_point(sp.get('away_point'))} | "
            f"{g['home_team']} {_fmt_point(sp.get('home_point'))}\n"
            f"   O/U: {tot.get('point','?')} | "
            f"Defense: {g['away_team']} allows {g.get('away_opp_pts','?')} pts/g, "
            f"{g['home_team']} allows {g.get('home_opp_pts','?')} pts/g\n"
            f"{alt_text}\n"
        )

    playoff_banner = (
        "🏆 PLAYOFF SLATE — Key adjustments: totals run 5-8 pts lower than regular season; "
        "defense is significantly tighter; elite closers outperform their regular season lines; "
        "role players underperform; pace slows considerably. Factor all of this into every pick.\n\n"
        if _is_playoffs() else ""
    )

    prompt = f"""You are a sharp NBA betting analyst writing in an engaging expert style. Here are today's games:

{playoff_banner}{lines_text}
STEP 1 — ALT SPREAD EVALUATION (do this for every game before picking):
- If a game has a main spread of 7+, check its alt spreads. If a line 3–5 pts tighter is available at -140 or better, that alt is the sharper play.
- If you like a team but the main spread feels like too much margin, look for an alt that gives 3+ pts of cushion at ≤ -150. If found, that's your pick.
- Annotate any game where an alt spread is the best play.

STEP 2 — Choose the SINGLE best bet across the slate, factoring in implied probability, defensive matchup, B2B fatigue, and line value (including alts from Step 1).

Write your analysis:
- Opening hook: what stands out about this matchup
- Key factors: pace, defense, fatigue, value vs. the number — explicitly note if an alt spread is the smarter line
- THE PICK with rationale — or "PASS" if the slate has no clear edge (name 1-2 props instead)

Use emojis (🏀 🎯 💤 ⚠️). Keep it punchy — 3-4 short paragraphs.

Respond ONLY with valid JSON:
{{
  "game": "Away Team @ Home Team (the matchup you chose, or 'Full Slate' if PASS)",
  "pick": "specific bet e.g. 'Boston Celtics -11.5' or 'Boston Celtics -8 (Alt)' or 'OKC Thunder ML' or 'PASS'",
  "confidence": "low|medium|high",
  "analysis": "your full multi-paragraph analysis with emojis and line breaks between paragraphs",
  "is_alternate": false,
  "alt_spread_comparison": null
}}

Set is_alternate to true if the pick is an alternate spread. Set alt_spread_comparison to a one-sentence comparison of main vs alternate (e.g. "Main line is Celtics -11.5 (-110); alternate Celtics -8 (-130) cuts margin risk while keeping solid value") or null if not recommending an alternate."""

    try:
        msg  = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        text  = msg.content[0].text.strip()
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])
            result.setdefault("is_alternate", False)
            result.setdefault("alt_spread_comparison", None)
            return result
        return {"game": "—", "pick": "—", "confidence": "low", "analysis": text,
                "is_alternate": False, "alt_spread_comparison": None}
    except Exception as exc:
        logger.error("Claude top-pick error: %s", exc)
        return _rule_based_top_pick(games)


def _rule_based_top_pick(games: list) -> dict:
    """Pick the game with the strongest implied edge (highest max win probability)."""
    best = None
    best_score = 0.0
    for g in games:
        ml = g.get("moneyline") or {}
        h  = ml.get("home_prob") or 0.5
        a  = ml.get("away_prob") or 0.5
        score = max(h, a)
        if score > best_score:
            best_score = score
            best = g

    if not best:
        return {"game": "—", "pick": "—", "confidence": "low", "analysis": ""}

    analysis = _rule_based_analysis(best)
    home = best["home_team"]
    away = best["away_team"]
    return {
        "game":       f"{away} @ {home}",
        "pick":       analysis["pick"],
        "confidence": analysis["confidence"],
        "analysis":   analysis["analysis"],
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_odds(price) -> str:
    if price is None:
        return "N/A"
    return f"+{price}" if price > 0 else str(price)


def _fmt_point(point) -> str:
    if point is None:
        return "N/A"
    return f"+{point}" if point > 0 else str(point)


def _build_prompt(ctx: dict) -> str:
    home = ctx["home_team"]
    away = ctx["away_team"]
    ml   = ctx.get("moneyline") or {}
    sp   = ctx.get("spread")    or {}
    tot  = ctx.get("total")     or {}

    home_ml   = _fmt_odds(ml.get("home_price"))
    away_ml   = _fmt_odds(ml.get("away_price"))
    home_sp   = _fmt_point(sp.get("home_point"))
    away_sp   = _fmt_point(sp.get("away_point"))
    home_sp_pr = _fmt_odds(sp.get("home_price", -110))
    away_sp_pr = _fmt_odds(sp.get("away_price", -110))
    total_pt  = tot.get("point", "N/A")
    over_pr   = _fmt_odds(tot.get("over_price",  -110))
    under_pr  = _fmt_odds(tot.get("under_price", -110))

    home_prob = round((ml.get("home_prob") or 0.5) * 100, 1)
    away_prob = round((ml.get("away_prob") or 0.5) * 100, 1)

    home_record = ctx.get("home_record", "")
    away_record = ctx.get("away_record", "")

    def_away = ctx.get("away_opp_pts", "N/A")
    def_home = ctx.get("home_opp_pts", "N/A")

    rec_home = f" ({home_record})" if home_record else ""
    rec_away = f" ({away_record})" if away_record else ""

    # Back-to-back flags
    b2b_parts = []
    if ctx.get("home_b2b"):
        b2b_parts.append(f"💤 {home} is on a back-to-back")
    if ctx.get("away_b2b"):
        b2b_parts.append(f"💤 {away} is on a back-to-back")
    b2b_line = "  |  ".join(b2b_parts) if b2b_parts else "No back-to-backs tonight"

    home_sp_val = sp.get("home_point", 0) or 0
    away_sp_val = sp.get("away_point", 0) or 0

    # Build alternate spreads section
    alt_spreads = ctx.get("alternate_spreads") or []
    if alt_spreads:
        home_alts = [a for a in alt_spreads if a["team"] == home][:4]
        away_alts = [a for a in alt_spreads if a["team"] == away][:4]
        alt_lines = []
        for a in home_alts:
            alt_lines.append(f"  {home} {_fmt_point(a['spread'])} ({_fmt_odds(a['odds'])})")
        for a in away_alts:
            alt_lines.append(f"  {away} {_fmt_point(a['spread'])} ({_fmt_odds(a['odds'])})")
        alt_section = "Alternate Spreads (DraftKings):\n" + "\n".join(alt_lines)
    else:
        alt_section = "Alternate Spreads: not available"

    playoff_note = (
        "🏆 PLAYOFF CONTEXT: This is an NBA Playoff game. Factor in: significantly tighter "
        "defense and slower pace than regular season; stars play heavier minutes; role players "
        "see reduced usage; totals run 5-8 pts lower than regular season averages; series "
        "history and matchup familiarity matter; back-to-back fatigue is not applicable "
        "(playoff schedules have rest days). Adjust projections DOWN for secondary scorers "
        "and UP for elite closers and primary ball-handlers.\n\n"
        if _is_playoffs() else ""
    )

    return f"""You are a sharp NBA betting analyst writing in an engaging, authoritative style.

{playoff_note}Game: {away}{rec_away} @ {home}{rec_home}
Tip-off: {ctx.get("game_time", "TBD")}
Fatigue: {b2b_line}

DraftKings Lines:
- Moneyline:  {away} {away_ml} ({away_prob}% implied)  |  {home} {home_ml} ({home_prob}% implied)
- Spread:     {away} {away_sp} ({away_sp_pr})  |  {home} {home_sp} ({home_sp_pr})
- Total:      O/U {total_pt}  —  Over {over_pr}  |  Under {under_pr}

{alt_section}

Defense (opp pts/g allowed — lower = tougher D):
- {away}: {def_away} pts/g allowed
- {home}: {def_home} pts/g allowed

ALT SPREAD EVALUATION — do this before picking:
1. If the main spread is 7 or larger, check whether a tighter alternate (3–5 pts closer to 0) is available at -140 or better odds. If yes, that alt is almost always the sharper play — recommend it.
2. If you like a team to win but the main spread feels too large (risky margin), scan the alt spreads for a line that gives 3+ pts of extra cushion at reasonable juice (≤ -150). If found, recommend the alt.
3. If the main spread is under 6, only recommend an alt if there's a specific reason the margin is uncertain (B2B, injury, pace mismatch).
4. If no alt spreads are listed above, stick with the main line or totals.

Write a sharp breakdown in this structure:

**Opening:** 1-2 sentences on what makes this matchup worth attention.

**Analysis:** 2-3 sentences on pace, defense, fatigue, and line value. If an alt spread is the better play, say why explicitly (e.g. "The -8.5 is risky margin-wise; the -5.5 at -125 gives essential cushion").

**🎯 THE PICK:** One specific bet. If recommending an alt, use "(Alt)" suffix — e.g. "{home} -4 (Alt)". OR "PASS — no clear edge" with 1-2 prop suggestions.

Use relevant emojis (🏀 🎯 💤 ⚠️ 🔥). Keep it punchy — 3 short paragraphs max. Separate paragraphs with \\n\\n.

Respond ONLY with a valid JSON object — no preamble, no markdown fences:
{{
  "analysis": "your full multi-paragraph analysis with emojis and \\n\\n between paragraphs",
  "pick": "specific bet e.g. '{home} {_fmt_point(home_sp_val)}' or '{away} ML' or 'Under {total_pt}' or '{home} -4 (Alt)' or 'PASS'",
  "confidence": "low|medium|high",
  "is_alternate": false,
  "alt_spread_comparison": null
}}

Set is_alternate to true only if the pick includes "(Alt)". Set alt_spread_comparison to a single sentence comparing the main spread to the recommended alternate (e.g. "Main line is {away} {away_sp} ({away_sp_pr}); alternate {home} -4 (-125) cuts the margin risk while keeping solid value") — or null if not recommending an alternate."""


def _rule_based_analysis(ctx: dict) -> dict:
    """Fallback analysis when Claude API is unavailable."""
    home = ctx["home_team"]
    away = ctx["away_team"]
    ml   = ctx.get("moneyline") or {}
    sp   = ctx.get("spread")    or {}
    tot  = ctx.get("total")     or {}

    home_prob = (ml.get("home_prob") or 0.5) * 100
    away_prob = (ml.get("away_prob") or 0.5) * 100
    home_sp   = sp.get("home_point", 0)
    away_sp   = sp.get("away_point", 0)
    total_pt  = tot.get("point", 0)
    def_home  = ctx.get("home_opp_pts")
    def_away  = ctx.get("away_opp_pts")

    def_note = ""
    if def_home and def_away:
        better_def = home if def_home < def_away else away
        def_note = f" {better_def} has the defensive edge, allowing fewer points per game."

    if home_prob >= 68:
        pick       = f"{home} {_fmt_point(home_sp)}"
        confidence = "high"
        analysis   = (
            f"{home} is a heavy {_fmt_odds(ml.get('home_price'))} favorite at home "
            f"({home_prob:.0f}% implied win probability).{def_note} "
            f"The spread of {_fmt_point(home_sp)} offers much better value than the ML. "
            f"Back {home} to cover."
        )
    elif away_prob >= 68:
        # Heavy away favorite — spread is better value than inflated ML price
        pick       = f"{away} {_fmt_point(away_sp)}"
        confidence = "high"
        analysis   = (
            f"{away} is a strong {_fmt_odds(ml.get('away_price'))} road favorite "
            f"({away_prob:.0f}% implied).{def_note} "
            f"The spread at {_fmt_point(away_sp)} offers far better value than the ML price. "
            f"Back {away} to cover."
        )
    elif away_prob >= 52:
        pick       = f"{away} ML"
        confidence = "medium"
        analysis   = (
            f"{away} enters as a road underdog at {_fmt_odds(ml.get('away_price'))} "
            f"({away_prob:.0f}% implied), which may undervalue them.{def_note} "
            f"The moneyline offers better risk/reward than the spread here. "
            f"Consider {away} ML as a value play."
        )
    else:
        pick       = f"Under {total_pt}"
        confidence = "low"
        analysis   = (
            f"This is a close matchup — {home} at {home_prob:.0f}% implied.{def_note} "
            f"Neither side offers a clear edge on the spread or moneyline. "
            f"If the defenses show up, the total at {total_pt} may be the play. "
            f"No high-conviction side bet."
        )

    return {"analysis": analysis, "pick": pick, "confidence": confidence,
            "is_alternate": False, "alt_spread_comparison": None}
