"""
Ingest from football-data.org API: 五大联赛（英超/西甲/意甲/德甲/法甲）赛程与比分。
数据为比赛级（比分、进球者），无逐次触球事件；进球会写入 events 表作为 Shot/Goal 供报告使用。
"""
import json
import logging
import time
from typing import Any

import requests
from sqlalchemy.orm import Session
from sqlalchemy import text

import config
from etl.schema import (
    Competition,
    Event,
    Match,
    Season,
    Team,
    get_engine,
    init_db,
)

logger = logging.getLogger(__name__)
# 避免与 StatsBomb 的 team/comp/season id 冲突
FD_TEAM_OFFSET = 50000
FD_COMP_OFFSET = 1000
FD_SEASON_OFFSET = 3000
RATE_LIMIT_SEC = 6.5


def _get_fd(path: str) -> dict | list:
    url = config.FOOTBALL_DATA_BASE.rstrip("/") + "/" + path.lstrip("/")
    r = requests.get(
        url,
        headers={"X-Auth-Token": config.FOOTBALL_DATA_API_KEY},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_upcoming_fixtures(league_code: str, season: int) -> list[dict]:
    """拉取指定联赛/赛季的未开赛或待开赛场次（SCHEDULED/TIMED）。用于预测前查看赛程。"""
    if not config.FOOTBALL_DATA_API_KEY:
        return []
    try:
        time.sleep(RATE_LIMIT_SEC)
        data = _get_fd(f"competitions/{league_code}/matches?season={season}")
    except Exception:
        return []
    out = []
    for m in data.get("matches") or []:
        if m.get("status") in ("SCHEDULED", "TIMED"):
            home = (m.get("homeTeam") or {}).get("name", "?")
            away = (m.get("awayTeam") or {}).get("name", "?")
            out.append({
                "utcDate": m.get("utcDate", ""),
                "matchday": m.get("matchday"),
                "home": home,
                "away": away,
            })
    return out


def _parse_fd_statistics(stat: dict | None) -> dict[str, Any]:
    """从 API 的 statistics 对象提取 shots, shots_on_goal, ball_possession, fouls 等。"""
    if not stat:
        return {}
    return {
        "shots": stat.get("shots"),
        "shots_on_goal": stat.get("shots_on_goal"),
        "ball_possession": stat.get("ball_possession"),
        "fouls": stat.get("fouls"),
    }


def enrich_fd_match_stats(match_id: int | None = None, limit: int | None = 20) -> int:
    """
    对 source_id 为 fd_* 的已入库比赛请求 GET /matches/{id}，将 home/away statistics 与半场比分
    写入 raw_meta（fd_statistics、half_time_home/away）。每次请求后遵守限频。
    返回补全的场次数。
    """
    if not config.FOOTBALL_DATA_API_KEY:
        logger.warning("未配置 FOOTBALL_DATA_API_KEY，跳过补全")
        return 0
    init_db()
    engine = get_engine()
    with Session(engine) as session:
        if match_id is not None:
            row = session.query(Match).filter(Match.id == match_id).first()
            if not row or not (row.source_id or "").startswith("fd_"):
                return 0
            rows = [(row.id, row.source_id, row.raw_meta)]
        else:
            all_fd = session.query(Match).filter(Match.source_id.like("fd_%")).all()
            need_enrich = []
            for row in all_fd:
                try:
                    meta = json.loads(row.raw_meta or "{}")
                except Exception:
                    meta = {}
                if meta.get("fd_statistics") is not None:
                    continue
                need_enrich.append((row.id, row.source_id, row.raw_meta))
            cap = limit if limit is not None else 20
            rows = need_enrich[:cap]
        count = 0
        for db_id, source_id, raw_meta_str in rows:
            try:
                fd_id = int((source_id or "").replace("fd_", ""))
            except ValueError:
                continue
            try:
                time.sleep(RATE_LIMIT_SEC)
                data = _get_fd(f"matches/{fd_id}")
            except Exception as e:
                logger.warning("GET /matches/%s 失败: %s", fd_id, e)
                continue
            try:
                meta = json.loads(raw_meta_str or "{}")
            except Exception:
                meta = {}
            home_stat = data.get("homeTeam") or {}
            away_stat = data.get("awayTeam") or {}
            meta["fd_statistics"] = {
                "home": _parse_fd_statistics(home_stat.get("statistics")),
                "away": _parse_fd_statistics(away_stat.get("statistics")),
            }
            ht = (data.get("score") or {}).get("halfTime") or {}
            if ht.get("home") is not None or ht.get("away") is not None:
                meta["half_time_home"] = ht.get("home")
                meta["half_time_away"] = ht.get("away")
            session.query(Match).filter(Match.id == db_id).update(
                {Match.raw_meta: json.dumps(meta)},
                synchronize_session=False,
            )
            session.commit()
            count += 1
            logger.info("FD enrich: match_id=%s fd_id=%s", db_id, fd_id)
    return count


def _ensure_team(session: Session, fd_team_id: int, name: str) -> int:
    our_id = FD_TEAM_OFFSET + fd_team_id
    row = session.query(Team).filter(Team.id == our_id).first()
    if not row:
        session.add(Team(id=our_id, name=name))
        session.flush()
    return our_id


def _ensure_comp_season(
    session: Session,
    fd_comp_id: int,
    comp_name: str,
    area_name: str,
    season_year: int,
    code: str,
    league_codes: list[str],
) -> tuple[int, int]:
    our_comp_id = FD_COMP_OFFSET + fd_comp_id
    idx = league_codes.index(code) if code in league_codes else 0
    our_season_id = FD_SEASON_OFFSET + season_year * 10 + idx
    if session.query(Competition).filter(Competition.id == our_comp_id).first() is None:
        session.add(Competition(id=our_comp_id, name=comp_name, country_name=area_name))
    if session.query(Season).filter(Season.id == our_season_id).first() is None:
        session.add(Season(id=our_season_id, competition_id=our_comp_id, name=f"{season_year}/{season_year+1}"))
    session.flush()
    return our_comp_id, our_season_id


def run_ingest_football_data(
    leagues: list[str] | None = None,
    seasons: list[int] | None = None,
    finished_only: bool = False,
) -> None:
    """
    拉取 football-data.org 五大联赛（或指定 leagues）的指定赛季，写入 matches + 进球事件。
    leagues: 如 ["PL","PD","SA","BL1","FL1"]，默认五大联赛。
    seasons: 如 [2023, 2024, 2025]，默认最近 4 年。
    finished_only: True 时仅写入/更新 FINISHED 场次，不写入待赛场次。
    """
    if not config.FOOTBALL_DATA_API_KEY:
        logger.warning("未配置 FOOTBALL_DATA_API_KEY，跳过 football-data 拉取")
        return
    init_db()
    engine = get_engine()
    leagues = leagues or config.FOOTBALL_DATA_BIG5
    seasons = seasons or [2023, 2024, 2025, 2026]
    with Session(engine) as session:
        for code in leagues:
            for year in seasons:
                try:
                    time.sleep(RATE_LIMIT_SEC)
                    data = _get_fd(f"competitions/{code}/matches?season={year}")
                except Exception as e:
                    logger.warning("拉取 %s %s 失败: %s", code, year, e)
                    continue
                comp_node = data.get("competition") or {}
                area_node = data.get("area") or {}
                comp_id_fd = comp_node.get("id") or 0
                comp_name = comp_node.get("name") or code
                area_name = area_node.get("name") or ""
                our_comp_id, our_season_id = _ensure_comp_season(
                    session, comp_id_fd, comp_name, area_name, year, code, leagues
                )
                matches = data.get("matches") or []
                for m in matches:
                    fd_id = m.get("id")
                    source_id = f"fd_{fd_id}"
                    status = m.get("status")
                    home = m.get("homeTeam") or {}
                    away = m.get("awayTeam") or {}
                    home_id_fd = home.get("id")
                    away_id_fd = away.get("id")
                    home_name = home.get("name") or "Home"
                    away_name = away.get("name") or "Away"
                    if home_id_fd is None or away_id_fd is None:
                        logger.warning("跳过无队伍 id 的场次 fd_id=%s: %s vs %s", fd_id, home_name, away_name)
                        continue
                    _ensure_team(session, home_id_fd, home_name)
                    _ensure_team(session, away_id_fd, away_name)
                    home_id = FD_TEAM_OFFSET + home_id_fd
                    away_id = FD_TEAM_OFFSET + away_id_fd
                    utc = (m.get("utcDate") or "")[:10]
                    existing = session.query(Match).filter(Match.source_id == source_id).first()

                    if existing:
                        if status == "FINISHED" and existing.home_score is None:
                            score = m.get("score") or {}
                            ft = score.get("fullTime") or {}
                            ht = score.get("halfTime") or {}
                            home_score = ft.get("home") if ft.get("home") is not None else 0
                            away_score = ft.get("away") if ft.get("away") is not None else 0
                            raw_meta_dict = {
                                "home_team_name": home_name,
                                "away_team_name": away_name,
                                "competition_name": comp_name,
                                "season_name": f"{year}/{year+1}",
                            }
                            if ht.get("home") is not None or ht.get("away") is not None:
                                raw_meta_dict["half_time_home"] = ht.get("home")
                                raw_meta_dict["half_time_away"] = ht.get("away")
                            existing.home_score = home_score
                            existing.away_score = away_score
                            existing.raw_meta = json.dumps(raw_meta_dict)
                            session.flush()
                            match_db_id = existing.id
                            goals = m.get("goals") or []
                            for i, g in enumerate(goals):
                                team_fd = g.get("team") or {}
                                team_id_fd = team_fd.get("id")
                                team_name = team_fd.get("name") or ""
                                scorer = g.get("scorer") or {}
                                player_name = scorer.get("name") or "?"
                                minute = g.get("minute") or 0
                                team_our_id = FD_TEAM_OFFSET + team_id_fd if team_id_fd is not None else None
                                ev = Event(
                                    match_id=match_db_id,
                                    source_event_id=f"fd_goal_{fd_id}_{i}",
                                    index=i,
                                    period=1,
                                    minute=minute if isinstance(minute, int) else None,
                                    second=0,
                                    event_type="Shot",
                                    team_id=team_our_id,
                                    team_name=team_name,
                                    player_id=scorer.get("id"),
                                    player_name=player_name,
                                    position_name=None,
                                    location_x=None,
                                    location_y=None,
                                    end_location_x=None,
                                    end_location_y=None,
                                    duration=None,
                                    under_pressure=False,
                                    outcome="Goal",
                                    outcome_id=None,
                                    raw_type_id=None,
                                )
                                session.add(ev)
                            session.commit()
                            logger.info("FD: updated %s %s %s vs %s %s-%s", comp_name, year, home_name, away_name, home_score, away_score)
                        continue

                    if status == "FINISHED":
                        score = m.get("score") or {}
                        ft = score.get("fullTime") or {}
                        ht = score.get("halfTime") or {}
                        home_score = ft.get("home")
                        away_score = ft.get("away")
                        if home_score is None:
                            home_score = 0
                        if away_score is None:
                            away_score = 0
                        raw_meta_dict = {
                            "home_team_name": home_name,
                            "away_team_name": away_name,
                            "competition_name": comp_name,
                            "season_name": f"{year}/{year+1}",
                        }
                        if ht.get("home") is not None or ht.get("away") is not None:
                            raw_meta_dict["half_time_home"] = ht.get("home")
                            raw_meta_dict["half_time_away"] = ht.get("away")
                        match_row = Match(
                            competition_id=our_comp_id,
                            season_id=our_season_id,
                            match_date=utc,
                            home_team_id=home_id,
                            away_team_id=away_id,
                            home_score=home_score,
                            away_score=away_score,
                            match_week=m.get("matchday"),
                            source_id=source_id,
                            raw_meta=json.dumps(raw_meta_dict),
                        )
                        session.add(match_row)
                        session.flush()
                        match_db_id = match_row.id
                        goals = m.get("goals") or []
                        for i, g in enumerate(goals):
                            team_fd = g.get("team") or {}
                            team_id_fd = team_fd.get("id")
                            team_name = team_fd.get("name") or ""
                            scorer = g.get("scorer") or {}
                            player_name = scorer.get("name") or "?"
                            minute = g.get("minute") or 0
                            team_our_id = FD_TEAM_OFFSET + team_id_fd if team_id_fd is not None else None
                            ev = Event(
                                match_id=match_db_id,
                                source_event_id=f"fd_goal_{fd_id}_{i}",
                                index=i,
                                period=1,
                                minute=minute if isinstance(minute, int) else None,
                                second=0,
                                event_type="Shot",
                                team_id=team_our_id,
                                team_name=team_name,
                                player_id=scorer.get("id"),
                                player_name=player_name,
                                position_name=None,
                                location_x=None,
                                location_y=None,
                                end_location_x=None,
                                end_location_y=None,
                                duration=None,
                                under_pressure=False,
                                outcome="Goal",
                                outcome_id=None,
                                raw_type_id=None,
                            )
                            session.add(ev)
                        session.commit()
                        logger.info("FD: %s %s %s vs %s %s-%s", comp_name, year, home_name, away_name, home_score, away_score)
                    elif (not finished_only) and status in ("SCHEDULED", "TIMED"):
                        match_row = Match(
                            competition_id=our_comp_id,
                            season_id=our_season_id,
                            match_date=utc,
                            home_team_id=home_id,
                            away_team_id=away_id,
                            home_score=None,
                            away_score=None,
                            match_week=m.get("matchday"),
                            source_id=source_id,
                            raw_meta=json.dumps({
                                "home_team_name": home_name,
                                "away_team_name": away_name,
                                "competition_name": comp_name,
                                "season_name": f"{year}/{year+1}",
                            }),
                        )
                        session.add(match_row)
                        session.commit()
                        logger.info("FD: %s %s %s vs %s 待赛", comp_name, year, home_name, away_name)
        logger.info("football-data.org ingest done.")


def run_ingest_football_data_for_date(
    target_date: str,
    leagues: list[str] | None = None,
    seasons: list[int] | None = None,
) -> int:
    """
    仅拉取并入库指定比赛日（YYYY-MM-DD）的场次，用于复盘前补全该日数据。
    返回本次写入/更新的场次数。
    """
    if not config.FOOTBALL_DATA_API_KEY:
        logger.warning("未配置 FOOTBALL_DATA_API_KEY，跳过")
        return 0
    date_prefix = (target_date or "")[:10]
    if len(date_prefix) != 10:
        return 0
    init_db()
    engine = get_engine()
    leagues = leagues or config.FOOTBALL_DATA_BIG5
    if seasons is None:
        try:
            y, m = int(date_prefix[:4]), int(date_prefix[5:7])
            season_year = y if m >= 8 else y - 1
            seasons = [season_year, season_year - 1]
        except (ValueError, IndexError):
            seasons = [2024, 2025]
    count = 0
    with Session(engine) as session:
        for code in leagues:
            for year in seasons:
                try:
                    time.sleep(RATE_LIMIT_SEC)
                    data = _get_fd(f"competitions/{code}/matches?season={year}")
                except Exception as e:
                    logger.warning("拉取 %s %s 失败: %s", code, year, e)
                    continue
                comp_node = data.get("competition") or {}
                area_node = data.get("area") or {}
                comp_id_fd = comp_node.get("id") or 0
                comp_name = comp_node.get("name") or code
                area_name = area_node.get("name") or ""
                our_comp_id, our_season_id = _ensure_comp_season(
                    session, comp_id_fd, comp_name, area_name, year, code, leagues
                )
                matches = data.get("matches") or []
                for m in matches:
                    utc = (m.get("utcDate") or "")[:10]
                    if utc != date_prefix:
                        continue
                    fd_id = m.get("id")
                    source_id = f"fd_{fd_id}"
                    status = m.get("status")
                    home = m.get("homeTeam") or {}
                    away = m.get("awayTeam") or {}
                    home_id_fd = home.get("id")
                    away_id_fd = away.get("id")
                    home_name = home.get("name") or "Home"
                    away_name = away.get("name") or "Away"
                    if home_id_fd is None or away_id_fd is None:
                        continue
                    _ensure_team(session, home_id_fd, home_name)
                    _ensure_team(session, away_id_fd, away_name)
                    home_id = FD_TEAM_OFFSET + home_id_fd
                    away_id = FD_TEAM_OFFSET + away_id_fd
                    existing = session.query(Match).filter(Match.source_id == source_id).first()
                    if existing:
                        if status == "FINISHED" and existing.home_score is None:
                            score = m.get("score") or {}
                            ft = score.get("fullTime") or {}
                            ht = score.get("halfTime") or {}
                            home_score = ft.get("home") if ft.get("home") is not None else 0
                            away_score = ft.get("away") if ft.get("away") is not None else 0
                            raw_meta_dict = {
                                "home_team_name": home_name,
                                "away_team_name": away_name,
                                "competition_name": comp_name,
                                "season_name": f"{year}/{year+1}",
                            }
                            if ht.get("home") is not None or ht.get("away") is not None:
                                raw_meta_dict["half_time_home"] = ht.get("home")
                                raw_meta_dict["half_time_away"] = ht.get("away")
                            existing.home_score = home_score
                            existing.away_score = away_score
                            existing.raw_meta = json.dumps(raw_meta_dict)
                            session.flush()
                            match_db_id = existing.id
                            for i, g in enumerate(m.get("goals") or []):
                                team_fd = g.get("team") or {}
                                team_id_fd = team_fd.get("id")
                                team_name = team_fd.get("name") or ""
                                scorer = g.get("scorer") or {}
                                player_name = scorer.get("name") or "?"
                                minute = g.get("minute") or 0
                                team_our_id = FD_TEAM_OFFSET + team_id_fd if team_id_fd is not None else None
                                ev = Event(
                                    match_id=match_db_id,
                                    source_event_id=f"fd_goal_{fd_id}_{i}",
                                    index=i,
                                    period=1,
                                    minute=minute if isinstance(minute, int) else None,
                                    second=0,
                                    event_type="Shot",
                                    team_id=team_our_id,
                                    team_name=team_name,
                                    player_id=scorer.get("id"),
                                    player_name=player_name,
                                    position_name=None,
                                    location_x=None,
                                    location_y=None,
                                    end_location_x=None,
                                    end_location_y=None,
                                    duration=None,
                                    under_pressure=False,
                                    outcome="Goal",
                                    outcome_id=None,
                                    raw_type_id=None,
                                )
                                session.add(ev)
                            session.commit()
                            count += 1
                            logger.info("FD: updated %s %s %s vs %s %s-%s", comp_name, year, home_name, away_name, home_score, away_score)
                    elif status == "FINISHED":
                        score = m.get("score") or {}
                        ft = score.get("fullTime") or {}
                        ht = score.get("halfTime") or {}
                        home_score = ft.get("home")
                        away_score = ft.get("away")
                        if home_score is None:
                            home_score = 0
                        if away_score is None:
                            away_score = 0
                        raw_meta_dict = {
                            "home_team_name": home_name,
                            "away_team_name": away_name,
                            "competition_name": comp_name,
                            "season_name": f"{year}/{year+1}",
                        }
                        if ht.get("home") is not None or ht.get("away") is not None:
                            raw_meta_dict["half_time_home"] = ht.get("home")
                            raw_meta_dict["half_time_away"] = ht.get("away")
                        match_row = Match(
                            competition_id=our_comp_id,
                            season_id=our_season_id,
                            match_date=utc,
                            home_team_id=home_id,
                            away_team_id=away_id,
                            home_score=home_score,
                            away_score=away_score,
                            match_week=m.get("matchday"),
                            source_id=source_id,
                            raw_meta=json.dumps(raw_meta_dict),
                        )
                        session.add(match_row)
                        session.flush()
                        match_db_id = match_row.id
                        for i, g in enumerate(m.get("goals") or []):
                            team_fd = g.get("team") or {}
                            team_id_fd = team_fd.get("id")
                            team_name = team_fd.get("name") or ""
                            scorer = g.get("scorer") or {}
                            player_name = scorer.get("name") or "?"
                            minute = g.get("minute") or 0
                            team_our_id = FD_TEAM_OFFSET + team_id_fd if team_id_fd is not None else None
                            ev = Event(
                                match_id=match_db_id,
                                source_event_id=f"fd_goal_{fd_id}_{i}",
                                index=i,
                                period=1,
                                minute=minute if isinstance(minute, int) else None,
                                second=0,
                                event_type="Shot",
                                team_id=team_our_id,
                                team_name=team_name,
                                player_id=scorer.get("id"),
                                player_name=player_name,
                                position_name=None,
                                location_x=None,
                                location_y=None,
                                end_location_x=None,
                                end_location_y=None,
                                duration=None,
                                under_pressure=False,
                                outcome="Goal",
                                outcome_id=None,
                                raw_type_id=None,
                            )
                            session.add(ev)
                        session.commit()
                        count += 1
                        logger.info("FD: %s %s %s vs %s %s-%s", comp_name, year, home_name, away_name, home_score, away_score)
                    elif (not finished_only) and status in ("SCHEDULED", "TIMED"):
                        match_row = Match(
                            competition_id=our_comp_id,
                            season_id=our_season_id,
                            match_date=utc,
                            home_team_id=home_id,
                            away_team_id=away_id,
                            home_score=None,
                            away_score=None,
                            match_week=m.get("matchday"),
                            source_id=source_id,
                            raw_meta=json.dumps({
                                "home_team_name": home_name,
                                "away_team_name": away_name,
                                "competition_name": comp_name,
                                "season_name": f"{year}/{year+1}",
                            }),
                        )
                        session.add(match_row)
                        session.commit()
                        count += 1
                        logger.info("FD: %s %s %s vs %s 待赛", comp_name, year, home_name, away_name)
    logger.info("ingest for date %s done, count=%s", target_date, count)
    return count


def reset_fd_data() -> dict[str, int]:
    """
    清空 football-data.org 相关数据（fd_* 场次及其进球事件、FD offset 的队伍/赛事/赛季）。
    返回删除条数统计。
    """
    init_db()
    engine = get_engine()
    with Session(engine) as session:
        fd_match_ids = [r[0] for r in session.query(Match.id).filter(Match.source_id.like("fd_%")).all()]
        events_deleted = 0
        matches_deleted = 0
        if fd_match_ids:
            events_deleted = session.query(Event).filter(Event.match_id.in_(fd_match_ids)).delete(synchronize_session=False)
            matches_deleted = session.query(Match).filter(Match.id.in_(fd_match_ids)).delete(synchronize_session=False)
        seasons_deleted = session.query(Season).filter(Season.id >= FD_SEASON_OFFSET).delete(synchronize_session=False)
        competitions_deleted = session.query(Competition).filter(Competition.id >= FD_COMP_OFFSET).delete(synchronize_session=False)
        teams_deleted = session.query(Team).filter(Team.id >= FD_TEAM_OFFSET).delete(synchronize_session=False)
        session.commit()
    return {
        "events": events_deleted,
        "matches": matches_deleted,
        "seasons": seasons_deleted,
        "competitions": competitions_deleted,
        "teams": teams_deleted,
    }
