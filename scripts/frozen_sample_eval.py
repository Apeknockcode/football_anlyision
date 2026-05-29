#!/usr/bin/env python3
"""
冻结样本 + 固定口径评估（竞彩）。

用法示例：
  # 1) 冻结 2025/2026 样本（仅保留“有预测且已完赛”的 sporttery_match_id）
  python3 scripts/frozen_sample_eval.py freeze --years 2025,2026 --out data/eval_samples/frozen_2526_ids.json

  # 2) 在固定样本上评估当前模型表现
  python3 scripts/frozen_sample_eval.py eval --ids data/eval_samples/frozen_2526_ids.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.self_improvement import get_self_improvement_db


def _parse_years(years: str) -> list[int]:
    out: list[int] = []
    for part in (years or "").split(","):
        s = part.strip()
        if s.isdigit():
            out.append(int(s))
    return sorted(set(out))


def _winner(h: int, a: int) -> str:
    if h > a:
        return "home"
    if h < a:
        return "away"
    return "draw"


def _pred_label_from_prediction(pred: dict[str, Any]) -> str | None:
    try:
        ph = float(pred.get("prob_home_win") or 0)
        pd = float(pred.get("prob_draw") or 0)
        pa = float(pred.get("prob_away_win") or 0)
    except Exception:
        return None
    arr = [("home", ph), ("draw", pd), ("away", pa)]
    arr.sort(key=lambda x: x[1], reverse=True)
    return arr[0][0]


def _load_latest_predictions_by_mid(db, mids: list[int] | None = None) -> dict[int, dict[str, Any]]:
    q: dict[str, Any] = {"sporttery_match_id": {"$ne": None}}
    if mids is not None:
        q = {"sporttery_match_id": {"$in": mids}}
    proj = {"_id": 0, "sporttery_match_id": 1, "prediction": 1, "date": 1, "updated_at": 1}
    cur = db["jc_predictions"].find(q, proj)

    # 同一个 mid 若出现多条，保留 updated_at 最新的一条
    best: dict[int, dict[str, Any]] = {}
    for d in cur:
        mid_raw = d.get("sporttery_match_id")
        try:
            mid = int(mid_raw)
        except Exception:
            continue
        old = best.get(mid)
        if old is None:
            best[mid] = d
            continue
        if str(d.get("updated_at") or "") >= str(old.get("updated_at") or ""):
            best[mid] = d
    return best


def _load_finished_analysis_by_mid(db, years: list[int] | None = None, mids: list[int] | None = None) -> dict[int, dict[str, Any]]:
    q: dict[str, Any] = {
        "sporttery_match_id": {"$ne": None},
        "home_score": {"$ne": None},
        "away_score": {"$ne": None},
    }
    if mids is not None:
        q["sporttery_match_id"] = {"$in": mids}
    elif years:
        year_prefix = [str(y) for y in years]
        q["$or"] = [{"jc_date": {"$regex": f"^{y}"}} for y in year_prefix]
    proj = {"_id": 0, "sporttery_match_id": 1, "jc_date": 1, "home_score": 1, "away_score": 1}
    cur = db["jingcai_match_analysis"].find(q, proj)
    out: dict[int, dict[str, Any]] = {}
    for d in cur:
        try:
            mid = int(d.get("sporttery_match_id"))
        except Exception:
            continue
        out[mid] = d
    return out


def cmd_freeze(args: argparse.Namespace) -> int:
    db = get_self_improvement_db()
    if db is None:
        print("未连接 MongoDB。")
        return 1
    years = _parse_years(args.years)
    if not years:
        print("--years 需要逗号分隔年份，例如 2025,2026")
        return 1

    ana = _load_finished_analysis_by_mid(db, years=years, mids=None)
    mids = list(ana.keys())
    pred = _load_latest_predictions_by_mid(db, mids=mids)

    frozen = sorted([mid for mid in mids if mid in pred and isinstance((pred[mid].get("prediction") or {}), dict)])
    payload = {
        "years": years,
        "frozen_count": len(frozen),
        "ids": frozen,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"冻结完成：{len(frozen)} 场，输出 {out_path}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    db = get_self_improvement_db()
    if db is None:
        print("未连接 MongoDB。")
        return 1

    ids_path = Path(args.ids)
    if not ids_path.exists():
        print(f"ids 文件不存在: {ids_path}")
        return 1
    payload = json.loads(ids_path.read_text(encoding="utf-8"))
    mids = [int(x) for x in (payload.get("ids") or []) if str(x).isdigit()]
    if not mids:
        print("ids 文件中没有有效的 sporttery_match_id。")
        return 1

    ana = _load_finished_analysis_by_mid(db, years=None, mids=mids)
    pred = _load_latest_predictions_by_mid(db, mids=mids)

    total = 0
    single_ok = 0
    outcome_ok = 0
    missing_pred = 0
    missing_finished = 0

    by_year = defaultdict(lambda: {"n": 0, "single_ok": 0, "outcome_ok": 0})

    for mid in mids:
        a = ana.get(mid)
        p = pred.get(mid)
        if a is None:
            missing_finished += 1
            continue
        if p is None or not isinstance((p.get("prediction") or {}), dict):
            missing_pred += 1
            continue
        total += 1
        pred_obj = p.get("prediction") or {}
        pred_label = _pred_label_from_prediction(pred_obj)
        if pred_label is None:
            continue
        actual_label = _winner(int(a["home_score"]), int(a["away_score"]))
        if pred_label == actual_label:
            outcome_ok += 1
            single_ok += 1  # 当前口径下单场推荐取三选一 argmax，等同于 outcome
        year = str((a.get("jc_date") or p.get("date") or ""))[:4]
        b = by_year[year]
        b["n"] += 1
        if pred_label == actual_label:
            b["outcome_ok"] += 1
            b["single_ok"] += 1

    single_acc = (single_ok / total) if total else 0.0
    outcome_acc = (outcome_ok / total) if total else 0.0

    report = {
        "ids_file": str(ids_path),
        "ids_count": len(mids),
        "n": total,
        "missing_pred": missing_pred,
        "missing_finished": missing_finished,
        "single_ok": single_ok,
        "single_acc": single_acc,
        "outcome_ok": outcome_ok,
        "outcome_acc": outcome_acc,
        "by_year": {},
    }

    print(f"固定样本评估：{ids_path}")
    print(f"  样本总数(ids): {len(mids)}")
    print(f"  有效评估场次: {total}")
    print(f"  缺预测: {missing_pred}  缺完赛: {missing_finished}")
    print(f"  单场推荐命中率: {single_ok}/{total} = {single_acc*100:.1f}%")
    print(f"  胜平负命中率: {outcome_ok}/{total} = {outcome_acc*100:.1f}%")
    print("  分年：")
    for y in sorted(by_year.keys()):
        b = by_year[y]
        n = b["n"] or 0
        s = (b["single_ok"] / n * 100) if n else 0
        o = (b["outcome_ok"] / n * 100) if n else 0
        print(f"    {y}: n={n}, single={s:.1f}%, outcome={o:.1f}%")
        report["by_year"][y] = {
            "n": n,
            "single_ok": b["single_ok"],
            "single_acc": (b["single_ok"] / n) if n else 0.0,
            "outcome_ok": b["outcome_ok"],
            "outcome_acc": (b["outcome_ok"] / n) if n else 0.0,
        }

    out_path = (getattr(args, "out", None) or "").strip()
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  已导出评估 JSON: {p}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    before_path = Path(args.before)
    after_path = Path(args.after)
    if not before_path.exists():
        print(f"before 文件不存在: {before_path}")
        return 1
    if not after_path.exists():
        print(f"after 文件不存在: {after_path}")
        return 1

    b = json.loads(before_path.read_text(encoding="utf-8"))
    a = json.loads(after_path.read_text(encoding="utf-8"))

    def _f(x: Any) -> float:
        try:
            return float(x)
        except Exception:
            return 0.0

    b_n, a_n = int(_f(b.get("n"))), int(_f(a.get("n")))
    b_s, a_s = _f(b.get("single_acc")), _f(a.get("single_acc"))
    b_o, a_o = _f(b.get("outcome_acc")), _f(a.get("outcome_acc"))
    ds = (a_s - b_s) * 100.0
    do = (a_o - b_o) * 100.0

    print("固定样本对比：")
    print(f"  before: {before_path}")
    print(f"  after : {after_path}")
    print(f"  场次 n: {b_n} -> {a_n} (Δ {a_n - b_n:+d})")
    print(f"  单场推荐: {b_s*100:.2f}% -> {a_s*100:.2f}% (Δ {ds:+.2f}pp)")
    print(f"  胜平负:   {b_o*100:.2f}% -> {a_o*100:.2f}% (Δ {do:+.2f}pp)")

    b_year = b.get("by_year") or {}
    a_year = a.get("by_year") or {}
    years = sorted(set(list(b_year.keys()) + list(a_year.keys())))
    if years:
        print("  分年差异：")
    for y in years:
        by = b_year.get(y) or {}
        ay = a_year.get(y) or {}
        bs = _f(by.get("single_acc")) * 100.0
        as_ = _f(ay.get("single_acc")) * 100.0
        bo = _f(by.get("outcome_acc")) * 100.0
        ao = _f(ay.get("outcome_acc")) * 100.0
        print(f"    {y}: single Δ {as_ - bs:+.2f}pp, outcome Δ {ao - bo:+.2f}pp")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="冻结样本ID版验收脚本")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_freeze = sub.add_parser("freeze", help="冻结样本ID（sporttery_match_id）")
    p_freeze.add_argument("--years", type=str, required=True, help="年份逗号分隔，如 2025,2026")
    p_freeze.add_argument("--out", type=str, required=True, help="输出 JSON 路径")
    p_freeze.set_defaults(run=cmd_freeze)

    p_eval = sub.add_parser("eval", help="按冻结样本做固定口径评估")
    p_eval.add_argument("--ids", type=str, required=True, help="freeze 输出的 JSON 文件")
    p_eval.add_argument("--out", type=str, default=None, help="可选，导出评估结果 JSON，供 compare 使用")
    p_eval.set_defaults(run=cmd_eval)

    p_compare = sub.add_parser("compare", help="对比两次固定样本评估 JSON（before/after）")
    p_compare.add_argument("--before", type=str, required=True, help="基线评估 JSON")
    p_compare.add_argument("--after", type=str, required=True, help="新参数评估 JSON")
    p_compare.set_defaults(run=cmd_compare)

    args = parser.parse_args()
    return int(args.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
