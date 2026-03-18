"""
Claude AI Service
Generates NBA game analysis write-ups using the Anthropic API.
Falls back to a rule-based analysis if ANTHROPIC_API_KEY is not set.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

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
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # Pull JSON out of the response
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
        return {"analysis": text, "pick": "—", "confidence": "low"}
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
        lines_text += (
            f"{i}. {g['away_team']} ({g.get('away_record','')}) @ "
            f"{g['home_team']} ({g.get('home_record','')})  {g.get('game_time','')}{b2b_note}\n"
            f"   ML: {g['away_team']} {_fmt_odds(ml.get('away_price'))} ({a_prob}%) | "
            f"{g['home_team']} {_fmt_odds(ml.get('home_price'))} ({h_prob}%)\n"
            f"   Spread: {g['away_team']} {_fmt_point(sp.get('away_point'))} | "
            f"{g['home_team']} {_fmt_point(sp.get('home_point'))}\n"
            f"   O/U: {tot.get('point','?')} | "
            f"Defense: {g['away_team']} allows {g.get('away_opp_pts','?')} pts/g, "
            f"{g['home_team']} allows {g.get('home_opp_pts','?')} pts/g\n\n"
        )

    prompt = f"""You are a sharp NBA betting analyst writing in an engaging expert style. Here are today's games:

{lines_text}Scan the full slate and choose the SINGLE best bet — the one with the clearest edge considering implied probability, defensive matchup, back-to-back fatigue, and line value. If no game has a strong edge, say so.

Write your analysis in this style:
- Opening hook: what stands out about this matchup or the slate overall
- Key factors: pace, defense, fatigue (B2B), value vs. the number
- THE PICK with a clear rationale — or "PASS" if the slate is chalk with no value
- If PASS, name 1-2 player props you'd target instead

Use a few relevant emojis (🏀 🎯 💤 ⚠️). Keep it punchy — 3-4 short paragraphs.

Respond ONLY with valid JSON:
{{
  "game": "Away Team @ Home Team (the matchup you chose, or 'Full Slate' if PASS)",
  "pick": "specific bet e.g. 'Boston Celtics -11.5' or 'OKC Thunder ML' or 'Under 213.5' or 'PASS'",
  "confidence": "low|medium|high",
  "analysis": "your full multi-paragraph analysis with emojis and line breaks between paragraphs"
}}"""

    try:
        msg  = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text  = msg.content[0].text.strip()
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
        return {"game": "—", "pick": "—", "confidence": "low", "analysis": text}
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

    return f"""You are a sharp NBA betting analyst writing in an engaging, authoritative style.

Game: {away}{rec_away} @ {home}{rec_home}
Tip-off: {ctx.get("game_time", "TBD")}
Fatigue: {b2b_line}

DraftKings Lines:
- Moneyline:  {away} {away_ml} ({away_prob}% implied)  |  {home} {home_ml} ({home_prob}% implied)
- Spread:     {away} {away_sp} ({away_sp_pr})  |  {home} {home_sp} ({home_sp_pr})
- Total:      O/U {total_pt}  —  Over {over_pr}  |  Under {under_pr}

Defense (opp pts/g allowed — lower = tougher D):
- {away}: {def_away} pts/g allowed
- {home}: {def_home} pts/g allowed

Write a sharp betting breakdown in this structure:

**Opening:** 1-2 sentences on what makes this matchup worth attention — back-to-back situations, a notable line, or a key stylistic mismatch.

**Analysis:** 2-3 sentences on the key factors: pace, defense, fatigue impact, home/road splits, and whether the implied probability reflects true edge.

**🎯 THE PICK:** Give a specific bet recommendation — e.g. "{home} {_fmt_point(home_sp_val)}" or "{away} ML" or "Under {total_pt}" — with a 1-sentence rationale. OR, if the line is too tight/vig too high with no clear edge, write "PASS — no clear edge" and suggest 1-2 player props to target instead (e.g. "Target [Player] points over X.X").

Use relevant emojis (🏀 🎯 💤 ⚠️ 🔥). Keep it punchy — 3 short paragraphs max. Separate paragraphs with \\n\\n.

Respond ONLY with a valid JSON object — no preamble, no markdown fences:
{{
  "analysis": "your full multi-paragraph analysis with emojis and \\n\\n between paragraphs",
  "pick": "specific bet e.g. '{home} {_fmt_point(home_sp_val)}' or '{away} ML' or 'Under {total_pt}' or 'PASS'",
  "confidence": "low|medium|high"
}}"""


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

    return {"analysis": analysis, "pick": pick, "confidence": confidence}
