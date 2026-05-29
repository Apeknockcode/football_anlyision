"""
半全场 BQC：按历史半场/全场进球比例拟合泊松半场权重 α，写入 data/bqc_alpha_by_competition.json。
预测时由 resolve_bqc_alpha(competition_id) 读取；无数据时用 config.PREDICT_BQC_ALPHA_DEFAULT。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

import config


def bqc_alpha_json_path() -> Path:
    return Path(getattr(config, "PREDICT_BQC_ALPHA_PATH", str(config.DATA_DIR / "bqc_alpha_by_competition.json")))


def resolve_bqc_alpha(competition_id: int | None) -> float:
    """优先按赛事 ID 取拟合 α，否则用文件内 default，最后用配置默认。"""
    fallback = float(getattr(config, "PREDICT_BQC_ALPHA_DEFAULT", 0.45))
    p = bqc_alpha_json_path()
    if not p.is_file():
        return max(0.05, min(0.95, fallback))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return max(0.05, min(0.95, fallback))
    by_cid = data.get("by_competition_id") or {}
    if competition_id is not None:
        key = str(int(competition_id))
        if key in by_cid:
            try:
                v = float(by_cid[key])
                return max(0.05, min(0.95, v))
            except (TypeError, ValueError):
                pass
    try:
        d0 = float(data.get("default", fallback))
        return max(0.05, min(0.95, d0))
    except (TypeError, ValueError):
        return max(0.05, min(0.95, fallback))


def fit_and_save_bqc_alpha(
    min_samples_per_competition: int = 12,
    min_global_samples: int = 40,
) -> dict[str, Any]:
    """
    用 jc_predictions + jingcai_match_analysis 已完赛且含半场比分的场次，
    按 competition_id 聚合 r = (半场总进球)/(全场总进球)，取中位数并裁剪到 [0.38, 0.52]。
    """
    from db.self_improvement import (
        _jc_match_analysis_batch_find,
        get_self_improvement_db,
        jc_predictions_load_all,
    )

    db = get_self_improvement_db()
    if db is None:
        return {"error": "未配置 MONGO_URI 或 self_improvement 库", "path": str(bqc_alpha_json_path())}

    preds = jc_predictions_load_all()
    mids = [p.get("sporttery_match_id") for p in preds if p.get("sporttery_match_id") is not None]
    analysis_by_mid = _jc_match_analysis_batch_find(db, mids)

    ratios_by_cid: dict[int, list[float]] = defaultdict(list)
    ratios_all: list[float] = []

    for pred in preds:
        mid = pred.get("sporttery_match_id")
        if mid is None:
            continue
        doc = analysis_by_mid.get(mid)
        if not doc:
            continue
        fh = doc.get("home_score")
        fa = doc.get("away_score")
        hh = doc.get("half_home")
        ha = doc.get("half_away")
        try:
            fi, fa_i = int(fh), int(fa)
            hi, ha_i = int(hh), int(ha)
        except (TypeError, ValueError):
            continue
        full_t = fi + fa_i
        if full_t <= 0:
            continue
        half_t = hi + ha_i
        if half_t < 0 or half_t > full_t:
            continue
        r = half_t / full_t
        if r < 0.12 or r > 0.88:
            continue
        ratios_all.append(r)
        pred_obj = pred.get("prediction") or {}
        cid = pred_obj.get("competition_id")
        if cid is not None:
            try:
                ratios_by_cid[int(cid)].append(r)
            except (TypeError, ValueError):
                pass

    def _clip_alpha(m: float) -> float:
        lo = float(getattr(config, "PREDICT_BQC_ALPHA_CLIP_MIN", 0.38))
        hi = float(getattr(config, "PREDICT_BQC_ALPHA_CLIP_MAX", 0.52))
        return max(lo, min(hi, m))

    by_out: dict[str, float] = {}
    for cid, xs in ratios_by_cid.items():
        if len(xs) < min_samples_per_competition:
            continue
        by_out[str(cid)] = round(_clip_alpha(float(median(xs))), 4)

    default_alpha = float(getattr(config, "PREDICT_BQC_ALPHA_DEFAULT", 0.45))
    if len(ratios_all) >= min_global_samples:
        default_alpha = _clip_alpha(float(median(ratios_all)))

    out_doc = {
        "default": round(default_alpha, 4),
        "by_competition_id": by_out,
        "meta": {
            "n_samples_global": len(ratios_all),
            "n_competitions": len(by_out),
            "min_samples_per_competition": min_samples_per_competition,
        },
    }
    path = bqc_alpha_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(path), **out_doc["meta"], "default": out_doc["default"]}
