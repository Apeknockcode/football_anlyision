"""
API-FOOTBALL 积分榜拉取：按联赛+赛季拉取 standings，写入 data/api_football_standings.json，供预测时排名特征使用。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from etl.injury_api_football import LEAGUE_IDS, _request

STANDINGS_JSON_PATH: Path = config.DATA_DIR / "api_football_standings.json"
RATE_LIMIT_SEC = getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
# 联赛中文名 -> API-Football league_id（与 LEAGUE_IDS 反向）
LEAGUE_CN_TO_ID = {v: k for k, v in LEAGUE_IDS.items()}


def fetch_standings(league_id: int, season: int, rate_limit_sec: float = RATE_LIMIT_SEC) -> list[dict[str, Any]]:
    """
    拉取某联赛某赛季积分榜。
    返回 [{"rank", "team_id", "team_name", "points", "goalsDiff", "form"}, ...]。
    """
    data = _request("standings", {"league": league_id, "season": season})
    time.sleep(rate_limit_sec)
    rows = data.get("response") or []
    if not rows:
        return []
    # response 为 [ { league: { standings: [ [ { rank, team, points, goalsDiff, form } ] } } ] ] 或旧版 [ { league, standings } ]
    out = []
    for league_block in rows:
        league_obj = league_block.get("league") or league_block
        standings = league_obj.get("standings") or league_block.get("standings") or []
        for group in standings:
            rows_in_group = group if isinstance(group, list) else [group]
            for row in rows_in_group:
                rank = row.get("rank") or row.get("position")
                team = row.get("team") or {}
                team_id = team.get("id")
                team_name = (team.get("name") or "").strip()
                points = row.get("points")
                goals_diff = row.get("goalsDiff")
                form = (row.get("form") or "").strip()
                out.append({
                    "rank": rank,
                    "team_id": team_id,
                    "team_name": team_name,
                    "points": points,
                    "goalsDiff": goals_diff,
                    "form": form,
                })
    return out


def fetch_all_big5_standings(season: int, rate_limit_sec: float = RATE_LIMIT_SEC) -> dict[int, list[dict[str, Any]]]:
    """拉取五大联赛当季积分榜，返回 league_id -> list of standing row。"""
    by_league: dict[int, list[dict[str, Any]]] = {}
    for league_id in LEAGUE_IDS:
        rows = fetch_standings(league_id, season, rate_limit_sec)
        if rows:
            by_league[league_id] = rows
    return by_league


def migrate_standings_json_to_db() -> int:
    """将 data/api_football_standings.json 一次性导入 league_standings 表，返回导入条数。"""
    if not STANDINGS_JSON_PATH.is_file():
        return 0
    try:
        data = json.loads(STANDINGS_JSON_PATH.read_text(encoding="utf-8"))
        season = data.get("season")
        by_league = data.get("by_league") or {}
        if season is None or not by_league:
            return 0
        by_league_int = {int(k): v for k, v in by_league.items() if str(k).isdigit()}
        _save_standings_to_db(season, by_league_int)
        return sum(len(v) for v in by_league_int.values())
    except Exception:
        return 0


def _save_standings_to_mongo(season: int, by_league: dict[int, list[dict[str, Any]]]) -> None:
    """将积分榜写入 MongoDB league_standings 集合。"""
    if not by_league:
        return
    from db import repo
    if not repo.use_mongo():
        return
    db = repo._db()
    coll = db["league_standings"]
    now = datetime.utcnow()
    for league_id, rows in by_league.items():
        coll.delete_many({"league_id": league_id, "season": season})
        for row in rows:
            coll.insert_one({
                "league_id": league_id,
                "season": season,
                "team_name": (row.get("team_name") or "").strip() or "?",
                "rank": row.get("rank"),
                "points": row.get("points"),
                "goals_diff": row.get("goalsDiff"),
                "form": (row.get("form") or "").strip() or None,
                "updated_at": now,
            })


def _save_standings_to_db(season: int, by_league: dict[int, list[dict[str, Any]]]) -> None:
    """将积分榜写入 league_standings 表（SQLite）。"""
    if not by_league:
        return
    try:
        import config
        from sqlalchemy import text
        from etl.schema import get_engine, init_db
        if not config.DB_PATH.exists():
            return
        engine = get_engine()
        init_db(engine)
        with engine.connect() as conn:
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            for league_id, rows in by_league.items():
                conn.execute(text("DELETE FROM league_standings WHERE league_id = :lid AND season = :s"), {"lid": league_id, "s": season})
                for row in rows:
                    conn.execute(
                        text("""
                            INSERT INTO league_standings (league_id, season, team_name, rank, points, goals_diff, form, updated_at)
                            VALUES (:lid, :season, :team_name, :rank, :points, :goals_diff, :form, :updated_at)
                        """),
                        {
                            "lid": league_id,
                            "season": season,
                            "team_name": (row.get("team_name") or "").strip() or "?",
                            "rank": row.get("rank"),
                            "points": row.get("points"),
                            "goals_diff": row.get("goalsDiff"),
                            "form": (row.get("form") or "").strip() or None,
                            "updated_at": now,
                        },
                    )
            conn.commit()
    except Exception as e:
        import warnings
        warnings.warn(f"league_standings 写入失败: {e}", stacklevel=2)
        raise


def save_standings(season: int, by_league: dict[int, list[dict[str, Any]]]) -> Path:
    """写入 league_standings（SQLite 或 MongoDB，不写本地文件）。"""
    try:
        from db import repo
        if repo.use_mongo():
            _save_standings_to_mongo(season, by_league)
            return STANDINGS_JSON_PATH
    except Exception:
        pass
    _save_standings_to_db(season, by_league)
    return STANDINGS_JSON_PATH


def fetch_and_save_standings(season: int, rate_limit_sec: float = RATE_LIMIT_SEC) -> tuple[dict[int, list], str]:
    """
    拉取五大联赛当季积分榜并保存。
    返回 (by_league, message)。
    """
    by_league = fetch_all_big5_standings(season, rate_limit_sec)
    save_standings(season, by_league)
    total = sum(len(v) for v in by_league.values())
    msg = f"积分榜已更新：赛季 {season}，五大联赛共 {len(by_league)} 个联赛、{total} 条排名，已写入 DB（league_standings）"
    return by_league, msg


def load_standings(season: int | None = None) -> dict[int, list[dict[str, Any]]]:
    """
    读取积分榜，供预测时排名特征使用。USE_MONGO 时从 MongoDB 读，否则从 SQLite 或 JSON 读。
    返回 league_id -> list of standing row。season 不传时（从 JSON 回退）用 DB 中最大 season 或文件中的 season。
    """
    try:
        from db import repo
        if repo.use_mongo():
            db = repo._db()
            coll = db["league_standings"]
            if season is None:
                doc = coll.find_one(sort=[("season", -1)], projection=["season"])
                season = doc["season"] if doc and doc.get("season") is not None else None
            if season is None:
                return {}
            rows = list(coll.find({"season": season}).sort([("league_id", 1), ("rank", 1)]))
            if rows:
                by_league: dict[int, list[dict[str, Any]]] = {}
                for r in rows:
                    lid = int(r.get("league_id", 0))
                    if lid not in by_league:
                        by_league[lid] = []
                    by_league[lid].append({
                        "team_name": (r.get("team_name") or "").strip(),
                        "rank": r.get("rank"),
                        "points": r.get("points"),
                        "goalsDiff": r.get("goals_diff"),
                        "form": (r.get("form") or "").strip() or "",
                    })
                return by_league
            return {}
    except Exception:
        pass
    try:
        import config
        from sqlalchemy import text
        from etl.schema import get_engine, init_db
        if config.DB_PATH.exists():
            engine = get_engine()
            init_db(engine)
            with engine.connect() as conn:
                if season is not None:
                    rows = conn.execute(
                        text("SELECT league_id, team_name, rank, points, goals_diff, form FROM league_standings WHERE season = :s ORDER BY league_id, rank"),
                        {"s": season},
                    ).fetchall()
                else:
                    s = conn.execute(text("SELECT MAX(season) FROM league_standings")).scalar()
                    if s is None:
                        return {}
                    rows = conn.execute(
                        text("SELECT league_id, team_name, rank, points, goals_diff, form FROM league_standings WHERE season = :s ORDER BY league_id, rank"),
                        {"s": s},
                    ).fetchall()
                if rows:
                    by_league: dict[int, list[dict[str, Any]]] = {}
                    for r in rows:
                        lid = int(r[0])
                        if lid not in by_league:
                            by_league[lid] = []
                        by_league[lid].append({
                            "team_name": r[1],
                            "rank": r[2],
                            "points": r[3],
                            "goalsDiff": r[4],
                            "form": r[5] or "",
                        })
                    return by_league
    except Exception:
        pass
    return {}


def get_team_rank(standings_list: list[dict[str, Any]], team_name: str) -> int | None:
    """
    根据队名在积分榜列表中查找排名（1-based）。
    队名做宽松匹配（含或包含）。
    """
    if not standings_list or not (team_name or "").strip():
        return None
    team_norm = (team_name or "").strip().lower().replace(" ", "")
    for row in standings_list:
        tn = (row.get("team_name") or "").strip().lower().replace(" ", "")
        if team_norm == tn or team_norm in tn or tn in team_norm:
            r = row.get("rank")
            if r is not None:
                return int(r)
    return None


# DB 中赛事名 -> 五大联赛中文（与 api_football_odds 一致，用于按 DB 比赛回填积分/排名）
def _db_competition_to_league_cn() -> dict[str, str]:
    try:
        from etl.api_football_odds import DB_COMPETITION_TO_LEAGUE_CN
        return DB_COMPETITION_TO_LEAGUE_CN
    except Exception:
        return {
            "Premier League": "英超",
            "La Liga": "西甲",
            "Primera Division": "西甲",
            "Primera División": "西甲",
            "Serie A": "意甲",
            "Bundesliga": "德甲",
            "1. Bundesliga": "德甲",
            "Ligue 1": "法甲",
        }


def _season_year_from_season_name(season_name: str) -> int | None:
    """从 seasons.name（如 2023/2024）解析出 API-FOOTBALL 赛季年（起始年，如 2023）。"""
    if not (season_name or "").strip():
        return None
    parts = (season_name or "").strip().split("/")
    if not parts:
        return None
    try:
        return int(parts[0].strip())
    except ValueError:
        return None


def _ensure_match_rank_columns(conn: Any) -> None:
    """确保 matches 表存在 home_rank, away_rank 列（SQLite 无 CREATE IF NOT EXISTS for column）。"""
    from sqlalchemy import text
    try:
        info = conn.execute(text("PRAGMA table_info(matches)")).fetchall()
        cols = {row[1] for row in info}
        if "home_rank" not in cols:
            conn.execute(text("ALTER TABLE matches ADD COLUMN home_rank INTEGER"))
        if "away_rank" not in cols:
            conn.execute(text("ALTER TABLE matches ADD COLUMN away_rank INTEGER"))
        conn.commit()
    except Exception:
        pass


def backfill_standings_for_db_matches(
    rate_limit_sec: float = RATE_LIMIT_SEC,
    seasons: list[int] | None = None,
) -> tuple[list[int], str]:
    """
    按 DB 中五大联赛比赛的赛季，拉取历史积分榜并写入 league_standings（SQLite 或 MongoDB）。
    若未传 seasons，则从 matches 中解析出所有涉及的赛季年并拉取。
    返回 (已拉取赛季列表, 说明信息)。
    """
    try:
        from db import repo
        if repo.use_mongo():
            comp_map = _db_competition_to_league_cn()
            comp_names = list(comp_map.keys())
            db = repo._db()
            comp_docs = list(db["competitions"].find({"name": {"$in": comp_names}}, {"id": 1}))
            comp_ids = [c["id"] for c in comp_docs]
            if not comp_ids:
                return [], "Mongo 中无五大联赛赛事，跳过。"
            to_fetch: list[int] = list(seasons) if seasons else []
            if not to_fetch:
                season_docs = list(db["seasons"].find({}, {"id": 1, "name": 1}))
                match_comp_season = set()
                for m in db["matches"].find({"competition_id": {"$in": comp_ids}}, {"season_id": 1}):
                    sid = m.get("season_id")
                    if sid is not None:
                        match_comp_season.add(sid)
                for s in season_docs:
                    if s["id"] in match_comp_season:
                        y = _season_year_from_season_name(s.get("name") or "")
                        if y is not None and y not in to_fetch:
                            to_fetch.append(y)
                to_fetch.sort()
            if not to_fetch:
                return [], "Mongo 中无五大联赛比赛或无法解析赛季，未拉取。"
            done: list[int] = []
            for season in to_fetch:
                try:
                    fetch_and_save_standings(season, rate_limit_sec=rate_limit_sec)
                    done.append(season)
                except Exception:
                    pass
            msg = f"已拉取并写入 league_standings：赛季 {done}，共 {len(done)} 个赛季。"
            return done, msg
    except Exception:
        pass

    import config
    from sqlalchemy import text
    from etl.schema import get_engine, init_db

    if not config.DB_PATH.exists():
        return [], "DB 不存在，跳过。"

    comp_map = _db_competition_to_league_cn()
    comp_names = list(comp_map.keys())

    engine = get_engine()
    init_db(engine)
    to_fetch: list[int] = list(seasons) if seasons else []
    if not to_fetch:
        with engine.connect() as conn:
            placeholders = ", ".join(f":c{i}" for i in range(len(comp_names)))
            params: dict[str, Any] = {f"c{i}": n for i, n in enumerate(comp_names)}
            rows = conn.execute(
                text(f"""
                    SELECT DISTINCT s.name
                    FROM matches m
                    JOIN competitions c ON m.competition_id = c.id
                    JOIN seasons s ON m.season_id = s.id
                    WHERE c.name IN ({placeholders})
                """),
                params,
            ).fetchall()
        for (name,) in rows:
            y = _season_year_from_season_name(name)
            if y is not None and y not in to_fetch:
                to_fetch.append(y)
        to_fetch.sort()

    if not to_fetch:
        return [], "DB 中无五大联赛比赛或无法解析赛季，未拉取。"

    done: list[int] = []
    for season in to_fetch:
        try:
            fetch_and_save_standings(season, rate_limit_sec=rate_limit_sec)
            done.append(season)
        except Exception as e:
            # 单季失败不中断，继续下一季
            pass
    msg = f"已拉取并写入 league_standings：赛季 {done}，共 {len(done)} 个赛季。"
    return done, msg


def backfill_match_ranks() -> tuple[int, str]:
    """
    根据 league_standings 为 DB 中五大联赛比赛回填 home_rank, away_rank（支持 SQLite 与 MongoDB）。
    依赖：league_standings 中已有对应赛季数据（可先运行 backfill_standings_for_db_matches）。
    返回 (更新场次数, 说明信息)。
    """
    try:
        from db import repo
        if repo.use_mongo():
            comp_map = _db_competition_to_league_cn()
            comp_names = list(comp_map.keys())
            db = repo._db()
            comp_docs = list(db["competitions"].find({"name": {"$in": comp_names}}, {"id": 1, "name": 1}))
            comp_ids = [c["id"] for c in comp_docs]
            comp_id_to_name = {c["id"]: c.get("name") or "" for c in comp_docs}
            if not comp_ids:
                return 0, "Mongo 中无五大联赛赛事，跳过。"
            season_docs = list(db["seasons"].find({}, {"id": 1, "name": 1}))
            season_id_to_name = {s["id"]: s.get("name") or "" for s in season_docs}
            matches = list(db["matches"].find({"competition_id": {"$in": comp_ids}}))
            updated = 0
            for m in matches:
                match_id = m.get("id")
                comp_name = comp_id_to_name.get(m.get("competition_id")) or ""
                league_cn = comp_map.get(comp_name)
                if not league_cn:
                    continue
                league_id = LEAGUE_CN_TO_ID.get(league_cn)
                if not league_id:
                    continue
                season_name = season_id_to_name.get(m.get("season_id")) or ""
                season_year = _season_year_from_season_name(season_name)
                if season_year is None:
                    continue
                by_league = load_standings(season_year)
                st_list = by_league.get(league_id) or []
                if not st_list:
                    continue
                home_name = away_name = None
                raw_meta = m.get("raw_meta")
                if raw_meta:
                    try:
                        meta = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
                        home_name = (meta.get("home_team_name") or meta.get("home_team") or "").strip()
                        away_name = (meta.get("away_team_name") or meta.get("away_team") or "").strip()
                    except Exception:
                        pass
                if not home_name or not away_name:
                    for tid, key in [(m.get("home_team_id"), "home"), (m.get("away_team_id"), "away")]:
                        if tid is None:
                            continue
                        t = db["teams"].find_one({"id": tid}, {"name": 1})
                        name = (t.get("name") or "").strip() if t else ""
                        if key == "home":
                            home_name = home_name or name
                        else:
                            away_name = away_name or name
                if not home_name and not away_name:
                    continue
                home_rank = get_team_rank(st_list, home_name) if home_name else None
                away_rank = get_team_rank(st_list, away_name) if away_name else None
                if home_rank is None and away_rank is None:
                    continue
                repo.standings_update_match_ranks(match_id, home_rank, away_rank)
                updated += 1
            return updated, f"已为 {updated} 场五大联赛比赛回填主客队排名（home_rank, away_rank）。"
    except Exception:
        pass

    import json
    import config
    from sqlalchemy import text
    from etl.schema import get_engine, init_db

    if not config.DB_PATH.exists():
        return 0, "DB 不存在，跳过。"

    comp_map = _db_competition_to_league_cn()
    engine = get_engine()
    init_db(engine)

    with engine.connect() as conn:
        _ensure_match_rank_columns(conn)
        # 五大联赛比赛：m.id, competition_name, season_name, home_team_id, away_team_id, match_date
        comp_names_list = list(comp_map.keys())
        placeholders = ", ".join(f":c{i}" for i in range(len(comp_names_list)))
        params_sel: dict[str, Any] = {f"c{i}": n for i, n in enumerate(comp_names_list)}
        rows = conn.execute(
            text(f"""
                SELECT m.id, c.name AS comp_name, s.name AS season_name,
                       m.home_team_id, m.away_team_id, m.raw_meta
                FROM matches m
                JOIN competitions c ON m.competition_id = c.id
                JOIN seasons s ON m.season_id = s.id
                WHERE c.name IN ({placeholders})
            """),
            params_sel,
        ).fetchall()

        updated = 0
        for r in rows:
            match_id, comp_name, season_name, home_team_id, away_team_id, raw_meta = (
                r[0], r[1], r[2], r[3], r[4], r[5]
            )
            league_cn = comp_map.get(comp_name)
            if not league_cn:
                continue
            league_id = LEAGUE_CN_TO_ID.get(league_cn)
            if not league_id:
                continue
            season_year = _season_year_from_season_name(season_name)
            if season_year is None:
                continue
            by_league = load_standings(season_year)
            st_list = by_league.get(league_id) or []
            if not st_list:
                continue
            home_name = away_name = None
            if raw_meta:
                try:
                    meta = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
                    home_name = (meta.get("home_team_name") or meta.get("home_team") or "").strip()
                    away_name = (meta.get("away_team_name") or meta.get("away_team") or "").strip()
                except Exception:
                    pass
            if not home_name or not away_name:
                if home_team_id and away_team_id:
                    team_rows = conn.execute(
                        text("SELECT id, name FROM teams WHERE id IN (:hid, :aid)"),
                        {"hid": home_team_id, "aid": away_team_id},
                    ).fetchall()
                    by_id = {row[0]: (row[1] or "").strip() for row in team_rows}
                    home_name = home_name or by_id.get(home_team_id) or ""
                    away_name = away_name or by_id.get(away_team_id) or ""
            if not home_name and not away_name:
                continue
            home_rank = get_team_rank(st_list, home_name) if home_name else None
            away_rank = get_team_rank(st_list, away_name) if away_name else None
            if home_rank is None and away_rank is None:
                continue
            conn.execute(
                text("UPDATE matches SET home_rank = :hr, away_rank = :ar WHERE id = :mid"),
                {"hr": home_rank, "ar": away_rank, "mid": match_id},
            )
            updated += 1
        conn.commit()

    return updated, f"已为 {updated} 场五大联赛比赛回填主客队排名（home_rank, away_rank）。"
