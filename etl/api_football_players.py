"""
API-FOOTBALL 球员数据拉取：
1. players/statistics（按 fixture）— 单场球员级统计（射门、xG、传球等），用于关键球员近期状态。
2. players/topscorers — 联赛射手榜（前 N 名），用于关键球员名单 / 伤停加权。
竞彩场次通过 jingcai_match_analysis.api_football_fixture_id 关联。
"""
from __future__ import annotations

import time
from typing import Any

import config
from etl.injury_api_football import _request

RATE_LIMIT_SEC = getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)


def fetch_player_statistics_for_fixture(
    fixture_id: int,
    rate_limit_sec: float = RATE_LIMIT_SEC,
) -> list[dict[str, Any]]:
    """
    拉取单场球员统计（每队每位球员的射门、xG、传球等）。
    API 路径: fixtures/players，参数: fixture=<id>（api-sports v3）。
    返回 API response 中的 response 数组；请求失败或空返回 []。
    """
    try:
        data = _request("fixtures/players", {"fixture": int(fixture_id)})
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


def fetch_topscorers(
    league_id: int,
    season: int,
    rate_limit_sec: float = RATE_LIMIT_SEC,
) -> list[dict[str, Any]]:
    """
    拉取指定联赛+赛季的射手榜（前 N 名）。
    API 路径: players/topscorers，参数: league=<id>, season=<年>。
    返回 API response 中的 response 数组；请求失败或空返回 []。
    """
    try:
        data = _request("players/topscorers", {"league": int(league_id), "season": int(season)})
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
