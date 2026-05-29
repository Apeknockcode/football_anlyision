"""
独立库（默认 self_improvement_football）：存放竞彩今日比赛列表等，不修改主库 football。
使用同一 MONGO_URI，仅数据库名不同。库名可由 .env 的 MONGO_SELF_IMPROVEMENT_DB_NAME 指定（如 self_improvement_analysis）。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

import config
from db.mongo import get_mongo_client

SELF_IMPROVEMENT_DB_NAME = getattr(config, "MONGO_SELF_IMPROVEMENT_DB_NAME", "self_improvement_football")
COLLECTION_JC_MATCHES = "jc_matches"
COLLECTION_JC_MATCH_ANALYSIS = "jingcai_match_analysis"
# 兼容可能的拼写：部分环境表名为 jingcai_match_anlysis
COLLECTION_JC_MATCH_ANALYSIS_ALT = "jingcai_match_anlysis"
COLLECTION_JC_PREDICTIONS = "jc_predictions"
COLLECTION_JC_TEAMS = "jingcai_teams"
COLLECTION_JC_API_PREDICTIONS = "jc_api_predictions"
COLLECTION_API_FOOTBALL_STATISTICS = "api_football_statistics"
COLLECTION_DONGQIUDI_LINEUPS = "dongqiudi_lineups"
COLLECTION_TRANSFERMARKT_LINEUPS = "transfermarkt_lineups"
COLLECTION_MATCH_LINEUPS = "match_lineups"
COLLECTION_PRE_MATCH_LINEUPS = "pre_match_lineups"  # 赛前阵容，结构与 match_lineups 一致
COLLECTION_API_FOOTBALL_EVENTS = "api_football_events"
COLLECTION_API_FOOTBALL_PLAYER_STATISTICS = "api_football_player_statistics"
COLLECTION_API_FOOTBALL_TOPSCORERS = "api_football_topscorers"
COLLECTION_API_FOOTBALL_TEAM_STATISTICS = "api_football_team_statistics"
COLLECTION_API_FOOTBALL_SQUADS = "api_football_squads"
COLLECTION_API_FOOTBALL_COACHES = "api_football_coaches"

# 联赛中文名 -> API-Football league_id，用于从射手榜等数据按联赛补全关键球员缺阵
LEAGUE_CN_TO_API_ID: dict[str, int] = {
    "英超": 39,
    "西甲": 140,
    "意甲": 135,
    "德甲": 78,
    "法甲": 61,
}


def get_self_improvement_db():
    """获取 self_improvement_football 数据库；未配置 MONGO_URI 时返回 None。"""
    client = get_mongo_client()
    if client is None:
        return None
    return client[SELF_IMPROVEMENT_DB_NAME]


def jc_matches_save(date_str: str, matches: list[dict[str, Any]], source: str = "sporttery") -> int:
    """
    将指定日期的竞彩比赛列表写入 self_improvement_football.jc_matches。
    date_str: YYYY-MM-DD，即竞彩日（该日赛程），作为文档 date 与 jc_date 便于按日查询。
    每条比赛会带上 jc_date=竞彩日，便于后续按竞彩日使用。
    返回写入/更新的条数（1 表示成功）。
    """
    db = get_self_improvement_db()
    if db is None:
        return 0
    coll = db[COLLECTION_JC_MATCHES]
    date_only = (date_str or "").strip()[:10]
    if not date_only:
        return 0
    # 序列化：去掉不可序列化类型，统一日期格式；并为每条比赛写入竞彩日
    def _sanitize(m: dict, jc_date: str) -> dict:
        out = {}
        for k, v in m.items():
            if v is None or isinstance(v, (str, int, float, bool)):
                out[k] = v
            elif isinstance(v, dict):
                out[k] = _sanitize(v, jc_date) if k != "jc_date" else v
            elif isinstance(v, list):
                out[k] = [_sanitize(x, jc_date) if isinstance(x, dict) else x for x in v]
            elif isinstance(v, datetime):
                out[k] = v.isoformat()
            else:
                out[k] = str(v)
        out["jc_date"] = jc_date  # 统一为该列表的竞彩日
        return out
    doc = {
        "date": date_only,
        "jc_date": date_only,  # 竞彩日，与 date 一致，便于语义明确
        "matches": [_sanitize(m, date_only) for m in matches],
        "source": source,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "count": len(matches),
    }
    coll.replace_one({"date": date_only}, doc, upsert=True)
    return 1


def jc_matches_load(date_str: str) -> list[dict[str, Any]]:
    """从 self_improvement_football.jc_matches 读取指定日期的竞彩比赛列表。
    支持 date 字段为字符串 YYYY-MM-DD 或 BSON Date，避免历史数据或其它工具写入的格式不一致导致读不到。
    """
    db = get_self_improvement_db()
    if db is None:
        return []
    date_only = (date_str or "").strip()[:10]
    if not date_only:
        return []
    coll = db[COLLECTION_JC_MATCHES]
    doc = coll.find_one({"date": date_only})
    if doc is None:
        try:
            dt = datetime.strptime(date_only, "%Y-%m-%d")
            doc = coll.find_one({"date": dt})
        except ValueError:
            pass
    if not doc or "matches" not in doc:
        return []
    return list(doc.get("matches") or [])


def jc_matches_list_dates() -> list[dict[str, Any]]:
    """列出 jc_matches 中所有已保存的日期及场次数，便于排查「库里有数据但读取不到」。
    返回 [{"date": "YYYY-MM-DD", "count": n}, ...]，按 date 降序。
    """
    db = get_self_improvement_db()
    if db is None:
        return []
    out = []
    for doc in db[COLLECTION_JC_MATCHES].find({}, {"date": 1, "count": 1}):
        d = doc.get("date")
        if d is None:
            continue
        if isinstance(d, datetime):
            date_str = d.strftime("%Y-%m-%d")
        else:
            date_str = (str(d) or "")[:10]
        if not date_str or len(date_str) < 10:
            continue
        out.append({"date": date_str, "count": doc.get("count", len(doc.get("matches") or []))})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def _jc_match_analysis_collections(db):
    """返回要参与统计的集合列表：主集合 + 备用集合（若存在）。合并两处数据，避免数据在备用集合时漏统计。"""
    colls = [db[COLLECTION_JC_MATCH_ANALYSIS]]
    if COLLECTION_JC_MATCH_ANALYSIS_ALT in db.list_collection_names():
        colls.append(db[COLLECTION_JC_MATCH_ANALYSIS_ALT])
    return colls


def _jc_match_analysis_find_by_mid(db: Any, mid: Any) -> dict[str, Any] | None:
    """从 jingcai_match_analysis 主集合及备用集合中按 sporttery_match_id 查找文档，找到即返回。"""
    for coll in _jc_match_analysis_collections(db):
        doc = coll.find_one({"sporttery_match_id": mid})
        if doc is not None:
            return doc
        if isinstance(mid, str):
            try:
                doc = coll.find_one({"sporttery_match_id": int(mid)})
                if doc is not None:
                    return doc
            except (TypeError, ValueError):
                pass
        if isinstance(mid, int):
            try:
                doc = coll.find_one({"sporttery_match_id": str(mid)})
                if doc is not None:
                    return doc
            except (TypeError, ValueError):
                pass
    return None


def _jc_match_analysis_batch_find(db: Any, mids: list[Any]) -> dict[Any, dict[str, Any]]:
    """批量按 sporttery_match_id 查 jingcai_match_analysis（主+备用集合），返回 mid -> doc，用于避免 N+1 查询。"""
    if not mids:
        return {}
    # 统一成可哈希键，支持 int/str 混用
    seen: set[Any] = set()
    norm_mids: list[Any] = []
    for m in mids:
        if m is None or m in seen:
            continue
        seen.add(m)
        norm_mids.append(m)
    if not norm_mids:
        return {}
    out: dict[Any, dict[str, Any]] = {}
    for coll in _jc_match_analysis_collections(db):
        cursor = coll.find({"sporttery_match_id": {"$in": norm_mids}})
        for doc in cursor:
            mid = doc.get("sporttery_match_id")
            if mid is not None and mid not in out:
                out[mid] = doc
                if isinstance(mid, int) and str(mid) not in out:
                    out[str(mid)] = doc
                elif isinstance(mid, str):
                    try:
                        mi = int(mid)
                        if mi not in out:
                            out[mi] = doc
                    except (TypeError, ValueError):
                        pass
    return out


def jc_match_analysis_list_dates() -> list[dict[str, Any]]:
    """列出 jingcai_match_analysis（及 jingcai_match_anlysis 若存在）中所有出现过的 jc_date/date 及该日场次数。"""
    db = get_self_improvement_db()
    if db is None:
        return []
    from collections import Counter
    counts: Counter[str] = Counter()
    for coll in _jc_match_analysis_collections(db):
        for doc in coll.find({}, {"jc_date": 1, "date": 1, "match_date": 1}):
            for key in ("jc_date", "date", "match_date"):
                d = _normalize_jc_date(doc.get(key))
                if d:
                    counts[d] += 1
                    break  # 每条文档只按一个日期计入，优先 jc_date
    return [{"date": d, "count": c} for d, c in sorted(counts.items(), reverse=True)]


def jc_match_analysis_distinct_jc_dates_for_years(years: list[int]) -> list[str]:
    """从 jingcai_match_analysis 已出现的竞彩日中，筛出年份属于 years 的 YYYY-MM-DD，升序。"""
    if not years:
        return []
    yset = {int(y) for y in years}
    out: set[str] = set()
    for row in jc_match_analysis_list_dates():
        d = (row.get("date") or "")[:10]
        if len(d) != 10 or d[4] != "-" or d[7] != "-":
            continue
        try:
            if int(d[:4]) in yset:
                out.add(d)
        except ValueError:
            continue
    return sorted(out)


def jc_match_analysis_raw_date_counts(year: int | None = None) -> dict[str, list[tuple[str, int]]]:
    """
    统计 jingcai_match_analysis（及备用集合）中 jc_date / date / match_date 的原始值及条数（用于诊断）。
    若指定 year，只统计该年份。返回 {"jc_date": [(raw_val, count), ...], ...}。
    """
    db = get_self_improvement_db()
    if db is None:
        return {}
    from collections import Counter
    out: dict[str, Counter[str]] = {"jc_date": Counter(), "date": Counter(), "match_date": Counter()}
    for coll in _jc_match_analysis_collections(db):
        for doc in coll.find({}, {"jc_date": 1, "date": 1, "match_date": 1}):
            for key in ("jc_date", "date", "match_date"):
                val = doc.get(key)
                if val is None:
                    continue
                raw = str(val).strip()[:30]  # 只取前 30 字符便于展示
                if year is not None:
                    if str(year) not in raw:
                        norm = _normalize_jc_date(val)
                        if not norm or not norm.startswith(str(year)):
                            continue
                out[key][raw] += 1
    result: dict[str, list[tuple[str, int]]] = {}
    for key in ("jc_date", "date", "match_date"):
        items = sorted(out[key].items(), key=lambda x: (-x[1], x[0]))
        if items:
            result[key] = items
    return result


def jc_match_analysis_fixture_ids_for_years(years: list[int]) -> list[int]:
    """
    从 jingcai_match_analysis（及备用集合）中取出指定年份内、且已有 api_football_fixture_id 的场次，
    返回去重后的 fixture_id 列表。用于按「竞彩库场次」拉取单场统计（射门、控球等）。
    years: 如 [2025, 2026]，按 jc_date / match_date 的年份过滤。
    """
    if not years:
        return []
    db = get_self_improvement_db()
    if db is None:
        return []
    seen: set[int] = set()
    year_pattern = "|".join(str(y) for y in years)
    regex = f"^({year_pattern})"
    query: dict[str, Any] = {
        "api_football_fixture_id": {"$exists": True, "$ne": None},
        "$or": [
            {"jc_date": {"$regex": regex}},
            {"match_date": {"$regex": regex}},
        ],
    }
    for coll in _jc_match_analysis_collections(db):
        for doc in coll.find(query, {"api_football_fixture_id": 1}):
            fid = doc.get("api_football_fixture_id")
            if fid is not None:
                try:
                    seen.add(int(fid))
                except (TypeError, ValueError):
                    pass
    out = sorted(seen)
    logger.info("jingcai_match_analysis 按年份 %s 查询：共 %d 个 fixture_id", years, len(out))
    return out


def jc_big5_api_football_fixture_ids() -> set[int]:
    """
    竞彩 jingcai_match_analysis 中联赛为五大联赛（中文）且含 api_football_fixture_id 的去重集合。
    供 ingest-api-history --exclude-jingcai-big5 使用：跳过这些场次，仅将「竞彩未覆盖」的五大联赛比赛写入主库 matches。
    """
    from etl.sporttery_crawl import BIG5_LEAGUES

    db = get_self_improvement_db()
    if db is None:
        return set()
    out: set[int] = set()
    query: dict[str, Any] = {
        "api_football_fixture_id": {"$exists": True, "$ne": None},
        "$or": [
            {"league_cn": {"$in": list(BIG5_LEAGUES)}},
            {"league": {"$in": list(BIG5_LEAGUES)}},
        ],
    }
    for coll in _jc_match_analysis_collections(db):
        for doc in coll.find(query, {"api_football_fixture_id": 1}):
            fid = doc.get("api_football_fixture_id")
            if fid is not None:
                try:
                    out.add(int(fid))
                except (TypeError, ValueError):
                    pass
    return out


def jc_match_analysis_fixture_ids_by_year(years: list[int] | None = None) -> dict[str, list[int]]:
    """
    按年汇总 jingcai_match_analysis 中已有 api_football_fixture_id 的场次。
    years: 仅这些年份；None 表示全部年份。
    返回 { "2025": [fid, ...], ... }，每年为去重后的 fixture_id 列表。
    """
    db = get_self_improvement_db()
    if db is None:
        return {}
    by_year: dict[str, set[int]] = {}
    query: dict[str, Any] = {"api_football_fixture_id": {"$exists": True, "$ne": None}}
    for coll in _jc_match_analysis_collections(db):
        for doc in coll.find(query, {"api_football_fixture_id": 1, "match_date": 1, "jc_date": 1}):
            date_val = doc.get("match_date") or doc.get("jc_date")
            day = _normalize_jc_date(date_val)
            if not day or len(day) < 10:
                continue
            try:
                y = int(day[:4])
            except (ValueError, TypeError):
                continue
            if years is not None and y not in years:
                continue
            fid = doc.get("api_football_fixture_id")
            if fid is not None:
                try:
                    key = str(y)
                    if key not in by_year:
                        by_year[key] = set()
                    by_year[key].add(int(fid))
                except (TypeError, ValueError):
                    pass
    return {k: sorted(v) for k, v in sorted(by_year.items())}


def _sanitize_for_mongo(obj: Any) -> Any:
    """递归序列化，便于写入 MongoDB（datetime -> str、tuple -> list 等）。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat() + "Z" if obj.tzinfo is None else obj.isoformat()
    if isinstance(obj, dict):
        return {k: _sanitize_for_mongo(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_mongo(x) for x in obj]
    return str(obj)


def _half_from_analysis_doc(
    doc: dict[str, Any],
    home_ft: int | None,
    away_ft: int | None,
) -> tuple[int | None, int | None]:
    """从 match_head.pageTitle.sectionsNo1 等解析半场比分。半场与全场数值可能相同（如半场 1:0、全场 1:0），仍返回半场。"""
    mh = doc.get("match_head")
    if not isinstance(mh, dict):
        return (None, None)
    pt = mh.get("pageTitle") or mh
    if not isinstance(pt, dict):
        pt = {}
    ht = (
        pt.get("sectionsNo1")
        or pt.get("halfTimeGoal")
        or pt.get("halfTimeScore")
        or pt.get("halfScore")
        or pt.get("htScore")
        or mh.get("sectionsNo1")
        or mh.get("halfScore")
        or mh.get("htScore")
    )
    hh, ha = None, None
    if isinstance(ht, dict):
        try:
            hh = int(ht["home"]) if ht.get("home") is not None else None
            ha = int(ht["away"]) if ht.get("away") is not None else None
        except (TypeError, ValueError, KeyError):
            hh, ha = None, None
    elif isinstance(ht, str) and ht.strip():
        try:
            from etl.sporttery_crawl import _parse_half

            hh, ha = _parse_half(ht)
        except Exception:
            hh, ha = None, None
    return (hh, ha)


def _enrich_jc_analysis_top_level_scores(detail: dict[str, Any]) -> None:
    """
    写入 jingcai_match_analysis 前：从 match_head 嵌套结构解析全场/半场比分，填到顶层 home_score、away_score、half_home、half_away，
    便于列表接口与复盘直接使用（体彩详情接口往往只把比分放在 pageTitle 内）。
    """
    h, a = _score_from_analysis_doc(detail)
    if detail.get("home_score") is None and h is not None:
        detail["home_score"] = h
    if detail.get("away_score") is None and a is not None:
        detail["away_score"] = a
    hf = _int_from_any(detail.get("home_score"))
    af = _int_from_any(detail.get("away_score"))
    hh, ha = _half_from_analysis_doc(detail, hf, af)
    if detail.get("half_home") is None and hh is not None:
        detail["half_home"] = hh
    if detail.get("half_away") is None and ha is not None:
        detail["half_away"] = ha


def _fill_english_names_in_detail(detail: dict[str, Any]) -> None:
    """
    在写入前根据 jingcai_teams 映射与详情内已有英文名字段，补全 detail 顶层的 home_en、away_en（就地修改）。
    优先使用 detail 内 match_head.pageTitle 的英文名，没有则用 jingcai_teams 表映射。
    """
    pt = (detail.get("match_head") or {}).get("pageTitle") or detail.get("match_head") or {}
    if not isinstance(pt, dict):
        pt = {}
    home_cn = (pt.get("homeTeamShortName") or pt.get("homeTeamAbbName") or "").strip()
    away_cn = (pt.get("awayTeamShortName") or pt.get("awayTeamAbbName") or "").strip()
    home_en = (detail.get("home_en") or "").strip() or (pt.get("homeTeamAbbEnName") or pt.get("homeTeamEnName") or pt.get("homeTeamNameEn") or "").strip()
    away_en = (detail.get("away_en") or "").strip() or (pt.get("awayTeamAbbEnName") or pt.get("awayTeamEnName") or pt.get("awayTeamNameEn") or "").strip()
    if home_en:
        detail["home_en"] = home_en
    if away_en:
        detail["away_en"] = away_en
    if (not home_en and home_cn) or (not away_en and away_cn):
        cn_to_en = get_jingcai_cn_to_en()
        if not home_en and home_cn:
            detail["home_en"] = cn_to_en.get(home_cn) or ""
        if not away_en and away_cn:
            detail["away_en"] = cn_to_en.get(away_cn) or ""


def jc_match_analysis_save(sporttery_match_id: int, detail: dict[str, Any]) -> bool:
    """
    将对阵详情写入 self_improvement_football.jingcai_match_analysis。
    以 sporttery_match_id 为唯一键 upsert，同场多次拉取会覆盖更新。
    保存前会根据 jingcai_teams 映射与详情内英文名字段自动补全 home_en、away_en（新拉取的比赛也会带英文名）。
    并从详情中解析联赛中文名写入顶层 league_cn，便于按联赛统计与伤停调参时无需再解析嵌套结构。
    返回是否写入成功（未配置 MongoDB 时返回 False）。
    """
    db = get_self_improvement_db()
    if db is None:
        return False
    _fill_english_names_in_detail(detail)
    _enrich_jc_analysis_top_level_scores(detail)
    coll = db[COLLECTION_JC_MATCH_ANALYSIS]
    doc = _sanitize_for_mongo({
        "sporttery_match_id": sporttery_match_id,
        **detail,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
    if not (doc.get("league_cn") or "").strip():
        league_cn = _get_league_cn_from_analysis_doc(doc)
        if league_cn:
            doc["league_cn"] = league_cn
    coll.replace_one({"sporttery_match_id": sporttery_match_id}, doc, upsert=True)
    return True


def jc_match_analysis_load(sporttery_match_id: int) -> dict[str, Any] | None:
    """从 self_improvement_football.jingcai_match_analysis 读取某场的对阵详情；不存在返回 None。
    主集合与备用集合（jingcai_match_anlysis）都会查，避免数据在备用集合时漏统计。
    若文档无 home_en/away_en，会用 jingcai_teams 表补全后放入返回结果（不写回库）。"""
    db = get_self_improvement_db()
    if db is None:
        return None
    doc = _jc_match_analysis_find_by_mid(db, sporttery_match_id)
    if not doc:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    pt = (doc.get("match_head") or {}).get("pageTitle") or doc.get("match_head") or {}
    if isinstance(pt, dict):
        home_cn = (pt.get("homeTeamShortName") or pt.get("homeTeamAbbName") or "").strip()
        away_cn = (pt.get("awayTeamShortName") or pt.get("awayTeamAbbName") or "").strip()
        need_en = (not (doc.get("home_en") or "").strip() and home_cn) or (not (doc.get("away_en") or "").strip() and away_cn)
        if need_en:
            cn_to_en = get_jingcai_cn_to_en()
            if not (doc.get("home_en") or "").strip() and home_cn:
                doc["home_en"] = cn_to_en.get(home_cn) or ""
            if not (doc.get("away_en") or "").strip() and away_cn:
                doc["away_en"] = cn_to_en.get(away_cn) or ""
    return doc


def save_api_football_statistics_bulk(by_fixture: dict[int, list[dict[str, Any]]]) -> int:
    """
    将 API-FOOTBALL 单场统计批量写入 self_improvement_football.api_football_statistics。
    每场一条文档：{ "fixture_id": int, "teams": [ 主队统计, 客队统计 ], "updated_at": "..." }，按 fixture_id upsert。
    返回成功写入/更新的场次数；未配置 MONGO_URI 时返回 0。
    """
    db = get_self_improvement_db()
    if db is None:
        return 0
    coll = db[COLLECTION_API_FOOTBALL_STATISTICS]
    n = 0
    for fixture_id, teams in by_fixture.items():
        if not teams:
            continue
        doc = _sanitize_for_mongo({
            "fixture_id": fixture_id,
            "teams": teams,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        })
        coll.replace_one({"fixture_id": fixture_id}, doc, upsert=True)
        n += 1
    return n


def load_api_football_statistics_from_db(
    fixture_ids: list[int] | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """
    从 self_improvement_football.api_football_statistics 读取单场统计。
    fixture_ids 为 None 时返回全部；否则只返回这些 fixture_id 的文档。
    返回 { fixture_id: [ 主队统计, 客队统计 ], ... }。
    """
    db = get_self_improvement_db()
    if db is None:
        return {}
    coll = db[COLLECTION_API_FOOTBALL_STATISTICS]
    if fixture_ids is not None and len(fixture_ids) == 0:
        return {}
    query = {"fixture_id": {"$in": fixture_ids}} if fixture_ids else {}
    out: dict[int, list[dict[str, Any]]] = {}
    for doc in coll.find(query, {"_id": 0, "fixture_id": 1, "teams": 1}):
        fid = doc.get("fixture_id")
        teams = doc.get("teams")
        if fid is not None and isinstance(teams, list) and len(teams) >= 1:
            out[int(fid)] = list(teams)
    return out


def dongqiudi_lineups_save(
    sporttery_match_id: int,
    jc_date: str,
    home_cn: str,
    away_cn: str,
    dongqiudi_match_id: int,
    predicted_home_starting: list[str],
    predicted_away_starting: list[str],
    formation_home: str | None = None,
    formation_away: str | None = None,
) -> bool:
    """
    保存懂球帝预计首发到 dongqiudi_lineups。按 sporttery_match_id 唯一，upsert。
    predicted_*_starting 为球员名列表（预计首发 11 人）。
    """
    db = get_self_improvement_db()
    if db is None:
        return False
    coll = db[COLLECTION_DONGQIUDI_LINEUPS]
    doc = _sanitize_for_mongo({
        "sporttery_match_id": sporttery_match_id,
        "jc_date": (jc_date or "")[:10],
        "home_cn": (home_cn or "").strip(),
        "away_cn": (away_cn or "").strip(),
        "dongqiudi_match_id": dongqiudi_match_id,
        "predicted_home_starting": list(predicted_home_starting) if predicted_home_starting else [],
        "predicted_away_starting": list(predicted_away_starting) if predicted_away_starting else [],
        "formation_home": (formation_home or "").strip() or None,
        "formation_away": (formation_away or "").strip() or None,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
    coll.replace_one({"sporttery_match_id": sporttery_match_id}, doc, upsert=True)
    return True


def dongqiudi_lineups_load_by_sporttery_match_id(sporttery_match_id: int) -> dict[str, Any] | None:
    """按体彩场次 ID 读取懂球帝预计首发；不存在返回 None。"""
    db = get_self_improvement_db()
    if db is None:
        return None
    doc = db[COLLECTION_DONGQIUDI_LINEUPS].find_one({"sporttery_match_id": sporttery_match_id})
    if not doc:
        return None
    doc.pop("_id", None)
    return dict(doc)


def transfermarkt_lineups_save(
    sporttery_match_id: int,
    jc_date: str,
    home_cn: str,
    away_cn: str,
    transfermarkt_match_id: int,
    predicted_home_starting: list[str],
    predicted_away_starting: list[str],
    formation_home: str | None = None,
    formation_away: str | None = None,
) -> bool:
    """
    保存 Transfermarkt 预计/用户阵型到 transfermarkt_lineups。按 sporttery_match_id 唯一，upsert。
    predicted_*_starting 为球员名列表（vorbericht 用户阵型或 spielbericht 确认阵容）。
    """
    db = get_self_improvement_db()
    if db is None:
        return False
    coll = db[COLLECTION_TRANSFERMARKT_LINEUPS]
    doc = _sanitize_for_mongo({
        "sporttery_match_id": sporttery_match_id,
        "jc_date": (jc_date or "")[:10],
        "home_cn": (home_cn or "").strip(),
        "away_cn": (away_cn or "").strip(),
        "transfermarkt_match_id": transfermarkt_match_id,
        "predicted_home_starting": list(predicted_home_starting) if predicted_home_starting else [],
        "predicted_away_starting": list(predicted_away_starting) if predicted_away_starting else [],
        "formation_home": (formation_home or "").strip() or None,
        "formation_away": (formation_away or "").strip() or None,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
    coll.replace_one({"sporttery_match_id": sporttery_match_id}, doc, upsert=True)
    return True


def transfermarkt_lineups_load_by_sporttery_match_id(sporttery_match_id: int) -> dict[str, Any] | None:
    """按体彩场次 ID 读取 Transfermarkt 阵容；不存在返回 None。"""
    db = get_self_improvement_db()
    if db is None:
        return None
    doc = db[COLLECTION_TRANSFERMARKT_LINEUPS].find_one({"sporttery_match_id": sporttery_match_id})
    if not doc:
        return None
    doc.pop("_id", None)
    return dict(doc)


# ----- Match lineups（竞彩用：按 sporttery_match_id 存于 self_improvement_football） -----
def lineups_get_by_match_id(match_id: int) -> dict[str, Any] | None:
    """从 self_improvement_football.match_lineups 按 match_id（竞彩即 sporttery_match_id）读取阵容。"""
    if match_id is None:
        return None
    db = get_self_improvement_db()
    if db is None:
        return None
    doc = db[COLLECTION_MATCH_LINEUPS].find_one({"match_id": match_id})
    if not doc:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


def lineups_save(
    match_id: int,
    home_starting_count: int | None = None,
    away_starting_count: int | None = None,
    home_starting: list[dict[str, Any]] | None = None,
    away_starting: list[dict[str, Any]] | None = None,
    home_substitutes: list[dict[str, Any]] | None = None,
    away_substitutes: list[dict[str, Any]] | None = None,
    actual_home_key_starting: int | None = None,
    actual_away_key_starting: int | None = None,
    actual_home_key_absent: int | None = None,
    actual_away_key_absent: int | None = None,
) -> None:
    """写入或更新 self_improvement_football.match_lineups（match_id 即 sporttery_match_id）。
    可选写入「关键球员 vs 实际首发」四字段，通常由 lineup-backfill-key-absence 回填。"""
    if match_id is None:
        return
    db = get_self_improvement_db()
    if db is None:
        return
    coll = db[COLLECTION_MATCH_LINEUPS]
    doc: dict[str, Any] = {
        "match_id": match_id,
        "home_starting_count": home_starting_count,
        "away_starting_count": away_starting_count,
    }
    if home_starting is not None:
        doc["home_starting"] = home_starting
    if away_starting is not None:
        doc["away_starting"] = away_starting
    if home_substitutes is not None:
        doc["home_substitutes"] = home_substitutes
    if away_substitutes is not None:
        doc["away_substitutes"] = away_substitutes
    if actual_home_key_starting is not None:
        doc["actual_home_key_starting"] = actual_home_key_starting
    if actual_away_key_starting is not None:
        doc["actual_away_key_starting"] = actual_away_key_starting
    if actual_home_key_absent is not None:
        doc["actual_home_key_absent"] = actual_home_key_absent
    if actual_away_key_absent is not None:
        doc["actual_away_key_absent"] = actual_away_key_absent
    existing = coll.find_one({"match_id": match_id})
    if existing:
        upd = {k: v for k, v in doc.items() if k != "match_id"}
        coll.update_one({"match_id": match_id}, {"$set": upd})
    else:
        coll.insert_one(doc)


def pre_lineups_save(
    match_id: int,
    home_starting_count: int | None = None,
    away_starting_count: int | None = None,
    home_starting: list[dict[str, Any]] | None = None,
    away_starting: list[dict[str, Any]] | None = None,
    home_substitutes: list[dict[str, Any]] | None = None,
    away_substitutes: list[dict[str, Any]] | None = None,
    source: str = "dongqiudi",
) -> None:
    """写入或更新赛前阵容到 pre_match_lineups，结构与 match_lineups 一致（不含 actual_* 关键球员字段）。"""
    if match_id is None:
        return
    db = get_self_improvement_db()
    if db is None:
        return
    coll = db[COLLECTION_PRE_MATCH_LINEUPS]
    doc: dict[str, Any] = {
        "match_id": match_id,
        "source": source,
        "home_starting_count": home_starting_count,
        "away_starting_count": away_starting_count,
    }
    if home_starting is not None:
        doc["home_starting"] = home_starting
    if away_starting is not None:
        doc["away_starting"] = away_starting
    if home_substitutes is not None:
        doc["home_substitutes"] = home_substitutes
    if away_substitutes is not None:
        doc["away_substitutes"] = away_substitutes
    existing = coll.find_one({"match_id": match_id})
    if existing:
        upd = {k: v for k, v in doc.items() if k != "match_id"}
        coll.update_one({"match_id": match_id}, {"$set": upd})
    else:
        coll.insert_one(doc)


def pre_lineups_get_by_match_id(match_id: int) -> dict[str, Any] | None:
    """从 pre_match_lineups 按 match_id 读取赛前阵容；不存在返回 None。"""
    if match_id is None:
        return None
    db = get_self_improvement_db()
    if db is None:
        return None
    doc = db[COLLECTION_PRE_MATCH_LINEUPS].find_one({"match_id": match_id})
    if not doc:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


def lineups_update_key_absence(
    match_id: int,
    actual_home_key_starting: int,
    actual_away_key_starting: int,
    actual_home_key_absent: int,
    actual_away_key_absent: int,
) -> bool:
    """仅更新 match_lineups 中某场的「关键球员 vs 实际首发」四字段；文档不存在则返回 False。"""
    db = get_self_improvement_db()
    if db is None:
        return False
    r = db[COLLECTION_MATCH_LINEUPS].update_one(
        {"match_id": match_id},
        {
            "$set": {
                "actual_home_key_starting": actual_home_key_starting,
                "actual_away_key_starting": actual_away_key_starting,
                "actual_home_key_absent": actual_home_key_absent,
                "actual_away_key_absent": actual_away_key_absent,
            }
        },
    )
    return r.modified_count > 0 or r.matched_count > 0


def lineups_backfill_key_absence(
    years: list[int] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
    skip_existing: bool = True,
) -> dict[str, Any]:
    """
    对 match_lineups 中已有完整首发名单（home_starting/away_starting）的场次，用 jingcai_match_analysis 的关键球员
    计算「关键球员 vs 实际首发」并写回 match_lineups（actual_home_key_starting 等四字段）。
    返回：{"updated": N, "skipped_no_jingcai": M, "skipped_no_lineup": K, "skipped_date": L}。
    """
    db = get_self_improvement_db()
    if db is None:
        return {"updated": 0, "skipped_no_jingcai": 0, "skipped_no_lineup": 0, "skipped_date": 0}
    coll = db[COLLECTION_MATCH_LINEUPS]
    query: dict[str, Any] = {
        "$and": [
            {"$or": [{"home_starting": {"$exists": True, "$type": "array"}}, {"away_starting": {"$exists": True, "$type": "array"}}]},
        ]
    }
    if skip_existing:
        query["$and"].append({"$or": [{"actual_home_key_absent": {"$exists": False}}, {"actual_home_key_absent": None}]})
    cursor = coll.find(query, {"match_id": 1, "home_starting": 1, "away_starting": 1})
    updated = 0
    skipped_no_jingcai = 0
    skipped_no_lineup = 0
    skipped_date = 0
    for doc in cursor:
        match_id = doc.get("match_id")
        if match_id is None:
            continue
        if not doc.get("home_starting") and not doc.get("away_starting"):
            skipped_no_lineup += 1
            continue
        jingcai = jc_match_analysis_load(int(match_id))
        if not jingcai:
            skipped_no_jingcai += 1
            continue
        day = (_normalize_jc_date(jingcai.get("jc_date") or jingcai.get("match_date") or "") or "")[:10]
        if len(day) >= 4:
            if years and day[:4].isdigit() and int(day[:4]) not in years:
                skipped_date += 1
                continue
            if date_from and day < date_from[:10]:
                skipped_date += 1
                continue
            if date_to and day > date_to[:10]:
                skipped_date += 1
                continue
        kp = key_player_absence_for_match(jingcai, doc)
        ahs = kp.get("actual_home_key_starting")
        aas = kp.get("actual_away_key_starting")
        aha = kp.get("actual_home_key_absent")
        aaa = kp.get("actual_away_key_absent")
        if ahs is None and aas is None and aha is None and aaa is None:
            skipped_no_lineup += 1
            continue
        ok = lineups_update_key_absence(
            int(match_id),
            ahs if ahs is not None else 0,
            aas if aas is not None else 0,
            aha if aha is not None else 0,
            aaa if aaa is not None else 0,
        )
        if ok:
            updated += 1
        if limit is not None and limit > 0 and updated >= limit:
            break
    return {
        "updated": updated,
        "skipped_no_jingcai": skipped_no_jingcai,
        "skipped_no_lineup": skipped_no_lineup,
        "skipped_date": skipped_date,
    }


def _norm_name_for_key(s: str) -> str:
    """姓名规范化，用于关键球员/首发名单匹配（与 parse 内逻辑一致）。"""
    return (s or "").strip().lower().replace(" ", "").replace("　", "")


def get_key_player_names_from_doc(doc: dict[str, Any]) -> tuple[set[str], set[str]]:
    """
    从 jingcai_match_analysis 单条文档解析主客队关键球员名单（规范化姓名集合）。
    返回 (home_key_names, away_key_names)。
    """
    home_key_names: set[str] = set()
    away_key_names: set[str] = set()

    def _names_from_player_list(rows: list) -> set[str]:
        names: set[str] = set()
        for row in (rows or []) if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = (
                row.get("personName") or row.get("playerName") or row.get("name")
                or row.get("playerNameCn") or row.get("person名") or ""
            )
            if name and str(name).strip():
                names.add(_norm_name_for_key(str(name).strip()))
        return names

    mp = doc.get("match_player")
    if not mp or not isinstance(mp, dict):
        return (home_key_names, away_key_names)
    home_list: list = []
    away_list_raw: list = []
    if mp.get("home") is not None:
        if isinstance(mp["home"], list):
            home_list = mp["home"]
        elif isinstance(mp["home"], dict):
            home_list = mp["home"].get("playerList") or mp["home"].get("scorerList") or mp["home"].get("list") or []
    if mp.get("away") is not None:
        if isinstance(mp["away"], list):
            away_list_raw = mp["away"]
        elif isinstance(mp["away"], dict):
            away_list_raw = mp["away"].get("playerList") or mp["away"].get("scorerList") or mp["away"].get("list") or []
    if not isinstance(home_list, list):
        home_list = []
    if not isinstance(away_list_raw, list):
        away_list_raw = []
    home_key_names = _names_from_player_list(home_list)
    away_key_names = _names_from_player_list(away_list_raw)
    if not home_key_names and not away_key_names:
        home_list = mp.get("homePlayerList") or mp.get("homeList") or (mp.get("home") if isinstance(mp.get("home"), list) else [])
        away_list_raw = mp.get("awayPlayerList") or mp.get("awayList") or (mp.get("away") if isinstance(mp.get("away"), list) else [])
        home_key_names = _names_from_player_list(home_list)
        away_key_names = _names_from_player_list(away_list_raw)
    return (home_key_names, away_key_names)


def _actual_starting_names_from_lineup(lineup_doc: dict[str, Any], side: str) -> set[str]:
    """从 match_lineups 文档中取主/客队实际首发的规范化姓名集合。side 为 'home' 或 'away'。"""
    key = "home_starting" if side == "home" else "away_starting"
    raw = lineup_doc.get(key)
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            name = item.get("player_name") or item.get("playerName") or item.get("name") or ""
        else:
            name = str(item) if item else ""
        if name and str(name).strip():
            out.add(_norm_name_for_key(str(name).strip()))
    return out


def key_player_absence_for_match(doc: dict[str, Any], actual_lineups: dict[str, Any] | None) -> dict[str, Any]:
    """
    关键球员 vs 实际首发对比（单场）。
    当 jingcai 文档含 match_player 且 actual_lineups 含 home_starting/away_starting 时，
    计算关键球员中实际首发人数、实际未首发人数。
    返回字段：actual_home_key_starting, actual_away_key_starting, actual_home_key_absent, actual_away_key_absent。
    """
    result: dict[str, Any] = {
        "actual_home_key_starting": None,
        "actual_away_key_starting": None,
        "actual_home_key_absent": None,
        "actual_away_key_absent": None,
    }
    if not actual_lineups or not isinstance(actual_lineups, dict):
        return result
    home_starting_list = actual_lineups.get("home_starting")
    away_starting_list = actual_lineups.get("away_starting")
    if not isinstance(home_starting_list, list) and not isinstance(away_starting_list, list):
        return result
    home_key, away_key = get_key_player_names_from_doc(doc)
    if not home_key and not away_key:
        return result
    home_actual = _actual_starting_names_from_lineup(actual_lineups, "home")
    away_actual = _actual_starting_names_from_lineup(actual_lineups, "away")
    if home_key:
        started = len(home_key & home_actual)
        result["actual_home_key_starting"] = started
        result["actual_home_key_absent"] = len(home_key) - started
    if away_key:
        started = len(away_key & away_actual)
        result["actual_away_key_starting"] = started
        result["actual_away_key_absent"] = len(away_key) - started
    return result


def lineups_list_jingcai_to_fetch(
    *,
    years: list[int] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    skip_existing: bool = True,
    limit: int | None = None,
) -> list[tuple[int, int]]:
    """
    从 jingcai_match_analysis 列出待拉取阵容的 (sporttery_match_id, api_football_fixture_id)。
    skip_existing=True 时排除 match_lineups 中已有记录的 match_id。
    """
    db = get_self_improvement_db()
    if db is None:
        return []
    query: dict[str, Any] = {"api_football_fixture_id": {"$exists": True, "$ne": None}}
    if date_from or date_to:
        date_range: dict[str, Any] = {}
        if date_from:
            date_range["$gte"] = date_from[:10]
        if date_to:
            date_range["$lte"] = date_to[:10]
        if date_range:
            query["$or"] = [{"jc_date": date_range}, {"match_date": date_range}]
    rows: list[dict[str, Any]] = []
    for coll in _jc_match_analysis_collections(db):
        for doc in coll.find(query, {"sporttery_match_id": 1, "api_football_fixture_id": 1, "jc_date": 1, "match_date": 1}):
            mid = doc.get("sporttery_match_id")
            fid = doc.get("api_football_fixture_id")
            if mid is None or fid is None:
                continue
            try:
                mid, fid = int(mid), int(fid)
            except (TypeError, ValueError):
                continue
            if years:
                day = (doc.get("match_date") or doc.get("jc_date") or "")[:10]
                if len(day) >= 4 and day[:4].isdigit() and int(day[:4]) not in years:
                    continue
            rows.append((mid, fid))
    # 去重：同一 sporttery_match_id 只保留一条（取第一个）
    seen_mid: set[int] = set()
    unique: list[tuple[int, int]] = []
    for mid, fid in rows:
        if mid in seen_mid:
            continue
        seen_mid.add(mid)
        unique.append((mid, fid))
    if skip_existing and unique:
        lineups_coll = db[COLLECTION_MATCH_LINEUPS]
        have = set()
        for d in lineups_coll.find({}, {"match_id": 1}):
            m = d.get("match_id")
            if m is not None:
                have.add(int(m))
        unique = [(mid, fid) for mid, fid in unique if mid not in have]
    if limit is not None and limit > 0 and len(unique) > limit:
        unique = unique[:limit]
    return unique


def events_list_jingcai_to_fetch(
    years: list[int] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    skip_existing: bool = True,
    limit: int | None = None,
) -> list[tuple[int, int]]:
    """
    从 jingcai_match_analysis 列出待拉取事件的 (sporttery_match_id, api_football_fixture_id)。
    skip_existing=True 时排除 api_football_events 中已有记录的 fixture_id。
    """
    db = get_self_improvement_db()
    if db is None:
        return []
    query: dict[str, Any] = {"api_football_fixture_id": {"$exists": True, "$ne": None}}
    if date_from or date_to:
        date_range: dict[str, Any] = {}
        if date_from:
            date_range["$gte"] = date_from[:10]
        if date_to:
            date_range["$lte"] = date_to[:10]
        if date_range:
            query["$or"] = [{"jc_date": date_range}, {"match_date": date_range}]
    rows: list[dict[str, Any]] = []
    for coll in _jc_match_analysis_collections(db):
        for doc in coll.find(query, {"sporttery_match_id": 1, "api_football_fixture_id": 1, "jc_date": 1, "match_date": 1}):
            mid = doc.get("sporttery_match_id")
            fid = doc.get("api_football_fixture_id")
            if mid is None or fid is None:
                continue
            try:
                mid, fid = int(mid), int(fid)
            except (TypeError, ValueError):
                continue
            if years:
                day = (doc.get("match_date") or doc.get("jc_date") or "")[:10]
                if len(day) >= 4 and day[:4].isdigit() and int(day[:4]) not in years:
                    continue
            rows.append((mid, fid))
    # 去重：同一 fixture_id 只保留一条（取第一个 match_id）
    seen_fid: set[int] = set()
    unique: list[tuple[int, int]] = []
    for mid, fid in rows:
        if fid in seen_fid:
            continue
        seen_fid.add(fid)
        unique.append((mid, fid))
    if skip_existing and unique:
        events_coll = db[COLLECTION_API_FOOTBALL_EVENTS]
        have = set(d.get("api_football_fixture_id") for d in events_coll.find({}, {"api_football_fixture_id": 1}) if d.get("api_football_fixture_id") is not None)
        unique = [(mid, fid) for mid, fid in unique if fid not in have]
    if limit is not None and limit > 0 and len(unique) > limit:
        unique = unique[:limit]
    return unique


def save_api_football_events(
    api_football_fixture_id: int,
    events: list[dict[str, Any]],
    sporttery_match_id: int | None = None,
) -> None:
    """
    将单场事件写入 self_improvement_football.api_football_events。
    以 api_football_fixture_id 唯一，upsert；events 为 API 返回的 response 数组。
    """
    db = get_self_improvement_db()
    if db is None:
        return
    from datetime import datetime
    doc: dict[str, Any] = {
        "api_football_fixture_id": api_football_fixture_id,
        "events": list(events) if isinstance(events, list) else [],
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    if sporttery_match_id is not None:
        doc["sporttery_match_id"] = sporttery_match_id
    coll = db[COLLECTION_API_FOOTBALL_EVENTS]
    coll.update_one(
        {"api_football_fixture_id": api_football_fixture_id},
        {"$set": doc},
        upsert=True,
    )


def players_stats_list_jingcai_to_fetch(
    years: list[int] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    skip_existing: bool = True,
    limit: int | None = None,
) -> list[tuple[int, int]]:
    """
    从 jingcai_match_analysis 列出待拉取球员统计的 (sporttery_match_id, api_football_fixture_id)。
    skip_existing=True 时排除 api_football_player_statistics 中已有记录的 fixture_id。
    """
    db = get_self_improvement_db()
    if db is None:
        return []
    query: dict[str, Any] = {"api_football_fixture_id": {"$exists": True, "$ne": None}}
    if date_from or date_to:
        date_range: dict[str, Any] = {}
        if date_from:
            date_range["$gte"] = date_from[:10]
        if date_to:
            date_range["$lte"] = date_to[:10]
        if date_range:
            query["$or"] = [{"jc_date": date_range}, {"match_date": date_range}]
    rows: list[tuple[int, int]] = []
    for coll in _jc_match_analysis_collections(db):
        for doc in coll.find(query, {"sporttery_match_id": 1, "api_football_fixture_id": 1, "jc_date": 1, "match_date": 1}):
            mid = doc.get("sporttery_match_id")
            fid = doc.get("api_football_fixture_id")
            if mid is None or fid is None:
                continue
            try:
                mid, fid = int(mid), int(fid)
            except (TypeError, ValueError):
                continue
            if years:
                day = (doc.get("match_date") or doc.get("jc_date") or "")[:10]
                if len(day) >= 4 and day[:4].isdigit() and int(day[:4]) not in years:
                    continue
            rows.append((mid, fid))
    seen_fid: set[int] = set()
    unique: list[tuple[int, int]] = []
    for mid, fid in rows:
        if fid in seen_fid:
            continue
        seen_fid.add(fid)
        unique.append((mid, fid))
    if skip_existing and unique:
        stats_coll = db[COLLECTION_API_FOOTBALL_PLAYER_STATISTICS]
        have = set(
            d.get("api_football_fixture_id")
            for d in stats_coll.find({}, {"api_football_fixture_id": 1})
            if d.get("api_football_fixture_id") is not None
        )
        unique = [(mid, fid) for mid, fid in unique if fid not in have]
    if limit is not None and limit > 0 and len(unique) > limit:
        unique = unique[:limit]
    return unique


def save_api_football_player_statistics(
    api_football_fixture_id: int,
    players: list[dict[str, Any]],
    sporttery_match_id: int | None = None,
) -> None:
    """
    将单场球员统计写入 self_improvement_football.api_football_player_statistics。
    以 api_football_fixture_id 唯一，upsert；players 为 API players/statistics 返回的 response 数组。
    """
    db = get_self_improvement_db()
    if db is None:
        return
    doc = _sanitize_for_mongo({
        "api_football_fixture_id": api_football_fixture_id,
        "players": list(players) if isinstance(players, list) else [],
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
    if sporttery_match_id is not None:
        doc["sporttery_match_id"] = sporttery_match_id
    coll = db[COLLECTION_API_FOOTBALL_PLAYER_STATISTICS]
    coll.replace_one({"api_football_fixture_id": api_football_fixture_id}, doc, upsert=True)


def load_api_football_player_statistics(
    fixture_ids: list[int] | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """
    从 api_football_player_statistics 读取单场球员统计。
    fixture_ids 为 None 时返回全部；否则只返回这些 fixture_id 的文档。
    返回 { fixture_id: [ 球员统计列表 ], ... }。
    """
    db = get_self_improvement_db()
    if db is None:
        return {}
    coll = db[COLLECTION_API_FOOTBALL_PLAYER_STATISTICS]
    if fixture_ids is not None and len(fixture_ids) == 0:
        return {}
    query = {"api_football_fixture_id": {"$in": fixture_ids}} if fixture_ids else {}
    out: dict[int, list[dict[str, Any]]] = {}
    for doc in coll.find(query, {"_id": 0, "api_football_fixture_id": 1, "players": 1}):
        fid = doc.get("api_football_fixture_id")
        players = doc.get("players")
        if fid is not None and isinstance(players, list):
            out[int(fid)] = list(players)
    return out


def save_topscorers(
    league_id: int,
    season: int,
    players: list[dict[str, Any]],
    league_name: str | None = None,
) -> None:
    """
    将联赛射手榜写入 self_improvement_football.api_football_topscorers。
    以 (league_id, season) 唯一，upsert。
    """
    db = get_self_improvement_db()
    if db is None:
        return
    doc = _sanitize_for_mongo({
        "league_id": int(league_id),
        "season": int(season),
        "players": list(players) if isinstance(players, list) else [],
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
    if league_name:
        doc["league_name"] = str(league_name).strip()
    coll = db[COLLECTION_API_FOOTBALL_TOPSCORERS]
    coll.replace_one(
        {"league_id": league_id, "season": season},
        doc,
        upsert=True,
    )


def load_topscorers(
    league_id: int | None = None,
    season: int | None = None,
) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """
    从 api_football_topscorers 读取射手榜。
    league_id/season 为 None 时查全部；否则只查该联赛+赛季。
    返回 { (league_id, season): [ 球员列表 ], ... }。
    """
    db = get_self_improvement_db()
    if db is None:
        return {}
    coll = db[COLLECTION_API_FOOTBALL_TOPSCORERS]
    query: dict[str, Any] = {}
    if league_id is not None:
        query["league_id"] = league_id
    if season is not None:
        query["season"] = season
    out: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for doc in coll.find(query, {"_id": 0, "league_id": 1, "season": 1, "players": 1}):
        lid = doc.get("league_id")
        s = doc.get("season")
        players = doc.get("players")
        if lid is not None and s is not None and isinstance(players, list):
            out[(int(lid), int(s))] = list(players)
    return out


def save_team_statistics(
    league_id: int,
    team_id: int,
    season: int,
    statistics: dict[str, Any],
    team_name: str | None = None,
) -> None:
    """将球队赛季统计写入 api_football_team_statistics。以 (league_id, team_id, season) 唯一，upsert。"""
    db = get_self_improvement_db()
    if db is None:
        return
    doc = _sanitize_for_mongo({
        "league_id": int(league_id),
        "team_id": int(team_id),
        "season": int(season),
        "statistics": dict(statistics) if isinstance(statistics, dict) else {},
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
    if team_name:
        doc["team_name"] = str(team_name).strip()
    coll = db[COLLECTION_API_FOOTBALL_TEAM_STATISTICS]
    coll.replace_one(
        {"league_id": league_id, "team_id": team_id, "season": season},
        doc,
        upsert=True,
    )


def load_team_statistics(
    league_id: int | None = None,
    team_id: int | None = None,
    season: int | None = None,
) -> dict[tuple[int, int, int], dict[str, Any]]:
    """从 api_football_team_statistics 读取。返回 { (league_id, team_id, season): statistics_dict }。"""
    db = get_self_improvement_db()
    if db is None:
        return {}
    coll = db[COLLECTION_API_FOOTBALL_TEAM_STATISTICS]
    query: dict[str, Any] = {}
    if league_id is not None:
        query["league_id"] = league_id
    if team_id is not None:
        query["team_id"] = team_id
    if season is not None:
        query["season"] = season
    out: dict[tuple[int, int, int], dict[str, Any]] = {}
    for doc in coll.find(query, {"_id": 0, "league_id": 1, "team_id": 1, "season": 1, "statistics": 1}):
        lid = doc.get("league_id")
        tid = doc.get("team_id")
        s = doc.get("season")
        stats = doc.get("statistics")
        if lid is not None and tid is not None and s is not None and isinstance(stats, dict):
            out[(int(lid), int(tid), int(s))] = dict(stats)
    return out


def save_squads(
    team_id: int,
    players: list[dict[str, Any]],
    season: int | None = None,
    team_name: str | None = None,
) -> None:
    """将球队阵容写入 api_football_squads。以 team_id 唯一，upsert；可选 season/team_name 便于查询。"""
    db = get_self_improvement_db()
    if db is None:
        return
    doc = _sanitize_for_mongo({
        "team_id": int(team_id),
        "players": list(players) if isinstance(players, list) else [],
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
    if season is not None:
        doc["season"] = int(season)
    if team_name:
        doc["team_name"] = str(team_name).strip()
    coll = db[COLLECTION_API_FOOTBALL_SQUADS]
    coll.replace_one({"team_id": team_id}, doc, upsert=True)


def load_squads(
    team_id: int | None = None,
    season: int | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """从 api_football_squads 读取。返回 { team_id: [ 球员列表 ], ... }。"""
    db = get_self_improvement_db()
    if db is None:
        return {}
    coll = db[COLLECTION_API_FOOTBALL_SQUADS]
    query: dict[str, Any] = {}
    if team_id is not None:
        query["team_id"] = team_id
    if season is not None:
        query["season"] = season
    out: dict[int, list[dict[str, Any]]] = {}
    for doc in coll.find(query, {"_id": 0, "team_id": 1, "players": 1}):
        tid = doc.get("team_id")
        players = doc.get("players")
        if tid is not None and isinstance(players, list):
            out[int(tid)] = list(players)
    return out


def save_coach(
    team_id: int,
    coaches: list[dict[str, Any]],
    team_name: str | None = None,
) -> None:
    """将教练信息写入 api_football_coaches。以 team_id 唯一，upsert；coaches 为 API 返回的 response 数组。"""
    db = get_self_improvement_db()
    if db is None:
        return
    doc = _sanitize_for_mongo({
        "team_id": int(team_id),
        "coaches": list(coaches) if isinstance(coaches, list) else [],
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
    if team_name:
        doc["team_name"] = str(team_name).strip()
    coll = db[COLLECTION_API_FOOTBALL_COACHES]
    coll.replace_one({"team_id": team_id}, doc, upsert=True)


def load_coaches(
    team_id: int | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """从 api_football_coaches 读取。返回 { team_id: [ 教练列表 ], ... }。"""
    db = get_self_improvement_db()
    if db is None:
        return {}
    coll = db[COLLECTION_API_FOOTBALL_COACHES]
    query = {} if team_id is None else {"team_id": team_id}
    out: dict[int, list[dict[str, Any]]] = {}
    for doc in coll.find(query, {"_id": 0, "team_id": 1, "coaches": 1}):
        tid = doc.get("team_id")
        coaches = doc.get("coaches")
        if tid is not None and isinstance(coaches, list):
            out[int(tid)] = list(coaches)
    return out


def _normalize_jc_date(val: Any) -> str:
    """将 jc_date 规范为 YYYY-MM-DD，去掉末尾逗号等；支持 2025-1-5、2025/01/05、20250105 等格式。"""
    if val is None:
        return ""
    s = str(val).strip().rstrip(",").replace("/", "-").replace(".", "-")
    if not s:
        return ""
    # 纯数字 8 位如 20250105 -> YYYY-MM-DD
    if s.isdigit() and len(s) >= 8:
        return s[:4] + "-" + s[4:6].zfill(2) + "-" + s[6:8].zfill(2)
    if s.isdigit() and len(s) >= 6:
        return s[:4] + "-" + s[4:6].zfill(2) + "-01"
    # 已有 YYYY-MM-DD 或 YYYY-M-D
    parts = s.split("-")
    if len(parts) >= 3 and parts[0].isdigit() and len(parts[0]) == 4:
        y, m, d = parts[0], parts[1].zfill(2), (parts[2][:2] if parts[2] else "01").zfill(2)
        out = f"{y}-{m}-{d}"
        return out if len(out) >= 10 else ""
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return ""


def _jc_date_query_values(day: str) -> list[str]:
    """给定 YYYY-MM-DD，返回库里可能出现的 jc_date/date 取值（含短格式、带逗号等），用于 $in 查询。"""
    if not day or len(day) < 10:
        return []
    parts = day.split("-")
    if len(parts) != 3:
        return [day, day + ","]
    y, m, d = parts[0], parts[1].lstrip("0") or "0", parts[2].lstrip("0") or "0"
    variants = [
        day,
        day + ",",
        f"{y}-{int(m)}-{int(d)}" if m != "0" and d != "0" else day,
        f"{y}-{m}-{parts[2]}",
        f"{y}-{parts[1]}-{d}",
    ]
    return list(dict.fromkeys(v for v in variants if v))


def _is_match_num_previous_jc_day(match_num: str, jc_date: str) -> bool:
    """比赛编号是否属于「前一竞彩日」：竞彩日周二不应显示周一xxx，仅排除这类错日数据。"""
    if not match_num or not jc_date or len(jc_date) < 10:
        return False
    import re
    mat = re.match(r"周([一二三四五六日])", (match_num or "").strip())
    if not mat:
        return False
    week_zh_to_num = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6}
    num_weekday = week_zh_to_num.get(mat.group(1), -1)
    if num_weekday < 0:
        return False
    try:
        from datetime import datetime as _dt
        jc_weekday = _dt.strptime(jc_date[:10], "%Y-%m-%d").weekday()  # 0=周一
        # 编号为「前一天」则排除：周二(1) 排除 周一(0)，周三(2) 排除 周二(1)
        prev_day = (jc_weekday - 1 + 7) % 7
        return num_weekday == prev_day
    except Exception:
        return False


def jc_match_analysis_list_finished() -> list[tuple[str, dict[str, Any]]]:
    """
    返回 jingcai_match_analysis 中所有已完赛场次的 (date_str, match_item) 列表。
    match_item 与 jc_match_analysis_list_by_jc_date 单条结构一致，用于 repredict 主库（竞彩）重算。
    """
    db = get_self_improvement_db()
    if db is None:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for d in jc_match_analysis_list_dates():
        day = (d.get("date") or "")[:10]
        if len(day) < 10:
            continue
        for m in jc_match_analysis_list_by_jc_date(day):
            if m.get("home_score") is not None and m.get("away_score") is not None:
                out.append((day, m))
    return out


def jc_match_analysis_list_by_jc_date(jc_date: str) -> list[dict[str, Any]]:
    """从 jingcai_match_analysis（及备用集合若存在）按 jc_date 查出该日的所有场次。主客队英文名优先用文档内 home_en/away_en，否则用 jingcai_teams 补齐。排除「前一竞彩日」的场次（如周二列表不显示周一004）。不做 sporttery_match_id 去重，每条文档算一场（主集合与备用集合若同时存在会都计入）。"""
    db = get_self_improvement_db()
    if db is None:
        return []
    day = (jc_date or "").strip()[:10]
    if not day:
        return []
    cn_to_en = get_jingcai_cn_to_en()
    day_vals = _jc_date_query_values(day)
    query = {"$or": [{"jc_date": {"$in": day_vals}}, {"date": {"$in": day_vals}}]}
    out: list[dict[str, Any]] = []
    for coll in _jc_match_analysis_collections(db):
        for doc in coll.find(query):
            pt = (doc.get("match_head") or {}).get("pageTitle") or doc.get("match_head") or {}
            if not isinstance(pt, dict):
                pt = {}
            # 优先用拉取列表时写入的 list_match_num（与竞彩日一致），避免详情接口返回的周几导致错日
            match_num = (doc.get("list_match_num") or pt.get("matchNum") or pt.get("match_num") or "").strip()
            if _is_match_num_previous_jc_day(match_num, day):
                continue
            home_cn = (pt.get("homeTeamShortName") or pt.get("homeTeamAbbName") or "").strip()
            away_cn = (pt.get("awayTeamShortName") or pt.get("awayTeamAbbName") or "").strip()
            home_en = (doc.get("home_en") or "").strip() or (cn_to_en.get(home_cn) if home_cn else None)
            away_en = (doc.get("away_en") or "").strip() or (cn_to_en.get(away_cn) if away_cn else None)
            # 已完赛比分：match_head.pageTitle.fullCourtGoal / 顶层 home_score,away_score / wbsjStats 主客队
            home_score = doc.get("home_score")
            away_score = doc.get("away_score")
            fcg = pt.get("fullCourtGoal")
            if home_score is None or away_score is None:
                if isinstance(fcg, dict) and ("home" in fcg or "away" in fcg):
                    home_score = fcg.get("home")
                    away_score = fcg.get("away")
                elif isinstance(fcg, str) and fcg.strip():
                    parts = fcg.replace("：", ":").split(":")
                    if len(parts) >= 2:
                        try:
                            home_score = int(parts[0].strip())
                            away_score = int(parts[1].strip())
                        except (ValueError, TypeError):
                            pass
            if (home_score is None or away_score is None) and isinstance(pt, dict):
                wbsj = doc.get("wbsjStats") or pt.get("wbsjStats") or (doc.get("match_head") or {}).get("wbsjStats")
                if isinstance(wbsj, dict):
                    h, a = _score_from_wbsj(wbsj)
                    if h is not None and a is not None:
                        home_score, away_score = h, a
            score_str = ""
            if home_score is not None and away_score is not None:
                score_str = f"{home_score} : {away_score}"
            hi = _int_from_any(doc.get("half_home"))
            ai = _int_from_any(doc.get("half_away"))
            if hi is None or ai is None:
                try:
                    hf = int(home_score) if home_score is not None else None
                    af = int(away_score) if away_score is not None else None
                except (TypeError, ValueError):
                    hf, af = None, None
                hp, ap = _half_from_analysis_doc(doc, hf, af)
                hi = hi if hi is not None else hp
                ai = ai if ai is not None else ap
            jc_day_row = _normalize_jc_date(doc.get("jc_date") or doc.get("date") or "")
            out.append({
                "match_id": doc.get("sporttery_match_id"),
                "jc_date": jc_day_row or "",
                "match_num": match_num or pt.get("matchNum") or pt.get("match_num") or "",
                "match_time": doc.get("match_time") or "",
                "match_date": doc.get("match_date") or "",
                "home_cn": home_cn or "",
                "away_cn": away_cn or "",
                "home_en": home_en or "",
                "away_en": away_en or "",
                "home": home_en or home_cn or "",
                "away": away_en or away_cn or "",
                "league": pt.get("tournamentCnShortName") or pt.get("leagueName") or "",
                "odds": doc.get("odds") if isinstance(doc.get("odds"), dict) else {},
                "home_score": home_score,
                "away_score": away_score,
                "half_home": hi,
                "half_away": ai,
                "score_str": score_str,
                "api_football_fixture_id": doc.get("api_football_fixture_id"),
            })
    out.sort(key=_match_num_sort_key)
    return out


def jc_match_analysis_delete_for_jc_date(
    jc_date: str,
    *,
    delete_jc_matches_doc: bool = True,
    delete_jc_predictions: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    按竞彩日删除 jingcai_match_analysis（主集合 + 备用拼写集合）中的文档。
    可选同时删除 jc_matches 中该日的汇总文档、jc_predictions 中该日的模型预测。
    用于清理串日/错日拉取产生的旧数据；删除后可在该日重新「拉取竞彩列表」。
    """
    db = get_self_improvement_db()
    if db is None:
        raise RuntimeError("未配置 MONGO_URI 或无法连接 self_improvement_football")
    day = (jc_date or "").strip()[:10]
    if len(day) != 10:
        raise ValueError("jc_date 须为 YYYY-MM-DD")
    day_vals = _jc_date_query_values(day)
    query: dict[str, Any] = {"$or": [{"jc_date": {"$in": day_vals}}, {"date": {"$in": day_vals}}]}
    out: dict[str, Any] = {"jc_date": day, "dry_run": dry_run, "collections": {}}
    total_analysis = 0
    for coll in _jc_match_analysis_collections(db):
        name = coll.name
        if dry_run:
            n = coll.count_documents(query)
            out["collections"][name] = {"would_delete": n}
            total_analysis += n
        else:
            r = coll.delete_many(query)
            out["collections"][name] = {"deleted": r.deleted_count}
            total_analysis += r.deleted_count
    out["deleted_analysis_total"] = total_analysis

    out["jc_matches"] = {}
    if delete_jc_matches_doc:
        jc_coll = db[COLLECTION_JC_MATCHES]
        jc_query: dict[str, Any] = {"$or": [{"date": day}, {"date": day + ","}]}
        try:
            dt = datetime.strptime(day, "%Y-%m-%d")
            jc_query = {"$or": [{"date": day}, {"date": day + ","}, {"date": dt}]}
        except ValueError:
            pass
        if dry_run:
            n = jc_coll.count_documents(jc_query)
            out["jc_matches"]["would_delete_docs"] = n
        else:
            r = jc_coll.delete_many(jc_query)
            out["jc_matches"]["deleted"] = r.deleted_count

    out["jc_predictions"] = {}
    if delete_jc_predictions:
        if dry_run:
            n = db[COLLECTION_JC_PREDICTIONS].count_documents({"date": day})
            out["jc_predictions"]["would_delete"] = n
        else:
            out["jc_predictions"]["deleted"] = jc_predictions_delete_by_date(day)
    return out


def jc_match_analysis_prune_jc_date_not_in_ids(jc_date: str, keep_ids: set[int]) -> int:
    """
    删除该竞彩日下 sporttery_match_id 不在 keep_ids 中的 jingcai_match_analysis 文档（主集合 + 备用集合）。
    用于「拉取竞彩列表」后去掉已不在官网该日开售列表中的库内残留。
    """
    db = get_self_improvement_db()
    if db is None or not keep_ids:
        return 0
    day = (jc_date or "").strip()[:10]
    if len(day) != 10:
        return 0
    day_vals = _jc_date_query_values(day)
    ids_list = sorted(int(x) for x in keep_ids)
    q: dict[str, Any] = {
        "$and": [
            {"$or": [{"jc_date": {"$in": day_vals}}, {"date": {"$in": day_vals}}]},
            {"sporttery_match_id": {"$nin": ids_list}},
        ]
    }
    total = 0
    for coll in _jc_match_analysis_collections(db):
        r = coll.delete_many(q)
        total += r.deleted_count
    return total


def _match_num_sort_key(m: dict[str, Any]) -> tuple[int, int]:
    """比赛编号排序键：周XNNN 解析为 (weekday, num)，周一001=(1,1) 周日015=(7,15)，无编号返回 (99,999) 排到末尾。"""
    import re
    s = (m.get("match_num") or m.get("match_no") or "").strip()
    if not s:
        return (99, 999)
    # 全角数字转半角（\uff10-\uff19 为全角 0-9）
    for i in range(10):
        s = s.replace(chr(0xFF10 + i), str(i))
    mat = re.match(r"周([一二三四五六日])(\d+)", s)
    if mat:
        week_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7}
        w = week_map.get(mat.group(1), 0)
        n = int(mat.group(2), 10)
        return (w, n)
    num_mat = re.search(r"(\d+)\s*$", s)
    if num_mat:
        return (99, int(num_mat.group(1), 10))
    return (99, 999)


def lineup_comparison_report(
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """
    复盘：已完赛且具备阵容数据的场次，对比「预计首发/关键球员缺阵」与「实际首发人数」。
    返回列表每项含：sporttery_match_id, jc_date, home_cn, away_cn, home_score, away_score,
    pred_home_key_absent, pred_away_key_absent, actual_home_starting_count, actual_away_starting_count,
    pred_home_starting_count, pred_away_starting_count, league。
    当 match_lineups 含完整首发名单（home_starting/away_starting）时，额外返回：
    actual_home_key_starting, actual_away_key_starting, actual_home_key_absent, actual_away_key_absent（关键球员 vs 实际首发）。
    """
    db = get_self_improvement_db()
    if db is None:
        return []
    out: list[dict[str, Any]] = []
    date_from = (date_from or "")[:10] if date_from else ""
    date_to = (date_to or "")[:10] if date_to else ""
    for d in jc_match_analysis_list_dates():
        day = (d.get("date") or "")[:10]
        if len(day) < 10:
            continue
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        for m in jc_match_analysis_list_by_jc_date(day):
            if m.get("home_score") is None or m.get("away_score") is None:
                continue
            mid = m.get("match_id")
            if mid is None:
                continue
            doc = jc_match_analysis_load(int(mid))
            if not doc:
                continue
            parsed = parse_jingcai_analysis_for_predict(doc, m.get("match_date"))
            pred_lineup = dongqiudi_lineups_load_by_sporttery_match_id(int(mid))
            if not pred_lineup or not isinstance(pred_lineup, dict):
                pred_lineup = transfermarkt_lineups_load_by_sporttery_match_id(int(mid))
            actual = lineups_get_by_match_id(int(mid))
            pred_home = (pred_lineup.get("predicted_home_starting") or []) if isinstance(pred_lineup, dict) else []
            pred_away = (pred_lineup.get("predicted_away_starting") or []) if isinstance(pred_lineup, dict) else []
            row: dict[str, Any] = {
                "sporttery_match_id": mid,
                "jc_date": day,
                "home_cn": m.get("home_cn") or "",
                "away_cn": m.get("away_cn") or "",
                "home_score": m.get("home_score"),
                "away_score": m.get("away_score"),
                "league": m.get("league") or "",
                "pred_home_key_absent": parsed.get("home_absent_key_count") or 0,
                "pred_away_key_absent": parsed.get("away_absent_key_count") or 0,
                "actual_home_starting_count": actual.get("home_starting_count") if actual else None,
                "actual_away_starting_count": actual.get("away_starting_count") if actual else None,
                "pred_home_starting_count": len(pred_home) if isinstance(pred_home, list) else None,
                "pred_away_starting_count": len(pred_away) if isinstance(pred_away, list) else None,
            }
            # 关键球员 vs 实际首发：优先用 match_lineups 已落库的四字段，否则现场计算
            if actual and (actual.get("home_starting") or actual.get("away_starting")):
                if actual.get("actual_home_key_absent") is not None or actual.get("actual_away_key_absent") is not None:
                    row["actual_home_key_starting"] = actual.get("actual_home_key_starting")
                    row["actual_away_key_starting"] = actual.get("actual_away_key_starting")
                    row["actual_home_key_absent"] = actual.get("actual_home_key_absent")
                    row["actual_away_key_absent"] = actual.get("actual_away_key_absent")
                else:
                    kp = key_player_absence_for_match(doc, actual)
                    row["actual_home_key_starting"] = kp.get("actual_home_key_starting")
                    row["actual_away_key_starting"] = kp.get("actual_away_key_starting")
                    row["actual_home_key_absent"] = kp.get("actual_home_key_absent")
                    row["actual_away_key_absent"] = kp.get("actual_away_key_absent")
            out.append(row)
            if limit is not None and limit > 0 and len(out) >= limit:
                return out
    return out


def lineup_diagnostic_counts(years: list[int] | None = None) -> dict[str, Any]:
    """
    诊断阵容/关键缺阵数据量：jingcai 场次数、match_lineups 场次数、已落库关键缺阵数等，便于排查「数据少」问题。
    years: 如 [2024, 2025, 2026]，仅统计这些年份的 jingcai 文档；None 表示全部。
    """
    db = get_self_improvement_db()
    if db is None:
        return {"error": "未配置 MONGO_URI"}
    out: dict[str, Any] = {}
    # jingcai：主集合 + 备用集合
    jingcai_total = 0
    jingcai_in_years = 0
    jingcai_finished = 0
    year_regex = "|".join(str(y) for y in (years or [2024, 2025, 2026]))
    date_query = {"$or": [{"jc_date": {"$regex": f"^({year_regex})"}}, {"match_date": {"$regex": f"^({year_regex})"}}, {"date": {"$regex": f"^({year_regex})"}}]} if year_regex else {}
    for coll in _jc_match_analysis_collections(db):
        n = coll.count_documents({})
        jingcai_total += n
        if date_query:
            jingcai_in_years += coll.count_documents(date_query)
        # 已完赛：与 _score_from_analysis_doc 一致的多路径——顶层比分、pageTitle.fullCourtGoal/sectionsNo999、wbsjStats 等
        q_fin = {"$or": [
            {"home_score": {"$exists": True, "$ne": None}, "away_score": {"$exists": True, "$ne": None}},
            {"match_head.pageTitle.fullCourtGoal": {"$exists": True, "$ne": None}},
            {"match_head.pageTitle.sectionsNo999": {"$exists": True, "$ne": None}},
            {"match_head.sectionsNo999": {"$exists": True, "$ne": None}},
            {"match_head.value.pageTitle.fullCourtGoal": {"$exists": True, "$ne": None}},
            {"match_head.value.pageTitle.sectionsNo999": {"$exists": True, "$ne": None}},
            {"wbsjStats": {"$exists": True, "$type": "object"}},
            {"match_head.wbsjStats": {"$exists": True, "$type": "object"}},
        ]}
        if date_query:
            q_fin = {"$and": [date_query, q_fin]}
        jingcai_finished += coll.count_documents(q_fin)
    out["jingcai_total_docs"] = jingcai_total
    out["jingcai_in_years_docs"] = jingcai_in_years
    out["jingcai_in_years_finished"] = jingcai_finished
    out["jingcai_years"] = years or [2024, 2025, 2026]
    # match_lineups
    coll_lineups = db[COLLECTION_MATCH_LINEUPS]
    out["match_lineups_total"] = coll_lineups.count_documents({})
    out["match_lineups_with_starting"] = coll_lineups.count_documents({
        "$or": [{"home_starting": {"$exists": True, "$type": "array"}}, {"away_starting": {"$exists": True, "$type": "array"}}]
    })
    out["match_lineups_with_key_absence"] = coll_lineups.count_documents({"actual_home_key_absent": {"$exists": True, "$ne": None}})
    return out


def _lineup_key_absent_items_from_match_lineups(
    date_from: str | None = None,
    date_to: str | None = None,
    years: list[int] | None = None,
    use_predicted_lineup_for_key_absent: bool | None = None,
    also_return_simulate: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    以 match_lineups 已落库关键缺阵的场次为驱动，逐场拉 jingcai 取预计缺阵，生成与 lineup_comparison_report 同结构的行，
    用于 lineup_key_absent_analysis(driver="match_lineups")，避免按日列表漏场次。
    联赛优先从 jingcai 文档解析，若为空则从 jc_matches 该日列表中按 match_id 回填（按日缓存，避免重复查）。
    use_predicted_lineup_for_key_absent: 若指定，则解析时用该值覆盖 config，用于模拟「启用/关闭预计首发」时的预计缺。
    also_return_simulate: 为 True 时在同一遍遍历中再解析一次 use_predicted_lineup_override=True，在每行写入 pred_*_sim，便于单次遍历对比。
    """
    db = get_self_improvement_db()
    if db is None:
        return []
    coll = db[COLLECTION_MATCH_LINEUPS]
    date_from = (date_from or "")[:10] if date_from else ""
    date_to = (date_to or "")[:10] if date_to else ""
    cursor = coll.find(
        {"actual_home_key_absent": {"$exists": True, "$ne": None}, "actual_away_key_absent": {"$exists": True, "$ne": None}},
        {"match_id": 1, "actual_home_key_absent": 1, "actual_away_key_absent": 1},
    ).sort("match_id", 1)
    print("  开始遍历 match_lineups" + ("（含模拟预计首发）" if also_return_simulate else "") + "，每 2000 场打印进度…", flush=True)
    out: list[dict[str, Any]] = []
    jc_matches_cache: dict[str, dict[int, str]] = {}  # day -> {match_id: league}
    empty_league_debug: list[dict[str, Any]] = []  # 前几条联赛为空的文档用于诊断
    for doc in cursor:
        match_id = doc.get("match_id")
        if match_id is None:
            continue
        jingcai = jc_match_analysis_load(int(match_id))
        if not jingcai:
            continue
        day = (_normalize_jc_date(jingcai.get("jc_date") or jingcai.get("match_date") or "") or "")[:10]
        if len(day) >= 4:
            if years and day[:4].isdigit() and int(day[:4]) not in years:
                continue
            if date_from and day < date_from:
                continue
            if date_to and day > date_to:
                continue
        parsed = parse_jingcai_analysis_for_predict(
            jingcai, jingcai.get("match_date"), use_predicted_lineup_override=use_predicted_lineup_for_key_absent
        )
        row: dict[str, Any] = {
            "pred_home_key_absent": parsed.get("home_absent_key_count") or 0,
            "pred_away_key_absent": parsed.get("away_absent_key_count") or 0,
            "actual_home_key_absent": doc.get("actual_home_key_absent"),
            "actual_away_key_absent": doc.get("actual_away_key_absent"),
            "jc_date": day or None,
        }
        if also_return_simulate:
            parsed_sim = parse_jingcai_analysis_for_predict(
                jingcai, jingcai.get("match_date"), use_predicted_lineup_override=True
            )
            row["pred_home_key_absent_sim"] = parsed_sim.get("home_absent_key_count") or 0
            row["pred_away_key_absent_sim"] = parsed_sim.get("away_absent_key_count") or 0
        league = (jingcai.get("league_cn") or "").strip() or _get_league_cn_from_analysis_doc(jingcai) or ""
        # 优先用当日 jc_matches 列表的联赛（getMatchListV1 等常带 tournamentCnShortName），缺失再用 jingcai 详情/回填的 league_cn
        if day:
            if day not in jc_matches_cache:
                mid_to_league: dict[int, str] = {}
                for m in jc_matches_load(day):
                    mid = m.get("sporttery_match_id") or m.get("match_id")
                    if mid is not None:
                        try:
                            mid = int(mid)
                        except (TypeError, ValueError):
                            pass
                        if isinstance(mid, int):
                            lig = (
                                m.get("league") or m.get("league_cn") or m.get("leagueName")
                                or m.get("tournamentCnShortName") or m.get("tournamentName") or m.get("tournamentShortName") or ""
                            )
                            lig = (lig.strip() if isinstance(lig, str) else "")
                            if lig:
                                mid_to_league[mid] = lig
                jc_matches_cache[day] = mid_to_league
            try:
                _mid = int(match_id) if match_id is not None else None
            except (TypeError, ValueError):
                _mid = None
            from_list = (jc_matches_cache.get(day, {}).get(_mid) or "") if _mid is not None else ""
            from_list = (from_list.strip() if isinstance(from_list, str) else "") or ""
            if from_list:
                league = from_list
        if not (league or "").strip() and len(empty_league_debug) < 3:
            empty_league_debug.append({
                "match_id": match_id,
                "jc_date": day,
                "top_keys": list(jingcai.keys()),
                "league_cn_raw": jingcai.get("league_cn"),
                "has_match_head": "match_head" in jingcai or "matchHead" in jingcai,
            })
        row["league"] = league
        out.append(row)
        n = len(out)
        if n % 2000 == 0:
            print(f"  lineup-key-absent 进度: 已处理 {n} 场", flush=True)
    return out, empty_league_debug


def lineup_key_absent_analysis(
    date_from: str | None = None,
    date_to: str | None = None,
    years: list[int] | None = None,
    by_year: bool = False,
    by_league: bool = False,
    driver: str = "match_lineups",
    export_injury_params_path: str | None = None,
    simulate_predicted_lineup: bool = False,
) -> dict[str, Any]:
    """
    对比「预计主/客缺」与「实际主/客关键缺」，做系统性偏差分析，供调 KEY_WEIGHT 或是否用预计首发参与 key_count 参考。
    仅统计有实际阵容数据（actual_*_key_absent 非空）的场次。
    driver: "match_lineups"=以已落库关键缺阵的场次为驱动（覆盖全量，推荐）；"date"=以竞彩按日列表为驱动（可能漏部分场次）。
    simulate_predicted_lineup: 为 True 时，额外用「启用预计首发」重新解析预计缺，输出模拟偏差，用于量化启用 JC_USE_PREDICTED_LINEUP_FOR_KEY_ABSENT 的预期提升。
    返回：total_matches, 主/客的 diff 均值/中位数、高估/低估/持平比例，及可选 by_year 分年统计；若 simulate_predicted_lineup 则含 simulate_use_predicted_lineup。
    """
    if years:
        date_from = date_from or f"{min(years)}-01-01"
        date_to = date_to or f"{max(years)}-12-31"
    empty_league_debug: list[dict[str, Any]] = []
    if driver == "match_lineups":
        valid, empty_league_debug = _lineup_key_absent_items_from_match_lineups(
            date_from=date_from, date_to=date_to, years=years, also_return_simulate=simulate_predicted_lineup
        )
    else:
        items = lineup_comparison_report(date_from=date_from, date_to=date_to, limit=None)
        valid = [
            r
            for r in items
            if r.get("actual_home_key_absent") is not None and r.get("actual_away_key_absent") is not None
        ]
    n = len(valid)
    if n == 0:
        return {
            "date_from": (date_from or "")[:10] or None,
            "date_to": (date_to or "")[:10] or None,
            "total_matches": 0,
            "message": "无同时具备预计缺阵与实际关键缺阵的场次（请确认已拉取阵容 lineups-by-jingcai 且 jingcai 有伤停/关键球员数据）。",
        }
    def stats(diffs: list[int]) -> dict[str, Any]:
        if not diffs:
            return {"mean": None, "median": None, "pct_over": None, "pct_under": None, "pct_exact": None}
        s = sorted(diffs)
        mean = sum(diffs) / len(diffs)
        mid = len(s) // 2
        median = (s[mid] + s[mid - 1]) / 2.0 if len(s) % 2 == 0 else float(s[mid])
        over = sum(1 for d in diffs if d > 0)
        under = sum(1 for d in diffs if d < 0)
        exact = sum(1 for d in diffs if d == 0)
        return {
            "mean": round(mean, 4),
            "median": median,
            "pct_over": round(100.0 * over / len(diffs), 2),
            "pct_under": round(100.0 * under / len(diffs), 2),
            "pct_exact": round(100.0 * exact / len(diffs), 2),
        }

    home_diffs = [r["pred_home_key_absent"] - r["actual_home_key_absent"] for r in valid]
    away_diffs = [r["pred_away_key_absent"] - r["actual_away_key_absent"] for r in valid]
    home_stats = stats(home_diffs)
    away_stats = stats(away_diffs)

    out: dict[str, Any] = {
        "date_from": (date_from or "")[:10] or None,
        "date_to": (date_to or "")[:10] or None,
        "total_matches": n,
        "home": home_stats,
        "away": away_stats,
    }
    if simulate_predicted_lineup and driver == "match_lineups" and valid and valid[0].get("pred_home_key_absent_sim") is not None:
        home_diffs_sim = [r["pred_home_key_absent_sim"] - r["actual_home_key_absent"] for r in valid]
        away_diffs_sim = [r["pred_away_key_absent_sim"] - r["actual_away_key_absent"] for r in valid]
        out_sim = {"home": stats(home_diffs_sim), "away": stats(away_diffs_sim)}
        out["simulate_use_predicted_lineup"] = out_sim
        mean_h_cur = (out.get("home") or {}).get("mean")
        mean_a_cur = (out.get("away") or {}).get("mean")
        mean_h_sim = out_sim.get("home", {}).get("mean")
        mean_a_sim = out_sim.get("away", {}).get("mean")
        cur_mean = (mean_h_cur + mean_a_cur) / 2.0 if mean_h_cur is not None and mean_a_cur is not None else (mean_h_cur or mean_a_cur)
        sim_mean = (mean_h_sim + mean_a_sim) / 2.0 if mean_h_sim is not None and mean_a_sim is not None else (mean_h_sim or mean_a_sim)
        if cur_mean is not None and sim_mean is not None:
            toward_zero = abs(sim_mean) < abs(cur_mean)
            improvement = abs(cur_mean) - abs(sim_mean) if toward_zero else 0.0
            if toward_zero and improvement > 0.1:
                rec = "recommend_lineup_first"
                rec_text = "启用预计首发可明显缩小偏差，建议优先在 .env 设置 JC_USE_PREDICTED_LINEUP_FOR_KEY_ABSENT=true；若启用后偏差仍偏负，再按联赛调高 PREDICT_INJURY_KEY_PLAYER_WEIGHT。"
            elif not toward_zero or abs(sim_mean) > 0.5:
                rec = "recommend_weight_first"
                rec_text = "预计首发对偏差改善有限（可能多数场次无懂球帝/TM 预计首发），建议优先按联赛调高 PREDICT_INJURY_KEY_PLAYER_WEIGHT，再视情况启用预计首发。"
            else:
                rec = "recommend_both"
                rec_text = "启用预计首发后偏差已接近 0，可先启用预计首发；若需进一步强化对缺阵的反应，再按联赛微调 KEY_WEIGHT。"
            out["recommendation"] = rec
            out["recommendation_text"] = rec_text

    if by_year and valid:
        by_y: dict[int, list[dict[str, Any]]] = {}
        for r in valid:
            day = (r.get("jc_date") or "")[:10]
            y = int(day[:4]) if len(day) >= 4 and day[:4].isdigit() else 0
            if y not in by_y:
                by_y[y] = []
            by_y[y].append(r)
        out["by_year"] = {}
        for y in sorted(by_y.keys()):
            sub = by_y[y]
            hd = [x["pred_home_key_absent"] - x["actual_home_key_absent"] for x in sub]
            ad = [x["pred_away_key_absent"] - x["actual_away_key_absent"] for x in sub]
            out["by_year"][y] = {"n": len(sub), "home": stats(hd), "away": stats(ad)}
    if by_league and valid:
        by_lig: dict[str, list[dict[str, Any]]] = {}
        for r in valid:
            lig = (r.get("league") or "").strip() or "(未分类)"
            if lig not in by_lig:
                by_lig[lig] = []
            by_lig[lig].append(r)
        out["by_league"] = {}
        for lig in sorted(by_lig.keys(), key=lambda x: (-len(by_lig[x]), x)):
            sub = by_lig[lig]
            hd = [x["pred_home_key_absent"] - x["actual_home_key_absent"] for x in sub]
            ad = [x["pred_away_key_absent"] - x["actual_away_key_absent"] for x in sub]
            mean_h = (sum(hd) / len(hd)) if hd else None
            mean_a = (sum(ad) / len(ad)) if ad else None
            mean_diff_val = round((mean_h + mean_a) / 2, 4) if mean_h is not None and mean_a is not None else None
            # 建议伤停权重：偏差越负（低估缺阵越严重）建议越高，默认 3.0
            if mean_diff_val is not None:
                if mean_diff_val <= -2.5:
                    suggested_w = 4.0
                elif mean_diff_val <= -2.2:
                    suggested_w = 3.5
                elif mean_diff_val <= -2.0:
                    suggested_w = 3.25
                else:
                    suggested_w = 3.0
            else:
                suggested_w = 3.0
            out["by_league"][lig] = {"n": len(sub), "home": stats(hd), "away": stats(ad), "mean_diff": mean_diff_val, "suggested_PREDICT_INJURY_KEY_PLAYER_WEIGHT": suggested_w}
    if export_injury_params_path and out.get("by_league"):
        import json as _json
        league_cn_to_en = getattr(config, "LEAGUE_CN_TO_EN", {}) or {}
        for_merge: dict[str, dict[str, float]] = {}
        for lig, v in out["by_league"].items():
            if lig.startswith("(") or not v.get("n"):
                continue
            w = v.get("suggested_PREDICT_INJURY_KEY_PLAYER_WEIGHT")
            if w is None:
                continue
            en = league_cn_to_en.get(lig)
            if en:
                for_merge[en] = {"PREDICT_INJURY_KEY_PLAYER_WEIGHT": float(w)}
        payload = {
            "meta": {"source": "lineup_key_absent_analysis", "date_from": out.get("date_from"), "date_to": out.get("date_to"), "total_matches": out.get("total_matches")},
            "by_league_cn": {lig: {"n": v["n"], "mean_diff": v.get("mean_diff"), "suggested_PREDICT_INJURY_KEY_PLAYER_WEIGHT": v.get("suggested_PREDICT_INJURY_KEY_PLAYER_WEIGHT")} for lig, v in out["by_league"].items()},
            "for_merge": for_merge,
        }
        try:
            p = Path(export_injury_params_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("export_injury_params_path write failed: %s", e)
    if empty_league_debug:
        out["_empty_league_debug"] = empty_league_debug
    return out


def _doc_structure_for_league(doc: dict[str, Any]) -> dict[str, Any]:
    """对一条 jingcai 文档生成结构摘要与联赛路径，供 alt_jingcai 与 main_empty_league 共用。"""
    league_keys = ("league", "league_cn", "leagueName", "leagueShortName", "tournamentCnShortName", "tournamentCnName", "tournamentName", "tournamentAbbName", "leagueNameCn", "competitionName", "tournamentShortName")

    def _collect_league_paths(obj: Any, prefix: str, depth: int, out: list[tuple[str, Any]]) -> None:
        if depth <= 0:
            return
        if not isinstance(obj, dict):
            return
        for k, v in obj.items():
            if k in league_keys and v is not None and (isinstance(v, str) or (isinstance(v, (int, float)) and "Id" not in k and "id" not in k)):
                out.append((f"{prefix}.{k}", v))
            if isinstance(v, dict) and k not in ("_id", "wbsjStats") and depth > 1:
                _collect_league_paths(v, f"{prefix}.{k}", depth - 1, out)
            if isinstance(v, list) and v and isinstance(v[0], dict) and depth > 1:
                _collect_league_paths(v[0], f"{prefix}.{k}[0]", depth - 1, out)

    paths: list[tuple[str, Any]] = []
    _collect_league_paths(doc, "doc", 4, paths)

    def _structure_summary(obj: Any, depth: int) -> Any:
        if depth <= 0:
            return "..."
        if not isinstance(obj, dict):
            return type(obj).__name__
        skip = ("wbsjStats", "result_history_all", "result_history_same_tournament", "match_player", "injury_suspension", "match_feature")
        out = {}
        for k, v in list(obj.items())[:30]:
            if k in skip:
                out[k] = f"<{type(v).__name__}>"
            elif isinstance(v, dict):
                out[k] = _structure_summary(v, depth - 1)
            elif isinstance(v, list):
                out[k] = f"list[{len(v)}]" if v else "[]"
            else:
                out[k] = v if k in league_keys and v else f"<{type(v).__name__}>"
        return out

    return {"league_paths_found": paths, "structure_sample": _structure_summary(doc, 3)}


def get_alt_jingcai_doc_structure() -> dict[str, Any]:
    """
    从备用集合 jingcai_match_anlysis 取一条文档，返回其结构摘要（含联赛相关键），
    用于按真实结构补全 _get_league_cn_from_analysis_doc 的解析路径。
    若备用集合不存在，则从主集合取一条作为参考并标注来源。
    """
    db = get_self_improvement_db()
    if db is None:
        return {"error": "未配置 MongoDB"}
    if COLLECTION_JC_MATCH_ANALYSIS_ALT in db.list_collection_names():
        coll = db[COLLECTION_JC_MATCH_ANALYSIS_ALT]
        doc = coll.find_one({"sporttery_match_id": {"$exists": True}})
        if not doc:
            doc = coll.find_one({})
        if not doc:
            return {"error": "备用集合为空", "collection": COLLECTION_JC_MATCH_ANALYSIS_ALT}
        doc = dict(doc)
        doc.pop("_id", None)
        struct = _doc_structure_for_league(doc)
        return {
            "collection": COLLECTION_JC_MATCH_ANALYSIS_ALT,
            "sporttery_match_id": doc.get("sporttery_match_id"),
            "top_level_keys": list(doc.keys()),
            **struct,
        }
    # 备用集合不存在：从主集合取一条 2024–2026 的文档作为结构参考
    year_regex = "2024|2025|2026"
    date_query = {"$or": [{"jc_date": {"$regex": f"^({year_regex})"}}, {"match_date": {"$regex": f"^({year_regex})"}}, {"date": {"$regex": f"^({year_regex})"}}]}
    coll = db[COLLECTION_JC_MATCH_ANALYSIS]
    doc = coll.find_one(date_query)
    if not doc:
        doc = coll.find_one({})
    if not doc:
        return {"error": "主集合也无文档", "collection": COLLECTION_JC_MATCH_ANALYSIS}
    doc = dict(doc)
    doc.pop("_id", None)
    struct = _doc_structure_for_league(doc)
    league_parsed = _get_league_cn_from_analysis_doc(doc)
    return {
        "collection": COLLECTION_JC_MATCH_ANALYSIS,
        "note": "备用集合不存在，此处为主集合一条文档作为结构参考；若你有备用集合的文档可导出为 JSON 后用 main.py alt-jingcai-structure --from-file <path> 查看",
        "sporttery_match_id": doc.get("sporttery_match_id"),
        "league_parsed": league_parsed or "(空)",
        "top_level_keys": list(doc.keys()),
        **struct,
    }


def get_jingcai_doc_structure_from_file(file_path: str) -> dict[str, Any]:
    """
    从本地 JSON 文件读取一条 jingcai 文档（可为备用集合导出），返回结构摘要与联赛解析结果，
    用于按真实结构补全 _get_league_cn_from_analysis_doc。
    """
    import json as _json
    from pathlib import Path
    p = Path(file_path)
    if not p.exists():
        return {"error": f"文件不存在: {file_path}"}
    try:
        raw = p.read_text(encoding="utf-8")
        doc = _json.loads(raw)
    except Exception as e:
        return {"error": f"读取/解析 JSON 失败: {e}"}
    if not isinstance(doc, dict):
        return {"error": "JSON 根节点须为对象"}
    doc = dict(doc)
    doc.pop("_id", None)
    struct = _doc_structure_for_league(doc)
    league_parsed = _get_league_cn_from_analysis_doc(doc)
    return {
        "source": f"文件 {file_path}",
        "sporttery_match_id": doc.get("sporttery_match_id"),
        "league_parsed": league_parsed or "(空)",
        "top_level_keys": list(doc.keys()),
        **struct,
    }


def get_main_jingcai_empty_league_doc_structure(max_checks: int = 500, years: list[int] | None = None, from_lineups: bool = False) -> dict[str, Any]:
    """
    找到第一条「联赛解析为空」的 jingcai 文档，返回其结构摘要，用于补全解析路径。
    from_lineups=False（默认）：直接扫主集合 jingcai_match_analysis 中 jc_date 在 years 内的文档；
    from_lineups=True：从 match_lineups 取 match_id 再 load（与伤停分析同源）。
    years: 默认 [2024,2025,2026]。
    """
    if years is None:
        years = [2024, 2025, 2026]
    db = get_self_improvement_db()
    if db is None:
        return {"error": "未配置 MongoDB"}
    year_regex = "|".join(str(y) for y in years)
    date_query = {"$or": [{"jc_date": {"$regex": f"^({year_regex})"}}, {"match_date": {"$regex": f"^({year_regex})"}}, {"date": {"$regex": f"^({year_regex})"}}]}

    if from_lineups:
        coll_lineups = db[COLLECTION_MATCH_LINEUPS]
        cursor = coll_lineups.find(
            {"actual_home_key_absent": {"$exists": True, "$ne": None}, "actual_away_key_absent": {"$exists": True, "$ne": None}},
            {"match_id": 1},
        ).sort("match_id", 1)
        checked = 0
        for doc in cursor:
            if checked >= max_checks:
                break
            match_id = doc.get("match_id")
            if match_id is None:
                continue
            jingcai = jc_match_analysis_load(int(match_id))
            if not jingcai:
                continue
            day = (_normalize_jc_date(jingcai.get("jc_date") or jingcai.get("match_date") or "") or "")[:10]
            if len(day) >= 4 and day[:4].isdigit() and int(day[:4]) not in years:
                continue
            checked += 1
            league = _get_league_cn_from_analysis_doc(jingcai)
            if league:
                continue
            jingcai = dict(jingcai)
            jingcai.pop("_id", None)
            struct = _doc_structure_for_league(jingcai)
            return {
                "source": "主集合(联赛解析为空的一条,来自match_lineups)",
                "match_id": match_id,
                "jc_date": day,
                "top_level_keys": list(jingcai.keys()),
                **struct,
            }
        return {"error": f"前 {max_checks} 条 lineup（年份 {years}）中未找到联赛为空的文档"}
    # 直接扫主集合 + 备用集合（若存在）
    for coll in _jc_match_analysis_collections(db):
        cursor = coll.find(date_query)
        checked = 0
        for doc in cursor:
            if checked >= max_checks:
                break
            checked += 1
            league = _get_league_cn_from_analysis_doc(doc)
            if league:
                continue
            doc = dict(doc)
            doc.pop("_id", None)
            struct = _doc_structure_for_league(doc)
            mid = doc.get("sporttery_match_id")
            day = (_normalize_jc_date(doc.get("jc_date") or doc.get("match_date") or "") or "")[:10]
            return {
                "source": f"集合 {coll.name}(联赛解析为空的一条)",
                "match_id": mid,
                "jc_date": day,
                "top_level_keys": list(doc.keys()),
                **struct,
            }
    return {"error": f"前 {max_checks} 条（年份 {years}，直接扫集合）中未找到联赛为空的文档"}


def lineup_league_diagnostic(sample_n: int = 5) -> dict[str, Any]:
    """
    诊断「按联赛」为何多为空：抽样 match_lineups 的 match_id，看 jingcai 文档与 jc_matches 的实际结构。
    返回便于打印的摘要，用于确认联赛字段在文档中的真实路径。
    """
    db = get_self_improvement_db()
    if db is None:
        return {"error": "未配置 MongoDB"}
    coll = db[COLLECTION_MATCH_LINEUPS]
    cursor = coll.find(
        {"actual_home_key_absent": {"$exists": True, "$ne": None}, "actual_away_key_absent": {"$exists": True, "$ne": None}},
        {"match_id": 1},
    ).limit(sample_n * 20)  # 多取一些，避免前几条恰好来自同一日
    samples: list[dict[str, Any]] = []
    seen_days: set[str] = set()
    jc_matches_sample: dict[str, Any] | None = None
    for doc in cursor:
        if len(samples) >= sample_n:
            break
        match_id = doc.get("match_id")
        if match_id is None:
            continue
        jingcai = jc_match_analysis_load(int(match_id))
        if not jingcai:
            continue
        day = (_normalize_jc_date(jingcai.get("jc_date") or jingcai.get("match_date") or "") or "")[:10]
        league = _get_league_cn_from_analysis_doc(jingcai)
        top_keys = list(jingcai.keys()) if isinstance(jingcai, dict) else []
        match_head = jingcai.get("match_head") or jingcai.get("matchHead") or {}
        pt = (match_head.get("pageTitle") or match_head.get("page_title") or match_head) if isinstance(match_head, dict) else None
        pt_keys = list(pt.keys()) if isinstance(pt, dict) else []
        pt_league_fields = {}
        if isinstance(pt, dict):
            for k in ("tournamentCnShortName", "leagueName", "leagueShortName", "tournamentName", "leagueNameCn", "tournamentCnName"):
                if k in pt and pt[k] is not None:
                    pt_league_fields[k] = pt[k]
        samples.append({
            "match_id": match_id,
            "jc_date": day,
            "league_parsed": league or "(空)",
            "jingcai_top_keys": top_keys,
            "match_head_keys": list(match_head.keys()) if isinstance(match_head, dict) else [],
            "pageTitle_keys": pt_keys,
            "pageTitle_league_values": pt_league_fields,
        })
        if day and day not in seen_days and jc_matches_sample is None:
            seen_days.add(day)
            list_matches = jc_matches_load(day)
            if list_matches:
                first = list_matches[0]
                jc_matches_sample = {
                    "date": day,
                    "first_item_keys": list(first.keys()) if isinstance(first, dict) else [],
                    "league_fields": {k: first.get(k) for k in ("league", "league_cn", "leagueName", "tournamentCnShortName", "tournamentName", "tournamentShortName") if first.get(k) is not None},
                }
    return {"samples": samples, "jc_matches_sample": jc_matches_sample, "message": "若 league_parsed 多为(空)，请根据 jingcai_top_keys/pageTitle_league_values 和 jc_matches_sample 确认联赛实际字段路径"}


def merge_injury_params_into_predict_league(
    injury_params_path: str,
    league_params_path: str | None = None,
) -> dict[str, Any]:
    """
    将 lineup-key-absent-analysis --export-injury-params 生成的 JSON 中的 for_merge 合并进
    predict_league_params.json，只新增或更新 PREDICT_INJURY_KEY_PLAYER_WEIGHT，不覆盖其他键。
    返回 {"merged": [联赛名], "league_params_path": 写入路径}。
    """
    import json as _json
    league_params_path = league_params_path or str(config.PREDICT_LEAGUE_PARAMS_PATH)
    path_inj = Path(injury_params_path)
    path_league = Path(league_params_path)
    if not path_inj.is_file():
        return {"error": f"injury params file not found: {injury_params_path}", "merged": []}
    try:
        data = _json.loads(path_inj.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": str(e), "merged": []}
    for_merge = data.get("for_merge") or {}
    if not for_merge:
        return {"message": "no for_merge in file", "merged": [], "league_params_path": str(path_league)}
    league_data: dict[str, dict[str, Any]] = {}
    if path_league.is_file():
        try:
            league_data = _json.loads(path_league.read_text(encoding="utf-8"))
            if not isinstance(league_data, dict):
                league_data = {}
        except Exception:
            league_data = {}
    merged: list[str] = []
    for en_name, overrides in for_merge.items():
        if not en_name or not isinstance(overrides, dict):
            continue
        w = overrides.get("PREDICT_INJURY_KEY_PLAYER_WEIGHT")
        if w is None:
            continue
        if en_name not in league_data:
            league_data[en_name] = {}
        league_data[en_name]["PREDICT_INJURY_KEY_PLAYER_WEIGHT"] = float(w)
        merged.append(en_name)
    try:
        path_league.parent.mkdir(parents=True, exist_ok=True)
        path_league.write_text(_json.dumps(league_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return {"error": f"write failed: {e}", "merged": merged, "league_params_path": str(path_league)}
    return {"merged": merged, "league_params_path": str(path_league)}


def lineup_stats_aggregate(
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """
    统计：在指定日期范围内已完赛场次中，有 match_lineups 数据的场次的场均首发人数。
    返回 total_finished, total_with_lineup, avg_home_starting, avg_away_starting。
    """
    db = get_self_improvement_db()
    if db is None:
        return {"total_finished": 0, "total_with_lineup": 0, "avg_home_starting": None, "avg_away_starting": None}
    date_from = (date_from or "")[:10] if date_from else ""
    date_to = (date_to or "")[:10] if date_to else ""
    total_finished = 0
    total_with_lineup = 0
    sum_home = 0
    sum_away = 0
    for d in jc_match_analysis_list_dates():
        day = (d.get("date") or "")[:10]
        if len(day) < 10:
            continue
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        for m in jc_match_analysis_list_by_jc_date(day):
            if m.get("home_score") is None or m.get("away_score") is None:
                continue
            total_finished += 1
            mid = m.get("match_id")
            if mid is None:
                continue
            actual = lineups_get_by_match_id(int(mid))
            if not actual:
                continue
            hc = actual.get("home_starting_count")
            ac = actual.get("away_starting_count")
            if hc is None and ac is None:
                continue
            total_with_lineup += 1
            if hc is not None:
                sum_home += hc
            if ac is not None:
                sum_away += ac
    n = total_with_lineup
    return {
        "date_from": date_from or None,
        "date_to": date_to or None,
        "total_finished": total_finished,
        "total_with_lineup": total_with_lineup,
        "avg_home_starting": round(sum_home / n, 2) if n and sum_home else None,
        "avg_away_starting": round(sum_away / n, 2) if n and sum_away else None,
    }


def parse_jingcai_analysis_for_predict(
    doc: dict[str, Any],
    match_date: str | None = None,
    use_predicted_lineup_override: bool | None = None,
) -> dict[str, Any]:
    """
    从 jingcai_match_analysis 单条文档解析出可供 predict_match 使用的参数字典。
    返回: odds, home_absent, away_absent, home_matches_next_7d, away_matches_next_7d,
          home_absent_key_count, away_absent_key_count（伤停中属于射手/关键球员的人数），
          h2h_override（体彩历史交锋解析出的 (主队场均进, 主队场均失, 客队场均进, 客队场均失, 场数)）。
    用于竞彩预测时把体彩伤停、赔率、未来场次、关键球员伤停、历史交锋传入模型。
    """
    out: dict[str, Any] = {
        "odds": None,
        "home_absent": [],
        "away_absent": [],
        "home_matches_next_7d": None,
        "away_matches_next_7d": None,
        "home_absent_key_count": 0,
        "away_absent_key_count": 0,
        "h2h_override": None,  # (home_for, home_against, away_for, away_against, n) 与 repo.predict_head_to_head 同格式
        "actual_home_starting_count": None,  # 实际首发人数（来自 match_lineups，复盘用）
        "actual_away_starting_count": None,
        "home_team_stats_prior": None,  # dict: goals_for_pg, goals_against_pg（来自 api_football_team_statistics）
        "away_team_stats_prior": None,
        "home_coach_tenure_days": None,
        "away_coach_tenure_days": None,
    }
    # 赔率：文档顶层 odds 或 match_head 内
    odds = doc.get("odds") if isinstance(doc.get("odds"), dict) else {}
    if odds and (odds.get("spf_win") or odds.get("spf_draw") or odds.get("spf_loss")):
        out["odds"] = {
            "spf_win": str(odds["spf_win"]) if odds.get("spf_win") is not None else None,
            "spf_draw": str(odds["spf_draw"]) if odds.get("spf_draw") is not None else None,
            "spf_loss": str(odds["spf_loss"]) if odds.get("spf_loss") is not None else None,
        }
        if not (out["odds"]["spf_win"] and out["odds"]["spf_draw"] and out["odds"]["spf_loss"]):
            out["odds"] = None
        elif out["odds"] is not None:
            # 让球胜平负：传入 predict_match / jc_play_markets.rqspf（原先只传胜平负会丢失让球数）
            if odds.get("rqspf_handicap") is not None:
                gl = odds["rqspf_handicap"]
                try:
                    out["odds"]["rqspf_handicap"] = int(float(gl)) if float(gl) == int(float(gl)) else float(gl)
                except (TypeError, ValueError):
                    out["odds"]["rqspf_handicap"] = gl
            for k_src, k_dst in (
                ("rqspf_win", "rqspf_win"),
                ("rqspf_draw", "rqspf_draw"),
                ("rqspf_loss", "rqspf_loss"),
            ):
                if odds.get(k_src) is not None:
                    out["odds"][k_dst] = str(odds[k_src])
            # 总进球数盘口：供 jc_play_markets.zjqs 与赔率隐含融合
            zj = odds.get("zjqs")
            if isinstance(zj, dict) and zj:
                out["odds"]["zjqs"] = {str(k): str(v) for k, v in zj.items() if v is not None}
    else:
        out["odds"] = None

    # 伤停：体彩 injury_suspension 多种结构兼容
    inj = doc.get("injury_suspension")
    if inj and isinstance(inj, dict):
        home_list = (
            inj.get("mainTeamList") or inj.get("homeList") or inj.get("homeInjuryList") or inj.get("homeTeamInjury") or []
        )
        away_list = (
            inj.get("guestTeamList") or inj.get("awayList") or inj.get("awayInjuryList") or inj.get("awayTeamInjury") or []
        )
        if not home_list and (inj.get("home") and isinstance(inj["home"], (list, dict))):
            h = inj["home"]
            home_list = h.get("list") or h.get("injuryList") or (h if isinstance(h, list) else [])
        if not away_list and (inj.get("away") and isinstance(inj["away"], (list, dict))):
            a = inj["away"]
            away_list = a.get("list") or a.get("injuryList") or (a if isinstance(a, list) else [])
        if isinstance(home_list, dict):
            home_list = home_list.get("list") or home_list.get("playerList") or []
        if isinstance(away_list, dict):
            away_list = away_list.get("list") or away_list.get("playerList") or []
        if not isinstance(home_list, list):
            home_list = []
        if not isinstance(away_list, list):
            away_list = []

        def _player_to_absent(rows: list) -> list[dict[str, Any]]:
            result = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = (
                    row.get("playerName") or row.get("personName") or row.get("name") or row.get("playerNameCn") or ""
                )
                if not name:
                    continue
                reason = (
                    row.get("status") or row.get("state") or row.get("injuryStatus") or row.get("statusCn") or row.get("type")
                )
                if reason is None and (row.get("injuryFlag") or row.get("suspensionFlag")):
                    reason = "伤" if row.get("injuryFlag") else "停"
                result.append({"name": str(name).strip(), "reason": str(reason).strip() if reason is not None else ""})
            return result

        out["home_absent"] = _player_to_absent(home_list)
        out["away_absent"] = _player_to_absent(away_list)
    elif isinstance(inj, list) and len(inj) >= 1:
        # 可能 [主队列表, 客队列表]
        if isinstance(inj[0], list) and len(inj[0]) and isinstance(inj[0][0], dict):
            out["home_absent"] = [
                {"name": (r.get("playerName") or r.get("name") or ""), "reason": (r.get("status") or r.get("reason") or "")}
                for r in inj[0]
                if isinstance(r, dict)
            ]
        if len(inj) >= 2 and isinstance(inj[1], list) and len(inj[1]) and isinstance(inj[1][0], dict):
            out["away_absent"] = [
                {"name": (r.get("playerName") or r.get("name") or ""), "reason": (r.get("status") or r.get("reason") or "")}
                for r in inj[1]
                if isinstance(r, dict)
            ]

    # 未来场次：未来 7 天内比赛数（从 match_date 次日算起）
    future = doc.get("future_matches")
    if future and isinstance(future, dict) and match_date and len((match_date or "")[:10]) == 10:
        try:
            from datetime import datetime, timedelta
            d0 = datetime.strptime((match_date or "")[:10], "%Y-%m-%d")
            d_from = (d0 + timedelta(days=1)).date()
            d_to = (d0 + timedelta(days=7)).date()

            def _count_in_range(block: Any) -> int:
                if not block:
                    return 0
                if isinstance(block, list):
                    lst = []
                    for item in block:
                        if isinstance(item, dict) and isinstance(item.get("matchList"), list):
                            lst.extend(item["matchList"])
                        elif isinstance(item, list):
                            lst.extend(item)
                    list_ = lst if lst else block
                elif isinstance(block, dict) and isinstance(block.get("matchList"), list):
                    list_ = block["matchList"]
                else:
                    list_ = block if isinstance(block, list) else []
                count = 0
                for row in list_:
                    if not isinstance(row, dict):
                        continue
                    dt = row.get("matchDateTime") or row.get("matchDate") or row.get("date") or ""
                    if not dt:
                        continue
                    s = (str(dt).replace("T", " ").split(" ")[0])[:10]
                    if len(s) != 10:
                        continue
                    try:
                        md = datetime.strptime(s, "%Y-%m-%d").date()
                        if d_from <= md <= d_to:
                            count += 1
                    except ValueError:
                        pass
                return count

            out["home_matches_next_7d"] = _count_in_range(future.get("home") or future.get("mainTeamMatchList"))
            out["away_matches_next_7d"] = _count_in_range(future.get("away") or future.get("guestTeamMatchList"))
        except Exception:
            pass

    # 历史交锋：体彩 result_history_same_tournament（同赛制）优先，无则 result_history_all，解析为 (主队场均进, 主队场均失, 客队场均进, 客队场均失, 场数)
    for hist_key in ("result_history_same_tournament", "result_history_all"):
        raw = doc.get(hist_key)
        if not raw:
            continue
        if isinstance(raw, dict):
            raw = raw.get("matchList") or raw.get("matchResult") or raw.get("list") or raw.get("data") or []
        if isinstance(raw, list) and len(raw) > 0:
            scores: list[tuple[int, int]] = []
            for m in raw:
                if not isinstance(m, dict):
                    continue
                ft = m.get("sectionsNo999") or m.get("fullTimeScore") or m.get("score")
                h, a = None, None
                if isinstance(ft, str) and ":" in ft:
                    parts = ft.replace("：", ":").split(":")
                    if len(parts) >= 2:
                        try:
                            h, a = int(parts[0].strip()), int(parts[1].strip())
                        except (ValueError, TypeError):
                            pass
                if h is None or a is None:
                    h = m.get("homeScore") or m.get("hostScore") or m.get("homeGoal")
                    a = m.get("awayScore") or m.get("guestScore") or m.get("awayGoal")
                    if h is not None and a is not None:
                        try:
                            h, a = int(h), int(a)
                        except (TypeError, ValueError):
                            h, a = None, None
                if h is not None and a is not None:
                    scores.append((h, a))
            if scores:
                n = len(scores)
                home_for = sum(s[0] for s in scores) / n
                home_against = sum(s[1] for s in scores) / n
                # 与 repo.predict_head_to_head 一致：(主队场均进, 主队场均失, 客队场均进, 客队场均失, 场数)
                out["h2h_override"] = (home_for, home_against, home_against, home_for, n)
                break
        if out.get("h2h_override") is not None:
            break

    # 关键球员伤停：从 match_player（射手信息）取主客队关键球员名单，统计伤停中有几人命中
    def _names_from_player_list(rows: list) -> set[str]:
        names: set[str] = set()
        for row in (rows or []) if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = (
                row.get("personName") or row.get("playerName") or row.get("name")
                or row.get("playerNameCn") or row.get("person名") or ""
            )
            if name and str(name).strip():
                names.add(_norm_name_for_key(str(name).strip()))
        return names

    def _norm_name_for_key(s: str) -> str:
        return (s or "").strip().lower().replace(" ", "").replace("　", "")

    def _count_absent_in_key(absent_list: list, key_names: set[str]) -> int:
        if not key_names:
            return 0
        count = 0
        for a in absent_list or []:
            if not isinstance(a, dict):
                continue
            aname = (a.get("name") or "").strip()
            if not aname:
                continue
            norm_absent = _norm_name_for_key(aname)
            if norm_absent in key_names:
                count += 1
                continue
            for kn in key_names:
                if kn in norm_absent or norm_absent in kn:
                    count += 1
                    break
        return count

    home_key_names: set[str] = set()
    away_key_names: set[str] = set()
    mp = doc.get("match_player")
    if mp and isinstance(mp, dict):
        home_list: list = []
        away_list_raw: list = []
        if mp.get("home") is not None:
            if isinstance(mp["home"], list):
                home_list = mp["home"]
            elif isinstance(mp["home"], dict):
                home_list = mp["home"].get("playerList") or mp["home"].get("scorerList") or mp["home"].get("list") or []
        if mp.get("away") is not None:
            if isinstance(mp["away"], list):
                away_list_raw = mp["away"]
            elif isinstance(mp["away"], dict):
                away_list_raw = mp["away"].get("playerList") or mp["away"].get("scorerList") or mp["away"].get("list") or []
        if not isinstance(home_list, list):
            home_list = []
        if not isinstance(away_list_raw, list):
            away_list_raw = []
        home_key_names = _names_from_player_list(home_list)
        away_key_names = _names_from_player_list(away_list_raw)
        if not home_key_names and not away_key_names:
            home_list = mp.get("homePlayerList") or mp.get("homeList") or (mp.get("home") if isinstance(mp.get("home"), list) else [])
            away_list_raw = mp.get("awayPlayerList") or mp.get("awayList") or (mp.get("away") if isinstance(mp.get("away"), list) else [])
            home_key_names = _names_from_player_list(home_list)
            away_key_names = _names_from_player_list(away_list_raw)
        out["home_absent_key_count"] = _count_absent_in_key(out.get("home_absent") or [], home_key_names)
        out["away_absent_key_count"] = _count_absent_in_key(out.get("away_absent") or [], away_key_names)

    # 预计首发（懂球帝/TM）：仅当配置开启时，用赛前预计首发叠加「关键球员未在预计首发」到 key_count。默认开启（见 config.JC_USE_PREDICTED_LINEUP_FOR_KEY_ABSENT），以缩小预计缺与实际关键缺的差距。
    use_pred = use_predicted_lineup_override if use_predicted_lineup_override is not None else getattr(config, "JC_USE_PREDICTED_LINEUP_FOR_KEY_ABSENT", True)
    if use_pred:
        try:
            mid = doc.get("sporttery_match_id")
            if mid is not None and (home_key_names or away_key_names):
                lineup_doc = dongqiudi_lineups_load_by_sporttery_match_id(int(mid))
                if not lineup_doc or not isinstance(lineup_doc, dict):
                    lineup_doc = transfermarkt_lineups_load_by_sporttery_match_id(int(mid))
                dq = lineup_doc
                if dq and isinstance(dq, dict):
                    pred_home = dq.get("predicted_home_starting") or []
                    pred_away = dq.get("predicted_away_starting") or []
                    if isinstance(pred_home, list) and isinstance(pred_away, list):
                        pred_home_set = {_norm_name_for_key(str(p)) for p in pred_home if p}
                        pred_away_set = {_norm_name_for_key(str(p)) for p in pred_away if p}
                        absent_home_norm = {_norm_name_for_key((a.get("name") or "").strip()) for a in (out.get("home_absent") or []) if isinstance(a, dict)}
                        absent_away_norm = {_norm_name_for_key((a.get("name") or "").strip()) for a in (out.get("away_absent") or []) if isinstance(a, dict)}
                        extra_home = sum(1 for n in home_key_names if n not in pred_home_set and n not in absent_home_norm)
                        extra_away = sum(1 for n in away_key_names if n not in pred_away_set and n not in absent_away_norm)
                        out["home_absent_key_count"] = (out.get("home_absent_key_count") or 0) + extra_home
                        out["away_absent_key_count"] = (out.get("away_absent_key_count") or 0) + extra_away
        except Exception:
            pass

    # 射手榜加权：伤停名单中若出现在该联赛射手榜（api_football_topscorers），且未在 match_player 关键名单中，则补算为关键缺阵
    try:
        league_cn = (doc.get("league_cn") or "").strip() or _get_league_cn_from_analysis_doc(doc)
        league_id: int | None = LEAGUE_CN_TO_API_ID.get((league_cn or "").strip()) if league_cn else None
        if league_id is None and doc.get("api_football_league_id") is not None:
            try:
                league_id = int(doc["api_football_league_id"])
            except (TypeError, ValueError):
                pass
        season_arg: int | None = None
        if match_date and len((match_date or "")[:4]) == 4 and (match_date or "")[:4].isdigit():
            try:
                season_arg = int((match_date or "")[:4])
            except ValueError:
                pass
        if league_id is not None and season_arg is not None:
            data = load_topscorers(league_id=league_id, season=season_arg)
            players = data.get((league_id, season_arg), [])
            if players:
                topscorer_norm: set[str] = set()
                for p in players:
                    if not isinstance(p, dict):
                        continue
                    name = (p.get("player") or {})
                    if isinstance(name, dict):
                        name = name.get("name") or ""
                    else:
                        name = p.get("name") or ""
                    if name and str(name).strip():
                        topscorer_norm.add(_norm_name_for_key(str(name).strip()))
                if topscorer_norm:
                    for a in out.get("home_absent") or []:
                        if not isinstance(a, dict):
                            continue
                        aname = (a.get("name") or "").strip()
                        if not aname:
                            continue
                        norm_absent = _norm_name_for_key(aname)
                        if norm_absent in topscorer_norm and norm_absent not in home_key_names:
                            out["home_absent_key_count"] = (out.get("home_absent_key_count") or 0) + 1
                            home_key_names.add(norm_absent)
                    for a in out.get("away_absent") or []:
                        if not isinstance(a, dict):
                            continue
                        aname = (a.get("name") or "").strip()
                        if not aname:
                            continue
                        norm_absent = _norm_name_for_key(aname)
                        if norm_absent in topscorer_norm and norm_absent not in away_key_names:
                            out["away_absent_key_count"] = (out.get("away_absent_key_count") or 0) + 1
                            away_key_names.add(norm_absent)
    except Exception:
        pass

    # 阵容名单（squads）：伤停中若在该队 api_football_squads 名单内且未计入关键缺阵，则补算为关键缺阵
    # 球队赛季统计（team_statistics）与教练任期（coaches）：供 predict 样本少时先验与换帅稳定性
    try:
        names_tuples = _team_names_from_analysis_doc(doc)
        home_cn = (names_tuples[0][0] or "").strip() if len(names_tuples) > 0 else ""
        away_cn = (names_tuples[1][0] or "").strip() if len(names_tuples) > 1 else ""
        home_api_tid = get_api_football_team_id_by_name_cn(home_cn) if home_cn else None
        away_api_tid = get_api_football_team_id_by_name_cn(away_cn) if away_cn else None

        if home_api_tid is not None or away_api_tid is not None:
            # Squads：缺阵且在该队阵容中则计为关键缺阵
            for api_tid, key_names_set, absent_list, side in [
                (home_api_tid, home_key_names, out.get("home_absent") or [], "home"),
                (away_api_tid, away_key_names, out.get("away_absent") or [], "away"),
            ]:
                if api_tid is None or not absent_list:
                    continue
                squads_map = load_squads(team_id=api_tid)
                squad_players = squads_map.get(api_tid, [])
                squad_norm: set[str] = set()
                for pl in squad_players:
                    if not isinstance(pl, dict):
                        continue
                    name = (pl.get("name") or pl.get("player", {}).get("name") if isinstance(pl.get("player"), dict) else "") or ""
                    if name and str(name).strip():
                        squad_norm.add(_norm_name_for_key(str(name).strip()))
                if not squad_norm:
                    continue
                for a in absent_list:
                    if not isinstance(a, dict):
                        continue
                    aname = (a.get("name") or "").strip()
                    if not aname:
                        continue
                    norm_absent = _norm_name_for_key(aname)
                    if norm_absent in squad_norm and norm_absent not in key_names_set:
                        if side == "home":
                            out["home_absent_key_count"] = (out.get("home_absent_key_count") or 0) + 1
                            home_key_names.add(norm_absent)
                        else:
                            out["away_absent_key_count"] = (out.get("away_absent_key_count") or 0) + 1
                            away_key_names.add(norm_absent)

        league_cn = (doc.get("league_cn") or "").strip() or _get_league_cn_from_analysis_doc(doc)
        league_id_prior: int | None = LEAGUE_CN_TO_API_ID.get((league_cn or "").strip()) if league_cn else None
        if league_id_prior is None and doc.get("api_football_league_id") is not None:
            try:
                league_id_prior = int(doc["api_football_league_id"])
            except (TypeError, ValueError):
                pass
        season_prior: int | None = None
        if match_date and len((match_date or "")[:4]) == 4 and (match_date or "")[:4].isdigit():
            try:
                season_prior = int((match_date or "")[:4])
            except ValueError:
                pass

        if league_id_prior is not None and season_prior is not None:
            # 球队赛季统计：提取场均进球/失球作先验
            def _stats_prior_from_dict(stat: dict[str, Any]) -> dict[str, float] | None:
                if not stat or not isinstance(stat, dict):
                    return None
                goals = stat.get("goals") if isinstance(stat.get("goals"), dict) else {}
                fixtures = stat.get("fixtures") if isinstance(stat.get("fixtures"), dict) else {}
                played = None
                if isinstance(fixtures.get("played"), dict):
                    played = fixtures["played"].get("total")
                elif fixtures.get("played") is not None:
                    played = fixtures["played"]
                if played is None or int(played) == 0:
                    return None
                played = int(played)
                g_for = goals.get("for")
                if isinstance(g_for, dict):
                    g_for = g_for.get("total")
                g_against = goals.get("against")
                if isinstance(g_against, dict):
                    g_against = g_against.get("total")
                if g_for is None or g_against is None:
                    return None
                try:
                    for_val = float(g_for)
                    against_val = float(g_against)
                except (TypeError, ValueError):
                    return None
                return {"goals_for_pg": for_val / played, "goals_against_pg": against_val / played}

            data_ts = load_team_statistics(league_id=league_id_prior, season=season_prior)
            if home_api_tid is not None:
                st = data_ts.get((league_id_prior, home_api_tid, season_prior))
                if isinstance(st, dict):
                    out["home_team_stats_prior"] = _stats_prior_from_dict(st)
            if away_api_tid is not None:
                st = data_ts.get((league_id_prior, away_api_tid, season_prior))
                if isinstance(st, dict):
                    out["away_team_stats_prior"] = _stats_prior_from_dict(st)

        # 教练任期（换帅/稳定性）：从 api_football_coaches 取当前教练任职起始日，算天数
        def _coach_tenure_days(coaches_list: list) -> int | None:
            if not coaches_list or not isinstance(coaches_list, list):
                return None
            from datetime import datetime, date
            ref_date: date | None = None
            if match_date and len((match_date or "")[:10]) == 10:
                try:
                    ref_date = datetime.strptime((match_date or "")[:10], "%Y-%m-%d").date()
                except ValueError:
                    pass
            if ref_date is None:
                ref_date = date.today()
            for c in coaches_list:
                if not isinstance(c, dict):
                    continue
                career = c.get("career")
                if isinstance(career, list) and len(career) > 0:
                    first = career[0]
                    if isinstance(first, dict) and first.get("start"):
                        try:
                            start_s = str(first["start"])[:10]
                            start_d = datetime.strptime(start_s, "%Y-%m-%d").date()
                            end_s = (first.get("end") or "")[:10]
                            end_d = datetime.strptime(end_s, "%Y-%m-%d").date() if end_s and len(end_s) == 10 else None
                            ref = end_d if end_d else ref_date
                            delta = (ref - start_d).days
                            return max(0, delta)
                        except Exception:
                            continue
            return None

        if home_api_tid is not None:
            coaches_h = load_coaches(team_id=home_api_tid)
            out["home_coach_tenure_days"] = _coach_tenure_days(coaches_h.get(home_api_tid, []))
        if away_api_tid is not None:
            coaches_a = load_coaches(team_id=away_api_tid)
            out["away_coach_tenure_days"] = _coach_tenure_days(coaches_a.get(away_api_tid, []))
    except Exception:
        pass

    # 实际首发人数（来自 match_lineups，用于复盘/统计：对比预计 vs 实际、轮换等）
    try:
        mid = doc.get("sporttery_match_id")
        if mid is not None:
            actual = lineups_get_by_match_id(int(mid))
            if actual and isinstance(actual, dict):
                hc = actual.get("home_starting_count")
                ac = actual.get("away_starting_count")
                if hc is not None or ac is not None:
                    out["actual_home_starting_count"] = hc if hc is not None else None
                    out["actual_away_starting_count"] = ac if ac is not None else None
    except Exception:
        pass

    return out


def _team_names_from_analysis_doc(doc: dict[str, Any]) -> list[tuple[str, str | None]]:
    """从单条 jingcai_match_analysis 文档中提取主客队 (name_cn, name_en)，返回 [(home_cn, home_en), (away_cn, away_en)]。"""
    pt = (doc.get("match_head") or {}).get("pageTitle") or doc.get("match_head") or {}
    if not isinstance(pt, dict):
        pt = {}
    home_cn = (pt.get("homeTeamShortName") or pt.get("homeTeamAbbName") or "").strip()
    away_cn = (pt.get("awayTeamShortName") or pt.get("awayTeamAbbName") or "").strip()
    home_en = (pt.get("homeTeamAbbEnName") or pt.get("homeTeamEnName") or pt.get("homeTeamNameEn") or "").strip() or None
    away_en = (pt.get("awayTeamAbbEnName") or pt.get("awayTeamEnName") or pt.get("awayTeamNameEn") or "").strip() or None
    out: list[tuple[str, str | None]] = []
    if home_cn:
        out.append((home_cn, home_en))
    if away_cn:
        out.append((away_cn, away_en))
    return out


def _get_jc_match_analysis_collection(db):
    """优先使用 jingcai_match_anlysis（兼容拼写），若存在且有数据则用该集合；否则用 jingcai_match_analysis。"""
    if COLLECTION_JC_MATCH_ANALYSIS_ALT in db.list_collection_names():
        alt = db[COLLECTION_JC_MATCH_ANALYSIS_ALT]
        if alt.count_documents({}) > 0:
            return alt, COLLECTION_JC_MATCH_ANALYSIS_ALT
    return db[COLLECTION_JC_MATCH_ANALYSIS], COLLECTION_JC_MATCH_ANALYSIS


def jc_teams_sync_from_analysis() -> dict[str, Any]:
    """
    从 jingcai_match_analysis（或 jingcai_match_anlysis）中整理主客队队名，去重后仅向 jingcai_teams 补全缺失的队名。
    已存在于 jingcai_teams 的记录一律不覆盖，只插入分析表中有而表中没有的 name_cn。
    每条新记录: name_cn（唯一）, name_en（可选）, updated_at。
    返回: {"written": n, "total_teams": n, "scanned_docs": n, "source": 使用的集合名}。
    """
    db = get_self_improvement_db()
    if db is None:
        return {"written": 0, "total_teams": 0, "scanned_docs": 0, "source": None, "message": "未配置 MONGO_URI"}
    coll_analysis, source_name = _get_jc_match_analysis_collection(db)
    coll_teams = db[COLLECTION_JC_TEAMS]
    # name_cn -> best name_en (non-empty preferred)，去重
    teams: dict[str, str | None] = {}
    scanned = 0
    for doc in coll_analysis.find({}):
        scanned += 1
        for name_cn, name_en in _team_names_from_analysis_doc(doc):
            key = name_cn.strip()
            if not key:
                continue
            if key not in teams or (name_en and not teams[key]):
                teams[key] = name_en or teams.get(key)
    # 只补全：已有 name_cn 不覆盖，仅插入表中不存在的队名
    existing_names = {doc["name_cn"] for doc in coll_teams.find({}, {"name_cn": 1})}
    written = 0
    for name_cn, name_en in teams.items():
        if name_cn in existing_names:
            continue
        doc = _sanitize_for_mongo({
            "name_cn": name_cn,
            "name_en": (name_en or "").strip() or None,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        })
        coll_teams.insert_one(doc)
        written += 1
        existing_names.add(name_cn)  # 同批内去重，避免重复插入
    return {"written": written, "total_teams": len(teams), "scanned_docs": scanned, "source": source_name}


def get_api_football_team_id_by_name_cn(name_cn: str) -> int | None:
    """根据 jingcai_teams 中的 name_cn 查询 api_football_team_id，用于按队拉取 squads/team_statistics/coaches。"""
    if not (name_cn or "").strip():
        return None
    db = get_self_improvement_db()
    if db is None:
        return None
    doc = db[COLLECTION_JC_TEAMS].find_one({"name_cn": (name_cn or "").strip()}, {"api_football_team_id": 1})
    if not doc:
        return None
    tid = doc.get("api_football_team_id")
    if tid is None:
        return None
    try:
        return int(tid)
    except (TypeError, ValueError):
        return None


def jc_teams_list() -> list[dict[str, Any]]:
    """从 jingcai_teams 表读取全部队名，按 name_cn 排序。"""
    db = get_self_improvement_db()
    if db is None:
        return []
    out = []
    for doc in db[COLLECTION_JC_TEAMS].find({}).sort("name_cn", 1):
        doc.pop("_id", None)
        out.append(doc)
    return out


def jc_teams_sync_to_main_teams() -> dict[str, Any]:
    """
    将 jingcai_teams 中已有 name_en 的队伍同步到主库 teams 表（按需插入，不覆盖已有）。
    每条调用 repo.ensure_team_by_name(name_en, name_zh=name_cn)，保证主库 teams 有对应 id 与 name/name_zh。
    返回: {"processed": n, "message": str}。需配置主库（USE_MONGO + MONGO_URI 等）。
    """
    from db import repo
    repo.init_db()
    db = get_self_improvement_db()
    if db is None:
        return {"processed": 0, "message": "未配置 MONGO_URI"}
    coll = db[COLLECTION_JC_TEAMS]
    processed = 0
    for doc in coll.find({}):
        name_cn = (doc.get("name_cn") or "").strip()
        name_en = (doc.get("name_en") or "").strip()
        if not name_en:
            continue
        repo.ensure_team_by_name(name_en, name_zh=name_cn or None)
        processed += 1
    return {"processed": processed, "message": f"已按 jingcai_teams 同步 {processed} 支队伍到主库 teams（已存在则跳过）。"}


def jc_teams_from_analysis_for_year(year: int) -> dict[str, Any]:
    """
    从 jingcai_match_analysis 中取出指定年份（按 jc_date/match_date）出现的所有主客队中文名，去重。
    返回: {"teams": [name_cn, ...], "count": n, "scanned_docs": n, "source": 集合名}。
    """
    db = get_self_improvement_db()
    if db is None:
        return {"teams": [], "count": 0, "scanned_docs": 0, "source": None, "message": "未配置 MONGO_URI"}
    coll_analysis, source_name = _get_jc_match_analysis_collection(db)
    regex = f"^{year}"
    query = {"$or": [{"jc_date": {"$regex": regex}}, {"match_date": {"$regex": regex}}, {"date": {"$regex": regex}}]}
    teams_set: set[str] = set()
    scanned = 0
    for doc in coll_analysis.find(query):
        scanned += 1
        for name_cn, _ in _team_names_from_analysis_doc(doc):
            key = name_cn.strip()
            if key:
                teams_set.add(key)
    return {"teams": sorted(teams_set), "count": len(teams_set), "scanned_docs": scanned, "source": source_name}


def jc_teams_missing_all() -> dict[str, Any]:
    """
    检查 jingcai_match_analysis 全表中出现的所有队伍有多少已在 jingcai_teams、多少缺失（不按年份）。
    返回: {"in_analysis": [name_cn], "in_jingcai_teams": [name_cn], "missing": [name_cn],
           "total_in_analysis": n, "total_in_jingcai_teams": n, "total_missing": n,
           "scanned_docs": n, "source": 集合名, "summary": str}。
    """
    db = get_self_improvement_db()
    if db is None:
        return {
            "in_analysis": [], "in_jingcai_teams": [], "missing": [],
            "total_in_analysis": 0, "total_in_jingcai_teams": 0, "total_missing": 0,
            "scanned_docs": 0, "source": None, "summary": "未配置 MONGO_URI",
        }
    coll_analysis, source_name = _get_jc_match_analysis_collection(db)
    coll_teams = db[COLLECTION_JC_TEAMS]
    teams_set: set[str] = set()
    scanned = 0
    for doc in coll_analysis.find({}):
        scanned += 1
        for name_cn, _ in _team_names_from_analysis_doc(doc):
            key = name_cn.strip()
            if key:
                teams_set.add(key)
    in_analysis = sorted(teams_set)
    existing = {doc["name_cn"] for doc in coll_teams.find({}, {"name_cn": 1})}
    in_jingcai_teams = [n for n in in_analysis if n in existing]
    missing = [n for n in in_analysis if n not in existing]
    summary = (
        f"分析表共 {len(in_analysis)} 支队伍，其中在 jingcai_teams 中 {len(in_jingcai_teams)} 支，缺失 {len(missing)} 支"
        f"（扫描 {scanned} 条对阵文档）"
    )
    return {
        "in_analysis": in_analysis,
        "in_jingcai_teams": in_jingcai_teams,
        "missing": missing,
        "total_in_analysis": len(in_analysis),
        "total_in_jingcai_teams": len(in_jingcai_teams),
        "total_missing": len(missing),
        "scanned_docs": scanned,
        "source": source_name,
        "summary": summary,
    }


def jc_teams_missing_for_year(year: int) -> dict[str, Any]:
    """
    检查指定年份在 jingcai_match_analysis 中出现的队伍有多少已在 jingcai_teams、多少缺失。
    返回: {"year": year, "in_analysis": [name_cn], "in_jingcai_teams": [name_cn], "missing": [name_cn], "summary": str}。
    """
    by_year = jc_teams_from_analysis_for_year(year)
    if by_year.get("message"):
        return {"year": year, "in_analysis": [], "in_jingcai_teams": [], "missing": [], "summary": by_year["message"]}
    in_analysis = by_year.get("teams") or []
    db = get_self_improvement_db()
    if db is None:
        return {"year": year, "in_analysis": in_analysis, "in_jingcai_teams": [], "missing": in_analysis, "summary": "未配置 MONGO_URI"}
    existing = {doc["name_cn"] for doc in db[COLLECTION_JC_TEAMS].find({}, {"name_cn": 1})}
    in_teams = [n for n in in_analysis if n in existing]
    missing = [n for n in in_analysis if n not in existing]
    summary = f"{year} 年: 分析表共 {len(in_analysis)} 支队伍，其中在 jingcai_teams 中 {len(in_teams)} 支，缺失 {len(missing)} 支"
    return {"year": year, "in_analysis": in_analysis, "in_jingcai_teams": in_teams, "missing": missing, "summary": summary}


def _get_home_away_cn_from_analysis_doc(doc: dict[str, Any]) -> tuple[str, str]:
    """从 jingcai_match_analysis 单条文档解析主客队中文名。尝试 pageTitle 下多种字段，再回退到文档顶层 home/away、home_cn/away_cn。"""
    pt = (doc.get("match_head") or {}).get("pageTitle") or doc.get("match_head") or {}
    if not isinstance(pt, dict):
        pt = {}
    home_cn = (
        (pt.get("homeTeamShortName") or pt.get("homeTeamAbbName") or pt.get("homeTeamName") or pt.get("homeTeamFullName") or "")
    ).strip()
    away_cn = (
        (pt.get("awayTeamShortName") or pt.get("awayTeamAbbName") or pt.get("awayTeamName") or pt.get("awayTeamFullName") or "")
    ).strip()
    # 回退：部分文档仅有顶层 home/away 或 home_cn/away_cn（如列表拉取时的字段）
    if not home_cn:
        home_cn = (doc.get("home_cn") or doc.get("home") or "").strip()
    if not away_cn:
        away_cn = (doc.get("away_cn") or doc.get("away") or "").strip()
    return home_cn or "", away_cn or ""


def _get_home_away_en_from_analysis_doc(doc: dict[str, Any], cn_to_en: dict[str, str]) -> tuple[str, str]:
    """从 jingcai_match_analysis 单条文档解析主客队英文名。优先级：文档顶层 home_en/away_en → pageTitle 英文字段 → 映射表 → 中文名若已是英文则直接用。"""
    pt = (doc.get("match_head") or {}).get("pageTitle") or doc.get("match_head") or {}
    if not isinstance(pt, dict):
        pt = {}
    home_cn, away_cn = _get_home_away_cn_from_analysis_doc(doc)
    home_en = (doc.get("home_en") or "").strip() or (pt.get("homeTeamAbbEnName") or pt.get("homeTeamEnName") or pt.get("homeTeamNameEn") or "").strip() or (cn_to_en.get(home_cn) if home_cn else "")
    away_en = (doc.get("away_en") or "").strip() or (pt.get("awayTeamAbbEnName") or pt.get("awayTeamEnName") or pt.get("awayTeamNameEn") or "").strip() or (cn_to_en.get(away_cn) if away_cn else "")
    # 回退：顶层 home/away 可能已是英文（如列表接口直接返回的）
    if not home_en and home_cn:
        home_en = (doc.get("home_en") or doc.get("home") or "").strip() or (home_cn if _looks_like_english(home_cn) else "")
    if not away_en and away_cn:
        away_en = (doc.get("away_en") or doc.get("away") or "").strip() or (away_cn if _looks_like_english(away_cn) else "")
    return home_en or "", away_en or ""


def _looks_like_english(s: str) -> bool:
    """粗略判断字符串是否主要为英文（可用于将未映射的「已是英文」的队名当英文用）。"""
    if not (s or "").strip():
        return False
    s = (s or "").strip()
    # 若绝大部分为 ASCII 字母/空格，视为英文
    alpha = sum(1 for c in s if c.isalpha())
    ascii_alpha = sum(1 for c in s if c.isalpha() and ord(c) < 128)
    return alpha > 0 and ascii_alpha >= min(3, alpha)


def _get_league_cn_from_analysis_doc(doc: dict[str, Any]) -> str:
    """从 jingcai_match_analysis 文档解析联赛中文名（用于按联赛+赛季回退拉取赛程、按联赛调伤停）。"""
    def _from_pt(obj: dict[str, Any] | None) -> str:
        if not isinstance(obj, dict):
            return ""
        return (
            (obj.get("tournamentCnShortName") or obj.get("leagueName") or obj.get("leagueShortName") or obj.get("tournamentName") or obj.get("tournamentAbbName") or obj.get("leagueNameCn") or obj.get("tournamentCnName") or obj.get("competitionName") or obj.get("tournamentShortName") or "")
        ).strip()

    def _league_from_dict(d: dict[str, Any] | None) -> str:
        if not isinstance(d, dict):
            return ""
        s = (d.get("league_cn") or d.get("league") or d.get("leagueName") or d.get("tournamentCnShortName") or d.get("tournamentName") or "").strip()
        return s or _from_pt(d)

    s = (doc.get("league_cn") or doc.get("league") or "").strip()
    if s:
        return s
    # 优先：match_head 顶层直接带联赛字段（无 pageTitle 时），与 jingcai 实际入库结构一致
    for head_key in ("match_head", "matchHead"):
        head = doc.get(head_key) or {}
        if isinstance(head, dict):
            s = (head.get("tournamentCnShortName") or head.get("tournamentCnName") or head.get("leagueName") or head.get("leagueShortName") or "").strip()
            if s:
                return s
    # 常见：match_head.pageTitle 或 match_head 整体作为 pt
    for head_key in ("match_head", "matchHead"):
        head = doc.get(head_key) or {}
        if not isinstance(head, dict):
            continue
        pt = head.get("pageTitle") or head.get("page_title") or head
        s = _from_pt(pt) if isinstance(pt, dict) else ""
        if s:
            return s
        val_pt = head.get("value") or {}
        if isinstance(val_pt, dict):
            pt2 = val_pt.get("pageTitle") or val_pt.get("page_title") or val_pt
            s = _from_pt(pt2) if isinstance(pt2, dict) else ""
            if s:
                return s
    # API 常见：data / result 包装（含 matchDetail/match_detail 再包一层）
    for wrap in ("data", "result"):
        data = doc.get(wrap) or {}
        if not isinstance(data, dict):
            continue
        s = _league_from_dict(data)
        if s:
            return s
        for head_key in ("match_head", "matchHead"):
            pt_data = (data.get(head_key) or {}).get("pageTitle") or (data.get(head_key) or {}).get("page_title") or data.get(head_key) or {}
            s = _from_pt(pt_data) if isinstance(pt_data, dict) else ""
            if s:
                return s
        for detail_key in ("matchDetail", "match_detail", "detail"):
            detail_obj = data.get(detail_key) or {}
            if not isinstance(detail_obj, dict):
                continue
            s = _league_from_dict(detail_obj)
            if s:
                return s
            for head_key in ("match_head", "matchHead"):
                head_in = detail_obj.get(head_key) or {}
                if isinstance(head_in, dict):
                    s = (head_in.get("tournamentCnShortName") or head_in.get("tournamentCnName") or head_in.get("leagueName") or head_in.get("leagueShortName") or "").strip()
                    if s:
                        return s
                pt_in = (head_in.get("pageTitle") or head_in.get("page_title") or head_in) if isinstance(head_in, dict) else {}
                s = _from_pt(pt_in) if isinstance(pt_in, dict) else ""
                if s:
                    return s
    # value 包装
    top_val = doc.get("value") or {}
    if isinstance(top_val, dict):
        s = _league_from_dict(top_val)
        if s:
            return s
        pt3 = top_val.get("pageTitle") or top_val.get("match_head") or top_val.get("matchHead") or {}
        if isinstance(pt3, dict):
            pt3 = pt3.get("pageTitle") or pt3.get("page_title") or pt3
        s = _from_pt(pt3) if isinstance(pt3, dict) else ""
        if s:
            return s
    # detail 包裹
    detail = doc.get("detail") or {}
    if isinstance(detail, dict):
        s = _league_from_dict(detail)
        if s:
            return s
        for head_key in ("match_head", "matchHead"):
            pt_detail = (detail.get(head_key) or {}).get("pageTitle") or (detail.get(head_key) or {}).get("page_title") or detail.get(head_key) or {}
            s = _from_pt(pt_detail) if isinstance(pt_detail, dict) else ""
            if s:
                return s
    # matchInfo / match_info（列表项常用）
    for info_key in ("matchInfo", "match_info"):
        info = doc.get(info_key) or {}
        if isinstance(info, dict):
            s = _league_from_dict(info)
            if s:
                return s
    # 一层递归：任意子 dict 的 league 字段（兼容未知结构）
    for _k, v in doc.items():
        if isinstance(v, dict) and _k not in ("_id", "odds", "wbsjStats"):
            s = _league_from_dict(v)
            if s:
                return s
    # 两层递归：备用集合可能把 match_head 等放在 value/data 下且再套一层
    for _k, v in doc.items():
        if not isinstance(v, dict) or _k in ("_id", "odds"):
            continue
        for _k2, v2 in v.items():
            if isinstance(v2, dict) and _k2 not in ("_id", "odds", "wbsjStats"):
                s = _league_from_dict(v2)
                if s:
                    return s
    # 最后手段：深度递归扫描整棵文档，任意层级出现联赛相关键且值为非空字符串即返回（深度上限 6，跳过超大列表）
    _LEAGUE_KEYS = frozenset({
        "league", "league_cn", "leagueName", "leagueShortName", "tournamentCnShortName", "tournamentCnName",
        "tournamentName", "tournamentAbbName", "leagueNameCn", "competitionName", "tournamentShortName",
    })

    def _deep_league(obj: Any, depth: int) -> str:
        if depth <= 0:
            return ""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in _LEAGUE_KEYS and isinstance(v, str) and (v or "").strip():
                    return (v or "").strip()
            for k, v in obj.items():
                if k in ("_id", "odds", "wbsjStats"):
                    continue
                s = _deep_league(v, depth - 1)
                if s:
                    return s
        elif isinstance(obj, list) and len(obj) < 50:
            for item in obj:
                s = _deep_league(item, depth - 1)
                if s:
                    return s
        return ""

    s = _deep_league(doc, 6)
    if s:
        return s
    return ""


def backfill_league_cn_in_jingcai_analysis(years: list[int] | None = None, dry_run: bool = False) -> dict[str, Any]:
    """
    对 jingcai_match_analysis（及备用集合若存在）中缺少或为空的 league_cn 的文档，
    用 _get_league_cn_from_analysis_doc 解析并写回 league_cn，便于按联赛统计。
    years: 仅处理 jc_date/match_date 在该年份内的文档；None 表示全部。
    dry_run: True 时只统计会更新多少条，不写库。
    返回: {"updated": n, "skipped": n, "empty_parsed": n, "collection": name} 或主+备多集合汇总。
    """
    db = get_self_improvement_db()
    if db is None:
        return {"error": "未配置 MongoDB", "updated": 0}
    date_query: dict[str, Any] = {}
    if years:
        year_regex = "|".join(str(y) for y in years)
        date_query = {"$or": [{"jc_date": {"$regex": f"^({year_regex})"}}, {"match_date": {"$regex": f"^({year_regex})"}}, {"date": {"$regex": f"^({year_regex})"}}]}
    total_updated = 0
    total_skipped = 0
    total_empty_parsed = 0
    for coll in _jc_match_analysis_collections(db):
        q = {
            "$or": [
                {"league_cn": {"$exists": False}},
                {"league_cn": ""},
                {"league_cn": None},
                {"league_cn": {"$regex": r"^\s*$"}},
            ]
        }
        if date_query:
            q = {"$and": [date_query, q]}
        updated = 0
        skipped = 0
        empty_parsed = 0
        for doc in coll.find(q):
            league_cn = _get_league_cn_from_analysis_doc(doc)
            if not (league_cn or "").strip():
                empty_parsed += 1
                continue
            if not dry_run:
                mid = doc.get("sporttery_match_id")
                if mid is not None:
                    r = coll.update_one({"_id": doc["_id"]}, {"$set": {"league_cn": league_cn}})
                    if r.modified_count:
                        updated += 1
                    else:
                        skipped += 1
            else:
                updated += 1
        total_updated += updated
        total_skipped += skipped
        total_empty_parsed += empty_parsed
    return {"updated": total_updated, "skipped": total_skipped, "empty_parsed": total_empty_parsed, "dry_run": dry_run}


def get_jingcai_cn_to_en() -> dict[str, str]:
    """从 jingcai_teams 表 + data/jingcai_team_name_mapping.json 合并 竞彩中文名→英文名；文件映射优先（可纠正表中错误如 莱切斯特→Lecce）。"""
    out: dict[str, str] = {}
    db = get_self_improvement_db()
    if db is not None:
        for doc in db[COLLECTION_JC_TEAMS].find({}):
            name_cn = (doc.get("name_cn") or "").strip()
            name_en = (doc.get("name_en") or "").strip()
            if name_cn and name_en:
                out[name_cn] = name_en
    file_map = _load_jingcai_team_mapping()
    if file_map:
        out.update(file_map)
    return out


def _data_dir() -> "Path":
    import config
    path = getattr(config, "DATA_DIR", None)
    if path:
        return path
    from pathlib import Path
    return Path(__file__).resolve().parent.parent / "data"


def _load_zh_to_en_fallback() -> dict[str, str]:
    """从 data/team_name_zh_mapping.json（英文→中文）反查中文→英文，用于主库无 name_zh 时的兜底。"""
    try:
        f = _data_dir() / "team_name_zh_mapping.json"
        if not f.exists():
            return {}
        with open(f, encoding="utf-8") as fp:
            data = __import__("json").load(fp)
        if not isinstance(data, dict):
            return {}
        zh_to_en: dict[str, str] = {}
        for en, zh in data.items():
            if isinstance(en, str) and isinstance(zh, str) and not en.startswith("_"):
                z = zh.strip()
                if z and z not in zh_to_en:
                    zh_to_en[z] = en.strip()
        return zh_to_en
    except Exception:
        return {}


def _load_jingcai_team_mapping() -> dict[str, str]:
    """从 data/jingcai_team_name_mapping.json 加载 竞彩中文名 → 主库英文名（teams.name）。"""
    try:
        f = _data_dir() / "jingcai_team_name_mapping.json"
        if not f.exists():
            return {}
        with open(f, encoding="utf-8") as fp:
            data = __import__("json").load(fp)
        if not isinstance(data, dict):
            return {}
        return {
            k.strip(): v.strip()
            for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and not k.startswith("_") and k.strip() and v.strip()
        }
    except Exception:
        return {}


def get_jingcai_cn_to_en_from_file() -> dict[str, str]:
    """从 data/jingcai_team_name_mapping.json 加载 竞彩中文名 → 主库英文名。供预测解析队名时使用（不限于五大联赛）。"""
    return _load_jingcai_team_mapping()


def _load_jingcai_team_api_id_override() -> dict[str, int]:
    """从 data/jingcai_team_api_id_override.json 加载 英文名(name_en) → api_football_team_id。用于 API 按名查不到的队手动填 id。"""
    try:
        f = _data_dir() / "jingcai_team_api_id_override.json"
        if not f.exists():
            return {}
        with open(f, encoding="utf-8") as fp:
            data = __import__("json").load(fp)
        if not isinstance(data, dict):
            return {}
        out: dict[str, int] = {}
        for k, v in data.items():
            if not isinstance(k, str) or k.startswith("_"):
                continue
            key = k.strip()
            if not key:
                continue
            if isinstance(v, int) and v > 0:
                out[key] = v
        return out
    except Exception:
        return {}


def _load_jingcai_fixture_id_override() -> dict[int, int]:
    """从 data/jingcai_fixture_id_override.json 加载 sporttery_match_id → api_football_fixture_id。用于 API 自动匹配不到的场次（亚洲杯、德乙等）手动填 fixture_id。"""
    try:
        f = _data_dir() / "jingcai_fixture_id_override.json"
        if not f.exists():
            return {}
        with open(f, encoding="utf-8") as fp:
            data = __import__("json").load(fp)
        if not isinstance(data, dict):
            return {}
        out: dict[int, int] = {}
        for k, v in data.items():
            if isinstance(k, str) and k.startswith("_"):
                continue
            try:
                mid = int(k) if isinstance(k, int) else int(str(k).strip())
                fid = int(v) if isinstance(v, int) else int(str(v).strip())
            except (TypeError, ValueError):
                continue
            if mid > 0 and fid > 0:
                out[mid] = fid
        return out
    except Exception:
        return {}


def _prefix_match_cn_to_en(
    name_cn: str,
    cn_to_en: dict[str, tuple[str, int | None]],
) -> tuple[str | None, int | None]:
    """
    前缀模糊匹配：若存在唯一的 name_zh 满足 name_zh.startswith(name_cn) 或 name_cn.startswith(name_zh)，返回其 (name_en, id)。
    用于竞彩简称（如 拜仁）匹配主库全称（拜仁慕尼黑）。
    """
    candidates: list[tuple[str, str, int | None]] = []
    for name_zh, (name_en, tid) in cn_to_en.items():
        if name_zh.startswith(name_cn) or name_cn.startswith(name_zh):
            candidates.append((name_zh, name_en, tid))
    if len(candidates) != 1:
        return (None, None)
    return (candidates[0][1], candidates[0][2])


def _fetch_api_football_team_id_by_name(
    team_name: str,
    rate_limit_sec: float = 0.2,
    try_variants: bool = True,
) -> int | None:
    """
    通过 API Football /teams?name= 按英文队名查询，返回匹配的 team id（不限于五大联赛）。
    先试全名，无结果且 try_variants 时再试：去掉尾部 FC/CF/AFC/SC、取最后一个词（如 Frontale）。
    多结果时选名称最贴近查询串的一条。调用方需在多次调用间自行 sleep 限流。
    """
    import time
    name = (team_name or "").strip()
    if not name or len(name) < 2:
        return None

    # 竞彩/我们用的英文名与 API-Football 官方名可能不一致，先试这些变体（按 name 精确查）
    _API_NAME_VARIANTS: dict[str, list[str]] = {
        "Al-Qadsiah": ["Al Qadisiya", "Al Qadisiya FC", "Al-Qadisiyah FC", "Qadisiya", "Al Qadsiah"],
        "Al-Khaleej FC": ["Al Khaleej", "Al Khaleej FC", "Khaleej", "Al-Khaleej"],
        "Al Hazem": ["Al Hazm", "Al Hazem", "Al-Hazm", "Al-Hazem"],
        "Al Taawoun": ["Al Taawon", "Al Taawoun", "Taawoun", "Al-Taawoun"],
        "VVV-Venlo": ["VVV Venlo", "VVV-Venlo", "Venlo"],
        "Al Wehdat": ["Al Wehdat", "Al-Wehdat", "Al Wehdat SC", "Al-Wehdat SC", "Wehdat"],
    }

    def _search_safe(s: str) -> str:
        """API-Football search 仅接受字母，去掉连字符/空格等后返回。"""
        return "".join(c for c in (s or "") if c.isalpha())

    def _one_request(query: str, use_search_param: bool = False) -> list[dict] | None:
        if not query or len(query) < (3 if use_search_param else 2):
            return None
        try:
            from etl.injury_api_football import _request
            time.sleep(rate_limit_sec)
            if use_search_param:
                search_val = _search_safe(query)
                if len(search_val) < 3:
                    return None
                params = {"search": search_val}
            else:
                params = {"name": query}
            data = _request("teams", params)
            resp = data.get("response") if isinstance(data, dict) else None
            if isinstance(resp, list) and len(resp) > 0:
                return resp
        except Exception:
            pass
        return None

    def _normalize_for_match(s: str) -> str:
        """去掉空格、连字符等便于比较拼写变体（如 Al-Qadsiah / Al Qadisiya）。"""
        return "".join(c for c in (s or "").lower() if c.isalnum())

    def _best_team_id(resp: list[dict], query: str) -> int | None:
        q = _normalize_for_match(query)
        best_id: int | None = None
        best_score = -1
        for item in resp:
            team = item.get("team") if isinstance(item, dict) else None
            if not isinstance(team, dict) or team.get("id") is None:
                continue
            tname = _normalize_for_match(team.get("name") or "")
            if q in tname or tname in q:
                score = len(tname) if tname else 0
                if score > best_score:
                    best_score = score
                    best_id = int(team["id"])
        if best_id is not None:
            return best_id
        # 无包含关系时取第一条
        first = resp[0]
        team = first.get("team") if isinstance(first, dict) else None
        if isinstance(team, dict) and team.get("id") is not None:
            return int(team["id"])
        return None

    resp = _one_request(name)
    if resp:
        return _best_team_id(resp, name)
    # 已知英文名与 API 官方拼写不一致时，先试预设变体（避免误判为“无此队”）
    for variant in _API_NAME_VARIANTS.get(name, []):
        if variant == name:
            continue
        resp = _one_request(variant)
        if resp:
            return _best_team_id(resp, name)
    if not try_variants:
        return None
    # 变体0：Al 开头的队名，API 常用连字符（Al Kholood -> Al-Kholood 或反过来）
    if name.startswith("Al ") or name.startswith("Al-"):
        alt = name.replace("Al ", "Al-", 1) if "Al " in name[:4] else name.replace("Al-", "Al ", 1)
        if alt != name:
            resp = _one_request(alt)
            if resp:
                return _best_team_id(resp, name)
    # 变体1：去掉常见后缀
    for suffix in (" FC", " CF", " AFC", " SC", " United", " City", " Hotspur", " Rovers", " Rangers"):
        if name.endswith(suffix) and len(name) > len(suffix):
            short = name[: -len(suffix)].strip()
            if len(short) >= 2:
                resp = _one_request(short)
                if resp:
                    return _best_team_id(resp, name)
    # 变体2：多词时用最后一个词（如 Kawasaki Frontale -> Frontale）
    parts = name.split()
    if len(parts) >= 2:
        last = parts[-1].strip()
        if len(last) >= 3:
            resp = _one_request(last)
            if resp:
                return _best_team_id(resp, name)
    # 变体3：用 search 参数（支持缩写/关键词，至少 3 字符）
    if len(name) >= 3:
        resp = _one_request(name, use_search_param=True)
        if resp:
            return _best_team_id(resp, name)
    for part in parts:
        if len(part) >= 3:
            resp = _one_request(part, use_search_param=True)
            if resp:
                return _best_team_id(resp, name)
    # 变体4：含连字符的缩写（如 VVV-Venlo）试最后一节
    if "-" in name:
        tail = name.split("-")[-1].strip()
        if len(tail) >= 3 and tail not in parts:
            resp = _one_request(tail)
            if resp:
                return _best_team_id(resp, name)
    return None


def jc_teams_map_to_api_football(
    use_api_search: bool = True,
    api_search_rate_limit_sec: float = 0.25,
    only_missing_team_id: bool = True,
) -> dict[str, Any]:
    """
    将 jingcai_teams 中的中文名映射为英文名，写回 name_en；并尽量补全 api_football_team_id（不限于五大联赛）。
    若表中已有 name_en，优先保留不覆盖，仅补 api_football_team_id。
    否则匹配顺序：① 主库 name_zh 精确 ② data/jingcai_team_name_mapping.json ③ 主库 name_zh 前缀唯一 ④ data/team_name_zh_mapping.json 反查。
    当主库无该队（team_id 为空）时，若 use_api_search=True，会调用 API Football /teams?name= 按英文名查询并写入 api_football_team_id。
    only_missing_team_id: 默认 True，仅处理 api_football_team_id 为空或缺失的文档，跳过已有 id 的，节省 API 调用；传 False 时处理全部。
    返回: updated, from_existing, from_db, from_jingcai, from_prefix, from_mapping, from_api_search, not_found[], message。
    """
    import time
    db = get_self_improvement_db()
    if db is None:
        return {"updated": 0, "from_existing": 0, "from_db": 0, "from_jingcai": 0, "from_prefix": 0, "from_mapping": 0, "from_api_search": 0, "from_override": 0, "not_found": [], "message": "未配置 MONGO_URI"}
    from db import repo
    repo.init_db()
    # 主库 teams：name_zh -> (name, id)；name -> id（用于竞彩映射表查到的英文名反查 id）
    cn_to_en: dict[str, tuple[str, int | None]] = {}
    name_to_id: dict[str, int] = {}
    for t in repo.list_all_teams():
        name_zh = (t.get("name_zh") or "").strip()
        name = (t.get("name") or "").strip()
        tid = t.get("id")
        if name:
            name_to_id[name] = tid
        if name_zh and name and name_zh not in cn_to_en:
            cn_to_en[name_zh] = (name, tid)
    jingcai_cn_to_en = _load_jingcai_team_mapping()
    zh_to_en_fallback = _load_zh_to_en_fallback()
    api_id_override = _load_jingcai_team_api_id_override()
    coll = db[COLLECTION_JC_TEAMS]
    query: dict[str, Any] = {}
    if only_missing_team_id:
        query["$or"] = [
            {"api_football_team_id": {"$exists": False}},
            {"api_football_team_id": None},
            {"api_football_team_id": ""},
        ]
    updated = 0
    from_existing = 0
    from_db = 0
    from_jingcai = 0
    from_prefix = 0
    from_mapping = 0
    from_api_search = 0
    from_override = 0
    not_found: list[str] = []
    still_no_team_id: list[tuple[str, str]] = []  # (name_cn, name_en) 有英文名但 API 未查到 id
    for doc in coll.find(query):
        name_cn = (doc.get("name_cn") or "").strip()
        if not name_cn:
            continue
        new_en: str | None = None
        team_id: int | None = doc.get("api_football_team_id")  # 已有则保留
        if team_id is not None:
            try:
                team_id = int(team_id)
            except (TypeError, ValueError):
                team_id = None
        source = "none"
        existing_en = (doc.get("name_en") or "").strip()
        # 映射表优先：若 jingcai_team_name_mapping 有该中文名，用其英文名（可纠正表中错误，如 莱切斯特→Leicester City）
        if name_cn in jingcai_cn_to_en:
            new_en = jingcai_cn_to_en[name_cn]
            if team_id is None:
                team_id = name_to_id.get(new_en)
            source = "jingcai"
        elif existing_en:
            new_en = existing_en
            if team_id is None:
                team_id = name_to_id.get(existing_en)
            source = "existing"
        if new_en is None and name_cn in cn_to_en:
            new_en, tid_from_db = cn_to_en[name_cn]
            if team_id is None:
                team_id = tid_from_db
            source = "db"
        if new_en is None:
            new_en, tid_from_db = _prefix_match_cn_to_en(name_cn, cn_to_en)
            if new_en is not None:
                if team_id is None:
                    team_id = tid_from_db
                source = "prefix"
        if new_en is None and name_cn in zh_to_en_fallback:
            new_en = zh_to_en_fallback[name_cn]
            source = "mapping"
        if new_en is None:
            not_found.append(name_cn)
        if new_en is not None:
            # 先查手动覆盖表，再按英文名调 API
            if team_id is None and new_en in api_id_override:
                team_id = api_id_override[new_en]
                from_override += 1
            if team_id is None and use_api_search:
                api_id = _fetch_api_football_team_id_by_name(new_en, rate_limit_sec=api_search_rate_limit_sec)
                if api_id is not None:
                    team_id = api_id
                    from_api_search += 1
            update = {
                "name_en": new_en,
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
            if team_id is not None:
                update["api_football_team_id"] = team_id
            else:
                still_no_team_id.append((name_cn, new_en))
            coll.update_one(
                {"name_cn": doc["name_cn"]},
                {"$set": update},
            )
            updated += 1
            if source == "existing":
                from_existing += 1
            elif source == "db":
                from_db += 1
            elif source == "jingcai":
                from_jingcai += 1
            elif source == "prefix":
                from_prefix += 1
            else:
                from_mapping += 1
    return {
        "updated": updated,
        "from_existing": from_existing,
        "from_db": from_db,
        "from_jingcai": from_jingcai,
        "from_prefix": from_prefix,
        "from_mapping": from_mapping,
        "from_api_search": from_api_search,
        "from_override": from_override,
        "not_found": not_found,
        "total_not_found": len(not_found),
        "still_no_team_id": still_no_team_id,
    }


def jc_match_analysis_backfill_english_names(
    overwrite: bool = False,
    year: int | None = None,
) -> dict[str, Any]:
    """
    将 jingcai_teams 中已整理的英文名回填到 jingcai_match_analysis 表：每条文档按主客队中文名查表，
    写入顶层 home_en、away_en。overwrite=False 时仅补空；overwrite=True 时用表/映射覆盖已有（可纠正错误如 莱切斯特→Lecce）。
    year: 若指定，仅处理 match_date/jc_date 属于该年份的文档。
    返回: {"updated": n, "skipped": n, "total": n}。
    """
    db = get_self_improvement_db()
    if db is None:
        return {"updated": 0, "skipped": 0, "total": 0, "message": "未配置 MONGO_URI"}
    cn_to_en = get_jingcai_cn_to_en()
    coll = db[COLLECTION_JC_MATCH_ANALYSIS]
    updated = 0
    skipped = 0
    for doc in coll.find({}):
        date_val = doc.get("match_date") or doc.get("jc_date")
        day = _normalize_jc_date(date_val)
        if year is not None and day and len(day) >= 4:
            try:
                if int(day[:4]) != year:
                    continue
            except (ValueError, TypeError):
                continue
        elif year is not None:
            continue
        pt = (doc.get("match_head") or {}).get("pageTitle") or doc.get("match_head") or {}
        if not isinstance(pt, dict):
            skipped += 1
            continue
        home_cn, away_cn = _get_home_away_cn_from_analysis_doc(doc)
        existing_home_en = (doc.get("home_en") or "").strip()
        existing_away_en = (doc.get("away_en") or "").strip()
        home_en = cn_to_en.get(home_cn) if home_cn else ""
        away_en = cn_to_en.get(away_cn) if away_cn else ""
        set_fields: dict[str, Any] = {"updated_at": datetime.utcnow().isoformat() + "Z"}
        if home_en and (overwrite or not existing_home_en):
            set_fields["home_en"] = home_en
        if away_en and (overwrite or not existing_away_en):
            set_fields["away_en"] = away_en
        if len(set_fields) > 1:
            coll.update_one(
                {"sporttery_match_id": doc["sporttery_match_id"]},
                {"$set": set_fields},
            )
            updated += 1
        else:
            skipped += 1
    return {"updated": updated, "skipped": skipped, "total": updated + skipped}


def jc_match_analysis_backfill_fixture_id_for_jc_date(
    jc_date: str,
    rate_limit_sec: float = 1.0,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    """
    针对指定竞彩日，只处理「该日下面所有比赛」：已有 api_football_fixture_id 的跳过不再请求；
    没有的用该日+次日两天的 API 赛程匹配后写入（覆盖开球日为次日的情况）。
    返回: updated, skipped, no_match, no_en, no_in_api, total。
    """
    import time
    from datetime import datetime as dt, timedelta
    from etl.api_football_odds import fixtures_with_id_all_leagues, fixtures_with_id_by_league_season, _season_year_from_date
    from etl.api_football_predictions import get_fixture_id_from_fixtures_list
    from etl.injury_api_football import LEAGUE_IDS
    _league_cn_to_id = {v: k for k, v in LEAGUE_IDS.items()}

    db = get_self_improvement_db()
    if db is None:
        return {"updated": 0, "skipped": 0, "no_match": 0, "no_en": 0, "no_in_api": 0, "total": 0, "message": "未配置 MONGO_URI"}
    day = (jc_date or "").strip()[:10]
    if not day or len(day) < 10:
        return {"updated": 0, "skipped": 0, "no_match": 0, "no_en": 0, "no_in_api": 0, "total": 0, "message": "jc_date 格式应为 YYYY-MM-DD"}
    cn_to_en = get_jingcai_cn_to_en()
    coll = db[COLLECTION_JC_MATCH_ANALYSIS]
    query = {"jc_date": {"$in": [day, day + ","]}}
    docs = list(coll.find(query))
    need_backfill_docs = [doc for doc in docs if overwrite_existing or doc.get("api_football_fixture_id") is None]
    if not need_backfill_docs:
        return {"updated": 0, "skipped": len(docs), "no_match": 0, "no_en": 0, "no_in_api": 0, "total": len(docs)}
    # 竞彩日列表含「开球日为次日」的场次，拉取前后各 2 天共 5 天赛程以应对时区/延期
    try:
        day_dt = dt.strptime(day, "%Y-%m-%d")
        prev2 = (day_dt - timedelta(days=2)).strftime("%Y-%m-%d")
        prev_day = (day_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        next_day = (day_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        next2 = (day_dt + timedelta(days=2)).strftime("%Y-%m-%d")
    except Exception:
        prev2 = prev_day = next_day = next2 = day
    fixtures_by_date: dict[str, list] = {}
    for d in [prev2, prev_day, day, next_day, next2]:
        if d not in fixtures_by_date:
            fixtures_by_date[d] = fixtures_with_id_all_leagues(d)
            time.sleep(rate_limit_sec)
    all_fixtures = fixtures_by_date.get(prev2, []) + fixtures_by_date.get(prev_day, []) + fixtures_by_date.get(day, []) + fixtures_by_date.get(next_day, []) + fixtures_by_date.get(next2, [])
    fixture_id_override = _load_jingcai_fixture_id_override()
    updated = 0
    skipped = len(docs) - len(need_backfill_docs)
    no_match = 0
    no_en = 0
    no_in_api = 0
    for doc in need_backfill_docs:
        mid = doc.get("sporttery_match_id")
        if mid is not None and fixture_id_override.get(int(mid)):
            fid = fixture_id_override[int(mid)]
            coll.update_one(
                {"sporttery_match_id": mid},
                {"$set": {"api_football_fixture_id": fid, "updated_at": datetime.utcnow().isoformat() + "Z"}},
            )
            updated += 1
            continue
        home_en, away_en = _get_home_away_en_from_analysis_doc(doc, cn_to_en)
        if not home_en and not away_en:
            no_match += 1
            no_en += 1
            continue
        fid = get_fixture_id_from_fixtures_list(all_fixtures, home_en, away_en)
        if fid is None and home_en and away_en:
            league_cn = _get_league_cn_from_analysis_doc(doc)
            if league_cn in _league_cn_to_id:
                league_id = _league_cn_to_id[league_cn]
                try:
                    season = _season_year_from_date(day)
                except Exception:
                    season = int(day[:4]) if len(day) >= 4 else 0
                league_fixtures = fixtures_with_id_by_league_season(league_id, season, quiet=True)
                time.sleep(rate_limit_sec)
                try:
                    day_dt = dt.strptime(day, "%Y-%m-%d")
                    start = (day_dt - timedelta(days=7)).strftime("%Y-%m-%d")
                    end = (day_dt + timedelta(days=7)).strftime("%Y-%m-%d")
                except Exception:
                    start = end = day
                filtered = [f for f in league_fixtures if start <= (f.get("fixture_date") or "") <= end]
                fid = get_fixture_id_from_fixtures_list(filtered, home_en, away_en)
        if fid is None:
            no_match += 1
            no_in_api += 1
            continue
        coll.update_one(
            {"sporttery_match_id": doc["sporttery_match_id"]},
            {"$set": {"api_football_fixture_id": fid, "updated_at": datetime.utcnow().isoformat() + "Z"}},
        )
        updated += 1
    total = updated + skipped + no_match
    return {"updated": updated, "skipped": skipped, "no_match": no_match, "no_en": no_en, "no_in_api": no_in_api, "total": total}


def jc_match_analysis_backfill_fixture_id(
    rate_limit_sec: float = 1.0,
    overwrite_existing: bool = False,
    year: int | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """
    将 API Football 的 fixture_id 批量回填到 jingcai_match_analysis：按日期拉取全部联赛赛程（不限于五大联赛），
    用主客队英文名匹配后写入 api_football_fixture_id。默认不覆盖已有 api_football_fixture_id；某日若全部文档已有则不再请求该日 API。
    overwrite_existing: 为 False 时，已有 api_football_fixture_id 的文档跳过不更新、且该日不会调用 API。
    year: 若指定，仅处理该年份的文档（按 match_date/jc_date 的年份）。
    date: 若指定（YYYY-MM-DD），仅处理该竞彩日（按 jc_date 匹配）；同一竞彩日内开球日为次日的场次也会被包含，并按开球日拉取 API 赛程匹配。
    返回: updated, skipped, no_match, no_en, no_in_api, total。
    英文名来源（按优先级）：文档顶层 home_en/away_en、match_head.pageTitle 英文字段、jingcai_teams 表。命令行默认会先执行英文名回填。
    未匹配原因：no_en=上述来源均无英文名（可先跑 jc-teams-map-api-football 补 jingcai_teams 后重试）；
    no_in_api=有英文名但该日 API 赛程中无对应场次（联赛/赛事未覆盖、日期时区差异、或队名与 API 不一致）。
    """
    import sys
    import time
    from collections import defaultdict
    from datetime import datetime as _dt, timedelta as _delta
    from etl.api_football_odds import (
        fixtures_with_id_all_leagues,
        fixtures_with_id_by_league_season,
        _season_year_from_date,
    )
    from etl.api_football_predictions import get_fixture_id_from_fixtures_list
    from etl.injury_api_football import LEAGUE_IDS
    LEAGUE_CN_TO_ID = {v: k for k, v in LEAGUE_IDS.items()}

    db = get_self_improvement_db()
    if db is None:
        return {"updated": 0, "skipped": 0, "no_match": 0, "total": 0, "message": "未配置 MONGO_URI"}
    fixture_id_override = _load_jingcai_fixture_id_override()
    cn_to_en = get_jingcai_cn_to_en()
    coll = db[COLLECTION_JC_MATCH_ANALYSIS]
    # 收集所有文档，按日期分组；(doc, home_en, away_en)
    by_date: dict[str, list[tuple[dict, str, str]]] = defaultdict(list)
    for doc in coll.find({}):
        pt = (doc.get("match_head") or {}).get("pageTitle") or doc.get("match_head") or {}
        if not isinstance(pt, dict):
            continue
        home_en, away_en = _get_home_away_en_from_analysis_doc(doc, cn_to_en)
        date_val = doc.get("match_date") or doc.get("jc_date")
        day = _normalize_jc_date(date_val)
        if not day or len(day) < 10:
            continue
        if year is not None:
            try:
                if int(day[:4]) != year:
                    continue
            except (ValueError, TypeError):
                continue
        # 指定 date 时按竞彩日(jc_date)过滤，避免「开球日为次日」的场次被漏掉（竞彩日 3 月 1 号列表里含 3 月 2 号凌晨开球的比赛）
        if date is not None:
            day_norm = (date or "").strip()[:10]
            jc_day = _normalize_jc_date(doc.get("jc_date"))
            if jc_day != day_norm:
                continue
        by_date[day].append((doc, home_en or "", away_en or ""))
    days_with_work = []
    total_need = 0
    for d in sorted(by_date.keys()):
        n = sum(1 for doc, _, _ in by_date[d] if overwrite_existing or doc.get("api_football_fixture_id") is None)
        if n > 0:
            days_with_work.append(d)
            total_need += n
    if days_with_work:
        print(f"共 {len(days_with_work)} 天、{total_need} 条待回填，开始按日拉取 API 并匹配…", file=sys.stderr)
        sys.stderr.flush()
    updated = 0
    skipped = 0
    no_match = 0
    no_en: int = 0  # 缺主客队英文名
    no_in_api: int = 0  # 有英文名但 API 该日无匹配场次
    sample_no_en: list[tuple[str, str, str]] = []  # (jc_date, home_cn, away_cn) 最多 5 条
    sample_no_in_api: list[tuple[str, str, str, str]] = []  # (jc_date, home_en, away_en, day) 最多 5 条
    days_empty_api = 0  # API 该日返回 0 场的天数（便于排查套餐/时区）
    total_days = len(days_with_work)
    processed_days = 0
    # 按联赛+赛季缓存赛程，用于「按日无结果」时回退匹配（如 2022 年历史）
    league_season_fixtures_cache: dict[tuple[int, int], list[dict]] = {}
    for day in sorted(by_date.keys()):
        # 只处理需要回填的文档；若该日全部已有 api_football_fixture_id 且不覆盖，则不请求 API
        need_backfill = [
            (doc, home_en, away_en)
            for doc, home_en, away_en in by_date[day]
            if overwrite_existing or doc.get("api_football_fixture_id") is None
        ]
        skipped += len(by_date[day]) - len(need_backfill)
        if not need_backfill:
            continue
        processed_days += 1
        # 每 30 天或第一天打印进度，避免长时间无输出
        if processed_days % 30 == 1 or processed_days <= 2:
            print(f"  处理至 {day}（第 {processed_days}/{total_days} 天）…", file=sys.stderr)
            sys.stderr.flush()
        # 拉取当日及前后各 4 天赛程，应对时区/延期/赛历差异（API 归日可能与竞彩日差 2～4 天）
        try:
            day_dt = _dt.strptime(day, "%Y-%m-%d")
            prev4 = (day_dt - _delta(days=4)).strftime("%Y-%m-%d")
            prev3 = (day_dt - _delta(days=3)).strftime("%Y-%m-%d")
            prev2 = (day_dt - _delta(days=2)).strftime("%Y-%m-%d")
            prev_day = (day_dt - _delta(days=1)).strftime("%Y-%m-%d")
            next_day = (day_dt + _delta(days=1)).strftime("%Y-%m-%d")
            next2 = (day_dt + _delta(days=2)).strftime("%Y-%m-%d")
            next3 = (day_dt + _delta(days=3)).strftime("%Y-%m-%d")
            next4 = (day_dt + _delta(days=4)).strftime("%Y-%m-%d")
        except Exception:
            prev4 = prev3 = prev2 = prev_day = next_day = next2 = next3 = next4 = day
        # 竞彩日按北京时间，用 Asia/Shanghai 使 API 返回的「当日」与 竞彩日 一致；不限制联赛
        all_fixture_days = [d for d in (prev4, prev3, prev2, prev_day, day, next_day, next2, next3, next4) if d]
        fixtures = []
        for d in all_fixture_days:
            fixtures.extend(fixtures_with_id_all_leagues(d, quiet=True, timezone="Asia/Shanghai"))
            time.sleep(rate_limit_sec)
        if len(fixtures) == 0 and len(need_backfill) > 0:
            days_empty_api += 1
            if processed_days <= 3:
                print(f"  提示: {day} API 返回 0 场，该日 {len(need_backfill)} 条待匹配无法命中。", file=sys.stderr)
                sys.stderr.flush()
        for doc, home_en, away_en in need_backfill:
            mid = doc.get("sporttery_match_id")
            if mid is not None and fixture_id_override.get(int(mid)):
                fid = fixture_id_override[int(mid)]
                coll.update_one(
                    {"sporttery_match_id": mid},
                    {"$set": {"api_football_fixture_id": fid, "updated_at": datetime.utcnow().isoformat() + "Z"}},
                )
                updated += 1
                jc_day = _normalize_jc_date(doc.get("jc_date")) or day
                print(f"回填成功(override): mid={mid} jc_date={jc_day} -> fixture_id={fid}", file=sys.stderr)
                continue
            if not home_en and not away_en:
                no_match += 1
                no_en += 1
                if len(sample_no_en) < 5:
                    h, a = _get_home_away_cn_from_analysis_doc(doc)
                    sample_no_en.append((_normalize_jc_date(doc.get("jc_date")) or day, h, a))
                continue
            fid = get_fixture_id_from_fixtures_list(fixtures, home_en, away_en)
            # 回退：按日无匹配时，若为五大联赛则按联赛+赛季拉取赛程再匹配（应对 2022 等历史日期 API 按日无数据）
            if fid is None and home_en and away_en:
                league_cn = _get_league_cn_from_analysis_doc(doc)
                if league_cn in LEAGUE_CN_TO_ID:
                    league_id = LEAGUE_CN_TO_ID[league_cn]
                    try:
                        season = _season_year_from_date(day)
                    except Exception:
                        season = int(day[:4]) if len(day) >= 4 else 0
                    key = (league_id, season)
                    if key not in league_season_fixtures_cache:
                        league_season_fixtures_cache[key] = fixtures_with_id_by_league_season(league_id, season, quiet=True)
                        time.sleep(rate_limit_sec)
                    all_league = league_season_fixtures_cache[key]
                    try:
                        day_dt = _dt.strptime(day, "%Y-%m-%d")
                        start = (day_dt - _delta(days=7)).strftime("%Y-%m-%d")
                        end = (day_dt + _delta(days=7)).strftime("%Y-%m-%d")
                    except Exception:
                        start = end = day
                    filtered = [f for f in all_league if start <= (f.get("fixture_date") or "") <= end]
                    fid = get_fixture_id_from_fixtures_list(filtered, home_en, away_en)
            if fid is None:
                no_match += 1
                no_in_api += 1
                if len(sample_no_in_api) < 5:
                    sample_no_in_api.append((_normalize_jc_date(doc.get("jc_date")) or day, home_en, away_en, day))
                continue
            coll.update_one(
                {"sporttery_match_id": doc["sporttery_match_id"]},
                {
                    "$set": {
                        "api_football_fixture_id": fid,
                        "updated_at": datetime.utcnow().isoformat() + "Z",
                    }
                },
            )
            updated += 1
            jc_day = _normalize_jc_date(doc.get("jc_date")) or day
            print(f"回填成功: mid={doc.get('sporttery_match_id')} jc_date={jc_day} {home_en} vs {away_en} -> fixture_id={fid}", file=sys.stderr)
    total = updated + skipped + no_match
    return {
        "updated": updated,
        "skipped": skipped,
        "no_match": no_match,
        "no_en": no_en,
        "no_in_api": no_in_api,
        "total": total,
        "sample_no_en": sample_no_en,
        "sample_no_in_api": sample_no_in_api,
        "days_empty_api": days_empty_api,
    }


def jc_match_analysis_fixture_id_stats(years: list[int] | None = None) -> dict[str, Any]:
    """
    统计 jingcai_match_analysis 中按年份的「已匹配 api_football_fixture_id」与「未匹配」数量。
    years: 仅统计这些年份（按 match_date/jc_date 取年）；None 表示全部年份。
    返回: { "by_year": { "2025": { "matched", "unmatched", "total" }, ... }, "total_matched", "total_unmatched", "total" }
    """
    db = get_self_improvement_db()
    if db is None:
        return {"by_year": {}, "total_matched": 0, "total_unmatched": 0, "total": 0}
    coll = db[COLLECTION_JC_MATCH_ANALYSIS]
    by_year: dict[str, dict[str, int]] = {}
    for doc in coll.find({}, {"api_football_fixture_id": 1, "match_date": 1, "jc_date": 1}):
        date_val = doc.get("match_date") or doc.get("jc_date")
        day = _normalize_jc_date(date_val)
        if not day or len(day) < 10:
            continue
        try:
            y = int(day[:4])
        except (ValueError, TypeError):
            continue
        if years is not None and y not in years:
            continue
        key = str(y)
        if key not in by_year:
            by_year[key] = {"matched": 0, "unmatched": 0, "total": 0}
        by_year[key]["total"] += 1
        if doc.get("api_football_fixture_id") is not None:
            by_year[key]["matched"] += 1
        else:
            by_year[key]["unmatched"] += 1
    total_matched = sum(b["matched"] for b in by_year.values())
    total_unmatched = sum(b["unmatched"] for b in by_year.values())
    return {
        "by_year": dict(sorted(by_year.items())),
        "total_matched": total_matched,
        "total_unmatched": total_unmatched,
        "total": total_matched + total_unmatched,
    }


def jc_match_analysis_unmatched_diagnose(
    years: list[int] | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """
    诊断未匹配 api_football_fixture_id 的场次：区分「缺英文名」与「有英文名但 API 无匹配」。
    years: 仅这些年份；None 表示全部。limit: 最多统计条数。
    返回: no_en（缺主客队英文名条数）, has_en（有英文名条数）, sample_no_en, sample_has_en。
    """
    db = get_self_improvement_db()
    if db is None:
        return {"no_en": 0, "has_en": 0, "sample_no_en": [], "sample_has_en": []}
    coll = db[COLLECTION_JC_MATCH_ANALYSIS]
    query: dict[str, Any] = {"$or": [{"api_football_fixture_id": None}, {"api_football_fixture_id": {"$exists": False}}]}
    cn_to_en = get_jingcai_cn_to_en()
    no_en = 0
    has_en = 0
    sample_no_en: list[dict[str, str]] = []
    sample_has_en: list[dict[str, str]] = []
    n = 0
    for doc in coll.find(query):
        date_val = doc.get("match_date") or doc.get("jc_date")
        day = _normalize_jc_date(date_val)
        if day and len(day) >= 4:
            try:
                y = int(day[:4])
                if years is not None and y not in years:
                    continue
            except (ValueError, TypeError):
                if years is not None:
                    continue
        elif years is not None:
            continue
        n += 1
        if n > limit:
            break
        home_en, away_en = _get_home_away_en_from_analysis_doc(doc, cn_to_en)
        pt = (doc.get("match_head") or {}).get("pageTitle") or doc.get("match_head") or {}
        pt = pt if isinstance(pt, dict) else {}
        home_cn, away_cn = _get_home_away_cn_from_analysis_doc(doc)
        jc_date = _normalize_jc_date(doc.get("jc_date"))
        league = (pt.get("tournamentCnShortName") or pt.get("leagueName") or "").strip()
        row = {"jc_date": jc_date or "", "home_cn": home_cn, "away_cn": away_cn, "home_en": home_en or "", "away_en": away_en or "", "league": league}
        if not home_en and not away_en:
            no_en += 1
            if len(sample_no_en) < 10:
                sample_no_en.append(row)
        else:
            has_en += 1
            if len(sample_has_en) < 10:
                sample_has_en.append(row)
    return {"no_en": no_en, "has_en": has_en, "sample_no_en": sample_no_en, "sample_has_en": sample_has_en}


def jc_match_analysis_unmatched_list(
    years: list[int] | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """
    列出 jingcai_match_analysis 中未匹配 api_football_fixture_id 的场次，用于排查或导出。
    years: 仅这些年份；None 表示全部。limit: 最多返回条数。
    返回列表每项: sporttery_match_id, jc_date, match_date, home_cn, away_cn, home_en, away_en, league。
    """
    db = get_self_improvement_db()
    if db is None:
        return []
    coll = db[COLLECTION_JC_MATCH_ANALYSIS]
    query: dict[str, Any] = {"$or": [{"api_football_fixture_id": None}, {"api_football_fixture_id": {"$exists": False}}]}
    out: list[dict[str, Any]] = []
    for doc in coll.find(query):
        date_val = doc.get("match_date") or doc.get("jc_date")
        day = _normalize_jc_date(date_val)
        if day and len(day) >= 4:
            try:
                y = int(day[:4])
                if years is not None and y not in years:
                    continue
            except (ValueError, TypeError):
                if years is not None:
                    continue
        elif years is not None:
            continue
        if len(out) >= limit:
            break
        pt = (doc.get("match_head") or {}).get("pageTitle") or doc.get("match_head") or {}
        if not isinstance(pt, dict):
            pt = {}
        home_cn, away_cn = _get_home_away_cn_from_analysis_doc(doc)
        home_en = (doc.get("home_en") or "").strip() or None
        away_en = (doc.get("away_en") or "").strip() or None
        jc_date = _normalize_jc_date(doc.get("jc_date"))
        match_date = _normalize_jc_date(doc.get("match_date"))
        out.append({
            "sporttery_match_id": doc.get("sporttery_match_id"),
            "jc_date": jc_date or "",
            "match_date": match_date or "",
            "home_cn": home_cn or "",
            "away_cn": away_cn or "",
            "home_en": home_en or "",
            "away_en": away_en or "",
            "league": (pt.get("tournamentCnShortName") or pt.get("leagueName") or "").strip(),
        })
    return out


def jc_match_analysis_backfill_future_matches(
    rate_limit_sec: float = 1.0,
    overwrite_existing: bool = False,
    year: int | None = None,
    date: str | None = None,
    limit: int | None = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """
    对 jingcai_match_analysis 中缺少 future_matches 的文档，仅拉取 getFutureMatchesV1 并回填该字段。
    不重拉整条详情，节省体彩 API 请求。overwrite_existing=True 时也会更新已有 future_matches 的文档。
    year: 仅处理该年份（按 match_date/jc_date）；date: 仅处理该竞彩日（YYYY-MM-DD）。
    limit: 最多处理条数，用于试跑。
    返回: updated, skipped, failed, total。
    """
    import time

    db = get_self_improvement_db()
    if db is None:
        return {"updated": 0, "skipped": 0, "failed": 0, "total": 0, "message": "未配置 MONGO_URI"}
    coll = db[COLLECTION_JC_MATCH_ANALYSIS]
    query: dict[str, Any] = {}
    if not overwrite_existing:
        query["$or"] = [{"future_matches": {"$exists": False}}, {"future_matches": None}]
    docs = list(coll.find(query, {"sporttery_match_id": 1, "match_date": 1, "jc_date": 1}))
    from etl.sporttery_crawl import fetch_sporttery_future_matches_v1

    if year is not None:
        docs = [
            d for d in docs
            if _normalize_jc_date(d.get("match_date") or d.get("jc_date"))[:4] == str(year)
        ]
    if date is not None:
        day_norm = (date or "").strip()[:10]
        docs = [d for d in docs if _normalize_jc_date(d.get("jc_date")) == day_norm]
    if limit is not None and limit > 0:
        docs = docs[:limit]
    total = len(docs)
    updated = 0
    skipped = 0
    failed = 0
    for i, doc in enumerate(docs):
        mid = doc.get("sporttery_match_id")
        if mid is None:
            skipped += 1
            continue
        resp = fetch_sporttery_future_matches_v1(int(mid))
        if not resp or not isinstance(resp.get("value"), dict):
            failed += 1
            if progress_callback and callable(progress_callback):
                progress_callback(i + 1, total, updated, failed)
            time.sleep(rate_limit_sec)
            continue
        value = _sanitize_for_mongo(resp["value"])
        coll.update_one(
            {"sporttery_match_id": mid},
            {"$set": {"future_matches": value, "updated_at": datetime.utcnow().isoformat() + "Z"}},
        )
        updated += 1
        if progress_callback and callable(progress_callback):
            progress_callback(i + 1, total, updated, failed)
        time.sleep(rate_limit_sec)
    return {"updated": updated, "skipped": skipped, "failed": failed, "total": total}


def jc_api_predictions_save(
    api_football_fixture_id: int,
    sporttery_match_id: int | None,
    raw: dict[str, Any],
    result: str | None = None,
    scores: tuple[int, int] | None = None,
    win_pct: float | None = None,
    draw_pct: float | None = None,
    loss_pct: float | None = None,
    goals_expected_home: str | None = None,
    goals_expected_away: str | None = None,
    derived_scores: tuple[int, int] | None = None,
    goals_spread: str | None = None,
) -> bool:
    """
    将 API Football 单场预测写入 self_improvement_football.jc_api_predictions。
    以 api_football_fixture_id 为唯一键 upsert。goals_spread 为负数盘口时的主客差衍生（如 "主客差约 +3（主优）"）。
    """
    db = get_self_improvement_db()
    if db is None:
        return False
    doc = _sanitize_for_mongo({
        "api_football_fixture_id": api_football_fixture_id,
        "sporttery_match_id": sporttery_match_id,
        "raw": raw,
        "result": result,
        "scores": list(scores) if scores else None,
        "win_pct": win_pct,
        "draw_pct": draw_pct,
        "loss_pct": loss_pct,
        "goals_expected_home": goals_expected_home,
        "goals_expected_away": goals_expected_away,
        "derived_scores": list(derived_scores) if derived_scores else None,
        "goals_spread": goals_spread,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
    db[COLLECTION_JC_API_PREDICTIONS].replace_one(
        {"api_football_fixture_id": api_football_fixture_id},
        doc,
        upsert=True,
    )
    return True


def _jc_api_sync_rate_limiter_acquire(limiter: Any) -> None:
    """在共享限速器上等待一个配额（供多 worker 时使用）。"""
    import time
    import threading
    with limiter["lock"]:
        now = time.monotonic()
        wait = limiter["next_ok"] - now
        if wait > 0:
            time.sleep(wait)
        limiter["next_ok"] = time.monotonic() + limiter["interval_sec"]


def jc_api_predictions_sync_from_analysis(
    rate_limit_sec: float = 1.0,
    year: int | None = None,
    date: str | None = None,
    overwrite_existing: bool = False,
    workers: int = 1,
) -> dict[str, Any]:
    """
    从 jingcai_match_analysis 中读取有 api_football_fixture_id 的场次，调用 API Football 预测接口，
    将结果写入 jc_api_predictions 表。
    year: 若指定且 date 未指定，仅处理该年份的文档。
    date: 若指定（YYYY-MM-DD），仅处理该竞彩日/开球日的场次（jc_date 或 match_date 匹配）。
    workers: 并发请求数，>1 时使用共享限速，请求会更快（付费档可配合 --rate-limit 0.05 使用）。
    返回: updated, skipped（已有且不覆盖）, no_fixture_id, api_no_data, total。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from etl.api_football_predictions import (
        fetch_predictions_for_fixture,
        parse_predictions_derived_score,
        parse_predictions_expected_goals,
        parse_predictions_goals_spread,
        parse_predictions_to_outcome,
        parse_predictions_to_probs,
    )

    db = get_self_improvement_db()
    if db is None:
        return {
            "updated": 0,
            "skipped": 0,
            "no_fixture_id": 0,
            "api_no_data": 0,
            "total": 0,
            "message": "未配置 MONGO_URI",
        }
    coll = db[COLLECTION_JC_MATCH_ANALYSIS]
    query: dict[str, Any] = {"api_football_fixture_id": {"$exists": True, "$ne": None}}
    if date is not None:
        query["$or"] = [{"jc_date": date}, {"match_date": date}]
    elif year is not None:
        query["$or"] = [
            {"jc_date": {"$regex": f"^{year}"}},
            {"match_date": {"$regex": f"^{year}"}},
        ]
    doc_list = list(coll.find(query, {"api_football_fixture_id": 1, "sporttery_match_id": 1}))
    tasks: list[tuple[int, Any]] = []
    skipped = 0
    no_fixture_id = 0
    pred_coll = db[COLLECTION_JC_API_PREDICTIONS]
    for doc in doc_list:
        fid = doc.get("api_football_fixture_id")
        if fid is None:
            no_fixture_id += 1
            continue
        if not overwrite_existing and pred_coll.find_one({"api_football_fixture_id": fid}) is not None:
            skipped += 1
            continue
        tasks.append((int(fid), doc.get("sporttery_match_id")))

    updated = 0
    api_no_data = 0
    use_parallel = workers > 1
    limiter = {
        "lock": threading.Lock(),
        "next_ok": 0.0,
        "interval_sec": rate_limit_sec,
    } if use_parallel else None

    def process_one(item: tuple[int, Any]) -> str:
        fid, mid = item
        if use_parallel and limiter is not None:
            _jc_api_sync_rate_limiter_acquire(limiter)
        raw = fetch_predictions_for_fixture(
            fid,
            rate_limit_sec=0.0 if use_parallel else rate_limit_sec,
        )
        if raw is None:
            return "no_data"
        result, scores = parse_predictions_to_outcome(raw)
        probs = parse_predictions_to_probs(raw)
        win_pct, draw_pct, loss_pct = (probs[0], probs[1], probs[2]) if probs else (None, None, None)
        exp_goals = parse_predictions_expected_goals(raw)
        goals_exp_home, goals_exp_away = (exp_goals[0], exp_goals[1]) if exp_goals else (None, None)
        derived = parse_predictions_derived_score(raw)
        goals_spread = parse_predictions_goals_spread(raw)
        jc_api_predictions_save(
            api_football_fixture_id=fid,
            sporttery_match_id=mid,
            raw=raw,
            result=result,
            scores=scores,
            win_pct=win_pct,
            draw_pct=draw_pct,
            loss_pct=loss_pct,
            goals_expected_home=goals_exp_home,
            goals_expected_away=goals_exp_away,
            derived_scores=derived,
            goals_spread=goals_spread,
        )
        return "updated"

    if use_parallel:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(process_one, t) for t in tasks]
            for fut in as_completed(futures):
                try:
                    status = fut.result()
                    if status == "updated":
                        updated += 1
                    else:
                        api_no_data += 1
                except Exception:
                    api_no_data += 1
    else:
        for t in tasks:
            status = process_one(t)
            if status == "updated":
                updated += 1
            else:
                api_no_data += 1

    total = updated + skipped + no_fixture_id + api_no_data
    return {
        "updated": updated,
        "skipped": skipped,
        "no_fixture_id": no_fixture_id,
        "api_no_data": api_no_data,
        "total": total,
    }


def jc_api_predictions_load_by_fixture_id(api_football_fixture_id: int) -> dict[str, Any] | None:
    """按 api_football_fixture_id 从 jc_api_predictions 读取一条。"""
    db = get_self_improvement_db()
    if db is None:
        return None
    doc = db[COLLECTION_JC_API_PREDICTIONS].find_one({"api_football_fixture_id": api_football_fixture_id})
    if not doc:
        return None
    doc.pop("_id", None)
    return doc


def jc_api_predictions_load_by_sporttery_match_id(sporttery_match_id: int) -> dict[str, Any] | None:
    """按 sporttery_match_id 从 jc_api_predictions 读取一条。"""
    db = get_self_improvement_db()
    if db is None:
        return None
    doc = db[COLLECTION_JC_API_PREDICTIONS].find_one({"sporttery_match_id": sporttery_match_id})
    if not doc:
        return None
    doc.pop("_id", None)
    return doc


def jc_backfill_past_match_data(
    jc_date: str,
    rate_limit_sec: float = 0.2,
    limit_per_category: int | None = 50,
    include_today: bool = False,
) -> dict[str, Any]:
    """
    针对指定竞彩日，回填近期比赛日的单场统计、阵容、事件、球员单场统计、射手榜。
    用于 Web 拉取 API 预测时顺带补全已完赛的近期数据（只补缺、不覆盖）。
    jc_date: 当前选择的竞彩日 YYYY-MM-DD。
    include_today: False 时回填「前天、昨天」(jc_date-2, jc_date-1)；True 时回填「前一天、当天」(jc_date-1, jc_date)。
    limit_per_category: 每类最多拉取场数，避免单次请求过长；None 表示不限制。
    返回: statistics_updated, lineups_updated, events_updated, player_stats_updated, topscorers_updated, date_from, date_to。
    """
    from datetime import timedelta

    db = get_self_improvement_db()
    if db is None:
        return {
            "statistics_updated": 0,
            "lineups_updated": 0,
            "events_updated": 0,
            "player_stats_updated": 0,
            "topscorers_updated": 0,
            "date_from": None,
            "date_to": None,
            "message": "未配置 MONGO_URI",
        }
    day = (jc_date or "").strip()[:10]
    if len(day) < 10:
        return {
            "statistics_updated": 0,
            "lineups_updated": 0,
            "events_updated": 0,
            "player_stats_updated": 0,
            "topscorers_updated": 0,
            "date_from": None,
            "date_to": None,
            "message": "jc_date 格式应为 YYYY-MM-DD",
        }
    try:
        from datetime import datetime as _dt
        day_dt = _dt.strptime(day, "%Y-%m-%d")
        if include_today:
            date_from_dt = day_dt - timedelta(days=1)
            date_to_dt = day_dt
        else:
            date_to_dt = day_dt - timedelta(days=1)
            date_from_dt = day_dt - timedelta(days=2)
        date_from = date_from_dt.strftime("%Y-%m-%d")
        date_to = date_to_dt.strftime("%Y-%m-%d")
    except Exception:
        return {
            "statistics_updated": 0,
            "lineups_updated": 0,
            "events_updated": 0,
            "player_stats_updated": 0,
            "topscorers_updated": 0,
            "date_from": None,
            "date_to": None,
            "message": "日期解析失败",
        }

    out: dict[str, Any] = {
        "statistics_updated": 0,
        "lineups_updated": 0,
        "events_updated": 0,
        "player_stats_updated": 0,
        "topscorers_updated": 0,
        "date_from": date_from,
        "date_to": date_to,
    }
    import time

    logger.info("回填近期数据: 竞彩日 %s，范围 %s～%s（前天、昨天）", day, date_from, date_to)

    # 该日期范围内所有有 fixture_id 的场次 (mid, fid)
    all_pairs = lineups_list_jingcai_to_fetch(
        date_from=date_from,
        date_to=date_to,
        skip_existing=False,
        limit=None,
    )
    if not all_pairs:
        out["message"] = f"{date_from}～{date_to} 无竞彩场次或均无 api_football_fixture_id"
        return out

    fixture_ids = [fid for _, fid in all_pairs]
    if limit_per_category is not None and len(fixture_ids) > limit_per_category:
        fixture_ids = fixture_ids[:limit_per_category]

    # 1) 单场统计（射门、控球、角球等）：只补缺
    existing_stats = load_api_football_statistics_from_db(fixture_ids)
    missing_fids = [f for f in fixture_ids if f not in existing_stats]
    if missing_fids:
        try:
            from etl.api_football_statistics import fetch_statistics_for_fixture
            by_fixture: dict[int, list[dict[str, Any]]] = {}
            for fid in missing_fids:
                try:
                    rows = fetch_statistics_for_fixture(fid, rate_limit_sec=rate_limit_sec)
                    if rows:
                        by_fixture[fid] = rows
                    time.sleep(rate_limit_sec)
                except Exception:
                    pass
            if by_fixture:
                out["statistics_updated"] = save_api_football_statistics_bulk(by_fixture)
        except Exception as e:
            logger.warning("回填单场统计失败: %s", e)

    # 2) 阵容
    todo_lineups = lineups_list_jingcai_to_fetch(
        date_from=date_from,
        date_to=date_to,
        skip_existing=True,
        limit=limit_per_category,
    )
    if todo_lineups:
        try:
            from etl.api_football_lineups import fetch_lineups_for_fixture
            for sporttery_match_id, fixture_id in todo_lineups:
                try:
                    lu = fetch_lineups_for_fixture(fixture_id, rate_limit_sec=rate_limit_sec, full=True)
                    if lu.get("home_starting_count") is not None or lu.get("away_starting_count") is not None:
                        lineups_save(
                            sporttery_match_id,
                            home_starting_count=lu.get("home_starting_count"),
                            away_starting_count=lu.get("away_starting_count"),
                            home_starting=lu.get("home_starting"),
                            away_starting=lu.get("away_starting"),
                            home_substitutes=lu.get("home_substitutes"),
                            away_substitutes=lu.get("away_substitutes"),
                        )
                        out["lineups_updated"] += 1
                    time.sleep(rate_limit_sec)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("回填阵容失败: %s", e)

    # 3) 单场事件
    todo_events = events_list_jingcai_to_fetch(
        date_from=date_from,
        date_to=date_to,
        skip_existing=True,
        limit=limit_per_category,
    )
    if todo_events:
        try:
            from etl.api_football_events import fetch_events_for_fixture
            for sporttery_match_id, fixture_id in todo_events:
                try:
                    events = fetch_events_for_fixture(fixture_id, rate_limit_sec=rate_limit_sec)
                    save_api_football_events(
                        api_football_fixture_id=fixture_id,
                        events=events,
                        sporttery_match_id=sporttery_match_id,
                    )
                    out["events_updated"] += 1
                    time.sleep(rate_limit_sec)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("回填单场事件失败: %s", e)

    # 4) 球员单场统计
    todo_players = players_stats_list_jingcai_to_fetch(
        date_from=date_from,
        date_to=date_to,
        skip_existing=True,
        limit=limit_per_category,
    )
    if todo_players:
        try:
            from etl.api_football_players import fetch_player_statistics_for_fixture
            for sporttery_match_id, fixture_id in todo_players:
                try:
                    players = fetch_player_statistics_for_fixture(fixture_id, rate_limit_sec=rate_limit_sec)
                    save_api_football_player_statistics(
                        api_football_fixture_id=fixture_id,
                        players=players or [],
                        sporttery_match_id=sporttery_match_id,
                    )
                    out["player_stats_updated"] += 1
                    time.sleep(rate_limit_sec)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("回填球员单场统计失败: %s", e)

    # 5) 射手榜：按该日期范围内出现的联赛+赛季更新
    try:
        date_range_query: dict[str, Any] = {
            "$or": [
                {"match_date": {"$gte": date_from, "$lte": date_to}},
                {"jc_date": {"$gte": date_from, "$lte": date_to}},
            ],
        }
        league_seasons: set[tuple[int, int]] = set()
        for coll in _jc_match_analysis_collections(db):
            for doc in coll.find(date_range_query, {"match_date": 1, "jc_date": 1}):
                league_cn = _get_league_cn_from_analysis_doc(doc)
                if not league_cn or league_cn not in LEAGUE_CN_TO_API_ID:
                    continue
                day_val = (doc.get("match_date") or doc.get("jc_date") or "")[:10]
                if len(day_val) >= 4 and day_val[:4].isdigit():
                    season = int(day_val[:4])
                    league_id = LEAGUE_CN_TO_API_ID[league_cn]
                    league_seasons.add((league_id, season))
        if league_seasons:
            from etl.api_football_players import fetch_topscorers
            from etl.injury_api_football import LEAGUE_IDS
            for league_id, season in league_seasons:
                try:
                    players = fetch_topscorers(league_id, season, rate_limit_sec=rate_limit_sec)
                    if players:
                        save_topscorers(
                            league_id=league_id,
                            season=season,
                            players=players,
                            league_name=LEAGUE_IDS.get(league_id),
                        )
                        out["topscorers_updated"] += 1
                    time.sleep(rate_limit_sec)
                except Exception:
                    pass
    except Exception as e:
        logger.warning("回填射手榜失败: %s", e)

    out["message"] = (
        f"{date_from}～{date_to} 回填: 单场统计 {out['statistics_updated']} 场、阵容 {out['lineups_updated']} 场、"
        f"事件 {out['events_updated']} 场、球员统计 {out['player_stats_updated']} 场、射手榜 {out['topscorers_updated']} 个联赛"
    )
    logger.info(
        "回填近期数据完成: %s",
        out["message"],
    )
    return out


def jc_api_predictions_load_for_match_ids(
    sporttery_match_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """批量按 sporttery_match_id 查询，返回 { match_id: { result, scores, win_pct, draw_pct, loss_pct } }，便于列表展示。"""
    if not sporttery_match_ids:
        return {}
    db = get_self_improvement_db()
    if db is None:
        return {}
    from etl.api_football_predictions import (
        parse_predictions_derived_score,
        parse_predictions_expected_goals,
        parse_predictions_goals_spread,
        parse_predictions_to_probs,
        parse_predictions_to_outcome,
    )
    out: dict[int, dict[str, Any]] = {}
    for doc in db[COLLECTION_JC_API_PREDICTIONS].find({"sporttery_match_id": {"$in": sporttery_match_ids}}):
        mid = doc.get("sporttery_match_id")
        if mid is None:
            continue
        scores = doc.get("scores")
        if not scores and isinstance(doc.get("raw"), dict):
            _, scores_from_raw = parse_predictions_to_outcome(doc["raw"])
            if scores_from_raw:
                scores = list(scores_from_raw)
        derived = doc.get("derived_scores")
        if (not derived or not isinstance(derived, list)) and isinstance(doc.get("raw"), dict):
            d = parse_predictions_derived_score(doc["raw"])
            if d:
                derived = list(d)
        goals_exp = doc.get("goals_expected_home"), doc.get("goals_expected_away")
        if (goals_exp[0] is None and goals_exp[1] is None) and isinstance(doc.get("raw"), dict):
            exp = parse_predictions_expected_goals(doc["raw"])
            if exp:
                goals_exp = exp
        goals_spread = doc.get("goals_spread")
        if not goals_spread and isinstance(doc.get("raw"), dict):
            goals_spread = parse_predictions_goals_spread(doc["raw"])
        item = {
            "result": doc.get("result"),
            "scores": scores,
            "derived_scores": derived,
            "goals_expected_home": goals_exp[0],
            "goals_expected_away": goals_exp[1],
            "goals_spread": goals_spread,
        }
        probs = doc.get("win_pct") is not None and doc.get("draw_pct") is not None and doc.get("loss_pct") is not None
        if probs:
            item["win_pct"] = doc.get("win_pct")
            item["draw_pct"] = doc.get("draw_pct")
            item["loss_pct"] = doc.get("loss_pct")
        else:
            raw = doc.get("raw")
            if isinstance(raw, dict):
                p = parse_predictions_to_probs(raw)
                if p is not None:
                    item["win_pct"], item["draw_pct"], item["loss_pct"] = p[0], p[1], p[2]
        out[int(mid)] = item
    return out


def jc_predictions_save(
    date_str: str,
    match_info: dict[str, Any],
    prediction: dict[str, Any],
) -> bool:
    """
    将单场模型预测结果写入 self_improvement_football.jc_predictions。
    match_info 至少含 home, away, league；可选 sporttery_match_id、odds（或 handicap）用于生成单场推荐理由。
    以 (date, sporttery_match_id) 或 (date, home, away) 唯一键 upsert。
    返回是否写入成功。
    """
    db = get_self_improvement_db()
    if db is None:
        return False
    date_only = (date_str or "")[:10]
    if not date_only:
        return False
    mid = match_info.get("sporttery_match_id") or match_info.get("match_id")
    home = (match_info.get("home") or "").strip()
    away = (match_info.get("away") or "").strip()
    # 单场推荐标签、概率与理由（供 Web 单场推荐列展示）
    pred_copy = dict(prediction)
    ph = _float_or_none(pred_copy.get("prob_home_win"))
    pd = _float_or_none(pred_copy.get("prob_draw"))
    pa = _float_or_none(pred_copy.get("prob_away_win"))
    exp_h = _float_or_none(pred_copy.get("exp_goals_home"))
    exp_a = _float_or_none(pred_copy.get("exp_goals_away"))
    if ph is not None and pd is not None and pa is not None:
        handicap = match_info.get("handicap")
        if handicap is None and isinstance(match_info.get("odds"), dict):
            handicap = _parse_handicap(match_info["odds"])
        rec_label, rec_prob = _single_rec_label_and_prob(ph, pd, pa, exp_h, exp_a, handicap)
        pred_copy["single_rec"] = rec_label
        pred_copy["single_rec_prob"] = round(rec_prob, 3)
        min_prob = float(getattr(config, "SINGLE_REC_MIN_PROB", 0.50))
        pred_copy["single_rec_confidence"] = "high" if rec_prob >= min_prob else "low"
        candidates: list[tuple[str, float]] = [("胜", ph), ("平", pd), ("负", pa)]
        if handicap is not None and exp_h is not None and exp_a is not None:
            try:
                hci = int(float(handicap))
            except (TypeError, ValueError):
                hci = None
            if hci is not None:
                rw, rd, rl = _rq_probs_from_prediction_doc(pred_copy, exp_h, exp_a, hci)
                candidates.extend([("让胜", rw), ("让平", rd), ("让负", rl)])
        sorted_cands = sorted(candidates, key=lambda x: -x[1])
        second_prob = sorted_cands[1][1] if len(sorted_cands) > 1 else 0.0
        conf = (pred_copy.get("confidence") or "high").strip().lower()
        model_conf = "high" if conf == "high" else "low"
        high_conf = float(getattr(config, "JC_REC_HIGH_CONFIDENCE_MIN_PROB", 0.60))
        tier_str = "高" if rec_prob >= high_conf else ("中" if rec_prob >= 0.45 else "低")
        pred_copy["single_rec_reason"] = _single_rec_reason(
            rec_label, rec_prob, second_prob, exp_h, exp_a, handicap, tier_str, model_conf
        )
    doc = _sanitize_for_mongo({
        "date": date_only,
        "sporttery_match_id": mid,
        "home": home,
        "away": away,
        "home_cn": match_info.get("home_cn"),
        "away_cn": match_info.get("away_cn"),
        "league": match_info.get("league"),
        "prediction": pred_copy,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
    coll = db[COLLECTION_JC_PREDICTIONS]
    if mid is not None:
        coll.replace_one({"date": date_only, "sporttery_match_id": mid}, doc, upsert=True)
    else:
        coll.replace_one({"date": date_only, "home": home, "away": away}, doc, upsert=True)
    return True


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def jc_predictions_delete_by_date(date_str: str) -> int:
    """删除指定日期的全部预测记录，返回删除条数。用于「运行预测」前清空该日，保证本次为重新预测。"""
    db = get_self_improvement_db()
    if db is None:
        return 0
    date_only = (date_str or "")[:10]
    if not date_only:
        return 0
    result = db[COLLECTION_JC_PREDICTIONS].delete_many({"date": date_only})
    return result.deleted_count


def jc_predictions_load(date_str: str) -> list[dict[str, Any]]:
    """从 self_improvement_football.jc_predictions 读取指定日期的全部预测。"""
    db = get_self_improvement_db()
    if db is None:
        return []
    date_only = (date_str or "")[:10]
    if not date_only:
        return []
    cursor = db[COLLECTION_JC_PREDICTIONS].find({"date": date_only})
    out = []
    for doc in cursor:
        doc.pop("_id", None)
        out.append(doc)
    return out


# 五大联赛（与 sporttery_crawl.BIG5_LEAGUES 一致），用于推荐时优先/限定联赛
_BIG5_LEAGUES = {"英超", "西甲", "意甲", "德甲", "法甲"}


def _parse_handicap(odds: dict[str, Any] | None) -> int | None:
    """从 odds 中解析让球数（rqspf_handicap），竞彩主队让球为负如 -1。"""
    if not odds or not isinstance(odds, dict):
        return None
    raw = odds.get("rqspf_handicap")
    if raw is None:
        return None
    try:
        return int(float(str(raw).strip()))
    except (ValueError, TypeError):
        return None


# 概率分档边界（用于按「联赛×推荐类型×概率区间」统计历史命中率）
_PROB_BUCKET_EDGES = (0.55, 0.60, 0.65, 0.70, 0.75, 1.01)


def _prob_to_bucket(prob: float) -> str:
    """将概率归入分档，用于细分统计 key。"""
    for i in range(len(_PROB_BUCKET_EDGES) - 1):
        if _PROB_BUCKET_EDGES[i] <= prob < _PROB_BUCKET_EDGES[i + 1]:
            return f"{_PROB_BUCKET_EDGES[i]:.2f}-{_PROB_BUCKET_EDGES[i+1]:.2f}"
    return "0.75+"


def _single_rec_label_and_prob_base(
    ph: float, pd: float, pa: float,
    exp_h: float | None, exp_a: float | None,
    handicap: int | None,
) -> tuple[str, float]:
    """
    仅按概率取最高一项（胜/平/负 与 让胜/让平/让负），不做任何启发式。
    用于：① 统计「基线推荐」在各细分的命中率；② 与分段覆盖结合做数据驱动自我提升。
    """
    candidates: list[tuple[str, float]] = [
        ("胜", ph), ("平", pd), ("负", pa),
    ]
    if handicap is not None and exp_h is not None and exp_a is not None:
        rw, rd, rl = _get_rq_probs(exp_h, exp_a, handicap)
        candidates.extend([("让胜", rw), ("让平", rd), ("让负", rl)])
    return max(candidates, key=lambda x: x[1])


def _load_league_rec_params_cached() -> dict[str, dict[str, Any]] | None:
    """加载各联赛单场推荐参数（含达目标命中率所需最低概率）。仅加载一次并缓存。用于今日推荐按联赛门槛过滤。"""
    if getattr(_load_league_rec_params_cached, "_cache", None) is not None:
        return _load_league_rec_params_cached._cache
    path = getattr(config, "JC_REC_LEAGUE_PARAMS_PATH", None) or (config.DATA_DIR / "jc_league_rec_params.json")
    path = Path(path) if path else (config.DATA_DIR / "jc_league_rec_params.json")
    if not path.is_file():
        _load_league_rec_params_cached._cache = None
        return None
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        leagues = data.get("leagues") if isinstance(data, dict) else None
        _load_league_rec_params_cached._cache = leagues if isinstance(leagues, dict) else None
        return _load_league_rec_params_cached._cache
    except Exception:
        _load_league_rec_params_cached._cache = None
        return None


def _load_segment_stats_cached() -> dict[str, Any] | None:
    """加载「联赛×推荐类型×概率区间」历史命中率（用于数据驱动覆盖）。仅加载一次并缓存。配置关闭时不加载。"""
    if getattr(_load_segment_stats_cached, "_cache", None) is not None:
        return _load_segment_stats_cached._cache
    if not getattr(config, "JC_REC_USE_SEGMENT_STATS", False):
        _load_segment_stats_cached._cache = None
        return None
    path = getattr(config, "JC_REC_SEGMENT_STATS_PATH", None) or (config.DATA_DIR / "jc_rec_segment_hit_rates.json")
    path = Path(path) if path else (config.DATA_DIR / "jc_rec_segment_hit_rates.json")
    if not path.is_file():
        _load_segment_stats_cached._cache = None
        return None
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        seg = data.get("segments") if isinstance(data, dict) else None
        _load_segment_stats_cached._cache = {"segments": seg} if seg else None
        return _load_segment_stats_cached._cache
    except Exception:
        _load_segment_stats_cached._cache = None
        return None


def _load_segment_stats_for_date(date_str: str) -> dict[str, Any] | None:
    """按日期加载细分命中率：2026 年且启用辅助年时用 2021–2025 导出的文件，否则用默认缓存。用于提高 2026 比赛列表单场推荐命中率。"""
    date_str = (date_str or "")[:10]
    if date_str.startswith("2026") and getattr(config, "JC_REC_FOR_2026_USE_AUXILIARY", False):
        path = getattr(config, "JC_REC_AUXILIARY_SEGMENT_STATS_PATH", None) or (config.DATA_DIR / "jc_rec_segment_hit_rates_2021_2025.json")
        path = Path(path) if path else (config.DATA_DIR / "jc_rec_segment_hit_rates_2021_2025.json")
        if path.is_file():
            cache = getattr(_load_segment_stats_for_date, "_aux_cache", None)
            if cache is not None and cache.get("path") == str(path):
                return cache.get("data")
            try:
                import json
                data = json.loads(path.read_text(encoding="utf-8"))
                seg = data.get("segments") if isinstance(data, dict) else None
                out = {"segments": seg} if seg else None
                _load_segment_stats_for_date._aux_cache = {"path": str(path), "data": out}
                return out
            except Exception:
                pass
    return _load_segment_stats_cached()


def _load_league_rec_params_for_date(date_str: str) -> dict[str, dict[str, Any]] | None:
    """按日期加载联赛推荐参数：2026 年且启用辅助年时用 2021–2025 导出的文件，否则用默认缓存。"""
    date_str = (date_str or "")[:10]
    if date_str.startswith("2026") and getattr(config, "JC_REC_FOR_2026_USE_AUXILIARY", False):
        path = getattr(config, "JC_REC_AUXILIARY_LEAGUE_PARAMS_PATH", None) or (config.DATA_DIR / "jc_league_rec_params_2021_2025.json")
        path = Path(path) if path else (config.DATA_DIR / "jc_league_rec_params_2021_2025.json")
        if path.is_file():
            cache = getattr(_load_league_rec_params_for_date, "_aux_cache", None)
            if cache is not None and cache.get("path") == str(path):
                return cache.get("data")
            try:
                import json
                data = json.loads(path.read_text(encoding="utf-8"))
                leagues = data.get("leagues") if isinstance(data, dict) else None
                out = leagues if isinstance(leagues, dict) else None
                _load_league_rec_params_for_date._aux_cache = {"path": str(path), "data": out}
                return out
            except Exception:
                pass
    return _load_league_rec_params_cached()


def _apply_segment_override(
    league: str,
    label: str,
    prob: float,
    ph: float,
    pa: float,
    segment_stats: dict[str, Any] | None,
) -> tuple[str, float]:
    """
    根据历史细分命中率决定是否将「让胜/让负」改为「胜/负」。
    当 (league, label, prob_bucket) 历史命中率 < 阈值 且 样本数 >= 最小样本 时，改为胜/负。
    """
    if not segment_stats or label not in ("让胜", "让负"):
        return (label, prob)
    segments = segment_stats.get("segments") or {}
    bucket = _prob_to_bucket(prob)
    key = f"{league}|{label}|{bucket}"
    seg = segments.get(key)
    if not seg or not isinstance(seg, dict):
        return (label, prob)
    n = seg.get("n") or 0
    hit_rate = seg.get("hit_rate")
    if hit_rate is None or n < getattr(config, "JC_REC_SEGMENT_MIN_N", 30):
        return (label, prob)
    floor = getattr(config, "JC_REC_SEGMENT_HIT_RATE_FLOOR", 0.52)
    if hit_rate >= floor:
        return (label, prob)
    # 该细分历史表现差，改为胜/负
    spf_label = "胜" if label == "让胜" else "负"
    spf_prob = ph if label == "让胜" else pa
    return (spf_label, spf_prob)


def _single_rec_label_and_prob(
    ph: float, pd: float, pa: float,
    exp_h: float | None, exp_a: float | None,
    handicap: int | None,
    league: str | None = None,
    segment_stats: dict[str, Any] | None = None,
) -> tuple[str, float]:
    """
    单场推荐：先取概率最高的基线推荐，再根据历史细分命中率（数据驱动）决定是否改为胜/负。
    league 与 segment_stats 均提供时，若该细分历史命中率低于阈值则改为胜/负，实现「根据未命中复盘自我提升」。
    返回 (推荐标签, 概率)。
    """
    base_label, base_prob = _single_rec_label_and_prob_base(ph, pd, pa, exp_h, exp_a, handicap)
    if league and segment_stats:
        return _apply_segment_override(league, base_label, base_prob, ph, pa, segment_stats)
    # 未提供细分数据时使用历史启发式（兼容旧行为）
    if base_label not in ("让胜", "让负"):
        return (base_label, base_prob)
    prefer_draw = getattr(config, "SINGLE_REC_RQ_PREFER_DRAW_WHEN_CLOSE", 0.03)
    if handicap is not None and exp_h is not None and exp_a is not None and prefer_draw > 0:
        rw, rd, rl = _get_rq_probs(exp_h, exp_a, handicap)
        if rd >= base_prob - prefer_draw:
            return ("让平", rd)
    rq_min = getattr(config, "SINGLE_REC_RQ_MIN_PROB", 0.65)
    rq_min_fu = getattr(config, "SINGLE_REC_RQ_MIN_PROB_LET_FU", 0.68)
    gap_fu, gap_win = 0.05, 0.03
    min_rq = rq_min_fu if base_label == "让负" else rq_min
    if base_prob < min_rq:
        spf = "胜" if base_label == "让胜" else "负"
        p_spf = ph if base_label == "让胜" else pa
        gap = gap_fu if base_label == "让负" else gap_win
        if p_spf >= base_prob - gap:
            return (spf, p_spf)
    # 期望进球非常接近时（易打出让平）：让球推荐更保守，优先胜/负
    exp_diff = abs(exp_h - exp_a) if (exp_h is not None and exp_a is not None) else 1.0
    if exp_diff < 0.35:
        spf = "胜" if base_label == "让胜" else "负"
        p_spf = ph if base_label == "让胜" else pa
        gap = gap_fu if base_label == "让负" else gap_win
        if p_spf >= base_prob - gap:
            return (spf, p_spf)
    if exp_diff >= 0.4:
        spf = "胜" if base_label == "让胜" else "负"
        p_spf = ph if base_label == "让胜" else pa
        gap = gap_fu if base_label == "让负" else gap_win
        if p_spf >= base_prob - gap:
            return (spf, p_spf)
    return (base_label, base_prob)


def _single_rec_reason(
    rec_label: str,
    rec_prob: float,
    second_prob: float,
    exp_h: float | None,
    exp_a: float | None,
    handicap: int | None,
    confidence_tier: str,
    model_confidence: str,
) -> str:
    """
    生成单场推荐理由（中文短句），供 Web 展示。
    基于：推荐项、概率、与第二选项差距、期望进球、让球、置信度。
    """
    parts: list[str] = []
    pct = int(round(rec_prob * 100))
    parts.append(f"推荐{rec_label}，概率 {pct}%")
    spread = rec_prob - second_prob
    if spread >= 0.08:
        parts.append("明显领先第二选项")
    elif spread >= 0.05:
        parts.append("领先第二选项")
    if exp_h is not None and exp_a is not None:
        diff = exp_h - exp_a
        if abs(diff) >= 0.6:
            if rec_label in ("胜", "让胜"):
                parts.append("主队期望进球占优")
            elif rec_label in ("负", "让负"):
                parts.append("客队期望进球占优")
            elif rec_label in ("平", "让平"):
                parts.append("期望进球接近")
    if handicap is not None and rec_label in ("让胜", "让平", "让负"):
        hc_str = f"+{handicap}" if handicap > 0 else str(handicap)
        parts.append(f"让球{hc_str}")
    if confidence_tier == "高":
        parts.append("高置信度")
    elif model_confidence == "low":
        parts.append("样本较少仅供参考")
    return "；".join(parts)


def _filter_jc_matches_by_official_ids(
    matches: list[dict[str, Any]],
    official_intersect_ids: set[int] | None,
) -> list[dict[str, Any]]:
    """official_intersect_ids 为 None 不过滤；为 set 时只保留 match_id 在开售列表中的场次（空集则结果为空）。"""
    if official_intersect_ids is None:
        return matches
    out: list[dict[str, Any]] = []
    for m in matches:
        mid = m.get("match_id")
        if mid is None:
            mid = m.get("sporttery_match_id")
        if mid is None:
            continue
        try:
            if int(mid) in official_intersect_ids:
                out.append(m)
        except (TypeError, ValueError):
            continue
    return out


def jc_recommend_bet_slip(
    date_str: str,
    min_prob: float | None = None,
    min_spread: float = 0.06,
    top: int | None = None,
    require_high_confidence: bool = True,
    big5_only: bool = False,
    max_per_league: int | None = 2,
    official_intersect_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """
    今日下注推荐：与「单场推荐」逻辑一致，在 胜/平/负 与 让胜/让平/让负（有让球时）中取概率最高的一项，
    再按 min_prob、min_spread、模型置信度、联赛分散 筛选与排序。
    min_prob 默认从 config.JC_REC_DEFAULT_MIN_PROB 读取（0.60 时展示命中率约 70%）。
    返回列表每项含：recommended（胜/平/负/让胜/让平/让负）, handicap（让球数，无则 null）, recommended_type（spf/rq）等。
    """
    if min_prob is None:
        min_prob = getattr(config, "JC_REC_DEFAULT_MIN_PROB", 0.60)
    date_only = (date_str or "").strip()[:10]
    if not date_only:
        return []
    matches = _filter_jc_matches_by_official_ids(
        jc_match_analysis_list_by_jc_date(date_only),
        official_intersect_ids,
    )
    predictions = jc_predictions_load(date_only)
    pred_by_mid: dict[Any, dict] = {}
    for p in predictions:
        mid = p.get("sporttery_match_id") or p.get("match_id")
        if mid is not None:
            pred_by_mid[mid] = p
    recommended: list[dict[str, Any]] = []
    seg_stats = _load_segment_stats_for_date(date_only)
    league_params = _load_league_rec_params_for_date(date_only) or {}
    for m in matches:
        mid = m.get("match_id") or m.get("sporttery_match_id")
        pred = pred_by_mid.get(mid) if mid is not None else None
        if pred is None:
            continue
        league = (m.get("league") or "").strip()
        if big5_only and league not in _BIG5_LEAGUES:
            continue
        p = pred.get("prediction") or pred
        ph = p.get("prob_home_win")
        pd = p.get("prob_draw")
        pa = p.get("prob_away_win")
        if ph is None or pd is None or pa is None:
            continue
        ph, pd, pa = float(ph), float(pd), float(pa)
        exp_h = p.get("exp_goals_home")
        exp_a = p.get("exp_goals_away")
        if exp_h is not None and exp_a is not None:
            exp_h, exp_a = float(exp_h), float(exp_a)
        else:
            exp_h = exp_a = None
        odds = m.get("odds") if isinstance(m.get("odds"), dict) else {}
        handicap = _parse_handicap(odds)
        rec_label, rec_prob = _single_rec_label_and_prob(ph, pd, pa, exp_h, exp_a, handicap, league=league, segment_stats=seg_stats)
        if rec_prob < min_prob:
            continue
        # 按联赛微调：若该联赛有达 70% 目标所需最低概率，则仅保留 rec_prob >= 该门槛的场次
        league_min = (league_params.get(league) or {}).get("min_prob_70")
        if league_min is not None and rec_prob < league_min:
            continue
        model_conf = (p.get("confidence") or "high").strip().lower()
        if require_high_confidence and model_conf == "low":
            continue
        candidates: list[tuple[str, float]] = [("胜", ph), ("平", pd), ("负", pa)]
        if handicap is not None and exp_h is not None and exp_a is not None:
            rw, rd, rl = _get_rq_probs(exp_h, exp_a, handicap)
            candidates.extend([("让胜", rw), ("让平", rd), ("让负", rl)])
        sorted_cands = sorted(candidates, key=lambda x: -x[1])
        second_prob = sorted_cands[1][1] if len(sorted_cands) > 1 else 0.0
        if rec_prob - second_prob < min_spread:
            continue
        high_conf = float(getattr(config, "JC_REC_HIGH_CONFIDENCE_MIN_PROB", 0.60))
        if rec_prob >= high_conf:
            confidence, tier = "高", "high"
        elif rec_prob >= 0.45:
            confidence, tier = "中", "medium"
        else:
            confidence, tier = "低", "medium"
        reason = _single_rec_reason(
            rec_label, rec_prob, second_prob,
            exp_h, exp_a, handicap,
            confidence, model_conf,
        )
        # 推荐项对应赔率（用于单场推荐卡片展示）
        rec_odds = None
        if odds:
            key = {"胜": "spf_win", "平": "spf_draw", "负": "spf_loss", "让胜": "rqspf_win", "让平": "rqspf_draw", "让负": "rqspf_loss"}.get(rec_label)
            if key and odds.get(key) is not None:
                rec_odds = _float_odds(odds[key])
                if rec_odds is not None:
                    rec_odds = round(rec_odds, 2)
        recommended.append({
            "sporttery_match_id": mid,
            "match_id": mid,
            "match_num": (m.get("match_num") or "").strip(),
            "match_time": (m.get("match_time") or m.get("match_date") or "").strip(),
            "home_cn": m.get("home_cn") or m.get("home") or "",
            "away_cn": m.get("away_cn") or m.get("away") or "",
            "league": league,
            "recommended": rec_label,
            "prob": round(rec_prob, 3),
            "prob_win": round(ph, 3),
            "prob_draw": round(pd, 3),
            "prob_loss": round(pa, 3),
            "confidence": confidence,
            "tier": tier,
            "model_confidence": "high" if model_conf == "high" else "low",
            "is_big5": league in _BIG5_LEAGUES,
            "handicap": handicap,
            "recommended_type": "rq" if rec_label in ("让胜", "让平", "让负") else "spf",
            "recommendation_reason": reason,
            "rec_odds": rec_odds,
        })
    recommended.sort(key=lambda x: (not x["is_big5"], -x["prob"], -x.get("prob_win", 0)))
    if top is not None and top > 0 and max_per_league is not None and max_per_league > 0:
        league_count: dict[str, int] = {}
        out: list[dict[str, Any]] = []
        for r in recommended:
            if len(out) >= top:
                break
            lig = r.get("league") or ""
            if league_count.get(lig, 0) >= max_per_league:
                continue
            out.append(r)
            league_count[lig] = league_count.get(lig, 0) + 1
        recommended = out
    elif top is not None and top > 0:
        recommended = recommended[:top]
    return recommended


def jc_recommend_diagnostic(
    date_str: str,
    min_prob: float | None = None,
    min_spread: float = 0.06,
    require_high_confidence: bool = True,
    official_intersect_ids: set[int] | None = None,
) -> dict[str, Any]:
    """
    诊断「今日推荐」为何没有结果：统计每道筛选的通过数，并返回若干失败样本及原因。
    返回: total_matches, with_predictions, passed_prob, passed_confidence, passed_spread,
          samples (列表，每项含 home_cn, away_cn, reason, rec_prob, second_prob, model_confidence)。
    """
    if min_prob is None:
        min_prob = getattr(config, "JC_REC_DEFAULT_MIN_PROB", 0.60)
    date_only = (date_str or "").strip()[:10]
    if not date_only:
        return {"total_matches": 0, "with_predictions": 0, "passed_prob": 0, "passed_confidence": 0, "passed_spread": 0, "samples": []}
    matches = _filter_jc_matches_by_official_ids(
        jc_match_analysis_list_by_jc_date(date_only),
        official_intersect_ids,
    )
    predictions = jc_predictions_load(date_only)
    pred_by_mid: dict[Any, dict] = {}
    for p in predictions:
        mid = p.get("sporttery_match_id") or p.get("match_id")
        if mid is not None:
            pred_by_mid[mid] = p
    with_predictions = 0
    passed_prob = 0
    passed_confidence = 0
    passed_spread = 0
    samples: list[dict[str, Any]] = []
    max_samples = 5
    for m in matches:
        mid = m.get("match_id") or m.get("sporttery_match_id")
        pred = pred_by_mid.get(mid) if mid is not None else None
        home_cn = (m.get("home_cn") or m.get("home") or "").strip() or "—"
        away_cn = (m.get("away_cn") or m.get("away") or "").strip() or "—"
        if pred is None:
            if len(samples) < max_samples:
                samples.append({"home_cn": home_cn, "away_cn": away_cn, "reason": "无预测", "rec_prob": None, "second_prob": None, "model_confidence": None})
            continue
        with_predictions += 1
        p = pred.get("prediction") or pred
        ph = p.get("prob_home_win")
        pd = p.get("prob_draw")
        pa = p.get("prob_away_win")
        if ph is None or pd is None or pa is None:
            if len(samples) < max_samples:
                samples.append({"home_cn": home_cn, "away_cn": away_cn, "reason": "预测缺胜平负概率", "rec_prob": None, "second_prob": None, "model_confidence": None})
            continue
        ph, pd, pa = float(ph), float(pd), float(pa)
        exp_h = p.get("exp_goals_home")
        exp_a = p.get("exp_goals_away")
        if exp_h is not None and exp_a is not None:
            exp_h, exp_a = float(exp_h), float(exp_a)
        else:
            exp_h = exp_a = None
        odds = m.get("odds") if isinstance(m.get("odds"), dict) else {}
        handicap = _parse_handicap(odds)
        rec_label, rec_prob = _single_rec_label_and_prob(ph, pd, pa, exp_h, exp_a, handicap)
        model_conf = (p.get("confidence") or "high").strip().lower()
        candidates: list[tuple[str, float]] = [("胜", ph), ("平", pd), ("负", pa)]
        if handicap is not None and exp_h is not None and exp_a is not None:
            rw, rd, rl = _get_rq_probs(exp_h, exp_a, handicap)
            candidates.extend([("让胜", rw), ("让平", rd), ("让负", rl)])
        sorted_cands = sorted(candidates, key=lambda x: -x[1])
        second_prob = sorted_cands[1][1] if len(sorted_cands) > 1 else 0.0
        spread = rec_prob - second_prob
        if rec_prob < min_prob:
            if len(samples) < max_samples:
                samples.append({"home_cn": home_cn, "away_cn": away_cn, "reason": "概率不足", "rec_prob": round(rec_prob, 3), "second_prob": round(second_prob, 3), "model_confidence": model_conf})
            continue
        passed_prob += 1
        if require_high_confidence and model_conf == "low":
            if len(samples) < max_samples:
                samples.append({"home_cn": home_cn, "away_cn": away_cn, "reason": "模型置信度低", "rec_prob": round(rec_prob, 3), "second_prob": round(second_prob, 3), "model_confidence": model_conf})
            continue
        passed_confidence += 1
        if spread < min_spread:
            if len(samples) < max_samples:
                samples.append({"home_cn": home_cn, "away_cn": away_cn, "reason": "与第二选项差不足", "rec_prob": round(rec_prob, 3), "second_prob": round(second_prob, 3), "model_confidence": model_conf})
            continue
        passed_spread += 1
    return {
        "date": date_only,
        "total_matches": len(matches),
        "with_predictions": with_predictions,
        "passed_prob": passed_prob,
        "passed_confidence": passed_confidence,
        "passed_spread": passed_spread,
        "samples": samples,
    }


def jc_predictions_eval_available_years() -> list[int]:
    """返回 jc_predictions 中有预测数据的年份列表（按 date 取年），用于前端按数据库实际年份展示统计。"""
    db = get_self_improvement_db()
    if db is None:
        return []
    seen: set[int] = set()
    for doc in db[COLLECTION_JC_PREDICTIONS].find({}, {"date": 1}):
        d = (doc.get("date") or "")[:10]
        if len(d) >= 4:
            try:
                seen.add(int(d[:4]))
            except (TypeError, ValueError):
                pass
    return sorted(seen)


def jc_predictions_load_by_year(year: int) -> list[dict[str, Any]]:
    """从 jc_predictions 读取指定年份的全部预测（date 在 year-01-01 ～ year-12-31）。"""
    return jc_predictions_load_by_date_range(f"{year}-01-01", f"{year}-12-31")


def jc_predictions_load_all() -> list[dict[str, Any]]:
    """从 jc_predictions 读取全部预测（全量，不按日期筛选）。"""
    db = get_self_improvement_db()
    if db is None:
        return []
    cursor = db[COLLECTION_JC_PREDICTIONS].find({})
    out = []
    for doc in cursor:
        doc.pop("_id", None)
        out.append(doc)
    return out


def jc_predictions_load_by_date_range(date_from: str, date_to: str) -> list[dict[str, Any]]:
    """从 jc_predictions 读取日期区间内的全部预测（date 在 date_from ～ date_to，含起止）。"""
    db = get_self_improvement_db()
    if db is None:
        return []
    start = (date_from or "")[:10]
    end = (date_to or "")[:10]
    if not start or not end:
        return []
    cursor = db[COLLECTION_JC_PREDICTIONS].find({"date": {"$gte": start, "$lte": end}})
    out = []
    for doc in cursor:
        doc.pop("_id", None)
        out.append(doc)
    return out


def _int_from_any(v: Any) -> int | None:
    """从任意类型安全转为 int，无效返回 None。"""
    if v is None:
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    if isinstance(v, float) and not isinstance(v, bool):
        try:
            return int(v) if not (v != v) else None  # NaN check
        except (ValueError, TypeError):
            return None
    if isinstance(v, str) and v.strip():
        try:
            return int(v.strip())
        except (ValueError, TypeError):
            pass
    return None


def _score_from_wbsj(wbsj: dict[str, Any]) -> tuple[int | None, int | None]:
    """从 wbsjStats 的 home/away 主客队信息中解析本场比分。常见字段：goal、score、goals、matchGoal 等。"""
    if not isinstance(wbsj, dict):
        return (None, None)
    home_obj = wbsj.get("home")
    away_obj = wbsj.get("away")
    if not isinstance(home_obj, dict) or not isinstance(away_obj, dict):
        return (None, None)
    for key in ("goal", "score", "goals", "matchGoal", "fullTimeGoal", "currentMatchGoal"):
        h = _int_from_any(home_obj.get(key))
        a = _int_from_any(away_obj.get(key))
        if h is not None and a is not None:
            return (h, a)
    return (None, None)


def jc_calibration_samples() -> list[tuple[float, float, float, str, int | None, str | None]]:
    """
    从 self_improvement 库的 jc_predictions + jingcai_match_analysis 收集「预测概率 + 实际结果」，
    用于校准拟合。返回 [(prob_home_win, prob_draw, prob_away_win, actual, competition_id, date_str), ...]，actual 为 'home'|'draw'|'away'，date_str 为 jc_date/match_date 归一化。
    """
    db = get_self_improvement_db()
    if db is None:
        return []
    predictions = jc_predictions_load_all()
    if not predictions:
        return []
    out: list[tuple[float, float, float, str, int | None, str | None]] = []
    for pred in predictions:
        mid = pred.get("sporttery_match_id")
        if mid is None:
            continue
        doc = _jc_match_analysis_find_by_mid(db, mid)
        if not doc:
            continue
        home_s, away_s = _score_from_analysis_doc(doc)
        if home_s is None or away_s is None:
            continue
        if home_s > away_s:
            actual = "home"
        elif away_s > home_s:
            actual = "away"
        else:
            actual = "draw"
        pred_obj = pred.get("prediction") or pred
        ph = pred_obj.get("prob_home_win")
        pd_ = pred_obj.get("prob_draw")
        pa = pred_obj.get("prob_away_win")
        if ph is None or pd_ is None or pa is None:
            continue
        try:
            ph, pd_, pa = float(ph), float(pd_), float(pa)
        except (TypeError, ValueError):
            continue
        cid = pred_obj.get("competition_id")
        if cid is not None:
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                cid = None
        date_val = doc.get("jc_date") or doc.get("match_date")
        date_str = (_normalize_jc_date(date_val) or "")[:10] if date_val else None
        out.append((ph, pd_, pa, actual, cid, date_str))
    return out


def jc_get_ml_training_rows() -> list[tuple]:
    """
    从 jc_predictions + jingcai_match_analysis 收集 ML 训练用行，与 repo.predict_get_ml_training_rows 结构一致。
    每行: (exp_goals_home, exp_goals_away, prob_home_win, prob_draw, prob_away_win, last_n, competition_id, home_score, away_score)。
    仅当已完赛且有比分时返回；用于与主库样本合并后训练 outcome_ml。
    """
    db = get_self_improvement_db()
    if db is None:
        return []
    predictions = jc_predictions_load_all()
    if not predictions:
        return []
    out: list[tuple] = []
    for pred in predictions:
        mid = pred.get("sporttery_match_id")
        if mid is None:
            continue
        doc = _jc_match_analysis_find_by_mid(db, mid)
        if not doc:
            continue
        home_s, away_s = _score_from_analysis_doc(doc)
        if home_s is None or away_s is None:
            continue
        pred_obj = pred.get("prediction") or pred
        exp_h = pred_obj.get("exp_goals_home")
        exp_a = pred_obj.get("exp_goals_away")
        p_h = pred_obj.get("prob_home_win")
        p_d = pred_obj.get("prob_draw")
        p_a = pred_obj.get("prob_away_win")
        last_n = pred_obj.get("last_n") or pred_obj.get("based_on_last_n") or 10
        cid = pred_obj.get("competition_id")
        if exp_h is None or exp_a is None or p_h is None or p_d is None or p_a is None:
            continue
        try:
            exp_h = float(exp_h)
            exp_a = float(exp_a)
            p_h = float(p_h)
            p_d = float(p_d)
            p_a = float(p_a)
            last_n = int(last_n)
        except (TypeError, ValueError):
            continue
        date_val = doc.get("jc_date") or doc.get("match_date")
        match_date_str = (_normalize_jc_date(date_val) or "")[:10] if date_val else None
        out.append((exp_h, exp_a, p_h, p_d, p_a, last_n, cid, int(home_s), int(away_s), None, None, match_date_str))
    return out


def parse_jc_match_kickoff_naive(match_row: dict[str, Any]) -> datetime | None:
    """
    从列表行中的 match_date、match_time 解析本地开球时间（naive datetime），用于判断是否已到可拉赛果的时间。
    match_time 可能为完整日期时间或仅「时:分」；无法解析时返回 None（调用方通常仍会拉取详情）。
    """
    import re

    t = (match_row.get("match_time") or "").strip()
    d = (match_row.get("match_date") or "").strip()[:10]
    if len(t) >= 16 and t[0:4].isdigit() and (" " in t or "T" in t):
        ts = t.replace("T", " ").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(ts[:19], fmt)
            except ValueError:
                continue
    if len(d) == 10 and d[4] == "-" and d[7] == "-":
        mat = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", t)
        if mat:
            try:
                h, mi = int(mat.group(1)), int(mat.group(2))
                return datetime.strptime(f"{d} {h:02d}:{mi:02d}", "%Y-%m-%d %H:%M")
            except ValueError:
                return None
    return None


def jc_match_analysis_backfill_scores_from_uniform_api(
    jc_date: str,
    *,
    uniform_rate_limit_sec: float = 0.15,
) -> dict[str, Any]:
    """
    用体彩 getUniformMatchResultV1（赛果开奖页同源）按日期批量回填 jingcai_match_analysis 的
    home_score / away_score / half_home / half_away。查询日 = 竞彩日 + 库内各场 match_date，避免仅按竞彩日查不到次日开球场次。
    uniform_rate_limit_sec：赛果接口分页之间的休眠，略大于 0 可降低封 IP 风险。
    """
    from etl.sporttery_crawl import fetch_uniform_match_results_by_match_id_for_dates

    db = get_self_improvement_db()
    day = (jc_date or "").strip()[:10]
    if db is None or len(day) != 10:
        return {"ok": False, "patched": 0, "dates_queried": [], "uniform_rows": 0, "error": "no_db_or_date"}
    dates: set[str] = {day}
    for m in jc_match_analysis_list_by_jc_date(day):
        md = (m.get("match_date") or "").strip()[:10]
        if len(md) == 10 and md[4] == "-" and md[7] == "-":
            dates.add(md)
    idx = fetch_uniform_match_results_by_match_id_for_dates(
        dates, page_size=30, rate_limit_sec=max(0.0, float(uniform_rate_limit_sec))
    )
    if not idx:
        return {"ok": True, "patched": 0, "dates_queried": sorted(dates), "uniform_rows": 0}
    day_vals = _jc_date_query_values(day)
    query: dict[str, Any] = {"$or": [{"jc_date": {"$in": day_vals}}, {"date": {"$in": day_vals}}]}
    patched = 0
    now_s = datetime.utcnow().isoformat() + "Z"
    for coll in _jc_match_analysis_collections(db):
        for doc in coll.find(query):
            pt = (doc.get("match_head") or {}).get("pageTitle") or doc.get("match_head") or {}
            if not isinstance(pt, dict):
                pt = {}
            match_num = (doc.get("list_match_num") or pt.get("matchNum") or pt.get("match_num") or "").strip()
            if _is_match_num_previous_jc_day(match_num, day):
                continue
            mid = doc.get("sporttery_match_id")
            if mid is None:
                continue
            try:
                mid_i = int(mid)
            except (TypeError, ValueError):
                continue
            payload = idx.get(mid_i)
            if not payload:
                continue
            set_fields: dict[str, Any] = {}
            for k in ("home_score", "away_score", "half_home", "half_away"):
                vi = _int_from_any(payload.get(k))
                if vi is not None:
                    set_fields[k] = vi
            if not set_fields:
                continue
            set_fields["updated_at"] = now_s
            coll.update_one({"_id": doc["_id"]}, {"$set": set_fields})
            patched += 1
    return {"ok": True, "patched": patched, "dates_queried": sorted(dates), "uniform_rows": len(idx)}


def jc_match_analysis_backfill_halftime_from_api_football(
    jc_date: str,
    *,
    rate_limit_sec: float = 0.2,
) -> dict[str, Any]:
    """
    当体彩赛果/详情缺少半场比分时，按 api_football_fixture_id 请求 API-Football fixtures?id，
    解析 score.halftime（兼容 halfTime）回填 half_home / half_away。
    仅更新当前为空的半场字段，不覆盖已有值。
    """
    from etl.injury_api_football import _request
    import time

    db = get_self_improvement_db()
    day = (jc_date or "").strip()[:10]
    if db is None or len(day) != 10:
        return {
            "ok": False,
            "attempted": 0,
            "patched": 0,
            "api_no_data": 0,
            "skipped_no_fixture_id": 0,
            "errors": 0,
            "error": "no_db_or_date",
        }
    day_vals = _jc_date_query_values(day)
    query: dict[str, Any] = {"$or": [{"jc_date": {"$in": day_vals}}, {"date": {"$in": day_vals}}]}
    attempted = 0
    patched = 0
    api_no_data = 0
    skipped_no_fixture_id = 0
    errors = 0
    now_s = datetime.utcnow().isoformat() + "Z"
    delay = max(0.0, float(rate_limit_sec))
    for coll in _jc_match_analysis_collections(db):
        for doc in coll.find(query):
            hh0 = _int_from_any(doc.get("half_home"))
            ha0 = _int_from_any(doc.get("half_away"))
            if hh0 is not None and ha0 is not None:
                continue
            fixture_id = doc.get("api_football_fixture_id")
            if fixture_id is None:
                skipped_no_fixture_id += 1
                continue
            try:
                fixture_id = int(fixture_id)
            except (TypeError, ValueError):
                skipped_no_fixture_id += 1
                continue
            attempted += 1
            try:
                data = _request("fixtures", {"id": fixture_id}, quiet=True)
                rows = data.get("response") or []
                item = rows[0] if rows else {}
                score = item.get("score") or {}
                ht = score.get("halftime") or score.get("halfTime") or {}
                hh = _int_from_any(ht.get("home"))
                ha = _int_from_any(ht.get("away"))
                set_fields: dict[str, Any] = {}
                if hh is not None and hh0 is None:
                    set_fields["half_home"] = hh
                if ha is not None and ha0 is None:
                    set_fields["half_away"] = ha
                if set_fields:
                    set_fields["updated_at"] = now_s
                    coll.update_one({"_id": doc["_id"]}, {"$set": set_fields})
                    patched += 1
                else:
                    api_no_data += 1
            except Exception:
                errors += 1
            if delay > 0:
                time.sleep(delay)
    return {
        "ok": True,
        "attempted": attempted,
        "patched": patched,
        "api_no_data": api_no_data,
        "skipped_no_fixture_id": skipped_no_fixture_id,
        "errors": errors,
    }


def _jc_row_has_complete_scores(row: dict[str, Any]) -> bool:
    """列表行是否已有全场+半场比分（与 jc_match_row_should_skip_result_fetch 判定一致）。"""
    hs, aws = row.get("home_score"), row.get("away_score")
    hh, ha = row.get("half_home"), row.get("half_away")
    return hs is not None and aws is not None and hh is not None and ha is not None


def _jc_sync_all_matches_have_complete_scores(matches: list[dict[str, Any]]) -> bool:
    """该日全部场次均已具备全场+半场且均有 match_id 时返回 True，可跳过赛果/详情等全部体彩请求。"""
    if not matches:
        return False
    for m in matches:
        mid = m.get("match_id") if m.get("match_id") is not None else m.get("sporttery_match_id")
        if mid is None:
            return False
        if not _jc_row_has_complete_scores(m):
            return False
    return True


def jc_match_analysis_sync_results_for_jc_date(
    jc_date: str,
    *,
    only_missing_scores: bool = True,
    respect_kickoff: bool = True,
    detail_delay_sec: float = 0.0,
    uniform_rate_limit_sec: float = 0.15,
    backfill_fixture_id: bool = False,
) -> dict[str, Any]:
    """
    与仪表盘「拉取比赛结果」、POST /api/jc-match-detail/save-by-date 相同逻辑：
    先 getUniformMatchResultV1 批量写全场/半场，再对仍缺数据的场次逐场 getMatchHeadV1 等详情并 jc_match_analysis_save。
    detail_delay_sec：每场详情拉取结束后休眠秒数（单场会连打多个体彩接口），批量时建议 >0 防封；Web API 默认 0。
    backfill_fixture_id：为 True 时在本流程末尾尝试用 API-Football 赛程匹配并写入 api_football_fixture_id（默认 False，避免拉赛果时消耗 API 配额）。
    """
    import time

    from etl.sporttery_crawl import fetch_sporttery_match_detail_via_api

    day = (jc_date or "").strip()[:10]
    if len(day) != 10:
        return {
            "date": day,
            "saved": 0,
            "failed": [],
            "skipped": 0,
            "total": 0,
            "message": "date 须为 YYYY-MM-DD",
            "uniform_match_result": {},
        }

    uniform_meta: dict[str, Any] = {"patched": 0, "dates_queried": [], "uniform_rows": 0}
    matches = jc_match_analysis_list_by_jc_date(day)
    if not matches:
        matches = jc_matches_load(day)
    if not matches:
        return {
            "date": day,
            "saved": 0,
            "failed": [],
            "skipped": 0,
            "total": 0,
            "message": "该日无已保存场次，请先拉取竞彩列表并保存到 jingcai_match_analysis",
            "uniform_match_result": uniform_meta,
        }

    if only_missing_scores and _jc_sync_all_matches_have_complete_scores(matches):
        return {
            "date": day,
            "saved": 0,
            "failed": [],
            "skipped": len(matches),
            "skipped_detail": [],
            "total": len(matches),
            "fetched_attempts": 0,
            "message": "该竞彩日全部场次已含全场与半场比分，已跳过体彩请求（无需重复拉取）。",
            "uniform_match_result": {"skipped": True, "reason": "all_scores_complete"},
            "backfill_fixture_id": None,
        }

    try:
        uniform_meta = jc_match_analysis_backfill_scores_from_uniform_api(
            day, uniform_rate_limit_sec=uniform_rate_limit_sec
        )
        matches = jc_match_analysis_list_by_jc_date(day)
        if not matches:
            matches = jc_matches_load(day)
    except Exception:
        uniform_meta = {"patched": 0, "dates_queried": [], "uniform_rows": 0, "error": "uniform_backfill_failed"}
        matches = jc_match_analysis_list_by_jc_date(day)
        if not matches:
            matches = jc_matches_load(day)

    rows_to_fetch: list[tuple[int, dict[str, Any]]] = []
    skipped: list[dict[str, Any]] = []
    for m in matches:
        mid = m.get("match_id") if m.get("match_id") is not None else m.get("sporttery_match_id")
        if mid is None:
            continue
        try:
            mid_i = int(mid)
        except (TypeError, ValueError):
            continue
        skip, reason = jc_match_row_should_skip_result_fetch(
            m,
            only_missing_scores=only_missing_scores,
            respect_kickoff=respect_kickoff,
        )
        if skip:
            skipped.append({"match_id": mid_i, "reason": reason})
            continue
        rows_to_fetch.append((mid_i, m))

    if not rows_to_fetch:
        u_patch = int(uniform_meta.get("patched") or 0)
        u_rows = int(uniform_meta.get("uniform_rows") or 0)
        early_msg = f"无需逐场详情：{len(skipped)} 场已跳过（已有比分或未到开球等）。"
        if u_patch > 0 or u_rows > 0:
            early_msg = f"赛果接口 getUniformMatchResultV1 已写入 {u_patch} 场（接口 {u_rows} 条）。" + early_msg
        return {
            "date": day,
            "saved": 0,
            "failed": [],
            "skipped": len(skipped),
            "skipped_detail": skipped[:30],
            "total": len(matches),
            "message": early_msg,
            "uniform_match_result": uniform_meta,
        }

    saved_count = 0
    failed_list: list[int] = []
    delay_detail = max(0.0, float(detail_delay_sec))
    n_fetch = len(rows_to_fetch)
    for fi, (mid, _m) in enumerate(rows_to_fetch):
        try:
            detail = fetch_sporttery_match_detail_via_api(mid)
            detail["jc_date"] = day
            if jc_match_analysis_save(mid, detail):
                saved_count += 1
            else:
                failed_list.append(mid)
        except Exception:
            failed_list.append(mid)
        if delay_detail > 0 and fi < n_fetch - 1:
            time.sleep(delay_detail)

    backfill_fixture = None
    if backfill_fixture_id and saved_count > 0:
        try:
            backfill_fixture = jc_match_analysis_backfill_fixture_id_for_jc_date(
                jc_date=day, rate_limit_sec=1.0, overwrite_existing=False
            )
        except Exception:
            pass
    halftime_fixture_backfill: dict[str, Any] | None = None
    try:
        db = get_self_improvement_db()
        if db is not None:
            day_vals = _jc_date_query_values(day)
            query_day: dict[str, Any] = {"$or": [{"jc_date": {"$in": day_vals}}, {"date": {"$in": day_vals}}]}
            need_fixture = False
            for coll in _jc_match_analysis_collections(db):
                for doc in coll.find(query_day, {"half_home": 1, "half_away": 1, "api_football_fixture_id": 1}):
                    hh0 = _int_from_any(doc.get("half_home"))
                    ha0 = _int_from_any(doc.get("half_away"))
                    if hh0 is None or ha0 is None:
                        if doc.get("api_football_fixture_id") is None:
                            need_fixture = True
                            break
                if need_fixture:
                    break
            if need_fixture:
                halftime_fixture_backfill = jc_match_analysis_backfill_fixture_id_for_jc_date(
                    jc_date=day, rate_limit_sec=1.0, overwrite_existing=False
                )
    except Exception:
        halftime_fixture_backfill = {"ok": False, "error": "halftime_fixture_backfill_failed"}

    halftime_backfill_meta = {
        "ok": False,
        "attempted": 0,
        "patched": 0,
        "api_no_data": 0,
        "skipped_no_fixture_id": 0,
        "errors": 0,
    }
    try:
        halftime_backfill_meta = jc_match_analysis_backfill_halftime_from_api_football(
            day, rate_limit_sec=max(0.0, detail_delay_sec)
        )
    except Exception:
        halftime_backfill_meta = {
            "ok": False,
            "attempted": 0,
            "patched": 0,
            "api_no_data": 0,
            "skipped_no_fixture_id": 0,
            "errors": 1,
            "error": "api_football_halftime_backfill_failed",
        }

    u_patch = int(uniform_meta.get("patched") or 0)
    u_rows = int(uniform_meta.get("uniform_rows") or 0)
    msg = ""
    if u_patch > 0 or u_rows > 0:
        msg = f"赛果接口 getUniformMatchResultV1 已写入 {u_patch} 场（接口共 {u_rows} 条 matchId）"
    msg += ("；" if msg else "") + f"详情接口已更新 {saved_count}/{len(rows_to_fetch)} 场（该日共 {len(matches)} 场，跳过 {len(skipped)} 场）"
    if failed_list:
        msg += "，部分失败"
    if backfill_fixture and backfill_fixture.get("updated", 0) > 0:
        msg += f"；已回填 api_football_fixture_id {backfill_fixture['updated']} 条"
    if halftime_fixture_backfill and int(halftime_fixture_backfill.get("updated", 0) or 0) > 0:
        msg += f"；半场兜底前自动补全 fixture_id {int(halftime_fixture_backfill.get('updated', 0) or 0)} 条"
    hp = int(halftime_backfill_meta.get("patched") or 0)
    ha = int(halftime_backfill_meta.get("attempted") or 0)
    if hp > 0:
        msg += f"；API-Football 半场回填 {hp}/{ha} 场"

    return {
        "date": day,
        "saved": saved_count,
        "failed": failed_list[:20],
        "skipped": len(skipped),
        "skipped_detail": skipped[:30],
        "total": len(matches),
        "fetched_attempts": len(rows_to_fetch),
        "message": msg,
        "backfill_fixture_id": backfill_fixture,
        "halftime_fixture_backfill": halftime_fixture_backfill,
        "halftime_backfill": halftime_backfill_meta,
        "uniform_match_result": uniform_meta,
    }


def jc_match_row_should_skip_result_fetch(
    row: dict[str, Any],
    *,
    only_missing_scores: bool = True,
    respect_kickoff: bool = True,
    kickoff_grace_minutes: int = 15,
) -> tuple[bool, str]:
    """
    是否跳过「拉取比赛结果」中对该场的详情请求。
    only_missing_scores：已有全场且已有半场则跳过（仅有全场无半场仍会拉取以补半场）；respect_kickoff：开球时间未到则跳过。
    """
    if only_missing_scores:
        if _jc_row_has_complete_scores(row):
            return True, "已有全场与半场比分"
    if respect_kickoff:
        kick = parse_jc_match_kickoff_naive(row)
        if kick is not None and kick > datetime.now() + timedelta(minutes=kickoff_grace_minutes):
            return True, "未到开球时间"
    return False, ""


def _score_from_analysis_doc(doc: dict[str, Any]) -> tuple[int | None, int | None]:
    """从 jingcai_match_analysis 文档解析全场比分，返回 (home_score, away_score)，无则 (None, None)。
    优先级：顶层 home_score/away_score → match_head.pageTitle.fullCourtGoal → wbsjStats 主客队信息 → 其他。"""
    pt = (doc.get("match_head") or {}).get("pageTitle") or doc.get("match_head") or {}
    if not isinstance(pt, dict):
        pt = {}
    home_s = doc.get("home_score")
    away_s = doc.get("away_score")
    if home_s is not None and away_s is not None:
        try:
            return (int(home_s), int(away_s))
        except (TypeError, ValueError):
            pass
    fcg = pt.get("fullCourtGoal")
    if isinstance(fcg, dict) and ("home" in fcg or "away" in fcg):
        try:
            h = int(fcg["home"]) if fcg.get("home") is not None else None
            a = int(fcg["away"]) if fcg.get("away") is not None else None
            if h is not None and a is not None:
                return (h, a)
        except (TypeError, ValueError):
            pass
    if isinstance(fcg, str) and fcg.strip():
        parts = fcg.replace("：", ":").split(":")
        if len(parts) >= 2:
            try:
                return (int(parts[0].strip()), int(parts[1].strip()))
            except (ValueError, TypeError):
                pass
    # 比分在 wbsjStats 的主客队信息中（如 goal、score、matchGoal 等）
    wbsj = doc.get("wbsjStats") or pt.get("wbsjStats") or (doc.get("match_head") or {}).get("wbsjStats")
    if isinstance(wbsj, dict):
        h, a = _score_from_wbsj(wbsj)
        if h is not None and a is not None:
            return (h, a)
    return (None, None)


def _float_odds(val: Any) -> float | None:
    """把赔率字符串或数字转为 float，无效返回 None。"""
    if val is None:
        return None
    try:
        return float(str(val).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _poisson_p(lam: float, k: int) -> float:
    """Poisson(lam, k)。"""
    if k < 0:
        return 0.0
    p = math.exp(-lam)
    for i in range(1, k + 1):
        p *= lam / i
    return p


def _prob_diff(exp_h: float, exp_a: float, d: int) -> float:
    """从期望进球用 Poisson 近似 P(主-客 = d)。"""
    total = 0.0
    max_g = 12
    for i in range(max_g + 1):
        j = i - d
        if 0 <= j <= max_g:
            total += _poisson_p(exp_h, i) * _poisson_p(exp_a, j)
    return total


def _get_rq_probs(exp_h: float, exp_a: float, rq: int) -> tuple[float, float, float]:
    """根据期望进球与让球数计算 (P(让胜), P(让平), P(让负))。当期望差与让球数接近时对让平做加成，减少「实际让平」误判为让胜/让负。"""
    p_win, p_draw, p_loss = 0.0, 0.0, 0.0
    for d in range(-12, 13):
        prob = _prob_diff(exp_h, exp_a, d)
        if d + rq > 0:
            p_win += prob
        elif d + rq == 0:
            p_draw += prob
        else:
            p_loss += prob
    # 让平增强：期望差 + 让球数 接近 0 时，实际常打出让平，对让平概率加成后重归一化
    threshold = getattr(config, "PREDICT_RQ_DRAW_BOOST_THRESHOLD", 0.55)
    boost = getattr(config, "PREDICT_RQ_DRAW_BOOST", 0.10)
    if threshold > 0 and boost > 0:
        exp_diff = exp_h - exp_a
        margin_after_rq = exp_diff + rq
        if abs(margin_after_rq) <= threshold:
            p_draw = min(1.0, p_draw + boost)
            total = p_win + p_draw + p_loss
            if total > 0:
                p_win, p_draw, p_loss = p_win / total, p_draw / total, p_loss / total
    return (p_win, p_draw, p_loss)


def _rq_probs_from_prediction_doc(
    pred_obj: dict[str, Any],
    exp_h: float,
    exp_a: float,
    hc: int,
) -> tuple[float, float, float]:
    """优先用 jc_play_markets.rqspf（DC+hhad 融合结果），否则回退泊松让球。"""
    jcp = pred_obj.get("jc_play_markets") if isinstance(pred_obj.get("jc_play_markets"), dict) else {}
    rqb = jcp.get("rqspf") if isinstance(jcp.get("rqspf"), dict) else None
    if rqb and isinstance(rqb.get("probs"), dict):
        pr = rqb["probs"]
        try:
            return (float(pr["win"]), float(pr["draw"]), float(pr["loss"]))
        except (KeyError, TypeError, ValueError):
            pass
    return _get_rq_probs(exp_h, exp_a, hc)


def _min_prob_for_target_hit_rate(
    rows: list[tuple[float, bool]],
    target: float = 0.70,
    min_n: int = 20,
) -> float | None:
    """
    给定 (推荐概率, 是否命中) 列表，求最小概率门槛 th，使得 推荐概率>=th 的子集 命中率>=target 且 样本数>=min_n。
    若无法达 target，则返回能达到的「最高命中率」对应的最小门槛，使每个联赛都有 min_prob_70 可过滤。
    """
    if not rows:
        return None
    thresholds = [x * 0.01 for x in range(50, 81, 2)]  # 0.50, 0.52, ..., 0.80
    for th in thresholds:
        subset = [(p, h) for p, h in rows if p >= th]
        if len(subset) < min_n:
            continue
        acc = sum(1 for _, h in subset if h) / len(subset)
        if acc >= target:
            return round(th, 2)
    # 无法达 target：取满足 min_n 且命中率最高的子集所对应的最小门槛
    best_th, best_acc = None, -1.0
    for th in thresholds:
        subset = [(p, h) for p, h in rows if p >= th]
        if len(subset) < min_n:
            continue
        acc = sum(1 for _, h in subset if h) / len(subset)
        if acc > best_acc:
            best_acc = acc
            best_th = th
    return round(best_th, 2) if best_th is not None else None


def jc_predictions_eval_report(
    year: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    all_time: bool = False,
    missed_cap: int = 500,
    segment_stats: dict[str, Any] | None = None,
    export_segment_stats_path: str | None = None,
    export_league_rec_params_path: str | None = None,
    include_hit_matches: bool = False,
) -> dict[str, Any]:
    """
    对比 jc_predictions 与 jingcai_match_analysis 中已完赛的比分，统计胜平负命中率与精确比分命中率。
    segment_stats: 由 --segment-stats 加载的「联赛×推荐类型×概率区间」历史命中率，用于数据驱动覆盖（让球→胜/负）。
    export_segment_stats_path: 若提供，将本次评估的基线推荐在各细分的命中率写入该文件，供后续 --segment-stats 使用。
    export_league_rec_params_path: 若提供，将各联赛单场推荐命中率及达目标命中率所需最低概率写入 JSON，供今日推荐按联赛门槛过滤。
    返回: n, by_segment, range_label, by_league_rec 等；by_segment 为 { "league|rec_type|bucket": {"n", "hit_rate"} }。
    """
    db = get_self_improvement_db()
    if db is None:
        return {"n": 0, "total_predictions": 0, "outcome_correct": 0, "outcome_accuracy": 0.0, "exact_score_correct": 0, "exact_score_accuracy": 0.0, "by_league": [], "message": "未配置 MONGO_URI"}
    if all_time:
        predictions = jc_predictions_load_all()
        range_label = "全量"
    elif date_from and date_to:
        predictions = jc_predictions_load_by_date_range(date_from, date_to)
        range_label = f"{date_from[:10]}～{date_to[:10]}"
    elif year is not None:
        predictions = jc_predictions_load_by_year(year)
        range_label = f"{year}年"
    else:
        return {"n": 0, "total_predictions": 0, "outcome_correct": 0, "outcome_accuracy": 0.0, "exact_score_correct": 0, "exact_score_accuracy": 0.0, "by_league": [], "message": "请指定 all_time、year 或 date_from+date_to"}
    if not predictions:
        return {"n": 0, "total_predictions": 0, "outcome_correct": 0, "outcome_accuracy": 0.0, "exact_score_correct": 0, "exact_score_accuracy": 0.0, "by_league": [], "range_label": range_label, "message": f"{range_label} 无预测数据", "diagnostic": {"predictions_count": 0}}
    outcome_ok = 0
    exact_ok = 0
    n = 0
    high_conf_threshold = 0.45
    n_high_conf = 0
    outcome_ok_high_conf = 0
    # 按置信度分档，便于看「只推荐高置信」时能否接近 70% 目标
    eval_rows: list[tuple[float, bool]] = []  # (max_prob, hit)
    by_league: dict[str, list[int]] = {}
    details_sample: list[dict[str, Any]] = []
    pred_with_mid = 0
    matched_analysis = 0
    matched_with_score = 0
    # 让球胜平负：仅统计有让球数且有比分的场次
    n_rq = 0
    n_rq_has_odds = 0  # 有让球且有三项赔率的场次
    rq_ok_odds = 0     # 按赔率最低项作为“预测”的命中数
    rq_ok_model = 0    # 按模型预测比分+让球数推导的让球结果的命中数
    by_league_rq: dict[str, list[int]] = {}  # league -> [n_rq, n_has_odds, rq_ok_odds, rq_ok_model]
    # 单场推荐：按概率权重在 胜/平/负 与 让胜/让平/让负 中取概率最高的一项；并记录 (rec_prob, hit) 用于分档统计
    single_rec_ok = 0
    single_rec_n = 0
    single_rec_rows: list[tuple[float, bool]] = []  # (rec_prob, hit)
    # 按联赛统计单场推荐 (prob, hit)，用于计算各联赛达目标命中率所需最低概率
    by_league_single_rec: dict[str, list[tuple[float, bool]]] = {}
    # 未命中复盘：收集单场推荐错的场次及简短原因，便于「看看为什么会错」；missed_cap<=0 表示不限制
    missed_single_rec: list[dict[str, Any]] = []
    _missed_cap = missed_cap if missed_cap > 0 else 0
    # 按「联赛×推荐类型×概率区间」统计基线推荐命中率，用于数据驱动自我提升
    segment_counts: dict[str, tuple[int, int]] = {}  # key -> (n, hits) 基于基线推荐
    hit_matches_tier60: list[dict[str, Any]] = []
    hit_matches_tier65: list[dict[str, Any]] = []  # ≥65% 命中列表，更少更稳
    hit_matches_tier55: list[dict[str, Any]] = []  # ≥55% 命中列表，场次多于 tier60
    hit_matches_single_rec: list[dict[str, Any]] = []
    # jc_play_markets：总进球桶 / 比分 best_pick / 半全场 / 让球三项 argmax
    ext_zjqs_n = ext_zjqs_ok = 0
    ext_bf_n = ext_bf_ok = 0
    ext_bqc_n = ext_bqc_ok = 0
    n_rq_jc_rqspf = rq_ok_jc_rqspf = 0

    from engine.jc_market_probs import (
        actual_bf_label,
        actual_bqc_label,
        actual_zjqs_bucket,
        jc_rqspf_actual_side,
        jc_rqspf_pick_to_side,
    )

    # 批量查赛果，避免对每条预测做一次 find_one（N+1 导致统计页极慢）
    mids = [p.get("sporttery_match_id") for p in predictions if p.get("sporttery_match_id") is not None]
    analysis_by_mid = _jc_match_analysis_batch_find(db, mids)

    for pred in predictions:
        mid = pred.get("sporttery_match_id")
        if mid is None:
            continue
        pred_with_mid += 1
        doc = analysis_by_mid.get(mid)
        if not doc:
            continue
        matched_analysis += 1
        home_s, away_s = _score_from_analysis_doc(doc)
        if home_s is None or away_s is None:
            continue
        matched_with_score += 1
        n += 1
        actual_outcome = "主胜" if home_s > away_s else ("客胜" if away_s > home_s else "平")
        pred_obj = pred.get("prediction") or pred
        ph = pred_obj.get("prob_home_win")
        pd_ = pred_obj.get("prob_draw")
        pa = pred_obj.get("prob_away_win")
        if ph is None and pd_ is None and pa is None:
            pred_outcome = None
        else:
            ph = float(ph) if ph is not None else 0.0
            pd_ = float(pd_) if pd_ is not None else 0.0
            pa = float(pa) if pa is not None else 0.0
            if ph >= pd_ and ph >= pa:
                pred_outcome = "主胜"
            elif pa >= pd_ and pa >= ph:
                pred_outcome = "客胜"
            else:
                pred_outcome = "平"
        if pred_outcome == actual_outcome:
            outcome_ok += 1
        max_prob = max(ph, pd_, pa) if (ph is not None and pd_ is not None and pa is not None) else 0.0
        if max_prob >= high_conf_threshold:
            n_high_conf += 1
            if pred_outcome == actual_outcome:
                outcome_ok_high_conf += 1
        eval_rows.append((max_prob, pred_outcome == actual_outcome))
        league = (pred.get("league") or "").strip() or "其他"
        if league not in by_league:
            by_league[league] = [0, 0]
        by_league[league][0] += 1
        if pred_outcome == actual_outcome:
            by_league[league][1] += 1
        odds_for_display = doc.get("odds") if isinstance(doc.get("odds"), dict) else {}
        hc_raw_d = odds_for_display.get("rqspf_handicap")
        try:
            hc_display = int(hc_raw_d) if hc_raw_d is not None and str(hc_raw_d).strip() != "" else None
        except (TypeError, ValueError):
            hc_display = None
        actual_rq_display: str | None = None
        if hc_display is not None:
            adj_d = (home_s - away_s) + hc_display
            rq_d = "主" if adj_d > 0 else ("客" if adj_d < 0 else "平")
            actual_rq_display = "让胜" if rq_d == "主" else ("让平" if rq_d == "平" else "让负")
        if include_hit_matches and pred_outcome == actual_outcome:
            jc_date = (doc.get("jc_date") or doc.get("match_date") or "")[:10]
            entry = {
                "home_cn": pred.get("home_cn") or pred.get("home") or "—",
                "away_cn": pred.get("away_cn") or pred.get("away") or "—",
                "league": league,
                "jc_date": jc_date,
                "score": f"{home_s}:{away_s}",
                "handicap": hc_display,
                "model_pred": pred_outcome,
                "recommended": pred_outcome,
                "rec_prob": round(max_prob * 100),
                "actual_outcome": actual_outcome,
                "actual_rq_display": actual_rq_display,
            }
            if max_prob >= 0.55:
                hit_matches_tier55.append(entry)
            if max_prob >= 0.6:
                hit_matches_tier60.append(entry)
            if max_prob >= 0.65:
                hit_matches_tier65.append(entry)
        pred_score = pred_obj.get("predicted_score")
        if isinstance(pred_score, (list, tuple)) and len(pred_score) >= 2:
            try:
                ph_s, pa_s = int(pred_score[0]), int(pred_score[1])
                if ph_s == home_s and pa_s == away_s:
                    exact_ok += 1
            except (TypeError, ValueError):
                pass

        jcpm = pred_obj.get("jc_play_markets")
        if isinstance(jcpm, dict):
            zj = jcpm.get("zjqs") or {}
            pz = zj.get("best_pick")
            if pz is not None:
                act_z = actual_zjqs_bucket(home_s, away_s)
                pzs = str(pz).strip()
                if pzs.upper().replace("＋", "+") in ("7+", "+7"):
                    pzs = "7"
                ext_zjqs_n += 1
                if pzs == act_z:
                    ext_zjqs_ok += 1
            bf_b = jcpm.get("bf") or {}
            pb = bf_b.get("best_pick")
            if pb is not None:
                ext_bf_n += 1
                if str(pb).strip() == actual_bf_label(home_s, away_s):
                    ext_bf_ok += 1
            bqc_b = jcpm.get("bqc") or {}
            qb = bqc_b.get("best_pick")
            hi0 = doc.get("half_home")
            ai0 = doc.get("half_away")
            if qb is not None and hi0 is not None and ai0 is not None:
                try:
                    act_bqc = actual_bqc_label(int(hi0), int(ai0), int(home_s), int(away_s))
                except (TypeError, ValueError):
                    act_bqc = None
                if act_bqc:
                    ext_bqc_n += 1
                    if str(qb).strip() == act_bqc:
                        ext_bqc_ok += 1

        # 让球胜平负：需有让球数及赔率/预测比分
        odds = doc.get("odds") if isinstance(doc.get("odds"), dict) else {}
        hc_raw = odds.get("rqspf_handicap")
        try:
            hc = int(hc_raw) if hc_raw is not None and str(hc_raw).strip() != "" else None
        except (TypeError, ValueError):
            hc = None
        pred_rq_model: str | None = None
        actual_rq: str | None = None
        if hc is not None:
            # 实际让球结果：(主-客)+让球数 >0 主胜，<0 客胜，=0 平
            adj = (home_s - away_s) + hc
            actual_rq = "主" if adj > 0 else ("客" if adj < 0 else "平")
            n_rq += 1
            if league not in by_league_rq:
                by_league_rq[league] = [0, 0, 0, 0]
            by_league_rq[league][0] += 1
            # 按赔率最低项作为“预测”
            rw = _float_odds(odds.get("rqspf_win"))
            rd = _float_odds(odds.get("rqspf_draw"))
            rl = _float_odds(odds.get("rqspf_loss"))
            if rw is not None and rd is not None and rl is not None:
                n_rq_has_odds += 1
                if rw <= rd and rw <= rl:
                    pred_rq_odds = "主"
                elif rl <= rd and rl <= rw:
                    pred_rq_odds = "客"
                else:
                    pred_rq_odds = "平"
                if pred_rq_odds == actual_rq:
                    rq_ok_odds += 1
                    by_league_rq[league][2] += 1
                by_league_rq[league][1] += 1
            # 按模型预测比分或期望进球 + 让球数推导让球结果（与前端单场推荐一致）
            if isinstance(pred_score, (list, tuple)) and len(pred_score) >= 2:
                try:
                    ph_s, pa_s = int(pred_score[0]), int(pred_score[1])
                    adj_m = (ph_s - pa_s) + hc
                    pred_rq_model = "主" if adj_m > 0 else ("客" if adj_m < 0 else "平")
                    if pred_rq_model == actual_rq:
                        rq_ok_model += 1
                        by_league_rq[league][3] += 1
                except (TypeError, ValueError):
                    pass
            if pred_rq_model is None:
                exp_h = pred_obj.get("exp_goals_home")
                exp_a = pred_obj.get("exp_goals_away")
                if exp_h is not None and exp_a is not None:
                    try:
                        eh, ea = round(float(exp_h)), round(float(exp_a))
                        adj_m = (eh - ea) + hc
                        pred_rq_model = "主" if adj_m > 0 else ("客" if adj_m < 0 else "平")
                    except (TypeError, ValueError):
                        pass
            jcp_rq = pred_obj.get("jc_play_markets")
            if isinstance(jcp_rq, dict):
                rqb = jcp_rq.get("rqspf")
                if isinstance(rqb, dict) and rqb.get("best_pick") is not None:
                    sp = jc_rqspf_pick_to_side(str(rqb["best_pick"]).strip())
                    sa = jc_rqspf_actual_side(home_s, away_s, hc)
                    if sp is not None:
                        n_rq_jc_rqspf += 1
                        if sp == sa:
                            rq_ok_jc_rqspf += 1

        # 单场推荐命中：按概率权重取最高一项，与前端一致（胜平负概率 + 让球 Poisson 近似概率）
        single_rec_n += 1
        candidates: list[tuple[str, float]] = []
        if ph is not None and pd_ is not None and pa is not None:
            candidates.append(("主胜", ph))
            candidates.append(("平", pd_))
            candidates.append(("客胜", pa))
        if hc is not None and actual_rq is not None:
            exp_h = pred_obj.get("exp_goals_home")
            exp_a = pred_obj.get("exp_goals_away")
            if exp_h is not None and exp_a is not None:
                try:
                    eh, ea = float(exp_h), float(exp_a)
                    p_rq_win, p_rq_draw, p_rq_loss = _rq_probs_from_prediction_doc(pred_obj, eh, ea, hc)
                    candidates.append(("让胜", p_rq_win))
                    candidates.append(("让平", p_rq_draw))
                    candidates.append(("让负", p_rq_loss))
                except (TypeError, ValueError):
                    pass
        if candidates:
            # 基线推荐：仅概率最高一项（用于分段统计与 export-segment-stats）
            base_label, base_prob = max(candidates, key=lambda x: x[1])
            hit_base = False
            if base_label in ("主胜", "平", "客胜"):
                hit_base = actual_outcome == base_label
            else:
                rq_base = "主" if base_label == "让胜" else ("客" if base_label == "让负" else "平")
                hit_base = actual_rq == rq_base
            # 单场推荐：与今日推荐一致，使用 _single_rec_label_and_prob（让球保守 + 接近盘让平 + 细分覆盖）
            _exp_h = pred_obj.get("exp_goals_home")
            _exp_a = pred_obj.get("exp_goals_away")
            try:
                _exp_h = float(_exp_h) if _exp_h is not None else None
                _exp_a = float(_exp_a) if _exp_a is not None else None
            except (TypeError, ValueError):
                _exp_h = _exp_a = None
            if (_exp_h is None or _exp_a is None) and hc is not None:
                pred_score = pred_obj.get("predicted_score")
                if isinstance(pred_score, (list, tuple)) and len(pred_score) >= 2:
                    try:
                        _exp_h = float(pred_score[0])
                        _exp_a = float(pred_score[1])
                    except (TypeError, ValueError):
                        pass
            rec_label, rec_prob = _single_rec_label_and_prob(
                ph or 0, pd_ or 0, pa or 0, _exp_h, _exp_a, hc, league=league, segment_stats=segment_stats
            )
            _map = {"胜": "主胜", "负": "客胜", "平": "平"}
            best_label = _map.get(rec_label, rec_label)  # 让胜/让平/让负 不变
            best_prob = rec_prob
            # 分段统计：按基线推荐统计命中率（供下次 --segment-stats 使用）
            seg_key = f"{league}|{base_label}|{_prob_to_bucket(base_prob)}"
            if seg_key not in segment_counts:
                segment_counts[seg_key] = (0, 0)
            nn, hh = segment_counts[seg_key]
            segment_counts[seg_key] = (nn + 1, hh + (1 if hit_base else 0))
            # 命中以最终推荐为准
            hit = False
            if best_label in ("主胜", "平", "客胜"):
                if actual_outcome == best_label:
                    single_rec_ok += 1
                    hit = True
            else:
                rq_actual = "主" if best_label == "让胜" else ("客" if best_label == "让负" else "平")
                if actual_rq == rq_actual:
                    single_rec_ok += 1
                    hit = True
            if include_hit_matches and hit:
                jc_date = (doc.get("jc_date") or doc.get("match_date") or "")[:10]
                rq_cn = "让胜" if actual_rq == "主" else ("让平" if actual_rq == "平" else "让负") if actual_rq is not None else None
                hit_matches_single_rec.append({
                    "home_cn": pred.get("home_cn") or pred.get("home") or "—",
                    "away_cn": pred.get("away_cn") or pred.get("away") or "—",
                    "league": league,
                    "jc_date": jc_date,
                    "score": f"{home_s}:{away_s}",
                    "handicap": hc,
                    "model_pred": pred_outcome,
                    "recommended": best_label,
                    "rec_prob": round(best_prob * 100),
                    "actual_outcome": actual_outcome,
                    "actual_rq_display": rq_cn,
                })
            single_rec_rows.append((best_prob, hit))
            if league not in by_league_single_rec:
                by_league_single_rec[league] = []
            by_league_single_rec[league].append((best_prob, hit))

            # 未命中复盘：收集推荐错的一场，便于「看看为什么会错」
            if not hit and (_missed_cap == 0 or len(missed_single_rec) < _missed_cap):
                jc_date = (doc.get("jc_date") or doc.get("match_date") or "")[:10]
                actual_display: str
                reason_str: str
                if best_label in ("主胜", "平", "客胜"):
                    actual_display = actual_outcome
                    if actual_outcome == "主胜" and best_label in ("平", "客胜"):
                        reason_str = "主队实际表现强于预期"
                    elif actual_outcome == "客胜" and best_label in ("平", "主胜"):
                        reason_str = "客队实际表现强于预期"
                    elif actual_outcome == "平" and best_label in ("主胜", "客胜"):
                        reason_str = "双方实际更接近，或一方未能扩大优势"
                    else:
                        reason_str = "赛果与推荐方向相反"
                else:
                    actual_rq_cn = "让胜" if actual_rq == "主" else ("让平" if actual_rq == "平" else "让负")
                    actual_display = actual_rq_cn
                    if actual_rq == "主" and best_label in ("让平", "让负"):
                        reason_str = "主队实际表现强于预期（让球）"
                    elif actual_rq == "客" and best_label in ("让平", "让胜"):
                        reason_str = "客队实际表现强于预期（让球）"
                    elif actual_rq == "平" and best_label in ("让胜", "让负"):
                        reason_str = "双方实际更接近（让球）"
                    else:
                        reason_str = "让球赛果与推荐方向相反"
                missed_single_rec.append({
                    "home_cn": pred.get("home_cn") or pred.get("home") or "—",
                    "away_cn": pred.get("away_cn") or pred.get("away") or "—",
                    "league": league,
                    "jc_date": jc_date,
                    "recommended": best_label,
                    "rec_prob": round(best_prob * 100),
                    "actual_score": f"{home_s}:{away_s}",
                    "actual_result": actual_display,
                    "reason": reason_str,
                })

        if len(details_sample) < 15:
            details_sample.append({
                "home": pred.get("home_cn") or pred.get("home"),
                "away": pred.get("away_cn") or pred.get("away"),
                "league": league,
                "actual": f"{home_s}:{away_s}",
                "actual_outcome": actual_outcome,
                "pred_outcome": pred_outcome or "—",
                "hit": pred_outcome == actual_outcome,
            })

    by_league_list = [{"league": k, "n": v[0], "outcome_correct": v[1], "outcome_accuracy": v[1] / v[0] if v[0] else 0} for k, v in sorted(by_league.items(), key=lambda x: -x[1][0])]
    # 按联赛单场推荐：命中率 + 达目标命中率(如70%)所需最低推荐概率
    target_hit = getattr(config, "JC_REC_TARGET_HIT_RATE", 0.70)
    league_min_n = getattr(config, "JC_REC_LEAGUE_MIN_N", 20)
    league_rec_params: dict[str, dict[str, Any]] = {}
    for league, rows in sorted(by_league_single_rec.items(), key=lambda x: -len(x[1])):
        n_rec = len(rows)
        ok_rec = sum(1 for (_, h) in rows if h)
        acc_rec = ok_rec / n_rec if n_rec else 0.0
        min_prob_70 = _min_prob_for_target_hit_rate(rows, target=target_hit, min_n=league_min_n)
        league_rec_params[league] = {
            "n": n_rec,
            "single_rec_ok": ok_rec,
            "single_rec_accuracy": round(acc_rec, 4),
            "min_prob_70": round(min_prob_70, 2) if min_prob_70 is not None else None,
        }
    by_league_rec_list = [
        {"league": k, **v} for k, v in sorted(league_rec_params.items(), key=lambda x: -x[1]["n"])
    ]
    if export_league_rec_params_path:
        try:
            import json
            Path(export_league_rec_params_path).parent.mkdir(parents=True, exist_ok=True)
            meta = {"target_hit_rate": target_hit, "min_n": league_min_n, "generated_from": "jc_predictions_eval_report"}
            Path(export_league_rec_params_path).write_text(
                json.dumps({"meta": meta, "leagues": league_rec_params}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
    # 单场推荐分档：仅统计推荐概率≥X% 的场次命中率（便于提高“只推荐高置信”时的命中率）
    by_single_rec_tier: list[dict[str, Any]] = []
    for th in (0.50, 0.55, 0.60, 0.65, 0.70, 0.72, 0.75):
        subset = [(p, h) for (p, h) in single_rec_rows if p >= th]
        n_t = len(subset)
        if n_t == 0:
            continue
        correct_t = sum(1 for (_, h) in subset if h)
        by_single_rec_tier.append({
            "threshold": th,
            "n": n_t,
            "correct": correct_t,
            "accuracy": correct_t / n_t,
        })
    # 让球胜平负按联赛：n_rq, n_has_odds, rq_ok_odds, rq_ok_model
    by_league_rq_list = [
        {
            "league": k,
            "n_rq": v[0],
            "n_rq_odds": v[1],
            "rq_ok_odds": v[2],
            "rq_accuracy_odds": v[2] / v[1] if v[1] else 0.0,
            "rq_ok_model": v[3],
            "rq_accuracy_model": v[3] / v[0] if v[0] else 0.0,
        }
        for k, v in sorted(by_league_rq.items(), key=lambda x: -x[1][0])
    ]
    total_predictions = len(predictions)
    by_confidence_tier: list[dict[str, Any]] = []
    for th in (0.50, 0.55, 0.60, 0.65, 0.70, 0.72, 0.75):
        subset = [(p, h) for (p, h) in eval_rows if p >= th]
        n_t = len(subset)
        if n_t == 0:
            continue
        correct_t = sum(1 for (_, h) in subset if h)
        by_confidence_tier.append({
            "threshold": th,
            "n": n_t,
            "correct": correct_t,
            "accuracy": correct_t / n_t,
        })
    out = {
        "n": n,
        "by_confidence_tier": by_confidence_tier,
        "total_predictions": total_predictions,
        "outcome_correct": outcome_ok,
        "outcome_accuracy": outcome_ok / n if n else 0.0,
        "exact_score_correct": exact_ok,
        "exact_score_accuracy": exact_ok / n if n else 0.0,
        "n_high_conf": n_high_conf,
        "outcome_correct_high_conf": outcome_ok_high_conf,
        "outcome_accuracy_high_conf": outcome_ok_high_conf / n_high_conf if n_high_conf else 0.0,
        "by_league": by_league_list,
        "by_league_rec": by_league_rec_list,
        "details_sample": details_sample,
        "range_label": range_label,
        "n_rq": n_rq,
        "n_rq_has_odds": n_rq_has_odds,
        "rq_ok_odds": rq_ok_odds,
        "rq_accuracy_odds": rq_ok_odds / n_rq_has_odds if n_rq_has_odds else 0.0,
        "rq_ok_model": rq_ok_model,
        "rq_accuracy_model": rq_ok_model / n_rq if n_rq else 0.0,
        "n_rq_jc_rqspf": n_rq_jc_rqspf,
        "rq_ok_jc_rqspf": rq_ok_jc_rqspf,
        "rq_accuracy_jc_rqspf": rq_ok_jc_rqspf / n_rq_jc_rqspf if n_rq_jc_rqspf else 0.0,
        "ext_zjqs_n": ext_zjqs_n,
        "ext_zjqs_ok": ext_zjqs_ok,
        "ext_zjqs_accuracy": ext_zjqs_ok / ext_zjqs_n if ext_zjqs_n else 0.0,
        "ext_bf_n": ext_bf_n,
        "ext_bf_ok": ext_bf_ok,
        "ext_bf_accuracy": ext_bf_ok / ext_bf_n if ext_bf_n else 0.0,
        "ext_bqc_n": ext_bqc_n,
        "ext_bqc_ok": ext_bqc_ok,
        "ext_bqc_accuracy": ext_bqc_ok / ext_bqc_n if ext_bqc_n else 0.0,
        "by_league_rq": by_league_rq_list,
        "single_rec_ok": single_rec_ok,
        "single_rec_n": single_rec_n,
        "single_rec_accuracy": single_rec_ok / single_rec_n if single_rec_n else 0.0,
        "by_single_rec_tier": by_single_rec_tier,
        "missed_single_rec": missed_single_rec,
    }
    if year is not None:
        out["year"] = year
    if include_hit_matches:
        out["hit_matches_tier60"] = hit_matches_tier60
        out["hit_matches_tier65"] = hit_matches_tier65
        out["hit_matches_tier55"] = hit_matches_tier55
        out["hit_matches_single_rec"] = hit_matches_single_rec
    if n == 0:
        out["diagnostic"] = {
            "predictions_count": len(predictions),
            "pred_with_sporttery_match_id": pred_with_mid,
            "matched_in_analysis": matched_analysis,
            "matched_with_score": matched_with_score,
        }
    # 分段命中率：用于数据驱动自我提升（--export-segment-stats 写出，--segment-stats 读入）
    by_segment = {
        k: {"n": v[0], "hit_rate": v[1] / v[0] if v[0] else 0.0}
        for k, v in segment_counts.items()
    }
    out["by_segment"] = by_segment
    if export_segment_stats_path:
        try:
            import json
            Path(export_segment_stats_path).parent.mkdir(parents=True, exist_ok=True)
            Path(export_segment_stats_path).write_text(
                json.dumps({"segments": by_segment, "prob_buckets": list(_PROB_BUCKET_EDGES)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
    return out
