"""
API-FOOTBALL 预测拉取：按 fixture 拉取 API 的赛前预测，可与本模型概率融合。
"""
from __future__ import annotations

import re
import time
from typing import Any

import config
from etl.injury_api_football import _request

RATE_LIMIT_SEC = getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)


def get_fixture_id_for_match(date_str: str, home_name: str, away_name: str) -> int | None:
    """
    按日期与主客队名解析 fixture_id（用于后续拉赔率/预测/统计）。
    返回 None 表示未找到。
    """
    from etl.api_football_odds import _fixtures_with_id
    fixtures = _fixtures_with_id((date_str or "")[:10])
    if not fixtures:
        return None
    home_n = (home_name or "").strip().lower().replace(" ", "")
    away_n = (away_name or "").strip().lower().replace(" ", "")
    for f in fixtures:
        h = (f.get("home_name") or "").strip().lower().replace(" ", "")
        a = (f.get("away_name") or "").strip().lower().replace(" ", "")
        if (home_n in h or h in home_n) and (away_n in a or a in away_n):
            return f.get("fixture_id")
    return None


def _normalize_team_for_match(s: str) -> str:
    """归一化队名用于匹配：去空格、小写、德文/西葡变音转写、去掉首尾前缀/后缀（VfL、CD/CF/CP/SD、FC、尾数字等），
    连字符/撇号、United→Utd、门兴 M'gladbach↔Monchengladbach 等，便于与 API 名一致。"""
    t = (s or "").strip().lower()
    t = t.replace("-", "").replace("'", "").replace(" ", "")
    # 西葡法等变音符号 -> 基础字母（Paços→Pacos、Zaragoza 等）
    for old, new in (
        ("ö", "o"), ("ü", "u"), ("ä", "a"), ("ß", "ss"),
        ("á", "a"), ("à", "a"), ("ã", "a"), ("â", "a"), ("å", "a"),
        ("é", "e"), ("è", "e"), ("ê", "e"),
        ("í", "i"), ("ì", "i"), ("î", "i"),
        ("ó", "o"), ("ò", "o"), ("ô", "o"), ("õ", "o"),
        ("ú", "u"), ("ù", "u"), ("û", "u"),
        ("ç", "c"), ("ñ", "n"), ("ø", "o"),
    ):
        t = t.replace(old, new)
    # 去掉首部 "1." / "2." / "1899" 等（如 1. FC Köln、1899 Hoffenheim）
    t = re.sub(r"^\d+\.?", "", t)
    if t.startswith("1899") and len(t) > 4:
        t = t[4:]
    # 去掉常见前缀 FC / VfL / CD / CF / AC / CP / SD / UD / SM / RC（不含 SC 以免误伤 Sporting CP）
    for prefix in ("fc", "vfl", "cd", "cf", "ac", "cp", "sd", "ud", "sm", "rc"):
        if t.startswith(prefix) and len(t) > len(prefix) + 1:
            t = t[len(prefix):]
            break
    for suffix in ("fc", "cf", "afc", "sc", "cfc", "sfc", "ufc"):
        if t.endswith(suffix) and len(t) > len(suffix) + 1:
            t = t[: -len(suffix)].rstrip(" ")
    # 去掉末尾两位数字（如 Mainz 05、Schalke 04）
    t = re.sub(r"\d{2}$", "", t).rstrip(" ")
    # United ↔ Utd（API 常用缩写）
    t = t.replace("united", "utd")
    # 门兴：Monchengladbach 与 M'gladbach 统一为 mgladbach
    t = t.replace("monchengladbach", "mgladbach")
    # 德文 München ↔ 英文 Munich（API 常用 FC Bayern München）
    t = t.replace("munchen", "munich")
    # 英文名 ↔ API 常用名（同队不同写法）
    t = t.replace("redstarbelgrade", "crvenazvezda")   # 贝尔格莱德红星
    t = t.replace("republicofireland", "ireland")      # 爱尔兰国家队
    t = t.replace("shanghaiport", "shanghaisipg")      # 上海海港
    t = t.replace("portlandtimbers", "portland")      # 波特兰伐木工
    t = t.replace("newyorkredbulls", "newyorkredbull") # 纽约红牛
    t = t.replace("sportinglisbon", "sportingcp")      # 里斯本竞技
    t = t.replace("sportingcp", "sporting")            # 统一为 sporting 便于与 API Sporting CP 匹配
    return t


