"""
统一数据访问层：USE_MONGO 时从 MongoDB 读写，否则从 SQLite 读写。
业务代码统一通过本模块的 get_* / save_* 等函数访问数据，不直接使用 Session 或 conn.execute。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import config


def use_mongo() -> bool:
    return getattr(config, "USE_MONGO", False)


def get_engine():
    """仅当未使用 MongoDB 时返回 SQLAlchemy engine（供仍依赖 engine 的兼容代码使用）。"""
    if use_mongo():
        return None
    from etl.schema import get_engine
    return get_engine()


def init_db(engine=None):
    """初始化 DB：MongoDB 无需建表；SQLite 时创建表。"""
    if use_mongo():
        return
    from etl.schema import get_engine, init_db
    init_db(engine or get_engine())


# ---------- MongoDB 实现 ----------

def _db():
    from db.mongo import get_mongo_db
    db = get_mongo_db()
    if db is None:
        raise RuntimeError("USE_MONGO 已开启但未配置 MONGO_URI")
    return db


def _matches_coll():
    """MongoDB 比赛集合（可由 MONGO_MATCHES_COLLECTION 指定为 matchs 等）。"""
    return _db()[getattr(config, "MONGO_MATCHES_COLLECTION", "matches")]


def _next_id(coll) -> int:
    """返回集合中 max(id)+1，空集为 1。"""
    doc = coll.find_one(sort=[("id", -1)], projection=["id"])
    return (doc["id"] + 1) if doc and doc.get("id") is not None else 1


def _doc_to_match_row(d: dict) -> dict:
    if not d:
        return None
    return {k: d[k] for k in ("id", "competition_id", "season_id", "match_date", "home_team_id", "away_team_id",
                               "home_score", "away_score", "match_week", "source_id", "raw_meta", "home_rank", "away_rank") if k in d}


# ----- Teams -----
def _team_row_to_dict(r: Any) -> dict:
    """把 Team ORM 行转为 {id, name, name_zh}。"""
    return {"id": r.id, "name": r.name, "name_zh": getattr(r, "name_zh", None)}


def get_team_by_id(tid: int) -> dict | None:
    """按 id 取队伍，返回 {id, name, name_zh} 或 None。"""
    if use_mongo():
        return _db()["teams"].find_one({"id": tid})
    from etl.schema import get_engine, Team
    from sqlalchemy.orm import Session
    with Session(get_engine()) as s:
        r = s.query(Team).filter(Team.id == tid).first()
        return _team_row_to_dict(r) if r else None


def get_team_id_by_name(name: str) -> int | None:
    """按名称解析球队 ID：精确匹配 teams.name 或 teams.name_zh（支持中文队名）。"""
    t = get_team_by_name(name) or get_team_by_name_zh(name)
    return t["id"] if t else None


def get_team_by_name(name: str) -> dict | None:
    """精确匹配 teams.name，返回 {id, name, name_zh} 或 None。"""
    s = (name or "").strip()
    if not s:
        return None
    if use_mongo():
        doc = _db()["teams"].find_one({"name": s})
        return doc
    from etl.schema import get_engine, Team
    from sqlalchemy.orm import Session
    with Session(get_engine()) as sess:
        row = sess.query(Team).filter(Team.name == s).first()
        return _team_row_to_dict(row) if row else None


def get_team_by_name_zh(name_zh: str) -> dict | None:
    """精确匹配 teams.name_zh，返回 {id, name, name_zh} 或 None。"""
    s = (name_zh or "").strip()
    if not s:
        return None
    if use_mongo():
        return _db()["teams"].find_one({"name_zh": s})
    from etl.schema import get_engine, Team
    from sqlalchemy.orm import Session
    with Session(get_engine()) as sess:
        row = sess.query(Team).filter(Team.name_zh == s).first()
        return _team_row_to_dict(row) if row else None


def ensure_team_by_id(team_id: int, name: str, name_zh: str | None = None) -> int:
    """按指定 id 确保队伍存在；不存在则插入。用于 API-Football 等固定 id 体系。返回 team_id。"""
    s = (name or "").strip() or f"team_{team_id}"
    if use_mongo():
        db = _db()
        c = db["teams"]
        t = c.find_one({"id": team_id})
        if t:
            return team_id
        doc: dict = {"id": team_id, "name": s}
        if (name_zh or "").strip():
            doc["name_zh"] = (name_zh or "").strip()
        c.insert_one(doc)
        return team_id
    from etl.schema import get_engine, Team
    from sqlalchemy.orm import Session
    engine = get_engine()
    with Session(engine) as sess:
        row = sess.query(Team).filter(Team.id == team_id).first()
        if row:
            return team_id
        sess.add(Team(id=team_id, name=s, name_zh=(name_zh or "").strip() or None))
        sess.commit()
        return team_id


def ensure_team_by_name(name: str, name_zh: str | None = None) -> int:
    """存在则返回 id，不存在则插入并返回新 id。创建时可传入 name_zh。"""
    s = (name or "").strip()
    if use_mongo():
        db = _db()
        c = db["teams"]
        t = c.find_one({"name": s})
        if t:
            return t["id"]
        nid = _next_id(c)
        doc: dict = {"id": nid, "name": s}
        if (name_zh or "").strip():
            doc["name_zh"] = (name_zh or "").strip()
        c.insert_one(doc)
        return nid
    from etl.schema import get_engine, Team
    from sqlalchemy.orm import Session
    engine = get_engine()
    with Session(engine) as sess:
        row = sess.query(Team).filter(Team.name == s).first()
        if row:
            return row.id
        mx = sess.query(Team).order_by(Team.id.desc()).first()
        next_id = (mx.id + 1) if mx else 1
        team = Team(id=next_id, name=s)
        if (name_zh or "").strip():
            team.name_zh = (name_zh or "").strip()
        sess.add(team)
        sess.commit()
        return next_id


def update_team_name_zh(team_id: int, name_zh: str | None) -> bool:
    """更新某队的 name_zh；传 None 或空串表示清空。返回是否找到并更新。"""
    val = (name_zh or "").strip() or None
    if use_mongo():
        r = _db()["teams"].update_one({"id": team_id}, {"$set": {"name_zh": val}})
        return r.matched_count > 0
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        r = conn.execute(text("UPDATE teams SET name_zh = :v WHERE id = :id"), {"v": val, "id": team_id})
        conn.commit()
        return r.rowcount > 0


def list_all_teams() -> list[dict]:
    """列出所有球队，每项 {id, name, name_zh}。用于批量更新 name_zh。"""
    if use_mongo():
        return list(_db()["teams"].find({}, {"id": 1, "name": 1, "name_zh": 1}))
    from etl.schema import get_engine, Team
    from sqlalchemy.orm import Session
    with Session(get_engine()) as s:
        rows = s.query(Team).all()
        return [_team_row_to_dict(r) for r in rows]


# ----- Competitions -----
def get_competition_id_by_name(name: str) -> int | None:
    """按名称解析赛事 ID：仅精确匹配 competitions.name。"""
    s = (name or "").strip()
    if not s:
        return None
    if use_mongo():
        c = _db()["competitions"].find_one({"name": s})
        return c["id"] if c else None
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT id FROM competitions WHERE name = :n"), {"n": s}).fetchone()
        return row[0] if row else None


def list_competitions() -> list[dict]:
    """列出所有赛事，每项 {id, name, country_name}。"""
    if use_mongo():
        return list(_db()["competitions"].find({}, {"id": 1, "name": 1, "country_name": 1}))
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        rows = conn.execute(text("SELECT id, name, country_name FROM competitions")).fetchall()
    return [{"id": r[0], "name": r[1], "country_name": r[2]} for r in rows]


def get_competition_by_id(cid: int) -> dict | None:
    if use_mongo():
        return _db()["competitions"].find_one({"id": cid})
    from etl.schema import get_engine, Competition
    from sqlalchemy.orm import Session
    with Session(get_engine()) as s:
        r = s.query(Competition).filter(Competition.id == cid).first()
        return {"id": r.id, "name": r.name, "country_name": getattr(r, "country_name", None)} if r else None


def ensure_competition(cid: int, name: str, country_name: str = None) -> None:
    if use_mongo():
        c = _db()["competitions"]
        if c.find_one({"id": cid}) is None:
            c.insert_one({"id": cid, "name": name, "country_name": country_name})
        return
    from etl.schema import get_engine, Competition
    from sqlalchemy.orm import Session
    with Session(get_engine()) as s:
        if s.query(Competition).filter(Competition.id == cid).first() is None:
            s.add(Competition(id=cid, name=name, country_name=country_name))
        s.commit()


def ensure_competition_by_name(name: str, country_name: str | None = None) -> int:
    """按名称确保赛事存在，不存在则插入并分配新 id。返回 competition id。用于竞彩预测时合并主库（日职、韩职等）。"""
    s = (name or "").strip()
    if not s:
        return None
    existing = get_competition_id_by_name(s)
    if existing is not None:
        return existing
    if use_mongo():
        c = _db()["competitions"]
        nid = _next_id(c)
        c.insert_one({"id": nid, "name": s, "country_name": country_name})
        return nid
    from etl.schema import get_engine, Competition
    from sqlalchemy.orm import Session
    engine = get_engine()
    with Session(engine) as sess:
        mx = sess.query(Competition).order_by(Competition.id.desc()).first()
        nid = (mx.id + 1) if mx else 1
        sess.add(Competition(id=nid, name=s, country_name=country_name))
        sess.commit()
        return nid


def update_competition_country(cid: int, country_name: str | None) -> bool:
    """更新指定赛事的 country_name。返回是否找到并更新。"""
    val = (country_name or "").strip() or None
    if use_mongo():
        r = _db()["competitions"].update_one({"id": cid}, {"$set": {"country_name": val}})
        return r.matched_count > 0
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        r = conn.execute(text("UPDATE competitions SET country_name = :v WHERE id = :id"), {"v": val, "id": cid})
        conn.commit()
        return r.rowcount > 0


def ensure_season(sid: int, competition_id: int, name: str) -> None:
    if use_mongo():
        c = _db()["seasons"]
        if c.find_one({"id": sid}) is None:
            c.insert_one({"id": sid, "competition_id": competition_id, "name": name})
        return
    from etl.schema import get_engine, Season
    from sqlalchemy.orm import Session
    with Session(get_engine()) as s:
        if s.query(Season).filter(Season.id == sid).first() is None:
            s.add(Season(id=sid, competition_id=competition_id, name=name))
        s.commit()


# ----- Matches -----
def get_match_by_id(mid: int) -> dict | None:
    if use_mongo():
        return _matches_coll().find_one({"id": mid})
    from etl.schema import get_engine, Match
    from sqlalchemy.orm import Session
    with Session(get_engine()) as s:
        r = s.query(Match).filter(Match.id == mid).first()
        return _orm_match_to_dict(r) if r else None


def get_match_by_source_id(source_id: str) -> dict | None:
    if use_mongo():
        return _matches_coll().find_one({"source_id": source_id})
    from etl.schema import get_engine, Match
    from sqlalchemy.orm import Session
    with Session(get_engine()) as s:
        r = s.query(Match).filter(Match.source_id == source_id).first()
        return _orm_match_to_dict(r) if r else None


def _orm_match_to_dict(r) -> dict:
    return {
        "id": r.id, "competition_id": r.competition_id, "season_id": r.season_id, "match_date": r.match_date,
        "home_team_id": r.home_team_id, "away_team_id": r.away_team_id, "home_score": r.home_score, "away_score": r.away_score,
        "match_week": r.match_week, "source_id": r.source_id, "raw_meta": r.raw_meta, "home_rank": r.home_rank, "away_rank": r.away_rank,
    }


def get_db_matches_for_date(date_str: str) -> list[dict]:
    """指定日期的五大联赛场次：home_name, away_name, league_cn, date。"""
    date_only = (date_str or "")[:10]
    if len(date_only) != 10:
        return []
    comp_list = ("Premier League", "La Liga", "Primera Division", "Primera División", "Serie A", "Bundesliga", "1. Bundesliga", "Ligue 1")
    league_map = {"Premier League": "英超", "La Liga": "西甲", "Primera Division": "西甲", "Primera División": "西甲",
                  "Serie A": "意甲", "Bundesliga": "德甲", "1. Bundesliga": "德甲", "Ligue 1": "法甲"}
    if use_mongo():
        db = _db()
        comp_ids = [c["id"] for c in db["competitions"].find({"name": {"$in": list(comp_list)}}, {"id": 1})]
        matches = _matches_coll().find({
            "match_date": {"$regex": f"^{date_only}"},
            "competition_id": {"$in": comp_ids},
        })
        teams = {t["id"]: {"name": t.get("name") or "", "name_zh": t.get("name_zh")} for t in db["teams"].find({}, {"id": 1, "name": 1, "name_zh": 1})}
        comps = {c["id"]: c["name"] for c in db["competitions"].find({}, {"id": 1, "name": 1})}
        out = []
        for m in matches:
            cname = comps.get(m.get("competition_id")) or ""
            league_cn = league_map.get(cname, "")
            if not league_cn:
                continue
            ht, at = teams.get(m.get("home_team_id")) or {}, teams.get(m.get("away_team_id")) or {}
            out.append({
                "home_name": (ht.get("name") or "").strip(),
                "away_name": (at.get("name") or "").strip(),
                "home_name_zh": (ht.get("name_zh") or "").strip(),
                "away_name_zh": (at.get("name_zh") or "").strip(),
                "league_cn": league_cn,
                "date": date_only,
            })
        return out
    from sqlalchemy import text
    from etl.schema import get_engine
    q = """
        SELECT ht.name, at.name, c.name, ht.name_zh, at.name_zh FROM matches m
        JOIN teams ht ON m.home_team_id = ht.id JOIN teams at ON m.away_team_id = at.id
        JOIN competitions c ON m.competition_id = c.id
        WHERE date(m.match_date) = :d AND trim(coalesce(m.match_date, '')) != ''
        AND c.name IN ('Premier League','La Liga','Primera Division','Primera División','Serie A','Bundesliga','1. Bundesliga','Ligue 1')
    """
    engine = get_engine()
    out = []
    with engine.connect() as conn:
        for r in conn.execute(text(q), {"d": date_only}).fetchall():
            cname = (r[2] or "").strip()
            league_cn = league_map.get(cname)
            if league_cn:
                out.append({
                    "home_name": (r[0] or "").strip(), "away_name": (r[1] or "").strip(),
                    "home_name_zh": (r[3] or "").strip() if len(r) > 4 else "",
                    "away_name_zh": (r[4] or "").strip() if len(r) > 4 else "",
                    "league_cn": league_cn, "date": date_only,
                })
    return out


def get_distinct_match_dates(
    years: list[int] | None = None,
    limit: int | None = None,
    big5_only: bool = True,
) -> list[str]:
    """
    从 DB matches 取不重复的比赛日（YYYY-MM-DD），按日期升序。
    years: 只保留这些年份，如 [2024]；None 表示不过滤。
    limit: 只返回最近 limit 个日期；None 表示全部。
    big5_only: 为 True 时只统计五大联赛场次对应的日期（与 get_db_matches_for_date 一致）。
    """
    comp_list = ("Premier League", "La Liga", "Primera Division", "Primera División", "Serie A", "Bundesliga", "1. Bundesliga", "Ligue 1")
    if use_mongo():
        db = _db()
        q = {"match_date": {"$regex": r"^\d{4}-\d{2}-\d{2}"}}
        if years:
            or_dates = [{"match_date": {"$regex": f"^{y}-"}} for y in years]
            q["$or"] = or_dates
        if big5_only:
            comp_ids = [c["id"] for c in db["competitions"].find({"name": {"$in": list(comp_list)}}, {"id": 1})]
            q["competition_id"] = {"$in": comp_ids}
        dates = set()
        for m in _matches_coll().find(q, {"match_date": 1}):
            d = (m.get("match_date") or "")[:10]
            if len(d) == 10 and d[4] == "-" and d[7] == "-":
                dates.add(d)
        out = sorted(dates)
    else:
        from sqlalchemy import text
        from etl.schema import get_engine
        engine = get_engine()
        q = """
            SELECT DISTINCT date(match_date) AS d FROM matches
            WHERE match_date IS NOT NULL AND trim(match_date) != ''
        """
        params = {}
        if big5_only:
            q += """ AND competition_id IN (SELECT id FROM competitions WHERE name IN ('Premier League','La Liga','Primera Division','Primera División','Serie A','Bundesliga','1. Bundesliga','Ligue 1'))"""
        if years:
            placeholders = ",".join(str(y) for y in years)
            q += f" AND strftime('%Y', match_date) IN ({placeholders})"
        q += " ORDER BY d"
        with engine.connect() as conn:
            rows = conn.execute(text(q), params).fetchall()
        out = [str(r[0])[:10] for r in rows if r[0] and len(str(r[0])[:10]) == 10]
    if years and not use_mongo():
        year_set = set(years)
        out = [d for d in out if int(d[:4]) in year_set]
    if limit is not None and limit > 0:
        out = out[-limit:]
    return out


def count_h2h_matches(home_id: int, away_id: int, competition_id: int) -> int:
    """该主客对+联赛在 matches 中已存在的场次数（用于 h2h-backfill 判断是否可跳过请求）。"""
    if use_mongo():
        return _matches_coll().count_documents({
            "home_team_id": home_id,
            "away_team_id": away_id,
            "competition_id": competition_id,
        })
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        r = conn.execute(
            text(
                "SELECT COUNT(*) FROM matches WHERE home_team_id = :hid AND away_team_id = :aid AND competition_id = :cid"
            ),
            {"hid": home_id, "aid": away_id, "cid": competition_id},
        )
        return r.scalar() or 0


def get_distinct_h2h_pairs(
    years: list[int] | None = None,
    limit: int | None = None,
    big5_only: bool = True,
) -> list[tuple[int, int, int]]:
    """
    从 matches 取不重复的 (home_team_id, away_team_id, competition_id) 组合，用于按对拉取历史交锋。
    years: 只保留这些年份；None 表示不过滤。
    limit: 最多返回 limit 组；None 表示全部。
    big5_only: 只统计五大联赛。
    返回 [(home_id, away_id, comp_id), ...]。
    """
    comp_list = ("Premier League", "La Liga", "Primera Division", "Primera División", "Serie A", "Bundesliga", "1. Bundesliga", "Ligue 1")
    if use_mongo():
        db = _db()
        q = {"home_team_id": {"$ne": None}, "away_team_id": {"$ne": None}, "competition_id": {"$ne": None}}
        if years:
            q["$or"] = [{"match_date": {"$regex": f"^{y}-"}} for y in years]
        if big5_only:
            comp_ids = [c["id"] for c in db["competitions"].find({"name": {"$in": list(comp_list)}}, {"id": 1})]
            q["competition_id"] = {"$in": comp_ids}
        seen: set[tuple[int, int, int]] = set()
        out: list[tuple[int, int, int]] = []
        for m in _matches_coll().find(q, {"home_team_id": 1, "away_team_id": 1, "competition_id": 1}):
            hid, aid, cid = m.get("home_team_id"), m.get("away_team_id"), m.get("competition_id")
            if hid is None or aid is None or cid is None:
                continue
            key = (int(hid), int(aid), int(cid))
            if key not in seen:
                seen.add(key)
                out.append(key)
            if limit is not None and len(out) >= limit:
                break
        return out
    from sqlalchemy import text
    from etl.schema import get_engine
    engine = get_engine()
    q = """
        SELECT DISTINCT home_team_id, away_team_id, competition_id FROM matches
        WHERE home_team_id IS NOT NULL AND away_team_id IS NOT NULL AND competition_id IS NOT NULL
    """
    params = {}
    if big5_only:
        q += """ AND competition_id IN (SELECT id FROM competitions WHERE name IN ('Premier League','La Liga','Primera Division','Primera División','Serie A','Bundesliga','1. Bundesliga','Ligue 1'))"""
    if years:
        placeholders = ",".join(str(y) for y in years)
        q += f" AND strftime('%Y', match_date) IN ({placeholders})"
    if limit is not None and limit > 0:
        q += f" LIMIT {int(limit) * 3}"
    with engine.connect() as conn:
        rows = conn.execute(text(q), params).fetchall()
    seen = set()
    out = []
    for r in rows:
        if r[0] is None or r[1] is None or r[2] is None:
            continue
        key = (int(r[0]), int(r[1]), int(r[2]))
        if key not in seen:
            seen.add(key)
            out.append(key)
        if limit is not None and len(out) >= limit:
            break
    return out


def _half_time_scores_from_match_doc(m: dict[str, Any]) -> tuple[int | None, int | None]:
    """从 matches 文档的 raw_meta（JSON 字符串或 dict）解析 half_time_home / half_time_away。"""
    rm = m.get("raw_meta")
    meta: dict[str, Any] | None = None
    if isinstance(rm, str) and rm.strip():
        try:
            meta = json.loads(rm)
        except Exception:
            return None, None
    elif isinstance(rm, dict):
        meta = rm
    if not meta:
        return None, None
    hh, ha = meta.get("half_time_home"), meta.get("half_time_away")
    if hh is None and ha is None:
        return None, None
    try:
        hi = int(hh) if hh is not None else None
        ai = int(ha) if ha is not None else None
    except (TypeError, ValueError):
        return None, None
    if hi is not None and ai is not None:
        return hi, ai
    return None, None


def get_db_matches_for_date_with_scores(date_str: str) -> list[dict]:
    """指定日期的五大联赛场次，含 match_id、比分（用于从 DB 回填 prediction_log，不依赖体彩）。"""
    date_only = (date_str or "")[:10]
    if len(date_only) != 10:
        return []
    comp_list = ("Premier League", "La Liga", "Primera Division", "Primera División", "Serie A", "Bundesliga", "1. Bundesliga", "Ligue 1")
    league_map = {"Premier League": "英超", "La Liga": "西甲", "Primera Division": "西甲", "Primera División": "西甲",
                  "Serie A": "意甲", "Bundesliga": "德甲", "1. Bundesliga": "德甲", "Ligue 1": "法甲"}
    if use_mongo():
        db = _db()
        comp_ids = [c["id"] for c in db["competitions"].find({"name": {"$in": list(comp_list)}}, {"id": 1})]
        matches = list(_matches_coll().find({
            "match_date": {"$regex": f"^{date_only}"},
            "competition_id": {"$in": comp_ids},
        }))
        teams = {t["id"]: {"name": t.get("name") or "", "name_zh": t.get("name_zh")} for t in db["teams"].find({}, {"id": 1, "name": 1, "name_zh": 1})}
        comps = {c["id"]: c["name"] for c in db["competitions"].find({}, {"id": 1, "name": 1})}
        out = []
        for m in matches:
            cname = comps.get(m.get("competition_id")) or ""
            league_cn = league_map.get(cname, "")
            if not league_cn:
                continue
            ht, at = teams.get(m.get("home_team_id")) or {}, teams.get(m.get("away_team_id")) or {}
            home_name = ((m.get("home_team_name") or "").strip() or (ht.get("name") or "").strip())
            away_name = ((m.get("away_team_name") or "").strip() or (at.get("name") or "").strip())
            home_name_zh = ((m.get("home_team_name_zh") or "").strip() or (ht.get("name_zh") or "").strip())
            away_name_zh = ((m.get("away_team_name_zh") or "").strip() or (at.get("name_zh") or "").strip())
            hth, hta = _half_time_scores_from_match_doc(m)
            out.append({
                "match_id": m.get("id"),
                "source_id": m.get("source_id"),
                "home_name": home_name,
                "away_name": away_name,
                "home_name_zh": home_name_zh,
                "away_name_zh": away_name_zh,
                "league_cn": league_cn,
                "date": date_only,
                "home_score": m.get("home_score"),
                "away_score": m.get("away_score"),
                "half_home": hth,
                "half_away": hta,
            })
        return out
    from sqlalchemy import text
    from etl.schema import get_engine
    q = """
        SELECT m.id, ht.name, at.name, c.name, ht.name_zh, at.name_zh, m.home_score, m.away_score, m.raw_meta
        FROM matches m
        JOIN teams ht ON m.home_team_id = ht.id JOIN teams at ON m.away_team_id = at.id
        JOIN competitions c ON m.competition_id = c.id
        WHERE date(m.match_date) = :d AND trim(coalesce(m.match_date, '')) != ''
        AND c.name IN ('Premier League','La Liga','Primera Division','Primera División','Serie A','Bundesliga','1. Bundesliga','Ligue 1')
    """
    engine = get_engine()
    out = []
    with engine.connect() as conn:
        for r in conn.execute(text(q), {"d": date_only}).fetchall():
            cname = (r[3] or "").strip()
            league_cn = league_map.get(cname)
            if league_cn:
                raw_m = r[8] if len(r) > 8 else None
                hth, hta = _half_time_scores_from_match_doc({"raw_meta": raw_m})
                out.append({
                    "match_id": r[0],
                    "home_name": (r[1] or "").strip(), "away_name": (r[2] or "").strip(),
                    "home_name_zh": (r[4] or "").strip() if len(r) > 5 else "",
                    "away_name_zh": (r[5] or "").strip() if len(r) > 5 else "",
                    "league_cn": league_cn, "date": date_only,
                    "home_score": r[6] if len(r) > 7 else None, "away_score": r[7] if len(r) > 7 else None,
                    "half_home": hth,
                    "half_away": hta,
                })
    return out


def insert_match(match_dict: dict) -> int:
    """插入一场比赛，返回 match id。"""
    if use_mongo():
        db = _db()
        coll = _matches_coll()
        mid = match_dict.get("id")
        if mid is not None and coll.find_one({"id": mid}):
            return mid
        if mid is None:
            mid = _next_id(coll)
            match_dict = dict(match_dict)
            match_dict["id"] = mid
        # Mongo matches 额外写入主客队英文/中文名，便于前端直接展示与检索
        try:
            home_id = match_dict.get("home_team_id")
            away_id = match_dict.get("away_team_id")
            teams = db["teams"]
            ht = teams.find_one({"id": home_id}, {"name": 1, "name_zh": 1}) if home_id is not None else None
            at = teams.find_one({"id": away_id}, {"name": 1, "name_zh": 1}) if away_id is not None else None
            if ht:
                match_dict["home_team_name"] = (ht.get("name") or "").strip()
                match_dict["home_team_name_zh"] = (ht.get("name_zh") or "").strip()
            if at:
                match_dict["away_team_name"] = (at.get("name") or "").strip()
                match_dict["away_team_name_zh"] = (at.get("name_zh") or "").strip()
        except Exception:
            pass
        coll.insert_one(match_dict)
        return mid
    from etl.schema import get_engine, Match
    from sqlalchemy.orm import Session
    with Session(get_engine()) as s:
        m = Match(**{k: v for k, v in match_dict.items() if k in ("competition_id", "season_id", "match_date", "home_team_id", "away_team_id", "home_score", "away_score", "match_week", "source_id", "raw_meta", "home_rank", "away_rank")})
        s.add(m)
        s.commit()
        s.refresh(m)
        return m.id


def update_match(match_id: int, **fields) -> None:
    allowed = (
        "home_score", "away_score", "raw_meta", "home_rank", "away_rank", "match_date",
        "competition_id", "season_id", "home_team_id", "away_team_id", "match_week",
    )
    if use_mongo():
        db = _matches_coll()
        upd = {k: v for k, v in fields.items() if k in allowed}
        if upd:
            db.update_one({"id": match_id}, {"$set": upd})
        return
    from etl.schema import get_engine, Match
    from sqlalchemy.orm import Session
    upd = {k: v for k, v in fields.items() if k in allowed}
    if upd:
        with Session(get_engine()) as s:
            s.query(Match).filter(Match.id == match_id).update(upd)
            s.commit()


def backfill_matches_team_name_zh_mongo(only_if_empty: bool = True) -> tuple[int, int]:
    """
    仅 Mongo：用 teams 表的 name/name_zh 回填 matches 文档的 home_team_name、away_team_name、
    home_team_name_zh、away_team_name_zh。only_if_empty=True 时只补当前为空的字段。
    返回 (更新文档数, 跳过文档数)。
    """
    if not use_mongo():
        return 0, 0
    db = _db()
    teams_coll = db["teams"]
    matches_coll = _matches_coll()
    team_by_id: dict[int, dict[str, str]] = {}
    for t in teams_coll.find({}, {"id": 1, "name": 1, "name_zh": 1}):
        tid = t.get("id")
        if tid is None:
            continue
        team_by_id[int(tid)] = {
            "name": (t.get("name") or "").strip(),
            "name_zh": (t.get("name_zh") or "").strip(),
        }
    updated = 0
    skipped = 0
    for m in matches_coll.find({}, {"id": 1, "home_team_id": 1, "away_team_id": 1, "home_team_name": 1, "away_team_name": 1, "home_team_name_zh": 1, "away_team_name_zh": 1}):
        mid = m.get("id")
        if mid is None:
            continue
        hid, aid = m.get("home_team_id"), m.get("away_team_id")
        ht = team_by_id.get(hid) if hid is not None else None
        at = team_by_id.get(aid) if aid is not None else None
        set_fields: dict[str, str] = {}
        if ht:
            if not only_if_empty or not (m.get("home_team_name") or "").strip():
                set_fields["home_team_name"] = ht["name"] or ""
            if not only_if_empty or not (m.get("home_team_name_zh") or "").strip():
                if ht["name_zh"]:
                    set_fields["home_team_name_zh"] = ht["name_zh"]
        if at:
            if not only_if_empty or not (m.get("away_team_name") or "").strip():
                set_fields["away_team_name"] = at["name"] or ""
            if not only_if_empty or not (m.get("away_team_name_zh") or "").strip():
                if at["name_zh"]:
                    set_fields["away_team_name_zh"] = at["name_zh"]
        if set_fields:
            matches_coll.update_one({"id": mid}, {"$set": set_fields})
            updated += 1
        else:
            skipped += 1
    return updated, skipped


def _result_from_prediction(result: dict) -> str:
    """从预测 result 推导胜平负：主/平/客。优先 predicted_score，否则按 prob 最大。"""
    pred_score = result.get("predicted_score")
    if pred_score and len(pred_score) >= 2:
        sh, sa = int(pred_score[0]), int(pred_score[1])
        if sh > sa:
            return "主"
        if sa > sh:
            return "客"
        return "平"
    ph = float(result.get("prob_home_win") or 0)
    pd = float(result.get("prob_draw") or 0)
    pa = float(result.get("prob_away_win") or 0)
    if ph >= pd and ph >= pa:
        return "主"
    if pa >= pd and pa >= ph:
        return "客"
    return "平"


# ----- Predictions -----
def save_prediction(match_id: int, result: dict) -> None:
    if use_mongo():
        db = _db()
        coll = db["predictions"]
        doc = {
            "match_id": match_id,
            "exp_goals_home": float(result["exp_goals_home"]),
            "exp_goals_away": float(result["exp_goals_away"]),
            "prob_home_win": float(result["prob_home_win"]),
            "prob_draw": float(result["prob_draw"]),
            "prob_away_win": float(result["prob_away_win"]),
            "last_n": int(result["based_on_last_n"]),
            "competition_id": result.get("competition_id"),
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        model_res = result.get("model_predicted_result")
        if model_res is None or (isinstance(model_res, str) and not model_res.strip()):
            model_res = _result_from_prediction(result)
        doc["model_predicted_result"] = model_res
        doc["model_predicted_scores"] = result.get("model_predicted_scores") or result.get("predicted_scores") or [[int(result.get("exp_goals_home") or 0), int(result.get("exp_goals_away") or 0)]]
        fused_res = result.get("predicted_result")
        if fused_res is None or (isinstance(fused_res, str) and not fused_res.strip()):
            fused_res = model_res
        doc["predicted_result"] = fused_res
        doc["confidence"] = result.get("confidence", "high")
        doc["is_boundary"] = bool(result.get("is_boundary", False))
        if result.get("api_predicted_result") is not None:
            doc["api_predicted_result"] = result["api_predicted_result"]
            if result.get("api_predicted_scores"):
                doc["api_predicted_scores"] = result["api_predicted_scores"]
        existing = coll.find_one({"match_id": match_id})
        # 若本次未拿到 API 预测（如限流），保留已有文档中的 API 预测，避免 repredict 后列表 API 列为空
        if existing and doc.get("api_predicted_result") is None and existing.get("api_predicted_result") is not None:
            doc["api_predicted_result"] = existing["api_predicted_result"]
            if existing.get("api_predicted_scores"):
                doc["api_predicted_scores"] = existing["api_predicted_scores"]
        # 竞彩让球：优先用 result 传入的让球数，否则按 match_id 从 match_odds 取
        hc = result.get("rqspf_handicap")
        if hc is None:
            odds = match_odds_load_by_match_id(match_id)
            if odds:
                hc = odds.get("rqspf_handicap")
                if hc is None and odds.get("ah_handicap") is not None:
                    try:
                        hc = float(str(odds["ah_handicap"]).strip())
                    except (TypeError, ValueError):
                        pass
        if hc is not None:
            try:
                hcf = float(hc)
                doc["rqspf_handicap"] = int(hcf) if hcf == int(hcf) else hcf
                eh, ea = float(result.get("exp_goals_home", 0)), float(result.get("exp_goals_away", 0))
                adj_h = eh + hcf
                if adj_h > ea:
                    doc["predicted_rqspf"] = "主"
                elif adj_h < ea:
                    doc["predicted_rqspf"] = "客"
                else:
                    doc["predicted_rqspf"] = "平"
            except (TypeError, ValueError):
                pass
        if existing:
            doc["id"] = existing.get("id")
            coll.replace_one({"match_id": match_id}, doc)
        else:
            doc["id"] = _next_id(coll)
            coll.insert_one(doc)
        return
    from etl.schema import get_engine, Prediction
    from sqlalchemy.orm import Session
    with Session(get_engine()) as s:
        s.add(Prediction(
            match_id=match_id, exp_goals_home=float(result["exp_goals_home"]), exp_goals_away=float(result["exp_goals_away"]),
            prob_home_win=float(result["prob_home_win"]), prob_draw=float(result["prob_draw"]), prob_away_win=float(result["prob_away_win"]),
            last_n=int(result["based_on_last_n"]), competition_id=result.get("competition_id"),
        ))
        s.commit()


def predictions_get_entry_by_match_id(match_id, home_name: str = "", away_name: str = "", match_date: str = "") -> dict | None:
    """
    仅 Mongo：按 match_id 从 predictions 集合取一条预测，转成与 prediction_log 一致的 entry 结构，
    供 Web /predictions 在 prediction_log 无记录时兜底展示。未找到返回 None。
    """
    if not use_mongo() or match_id is None:
        return None
    db = _db()
    p = db["predictions"].find_one({"match_id": match_id})
    if not p:
        return None
    m = _matches_coll().find_one({"id": match_id}, {"match_date": 1, "home_team_id": 1, "away_team_id": 1, "home_team_name": 1, "away_team_name": 1, "competition_id": 1})
    date_str = (match_date or "")[:10]
    h_name, a_name = home_name.strip(), away_name.strip()
    if m:
        date_str = (m.get("match_date") or "")[:10]
        if not h_name or not a_name:
            teams = {t["id"]: t.get("name") or "" for t in db["teams"].find({"id": {"$in": [m.get("home_team_id"), m.get("away_team_id")]}}, {"id": 1, "name": 1})}
            h_name = h_name or (m.get("home_team_name") or "").strip() or teams.get(m.get("home_team_id")) or "?"
            a_name = a_name or (m.get("away_team_name") or "").strip() or teams.get(m.get("away_team_id")) or "?"
    comp_name = ""
    if m and m.get("competition_id") is not None:
        c = db["competitions"].find_one({"id": m["competition_id"]}, {"name": 1})
        comp_name = (c.get("name") or "").strip() if c else ""
    eh = float(p.get("exp_goals_home", 0))
    ea = float(p.get("exp_goals_away", 0))
    sh, sa = round(eh), round(ea)
    pred_result = "主" if sh > sa else ("客" if sa > sh else "平")
    out = {
        "id": p.get("id"),
        "match_id": match_id,
        "date": date_str,
        "home_team": h_name or "?",
        "away_team": a_name or "?",
        "competition": comp_name,
        "exp_goals_home": eh,
        "exp_goals_away": ea,
        "prob_home_win": float(p.get("prob_home_win", 0)),
        "prob_draw": float(p.get("prob_draw", 0)),
        "prob_away_win": float(p.get("prob_away_win", 0)),
        "predicted_result": pred_result,
        "last_n": int(p.get("last_n", 10)),
        "actual_home_score": None,
        "actual_away_score": None,
        "actual_result": None,
        "confidence": p.get("confidence") or "high",
        "is_boundary": bool(p.get("is_boundary", False)),
        "odds_used": False,
        "predicted_scores": [[sh, sa]],
    }
    if p.get("model_predicted_result") is not None:
        out["model_predicted_result"] = p.get("model_predicted_result")
        out["model_predicted_scores"] = p.get("model_predicted_scores") or [[sh, sa]]
    if p.get("api_predicted_result") is not None:
        out["api_predicted_result"] = p.get("api_predicted_result")
        out["api_predicted_scores"] = p.get("api_predicted_scores")
    if p.get("rqspf_handicap") is not None:
        out["rqspf_handicap"] = p.get("rqspf_handicap")
    if p.get("predicted_rqspf") is not None:
        out["predicted_rqspf"] = p.get("predicted_rqspf")
    return out


def predict_get_eval_rows(
    today_only: bool = False,
    year: int | None = None,
    need_breakdown: bool = False,
    by_competition: bool = False,
    by_last_n: bool = False,
    include_match_date: bool = False,
) -> list[tuple]:
    """
    从 DB 取「有预测且已完赛」的评估用行，与 evaluate_predictions 的 SQL 行结构一致。
    每行: (id, match_id, exp_h, exp_a, prob_h, prob_d, prob_a, created_at, act_h, act_a [, last_n, cid, cname] [, match_date])。
    仅 Mongo 时使用；SQL 路径仍由 engine 查。
    """
    if not use_mongo():
        return []
    db = _db()
    # 已完赛的 match_id 集合（有比分）
    finished = {m["id"]: m for m in _matches_coll().find(
        {"home_score": {"$ne": None}, "away_score": {"$ne": None}},
        {"id": 1, "home_score": 1, "away_score": 1, "match_date": 1},
    )}
    if not finished:
        return []
    match_ids = set(finished.keys())
    # 预测且 match 已完赛
    pred_cursor = db["predictions"].find({"match_id": {"$in": list(match_ids)}})
    comp_cache = {}
    if need_breakdown and by_competition:
        for c in db["competitions"].find({}, {"id": 1, "name": 1}):
            comp_cache[c["id"]] = (c.get("name") or "").strip()

    rows = []
    for p in pred_cursor:
        mid = p.get("match_id")
        m = finished.get(mid)
        if not m:
            continue
        match_date = (m.get("match_date") or "")[:10]
        if year and (len(match_date) < 10 or not (match_date >= f"{year}-01-01" and match_date < f"{year + 1}-01-01")):
            continue
        created = p.get("created_at") or ""
        if today_only and created:
            from datetime import datetime, timezone
            try:
                ct = datetime.fromisoformat(created.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                if (now - ct).total_seconds() > 86400:  # 超过 1 天
                    continue
            except Exception:
                pass
        act_h = int(m.get("home_score", 0))
        act_a = int(m.get("away_score", 0))
        base = (
            p.get("id") or 0,
            mid,
            float(p.get("exp_goals_home", 0)),
            float(p.get("exp_goals_away", 0)),
            float(p.get("prob_home_win", 0)),
            float(p.get("prob_draw", 0)),
            float(p.get("prob_away_win", 0)),
            created,
            act_h,
            act_a,
        )
        if need_breakdown:
            cid = p.get("competition_id")
            cname = comp_cache.get(cid, "") if cid is not None else ""
            base = base + (int(p.get("last_n") or 0), cid, cname)
        if include_match_date:
            base = base + (match_date,)
        rows.append(base)

    # 与 SQL 一致：按 created_at 倒序
    rows.sort(key=lambda r: (r[7] or ""), reverse=True)
    return rows


def predict_get_ml_training_rows() -> list[tuple]:
    """
    从 Mongo 取「有预测且已完赛」的 ML 训练用行。
    每行: (exp_goals_home, exp_goals_away, prob_home_win, prob_draw, prob_away_win, last_n, competition_id, home_score, away_score, home_rank, away_rank)。
    home_rank/away_rank 来自 match，无则 None。仅 Mongo 时使用。
    """
    if not use_mongo():
        return []
    db = _db()
    finished = {m["id"]: m for m in _matches_coll().find(
        {"home_score": {"$ne": None}, "away_score": {"$ne": None}},
        {"id": 1, "home_score": 1, "away_score": 1, "home_rank": 1, "away_rank": 1, "match_date": 1},
    )}
    if not finished:
        return []
    rows = []
    for p in db["predictions"].find({"match_id": {"$in": list(finished.keys())}}):
        mid = p.get("match_id")
        m = finished.get(mid)
        if not m:
            continue
        hr = m.get("home_rank")
        ar = m.get("away_rank")
        if hr is not None:
            try:
                hr = int(hr)
            except (TypeError, ValueError):
                hr = None
        if ar is not None:
            try:
                ar = int(ar)
            except (TypeError, ValueError):
                ar = None
        match_date_str = (m.get("match_date") or "")[:10] or None
        rows.append((
            float(p.get("exp_goals_home", 0)),
            float(p.get("exp_goals_away", 0)),
            float(p.get("prob_home_win", 0)),
            float(p.get("prob_draw", 0)),
            float(p.get("prob_away_win", 0)),
            int(p.get("last_n") or 10),
            p.get("competition_id"),
            int(m.get("home_score", 0)),
            int(m.get("away_score", 0)),
            hr,
            ar,
            match_date_str,
        ))
    return rows


def predict_get_recap_rows(
    year: int | None = None,
    by_competition: bool = False,
) -> list[tuple]:
    """
    从 Mongo 取「有预测且已完赛」的复盘用行，与 generate_recap_from_db 的 SQL 行结构一致。
    每行: (id, match_id, exp_h, exp_a, prob_h, prob_d, prob_a, act_h, act_a, home_team, away_team, comp_name)。
    仅 Mongo 时使用；SQL 路径仍由 engine 查。
    """
    if not use_mongo():
        return []
    db = _db()
    finished = {m["id"]: m for m in _matches_coll().find(
        {"home_score": {"$ne": None}, "away_score": {"$ne": None}},
        {"id": 1, "home_score": 1, "away_score": 1, "match_date": 1, "home_team_id": 1, "away_team_id": 1},
    )}
    if not finished:
        return []
    match_ids = list(finished.keys())
    teams = {t["id"]: (t.get("name") or "").strip() or "?" for t in db["teams"].find({"id": {"$in": _team_ids_from_matches(finished)}}, {"id": 1, "name": 1})}
    comp_cache = {}
    if by_competition:
        for c in db["competitions"].find({}, {"id": 1, "name": 1}):
            comp_cache[c["id"]] = (c.get("name") or "").strip()

    rows = []
    for p in db["predictions"].find({"match_id": {"$in": match_ids}}):
        mid = p.get("match_id")
        m = finished.get(mid)
        if not m:
            continue
        match_date = (m.get("match_date") or "")[:10]
        if year and (len(match_date) < 10 or not (match_date >= f"{year}-01-01" and match_date < f"{year + 1}-01-01")):
            continue
        act_h = int(m.get("home_score", 0))
        act_a = int(m.get("away_score", 0))
        home_team = teams.get(m.get("home_team_id")) or "?"
        away_team = teams.get(m.get("away_team_id")) or "?"
        cid = p.get("competition_id")
        comp_name = comp_cache.get(cid, "") if cid is not None else ""
        rows.append((
            p.get("id") or 0,
            mid,
            float(p.get("exp_goals_home", 0)),
            float(p.get("exp_goals_away", 0)),
            float(p.get("prob_home_win", 0)),
            float(p.get("prob_draw", 0)),
            float(p.get("prob_away_win", 0)),
            act_h,
            act_a,
            home_team,
            away_team,
            comp_name,
        ))
    # 与 SQL 一致：按 created_at 倒序（这里无 created_at，按 match_id 稳定排序）
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows


def _team_ids_from_matches(finished: dict) -> list:
    """从 finished matches 中收集所有 home_team_id, away_team_id。"""
    ids = set()
    for m in finished.values():
        if m.get("home_team_id") is not None:
            ids.add(m["home_team_id"])
        if m.get("away_team_id") is not None:
            ids.add(m["away_team_id"])
    return list(ids)


def predictions_exists_for_match(match_id: int) -> bool:
    """predictions 表是否已有该 match_id 的记录。"""
    if use_mongo():
        return _db()["predictions"].find_one({"match_id": match_id}) is not None
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT 1 FROM predictions WHERE match_id = :mid LIMIT 1"), {"mid": match_id}).fetchone()
    return row is not None


def predict_consistency_stats_mongo() -> dict | None:
    """
    仅 Mongo：检查 predictions 与 matches 表（由 MONGO_MATCHES_COLLECTION 指定，可能是 matchs）的一致性。
    返回 dict：matches_collection, matches_count, predictions_count, orphan_match_ids（在 match(s) 中不存在的预测）,
    matched_count（能对上比赛的预测数）。若未用 Mongo 返回 None。
    """
    if not use_mongo():
        return None
    db = _db()
    coll_name = getattr(config, "MONGO_MATCHES_COLLECTION", "matches")
    matches_coll = _matches_coll()
    match_ids_in_db = set(m["id"] for m in matches_coll.find({}, {"id": 1}))
    pred_docs = list(db["predictions"].find({}, {"match_id": 1}))
    predictions_count = len(pred_docs)
    orphan_match_ids = []
    matched_count = 0
    for p in pred_docs:
        mid = p.get("match_id")
        if mid is None:
            continue
        if mid in match_ids_in_db:
            matched_count += 1
        else:
            orphan_match_ids.append(mid)
    return {
        "matches_collection": coll_name,
        "matches_count": len(match_ids_in_db),
        "predictions_count": predictions_count,
        "orphan_match_ids": orphan_match_ids,
        "matched_count": matched_count,
    }


def delete_predictions_by_match_id(match_id: int) -> None:
    if use_mongo():
        _db()["predictions"].delete_many({"match_id": match_id})
        return
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        conn.execute(text("DELETE FROM predictions WHERE match_id = :mid"), {"mid": match_id})
        conn.commit()


def predictions_get_match_ids_missing_api(years: list[int] | None = None) -> list[int]:
    """仅 Mongo：返回已有预测记录但缺 api_predicted_result 的 match_id 列表。years 为 [2026] 时只返回该年比赛日的场次。"""
    if not use_mongo():
        return []
    db = _db()
    cursor = db["predictions"].find(
        {"$or": [{"api_predicted_result": {"$exists": False}}, {"api_predicted_result": None}, {"api_predicted_result": ""}]},
        {"match_id": 1},
    )
    match_ids = [d["match_id"] for d in cursor if d.get("match_id") is not None]
    if not years:
        return match_ids
    # 只保留比赛日在指定年份的 match_id
    coll = _matches_coll()
    year_prefixes = [str(y) for y in years]
    allowed = set()
    for yp in year_prefixes:
        for d in coll.find({"match_date": {"$regex": f"^{yp}-"}}, {"id": 1}):
            if d.get("id") is not None:
                allowed.add(d["id"])
    return [mid for mid in match_ids if mid in allowed]


def predictions_update_api_fields(match_id: int, api_predicted_result: str, api_predicted_scores: list | None) -> bool:
    """仅 Mongo：只更新该 match_id 预测文档的 API 字段，不覆盖模型等。返回是否更新成功。"""
    if not use_mongo() or not api_predicted_result:
        return False
    db = _db()
    update = {"api_predicted_result": api_predicted_result}
    if api_predicted_scores:
        update["api_predicted_scores"] = api_predicted_scores
    r = db["predictions"].update_one({"match_id": match_id}, {"$set": update})
    return r.modified_count > 0 or r.matched_count > 0


def predictions_api_stats(years: list[int] | None = None) -> tuple[int, int] | None:
    """仅 Mongo：返回 (总条数, 有 api_predicted_result 的条数)。years 为 [2025,2026] 时只统计该年比赛日的预测；无则返回 None。"""
    if not use_mongo():
        return None
    db = _db()
    if not years:
        total = db["predictions"].count_documents({})
        with_api = db["predictions"].count_documents({"api_predicted_result": {"$exists": True, "$ne": None, "$ne": ""}})
        return (total, with_api)
    coll = _matches_coll()
    allowed = set()
    for y in years:
        for d in coll.find({"match_date": {"$regex": f"^{y}-"}}, {"id": 1}):
            if d.get("id") is not None:
                allowed.add(d["id"])
    if not allowed:
        return (0, 0)
    total = db["predictions"].count_documents({"match_id": {"$in": list(allowed)}})
    with_api = db["predictions"].count_documents({
        "match_id": {"$in": list(allowed)},
        "api_predicted_result": {"$exists": True, "$ne": None, "$ne": ""},
    })
    return (total, with_api)


def _actual_result_from_score(home_score: int, away_score: int) -> str:
    """根据主客队比分返回 主/平/客。"""
    if home_score > away_score:
        return "主"
    if away_score > home_score:
        return "客"
    return "平"


def _normalize_pred_result(v: str | None) -> str | None:
    """统一为 主/平/客，空或无效返回 None。"""
    if v is None or not isinstance(v, str):
        return None
    s = (v or "").strip()
    if s in ("主", "平", "客"):
        return s
    return None


def _derive_outcome_from_pred_doc(p: dict) -> str:
    """从 predictions 文档用概率或期望进球推导 主/平/客。"""
    ph = float(p.get("prob_home_win") or 0)
    pd = float(p.get("prob_draw") or 0)
    pa = float(p.get("prob_away_win") or 0)
    if ph >= pd and ph >= pa:
        return "主"
    if pa >= pd and pa >= ph:
        return "客"
    return "平"


def predictions_accuracy_stats(year: int | None = None) -> dict | None:
    """
    仅 Mongo：统计「已完赛且有预测」场次中，模型预测 / API 预测 / 融合预测 的胜平负命中率。
    year 为 2026 时只统计该年 match_date 的比赛；None 表示全量。
    返回 { "model": { "hit", "total", "rate" }, "api": {...}, "fused": {...}, "year": year }，无数据时 rate 为 None 或 total 为 0。
    若文档缺少 model_predicted_result / predicted_result，则用 prob_* 推导，以便历史数据也能展示命中率。
    """
    if not use_mongo():
        return None
    db = _db()
    q = {"home_score": {"$ne": None}, "away_score": {"$ne": None}}
    if year is not None:
        q["match_date"] = {"$regex": f"^{year}-"}
    finished = {m["id"]: m for m in _matches_coll().find(q, {"id": 1, "home_score": 1, "away_score": 1})}
    if not finished:
        return {"model": {"hit": 0, "total": 0, "rate": None}, "api": {"hit": 0, "total": 0, "rate": None}, "fused": {"hit": 0, "total": 0, "rate": None}}
    model_hit, model_total = 0, 0
    api_hit, api_total = 0, 0
    fused_hit, fused_total = 0, 0
    for p in db["predictions"].find({"match_id": {"$in": list(finished.keys())}}):
        mid = p.get("match_id")
        m = finished.get(mid)
        if not m:
            continue
        h, a = int(m.get("home_score", 0)), int(m.get("away_score", 0))
        actual = _actual_result_from_score(h, a)
        model_res = _normalize_pred_result(p.get("model_predicted_result"))
        if model_res is None:
            model_res = _derive_outcome_from_pred_doc(p)
        model_total += 1
        if model_res == actual:
            model_hit += 1
        api_res = _normalize_pred_result(p.get("api_predicted_result"))
        if api_res is not None:
            api_total += 1
            if api_res == actual:
                api_hit += 1
        fused_res = _normalize_pred_result(p.get("predicted_result"))
        if fused_res is None:
            fused_res = model_res
        fused_total += 1
        if fused_res == actual:
            fused_hit += 1
    def _rate(h: int, t: int) -> float | None:
        return round(h / t, 4) if t and t > 0 else None
    out = {
        "model": {"hit": model_hit, "total": model_total, "rate": _rate(model_hit, model_total)},
        "api": {"hit": api_hit, "total": api_total, "rate": _rate(api_hit, api_total)},
        "fused": {"hit": fused_hit, "total": fused_total, "rate": _rate(fused_hit, fused_total)},
    }
    if year is not None:
        out["year"] = year
    # 说明：命中率只统计「已完赛且该场有对应预测」；若 API 显示 0/0 多为已完赛场次未补 API 预测
    api_covered = db["predictions"].count_documents({"api_predicted_result": {"$exists": True, "$ne": None, "$ne": ""}})
    out["api_covered_total"] = api_covered
    out["api_covered_finished"] = api_total  # 已完赛且含 API 预测的场数（与 api.total 一致）
    return out


def predict_dedup_predictions_mongo() -> int:
    """
    仅 Mongo：按 match_id 去重，每组只保留一条（保留 id 最小的）。
    返回删除的重复文档数。
    """
    if not use_mongo():
        return 0
    db = _db()
    coll = db["predictions"]
    pipeline = [
        {"$group": {"_id": "$match_id", "docs": {"$push": {"_id": "$_id", "id": "$id"}}, "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
    ]
    deleted = 0
    for g in coll.aggregate(pipeline):
        docs = g.get("docs") or []
        if len(docs) <= 1:
            continue
        # 按 id 升序保留第一条，删其余
        docs.sort(key=lambda d: (d.get("id") is None, d.get("id") or 0))
        to_remove = [d["_id"] for d in docs[1:]]
        if to_remove:
            r = coll.delete_many({"_id": {"$in": to_remove}})
            deleted += r.deleted_count
    return deleted


# ----- Prediction log -----
def prediction_log_get_by_match_id(match_id: int) -> dict | None:
    if use_mongo():
        return _db()["prediction_log"].find_one({"match_id": match_id})
    from etl.schema import get_engine, PredictionLog
    from sqlalchemy.orm import Session
    with Session(get_engine()) as s:
        r = s.query(PredictionLog).filter(PredictionLog.match_id == match_id).first()
        return _plog_row_to_dict(r) if r else None


def _plog_row_to_dict(r) -> dict:
    return {
        "id": r.id, "match_id": r.match_id, "log_date": r.log_date, "home_team": r.home_team, "away_team": r.away_team,
        "competition": r.competition or "", "exp_goals_home": r.exp_goals_home, "exp_goals_away": r.exp_goals_away,
        "prob_home_win": r.prob_home_win, "prob_draw": r.prob_draw, "prob_away_win": r.prob_away_win,
        "predicted_result": r.predicted_result, "last_n": r.last_n, "actual_home_score": r.actual_home_score,
        "actual_away_score": r.actual_away_score, "actual_result": r.actual_result, "confidence": r.confidence or "high",
        "odds_used": bool(r.odds_used), "predicted_scores_json": r.predicted_scores_json, "created_at": r.created_at,
    }


def prediction_log_add(entry: dict) -> None:
    """插入一条 prediction_log。"""
    if use_mongo():
        db = _db()["prediction_log"]
        e = dict(entry)
        if "id" not in e or e.get("id") is None:
            e["id"] = _next_id(_db()["prediction_log"])
        if "created_at" not in e:
            e["created_at"] = datetime.utcnow().isoformat() + "Z"
        db.insert_one(e)
        return
    from etl.schema import get_engine, PredictionLog
    from sqlalchemy.orm import Session
    with Session(get_engine()) as s:
        s.add(PredictionLog(
            match_id=entry.get("match_id"), log_date=entry["log_date"], home_team=entry["home_team"], away_team=entry["away_team"],
            competition=entry.get("competition", ""), exp_goals_home=entry["exp_goals_home"], exp_goals_away=entry["exp_goals_away"],
            prob_home_win=entry["prob_home_win"], prob_draw=entry["prob_draw"], prob_away_win=entry["prob_away_win"],
            predicted_result=entry["predicted_result"], last_n=entry["last_n"], actual_home_score=entry.get("actual_home_score"),
            actual_away_score=entry.get("actual_away_score"), actual_result=entry.get("actual_result"),
            confidence=entry.get("confidence", "high"), odds_used=entry.get("odds_used", False),
            predicted_scores_json=entry.get("predicted_scores_json"),
        ))
        s.commit()


def prediction_log_list_entries(date_filter: str | None = None) -> list[dict]:
    """返回 prediction_log 条目列表，与 _row_to_entry 结构一致（id, match_id, date, home_team, ...）。date_filter 为 YYYY-MM-DD 时只返回该日。"""
    if use_mongo():
        coll = _db()["prediction_log"]
        q = {"log_date": date_filter[:10]} if date_filter and len(date_filter) >= 10 else {}
        cursor = coll.find(q).sort("log_date", 1).sort("id", 1)
        out = []
        for r in cursor:
            try:
                ps = json.loads(r["predicted_scores_json"]) if r.get("predicted_scores_json") else [[0, 0]]
            except Exception:
                ps = [[0, 0]]
            try:
                mps = json.loads(r["model_predicted_scores_json"]) if r.get("model_predicted_scores_json") else None
            except Exception:
                mps = None
            try:
                aps = json.loads(r["api_predicted_scores_json"]) if r.get("api_predicted_scores_json") else None
            except Exception:
                aps = None
            e = {
                "id": r.get("id"),
                "match_id": r.get("match_id"),
                "date": (r.get("log_date") or "")[:10],
                "home_team": r.get("home_team") or "",
                "away_team": r.get("away_team") or "",
                "competition": r.get("competition") or "",
                "exp_goals_home": float(r.get("exp_goals_home", 0)),
                "exp_goals_away": float(r.get("exp_goals_away", 0)),
                "prob_home_win": float(r.get("prob_home_win", 0)),
                "prob_draw": float(r.get("prob_draw", 0)),
                "prob_away_win": float(r.get("prob_away_win", 0)),
                "predicted_result": r.get("predicted_result") or "平",
                "last_n": int(r.get("last_n", 10)),
                "actual_home_score": r.get("actual_home_score"),
                "actual_away_score": r.get("actual_away_score"),
                "actual_result": r.get("actual_result"),
                "confidence": r.get("confidence") or "high",
                "odds_used": bool(r.get("odds_used", False)),
                "predicted_scores": ps,
            }
            if r.get("model_predicted_result") is not None:
                e["model_predicted_result"] = r.get("model_predicted_result")
                e["model_predicted_scores"] = mps if mps is not None else ps
            if r.get("api_predicted_result") is not None:
                e["api_predicted_result"] = r.get("api_predicted_result")
                e["api_predicted_scores"] = aps
            out.append(e)
        return out
    from sqlalchemy import text
    from etl.schema import get_engine
    eng = get_engine()
    with eng.connect() as conn:
        q = "SELECT id, match_id, log_date, home_team, away_team, competition, exp_goals_home, exp_goals_away, prob_home_win, prob_draw, prob_away_win, predicted_result, last_n, actual_home_score, actual_away_score, actual_result, confidence, odds_used, predicted_scores_json FROM prediction_log"
        params = {}
        if date_filter and len(date_filter) >= 10:
            q += " WHERE log_date = :d"
            params["d"] = date_filter[:10]
        q += " ORDER BY log_date, id"
        rows = conn.execute(text(q), params).fetchall()
    out = []
    for r in rows:
        try:
            ps = json.loads(r[18]) if r[18] else [[0, 0]]
        except Exception:
            ps = [[0, 0]]
        out.append({
            "id": r[0], "match_id": r[1], "date": (r[2] or "")[:10], "home_team": r[3] or "", "away_team": r[4] or "",
            "competition": r[5] or "", "exp_goals_home": float(r[6]), "exp_goals_away": float(r[7]),
            "prob_home_win": float(r[8]), "prob_draw": float(r[9]), "prob_away_win": float(r[10]),
            "predicted_result": r[11] or "平", "last_n": int(r[12]), "actual_home_score": r[13], "actual_away_score": r[14],
            "actual_result": r[15], "confidence": r[16] or "high", "odds_used": bool(r[17]),
            "predicted_scores": ps,
        })
    return out


def prediction_log_update_by_id(entry_id: int, entry: dict) -> None:
    """按 id 更新一条 prediction_log。"""
    if use_mongo():
        coll = _db()["prediction_log"]
        pred_scores = entry.get("predicted_scores") or [[0, 0]]
        ps_json = json.dumps(pred_scores[:2], ensure_ascii=False)
        upd = {
            "log_date": (entry.get("date") or "")[:10],
            "home_team": (entry.get("home_team") or "").strip() or "?",
            "away_team": (entry.get("away_team") or "").strip() or "?",
            "competition": (entry.get("competition") or "").strip(),
            "exp_goals_home": float(entry.get("exp_goals_home", 0)),
            "exp_goals_away": float(entry.get("exp_goals_away", 0)),
            "prob_home_win": float(entry.get("prob_home_win", 0)),
            "prob_draw": float(entry.get("prob_draw", 0)),
            "prob_away_win": float(entry.get("prob_away_win", 0)),
            "predicted_result": (entry.get("predicted_result") or "平").strip(),
            "last_n": int(entry.get("last_n", 10)),
            "actual_home_score": entry.get("actual_home_score"),
            "actual_away_score": entry.get("actual_away_score"),
            "actual_result": entry.get("actual_result"),
            "confidence": (entry.get("confidence") or "high").strip(),
            "odds_used": bool(entry.get("odds_used", False)),
            "predicted_scores_json": ps_json,
        }
        coll.update_one({"id": entry_id}, {"$set": upd})
        return
    from sqlalchemy import text
    from etl.schema import get_engine
    pred_scores = entry.get("predicted_scores") or [[0, 0]]
    ps_json = json.dumps(pred_scores[:2], ensure_ascii=False)
    row = {
        "log_date": (entry.get("date") or "")[:10], "home_team": (entry.get("home_team") or "").strip() or "?",
        "away_team": (entry.get("away_team") or "").strip() or "?", "competition": (entry.get("competition") or "").strip(),
        "exp_goals_home": float(entry.get("exp_goals_home", 0)), "exp_goals_away": float(entry.get("exp_goals_away", 0)),
        "prob_home_win": float(entry.get("prob_home_win", 0)), "prob_draw": float(entry.get("prob_draw", 0)),
        "prob_away_win": float(entry.get("prob_away_win", 0)), "predicted_result": (entry.get("predicted_result") or "平").strip(),
        "last_n": int(entry.get("last_n", 10)), "actual_home_score": entry.get("actual_home_score"),
        "actual_away_score": entry.get("actual_away_score"), "actual_result": entry.get("actual_result"),
        "confidence": (entry.get("confidence") or "high").strip(), "odds_used": bool(entry.get("odds_used", False)),
        "predicted_scores_json": ps_json,
    }
    with get_engine().connect() as conn:
        conn.execute(text("""
            UPDATE prediction_log SET log_date=:log_date, home_team=:home_team, away_team=:away_team, competition=:competition,
            exp_goals_home=:exp_goals_home, exp_goals_away=:exp_goals_away, prob_home_win=:prob_home_win, prob_draw=:prob_draw, prob_away_win=:prob_away_win,
            predicted_result=:predicted_result, last_n=:last_n, actual_home_score=:actual_home_score, actual_away_score=:actual_away_score, actual_result=:actual_result,
            confidence=:confidence, odds_used=:odds_used, predicted_scores_json=:predicted_scores_json WHERE id=:id
        """), {**row, "id": entry_id})
        conn.commit()


def prediction_log_delete_by_date(date_str: str) -> int:
    """删除指定 log_date 的所有条目，返回删除条数。"""
    if use_mongo():
        r = _db()["prediction_log"].delete_many({"log_date": (date_str or "")[:10]})
        return r.deleted_count
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        r = conn.execute(text("DELETE FROM prediction_log WHERE log_date = :d"), {"d": (date_str or "")[:10]})
        conn.commit()
        return r.rowcount


def prediction_log_sync_source_rows() -> list[dict]:
    """返回「有预测且已完赛」的场次列表，用于 sync-from-db。每项含 match_id, log_date, home_score, away_score, exp_goals_*, home_team, away_team, competition 等。"""
    if use_mongo():
        db = _db()
        out = []
        for p in db["predictions"].find({}):
            match_id = p.get("match_id")
            if match_id is None:
                continue
            m = _matches_coll().find_one({"id": match_id})
            if not m or m.get("home_score") is None or m.get("away_score") is None:
                continue
            ht = db["teams"].find_one({"id": m.get("home_team_id")})
            at = db["teams"].find_one({"id": m.get("away_team_id")})
            home_team = (ht.get("name") or "").strip() if ht else "?"
            away_team = (at.get("name") or "").strip() if at else "?"
            raw_meta = m.get("raw_meta") or "{}"
            try:
                meta = json.loads(raw_meta)
                comp_name = (meta.get("competition_name") or "").strip()
            except Exception:
                comp_name = ""
            log_date = (m.get("match_date") or "")[:10]
            if not log_date:
                continue
            out.append({
                "match_id": match_id,
                "log_date": log_date,
                "home_score": int(m["home_score"]),
                "away_score": int(m["away_score"]),
                "exp_goals_home": float(p.get("exp_goals_home", 0)),
                "exp_goals_away": float(p.get("exp_goals_away", 0)),
                "prob_home_win": float(p.get("prob_home_win", 0)),
                "prob_draw": float(p.get("prob_draw", 0)),
                "prob_away_win": float(p.get("prob_away_win", 0)),
                "last_n": int(p.get("last_n", 10)),
                "home_team": home_team,
                "away_team": away_team,
                "competition": comp_name,
                "competition_id": p.get("competition_id"),
            })
        return out
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT p.match_id, m.match_date, m.home_score, m.away_score,
                   p.exp_goals_home, p.exp_goals_away, p.prob_home_win, p.prob_draw, p.prob_away_win, p.last_n,
                   m.raw_meta, ht.name, at.name, p.competition_id
            FROM predictions p
            JOIN matches m ON m.id = p.match_id
            LEFT JOIN teams ht ON ht.id = m.home_team_id
            LEFT JOIN teams at ON at.id = m.away_team_id
            WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
        """)).fetchall()
    out = []
    for r in rows:
        match_id, log_date = int(r[0]), (r[1] or "")[:10]
        home_team, away_team = (r[11] or "").strip() or "?", (r[12] or "").strip() or "?"
        if not home_team or not away_team:
            try:
                meta = json.loads(r[10] or "{}")
                home_team = (meta.get("home_team_name") or "").strip() or "?"
                away_team = (meta.get("away_team_name") or "").strip() or "?"
                comp_name = (meta.get("competition_name") or "").strip()
            except Exception:
                comp_name = ""
        else:
            comp_name = ""
            if not comp_name and r[10]:
                try:
                    comp_name = (json.loads(r[10] or "{}").get("competition_name") or "").strip()
                except Exception:
                    pass
        if not log_date:
            continue
        out.append({
            "match_id": match_id, "log_date": log_date,
            "home_score": int(r[2]), "away_score": int(r[3]),
            "exp_goals_home": float(r[4]), "exp_goals_away": float(r[5]),
            "prob_home_win": float(r[6]), "prob_draw": float(r[7]), "prob_away_win": float(r[8]), "last_n": int(r[9]),
            "home_team": home_team, "away_team": away_team, "competition": comp_name,
            "competition_id": r[13] if len(r) > 13 else None,
        })
    return out


