"""
Injury Minute-Redistribution Model (Feature 1)

Given a team's players (with minutes, usage%, position, and injury status),
redistribute the minutes & usage vacated by Out/Doubtful/Questionable players
to position-appropriate teammates.

Approach (per spec):
  • Out / Doubtful  → full vacancy (100% of their min+usg becomes available)
  • Questionable / GTD → partial (50%) — they might still play
  • Beneficiaries chosen by POSITION FIT first, then existing role (minutes)
  • Mismatched positions get little/no share
  • Minutes capped at a realistic per-player ceiling
  • Multiple injuries processed sequentially, caps applied cumulatively
"""

import logging

from services.injuries import OUT, DOUBTFUL, QUESTIONABLE, GTD, ACTIVE

logger = logging.getLogger(__name__)

# How much of a player's role is vacated by each status
VACANCY_BY_STATUS = {
    OUT:          1.00,
    DOUBTFUL:     1.00,
    QUESTIONABLE: 0.50,
    GTD:          0.50,
    ACTIVE:       0.00,
}

# Only fully-Active players absorb vacated minutes. Anyone with a non-Active
# status (Out/Doubtful/Questionable/GTD) has their own availability in doubt,
# so we don't also pile minutes onto them.
def _can_absorb(status: str) -> bool:
    return (status or ACTIVE) == ACTIVE

MINUTES_CEILING = 40.0   # realistic single-game max, even in playoffs
USG_CEILING     = 0.42   # ~42% usage is the practical ceiling for a focal star


def _position_axis(pos: str) -> float:
    """Map a position string to a 1-5 axis (PG=1 … C=5). Unknown → 3 (neutral)."""
    if not pos:
        return 3.0
    p = pos.strip().upper()
    table = {
        "PG": 1.0, "SG": 2.0, "SF": 3.0, "PF": 4.0, "C": 5.0,
        "G": 1.5, "F": 3.5,
        "G-F": 2.5, "F-G": 2.5, "F-C": 4.5, "C-F": 4.5,
        "PG-SG": 1.5, "SG-SF": 2.5, "SF-PF": 3.5, "PF-C": 4.5,
    }
    return table.get(p, 3.0)


def _position_fit(pos_a: str, pos_b: str) -> float:
    """Fit score in [0,1] between two positions by axis distance."""
    diff = abs(_position_axis(pos_a) - _position_axis(pos_b))
    if diff <= 0.5:
        return 1.0
    if diff <= 1.5:
        return 0.7
    if diff <= 2.5:
        return 0.35
    return 0.0   # clearly mismatched — do not redistribute


