"""Fetch StatsBomb open data from GitHub, normalize to unified schema, load into SQLite."""
import json
import logging
import time
from pathlib import Path
from typing import Any

import requests
from sqlalchemy.orm import Session
from sqlalchemy import text

from config import RAW_DIR, STATSBOMB_BASE
from etl.schema import (
    Base,
    Competition,
    Event,
    Match,
    Season,
    Team,
    get_engine,
    init_db,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 45
MAX_RETRIES = 3
RETRY_DELAY = 2


def _get(url: str, timeout: int = DEFAULT_TIMEOUT) -> list[dict] | dict:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _get_with_retry(url: str, timeout: int = DEFAULT_TIMEOUT, max_retries: int = MAX_RETRIES) -> list[dict] | dict:
    """Fetch URL with retries (for event files that may timeout behind proxy)."""
    last_err = None
    for attempt in range(max_retries):
        try:
            return _get(url, timeout=timeout)
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                logger.info("Retry %s/%s for %s: %s", attempt + 1, max_retries, url.split("/")[-1], e)
                time.sleep(RETRY_DELAY)
    raise last_err


def _safe(obj: Any, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            obj = obj[k]
        else:
            return default
    return obj


def _ensure_team(session: Session, team_id: int, team_name: str) -> int:
    row = session.query(Team).filter(Team.id == team_id).first()
    if not row:
        session.add(Team(id=team_id, name=team_name))
        session.flush()
    return team_id


def _ensure_competition_season(session: Session, comp_id: int, comp_name: str, country: str, season_id: int, season_name: str) -> None:
    if session.query(Competition).filter(Competition.id == comp_id).first() is None:
        session.add(Competition(id=comp_id, name=comp_name, country_name=country))
    if session.query(Season).filter(Season.id == season_id).first() is None:
        session.add(Season(id=season_id, competition_id=comp_id, name=season_name))
    session.flush()


def _event_type_name(ev: dict) -> str:
    t = ev.get("type")
    if isinstance(t, dict) and t.get("name"):
        return t["name"]
    return "Unknown"


def _outcome_name(ev: dict) -> tuple[str | None, int | None]:
    """Extract outcome from event (pass outcome, shot outcome, etc.). Returns (name, id)."""
    out = ev.get("pass", {}).get("outcome") or ev.get("shot", {}).get("outcome")
    if isinstance(out, dict):
        return out.get("name"), out.get("id")
    return None, None


def _location(ev: dict) -> tuple[float | None, float | None]:
    loc = ev.get("location")
    if isinstance(loc, (list, tuple)) and len(loc) >= 2:
        return float(loc[0]), float(loc[1])
    return None, None


def _end_location(ev: dict) -> tuple[float | None, float | None]:
    end = ev.get("pass", {}).get("end_location") or ev.get("carry", {}).get("end_location")
    if isinstance(end, (list, tuple)) and len(end) >= 2:
        return float(end[0]), float(end[1])
    shot = ev.get("shot", {}).get("end_location")
    if isinstance(shot, (list, tuple)) and len(shot) >= 2:
        return float(shot[0]), float(shot[1])
    return None, None


def _normalize_event(ev: dict, match_db_id: int) -> dict:
    team = ev.get("team") or {}
    team_id = team.get("id")
    team_name = team.get("name", "")
    player = ev.get("player") or {}
    player_id = player.get("id")
    player_name = player.get("name", "")
    pos = ev.get("position") or {}
    position_name = pos.get("name") if isinstance(pos, dict) else None
    loc_x, loc_y = _location(ev)
    end_x, end_y = _end_location(ev)
    outcome_name, outcome_id = _outcome_name(ev)
    type_obj = ev.get("type") or {}
    raw_type_id = type_obj.get("id") if isinstance(type_obj, dict) else None
    return {
        "match_id": match_db_id,
        "source_event_id": ev.get("id"),
        "index": ev.get("index"),
        "period": ev.get("period"),
        "minute": ev.get("minute"),
        "second": ev.get("second"),
        "event_type": _event_type_name(ev),
        "team_id": team_id,
        "team_name": team_name,
        "player_id": player_id,
        "player_name": player_name,
        "position_name": position_name,
        "location_x": loc_x,
        "location_y": loc_y,
        "end_location_x": end_x,
        "end_location_y": end_y,
        "duration": ev.get("duration"),
        "under_pressure": ev.get("under_pressure", False) or False,
        "outcome": outcome_name,
        "outcome_id": outcome_id,
        "raw_type_id": raw_type_id,
    }


def list_seasons_for_years(years: list[int]) -> list[dict]:
    """Fetch competitions.json and return entries whose season_name contains any of the given years (e.g. 2023, 2024)."""
    data = _get_with_retry(f"{STATSBOMB_BASE}/competitions.json")
    if not isinstance(data, list):
        data = [data]
    year_strs = [str(y) for y in years]
    out = []
    for c in data:
        season_name = (c.get("season_name") or "").strip()
        if any(ys in season_name for ys in year_strs):
            out.append({
                "competition_id": c.get("competition_id"),
                "season_id": c.get("season_id"),
                "competition_name": c.get("competition_name", ""),
                "country_name": c.get("country_name", ""),
                "season_name": season_name,
            })
    return sorted(out, key=lambda x: (x["season_name"], x["competition_name"]))


def load_competitions_and_seasons(engine) -> list[dict]:
    """Load competitions.json; return list of {competition_id, season_id, ...} for available data."""
    data = _get(f"{STATSBOMB_BASE}/competitions.json")
    if not isinstance(data, list):
        data = [data]
    comps = []
    for c in data:
        comp_id = c.get("competition_id")
        season_id = c.get("season_id")
        if comp_id is not None and season_id is not None:
            comps.append({
                "competition_id": comp_id,
                "competition_name": c.get("competition_name", ""),
                "country_name": c.get("country_name", ""),
                "season_id": season_id,
                "season_name": c.get("season_name", ""),
            })
    return comps


def _event_count_for_match(engine, match_db_id: int) -> int:
    with engine.connect() as conn:
        r = conn.execute(text("SELECT COUNT(1) FROM events WHERE match_id = :mid"), {"mid": match_db_id}).fetchone()
        return r[0] if r else 0


def run_ingest(
    competition_id: int | None = 11,
    season_id: int | None = 90,
    max_matches: int | None = 30,
    force_events: bool = False,
    ingest_all: bool = False,
) -> None:
    """
    Ingest StatsBomb open data: one competition/season by default (La Liga 2020/21).
    max_matches: 最多拉取场数，默认 30；ingest_all=True 时拉满该赛季不限制。
    force_events: 对已入库但无事件的比赛重新拉取事件（网络失败后可重试）。
    """
    init_db()
    engine = get_engine()
    comps = load_competitions_and_seasons(engine)
    if competition_id is not None and season_id is not None:
        comps = [c for c in comps if c["competition_id"] == competition_id and c["season_id"] == season_id]
    if not comps:
        # Fallback: use La Liga 11/90 if not in competitions.json (e.g. different file structure)
        comps = [{
            "competition_id": competition_id or 11,
            "competition_name": "La Liga",
            "country_name": "Spain",
            "season_id": season_id or 90,
            "season_name": "2020/2021",
        }]

    with Session(engine) as session:
        for c in comps:
            _ensure_competition_season(
                session,
                c["competition_id"],
                c["competition_name"],
                c["country_name"],
                c["season_id"],
                c["season_name"],
            )
        session.commit()

    comp_id = comps[0]["competition_id"]
    s_id = comps[0]["season_id"]
    matches_url = f"{STATSBOMB_BASE}/matches/{comp_id}/{s_id}.json"
    try:
        matches_list = _get(matches_url)
    except Exception as e:
        logger.warning("Could not fetch matches list: %s. Use competition_id/season_id from competitions.json.", e)
        matches_list = _get(f"{STATSBOMB_BASE}/matches/11/90.json")

    if not ingest_all and max_matches:
        matches_list = matches_list[: max_matches]

    with Session(engine) as session:
        for m in matches_list:
            mid = m.get("match_id")
            home = m.get("home_team") or {}
            away = m.get("away_team") or {}
            home_id = home.get("home_team_id") or home.get("id")
            away_id = away.get("away_team_id") or away.get("id")
            home_name = home.get("home_team_name") or home.get("name", "Unknown")
            away_name = away.get("away_team_name") or away.get("name", "Unknown")
            if home_id is None:
                home_id = m.get("home_team_id")
            if away_id is None:
                away_id = m.get("away_team_id")
            _ensure_team(session, home_id, home_name)
            _ensure_team(session, away_id, away_name)
            comp_row = session.query(Competition).filter(Competition.id == comp_id).first()
            existing = session.query(Match).filter(Match.source_id == str(mid)).first()
            if existing:
                match_db_id = existing.id
                need_events = force_events or (_event_count_for_match(engine, match_db_id) == 0)
                if not need_events:
                    logger.info("Match %s already in DB with events, skipping.", mid)
                else:
                    logger.info("Match %s already in DB, re-fetching events (force_events or no events).", mid)
            else:
                match_row = Match(
                    competition_id=comp_id,
                    season_id=s_id,
                    match_date=m.get("match_date"),
                    home_team_id=home_id,
                    away_team_id=away_id,
                    home_score=m.get("home_score"),
                    away_score=m.get("away_score"),
                    match_week=m.get("match_week"),
                    source_id=str(mid),
                    raw_meta=json.dumps({
                        "home_team_name": home_name,
                        "away_team_name": away_name,
                        "competition_name": comp_row.name if comp_row else "",
                        "season_name": comps[0].get("season_name", ""),
                    }),
                )
                session.add(match_row)
                session.flush()
                match_db_id = match_row.id

            # Events: fetch when match is new, or when existing but force_events / no events yet
            fetch_events = not existing or force_events or (_event_count_for_match(engine, match_db_id) == 0)
            if fetch_events:
                try:
                    events_list = _get_with_retry(f"{STATSBOMB_BASE}/events/{mid}.json")
                except Exception as e:
                    logger.warning("Skip events for match %s: %s", mid, e)
                    session.commit()
                    continue
                # If re-ingesting, delete old events to avoid duplicates
                if existing and _event_count_for_match(engine, match_db_id) > 0:
                    session.execute(text("DELETE FROM events WHERE match_id = :mid"), {"mid": match_db_id})
                    session.flush()
                for ev in events_list:
                    row = _normalize_event(ev, match_db_id)
                    session.add(Event(**row))
            session.commit()
            logger.info("Ingested match %s (%s vs %s)", mid, home_name, away_name)

    logger.info("Ingest done.")
