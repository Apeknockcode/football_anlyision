"""
API-FOOTBALL 球队与教练数据拉取：
1. teams — 联赛+赛季下的球队列表（用于获取 team_id，供 statistics/squads/coaches 使用）。
2. teams/statistics — 球队赛季统计（场均进球、控球、射门等），适合做赛季风格或样本少时先验。
3. players/squads — 球队当前阵容名单，配合伤停/lineups 做「谁常首发、谁缺阵」。
4. coaches — 教练信息、任期，可做换帅/稳定性粗特征。
"""
from __future__ import annotations

import time
from typing import Any

import config
from etl.injury_api_football import _request

RATE_LIMIT_SEC = getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)


def fetch_leagues(
    season: int | None = None,
    current_only: bool = True,
    league_type: str | None = "League",
    require_coverage: str | None = None,
    rate_limit_sec: float = RATE_LIMIT_SEC,
) -> list[dict[str, Any]]:
    """
    拉取联赛列表（用于「全部联赛」时获取 league_id 列表）。
    current_only=True 时请求 leagues?current=true；否则可传 season= 请求 leagues?season=。
    league_type: 只保留该类型的赛事，如 "League" 只保留联赛（排除 Cup 等）；传 None 则不过滤。
    require_coverage: 只保留 API 标明支持该 coverage 的联赛，如 "topscorers" 只保留有射手榜数据的联赛；传 None 则不过滤。
    返回 response 中每条的 league 信息列表 [{ id, name, country, type, ... }, ...]。
    """
    try:
        params: dict[str, Any] = {}
        if current_only:
            params["current"] = "true"
        elif season is not None:
            params["season"] = int(season)
        data = _request("leagues", params if params else None)
        time.sleep(rate_limit_sec)
    except Exception as e:
        msg = str(e).lower()
        if "limit" in msg or "超限" in str(e) or "套餐" in str(e) or "计划" in str(e) or "request limit" in msg:
            raise
        return []
    rows = data.get("response")
    if not isinstance(rows, list):
        return []
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        league = item.get("league")
        if not league or not isinstance(league, dict):
            continue
        if league_type is not None:
            if (league.get("type") or "").strip() != league_type:
                continue
        if require_coverage:
            seasons_list = item.get("seasons")
            if isinstance(seasons_list, list) and seasons_list:
                # 取当前请求的 season 或第一个 season 的 coverage
                target = None
                for s in seasons_list:
                    if isinstance(s, dict) and season is not None and s.get("year") == season:
                        target = s
                        break
                if target is None:
                    target = seasons_list[0] if isinstance(seasons_list[0], dict) else None
                cov = target.get("coverage") if target else None
                if not isinstance(cov, dict) or not cov.get(require_coverage):
                    continue
            else:
                continue
        out.append(dict(league))
    return out


def fetch_teams(
    league_id: int,
    season: int,
    rate_limit_sec: float = RATE_LIMIT_SEC,
) -> list[dict[str, Any]]:
    """
    拉取某联赛某赛季的球队列表（用于拿到 team_id，供 statistics/squads/coaches 调用）。
    API 路径: teams，参数: league=<id>, season=<年>。
    返回 response 中每项的 team 列表展平；请求失败或空返回 []。
    """
    try:
        data = _request("teams", {"league": int(league_id), "season": int(season)})
        time.sleep(rate_limit_sec)
    except Exception as e:
        msg = str(e).lower()
        if "limit" in msg or "超限" in str(e) or "套餐" in str(e) or "计划" in str(e) or "request limit" in msg:
            raise
        return []
    rows = data.get("response")
    if not isinstance(rows, list):
        return []
    # response 通常为 [ { team: { id, name, ... }, ... } ]
    out = []
    for item in rows:
        team = item.get("team") if isinstance(item, dict) else None
        if team and isinstance(team, dict):
            out.append(dict(team))
    return out


def fetch_team_statistics(
    league_id: int,
    team_id: int,
    season: int,
    rate_limit_sec: float = RATE_LIMIT_SEC,
) -> dict[str, Any] | None:
    """
    拉取某队在某联赛某赛季的统计（场均进球、控球、射门等）。
    API 路径: teams/statistics，参数: league=, team=, season=。
    返回单条统计对象；请求失败或空返回 None。
    """
    try:
        data = _request(
            "teams/statistics",
            {"league": int(league_id), "team": int(team_id), "season": int(season)},
        )
        time.sleep(rate_limit_sec)
    except Exception as e:
        msg = str(e).lower()
        if "limit" in msg or "超限" in str(e) or "套餐" in str(e) or "计划" in str(e) or "request limit" in msg:
            raise
        return None
    rows = data.get("response")
    if isinstance(rows, list) and len(rows) > 0 and isinstance(rows[0], dict):
        return dict(rows[0])
    if isinstance(rows, dict):
        return dict(rows)
    return None


def fetch_squads(
    team_id: int,
    rate_limit_sec: float = RATE_LIMIT_SEC,
) -> list[dict[str, Any]]:
    """
    拉取某队当前阵容名单（球员 id、姓名、位置等）。
    API 路径: players/squads，参数: team=<id>。
    返回 response 数组；请求失败或空返回 []。
    """
    try:
        data = _request("players/squads", {"team": int(team_id)})
        time.sleep(rate_limit_sec)
    except Exception as e:
        msg = str(e).lower()
        if "limit" in msg or "超限" in str(e) or "套餐" in str(e) or "计划" in str(e) or "request limit" in msg:
            raise
        return []
    rows = data.get("response")
    if not isinstance(rows, list):
        return []
    # API 返回 [ { team: {...}, players: [ {...}, ... ] } ]，单队时仅一条
    out = []
    for item in rows:
        if isinstance(item, dict):
            players = item.get("players")
            if isinstance(players, list):
                out.extend(players)
            else:
                # 无 players 键时当作单条球员对象
                if item.get("id") or item.get("name"):
                    out.append(item)
    return [x for x in out if isinstance(x, dict)]


def fetch_coach(
    team_id: int,
    rate_limit_sec: float = RATE_LIMIT_SEC,
) -> list[dict[str, Any]]:
    """
    拉取某队当前教练信息（可含任期、生涯等）。
    API 路径: coaches，参数: team=<id>。
    返回 response 数组（可能多条历史）；请求失败或空返回 []。
    """
    try:
        # API-FOOTBALL v3 使用的是 "coachs"（非 "coaches"）
        data = _request("coachs", {"team": int(team_id)})
        time.sleep(rate_limit_sec)
    except Exception as e:
        msg = str(e).lower()
        if "limit" in msg or "超限" in str(e) or "套餐" in str(e) or "计划" in str(e) or "request limit" in msg:
            raise
        return []
    rows = data.get("response")
    if not isinstance(rows, list):
        return []
    return list(rows)
