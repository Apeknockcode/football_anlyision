"""
基于历史比分的简单预测：近期场均进球/失球 -> 期望进球 -> 胜平负概率。
仅使用已结束比赛（home_score/away_score 非空）。
支持将预测落库并与完赛结果对比（事后分析：MAE、胜平负准确率、精确比分命中率）。
"""
from __future__ import annotations

import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

import config
from etl.schema import Prediction, get_engine


def odds_to_implied_probs(odds: dict) -> tuple[float, float, float] | None:
    """
    从胜平负赔率 spf_win/spf_draw/spf_loss 得到归一化隐含概率 (主胜, 平, 客胜)。
    缺任一或解析失败返回 None。若隐含概率总和偏离 1 过远（赔率质量异常）也返回 None。
    """
    win_s = odds.get("spf_win")
    draw_s = odds.get("spf_draw")
    loss_s = odds.get("spf_loss")
    if win_s is None or draw_s is None or loss_s is None:
        return None
    try:
        o_win = float(win_s) if isinstance(win_s, str) else win_s
        o_draw = float(draw_s) if isinstance(draw_s, str) else draw_s
        o_loss = float(loss_s) if isinstance(loss_s, str) else loss_s
    except (TypeError, ValueError):
        return None
    if o_win <= 0 or o_draw <= 0 or o_loss <= 0:
        return None
    inv = 1.0 / o_win, 1.0 / o_draw, 1.0 / o_loss
    total = inv[0] + inv[1] + inv[2]
    if total <= 0:
        return None
    # 赔率质量：隐含概率总和应在合理区间（典型 overround 约 1.02～1.15），过偏则视为异常
    total_min = getattr(config, "PREDICT_ODDS_IMPLIED_SUM_MIN", 0.85)
    total_max = getattr(config, "PREDICT_ODDS_IMPLIED_SUM_MAX", 1.35)
    if total < total_min or total > total_max:
        return None
    return (inv[0] / total, inv[1] / total, inv[2] / total)


def fuse_probs(
    model_probs: tuple[float, float, float],
    odds_probs: tuple[float, float, float],
    weight_model: float,
) -> tuple[float, float, float]:
    """线性融合模型概率与赔率隐含概率，再归一化。"""
    w = max(0.0, min(1.0, weight_model))
    p_h = w * model_probs[0] + (1.0 - w) * odds_probs[0]
    p_d = w * model_probs[1] + (1.0 - w) * odds_probs[1]
    p_a = w * model_probs[2] + (1.0 - w) * odds_probs[2]
    total = p_h + p_d + p_a
    if total <= 0:
        return model_probs
    return (p_h / total, p_d / total, p_a / total)


def _get_league_overrides(engine: Engine | None, competition_id: int | None) -> dict:
    """根据 competition_id 解析赛事名，返回该联赛在 predict_league_params 中的覆盖参数字典。"""
    if competition_id is None:
        return {}
    from db import repo
    if engine is None or repo.use_mongo():
        return repo.predict_get_league_overrides(competition_id)
    overrides_all = config.get_league_overrides()
    if not overrides_all:
        return {}
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT name FROM competitions WHERE id = :cid"),
            {"cid": competition_id},
        ).fetchone()
    if not row or not row[0]:
        return {}
    name = (row[0] or "").strip()
    return overrides_all.get(name, {})


def _cfg(overrides: dict | None, key: str, default: Any) -> Any:
    """本次预测有效配置：先查联赛 overrides，无则用 config。"""
    if overrides and key in overrides:
        return overrides[key]
    return getattr(config, key, default)


def _min_venue_samples(overrides: dict | None = None) -> int:
    return int(_cfg(overrides, "PREDICT_MIN_VENUE_SAMPLES", 2))


def _weighted_avg(
    recent: list, use_weights: bool, form_weight_decay: float = 0.0
) -> tuple[float, float]:
    """对 recent 的 (_, _, goals_for, goals_against) 做加权平均。use_weights 时最近场次权重大。
    form_weight_decay 在 (0,1) 时用指数衰减（最近=1，次近=decay，再次=decay^2…）；0 则用线性加权。"""
    if not recent:
        return (0.0, 0.0)
    n = len(recent)
    if use_weights:
        if 0 < form_weight_decay < 1:
            weights = [form_weight_decay ** t for t in range(n)]
        else:
            weights = [1.0 + 0.5 * (1.0 - t / max(1, n)) for t in range(n)]
        w_sum = sum(weights)
        if w_sum <= 0:
            weights = [1.0] * n
            w_sum = float(n)
        goals_for = sum(r[2] * weights[t] for t, r in enumerate(recent))
        goals_against = sum(r[3] * weights[t] for t, r in enumerate(recent))
        return (goals_for / w_sum, goals_against / w_sum)
    goals_for = sum(r[2] for r in recent)
    goals_against = sum(r[3] for r in recent)
    return (goals_for / n, goals_against / n)


def _team_recent_form_at_venue(
    engine: Engine | None,
    team_id: int,
    competition_id: int | None,
    last_n: int,
    at_home: bool,
    use_time_weight: bool = False,
    overrides: dict | None = None,
) -> tuple[float, float, int]:
    """
    只取该队作为主队（at_home=True）或客队（at_home=False）的最近 last_n 场，
    返回 (avg_goals_for, avg_goals_against, 实际场次数)。use_time_weight 时近期场次权重大。
    overrides 非空时可用 PREDICT_FORM_WEIGHT_DECAY 做指数衰减加权。
    """
    from db import repo
    if engine is None or repo.use_mongo():
        return repo.predict_team_recent_form(team_id, competition_id, last_n, at_home, use_time_weight)
    with engine.connect() as conn:
        if at_home:
            rows = conn.execute(
                text("""
                    SELECT match_date, id, home_score, away_score FROM matches
                    WHERE home_team_id = :tid AND home_score IS NOT NULL AND away_score IS NOT NULL
                      AND (:cid IS NULL OR competition_id = :cid)
                """),
                {"tid": team_id, "cid": competition_id},
            ).fetchall()
            combined = [(r[0], r[1], r[2], r[3]) for r in rows]
        else:
            rows = conn.execute(
                text("""
                    SELECT match_date, id, home_score, away_score FROM matches
                    WHERE away_team_id = :tid AND home_score IS NOT NULL AND away_score IS NOT NULL
                      AND (:cid IS NULL OR competition_id = :cid)
                """),
                {"tid": team_id, "cid": competition_id},
            ).fetchall()
            combined = [(r[0], r[1], r[3], r[2]) for r in rows]
    combined.sort(key=lambda x: (x[0] or "", x[1]), reverse=True)
    recent = combined[:last_n]
    if not recent:
        return (0.0, 0.0, 0)
    decay = float(_cfg(overrides, "PREDICT_FORM_WEIGHT_DECAY", 0)) if overrides else 0.0
    avg_for, avg_against = _weighted_avg(recent, use_time_weight, decay)
    return (avg_for, avg_against, len(recent))


def _team_recent_form(
    engine: Engine | None,
    team_id: int,
    competition_id: int | None,
    last_n: int,
    use_time_weight: bool = False,
    overrides: dict | None = None,
) -> tuple[float, float]:
    """
    取该队最近 last_n 场已结束比赛的场均进球、场均失球（主客混合）。
    若指定 competition_id 则只在该赛事内统计。
    Returns (avg_goals_for, avg_goals_against)。用于主客场样本不足时的 fallback。
    overrides 非空时可用 PREDICT_FORM_WEIGHT_DECAY 做指数衰减加权。
    """
    from db import repo
    if engine is None or repo.use_mongo():
        return repo.predict_team_recent_form_mixed(team_id, competition_id, last_n, use_time_weight)
    with engine.connect() as conn:
        home_rows = conn.execute(
            text("""
                SELECT match_date, id, home_score, away_score FROM matches
                WHERE home_team_id = :tid AND home_score IS NOT NULL AND away_score IS NOT NULL
                  AND (:cid IS NULL OR competition_id = :cid)
            """),
            {"tid": team_id, "cid": competition_id},
        ).fetchall()
        away_rows = conn.execute(
            text("""
                SELECT match_date, id, home_score, away_score FROM matches
                WHERE away_team_id = :tid AND home_score IS NOT NULL AND away_score IS NOT NULL
                  AND (:cid IS NULL OR competition_id = :cid)
            """),
            {"tid": team_id, "cid": competition_id},
        ).fetchall()

    combined = [
        (r[0], r[1], r[2], r[3]) for r in home_rows
    ] + [
        (r[0], r[1], r[3], r[2]) for r in away_rows
    ]
    combined.sort(key=lambda x: (x[0] or "", x[1]), reverse=True)
    recent = combined[:last_n]
    if not recent:
        return (0.0, 0.0)
    decay = float(_cfg(overrides, "PREDICT_FORM_WEIGHT_DECAY", 0)) if overrides else 0.0
    return _weighted_avg(recent, use_time_weight, decay)


def _league_avg_goals(
    engine: Engine | None,
    competition_id: int | None,
    last_n_matches: int = 50,
    overrides: dict | None = None,
) -> tuple[float, float]:
    """
    联赛近期场均进球（主队场均、客队场均），主客细分用于收缩。
    若 competition_id 为 None 或无数据，返回 (1.0, 1.0)。
    """
    last_n = int(_cfg(overrides, "PREDICT_LEAGUE_AVG_LAST_N", 50))
    if last_n_matches > 0:
        last_n = last_n_matches
    from db import repo
    if engine is None or repo.use_mongo():
        return repo.predict_league_avg_goals(competition_id, last_n)
    with engine.connect() as conn:
        if competition_id is None:
            rows = conn.execute(
                text("""
                    SELECT home_score, away_score FROM matches
                    WHERE home_score IS NOT NULL AND away_score IS NOT NULL
                    ORDER BY match_date DESC, id DESC LIMIT :lim
                """),
                {"lim": last_n},
            ).fetchall()
        else:
            rows = conn.execute(
                text("""
                    SELECT home_score, away_score FROM matches
                    WHERE competition_id = :cid AND home_score IS NOT NULL AND away_score IS NOT NULL
                    ORDER BY match_date DESC, id DESC LIMIT :lim
                """),
                {"cid": competition_id, "lim": last_n},
            ).fetchall()
    if not rows:
        return (1.0, 1.0)
    home_sum = sum(r[0] for r in rows)
    away_sum = sum(r[1] for r in rows)
    n = len(rows)
    return (home_sum / n, away_sum / n)


