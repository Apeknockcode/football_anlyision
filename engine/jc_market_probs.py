"""
竞彩扩展玩法：在全场 Dixon–Coles 比分网格与期望进球基础上，生成总进球、比分桶、让球三项、半全场概率。

- zjqs：默认 blend —— Poisson(λ主+λ客) 的 0–6 / 7+ 与 DC 网格混合；若有体彩 zjqs 赔率再做隐含概率融合。
- bf：与 `_score_probs_from_exp_goals` 同一网格；可选 crs（比分）赔率融合。
- rqspf：默认由 **DC 网格净胜球分布** 与让球数得到让胜/让平/让负；可与独立泊松混合；可选 hhad 三项赔率融合。
- bqc：上下半场独立泊松；α 可由 `fit-bqc-alpha` 按联赛/赛事拟合，默认 0.45。
"""
from __future__ import annotations

import math
from typing import Any

import config

# 竞彩「比分」固定项（其余主胜/平/客胜归入 胜其它/平其它/负其它）
_BF_HOME_WINS: frozenset[tuple[int, int]] = frozenset({
    (1, 0), (2, 0), (2, 1), (3, 0), (3, 1), (3, 2), (4, 0), (4, 1), (4, 2),
    (5, 0), (5, 1), (5, 2),
})
_BF_DRAWS: frozenset[tuple[int, int]] = frozenset({(0, 0), (1, 1), (2, 2), (3, 3)})
_BF_AWAY_WINS: frozenset[tuple[int, int]] = frozenset({
    (0, 1), (0, 2), (1, 2), (0, 3), (1, 3), (2, 3), (0, 4), (1, 4), (2, 4),
    (0, 5), (1, 5), (2, 5),
})

_BQC_LABELS: tuple[str, ...] = (
    "胜胜", "胜平", "胜负", "平胜", "平平", "平负", "负胜", "负平", "负负",
)


def _poisson_p(lam: float, k: int) -> float:
    if k < 0:
        return 0.0
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    p = math.exp(-lam)
    for i in range(1, k + 1):
        p *= lam / i
    return p


def _prob_goal_diff(exp_h: float, exp_a: float, d: int, max_g: int = 12) -> float:
    """P(主队进球 - 客队进球 = d)，独立泊松；与 dashboard-jc.html getRqProbs 一致。"""
    total = 0.0
    for i in range(max_g + 1):
        j = i - d
        if 0 <= j <= max_g:
            total += _poisson_p(exp_h, i) * _poisson_p(exp_a, j)
    return total


def rqspf_probs_from_exp(
    exp_h: float,
    exp_a: float,
    rq: float | int,
    max_d: int = 12,
) -> dict[str, float]:
    """P(让胜/让平/让负)，独立泊松、主客进球差分桶；rq 为体彩主队让球数（与 hhad.goalLine 一致）。"""
    rq = float(rq)
    p_win = p_draw = p_loss = 0.0
    for d in range(-max_d, max_d + 1):
        prob = _prob_goal_diff(exp_h, exp_a, d, max_g=max_d)
        if d + rq > 0:
            p_win += prob
        elif d + rq == 0:
            p_draw += prob
        else:
            p_loss += prob
    s = p_win + p_draw + p_loss
    if s <= 0:
        return {"win": 0.0, "draw": 0.0, "loss": 0.0}
    return {"win": p_win / s, "draw": p_draw / s, "loss": p_loss / s}


def rqspf_probs_from_score_grid(
    scores_probs: list[tuple[tuple[int, int], float]],
    rq: float | int,
) -> dict[str, float]:
    """由 DC 比分网格上 P(主=i,客=j) 汇总净胜球 d=i-j，再按让球数 rq 得到让胜/让平/让负（与胜平负网格一致）。"""
    rq = float(rq)
    p_win = p_draw = p_loss = 0.0
    for (i, j), p in scores_probs:
        d = int(i) - int(j)
        if d + rq > 0:
            p_win += p
        elif d + rq == 0:
            p_draw += p
        else:
            p_loss += p
    s = p_win + p_draw + p_loss
    if s <= 1e-15:
        return {"win": 0.0, "draw": 0.0, "loss": 0.0}
    return {"win": p_win / s, "draw": p_draw / s, "loss": p_loss / s}


