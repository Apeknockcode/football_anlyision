"""
API-FOOTBALL 单场统计拉取：按 fixture 或按日期拉取比赛统计（射门、控球等），写入 data/api_football_statistics.json。
可用于复盘分析或后续作为预测特征（如近期场均射门、控球）。
统计类型命名与 Opta 定义对齐，详见 docs/参考Opta定义与指标.md。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

import config
from etl.injury_api_football import LEAGUE_IDS, _request

STATISTICS_JSON_PATH: Path = config.DATA_DIR / "api_football_statistics.json"
RATE_LIMIT_SEC = getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)


def fetch_statistics_for_fixture(fixture_id: int, rate_limit_sec: float = RATE_LIMIT_SEC) -> list[dict[str, Any]]:
    """
    拉取单场两队的统计（射门、控球等）。
    返回 [{ "team_id", "team_name", "statistics": [ {"type": "Shots on Goal", "value": "12"}, ... ] }, ...]。
    """
    data = _request("fixtures/statistics", {"fixture": fixture_id})
    time.sleep(rate_limit_sec)
    rows = data.get("response") or []
    return list(rows)


def _extract_stat_value(team_block: dict, stat_type: str) -> int | float | None:
    """从单队统计块中取出指定类型的数值。"""
    for s in (team_block.get("statistics") or []):
        if (s.get("type") or "").strip() == stat_type:
            v = s.get("value")
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return v
            try:
                return int(str(v).strip())
            except ValueError:
                try:
                    return float(str(v).strip())
                except ValueError:
                    return None
    return None


# 与 Opta 定义对齐的统计类型名（见 docs/参考Opta定义与指标.md）
STAT_TYPE_XG = "expected_goals"  # Expected Goals：机会质量加总，0~1/脚，单场为和
STAT_TYPE_SHOTS_ON_GOAL = "Shots on Goal"  # 射正（含被扑、被最后一线封堵）
STAT_TYPE_TOTAL_SHOTS = "Total Shots"  # 总射门
STAT_TYPE_BALL_POSSESSION = "Ball Possession"  # 控球率


def statistics_to_xg_proxy(home_block: dict, away_block: dict) -> tuple[float | None, float | None]:
    """
    主客队期望进球（供可选 xG 混合）。与 Opta xG 定义对齐时优先用 API 的 expected_goals。
    有则返回真实 xG；否则用射门代理：优先 "Shots on Goal"×系数，无则 "Total Shots"×系数。
    返回 (home_xg, away_xg) 或 (None, None)，单位均为「预期进球」量级。
    """
    h_xg = _extract_stat_value(home_block, STAT_TYPE_XG)
    a_xg = _extract_stat_value(away_block, STAT_TYPE_XG)
    if h_xg is not None and a_xg is not None:
        return (float(h_xg), float(a_xg))
    # 回退：射门/射正换算为近似 xG 量级
    h_son = _extract_stat_value(home_block, STAT_TYPE_SHOTS_ON_GOAL)
    a_son = _extract_stat_value(away_block, STAT_TYPE_SHOTS_ON_GOAL)
    if h_son is not None and a_son is not None:
        return (float(h_son) * XG_PROXY_SHOTS_ON_GOAL_FACTOR, float(a_son) * XG_PROXY_SHOTS_ON_GOAL_FACTOR)
    h_tot = _extract_stat_value(home_block, STAT_TYPE_TOTAL_SHOTS)
    a_tot = _extract_stat_value(away_block, STAT_TYPE_TOTAL_SHOTS)
    if h_tot is not None and a_tot is not None:
        return (float(h_tot) * XG_PROXY_TOTAL_SHOTS_FACTOR, float(a_tot) * XG_PROXY_TOTAL_SHOTS_FACTOR)
    return (None, None)


def load_statistics() -> dict[int, list[dict[str, Any]]]:
    """从 data/api_football_statistics.json 读取，返回 fixture_id -> list of team stats。"""
    if not STATISTICS_JSON_PATH.is_file():
        return {}
    try:
        data = json.loads(STATISTICS_JSON_PATH.read_text(encoding="utf-8"))
        raw = data.get("by_fixture") or {}
        return {int(k): v for k, v in raw.items() if str(k).isdigit() and isinstance(v, list)}
    except Exception:
        return {}


# 射门/射正换算为近似期望进球的系数（单场 Shots on Goal 粗算 xG 约 0.30～0.35/次）
XG_PROXY_SHOTS_ON_GOAL_FACTOR = 0.32
XG_PROXY_TOTAL_SHOTS_FACTOR = 0.12


def team_recent_xg_proxy_from_matches(
    matches: list[dict[str, Any]],
    team_id: int,
    shots_on_goal_factor: float = XG_PROXY_SHOTS_ON_GOAL_FACTOR,
    total_shots_factor: float = XG_PROXY_TOTAL_SHOTS_FACTOR,
) -> float | None:
    """
    根据该队近期场次算近期场均 xG（与 Opta 定义对齐）。优先使用 API 的 expected_goals（机会质量加总），
    无则回退到射门/射正×系数作为代理。用于与形式期望进球混合。
    matches: 来自 repo.predict_team_recent_matches_with_source，每项含 source_id, home_team_id, away_team_id。
    返回场均 xG，无数据则 None。
    """
    if not matches:
        return None
    by_fixture = dict(load_statistics())
    missing_fids = []
    for m in matches:
        sid = m.get("source_id") or ""
        if not isinstance(sid, str) or not sid.startswith("af_"):
            continue
        try:
            fid = int(sid.replace("af_", "").strip())
        except ValueError:
            continue
        if fid not in by_fixture:
            missing_fids.append(fid)
    if missing_fids:
        try:
            from db.self_improvement import load_api_football_statistics_from_db
            mongo = load_api_football_statistics_from_db(missing_fids)
            for fid, teams in mongo.items():
                if teams and fid not in by_fixture:
                    by_fixture[fid] = teams
        except Exception:
            pass
    values: list[float] = []
    for m in matches:
        sid = m.get("source_id") or ""
        if not isinstance(sid, str) or not sid.startswith("af_"):
            continue
        try:
            fid = int(sid.replace("af_", "").strip())
        except ValueError:
            continue
        blocks = by_fixture.get(fid)
        if not blocks or len(blocks) < 2:
            continue
        is_home = (m.get("home_team_id")) == team_id
        block = blocks[0] if is_home else blocks[1]
        # 优先使用 API 的 expected_goals（与 Opta xG 一致）
        v = _extract_stat_value(block, STAT_TYPE_XG)
        if v is not None:
            values.append(float(v))
            continue
        v = _extract_stat_value(block, STAT_TYPE_SHOTS_ON_GOAL)
        if v is not None:
            values.append(float(v) * shots_on_goal_factor)
            continue
        v = _extract_stat_value(block, STAT_TYPE_TOTAL_SHOTS)
        if v is not None:
            values.append(float(v) * total_shots_factor)
    if not values:
        return None
    return sum(values) / len(values)


def fetch_and_save_statistics_for_date(
    date_str: str,
    rate_limit_sec: float = RATE_LIMIT_SEC,
    merge: bool = True,
    max_fixtures: int = 30,
) -> tuple[int, str]:
    """
    拉取指定日期五大联赛场次的统计并保存（仅已完赛场次通常有数据）。
    返回 (拉取场次数, message)。
    """
    from etl.api_football_odds import _fixtures_with_id
    date_only = (date_str or "")[:10]
    if len(date_only) != 10:
        return 0, "日期格式需为 YYYY-MM-DD"
    logger.info("按日期拉取五大联赛赛程: date=%s", date_only)
    fixtures = _fixtures_with_id(date_only)[:max_fixtures]
    logger.info("该日五大联赛共 %d 场，开始逐场拉取统计", len(fixtures))
    by_fixture: dict[int, list[dict[str, Any]]] = {}
    for f in fixtures:
        fid = f.get("fixture_id")
        if not fid:
            continue
        rows = fetch_statistics_for_fixture(fid, rate_limit_sec)
        if rows:
            by_fixture[fid] = rows
    save_statistics(by_fixture, merge=merge)
    logger.info("已保存至 %s，本批 %d 场有数据", STATISTICS_JSON_PATH, len(by_fixture))
    msg = f"统计已更新：{date_only} 共 {len(by_fixture)} 场有数据，已保存至 {STATISTICS_JSON_PATH}"
    return len(by_fixture), msg


def _existing_fixture_ids(candidate_ids: list[int] | None) -> set[int]:
    """返回已有统计的 fixture_id 集合（JSON + 可选 Mongo），用于断点续跑时跳过。"""
    existing: set[int] = set(load_statistics().keys())
    try:
        from db.self_improvement import load_api_football_statistics_from_db
        mongo = load_api_football_statistics_from_db(candidate_ids)
        existing |= set(mongo.keys())
    except Exception:
        pass
    return existing


def fetch_and_save_statistics_for_jingcai_years(
    years: list[int],
    rate_limit_sec: float = RATE_LIMIT_SEC,
    merge: bool = True,
    save_batch_size: int = 50,
    resume: bool = False,
) -> tuple[int, int, str]:
    """
    从 jingcai_match_analysis 中取指定年份内、已有 api_football_fixture_id 的场次，
    逐场拉取 API-FOOTBALL 单场统计（射门、控球、角球等），每 save_batch_size 场写入一次 JSON + Mongo，最后再写一次。
    resume=True 时跳过 JSON/Mongo 中已存在的 fixture_id。
    返回 (总场次数, 有数据的场次数, message)。
    """
    from db.self_improvement import jc_match_analysis_fixture_ids_for_years

    fixture_ids = jc_match_analysis_fixture_ids_for_years(years)
    if not fixture_ids:
        return 0, 0, f"jingcai_match_analysis 中 {years} 年无带 api_football_fixture_id 的场次"

    total_planned = len(fixture_ids)
    if resume:
        existing = _existing_fixture_ids(fixture_ids)
        fixture_ids = [f for f in fixture_ids if f not in existing]
        skipped = total_planned - len(fixture_ids)
        if skipped > 0:
            logger.info("断点续跑：跳过已存在 %d 场，剩余 %d 场待拉取", skipped, len(fixture_ids))
            print(f"断点续跑：跳过已存在 {skipped} 场，剩余 {len(fixture_ids)} 场待拉取", flush=True)
        if not fixture_ids:
            msg = f"竞彩场次统计已完整：{years} 年共 {total_planned} 场均已存在，无需拉取"
            return total_planned, len(existing), msg

    logger.info(
        "开始逐场拉取 API 统计，共 %d 场，rate_limit_sec=%.2f，每 %d 场写入一次",
        len(fixture_ids), rate_limit_sec, save_batch_size,
    )
    by_fixture: dict[int, list[dict[str, Any]]] = {}
    for i, fid in enumerate(fixture_ids):
        try:
            rows = fetch_statistics_for_fixture(fid, rate_limit_sec)
        except ValueError as e:
            err_msg = str(e)
            if "request limit" in err_msg or "套餐" in err_msg or "计划限制" in err_msg or "超限" in err_msg:
                if by_fixture:
                    save_statistics(by_fixture, merge=merge)
                    logger.info("触达 API 限额，已保存当前进度 %d 场有统计", len(by_fixture))
                    print("触达 API 每日请求上限，已保存当前进度。", flush=True)
                msg = (
                    f"触达 API-FOOTBALL 每日请求上限（已处理 {i + 1}/{len(fixture_ids)}，本批写入 {len(by_fixture)} 场有统计）。"
                    "明日请用相同命令加 --resume 续跑，将自动跳过已保存的场次。"
                )
                return total_planned, len(by_fixture), msg
            raise
        if rows:
            by_fixture[fid] = rows
        n_done = i + 1
        # 进度打印（每 10 场或结束时）
        if n_done % 10 == 0 or n_done == len(fixture_ids):
            logger.info("进度 [%d/%d] 已拉取 %d 场有统计", n_done, len(fixture_ids), len(by_fixture))
            print(f"  [{n_done}/{len(fixture_ids)}] 已拉取 {len(by_fixture)} 场有统计", flush=True)
        # 每 save_batch_size 场增量写入一次（按处理场数，不按有数据的场数）
        if n_done % save_batch_size == 0 and by_fixture:
            save_statistics(by_fixture, merge=merge)
            logger.info("增量写入: 已处理 %d 场，当前共 %d 场有统计已写入 JSON + Mongo", n_done, len(by_fixture))
            print(f"增量写入: 已处理 {n_done} 场，已写入 {len(by_fixture)} 场（JSON + Mongo）", flush=True)

    # 最后再写一次，确保末批（不足 save_batch_size 的）也写入
    if by_fixture:
        save_statistics(by_fixture, merge=merge)
        logger.info("保存完成: 本批共 %d 场有数据", len(by_fixture))
    msg = (
        f"竞彩场次统计已更新：{years} 年共 {total_planned} 场，本 run 拉取 {len(by_fixture)} 场有数据，"
        f"已保存至 {STATISTICS_JSON_PATH} 与 self_improvement_football.api_football_statistics"
    )
    return total_planned, len(by_fixture), msg


def statistics_missing_by_year(years: list[int] | None = None) -> dict[str, Any]:
    """
    统计「竞彩库中已有 api_football_fixture_id 但缺单场统计」的按年情况。
    years: 仅统计这些年份；None 表示全部年份。
    返回:
      by_year: { "2025": { "with_fixture_id", "has_statistics", "missing" }, ... }
      total_with_fixture_id, total_has_statistics, total_missing
      years_missing: 有缺失的年份列表（missing > 0）
    """
    from db.self_improvement import jc_match_analysis_fixture_ids_by_year

    by_year_fids = jc_match_analysis_fixture_ids_by_year(years)
    if not by_year_fids:
        return {
            "by_year": {},
            "total_with_fixture_id": 0,
            "total_has_statistics": 0,
            "total_missing": 0,
            "years_missing": [],
        }
    all_fids = [fid for fids in by_year_fids.values() for fid in fids]
    existing = _existing_fixture_ids(all_fids)
    by_year: dict[str, dict[str, int]] = {}
    total_with, total_has, total_missing = 0, 0, 0
    years_missing: list[str] = []
    for y, fids in sorted(by_year_fids.items()):
        with_cnt = len(fids)
        has_cnt = sum(1 for f in fids if f in existing)
        missing_cnt = with_cnt - has_cnt
        by_year[y] = {"with_fixture_id": with_cnt, "has_statistics": has_cnt, "missing": missing_cnt}
        total_with += with_cnt
        total_has += has_cnt
        total_missing += missing_cnt
        if missing_cnt > 0:
            years_missing.append(y)
    return {
        "by_year": by_year,
        "total_with_fixture_id": total_with,
        "total_has_statistics": total_has,
        "total_missing": total_missing,
        "years_missing": years_missing,
    }


def save_statistics(by_fixture: dict[int, list[dict[str, Any]]], merge: bool = True) -> Path:
    """写入 JSON；若配置了 MONGO_URI，同时写入 self_improvement_football.api_football_statistics。merge=True 时与现有文件合并。"""
    existing = {}
    if merge and STATISTICS_JSON_PATH.is_file():
        try:
            data = json.loads(STATISTICS_JSON_PATH.read_text(encoding="utf-8"))
            raw = data.get("by_fixture") or {}
            existing = {int(k): v for k, v in raw.items() if str(k).isdigit() and isinstance(v, list)}
        except Exception:
            pass
    for fid, rows in by_fixture.items():
        existing[fid] = rows
    out = {
        "updated_at": datetime.now().isoformat(),
        "source": "api_football",
        "by_fixture": {str(k): v for k, v in existing.items()},
    }
    STATISTICS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATISTICS_JSON_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    # 同时写入 self_improvement_football 库（本批 by_fixture 逐条 upsert）
    try:
        from db.self_improvement import save_api_football_statistics_bulk
        n_db = save_api_football_statistics_bulk(by_fixture)
        if n_db > 0:
            logger.info("已写入 self_improvement_football.api_football_statistics：%d 场", n_db)
            print(f"已写入 self_improvement_football.api_football_statistics：{n_db} 场", flush=True)
    except Exception as e:
        logger.warning("写入 MongoDB 单场统计失败（已保存至 JSON）：%s", e)
        print(f"警告: 写入 MongoDB 失败（已保存至 JSON）: {e}", flush=True)
    return STATISTICS_JSON_PATH