def prediction_log_count_duplicates_mongo() -> dict[str, Any]:
    """
    仅 Mongo：按 (log_date, home_team, away_team) 统计重复。
    返回 {"total_docs": N, "unique_keys": M, "duplicate_key_count": K, "duplicate_keys_sample": [(date, home, away), ...]}
    """
    if not use_mongo():
        return {"total_docs": 0, "unique_keys": 0, "duplicate_key_count": 0, "duplicate_keys_sample": []}
    coll = _db()["prediction_log"]
    by_key: dict[tuple[str, str, str], list[dict]] = {}
    for r in coll.find({}, {"log_date": 1, "home_team": 1, "away_team": 1, "match_id": 1, "id": 1, "_id": 1}):
        date_str = (r.get("log_date") or "")[:10]
        home = (r.get("home_team") or "").strip()
        away = (r.get("away_team") or "").strip()
        k = (date_str, home, away)
        by_key.setdefault(k, []).append(r)
    total = sum(len(v) for v in by_key.values())
    dup_keys = [k for k, v in by_key.items() if len(v) > 1]
    sample = dup_keys[:10]
    return {
        "total_docs": total,
        "unique_keys": len(by_key),
        "duplicate_key_count": len(dup_keys),
        "duplicate_keys_sample": sample,
    }