def _head_to_head_form(
    engine: Engine | None,
    home_team_id: int,
    away_team_id: int,
    competition_id: int | None,
    last_n: int,
) -> tuple[float, float, float, float, int]:
    """
    历史交锋：主队 vs 客队的最近 last_n 场。
    返回 (主队场均进, 主队场均失, 客队场均进, 客队场均失, 场次数)。无交锋则 (0,0,0,0,0)。
    """
    from db import repo
    if engine is None or repo.use_mongo():
        return repo.predict_head_to_head(home_team_id, away_team_id, competition_id, last_n)
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT match_date, id, home_score, away_score FROM matches
                WHERE home_team_id = :hid AND away_team_id = :aid
                  AND home_score IS NOT NULL AND away_score IS NOT NULL
                  AND (:cid IS NULL OR competition_id = :cid)
                ORDER BY match_date DESC, id DESC LIMIT :lim
            """),
            {"hid": home_team_id, "aid": away_team_id, "cid": competition_id, "lim": last_n},
        ).fetchall()
    if not rows:
        return (0.0, 0.0, 0.0, 0.0, 0)
    home_for = sum(r[2] for r in rows) / len(rows)
    home_against = sum(r[3] for r in rows) / len(rows)
    away_for = sum(r[3] for r in rows) / len(rows)
    away_against = sum(r[2] for r in rows) / len(rows)
    return (home_for, home_against, away_for, away_against, len(rows))


def _days_since_last_match(
    engine: Engine | None,
    team_id: int,
    before_date: str | None,
    competition_id: int | None,
) -> int | None:
    """该队在 before_date 之前最近一场的比赛日与 before_date 的天数差；无则返回 None。"""
    if not before_date or len(before_date) < 10:
        return None
    from db import repo
    if engine is None or repo.use_mongo():
        return repo.predict_days_since_last_match(team_id, before_date, competition_id)
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT match_date FROM matches
                WHERE (home_team_id = :tid OR away_team_id = :tid)
                  AND home_score IS NOT NULL AND away_score IS NOT NULL
                  AND (:cid IS NULL OR competition_id = :cid)
                  AND (match_date IS NULL OR match_date < :bd)
                ORDER BY match_date DESC LIMIT 1
            """),
            {"tid": team_id, "cid": competition_id, "bd": before_date[:10]},
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        from datetime import datetime
        d0 = datetime.strptime(row[0][:10], "%Y-%m-%d")
        d1 = datetime.strptime(before_date[:10], "%Y-%m-%d")
        return (d1 - d0).days
    except Exception:
        return None


def _consecutive_away_count(
    team_id: int, before_date: str | None, competition_id: int | None, look_n: int = 5
) -> int:
    """该队在 before_date 之前最近 look_n 场中，从最近一场起连续客场的场数。"""
    if not before_date or len(before_date) < 10:
        return 0
    from db import repo
    matches = repo.predict_team_recent_matches_with_source(team_id, before_date, competition_id, look_n)
    count = 0
    for m in matches:
        if m.get("away_team_id") == team_id:
            count += 1
        else:
            break
    return count


def _poisson_pmf(lam: float, k: int) -> float:
    import math
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * (math.exp(-lam)) / (math.factorial(k) if k <= 10 else math.factorial(10))


def _draw_boost_coef(overrides: dict | None = None) -> float:
    return float(_cfg(overrides, "PREDICT_DRAW_BOOST_COEF", 0.15))


def _dc_rho(overrides: dict | None = None) -> float:
    return float(_cfg(overrides, "PREDICT_DC_RHO", -0.13))


def _dc_tau(exp_home: float, exp_away: float, i: int, j: int, overrides: dict | None = None) -> float:
    """Dixon-Coles 低比分相关修正因子：tau(i,j)。"""
    rho = _dc_rho(overrides)
    if rho == 0:
        return 1.0
    if i == 0 and j == 0:
        return 1.0 - rho * (exp_home * exp_away)
    if i == 0 and j == 1:
        return 1.0 + rho * exp_home
    if i == 1 and j == 0:
        return 1.0 + rho * exp_away
    if i == 1 and j == 1:
        return 1.0 - rho
    return 1.0


def _max_goals(overrides: dict | None = None) -> int:
    """比分网格每侧最大进球数（0..max_goals-1），支持联赛覆盖 PREDICT_MAX_GOALS。"""
    return int(_cfg(overrides, "PREDICT_MAX_GOALS", getattr(config, "PREDICT_MAX_GOALS", 8)))


def probs_from_exp_goals(
    exp_home: float, exp_away: float, overrides: dict | None = None
) -> tuple[float, float, float]:
    """
    由期望进球算胜/平/负概率（Dixon-Coles 泊松 + 平局修正）。供预测与伤停调整后重算概率复用。
    返回 (p_home_win, p_draw, p_away_win)，已归一化。
    """
    p_home_win = 0.0
    p_draw = 0.0
    p_away_win = 0.0
    mg = _max_goals(overrides)
    for i in range(mg):
        for j in range(mg):
            p = _poisson_pmf(exp_home, i) * _poisson_pmf(exp_away, j) * _dc_tau(exp_home, exp_away, i, j, overrides)
            if i > j:
                p_home_win += p
            elif i < j:
                p_away_win += p
            else:
                p_draw += p
    total = p_home_win + p_draw + p_away_win
    if total > 0:
        p_home_win /= total
        p_draw /= total
        p_away_win /= total
    draw_boost = 1.0 + _draw_boost_coef(overrides) * math.exp(-abs(exp_home - exp_away))
    p_draw *= draw_boost
    total = p_home_win + p_draw + p_away_win
    if total > 0:
        p_home_win /= total
        p_draw /= total
        p_away_win /= total
    return (p_home_win, p_draw, p_away_win)


def _score_probs_from_exp_goals(
    exp_home: float, exp_away: float, overrides: dict | None = None
) -> list[tuple[tuple[int, int], float]]:
    """由期望进球得到各比分概率列表（Dixon-Coles），用于取 top_scores。网格大小见 PREDICT_MAX_GOALS。"""
    scores_probs: list[tuple[tuple[int, int], float]] = []
    mg = _max_goals(overrides)
    for i in range(mg):
        for j in range(mg):
            p = _poisson_pmf(exp_home, i) * _poisson_pmf(exp_away, j) * _dc_tau(exp_home, exp_away, i, j, overrides)
            scores_probs.append(((i, j), p))
    return scores_probs


def _dominant_1x2(ph: float, pd: float, pa: float) -> str:
    """返回 'H'|'D'|'A'，与 predict_match 中胜平负比较顺序一致（平手时优先主胜再客胜）。"""
    if ph >= pd and ph >= pa:
        return "H"
    if pa >= ph and pa >= pd:
        return "A"
    return "D"


def _score_matches_outcome(i: int, j: int, outcome: str) -> bool:
    if outcome == "H":
        return i > j
    if outcome == "A":
        return i < j
    return i == j


def _pick_predicted_scores(
    scores_probs: list[tuple[tuple[int, int], float]],
    ph: float,
    pd: float,
    pa: float,
    exp_home: float,
    exp_away: float,
    *,
    align_to_1x2: bool,
) -> tuple[tuple[int, int], list[tuple[int, int]]]:
    """
    主预测比分 + 次选。align_to_1x2=True 时：先在最终 1X2 众数对应的结果子集（主胜/平/客胜）内取 DC 联合概率最高的比分，
    与页面上展示的胜平负概率一致，减少「预测主胜却给 0-1」类矛盾，一般可提高精确比分命中。
    """
    if not scores_probs:
        rh, ra = max(0, round(exp_home)), max(0, round(exp_away))
        return (rh, ra), [(rh, ra)]

    def sort_key(item: tuple[tuple[int, int], float]) -> tuple[float, int, int]:
        (i, j), p = item
        return (-p, i, j)

    sorted_all = sorted(scores_probs, key=sort_key)

    pool = list(scores_probs)
    if align_to_1x2:
        o = _dominant_1x2(ph, pd, pa)
        aligned = [(pair, p) for pair, p in scores_probs if _score_matches_outcome(pair[0], pair[1], o)]
        if aligned:
            pool = aligned

    pool_sorted = sorted(pool, key=sort_key)
    primary = pool_sorted[0][0]
    top_scores: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for pair, _ in pool_sorted:
        if pair not in seen:
            seen.add(pair)
            top_scores.append(pair)
        if len(top_scores) >= 2:
            break
    if len(top_scores) < 2:
        for pair, _ in sorted_all:
            if pair not in seen:
                top_scores.append(pair)
                break
    if len(top_scores) < 2:
        rh, ra = max(0, round(exp_home)), max(0, round(exp_away))
        if (rh, ra) != top_scores[0]:
            top_scores.append((rh, ra))
        else:
            for pair, _ in sorted_all:
                if pair != top_scores[0]:
                    top_scores.append(pair)
                    break
    return primary, top_scores[:2]


