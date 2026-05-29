#!/usr/bin/env python3
import json
import os
import re
import subprocess
from pathlib import Path

WD = "/Users/taotao/Documents/QiyuanSymbol/football_anlyision"
OUT = Path(WD) / "data" / "tune_2026" / "two_stage_tune_2526.json"
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


def _env_with(params: dict) -> dict:
    env = os.environ.copy()
    for k in RM:
        env.pop(k, None)
    env.update({k: str(v) for k, v in params.items()})
    return env


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


def repredict_year(year: int, env: dict) -> None:
    subprocess.run(
        ["python3", "main.py", "predict-jc-from-self-improvement", "--year", str(year), "--include-finished"],
        cwd=WD,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def stage1_scan_2026(b_single_2026: float) -> list[dict]:
    # 窄带：ODDS 固定 0.55，只微调 ML 与 DRAW
    candidates = []
    idx = 1
    for ml in [0.33, 0.34, 0.35]:
        for draw in [0.21, 0.22, 0.23]:
            candidates.append(
                (
                    f"s1_c{idx:02d}",
                    {
                        "PREDICT_OUTCOME_ML_WEIGHT": ml,
                        "PREDICT_ODDS_FUSION_WEIGHT_MODEL": 0.55,
                        "PREDICT_RQSPF_ODDS_FUSION_WEIGHT_MODEL": 0.35,
                        "PREDICT_DRAW_BOOST_COEF": draw,
                        "PREDICT_RQSPF_BLEND_DC_WEIGHT": 1.0,
                    },
                )
            )
            idx += 1

    out = []
    for name, params in candidates:
        env = _env_with(params)
        repredict_year(2026, env)
        s26, o26 = eval_year(2026, env)
        out.append(
            {
                "name": name,
                "params": params,
                "single_2026": s26,
                "outcome_2026": o26,
                "delta_single_2026": round(s26 - b_single_2026, 2),
            }
        )
    return out


def pick_top2(stage1_results: list[dict]) -> list[dict]:
    # 先取目标区间 +0.2~+0.4；若不足 2 个，用“提升最大”补齐
    in_band = [r for r in stage1_results if 0.2 <= r["delta_single_2026"] <= 0.4]
    in_band = sorted(in_band, key=lambda x: (x["single_2026"], x["outcome_2026"]), reverse=True)
    if len(in_band) >= 2:
        return in_band[:2]
    rest = sorted(stage1_results, key=lambda x: (x["delta_single_2026"], x["single_2026"], x["outcome_2026"]), reverse=True)
    chosen = []
    seen = set()
    for r in in_band + rest:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        chosen.append(r)
        if len(chosen) >= 2:
            break
    return chosen


def stage2_guard_2025(top2: list[dict], b_single_2025: float, b_out_2025: float) -> list[dict]:
    out = []
    for r in top2:
        env = _env_with(r["params"])
        repredict_year(2025, env)
        s25, o25 = eval_year(2025, env)
        out.append(
            {
                **r,
                "single_2025": s25,
                "outcome_2025": o25,
                "delta_single_2025": round(s25 - b_single_2025, 2),
                "delta_out_2025": round(o25 - b_out_2025, 2),
                "guard_ok": (s25 >= b_single_2025 and o25 >= b_out_2025),
            }
        )
    return out


def main() -> int:
    base_env = _env_with({})
    b25s, b25o = eval_year(2025, base_env)
    b26s, b26o = eval_year(2026, base_env)

    s1 = stage1_scan_2026(b26s)
    top2 = pick_top2(s1)
    s2 = stage2_guard_2025(top2, b25s, b25o)

    guarded = [x for x in s2 if x["guard_ok"]]
    if guarded:
        best = max(guarded, key=lambda x: (x["single_2026"], x["outcome_2026"]))
        decision = "guarded_best"
    else:
        best = max(s2, key=lambda x: (x["single_2026"], x["outcome_2026"]))
        decision = "no_guard_pass_fallback"

    payload = {
        "baseline": {
            "single_2025": b25s,
            "outcome_2025": b25o,
            "single_2026": b26s,
            "outcome_2026": b26o,
        },
        "stage1_results": sorted(s1, key=lambda x: (x["single_2026"], x["outcome_2026"]), reverse=True),
        "stage2_top2_guard": s2,
        "decision": decision,
        "best": best,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