def prediction_log_dedup_mongo(dry_run: bool = False) -> tuple[int, int]:
    """
    仅 Mongo：按 (log_date, home_team, away_team) 去重，每组保留一条（优先有 match_id，其次 id 最小）。
    返回 (删除条数, 发生去重的 key 数)。dry_run=True 只统计不删。
    """
    if not use_mongo():
        return 0, 0
    coll = _db()["prediction_log"]
    by_key: dict[tuple[str, str, str], list[dict]] = {}
    for r in coll.find({}, {"log_date": 1, "home_team": 1, "away_team": 1, "match_id": 1, "id": 1, "_id": 1}):
        date_str = (r.get("log_date") or "")[:10]
        home = (r.get("home_team") or "").strip()
        away = (r.get("away_team") or "").strip()
        k = (date_str, home, away)
        by_key.setdefault(k, []).append(r)
    to_delete: list[Any] = []
    keys_deduped = 0
    for k, docs in by_key.items():
        if len(docs) <= 1:
            continue
        keys_deduped += 1
        # 保留：有 match_id 的优先，其次 id 最小的
        docs.sort(key=lambda d: (d.get("match_id") is None, d.get("id") or 0))
        keep = docs[0]
        for d in docs[1:]:
            to_delete.append(d["_id"])
    if dry_run or not to_delete:
        return len(to_delete), keys_deduped
    if to_delete:
        coll.delete_many({"_id": {"$in": to_delete}})
    return len(to_delete), keys_deduped


