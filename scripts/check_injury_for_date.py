#!/usr/bin/env python3
"""列出指定日期的伤停记录（DB 中的主客队、联赛），用于核对仪表盘为何某场无伤停。"""
import sys
from pathlib import Path

# 项目根
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    date_str = (sys.argv[1] if len(sys.argv) > 1 else "").strip()[:10]
    if len(date_str) != 10:
        print("用法: python3 scripts/check_injury_for_date.py YYYY-MM-DD")
        print("示例: python3 scripts/check_injury_for_date.py 2024-01-02")
        sys.exit(1)
    try:
        from etl.injury_db import get_injury_matches_from_db
        from db import repo
    except Exception as e:
        print(f"导入失败: {e}")
        sys.exit(1)
    store = "Mongo" if repo.use_mongo() else "SQLite"
    print(f"从 {store} 读取 match_injury，比赛日 = {date_str}\n")
    all_matches = get_injury_matches_from_db()
    on_date = [m for m in all_matches if (m.get("fixture_date") or "")[:10] == date_str]
    print(f"该日共 {len(on_date)} 场伤停记录：")
    for i, m in enumerate(on_date, 1):
        home = (m.get("home") or "").strip()
        away = (m.get("away") or "").strip()
        league = (m.get("league") or "").strip()
        ha = len(m.get("home_absent") or [])
        aa = len(m.get("away_absent") or [])
        print(f"  {i}. {home} vs {away}  ({league})  主队缺阵 {ha} 人，客队缺阵 {aa} 人")
    if not on_date:
        print("  （无）")
        print(f"\n全部伤停记录共 {len(all_matches)} 场；若仪表盘该日有多场无伤停，请核对上列队名与 matches/teams 中的名称是否一致。")


if __name__ == "__main__":
    main()