def _abbrev_team(s: str) -> str | None:
    """多词队名取首字母缩写（如 Queens Park Rangers -> qpr），用于匹配 API 的缩写名。"""
    parts = (s or "").strip().split()
    if len(parts) < 2:
        return None
    return "".join(p[0] for p in parts if p).lower()


def _load_fixture_team_alias() -> dict[str, list[str]]:
    """加载 data/jingcai_fixture_team_alias.json：归一化队名 -> [别名归一化名]，用于 fixture 匹配时视为同队。键值会做归一化，可写可读队名。"""
    try:
        import config
        f = getattr(config, "DATA_DIR", None)
        if f is None:
            return {}
        from pathlib import Path
        p = Path(f) / "jingcai_fixture_team_alias.json"
        if not p.exists():
            return {}
        data = __import__("json").loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        out: dict[str, list[str]] = {}
        for k, v in data.items():
            if isinstance(k, str) and k.startswith("_"):
                continue
            k_norm = _normalize_team_for_match(str(k).strip())
            if not k_norm:
                continue
            if isinstance(v, list):
                aliases = [_normalize_team_for_match(str(x).strip()) for x in v if x]
            elif isinstance(v, str):
                aliases = [_normalize_team_for_match(v.strip())] if v.strip() else []
            else:
                continue
            out[k_norm] = list(dict.fromkeys(aliases))  # 去重保序
        return out
    except Exception:
        return {}


_team_alias_cache: dict[str, list[str]] | None = None


def _team_aliases(normalized_name: str) -> list[str]:
    """返回该归一化队名及其别名列表（含自身），用于 _team_match 扩展匹配。"""
    global _team_alias_cache
    if _team_alias_cache is None:
        _team_alias_cache = _load_fixture_team_alias()
    names = [normalized_name]
    names.extend(_team_alias_cache.get(normalized_name, []))
    return names


def _team_match(our: str, api: str) -> bool:
    """主/客队名是否视为同一队：归一化后包含关系、缩写一致、或别名表命中。"""
    if not our and not api:
        return True
    if not our or not api:
        return False
    no = _normalize_team_for_match(our)
    na = _normalize_team_for_match(api)
    if no in na or na in no:
        return True
    ab = _abbrev_team(our)
    if ab and len(ab) >= 2 and (ab == na or ab in na or na in ab):
        return True
    # 别名表：任一方归一化名在另一方「自身+别名」列表中即视为同队
    our_set = set(_team_aliases(no))
    api_set = set(_team_aliases(na))
    if our_set & api_set:
        return True
    return False


def get_fixture_id_from_fixtures_list(
    fixtures: list[dict[str, Any]], home_name: str, away_name: str
) -> int | None:
    """在已拉取的赛程列表中按主客队名匹配，返回 fixture_id。不限制联赛。支持 FC/CF 等后缀忽略及缩写匹配（如 QPR）。"""
    if not fixtures or (not (home_name or "").strip() and not (away_name or "").strip()):
        return None
    for f in fixtures:
        h = (f.get("home_name") or "").strip()
        a = (f.get("away_name") or "").strip()
        if _team_match(home_name, h) and _team_match(away_name, a):
            return f.get("fixture_id")
    return None


def get_fixture_id_for_match_all_leagues(
    date_str: str, home_name: str, away_name: str
) -> int | None:
    """按日期与主客队英文名解析 fixture_id，不限制五大联赛。"""
    from etl.api_football_odds import fixtures_with_id_all_leagues
    fixtures = fixtures_with_id_all_leagues((date_str or "")[:10])
    return get_fixture_id_from_fixtures_list(fixtures, home_name, away_name)


def fetch_predictions_for_fixture(
    fixture_id: int,
    rate_limit_sec: float = RATE_LIMIT_SEC,
    return_full: bool = False,
) -> dict[str, Any] | None | tuple[dict[str, Any] | None, dict[str, Any]]:
    """拉取 API-Football 对该场的预测，返回原始 response 首条或 None。return_full=True 时返回 (首条或None, 完整响应)。"""
    data = _request("predictions", {"fixture": fixture_id})
    time.sleep(rate_limit_sec)
    rows = data.get("response") or []
    raw = (rows[0] if isinstance(rows[0], dict) else None) if rows else None
    if return_full:
        return raw, data
    return raw