def _compute_exp_goals_one(
    eng: Engine,
    home_team_id: int,
    away_team_id: int,
    competition_id: int | None,
    last_n: int,
    use_time_weight: bool,
    overrides: dict | None = None,
    h2h_override: tuple[float, float, float, float, int] | None = None,
) -> tuple[float, float, int, int]:
    """
    单次期望进球计算（供融合用）。返回 (exp_home, exp_away, home_n, away_n) 场次样本数。
    h2h_override: 可选，体彩/竞彩历史交锋 (主队场均进, 主队场均失, 客队场均进, 客队场均失, 场数)，有则优先于主库 _head_to_head_form。
    """
    min_venue = _min_venue_samples(overrides)
    home_at_home_f, home_at_home_a, home_venue_n = _team_recent_form_at_venue(
        eng, home_team_id, competition_id, last_n, at_home=True, use_time_weight=use_time_weight, overrides=overrides
    )
    away_at_away_f, away_at_away_a, away_venue_n = _team_recent_form_at_venue(
        eng, away_team_id, competition_id, last_n, at_home=False, use_time_weight=use_time_weight, overrides=overrides
    )
    home_mixed = _team_recent_form(eng, home_team_id, competition_id, last_n, use_time_weight=use_time_weight, overrides=overrides)
    away_mixed = _team_recent_form(eng, away_team_id, competition_id, last_n, use_time_weight=use_time_weight, overrides=overrides)
    if home_venue_n >= min_venue:
        home_for, home_against = home_at_home_f, home_at_home_a
    else:
        home_for, home_against = home_mixed[0], home_mixed[1]
    if away_venue_n >= min_venue:
        away_for, away_against = away_at_away_f, away_at_away_a
    else:
        away_for, away_against = away_mixed[0], away_mixed[1]
    home_n = home_venue_n if home_venue_n >= min_venue else min(last_n, 20)
    away_n = away_venue_n if away_venue_n >= min_venue else min(last_n, 20)

    league_avg_h, league_avg_a = _league_avg_goals(eng, competition_id, overrides=overrides)
    league_avg = (league_avg_h + league_avg_a) / 2.0 if (league_avg_h + league_avg_a) > 0 else 1.0
    raw_exp_home = (home_for + away_against) / 2.0 if (home_for + away_against) > 0 else league_avg
    raw_exp_away = (away_for + home_against) / 2.0 if (away_for + home_against) > 0 else league_avg

    # 历史交锋融合：优先使用传入的 h2h_override（如 jingcai_match_analysis 体彩历史），否则查主库
    blend = float(_cfg(overrides, "PREDICT_H2H_BLEND", 0.3))
    h2h = h2h_override if (h2h_override and len(h2h_override) >= 5 and h2h_override[4] >= 1) else _head_to_head_form(eng, home_team_id, away_team_id, competition_id, last_n)
    if h2h[4] >= 1 and blend > 0:
        h2h_exp_h = (h2h[0] + h2h[3]) / 2.0 if (h2h[0] + h2h[3]) > 0 else raw_exp_home
        h2h_exp_a = (h2h[2] + h2h[1]) / 2.0 if (h2h[2] + h2h[1]) > 0 else raw_exp_away
        raw_exp_home = blend * h2h_exp_h + (1 - blend) * raw_exp_home
        raw_exp_away = blend * h2h_exp_a + (1 - blend) * raw_exp_away

    # 联赛收缩：样本不足时向联赛平均收缩
    min_samples = int(_cfg(overrides, "PREDICT_LEAGUE_SHRINKAGE_MIN_SAMPLES", 5))
    alpha_h = max(0, 1.0 - home_n / min_samples) if home_n < min_samples else 0.0
    alpha_a = max(0, 1.0 - away_n / min_samples) if away_n < min_samples else 0.0
    exp_home = (1 - alpha_h) * raw_exp_home + alpha_h * league_avg_h
    exp_away = (1 - alpha_a) * raw_exp_away + alpha_a * league_avg_a

    return (exp_home, exp_away, home_n, away_n)