def prediction_log_replace_for_match_id(match_id: int, entry: dict) -> None:
    """若已存在该 match_id 则更新，否则插入。"""
    if use_mongo():
        coll = _db()["prediction_log"]
        existing = coll.find_one({"match_id": match_id})
        e = dict(entry)
        e["match_id"] = match_id
        if "created_at" not in e:
            e["created_at"] = datetime.utcnow().isoformat() + "Z"
        if existing:
            coll.replace_one({"match_id": match_id}, e)
        else:
            if "id" not in e or e.get("id") is None:
                e["id"] = _next_id(coll)
            coll.insert_one(e)
        return
    from etl.schema import get_engine, PredictionLog
    from sqlalchemy.orm import Session
    with Session(get_engine()) as s:
        ex = s.query(PredictionLog).filter(PredictionLog.match_id == match_id).first()
        row = PredictionLog(
            match_id=match_id, log_date=entry["log_date"], home_team=entry["home_team"], away_team=entry["away_team"],
            competition=entry.get("competition", ""), exp_goals_home=entry["exp_goals_home"], exp_goals_away=entry["exp_goals_away"],
            prob_home_win=entry["prob_home_win"], prob_draw=entry["prob_draw"], prob_away_win=entry["prob_away_win"],
            predicted_result=entry["predicted_result"], last_n=entry["last_n"], actual_home_score=entry.get("actual_home_score"),
            actual_away_score=entry.get("actual_away_score"), actual_result=entry.get("actual_result"),
            confidence=entry.get("confidence", "high"), odds_used=entry.get("odds_used", False),
            predicted_scores_json=entry.get("predicted_scores_json"),
        )
        if ex:
            s.query(PredictionLog).filter(PredictionLog.match_id == match_id).delete()
        s.add(row)
        s.commit()


