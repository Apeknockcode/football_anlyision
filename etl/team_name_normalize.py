"""
队名统一：以 DB 中 teams.name（与 matches 一致）为规范名，
将 match_odds、match_injury 中的主客队名规范化为规范名，便于按场次精确匹配。
支持 data/match_odds_to_teams_mapping.json：API-Football 等队名 -> teams.name，用于无法精确匹配时的映射。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import config


def _load_match_odds_to_teams_mapping() -> dict[str, str]:
    """加载 data/match_odds_to_teams_mapping.json（API 队名 -> teams.name），不存在或非 dict 则返回 {}。"""
    path = getattr(config, "DATA_DIR", None) or Path(__file__).resolve().parent.parent / "data"
    if not path:
        return {}
    f = path / "match_odds_to_teams_mapping.json"
    if not f.exists():
        return {}
    try:
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and not k.startswith("_")}
    except Exception:
        return {}


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")


def _exact_match(a: str, b: str) -> bool:
    """精确匹配：strip 后忽略大小写、忽略空格后相等。"""
    na, nb = _norm(a), _norm(b)
    return bool(na and nb and na == nb)


def _resolve_to_canonical(name: str, canonical_list: list[str]) -> str | None:
    """
    在规范名列表中找与 name 精确匹配的项，返回规范名。
    仅当 name 与某条规范名一致（strip、忽略大小写与空格）时返回。
    """
    if not (name or "").strip() or not canonical_list:
        return None
    for c in canonical_list:
        if _exact_match(name, c):
            return c
    return None


def _get_canonical_team_names_by_league_mongo() -> dict[str, list[str]]:
    """MongoDB：从 matches + teams + competitions 得到「联赛中文 -> 该联赛下所有规范队名」。"""
    try:
        from db import repo
        from etl.api_football_odds import DB_COMPETITION_TO_LEAGUE_CN
    except Exception:
        return {}
    if not repo.use_mongo():
        return {}
    db = repo._db()
    comp_list = list(DB_COMPETITION_TO_LEAGUE_CN.keys())
    comps = {c["id"]: c.get("name") or "" for c in db["competitions"].find({"name": {"$in": comp_list}}, {"id": 1, "name": 1})}
    comp_ids = list(comps.keys())
    if not comp_ids:
        return {}
    league_to_names: dict[str, list[str]] = {}
    for m in db["matches"].find({"competition_id": {"$in": comp_ids}}, {"competition_id": 1, "home_team_id": 1, "away_team_id": 1}):
        cname = comps.get(m.get("competition_id")) or ""
        league_cn = DB_COMPETITION_TO_LEAGUE_CN.get(cname)
        if not league_cn:
            continue
        for tid in (m.get("home_team_id"), m.get("away_team_id")):
            if tid is None:
                continue
            t = db["teams"].find_one({"id": tid}, {"name": 1})
            name = (t.get("name") or "").strip() if t else ""
            if not name:
                continue
            if league_cn not in league_to_names:
                league_to_names[league_cn] = []
            if name not in league_to_names[league_cn]:
                league_to_names[league_cn].append(name)
    return league_to_names


def get_canonical_team_names_by_league() -> dict[str, list[str]]:
    """
    从 matches + teams + competitions 得到「联赛中文 -> 该联赛下所有规范队名」。
    规范名 = teams.name（与 matches 中主客队一致）。
    """
    try:
        from db import repo
        if repo.use_mongo():
            return _get_canonical_team_names_by_league_mongo()
    except Exception:
        pass
    try:
        from sqlalchemy import text
        from etl.schema import get_engine, init_db
        from etl.api_football_odds import DB_COMPETITION_TO_LEAGUE_CN
    except Exception:
        return {}
    if not config.DB_PATH.exists():
        return {}
    engine = get_engine()
    init_db(engine)
    comp_names = list(DB_COMPETITION_TO_LEAGUE_CN.keys())
    placeholders = ", ".join(f":c{i}" for i in range(len(comp_names)))
    params = {f"c{i}": comp_names[i] for i in range(len(comp_names))}
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT DISTINCT c.name AS comp_name, t.name AS team_name
                FROM matches m
                JOIN teams t ON (m.home_team_id = t.id OR m.away_team_id = t.id)
                JOIN competitions c ON m.competition_id = c.id
                WHERE c.name IN ({placeholders})
            """),
            params,
        ).fetchall()
    league_to_names: dict[str, list[str]] = {}
    for comp_name, team_name in rows:
        league_cn = DB_COMPETITION_TO_LEAGUE_CN.get(comp_name)
        if not league_cn or not (team_name or "").strip():
            continue
        if league_cn not in league_to_names:
            league_to_names[league_cn] = []
        t = (team_name or "").strip()
        if t and t not in league_to_names[league_cn]:
            league_to_names[league_cn].append(t)
    return league_to_names


