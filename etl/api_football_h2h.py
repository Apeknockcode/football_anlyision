"""
API-FOOTBALL 历史交锋（Head to Head）：拉取两队的过往对阵并写入 matches，
使「历史交锋」展示与模型 predict_head_to_head 有更多数据。
"""
from __future__ import annotations

import json
import time
from typing import Any

import config
from etl.injury_api_football import _request

# 与 api_football_match_ingest 一致
AF_TEAM_OFFSET = 90000
AF_COMP_OFFSET = 6000
AF_SEASON_OFFSET = 9000
LEAGUE_IDS = {39: "Premier League", 140: "La Liga", 135: "Serie A", 78: "Bundesliga", 61: "Ligue 1"}
LEAGUE_NAME_BY_ID = {
    39: "Premier League",
    140: "La Liga",
    135: "Serie A",
    78: "Bundesliga",
    61: "Ligue 1",
}


def _our_team_id_to_api(our_id: int) -> int | None:
    """内部 team_id 转 API-Football team_id；非 AF 来源返回 None。"""
    if our_id is None or our_id < AF_TEAM_OFFSET:
        return None
    return int(our_id - AF_TEAM_OFFSET)


def _competition_id_to_league_id(competition_id: int) -> int | None:
    """DB competition_id 转 API league_id（仅五大联赛）。"""
    if competition_id is None:
        return None
    lid = int(competition_id - AF_COMP_OFFSET)
    return lid if lid in LEAGUE_IDS else None


def fetch_h2h_fixtures(
    api_team_id_1: int,
    api_team_id_2: int,
    league_id: int | None = None,
    season: int | None = None,
) -> list[dict[str, Any]]:
    """
    调用 API-Football fixtures/headtohead，返回已完赛的 fixture 列表（与 ingest 同结构便于复用）。
    league_id / season 可选，用于过滤同一联赛/赛季。
    """
    params: dict[str, Any] = {"h2h": f"{int(api_team_id_1)}-{int(api_team_id_2)}"}
    if league_id is not None:
        params["league"] = league_id
    if season is not None:
        params["season"] = season
    data = _request("fixtures/headtohead", params)
    items = data.get("response") or []
    out = []
    for item in items:
        fixture = item.get("fixture") or {}
        status = fixture.get("status") or {}
        short = (status.get("short") or "").upper()
        if short not in ("FT", "AET", "PEN"):
            continue
        teams = item.get("teams") or {}
        home_t = teams.get("home") or {}
        away_t = teams.get("away") or {}
        goals = item.get("goals") or {}
        hs, aws = goals.get("home"), goals.get("away")
        if hs is None or aws is None:
            continue
        fid = fixture.get("id")
        if not fid:
            continue
        out.append({
            "fixture_id": fid,
            "match_date": (fixture.get("date") or "")[:10],
            "home_api_id": home_t.get("id"),
            "away_api_id": away_t.get("id"),
            "home_name": (home_t.get("name") or "").strip(),
            "away_name": (away_t.get("name") or "").strip(),
            "home_score": int(hs),
            "away_score": int(aws),
            "league_id": (item.get("league") or {}).get("id"),
            "season": (item.get("league") or {}).get("season"),
            "round": (item.get("league") or {}).get("round"),
        })
    return out


def merge_h2h_into_matches(
    fixtures: list[dict[str, Any]],
    rate_limit_sec: float = 0.5,
) -> tuple[int, int]:
    """
    将 H2H 返回的 fixture 列表写入 matches（仅插入不存在的 source_id）。
    使用与 api_football_match_ingest 相同的 source_id=af_{id}、team_id=AF_TEAM_OFFSET+api_id。
    返回 (插入数, 已存在跳过数)。
    """
    from db import repo
    inserted = 0
    skipped = 0
    for f in fixtures:
        fid = f.get("fixture_id")
        if not fid:
            continue
        source_id = f"af_{int(fid)}"
        if repo.get_match_by_source_id(source_id):
            skipped += 1
            continue
        league_id = f.get("league_id")
        season_year = f.get("season")
        if league_id not in LEAGUE_IDS or season_year is None:
            skipped += 1
            continue
        comp_id = AF_COMP_OFFSET + int(league_id)
        comp_name = LEAGUE_NAME_BY_ID.get(int(league_id), f"League {league_id}")
        idx = list(LEAGUE_IDS.keys()).index(int(league_id))
        season_id = AF_SEASON_OFFSET + int(season_year) * 10 + idx
        home_api_id = f.get("home_api_id")
        away_api_id = f.get("away_api_id")
        home_name = (f.get("home_name") or "").strip()
        away_name = (f.get("away_name") or "").strip()
        if home_api_id is None or away_api_id is None:
            skipped += 1
            continue
        repo.ensure_competition(comp_id, comp_name)
        repo.ensure_season(season_id, comp_id, f"{int(season_year)}/{int(season_year) + 1}")
        home_id = repo.ensure_team_by_id(AF_TEAM_OFFSET + int(home_api_id), home_name)
        away_id = repo.ensure_team_by_id(AF_TEAM_OFFSET + int(away_api_id), away_name)
        raw_meta = json.dumps(
            {
                "home_team_name": home_name,
                "away_team_name": away_name,
                "competition_name": comp_name,
                "season_name": f"{int(season_year)}/{int(season_year) + 1}",
                "data_source": "api_football",
                "h2h_backfill": True,
            },
            ensure_ascii=False,
        )
        repo.insert_match({
            "competition_id": comp_id,
            "season_id": season_id,
            "match_date": (f.get("match_date") or "")[:10],
            "home_team_id": home_id,
            "away_team_id": away_id,
            "home_score": int(f.get("home_score", 0)),
            "away_score": int(f.get("away_score", 0)),
            "match_week": f.get("round"),
            "source_id": source_id,
            "raw_meta": raw_meta,
        })
        inserted += 1
        time.sleep(rate_limit_sec)
    return inserted, skipped
