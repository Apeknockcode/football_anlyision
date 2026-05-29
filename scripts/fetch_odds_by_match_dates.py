#!/usr/bin/env python3
"""
按 DB 中 matches.match_date 去重得到的日期列表，循环调用 API-FOOTBALL 赔率拉取并 merge 到 data/api_football_odds.json。
用法:
  python scripts/fetch_odds_by_match_dates.py
  python scripts/fetch_odds_by_match_dates.py --years 2024,2025,2026   # 只拉 2024/2025/2026 年的比赛日
  python scripts/fetch_odds_by_match_dates.py --by-db   # 按 DB 比赛拉取：只拉 DB 中有的场次（推荐，避免 API 某日返回空导致 0 场）
  python scripts/fetch_odds_by_match_dates.py --limit 10   # 只处理最近 10 个日期
  python scripts/fetch_odds_by_match_dates.py --dry-run    # 只打印日期，不拉取
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 项目根目录加入 path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from sqlalchemy import text

from etl.api_football_odds import RATE_LIMIT_SEC, fetch_odds_for_date, fetch_odds_for_date_from_db
from etl.schema import get_engine


def get_distinct_match_dates(
    limit: int | None = None,
    years: list[int] | None = None,
) -> list[str]:
    """
    从 DB matches 表取不重复的 match_date（归一化为 YYYY-MM-DD），按日期升序。
    years: 若指定则只保留该年份的日期，如 [2024, 2025, 2026] 表示只取 2024/2025/2026 年的比赛日。
    """
    engine = get_engine()
    # SQLite: date() 把 datetime 转为 YYYY-MM-DD；若已是日期字符串则保持不变
    q = """
        SELECT DISTINCT date(match_date) AS d
        FROM matches
        WHERE match_date IS NOT NULL AND trim(match_date) != ''
        ORDER BY d
    """
    with engine.connect() as conn:
        rows = conn.execute(text(q)).fetchall()
    dates = [str(r[0])[:10] for r in rows if r[0]]
    # 过滤掉无效日期（非 YYYY-MM-DD）
    dates = [d for d in dates if len(d) == 10 and d[4] == "-" and d[7] == "-"]
    if years:
        year_set = set(years)
        dates = [d for d in dates if int(d[:4]) in year_set]
    if limit is not None and limit > 0:
        dates = dates[-limit:]  # 最近 limit 个日期
    return dates


def _parse_years(years_str: str | None) -> list[int] | None:
    """解析 --years 2024,2025,2026 为 [2024, 2025, 2026]，空则返回 None（表示不过滤）。"""
    if not (years_str or "").strip():
        return None
    out = []
    for s in years_str.strip().split(","):
        s = s.strip()
        if not s:
            continue
        try:
            out.append(int(s))
        except ValueError:
            continue
    return out if out else None


def main() -> int:
    parser = argparse.ArgumentParser(description="按 DB match_date 去重日期拉取赔率并 merge")
    parser.add_argument("--years", type=str, default=None, metavar="YEAR[,YEAR...]", help="只处理这些年份的比赛日，如 2024,2025,2026；不传则全部")
    parser.add_argument("--by-db", action="store_true", help="按 DB 比赛拉取：每日期先查 DB 该日五大联赛场次，再与 API 赛程匹配后只拉这些场次的赔率（推荐）")
    parser.add_argument("--limit", type=int, default=None, help="只处理最近 N 个日期（默认全部）")
    parser.add_argument("--dry-run", action="store_true", help="只打印日期列表，不请求 API")
    parser.add_argument("--rate-limit", type=float, default=None, help=f"请求间隔秒数（默认 {RATE_LIMIT_SEC}）")
    args = parser.parse_args()

    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY。", file=sys.stderr)
        return 1

    years = _parse_years(args.years)
    dates = get_distinct_match_dates(limit=args.limit, years=years)
    if not dates:
        print("DB 中无有效 match_date，无需拉取。")
        return 0

    print(f"共 {len(dates)} 个日期: {dates[0]} ~ {dates[-1]}")
    if args.dry_run:
        for d in dates:
            print(d)
        return 0

    rate = args.rate_limit if args.rate_limit is not None else RATE_LIMIT_SEC
    fetch_fn = fetch_odds_for_date_from_db if args.by_db else fetch_odds_for_date
    ok = 0
    for i, date_str in enumerate(dates, 1):
        try:
            _, msg = fetch_fn(date_str, rate_limit_sec=rate, merge=True)
            print(f"[{i}/{len(dates)}] {msg}")
            ok += 1
        except Exception as e:
            print(f"[{i}/{len(dates)}] {date_str} 失败: {e}", file=sys.stderr)
    print(f"完成: {ok}/{len(dates)} 个日期已拉取并 merge。")
    return 0 if ok == len(dates) else 1


if __name__ == "__main__":
    sys.exit(main())
