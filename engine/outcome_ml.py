"""
2.2 ML 修正层：在规则模型输出的基础上，用历史「规则预测 + 实际结果」训练一个小型分类器，
对胜/平/负概率做修正，实现「规则 + ML」的加大模型。特征为规则输出的期望进球与概率等，易解释。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import config


def _model_path(competition_id: int | None = None) -> Path:
    """联赛专属模型：competition_id 非空时用 outcome_ml_model_{id}.joblib，否则用默认路径。"""
    base = Path(getattr(config, "PREDICT_OUTCOME_ML_PATH", str(config.DATA_DIR / "outcome_ml_model.joblib")))
    if competition_id is not None:
        return base.parent / f"outcome_ml_model_{competition_id}.joblib"
    return base


def _feature_names(use_rank: bool = True) -> list[str]:
    names = [
        "exp_goals_home", "exp_goals_away",
        "prob_home_win", "prob_draw", "prob_away_win",
        "last_n", "competition_id",
    ]
    if use_rank:
        names.append("rank_diff")
    return names


def build_feature_vector(
    exp_goals_home: float,
    exp_goals_away: float,
    prob_home_win: float,
    prob_draw: float,
    prob_away_win: float,
    last_n: int,
    competition_id: int | None,
    competition_id_to_idx: dict[int | None, int] | None = None,
    home_rank: int | None = None,
    away_rank: int | None = None,
    use_rank: bool = True,
) -> list[float]:
    """构建与训练时一致的特征向量。use_rank 且主客排名都有时加入 rank_diff（(away_rank - home_rank)/20，归一化）。"""
    vec = [
        float(exp_goals_home), float(exp_goals_away),
        float(prob_home_win), float(prob_draw), float(prob_away_win),
        float(last_n),
    ]
    if competition_id_to_idx is not None:
        idx = competition_id_to_idx.get(competition_id, 0)
        vec.append(float(idx))
    else:
        vec.append(0.0)
    if use_rank:
        if home_rank is not None and away_rank is not None:
            rank_diff = (float(away_rank) - float(home_rank)) / 20.0
            vec.append(max(-1.0, min(1.0, rank_diff)))
        else:
            vec.append(0.0)
    return vec


def load_model(competition_id: int | None = None) -> tuple[Any, dict[int | None, int], dict[str, Any]] | None:
    """加载训练好的模型、competition_id 映射与 meta。competition_id 非空时先尝试联赛专属模型。返回 (model, competition_id_to_idx, meta) 或 None。"""
    for cid in ([competition_id] if competition_id is not None else []) + [None]:
        path = _model_path(cid)
        if not path.is_file():
            continue
        meta_path = path.with_suffix(path.suffix + ".meta.json")
        try:
            import joblib
            model = joblib.load(path)
            meta: dict[str, Any] = {}
            competition_id_to_idx: dict[int | None, int] = {}
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                competition_id_to_idx = {int(k) if k != "None" else None: v for k, v in meta.get("competition_id_to_idx", {}).items()}
            meta.setdefault("use_rank", False)
            return (model, competition_id_to_idx, meta)
        except Exception:
            continue
    return None


def predict_proba(
    exp_goals_home: float,
    exp_goals_away: float,
    prob_home_win: float,
    prob_draw: float,
    prob_away_win: float,
    last_n: int,
    competition_id: int | None,
    home_rank: int | None = None,
    away_rank: int | None = None,
) -> tuple[float, float, float] | None:
    """用 ML 模型预测胜/平/负概率。优先使用联赛专属模型。可选传入 home_rank/away_rank 参与扩展特征。"""
    loaded = load_model(competition_id)
    if not loaded:
        return None
    model, comp_map, meta = loaded
    use_rank = meta.get("use_rank", False)
    vec = build_feature_vector(
        exp_goals_home, exp_goals_away,
        prob_home_win, prob_draw, prob_away_win,
        last_n, competition_id, comp_map,
        home_rank=home_rank,
        away_rank=away_rank,
        use_rank=use_rank,
    )
    try:
        import numpy as np
        X = np.array([vec])
        if hasattr(model, "predict_proba"):
            probs = model.predict_proba(X)[0]
            # 类别顺序需与训练时一致：通常 0=主胜 1=平 2=客胜
            if probs.shape[0] >= 3:
                return (float(probs[0]), float(probs[1]), float(probs[2]))
        return None
    except Exception:
        return None


def train_and_save(
    engine,
    competition_id: int | None = None,
    train_date_from: str | None = None,
    train_date_to: str | None = None,
) -> dict[str, Any]:
    """
    从 DB predictions + matches 与竞彩库收集 (规则输出特征, 实际结果)，训练分类器并保存。
    competition_id 非空时仅用该联赛样本训练，并保存为联赛专属模型 outcome_ml_model_{id}.joblib。
    train_date_from/train_date_to 同时给出时，仅保留该日期范围内的样本（用于「仅用往年数据训练、预测当年」对比）。
    返回 {"n_samples": int, "path": str, "accuracy": float, "n_main": int, "n_jc": int} 或 {"error": str}。
    """
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        import joblib
        import numpy as np
    except ImportError:
        return {"error": "请安装 scikit-learn 与 joblib：pip install scikit-learn joblib"}

    rows = []
    n_main, n_jc = 0, 0
    try:
        from db import repo
        if repo.use_mongo():
            rows = list(repo.predict_get_ml_training_rows())
            n_main = len(rows)
            try:
                from db.self_improvement import jc_get_ml_training_rows
                jc_rows = jc_get_ml_training_rows()
                if jc_rows:
                    n_jc = len(jc_rows)
                    rows.extend(jc_rows)
            except Exception:
                pass
    except Exception:
        pass
    if not rows and engine is not None:
        from sqlalchemy import text
        raw = None
        try:
            raw = engine.connect().execute(text("""
                SELECT p.exp_goals_home, p.exp_goals_away, p.prob_home_win, p.prob_draw, p.prob_away_win,
                       p.last_n, p.competition_id,
                       m.home_score, m.away_score, m.home_rank, m.away_rank, m.match_date
                FROM predictions p
                JOIN matches m ON m.id = p.match_id
                WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            """)).fetchall()
        except Exception:
            try:
                raw = engine.connect().execute(text("""
                    SELECT p.exp_goals_home, p.exp_goals_away, p.prob_home_win, p.prob_draw, p.prob_away_win,
                           p.last_n, p.competition_id,
                           m.home_score, m.away_score, NULL, NULL, m.match_date
                    FROM predictions p
                    JOIN matches m ON m.id = p.match_id
                    WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                """)).fetchall()
            except Exception:
                pass
        if raw:
            rows = []
            for r in raw:
                hr = r[9] if len(r) > 9 else None
                ar = r[10] if len(r) > 10 else None
                date_str = (r[11] or "")[:10] if len(r) > 11 and r[11] else None
                rows.append((float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5]), r[6], int(r[7]), int(r[8]), hr, ar, date_str))

    # 统一为 12 元组（最后一列为 match_date_str），缺则补 None
    def _norm_row(r):
        if len(r) >= 12:
            return r
        return (*r[:11], (r[11] if len(r) > 11 else None))
    rows = [_norm_row(r) for r in rows]

    if competition_id is not None:
        rows = [r for r in rows if r[6] == competition_id]
    if train_date_from and train_date_to:
        from_str = (train_date_from or "")[:10]
        to_str = (train_date_to or "")[:10]
        rows = [r for r in rows if r[11] and from_str <= (r[11] or "")[:10] <= to_str]
    if len(rows) < 30:
        return {"error": "样本不足（至少 30 条）", "n_samples": len(rows)}

    competition_ids = sorted(set(r[6] for r in rows), key=lambda x: (x is None, x))
    comp_to_idx = {c: i for i, c in enumerate(competition_ids)}
    use_rank = any(len(r) >= 11 and r[9] is not None and r[10] is not None for r in rows)

    X_list = []
    y_list = []
    for r in rows:
        exp_h, exp_a = float(r[0]), float(r[1])
        p_h, p_d, p_a = float(r[2]), float(r[3]), float(r[4])
        ln, cid = int(r[5]), r[6]
        h_score, a_score = int(r[7]), int(r[8])
        hr = r[9] if len(r) >= 11 else None
        ar = r[10] if len(r) >= 11 else None
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
        if h_score > a_score:
            outcome = 0
        elif h_score < a_score:
            outcome = 2
        else:
            outcome = 1
        vec = build_feature_vector(exp_h, exp_a, p_h, p_d, p_a, ln, cid, comp_to_idx, home_rank=hr, away_rank=ar, use_rank=use_rank)
        X_list.append(vec)
        y_list.append(outcome)

    X = np.array(X_list)
    y = np.array(y_list)
    rs = getattr(config, "PREDICT_OUTCOME_ML_RANDOM_STATE", 42)
    n_est = int(getattr(config, "PREDICT_OUTCOME_ML_N_ESTIMATORS", 100))
    depth = int(getattr(config, "PREDICT_OUTCOME_ML_MAX_DEPTH", 4))
    leaf = int(getattr(config, "PREDICT_OUTCOME_ML_MIN_SAMPLES_LEAF", 8))
    clf = GradientBoostingClassifier(
        n_estimators=n_est, max_depth=depth, min_samples_leaf=leaf, random_state=rs
    )
    clf.fit(X, y)
    acc = float(np.mean(clf.predict(X) == y))

    path = _model_path(competition_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, path)
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    meta = {"competition_id_to_idx": {str(k): v for k, v in comp_to_idx.items()}, "use_rank": use_rank}
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return {
        "n_samples": len(rows),
        "path": str(path),
        "accuracy": round(acc, 4),
        "n_main": n_main,
        "n_jc": n_jc,
    }
