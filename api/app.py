"""FastAPI app: list matches, generate report, get report by id."""
import json
import re
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import text

import config
from etl.schema import get_engine, init_db
from etl.football_data_ingest import run_ingest_football_data_for_date
from engine.report_payload import build_report_payload
from report.assembler import ReportAssembler
from report.output import save_report_md, save_report_html

app = FastAPI(
    title="Football Analysis API",
    version="0.1.0",
    description="足球分析 Web API：预测列表、比赛与报告、伤停、复盘统计。数据来自 API/DB，体彩同步已废弃。",
)
REPORTS_DIR = config.DATA_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# 竞彩 2026 年后台拉取进度（内存 + 日志文件）
LOG_DIR = config.DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
JC_2026_LOG = LOG_DIR / "jc_2026_fetch.log"
_jc_2026_state: dict[str, Any] = {"running": False, "current_date": None, "index": 0, "total": 0, "total_written": 0, "log_lines": [], "error": None}
_jc_2026_lock = threading.Lock()


def _jingcai_day_to_match_day(day_str: str) -> str:
    """竞彩列表出现日（开赛前一日）→ 实际比赛日。用户选「某日」查的是该日能看到的竞彩场次，即次日开球的比赛。"""
    s = (day_str or "")[:10]
    if len(s) != 10:
        return s
    try:
        d = date.fromisoformat(s)
        return (d + timedelta(days=1)).isoformat()
    except ValueError:
        return s


def _add_one_day(day_str: str) -> str:
    """日期 +1 天（用于尝试开奖日同步）。"""
    s = (day_str or "")[:10]
    if len(s) != 10:
        return s
    try:
        d = date.fromisoformat(s)
        return (d + timedelta(days=1)).isoformat()
    except ValueError:
        return s


# 伤停原因英文 -> 中文（API-Football 等返回英文，前端需显示中文）
INJURY_REASON_ZH: dict[str, str] = {
    "Muscle Injury": "肌肉损伤",
    "Calf Injury": "小腿受伤",
    "Hamstring Injury": "腿筋受伤",
    "Knee Injury": "膝盖受伤",
    "Injury": "受伤",
    "Suspended": "停赛",
    "Foot Injury": "脚部受伤",
    "Thigh Injury": "大腿受伤",
    "Ankle Injury": "脚踝受伤",
    "Back Injury": "背部受伤",
    "Groin Injury": "腹股沟受伤",
    "Head Injury": "头部受伤",
    "Hip Injury": "髋部受伤",
    "Illness": "生病",
    "Unknown Injury": "伤病（原因未知）",
    "Personal Reasons": "个人原因",
    "Rest": "轮休",
    "Red Card": "红牌停赛",
    "Yellow Card Suspension": "累积黄牌停赛",
}


def _injury_reasons_to_zh(injury: dict[str, Any] | None) -> dict[str, Any] | None:
    """将伤停中的 reason 转为中文后返回新对象，不修改原对象。"""
    if not injury or not (injury.get("home_absent") or injury.get("away_absent")):
        return injury

    def _translate_list(lst: list[dict]) -> list[dict]:
        if not lst:
            return lst
        out = []
        for p in lst:
            rec = dict(p)
            reason = (p.get("reason") or "").strip()
            rec["reason"] = INJURY_REASON_ZH.get(reason) or INJURY_REASON_ZH.get(reason.title()) or (reason if reason else "伤停")
            out.append(rec)
        return out

    return {
        **injury,
        "home_absent": _translate_list(injury.get("home_absent") or []),
        "away_absent": _translate_list(injury.get("away_absent") or []),
    }


# 赛事英文名 -> 伤停 JSON 中的联赛中文（用于场次号匹配）
_COMPETITION_TO_LEAGUE_CN: dict[str, str] = {
    "Premier League": "英超",
    "La Liga": "西甲",
    "Primera División": "西甲",
    "Primera Division": "西甲",
    "Serie A": "意甲",
    "Bundesliga": "德甲",
    "1. Bundesliga": "德甲",
    "Ligue 1": "法甲",
}


def _teams_match(pred_home: str, pred_away: str, inj_home: str, inj_away: str) -> bool:
    """
    校验体彩主客队与伤停主客队是否一致（支持简称/别名、重音归一，如 Alavés vs Alaves）。
    若伤停无队名信息则通过校验。
    """
    def _norm(t: str) -> str:
        s = (t or "").strip().replace(" ", "").lower()
        for old, new in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u"), ("ñ", "n")):
            s = s.replace(old, new)
        return s

    ih, ia = _norm(inj_home), _norm(inj_away)
    if not ih and not ia:
        return True
    ph, pa = _norm(pred_home), _norm(pred_away)
    home_ok = (ph == ih) or (ph in ih) or (ih in ph) if ih else True
    away_ok = (pa == ia) or (pa in ia) or (ia in pa) if ia else True
    return home_ok and away_ok


def _norm_team_key(name: str) -> str:
    """用于伤停按主客队+联赛匹配的键规范化（与 API-FOOTBALL 英文队名一致）。"""
    s = (name or "").strip().lower().replace("&", " ")
    # 重音归一：Alavés / Alaves 等统一为 ascii
    for old, new in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u"), ("ñ", "n")):
        s = s.replace(old, new)
    # 统一常见标点，避免 "Nott'm" / "St.-Etienne" 这类写法影响匹配
    s = re.sub(r"[.'’`-]", " ", s)
    # 去掉常见前后缀与虚词，尽量把同一球队不同写法归到同一键
    drop_tokens = {
        "fc", "cf", "afc", "sc", "cfc", "ac", "us",
        "club", "de", "del", "la", "los", "las",
        "futbol", "futebol", "football", "calcio",
    }
    tokens = [t for t in re.split(r"\s+", s) if t and t not in drop_tokens]
    # 某些来源会带地区后缀，单独剔除可提升 API/DB 互配概率
    if tokens[-2:] == ["hove", "albion"]:
        tokens = tokens[:-2]
    elif tokens[-1:] == ["albion"]:
        tokens = tokens[:-1]
    return "".join(tokens)


def _load_injury_data() -> tuple[
    dict[str, dict],
    dict[tuple[str, str], list[dict]],
    dict[tuple[str, str, str, str], dict],
]:
    """
    加载伤停 JSON。
    返回 (exact_index, fallback_index, by_teams_index)：
    - exact_index: { match_num: {...} }
    - fallback_index: { (数字后缀, 联赛中文): [payload, ...] }
    - by_teams_index: { (norm_home, norm_away, league_cn, fixture_date): payload }，按比赛日匹配
    """
    exact: dict[str, dict] = {}
    fallback: dict[tuple[str, str], list[dict]] = {}
    by_teams: dict[tuple[str, str, str, str], dict] = {}
    try:
        from etl.injury_db import get_injury_matches_from_db
        for m in get_injury_matches_from_db():
            home = (m.get("home") or "").strip()
            away = (m.get("away") or "").strip()
            league_cn = (m.get("league") or "").strip()
            fixture_date = (m.get("fixture_date") or "")[:10]
            if not league_cn:
                continue
            key = (_norm_team_key(home), _norm_team_key(away), league_cn, fixture_date)
            by_teams[key] = {
                "home_absent": m.get("home_absent") or [],
                "away_absent": m.get("away_absent") or [],
                "home": home,
                "away": away,
            }
    except Exception:
        pass
    return exact, fallback, by_teams


class ReportGenerateResponse(BaseModel):
    match_id: int
    report_id: str
    message: str


class ReportGenerateByFixtureResponse(BaseModel):
    match_id: int
    report_id: str
    url: str


class SyncSportteryResponse(BaseModel):
    added: int
    updated_log: int
    total: int
    message: str


class InjuryCrawlResponse(BaseModel):
    ok: bool
    matches_count: int = 0
    message: str


class PredictionsRunResponse(BaseModel):
    """对指定日期 DB/Mongo 场次跑模型预测并写入 prediction_log 的返回。"""
    date: str
    added: int
    skipped: int
    total: int
    message: str


class ScoreBackfillResponse(BaseModel):
    """比分回填：用 DB 中已完赛场次比分更新 prediction_log 的返回。"""
    ok: bool
    added_predictions: int = 0
    updated_log: int = 0
    total_matches: int = 0
    dates: list[str] = []
    message: str


@app.post("/predictions/run", response_model=PredictionsRunResponse, tags=["预测"])
def run_predictions_for_date(
    date_str: str = Query(..., description="比赛日 YYYY-MM-DD"),
) -> PredictionsRunResponse:
    """对指定日期的 MongoDB（或 DB）五大联赛场次用模型预测，结果写入 prediction_log，供 Web 仪表盘展示。"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from main import run_predict_for_date

    date_only = (date_str or "").strip()[:10]
    if len(date_only) != 10:
        raise HTTPException(status_code=400, detail="请提供有效日期 YYYY-MM-DD")
    try:
        added, skipped, total = run_predict_for_date(date_only)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"预测执行失败: {e}")
    msg = f"比赛日 {date_only}：共 {total} 场，新增预测 {added} 条，已存在跳过 {skipped} 场。刷新仪表盘即可查看。"
    return PredictionsRunResponse(date=date_only, added=added, skipped=skipped, total=total, message=msg)


@app.post("/predictions/score-backfill", response_model=ScoreBackfillResponse, tags=["预测"])
def api_score_backfill(
    dates: str | None = Query(None, description="逗号分隔的日期 YYYY-MM-DD，不传则默认昨日/今日/明日"),
) -> ScoreBackfillResponse:
    """用 DB 中已完赛场次的比分回填 prediction_log；未开赛且 log 无预测的会写入预测。默认处理昨日、今日、明日。"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from main import run_auto_accumulate_log

    date_list: list[str] = []
    if dates and (dates := dates.strip()):
        date_list = [d.strip()[:10] for d in dates.split(",") if d.strip()[:10]]
    try:
        added, updated, total = run_auto_accumulate_log(date_list)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"比分回填失败: {e}")
    used = date_list if date_list else ["昨日", "今日", "明日"]
    msg = f"处理 {total} 场：新增预测 {added} 条，回填比分 {updated} 条。刷新页面即可查看。"
    return ScoreBackfillResponse(
        ok=True,
        added_predictions=added,
        updated_log=updated,
        total_matches=total,
        dates=date_list if date_list else [],
        message=msg,
    )


@app.post("/injury-crawl", response_model=InjuryCrawlResponse, tags=["伤停"])
def api_injury_crawl(
    date_str: str | None = Query(None, description="比赛日 YYYY-MM-DD，用于按日期选文章；不传则用今天"),
    url: str | None = Query(None, description="公众号伤停文章 URL，不传则按 date 从 data/injury_urls.json 或默认 URL 解析"),
) -> InjuryCrawlResponse:
    """从公众号（如亚鸣笔记）伤停文章抓图并 OCR 解析，写入 DB（match_injury）；支持按日期自动换文章。"""
    from datetime import date
    from etl.injury_crawl import crawl_injury, save_injury_json

    date_used = (date_str or "").strip()[:10] if date_str else date.today().isoformat()
    source_url = (url or "").strip() or config.get_injury_url_for_date(date_used) or config.INJURY_ARTICLE_URL or ""
    if not source_url:
        raise HTTPException(
            status_code=400,
            detail="未配置伤停文章 URL，请在 .env 设置 INJURY_ARTICLE_URL 或 data/injury_urls.json 的 by_date/default_url，或请求时传入 url 参数",
        )
    try:
        matches = crawl_injury(url=source_url, from_image=None, include_all=False)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"抓取或解析失败: {e}。若公众号环境异常，请用命令行：python3 main.py injury-crawl --from-image <本地图片路径>",
        )
    if not matches:
        return InjuryCrawlResponse(
            ok=False,
            matches_count=0,
            message="已拉取文章图片但 OCR 未解析出任何场次（可能抓到的不是伤停表、或图片被缩小导致表格识别失败）。请用本地图片重试：在浏览器打开文章并截图伤停表，保存后执行 python3 main.py injury-crawl --from-image data/raw/伤停.png",
        )
    save_injury_json(matches, source_url=source_url, merge=False)
    config.save_injury_url_for_date(date_used, source_url)
    big5 = sum(1 for m in matches if m.get("league") in {"英超", "西甲", "意甲", "德甲", "法甲"})
    return InjuryCrawlResponse(
        ok=True,
        matches_count=len(matches),
        message=f"伤停已更新：共 {len(matches)} 场（五大联赛 {big5} 场），请刷新页面查看",
    )


