#!/usr/bin/env python3
import json
import os
import re
import subprocess
from pathlib import Path

WD = "/Users/taotao/Documents/QiyuanSymbol/football_anlyision"
OUT = Path(WD) / "data" / "tune_2026" / "ab_guard_2526_small_grid.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

PAT_SINGLE = r"【主指标】单场推荐猜对：\d+/\d+，命中率 ([\d.]+)%"
PAT_OUT = r"胜平负猜对：\d+/\d+，命中率 ([\d.]+)%"
VARS_ALL = [
    "PREDICT_OUTCOME_ML_WEIGHT",
    "PREDICT_ODDS_FUSION_WEIGHT_MODEL",
    "PREDICT_RQSPF_ODDS_FUSION_WEIGHT_MODEL",
    "PREDICT_DRAW_BOOST_COEF",
    "PREDICT_RQSPF_BLEND_DC_WEIGHT",
]


def eval_year(year: int, env: dict) -> tuple[float, float]:
    txt = subprocess.check_output(
        ["python3", "main.py", "jc-predictions-eval", "--year", str(year)],
        cwd=WD,
        env=env,
        text=True,
        errors="ignore",
    )
    s = float(re.search(PAT_SINGLE, txt).group(1))
    o = float(re.search(PAT_OUT, txt).group(1))
    return s, o


def run_combo(tag: str, params: dict) -> dict:
    env = os.environ.copy()
    for k in VARS_ALL:
        env.pop(k, None)
    env.update({k: str(v) for k, v in params.items()})
    subprocess.run(
        ["python3", "main.py", "predict-jc-from-self-improvement", "--years", "2025,2026", "--include-finished"],
        cwd=WD,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    s25, o25 = eval_year(2025, env)
    s26, o26 = eval_year(2026, env)
    return {
        "tag": tag,
        "params": params,
        "single_2025": s25,
        "outcome_2025": o25,
        "single_2026": s26,
        "outcome_2026": o26,
        "score_2026": round(0.7 * s26 + 0.3 * o26, 4),
        "score_avg": round(0.35 * (s25 + s26) + 0.15 * (o25 + o26), 4),
    }


def main() -> int:
    baseline = run_combo("baseline", {})
    b25s, b25o = baseline["single_2025"], baseline["outcome_2025"]
    b26s = baseline["single_2026"]

    cands = []
    for ml in [0.32, 0.34, 0.36]:
        for ow in [0.50, 0.52, 0.55]:
            cands.append(
                {
                    "PREDICT_OUTCOME_ML_WEIGHT": ml,
                    "PREDICT_ODDS_FUSION_WEIGHT_MODEL": ow,
                    "PREDICT_RQSPF_ODDS_FUSION_WEIGHT_MODEL": 0.35,
                    "PREDICT_DRAW_BOOST_COEF": 0.22,
                    "PREDICT_RQSPF_BLEND_DC_WEIGHT": 1.0,
                }
            )
    for rq in [0.32, 0.35, 0.38]:
        for db in [0.20, 0.22, 0.24]:
            cands.append(
                {
                    "PREDICT_OUTCOME_ML_WEIGHT": 0.34,
                    "PREDICT_ODDS_FUSION_WEIGHT_MODEL": 0.52,
                    "PREDICT_RQSPF_ODDS_FUSION_WEIGHT_MODEL": rq,
                    "PREDICT_DRAW_BOOST_COEF": db,
                    "PREDICT_RQSPF_BLEND_DC_WEIGHT": 1.0,
                }
            )

    uniq = []
    seen = set()
    for c in cands:
        k = tuple(sorted(c.items()))
        if k not in seen:
            seen.add(k)
            uniq.append(c)

    results = [baseline]
    for i, c in enumerate(uniq, 1):
        results.append(run_combo(f"c{i:02d}", c))

    guard = [r for r in results if r["single_2025"] >= b25s - 0.1 and r["outcome_2025"] >= b25o - 0.1]
    target = [r for r in guard if 0.3 <= (r["single_2026"] - b26s) <= 0.8]
    if target:
        best = max(target, key=lambda x: (x["single_2026"], x["outcome_2026"]))
        bucket = "target"
    elif guard:
        best = max(guard, key=lambda x: (x["single_2026"], x["outcome_2026"]))
        bucket = "guard"
    else:
        best = max(results, key=lambda x: (x["single_2026"], x["outcome_2026"]))
        bucket = "fallback"

    out_json = {
        "baseline": baseline,
        "best": best,
        "bucket": bucket,
        "results_sorted": sorted(results, key=lambda x: (x["single_2026"], x["outcome_2026"]), reverse=True),
    }
    OUT.write_text(json.dumps(out_json, ensure_ascii=False, indent=2), encoding="utf-8")
    print("baseline", baseline)
    print("bucket", bucket)
    print("best", best)
    print("top5", [(r["tag"], r["single_2025"], r["outcome_2025"], r["single_2026"], r["outcome_2026"]) for r in out_json["results_sorted"][:5]])
    print("saved", str(OUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
