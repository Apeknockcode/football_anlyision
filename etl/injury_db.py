"""
伤停数据 DB 读写：match_injury 表。拉取/合并写入 JSON 时同步写入 DB；预测时优先从 DB 读。
展示与预测会读取全部来源（API、公众号、migrate），同一场次优先采用 API 数据。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

import config

INJURY_JSON_PATH = config.DATA_DIR / "injury_suspension.json"

# 同一场次多来源时优先使用 API 来源
INJURY_SOURCE_API = "api_football"


def migrate_injury_json_to_db(batch_size: int = 3000) -> int:
    """将 data/injury_suspension.json 一次性导入 match_injury 表，返回导入条数。文件大时分批写入。"""
    if not INJURY_JSON_PATH.is_file():
        return 0
    try:
        data = json.loads(INJURY_JSON_PATH.read_text(encoding="utf-8"))
        matches = data.get("matches") or []
    except Exception:
        return 0
    if not matches:
        return 0
    total = 0
    for i in range(0, len(matches), batch_size):
        chunk = matches[i : i + batch_size]
        save_injury_to_db(chunk, source="migrate")
        total += len(chunk)
    return total


def _norm_team_key(name: str) -> str:
    s = (name or "").strip().lower().replace(" ", "")
    for suffix in ("fc", "cf", "sc", "cf", "afc", "cfc", "utd", "united", "city", "hotspur", "spurs"):
        if len(s) > len(suffix) and s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s.replace(" ", "").lower()


def save_injury_to_db(matches: list[dict[str, Any]], source: str = "api_football") -> None:
    """将伤停场次列表写入 match_injury 表（与 injury_suspension.json 同源）。"""
    if not matches:
        return
    try:
        from db import repo
        if repo.use_mongo():
            repo.injury_save_to_db(matches, source=source)
            return
        import config
        from etl.schema import get_engine, init_db
        if not config.DB_PATH.exists():
            return
        engine = get_engine()
        init_db(engine)
        with engine.connect() as conn:
            for m in matches:
                log_date = (m.get("fixture_date") or m.get("date") or "")[:10]
                home = (m.get("home") or "").strip() or "?"
                away = (m.get("away") or "").strip() or "?"
                league = (m.get("league") or "").strip()
                if not league:
                    continue
                # 非 API 来源不得覆盖已有 API 数据
                if source != INJURY_SOURCE_API:
                    existing = conn.execute(
                        text("SELECT 1 FROM match_injury WHERE log_date=:d AND home_team=:h AND away_team=:a AND league_cn=:lg AND source=:src"),
                        {"d": log_date or "0000-00-00", "h": home, "a": away, "lg": league, "src": INJURY_SOURCE_API},
                    ).fetchone()
                    if existing:
                        continue
                home_absent = m.get("home_absent") or []
                away_absent = m.get("away_absent") or []
                conn.execute(
                    text("""
                        UPDATE match_injury SET home_absent_json=:haj, away_absent_json=:aaj, source=:src, updated_at=CURRENT_TIMESTAMP
                        WHERE log_date=:d AND home_team=:h AND away_team=:a AND league_cn=:lg
                    """),
                    {
                        "d": log_date or "0000-00-00",
                        "h": home,
                        "a": away,
                        "lg": league,
                        "haj": json.dumps(home_absent, ensure_ascii=False),
                        "aaj": json.dumps(away_absent, ensure_ascii=False),
                        "src": source,
                    },
                )
                if conn.execute(text("SELECT changes()")).scalar() == 0:
                    conn.execute(
                        text("""
                            INSERT INTO match_injury (log_date, home_team, away_team, league_cn, home_absent_json, away_absent_json, source, updated_at)
                            VALUES (:d, :h, :a, :lg, :haj, :aaj, :src, CURRENT_TIMESTAMP)
                        """),
                        {
                            "d": log_date or "0000-00-00",
                            "h": home,
                            "a": away,
                            "lg": league,
                            "haj": json.dumps(home_absent, ensure_ascii=False),
                            "aaj": json.dumps(away_absent, ensure_ascii=False),
                            "src": source,
                        },
                    )
            conn.commit()
    except Exception:
        pass


def get_injury_matches_from_db() -> list[dict[str, Any]]:
    """从 match_injury 表/集合加载全部场次（含 API、公众号、migrate），同一场次优先保留 API。返回与 injury_suspension.json matches 同结构。"""
    try:
        from db import repo
        if repo.use_mongo():
            return repo.injury_list_from_db()
    except Exception:
        pass
    # SQLite：读全部来源，按 (log_date, home, away, league) 去重，优先保留 api_football
    seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    try:
        import config
        from etl.schema import get_engine, init_db
        if not config.DB_PATH.exists():
            return []
        engine = get_engine()
        init_db(engine)
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT log_date, home_team, away_team, league_cn, home_absent_json, away_absent_json, source
                    FROM match_injury
                """),
            ).fetchall()
        for r in rows:
            log_date = (r[0] or "")[:10]
            home = (r[1] or "").strip()
            away = (r[2] or "").strip()
            league = (r[3] or "").strip()
            source = (r[6] or "").strip()
            if not league:
                continue
            key = (log_date, home, away, league)
            try:
                ha = json.loads(r[4]) if r[4] else []
                aa = json.loads(r[5]) if r[5] else []
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
            if key not in seen or source == INJURY_SOURCE_API:
                seen[key] = item
        return list(seen.values())
    except Exception:
        return []


def load_injury_from_db() -> dict[tuple[str, str, str], dict[str, Any]]:
    """
    从 match_injury 表加载伤停（含 API、公众号、migrate），同一 (主客队+联赛) 优先保留 API。
    返回 (norm_home, norm_away, league_cn) -> { home_absent, away_absent }。
    """
    try:
        from db import repo
        if repo.use_mongo():
            return repo.injury_load_from_db()
    except Exception:
        pass
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    try:
        import config
        from etl.schema import get_engine, init_db
        if not config.DB_PATH.exists():
            return out
        engine = get_engine()
        init_db(engine)
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT log_date, home_team, away_team, league_cn, home_absent_json, away_absent_json, source
                    FROM match_injury
                """),
            ).fetchall()
        for r in rows:
            league = (r[3] or "").strip()
            if not league:
                continue
            key = (_norm_team_key(r[1] or ""), _norm_team_key(r[2] or ""), league)
            source = (r[6] or "").strip()
            try:
                ha = json.loads(r[4]) if r[4] else []
                aa = json.loads(r[5]) if r[5] else []
            except Exception:
                ha, aa = [], []
            payload = {"home_absent": ha, "away_absent": aa}
            if key not in out or source == INJURY_SOURCE_API:
                out[key] = payload
        return out
    except Exception:
        return out


def delete_injury_non_api_rows() -> int:
    """删除 match_injury 中 source != api_football 的条数（公众号 OCR、migrate 等），仅保留 API 数据。返回删除行数。"""
    try:
        from db import repo
        if repo.use_mongo():
            return repo.injury_delete_non_api()
        import config
        from etl.schema import get_engine, init_db
        if not config.DB_PATH.exists():
            return 0
        engine = get_engine()
        init_db(engine)
        with engine.connect() as conn:
            result = conn.execute(
                text("DELETE FROM match_injury WHERE source != :src"),
                {"src": INJURY_SOURCE_API},
            )
            conn.commit()
            return result.rowcount
    except Exception:
        return 0