def predict_match(
    engine: Engine | None,
    home_team_id: int,
    away_team_id: int,
    competition_id: int | None = None,
    last_n: int = 10,
    use_time_weight: bool = True,
    home_absent: list | None = None,
    away_absent: list | None = None,
    home_absent_key_count: int = 0,
    away_absent_key_count: int = 0,
    match_date: str | None = None,
    odds: dict | None = None,
    xg_home: float | None = None,
    xg_away: float | None = None,
    home_rank: int | None = None,
    away_rank: int | None = None,
    home_matches_next_7d: int | None = None,
    away_matches_next_7d: int | None = None,
    h2h_override: tuple[float, float, float, float, int] | None = None,
    home_team_stats_prior: dict | None = None,
    away_team_stats_prior: dict | None = None,
    home_coach_tenure_days: int | None = None,
    away_coach_tenure_days: int | None = None,
) -> dict[str, Any]:
    """
    根据双方近期表现预测期望进球与胜/平/负概率。
    主队用主场近期、客队用客场近期；样本不足时回退为主客混合。
    支持联赛主客细分收缩、历史交锋融合、休息天数疲劳、主场优势、多 last_n 融合（5/10/15）、伤停与关键球员细化、可选 xG 混合、可选 ML 修正层、可选积分榜排名微调。
    若传入 odds 且开启赔率融合，则概率为模型与赔率隐含概率的线性融合。
    h2h_override: 可选，体彩/竞彩历史交锋 (主队场均进, 主队场均失, 客队场均进, 客队场均失, 场数)，有则用于历史交锋融合。
    """
    from db import repo
    eng = engine if engine is not None else repo.get_engine()
    overrides = _get_league_overrides(eng, competition_id)
    effective_last_n = int(overrides.get("last_n", last_n))

    use_fusion = _cfg(overrides, "PREDICT_USE_FUSION", False)
    use_fusion = use_fusion if isinstance(use_fusion, bool) else str(use_fusion).lower() in ("1", "true", "yes")
    w5 = float(_cfg(overrides, "PREDICT_FUSION_WEIGHT_5", 0.4))
    w10 = float(_cfg(overrides, "PREDICT_FUSION_WEIGHT_10", 0.6))
    ext_n = int(_cfg(overrides, "PREDICT_EXTENDED_LAST_N", 0))
    w15 = float(_cfg(overrides, "PREDICT_FUSION_WEIGHT_15", 0.2))
    use_15 = ext_n >= 15 and effective_last_n >= 15

    if use_fusion and effective_last_n == 10 and not use_15:
        e5_h, e5_a, _, _ = _compute_exp_goals_one(
            eng, home_team_id, away_team_id, competition_id, 5, use_time_weight, overrides, h2h_override=h2h_override
        )
        e10_h, e10_a, home_n, away_n = _compute_exp_goals_one(
            eng, home_team_id, away_team_id, competition_id, 10, use_time_weight, overrides, h2h_override=h2h_override
        )
        exp_home = w5 * e5_h + w10 * e10_h
        exp_away = w5 * e5_a + w10 * e10_a
    elif use_fusion and use_15:
        e5_h, e5_a, _, _ = _compute_exp_goals_one(
            eng, home_team_id, away_team_id, competition_id, 5, use_time_weight, overrides, h2h_override=h2h_override
        )
        e10_h, e10_a, _, _ = _compute_exp_goals_one(
            eng, home_team_id, away_team_id, competition_id, 10, use_time_weight, overrides, h2h_override=h2h_override
        )
        e15_h, e15_a, home_n, away_n = _compute_exp_goals_one(
            eng, home_team_id, away_team_id, competition_id, 15, use_time_weight, overrides, h2h_override=h2h_override
        )
        total_w = w5 + w10 + w15
        exp_home = (w5 * e5_h + w10 * e10_h + w15 * e15_h) / total_w
        exp_away = (w5 * e5_a + w10 * e10_a + w15 * e15_a) / total_w
    else:
        exp_home, exp_away, home_n, away_n = _compute_exp_goals_one(
            eng, home_team_id, away_team_id, competition_id, effective_last_n, use_time_weight, overrides, h2h_override=h2h_override
        )

    # 球队赛季统计先验（api_football_team_statistics）：样本不足时与赛季场均进球/失球融合
    if (
        home_team_stats_prior and away_team_stats_prior
        and "goals_for_pg" in home_team_stats_prior and "goals_against_pg" in home_team_stats_prior
        and "goals_for_pg" in away_team_stats_prior and "goals_against_pg" in away_team_stats_prior
    ):
        prior_exp_home = (float(home_team_stats_prior["goals_for_pg"]) + float(away_team_stats_prior["goals_against_pg"])) / 2.0
        prior_exp_away = (float(away_team_stats_prior["goals_for_pg"]) + float(home_team_stats_prior["goals_against_pg"])) / 2.0
        min_samples = int(_cfg(overrides, "PREDICT_TEAM_STATS_PRIOR_MIN_SAMPLES", 5))
        weight_prior = float(_cfg(overrides, "PREDICT_TEAM_STATS_PRIOR_WEIGHT", 0.3))
        weight_prior = max(0.0, min(1.0, weight_prior))
        if home_n < min_samples:
            exp_home = (1.0 - weight_prior) * exp_home + weight_prior * prior_exp_home
        if away_n < min_samples:
            exp_away = (1.0 - weight_prior) * exp_away + weight_prior * prior_exp_away

    # 主场优势
    home_adv = float(_cfg(overrides, "PREDICT_HOME_ADVANTAGE", 1.05))
    exp_home *= home_adv

    # 休息天数疲劳 + 赛程密度（未来 7 天内场次多也视为疲劳）
    if match_date:
        thresh = int(_cfg(overrides, "PREDICT_FATIGUE_DAYS_THRESHOLD", 3))
        fact = float(_cfg(overrides, "PREDICT_FATIGUE_FACTOR", 0.95))
        dense_thresh = int(_cfg(overrides, "PREDICT_FATIGUE_MATCHES_NEXT_7D", 2))
        d_home = _days_since_last_match(eng, home_team_id, match_date, competition_id)
        d_away = _days_since_last_match(eng, away_team_id, match_date, competition_id)
        home_fatigue = (d_home is not None and d_home < thresh) or (home_matches_next_7d is not None and home_matches_next_7d >= dense_thresh)
        away_fatigue = (d_away is not None and d_away < thresh) or (away_matches_next_7d is not None and away_matches_next_7d >= dense_thresh)
        if home_fatigue:
            exp_home *= fact
        if away_fatigue:
            exp_away *= fact

        # 连续客场：客队最近 N 场均为客场时略降客队期望
        cons_min = getattr(config, "PREDICT_CONSECUTIVE_AWAY_MIN", 2)
        if cons_min >= 1 and match_date:
            away_cons = _consecutive_away_count(
                away_team_id, (match_date or "")[:10], competition_id, max(5, cons_min)
            )
            if away_cons >= cons_min:
                exp_away *= getattr(config, "PREDICT_CONSECUTIVE_AWAY_FACTOR", 0.98)

    # 伤停下调（普通缺阵 + 关键球员缺阵按 KEY_WEIGHT 折算）
    key_w = float(_cfg(overrides, "PREDICT_INJURY_KEY_PLAYER_WEIGHT", 3.0))
    eff_home = len(home_absent or []) + key_w * max(0, home_absent_key_count)
    eff_away = len(away_absent or []) + key_w * max(0, away_absent_key_count)
    if eff_home > 0 or eff_away > 0:
        per_n = float(_cfg(overrides, "PREDICT_INJURY_DOWN_PER_N", 2.0))
        cap = float(_cfg(overrides, "PREDICT_INJURY_CAP", 0.15))
        down_home = min(cap, eff_home * (0.05 / max(0.1, per_n)))
        down_away = min(cap, eff_away * (0.05 / max(0.1, per_n)))
        exp_home *= 1.0 - down_home
        exp_away *= 1.0 - down_away

    # 教练任期（api_football_coaches）：新帅上任短期内略降该队期望（不确定性）
    coach_tenure_threshold = int(_cfg(overrides, "PREDICT_COACH_NEW_DAYS_THRESHOLD", 60))
    coach_new_factor = float(_cfg(overrides, "PREDICT_COACH_NEW_FACTOR", 0.98))
    if home_coach_tenure_days is not None and home_coach_tenure_days < coach_tenure_threshold:
        exp_home *= coach_new_factor
    if away_coach_tenure_days is not None and away_coach_tenure_days < coach_tenure_threshold:
        exp_away *= coach_new_factor

    # 积分榜排名微调（可选）：主队排名更靠前则略升主队期望、降客队，反之亦然；支持联赛覆盖
    if home_rank is not None and away_rank is not None and _cfg(overrides, "PREDICT_USE_STANDINGS_NUDGE", True):
        nudge = float(_cfg(overrides, "PREDICT_STANDINGS_NUDGE_FACTOR", 0.02))
        if home_rank < away_rank:  # 主队排名更好
            exp_home *= 1.0 + nudge
            exp_away *= 1.0 - nudge
        elif home_rank > away_rank:
            exp_home *= 1.0 - nudge
            exp_away *= 1.0 + nudge

    # 可选 xG 混合：若有近期 xG 且开启，与形式期望进球按比例混合；支持联赛覆盖 PREDICT_XG_BLEND
    if getattr(config, "PREDICT_USE_XG", False) and xg_home is not None and xg_away is not None:
        blend = float(_cfg(overrides, "PREDICT_XG_BLEND", 0.3))
        blend = max(0.0, min(1.0, blend))
        exp_home = (1.0 - blend) * exp_home + blend * float(xg_home)
        exp_away = (1.0 - blend) * exp_away + blend * float(xg_away)

        # 实际进球−xG 微调：近期把握/运气，小幅加减期望（可关闭：PREDICT_ACTUAL_XG_NUDGE_FACTOR=0）
        nudge_f = getattr(config, "PREDICT_ACTUAL_XG_NUDGE_FACTOR", 0.05)
        nudge_cap = getattr(config, "PREDICT_ACTUAL_XG_NUDGE_CAP", 0.15)
        if nudge_f > 0 and eng is not None:
            actual_home, _ = _team_recent_form(eng, home_team_id, competition_id, 5, False, overrides=overrides)
            actual_away, _ = _team_recent_form(eng, away_team_id, competition_id, 5, False, overrides=overrides)
            nh = max(-nudge_cap, min(nudge_cap, nudge_f * (float(actual_home) - float(xg_home))))
            na = max(-nudge_cap, min(nudge_cap, nudge_f * (float(actual_away) - float(xg_away))))
            exp_home += nh
            exp_away += na

    # 边界场次略偏主：势均力敌且主队略优时小幅提高主队期望；支持联赛覆盖
    spread = exp_home - exp_away
    boundary_spread = float(_cfg(overrides, "PREDICT_BOUNDARY_HOME_SPREAD", 0.25))
    boundary_nudge = float(_cfg(overrides, "PREDICT_BOUNDARY_HOME_NUDGE", 0.02))
    is_boundary = 0 < spread <= boundary_spread
    if is_boundary and boundary_nudge > 0:
        exp_home *= 1.0 + boundary_nudge

    p_home_win, p_draw, p_away_win = probs_from_exp_goals(exp_home, exp_away, overrides)
    scores_probs = _score_probs_from_exp_goals(exp_home, exp_away, overrides)
    scores_probs_for_jc = list(scores_probs)
    # 扩展玩法用：保留一份赔率快照（含 zjqs），避免胜平负融合因时效把 odds 置空后丢失总进球盘口
    odds_for_jc_markets = {k: v for k, v in odds.items()} if (odds and isinstance(odds, dict)) else None

    # 赔率融合（可选时效：超过 PREDICT_ODDS_MAX_AGE_HOURS 小时则不参与融合）
    prob_home_win_out = p_home_win
    prob_draw_out = p_draw
    prob_away_win_out = p_away_win
    prob_model_out: dict[str, float] = {}
    odds_updated_at = odds.get("created_at") if isinstance(odds, dict) else None
    if odds and getattr(config, "PREDICT_USE_ODDS_FUSION", True):
        max_age_hours = getattr(config, "PREDICT_ODDS_MAX_AGE_HOURS", None)
        if max_age_hours is not None and odds_updated_at:
            try:
                from datetime import datetime, timezone
                ct = odds_updated_at
                if isinstance(ct, str):
                    dt = datetime.fromisoformat(ct.replace("Z", "+00:00")) if ct.endswith("Z") else datetime.fromisoformat(ct)
                else:
                    dt = ct
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
                if age_hours > float(max_age_hours):
                    odds = None
            except Exception:
                pass
        if odds:
            implied = odds_to_implied_probs(odds)
            if implied is not None:
                weight_model = float(_cfg(overrides, "PREDICT_ODDS_FUSION_WEIGHT_MODEL", getattr(config, "PREDICT_ODDS_FUSION_WEIGHT_MODEL", 0.55)))
                prob_home_win_out, prob_draw_out, prob_away_win_out = fuse_probs(
                    (p_home_win, p_draw, p_away_win), implied, weight_model
                )
                prob_model_out = {
                    "prob_home_win_model": round(p_home_win, 2),
                    "prob_draw_model": round(p_draw, 2),
                    "prob_away_win_model": round(p_away_win, 2),
                }

    # 概率校准（可选）：用历史数据拟合的 Platt 缩放修正胜平负概率；支持联赛专属参数
    if getattr(config, "PREDICT_USE_CALIBRATION", False):
        try:
            from engine.calibration import load_calibration_params, apply_calibration
            calib = load_calibration_params()
            if calib:
                prob_home_win_out, prob_draw_out, prob_away_win_out = apply_calibration(
                    prob_home_win_out, prob_draw_out, prob_away_win_out, calib, competition_id
                )
        except Exception:
            pass

    # 2.2 ML 修正层（可选）：用规则输出作特征，小模型预测胜平负概率并与规则概率混合；支持联赛专属模型与 rank 特征
    if getattr(config, "PREDICT_USE_OUTCOME_ML", False):
        try:
            from engine.outcome_ml import predict_proba as ml_predict_proba
            ml_probs = ml_predict_proba(
                exp_home, exp_away,
                prob_home_win_out, prob_draw_out, prob_away_win_out,
                effective_last_n, competition_id,
                home_rank=home_rank,
                away_rank=away_rank,
            )
            if ml_probs is not None:
                w = getattr(config, "PREDICT_OUTCOME_ML_WEIGHT", 0.3)
                w = max(0.0, min(1.0, w))
                ph = (1.0 - w) * prob_home_win_out + w * ml_probs[0]
                pd = (1.0 - w) * prob_draw_out + w * ml_probs[1]
                pa = (1.0 - w) * prob_away_win_out + w * ml_probs[2]
                s = ph + pd + pa
                if s > 0:
                    prob_home_win_out, prob_draw_out, prob_away_win_out = ph / s, pd / s, pa / s
        except Exception:
            pass

    align_score = _cfg(overrides, "PREDICT_SCORE_ALIGN_TO_1X2", getattr(config, "PREDICT_SCORE_ALIGN_TO_1X2", True))
    align_score = align_score if isinstance(align_score, bool) else str(align_score).lower() in ("1", "true", "yes")
    primary, top_scores = _pick_predicted_scores(
        scores_probs_for_jc,
        prob_home_win_out,
        prob_draw_out,
        prob_away_win_out,
        exp_home,
        exp_away,
        align_to_1x2=align_score,
    )

    # 低置信度：样本不足或期望进球几乎持平（接近瞎猜）时标为 low
    min_samples = int(_cfg(overrides, "PREDICT_LOW_CONFIDENCE_MIN_SAMPLES", 5))
    max_spread = float(_cfg(overrides, "PREDICT_LOW_CONFIDENCE_MAX_SPREAD", 0.25))
    low_conf = (
        home_n < min_samples or away_n < min_samples
        or abs(exp_home - exp_away) < max_spread
    )
    confidence = "low" if low_conf else "high"

    out = {
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "exp_goals_home": round(exp_home, 2),
        "exp_goals_away": round(exp_away, 2),
        "exp_goals_total": round(exp_home + exp_away, 2),  # 预测本场总进球数（主+客期望进球）
        "predicted_score": primary,
        "predicted_scores": top_scores,
        "prob_home_win": round(prob_home_win_out, 2),
        "prob_draw": round(prob_draw_out, 2),
        "prob_away_win": round(prob_away_win_out, 2),
        "based_on_last_n": effective_last_n,
        "competition_id": competition_id,
        "confidence": confidence,
        "is_boundary": is_boundary,
    }
    try:
        from engine.bqc_alpha_fit import resolve_bqc_alpha
        from engine.jc_market_probs import build_jc_play_markets

        _src_rq = odds if (odds and isinstance(odds, dict)) else odds_for_jc_markets
        rq_hc = None
        if _src_rq and _src_rq.get("rqspf_handicap") is not None:
            rq_hc = _src_rq["rqspf_handicap"]
        bqc_alpha = resolve_bqc_alpha(competition_id)
        out["jc_play_markets"] = build_jc_play_markets(
            exp_home,
            exp_away,
            scores_probs_for_jc,
            rq_handicap=rq_hc,
            odds_for_markets=odds_for_jc_markets,
            league_overrides=overrides,
            bqc_alpha=bqc_alpha,
        )
    except Exception:
        out["jc_play_markets"] = None
    if prob_model_out:
        out["odds_used"] = True
        out.update(prob_model_out)
    if odds_updated_at is not None:
        out["odds_updated_at"] = odds_updated_at
    return out