@app.post("/injury-api", response_model=InjuryCrawlResponse, tags=["伤停"])
def api_injury_api(
    date_str: str | None = Query(None, description="比赛日 YYYY-MM-DD，不传则用今天"),
    merge: bool = Query(False, description="是否与现有伤停 JSON 合并（按主客队+联赛去重）"),
) -> InjuryCrawlResponse:
    """从 API-FOOTBALL 拉取五大联赛伤停并写入 DB（match_injury）。需配置 API_FOOTBALL_KEY。"""
    from datetime import date
    from etl.injury_api_football import fetch_and_save

    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        raise HTTPException(
            status_code=400,
            detail="未配置 API_FOOTBALL_KEY 或 API_FOOTBALL_KEY_2，请在 .env 中设置（api-sports.io 注册免费 key）",
        )
    date_used = (date_str or "").strip()[:10] if date_str else date.today().isoformat()
    if len(date_used) != 10:
        raise HTTPException(status_code=400, detail="日期格式需为 YYYY-MM-DD")
    try:
        matches, message = fetch_and_save(date_used, merge=merge)
    except ValueError as e:
        msg = str(e)
        # 免费档/计划限制用 403，与「请求参数错误」的 400 区分
        if "免费档" in msg or "do not have access" in msg or "plan" in msg.lower():
            raise HTTPException(status_code=403, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拉取伤停失败: {e}")
    return InjuryCrawlResponse(
        ok=True,
        matches_count=len(matches),
        message=message,
    )


@app.post("/sync-sporttery", response_model=SyncSportteryResponse, tags=["已废弃"])
def sync_sporttery(
    date_str: str = Query(None, description="已废弃"),
    from_json: str | None = Query(None, description="已废弃"),
    json_body: dict | list | None = Body(None),
) -> SyncSportteryResponse:
    """已废弃：数据改为仅从 API 获取，请使用 ingest 等回填赛果后配合 prediction-log sync-from-db。"""
    raise HTTPException(status_code=410, detail="已废弃，数据请从 API 获取（ingest / 回填赛果后使用 prediction-log sync-from-db）")


@app.post("/sync-sporttery-from-www", response_model=SyncSportteryResponse, tags=["已废弃"])
def sync_sporttery_from_www(
    date_str: str = Query(None, description="已废弃"),
) -> SyncSportteryResponse:
    """已废弃：数据改为仅从 API 获取。"""
    raise HTTPException(status_code=410, detail="已废弃，数据请从 API 获取")


@app.get("/", tags=["根"])
def root() -> dict:
    """服务信息与版本。"""
    return {"service": "football_analysis", "version": "0.1.0"}


def _injury_match_year(m: dict) -> str | None:
    """从场次的 fixture_date 或 match_time 解析出年份（YYYY），无则返回 None。"""
    date_str = (m.get("fixture_date") or m.get("match_time") or "").strip()
    if len(date_str) >= 4 and date_str[:4].isdigit():
        return date_str[:4]
    return None


@app.get("/injury", tags=["伤停"])
def get_injury_data(
    year: int | None = Query(None, description="筛选年份，默认当年"),
    league: str | None = Query(None, description="筛选联赛：英超/西甲/意甲/德甲/法甲，空=全部"),
    limit: int = Query(100, ge=1, le=100000, description="最多返回场次数"),
) -> dict:
    """返回 DB（match_injury）的伤停列表；支持按年份、联赛筛选，默认当年。"""
    try:
        from etl.injury_db import get_injury_matches_from_db
        matches = get_injury_matches_from_db()
        filter_year = year if year is not None else date.today().year
        filter_league = (league or "").strip()
        filtered = []
        for m in matches:
            m_year = _injury_match_year(m)
            if m_year and int(m_year) != filter_year:
                continue
            if filter_league and (m.get("league") or "").strip() != filter_league:
                continue
            filtered.append(m)
        filtered = filtered[:limit]
        # 伤停原因转为中文后再返回
        matches_zh = [_injury_reasons_to_zh(m) if m else m for m in filtered]
        return {
            "updated_at": None,
            "source": "db",
            "source_url": None,
            "matches": matches_zh,
            "total": len(filtered),
            "filters": {"year": filter_year, "league": filter_league or "全部"},
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取伤停数据失败: {e}")


@app.get("/recap-stats", tags=["复盘"])
def get_recap_stats() -> dict:
    """返回 prediction_log 复盘统计：场次、胜平负/精确比分命中率、MAE、按联赛拆解。供仪表盘「预测命中率」展示。"""
    try:
        from engine.prediction_log import generate_recap
        recap = generate_recap(by_competition=True)
        return recap
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成复盘统计失败: {e}")


@app.get("/match-detail", tags=["比赛"])
def get_match_detail(
    match_id: int = Query(..., description="DB match id（体彩详情已废弃，仅返回空）"),
) -> dict:
    """体彩单场详情已废弃，数据从 API 获取；保留接口仅返回空 detail。"""
    return {"match_id": match_id, "sporttery_detail": {}}


def _odds_for_result(odds: dict, result: str | None, prefix: str) -> str | None:
    """胜平负：根据实际结果取对应赔率。result 主/平/客 -> spf_win/spf_draw/spf_loss"""
    if not result:
        return None
    key = "spf_win" if result == "主" else ("spf_draw" if result == "平" else "spf_loss")
    return _str_or_none(odds.get(key))


def _odds_rqspf(odds: dict, h: int | None, a: int | None) -> tuple[str, str | None]:
    """让球胜平负：返回 (结果描述, 赔率)。需 API 提供让球数。"""
    handicap = odds.get("rqspf_handicap")
    if h is not None and a is not None and handicap is not None:
        try:
            hc = int(handicap)
            adj = (h - a) + hc
            res = "主" if adj > 0 else ("客" if adj < 0 else "平")
            key = "rqspf_win" if res == "主" else ("rqspf_draw" if res == "平" else "rqspf_loss")
            od = odds.get(key)
            hc_str = f"+{hc}" if hc > 0 else str(hc)
            return (f"({hc_str}){res}", _str_or_none(od))
        except (TypeError, ValueError):
            pass
    return ("—", _str_or_none(odds.get("rqspf_raw")))


def _odds_bf(odds: dict, h: int | None, a: int | None) -> tuple[str, str | None]:
    """比分：返回 (比分, 赔率)。"""
    if h is None or a is None:
        return ("—", None)
    score_str = f"{h}:{a}"
    bf = odds.get("bf")
    if isinstance(bf, dict):
        od = bf.get(score_str) or bf.get(str(h) + ":" + str(a))
        return (score_str, _str_or_none(od))
    return (score_str, None)


def _odds_zjqs(odds: dict, h: int | None, a: int | None) -> tuple[str, str | None]:
    """总进球数：返回 (进球数, 赔率)。"""
    if h is None or a is None:
        return ("—", None)
    total = h + a
    total_str = str(total) if total <= 7 else "7+"
    zj = odds.get("zjqs") or odds.get("zjqs_raw")
    if isinstance(zj, dict):
        od = zj.get(str(total)) or zj.get(total_str)
        return (str(total), _str_or_none(od))
    return (str(total), None)


def _odds_bqc(odds: dict, hh: int | None, ha: int | None, h: int | None, a: int | None) -> tuple[str, str | None]:
    """半全场胜平负：返回 (如 平平, 赔率)。"""
    def _res(ph, pa):
        if ph is None or pa is None:
            return None
        if ph > pa:
            return "胜"
        if ph < pa:
            return "负"
        return "平"

    half = _res(hh, ha)
    full = _res(h, a)
    if half is None or full is None:
        return ("—", None)
    bqc_str = half + full
    bqc = odds.get("bqc") or odds.get("bqc_raw")
    if isinstance(bqc, dict):
        od = bqc.get(bqc_str)
        return (bqc_str, _str_or_none(od))
    return (bqc_str, None)


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _match_team_names_for_pred(a: str, b: str) -> bool:
    """精确匹配队名：strip 后忽略大小写相等（用于体彩场次与 prediction_log 合并）。"""
    x, y = (a or "").strip(), (b or "").strip()
    if not x or not y:
        return False
    return x.lower() == y.lower()


def _get_half_time_from_db(engine, home: str, away: str, match_date: str) -> tuple[int | None, int | None]:
    """从 DB 中按主客队、日期查找比赛的半场比分。优先使用 football-data（fd_）来源。"""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT source_id, raw_meta FROM matches
                WHERE match_date = :d AND raw_meta IS NOT NULL
                ORDER BY CASE WHEN source_id LIKE 'fd_%' THEN 0 ELSE 1 END
            """),
            {"d": match_date[:10]},
        ).fetchall()
    for r in rows:
        source_id, raw = r[0], r[1]
        if not raw:
            continue
        try:
            meta = json.loads(raw)
        except Exception:
            continue
        h = (meta.get("home_team_name") or "").strip()
        a = (meta.get("away_team_name") or "").strip()
        if not _match_team_names_for_pred(home, h) or not _match_team_names_for_pred(away, a):
            continue
        hh = meta.get("half_time_home")
        ha = meta.get("half_time_away")
        if hh is not None and ha is not None:
            try:
                return int(hh), int(ha)
            except (TypeError, ValueError):
                pass
    return None, None


def _get_fulltime_from_db(engine, home: str, away: str, match_date: str) -> tuple[int | None, int | None]:
    """从 DB 中按主客队、日期查找比赛的全场比分（用于体彩接口无比分时用已同步的 DB 补全）。"""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT home_score, away_score, raw_meta FROM matches
                WHERE match_date = :d AND raw_meta IS NOT NULL
                ORDER BY CASE WHEN source_id LIKE 'fd_%' THEN 0 WHEN source_id LIKE 'manual_%' THEN 1 ELSE 2 END
            """),
            {"d": match_date[:10]},
        ).fetchall()
    for r in rows:
        hs, aws, raw = r[0], r[1], r[2]
        if raw is None or hs is None or aws is None:
            continue
        try:
            meta = json.loads(raw)
        except Exception:
            continue
        h = (meta.get("home_team_name") or "").strip()
        a = (meta.get("away_team_name") or "").strip()
        if not _match_team_names_for_pred(home, h) or not _match_team_names_for_pred(away, a):
            continue
        try:
            return int(hs), int(aws)
        except (TypeError, ValueError):
            pass
    return None, None


def _find_prediction_entry(preds: list[dict], home: str, away: str, match_date: str) -> dict | None:
    """在 prediction_log 中找到与主客队、日期匹配的预测。"""
    for p in preds:
        if (p.get("date") or "")[:10] != (match_date or "")[:10]:
            continue
        if not _match_team_names_for_pred(home, p.get("home_team") or ""):
            continue
        if not _match_team_names_for_pred(away, p.get("away_team") or ""):
            continue
        return p
    return None


def _find_prediction_entry_by_match_id(preds: list[dict], match_id) -> dict | None:
    """按 match_id 在 prediction_log 中查找预测（队名不一致时兜底）。"""
    if match_id is None:
        return None
    for p in preds:
        if p.get("match_id") == match_id:
            return p
    return None


_COMP_EN_FROM_CN = {"英超": "Premier League", "西甲": "La Liga", "意甲": "Serie A", "德甲": "Bundesliga", "法甲": "Ligue 1"}