def redistribute(players: list) -> dict:
    """
    Args:
        players: list of dicts, each with:
            player_id, name, position (str), min (float),
            usg (float, 0-1), status (canonical enum)

    Returns:
        {
          "boosts": { player_id: {min_boost, usg_boost, absorbed_from[],
                                   reasons[], is_primary} },
          "out_players": [ {player_id, name, status, min, usg, position} ],
          "summary": str,
          "has_impact": bool,
        }
    """
    boosts: dict = {}
    out_players: list = []

    # Mutable running minutes so caps apply cumulatively across multiple outs
    running_min = {p["player_id"]: float(p.get("min", 0.0)) for p in players}

    # Injured players who vacate role, processed worst-first (Out before GTD)
    injured = [
        p for p in players
        if VACANCY_BY_STATUS.get(p.get("status", ACTIVE), 0.0) > 0.0
        and float(p.get("min", 0.0)) > 0
    ]
    injured.sort(key=lambda p: VACANCY_BY_STATUS.get(p["status"], 0.0), reverse=True)

    def _ensure(pid):
        if pid not in boosts:
            boosts[pid] = {
                "min_boost": 0.0, "usg_boost": 0.0,
                "absorbed_from": [], "reasons": [], "is_primary": False,
            }
        return boosts[pid]

    for inj in injured:
        vac = VACANCY_BY_STATUS.get(inj["status"], 0.0)
        avail_min = float(inj.get("min", 0.0)) * vac
        avail_usg = float(inj.get("usg", 0.0)) * vac
        if avail_min <= 0:
            continue

        out_players.append({
            "player_id": inj["player_id"],
            "name":      inj["name"],
            "status":    inj["status"],
            "min":       round(float(inj.get("min", 0.0)), 1),
            "usg":       round(float(inj.get("usg", 0.0)), 3),
            "position":  inj.get("position", ""),
        })

        # Eligible beneficiaries: anyone not Out/Doubtful and not the injured guy
        candidates = []
        for p in players:
            if p["player_id"] == inj["player_id"]:
                continue
            if not _can_absorb(p.get("status", ACTIVE)):
                continue
            fit = _position_fit(inj.get("position", ""), p.get("position", ""))
            if fit <= 0:
                continue
            # Score: position fit dominates, existing role (minutes) as multiplier
            role = 0.5 + (running_min.get(p["player_id"], 0.0) / 48.0)
            candidates.append((p, fit, fit * role))

        # Fallback: if every teammate is a positional mismatch, allow the
        # closest-fit non-sidelined players so the minutes don't vanish.
        if not candidates:
            for p in players:
                if p["player_id"] == inj["player_id"]:
                    continue
                if not _can_absorb(p.get("status", ACTIVE)):
                    continue
                role = 0.5 + (running_min.get(p["player_id"], 0.0) / 48.0)
                candidates.append((p, 0.2, 0.2 * role))

        if not candidates:
            continue

        candidates.sort(key=lambda c: c[2], reverse=True)
        total_score = sum(c[2] for c in candidates) or 1.0

        # Identify the single primary beneficiary (highest score)
        primary_pid = candidates[0][0]["player_id"]

        # Distribute proportional to score, respecting the minutes ceiling.
        # Two passes: first allocate, then redistribute any clipped overflow
        # to uncapped players.
        remaining_min = avail_min
        remaining_usg = avail_usg
        for _pass in range(2):
            if remaining_min <= 0.05:
                break
            open_cands = [
                c for c in candidates
                if running_min.get(c[0]["player_id"], 0.0) < MINUTES_CEILING
            ]
            if not open_cands:
                break
            open_score = sum(c[2] for c in open_cands) or 1.0
            pass_min = remaining_min
            pass_usg = remaining_usg
            remaining_min = 0.0
            remaining_usg = 0.0
            for p, fit, score in open_cands:
                pid = p["player_id"]
                share = score / open_score
                give_min = pass_min * share
                cur = running_min.get(pid, 0.0)
                allowed = max(0.0, MINUTES_CEILING - cur)
                clipped = min(give_min, allowed)
                overflow = give_min - clipped
                remaining_min += overflow

                give_usg = pass_usg * share
                b = _ensure(pid)
                b["min_boost"] += clipped
                b["usg_boost"] = min(USG_CEILING, b["usg_boost"] + give_usg)
                running_min[pid] = cur + clipped

                if clipped > 0.4 and not any(
                    a["player_id"] == inj["player_id"] for a in b["absorbed_from"]
                ):
                    b["absorbed_from"].append({
                        "player_id": inj["player_id"],
                        "name": inj["name"],
                        "min": round(clipped, 1),
                        "position": inj.get("position", ""),
                    })
                    pos_txt = f" at {inj.get('position')}" if inj.get("position") else ""
                    b["reasons"].append(
                        f"Absorbs ~{round(clipped)} min from {inj['name']}{pos_txt}"
                    )
                if pid == primary_pid:
                    b["is_primary"] = True

    # Round everything & drop negligible boosts
    cleaned = {}
    for pid, b in boosts.items():
        if b["min_boost"] < 0.5 and b["usg_boost"] < 0.01:
            continue
        b["min_boost"] = round(b["min_boost"], 1)
        b["usg_boost"] = round(b["usg_boost"], 3)
        cleaned[pid] = b

    summary = _build_summary(out_players, cleaned, players)
    return {
        "boosts": cleaned,
        "out_players": out_players,
        "summary": summary,
        "has_impact": bool(out_players),
    }


def _build_summary(out_players: list, boosts: dict, players: list) -> str:
    if not out_players:
        return "No injury impact — full strength expected."
    name_by_id = {p["player_id"]: p["name"] for p in players}
    outs = ", ".join(
        f"{o['name']} ({o['status']})" for o in out_players
    )
    # Top 3 beneficiaries by minutes absorbed
    top = sorted(boosts.items(), key=lambda kv: kv[1]["min_boost"], reverse=True)[:3]
    if top:
        bens = "; ".join(
            f"{name_by_id.get(pid, 'Player')} +{b['min_boost']} min"
            + (f", +{round(b['usg_boost']*100,1)}% usg" if b["usg_boost"] >= 0.01 else "")
            for pid, b in top
        )
        return f"Out/limited: {outs}. Projected beneficiaries — {bens}."
    return f"Out/limited: {outs}. No clear positional beneficiary."