def resolve_team_by_name(engine: Engine | None, name: str) -> int | None:
    """按名称解析球队 ID：仅精确匹配 teams.name。"""
    from db import repo
    if engine is None or repo.use_mongo():
        return repo.get_team_id_by_name(name)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM teams WHERE name = :n"), {"n": (name or "").strip()}).fetchone()
        return row[0] if row else None


def resolve_competition_by_name(engine: Engine | None, name: str) -> int | None:
    """按名称解析赛事 ID：仅精确匹配 competitions.name。"""
    from db import repo
    if engine is None or repo.use_mongo():
        return repo.get_competition_id_by_name(name)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM competitions WHERE name = :n"), {"n": (name or "").strip()}).fetchone()
        return row[0] if row else None


def save_prediction(
    engine: Engine,
    match_id: int,
    result: dict[str, Any],
) -> None:
    """将单场预测结果写入 predictions 表，供事后与完赛结果对比。"""
    from db import repo
    if repo.use_mongo():
        repo.save_prediction(match_id, result)
        return
    from sqlalchemy.orm import Session
    with Session(engine) as session:
        session.add(Prediction(
            match_id=match_id,
            exp_goals_home=float(result["exp_goals_home"]),
            exp_goals_away=float(result["exp_goals_away"]),
            prob_home_win=float(result["prob_home_win"]),
            prob_draw=float(result["prob_draw"]),
            prob_away_win=float(result["prob_away_win"]),
            last_n=int(result["based_on_last_n"]),
            competition_id=result.get("competition_id"),
        ))
        session.commit()


# 赛事名 -> 联赛中文（与 api_football_odds/standings 一致，供按 DB 场次加载赔率/伤停/积分）
_COMPETITION_TO_LEAGUE_CN: dict[str, str] = {
    "Premier League": "英超",
    "La Liga": "西甲",
    "Primera Division": "西甲",
    "Primera División": "西甲",
    "Serie A": "意甲",
    "Bundesliga": "德甲",
    "1. Bundesliga": "德甲",
    "Ligue 1": "法甲",
}


def _load_odds_injury_rank_for_match(
    engine: Engine,
    home_team_id: int,
    away_team_id: int,
    competition_id: int | None,
    match_date: str | None,
    match_id: int | None = None,
) -> dict[str, Any]:
    """
    按场加载赔率、伤停、积分榜排名，供 backfill/repredict 时传入 predict_match。
    返回 {"odds": dict|None, "home_absent": list, "away_absent": list, "home_rank": int|None, "away_rank": int|None}。
    """
    out: dict[str, Any] = {
        "odds": None,
        "home_absent": None,
        "away_absent": None,
        "home_rank": None,
        "away_rank": None,
        "xg_home": None,
        "xg_away": None,
        "home_matches_next_7d": None,
        "away_matches_next_7d": None,
    }
    try:
        from db import repo
        if repo.use_mongo():
            ht = repo.get_team_by_id(home_team_id)
            at = repo.get_team_by_id(away_team_id)
            if not ht or not at:
                return out
            home_name, away_name = (ht.get("name") or "").strip(), (at.get("name") or "").strip()
            comp_name = (repo.get_competition_name(competition_id) or "").strip()
            league_cn = _COMPETITION_TO_LEAGUE_CN.get(comp_name) or _COMPETITION_TO_LEAGUE_CN.get(comp_name.replace("í", "i"))
            if not league_cn:
                return out
            # 赔率：优先从 match_odds 按 match_id 读取（不请求 API）；无 match_id 或无记录则无赔率
            if match_id:
                odds = repo.match_odds_load_by_match_id(match_id)
                if odds and (odds.get("spf_win") or odds.get("spf_draw") or odds.get("spf_loss")):
                    out["odds"] = odds
            try:
                from etl.injury_db import load_injury_from_db
                def _norm(s: str) -> str:
                    s = (s or "").strip().lower().replace(" ", "")
                    for suffix in ("fc", "cf", "sc", "afc", "cfc", "utd", "united", "city", "hotspur", "spurs"):
                        if len(s) > len(suffix) and s.endswith(suffix):
                            s = s[: -len(suffix)]
                            break
                    return s
                injury_map = load_injury_from_db()
                key = (_norm(home_name), _norm(away_name), league_cn)
                inj = injury_map.get(key)
                if inj:
                    out["home_absent"] = inj.get("home_absent") or []
                    out["away_absent"] = inj.get("away_absent") or []
            except Exception:
                pass
            if match_id:
                out["home_rank"], out["away_rank"] = repo.standings_get_for_match(match_id)
            # 竞彩场次 match_id 多为体彩 ID，主库可能无此 match，排名回退：按联赛+赛季从 league_standings 取
            if (out["home_rank"] is None and out["away_rank"] is None) and match_date and len((match_date or "")[:10]) == 10 and league_cn and home_name and away_name:
                try:
                    from etl.api_football_standings import LEAGUE_CN_TO_ID, load_standings, get_team_rank
                    y, mon = int((match_date or "2025-01-01")[:4]), int((match_date or "2025-06-01")[5:7] or 0)
                    season = y if mon >= 8 else y - 1
                    lid = LEAGUE_CN_TO_ID.get(league_cn)
                    if season and lid:
                        by_league = load_standings(season)
                        st_list = (by_league or {}).get(lid) or []
                        if st_list:
                            out["home_rank"] = get_team_rank(st_list, home_name)
                            out["away_rank"] = get_team_rank(st_list, away_name)
                except Exception:
                    pass
            if getattr(config, "PREDICT_USE_XG", False) and match_date:
                try:
                    from etl.api_football_statistics import team_recent_xg_proxy_from_matches
                    xg_last_n = 5
                    home_matches = repo.predict_team_recent_matches_with_source(
                        home_team_id, (match_date or "")[:10], competition_id, xg_last_n
                    )
                    away_matches = repo.predict_team_recent_matches_with_source(
                        away_team_id, (match_date or "")[:10], competition_id, xg_last_n
                    )
                    out["xg_home"] = team_recent_xg_proxy_from_matches(home_matches, home_team_id)
                    out["xg_away"] = team_recent_xg_proxy_from_matches(away_matches, away_team_id)
                except Exception:
                    pass
            if match_date and len((match_date or "")[:10]) == 10:
                try:
                    from datetime import datetime, timedelta
                    d = datetime.strptime((match_date or "")[:10], "%Y-%m-%d")
                    d_from = (d + timedelta(days=1)).strftime("%Y-%m-%d")
                    d_to = (d + timedelta(days=7)).strftime("%Y-%m-%d")
                    out["home_matches_next_7d"] = repo.predict_count_team_matches_in_range(
                        home_team_id, d_from, d_to, competition_id
                    )
                    out["away_matches_next_7d"] = repo.predict_count_team_matches_in_range(
                        away_team_id, d_from, d_to, competition_id
                    )
                except Exception:
                    pass
            return out
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT ht.name, at.name, c.name
                    FROM teams ht, teams at
                    LEFT JOIN competitions c ON c.id = :cid
                    WHERE ht.id = :hid AND at.id = :aid
                """),
                {"hid": home_team_id, "aid": away_team_id, "cid": competition_id or 0},
            ).fetchone()
        if not row or not row[0] or not row[1]:
            return out
        home_name, away_name, comp_name = (row[0] or "").strip(), (row[1] or "").strip(), (row[2] or "").strip()
        league_cn = _COMPETITION_TO_LEAGUE_CN.get(comp_name) or _COMPETITION_TO_LEAGUE_CN.get(comp_name.replace("í", "i"))
        if not league_cn:
            return out
        date_prefix = (match_date or "")[:10] if match_date else None
        # 赔率
        try:
            from etl.api_football_odds import load_odds_for_match
            odds = load_odds_for_match(home_name, away_name, league_cn, date_prefix)
            if odds:
                out["odds"] = odds
        except Exception:
            pass
        # 伤停（与 injury_db 的 key 一致：norm 用简单小写去空格）
        try:
            from etl.injury_db import load_injury_from_db
            def _norm(s: str) -> str:
                s = (s or "").strip().lower().replace(" ", "")
                for suffix in ("fc", "cf", "sc", "afc", "cfc", "utd", "united", "city", "hotspur", "spurs"):
                    if len(s) > len(suffix) and s.endswith(suffix):
                        s = s[: -len(suffix)]
                        break
                return s
            injury_map = load_injury_from_db()
            key = (_norm(home_name), _norm(away_name), league_cn)
            inj = injury_map.get(key)
            if inj:
                out["home_absent"] = inj.get("home_absent") or []
                out["away_absent"] = inj.get("away_absent") or []
        except Exception:
            pass
        # 积分榜排名
        if date_prefix:
            try:
                from etl.api_football_standings import LEAGUE_CN_TO_ID, load_standings, get_team_rank
                y, m = int(date_prefix[:4] or 0), int(date_prefix[5:7] or 1)
                season = y if m >= 8 else y - 1
                if season and league_cn and LEAGUE_CN_TO_ID.get(league_cn):
                    by_league = load_standings(season)
                    st_list = by_league.get(LEAGUE_CN_TO_ID[league_cn]) or []
                    if st_list:
                        out["home_rank"] = get_team_rank(st_list, home_name)
                        out["away_rank"] = get_team_rank(st_list, away_name)
            except Exception:
                pass
        if getattr(config, "PREDICT_USE_XG", False) and date_prefix:
            try:
                from etl.api_football_statistics import team_recent_xg_proxy_from_matches
                xg_last_n = 5
                home_matches = repo.predict_team_recent_matches_with_source(
                    home_team_id, date_prefix, competition_id, xg_last_n
                )
                away_matches = repo.predict_team_recent_matches_with_source(
                    away_team_id, date_prefix, competition_id, xg_last_n
                )
                out["xg_home"] = team_recent_xg_proxy_from_matches(home_matches, home_team_id)
                out["xg_away"] = team_recent_xg_proxy_from_matches(away_matches, away_team_id)
            except Exception:
                pass
        if date_prefix and len(date_prefix) == 10:
            try:
                from datetime import datetime, timedelta
                d = datetime.strptime(date_prefix, "%Y-%m-%d")
                d_from = (d + timedelta(days=1)).strftime("%Y-%m-%d")
                d_to = (d + timedelta(days=7)).strftime("%Y-%m-%d")
                out["home_matches_next_7d"] = repo.predict_count_team_matches_in_range(
                    home_team_id, d_from, d_to, competition_id
                )
                out["away_matches_next_7d"] = repo.predict_count_team_matches_in_range(
                    away_team_id, d_from, d_to, competition_id
                )
            except Exception:
                pass
    except Exception:
        pass
    return out


def backfill_predictions_for_finished_matches(
    engine: Engine | None,
    years: list[int] | None = None,
    limit: int | None = None,
) -> int:
    """
    对「已完赛但尚未有预测」的场次，用当前模型逐场预测并落库。
    用于用历史数据（如 2025 年）做回测：先本函数回填预测，再 predict-eval 看命中率，再调参/repredict 迭代提高。
    years: 只处理这些年份的比赛，如 [2025] 或 [2024, 2025]；None 表示全部。
    limit: 最多处理场次数，None 表示不限制。
    返回新写入的预测场次数。
    """
    from db import repo
    if repo.use_mongo():
        match_ids = repo.predict_get_match_ids_without_predictions(years=years, limit=limit)
    else:
        year_filter = ""
        if years:
            placeholders = ",".join(repr(str(y)) for y in years)
            year_filter = f" AND strftime('%Y', m.match_date) IN ({placeholders})"
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT m.id
                FROM matches m
                WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                  AND m.home_team_id IS NOT NULL AND m.away_team_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM predictions p WHERE p.match_id = m.id)
                {year_filter}
                ORDER BY m.match_date
            """ + (f" LIMIT {int(limit)}" if limit else ""))).fetchall()
        match_ids = [r[0] for r in rows]
    if not match_ids:
        return 0
    n = 0
    for match_id in match_ids:
        if repo.use_mongo():
            info = repo.predict_get_match_info(match_id)
            if not info or info.get("home_team_id") is None or info.get("away_team_id") is None:
                continue
            home_id = info["home_team_id"]
            away_id = info["away_team_id"]
            comp_id = info.get("competition_id")
            match_date = (info.get("match_date") or "")[:10] or None
        else:
            with engine.connect() as conn:
                row = conn.execute(
                    text("""
                        SELECT home_team_id, away_team_id, competition_id, match_date
                        FROM matches WHERE id = :mid
                    """),
                    {"mid": match_id},
                ).fetchone()
            if not row or row[0] is None or row[1] is None:
                continue
            home_id, away_id, comp_id, match_date = row[0], row[1], row[2], (row[3] or "")[:10] or None
        eng = repo.get_engine() if repo.use_mongo() else engine
        extra = _load_odds_injury_rank_for_match(eng, home_id, away_id, comp_id, match_date, match_id=match_id)
        result = predict_match(
            eng,
            home_id,
            away_id,
            competition_id=comp_id,
            match_date=match_date,
            odds=extra.get("odds"),
            home_absent=extra.get("home_absent"),
            away_absent=extra.get("away_absent"),
            home_rank=extra.get("home_rank"),
            away_rank=extra.get("away_rank"),
            xg_home=extra.get("xg_home"),
            xg_away=extra.get("xg_away"),
            home_matches_next_7d=extra.get("home_matches_next_7d"),
            away_matches_next_7d=extra.get("away_matches_next_7d"),
        )
        result["competition_id"] = result.get("competition_id") or comp_id
        save_prediction(eng, match_id, result)
        n += 1
    return n


