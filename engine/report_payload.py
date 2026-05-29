"""Assemble stats + tactical summary into a single report payload for LLM."""
from typing import Any

from etl.schema import get_engine
from engine.stats import compute_match_stats
from engine.tactical import compute_tactical_summary


def build_report_payload(engine=None, match_id: int = None) -> dict[str, Any]:
    """
    Build the structured payload for report generation: match meta, stats, tactical summary.
    """
    if match_id is None:
        return {}
    eng = engine or get_engine()
    stats = compute_match_stats(eng, match_id)
    if not stats:
        return {}
    tactical = compute_tactical_summary(eng, match_id)
    source_id = stats.get("source_id") or ""
    data_source = "football-data.org（仅比分与进球时刻）" if source_id.startswith("fd_") else "StatsBomb（含事件数据）"
    ht_home = stats.get("half_time_home")
    ht_away = stats.get("half_time_away")
    half_time_score = None
    if ht_home is not None or ht_away is not None:
        half_time_score = f"{ht_home or 0}-{ht_away or 0}"
    payload = {
        "match_id": match_id,
        "title": f"{stats.get('home_team_name', 'Home')} vs {stats.get('away_team_name', 'Away')}",
        "competition_name": stats.get("competition_name", ""),
        "season_name": stats.get("season_name", ""),
        "score": f"{stats.get('home_score', 0)} - {stats.get('away_score', 0)}",
        "home_team_name": stats.get("home_team_name", ""),
        "away_team_name": stats.get("away_team_name", ""),
        "home_score": stats.get("home_score", 0),
        "away_score": stats.get("away_score", 0),
        "data_source": data_source,
        "stats": {
            "home": stats.get("home", {}),
            "away": stats.get("away", {}),
        },
        "tactical": {
            "key_moments": tactical.get("key_moments", []),
            "dominance": tactical.get("dominance", "balanced"),
            "home_attacks": tactical.get("home_attacks", 0),
            "away_attacks": tactical.get("away_attacks", 0),
        },
    }
    if half_time_score is not None:
        payload["half_time_score"] = half_time_score
    return payload
