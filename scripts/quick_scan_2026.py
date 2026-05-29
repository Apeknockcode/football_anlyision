#!/usr/bin/env python3
import json
import os
import re
import subprocess
from pathlib import Path

WD = "/Users/taotao/Documents/QiyuanSymbol/football_anlyision"
OUT = Path(WD) / "data" / "tune_2026" / "quick_guard_2026_scan.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

PAT_SINGLE = r"【主指标】单场推荐猜对：\d+/\d+，命中率 ([\d.]+)%"
PAT_OUT = r"胜平负猜对：\d+/\d+，命中率 ([\d.]+)%"
RM = [
    "PREDICT_OUTCOME_ML_WEIGHT",
    "PREDICT_ODDS_FUSION_WEIGHT_MODEL",
    "PREDICT_RQSPF_ODDS_FUSION_WEIGHT_MODEL",
    "PREDICT_DRAW_BOOST_COEF",
    "PREDICT_RQSPF_BLEND_DC_WEIGHT",
]


def eval_2026(env: dict) -> tuple[float, float]:
    txt = subprocess.check_output(
        ["python3", "main.py", "jc-predictions-eval", "--year", "2026"],
        cwd=WD,
        env=env,
        text=True,
        errors="ignore",
    )
    s = float(re.search(PAT_SINGLE, txt).group(1))
    o = float(re.search(PAT_OUT, txt).group(1))
    return s, o


def run_one(name: str, params: dict, b_single: float, b_out: float) -> dict:
    env = os.environ.copy()
    for k in RM:
        env.pop(k, None)
    env.update({k: str(v) for k, v in params.items()})
    subprocess.run(
        ["python3", "main.py", "predict-jc-from-self-improvement", "--year", "2026", "--include-finished"],
        cwd=WD,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    s, o = eval_2026(env)
    return {
        "name": name,
        "single_2026": s,
        "outcome_2026": o,
        "delta_single": round(s - b_single, 2),
        "delta_out": round(o - b_out, 2),
        "env": params,
    }


def main() -> int:
    base_env = os.environ.copy()
    for k in RM:
        base_env.pop(k, None)
    b_single, b_out = eval_2026(base_env)

    cands = [
        ("baseline", {}),
        (
            "c1",
            {
                "PREDICT_OUTCOME_ML_WEIGHT": 0.35,
                "PREDICT_ODDS_FUSION_WEIGHT_MODEL": 0.52,
                "PREDICT_RQSPF_ODDS_FUSION_WEIGHT_MODEL": 0.35,
                "PREDICT_DRAW_BOOST_COEF": 0.22,
                "PREDICT_RQSPF_BLEND_DC_WEIGHT": 1.0,
            },
        ),
        (
            "c2",
            {
                "PREDICT_OUTCOME_ML_WEIGHT": 0.36,
                "PREDICT_ODDS_FUSION_WEIGHT_MODEL": 0.50,
                "PREDICT_RQSPF_ODDS_FUSION_WEIGHT_MODEL": 0.35,
                "PREDICT_DRAW_BOOST_COEF": 0.22,
                "PREDICT_RQSPF_BLEND_DC_WEIGHT": 1.0,
            },
        ),
        (
            "c3",
            {
                "PREDICT_OUTCOME_ML_WEIGHT": 0.34,
                "PREDICT_ODDS_FUSION_WEIGHT_MODEL": 0.55,
                "PREDICT_RQSPF_ODDS_FUSION_WEIGHT_MODEL": 0.35,
                "PREDICT_DRAW_BOOST_COEF": 0.22,
                "PREDICT_RQSPF_BLEND_DC_WEIGHT": 1.0,
            },
        ),
        (
            "c4",
            {
                "PREDICT_OUTCOME_ML_WEIGHT": 0.34,
                "PREDICT_ODDS_FUSION_WEIGHT_MODEL": 0.52,
                "PREDICT_RQSPF_ODDS_FUSION_WEIGHT_MODEL": 0.38,
                "PREDICT_DRAW_BOOST_COEF": 0.20,
                "PREDICT_RQSPF_BLEND_DC_WEIGHT": 1.0,
            },
        ),
    ]

    res = [run_one(name, env, b_single, b_out) for name, env in cands]
    best = max(res, key=lambda x: (x["single_2026"], x["outcome_2026"]))

    payload = {"baseline_2026": {"single": b_single, "outcome": b_out}, "results": res, "best": best}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