# ----- Match odds -----
def match_odds_list_all() -> list[dict]:
    """返回 match_odds 全部条目：{date, home, away, league_cn, spf_win, spf_draw, spf_loss}。"""
    if use_mongo():
        out = []
        for r in _db()["match_odds"].find({}).sort("id", 1):
            league = (r.get("league_cn") or "").strip()
            if not league:
                continue
            out.append({
                "date": (r.get("log_date") or "")[:10],
                "home": (r.get("home_team") or "").strip(),
                "away": (r.get("away_team") or "").strip(),
                "league_cn": league,
                "spf_win": r.get("spf_win"),
                "spf_draw": r.get("spf_draw"),
                "spf_loss": r.get("spf_loss"),
            })
        return out
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        rows = conn.execute(text("SELECT log_date, home_team, away_team, league_cn, spf_win, spf_draw, spf_loss FROM match_odds ORDER BY id")).fetchall()
    return [{"date": (r[0] or "")[:10], "home": (r[1] or "").strip(), "away": (r[2] or "").strip(), "league_cn": (r[3] or "").strip(), "spf_win": r[4], "spf_draw": r[5], "spf_loss": r[6]} for r in rows if (r[3] or "").strip()]


def match_odds_get_dates() -> list[str]:
    """返回 match_odds 中所有不重复的 log_date。"""
    if use_mongo():
        return list(_db()["match_odds"].distinct("log_date"))
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        rows = conn.execute(text("SELECT DISTINCT log_date FROM match_odds WHERE log_date IS NOT NULL AND trim(log_date) != ''")).fetchall()
    return [r[0] for r in rows if r[0]]


def match_odds_upsert_one(
    log_date: str, home_team: str, away_team: str, league_cn: str,
    spf_win: str, spf_draw: str, spf_loss: str, source: str = "api_football",
    ah_handicap: str | None = None, ah_home: str | None = None, ah_away: str | None = None,
    **extra: Any,
) -> None:
    """写入或更新一条 match_odds。extra 仅 Mongo 生效，可传 sporttery_match_id、rqspf_handicap、rqspf_win 等体彩扩展字段。"""
    if use_mongo():
        coll = _db()["match_odds"]
        sett: dict[str, Any] = {"spf_win": spf_win, "spf_draw": spf_draw, "spf_loss": spf_loss, "source": source, "created_at": datetime.utcnow().isoformat() + "Z"}
        if ah_handicap is not None:
            sett["ah_handicap"] = ah_handicap
        if ah_home is not None:
            sett["ah_home"] = ah_home
        if ah_away is not None:
            sett["ah_away"] = ah_away
        for k, v in extra.items():
            if v is not None and k not in ("log_date", "home_team", "away_team", "league_cn"):
                sett[k] = v
        coll.update_one(
            {"log_date": log_date[:10], "home_team": home_team, "away_team": away_team, "league_cn": league_cn},
            {"$set": sett},
            upsert=True,
        )
        return
    from sqlalchemy import text
    from etl.schema import get_engine
    # SQLite: INSERT OR REPLACE / upsert 逻辑
    with get_engine().connect() as conn:
        conn.execute(text("""
            INSERT INTO match_odds (log_date, home_team, away_team, league_cn, spf_win, spf_draw, spf_loss, source, created_at)
            VALUES (:d,:h,:a,:l,:w,:dr,:lo,:s,CURRENT_TIMESTAMP)
            ON CONFLICT(log_date,home_team,away_team,league_cn) DO UPDATE SET spf_win=:w, spf_draw=:dr, spf_loss=:lo, source=:s, created_at=CURRENT_TIMESTAMP
        """), {"d": log_date[:10], "h": home_team, "a": away_team, "l": league_cn, "w": spf_win, "dr": spf_draw, "lo": spf_loss, "s": source})
        conn.commit()


def match_odds_set_match_id(log_date: str, home_team: str, away_team: str, league_cn: str, match_id: int) -> bool:
    """仅 Mongo：给 match_odds 中匹配的文档设置 match_id，返回是否更新了至少一条。"""
    if not use_mongo():
        return False
    r = _db()["match_odds"].update_one(
        {
            "log_date": (log_date or "")[:10],
            "home_team": (home_team or "").strip(),
            "away_team": (away_team or "").strip(),
            "league_cn": (league_cn or "").strip(),
        },
        {"$set": {"match_id": match_id}},
    )
    return r.modified_count > 0


def match_odds_set_match_id_by_teams(log_date: str, home_team: str, away_team: str, match_id: int) -> bool:
    """仅 Mongo：按日期+主客队（不限定联赛）给未关联赔率回填 match_id。用于非五大联赛兜底。"""
    if not use_mongo():
        return False
    r = _db()["match_odds"].update_many(
        {
            "log_date": (log_date or "")[:10],
            "home_team": (home_team or "").strip(),
            "away_team": (away_team or "").strip(),
            "$or": [{"match_id": {"$exists": False}}, {"match_id": None}],
        },
        {"$set": {"match_id": int(match_id)}},
    )
    return r.modified_count > 0


def backfill_match_odds_match_id() -> tuple[int, int]:
    """
    仅 Mongo：用 matches 表回填 match_odds 的 match_id，使每条赔率对应到一场比赛。
    返回 (已关联的 match_odds 条数, 尝试匹配的 matches 场数)。
    """
    if not use_mongo():
        return 0, 0
    db = _db()
    comp_list = ("Premier League", "La Liga", "Primera Division", "Primera División", "Serie A", "Bundesliga", "1. Bundesliga", "Ligue 1")
    league_map = {"Premier League": "英超", "La Liga": "西甲", "Primera Division": "西甲", "Primera División": "西甲",
                  "Serie A": "意甲", "Bundesliga": "德甲", "1. Bundesliga": "德甲", "Ligue 1": "法甲"}
    # 五大联赛优先用 league_cn 精确匹配；非五大联赛走「日期+主客队」兜底匹配
    comp_ids = [c["id"] for c in db["competitions"].find({}, {"id": 1})]
    teams = {t["id"]: (t.get("name") or "").strip() for t in db["teams"].find({}, {"id": 1, "name": 1})}
    comps = {c["id"]: c.get("name") or "" for c in db["competitions"].find({}, {"id": 1, "name": 1})}
    linked = 0
    total = 0
    for m in _matches_coll().find({"competition_id": {"$in": comp_ids}}, {"id": 1, "match_date": 1, "home_team_id": 1, "away_team_id": 1, "competition_id": 1}):
        total += 1
        date_prefix = (m.get("match_date") or "")[:10]
        if len(date_prefix) != 10:
            continue
        home_name = teams.get(m.get("home_team_id")) or ""
        away_name = teams.get(m.get("away_team_id")) or ""
        cname = comps.get(m.get("competition_id")) or ""
        league_cn = league_map.get(cname) or ""
        if not home_name or not away_name:
            continue
        linked_now = False
        if league_cn:
            linked_now = match_odds_set_match_id(date_prefix, home_name, away_name, league_cn, m["id"])
        if not linked_now:
            linked_now = match_odds_set_match_id_by_teams(date_prefix, home_name, away_name, m["id"])
        # 兼容部分赔率源日期与比赛日存在时区导致的 ±1 天偏移
        if not linked_now:
            try:
                from datetime import datetime, timedelta
                d0 = datetime.strptime(date_prefix, "%Y-%m-%d").date()
                for d in (d0 - timedelta(days=1), d0 + timedelta(days=1)):
                    ds = d.isoformat()
                    ok = False
                    if league_cn:
                        ok = match_odds_set_match_id(ds, home_name, away_name, league_cn, m["id"])
                    if not ok:
                        ok = match_odds_set_match_id_by_teams(ds, home_name, away_name, m["id"])
                    if ok:
                        linked_now = True
                        break
            except Exception:
                pass
        if linked_now:
            linked += 1
    return linked, total


def dedup_match_odds(keep_with_match_id: bool = True, dry_run: bool = False) -> tuple[int, int]:
    """
    仅 Mongo：按 (log_date, home_team, away_team, league_cn) 去重，每组只保留一条。
    保留策略：若 keep_with_match_id 为 True 则优先保留已设置 match_id 的文档，否则保留 _id 最小的。
    dry_run 为 True 时只统计不删除。
    返回 (删除的文档数, 发生重复的键组数)。
    """
    if not use_mongo():
        return 0, 0
    db = _db()
    coll = db["match_odds"]
    pipeline = [
        {"$match": {"log_date": {"$exists": True, "$ne": None}, "league_cn": {"$exists": True, "$ne": ""}}},
        {"$group": {
            "_id": {
                "log_date": {"$substr": ["$log_date", 0, 10]},
                "home_team": "$home_team",
                "away_team": "$away_team",
                "league_cn": "$league_cn",
            },
            "docs": {"$push": {"_id": "$_id", "match_id": "$match_id"}},
            "count": {"$sum": 1},
        }},
        {"$match": {"count": {"$gt": 1}}},
    ]
    to_delete: list = []
    num_duplicate_groups = 0
    for g in coll.aggregate(pipeline):
        docs = g.get("docs") or []
        if len(docs) <= 1:
            continue
        num_duplicate_groups += 1
        def _key(d: dict) -> tuple:
            return (0 if d.get("match_id") is not None else 1, str(d.get("_id", "")))
        docs_sorted = sorted(docs, key=_key)
        keep_id = docs_sorted[0]["_id"]
        for d in docs:
            if d["_id"] != keep_id:
                to_delete.append(d["_id"])
    if not to_delete:
        return 0, 0
    if dry_run:
        return len(to_delete), num_duplicate_groups
    r = coll.delete_many({"_id": {"$in": to_delete}})
    return r.deleted_count, num_duplicate_groups


def dedup_match_odds_by_canonical(dry_run: bool = False) -> tuple[int, int]:
    """
    仅 Mongo：按「规范队名」去重。未设 match_id 的文档若经队名规范化后的键 (log_date, canon_home, canon_away, league_cn)
    已存在于「已设 match_id 的文档」中，则视为同一场比赛的重复（队名拼写不同），删除未关联的那条。
    返回 (删除的文档数, 涉及的联赛数/批次数)。
    """
    if not use_mongo():
        return 0, 0
    try:
        from etl.team_name_normalize import get_canonical_team_names_by_league, _resolve_to_canonical
    except Exception:
        return 0, 0
    db = _db()
    coll = db["match_odds"]
    # 已设 match_id 的文档的 (log_date, home_team, away_team, league_cn) 集合
    linked_keys: set[tuple[str, str, str, str]] = set()
    for doc in coll.find({"match_id": {"$exists": True, "$ne": None}}, {"log_date": 1, "home_team": 1, "away_team": 1, "league_cn": 1}):
        log_date = (doc.get("log_date") or "")[:10]
        if len(log_date) != 10:
            continue
        linked_keys.add((
            log_date,
            (doc.get("home_team") or "").strip(),
            (doc.get("away_team") or "").strip(),
            (doc.get("league_cn") or "").strip(),
        ))
    if not linked_keys:
        return 0, 0
    by_league = get_canonical_team_names_by_league()
    if not by_league:
        return 0, 0
    to_delete: list = []
    for doc in coll.find({"$or": [{"match_id": {"$exists": False}}, {"match_id": None}]}):
        log_date = (doc.get("log_date") or "")[:10]
        home = (doc.get("home_team") or "").strip()
        away = (doc.get("away_team") or "").strip()
        league_cn = (doc.get("league_cn") or "").strip()
        if not league_cn or len(log_date) != 10:
            continue
        canonical_list = by_league.get(league_cn)
        if not canonical_list:
            continue
        ch = _resolve_to_canonical(home, canonical_list)
        ca = _resolve_to_canonical(away, canonical_list)
        if not ch or not ca:
            continue
        key = (log_date, ch, ca, league_cn)
        if key in linked_keys:
            to_delete.append(doc["_id"])
    if not to_delete:
        return 0, 0
    if dry_run:
        return len(to_delete), 1
    r = coll.delete_many({"_id": {"$in": to_delete}})
    return r.deleted_count, 1


