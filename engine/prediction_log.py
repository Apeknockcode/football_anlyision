"""
预测结果统计与复盘：将每日预测写入 prediction_log 表（DB），完赛后填写实际比分并生成复盘报告。
默认仅用 DB，不创建或读写 prediction_log.json。旧数据可用 prediction-log migrate-from-json 一次性导入。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

import config
from engine.predict import predict_match, probs_from_exp_goals, resolve_competition_by_name, resolve_team_by_name, save_prediction


PREDICTION_LOG_PATH = config.DATA_DIR / "prediction_log.json"


def _use_db() -> bool:
    return getattr(config, "PREDICTION_LOG_USE_DB", True)


def _row_to_entry(row: tuple) -> dict[str, Any]:
    """将 prediction_log 表的一行转为与 JSON 一致的 entry dict。"""
    # id, match_id, log_date, home_team, away_team, competition, exp_goals_home, exp_goals_away,
    # prob_home_win, prob_draw, prob_away_win, predicted_result, last_n,
    # actual_home_score, actual_away_score, actual_result, confidence, odds_used, predicted_scores_json
    return {
        "id": row[0],
        "match_id": row[1],
        "date": (row[2] or "")[:10],
        "home_team": row[3] or "",
        "away_team": row[4] or "",
        "competition": row[5] or "",
        "exp_goals_home": float(row[6]) if row[6] is not None else 0.0,
        "exp_goals_away": float(row[7]) if row[7] is not None else 0.0,
        "prob_home_win": float(row[8]) if row[8] is not None else 0.0,
        "prob_draw": float(row[9]) if row[9] is not None else 0.0,
        "prob_away_win": float(row[10]) if row[10] is not None else 0.0,
        "predicted_result": row[11] or "平",
        "last_n": int(row[12]) if row[12] is not None else 10,
        "actual_home_score": row[13],
        "actual_away_score": row[14],
        "actual_result": row[15],
        "confidence": row[16] or "high",
        "odds_used": bool(row[17]) if row[17] is not None else False,
        "predicted_scores": json.loads(row[18]) if row[18] else [[0, 0]],
    }


def _migrate_json_to_db(engine: Engine) -> int:
    """将现有 prediction_log.json 导入到 prediction_log 表，返回导入条数。"""
    p = PREDICTION_LOG_PATH
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = data.get("predictions", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    except Exception:
        return 0
    if not entries:
        return 0
    from etl.schema import init_db, PredictionLog

    init_db(engine)
    n = 0
    for e in entries:
        try:
            log_date = (e.get("date") or "")[:10]
            if not log_date:
                continue
            pred_scores = e.get("predicted_scores") or [[0, 0]]
            row = PredictionLog(
                match_id=None,
                log_date=log_date,
                home_team=(e.get("home_team") or "").strip() or "?",
                away_team=(e.get("away_team") or "").strip() or "?",
                competition=(e.get("competition") or "").strip(),
                exp_goals_home=float(e.get("exp_goals_home", 0)),
                exp_goals_away=float(e.get("exp_goals_away", 0)),
                prob_home_win=float(e.get("prob_home_win", 0)),
                prob_draw=float(e.get("prob_draw", 0)),
                prob_away_win=float(e.get("prob_away_win", 0)),
                predicted_result=(e.get("predicted_result") or "平").strip(),
                last_n=int(e.get("last_n", 10)),
                actual_home_score=e.get("actual_home_score"),
                actual_away_score=e.get("actual_away_score"),
                actual_result=e.get("actual_result"),
                confidence=(e.get("confidence") or "high").strip(),
                odds_used=bool(e.get("odds_used", False)),
                predicted_scores_json=json.dumps(pred_scores[:2], ensure_ascii=False),
            )
            from sqlalchemy.orm import Session
            with Session(engine) as session:
                session.add(row)
                session.commit()
                n += 1
        except Exception:
            pass
    return n


def migrate_json_to_db(engine: Engine) -> int:
    """将 data/prediction_log.json 一次性导入到 prediction_log 表，返回导入条数。仅在有旧 JSON 文件时需执行。"""
    return _migrate_json_to_db(engine)


INJURY_JSON_PATH = config.DATA_DIR / "injury_suspension.json"

# 赛事英文名 -> 伤停 JSON 中的联赛中文（与 api/app 一致，用于复盘时匹配伤停）
COMPETITION_TO_LEAGUE_CN: dict[str, str] = {
    "Premier League": "英超",
    "Primera División": "西甲",
    "Serie A": "意甲",
    "Bundesliga": "德甲",
    "Ligue 1": "法甲",
}


def _norm_team_key(name: str) -> str:
    """队名规范化，与 API-FOOTBALL 英文队名匹配用。"""
    s = (name or "").strip()
    for suffix in (" FC", " CF", " AFC", " United", " City"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s.replace(" ", "").lower()


def _load_injury_by_teams() -> dict[tuple[str, str, str], dict]:
    """加载伤停，返回 (norm_home, norm_away, league_cn) -> { home_absent, away_absent }。仅从 DB（match_injury）读。"""
    try:
        from etl.injury_db import load_injury_from_db
        return load_injury_from_db()
    except Exception:
        return {}


def _match_team_names(a: str, b: str) -> bool:
    """精确匹配队名：strip 后忽略大小写相等（用于 match 与 prediction 主客队对应）。"""
    x, y = (a or "").strip(), (b or "").strip()
    if not x or not y:
        return False
    return x.lower() == y.lower()


def find_match_id_by_teams_and_date(
    engine: Engine | None,
    home_team: str,
    away_team: str,
    match_date: str,
) -> int | None:
    """按比赛日 + 主客队名在 matches 表中查找 match_id；无则返回 None。engine 为 None 时走 repo（Mongo）。"""
    from db import repo
    if engine is None or repo.use_mongo():
        return repo.predict_find_match_id_by_teams_and_date((match_date or "")[:10], home_team, away_team)
    date_str = (match_date or "")[:10]
    if not date_str:
        return None
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, raw_meta FROM matches WHERE match_date = :d AND raw_meta IS NOT NULL"),
            {"d": date_str},
        ).fetchall()
    for row in rows:
        match_id, raw = row[0], row[1]
        if not raw:
            continue
        try:
            meta = json.loads(raw)
        except Exception:
            continue
        h = (meta.get("home_team_name") or "").strip()
        a = (meta.get("away_team_name") or "").strip()
        if _match_team_names(home_team, h) and _match_team_names(away_team, a):
            return match_id
    return None


def _prediction_exists_for_match(engine: Engine | None, match_id: int) -> bool:
    """predictions 表是否已有该 match_id 的记录。"""
    from db import repo
    if engine is None or repo.use_mongo():
        return repo.predictions_exists_for_match(match_id)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM predictions WHERE match_id = :mid LIMIT 1"),
            {"mid": match_id},
        ).fetchone()
    return row is not None


def _log_path() -> Path:
    return PREDICTION_LOG_PATH


def load_log(engine: Engine | None = None) -> list[dict[str, Any]]:
    """加载预测日志。PREDICTION_LOG_USE_DB 时仅从 prediction_log 表读，不读写 JSON 文件。"""
    if _use_db():
        from db import repo
        if repo.use_mongo():
            entries = repo.prediction_log_list_entries()
            if entries:
                by_key: dict[tuple[str, str, str], dict] = {}
                for e in entries:
                    k = ((e.get("date") or "")[:10], (e.get("home_team") or "").strip(), (e.get("away_team") or "").strip())
                    if k not in by_key or (e.get("match_id") is not None and by_key[k].get("match_id") is None):
                        by_key[k] = e
                out = list(by_key.values())
                out.sort(key=lambda x: (x.get("date") or "", x.get("id", 0)))
                return out
            return []
        if config.DB_PATH.exists():
            from etl.schema import get_engine, init_db
            eng = engine or get_engine()
            init_db(eng)
            with eng.connect() as conn:
                try:
                    rows = conn.execute(
                        text("""
                            SELECT id, match_id, log_date, home_team, away_team, competition,
                                   exp_goals_home, exp_goals_away, prob_home_win, prob_draw, prob_away_win,
                                   predicted_result, last_n, actual_home_score, actual_away_score, actual_result,
                                   confidence, odds_used, predicted_scores_json
                            FROM prediction_log ORDER BY log_date, id
                        """)
                    ).fetchall()
                except Exception:
                    rows = []
                entries = [_row_to_entry(r) for r in rows]
                if entries:
                    by_key = {}
                    for e in entries:
                        k = ((e.get("date") or "")[:10], (e.get("home_team") or "").strip(), (e.get("away_team") or "").strip())
                        if k not in by_key or (e.get("match_id") is not None and by_key[k].get("match_id") is None):
                            by_key[k] = e
                    out = list(by_key.values())
                    out.sort(key=lambda x: (x.get("date") or "", x.get("id", 0)))
                    return out
                return []
    # 仅当 PREDICTION_LOG_USE_DB=false 时读 JSON（不推荐，建议统一用 DB）
    p = _log_path()
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        entries = data.get("predictions", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for i, e in enumerate(entries):
            if "id" not in e:
                e["id"] = i
        return entries
    except Exception:
        return []


def save_log(entries: list[dict[str, Any]], engine: Engine | None = None) -> None:
    """保存预测日志。PREDICTION_LOG_USE_DB 时按 id 更新或插入 prediction_log 表。"""
    if _use_db():
        from db import repo
        if repo.use_mongo():
            for e in entries:
                entry = {
                    "date": (e.get("date") or "")[:10],
                    "home_team": (e.get("home_team") or "").strip() or "?",
                    "away_team": (e.get("away_team") or "").strip() or "?",
                    "competition": (e.get("competition") or "").strip(),
                    "exp_goals_home": float(e.get("exp_goals_home", 0)),
                    "exp_goals_away": float(e.get("exp_goals_away", 0)),
                    "prob_home_win": float(e.get("prob_home_win", 0)),
                    "prob_draw": float(e.get("prob_draw", 0)),
                    "prob_away_win": float(e.get("prob_away_win", 0)),
                    "predicted_result": (e.get("predicted_result") or "平").strip(),
                    "last_n": int(e.get("last_n", 10)),
                    "actual_home_score": e.get("actual_home_score"),
                    "actual_away_score": e.get("actual_away_score"),
                    "actual_result": e.get("actual_result"),
                    "confidence": (e.get("confidence") or "high").strip(),
                    "odds_used": bool(e.get("odds_used", False)),
                    "predicted_scores": e.get("predicted_scores") or [[0, 0]],
                }
                if e.get("id") is not None:
                    repo.prediction_log_update_by_id(e["id"], entry)
                else:
                    add_entry = dict(entry)
                    add_entry["log_date"] = (e.get("date") or "")[:10]
                    add_entry["predicted_scores_json"] = json.dumps((e.get("predicted_scores") or [[0, 0]])[:2], ensure_ascii=False)
                    repo.prediction_log_add(add_entry)
            return
        if config.DB_PATH.exists():
            from etl.schema import get_engine, init_db, PredictionLog
            from sqlalchemy.orm import Session
            eng = engine or get_engine()
            init_db(eng)
            with Session(eng) as session:
                for e in entries:
                    pred_scores = e.get("predicted_scores") or [[0, 0]]
                    ps_json = json.dumps(pred_scores[:2], ensure_ascii=False)
                    row_data = {
                        "log_date": (e.get("date") or "")[:10],
                        "home_team": (e.get("home_team") or "").strip() or "?",
                        "away_team": (e.get("away_team") or "").strip() or "?",
                        "competition": (e.get("competition") or "").strip(),
                        "exp_goals_home": float(e.get("exp_goals_home", 0)),
                        "exp_goals_away": float(e.get("exp_goals_away", 0)),
                        "prob_home_win": float(e.get("prob_home_win", 0)),
                        "prob_draw": float(e.get("prob_draw", 0)),
                        "prob_away_win": float(e.get("prob_away_win", 0)),
                        "predicted_result": (e.get("predicted_result") or "平").strip(),
                        "last_n": int(e.get("last_n", 10)),
                        "actual_home_score": e.get("actual_home_score"),
                        "actual_away_score": e.get("actual_away_score"),
                        "actual_result": e.get("actual_result"),
                        "confidence": (e.get("confidence") or "high").strip(),
                        "odds_used": bool(e.get("odds_used", False)),
                        "predicted_scores_json": ps_json,
                    }
                    if e.get("id") is not None:
                        session.execute(
                            text("""
                                UPDATE prediction_log SET
                                    log_date=:log_date, home_team=:home_team, away_team=:away_team, competition=:competition,
                                    exp_goals_home=:exp_goals_home, exp_goals_away=:exp_goals_away,
                                    prob_home_win=:prob_home_win, prob_draw=:prob_draw, prob_away_win=:prob_away_win,
                                    predicted_result=:predicted_result, last_n=:last_n,
                                    actual_home_score=:actual_home_score, actual_away_score=:actual_away_score, actual_result=:actual_result,
                                    confidence=:confidence, odds_used=:odds_used, predicted_scores_json=:predicted_scores_json
                                WHERE id=:id
                            """),
                            {**row_data, "id": e["id"]},
                        )
                    else:
                        row = PredictionLog(match_id=None, **row_data)
                        session.add(row)
                session.commit()
            return
    _log_path().parent.mkdir(parents=True, exist_ok=True)
    # 写 JSON 时去掉 id 以便兼容旧格式
    out = []
    for e in entries:
        o = {k: v for k, v in e.items() if k != "id"}
        out.append(o)
    payload = {"predictions": out}
    _log_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def remove_entries_by_date(target_date: str, engine: Engine | None = None) -> int:
    """删除日志中指定比赛日的所有记录，返回删除条数。"""
    date_str = (target_date or "")[:10]
    if not date_str:
        return 0
    if _use_db():
        from db import repo
        if repo.use_mongo():
            return repo.prediction_log_delete_by_date(date_str)
        if config.DB_PATH.exists():
            from etl.schema import get_engine, init_db
            eng = engine or get_engine()
            init_db(eng)
            with eng.connect() as conn:
                r = conn.execute(text("DELETE FROM prediction_log WHERE log_date = :d"), {"d": date_str})
                conn.commit()
                return r.rowcount
    entries = load_log()
    before = len(entries)
    entries = [e for e in entries if (e.get("date") or "")[:10] != date_str]
    removed = before - len(entries)
    if removed:
        save_log(entries)
    return removed


def _outcome(home_score: int, away_score: int) -> str:
    if home_score > away_score:
        return "主"
    if home_score < away_score:
        return "客"
    return "平"


def append_prediction(
    engine: Engine,
    *,
    home_team: str,
    away_team: str,
    competition: str | None = None,
    last_n: int = 10,
    prediction_date: str | None = None,
    odds: dict | None = None,
    match_id: int | None = None,
) -> dict[str, Any] | None:
    """
    对主队 vs 客队做预测并追加到日志。自动按主客队+联赛匹配伤停并传入预测（伤停进 predict 统一处理）。
    若传入 odds（胜平负赔率），则与模型概率融合后写入日志。
    若传入 match_id（如来自 /predictions 接口的场次列表），则用其写入 prediction_log 与 predictions，确保再次打开页面时能命中、不再重复调 API。
    返回本次预测结果 dict，若解析队伍失败则返回 None。
    """
    home_id = resolve_team_by_name(engine, home_team)
    away_id = resolve_team_by_name(engine, away_team)
    if home_id is None or away_id is None:
        return None
    comp_id = resolve_competition_by_name(engine, competition) if competition else None
    pred_date = prediction_date or date.today().isoformat()
    injury_by_teams = _load_injury_by_teams()
    league_cn = COMPETITION_TO_LEAGUE_CN.get((competition or "").strip(), "")
    key = (_norm_team_key(home_team), _norm_team_key(away_team), league_cn)
    injury = injury_by_teams.get(key) if league_cn else None
    home_absent = (injury.get("home_absent") or []) if injury else None
    away_absent = (injury.get("away_absent") or []) if injury else None
    # 体彩无赔率时尝试 API-Football 赔率
    odds_to_use = odds
    if (not odds_to_use or not odds_to_use.get("spf_win")) and league_cn:
        try:
            from etl.api_football_odds import load_odds_for_match
            af_odds = load_odds_for_match(home_team, away_team, league_cn, pred_date)
            if af_odds:
                odds_to_use = dict(odds_to_use or {}, **af_odds)
        except Exception:
            pass
    # 积分榜排名（可选）
    home_rank = away_rank = None
    if league_cn:
        try:
            from etl.api_football_standings import LEAGUE_CN_TO_ID, load_standings, get_team_rank
            y, m = int((pred_date or "")[:4] or 0), int((pred_date or "01")[5:7] or 1)
            season = y if m >= 8 else y - 1
            if season and LEAGUE_CN_TO_ID.get(league_cn):
                by_league = load_standings(season)
                st_list = by_league.get(LEAGUE_CN_TO_ID[league_cn]) or []
                if st_list:
                    home_rank = get_team_rank(st_list, home_team)
                    away_rank = get_team_rank(st_list, away_team)
        except Exception:
            pass
    result = predict_match(
        engine,
        home_id,
        away_id,
        competition_id=comp_id,
        last_n=last_n,
        home_absent=home_absent or None,
        away_absent=away_absent or None,
        match_date=pred_date,
        odds=odds_to_use,
        home_rank=home_rank,
        away_rank=away_rank,
    )
    # 保存融合前的模型单独结果，供列表展示「模型 | API | 融合」
    model_predicted_result = _outcome(result["predicted_score"][0], result["predicted_score"][1])
    model_predicted_scores = [result["predicted_score"]]
    api_predicted_result = None
    api_predicted_scores = None
    # API-Football 预测融合（可选）：与模型胜平负概率线性融合，并记录 API 单独结果
    if getattr(config, "PREDICT_USE_API_PREDICTIONS", False):
        try:
            from etl.api_football_predictions import get_api_predictions_full
            from engine.predict import fuse_probs
            api_probs, api_result, api_scores = get_api_predictions_full(pred_date, home_team, away_team)
            if api_result is not None:
                api_predicted_result = api_result
            if api_scores is not None:
                api_predicted_scores = [list(api_scores)]
            if api_probs is not None:
                w_api = getattr(config, "PREDICT_API_PREDICTIONS_WEIGHT", 0.25)
                w_api = max(0.0, min(1.0, w_api))
                model_probs = (result["prob_home_win"], result["prob_draw"], result["prob_away_win"])
                fused = fuse_probs(model_probs, api_probs, weight_model=1.0 - w_api)
                result["prob_home_win"], result["prob_draw"], result["prob_away_win"] = fused[0], fused[1], fused[2]
        except Exception:
            pass
    pred_scores = result.get("predicted_scores") or [result["predicted_score"]]
    entry = {
        "date": pred_date,
        "home_team": home_team,
        "away_team": away_team,
        "competition": competition or "",
        "exp_goals_home": round(result["exp_goals_home"], 2),
        "exp_goals_away": round(result["exp_goals_away"], 2),
        "predicted_scores": [[int(h), int(a)] for h, a in pred_scores[:2]],
        "prob_home_win": round(result["prob_home_win"], 3),
        "prob_draw": round(result["prob_draw"], 3),
        "prob_away_win": round(result["prob_away_win"], 3),
        "predicted_result": _outcome(result["predicted_score"][0], result["predicted_score"][1]),
        "last_n": result["based_on_last_n"],
        "actual_home_score": None,
        "actual_away_score": None,
        "actual_result": None,
        "confidence": result.get("confidence", "high"),
        "model_predicted_result": model_predicted_result,
        "model_predicted_scores": [[int(h), int(a)] for h, a in model_predicted_scores[:2]],
        "api_predicted_result": api_predicted_result,
        "api_predicted_scores": [[int(h), int(a)] for h, a in (api_predicted_scores or [])[:2]] or None,
    }
    if result.get("odds_used"):
        entry["odds_used"] = True
    # 使用传入的 match_id，否则按主客队+日期解析；写入 prediction_log/predictions 便于下次命中，避免重复调 API
    if match_id is None:
        match_id = find_match_id_by_teams_and_date(engine, home_team, away_team, pred_date)
    if _use_db():
        from db import repo
        if repo.use_mongo():
            doc = {
                "log_date": entry["date"],
                "home_team": entry["home_team"],
                "away_team": entry["away_team"],
                "competition": entry.get("competition", ""),
                "exp_goals_home": entry["exp_goals_home"],
                "exp_goals_away": entry["exp_goals_away"],
                "prob_home_win": entry["prob_home_win"],
                "prob_draw": entry["prob_draw"],
                "prob_away_win": entry["prob_away_win"],
                "predicted_result": entry["predicted_result"],
                "last_n": entry["last_n"],
                "actual_home_score": None,
                "actual_away_score": None,
                "actual_result": None,
                "confidence": entry.get("confidence", "high"),
                "odds_used": entry.get("odds_used", False),
                "predicted_scores_json": json.dumps(entry.get("predicted_scores") or [[0, 0]], ensure_ascii=False),
            }
            if entry.get("model_predicted_result") is not None:
                doc["model_predicted_result"] = entry["model_predicted_result"]
                doc["model_predicted_scores_json"] = json.dumps(entry.get("model_predicted_scores") or [[0, 0]], ensure_ascii=False)
            if entry.get("api_predicted_result") is not None:
                doc["api_predicted_result"] = entry["api_predicted_result"]
                if entry.get("api_predicted_scores"):
                    doc["api_predicted_scores_json"] = json.dumps(entry["api_predicted_scores"], ensure_ascii=False)
            if match_id is not None:
                doc["match_id"] = match_id
            repo.prediction_log_add(doc)
        elif config.DB_PATH.exists():
            from etl.schema import get_engine, init_db, PredictionLog
            eng = get_engine()
            init_db(eng)
            pred_scores = entry.get("predicted_scores") or [[0, 0]]
            row = PredictionLog(
                match_id=None,
                log_date=entry["date"],
                home_team=entry["home_team"],
                away_team=entry["away_team"],
                competition=entry.get("competition", ""),
                exp_goals_home=entry["exp_goals_home"],
                exp_goals_away=entry["exp_goals_away"],
                prob_home_win=entry["prob_home_win"],
                prob_draw=entry["prob_draw"],
                prob_away_win=entry["prob_away_win"],
                predicted_result=entry["predicted_result"],
                last_n=entry["last_n"],
                actual_home_score=None,
                actual_away_score=None,
                actual_result=None,
                confidence=entry.get("confidence", "high"),
                odds_used=entry.get("odds_used", False),
                predicted_scores_json=json.dumps(pred_scores[:2], ensure_ascii=False),
            )
            from sqlalchemy.orm import Session
            with Session(eng) as session:
                session.add(row)
                session.commit()
    else:
        entries = load_log()
        entries.append(entry)
        save_log(entries)
    # 若 DB 中已有该场比赛，同步写入 predictions 表供 predict-eval 统计（match_id 已在上方解析）
    if match_id is not None and not _prediction_exists_for_match(engine, match_id):
        res = result.copy()
        if res.get("competition_id") is None and competition:
            res["competition_id"] = resolve_competition_by_name(engine, competition)
        res["model_predicted_result"] = entry.get("model_predicted_result")
        res["model_predicted_scores"] = entry.get("model_predicted_scores")
        res["api_predicted_result"] = entry.get("api_predicted_result")
        res["api_predicted_scores"] = entry.get("api_predicted_scores")
        try:
            save_prediction(engine, match_id, res)
        except Exception:
            pass
    return result


def set_actual(log_index: int, home_score: int, away_score: int, engine: Engine | None = None) -> bool:
    """按日志序号（从 0 开始）填写实际比分。"""
    entries = load_log(engine)
    if log_index < 0 or log_index >= len(entries):
        return False
    entries[log_index]["actual_home_score"] = home_score
    entries[log_index]["actual_away_score"] = away_score
    entries[log_index]["actual_result"] = _outcome(home_score, away_score)
    if _use_db() and config.DB_PATH.exists() and entries[log_index].get("id") is not None:
        from etl.schema import get_engine, init_db

        eng = engine or get_engine()
        init_db(eng)
        with eng.connect() as conn:
            conn.execute(
                text("""
                    UPDATE prediction_log SET actual_home_score=:hs, actual_away_score=:as, actual_result=:ar
                    WHERE id=:id
                """),
                {"hs": home_score, "as": away_score, "ar": _outcome(home_score, away_score), "id": entries[log_index]["id"]},
            )
            conn.commit()
        return True
    save_log(entries, engine)
    return True


def repredict_log_entries_with_actuals(engine: Engine) -> int:
    """
    对日志中「已填写实际比分」的条目，用当前 config 重算预测并写回该条目的预测字段（不新增、不删条目不改 actual_*）。
    用于调参后做严格前后对比：跑本函数后再跑 prediction-log recap 即可得到新参数下的指标。
    返回被重算并写回的条数。
    """
    entries = load_log()
    with_actual = [
        (i, e)
        for i, e in enumerate(entries)
        if e.get("actual_home_score") is not None and e.get("actual_away_score") is not None
    ]
    if not with_actual:
        return 0
    total_log = len(with_actual)
    print(f"  共 {total_log} 条，开始重算...", flush=True)
    injury_by_teams = _load_injury_by_teams()
    comp_cn_map = COMPETITION_TO_LEAGUE_CN
    n = 0
    for i, e in with_actual:
        home_team = (e.get("home_team") or "").strip()
        away_team = (e.get("away_team") or "").strip()
        competition = (e.get("competition") or "").strip()
        pred_date = (e.get("date") or "")[:10] or None
        home_id = resolve_team_by_name(engine, home_team)
        away_id = resolve_team_by_name(engine, away_team)
        if home_id is None or away_id is None:
            continue
        comp_id = resolve_competition_by_name(engine, competition) if competition else None
        league_cn = comp_cn_map.get(competition, "")
        key = (_norm_team_key(home_team), _norm_team_key(away_team), league_cn)
        injury = injury_by_teams.get(key) if league_cn else None
        home_absent = (injury.get("home_absent") or []) if injury else None
        away_absent = (injury.get("away_absent") or []) if injury else None
        # 与 append_prediction 对齐：补赔率（API-Football）、积分榜排名，再预测
        odds_to_use = None
        if league_cn:
            try:
                from etl.api_football_odds import load_odds_for_match
                af_odds = load_odds_for_match(home_team, away_team, league_cn, pred_date)
                if af_odds:
                    odds_to_use = af_odds
            except Exception:
                pass
        home_rank = away_rank = None
        if league_cn:
            try:
                from etl.api_football_standings import LEAGUE_CN_TO_ID, load_standings, get_team_rank
                y, m = int((pred_date or "")[:4] or 0), int((pred_date or "01")[5:7] or 1)
                season = y if m >= 8 else y - 1
                if season and LEAGUE_CN_TO_ID.get(league_cn):
                    by_league = load_standings(season)
                    st_list = by_league.get(LEAGUE_CN_TO_ID[league_cn]) or []
                    if st_list:
                        home_rank = get_team_rank(st_list, home_team)
                        away_rank = get_team_rank(st_list, away_team)
            except Exception:
                pass
        result = predict_match(
            engine,
            home_id,
            away_id,
            competition_id=comp_id,
            last_n=10,
            home_absent=home_absent or None,
            away_absent=away_absent or None,
            match_date=pred_date,
            odds=odds_to_use,
            home_rank=home_rank,
            away_rank=away_rank,
        )
        pred_scores = result.get("predicted_scores") or [result["predicted_score"]]
        ph, pa = result["predicted_score"][0], result["predicted_score"][1]
        entries[i]["exp_goals_home"] = round(result["exp_goals_home"], 2)
        entries[i]["exp_goals_away"] = round(result["exp_goals_away"], 2)
        entries[i]["predicted_scores"] = [[int(h), int(a)] for h, a in pred_scores[:2]]
        entries[i]["prob_home_win"] = round(result["prob_home_win"], 3)
        entries[i]["prob_draw"] = round(result["prob_draw"], 3)
        entries[i]["prob_away_win"] = round(result["prob_away_win"], 3)
        entries[i]["predicted_result"] = _outcome(int(ph), int(pa))
        # API 预测融合（与 append_prediction 一致）：有则与模型概率融合并更新胜平负与 predicted_result
        if getattr(config, "PREDICT_USE_API_PREDICTIONS", False) and pred_date:
            try:
                from etl.api_football_predictions import get_api_predictions_probs
                from engine.predict import fuse_probs
                api_probs = get_api_predictions_probs(pred_date, home_team, away_team)
                if api_probs is not None:
                    w_api = getattr(config, "PREDICT_API_PREDICTIONS_WEIGHT", 0.2)
                    w_api = max(0.0, min(1.0, w_api))
                    model_probs = (result["prob_home_win"], result["prob_draw"], result["prob_away_win"])
                    fused = fuse_probs(model_probs, api_probs, weight_model=1.0 - w_api)
                    entries[i]["prob_home_win"] = round(fused[0], 3)
                    entries[i]["prob_draw"] = round(fused[1], 3)
                    entries[i]["prob_away_win"] = round(fused[2], 3)
                    if fused[0] >= fused[1] and fused[0] >= fused[2]:
                        entries[i]["predicted_result"] = "主"
                    elif fused[2] >= fused[1] and fused[2] >= fused[0]:
                        entries[i]["predicted_result"] = "客"
                    else:
                        entries[i]["predicted_result"] = "平"
            except Exception:
                pass
        entries[i]["last_n"] = result["based_on_last_n"]
        entries[i]["confidence"] = result.get("confidence", "high")
        if result.get("odds_used"):
            entries[i]["odds_used"] = True
        elif "odds_used" in entries[i]:
            del entries[i]["odds_used"]
        n += 1
        if n % 20 == 0 or n == total_log:
            print(f"  已重算 prediction_log {n}/{total_log} 条", flush=True)
    if n > 0:
        save_log(entries)
    return n


def ensure_prediction_in_db_for_match(
    engine: Engine,
    match_id: int,
    match_date: str,
    home_team: str,
    away_team: str,
) -> bool:
    """
    若 prediction_log 中有该场（日期+主客队）的预测且 predictions 表尚无该 match_id，
    则从 log 拼 result 并写入 DB。用于 sync-sporttery 新增场次后回填预测。
    返回是否写入了新预测。
    """
    if _prediction_exists_for_match(engine, match_id):
        return False
    date_str = (match_date or "")[:10]
    entries = load_log()
    for e in entries:
        if (e.get("date") or "")[:10] != date_str:
            continue
        if not _match_team_names(home_team, e.get("home_team") or ""):
            continue
        if not _match_team_names(away_team, e.get("away_team") or ""):
            continue
        comp = (e.get("competition") or "").strip()
        comp_id = resolve_competition_by_name(engine, comp) if comp else None
        result = {
            "exp_goals_home": float(e.get("exp_goals_home", 0)),
            "exp_goals_away": float(e.get("exp_goals_away", 0)),
            "prob_home_win": float(e.get("prob_home_win", 0)),
            "prob_draw": float(e.get("prob_draw", 0)),
            "prob_away_win": float(e.get("prob_away_win", 0)),
            "based_on_last_n": int(e.get("last_n", 10)),
            "competition_id": comp_id,
        }
        try:
            save_prediction(engine, match_id, result)
            return True
        except Exception:
            pass
        break
    return False


def has_entry_for_match(home_team: str, away_team: str, prediction_date: str) -> bool:
    """prediction_log 中是否已有该场（日期+主客队）的预测条目。"""
    date_prefix = (prediction_date or "")[:10]
    if not date_prefix:
        return False
    entries = load_log()
    for e in entries:
        if (e.get("date") or "")[:10] != date_prefix:
            continue
        if not _match_team_names(home_team, e.get("home_team") or ""):
            continue
        if not _match_team_names(away_team, e.get("away_team") or ""):
            continue
        return True
    return False


def set_actual_by_match(
    home_team: str,
    away_team: str,
    home_score: int,
    away_score: int,
    match_date: str | None = None,
) -> int:
    """按主客队名称（及可选比赛日）匹配第一条并填写实际比分并保存，返回被更新的条目索引，未找到返回 -1。"""
    entries = load_log()
    date_prefix = (match_date or "")[:10] if match_date else None
    for i, e in enumerate(entries):
        if date_prefix and (e.get("date") or "")[:10] != date_prefix:
            continue
        h, a = e.get("home_team", ""), e.get("away_team", "")
        if not _match_team_names(home_team, h) or not _match_team_names(away_team, a):
            continue
        entries[i]["actual_home_score"] = home_score
        entries[i]["actual_away_score"] = away_score
        entries[i]["actual_result"] = _outcome(home_score, away_score)
        save_log(entries)
        return i
    return -1


def _injury_adjusted_result(
    exp_home: float,
    exp_away: float,
    home_absent: list,
    away_absent: list,
) -> str:
    """与 api/app 一致的伤停下调：每约 2 人 5%，总上限 15%，再算胜平负。"""
    down_home = min(0.15, len(home_absent) * 0.025)
    down_away = min(0.15, len(away_absent) * 0.025)
    eh_adj = exp_home * (1.0 - down_home)
    ea_adj = exp_away * (1.0 - down_away)
    ph, pd, pa = probs_from_exp_goals(eh_adj, ea_adj)
    if ph >= pd and ph >= pa:
        return "主"
    if pa >= pd and pa >= ph:
        return "客"
    return "平"


def generate_recap(
    entries: list[dict[str, Any]] | None = None,
    *,
    only_date: str | None = None,
    by_competition: bool = False,
    by_year: bool = False,
) -> dict[str, Any]:
    """
    对已填写实际比分的条目做复盘统计。
    only_date: 只统计该日期（prediction date）的预测；None 表示全部。
    by_competition: 为 True 时按联赛分组返回 by_competition 汇总（n, mae_*, outcome_accuracy 等）。
    返回: n, mae_*, outcome_*, details[]；若有伤停数据则含 injury_* 统计（用于评估伤停是否提升命中率）。
    """
    if entries is None:
        entries = load_log()
    with_actual = [
        e for e in entries
        if e.get("actual_home_score") is not None and e.get("actual_away_score") is not None
    ]
    if only_date:
        with_actual = [e for e in with_actual if e.get("date") == only_date]
    if not with_actual:
        out_empty = {
            "n": 0,
            "mae_home": 0.0,
            "mae_away": 0.0,
            "mae_overall": 0.0,
            "outcome_correct": 0,
            "outcome_accuracy": 0.0,
            "exact_score_correct": 0,
            "exact_score_accuracy": 0.0,
            "details": [],
            "injury_n": 0,
            "injury_original_correct": 0,
            "injury_adjusted_correct": 0,
        }
        if by_competition:
            out_empty["by_competition"] = {}
        if by_year:
            out_empty["by_year"] = {}
        return out_empty
    injury_by_teams = _load_injury_by_teams()
    mae_home = sum(abs(e["exp_goals_home"] - e["actual_home_score"]) for e in with_actual) / len(with_actual)
    mae_away = sum(abs(e["exp_goals_away"] - e["actual_away_score"]) for e in with_actual) / len(with_actual)
    mae_overall = (mae_home + mae_away) / 2.0
    outcome_correct = sum(1 for e in with_actual if e.get("predicted_result") == e.get("actual_result"))
    outcome_accuracy = outcome_correct / len(with_actual)
    exact_score_correct = 0
    for e in with_actual:
        pred_scores = e.get("predicted_scores") or []
        if pred_scores:
            ph, pa = pred_scores[0][0], pred_scores[0][1]
        else:
            ph = round(e["exp_goals_home"])
            pa = round(e["exp_goals_away"])
        if ph == e["actual_home_score"] and pa == e["actual_away_score"]:
            exact_score_correct += 1
    exact_score_accuracy = exact_score_correct / len(with_actual)

    injury_n = 0
    injury_original_correct = 0
    injury_adjusted_correct = 0
    details = []
    for e in with_actual:
        pred_ok = e.get("predicted_result") == e.get("actual_result")
        err_h = e["exp_goals_home"] - e["actual_home_score"]
        err_a = e["exp_goals_away"] - e["actual_away_score"]
        reason = ""
        if not pred_ok:
            if e.get("actual_result") == "主" and e.get("predicted_result") in ("平", "客"):
                reason = "主队实际表现强于预期"
            elif e.get("actual_result") == "客" and e.get("predicted_result") in ("平", "主"):
                reason = "客队实际表现强于预期"
            elif e.get("actual_result") == "平" and e.get("predicted_result") in ("主", "客"):
                reason = "双方实际更接近，或一方未能扩大优势"
            else:
                reason = "赛果与概率预测方向相反"

        # 若当前伤停 JSON 能匹配该场，计算「伤停调整后」的预测结果，用于复盘看伤停是否有助于命中
        detail_extra: dict[str, Any] = {}
        comp = (e.get("competition") or "").strip()
        league_cn = COMPETITION_TO_LEAGUE_CN.get(comp) if comp else None
        if league_cn:
            key = (_norm_team_key(e.get("home_team") or ""), _norm_team_key(e.get("away_team") or ""), league_cn)
            injury = injury_by_teams.get(key)
            if injury and (injury.get("home_absent") or injury.get("away_absent")):
                injury_n += 1
                if pred_ok:
                    injury_original_correct += 1
                adj_result = _injury_adjusted_result(
                    e["exp_goals_home"],
                    e["exp_goals_away"],
                    injury.get("home_absent") or [],
                    injury.get("away_absent") or [],
                )
                adj_ok = adj_result == e.get("actual_result")
                if adj_ok:
                    injury_adjusted_correct += 1
                detail_extra["had_injury"] = True
                detail_extra["injury_adjusted_result"] = adj_result
                detail_extra["injury_adjusted_correct"] = adj_ok

        details.append({
            "home_team": e.get("home_team"),
            "away_team": e.get("away_team"),
            "predicted": f"{e['exp_goals_home']}-{e['exp_goals_away']} (预测结果: {e.get('predicted_result')})",
            "actual": f"{e['actual_home_score']}-{e['actual_away_score']} (实际: {e.get('actual_result')})",
            "outcome_correct": pred_ok,
            "error_home": round(err_h, 2),
            "error_away": round(err_a, 2),
            "reason": reason,
            **detail_extra,
        })

    out = {
        "n": len(with_actual),
        "mae_home": round(mae_home, 3),
        "mae_away": round(mae_away, 3),
        "mae_overall": round(mae_overall, 3),
        "outcome_correct": outcome_correct,
        "outcome_accuracy": round(outcome_accuracy, 3),
        "exact_score_correct": exact_score_correct,
        "exact_score_accuracy": round(exact_score_accuracy, 3),
        "details": details,
        "injury_n": injury_n,
        "injury_original_correct": injury_original_correct,
        "injury_adjusted_correct": injury_adjusted_correct,
    }
    if by_year:
        by_y: dict[int, dict[str, Any]] = {}
        for e in with_actual:
            d = (e.get("date") or "")[:10]
            y = int(d[:4]) if len(d) >= 4 and str(d[:4]).isdigit() else None
            if y is None:
                continue
            if y not in by_y:
                by_y[y] = {
                    "n": 0,
                    "mae_home_sum": 0.0,
                    "mae_away_sum": 0.0,
                    "outcome_correct": 0,
                    "exact_score_correct": 0,
                }
            g = by_y[y]
            g["n"] += 1
            g["mae_home_sum"] += abs(e["exp_goals_home"] - e["actual_home_score"])
            g["mae_away_sum"] += abs(e["exp_goals_away"] - e["actual_away_score"])
            if e.get("predicted_result") == e.get("actual_result"):
                g["outcome_correct"] += 1
            pred_scores = e.get("predicted_scores") or []
            ph = pred_scores[0][0] if pred_scores else round(e["exp_goals_home"])
            pa = pred_scores[0][1] if pred_scores else round(e["exp_goals_away"])
            if ph == e["actual_home_score"] and pa == e["actual_away_score"]:
                g["exact_score_correct"] += 1
        out["by_year"] = {}
        for yy, g in sorted(by_y.items()):
            nn = g["n"]
            out["by_year"][yy] = {
                "n": nn,
                "mae_home": round(g["mae_home_sum"] / nn, 3),
                "mae_away": round(g["mae_away_sum"] / nn, 3),
                "mae_overall": round((g["mae_home_sum"] + g["mae_away_sum"]) / (2 * nn), 3),
                "outcome_correct": g["outcome_correct"],
                "outcome_accuracy": round(g["outcome_correct"] / nn, 3) if nn else 0.0,
                "exact_score_correct": g["exact_score_correct"],
                "exact_score_accuracy": round(g["exact_score_correct"] / nn, 3) if nn else 0.0,
            }
    if by_competition:
        by_comp: dict[str, dict[str, Any]] = {}
        for e in with_actual:
            comp = (e.get("competition") or "").strip() or "_"
            if comp not in by_comp:
                by_comp[comp] = {
                    "n": 0,
                    "mae_home_sum": 0.0,
                    "mae_away_sum": 0.0,
                    "outcome_correct": 0,
                    "exact_score_correct": 0,
                }
            g = by_comp[comp]
            g["n"] += 1
            g["mae_home_sum"] += abs(e["exp_goals_home"] - e["actual_home_score"])
            g["mae_away_sum"] += abs(e["exp_goals_away"] - e["actual_away_score"])
            if e.get("predicted_result") == e.get("actual_result"):
                g["outcome_correct"] += 1
            pred_scores = e.get("predicted_scores") or []
            ph = pred_scores[0][0] if pred_scores else round(e["exp_goals_home"])
            pa = pred_scores[0][1] if pred_scores else round(e["exp_goals_away"])
            if ph == e["actual_home_score"] and pa == e["actual_away_score"]:
                g["exact_score_correct"] += 1
        out["by_competition"] = {}
        for comp_name, g in sorted(by_comp.items()):
            nn = g["n"]
            out["by_competition"][comp_name] = {
                "n": nn,
                "mae_home": round(g["mae_home_sum"] / nn, 3),
                "mae_away": round(g["mae_away_sum"] / nn, 3),
                "mae_overall": round((g["mae_home_sum"] + g["mae_away_sum"]) / (2 * nn), 3),
                "outcome_correct": g["outcome_correct"],
                "outcome_accuracy": round(g["outcome_correct"] / nn, 3) if nn else 0.0,
                "exact_score_correct": g["exact_score_correct"],
                "exact_score_accuracy": round(g["exact_score_correct"] / nn, 3) if nn else 0.0,
            }
    return out


def _recap_key(date_str: str, home: str, away: str) -> tuple[str, str, str]:
    """用于复盘场次匹配的键：(日期, norm_home, norm_away)。"""
    d = (date_str or "")[:10]
    return (d, _norm_team_key(home or ""), _norm_team_key(away or ""))


def compare_recap_with_db(engine: Engine | None) -> dict[str, Any]:
    """
    对比「prediction_log 已填比分的复盘数据」与「DB 中已完赛且有预测」的数据是否一致。
    engine 为 None 且 USE_MONGO 时从 MongoDB 取 DB 数据；否则用 SQL(engine)。
    返回：only_in_db 数量与列表、only_in_log 数量与列表、in_both 数量、mismatch 数量与列表（比分或预测值不一致）。
    """
    from db import repo

    db_by_key: dict[tuple[str, str, str], dict] = {}
    if engine is None and repo.use_mongo():
        for r in repo.prediction_log_sync_source_rows():
            date_str = (r.get("log_date") or "")[:10]
            home = (r.get("home_team") or "").strip() or "?"
            away = (r.get("away_team") or "").strip() or "?"
            key = _recap_key(date_str, home, away)
            if key in db_by_key:
                continue
            db_by_key[key] = {
                "date": date_str,
                "home_team": home,
                "away_team": away,
                "home_score": int(r.get("home_score", 0)),
                "away_score": int(r.get("away_score", 0)),
                "exp_goals_home": float(r.get("exp_goals_home", 0)),
                "exp_goals_away": float(r.get("exp_goals_away", 0)),
            }
    else:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT m.match_date, m.home_score, m.away_score,
                           ht.name AS home_team, at.name AS away_team,
                           p.exp_goals_home, p.exp_goals_away
                    FROM predictions p
                    JOIN matches m ON m.id = p.match_id
                    LEFT JOIN teams ht ON ht.id = m.home_team_id
                    LEFT JOIN teams at ON at.id = m.away_team_id
                    WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                """)
            ).fetchall()
        for r in rows:
            date_str = (r[0] or "")[:10]
            home = (r[3] or "").strip() or "?"
            away = (r[4] or "").strip() or "?"
            key = _recap_key(date_str, home, away)
            if key in db_by_key:
                continue
            db_by_key[key] = {
                "date": date_str,
                "home_team": home,
                "away_team": away,
                "home_score": int(r[1]),
                "away_score": int(r[2]),
                "exp_goals_home": float(r[5]),
                "exp_goals_away": float(r[6]),
            }

    entries = load_log()
    log_with_actual = [
        e for e in entries
        if e.get("actual_home_score") is not None and e.get("actual_away_score") is not None
    ]
    log_by_key: dict[tuple[str, str, str], dict] = {}
    for e in log_with_actual:
        date_str = (e.get("date") or "")[:10]
        home = (e.get("home_team") or "").strip()
        away = (e.get("away_team") or "").strip()
        key = _recap_key(date_str, home, away)
        if key in log_by_key:
            continue
        log_by_key[key] = {
            "date": date_str,
            "home_team": home,
            "away_team": away,
            "home_score": int(e["actual_home_score"]),
            "away_score": int(e["actual_away_score"]),
            "exp_goals_home": float(e.get("exp_goals_home", 0)),
            "exp_goals_away": float(e.get("exp_goals_away", 0)),
        }

    only_in_db: list[dict] = []
    only_in_log: list[dict] = []
    in_both_consistent = 0
    mismatch: list[dict] = []

    for key, db_row in db_by_key.items():
        log_row = log_by_key.get(key)
        if log_row is None:
            only_in_db.append(db_row)
            continue
        if (
            db_row["home_score"] == log_row["home_score"]
            and db_row["away_score"] == log_row["away_score"]
            and abs(db_row["exp_goals_home"] - log_row["exp_goals_home"]) < 1e-3
            and abs(db_row["exp_goals_away"] - log_row["exp_goals_away"]) < 1e-3
        ):
            in_both_consistent += 1
        else:
            mismatch.append({
                "date": db_row["date"],
                "home_team": db_row["home_team"],
                "away_team": db_row["away_team"],
                "db_score": f"{db_row['home_score']}-{db_row['away_score']}",
                "log_score": f"{log_row['home_score']}-{log_row['away_score']}",
                "db_exp": f"{db_row['exp_goals_home']:.2f}-{db_row['exp_goals_away']:.2f}",
                "log_exp": f"{log_row['exp_goals_home']:.2f}-{log_row['exp_goals_away']:.2f}",
            })

    for key, log_row in log_by_key.items():
        if key not in db_by_key:
            only_in_log.append(log_row)

    return {
        "db_total": len(db_by_key),
        "log_total": len(log_by_key),
        "only_in_db_count": len(only_in_db),
        "only_in_db": only_in_db,
        "only_in_log_count": len(only_in_log),
        "only_in_log": only_in_log,
        "in_both_consistent": in_both_consistent,
        "mismatch_count": len(mismatch),
        "mismatch": mismatch,
    }


