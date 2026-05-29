"""
用 API-FOOTBALL（api-sports）拉取五大联赛历史完赛比赛并入库。
目标：让历史比赛与后续伤停/赔率/积分同源，减少队名体系不一致。
"""
from __future__ import annotations

import json
import time
from typing import Any

import config
from etl.injury_api_football import LEAGUE_IDS, _request
from etl.schema import (
    Competition,
    Event,
    Match,
    Prediction,
    PredictionLog,
    Season,
    Team,
    get_engine,
    init_db,
)
from sqlalchemy.orm import Session

# 避免与 StatsBomb / football-data 的 id 范围冲突
AF_TEAM_OFFSET = 90000
AF_COMP_OFFSET = 6000
AF_SEASON_OFFSET = 9000

# API 联赛名标准化（用于 DB competitions.name）
LEAGUE_NAME_BY_ID: dict[int, str] = {
    39: "Premier League",
    140: "La Liga",
    135: "Serie A",
    78: "Bundesliga",
    61: "Ligue 1",
}
# 联赛 ID -> 国家名（用于 competitions.country_name）
LEAGUE_COUNTRY_BY_ID: dict[int, str] = {
    39: "England",
    140: "Spain",
    135: "Italy",
    78: "Germany",
    61: "France",
}


def _is_finished_fixture(item: dict[str, Any]) -> bool:
    status = ((item.get("fixture") or {}).get("status") or {})
    short = (status.get("short") or "").upper().strip()
    long_name = (status.get("long") or "").upper().strip()
    return short in {"FT", "AET", "PEN"} or "FINISHED" in long_name


def _is_upcoming_fixture(item: dict[str, Any]) -> bool:
    """未开赛或进行中（NS/TBD/TIMED/1H/2H 等），非已完赛。"""
    return not _is_finished_fixture(item)


def _halftime_from_api_football_item(item: dict[str, Any]) -> tuple[Any, Any]:
    """fixtures 响应里 score.halftime（或 halfTime）的主客半场进球，与 football-data 一致写入 raw_meta。"""
    score = item.get("score") or {}
    ht = score.get("halftime") or score.get("halfTime") or {}
    return ht.get("home"), ht.get("away")


def _ensure_team(session: Session, api_team_id: int, team_name: str) -> int:
    our_id = AF_TEAM_OFFSET + int(api_team_id)
    row = session.query(Team).filter(Team.id == our_id).first()
    if row is None:
        session.add(Team(id=our_id, name=(team_name or "").strip() or f"team_{api_team_id}"))
        session.flush()
    return our_id


def _ensure_competition_season(session: Session, league_id: int, season_year: int) -> tuple[int, int]:
    comp_name = LEAGUE_NAME_BY_ID.get(int(league_id), f"League {league_id}")
    comp_id = AF_COMP_OFFSET + int(league_id)
    idx = list(LEAGUE_IDS.keys()).index(int(league_id))
    season_id = AF_SEASON_OFFSET + int(season_year) * 10 + idx
    if session.query(Competition).filter(Competition.id == comp_id).first() is None:
        country = LEAGUE_COUNTRY_BY_ID.get(int(league_id), "")
        session.add(Competition(id=comp_id, name=comp_name, country_name=country))
    season_name = f"{int(season_year)}/{int(season_year) + 1}"
    if session.query(Season).filter(Season.id == season_id).first() is None:
        session.add(Season(id=season_id, competition_id=comp_id, name=season_name))
    session.flush()
    return comp_id, season_id