def match_odds_link_debug_samples(odds_limit: int = 12, matches_per_league: int = 5) -> tuple[list[dict], list[dict]]:
    """
    仅 Mongo：返回用于诊断「为何 link 为 0」的样本。
    返回 (match_odds 样本列表, matches 样本列表)，便于对比两边的 log_date/match_date、主客队名、league_cn。
    """
    if not use_mongo():
        return [], []
    db = _db()
    # 未关联的 match_odds：按 league_cn 各取几条，含 log_date, home_team, away_team, league_cn
    cursor_odds = db["match_odds"].find(
        {"$or": [{"match_id": {"$exists": False}}, {"match_id": None}]},
        {"log_date": 1, "home_team": 1, "away_team": 1, "league_cn": 1},
    ).limit(odds_limit * 3)
    by_league: dict[str, list[dict]] = {}
    for doc in cursor_odds:
        league_cn = (doc.get("league_cn") or "").strip()
        if not league_cn:
            continue
        if league_cn not in by_league:
            by_league[league_cn] = []
        if len(by_league[league_cn]) >= odds_limit:
            continue
        by_league[league_cn].append({
            "log_date": (doc.get("log_date") or "")[:10],
            "home_team": (doc.get("home_team") or "").strip(),
            "away_team": (doc.get("away_team") or "").strip(),
            "league_cn": league_cn,
        })
    sample_odds: list[dict] = []
    for v in by_league.values():
        sample_odds.extend(v)
    if not sample_odds:
        return [], []

    comp_list = ("Premier League", "La Liga", "Primera Division", "Primera División", "Serie A", "Bundesliga", "1. Bundesliga", "Ligue 1")
    league_map = {"Premier League": "英超", "La Liga": "西甲", "Primera Division": "西甲", "Primera División": "西甲",
                  "Serie A": "意甲", "Bundesliga": "德甲", "1. Bundesliga": "德甲", "Ligue 1": "法甲"}
    comp_ids = [c["id"] for c in db["competitions"].find({"name": {"$in": list(comp_list)}}, {"id": 1})]
    teams = {t["id"]: (t.get("name") or "").strip() for t in db["teams"].find({}, {"id": 1, "name": 1})}
    comps = {c["id"]: c.get("name") or "" for c in db["competitions"].find({}, {"id": 1, "name": 1})}

    # 取若干 matches（五大联赛），含 match_date, home_name, away_name, league_cn
    sample_matches: list[dict] = []
    seen_keys: set[tuple[str, str, str, str]] = set()
    for m in _matches_coll().find({"competition_id": {"$in": comp_ids}}, {"id": 1, "match_date": 1, "home_team_id": 1, "away_team_id": 1, "competition_id": 1}).sort("match_date", -1).limit(2000):
        date_prefix = (m.get("match_date") or "")[:10]
        if len(date_prefix) != 10:
            continue
        home_name = teams.get(m.get("home_team_id")) or ""
        away_name = teams.get(m.get("away_team_id")) or ""
        cname = comps.get(m.get("competition_id")) or ""
        league_cn = league_map.get(cname) or ""
        if not home_name or not away_name or not league_cn:
            continue
        key = (date_prefix, home_name, away_name, league_cn)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        sample_matches.append({
            "match_date": date_prefix,
            "home_team": home_name,
            "away_team": away_name,
            "league_cn": league_cn,
        })
        if len(sample_matches) >= matches_per_league * 5:
            break
    return sample_odds[:odds_limit], sample_matches[: matches_per_league * 5]


def match_odds_stats() -> dict | None:
    """
    仅 Mongo：统计 match_odds 文档数、已关联 match_id 数、去重后的比赛数，用于判断是否存在重复。
    返回 {"total_docs", "docs_with_match_id", "docs_without_match_id", "distinct_match_ids"} 或 None。
    """
    if not use_mongo():
        return None
    db = _db()
    total_docs = db["match_odds"].count_documents({})
    docs_with_match_id = db["match_odds"].count_documents({"match_id": {"$exists": True, "$ne": None}})
    distinct = db["match_odds"].distinct("match_id")
    distinct_match_ids = len([x for x in distinct if x is not None])
    return {
        "total_docs": total_docs,
        "docs_with_match_id": docs_with_match_id,
        "docs_without_match_id": total_docs - docs_with_match_id,
        "distinct_match_ids": distinct_match_ids,
    }


def matches_without_odds() -> tuple[list[dict], int, int]:
    """
    仅 Mongo：统计五大联赛中哪些比赛没有赔率（match_odds 中无该 match_id）。
    返回 (无赔率的比赛列表, 五大联赛总场次, 有赔率场次)。
    列表中每项为 {"match_id", "match_date", "home_name", "away_name", "league_cn"}。
    """
    if not use_mongo():
        return [], 0, 0
    db = _db()
    comp_list = ("Premier League", "La Liga", "Primera Division", "Primera División", "Serie A", "Bundesliga", "1. Bundesliga", "Ligue 1")
    league_map = {"Premier League": "英超", "La Liga": "西甲", "Primera Division": "西甲", "Primera División": "西甲",
                  "Serie A": "意甲", "Bundesliga": "德甲", "1. Bundesliga": "德甲", "Ligue 1": "法甲"}
    comp_ids = [c["id"] for c in db["competitions"].find({"name": {"$in": list(comp_list)}}, {"id": 1})]
    teams = {t["id"]: (t.get("name") or "").strip() for t in db["teams"].find({}, {"id": 1, "name": 1})}
    comps = {c["id"]: c.get("name") or "" for c in db["competitions"].find({}, {"id": 1, "name": 1})}
    odds_match_ids: set[int] = set()
    for doc in db["match_odds"].find({}, {"match_id": 1}):
        mid = doc.get("match_id")
        if mid is not None:
            odds_match_ids.add(int(mid))
    without: list[dict] = []
    total = 0
    for m in _matches_coll().find({"competition_id": {"$in": comp_ids}}, {"id": 1, "match_date": 1, "home_team_id": 1, "away_team_id": 1, "competition_id": 1}):
        total += 1
        mid = m.get("id")
        if mid is None:
            continue
        if mid in odds_match_ids:
            continue
        date_prefix = (m.get("match_date") or "")[:10]
        home_name = teams.get(m.get("home_team_id")) or ""
        away_name = teams.get(m.get("away_team_id")) or ""
        cname = comps.get(m.get("competition_id")) or ""
        league_cn = league_map.get(cname) or ""
        without.append({
            "match_id": mid,
            "match_date": date_prefix,
            "home_name": home_name,
            "away_name": away_name,
            "league_cn": league_cn,
        })
    return without, total, total - len(without)


def match_odds_load_by_match_id(match_id: int) -> dict | None:
    """仅 Mongo：按 match_id 取一条赔率（link-match-odds 回填后可用），返回 {spf_win, spf_draw, spf_loss, created_at, rqspf_*, ah_*} 等。created_at 供时效校验。"""
    if not use_mongo() or match_id is None:
        return None
    doc = _db()["match_odds"].find_one({"match_id": match_id})
    if not doc:
        return None
    out = {"spf_win": doc.get("spf_win"), "spf_draw": doc.get("spf_draw"), "spf_loss": doc.get("spf_loss")}
    if doc.get("created_at") is not None:
        out["created_at"] = doc.get("created_at")
    if doc.get("rqspf_handicap") is not None:
        out["rqspf_handicap"] = doc.get("rqspf_handicap")
        out["rqspf_win"] = doc.get("rqspf_win")
        out["rqspf_draw"] = doc.get("rqspf_draw")
        out["rqspf_loss"] = doc.get("rqspf_loss")
    if doc.get("ah_handicap") is not None:
        out["ah_handicap"] = doc.get("ah_handicap")
        out["ah_home"] = doc.get("ah_home")
        out["ah_away"] = doc.get("ah_away")
    return out


def match_odds_load_for_match(home_name: str, away_name: str, league_cn: str, date_prefix: str | None) -> dict | None:
    """按主客队+联赛+日期取一条赔率，返回 {spf_win, spf_draw, spf_loss, rqspf_*, ah_*} 或 None。"""
    if use_mongo():
        coll = _db()["match_odds"]
        q = {"home_team": home_name.strip(), "away_team": away_name.strip(), "league_cn": league_cn}
        if date_prefix:
            q["log_date"] = date_prefix[:10]
        doc = coll.find_one(q)
        if not doc:
            return None
        out = {"spf_win": doc.get("spf_win"), "spf_draw": doc.get("spf_draw"), "spf_loss": doc.get("spf_loss")}
        if doc.get("created_at") is not None:
            out["created_at"] = doc.get("created_at")
        if doc.get("rqspf_handicap") is not None:
            out["rqspf_handicap"] = doc.get("rqspf_handicap")
            out["rqspf_win"] = doc.get("rqspf_win")
            out["rqspf_draw"] = doc.get("rqspf_draw")
            out["rqspf_loss"] = doc.get("rqspf_loss")
        if doc.get("ah_handicap") is not None:
            out["ah_handicap"] = doc.get("ah_handicap")
            out["ah_home"] = doc.get("ah_home")
            out["ah_away"] = doc.get("ah_away")
        return out
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        q = "SELECT spf_win, spf_draw, spf_loss FROM match_odds WHERE home_team=:h AND away_team=:a AND league_cn=:l"
        params = {"h": home_name.strip(), "a": away_name.strip(), "l": league_cn}
        if date_prefix:
            q += " AND log_date = :d"
            params["d"] = date_prefix[:10]
        row = conn.execute(text(q), params).fetchone()
    return {"spf_win": row[0], "spf_draw": row[1], "spf_loss": row[2]} if row else None


# ----- Match lineups（上场人数 + 完整阵容）-----
def lineups_list_matches_to_fetch(
    *,
    years: list[int] | None = None,
    skip_existing: bool = True,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
) -> list[tuple[int, str]]:
    """
    仅 Mongo：返回待拉取阵容的 (match_id, source_id) 列表。
    - years: 只考虑这些年份的比赛（按 match_date 年）。
    - skip_existing: 若 True，排除 match_lineups 中已有记录的 match_id（断点续拉）。
    - date_from/date_to: YYYY-MM-DD，限定比赛日范围。
    - limit: 最多返回条数（按比赛日升序取，便于分批）。
    """
    if not use_mongo():
        return []
    db = _db()
    q: dict[str, Any] = {"source_id": {"$exists": True, "$nin": [None, ""]}}
    if date_from or date_to:
        q["match_date"] = {}
        if date_from:
            q["match_date"]["$gte"] = date_from[:10]
        if date_to:
            q["match_date"]["$lte"] = date_to[:10]
    cursor = _matches_coll().find(q, {"id": 1, "source_id": 1, "match_date": 1}).sort("match_date", 1)
    rows = list(cursor)
    if years:
        years_set = set(years)
        rows = [r for r in rows if r.get("match_date") and (r.get("match_date") or "")[:4].isdigit() and int((r.get("match_date") or "")[:4]) in years_set]
    if skip_existing and rows:
        have = set()
        for d in db["match_lineups"].find({}, {"match_id": 1}):
            mid = d.get("match_id")
            if mid is not None:
                have.add(mid)
        rows = [r for r in rows if r.get("id") not in have]
    out = []
    for r in rows:
        mid, sid = r.get("id"), r.get("source_id")
        if mid is not None and sid is not None and str(sid).strip():
            out.append((int(mid), str(sid).strip()))
    if limit is not None and limit > 0 and len(out) > limit:
        out = out[:limit]
    return out


def lineups_get_by_match_id(match_id: int) -> dict[str, Any] | None:
    """仅 Mongo：按 match_id 取阵容，返回 { home_starting_count, away_starting_count, home_starting, away_starting, ... } 或 None。"""
    if not use_mongo() or match_id is None:
        return None
    doc = _db()["match_lineups"].find_one({"match_id": match_id})
    if not doc:
        return None
    # 返回完整文档（含 count 与名单），调用方按需取用
    return dict(doc)


def lineups_save(
    match_id: int,
    home_starting_count: int | None = None,
    away_starting_count: int | None = None,
    home_starting: list[dict[str, Any]] | None = None,
    away_starting: list[dict[str, Any]] | None = None,
    home_substitutes: list[dict[str, Any]] | None = None,
    away_substitutes: list[dict[str, Any]] | None = None,
) -> None:
    """仅 Mongo：写入或更新 match_lineups（上场人数 + 完整首发/替补名单）。名单每项 {player_id, player_name, number}。"""
    if not use_mongo() or match_id is None:
        return
    coll = _db()["match_lineups"]
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
    existing = coll.find_one({"match_id": match_id})
    if existing:
        upd = {k: v for k, v in doc.items() if k != "match_id"}
        coll.update_one({"match_id": match_id}, {"$set": upd})
    else:
        coll.insert_one(doc)


# ----- Match injury -----
def injury_list_from_db() -> list[dict[str, Any]]:
    """从 match_injury 读取全部来源（API、公众号、migrate），同一场次优先保留 API；返回与 get_injury_matches_from_db 同构（仅 Mongo）。"""
    out: list[dict[str, Any]] = []
    if not use_mongo():
        return out
    seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for r in _db()["match_injury"].find({}):
        log_date = (r.get("log_date") or "")[:10]
        home = (r.get("home_team") or "").strip()
        away = (r.get("away_team") or "").strip()
        league = (r.get("league_cn") or "").strip()
        source = (r.get("source") or "").strip()
        if not league:
            continue
        try:
            ha = json.loads(r["home_absent_json"]) if r.get("home_absent_json") else []
            aa = json.loads(r["away_absent_json"]) if r.get("away_absent_json") else []
        except Exception:
            ha, aa = [], []
        item = {
            "home": home,
            "away": away,
            "league": league,
            "fixture_date": log_date,
            "home_absent": ha,
            "away_absent": aa,
        }
        key = (log_date, home, away, league)
        if key not in seen or source == "api_football":
            seen[key] = item
    return list(seen.values())


def injury_has_date(match_date: str) -> bool:
    """该比赛日（YYYY-MM-DD）在 match_injury 中是否已有任意一条伤停记录（任意来源）。用于判断是否需自动拉取。"""
    date_only = (match_date or "")[:10]
    if len(date_only) != 10:
        return False
    if use_mongo():
        return _db()["match_injury"].count_documents({"log_date": date_only}) > 0
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM match_injury WHERE log_date = :d LIMIT 1"),
            {"d": date_only},
        ).fetchone()
    return row is not None


def injury_load_from_db() -> dict[tuple[str, str, str], dict[str, Any]]:
    """(norm_home, norm_away, league_cn) -> { home_absent, away_absent }。全部来源，同一场优先 API。"""
    def _norm(s: str) -> str:
        s = (s or "").strip().lower().replace(" ", "")
        for suf in ("fc", "cf", "sc", "afc", "cfc", "utd", "united", "city", "hotspur", "spurs"):
            if len(s) > len(suf) and s.endswith(suf):
                s = s[:-len(suf)]
                break
        return s
    if use_mongo():
        out = {}
        for r in _db()["match_injury"].find({}):
            league = (r.get("league_cn") or "").strip()
            if not league:
                continue
            key = (_norm(r.get("home_team") or ""), _norm(r.get("away_team") or ""), league)
            source = (r.get("source") or "").strip()
            try:
                ha = json.loads(r["home_absent_json"]) if r.get("home_absent_json") else []
                aa = json.loads(r["away_absent_json"]) if r.get("away_absent_json") else []
            except Exception:
                ha, aa = [], []
            if key not in out or source == "api_football":
                out[key] = {"home_absent": ha, "away_absent": aa}
        return out
    from sqlalchemy import text
    from etl.schema import get_engine
    out = {}
    with get_engine().connect() as conn:
        for r in conn.execute(text("SELECT home_team, away_team, league_cn, home_absent_json, away_absent_json, source FROM match_injury")).fetchall():
            league = (r[2] or "").strip()
            if not league:
                continue
            key = (_norm(r[0] or ""), _norm(r[1] or ""), league)
            source = (r[5] or "").strip() if len(r) > 5 else ""
            try:
                ha = json.loads(r[3]) if r[3] else []
                aa = json.loads(r[4]) if r[4] else []
            except Exception:
                ha, aa = [], []
            if key not in out or source == "api_football":
                out[key] = {"home_absent": ha, "away_absent": aa}
    return out