@app.get("/predictions", tags=["预测"])
def list_predictions(
    date_str: str | None = Query(None, description="YYYY-MM-DD，比赛日（默认今天）；从 DB 取该日五大联赛场次"),
    mode: str = Query("exact", pattern="^(exact|until)$", description="exact=仅该日比赛，until=该日及之前"),
    date_mode: str = Query("exact", pattern="^(exact|jingcai)$", description="exact=所选日期即比赛日（直接查 MongoDB）；jingcai=所选日期+1 为比赛日"),
    high_confidence_only: bool = Query(False, description="主推高置信度：仅返回 high_confidence=True 或 confidence=high 的场次"),
    league: str | None = Query(None, description="按联赛筛选：联赛中文名或英文名，如 英超、Premier League"),
) -> dict:
    """从 DB（MongoDB）获取指定日期的五大联赛场次，与 prediction_log 合并展示。支持主推高置信度、按联赛分层筛选。"""
    list_date = (date_str or str(date.today()))[:10]
    if len(list_date) != 10:
        return {"predictions": [], "date_filter": list_date, "list_date": list_date, "source": "db"}
    match_date = list_date if date_mode == "exact" else _jingcai_day_to_match_day(list_date)
    try:
        from db import repo
        repo.init_db()
        matches = repo.get_db_matches_for_date_with_scores(match_date)
    except Exception:
        matches = []
    preds = []
    try:
        from engine.prediction_log import load_log
        from db import repo as _repo
        _repo.init_db()
        engine = _repo.get_engine()  # None 当 USE_MONGO，保证预测读写与 matches 同源
        preds = load_log(engine)
    except Exception:
        pass
    injury_exact, injury_fallback, injury_by_teams = _load_injury_data()
    injury_fetching = False  # 是否已触发后台拉取该日伤停（前端可提示「稍后刷新」）
    if matches:
        try:
            from db import repo as _repo
            # 仅当开启「自动拉取」且该日在 DB 中确实无伤停记录时才请求 API，避免重复消耗 key（伤停建议用 main.py injury-api 按需拉取）
            if getattr(config, "PREDICTIONS_AUTO_FETCH_INJURY", False) and not _repo.injury_has_date(match_date):
                injury_fetching = True
                import threading
                def _fetch_injury_background():
                    try:
                        from etl.injury_api_football import fetch_and_save
                        fetch_and_save(match_date, merge=True)
                    except Exception:
                        pass
                threading.Thread(target=_fetch_injury_background, daemon=True).start()
        except Exception:
            pass
    out = []
    for m in matches:
        mdate = (m.get("date") or match_date)[:10]
        if mode == "exact" and mdate != match_date:
            continue
        if mode == "until" and mdate > match_date:
            continue
        home = (m.get("home_name") or "").strip()
        away = (m.get("away_name") or "").strip()
        home_cn = (m.get("home_name_zh") or "").strip()
        away_cn = (m.get("away_name_zh") or "").strip()
        league_cn = (m.get("league_cn") or "").strip()
        comp = _COMP_EN_FROM_CN.get(league_cn, "Premier League")
        # 优先按 match_id 从 MongoDB predictions 取，避免每次打开页面都重新调 API 生成预测
        entry = None
        if m.get("match_id") is not None:
            try:
                from db import repo as _repo
                if _repo.use_mongo():
                    entry = _repo.predictions_get_entry_by_match_id(m.get("match_id"), home, away, mdate)
            except Exception:
                pass
        if entry is None:
            entry = _find_prediction_entry(preds, home, away, mdate)
        if entry is None and m.get("match_id") is not None:
            entry = _find_prediction_entry_by_match_id(preds, m.get("match_id"))
        # 若无预测记录且队伍在 DB 中，自动生成预测并写入 prediction_log（会调 API 拉赔率/积分榜等）；传入 match_id 确保写入 predictions 后再次打开能命中、不再重复调 API
        if entry is None:
            try:
                from engine.prediction_log import append_prediction, set_actual_by_match, load_log
                from db import repo as _repo
                _repo.init_db()
                eng = _repo.get_engine()
                result = append_prediction(
                    eng, home_team=home, away_team=away, competition=comp,
                    prediction_date=mdate, last_n=10,
                    match_id=m.get("match_id"),
                )
                if result is not None:
                    hs, aws = m.get("home_score"), m.get("away_score")
                    if hs is not None and aws is not None:
                        set_actual_by_match(home, away, hs, aws, match_date=mdate)
                    preds = load_log(eng)
                    entry = _find_prediction_entry(preds, home, away, mdate)
            except Exception:
                pass
        # 主队/客队展示：英文名/中文名（有中文时），否则仅英文
        home_display = f"{home}/{home_cn}" if home_cn and home_cn != home else (home_cn or home)
        away_display = f"{away}/{away_cn}" if away_cn and away_cn != away else (away_cn or away)
        item = {
            "date": mdate,
            "match_num": None,
            "match_time": None,
            "home_team": home_display,
            "away_team": away_display,
            "home_team_match": home,
            "away_team_match": away,
            "competition": comp,
            "competition_display": league_cn or _COMPETITION_TO_LEAGUE_CN.get(comp, comp),
            "match_id": m.get("match_id"),
            "sporttery_detail": None,
            "actual_home_score": m.get("home_score"),
            "actual_away_score": m.get("away_score"),
            "actual_result": None,
        }
        if item["actual_home_score"] is not None and item["actual_away_score"] is not None:
            h, a = item["actual_home_score"], item["actual_away_score"]
            item["actual_result"] = "主" if h > a else ("客" if a > h else "平")
        else:
            try:
                from db import repo as _repo
                _repo.init_db()
                eng = _repo.get_engine()
                if eng is not None:
                    db_hs, db_aws = _get_fulltime_from_db(eng, home, away, mdate)
                    if db_hs is not None and db_aws is not None:
                        item["actual_home_score"] = db_hs
                        item["actual_away_score"] = db_aws
                        item["actual_result"] = "主" if db_hs > db_aws else ("客" if db_aws > db_hs else "平")
            except Exception:
                pass
        if item["actual_home_score"] is None and item["actual_away_score"] is None and entry:
            eh, ea = entry.get("actual_home_score"), entry.get("actual_away_score")
            if eh is not None and ea is not None:
                item["actual_home_score"] = int(eh)
                item["actual_away_score"] = int(ea)
                item["actual_result"] = "主" if item["actual_home_score"] > item["actual_away_score"] else ("客" if item["actual_away_score"] > item["actual_home_score"] else "平")
        hh, ha = m.get("half_home"), m.get("half_away")
        if hh is None or ha is None:
            hh, ha = None, None
            try:
                from db import repo as _repo
                _repo.init_db()
                eng = _repo.get_engine()
                if eng is not None:
                    hh, ha = _get_half_time_from_db(eng, home, away, mdate)
            except Exception:
                pass
        item["half_home"] = hh
        item["half_away"] = ha
        # 赔率：仅从 DB match_odds 取，不在此处调 API（避免每次打开页面都请求；赔率请用 main.py odds-api / sporttery-odds-to-db 等预先拉取）
        odds = {}
        try:
            from db import repo as _repo
            mid = item.get("match_id")
            if mid and _repo.use_mongo():
                o = _repo.match_odds_load_by_match_id(mid)
            else:
                o = None
            # 若按 match_id 查到的赔率没有让球，尝试按「日期+主客队+联赛」再查（体彩可能用中文队名存，可补全让球）
            if o and (o.get("spf_win") or o.get("spf_draw") or o.get("spf_loss")) and o.get("rqspf_handicap") is None and o.get("ah_handicap") is None:
                for h, a in [(home, away), (home_cn or home, away_cn or away)]:
                    if not h or not a:
                        continue
                    o2 = _repo.match_odds_load_for_match(h.strip(), a.strip(), league_cn or "", mdate[:10] if mdate else None)
                    if o2 and (o2.get("rqspf_handicap") is not None or o2.get("ah_handicap") is not None):
                        if o2.get("rqspf_handicap") is not None:
                            o["rqspf_handicap"] = o2["rqspf_handicap"]
                            o["rqspf_win"] = o2.get("rqspf_win")
                            o["rqspf_draw"] = o2.get("rqspf_draw")
                            o["rqspf_loss"] = o2.get("rqspf_loss")
                        if o2.get("ah_handicap") is not None:
                            o["ah_handicap"] = o2["ah_handicap"]
                            o["ah_home"] = o2.get("ah_home")
                            o["ah_away"] = o2.get("ah_away")
                        break
            if o and (o.get("spf_win") or o.get("spf_draw") or o.get("spf_loss")):
                odds = {"win": o.get("spf_win"), "draw": o.get("spf_draw"), "loss": o.get("spf_loss")}
                if o.get("rqspf_handicap") is not None:
                    odds["rqspf_handicap"] = o.get("rqspf_handicap")
                    odds["rqspf_win"] = o.get("rqspf_win")
                    odds["rqspf_draw"] = o.get("rqspf_draw")
                    odds["rqspf_loss"] = o.get("rqspf_loss")
                if o.get("ah_handicap") is not None:
                    odds["ah_handicap"] = o.get("ah_handicap")
                    odds["ah_home"] = o.get("ah_home")
                    odds["ah_away"] = o.get("ah_away")
        except Exception:
            pass
        item["odds"] = odds if odds else None
        rq_handicap = odds.get("rqspf_handicap")
        ah_handicap = odds.get("ah_handicap")
        if rq_handicap is not None:
            try:
                hc = int(rq_handicap) if isinstance(rq_handicap, (int, float)) and float(rq_handicap) == int(float(rq_handicap)) else float(rq_handicap)
                item["rqspf_handicap"] = hc
            except (TypeError, ValueError):
                item["rqspf_handicap"] = None
        else:
            item["rqspf_handicap"] = None
        if ah_handicap is not None:
            item["ah_handicap"] = ah_handicap
        else:
            item["ah_handicap"] = None
        # 用于让球预测的数值：优先竞彩让球数，无则用亚盘让球（如 -0.5）；若预测表已存让球数则优先用表内（竞彩接入）
        handicap_for_pred = rq_handicap
        if handicap_for_pred is None and ah_handicap is not None:
            try:
                handicap_for_pred = float(str(ah_handicap).strip())
                item["rqspf_handicap"] = handicap_for_pred  # 前端用同一字段展示让球数（亚盘时显示 -0.5 等）
            except (TypeError, ValueError):
                pass
        if entry and entry.get("rqspf_handicap") is not None:
            item["rqspf_handicap"] = entry["rqspf_handicap"]
            handicap_for_pred = entry["rqspf_handicap"]
        # 无竞彩/亚盘让球时：用模型期望进球差推算「模型让球数」，使让球列与让球预测有数可显
        # 竞彩约定：主队让 N 球 = -N（主强时 round(eh-ea)>0，应存为负数），与 _odds_rqspf 的 adj=(h-a)+hc 一致
        if handicap_for_pred is None and entry:
            eh, ea = entry.get("exp_goals_home"), entry.get("exp_goals_away")
            if eh is not None and ea is not None:
                try:
                    diff = float(eh) - float(ea)
                    hc_model = -int(round(diff))  # 主强则主让球，让球数为负
                    if -15 <= hc_model <= 15:
                        item["rqspf_handicap"] = hc_model
                        item["rqspf_handicap_source"] = "model"
                        handicap_for_pred = hc_model
                except (TypeError, ValueError):
                    pass
        # 上场人数 + 首发名单：优先从 match_lineups 读，无则用 source_id 调 API 拉取并写入
        item["home_lineup_count"] = None
        item["away_lineup_count"] = None
        item["home_lineup"] = None
        item["away_lineup"] = None
        try:
            from db import repo as _repo
            mid = item.get("match_id")
            if mid:
                cached = None
                if _repo.use_mongo():
                    cached = _repo.lineups_get_by_match_id(mid)
                # 竞彩今日的 match_id 为 sporttery_match_id，阵容可能在 self_improvement_football.match_lineups
                if not cached or (cached.get("home_starting_count") is None and cached.get("away_starting_count") is None and not cached.get("home_starting") and not cached.get("away_starting")):
                    try:
                        from db import self_improvement as _si
                        if _si.get_self_improvement_db():
                            cached = _si.lineups_get_by_match_id(mid)
                    except Exception:
                        cached = None
                if cached and (cached.get("home_starting_count") is not None or cached.get("away_starting_count") is not None or cached.get("home_starting") or cached.get("away_starting")):
                    item["home_lineup_count"] = cached.get("home_starting_count")
                    item["away_lineup_count"] = cached.get("away_starting_count")
                    item["home_lineup"] = cached.get("home_starting") or []
                    item["away_lineup"] = cached.get("away_starting") or []
            # 不在此处调 API 拉阵容，避免每次打开页面都请求；阵容请用 main.py lineups-by-jingcai 预先拉取并落库
        except Exception:
            pass
        # 让球预测：优先用预测表已存的让球模型预测（竞彩接入）；否则用期望进球 + 让球数 现场算
        item["predicted_rqspf"] = (entry.get("predicted_rqspf") if entry else None) or None
        if item["predicted_rqspf"] is None and handicap_for_pred is not None and entry:
            eh, ea = entry.get("exp_goals_home"), entry.get("exp_goals_away")
            if eh is not None and ea is not None:
                try:
                    hc = float(handicap_for_pred)
                    adj_h = eh + hc
                    if adj_h > ea:
                        item["predicted_rqspf"] = "主"
                    elif adj_h < ea:
                        item["predicted_rqspf"] = "客"
                    else:
                        item["predicted_rqspf"] = "平"
                except (TypeError, ValueError):
                    pass
        if entry:
            item["exp_goals_home"] = entry.get("exp_goals_home")
            item["exp_goals_away"] = entry.get("exp_goals_away")
            ps = entry.get("predicted_scores")
            if ps:
                item["predicted_scores"] = ps
            else:
                eh, ea = entry.get("exp_goals_home"), entry.get("exp_goals_away")
                item["predicted_scores"] = [[round(eh), round(ea)]] if eh is not None and ea is not None else []
            item["prob_home_win"] = entry.get("prob_home_win")
            item["prob_draw"] = entry.get("prob_draw")
            item["prob_away_win"] = entry.get("prob_away_win")
            item["predicted_result"] = entry.get("predicted_result")
            item["model_predicted_result"] = entry.get("model_predicted_result")
            item["model_predicted_scores"] = entry.get("model_predicted_scores")
            item["api_predicted_result"] = entry.get("api_predicted_result")
            item["api_predicted_scores"] = entry.get("api_predicted_scores")
            # 旧数据未单独存模型/API 时：用融合结果回填「模型预测」，避免该列为空
            if item["model_predicted_result"] is None and item["predicted_result"] is not None:
                item["model_predicted_result"] = item["predicted_result"]
                item["model_predicted_scores"] = item.get("predicted_scores") or entry.get("predicted_scores")
            probs = [entry.get("prob_home_win"), entry.get("prob_draw"), entry.get("prob_away_win")]
            item["high_confidence"] = max(p for p in probs if p is not None) >= 0.55 if any(p is not None for p in probs) else False
            item["confidence"] = entry.get("confidence", "high")  # low = 样本不足或期望进球接近，仅供参考
            item["is_boundary"] = bool(entry.get("is_boundary", False))  # 边界场次，势均力敌略偏主，产品可标「仅供参考」
            # 多窗口一致：last_n=5 与 last_n=10 预测结果一致则标 True
            item["agreement_5_10"] = False
            item["odds_used"] = entry.get("odds_used")
            try:
                from engine.predict import predict_match, resolve_team_by_name, resolve_competition_by_name
                from db import repo as _repo
                _repo.init_db()
                _eng = _repo.get_engine()
                home_id = resolve_team_by_name(_eng, home)
                away_id = resolve_team_by_name(_eng, away)
                comp_id = resolve_competition_by_name(_eng, comp)
                if home_id is not None and away_id is not None:
                    res5 = predict_match(_eng, home_id, away_id, competition_id=comp_id, last_n=5)
                    sh, sa = res5["predicted_score"][0], res5["predicted_score"][1]
                    outcome_5 = "主" if sh > sa else ("客" if sa > sh else "平")
                    item["agreement_5_10"] = (outcome_5 == entry.get("predicted_result"))
            except Exception:
                pass
        else:
            item["exp_goals_home"] = None
            item["exp_goals_away"] = None
            item["predicted_scores"] = []
            item["prob_home_win"] = None
            item["prob_draw"] = None
            item["prob_away_win"] = None
            item["predicted_result"] = None
            item["model_predicted_result"] = None
            item["model_predicted_scores"] = None
            item["api_predicted_result"] = None
            item["api_predicted_scores"] = None
            item["high_confidence"] = False
            item["confidence"] = "high"
            item["is_boundary"] = False
            item["agreement_5_10"] = False
            item["odds_used"] = False
        # 主客队优势：由预测概率推导 (prob_home_win - prob_away_win)*100，正值主队占优、负值客队占优
        ph, pa = item.get("prob_home_win"), item.get("prob_away_win")
        if ph is not None and pa is not None:
            item["home_advantage"] = round((float(ph) - float(pa)) * 100, 1)
        else:
            item["home_advantage"] = None
        # 合并伤停数据：先按 match_num 精确匹配，再按「场次号后缀+联赛」匹配，最后按「主客队+联赛」匹配（API-FOOTBALL 等无场次号来源）
        match_num = (item.get("match_num") or "").strip()
        comp = (item.get("competition") or "").strip()
        pred_home = (item.get("home_team") or "").strip()
        pred_away = (item.get("away_team") or "").strip()
        home_match = (item.get("home_team_match") or "").strip()
        away_match = (item.get("away_team_match") or "").strip()
        league_cn = _COMPETITION_TO_LEAGUE_CN.get(comp, "")
        injury = injury_exact.get(match_num) if match_num else None
        if not injury and match_num and comp:
            suffix = match_num[-3:] if len(match_num) >= 3 and match_num[-3:].isdigit() else ""
            if suffix and league_cn:
                candidates = injury_fallback.get((suffix, league_cn)) or []
                for cand in candidates:
                    if _teams_match(pred_home, pred_away, cand.get("home") or "", cand.get("away") or ""):
                        injury = cand
                        break
        if not injury and league_cn and (home_match or away_match):
            match_date = (item.get("date") or "")[:10]
            nh, na = _norm_team_key(home_match), _norm_team_key(away_match)
            key = (nh, na, league_cn, match_date)
            injury = injury_by_teams.get(key)
            # 无当日记录时：同主客队+联赛任意日期的伤停也显示（回退）
            if not injury and (nh or na):
                for k, v in injury_by_teams.items():
                    if k[2] != league_cn or not k[0] or not k[1]:
                        continue
                    if (k[0] in nh or nh in k[0]) and (k[1] in na or na in k[1]):
                        injury = v
                        break
        if injury and (injury.get("home_absent") or injury.get("away_absent")):
            inj_home = injury.get("home") or ""
            inj_away = injury.get("away") or ""
            # 用与查找一致的队名（home_match/away_match）校验，避免展示名含中文导致误清空
            if not _teams_match(home_match, away_match, inj_home, inj_away) and not _teams_match(pred_home, pred_away, inj_home, inj_away):
                injury = None
        # 返回前将伤停原因转为中文，供前端展示
        item["injury"] = _injury_reasons_to_zh(injury) if injury and (injury.get("home_absent") or injury.get("away_absent")) else None
        # 伤停已统一在 predict_match 内处理（append_prediction 会传入伤停），此处仅展示伤停信息，不再二次下调

        # 模型分析用数据：近期战绩、历史交锋、均进均失、积分榜排名
        try:
            from engine.predict import resolve_team_by_name, resolve_competition_by_name
            from db import repo as _repo
            _repo.init_db()
            eng = _repo.get_engine()
            home_id = resolve_team_by_name(eng, home_match)
            away_id = resolve_team_by_name(eng, away_match)
            comp_id = resolve_competition_by_name(eng, comp)
            if home_id is not None and away_id is not None:
                item["form_home_5"] = _repo.predict_recent_form_string(home_id, comp_id, 5, at_home=True)
                item["form_away_5"] = _repo.predict_recent_form_string(away_id, comp_id, 5, at_home=False)
                item["h2h_matches"] = _repo.predict_h2h_matches_list(home_id, away_id, comp_id, 6)
                fh = _repo.predict_team_recent_form(home_id, comp_id, 5, at_home=True)
                fa = _repo.predict_team_recent_form(away_id, comp_id, 5, at_home=False)
                item["avg_scored_home"] = round(fh[0], 2) if fh[2] else None
                item["avg_conceded_home"] = round(fh[1], 2) if fh[2] else None
                item["avg_scored_away"] = round(fa[0], 2) if fa[2] else None
                item["avg_conceded_away"] = round(fa[1], 2) if fa[2] else None
                match_date_str = (item.get("date") or "")[:10]
                if len(match_date_str) == 10:
                    item["home_days_rest"] = _repo.predict_days_since_last_match(home_id, match_date_str, comp_id)
                    item["away_days_rest"] = _repo.predict_days_since_last_match(away_id, match_date_str, comp_id)
            mid = item.get("match_id")
            if mid:
                hr, ar = _repo.standings_get_for_match(mid)
                if hr is not None or ar is not None:
                    item["home_rank"] = hr
                    item["away_rank"] = ar
        except Exception:
            item.setdefault("form_home_5", None)
            item.setdefault("form_away_5", None)
            item.setdefault("h2h_matches", [])
            item.setdefault("avg_scored_home", None)
            item.setdefault("avg_conceded_home", None)
            item.setdefault("avg_scored_away", None)
            item.setdefault("avg_conceded_away", None)
            item.setdefault("home_days_rest", None)
            item.setdefault("away_days_rest", None)

        out.append(item)
    if high_confidence_only:
        out = [i for i in out if i.get("high_confidence") or (i.get("confidence") or "high") == "high"]
    if league and (league := (league or "").strip()):
        def _match_league(item: dict) -> bool:
            disp = (item.get("competition_display") or "").strip()
            comp = (item.get("competition") or "").strip()
            return league in (disp, comp) or disp == league or comp == league
        out = [i for i in out if _match_league(i)]
    if mode == "until":
        out.sort(key=lambda x: x.get("date") or "", reverse=True)
    # 若本页已有任意一场匹配到伤停（含按主客队+联赛回退），不再提示「拉取中」，避免用户已拉过数据仍被要求拉取
    if injury_fetching and any(p.get("injury") for p in out):
        injury_fetching = False
    return {"predictions": out, "date_filter": match_date, "list_date": list_date, "source": "db", "injury_fetching": injury_fetching}