def _normalize_match_odds_mongo(dry_run: bool) -> tuple[int, str]:
    """MongoDB：将 match_odds 中 home_team、away_team 规范化为 teams 表队名。"""
    try:
        from db import repo
    except Exception:
        return 0, "依赖未就绪"
    if not repo.use_mongo():
        return 0, "未使用 MongoDB"
    by_league = get_canonical_team_names_by_league()
    if not by_league:
        return 0, "无五大联赛规范队名（请先 ingest 入库比赛）"
    api_to_teams = _load_match_odds_to_teams_mapping()
    coll = repo._db()["match_odds"]
    updated = 0
    for doc in coll.find({}, {"_id": 1, "log_date": 1, "home_team": 1, "away_team": 1, "league_cn": 1}):
        log_date = (doc.get("log_date") or "")[:10]
        home = (doc.get("home_team") or "").strip()
        away = (doc.get("away_team") or "").strip()
        league_cn = (doc.get("league_cn") or "").strip()
        canonical_list = by_league.get(league_cn)
        if not canonical_list:
            continue
        ch = _resolve_to_canonical(home or "", canonical_list)
        if not ch and home and api_to_teams.get(home) in canonical_list:
            ch = api_to_teams[home]
        ca = _resolve_to_canonical(away or "", canonical_list)
        if not ca and away and api_to_teams.get(away) in canonical_list:
            ca = api_to_teams[away]
        if (ch and ch != home) or (ca and ca != away):
            new_home = ch or home or "?"
            new_away = ca or away or "?"
            # 若目标 (log_date, new_home, new_away, league_cn) 已被其他文档占用则跳过
            other = coll.find_one({
                "log_date": log_date, "league_cn": league_cn,
                "home_team": new_home, "away_team": new_away,
                "_id": {"$ne": doc["_id"]},
            })
            if other:
                continue
            if not dry_run:
                coll.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"home_team": new_home, "away_team": new_away}},
                )
            updated += 1
    msg = f"match_odds: 已规范化 {updated} 条主客队名（与 teams 一致）" if not dry_run else f"match_odds: 拟规范化 {updated} 条（dry-run）"
    return updated, msg


