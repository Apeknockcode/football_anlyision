#!/usr/bin/env python3
"""从两次 jc-predictions-eval --all-time 的完整输出中提取关键指标并对比。"""
import re
import sys
from pathlib import Path

def extract_metrics(text: str) -> dict:
    m = {}
    # 胜平负猜对：9044/17909，命中率 50.5%
    r = re.search(r"胜平负猜对[：:]\s*(\d+)/(\d+)[，,]\s*命中率\s*([\d.]+)%", text)
    if r:
        m["outcome_hit"], m["outcome_total"], m["outcome_pct"] = r.group(1), r.group(2), float(r.group(3))
    # 单场推荐命中：9464/17909，命中率 52.8%
    r = re.search(r"单场推荐命中[：:]\s*(\d+)/(\d+)[，,]\s*命中率\s*([\d.]+)%", text)
    if r:
        m["rec_hit"], m["rec_total"], m["rec_pct"] = r.group(1), r.group(2), float(r.group(3))
    # 高置信度（推荐概率≥45%）：6034/10522，命中率 57.3%
    r = re.search(r"高置信度[^：:]*[：:]\s*(\d+)/(\d+)[，,]\s*命中率\s*([\d.]+)%", text)
    if r:
        m["high_conf_hit"], m["high_conf_total"], m["high_conf_pct"] = r.group(1), r.group(2), float(r.group(3))
    # 推荐概率≥60%（取「为接近 70% 目标」区块内那条，即条件命中率）
    all_p60 = re.findall(
        r"推荐概率≥60%[^：:]*[：:]\s*共\s*(\d+)\s*场[，,]\s*命中\s*(\d+)\s*场[，,]\s*命中率\s*([\d.]+)%",
        text,
    )
    if len(all_p60) >= 2:
        # 第二条为「仅统计推荐≥60%的场次」的条件命中率
        m["p60_total"], m["p60_hit"], m["p60_pct"] = all_p60[1][0], all_p60[1][1], float(all_p60[1][2])
    elif all_p60:
        m["p60_total"], m["p60_hit"], m["p60_pct"] = all_p60[0][0], all_p60[0][1], float(all_p60[0][2])
    # 精确比分命中：2076/17909，命中率 11.6%
    r = re.search(r"精确比分命中[：:]\s*(\d+)/(\d+)[，,]\s*命中率\s*([\d.]+)%", text)
    if r:
        m["exact_hit"], m["exact_total"], m["exact_pct"] = r.group(1), r.group(2), float(r.group(3))
    # 按模型预测比分+让球数推导：8866/17868，命中率 49.6%
    r = re.search(r"按模型预测比分\+让球数推导[：:]\s*(\d+)/(\d+)[，,]\s*命中率\s*([\d.]+)%", text)
    if r:
        m["rq_model_hit"], m["rq_model_total"], m["rq_model_pct"] = r.group(1), r.group(2), float(r.group(3))
    return m

def main():
    root = Path(__file__).resolve().parent.parent
    old_path = root / "data" / "eval_old_model.txt"
    new_path = root / "data" / "eval_new_model.txt"
    if not old_path.exists() or not new_path.exists():
        print("请先运行：OLD_JC_PREDICT_PARAMS=1 与 默认 各跑一次 predict-jc + jc-predictions-eval，并保存到 data/eval_old_model.txt 与 data/eval_new_model.txt", file=sys.stderr)
        sys.exit(1)
    old_text = old_path.read_text(encoding="utf-8")
    new_text = new_path.read_text(encoding="utf-8")
    old_m = extract_metrics(old_text)
    new_m = extract_metrics(new_text)
    print("=" * 60)
    print("竞彩预测评估对比（旧参数 vs 新参数）")
    print("旧参数: KEY_WEIGHT=2.0, 预计首发不参与 key_count")
    print("新参数: KEY_WEIGHT=3.0, 预计首发参与 key_count")
    print("=" * 60)
    for key, label in [
        ("outcome_pct", "胜平负命中率(%)"),
        ("rec_pct", "单场推荐命中率(%)"),
        ("high_conf_pct", "高置信(≥45%)命中率(%)"),
        ("p60_pct", "推荐≥60%命中率(%)"),
        ("exact_pct", "精确比分命中率(%)"),
        ("rq_model_pct", "让球模型推导命中率(%)"),
    ]:
        o = old_m.get(key)
        n = new_m.get(key)
        if o is not None and n is not None:
            diff = n - o
            sign = "↑" if diff > 0 else ("↓" if diff < 0 else "=")
            print(f"  {label}:  旧 {o:.1f}%  新 {n:.1f}%  {sign} {diff:+.1f}%")
        elif o is not None or n is not None:
            print(f"  {label}:  旧 {o}  新 {n}")
    print("=" * 60)

if __name__ == "__main__":
    main()