def injury_save_to_db(matches: list[dict], source: str = "api_football") -> None:
    """批量写入/更新 match_injury。"""
    if not matches:
        return
    if use_mongo():
        coll = _db()["match_injury"]
        for m in matches:
            log_date = (m.get("fixture_date") or m.get("date") or "")[:10]
            home = (m.get("home") or "").strip() or "?"
            away = (m.get("away") or "").strip() or "?"
            league = (m.get("league") or "").strip()
            if not league:
                continue
            ha = m.get("home_absent") or []
            aa = m.get("away_absent") or []
            coll.update_one(
                {"log_date": log_date, "home_team": home, "away_team": away, "league_cn": league},
                {"$set": {"home_absent_json": json.dumps(ha, ensure_ascii=False), "away_absent_json": json.dumps(aa, ensure_ascii=False), "source": source, "updated_at": datetime.utcnow().isoformat() + "Z"}},
                upsert=True,
            )
        return
    from etl.injury_db import save_injury_to_db
    save_injury_to_db(matches, source=source)


def injury_delete_non_api() -> int:
    if use_mongo():
        r = _db()["match_injury"].delete_many({"source": {"$ne": "api_football"}})
        return r.deleted_count
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        r = conn.execute(text("DELETE FROM match_injury WHERE source != 'api_football'"))
        conn.commit()
        return r.rowcount


# ----- League standings -----
def standings_get_for_match(match_id: int) -> tuple[int | None, int | None]:
    """返回 (home_rank, away_rank) 或 (None, None)。"""
    if use_mongo():
        m = _matches_coll().find_one({"id": match_id})
        if not m:
            return (None, None)
        return (m.get("home_rank"), m.get("away_rank"))
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        r = conn.execute(text("SELECT home_rank, away_rank FROM matches WHERE id = :mid"), {"mid": match_id}).fetchone()
    return (r[0], r[1]) if r else (None, None)


def standings_update_match_ranks(match_id: int, home_rank: int | None, away_rank: int | None) -> None:
    if use_mongo():
        _matches_coll().update_one({"id": match_id}, {"$set": {"home_rank": home_rank, "away_rank": away_rank}})
        return
    from etl.schema import get_engine, Match
    from sqlalchemy.orm import Session
    with Session(get_engine()) as s:
        s.query(Match).filter(Match.id == match_id).update({"home_rank": home_rank, "away_rank": away_rank})
        s.commit()


# ----- Competition name (for predict) -----
def get_competition_name(competition_id: int | None) -> str | None:
    if competition_id is None:
        return None
    c = get_competition_by_id(competition_id)
    return (c.get("name") or "").strip() if c else None


# ----- Helpers for engine/predict.py: run SQL or Mongo equivalent -----
def predict_get_league_overrides(competition_id: int | None) -> dict:
    """根据 competition_id 取赛事名，再取 predict_league_params 中覆盖参数。"""
    name = get_competition_name(competition_id)
    if not name:
        return {}
    return config.get_league_overrides().get(name, {})


def predict_team_recent_form(team_id: int, competition_id: int | None, last_n: int, at_home: bool, use_time_weight: bool = False) -> tuple[float, float, int]:
    """主/客最近 last_n 场的 (场均进, 场均失, 场数)。competition_id 可过滤赛事。"""
    if use_mongo():
        db = _db()
        q = {"home_score": {"$ne": None}, "away_score": {"$ne": None}}
        q["home_team_id" if at_home else "away_team_id"] = team_id
        if competition_id is not None:
            q["competition_id"] = competition_id
        matches = list(_matches_coll().find(q).sort("match_date", -1).limit(last_n))
        if not matches:
            return (0.0, 0.0, 0)
        n = len(matches)
        if use_time_weight:
            weights = [1.0 + 0.5 * (1.0 - t / max(1, n)) for t in range(n)]
            w_sum = sum(weights)
            gf = sum((m["home_score"] if at_home else m["away_score"]) * weights[i] for i, m in enumerate(matches))
            ga = sum((m["away_score"] if at_home else m["home_score"]) * weights[i] for i, m in enumerate(matches))
            return (gf / w_sum, ga / w_sum, n)
        gf = sum(m["home_score"] if at_home else m["away_score"] for m in matches)
        ga = sum(m["away_score"] if at_home else m["home_score"] for m in matches)
        return (gf / n, ga / n, n)
    from sqlalchemy import text
    from etl.schema import get_engine
    col = "home_team_id" if at_home else "away_team_id"
    with get_engine().connect() as conn:
        rows = conn.execute(text(f"""
            SELECT match_date, id, home_score, away_score FROM matches
            WHERE {col} = :tid AND home_score IS NOT NULL AND away_score IS NOT NULL
            ORDER BY match_date DESC LIMIT :lim
        """), {"tid": team_id, "lim": last_n}).fetchall()
    if not rows:
        return (0.0, 0.0, 0)
    n = len(rows)
    if use_time_weight:
        weights = [1.0 + 0.5 * (1.0 - t / max(1, n)) for t in range(n)]
        w_sum = sum(weights)
        gf = sum((r[2] if at_home else r[3]) * weights[i] for i, r in enumerate(rows))
        ga = sum((r[3] if at_home else r[2]) * weights[i] for i, r in enumerate(rows))
        return (gf / w_sum, ga / w_sum, n)
    gf = sum(r[2] if at_home else r[3] for r in rows)
    ga = sum(r[3] if at_home else r[2] for r in rows)
    return (gf / n, ga / n, n)


def predict_get_teams_and_comp(match_id: int) -> tuple[str, str, str] | None:
    """(home_name, away_name, comp_name)。"""
    if use_mongo():
        m = _matches_coll().find_one({"id": match_id})
        if not m:
            return None
        teams = _db()["teams"]
        comps = _db()["competitions"]
        h = teams.find_one({"id": m["home_team_id"]})
        a = teams.find_one({"id": m["away_team_id"]})
        c = comps.find_one({"id": m["competition_id"]}) if m.get("competition_id") else None
        return ((h.get("name") or "").strip(), (a.get("name") or "").strip(), (c.get("name") or "").strip() if c else "")
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        r = conn.execute(text("SELECT ht.name, at.name, c.name FROM matches m JOIN teams ht ON m.home_team_id=ht.id JOIN teams at ON m.away_team_id=at.id LEFT JOIN competitions c ON m.competition_id=c.id WHERE m.id=:mid"), {"mid": match_id}).fetchone()
    return (r[0], r[1], r[2]) if r else None


def predict_get_standings_ranks(league_id: int, season: int, home_team_name: str, away_team_name: str) -> tuple[int | None, int | None]:
    """(home_rank, away_rank)。"""
    if use_mongo():
        coll = _db()["league_standings"]
        hr = coll.find_one({"league_id": league_id, "season": season, "team_name": home_team_name.strip()})
        ar = coll.find_one({"league_id": league_id, "season": season, "team_name": away_team_name.strip()})
        return (hr.get("rank") if hr else None, ar.get("rank") if ar else None)
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        hr = conn.execute(text("SELECT rank FROM league_standings WHERE league_id=:lid AND season=:s AND team_name=:t"), {"lid": league_id, "s": season, "t": home_team_name.strip()}).fetchone()
        ar = conn.execute(text("SELECT rank FROM league_standings WHERE league_id=:lid AND season=:s AND team_name=:t"), {"lid": league_id, "s": season, "t": away_team_name.strip()}).fetchone()
    return (hr[0] if hr else None, ar[0] if ar else None)


def predict_team_recent_form_mixed(team_id: int, competition_id: int | None, last_n: int, use_time_weight: bool = False) -> tuple[float, float]:
    """该队最近 last_n 场（主客混合）场均进、场均失。"""
    if use_mongo():
        db = _db()
        q = {"home_score": {"$ne": None}, "away_score": {"$ne": None}, "$or": [{"home_team_id": team_id}, {"away_team_id": team_id}]}
        if competition_id is not None:
            q["competition_id"] = competition_id
        matches = list(_matches_coll().find(q).sort("match_date", -1).limit(last_n * 2))
        combined = []
        for m in matches:
            md = m.get("match_date") or ""
            mid = m.get("id")
            if m.get("home_team_id") == team_id:
                combined.append((md, mid, m["home_score"], m["away_score"]))
            else:
                combined.append((md, mid, m["away_score"], m["home_score"]))
        combined.sort(key=lambda x: (x[0], x[1]), reverse=True)
        recent = combined[:last_n]
        if not recent:
            return (0.0, 0.0)
        n = len(recent)
        if use_time_weight:
            weights = [1.0 + 0.5 * (1.0 - t / max(1, n)) for t in range(n)]
            w_sum = sum(weights)
            gf = sum(r[2] * weights[i] for i, r in enumerate(recent))
            ga = sum(r[3] * weights[i] for i, r in enumerate(recent))
            return (gf / w_sum, ga / w_sum)
        gf = sum(r[2] for r in recent)
        ga = sum(r[3] for r in recent)
        return (gf / n, ga / n)
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        home_rows = conn.execute(text("SELECT match_date, id, home_score, away_score FROM matches WHERE home_team_id=:tid AND home_score IS NOT NULL AND (:cid IS NULL OR competition_id=:cid)"), {"tid": team_id, "cid": competition_id}).fetchall()
        away_rows = conn.execute(text("SELECT match_date, id, home_score, away_score FROM matches WHERE away_team_id=:tid AND home_score IS NOT NULL AND (:cid IS NULL OR competition_id=:cid)"), {"tid": team_id, "cid": competition_id}).fetchall()
    combined = [(r[0], r[1], r[2], r[3]) for r in home_rows] + [(r[0], r[1], r[3], r[2]) for r in away_rows]
    combined.sort(key=lambda x: (x[0] or "", x[1]), reverse=True)
    recent = combined[:last_n]
    if not recent:
        return (0.0, 0.0)
    n = len(recent)
    if use_time_weight:
        weights = [1.0 + 0.5 * (1.0 - t / max(1, n)) for t in range(n)]
        w_sum = sum(weights)
        return (sum(r[2] * weights[i] for i, r in enumerate(recent)) / w_sum, sum(r[3] * weights[i] for i, r in enumerate(recent)) / w_sum)
    return (sum(r[2] for r in recent) / n, sum(r[3] for r in recent) / n)


def predict_league_avg_goals(competition_id: int | None, last_n: int = 50) -> tuple[float, float]:
    """联赛近期场均主队进、客队进。"""
    if use_mongo():
        db = _db()
        q = {"home_score": {"$ne": None}, "away_score": {"$ne": None}}
        if competition_id is not None:
            q["competition_id"] = competition_id
        rows = list(_matches_coll().find(q).sort("match_date", -1).limit(last_n))
        if not rows:
            return (1.0, 1.0)
        n = len(rows)
        return (sum(m["home_score"] for m in rows) / n, sum(m["away_score"] for m in rows) / n)
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        if competition_id is None:
            rows = conn.execute(text("SELECT home_score, away_score FROM matches WHERE home_score IS NOT NULL AND away_score IS NOT NULL ORDER BY match_date DESC, id DESC LIMIT :lim"), {"lim": last_n}).fetchall()
        else:
            rows = conn.execute(text("SELECT home_score, away_score FROM matches WHERE competition_id=:cid AND home_score IS NOT NULL ORDER BY match_date DESC, id DESC LIMIT :lim"), {"cid": competition_id, "lim": last_n}).fetchall()
    if not rows:
        return (1.0, 1.0)
    n = len(rows)
    return (sum(r[0] for r in rows) / n, sum(r[1] for r in rows) / n)


def predict_head_to_head(home_id: int, away_id: int, competition_id: int | None, last_n: int) -> tuple[float, float, float, float, int]:
    """(主队场均进, 主队场均失, 客队场均进, 客队场均失, 场数)。"""
    if use_mongo():
        db = _db()
        q = {"home_team_id": home_id, "away_team_id": away_id, "home_score": {"$ne": None}, "away_score": {"$ne": None}}
        if competition_id is not None:
            q["competition_id"] = competition_id
        rows = list(_matches_coll().find(q).sort("match_date", -1).limit(last_n))
        if not rows:
            return (0.0, 0.0, 0.0, 0.0, 0)
        n = len(rows)
        hf = sum(m["home_score"] for m in rows) / n
        ha = sum(m["away_score"] for m in rows) / n
        return (hf, ha, ha, hf, n)
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        rows = conn.execute(text("SELECT home_score, away_score FROM matches WHERE home_team_id=:hid AND away_team_id=:aid AND home_score IS NOT NULL AND (:cid IS NULL OR competition_id=:cid) ORDER BY match_date DESC LIMIT :lim"), {"hid": home_id, "aid": away_id, "cid": competition_id, "lim": last_n}).fetchall()
    if not rows:
        return (0.0, 0.0, 0.0, 0.0, 0)
    n = len(rows)
    return (sum(r[0] for r in rows) / n, sum(r[1] for r in rows) / n, sum(r[1] for r in rows) / n, sum(r[0] for r in rows) / n, n)


def predict_recent_form_string(team_id: int, competition_id: int | None, last_n: int, at_home: bool) -> str:
    """该队最近 last_n 场（主/客）的胜平负字符串，如 'WWDLW'。最近一场在左。"""
    if use_mongo():
        db = _db()
        col = "home_team_id" if at_home else "away_team_id"
        q = {"home_score": {"$ne": None}, "away_score": {"$ne": None}, col: team_id}
        if competition_id is not None:
            q["competition_id"] = competition_id
        matches = list(_matches_coll().find(q).sort("match_date", -1).limit(last_n))
        chars = []
        for m in matches:
            my = m["home_score"] if at_home else m["away_score"]
            opp = m["away_score"] if at_home else m["home_score"]
            if my > opp:
                chars.append("W")
            elif my < opp:
                chars.append("L")
            else:
                chars.append("D")
        return "".join(chars)
    from sqlalchemy import text
    from etl.schema import get_engine
    col = "home_team_id" if at_home else "away_team_id"
    with get_engine().connect() as conn:
        rows = conn.execute(text(f"""
            SELECT home_score, away_score FROM matches
            WHERE {col} = :tid AND home_score IS NOT NULL AND away_score IS NOT NULL
            AND (:cid IS NULL OR competition_id = :cid)
            ORDER BY match_date DESC LIMIT :lim
        """), {"tid": team_id, "cid": competition_id, "lim": last_n}).fetchall()
    chars = []
    for r in rows:
        my = r[0] if at_home else r[1]
        opp = r[1] if at_home else r[0]
        chars.append("W" if my > opp else ("L" if my < opp else "D"))
    return "".join(chars)


