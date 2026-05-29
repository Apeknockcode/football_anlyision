"""
API-FOOTBALL 单场事件拉取：按 fixture_id 拉取 fixtures/events（进球、射门、红黄牌、换人等），
供复盘、xG 汇总或后续预测特征使用。竞彩场次通过 jingcai_match_analysis.api_football_fixture_id 关联。
"""
from __future__ import annotations

import time
from typing import Any

import config
from etl.injury_api_football import _request

RATE_LIMIT_SEC = getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)


def fetch_events_for_fixture(
    fixture_id: int,
    rate_limit_sec: float = RATE_LIMIT_SEC,
) -> list[dict[str, Any]]:
    """
    拉取单场事件列表（进球、黄红牌、换人、射门等）。
    返回 API response 中的 response 数组；请求失败或空返回 []。
    """
    try:
        data = _request("fixtures/events", {"fixture": int(fixture_id)})
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