def parse_predictions_to_outcome(raw: dict[str, Any]) -> tuple[str | None, tuple[int, int] | None]:
    """
    从 API 预测原始数据解析出 (结果, 比分)。
    结果: "主" | "平" | "客"；比分: (主队进球, 客队进球) 或 None（API 未提供时）。
    支持 winner 为 1/2/3 或 "home"/"draw"/"away"，以及 API 返回的 { "id": 40, "name": "Liverpool" }（与 raw.teams 主客队对比）。
    """
    if not raw:
        return None, None
    pred = raw.get("predictions") or raw
    if isinstance(pred, list):
        pred = pred[0] if pred else {}
    if not isinstance(pred, dict):
        return None, None
    winner = pred.get("winner")
    winner_id = None
    winner_name = None
    if isinstance(winner, dict):
        winner_id = winner.get("id")
        winner_name = winner.get("name")
    else:
        winner_id = winner
        winner_name = winner
    result = None
    if winner_id is not None or winner_name is not None:
        if winner_id in (1, "1") or (isinstance(winner_name, str) and winner_name.lower() in ("home",)):
            result = "主"
        elif winner_id in (2, "2") or (isinstance(winner_name, str) and winner_name.lower() in ("draw",)):
            result = "平"
        elif winner_id in (3, "3") or (isinstance(winner_name, str) and winner_name.lower() in ("away",)):
            result = "客"
        else:
            # API 返回 winner = { "id": 40, "name": "Liverpool" }，需与 raw.teams 主客队对比
            teams = raw.get("teams") or {}
            home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
            away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
            home_id = home.get("id") if home else None
            away_id = away.get("id") if away else None
            home_name = (home.get("name") or "").strip().lower() if home else ""
            away_name = (away.get("name") or "").strip().lower() if away else ""
            if winner_id is not None and home_id is not None and winner_id == home_id:
                result = "主"
            elif winner_id is not None and away_id is not None and winner_id == away_id:
                result = "客"
            elif winner_name and home_name and str(winner_name).strip().lower() == home_name:
                result = "主"
            elif winner_name and away_name and str(winner_name).strip().lower() == away_name:
                result = "客"
            elif winner_id is not None or winner_name:
                result = "平"
    # goals: API 的 predictions.goals 多为预测进球/盘口（如 home:"-2.5", away:"-1.5"），非预测比分；展示时用「预测进球」以区分 Opta xG。
    # 仅当解析出 0~15 的整数主客进球时才当作比分，否则不展示
    scores = _parse_goals_from_raw(pred, raw)
    return result, scores


def _parse_goals_from_raw(pred: dict[str, Any], raw: dict[str, Any]) -> tuple[int, int] | None:
    """从预测原始数据中解析比分，尝试多种 API 常见结构。"""
    goals = pred.get("goals") or raw.get("goals")
    s = _goals_dict_to_scores(goals)
    if s is not None:
        return s
    comparison = pred.get("comparison") or raw.get("comparison")
    if isinstance(comparison, dict):
        s = _goals_dict_to_scores(comparison.get("goals"))
        if s is not None:
            return s
    if isinstance(goals, str) and goals.strip():
        parts = str(goals).strip().replace("–", "-").split("-")
        if len(parts) >= 2:
            h, a = _safe_int(parts[0].strip()), _safe_int(parts[1].strip())
            if h is not None and a is not None and 0 <= h <= 15 and 0 <= a <= 15:
                return (int(h), int(a))
    for text in (pred.get("advice"), pred.get("score"), raw.get("advice"), raw.get("score")):
        if not isinstance(text, str) or not text.strip():
            continue
        m = re.search(r"(\d+)\s*[-:]\s*(\d+)", text.strip())
        if m:
            h, a = _safe_int(m.group(1)), _safe_int(m.group(2))
            if h is not None and a is not None and 0 <= h <= 15 and 0 <= a <= 15:
                return (int(h), int(a))
    return None