def sync_prediction_log_from_db(engine: Engine) -> tuple[int, int]:
    """
    从 DB 的 predictions + matches（已完赛）同步到 prediction_log 表，使「已填比分」条数与 DB 一致。
    返回 (插入条数, 更新条数)。
    """
    if not _use_db():
        return 0, 0
    from db import repo
    if repo.use_mongo():
        rows = repo.prediction_log_sync_source_rows()
        inserted = updated = 0
        for r in rows:
            match_id = r["match_id"]
            ph, pd, pa = r["prob_home_win"], r["prob_draw"], r["prob_away_win"]
            pred_result = "主" if ph >= pd and ph >= pa else ("客" if pa >= pd and pa >= ph else "平")
            actual_result = _outcome(r["home_score"], r["away_score"])
            pred_scores = [[int(round(r["exp_goals_home"])), int(round(r["exp_goals_away"]))]]
            entry = {
                "log_date": r["log_date"],
                "home_team": r["home_team"] or "?",
                "away_team": r["away_team"] or "?",
                "competition": r["competition"],
                "exp_goals_home": r["exp_goals_home"],
                "exp_goals_away": r["exp_goals_away"],
                "prob_home_win": ph,
                "prob_draw": pd,
                "prob_away_win": pa,
                "predicted_result": pred_result,
                "last_n": r["last_n"],
                "actual_home_score": r["home_score"],
                "actual_away_score": r["away_score"],
                "actual_result": actual_result,
                "confidence": "high",
                "odds_used": False,
                "predicted_scores_json": json.dumps(pred_scores, ensure_ascii=False),
            }
            existing = repo.prediction_log_get_by_match_id(match_id)
            if existing:
                updated += 1
            else:
                inserted += 1
            repo.prediction_log_replace_for_match_id(match_id, entry)
        return inserted, updated
    if not config.DB_PATH.exists():
        return 0, 0
    from etl.schema import init_db, PredictionLog
    from sqlalchemy.orm import Session
    init_db(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT p.match_id, m.match_date, m.home_score, m.away_score,
                       p.exp_goals_home, p.exp_goals_away, p.prob_home_win, p.prob_draw, p.prob_away_win, p.last_n,
                       m.raw_meta,
                       ht.name AS home_team, at.name AS away_team
                FROM predictions p
                JOIN matches m ON m.id = p.match_id
                LEFT JOIN teams ht ON ht.id = m.home_team_id
                LEFT JOIN teams at ON at.id = m.away_team_id
                WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            """)
        ).fetchall()
    inserted = updated = 0
    with Session(engine) as session:
        for r in rows:
            match_id, log_date, hs, aws = int(r[0]), (r[1] or "")[:10], int(r[2]), int(r[3])
            exp_h, exp_a = float(r[4]), float(r[5])
            ph, pd, pa = float(r[6]), float(r[7]), float(r[8])
            last_n = int(r[9])
            raw_meta = r[10]
            home_team = (r[11] or "").strip()
            away_team = (r[12] or "").strip()
            if not home_team or not away_team:
                try:
                    meta = json.loads(raw_meta or "{}")
                    home_team = (meta.get("home_team_name") or "").strip() or "?"
                    away_team = (meta.get("away_team_name") or "").strip() or "?"
                    comp_name = (meta.get("competition_name") or "").strip()
                except Exception:
                    comp_name = ""
            else:
                comp_name = ""
            if not comp_name and raw_meta:
                try:
                    meta = json.loads(raw_meta or "{}")
                    comp_name = (meta.get("competition_name") or "").strip()
                except Exception:
                    pass
            if not log_date:
                continue
            pred_result = "主" if ph >= pd and ph >= pa else ("客" if pa >= pd and pa >= ph else "平")
            actual_result = _outcome(hs, aws)
            pred_scores = [[int(round(exp_h)), int(round(exp_a))]]
            existing = session.query(PredictionLog).filter(PredictionLog.match_id == match_id).first()
            if existing:
                existing.log_date = log_date
                existing.home_team = home_team or "?"
                existing.away_team = away_team or "?"
                existing.competition = comp_name
                existing.exp_goals_home = exp_h
                existing.exp_goals_away = exp_a
                existing.prob_home_win = ph
                existing.prob_draw = pd
                existing.prob_away_win = pa
                existing.predicted_result = pred_result
                existing.last_n = last_n
                existing.actual_home_score = hs
                existing.actual_away_score = aws
                existing.actual_result = actual_result
                existing.predicted_scores_json = json.dumps(pred_scores, ensure_ascii=False)
                updated += 1
            else:
                row = PredictionLog(
                    match_id=match_id,
                    log_date=log_date,
                    home_team=home_team or "?",
                    away_team=away_team or "?",
                    competition=comp_name,
                    exp_goals_home=exp_h,
                    exp_goals_away=exp_a,
                    prob_home_win=ph,
                    prob_draw=pd,
                    prob_away_win=pa,
                    predicted_result=pred_result,
                    last_n=last_n,
                    actual_home_score=hs,
                    actual_away_score=aws,
                    actual_result=actual_result,
                    confidence="high",
                    odds_used=False,
                    predicted_scores_json=json.dumps(pred_scores, ensure_ascii=False),
                )
                session.add(row)
                inserted += 1
        session.commit()
    return inserted, updated
