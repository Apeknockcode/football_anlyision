"""Minimal tactical/review logic: dangerous attacks, key moments, who dominated."""
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from etl.schema import get_engine


def compute_tactical_summary(engine: Engine | None, match_id: int) -> dict[str, Any]:
    """
    Heuristic tactical summary: dangerous attacks (shots in box), key events list,
    who had more pressure (passes under_pressure), dominance proxy.
    """
    eng = engine or get_engine()
    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT home_team_id, away_team_id FROM matches WHERE id = :mid"),
            {"mid": match_id},
        ).fetchone()
        if not row:
            return {}
        home_team_id, away_team_id = row[0], row[1]

        events = conn.execute(
            text("""
                SELECT event_type, team_id, team_name, player_name, minute, second,
                       location_x, location_y, outcome, under_pressure
                FROM events WHERE match_id = :mid
                ORDER BY minute, second
            """),
            {"mid": match_id},
        ).fetchall()

    shots_danger = []
    key_moments = []
    home_pressure_events = 0
    away_pressure_events = 0
    home_attacks = 0
    away_attacks = 0

    for ev in events:
        event_type, team_id, team_name, player_name, minute, second, loc_x, loc_y, outcome, under_pressure = ev
        player_name = player_name or "Unknown"
        if event_type == "Shot":
            # Danger: shot in box (x > 83 approx in StatsBomb 100x100)
            in_box = loc_x is not None and loc_x >= 83
            shots_danger.append({
                "team_name": team_name,
                "player_name": player_name,
                "minute": minute,
                "second": second,
                "in_box": in_box,
                "outcome": outcome,
            })
            key_moments.append(f"{minute}' {player_name} ({team_name}) 射门 - {outcome or '未果'}")
            if team_id == home_team_id:
                home_attacks += 1
            else:
                away_attacks += 1
        elif event_type == "Goal" or (event_type == "Shot" and outcome == "Goal"):
            key_moments.append(f"{minute}' 进球: {player_name} ({team_name})")
        if under_pressure:
            if team_id == home_team_id:
                home_pressure_events += 1
            else:
                away_pressure_events += 1

    # Dominance: more shots + more pressure resistance (fewer under_pressure when in possession proxy)
    dominance = "balanced"
    if home_attacks > away_attacks + 2 and home_pressure_events <= away_pressure_events:
        dominance = "home"
    elif away_attacks > home_attacks + 2 and away_pressure_events <= home_pressure_events:
        dominance = "away"

    return {
        "match_id": match_id,
        "shots_danger": shots_danger[-20:],
        "key_moments": key_moments[-15:],
        "home_pressure_events": home_pressure_events,
        "away_pressure_events": away_pressure_events,
        "home_attacks": home_attacks,
        "away_attacks": away_attacks,
        "dominance": dominance,
    }
