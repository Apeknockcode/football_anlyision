"""
API-FOOTBALL 阵容拉取：按 fixture_id 拉取首发人数与完整阵容（首发+替补），落库供展示与后续关键球员等用。
"""
from __future__ import annotations

import time
from typing import Any

import config
from etl.injury_api_football import _request

RATE_LIMIT_SEC = getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)


def _parse_players(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 API 的 startXI/substitutes 项转为 [{player_id, player_name, number}, ...]。"""
    out = []
    for item in entries or []:
        player = item.get("player") or {}
        pid = player.get("id")
        name = (player.get("name") or "").strip()
        num = item.get("number") or item.get("jersey_number")
        if num is not None and not isinstance(num, int):
            try:
                num = int(num)
            except (TypeError, ValueError):
                num = None
        out.append({
            "player_id": pid,
            "player_name": name or None,
            "number": num,
        })
    return out


def fetch_lineups_for_fixture(
    fixture_id: int,
    rate_limit_sec: float = RATE_LIMIT_SEC,
    *,
    full: bool = False,
) -> dict[str, Any]:
    """
    拉取单场两队阵容。
    - full=False（默认）：仅返回 home_starting_count, away_starting_count，兼容现有展示。
    - full=True：额外返回 home_starting, away_starting, home_substitutes, away_substitutes（完整名单），用于落库。
    """
    try:
        data = _request("fixtures/lineups", {"fixture": int(fixture_id)})
        time.sleep(rate_limit_sec)
    except Exception as e:
        msg = str(e).lower()
        # 请求次数/套餐限流：向上抛出，便于调用方终止进程并提示断点续拉
        if "limit" in msg or "超限" in str(e) or "套餐" in str(e) or "计划" in str(e) or "request limit" in msg:
            raise
        return {"home_starting_count": None, "away_starting_count": None}
    rows = data.get("response") or []
    if not rows:
        return {"home_starting_count": None, "away_starting_count": None}
    # API 返回顺序一般为 [主队, 客队]
    home_row = rows[0] if len(rows) > 0 else {}
    away_row = rows[1] if len(rows) > 1 else {}
    home_xi = home_row.get("startXI") or []
    away_xi = away_row.get("startXI") or []
    home_count = len(home_xi) if home_xi else None
    away_count = len(away_xi) if away_xi else None
    result = {
        "home_starting_count": home_count if home_count and home_count > 0 else None,
        "away_starting_count": away_count if away_count and away_count > 0 else None,
    }
    if full:
        result["home_starting"] = _parse_players(home_xi)
        result["away_starting"] = _parse_players(away_xi)
        result["home_substitutes"] = _parse_players(home_row.get("substitutes") or [])
        result["away_substitutes"] = _parse_players(away_row.get("substitutes") or [])
    return result