def _score_to_result(score: list[int]) -> str:
    """从 [主队进球, 客队进球] 得到 主/平/客。"""
    if not score or len(score) < 2:
        return "平"
    h, a = int(score[0]), int(score[1])
    return "主" if h > a else ("客" if a > h else "平")


def run_predict_for_mongo_matches(
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int | None = None,
    force: bool = False,
) -> tuple[int, int, int]:
    """
    对 MongoDB matches 表中的比赛做模型预测并落库（predictions + prediction_log），供 Web 展示。
    仅 USE_MONGO 时有效。返回 (新增预测数, 已存在跳过数, 总处理场数)。
    """
    from db import repo
    if not repo.use_mongo():
        return 0, 0, 0
    matches = repo.predict_get_mongo_matches_for_predict(
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        skip_with_predictions=not force,
    )
    if not matches:
        return 0, 0, 0
    eng = repo.get_engine()
    added = 0
    skipped = 0
    for m in matches:
        match_id = m["id"]
        home_id = m["home_team_id"]
        away_id = m["away_team_id"]
        comp_id = m.get("competition_id")
        match_date = m.get("match_date") or ""
        if not force and repo.predictions_exists_for_match(match_id):
            skipped += 1
            continue
        extra = _load_odds_injury_rank_for_match(eng, home_id, away_id, comp_id, match_date[:10] if match_date else None, match_id=match_id)
        result = predict_match(
            eng,
            home_id,
            away_id,
            competition_id=comp_id,
            match_date=match_date[:10] if match_date else None,
            odds=extra.get("odds"),
            home_absent=extra.get("home_absent"),
            away_absent=extra.get("away_absent"),
            home_rank=extra.get("home_rank"),
            away_rank=extra.get("away_rank"),
            xg_home=extra.get("xg_home"),
            xg_away=extra.get("xg_away"),
            home_matches_next_7d=extra.get("home_matches_next_7d"),
            away_matches_next_7d=extra.get("away_matches_next_7d"),
        )
        result["competition_id"] = result.get("competition_id") or comp_id
        save_prediction(eng, match_id, result)
        # 写入 prediction_log 以便 Web /predictions 接口能合并展示
        pred_score = result.get("predicted_score") or [0, 0]
        pred_result = _score_to_result(pred_score)
        pred_scores = result.get("predicted_scores") or [pred_score]
        doc = {
            "match_id": match_id,
            "log_date": (match_date or "")[:10],
            "home_team": (m.get("home_name") or "").strip() or "?",
            "away_team": (m.get("away_name") or "").strip() or "?",
            "competition": (m.get("competition_name") or "").strip(),
            "exp_goals_home": round(float(result.get("exp_goals_home", 0)), 2),
            "exp_goals_away": round(float(result.get("exp_goals_away", 0)), 2),
            "prob_home_win": round(float(result.get("prob_home_win", 0)), 3),
            "prob_draw": round(float(result.get("prob_draw", 0)), 3),
            "prob_away_win": round(float(result.get("prob_away_win", 0)), 3),
            "predicted_result": pred_result,
            "last_n": int(result.get("based_on_last_n", 10)),
            "actual_home_score": None,
            "actual_away_score": None,
            "actual_result": None,
            "confidence": result.get("confidence", "high"),
            "odds_used": bool(result.get("odds_used", False)),
            "predicted_scores_json": __import__("json").dumps([list(s)[:2] for s in pred_scores[:2]], ensure_ascii=False),
        }
        repo.prediction_log_add(doc)
        added += 1
    return added, skipped, len(matches)