def blend_rqspf_probs(
    dc_p: dict[str, float],
    poisson_p: dict[str, float],
    weight_dc: float,
) -> dict[str, float]:
    """weight_dc 为 DC 网格权重，其余为独立泊松让球权重。"""
    w = max(0.0, min(1.0, float(weight_dc)))
    keys = ("win", "draw", "loss")
    out = {k: w * dc_p.get(k, 0.0) + (1.0 - w) * poisson_p.get(k, 0.0) for k in keys}
    return _normalize_probs(out)


def fuse_rqspf_probs(
    model: dict[str, float],
    implied: dict[str, float],
    weight_model: float,
) -> dict[str, float]:
    """线性融合 hhad 隐含概率与模型让球三项，再归一化。键 win/draw/loss。"""
    w = max(0.0, min(1.0, float(weight_model)))
    keys = ("win", "draw", "loss")
    out = {k: w * model.get(k, 0.0) + (1.0 - w) * implied.get(k, 0.0) for k in keys}
    return _normalize_probs(out)


def hhad_implied_probs(odds_flat: dict[str, Any]) -> dict[str, float] | None:
    """从 rqspf_win / rqspf_draw / rqspf_loss 得隐含概率 (win,draw,loss)；异常则 None。"""
    if not odds_flat or not isinstance(odds_flat, dict):
        return None
    win_s = odds_flat.get("rqspf_win")
    draw_s = odds_flat.get("rqspf_draw")
    loss_s = odds_flat.get("rqspf_loss")
    if win_s is None or draw_s is None or loss_s is None:
        return None
    try:
        o_w = float(win_s) if not isinstance(win_s, str) else float(str(win_s).strip().replace(",", "."))
        o_d = float(draw_s) if not isinstance(draw_s, str) else float(str(draw_s).strip().replace(",", "."))
        o_l = float(loss_s) if not isinstance(loss_s, str) else float(str(loss_s).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    if o_w <= 0 or o_d <= 0 or o_l <= 0:
        return None
    inv = (1.0 / o_w, 1.0 / o_d, 1.0 / o_l)
    tcheck = inv[0] + inv[1] + inv[2]
    if tcheck <= 0:
        return None
    total_min = getattr(config, "PREDICT_ODDS_IMPLIED_SUM_MIN", 0.85)
    total_max = getattr(config, "PREDICT_ODDS_IMPLIED_SUM_MAX", 1.35)
    if tcheck < total_min or tcheck > total_max:
        return None
    return {
        "win": inv[0] / tcheck,
        "draw": inv[1] / tcheck,
        "loss": inv[2] / tcheck,
    }


def _normalize_probs(m: dict[str, float]) -> dict[str, float]:
    s = sum(max(0.0, v) for v in m.values())
    if s <= 0:
        return {k: 0.0 for k in m}
    return {k: max(0.0, v) / s for k, v in m.items()}


def zjqs_probs_from_score_grid(
    scores_probs: list[tuple[tuple[int, int], float]],
) -> dict[str, float]:
    """总进球 0–6 与 7+（键 '0'…'6','7'），由 DC 网格上对 i+j 求和。"""
    buckets: dict[str, float] = {str(k): 0.0 for k in range(8)}
    for (i, j), p in scores_probs:
        t = i + j
        key = "7" if t >= 7 else str(t)
        buckets[key] = buckets.get(key, 0.0) + p
    return _normalize_probs(buckets)


def zjqs_probs_poisson_total(exp_home: float, exp_away: float) -> dict[str, float]:
    """
    独立泊松下总进球 T = H+A ~ Poisson(λh+λa)，竞彩桶 0..6 与 7+。
    避免 DC 网格 max_goals 截断导致的高球概率偏低。
    """
    lam = max(0.01, float(exp_home) + float(exp_away))
    buckets: dict[str, float] = {str(k): 0.0 for k in range(8)}
    cum = 0.0
    for t in range(7):
        p = _poisson_p(lam, t)
        buckets[str(t)] = p
        cum += p
    buckets["7"] = max(0.0, 1.0 - cum)
    return _normalize_probs(buckets)


def _zjqs_norm_key(k_raw: str) -> str | None:
    ks = str(k_raw).strip().upper().replace("＋", "+")
    if ks in ("7+", "7PLUS", "OVER", "07+", "七+"):
        return "7"
    try:
        n = int(float(ks))
        if 0 <= n <= 6:
            return str(n)
        if n >= 7:
            return "7"
    except (TypeError, ValueError):
        pass
    return None


def zjqs_implied_probs_from_odds(zjqs_odds: dict[str, Any]) -> dict[str, float] | None:
    """体彩 zjqs 赔率字典 -> 0..6、7 的归一化隐含概率；键不足或异常则 None。"""
    if not zjqs_odds or not isinstance(zjqs_odds, dict):
        return None
    inv: dict[str, float] = {str(i): 0.0 for i in range(7)}
    inv["7"] = 0.0
    n_ok = 0
    for k_raw, v_raw in zjqs_odds.items():
        kk = _zjqs_norm_key(str(k_raw))
        if kk is None:
            continue
        try:
            o = float(str(v_raw).strip().replace(",", "."))
        except (TypeError, ValueError):
            continue
        if o <= 0:
            continue
        inv[kk] = inv.get(kk, 0.0) + (1.0 / o)
        n_ok += 1
    if n_ok < 5:
        return None
    return _normalize_probs(inv)


def fuse_zjqs_probs(
    model: dict[str, float],
    implied: dict[str, float],
    weight_model: float,
) -> dict[str, float]:
    """线性融合后归一化；键为 0..6、7。"""
    w = max(0.0, min(1.0, float(weight_model)))
    keys = [str(i) for i in range(7)] + ["7"]
    out = {k: w * model.get(k, 0.0) + (1.0 - w) * implied.get(k, 0.0) for k in keys}
    return _normalize_probs(out)


def blend_zjqs_dc_poisson(
    z_dc: dict[str, float],
    z_po: dict[str, float],
    weight_dc: float,
) -> dict[str, float]:
    """weight_dc 为 DC 网格汇总权重，其余为泊松总进球权重。"""
    w = max(0.0, min(1.0, float(weight_dc)))
    keys = [str(i) for i in range(7)] + ["7"]
    out = {k: w * z_dc.get(k, 0.0) + (1.0 - w) * z_po.get(k, 0.0) for k in keys}
    return _normalize_probs(out)


def _cfg_zjqs(overrides: dict | None, key: str, default: Any) -> Any:
    if overrides and key in overrides:
        return overrides[key]
    return getattr(config, key, default)


def compute_zjqs_probs(
    exp_home: float,
    exp_away: float,
    scores_probs: list[tuple[tuple[int, int], float]],
    odds_for_markets: dict[str, Any] | None,
    league_overrides: dict | None = None,
) -> tuple[dict[str, float], str]:
    """
    返回 (zjqs 概率字典, 方法说明串)。
    """
    z_dc = zjqs_probs_from_score_grid(scores_probs)
    z_po = zjqs_probs_poisson_total(exp_home, exp_away)
    mode = str(_cfg_zjqs(league_overrides, "PREDICT_ZJQS_MODE", getattr(config, "PREDICT_ZJQS_MODE", "blend"))).strip().lower()
    w_dc = float(_cfg_zjqs(league_overrides, "PREDICT_ZJQS_BLEND_DC_WEIGHT", getattr(config, "PREDICT_ZJQS_BLEND_DC_WEIGHT", 0.22)))
    w_dc = max(0.0, min(1.0, w_dc))

    if mode == "dc_grid":
        z = z_dc
        desc = "dc_grid_only"
    elif mode == "poisson_sum":
        z = z_po
        desc = "poisson_total_only"
    else:
        z = blend_zjqs_dc_poisson(z_dc, z_po, w_dc)
        desc = f"blend_dc_{w_dc:.2f}_poisson_{1 - w_dc:.2f}"

    use_f = _cfg_zjqs(league_overrides, "PREDICT_ZJQS_USE_ODDS_FUSION", getattr(config, "PREDICT_ZJQS_USE_ODDS_FUSION", True))
    use_f = use_f if isinstance(use_f, bool) else str(use_f).lower() in ("1", "true", "yes")
    if use_f and odds_for_markets and isinstance(odds_for_markets, dict):
        zsub = odds_for_markets.get("zjqs")
        implied = zjqs_implied_probs_from_odds(zsub) if isinstance(zsub, dict) else None
        if implied is not None:
            w_m = float(
                _cfg_zjqs(
                    league_overrides,
                    "PREDICT_ZJQS_ODDS_FUSION_WEIGHT_MODEL",
                    getattr(config, "PREDICT_ZJQS_ODDS_FUSION_WEIGHT_MODEL", 0.42),
                )
            )
            w_m = max(0.0, min(1.0, w_m))
            z = fuse_zjqs_probs(z, implied, w_m)
            desc += f"+zjqs_odds_wmodel_{w_m:.2f}"

    return z, desc


def _norm_crs_bf_key(k_raw: str) -> str | None:
    s = str(k_raw).strip().replace("：", ":").replace("－", "-")
    s = s.replace("-", ":")
    if ":" not in s:
        return None
    parts = s.split(":", 1)
    if len(parts) != 2:
        return None
    try:
        a, b = int(float(parts[0].strip())), int(float(parts[1].strip()))
    except (TypeError, ValueError):
        return None
    if a < 0 or b < 0:
        return None
    return f"{a}:{b}"


def crs_implied_bf_probs(
    crs_odds: dict[str, Any],
    model_bf: dict[str, float],
) -> dict[str, float] | None:
    """体彩 crs 比分赔率 -> 与 model_bf 键交集上的归一化隐含概率；键过少则 None。"""
    if not crs_odds or not isinstance(crs_odds, dict) or not model_bf:
        return None
    inv: dict[str, float] = {}
    for k_raw, v_raw in crs_odds.items():
        kk = _norm_crs_bf_key(str(k_raw))
        if kk is None or kk not in model_bf:
            continue
        try:
            o = float(str(v_raw).strip().replace(",", "."))
        except (TypeError, ValueError):
            continue
        if o <= 0:
            continue
        inv[kk] = inv.get(kk, 0.0) + (1.0 / o)
    min_k = int(getattr(config, "PREDICT_BF_CRS_MIN_KEYS", 8))
    if len(inv) < min_k:
        return None
    return _normalize_probs(inv)


def fuse_bf_with_crs_implied(
    model: dict[str, float],
    implied_on_keys: dict[str, float],
    weight_model: float,
) -> dict[str, float]:
    """对 model 全键融合：有 crs 隐含的键线性混合，无 crs 的键仅用模型分量，最后归一化。"""
    w = max(0.0, min(1.0, float(weight_model)))
    out: dict[str, float] = {}
    for k, m in model.items():
        im = implied_on_keys.get(k)
        if im is not None:
            out[k] = w * m + (1.0 - w) * im
        else:
            out[k] = w * m
    return _normalize_probs(out)


def bf_probs_from_score_grid(
    scores_probs: list[tuple[tuple[int, int], float]],
) -> dict[str, float]:
    """竞彩比分：显式比分 + 胜其它/平其它/负其它。"""
    out: dict[str, float] = {"胜其它": 0.0, "平其它": 0.0, "负其它": 0.0}

    for (i, j), p in scores_probs:
        if (i, j) in _BF_HOME_WINS or (i, j) in _BF_DRAWS or (i, j) in _BF_AWAY_WINS:
            key = f"{i}:{j}"
            out[key] = out.get(key, 0.0) + p
        elif i > j:
            out["胜其它"] += p
        elif i == j:
            out["平其它"] += p
        else:
            out["负其它"] += p

    # 去掉概率为 0 的显式键，减小体积；保留 其它 与任意非零显式
    pruned = {k: v for k, v in out.items() if v > 1e-12}
    if not pruned:
        return {}
    return _normalize_probs(pruned)


def _result_cn(fh: int, fa: int) -> str:
    if fh > fa:
        return "胜"
    if fh < fa:
        return "负"
    return "平"


def bqc_probs_two_halves_poisson(
    exp_h: float,
    exp_a: float,
    alpha: float = 0.45,
    max_g: int = 8,
) -> dict[str, float]:
    """
    半全场 9 结局：上下半场独立泊松，λ1=λ*alpha，λ2=λ*(1-alpha)。
    半场结果按半场比分；全场结果按上下半进球之和。
    """
    alpha = max(0.05, min(0.95, float(alpha)))
    lam1h, lam1a = exp_h * alpha, exp_a * alpha
    lam2h, lam2a = exp_h * (1.0 - alpha), exp_a * (1.0 - alpha)
    counts = {label: 0.0 for label in _BQC_LABELS}
    total = 0.0
    for h1 in range(max_g):
        for a1 in range(max_g):
            p1 = _poisson_p(lam1h, h1) * _poisson_p(lam1a, a1)
            if p1 <= 0:
                continue
            for h2 in range(max_g):
                for a2 in range(max_g):
                    p = p1 * _poisson_p(lam2h, h2) * _poisson_p(lam2a, a2)
                    if p <= 0:
                        continue
                    fh, fa = h1 + h2, a1 + a2
                    half_r = _result_cn(h1, a1)
                    full_r = _result_cn(fh, fa)
                    label = half_r + full_r
                    if label in counts:
                        counts[label] += p
                        total += p
    if total <= 0:
        return {label: 1.0 / len(_BQC_LABELS) for label in _BQC_LABELS}
    return {k: v / total for k, v in counts.items()}


def _best_pick_str(probs: dict[str, float]) -> tuple[str | None, float]:
    if not probs:
        return None, 0.0
    best_k, best_p = max(probs.items(), key=lambda x: x[1])
    return best_k, float(best_p)


def _best_pick_rq(probs: dict[str, float]) -> tuple[str | None, float]:
    mapping = {"win": "让胜", "draw": "让平", "loss": "让负"}
    if not probs:
        return None, 0.0
    best_k, best_p = max(probs.items(), key=lambda x: x[1])
    return mapping.get(best_k, best_k), float(best_p)


def _cfg_rq(league_overrides: dict | None, key: str, default: Any) -> Any:
    if league_overrides and key in league_overrides:
        return league_overrides[key]
    return getattr(config, key, default)


def compute_rqspf_probs(
    exp_home: float,
    exp_away: float,
    scores_probs: list[tuple[tuple[int, int], float]],
    rq_f: float,
    odds_for_markets: dict[str, Any] | None,
    league_overrides: dict | None = None,
) -> tuple[dict[str, float], str]:
    use_dc = _cfg_rq(league_overrides, "PREDICT_RQSPF_USE_DC_GRID", getattr(config, "PREDICT_RQSPF_USE_DC_GRID", True))
    use_dc = use_dc if isinstance(use_dc, bool) else str(use_dc).lower() in ("1", "true", "yes")
    w_dc = float(_cfg_rq(league_overrides, "PREDICT_RQSPF_BLEND_DC_WEIGHT", getattr(config, "PREDICT_RQSPF_BLEND_DC_WEIGHT", 1.0)))
    w_dc = max(0.0, min(1.0, w_dc))
    poisson_p = rqspf_probs_from_exp(exp_home, exp_away, rq_f)
    desc = "poisson_goal_diff"
    if use_dc and scores_probs:
        dc_p = rqspf_probs_from_score_grid(scores_probs, rq_f)
        if sum(dc_p.values()) <= 1e-12:
            rp = poisson_p
            desc = "poisson_fallback_empty_dc"
        elif w_dc >= 0.999:
            rp, desc = dc_p, "dc_grid_goal_diff"
        elif w_dc <= 0.001:
            rp, desc = poisson_p, "poisson_goal_diff"
        else:
            rp = blend_rqspf_probs(dc_p, poisson_p, w_dc)
            desc = f"blend_dc_{w_dc:.2f}_poisson_rq"
    else:
        rp = poisson_p

    use_f = _cfg_rq(league_overrides, "PREDICT_RQSPF_USE_ODDS_FUSION", getattr(config, "PREDICT_RQSPF_USE_ODDS_FUSION", True))
    use_f = use_f if isinstance(use_f, bool) else str(use_f).lower() in ("1", "true", "yes")
    if use_f and odds_for_markets and isinstance(odds_for_markets, dict):
        implied = hhad_implied_probs(odds_for_markets)
        if implied is not None:
            w_m = float(
                _cfg_rq(
                    league_overrides,
                    "PREDICT_RQSPF_ODDS_FUSION_WEIGHT_MODEL",
                    getattr(config, "PREDICT_RQSPF_ODDS_FUSION_WEIGHT_MODEL", 0.42),
                )
            )
            w_m = max(0.0, min(1.0, w_m))
            rp = fuse_rqspf_probs(rp, implied, w_m)
            desc += f"+hhad_odds_wmodel_{w_m:.2f}"
    return rp, desc


def build_jc_play_markets(
    exp_home: float,
    exp_away: float,
    scores_probs: list[tuple[tuple[int, int], float]],
    rq_handicap: float | int | None = None,
    odds_for_markets: dict[str, Any] | None = None,
    league_overrides: dict | None = None,
    *,
    bqc_alpha: float = 0.45,
    bqc_max_g: int = 8,
) -> dict[str, Any]:
    """
    生成写入 prediction / jc_predictions 的扩展玩法结构。

    各玩法的 probs 已归一化；best_pick 为体彩友好字符串（rqspf 为 让胜/让平/让负）。
    """
    zjqs_p, zjqs_method = compute_zjqs_probs(exp_home, exp_away, scores_probs, odds_for_markets, league_overrides)
    bf_p = bf_probs_from_score_grid(scores_probs)
    bf_method = "jc_fixed_scores_plus_other_buckets"
    use_crs = _cfg_zjqs(league_overrides, "PREDICT_BF_USE_CRS_FUSION", getattr(config, "PREDICT_BF_USE_CRS_FUSION", True))
    use_crs = use_crs if isinstance(use_crs, bool) else str(use_crs).lower() in ("1", "true", "yes")
    if use_crs and odds_for_markets and isinstance(odds_for_markets, dict):
        crs_sub = odds_for_markets.get("bf")
        if isinstance(crs_sub, dict):
            implied_bf = crs_implied_bf_probs(crs_sub, bf_p)
            if implied_bf is not None:
                w_bf = float(
                    _cfg_zjqs(
                        league_overrides,
                        "PREDICT_BF_CRS_FUSION_WEIGHT_MODEL",
                        getattr(config, "PREDICT_BF_CRS_FUSION_WEIGHT_MODEL", 0.38),
                    )
                )
                w_bf = max(0.0, min(1.0, w_bf))
                bf_p = fuse_bf_with_crs_implied(bf_p, implied_bf, w_bf)
                bf_method += f"+crs_odds_wmodel_{w_bf:.2f}"

    bqc_p = bqc_probs_two_halves_poisson(exp_home, exp_away, alpha=bqc_alpha, max_g=bqc_max_g)

    z_best, z_pb = _best_pick_str(zjqs_p)
    b_best, b_pb = _best_pick_str(bf_p)
    q_best, q_pb = _best_pick_str(bqc_p)

    rq_block: dict[str, Any] | None = None
    rq_method = "poisson_goal_diff_d_plus_rq"
    if rq_handicap is not None:
        try:
            rq_f = float(rq_handicap)
        except (TypeError, ValueError):
            rq_f = None
        if rq_f is not None and not math.isnan(rq_f):
            rp, rq_method = compute_rqspf_probs(
                exp_home, exp_away, scores_probs, rq_f, odds_for_markets, league_overrides
            )
            rk, rpv = _best_pick_rq(rp)
            rq_block = {
                "handicap": rq_f,
                "probs": {k: round(v, 4) for k, v in rp.items()},
                "best_pick": rk,
                "best_prob": round(rpv, 4),
            }

    return {
        "zjqs": {
            "probs": {k: round(v, 4) for k, v in sorted(zjqs_p.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 99)},
            "best_pick": z_best,
            "best_prob": round(z_pb, 4),
        },
        "bf": {
            "probs": {k: round(v, 4) for k, v in sorted(bf_p.items(), key=lambda x: (len(x[0]), x[0]))},
            "best_pick": b_best,
            "best_prob": round(b_pb, 4),
        },
        "rqspf": rq_block,
        "bqc": {
            "probs": {k: round(bqc_p.get(k, 0.0), 4) for k in _BQC_LABELS},
            "best_pick": q_best,
            "best_prob": round(q_pb, 4),
        },
        "method": {
            "zjqs": zjqs_method,
            "bf": bf_method,
            "rqspf": rq_method,
            "bqc": f"two_independent_halves_poisson_alpha_{bqc_alpha:.3f}",
        },
    }


def actual_zjqs_bucket(home_goals: int, away_goals: int) -> str:
    t = int(home_goals) + int(away_goals)
    return "7" if t >= 7 else str(t)


def actual_bf_label(home_goals: int, away_goals: int) -> str:
    h, a = int(home_goals), int(away_goals)
    if (h, a) in _BF_HOME_WINS or (h, a) in _BF_DRAWS or (h, a) in _BF_AWAY_WINS:
        return f"{h}:{a}"
    if h > a:
        return "胜其它"
    if h < a:
        return "负其它"
    return "平其它"


def actual_bqc_label(half_h: int, half_a: int, full_h: int, full_a: int) -> str | None:
    hr = _result_cn(int(half_h), int(half_a))
    fr = _result_cn(int(full_h), int(full_a))
    lab = hr + fr
    return lab if lab in _BQC_LABELS else None


def jc_rqspf_actual_side(home_goals: int, away_goals: int, handicap: int) -> str:
    """竞彩让球实际结果：主 / 平 / 客（与 _get_rq_probs 一致）。"""
    adj = int(home_goals) - int(away_goals) + int(handicap)
    if adj > 0:
        return "主"
    if adj < 0:
        return "客"
    return "平"


def jc_rqspf_pick_to_side(pick_cn: str | None) -> str | None:
    if pick_cn == "让胜":
        return "主"
    if pick_cn == "让负":
        return "客"
    if pick_cn == "让平":
        return "平"
    return None