def _goals_dict_to_scores(goals: Any) -> tuple[int, int] | None:
    """从 goals 字典或带 home/away 的嵌套结构中解析 (主队进球, 客队进球)。"""
    if not isinstance(goals, dict):
        return None
    h = _safe_int(goals.get("home") or goals.get("home_goals"))
    a = _safe_int(goals.get("away") or goals.get("away_goals"))
    if h is not None and a is not None and 0 <= h <= 15 and 0 <= a <= 15:
        return (int(h), int(a))
    return None


def parse_predictions_expected_goals(raw: dict[str, Any]) -> tuple[str, str] | None:
    """
    解析 API 的 predictions.goals（预测进球/进球线），如 home:"-2.5", away:"-1.5"。
    当不是整数比分时返回 (home_str, away_str)，用于展示「预测进球」（与 Opta 机会质量 xG 区分）。
    """
    if not raw:
        return None
    pred = raw.get("predictions") or raw
    if isinstance(pred, list):
        pred = pred[0] if pred else {}
    if not isinstance(pred, dict):
        return None
    goals = pred.get("goals") or raw.get("goals")
    if not isinstance(goals, dict):
        return None
    home_val = goals.get("home") or goals.get("home_goals")
    away_val = goals.get("away") or goals.get("away_goals")
    if home_val is None and away_val is None:
        return None
    home_s = str(home_val).strip() if home_val is not None else ""
    away_s = str(away_val).strip() if away_val is not None else ""
    if not home_s and not away_s:
        return None
    # 若已是整数比分则不再当预测进球展示（由 scores 处理）
    if _goals_dict_to_scores(goals) is not None:
        return None
    return (home_s or "—", away_s or "—")


def parse_predictions_derived_score(raw: dict[str, Any]) -> tuple[int, int] | None:
    """
    根据 API 的 predictions.goals（预测进球）推算比分。
    仅当 home/away 为非负数值且在 0~15 时，四舍五入为整数作为「推算比分」（如 1.2/0.8 → 1:1）。
    若为负数或非数值（如 "-2.5"/"-1.5"）则不推算。
    """
    if not raw:
        return None
    pred = raw.get("predictions") or raw
    if isinstance(pred, list):
        pred = pred[0] if pred else {}
    if not isinstance(pred, dict):
        return None
    goals = pred.get("goals") or raw.get("goals")
    if not isinstance(goals, dict):
        return None
    h = _safe_float(goals.get("home") or goals.get("home_goals"))
    a = _safe_float(goals.get("away") or goals.get("away_goals"))
    if h is None or a is None or h < 0 or a < 0 or h > 15 or a > 15:
        return None
    return (int(round(h)), int(round(a)))


