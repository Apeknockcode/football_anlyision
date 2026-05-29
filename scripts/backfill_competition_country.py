#!/usr/bin/env python3
"""
根据赛事名称回填 competitions.country_name（MongoDB 或 SQLite 由配置决定）。
用法：python scripts/backfill_competition_country.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import repo

# 赛事名称 -> 国家（用于回填空 country_name）
COMP_NAME_TO_COUNTRY: dict[str, str] = {
    "Premier League": "England",
    "La Liga": "Spain",
    "Serie A": "Italy",
    "Bundesliga": "Germany",
    "1. Bundesliga": "Germany",
    "Ligue 1": "France",
    "Primera Division": "Spain",
    "Primera División": "Spain",
}


def main() -> int:
    repo.init_db()
    if repo.use_mongo():
        print("当前使用 MongoDB，将回填 competitions.country_name")
    else:
        print("当前使用 SQLite，将回填 competitions.country_name")

    comps = repo.list_competitions()
    updated = 0
    for c in comps:
        cid = c.get("id")
        name = (c.get("name") or "").strip()
        country = (c.get("country_name") or "").strip()
        if not name or cid is None:
            continue
        if country:
            continue
        fill = COMP_NAME_TO_COUNTRY.get(name)
        if not fill:
            continue
        if repo.update_competition_country(cid, fill):
            updated += 1
            print(f"  {name} -> {fill}")

    print(f"共回填 {updated} 条 country_name（总赛事数 {len(comps)}）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
