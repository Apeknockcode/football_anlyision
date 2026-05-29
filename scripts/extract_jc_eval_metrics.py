#!/usr/bin/env python3
"""从 refresh-and-eval / jc-predictions-eval 的完整日志中提取主指标（取文件中最后一次匹配）。"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 当前 jc-predictions-eval 输出格式
RE_SINGLE = re.compile(r"【主指标】单场推荐猜对：(\d+)/(\d+)，命中率 ([\d.]+)%")
RE_OUTCOME = re.compile(r"胜平负猜对：(\d+)/(\d+)，命中率 ([\d.]+)%")
# 旧格式兼容
RE_SINGLE_ALT = re.compile(r"单场推荐命中[：:]\s*(\d+)/(\d+)[，,]\s*命中率\s*([\d.]+)%")


def extract_metrics(text: str) -> dict[str, str | float | None]:
    ms = list(RE_SINGLE.finditer(text))
    if not ms:
        ms = list(RE_SINGLE_ALT.finditer(text))
    mo = list(RE_OUTCOME.finditer(text))
    out: dict[str, str | float | None] = {
        "single_hit": None,
        "single_n": None,
        "single_pct": None,
        "outcome_hit": None,
        "outcome_n": None,
        "outcome_pct": None,
    }
    if ms:
        m = ms[-1]
        out["single_hit"] = m.group(1)
        out["single_n"] = m.group(2)
        out["single_pct"] = float(m.group(3))
    if mo:
        m = mo[-1]
        out["outcome_hit"] = m.group(1)
        out["outcome_n"] = m.group(2)
        out["outcome_pct"] = float(m.group(3))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="从评估日志提取单场推荐 / 胜平负命中率")
    p.add_argument("log_file", type=Path, help="日志文件路径")
    p.add_argument("--ml-weight", type=str, default="", metavar="W", help="若给定，输出 TSV 首列写入该权重")
    args = p.parse_args()
    if not args.log_file.is_file():
        print("文件不存在:", args.log_file, file=sys.stderr)
        return 1
    text = args.log_file.read_text(encoding="utf-8", errors="replace")
    m = extract_metrics(text)
    w = args.ml_weight if args.ml_weight is not None else ""
    if m["single_pct"] is None and m["outcome_pct"] is None:
        print("未找到主指标行（需完整 refresh-and-eval 或 jc-predictions-eval 输出）", file=sys.stderr)
        return 1
    print(
        f"{w}\t{m['single_hit'] or ''}\t{m['single_n'] or ''}\t{m['single_pct'] or ''}\t"
        f"{m['outcome_hit'] or ''}\t{m['outcome_n'] or ''}\t{m['outcome_pct'] or ''}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