@app.get("/predictions/accuracy", tags=["预测"])
def get_predictions_accuracy(year: int | None = None) -> dict:
    """统计已完赛场次中：模型预测、API 预测、融合预测的胜平负命中率。仅 MongoDB 有效。year=2026 时只统计该年。"""
    empty = {"model": {"hit": 0, "total": 0, "rate": None}, "api": {"hit": 0, "total": 0, "rate": None}, "fused": {"hit": 0, "total": 0, "rate": None}}
    try:
        from db import repo
        repo.init_db()
        if not repo.use_mongo():
            return empty
        stats = repo.predictions_accuracy_stats(year=year)
        if stats:
            return stats
        return empty
    except Exception:
        return empty


class RepredictBody(BaseModel):
    match_id: int


@app.post("/predictions/repredict", tags=["预测"])
def repredict_match(body: RepredictBody) -> dict:
    """
    对指定比赛重新拉取积分榜/赔率（及可选阵容）并重新预测。用于未来场次「重新预测」按钮。
    会请求 API-Football（积分榜、赔率、可选阵容），消耗 key 次数。
    """
    match_id = body.match_id
    try:
        from db import repo
        repo.init_db()
        if not repo.use_mongo():
            return {"ok": False, "error": "仅支持 MongoDB 环境"}
        m = repo.get_match_by_id(match_id)
        if not m:
            return {"ok": False, "error": "比赛不存在"}
        db = repo._db()
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        home_name = (m.get("home_team_name") or "").strip()
        away_name = (m.get("away_team_name") or "").strip()
        if not home_name or not away_name:
            for tid in [home_id, away_id]:
                if tid is None:
                    continue
                t = db["teams"].find_one({"id": tid}, {"name": 1})
                if t and not (home_name if tid == home_id else away_name):
                    if tid == home_id:
                        home_name = (t.get("name") or "").strip()
                    else:
                        away_name = (t.get("name") or "").strip()
        if not home_name:
            home_name = "?"
        if not away_name:
            away_name = "?"
        comp_id = m.get("competition_id")
        comp_name = (repo.get_competition_name(comp_id) or "").strip() if comp_id is not None else ""
        league_cn = _COMP_EN_FROM_CN.get(comp_name) or ""
        if not league_cn and comp_name:
            for cn, en in _COMP_EN_FROM_CN.items():
                if en == comp_name:
                    league_cn = cn
                    break
        match_date = (m.get("match_date") or "")[:10]
        comp = comp_name or "Premier League"
        src = (m.get("source_id") or "").strip()

        # 1) 积分榜：拉取当季并回填该场 home_rank/away_rank
        try:
            y, mon = int((match_date or "2025-01-01")[:4]), int((match_date or "2025-06-01")[5:7] or 0)
            season = y if mon >= 8 else y - 1
            AF_COMP_OFFSET = 6000
            league_id = (comp_id - AF_COMP_OFFSET) if (comp_id is not None and comp_id >= AF_COMP_OFFSET) else None
            if league_id is not None and league_cn:
                from etl.api_football_standings import fetch_and_save_standings
                from etl.api_football_standings import get_team_rank, load_standings
                from etl.api_football_standings import LEAGUE_CN_TO_ID
                fetch_and_save_standings(season)
                lid = LEAGUE_CN_TO_ID.get(league_cn) or league_id
                by_league = load_standings(season)
                st_list = (by_league or {}).get(lid) or []
                if st_list:
                    hr = get_team_rank(st_list, home_name)
                    ar = get_team_rank(st_list, away_name)
                    if hr is not None or ar is not None:
                        repo.standings_update_match_ranks(match_id, hr, ar)
        except Exception:
            pass

        # 2) 赔率：若有 source_id (af_xxx) 则拉取并写入 match_odds 并关联 match_id
        try:
            if src.startswith("af_") and src[3:].isdigit():
                fid = int(src[3:])
                from etl.api_football_odds import fetch_odds_for_fixture
                odds = fetch_odds_for_fixture(fid)
                if odds and (odds.get("spf_win") or odds.get("spf_draw") or odds.get("spf_loss")):
                    from db.repo import match_odds_upsert_one, match_odds_set_match_id
                    log_d = match_date[:10]
                    match_odds_upsert_one(
                        log_d, home_name, away_name, league_cn or "英超",
                        str(odds.get("spf_win") or ""), str(odds.get("spf_draw") or ""), str(odds.get("spf_loss") or ""),
                        source="api_football",
                        ah_handicap=odds.get("ah_handicap"),
                        ah_home=odds.get("ah_home"),
                        ah_away=odds.get("ah_away"),
                    )
                    match_odds_set_match_id(log_d, home_name, away_name, league_cn or "英超", match_id)
        except Exception:
            pass

        # 3) 阵容：若有 source_id 可拉取并落库（可选）
        try:
            if src.startswith("af_") and src[3:].isdigit():
                fid = int(src[3:])
                from etl.api_football_lineups import fetch_lineups_for_fixture
                lu = fetch_lineups_for_fixture(fid, full=True)
                if lu and (lu.get("home_starting_count") is not None or lu.get("away_starting_count") is not None):
                    from db.repo import lineups_save
                    lineups_save(
                        match_id,
                        home_starting_count=lu.get("home_starting_count"),
                        away_starting_count=lu.get("away_starting_count"),
                        home_starting=lu.get("home_starting"),
                        away_starting=lu.get("away_starting"),
                        home_substitutes=lu.get("home_substitutes"),
                        away_substitutes=lu.get("away_substitutes"),
                    )
        except Exception:
            pass

        # 4) 重新预测并写入 predictions + prediction_log
        from engine.prediction_log import append_prediction
        from db import repo as _repo
        eng = _repo.get_engine()
        result = append_prediction(
            eng, home_team=home_name, away_team=away_name, competition=comp,
            prediction_date=match_date, last_n=10, match_id=match_id,
        )
        if result is None:
            return {"ok": False, "error": "解析队伍失败或无法生成预测"}

        entry = _repo.predictions_get_entry_by_match_id(match_id, home_name, away_name, match_date)
        return {"ok": True, "prediction": entry}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _report_date(report_id: str, engine) -> str | None:
    """从 report_id 解析出报告对应的比赛日（YYYY-MM-DD），用于按日期筛选。"""
    if report_id.startswith("recap_"):
        m = re.match(r"recap_(\d{4}-\d{2}-\d{2})", report_id)
        return m.group(1) if m else None
    if report_id.startswith("match_"):
        try:
            mid = int(report_id[6:].split("_")[0])
        except ValueError:
            return None
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT match_date FROM matches WHERE id = :id"),
                {"id": mid},
            ).fetchone()
        if row and row[0]:
            return (row[0] or "")[:10]
        return None
    return None


@app.get("/report-list", tags=["报告"])
def list_reports(
    date_str: str | None = Query(None, description="只返回该比赛日的报告，YYYY-MM-DD"),
) -> list[dict]:
    """列出已有复盘报告（report_id 及 HTML 链接）。若传 date_str 则只返回该日的报告。"""
    engine = get_engine()
    out = []
    for p in REPORTS_DIR.glob("*.html"):
        report_id = p.stem
        if date_str:
            report_date = _report_date(report_id, engine)
            if (report_date or "")[:10] != (date_str or "")[:10]:
                continue
        out.append({"report_id": report_id, "url": f"/reports/{report_id}?format=html"})
    return sorted(out, key=lambda x: x["report_id"])


