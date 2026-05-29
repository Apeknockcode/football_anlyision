"""Event and match-level statistics from unified events."""
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from etl.schema import get_engine


def compute_match_stats(engine: Engine | None, match_id: int) -> dict[str, Any]:
    """
    Compute match-level and per-team stats for a given match (DB match id).
    Returns dict with home/away stats: passes, shots, possession proxy, fouls, etc.
    """
    eng = engine or get_engine()
    with eng.connect() as conn:
        row = conn.execute(
            text("""
                SELECT m.id, m.home_team_id, m.away_team_id, m.home_score, m.away_score, m.raw_meta, m.source_id
                FROM matches m WHERE m.id = :mid
            """),
            {"mid": match_id},
        ).fetchone()
        if not row:
            return {}
        home_team_id = row[1]
        away_team_id = row[2]
        home_score = row[3]
        away_score = row[4]
        raw_meta = row[5] or "{}"
        source_id = row[6] or ""
        try:
            meta = json.loads(raw_meta)
        except Exception:
            meta = {}
        home_name = meta.get("home_team_name", "Home")
        away_name = meta.get("away_team_name", "Away")

        events = conn.execute(
            text("""
                SELECT event_type, team_id, outcome, outcome_id
                FROM events WHERE match_id = :mid
            """),
            {"mid": match_id},
        ).fetchall()

    # Aggregate by team
    home = {"team_id": home_team_id, "team_name": home_name, "passes": 0, "passes_complete": 0, "shots": 0, "shots_on_target": 0, "fouls": 0, "carries": 0}
    away = {"team_id": away_team_id, "team_name": away_name, "passes": 0, "passes_complete": 0, "shots": 0, "shots_on_target": 0, "fouls": 0, "carries": 0}

    for event_type, team_id, outcome, outcome_id in events:
        target = home if team_id == home_team_id else away
        if team_id not in (home_team_id, away_team_id):
            continue
        if event_type == "Pass":
            target["passes"] += 1
            if outcome == "Complete" or outcome_id == 9 or outcome is None:
                target["passes_complete"] += 1
        elif event_type == "Shot":
            target["shots"] += 1
            if outcome in ("Goal", "Saved", "Saved Off Target", "Saved to Post") or outcome_id in (97, 100, 115, 116):
                target["shots_on_target"] += 1
        elif event_type == "Foul Committed":
            target["fouls"] += 1
        elif event_type == "Carry":
            target["carries"] += 1

    # Possession proxy: passes + carries per team
    home_total = home["passes"] + home["carries"]
    away_total = away["passes"] + away["carries"]
    total = home_total + away_total
    home["possession_pct"] = round(100 * home_total / total, 1) if total else 0
    away["possession_pct"] = round(100 * away_total / total, 1) if total else 0

    # football-data 单场统计补全：若有 fd_statistics 则覆盖，使报告展示 API 提供的射门/控球等
    fd_stats = meta.get("fd_statistics")
    if isinstance(fd_stats, dict):
        fh = fd_stats.get("home") or {}
        fa = fd_stats.get("away") or {}
        if fh:
            if fh.get("shots") is not None:
                home["shots"] = int(fh["shots"])
            if fh.get("shots_on_goal") is not None:
                home["shots_on_target"] = int(fh["shots_on_goal"])
            if fh.get("ball_possession") is not None:
                home["possession_pct"] = round(float(fh["ball_possession"]), 1)
            if fh.get("fouls") is not None:
                home["fouls"] = int(fh["fouls"])
        if fa:
            if fa.get("shots") is not None:
                away["shots"] = int(fa["shots"])
            if fa.get("shots_on_goal") is not None:
                away["shots_on_target"] = int(fa["shots_on_goal"])
            if fa.get("ball_possession") is not None:
                away["possession_pct"] = round(float(fa["ball_possession"]), 1)
            if fa.get("fouls") is not None:
                away["fouls"] = int(fa["fouls"])

    half_time_home = meta.get("half_time_home")
    half_time_away = meta.get("half_time_away")
    return {
        "match_id": match_id,
        "home_score": home_score,
        "away_score": away_score,
        "home_team_name": home_name,
        "away_team_name": away_name,
        "half_time_home": half_time_home,
        "half_time_away": half_time_away,
        "home": home,
        "away": away,
        "competition_name": meta.get("competition_name", ""),
        "season_name": meta.get("season_name", ""),
        "source_id": source_id,
    }