def reset_big5_history_for_seasons(seasons: list[int]) -> dict[str, int]:
    """
    删除五大联赛指定赛季的历史比赛（无论来源），并清理关联 events/predictions/prediction_log。
    """
    init_db()
    engine = get_engine()
    season_names = {f"{int(y)}/{int(y) + 1}" for y in seasons}
    big5_names = tuple(LEAGUE_NAME_BY_ID.values()) + ("Primera Division", "Primera División", "1. Bundesliga")
    with Session(engine) as session:
        rows = (
            session.query(Match.id)
            .join(Season, Match.season_id == Season.id)
            .join(Competition, Match.competition_id == Competition.id)
            .filter(Season.name.in_(season_names), Competition.name.in_(big5_names))
            .all()
        )
        match_ids = [r[0] for r in rows]
        if not match_ids:
            return {"matches": 0, "events": 0, "predictions": 0, "prediction_log": 0}
        events_deleted = session.query(Event).filter(Event.match_id.in_(match_ids)).delete(synchronize_session=False)
        preds_deleted = session.query(Prediction).filter(Prediction.match_id.in_(match_ids)).delete(synchronize_session=False)
        plog_deleted = session.query(PredictionLog).filter(PredictionLog.match_id.in_(match_ids)).delete(synchronize_session=False)
        matches_deleted = session.query(Match).filter(Match.id.in_(match_ids)).delete(synchronize_session=False)
        session.commit()
    return {
        "matches": int(matches_deleted or 0),
        "events": int(events_deleted or 0),
        "predictions": int(preds_deleted or 0),
        "prediction_log": int(plog_deleted or 0),
    }


