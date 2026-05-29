#!/usr/bin/env python3
"""对指定竞彩日补全 fixture_id（已有则跳过）并同步 API 预测。用法: python scripts/backfill_and_sync_jc_date.py 2026-03-01"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.self_improvement import (
    get_self_improvement_db,
    jc_match_analysis_backfill_fixture_id_for_jc_date,
    jc_api_predictions_sync_from_analysis,
)

def main():
    day = (sys.argv[1] if len(sys.argv) > 1 else "").strip()[:10]
    if not day or len(day) != 10:
        print("用法: python scripts/backfill_and_sync_jc_date.py YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    if get_self_improvement_db() is None:
        print("MONGO_URI 未配置", file=sys.stderr)
        sys.exit(1)
    print(f"竞彩日 {day}: 先补全 fixture_id（已有则跳过），再同步 API 预测…", flush=True)
    r = jc_match_analysis_backfill_fixture_id_for_jc_date(day, rate_limit_sec=1.0, overwrite_existing=False)
    print("backfill_fixture_id:", r, flush=True)
    s = jc_api_predictions_sync_from_analysis(rate_limit_sec=0.2, date=day, overwrite_existing=True)
    print("sync_api_predictions:", s, flush=True)
    print("完成.", flush=True)

if __name__ == "__main__":
    main()