@app.get("/matches", tags=["比赛"])
def list_matches(
    competition_id: int | None = Query(None),
    season_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    """List matches in DB with optional competition/season filter."""
    engine = get_engine()
    with engine.connect() as conn:
        q = "SELECT id, source_id, match_date, home_team_id, away_team_id, home_score, away_score, raw_meta FROM matches WHERE 1=1"
        params = {}
        if competition_id is not None:
            q += " AND competition_id = :cid"
            params["cid"] = competition_id
        if season_id is not None:
            q += " AND season_id = :sid"
            params["sid"] = season_id
        q += " ORDER BY match_date DESC LIMIT :limit"
        params["limit"] = limit
        rows = conn.execute(text(q), params).fetchall()
    out = []
    for r in rows:
        meta = {}
        if r[7]:
            try:
                meta = json.loads(r[7])
            except Exception:
                pass
        out.append({
            "id": r[0],
            "source_id": r[1],
            "match_date": r[2],
            "home_team_id": r[3],
            "away_team_id": r[4],
            "home_score": r[5],
            "away_score": r[6],
            "home_team_name": meta.get("home_team_name", ""),
            "away_team_name": meta.get("away_team_name", ""),
        })
    return out


def _match_team_names(a: str, b: str) -> bool:
    """精确匹配队名：strip 后忽略大小写相等。"""
    x, y = (a or "").strip(), (b or "").strip()
    if not x or not y:
        return False
    return x.lower() == y.lower()


@app.post("/reports/generate", response_model=ReportGenerateResponse, tags=["报告"])
def generate_report(match_id: int = Query(..., description="DB match id")) -> ReportGenerateResponse:
    """Generate report for a match, persist as MD and HTML, return report_id."""
    engine = get_engine()
    payload = build_report_payload(engine, match_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Match not found or no data")
    assembler = ReportAssembler()
    md = assembler.generate(payload)
    report_id = f"match_{match_id}"
    md_path = REPORTS_DIR / f"{report_id}.md"
    html_path = REPORTS_DIR / f"{report_id}.html"
    save_report_md(md, md_path)
    save_report_html(md, html_path)
    return ReportGenerateResponse(
        match_id=match_id,
        report_id=report_id,
        message=f"Report saved to {md_path} and {html_path}",
    )


def _recap_id_from_fixture(date_str: str, home: str, away: str) -> str:
    """生成简要复盘的 report_id（文件名安全）。"""
    raw = f"{date_str}_{home}_{away}"
    safe = re.sub(r"[^\w\-]", "_", raw)[:120]
    return f"recap_{safe}" if safe else "recap"


def _analyze_prediction_vs_actual(entry: dict) -> str:
    """根据预测与实际对比，生成「为什么会出现这样子的结果」的简要分析。"""
    pred = (entry.get("predicted_result") or "").strip()
    actual = (entry.get("actual_result") or "").strip()
    exp_h = entry.get("exp_goals_home")
    exp_a = entry.get("exp_goals_away")
    ah = entry.get("actual_home_score")
    aa = entry.get("actual_away_score")
    if actual in ("", "-") or (ah is None and aa is None):
        return "暂无实际结果，无法做差异分析。"
    home = entry.get("home_team") or "主队"
    away = entry.get("away_team") or "客队"
    lines = []
    if pred == actual:
        lines.append("预测与最终结果一致。模型基于双方近期表现给出的期望进球与胜平负判断，与本场实际走势相符。")
        if exp_h is not None and exp_a is not None and ah is not None and aa is not None:
            if abs(ah - exp_h) <= 0.5 and abs(aa - exp_a) <= 0.5:
                lines.append("主客队实际进球与期望值接近，说明本场发挥符合近期趋势。")
            elif ah > exp_h or aa > exp_a:
                lines.append("实际总进球高于期望，本场进攻端表现略好于近期平均水平。")
            else:
                lines.append("实际进球低于期望，本场防守或机会把握与近期略有差异。")
        return " ".join(lines)
    lines.append("预测与结果不一致，差异分析如下：")
    if exp_h is not None and exp_a is not None and ah is not None and aa is not None:
        diff_h = ah - exp_h
        diff_a = aa - exp_a
        if diff_h > 0.5:
            lines.append(f"{home} 实际进球（{ah}）高于期望（{exp_h:.1f}），进攻或机会转化好于模型依据近期数据给出的估计。")
        elif diff_h < -0.5:
            lines.append(f"{home} 实际进球（{ah}）低于期望（{exp_h:.1f}），未达到近期表现对应的预期。")
        if diff_a > 0.5:
            lines.append(f"{away} 实际进球（{aa}）高于期望（{exp_a:.1f}），客队本场发挥优于模型预期。")
        elif diff_a < -0.5:
            lines.append(f"{away} 实际进球（{aa}）低于期望（{exp_a:.1f}），客队未打出近期水平。")
    if pred == "主" and actual == "平":
        lines.append("预测主胜而实际平局：主队未能将期望优势转化为胜利，或客队抵抗/反击好于预期。")
    elif pred == "主" and actual == "客":
        lines.append("预测主胜而实际客胜：客队表现明显好于基于近期数据的估计，或主队临场发挥不及预期。")
    elif pred == "客" and actual == "平":
        lines.append("预测客胜而实际平局：客队未能拉开差距，或主队表现好于模型预期。")
    elif pred == "客" and actual == "主":
        lines.append("预测客胜而实际主胜：主队本场发挥明显优于近期趋势，或客队未达预期。")
    elif pred == "平" and actual == "主":
        lines.append("预测平局而实际主胜：主队进攻或关键机会把握好于预期。")
    elif pred == "平" and actual == "客":
        lines.append("预测平局而实际客胜：客队本场表现好于基于近期数据的估计。")
    return " ".join(lines)


def _format_predicted_scores(entry: dict) -> str:
    """格式化预测比分：支持 1 或 2 个比分选项。"""
    ps = entry.get("predicted_scores")
    if ps:
        parts = [f"{s[0]}-{s[1]}" for s in ps[:2]]
        return " / ".join(parts)
    exp_h, exp_a = entry.get("exp_goals_home"), entry.get("exp_goals_away")
    if exp_h is not None and exp_a is not None:
        return f"{int(exp_h)} - {int(exp_a)}"
    return "-"


def _build_recap_html_from_log(entry: dict) -> str:
    """从 prediction_log 单条生成简要复盘 HTML（无需 DB 场次）。"""
    home = entry.get("home_team") or "主队"
    away = entry.get("away_team") or "客队"
    comp = entry.get("competition") or ""
    pred = entry.get("predicted_result") or "-"
    ah = entry.get("actual_home_score")
    aa = entry.get("actual_away_score")
    actual_result = entry.get("actual_result") or "-"
    score_str = f"{ah}-{aa}" if ah is not None and aa is not None else "—"
    correct = (pred == actual_result) if actual_result != "-" else None
    exp_h = entry.get("exp_goals_home")
    exp_a = entry.get("exp_goals_away")
    exp_str = f"{exp_h:.1f} - {exp_a:.1f}" if exp_h is not None and exp_a is not None else "—"
    conclusion = "预测正确" if correct is True else ("预测错误" if correct is False else "待定")
    analysis = _analyze_prediction_vs_actual(entry)
    body = f"""
<h1>{home} vs {away} 预测复盘</h1>
<p><strong>{comp}</strong></p>
<h2>预测 vs 实际</h2>
<ul>
<li>预测结果：{pred}</li>
<li>预测比分（取整）：{_format_predicted_scores(entry)}</li>
<li>期望进球：{exp_str}</li>
<li>实际比分：{score_str}</li>
<li>实际结果：{actual_result}</li>
<li>结论：{conclusion}</li>
</ul>
<h2>结果差异分析</h2>
<p>{analysis}</p>
<p><em>本页由预测记录生成，未使用数据库中的比赛详情。若需完整复盘（进球时刻、战术等），请先运行 <code>ingest-fd</code> 拉取该场数据后再点复盘。</em></p>
"""
    return body


@app.post("/reports/generate-by-fixture", response_model=ReportGenerateByFixtureResponse, tags=["报告"])
def generate_report_by_fixture(
    date_str: str = Query(..., description="比赛日 YYYY-MM-DD"),
    home_team: str = Query(..., description="主队名（与 prediction_log 一致）"),
    away_team: str = Query(..., description="客队名（与 prediction_log 一致）"),
) -> ReportGenerateByFixtureResponse:
    """按比赛日 + 主客队名查找 match_id 并生成复盘；若 DB 无该场则用 prediction_log 生成简要复盘。"""
    engine = get_engine()
    date_prefix = (date_str or "")[:10]
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, raw_meta FROM matches WHERE match_date = :d OR match_date LIKE :d_prefix"),
            {"d": date_str, "d_prefix": f"{date_prefix}%"},
        ).fetchall()
    candidates = []
    for r in rows:
        match_id, raw_meta = r[0], r[1]
        meta = {}
        if raw_meta:
            try:
                meta = json.loads(raw_meta)
            except Exception:
                pass
        h = (meta.get("home_team_name") or "").strip()
        a = (meta.get("away_team_name") or "").strip()
        if _match_team_names(home_team, h) and _match_team_names(away_team, a):
            candidates.append(match_id)
    if len(candidates) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"匹配到多场（match_id: {candidates}），请用 /reports/generate?match_id= 指定",
        )
    if len(candidates) == 1:
        match_id = candidates[0]
        payload = build_report_payload(engine, match_id)
        if not payload:
            raise HTTPException(status_code=404, detail="Match not found or no data")
        assembler = ReportAssembler()
        md = assembler.generate(payload)
        report_id = f"match_{match_id}"
        save_report_md(md, REPORTS_DIR / f"{report_id}.md")
        save_report_html(md, REPORTS_DIR / f"{report_id}.html")
        return ReportGenerateByFixtureResponse(
            match_id=match_id,
            report_id=report_id,
            url=f"/reports/{report_id}?format=html",
        )
    run_ingest_football_data_for_date(date_prefix)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, raw_meta FROM matches WHERE match_date = :d OR match_date LIKE :d_prefix"),
            {"d": date_str, "d_prefix": f"{date_prefix}%"},
        ).fetchall()
    candidates = []
    for r in rows:
        match_id, raw_meta = r[0], r[1]
        meta = {}
        if raw_meta:
            try:
                meta = json.loads(raw_meta)
            except Exception:
                pass
        h = (meta.get("home_team_name") or "").strip()
        a = (meta.get("away_team_name") or "").strip()
        if _match_team_names(home_team, h) and _match_team_names(away_team, a):
            candidates.append(match_id)
    if len(candidates) == 1:
        match_id = candidates[0]
        payload = build_report_payload(engine, match_id)
        if not payload:
            pass
        else:
            assembler = ReportAssembler()
            md = assembler.generate(payload)
            report_id = f"match_{match_id}"
            save_report_md(md, REPORTS_DIR / f"{report_id}.md")
            save_report_html(md, REPORTS_DIR / f"{report_id}.html")
            return ReportGenerateByFixtureResponse(
                match_id=match_id,
                report_id=report_id,
                url=f"/reports/{report_id}?format=html",
            )
    from engine.prediction_log import load_log
    init_db()
    preds = load_log(get_engine())
    entry = None
    for p in preds:
        if (p.get("date") or "")[:10] != date_prefix:
            continue
        if not _match_team_names(home_team, p.get("home_team") or ""):
            continue
        if not _match_team_names(away_team, p.get("away_team") or ""):
            continue
        if p.get("actual_home_score") is None and p.get("actual_away_score") is None:
            continue
        entry = p
        break
    if not entry:
        raise HTTPException(
            status_code=404,
            detail="未找到该场比赛或预测记录（请确认比赛已入库或预测日志中有实际比分）",
        )
    report_id = _recap_id_from_fixture(date_str, home_team, away_team)
    html_body = _build_recap_html_from_log(entry)
    full_html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<title>预测复盘 - {entry.get("home_team", "")} vs {entry.get("away_team", "")}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 720px; margin: 1rem auto; padding: 0 1rem; line-height: 1.5; }}