def repredict_evaluable_matches(engine: Engine | None) -> int:
    """
    对「已有预测且已完赛」的历史场次，用当前 config 重算预测并写回 DB（先删旧预测再写入新预测）。
    返回重算并写回的场次数。
    """
    from db import repo
    from sqlalchemy.orm import Session
    if repo.use_mongo():
        match_ids = repo.predict_get_match_ids_with_predictions()
    else:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT DISTINCT p.match_id
                FROM predictions p
                JOIN matches m ON m.id = p.match_id
                WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                  AND m.home_team_id IS NOT NULL AND m.away_team_id IS NOT NULL
            """)).fetchall()
        match_ids = [r[0] for r in rows]
    if not match_ids:
        return 0
    total = len(match_ids)
    print(f"  共 {total} 场，开始重算...", flush=True)
    n = 0
    eng = repo.get_engine() if repo.use_mongo() else engine
    for match_id in match_ids:
        if repo.use_mongo():
            info = repo.predict_get_match_info(match_id)
            if not info or info.get("home_team_id") is None or info.get("away_team_id") is None:
                continue
            home_id = info["home_team_id"]
            away_id = info["away_team_id"]
            comp_id = info.get("competition_id")
            match_date = (info.get("match_date") or "")[:10] or None
        else:
            with engine.connect() as conn:
                row = conn.execute(
                    text("""
                        SELECT home_team_id, away_team_id, competition_id, match_date
                        FROM matches WHERE id = :mid
                    """),
                    {"mid": match_id},
                ).fetchone()
            if not row or row[0] is None or row[1] is None:
                continue
            home_id, away_id, comp_id, match_date = row[0], row[1], row[2], (row[3] or "")[:10] or None
        if repo.use_mongo():
            repo.delete_predictions_by_match_id(match_id)
        else:
            with Session(engine) as session:
                session.execute(text("DELETE FROM predictions WHERE match_id = :mid"), {"mid": match_id})
                session.commit()
        extra = _load_odds_injury_rank_for_match(eng, home_id, away_id, comp_id, match_date, match_id=match_id)
        result = predict_match(
            eng,
            home_id,
            away_id,
            competition_id=comp_id,
            match_date=match_date,
            odds=extra.get("odds"),
            home_absent=extra.get("home_absent"),
            away_absent=extra.get("away_absent"),
            home_rank=extra.get("home_rank"),
            away_rank=extra.get("away_rank"),
            xg_home=extra.get("xg_home"),
            xg_away=extra.get("xg_away"),
            home_matches_next_7d=extra.get("home_matches_next_7d"),
            away_matches_next_7d=extra.get("away_matches_next_7d"),
        )
        result["competition_id"] = result.get("competition_id") or comp_id
        # 竞彩让球：写入预测时带上让球数，供 save_prediction 存 predicted_rqspf
        odds = extra.get("odds")
        if odds:
            hc = odds.get("rqspf_handicap")
            if hc is None and odds.get("ah_handicap") is not None:
                try:
                    hc = float(str(odds["ah_handicap"]).strip())
                except (TypeError, ValueError):
                    pass
            if hc is not None:
                result["rqspf_handicap"] = int(hc) if isinstance(hc, (int, float)) and float(hc) == int(float(hc)) else float(hc)
        save_prediction(eng, match_id, result)
        n += 1
        if n % 50 == 0 or n == total:
            print(f"  已重算 DB 预测 {n}/{total} 场", flush=True)
    return n


def _eval_one_row(
    exp_h: float, exp_a: float, prob_h: float, prob_d: float, prob_a: float,
    actual_h: int, actual_a: int,
) -> tuple[float, float, int, float, int]:
    """单条预测的误差与是否猜对。返回 (err_home, err_away, outcome_ok 0/1, brier_term, exact_score_ok 0/1)。"""
    err_h = abs(exp_h - actual_h)
    err_a = abs(exp_a - actual_a)
    if actual_h > actual_a:
        actual_outcome = "home"
        prob_correct = prob_h
    elif actual_h < actual_a:
        actual_outcome = "away"
        prob_correct = prob_a
    else:
        actual_outcome = "draw"
        prob_correct = prob_d
    outcome_ok = 1 if (prob_h >= prob_d and prob_h >= prob_a and actual_outcome == "home") or \
                      (prob_a >= prob_d and prob_a >= prob_h and actual_outcome == "away") or \
                      (prob_d >= prob_h and prob_d >= prob_a and actual_outcome == "draw") else 0
    brier_term = (1.0 - prob_correct) ** 2
    pred_h, pred_a = round(exp_h), round(exp_a)
    exact_score_ok = 1 if (pred_h == actual_h and pred_a == actual_a) else 0
    return (err_h, err_a, outcome_ok, brier_term, exact_score_ok)


def evaluate_predictions(
    engine: Engine,
    today_only: bool = False,
    by_last_n: bool = False,
    by_competition: bool = False,
    year: int | None = None,
    by_year: bool = False,
) -> dict[str, Any]:
    """
    对已落库且对应比赛已完赛的预测做事后分析。
    返回期望进球 MAE（主/客）、胜平负分类准确率、样本数等。
    by_last_n: 按 p.last_n 分组输出各组准确率；by_competition: 按赛事分组输出。
    year: 只统计该年份的比赛（按 match_date），如 2025。
    使用 MongoDB 时从 repo 取 predictions+matches，与 SQL 路径输出一致。
    """
    need_breakdown = by_last_n or by_competition
    need_match_date = bool(by_year)
    rows: list = []

    try:
        from db import repo
        if repo.use_mongo():
            rows = repo.predict_get_eval_rows(
                today_only=today_only,
                year=year,
                need_breakdown=need_breakdown,
                by_competition=by_competition,
                by_last_n=by_last_n,
                include_match_date=need_match_date,
            )
        else:
            pass  # 继续走下方 SQL
    except Exception:
        pass

    if not rows and engine is not None:
        select_extra = ", p.last_n, p.competition_id, c.name AS competition_name" if need_breakdown else ""
        join_extra = " LEFT JOIN competitions c ON c.id = p.competition_id" if need_breakdown else ""
        year_clause = " AND m.match_date >= :year_start AND m.match_date < :year_end" if year else ""
        match_date_select = ", m.match_date AS match_date" if need_match_date else ""

        with engine.connect() as conn:
            sql = text("""
                SELECT p.id, p.match_id, p.exp_goals_home, p.exp_goals_away,
                       p.prob_home_win, p.prob_draw, p.prob_away_win, p.created_at,
                       m.home_score, m.away_score
                       """ + select_extra + match_date_select + """
                FROM predictions p
                JOIN matches m ON m.id = p.match_id
                """ + join_extra + """
                WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                  AND (:today = 0 OR datetime(p.created_at) >= datetime('now', '-1 day'))
                  """ + year_clause + """
                ORDER BY p.created_at DESC
            """)
            params: dict = {"today": 1 if today_only else 0}
            if year:
                params["year_start"] = f"{year}-01-01"
                params["year_end"] = f"{year + 1}-01-01"
            rows = conn.execute(sql, params).fetchall()

    if not rows:
        out = {
            "n": 0,
            "mae_goals_home": None,
            "mae_goals_away": None,
            "mae_goals_overall": None,
            "outcome_correct": 0,
            "outcome_accuracy": None,
            "brier_outcome": None,
            "exact_score_correct": 0,
            "exact_score_accuracy": None,
        }
        if by_last_n:
            out["by_last_n"] = {}
        if by_competition:
            out["by_competition"] = {}
        if by_year:
            out["by_year"] = {}
        return out

    # 列索引：0 id, 1 match_id, 2 exp_h, 3 exp_a, 4 prob_h, 5 prob_d, 6 prob_a, 7 created_at, 8 act_h, 9 act_a；
    # 若 need_breakdown 则追加 10=last_n, 11=cid, 12=cname；若 by_year 则最后追加 match_date（放在末尾，索引为 -1）
    idx_last_n = 10 if need_breakdown and by_last_n else None
    idx_cid = 11 if need_breakdown and by_competition else None
    idx_cname = 12 if need_breakdown and by_competition else None

    errors_home = []
    errors_away = []
    outcome_ok = 0
    brier_sum = 0.0
    exact_score_ok = 0
    groups_ln: dict[int, list[tuple]] = {}  # last_n -> list of (err_h, err_a, ok, brier, exact_ok)
    groups_comp: dict[str, list[tuple]] = {}
    groups_year: dict[int, dict[str, float]] = {}  # year -> sums
    # 混淆矩阵与平局统计：pred/actual 取 H / D / A
    confusion: dict[str, dict[str, int]] = {"H": {"H": 0, "D": 0, "A": 0}, "D": {"H": 0, "D": 0, "A": 0}, "A": {"H": 0, "D": 0, "A": 0}}
    n_actual_draw = 0
    n_pred_draw = 0
    n_correct_draw = 0

    for r in rows:
        exp_h, exp_a = r[2], r[3]
        prob_h, prob_d, prob_a = r[4], r[5], r[6]
        actual_h, actual_a = int(r[8]), int(r[9])
        err_h, err_a, ok, brier, ex_ok = _eval_one_row(exp_h, exp_a, prob_h, prob_d, prob_a, actual_h, actual_a)
        errors_home.append(err_h)
        errors_away.append(err_a)
        outcome_ok += ok
        brier_sum += brier
        exact_score_ok += ex_ok

        pred = "H" if prob_h >= prob_d and prob_h >= prob_a else ("D" if prob_d >= prob_h and prob_d >= prob_a else "A")
        act = "H" if actual_h > actual_a else ("D" if actual_h == actual_a else "A")
        confusion[pred][act] += 1
        if act == "D":
            n_actual_draw += 1
            if pred == "D":
                n_correct_draw += 1
        if pred == "D":
            n_pred_draw += 1

        if need_breakdown:
            if by_last_n and len(r) > idx_last_n:
                ln = int(r[idx_last_n]) if r[idx_last_n] is not None else 0
                groups_ln.setdefault(ln, []).append((err_h, err_a, ok, brier, ex_ok))
            if by_competition and len(r) > idx_cname:
                cid = r[idx_cid]
                cname = (r[idx_cname] or "").strip()
                key = f"{cid}:{cname}" if cname else str(cid or "")
                groups_comp.setdefault(key, []).append((err_h, err_a, ok, brier, ex_ok))
        if need_match_date:
            md = (r[-1] or "")[:10] if r and r[-1] is not None else ""
            y = int(md[:4]) if len(md) >= 4 and str(md[:4]).isdigit() else None
            if y is not None:
                g = groups_year.get(y)
                if g is None:
                    g = {"n": 0.0, "mae_h_sum": 0.0, "mae_a_sum": 0.0, "outcome_correct": 0.0, "brier_sum": 0.0, "exact_ok": 0.0}
                    groups_year[y] = g
                g["n"] += 1.0
                g["mae_h_sum"] += float(err_h)
                g["mae_a_sum"] += float(err_a)
                g["outcome_correct"] += float(ok)
                g["brier_sum"] += float(brier)
                g["exact_ok"] += float(ex_ok)

    n = len(rows)
    mae_h = sum(errors_home) / n
    mae_a = sum(errors_away) / n
    mae_overall = (sum(errors_home) + sum(errors_away)) / (2 * n)
    draw_recall = round(n_correct_draw / n_actual_draw, 3) if n_actual_draw else None
    draw_precision = round(n_correct_draw / n_pred_draw, 3) if n_pred_draw else None
    out = {
        "n": n,
        "mae_goals_home": round(mae_h, 3),
        "mae_goals_away": round(mae_a, 3),
        "mae_goals_overall": round(mae_overall, 3),
        "outcome_correct": outcome_ok,
        "outcome_accuracy": round(outcome_ok / n, 3) if n else None,
        "brier_outcome": round(brier_sum / n, 4) if n else None,
        "exact_score_correct": exact_score_ok,
        "exact_score_accuracy": round(exact_score_ok / n, 3) if n else None,
        "confusion": confusion,
        "draw_stats": {
            "n_actual_draw": n_actual_draw,
            "n_pred_draw": n_pred_draw,
            "n_correct_draw": n_correct_draw,
            "draw_recall": draw_recall,
            "draw_precision": draw_precision,
        },
    }
    if by_last_n:
        out["by_last_n"] = {}
        for ln, lst in sorted(groups_ln.items()):
            nn = len(lst)
            out["by_last_n"][ln] = {
                "n": nn,
                "mae_goals_overall": round((sum(x[0] for x in lst) + sum(x[1] for x in lst)) / (2 * nn), 3),
                "outcome_correct": sum(x[2] for x in lst),
                "outcome_accuracy": round(sum(x[2] for x in lst) / nn, 3) if nn else None,
                "brier_outcome": round(sum(x[3] for x in lst) / nn, 4) if nn else None,
                "exact_score_correct": sum(x[4] for x in lst),
                "exact_score_accuracy": round(sum(x[4] for x in lst) / nn, 3) if nn else None,
            }
    if by_competition:
        out["by_competition"] = {}
        for key, lst in sorted(groups_comp.items()):
            nn = len(lst)
            out["by_competition"][key] = {
                "n": nn,
                "mae_goals_overall": round((sum(x[0] for x in lst) + sum(x[1] for x in lst)) / (2 * nn), 3),
                "outcome_correct": sum(x[2] for x in lst),
                "outcome_accuracy": round(sum(x[2] for x in lst) / nn, 3) if nn else None,
                "brier_outcome": round(sum(x[3] for x in lst) / nn, 4) if nn else None,
                "exact_score_correct": sum(x[4] for x in lst),
                "exact_score_accuracy": round(sum(x[4] for x in lst) / nn, 3) if nn else None,
            }
    if by_year:
        out["by_year"] = {}
        for y in sorted(groups_year.keys()):
            g = groups_year[y]
            nn = int(g["n"])
            if nn <= 0:
                continue
            out["by_year"][y] = {
                "n": nn,
                "mae_goals_overall": round((g["mae_h_sum"] + g["mae_a_sum"]) / (2.0 * nn), 3),
                "outcome_correct": int(g["outcome_correct"]),
                "outcome_accuracy": round(g["outcome_correct"] / nn, 3),
                "brier_outcome": round(g["brier_sum"] / nn, 4),
                "exact_score_correct": int(g["exact_ok"]),
                "exact_score_accuracy": round(g["exact_ok"] / nn, 3),
            }
    return out


def generate_recap_from_db(
    engine: Engine,
    by_competition: bool = False,
    limit: int | None = None,
    year: int | None = None,
) -> dict[str, Any]:
    """
    从 DB 的 predictions + matches（与 predict-eval 同源）生成复盘报告，结构与 prediction_log.generate_recap 一致。
    使复盘数据与 DB 全量一致；limit 用于只输出前 N 条详情（避免刷屏），None 表示全部。
    year: 只统计该年份的比赛（按 match_date），如 2025。
    Mongo 时从 repo.predict_get_recap_rows 取数，与 predict-eval 同源（predictions+matches）。
    """
    rows: list = []
    try:
        from db import repo
        if repo.use_mongo():
            rows = repo.predict_get_recap_rows(year=year, by_competition=by_competition)
    except Exception:
        pass

    if not rows and engine is not None:
        select_extra = ", c.name AS competition_name" if by_competition else ""
        join_extra = " LEFT JOIN competitions c ON c.id = p.competition_id" if by_competition else ""
        year_clause = " AND m.match_date >= :year_start AND m.match_date < :year_end" if year else ""
        with engine.connect() as conn:
            sql = text("""
                SELECT p.id, p.match_id, p.exp_goals_home, p.exp_goals_away,
                       p.prob_home_win, p.prob_draw, p.prob_away_win,
                       m.home_score, m.away_score,
                       ht.name AS home_team, at.name AS away_team
                       """ + select_extra + """
                FROM predictions p
                JOIN matches m ON m.id = p.match_id
                LEFT JOIN teams ht ON ht.id = m.home_team_id
                LEFT JOIN teams at ON at.id = m.away_team_id
                """ + join_extra + """
                WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                """ + year_clause + """
                ORDER BY p.created_at DESC
            """)
            params: dict = {}
            if year:
                params["year_start"] = f"{year}-01-01"
                params["year_end"] = f"{year + 1}-01-01"
            rows = conn.execute(sql, params).fetchall()

    if not rows:
        out_empty: dict[str, Any] = {
            "n": 0,
            "mae_home": 0.0,
            "mae_away": 0.0,
            "mae_overall": 0.0,
            "outcome_correct": 0,
            "outcome_accuracy": 0.0,
            "exact_score_correct": 0,
            "exact_score_accuracy": 0.0,
            "details": [],
        }
        if by_competition:
            out_empty["by_competition"] = {}
        return out_empty

    # 列索引：0 id, 1 match_id, 2 exp_h, 3 exp_a, 4-6 probs, 7-8 scores, 9 home_team, 10 away_team, 11 competition_name
    idx_cname = 11 if by_competition else None
    details: list[dict[str, Any]] = []
    mae_h_sum = 0.0
    mae_a_sum = 0.0
    outcome_ok = 0
    exact_ok = 0
    by_comp: dict[str, dict[str, Any]] = {}

    for r in rows:
        exp_h, exp_a = float(r[2]), float(r[3])
        prob_h, prob_d, prob_a = float(r[4]), float(r[5]), float(r[6])
        actual_h, actual_a = int(r[7]), int(r[8])
        home_team = (r[9] or "").strip() or "?"
        away_team = (r[10] or "").strip() or "?"
        comp_name = (r[idx_cname] or "").strip() if idx_cname is not None and len(r) > idx_cname else ""

        pred = "主" if prob_h >= prob_d and prob_h >= prob_a else ("客" if prob_a >= prob_d and prob_a >= prob_h else "平")
        act = "主" if actual_h > actual_a else ("客" if actual_h < actual_a else "平")
        ok = 1 if pred == act else 0
        err_h = abs(exp_h - actual_h)
        err_a = abs(exp_a - actual_a)
        mae_h_sum += err_h
        mae_a_sum += err_a
        outcome_ok += ok
        ex_ok = 1 if round(exp_h) == actual_h and round(exp_a) == actual_a else 0
        exact_ok += ex_ok

        reason = ""
        if not ok:
            if act == "主" and pred in ("平", "客"):
                reason = "主队实际表现强于预期"
            elif act == "客" and pred in ("平", "主"):
                reason = "客队实际表现强于预期"
            elif act == "平" and pred in ("主", "客"):
                reason = "双方实际更接近，或一方未能扩大优势"
            else:
                reason = "赛果与概率预测方向相反"

        if limit is None or len(details) < limit:
            details.append({
                "home_team": home_team,
                "away_team": away_team,
                "predicted": f"{exp_h:.2f}-{exp_a:.2f} (预测结果: {pred})",
                "actual": f"{actual_h}-{actual_a} (实际: {act})",
                "outcome_correct": bool(ok),
                "error_home": round(exp_h - actual_h, 2),
                "error_away": round(exp_a - actual_a, 2),
                "reason": reason,
            })

        if by_competition and comp_name:
            if comp_name not in by_comp:
                by_comp[comp_name] = {"n": 0, "mae_h_sum": 0.0, "mae_a_sum": 0.0, "outcome_correct": 0, "exact_score_correct": 0}
            g = by_comp[comp_name]
            g["n"] += 1
            g["mae_h_sum"] += err_h
            g["mae_a_sum"] += err_a
            g["outcome_correct"] += ok
            g["exact_score_correct"] += ex_ok

    n = len(rows)
    mae_h = mae_h_sum / n
    mae_a = mae_a_sum / n
    mae_overall = (mae_h + mae_a) / 2.0
    out: dict[str, Any] = {
        "n": n,
        "mae_home": round(mae_h, 3),
        "mae_away": round(mae_a, 3),
        "mae_overall": round(mae_overall, 3),
        "outcome_correct": outcome_ok,
        "outcome_accuracy": round(outcome_ok / n, 3) if n else 0.0,
        "exact_score_correct": exact_ok,
        "exact_score_accuracy": round(exact_ok / n, 3) if n else 0.0,
        "details": details,
        "details_total": n,
        "details_shown": len(details),
    }
    if by_competition:
        out["by_competition"] = {}
        for comp_name, g in sorted(by_comp.items()):
            nn = g["n"]
            out["by_competition"][comp_name] = {
                "n": nn,
                "mae_home": round(g["mae_h_sum"] / nn, 3),
                "mae_away": round(g["mae_a_sum"] / nn, 3),
                "mae_overall": round((g["mae_h_sum"] + g["mae_a_sum"]) / (2 * nn), 3),
                "outcome_correct": g["outcome_correct"],
                "outcome_accuracy": round(g["outcome_correct"] / nn, 3) if nn else 0.0,
                "exact_score_correct": g["exact_score_correct"],
                "exact_score_accuracy": round(g["exact_score_correct"] / nn, 3) if nn else 0.0,
            }
    return out