def _ingest_finished_big5_history_mongo(
    seasons: list[int],
    rate_limit_sec: float,
    exclude_fixture_ids: set[int] | None = None,
) -> dict[str, int]:
    """USE_MONGO 时：通过 repo 写入 MongoDB。"""
    from db import repo
    inserted = 0
    updated = 0
    skipped = 0
    for season_year in seasons:
        for league_id in LEAGUE_IDS:
            comp_id = AF_COMP_OFFSET + int(league_id)
            comp_name = LEAGUE_NAME_BY_ID.get(int(league_id), f"League {league_id}")
            country = LEAGUE_COUNTRY_BY_ID.get(int(league_id), "")
            repo.ensure_competition(comp_id, comp_name, country)
            idx = list(LEAGUE_IDS.keys()).index(int(league_id))
            season_id = AF_SEASON_OFFSET + int(season_year) * 10 + idx
            repo.ensure_season(season_id, comp_id, f"{int(season_year)}/{int(season_year) + 1}")
            data = _request("fixtures", {"league": league_id, "season": int(season_year)})
            time.sleep(rate_limit_sec)
            items = data.get("response") or []
            for item in items:
                if not _is_finished_fixture(item):
                    continue
                fixture = item.get("fixture") or {}
                fid = fixture.get("id")
                if not fid:
                    skipped += 1
                    continue
                fid_int = int(fid)
                if exclude_fixture_ids and fid_int in exclude_fixture_ids:
                    skipped += 1
                    continue
                teams = item.get("teams") or {}
                home_t = teams.get("home") or {}
                away_t = teams.get("away") or {}
                home_api_id = home_t.get("id")
                away_api_id = away_t.get("id")
                home_name = (home_t.get("name") or "").strip()
                away_name = (away_t.get("name") or "").strip()
                if home_api_id is None or away_api_id is None:
                    skipped += 1
                    continue
                home_id = repo.ensure_team_by_id(AF_TEAM_OFFSET + int(home_api_id), home_name)
                away_id = repo.ensure_team_by_id(AF_TEAM_OFFSET + int(away_api_id), away_name)
                goals = item.get("goals") or {}
                hs, aws = goals.get("home"), goals.get("away")
                if hs is None or aws is None:
                    skipped += 1
                    continue
                source_id = f"af_{fid_int}"
                match_date = (fixture.get("date") or "")[:10]
                meta_mongo: dict[str, Any] = {
                    "home_team_name": home_name,
                    "away_team_name": away_name,
                    "competition_name": LEAGUE_NAME_BY_ID.get(int(league_id), ""),
                    "season_name": f"{int(season_year)}/{int(season_year) + 1}",
                    "data_source": "api_football",
                }
                hh_m, ha_m = _halftime_from_api_football_item(item)
                if hh_m is not None or ha_m is not None:
                    meta_mongo["half_time_home"] = hh_m
                    meta_mongo["half_time_away"] = ha_m
                raw_meta = json.dumps(meta_mongo, ensure_ascii=False)
                match_week = item.get("league", {}).get("round")
                existing = repo.get_match_by_source_id(source_id)
                if existing:
                    repo.update_match(
                        existing["id"],
                        competition_id=comp_id,
                        season_id=season_id,
                        match_date=match_date,
                        home_team_id=home_id,
                        away_team_id=away_id,
                        home_score=int(hs),
                        away_score=int(aws),
                        match_week=match_week,
                        raw_meta=raw_meta,
                    )
                    updated += 1
                else:
                    repo.insert_match({
                        "competition_id": comp_id,
                        "season_id": season_id,
                        "match_date": match_date,
                        "home_team_id": home_id,
                        "away_team_id": away_id,
                        "home_score": int(hs),
                        "away_score": int(aws),
                        "match_week": match_week,
                        "source_id": source_id,
                        "raw_meta": raw_meta,
                    })
                    inserted += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def _ingest_upcoming_big5_mongo(
    seasons: list[int],
    rate_limit_sec: float,
) -> dict[str, int]:
    """
    拉取五大联赛未开赛场次（NS/TBD/TIMED 等）并 upsert 到 MongoDB。
    已完赛的场次跳过（由 ingest-api-history 维护）；若 DB 中已有该 source_id 且已有比分则不覆盖。
    """
    from db import repo
    inserted = 0
    updated = 0
    skipped = 0
    for season_year in seasons:
        for league_id in LEAGUE_IDS:
            comp_id = AF_COMP_OFFSET + int(league_id)
            comp_name = LEAGUE_NAME_BY_ID.get(int(league_id), f"League {league_id}")
            country = LEAGUE_COUNTRY_BY_ID.get(int(league_id), "")
            repo.ensure_competition(comp_id, comp_name, country)
            idx = list(LEAGUE_IDS.keys()).index(int(league_id))
            season_id = AF_SEASON_OFFSET + int(season_year) * 10 + idx
            repo.ensure_season(season_id, comp_id, f"{int(season_year)}/{int(season_year) + 1}")
            data = _request("fixtures", {"league": league_id, "season": int(season_year)})
            time.sleep(rate_limit_sec)
            items = data.get("response") or []
            for item in items:
                if not _is_upcoming_fixture(item):
                    continue
                fixture = item.get("fixture") or {}
                fid = fixture.get("id")
                if not fid:
                    skipped += 1
                    continue
                teams = item.get("teams") or {}
                home_t = teams.get("home") or {}
                away_t = teams.get("away") or {}
                home_api_id = home_t.get("id")
                away_api_id = away_t.get("id")
                home_name = (home_t.get("name") or "").strip()
                away_name = (away_t.get("name") or "").strip()
                if home_api_id is None or away_api_id is None:
                    skipped += 1
                    continue
                home_id = repo.ensure_team_by_id(AF_TEAM_OFFSET + int(home_api_id), home_name)
                away_id = repo.ensure_team_by_id(AF_TEAM_OFFSET + int(away_api_id), away_name)
                source_id = f"af_{int(fid)}"
                match_date = (fixture.get("date") or "")[:10]
                raw_meta = json.dumps(
                    {
                        "home_team_name": home_name,
                        "away_team_name": away_name,
                        "competition_name": LEAGUE_NAME_BY_ID.get(int(league_id), ""),
                        "season_name": f"{int(season_year)}/{int(season_year) + 1}",
                        "data_source": "api_football",
                    },
                    ensure_ascii=False,
                )
                match_week = item.get("league", {}).get("round")
                existing = repo.get_match_by_source_id(source_id)
                if existing and (
                    existing.get("home_score") is not None or existing.get("away_score") is not None
                ):
                    skipped += 1
                    continue
                if existing:
                    repo.update_match(
                        existing["id"],
                        competition_id=comp_id,
                        season_id=season_id,
                        match_date=match_date,
                        home_team_id=home_id,
                        away_team_id=away_id,
                        home_score=None,
                        away_score=None,
                        match_week=match_week,
                        raw_meta=raw_meta,
                    )
                    updated += 1
                else:
                    repo.insert_match({
                        "competition_id": comp_id,
                        "season_id": season_id,
                        "match_date": match_date,
                        "home_team_id": home_id,
                        "away_team_id": away_id,
                        "home_score": None,
                        "away_score": None,
                        "match_week": match_week,
                        "source_id": source_id,
                        "raw_meta": raw_meta,
                    })
                    inserted += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def ingest_upcoming_big5(
    seasons: list[int],
    rate_limit_sec: float | None = None,
) -> dict[str, int]:
    """
    拉取五大联赛未开赛场次并入库（仅未开赛：NS/TBD/TIMED 等）。
    USE_MONGO 时写入 MongoDB，否则写入 SQLite。用于补齐 2026-02-24 及以后的未来赛程。
    """
    wait = rate_limit_sec if rate_limit_sec is not None else getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    if getattr(config, "USE_MONGO", False):
        return _ingest_upcoming_big5_mongo(seasons, wait)
    init_db()
    engine = get_engine()
    inserted = 0
    updated = 0
    skipped = 0
    with Session(engine) as session:
        for season_year in seasons:
            for league_id in LEAGUE_IDS:
                comp_id, season_id = _ensure_competition_season(session, league_id, season_year)
                data = _request("fixtures", {"league": league_id, "season": int(season_year)})
                time.sleep(wait)
                items = data.get("response") or []
                for item in items:
                    if not _is_upcoming_fixture(item):
                        continue
                    fixture = item.get("fixture") or {}
                    fid = fixture.get("id")
                    if not fid:
                        skipped += 1
                        continue
                    teams = item.get("teams") or {}
                    home_t = teams.get("home") or {}
                    away_t = teams.get("away") or {}
                    home_api_id = home_t.get("id")
                    away_api_id = away_t.get("id")
                    home_name = (home_t.get("name") or "").strip()
                    away_name = (away_t.get("name") or "").strip()
                    if home_api_id is None or away_api_id is None:
                        skipped += 1
                        continue
                    home_id = _ensure_team(session, int(home_api_id), home_name)
                    away_id = _ensure_team(session, int(away_api_id), away_name)
                    source_id = f"af_{int(fid)}"
                    match_date = (fixture.get("date") or "")[:10]
                    raw_meta = json.dumps(
                        {
                            "home_team_name": home_name,
                            "away_team_name": away_name,
                            "competition_name": LEAGUE_NAME_BY_ID.get(int(league_id), ""),
                            "season_name": f"{int(season_year)}/{int(season_year) + 1}",
                            "data_source": "api_football",
                        },
                        ensure_ascii=False,
                    )
                    match_week = item.get("league", {}).get("round")
                    existing = session.query(Match).filter(Match.source_id == source_id).first()
                    if existing and (existing.home_score is not None or existing.away_score is not None):
                        skipped += 1
                        continue
                    if existing:
                        existing.competition_id = comp_id
                        existing.season_id = season_id
                        existing.match_date = match_date
                        existing.home_team_id = home_id
                        existing.away_team_id = away_id
                        existing.home_score = None
                        existing.away_score = None
                        existing.match_week = match_week
                        existing.raw_meta = raw_meta
                        updated += 1
                    else:
                        session.add(
                            Match(
                                competition_id=comp_id,
                                season_id=season_id,
                                match_date=match_date,
                                home_team_id=home_id,
                                away_team_id=away_id,
                                home_score=None,
                                away_score=None,
                                match_week=match_week,
                                source_id=source_id,
                                raw_meta=raw_meta,
                            )
                        )
                        inserted += 1
                session.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def ingest_finished_big5_history(
    seasons: list[int],
    rate_limit_sec: float | None = None,
    exclude_fixture_ids: set[int] | None = None,
) -> dict[str, int]:
    """
    拉取五大联赛指定赛季的已完赛场次（FT/AET/PEN）并入库。
    USE_MONGO 时写入 MongoDB，否则写入 SQLite/MySQL。
    exclude_fixture_ids: 若设置，这些 API fixture id 将跳过（不插入、不更新），用于只补竞彩未覆盖场次。
    """
    wait = rate_limit_sec if rate_limit_sec is not None else getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    if getattr(config, "USE_MONGO", False):
        return _ingest_finished_big5_history_mongo(seasons, wait, exclude_fixture_ids=exclude_fixture_ids)
    init_db()
    engine = get_engine()
    inserted = 0
    updated = 0
    skipped = 0
    with Session(engine) as session:
        for season_year in seasons:
            for league_id in LEAGUE_IDS:
                comp_id, season_id = _ensure_competition_season(session, league_id, season_year)
                data = _request("fixtures", {"league": league_id, "season": int(season_year)})
                time.sleep(wait)
                items = data.get("response") or []
                for item in items:
                    if not _is_finished_fixture(item):
                        continue
                    fixture = item.get("fixture") or {}
                    fid = fixture.get("id")
                    if not fid:
                        skipped += 1
                        continue
                    fid_int = int(fid)
                    if exclude_fixture_ids and fid_int in exclude_fixture_ids:
                        skipped += 1
                        continue
                    teams = item.get("teams") or {}
                    home_t = teams.get("home") or {}
                    away_t = teams.get("away") or {}
                    home_api_id = home_t.get("id")
                    away_api_id = away_t.get("id")
                    home_name = (home_t.get("name") or "").strip()
                    away_name = (away_t.get("name") or "").strip()
                    if home_api_id is None or away_api_id is None:
                        skipped += 1
                        continue
                    home_id = _ensure_team(session, int(home_api_id), home_name)
                    away_id = _ensure_team(session, int(away_api_id), away_name)
                    goals = item.get("goals") or {}
                    hs = goals.get("home")
                    aws = goals.get("away")
                    if hs is None or aws is None:
                        skipped += 1
                        continue
                    source_id = f"af_{fid_int}"
                    match_date = (fixture.get("date") or "")[:10]
                    meta_dict_sql: dict[str, Any] = {
                        "home_team_name": home_name,
                        "away_team_name": away_name,
                        "competition_name": LEAGUE_NAME_BY_ID.get(int(league_id), ""),
                        "season_name": f"{int(season_year)}/{int(season_year) + 1}",
                        "data_source": "api_football",
                    }
                    hh, ha = _halftime_from_api_football_item(item)
                    if hh is not None or ha is not None:
                        meta_dict_sql["half_time_home"] = hh
                        meta_dict_sql["half_time_away"] = ha
                    raw_meta = json.dumps(meta_dict_sql, ensure_ascii=False)
                    existing = session.query(Match).filter(Match.source_id == source_id).first()
                    if existing:
                        existing.competition_id = comp_id
                        existing.season_id = season_id
                        existing.match_date = match_date
                        existing.home_team_id = home_id
                        existing.away_team_id = away_id
                        existing.home_score = int(hs)
                        existing.away_score = int(aws)
                        existing.match_week = item.get("league", {}).get("round")
                        existing.raw_meta = raw_meta
                        updated += 1
                    else:
                        session.add(
                            Match(
                                competition_id=comp_id,
                                season_id=season_id,
                                match_date=match_date,
                                home_team_id=home_id,
                                away_team_id=away_id,
                                home_score=int(hs),
                                away_score=int(aws),
                                match_week=None,
                                source_id=source_id,
                                raw_meta=raw_meta,
                            )
                        )
                        inserted += 1
                session.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}
