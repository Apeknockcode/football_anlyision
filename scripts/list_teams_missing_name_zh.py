#!/usr/bin/env python3
"""
列出 name_zh 为 null 或空的球队（输出英文 name），便于补全 data/team_name_zh_mapping.json。
用法：python scripts/list_teams_missing_name_zh.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from db import repo

def main() -> int:
    repo.init_db()
    teams = repo.list_all_teams()
    missing = [
        (t.get("id"), (t.get("name") or "").strip())
        for t in teams
        if not (t.get("name_zh") or "").strip()
    ]
    missing = [(tid, name) for tid, name in missing if name]

    print(f"共 {len(teams)} 支球队，其中 name_zh 为空的有 {len(missing)} 支：\n")
    for tid, name in sorted(missing, key=lambda x: x[1].lower()):
        print(f"  {tid}: {name!r}")

    mapping_path = config.DATA_DIR / "team_name_zh_mapping.json"
    if mapping_path.is_file():
        with open(mapping_path, "r", encoding="utf-8") as f:
            mapping = json.load(f)
        mapping_keys_lower = {k.strip().lower(): k for k in mapping}
        print("\n以下在映射表中没有精确匹配（可加入 team_name_zh_mapping.json）：")
        for tid, name in sorted(missing, key=lambda x: x[1].lower()):
            if name.lower() not in mapping_keys_lower:
                print(f"  {name!r}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