def parse_predictions_goals_spread(raw: dict[str, Any]) -> str | None:
    """
    当 predictions.goals 为负数（盘口/让球类）时，用主客差值做倾向衍生。
    例如 home:-1.5, away:-4.5 → 差值为 3，返回 "主队比客队多进约3球（胜）"。
    """
    if not raw:
        return None
    pred = raw.get("predictions") or raw
    if isinstance(pred, list):
        pred = pred[0] if pred else {}
    if not isinstance(pred, dict):
        return None
    goals = pred.get("goals") or raw.get("goals")
    if not isinstance(goals, dict):
        return None
    h = _safe_float(goals.get("home") or goals.get("home_goals"))
    a = _safe_float(goals.get("away") or goals.get("away_goals"))
    if h is None or a is None:
        return None
    diff = round(h - a)
    if diff > 0:
        return f"主队比客队多进约{diff}球（胜）"
    if diff < 0:
        return f"客队比主队多进约{-diff}球（负）"
    return "主客相当（平）"


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
        return x
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _pct_to_float(v: Any) -> float | None:
    """解析 "45%" 或 0.45 为 float。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if 0 <= v <= 1 else float(v) / 100.0 if v > 1 else None
    s = str(v).strip().rstrip("%")
    if not s:
        return None
    try:
        x = float(s)
        return x / 100.0 if x > 1 else x
    except (TypeError, ValueError):
        return None


def parse_predictions_to_probs(raw: dict[str, Any]) -> tuple[float, float, float] | None:
    """
    从 API 预测原始数据解析出 (主胜, 平, 客胜) 概率。
    支持 percent: {"home":"45%","draw":"45%","away":"10%"} 或 winner 对象与 raw.teams 对比。
    """
    if not raw:
        return None
    pred = raw.get("predictions") or raw
    if isinstance(pred, list):
        pred = pred[0] if pred else {}
    if not isinstance(pred, dict):
        return None
    # API 的 percent 字段
    pct = pred.get("percent")
    if isinstance(pct, dict):
        home_p = _pct_to_float(pct.get("home"))
        draw_p = _pct_to_float(pct.get("draw"))
        away_p = _pct_to_float(pct.get("away"))
        if home_p is not None and draw_p is not None and away_p is not None and (home_p + draw_p + away_p) > 0:
            s = home_p + draw_p + away_p
            return (home_p / s, draw_p / s, away_p / s)
    # 尝试 win_or_draw / winner 下的 home, draw, away 数值
    win = pred.get("win_or_draw") or pred.get("winner")
    if isinstance(win, dict):
        home_p = _safe_float(win.get("home"))
        draw_p = _safe_float(win.get("draw"))
        away_p = _safe_float(win.get("away"))
        if home_p is not None and draw_p is not None and away_p is not None and (home_p + draw_p + away_p) > 0:
            s = home_p + draw_p + away_p
            return (home_p / s, draw_p / s, away_p / s)
    # winner: 1/2/3 或 "home"/"draw"/"away" 或 { id, name }（与 raw.teams 对比）
    winner = pred.get("winner")
    winner_id = winner.get("id") if isinstance(winner, dict) else winner
    winner_name = winner.get("name") if isinstance(winner, dict) else winner
    if winner_id is None and winner_name is None:
        winner_id = winner
        winner_name = winner
    if winner_id is not None or winner_name is not None:
        if winner_id in (1, "1") or (isinstance(winner_name, str) and winner_name.lower() == "home"):
            return (0.6, 0.2, 0.2)
        if winner_id in (2, "2") or (isinstance(winner_name, str) and winner_name.lower() == "draw"):
            return (0.2, 0.6, 0.2)
        if winner_id in (3, "3") or (isinstance(winner_name, str) and winner_name.lower() == "away"):
            return (0.2, 0.2, 0.6)
        teams = raw.get("teams") or {}
        home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
        away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
        if winner_id is not None and (home.get("id") == winner_id or (away.get("id") == winner_id)):
            return (0.6, 0.2, 0.2) if home.get("id") == winner_id else (0.2, 0.2, 0.6)
        if winner_name and (str(winner_name).strip().lower() == (home.get("name") or "").strip().lower() or str(winner_name).strip().lower() == (away.get("name") or "").strip().lower()):
            return (0.6, 0.2, 0.2) if str(winner_name).strip().lower() == (home.get("name") or "").strip().lower() else (0.2, 0.2, 0.6)
    return None


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_api_predictions_probs(date_str: str, home_name: str, away_name: str) -> tuple[float, float, float] | None:
    """
    根据日期与主客队获取 API-Football 预测概率（会请求 fixtures + predictions，共 2 次调用）。
    返回 (主胜, 平, 客胜) 或 None。
    """
    fid = get_fixture_id_for_match(date_str, home_name, away_name)
    if not fid:
        return None
    raw = fetch_predictions_for_fixture(fid)
    return parse_predictions_to_probs(raw) if raw else None


def get_api_predictions_full(
    date_str: str, home_name: str, away_name: str
) -> tuple[tuple[float, float, float] | None, str | None, tuple[int, int] | None]:
    """
    一次请求同时返回：概率 (主胜,平,客胜)、结果 (主|平|客)、比分 (h,a) 或 None。
    用于预测流程中既做融合又存 API 单独结果。
    """
    fid = get_fixture_id_for_match(date_str, home_name, away_name)
    if not fid:
        return None, None, None
    raw = fetch_predictions_for_fixture(fid)
    if not raw:
        return None, None, None
    probs = parse_predictions_to_probs(raw)
    result, scores = parse_predictions_to_outcome(raw)
    return probs, result, scores