h1 {{ border-bottom: 1px solid #eee; }}
pre {{ background: #f5f5f5; padding: 1rem; overflow: auto; }}
</style>
</head>
<body>
{html_body}
</body>
</html>
"""
    recap_path = REPORTS_DIR / f"{report_id}.html"
    recap_path.write_text(full_html, encoding="utf-8")
    return ReportGenerateByFixtureResponse(
        match_id=0,
        report_id=report_id,
        url=f"/reports/{report_id}?format=html",
    )


@app.get("/reports/{report_id}", tags=["报告"])
def get_report(report_id: str, format: str = Query("md", pattern="^(md|html)$")) -> Any:
    """Return report file (md or html) by report_id (e.g. match_1)."""
    path = REPORTS_DIR / f"{report_id}.{format}"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Report not found")
    if format == "md":
        return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")
    return FileResponse(path, media_type="text/html")


# 仪表盘：今日预测 + 复盘入口
STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/dashboard", tags=["前端"])
def dashboard() -> FileResponse:
    """返回查看今日预测与复盘的 Web 界面。"""
    path = STATIC_DIR / "index.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return FileResponse(path, media_type="text/html")


# ---------- 竞彩今日列表仪表盘 + self_improvement_football 库 ----------
@app.get("/dashboard/jc", tags=["前端"])
def dashboard_jc() -> FileResponse:
    """竞彩今日比赛仪表盘：展示今日竞彩列表并支持保存到 self_improvement_football 库。"""
    path = STATIC_DIR / "dashboard-jc.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Dashboard JC not found")
    return FileResponse(path, media_type="text/html")


@app.get("/dashboard/jc-eval-matches", tags=["前端"])
def dashboard_jc_eval_matches() -> FileResponse:
    """竞彩预测统计：按「只统计推荐≥60%」或「单场推荐」筛选的命中比赛列表页。"""
    path = STATIC_DIR / "dashboard-jc-eval-matches.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Dashboard JC eval matches not found")
    return FileResponse(path, media_type="text/html")


# ---------- 投注花费与收益（bet_records）----------
@app.get("/api/bet-records", tags=["投注记录"])
def api_bet_records_list(
    date_from: str | None = Query(None, description="起始日期 YYYY-MM-DD"),
    date_to: str | None = Query(None, description="截止日期 YYYY-MM-DD"),
) -> dict[str, Any]:
    """列出投注记录。"""
    from db import repo
    repo.init_db()
    rows = repo.bet_record_list(date_from=date_from, date_to=date_to)
    return {"records": rows, "count": len(rows)}


@app.post("/api/bet-records", tags=["投注记录"])
def api_bet_records_add(
    record_date: str = Body(..., embed=True),
    amount_spent: float = Body(..., embed=True),
    amount_returned: float = Body(0.0, embed=True),
    note: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    """新增一条投注记录（下注日 + 花费；收益未开奖可填 0，次日或开奖后可编辑补填）。"""
    from db import repo
    repo.init_db()
    try:
        rid = repo.bet_record_add(
            record_date=(record_date or "").strip()[:10],
            amount_spent=float(amount_spent),
            amount_returned=float(amount_returned or 0),
            note=(note or "").strip() or None,
        )
        return {"id": rid, "message": "已添加"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/bet-records/{record_id:int}", tags=["投注记录"])
def api_bet_records_update(
    record_id: int,
    amount_returned: float | None = Body(None, embed=True),
    note: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    """更新一条投注记录的收益或备注（次日或开奖后补填收益时使用）。"""
    from db import repo
    repo.init_db()
    ok = repo.bet_record_update(
        record_id=record_id,
        amount_returned=amount_returned,
        note=note,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="记录不存在或未变更")
    return {"message": "已更新"}


@app.get("/api/bet-records/stats", tags=["投注记录"])
def api_bet_records_stats(
    date_from: str | None = Query(None, description="起始日期 YYYY-MM-DD"),
    date_to: str | None = Query(None, description="截止日期 YYYY-MM-DD"),
) -> dict[str, Any]:
    """统计总花费、总收益、盈亏。"""
    from db import repo
    repo.init_db()
    return repo.bet_record_stats(date_from=date_from, date_to=date_to)


@app.get("/api/jc-today", tags=["竞彩"])
def api_jc_today(date: str = Query(None, description="日期 YYYY-MM-DD，默认今天")) -> dict[str, Any]:
    """拉取指定日期的竞彩比赛列表（体彩接口），不写库。"""
    from datetime import date as date_type
    from etl.sporttery_crawl import crawl_sporttery_for_date
    day = (date or "").strip()[:10] or date_type.today().isoformat()
    matches = crawl_sporttery_for_date(day)
    return {"date": day, "matches": matches, "count": len(matches)}


@app.post("/api/jc-today/save", tags=["竞彩"])
def api_jc_today_save(date: str = Query(..., description="日期 YYYY-MM-DD")) -> dict[str, Any]:
    """将指定日期的竞彩比赛列表写入 self_improvement_football.jc_matches。先拉取体彩再写入。"""
    from etl.sporttery_crawl import crawl_sporttery_for_date
    from db.self_improvement import jc_matches_save
    day = (date or "").strip()[:10]
    if not day:
        raise HTTPException(status_code=400, detail="date required (YYYY-MM-DD)")
    matches = crawl_sporttery_for_date(day)
    n = jc_matches_save(day, matches, source="sporttery")
    return {"date": day, "saved": n, "count": len(matches), "db": "self_improvement_football"}


def _attach_jc_api_predictions(matches: list[dict]) -> None:
    """为 matches 中每条附加 api_pred（API Football 预测：result, scores）。"""
    from db.self_improvement import jc_api_predictions_load_for_match_ids
    ids = [m["match_id"] for m in matches if m.get("match_id") is not None]
    if not ids:
        return
    api_map = jc_api_predictions_load_for_match_ids(ids)
    for m in matches:
        mid = m.get("match_id")
        m["api_pred"] = api_map.get(mid) if mid is not None else None


@app.get("/api/jc-today/saved", tags=["竞彩"])
def api_jc_today_saved(date: str = Query(..., description="日期 YYYY-MM-DD")) -> dict[str, Any]:
    """从 self_improvement_football 读取该竞彩日已保存的比赛列表：优先 jingcai_match_analysis（按 jc_date），无则用 jc_matches。"""
    from db.self_improvement import jc_matches_load, jc_match_analysis_list_by_jc_date
    day = (date or "").strip()[:10]
    if not day:
        raise HTTPException(status_code=400, detail="date required (YYYY-MM-DD)")
    matches = jc_match_analysis_list_by_jc_date(day)
    if not matches:
        matches = jc_matches_load(day)
    _attach_jc_api_predictions(matches)
    return {"date": day, "matches": matches, "count": len(matches)}


def _jc_date_in_official_sync_window(day: str) -> bool:
    """仅对「今天及未来两周」与体彩列表对账；历史日期不做开售对账，避免把历史库内数据误判为 0 场。"""
    from datetime import date, timedelta

    try:
        d = date.fromisoformat(day[:10])
    except ValueError:
        return False
    today = date.today()
    return today <= d <= (today + timedelta(days=14))


def _match_id_in_official(mid: Any, official_ids: set[int]) -> bool:
    if mid is None:
        return False
    try:
        return int(mid) in official_ids
    except (TypeError, ValueError):
        return False


def _fetch_official_jc_match_ids(day: str, sync_official: bool) -> set[int] | None:
    """
    请求体彩 getMatchListV1 得到该竞彩日在售 matchId 集合。
    返回 None：未开启同步、不在时间窗、或请求失败（此时调用方应保留库内原列表）。
    返回 set（可为空）：请求成功，应用与库内取交集；空集表示官网该日 0 场开售。
    """
    if not sync_official or not _jc_date_in_official_sync_window(day):
        return None
    try:
        from etl.sporttery_crawl import sporttery_official_match_ids_for_jc_date

        ids = sporttery_official_match_ids_for_jc_date(day)
        if ids is not None:
            return ids
    except Exception:
        pass
    # 回退：官方对账接口偶发失败时，退化到同源列表抓取并按「仍可售」规则过滤，避免页面继续展示该日旧数据。
    try:
        from etl.sporttery_crawl import _sporttery_list_row_still_actionable, crawl_sporttery_for_date

        rows = crawl_sporttery_for_date(day, prefer_result_api=False)
        out: set[int] = set()
        for m in rows:
            if not _sporttery_list_row_still_actionable(m):
                continue
            mid = m.get("match_id")
            if mid is None:
                continue
            try:
                out.add(int(mid))
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return None


@app.get("/api/jc-today/ensure", tags=["竞彩"])
def api_jc_today_ensure(
    date: str = Query(..., description="竞彩日 YYYY-MM-DD，仅从 jingcai_match_analysis 读库返回，不自动拉取体彩"),
    sync_official: bool = Query(
        False,
        description="为 True 时与体彩 getMatchListV1 对账（仅最近约一周内的竞彩日）：官网该日列表为 0 则隐藏库内条目；有列表则只保留仍在开售的 matchId，避免展示过期日/串日残留",
    ),
) -> dict[str, Any]:
    """从 jingcai_match_analysis（及备用集合）读取该竞彩日数据。可选 sync_official 与官网当前开售列表对账，避免库内旧数据与官网不一致时仍展示。"""
    from db.self_improvement import get_self_improvement_db, jc_match_analysis_list_by_jc_date

    day = (date or "").strip()[:10]
    if not day:
        raise HTTPException(status_code=400, detail="date required (YYYY-MM-DD)")
    db = get_self_improvement_db()
    db_name = db.name if db is not None else "未配置"
    matches = jc_match_analysis_list_by_jc_date(day)
    sync_meta: dict[str, Any] = {"sync_official": sync_official, "sync_applied": False}

    official_ids = _fetch_official_jc_match_ids(day, sync_official)
    sync_meta["official_ids_ok"] = official_ids is not None
    if official_ids is not None:
        sync_meta["sync_applied"] = True
        sync_meta["official_count"] = len(official_ids)
        db_before = len(matches)
        sync_meta["db_before_filter"] = db_before
        if len(official_ids) == 0 and db_before > 0:
            sync_meta["suppressed_db_count"] = db_before
            matches = []
        elif len(official_ids) > 0:
            matches = [m for m in matches if _match_id_in_official(m.get("match_id"), official_ids)]
            sync_meta["db_after_filter"] = len(matches)

    _attach_jc_api_predictions(matches)
    if not matches:
        msg_parts = []
        if sync_meta.get("sync_applied") and sync_meta.get("suppressed_db_count"):
            msg_parts.append(
                f"体彩接口当前无竞彩日 {day} 的开售列表（0 场），已隐藏库内 {sync_meta['suppressed_db_count']} 条与官网不一致的旧数据。"
                " 若需查看库内原记录，可将接口参数 sync_official 设为 false。"
            )
        elif sync_meta.get("sync_applied") and (sync_meta.get("official_count") or 0) > 0:
            db_bf = int(sync_meta.get("db_before_filter") or 0)
            if db_bf > 0:
                msg_parts.append(
                    f"体彩该竞彩日当前开售 {sync_meta['official_count']} 场；库内原有 {db_bf} 条均不在官网当前开售列表中，已全部隐藏。可点击「拉取竞彩列表」同步。"
                )
            else:
                msg_parts.append(
                    f"体彩该竞彩日当前开售 {sync_meta['official_count']} 场，库内尚无比赛明细，请点击「拉取竞彩列表」。"
                )
        elif sync_meta.get("sync_applied") and (sync_meta.get("official_count") or 0) == 0 and not sync_meta.get("suppressed_db_count"):
            msg_parts.append("体彩该竞彩日当前无开售列表，库内亦无该日数据。可切换日期或稍后重试。")
        if not msg_parts:
            msg_parts.append(
                f"当前库「{db_name}」中该竞彩日无数据。请用「拉取竞彩列表」按钮或命令行 jc-to-self-improvement --date 按日拉取，或 jc-batch-year --year 按年拉取；若数据在其它库，请设置 .env 的 MONGO_SELF_IMPROVEMENT_DB_NAME 后重启 API。"
            )
        out: dict[str, Any] = {
            "date": day,
            "matches": [],
            "count": 0,
            "db": db_name,
            "message": "".join(msg_parts),
            "sync": sync_meta,
        }
        return out
    out_ok: dict[str, Any] = {"date": day, "matches": matches, "count": len(matches), "sync": sync_meta}
    if sync_meta.get("sync_applied") and sync_meta.get("db_before_filter", 0) > len(matches) and sync_meta.get("official_count", 0) > 0:
        out_ok["message"] = (
            f"已与体彩开售列表对账：官网该日 {sync_meta['official_count']} 场，"
            f"库内匹配 {len(matches)} 场（已过滤不在当前开售列表中的记录）。"
        )
    return out_ok


@app.post("/api/jc-today/fetch", tags=["竞彩"])
def api_jc_today_fetch(date: str = Query(..., description="竞彩日 YYYY-MM-DD：与体彩官网开售页「业务日期」一致，非按当日开球筛选")) -> dict[str, Any]:
    """从体彩拉取指定**竞彩开售日**的官网列表（getMatchListV1 等，matchDate=所选日期，按 businessDate 过滤），逐场拉详情写入 jingcai_match_analysis。与命令行 jc-to-self-improvement --date 一致；开球日多为次日，勿与「比赛日」混淆。"""
    from main import run_jc_to_self_improvement_for_dates

    day = (date or "").strip()[:10]
    if not day or len(day) != 10:
        raise HTTPException(status_code=400, detail="date 需为 YYYY-MM-DD")
    try:
        from datetime import date as _date

        is_today_or_future = day >= _date.today().isoformat()
        per_date: list[dict] = []
        saved = run_jc_to_self_improvement_for_dates(
            [day], dry_run=False, sleep_sec=0.5, per_date_results=per_date, only_actionable=is_today_or_future
        )
        meta = per_date[0] if per_date else {}
        list_count = int(meta.get("list_count") or 0)
        db_cleared = int(meta.get("db_cleared") or 0)
        pruned_stale = int(meta.get("pruned_stale") or 0)
        if list_count == 0:
            msg = f"竞彩日 {day}：官网开售列表为 0 场。"
            if db_cleared:
                msg += f" 已清空库内该日残留 {db_cleared} 条（此前可能为赛果/计算器回退误写入）。"
            else:
                msg += " 库内该日已无对阵记录或无需清理。"
        else:
            msg = f"竞彩日 {day}：列表解析 {list_count} 场，写入 {saved} 场。"
            if pruned_stale:
                msg += f" 已删除不在本次官网列表中的库内残留 {pruned_stale} 条。"
            if saved < list_count:
                msg += f"（有 {list_count - saved} 场未写入详情，可能接口失败或无效）"
        msg += " 请刷新列表查看。"
        return {
            "date": day,
            "saved": saved,
            "list_count": list_count,
            "db_cleared": db_cleared,
            "pruned_stale": pruned_stale,
            "message": msg,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拉取失败: {e}") from e


@app.get("/api/jc-today/recommend", tags=["竞彩"])
def api_jc_today_recommend(
    date: str = Query(None, description="竞彩日 YYYY-MM-DD，默认今天"),
    min_prob: float | None = Query(None, ge=0.0, le=1.0, description="最低推荐概率阈值，默认从配置 JC_REC_DEFAULT_MIN_PROB 读取（0.60 时展示命中率约70%）"),
    min_spread: float = Query(0.06, ge=0.0, le=0.5, description="推荐项与第二高概率的最小差距，避免均分场次"),
    top: int | None = Query(6, ge=1, le=20, description="最多返回几场推荐，默认 6"),
    require_high_confidence: bool = Query(True, description="仅保留模型 confidence=high 的预测"),
    big5_only: bool = Query(False, description="仅五大联赛"),
    max_per_league: int | None = Query(2, ge=0, le=10, description="取 top 时每联赛最多几场，实现分散；0 表示不限制"),
    sync_official: bool = Query(
        False,
        description="为 True 时与体彩开售列表对账（与 /api/jc-today/ensure 一致），仅统计/推荐仍在官网该日开售的场次",
    ),
) -> dict[str, Any]:
    """根据今日竞彩与 jc_predictions 返回下注单推荐（优化逻辑：概率差距、模型置信度、联赛分散）。"""
    import config as _config
    from datetime import date as date_type
    from db.self_improvement import (
        _filter_jc_matches_by_official_ids,
        jc_match_analysis_list_by_jc_date,
        jc_recommend_bet_slip,
    )

    if min_prob is None:
        min_prob = getattr(_config, "JC_REC_DEFAULT_MIN_PROB", 0.60)
    day = (date or "").strip()[:10] or date_type.today().isoformat()
    raw_matches = jc_match_analysis_list_by_jc_date(day)
    official_ids = _fetch_official_jc_match_ids(day, sync_official)
    matches = _filter_jc_matches_by_official_ids(raw_matches, official_ids)
    max_league = max_per_league if max_per_league is not None and max_per_league > 0 else None
    recommended = jc_recommend_bet_slip(
        day,
        min_prob=min_prob,
        min_spread=min_spread,
        top=top,
        require_high_confidence=require_high_confidence,
        big5_only=big5_only,
        max_per_league=max_league,
        official_intersect_ids=official_ids,
    )
    if not matches:
        empty_msg = "该日无竞彩场次数据，请先拉取竞彩列表或选择有数据的日期。"
        if official_ids is not None and len(official_ids) == 0 and len(raw_matches) > 0:
            empty_msg = "体彩该竞彩日当前无开售列表，已按官网对账忽略库内旧场次（与比赛列表一致）。"
        return {
            "date": day,
            "recommended": [],
            "count": 0,
            "total_matches": 0,
            "message": empty_msg,
        }
    if not recommended:
        from db.self_improvement import jc_recommend_diagnostic

        diag = jc_recommend_diagnostic(
            day,
            min_prob=min_prob,
            min_spread=min_spread,
            require_high_confidence=require_high_confidence,
            official_intersect_ids=official_ids,
        )
        hint = "（需概率≥{}%、与第二选项差≥{}%、且模型置信度为高）".format(
            int(min_prob * 100), int(min_spread * 100)
        ) if require_high_confidence else ""
        return {
            "date": day,
            "recommended": [],
            "count": 0,
            "total_matches": len(matches),
            "diagnostic": diag,
            "message": f"该日共 {len(matches)} 场，暂无满足条件的推荐{hint}；可调低 min_prob/min_spread 或关闭 require_high_confidence。",
        }
    high = sum(1 for r in recommended if r.get("tier") == "high")
    return {
        "date": day,
        "recommended": recommended,
        "count": len(recommended),
        "total_matches": len(matches),
        "high_tier_count": high,
        "message": f"今日推荐 {len(recommended)} 场（首选 {high} 场 / 可考虑 {len(recommended) - high} 场，共 {len(matches)} 场）。",
    }


@app.get("/api/jc-today/saved/dates", tags=["竞彩"])
def api_jc_today_saved_dates() -> dict[str, Any]:
    """列出已保存的竞彩日及场次数：优先 jingcai_match_analysis，并合并 jc_matches（若存在）。"""
    from db.self_improvement import jc_matches_list_dates, jc_match_analysis_list_dates
    by_list = {d["date"]: d["count"] for d in jc_match_analysis_list_dates()}
    for d in jc_matches_list_dates():
        if d["date"] not in by_list or by_list[d["date"]] < d["count"]:
            by_list[d["date"]] = d["count"]
    dates = [{"date": d, "count": c} for d, c in sorted(by_list.items(), reverse=True)]
    return {"dates": dates, "total_days": len(dates)}


@app.get("/api/jc-today/lineup-comparison", tags=["竞彩"])
def api_jc_today_lineup_comparison(
    date_from: str | None = Query(None, description="开始日期 YYYY-MM-DD"),
    date_to: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    limit: int = Query(200, ge=1, le=2000, description="最多返回场次"),
) -> dict[str, Any]:
    """复盘：已完赛场次的预计 vs 实际阵容对比（关键球员缺阵、首发人数）。"""
    from db.self_improvement import lineup_comparison_report
    try:
        items = lineup_comparison_report(date_from=date_from, date_to=date_to, limit=limit)
        return {"items": items, "count": len(items)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/jc-today/lineup-stats", tags=["竞彩"])
def api_jc_today_lineup_stats(
    date_from: str | None = Query(None, description="开始日期 YYYY-MM-DD"),
    date_to: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
) -> dict[str, Any]:
    """统计：指定日期范围内已完赛且有阵容数据的场次的场均首发人数。"""
    from db.self_improvement import lineup_stats_aggregate
    try:
        return lineup_stats_aggregate(date_from=date_from, date_to=date_to)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/jc-match-analysis/fixture-id-stats", tags=["竞彩"])
def api_jc_match_analysis_fixture_id_stats(
    years: str | None = Query(None, description="年份，逗号分隔如 2025,2026；不传则统计全部"),
) -> dict[str, Any]:
    """统计 jingcai_match_analysis 按年份的已匹配/未匹配 api_football_fixture_id 数量。"""
    from db.self_improvement import jc_match_analysis_fixture_id_stats as get_stats
    year_list: list[int] | None = None
    if years:
        try:
            year_list = [int(y.strip()) for y in years.split(",") if y.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="years 需为逗号分隔的年份，如 2025,2026")
    return get_stats(years=year_list)


@app.get("/api/jc-match-analysis/unmatched", tags=["竞彩"])
def api_jc_match_analysis_unmatched(
    years: str | None = Query(None, description="年份，逗号分隔如 2025,2026；不传则全部"),
    limit: int = Query(500, ge=1, le=5000, description="最多返回条数"),
) -> dict[str, Any]:
    """列出未匹配 api_football_fixture_id 的场次，用于排查或导出。"""
    from db.self_improvement import jc_match_analysis_unmatched_list
    year_list: list[int] | None = None
    if years:
        try:
            year_list = [int(y.strip()) for y in years.split(",") if y.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="years 需为逗号分隔的年份，如 2025,2026")
    items = jc_match_analysis_unmatched_list(years=year_list, limit=limit)
    return {"unmatched": items, "count": len(items)}


@app.get("/api/jc-match-detail", tags=["竞彩"])
def api_jc_match_detail(
    sporttery_match_id: int = Query(..., description="体彩场次 ID（mid），如 2037891"),
) -> dict[str, Any]:
    """
    拉取单场对阵详情：头部信息、伤停、射手、相同主客场比赛结果、历史交锋（全部/同赛制）、特征分析。
    直接请求体彩 webapi（getMatchHeadV1、getInjurySuspensionV1、getMatchPlayerV1 等），无需 Playwright。
    仅返回数据，不落库；需落库请调 POST /api/jc-match-detail/save。
    """
    from etl.sporttery_crawl import fetch_sporttery_match_detail_via_api
    return fetch_sporttery_match_detail_via_api(sporttery_match_id)


@app.post("/api/jc-match-detail/save", tags=["竞彩"])
def api_jc_match_detail_save(
    sporttery_match_id: int = Query(..., description="体彩场次 ID（mid）"),
    date: str | None = Query(None, description="竞彩日 YYYY-MM-DD，传入则写入 jc_date 便于按竞彩日筛选"),
) -> dict[str, Any]:
    """拉取单场对阵详情并写入 MongoDB self_improvement_football.jingcai_match_analysis。"""
    from etl.sporttery_crawl import fetch_sporttery_match_detail_via_api
    from db.self_improvement import jc_match_analysis_save
    detail = fetch_sporttery_match_detail_via_api(sporttery_match_id)
    jc_day = (date or "").strip()[:10]
    if jc_day and len(jc_day) >= 10:
        detail["jc_date"] = jc_day
    saved = jc_match_analysis_save(sporttery_match_id, detail)
    return {"sporttery_match_id": sporttery_match_id, "saved": saved, "detail": detail}


@app.get("/api/jc-match-detail/saved", tags=["竞彩"])
def api_jc_match_detail_saved(
    sporttery_match_id: int = Query(..., description="体彩场次 ID（mid）"),
) -> dict[str, Any]:
    """从库中读取已保存的对阵详情（self_improvement_football.jingcai_match_analysis）。不存在返回 404。附带 api_prediction（API Football 预测）若有。"""
    from db.self_improvement import jc_match_analysis_load, jc_api_predictions_load_by_sporttery_match_id
    doc = jc_match_analysis_load(sporttery_match_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"未找到 mid={sporttery_match_id} 的已保存详情")
    api_pred = jc_api_predictions_load_by_sporttery_match_id(sporttery_match_id)
    if api_pred is not None:
        from etl.api_football_predictions import (
            parse_predictions_derived_score,
            parse_predictions_expected_goals,
            parse_predictions_goals_spread,
            parse_predictions_to_probs,
            parse_predictions_to_outcome,
        )
        raw = api_pred.get("raw")
        scores = api_pred.get("scores")
        if not scores and isinstance(raw, dict):
            _, scores_from_raw = parse_predictions_to_outcome(raw)
            if scores_from_raw:
                scores = list(scores_from_raw)
        derived = api_pred.get("derived_scores")
        if (not derived or not isinstance(derived, list)) and isinstance(raw, dict):
            d = parse_predictions_derived_score(raw)
            if d:
                derived = list(d)
        goals_exp = api_pred.get("goals_expected_home"), api_pred.get("goals_expected_away")
        if (goals_exp[0] is None and goals_exp[1] is None) and isinstance(raw, dict):
            exp = parse_predictions_expected_goals(raw)
            if exp:
                goals_exp = exp
        goals_spread = api_pred.get("goals_spread")
        if not goals_spread and isinstance(raw, dict):
            goals_spread = parse_predictions_goals_spread(raw)
        probs = None
        if api_pred.get("win_pct") is not None and api_pred.get("draw_pct") is not None and api_pred.get("loss_pct") is not None:
            probs = (api_pred["win_pct"], api_pred["draw_pct"], api_pred["loss_pct"])
        elif isinstance(raw, dict):
            probs = parse_predictions_to_probs(raw)
        doc["api_prediction"] = {
            "result": api_pred.get("result"),
            "scores": scores,
            "derived_scores": derived,
            "goals_expected_home": goals_exp[0],
            "goals_expected_away": goals_exp[1],
            "goals_spread": goals_spread,
            "raw": raw,
        }
        if probs is not None:
            doc["api_prediction"]["win_pct"] = probs[0]
            doc["api_prediction"]["draw_pct"] = probs[1]
            doc["api_prediction"]["loss_pct"] = probs[2]
    return doc


@app.post("/api/jc-match-detail/save-by-date", tags=["竞彩"])
def api_jc_match_detail_save_by_date(
    date: str = Query(..., description="竞彩日 YYYY-MM-DD：与列表所选日期一致，从库中取该竞彩日已保存场次"),
    only_missing_scores: bool = Query(
        True,
        description="为 True 时跳过已有全场+半场比分的场次；仅有全场无半场仍会拉取以补半场。为 False 时对该日每场都请求详情（慎用频率）",
    ),
    respect_kickoff: bool = Query(
        True,
        description="为 True 时结合场次 match_date+match_time 判断开球时间，未到开球（本地时间+15 分钟容差）则跳过，避免对未开赛场次无效请求",
    ),
    backfill_fixture_id: bool = Query(
        False,
        description="为 True 时在成功写入详情后额外请求 API-Football 回填 api_football_fixture_id（默认 False，仅拉体彩赛果不耗 API 配额）",
    ),
) -> dict[str, Any]:
    """
    按竞彩日更新赛果/半场：优先用 getUniformMatchResultV1（赛果开奖页同源，含 sectionsNo1 半场与 sectionsNo999 全场）
    按 matchId 批量写入 jingcai_match_analysis；仍缺数据的场次再逐场请求 getMatchHeadV1 等详情接口。
    与「拉取竞彩列表」不同：不拉开售列表，只更新已存场次比分。
    """
    from db.self_improvement import jc_match_analysis_sync_results_for_jc_date

    day = (date or "").strip()[:10]
    if not day:
        raise HTTPException(status_code=400, detail="date required (YYYY-MM-DD)")
    return jc_match_analysis_sync_results_for_jc_date(
        day,
        only_missing_scores=only_missing_scores,
        respect_kickoff=respect_kickoff,
        backfill_fixture_id=backfill_fixture_id,
    )


@app.post("/api/jc-api-predictions/sync", tags=["竞彩"])
def api_jc_api_predictions_sync(
    date: str = Query(..., description="竞彩日 YYYY-MM-DD，仅同步该日比赛列表的 API Football 预测"),
    rate_limit: float = Query(0.2, description="请求间隔秒数"),
    overwrite: bool = Query(False, description="是否覆盖已有预测"),
    backfill_past: bool = Query(True, description="是否同时回填前天/昨天比赛的单场统计、阵容、事件、球员统计、射手榜（只补缺不覆盖）"),
) -> dict[str, Any]:
    """根据指定竞彩日的比赛列表，拉取 API Football 预测并写入 jc_api_predictions。
    会先对该竞彩日补全缺失的 api_football_fixture_id（只补缺、不覆盖），再拉取预测；
    若 backfill_past=True，会顺带回填「前天、昨天」比赛日的单场统计、阵容、事件、球员单场统计、射手榜。"""
    from db.self_improvement import (
        get_self_improvement_db,
        jc_api_predictions_sync_from_analysis,
        jc_match_analysis_backfill_fixture_id_for_jc_date,
        jc_backfill_past_match_data,
    )

    if get_self_improvement_db() is None:
        raise HTTPException(status_code=503, detail="未配置 MONGO_URI，无法访问 self_improvement_football")
    day = (date or "").strip()[:10]
    if not day:
        raise HTTPException(status_code=400, detail="date required (YYYY-MM-DD)")
    # 1) 先对该竞彩日「下面所有比赛」补全 fixture_id（已有则不再请求）；拉取当天+次日赛程以覆盖开球日为次日
    backfill_result = jc_match_analysis_backfill_fixture_id_for_jc_date(
        jc_date=day,
        rate_limit_sec=min(rate_limit, 1.0),
        overwrite_existing=False,
    )
    # 2) 拉取 API 预测
    result = jc_api_predictions_sync_from_analysis(
        rate_limit_sec=rate_limit,
        date=day,
        overwrite_existing=overwrite,
        workers=1,
    )
    message = result.get("message")
    if not message:
        message = f"已更新 {result.get('updated', 0)} 条，跳过已有 {result.get('skipped', 0)} 条，无 fixture_id {result.get('no_fixture_id', 0)}，API 无数据 {result.get('api_no_data', 0)}"
    if backfill_result.get("updated", 0) > 0:
        message = f"先补全了 {backfill_result['updated']} 条 fixture_id；" + message

    # 3) 可选：回填前天/昨天比赛的单场统计、阵容、事件、球员统计、射手榜（只补缺不覆盖）
    past_backfill: dict[str, Any] = {}
    if backfill_past:
        try:
            past_backfill = jc_backfill_past_match_data(
                jc_date=day,
                rate_limit_sec=min(rate_limit, 1.0),
                limit_per_category=50,
            )
            if past_backfill.get("message"):
                message = message + "；" + past_backfill["message"]
        except Exception as e:
            past_backfill = {"message": str(e), "error": True}
            message = message + "；回填近期数据失败: " + str(e)

    return {
        "date": day,
        "message": message,
        "backfill_fixture_id": backfill_result,
        "backfill_past": past_backfill,
        **result,
    }


@app.post("/api/jc-daily-must-do", tags=["竞彩"])
def api_jc_daily_must_do(
    date: str = Query(..., description="竞彩日 YYYY-MM-DD"),
    skip_fetch_list: bool = Query(False, description="跳过第 1 步拉取竞彩列表（当日已拉过可勾选）"),
    skip_api_predict: bool = Query(False, description="跳过第 2 步拉取 API 预测与近期回填"),
) -> dict[str, Any]:
    """每日必做：仅 (1) 拉取竞彩列表+详情+fixture_id (2) 拉取 API 预测+近期回填（前天昨天+前一天当天）。运行模型预测请单独点「运行预测」按钮。"""
    day = (date or "").strip()[:10]
    if not day:
        raise HTTPException(status_code=400, detail="date required (YYYY-MM-DD)")
    from main import run_jc_to_self_improvement_for_dates
    from db.self_improvement import (
        get_self_improvement_db,
        jc_api_predictions_sync_from_analysis,
        jc_match_analysis_backfill_fixture_id_for_jc_date,
        jc_backfill_past_match_data,
    )

    if get_self_improvement_db() is None:
        raise HTTPException(status_code=503, detail="未配置 MONGO_URI，无法访问 self_improvement_football")

    steps: list[dict[str, Any]] = []
    all_ok = True

    # (1) 拉取竞彩列表 + 详情 + fixture_id 回填
    if not skip_fetch_list:
        try:
            per_date: list[dict] = []
            saved = run_jc_to_self_improvement_for_dates(
                [day], dry_run=False, sleep_sec=0.5, per_date_results=per_date
            )
            list_count = per_date[0]["list_count"] if per_date else 0
            steps.append({"step": "fetch_list", "name": "拉取竞彩列表", "saved": saved, "list_count": list_count})
        except Exception as e:
            steps.append({"step": "fetch_list", "name": "拉取竞彩列表", "error": str(e)})
            all_ok = False
    else:
        steps.append({"step": "fetch_list", "name": "拉取竞彩列表", "skipped": True})

    # (2) 拉取 API 预测 + 近期回填（前天/昨天 + 前一天/当天：单场统计、阵容、事件、球员统计、射手榜）
    if not skip_api_predict and all_ok:
        try:
            backfill_result = jc_match_analysis_backfill_fixture_id_for_jc_date(
                jc_date=day, rate_limit_sec=0.2, overwrite_existing=False
            )
            result = jc_api_predictions_sync_from_analysis(
                rate_limit_sec=0.2, date=day, overwrite_existing=False, workers=1
            )
            past_backfill = jc_backfill_past_match_data(
                jc_date=day, rate_limit_sec=0.2, limit_per_category=50, include_today=False
            )
            backfill_today = jc_backfill_past_match_data(
                jc_date=day, rate_limit_sec=0.2, limit_per_category=50, include_today=True
            )
            steps.append({
                "step": "api_predict",
                "name": "拉取 API 预测与近期回填",
                "backfill_fixture_id": backfill_result,
                "predictions": result,
                "backfill_past": past_backfill,
                "backfill_today": backfill_today,
            })
        except Exception as e:
            steps.append({"step": "api_predict", "name": "拉取 API 预测与近期回填", "error": str(e)})
            all_ok = False
    else:
        steps.append({"step": "api_predict", "name": "拉取 API 预测与近期回填", "skipped": True})

    message = "每日必做完成（运行模型预测请点「运行预测」）" if all_ok else "每日必做部分步骤失败，请查看 steps 明细"
    return {"date": day, "ok": all_ok, "message": message, "steps": steps}


@app.post("/api/jc-predict/run", tags=["竞彩"])
def api_jc_predict_run(
    date: str = Query(..., description="比赛日 YYYY-MM-DD"),
    last_n: int = Query(10, description="参考最近 N 场"),
) -> dict[str, Any]:
    """从 self_improvement_football.jingcai_match_analysis 读该日赛程，用模型预测并写入 jc_predictions（不限于五大联赛）。含已完赛场次，便于表格展示预测与复盘。"""
    from main import run_predict_jc_from_self_improvement
    day = (date or "").strip()[:10]
    if not day:
        raise HTTPException(status_code=400, detail="date required (YYYY-MM-DD)")
    result = run_predict_jc_from_self_improvement(day, last_n=last_n, include_finished=True)
    added, skipped, total = result[0], result[1], result[2]
    stats = result[3] if len(result) > 3 else {}
    out: dict[str, Any] = {"date": day, "added": added, "skipped": skipped, "total": total}
    if stats:
        out["resolution_rate"] = stats.get("resolution_rate")
        out["unresolved_sample"] = stats.get("unresolved_sample", [])
    return out


@app.get("/api/jc-predict/saved", tags=["竞彩"])
def api_jc_predict_saved(
    date: str = Query(..., description="比赛日 YYYY-MM-DD"),
) -> dict[str, Any]:
    """从 self_improvement_football.jc_predictions 读取指定日期的模型预测列表。"""
    from db.self_improvement import jc_predictions_load
    day = (date or "").strip()[:10]
    if not day:
        raise HTTPException(status_code=400, detail="date required (YYYY-MM-DD)")
    predictions = jc_predictions_load(day)
    return {"date": day, "predictions": predictions, "count": len(predictions)}


def _jc_predictions_eval_response(year: int | None = None, all_time: bool = False) -> JSONResponse:
    """共用：全量(all_time=True)或按年(year)生成 jc_predictions 命中率报告。"""
    from db.self_improvement import get_self_improvement_db, jc_predictions_eval_report

    if get_self_improvement_db() is None:
        raise HTTPException(status_code=503, detail="未配置 MONGO_URI，无法访问 self_improvement_football")
    data = jc_predictions_eval_report(year=year, all_time=all_time)
    return JSONResponse(
        content=data,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
    )


@app.get("/api/jc-predictions-eval/years", tags=["竞彩"])
def api_jc_predictions_eval_years() -> dict[str, Any]:
    """返回 jc_predictions 中有预测数据的年份列表，供前端按数据库实际年份展示统计卡片。"""
    from db.self_improvement import jc_predictions_eval_available_years
    return {"years": jc_predictions_eval_available_years()}


@app.get("/api/jc-predictions-eval", tags=["竞彩"])
def api_jc_predictions_eval_get(
    year: int | None = Query(2026, description="统计年份，与 all 二选一"),
    all: int = Query(0, description="1=全量 jc_predictions 统计，0=按 year"),
) -> JSONResponse:
    """jc_predictions 模型预测统计：all=1 为全量，否则按 year 年份。"""
    if all:
        return _jc_predictions_eval_response(all_time=True)
    return _jc_predictions_eval_response(year=year or 2026)


@app.post("/api/jc-predictions-eval", tags=["竞彩"])
def api_jc_predictions_eval_post(
    year: int | None = Body(2026, embed=True),
    all: bool = Body(False, embed=True),
) -> JSONResponse:
    """POST：all=true 返回全量统计，否则按 year 年份统计。"""
    if all:
        return _jc_predictions_eval_response(all_time=True)
    return _jc_predictions_eval_response(year=year or 2026)


@app.get("/api/jc-predictions-eval/matches", tags=["竞彩"])
def api_jc_predictions_eval_matches(
    year: int | None = Query(None, description="统计年份，与 all 二选一"),
    all: int = Query(0, description="1=全量，0=按 year"),
    filter: str = Query(..., description="tier55=≥55%, tier60=≥60%, tier65=≥65%(更稳), single_rec=单场推荐命中"),
) -> dict[str, Any]:
    """返回指定年份（或全量）下「只统计推荐≥55%/≥60%/≥65%」或「单场推荐」的命中比赛列表。"""
    from db.self_improvement import get_self_improvement_db, jc_predictions_eval_report

    if get_self_improvement_db() is None:
        raise HTTPException(status_code=503, detail="未配置 MONGO_URI，无法访问 self_improvement_football")
    if filter not in ("tier55", "tier60", "tier65", "single_rec"):
        raise HTTPException(status_code=400, detail="filter 必须为 tier55、tier60、tier65 或 single_rec")
    all_time = all == 1
    year_param = None if all_time else (year or 2026)
    data = jc_predictions_eval_report(year=year_param, all_time=all_time, include_hit_matches=True)
    range_label = data.get("range_label") or ("全量" if all_time else f"{year_param}年")
    key = "hit_matches_tier55" if filter == "tier55" else ("hit_matches_tier60" if filter == "tier60" else ("hit_matches_tier65" if filter == "tier65" else "hit_matches_single_rec"))
    matches = data.get(key) or []
    return {
        "year": None if all_time else (year_param or year),
        "filter": filter,
        "range_label": range_label,
        "count": len(matches),
        "matches": matches,
    }


def _run_jc_2026_fetch() -> None:
    """后台线程：按 self_improvement_football 逻辑拉取 2026 年至今竞彩数据（jc_matches + jingcai_match_analysis），同时更新进度与日志。"""
    from main import run_jc_to_self_improvement_for_dates

    year = 2026
    d0 = date(year, 1, 1)
    today = date.today()
    d1 = min(date(year, 12, 31), today)  # 2026 未过完，只拉到今天
    dates = []
    d = d0
    while d <= d1:
        dates.append(d.isoformat())
        d += timedelta(days=1)

    total_written_acc = [0]  # 用 list 以便 closure 内修改

    def progress_callback(i: int, total: int, date_str: str, matches_count: int, saved_analysis: int, hint: str) -> None:
        total_written_acc[0] += saved_analysis
        line = f"当前进行到 {date_str}（第 {i}/{total} 天），本日体彩场次 {matches_count} 场，写入 jingcai_match_analysis {saved_analysis} 场{hint}"
        with _jc_2026_lock:
            _jc_2026_state["current_date"] = date_str
            _jc_2026_state["index"] = i
            _jc_2026_state["total"] = total
            _jc_2026_state["total_written"] = total_written_acc[0]
            _jc_2026_state["log_lines"] = (_jc_2026_state.get("log_lines") or [])[-99:] + [line]
        try:
            with open(JC_2026_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    try:
        with _jc_2026_lock:
            _jc_2026_state["running"] = True
            _jc_2026_state["error"] = None
            _jc_2026_state["total"] = len(dates)
            _jc_2026_state["log_lines"] = [f"开始拉取 {year} 年竞彩数据到 self_improvement_football.jingcai_match_analysis，共 {len(dates)} 天（{dates[0]} ~ {dates[-1]}，到今日止）。"]
        try:
            JC_2026_LOG.write_text(_jc_2026_state["log_lines"][-1] + "\n", encoding="utf-8")
        except Exception:
            pass

        total = run_jc_to_self_improvement_for_dates(dates, dry_run=False, progress_callback=progress_callback)
        with _jc_2026_lock:
            _jc_2026_state["running"] = False
            _jc_2026_state["log_lines"] = (_jc_2026_state.get("log_lines") or []) + [f"完成: 共 {total} 条写入 jingcai_match_analysis。"]
        try:
            with open(JC_2026_LOG, "a", encoding="utf-8") as f:
                f.write(_jc_2026_state["log_lines"][-1] + "\n")
        except Exception:
            pass
    except Exception as e:
        with _jc_2026_lock:
            _jc_2026_state["running"] = False
            _jc_2026_state["error"] = str(e)
            _jc_2026_state["log_lines"] = (_jc_2026_state.get("log_lines") or []) + [f"失败: {e}"]


@app.post("/api/jc-fetch-2026/start", tags=["竞彩"])
def api_jc_fetch_2026_start() -> dict[str, Any]:
    """在后台启动 2026 年竞彩数据拉取任务（写入 self_improvement_football.jingcai_match_analysis）。若已在运行则返回当前进度。"""
    with _jc_2026_lock:
        if _jc_2026_state.get("running"):
            return {
                "started": False,
                "message": "任务已在运行中",
                "current_date": _jc_2026_state.get("current_date"),
                "index": _jc_2026_state.get("index"),
                "total": _jc_2026_state.get("total"),
                "total_written": _jc_2026_state.get("total_written"),
            }
        _jc_2026_state["running"] = True
        _jc_2026_state["current_date"] = None
        _jc_2026_state["index"] = 0
        _jc_2026_state["total"] = 0  # 由后台线程开始时设为实际天数
        _jc_2026_state["total_written"] = 0
        _jc_2026_state["error"] = None
        _jc_2026_state["log_lines"] = []

    t = threading.Thread(target=_run_jc_2026_fetch, daemon=True)
    t.start()
    return {
        "started": True,
        "message": "已启动 2026 年竞彩数据后台拉取，请通过 GET /api/jc-fetch-2026/status 查看进度与日志",
        "log_path": str(JC_2026_LOG),
    }


@app.get("/api/jc-fetch-2026/status", tags=["竞彩"])
def api_jc_fetch_2026_status() -> dict[str, Any]:
    """查询 2026 年竞彩数据拉取任务进度与最近日志。"""
    with _jc_2026_lock:
        return {
            "running": _jc_2026_state.get("running", False),
            "current_date": _jc_2026_state.get("current_date"),
            "index": _jc_2026_state.get("index"),
            "total": _jc_2026_state.get("total"),
            "total_written": _jc_2026_state.get("total_written"),
            "error": _jc_2026_state.get("error"),
            "log_lines": _jc_2026_state.get("log_lines") or [],
            "log_path": str(JC_2026_LOG),
        }


def create_app() -> FastAPI:
    return app
