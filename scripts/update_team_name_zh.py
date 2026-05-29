#!/usr/bin/env python3
"""
根据 data/team_name_zh_mapping.json（英文名→中文名）批量更新 teams 的 name_zh。
支持 MongoDB 与 SQLite（由 config USE_MONGO 决定）。
用法：python scripts/update_team_name_zh.py  或  python -m scripts.update_team_name_zh
"""
from __future__ import annotations

import json
import sys
import re
import unicodedata
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from db import repo

MAPPING_PATH = config.DATA_DIR / "team_name_zh_mapping.json"

_DROP_TOKENS = {
    "fc", "cf", "sc", "afc", "cfc", "ac", "as", "ss",
    "club", "de", "the",
}


def _ascii_fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _norm_team_key(name: str) -> str:
    s = _ascii_fold((name or "").strip().lower().replace("&", " and "))
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    toks = [t for t in s.split() if t and t not in _DROP_TOKENS]
    return " ".join(toks)


def _resolve_name_zh(name: str, mapping: dict[str, str], norm_map: dict[str, list[tuple[str, str]]]) -> str | None:
    s = (name or "").strip()
    if not s:
        return None
    exact = mapping.get(s)
    if exact:
        return exact
    nk = _norm_team_key(s)
    cands = norm_map.get(nk) or []
    if len(cands) == 1:
        return cands[0][1]
    if len(cands) > 1:
        # 名称更长者通常信息更全（如 Brighton & Hove Albion FC）
        cands = sorted(cands, key=lambda kv: len(kv[0]), reverse=True)
        return cands[0][1]
    # 前缀兜底：例如 "Brighton" -> "Brighton & Hove Albion FC"
    low = _ascii_fold(s).lower()
    prefix = [(k, v) for k, v in mapping.items() if _ascii_fold(k).lower().startswith(low)]
    if len(prefix) == 1:
        return prefix[0][1]
    return None


def main() -> int:
    if not MAPPING_PATH.is_file():
        print(f"映射文件不存在: {MAPPING_PATH}", file=sys.stderr)
        print("请确保 data/team_name_zh_mapping.json 存在（英文名→中文名）。", file=sys.stderr)
        return 1

    with open(MAPPING_PATH, "r", encoding="utf-8") as f:
        mapping = json.load(f)
    norm_map: dict[str, list[tuple[str, str]]] = {}
    for en, zh in mapping.items():
        nk = _norm_team_key(en)
        if not nk:
            continue
        norm_map.setdefault(nk, []).append((en, zh))

    repo.init_db()
    if repo.use_mongo():
        print("当前使用 MongoDB，将更新 MongoDB 中 teams.name_zh")
    else:
        print("当前使用 SQLite，将更新 SQLite 中 teams.name_zh")

    teams = repo.list_all_teams()
    updated = 0
    for t in teams:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        name_zh = _resolve_name_zh(name, mapping, norm_map)
        if name_zh is None:
            continue
        tid = t.get("id")
        if tid is None:
            continue
        if repo.update_team_name_zh(tid, name_zh):
            updated += 1
            print(f"  {name} -> {name_zh}")

    print(f"共更新 {updated} 支球队的 name_zh（总球队数 {len(teams)}，映射表 {len(mapping)} 条）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