def predict_h2h_matches_list(home_id: int, away_id: int, competition_id: int | None, last_n: int) -> list[dict]:
    """主客队最近 last_n 场交锋，返回 [{"date": "YYYY-MM-DD", "home": str, "away": str, "score": "x-y"}, ...]。"""
    if use_mongo():
        db = _db()
        q = {"home_team_id": home_id, "away_team_id": away_id, "home_score": {"$ne": None}, "away_score": {"$ne": None}}
        if competition_id is not None:
            q["competition_id"] = competition_id
        rows = list(_matches_coll().find(q).sort("match_date", -1).limit(last_n))
        teams = {t["id"]: (t.get("name") or "", (t.get("name_zh") or "").strip()) for t in db["teams"].find({}, {"id": 1, "name": 1, "name_zh": 1})}
        out = []
        for m in rows:
            ht = teams.get(m["home_team_id"]) or ("", "")
            at = teams.get(m["away_team_id"]) or ("", "")
            hname, hzh = ht[0] or "", ht[1] or ""
            aname, azh = at[0] or "", at[1] or ""
            out.append({
                "date": (m.get("match_date") or "")[:10],
                "home": hname,
                "away": aname,
                "home_cn": hzh,
                "away_cn": azh,
                "score": f"{m['home_score']}-{m['away_score']}",
            })
        return out
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT m.match_date, ht.name, at.name, m.home_score, m.away_score,
                   ht.name_zh, at.name_zh
            FROM matches m
            JOIN teams ht ON m.home_team_id = ht.id JOIN teams at ON m.away_team_id = at.id
            WHERE m.home_team_id = :hid AND m.away_team_id = :aid AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            AND (:cid IS NULL OR m.competition_id = :cid)
            ORDER BY m.match_date DESC LIMIT :lim
        """), {"hid": home_id, "aid": away_id, "cid": competition_id, "lim": last_n}).fetchall()
    return [
        {
            "date": (r[0] or "")[:10],
            "home": (r[1] or "").strip(),
            "away": (r[2] or "").strip(),
            "home_cn": (r[5] or "").strip() if len(r) > 5 else "",
            "away_cn": (r[6] or "").strip() if len(r) > 6 else "",
            "score": f"{r[3]}-{r[4]}",
        }
        for r in rows
    ]


def predict_days_since_last_match(team_id: int, before_date: str | None, competition_id: int | None) -> int | None:
    """该队在 before_date 之前最近一场距今天数；无则 None。"""
    if not before_date or len(before_date) < 10:
        return None
    bd = before_date[:10]
    if use_mongo():
        db = _db()
        q = {"$or": [{"home_team_id": team_id}, {"away_team_id": team_id}], "home_score": {"$ne": None}, "match_date": {"$lt": bd}}
        if competition_id is not None:
            q["competition_id"] = competition_id
        m = _matches_coll().find_one(q, sort=[("match_date", -1)], projection=["match_date"])
        if not m or not m.get("match_date"):
            return None
        from datetime import datetime
        d0 = datetime.strptime((m["match_date"] or "")[:10], "%Y-%m-%d")
        d1 = datetime.strptime(bd, "%Y-%m-%d")
        return (d1 - d0).days
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT match_date FROM matches WHERE (home_team_id=:tid OR away_team_id=:tid) AND home_score IS NOT NULL AND (:cid IS NULL OR competition_id=:cid) AND match_date < :bd ORDER BY match_date DESC LIMIT 1"), {"tid": team_id, "cid": competition_id, "bd": bd}).fetchone()
    if not row or not row[0]:
        return None
    from datetime import datetime
    d0 = datetime.strptime((row[0] or "")[:10], "%Y-%m-%d")
    d1 = datetime.strptime(bd, "%Y-%m-%d")
    return (d1 - d0).days


def predict_team_recent_matches_with_source(
    team_id: int,
    before_date: str | None,
    competition_id: int | None,
    last_n: int = 10,
) -> list[dict[str, Any]]:
    """
    该队在 before_date 之前最近 last_n 场已完赛比赛，含 source_id（用于按 fixture 拉取统计/xG）。
    返回 [{"id", "source_id", "home_team_id", "away_team_id"}, ...]，按 match_date 降序。
    """
    if not before_date or len(before_date) < 10:
        return []
    bd = before_date[:10]
    if use_mongo():
        q = {"$or": [{"home_team_id": team_id}, {"away_team_id": team_id}], "home_score": {"$ne": None}, "match_date": {"$lt": bd}}
        if competition_id is not None:
            q["competition_id"] = competition_id
        cursor = _matches_coll().find(
            q,
            {"id": 1, "source_id": 1, "home_team_id": 1, "away_team_id": 1},
        ).sort("match_date", -1).limit(last_n)
        return [{"id": m["id"], "source_id": m.get("source_id"), "home_team_id": m.get("home_team_id"), "away_team_id": m.get("away_team_id")} for m in cursor]
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT id, source_id, home_team_id, away_team_id FROM matches
            WHERE (home_team_id = :tid OR away_team_id = :tid) AND home_score IS NOT NULL
            AND (:cid IS NULL OR competition_id = :cid) AND match_date < :bd
            ORDER BY match_date DESC LIMIT :lim
        """), {"tid": team_id, "cid": competition_id, "bd": bd, "lim": last_n}).fetchall()
    return [{"id": r[0], "source_id": r[1], "home_team_id": r[2], "away_team_id": r[3]} for r in rows]


def predict_count_team_matches_in_range(
    team_id: int,
    date_from: str,
    date_to: str,
    competition_id: int | None = None,
) -> int:
    """
    该队在 [date_from, date_to] 区间内的已排期比赛场次数（含未开赛），用于赛程密度特征。
    date_from/date_to 为 YYYY-MM-DD；区间为左闭右闭。
    """
    if not date_from or not date_to or len(date_from) < 10 or len(date_to) < 10:
        return 0
    df, dt = date_from[:10], date_to[:10]
    if use_mongo():
        q = {"$or": [{"home_team_id": team_id}, {"away_team_id": team_id}], "match_date": {"$gte": df, "$lte": dt}}
        if competition_id is not None:
            q["competition_id"] = competition_id
        return _matches_coll().count_documents(q)
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        r = conn.execute(text("""
            SELECT COUNT(*) FROM matches
            WHERE (home_team_id = :tid OR away_team_id = :tid) AND match_date >= :df AND match_date <= :dt
            AND (:cid IS NULL OR competition_id = :cid)
        """), {"tid": team_id, "df": df, "dt": dt, "cid": competition_id}).scalar()
    return int(r) if r is not None else 0


def predict_get_match_ids_without_predictions(years: list[int] | None = None, limit: int | None = None) -> list[int]:
    """已完赛且尚无预测的 match_id 列表。"""
    if use_mongo():
        db = _db()
        q = {"home_score": {"$ne": None}, "away_score": {"$ne": None}, "home_team_id": {"$ne": None}, "away_team_id": {"$ne": None}}
        if years:
            q["$or"] = [{"match_date": {"$regex": f"^{y}-"}} for y in years]
        cursor = _matches_coll().find(q, {"id": 1}).sort("match_date", 1)
        if limit:
            cursor = cursor.limit(limit)
        ids = [m["id"] for m in cursor]
        have = set(db["predictions"].distinct("match_id", {"match_id": {"$in": ids}}))
        return [i for i in ids if i not in have]
    from sqlalchemy import text
    from etl.schema import get_engine
    year_filter = ""
    if years:
        year_filter = " AND strftime('%Y', m.match_date) IN (" + ",".join(repr(str(y)) for y in years) + ")"
    with get_engine().connect() as conn:
        rows = conn.execute(text(f"SELECT m.id FROM matches m WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL AND m.home_team_id IS NOT NULL AND m.away_team_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM predictions p WHERE p.match_id = m.id) {year_filter} ORDER BY m.match_date" + (f" LIMIT {int(limit)}" if limit else ""))).fetchall()
    return [r[0] for r in rows]


def predict_get_match_ids_with_predictions() -> list[int]:
    """已有预测且已完赛的 match_id 列表（用于 repredict）。"""
    if use_mongo():
        db = _db()
        pred_ids = set(db["predictions"].distinct("match_id"))
        matches = _matches_coll().find({"id": {"$in": list(pred_ids)}, "home_score": {"$ne": None}, "away_score": {"$ne": None}}, {"id": 1})
        return [m["id"] for m in matches]
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        rows = conn.execute(text("SELECT DISTINCT p.match_id FROM predictions p JOIN matches m ON m.id = p.match_id WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL")).fetchall()
    return [r[0] for r in rows]


def predict_get_match_info(match_id: int) -> dict | None:
    """(home_team_id, away_team_id, competition_id, match_date)。"""
    if use_mongo():
        m = _matches_coll().find_one({"id": match_id}, {"home_team_id": 1, "away_team_id": 1, "competition_id": 1, "match_date": 1})
        return m
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        r = conn.execute(text("SELECT home_team_id, away_team_id, competition_id, match_date FROM matches WHERE id = :mid"), {"mid": match_id}).fetchone()
    return {"home_team_id": r[0], "away_team_id": r[1], "competition_id": r[2], "match_date": (r[3] or "")[:10] if r and r[3] else None} if r else None


def predict_get_mongo_matches_for_predict(
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
    skip_with_predictions: bool = True,
) -> list[dict]:
    """
    从 MongoDB matches 表中取待预测比赛（五大联赛，含主客队名、联赛英文名）。
    仅 USE_MONGO 时有效；用于「对 MongoDB 中的比赛做模型预测」。
    date_from/date_to: 可选，YYYY-MM-DD，闭区间过滤比赛日。
    limit: 最多返回场数。
    skip_with_predictions: 为 True 时排除已有 predictions 记录的 match_id。
    返回 list[dict]，每项含 id, home_team_id, away_team_id, competition_id, match_date, home_name, away_name, competition_name。
    """
    if not use_mongo():
        return []
    db = _db()
    comp_list = ("Premier League", "La Liga", "Primera Division", "Primera División", "Serie A", "Bundesliga", "1. Bundesliga", "Ligue 1")
    comp_ids = [c["id"] for c in db["competitions"].find({"name": {"$in": list(comp_list)}}, {"id": 1})]
    q = {"competition_id": {"$in": comp_ids}, "home_team_id": {"$ne": None}, "away_team_id": {"$ne": None}}
    if date_from or date_to:
        q["match_date"] = {}
        if date_from:
            q["match_date"]["$gte"] = (date_from or "")[:10]
        if date_to:
            q["match_date"]["$lte"] = (date_to or "")[:10]
    cursor = _matches_coll().find(q, {"id": 1, "home_team_id": 1, "away_team_id": 1, "competition_id": 1, "match_date": 1}).sort("match_date", 1)
    if limit:
        cursor = cursor.limit(limit)
    match_list = list(cursor)
    if not match_list:
        return []
    if skip_with_predictions:
        ids = [m["id"] for m in match_list]
        have = set(db["predictions"].distinct("match_id", {"match_id": {"$in": ids}}))
        match_list = [m for m in match_list if m["id"] not in have]
    if not match_list:
        return []
    teams = {t["id"]: (t.get("name") or "").strip() for t in db["teams"].find({"id": {"$in": [m["home_team_id"] for m in match_list] + [m["away_team_id"] for m in match_list]}}, {"id": 1, "name": 1})}
    comps = {c["id"]: (c.get("name") or "").strip() for c in db["competitions"].find({"id": {"$in": comp_ids}}, {"id": 1, "name": 1})}
    out = []
    for m in match_list:
        out.append({
            "id": m["id"],
            "home_team_id": m["home_team_id"],
            "away_team_id": m["away_team_id"],
            "competition_id": m.get("competition_id"),
            "match_date": (m.get("match_date") or "")[:10],
            "home_name": teams.get(m["home_team_id"]) or "",
            "away_name": teams.get(m["away_team_id"]) or "",
            "competition_name": comps.get(m.get("competition_id")) or "Premier League",
        })
    return out


def predict_find_match_id_by_teams_and_date(date_str: str, home_name: str, away_name: str) -> int | None:
    """按比赛日 + 主客队名查 match_id（用于 Mongo 下 prediction_log 关联 predictions 表）。"""
    date_only = (date_str or "")[:10]
    if not date_only or len(date_only) != 10:
        return None
    home_name = (home_name or "").strip()
    away_name = (away_name or "").strip()
    if not home_name or not away_name:
        return None
    if use_mongo():
        db = _db()
        teams = {t["id"]: (t.get("name") or "").strip() for t in db["teams"].find({}, {"id": 1, "name": 1})}
        for m in _matches_coll().find({"match_date": {"$regex": f"^{date_only}"}}, {"id": 1, "home_team_id": 1, "away_team_id": 1}):
            h = teams.get(m.get("home_team_id")) or ""
            a = teams.get(m.get("away_team_id")) or ""
            if (h.lower() == home_name.lower() and a.lower() == away_name.lower()):
                return m.get("id")
        return None
    from sqlalchemy import text
    from etl.schema import get_engine
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT m.id, ht.name, at.name FROM matches m JOIN teams ht ON m.home_team_id = ht.id JOIN teams at ON m.away_team_id = at.id WHERE date(m.match_date) = :d"),
            {"d": date_only},
        ).fetchall()
    for r in rows:
        if (r[1] or "").strip().lower() == home_name.lower() and (r[2] or "").strip().lower() == away_name.lower():
            return r[0]
    return None


# ---------- 投注花费与收益（bet_records）----------

def bet_record_add(
    record_date: str,
    amount_spent: float,
    amount_returned: float = 0.0,
    note: str | None = None,
) -> int:
    """新增一条投注记录，返回 id。同一天已存在记录时则累加花费到该条，不新增行（合并到一起）。"""
    date_only = (record_date or "").strip()[:10]
    if not date_only:
        raise ValueError("record_date 需为 YYYY-MM-DD")
    amount_spent = float(amount_spent)
    amount_returned = float(amount_returned) if amount_returned is not None else 0.0
    if use_mongo():
        coll = _db()["bet_records"]
        existing = coll.find_one({"record_date": date_only})
        if existing:
            coll.update_one(
                {"id": existing["id"]},
                {"$inc": {"amount_spent": amount_spent}},
            )
            return existing["id"]
        doc = {
            "id": _next_id(coll),
            "record_date": date_only,
            "amount_spent": amount_spent,
            "amount_returned": amount_returned,
            "note": (note or "").strip() or None,
            "created_at": datetime.utcnow(),
        }
        coll.insert_one(doc)
        return doc["id"]
    from etl.schema import get_engine, BetRecord
    from sqlalchemy.orm import Session
    eng = get_engine()
    with Session(eng) as s:
        row = s.query(BetRecord).filter(BetRecord.record_date == date_only).first()
        if row:
            row.amount_spent = float(row.amount_spent or 0) + amount_spent
            s.commit()
            s.refresh(row)
            return row.id
        new_row = BetRecord(
            record_date=date_only,
            amount_spent=amount_spent,
            amount_returned=amount_returned,
            note=(note or "").strip() or None,
        )
        s.add(new_row)
        s.commit()
        s.refresh(new_row)
        return new_row.id


def bet_record_list(
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """按日期范围列出投注记录，每项含 id, record_date, amount_spent, amount_returned, note, created_at。"""
    if use_mongo():
        coll = _db()["bet_records"]
        q = {}
        if date_from and len(date_from) >= 10:
            q.setdefault("record_date", {})["$gte"] = date_from[:10]
        if date_to and len(date_to) >= 10:
            q.setdefault("record_date", {})["$lte"] = date_to[:10]
        cursor = coll.find(q).sort("record_date", -1).sort("id", -1)
        out = []
        for doc in cursor:
            out.append({
                "id": doc.get("id"),
                "record_date": (doc.get("record_date") or "")[:10],
                "amount_spent": float(doc.get("amount_spent", 0)),
                "amount_returned": float(doc.get("amount_returned", 0)),
                "note": doc.get("note"),
                "created_at": doc.get("created_at"),
            })
        return out
    from sqlalchemy import text
    from etl.schema import get_engine
    eng = get_engine()
    q = "SELECT id, record_date, amount_spent, amount_returned, note, created_at FROM bet_records"
    params = {}
    cond = []
    if date_from and len(date_from) >= 10:
        cond.append("record_date >= :date_from")
        params["date_from"] = date_from[:10]
    if date_to and len(date_to) >= 10:
        cond.append("record_date <= :date_to")
        params["date_to"] = date_to[:10]
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY record_date DESC, id DESC"
    with eng.connect() as conn:
        rows = conn.execute(text(q), params).fetchall()
    return [
        {
            "id": r[0],
            "record_date": (r[1] or "")[:10],
            "amount_spent": float(r[2] or 0),
            "amount_returned": float(r[3] or 0),
            "note": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]


def bet_record_update(
    record_id: int,
    amount_returned: float | None = None,
    note: str | None = None,
) -> bool:
    """更新一条投注记录的收益或备注（用于次日或开奖后补填收益）。返回是否更新成功。"""
    if use_mongo():
        coll = _db()["bet_records"]
        upd: dict[str, Any] = {}
        if amount_returned is not None:
            upd["amount_returned"] = float(amount_returned)
        if note is not None:
            upd["note"] = (note.strip() or None) if isinstance(note, str) else None
        if not upd:
            return True
        res = coll.update_one({"id": record_id}, {"$set": upd})
        return res.modified_count > 0 or res.matched_count > 0
    from sqlalchemy import text
    from etl.schema import get_engine
    eng = get_engine()
    updates = []
    params: dict[str, Any] = {"id": record_id}
    if amount_returned is not None:
        updates.append("amount_returned = :amount_returned")
        params["amount_returned"] = float(amount_returned)
    if note is not None:
        updates.append("note = :note")
        params["note"] = (note.strip() or None) if isinstance(note, str) else None
    if not updates:
        return True
    q = "UPDATE bet_records SET " + ", ".join(updates) + " WHERE id = :id"
    with eng.connect() as conn:
        res = conn.execute(text(q), params)
        conn.commit()
        return res.rowcount > 0


def bet_record_stats(
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """汇总指定日期范围内的总花费、总收益、盈亏、条数。"""
    rows = bet_record_list(date_from=date_from, date_to=date_to)
    total_spent = sum(r["amount_spent"] for r in rows)
    total_returned = sum(r["amount_returned"] for r in rows)
    return {
        "count": len(rows),
        "total_spent": round(total_spent, 2),
        "total_returned": round(total_returned, 2),
        "profit": round(total_returned - total_spent, 2),
    }