def normalize_match_odds(dry_run: bool = False) -> tuple[int, str]:
    """
    将 match_odds 中 home_team、away_team 规范化为 teams 表中的队名（同联赛下精确匹配）。
    返回 (更新行数, 说明)。
    """
    try:
        from db import repo
        if repo.use_mongo():
            return _normalize_match_odds_mongo(dry_run)
    except Exception:
        pass
    try:
        from sqlalchemy import text
        from etl.schema import get_engine, init_db
    except Exception:
        return 0, "依赖未就绪"
    if not config.DB_PATH.exists():
        return 0, "DB 不存在"
    by_league = get_canonical_team_names_by_league()
    if not by_league:
        return 0, "无五大联赛规范队名（请先 ingest-fd 入库比赛）"
    api_to_teams = _load_match_odds_to_teams_mapping()
    engine = get_engine()
    init_db(engine)
    updated = 0
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, log_date, home_team, away_team, league_cn FROM match_odds"),
        ).fetchall()
        for row in rows:
            oid, log_date, home, away, league_cn = row[0], row[1], row[2], row[3], (row[4] or "").strip()
            canonical_list = by_league.get(league_cn)
            if not canonical_list:
                continue
            home_s = (home or "").strip()
            away_s = (away or "").strip()
            ch = _resolve_to_canonical(home_s, canonical_list)
            if not ch and home_s and api_to_teams.get(home_s) in canonical_list:
                ch = api_to_teams[home_s]
            ca = _resolve_to_canonical(away_s, canonical_list)
            if not ca and away_s and api_to_teams.get(away_s) in canonical_list:
                ca = api_to_teams[away_s]
            if (ch and ch != home_s) or (ca and ca != away_s):
                new_home = ch or home_s or "?"
                new_away = ca or away_s or "?"
                # 表有 UNIQUE(log_date, home_team, away_team, league_cn)，若目标组合已被其他行占用则跳过
                exists = conn.execute(
                    text("""
                        SELECT 1 FROM match_odds
                        WHERE log_date = :log_date AND league_cn = :league_cn
                          AND home_team = :h AND away_team = :a AND id != :id
                        LIMIT 1
                    """),
                    {"log_date": log_date, "league_cn": league_cn, "h": new_home or "?", "a": new_away or "?", "id": oid},
                ).fetchone()
                if exists:
                    continue
                if not dry_run:
                    conn.execute(
                        text("UPDATE match_odds SET home_team = :h, away_team = :a WHERE id = :id"),
                        {"h": new_home or "?", "a": new_away or "?", "id": oid},
                    )
                updated += 1
        if not dry_run and updated:
            conn.commit()
    msg = f"match_odds: 已规范化 {updated} 条主客队名（与 teams 一致）" if not dry_run else f"match_odds: 拟规范化 {updated} 条（dry-run）"
    return updated, msg


def normalize_match_injury(dry_run: bool = False) -> tuple[int, str]:
    """
    将 match_injury 中 home_team、away_team 规范化为 teams 表中的队名。
    返回 (更新行数, 说明)。
    """
    try:
        from sqlalchemy import text
        from etl.schema import get_engine, init_db
    except Exception:
        return 0, "依赖未就绪"
    if not config.DB_PATH.exists():
        return 0, "DB 不存在"
    by_league = get_canonical_team_names_by_league()
    if not by_league:
        return 0, "无五大联赛规范队名（请先 ingest-fd 入库比赛）"
    engine = get_engine()
    init_db(engine)
    updated = 0
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, log_date, home_team, away_team, league_cn FROM match_injury"),
        ).fetchall()
        for row in rows:
            oid, log_date, home, away, league_cn = row[0], row[1], row[2], row[3], (row[4] or "").strip()
            canonical_list = by_league.get(league_cn)
            if not canonical_list:
                continue
            ch = _resolve_to_canonical(home or "", canonical_list)
            ca = _resolve_to_canonical(away or "", canonical_list)
            if (ch and ch != (home or "").strip()) or (ca and ca != (away or "").strip()):
                new_home = ch or (home or "").strip()
                new_away = ca or (away or "").strip()
                if not dry_run:
                    conn.execute(
                        text("UPDATE match_injury SET home_team = :h, away_team = :a WHERE id = :id"),
                        {"h": new_home or "?", "a": new_away or "?", "id": oid},
                    )
                updated += 1
        if not dry_run and updated:
            conn.commit()
    msg = f"match_injury: 已规范化 {updated} 条主客队名（与 teams 一致）" if not dry_run else f"match_injury: 拟规范化 {updated} 条（dry-run）"
    return updated, msg


def normalize_all(dry_run: bool = False) -> tuple[int, int, str]:
    """先 match_odds 再 match_injury，返回 (odds_updated, injury_updated, 说明)。"""
    n_odds, msg_odds = normalize_match_odds(dry_run=dry_run)
    n_injury, msg_injury = normalize_match_injury(dry_run=dry_run)
    return n_odds, n_injury, msg_odds + "；" + msg_injury
