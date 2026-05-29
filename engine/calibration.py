"""
概率校准：用历史「预测概率 vs 实际结果」拟合 Platt 缩放（sigmoid），使输出概率更贴近实际发生频率。
拟合数据来源：DB predictions+matches 或 prediction_log 中已填比分的条目。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

import config


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p.astype(float), 1e-6, 1.0 - 1e-6)
    return np.log(p / (1.0 - p))


def _sigmoid(x: np.ndarray) -> np.ndarray:
    x = np.clip(x.astype(float), -500, 500)
    return 1.0 / (1.0 + np.exp(-x))


def _platt_loss(ab: tuple[float, float], x: np.ndarray, y: np.ndarray) -> float:
    A, B = ab
    if A <= 0:
        return 1e10
    z = A * _logit(x) + B
    p = _sigmoid(z)
    p = np.clip(p, 1e-6, 1.0 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))


def _fit_platt(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """网格搜索找最小 log-loss 的 A, B。"""
    best_loss = 1e10
    best_A, best_B = 1.0, 0.0
    for A in [0.3, 0.5, 0.7, 1.0, 1.3, 1.5, 2.0]:
        for B in [-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5]:
            loss = _platt_loss((A, B), x, y)
            if loss < best_loss:
                best_loss, best_A, best_B = loss, A, B
    return (best_A, best_B)


def load_calibration_params() -> dict[str, Any] | None:
    """
    从 config.PREDICT_CALIBRATION_PATH 读取校准参数。
    支持两种格式：
    - 联赛专属：{"default": {home, draw, away}, "by_competition": {"6001": {...}, ...}}
    - 旧版单一：{"home": {...}, "draw": {...}, "away": {...}}，加载后等价于仅 default。
    无文件或格式错误返回 None。
    """
    path = Path(getattr(config, "PREDICT_CALIBRATION_PATH", config.DATA_DIR / "calibration_params.json"))
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        # 新版：含 default 或 by_competition
        if "default" in data:
            for key in ("home", "draw", "away"):
                if key not in data["default"] or "A" not in data["default"][key] or "B" not in data["default"][key]:
                    return None
            return data
        # 旧版：顶层即 home/draw/away
        for key in ("home", "draw", "away"):
            if key not in data or not isinstance(data[key], dict):
                return None
            if "A" not in data[key] or "B" not in data[key]:
                return None
        return {"default": data, "by_competition": {}}
    except Exception:
        return None


def _get_calibration_set(params: dict[str, Any], competition_id: int | None) -> dict[str, dict[str, float]]:
    """从完整校准参数中取出当前联赛使用的集合（联赛专属优先，否则 default）。"""
    if not params:
        return {}
    by_comp = params.get("by_competition") or {}
    if competition_id is not None and str(competition_id) in by_comp:
        return by_comp[str(competition_id)]
    return params.get("default") or params


def apply_calibration(
    prob_home: float, prob_draw: float, prob_away: float,
    params: dict[str, Any],
    competition_id: int | None = None,
) -> tuple[float, float, float]:
    """对胜/平/负概率做 Platt 校准后再归一化。competition_id 用于选取联赛专属参数（有则用，无则用 default）。"""
    cal = _get_calibration_set(params, competition_id)
    if not cal or "home" not in cal or "draw" not in cal or "away" not in cal:
        return (float(prob_home), float(prob_draw), float(prob_away))
    ph = np.clip(float(prob_home), 1e-6, 1.0 - 1e-6)
    pd = np.clip(float(prob_draw), 1e-6, 1.0 - 1e-6)
    pa = np.clip(float(prob_away), 1e-6, 1.0 - 1e-6)
    Ah, Bh = cal["home"]["A"], cal["home"]["B"]
    Ad, Bd = cal["draw"]["A"], cal["draw"]["B"]
    Aa, Ba = cal["away"]["A"], cal["away"]["B"]
    ch = _sigmoid(np.array([Ah * _logit(np.array([ph]))[0] + Bh]))[0]
    cd = _sigmoid(np.array([Ad * _logit(np.array([pd]))[0] + Bd]))[0]
    ca = _sigmoid(np.array([Aa * _logit(np.array([pa]))[0] + Ba]))[0]
    total = ch + cd + ca
    if total <= 0:
        return (prob_home, prob_draw, prob_away)
    return (float(ch / total), float(cd / total), float(ca / total))


def _collect_from_mongo() -> list[tuple[float, float, float, str, int | None, str | None]]:
    """从 Mongo predictions+matches（通过 sync_source_rows）收集 (prob_home, prob_draw, prob_away, actual, competition_id, date_str)。"""
    from db import repo
    if not repo.use_mongo():
        return []
    rows = repo.prediction_log_sync_source_rows()
    out: list[tuple[float, float, float, str, int | None, str | None]] = []
    for r in rows:
        ph = float(r.get("prob_home_win", 0))
        pd = float(r.get("prob_draw", 0))
        pa = float(r.get("prob_away_win", 0))
        h, a = int(r.get("home_score", 0)), int(r.get("away_score", 0))
        if h > a:
            actual = "home"
        elif h < a:
            actual = "away"
        else:
            actual = "draw"
        date_str = (r.get("log_date") or "")[:10] or None
        out.append((ph, pd, pa, actual, r.get("competition_id"), date_str))
    return out


def _collect_from_db(engine) -> list[tuple[float, float, float, str, int | None, str | None]]:
    """从 predictions + matches 收集 (prob_home, prob_draw, prob_away, actual, competition_id, date_str)。"""
    from sqlalchemy import text
    rows = engine.connect().execute(text("""
        SELECT p.prob_home_win, p.prob_draw, p.prob_away_win,
               m.home_score, m.away_score, p.competition_id, m.match_date
        FROM predictions p
        JOIN matches m ON m.id = p.match_id
        WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
    """)).fetchall()
    out: list[tuple[float, float, float, str, int | None, str | None]] = []
    for r in rows:
        ph, pd, pa = float(r[0]), float(r[1]), float(r[2])
        h, a = int(r[3]), int(r[4])
        cid = int(r[5]) if r[5] is not None else None
        date_str = (r[6] or "")[:10] if len(r) > 6 and r[6] else None
        if h > a:
            actual = "home"
        elif h < a:
            actual = "away"
        else:
            actual = "draw"
        out.append((ph, pd, pa, actual, cid, date_str))
    return out


def _collect_from_log() -> list[tuple[float, float, float, str, int | None, str | None]]:
    """从 prediction_log 中已填比分的条目收集 (prob_home, prob_draw, prob_away, actual, competition_id, date_str)。无日期填 None。"""
    from engine.prediction_log import load_log
    entries = load_log()
    out: list[tuple[float, float, float, str, int | None, str | None]] = []
    for e in entries:
        ah = e.get("actual_home_score")
        aa = e.get("actual_away_score")
        if ah is None or aa is None:
            continue
        ph = e.get("prob_home_win")
        pd = e.get("prob_draw")
        pa = e.get("prob_away_win")
        if ph is None or pd is None or pa is None:
            continue
        try:
            ph, pd, pa = float(ph), float(pd), float(pa)
        except (TypeError, ValueError):
            continue
        if ah > aa:
            actual = "home"
        elif ah < aa:
            actual = "away"
        else:
            actual = "draw"
        date_str = (e.get("log_date") or e.get("match_date") or "")[:10] or None
        out.append((ph, pd, pa, actual, None, date_str))
    return out


def _collect_from_self_improvement() -> list[tuple[float, float, float, str, int | None, str | None]]:
    """从 self_improvement 库（如 self_improvement_analysis）的 jc_predictions + jingcai_match_analysis 收集校准样本，含 date_str。"""
    try:
        from db.self_improvement import jc_calibration_samples
        return jc_calibration_samples()
    except Exception:
        return []


def fit_and_save(
    engine=None,
    use_log: bool = True,
    use_db: bool = True,
    use_self_improvement: bool = True,
    by_competition: bool = False,
    train_date_from: str | None = None,
    train_date_to: str | None = None,
) -> dict[str, Any]:
    """
    从 DB 和/或 prediction_log 和/或 self_improvement 库收集历史预测与结果，拟合 Platt 参数并写入 config.PREDICT_CALIBRATION_PATH。
    by_competition=True 时：按 competition_id 分组，每组≥20 条则拟合联赛专属参数。
    train_date_from/train_date_to 同时给出时，仅保留该日期范围内的样本（用于「仅用往年数据训练、预测当年」对比）。
    """
    from db import repo
    samples: list[tuple[float, float, float, str, int | None, str | None]] = []
    if use_log:
        samples.extend(_collect_from_log())
    if use_db:
        if engine is not None:
            samples.extend(_collect_from_db(engine))
        elif repo.use_mongo():
            samples.extend(_collect_from_mongo())
    if use_self_improvement:
        samples.extend(_collect_from_self_improvement())
    # 统一为 6 元组，缺日期补 None
    def _norm(s):
        if len(s) >= 6:
            return s
        return (*s[:5], s[5] if len(s) > 5 else None)
    samples = [_norm(s) for s in samples]
    if train_date_from and train_date_to:
        from_str = (train_date_from or "")[:10]
        to_str = (train_date_to or "")[:10]
        filtered = [s for s in samples if s[5] and from_str <= (s[5] or "")[:10] <= to_str]
        if len(filtered) < 20:
            return {"n_samples": len(filtered), "error": f"按日期 {from_str}~{to_str} 过滤后样本不足（至少 20 条）"}
        samples = filtered
    if len(samples) < 20:
        return {"n_samples": len(samples), "error": "样本不足（至少 20 条）"}

    def _fit_set(subs: list[tuple[float, float, float, str, int | None, str | None]]) -> dict[str, dict[str, float]]:
        subs_5 = [s[:5] for s in subs]
        ph_arr = np.array([s[0] for s in subs_5])
        pd_arr = np.array([s[1] for s in subs_5])
        pa_arr = np.array([s[2] for s in subs_5])
        y_home = np.array([1.0 if s[3] == "home" else 0.0 for s in subs_5])
        y_draw = np.array([1.0 if s[3] == "draw" else 0.0 for s in subs_5])
        y_away = np.array([1.0 if s[3] == "away" else 0.0 for s in subs_5])
        Ah, Bh = _fit_platt(ph_arr, y_home)
        Ad, Bd = _fit_platt(pd_arr, y_draw)
        Aa, Ba = _fit_platt(pa_arr, y_away)
        return {"home": {"A": Ah, "B": Bh}, "draw": {"A": Ad, "B": Bd}, "away": {"A": Aa, "B": Ba}}

    default_params = _fit_set(samples)
    out_payload: dict[str, Any] = {"default": default_params}
    if by_competition:
        by_cid: dict[int | None, list] = {}
        for s in samples:
            cid = s[4]
            by_cid.setdefault(cid, []).append(s)
        by_comp = {}
        min_per_league = int(getattr(config, "PREDICT_CALIBRATION_MIN_PER_LEAGUE", 20))
        for cid, subs in by_cid.items():
            if len(subs) < min_per_league:
                continue
            by_comp[str(cid) if cid is not None else "null"] = _fit_set(subs)
        out_payload["by_competition"] = by_comp

    path = Path(getattr(config, "PREDICT_CALIBRATION_PATH", config.DATA_DIR / "calibration_params.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out_payload, indent=2), encoding="utf-8")
    result: dict[str, Any] = {"n_samples": len(samples), "path": str(path), **default_params}
    if by_competition:
        result["by_competition"] = out_payload.get("by_competition", {})
    return result
