#!/usr/bin/env python3
"""诊断：某比赛日 DB 中既有比赛又有伤停时，是否能匹配到（队名规范化是否一致）。"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _norm_team_key(name: str) -> str:
    """与 api/app._norm_team_key 一致，用于伤停匹配。"""
    s = (name or "").strip().lower().replace("&", " ")
    for old, new in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u"), ("ñ", "n")):
        s = s.replace(old, new)
    s = re.sub(r"[.''`-]", " ", s)
    drop_tokens = {
        "fc", "cf", "afc", "sc", "cfc", "ac", "us",
        "club", "de", "del", "la", "los", "las",
        "futbol", "futebol", "football", "calcio",
    }
    tokens = [t for t in re.split(r"\s+", s) if t and t not in drop_tokens]
    if tokens[-2:] == ["hove", "albion"]:
        tokens = tokens[:-2]
    elif tokens[-1:] == ["albion"]:
        tokens = tokens[:-1]
    return "".join(tokens)


def main() -> None:
    date_str = (sys.argv[1] if len(sys.argv) > 1 else "").strip()[:10]
    if len(date_str) != 10:
        print("用法: python3 scripts/check_injury_match_for_date.py YYYY-MM-DD  （传入比赛日，即竞彩日+1）")
        print("示例: python3 scripts/check_injury_match_for_date.py 2024-01-14")
        sys.exit(1)
    try:
        from db import repo
        repo.init_db()
        from etl.injury_db import get_injury_matches_from_db
    except Exception as e:
        print(f"导入失败: {e}")
        sys.exit(1)

    store = "Mongo" if repo.use_mongo() else "SQLite"
    print(f"比赛日 = {date_str}，数据源 = {store}\n")

    # 1) 该日比赛（仪表盘展示的场次）
    matches = repo.get_db_matches_for_date_with_scores(date_str)
    print(f"【该日比赛】共 {len(matches)} 场")
    for i, m in enumerate(matches, 1):
        h, a = (m.get("home_name") or "").strip(), (m.get("away_name") or "").strip()
        league = (m.get("league_cn") or "").strip()
        print(f"  {i}. {h} vs {a}  ({league})")

    # 2) 该日伤停记录
    all_injury = get_injury_matches_from_db()
    on_date = [x for x in all_injury if (x.get("fixture_date") or "")[:10] == date_str]
    print(f"\n【该日伤停】共 {len(on_date)} 条")
    injury_by_key = {}
    for m in on_date:
        home = (m.get("home") or "").strip()
        away = (m.get("away") or "").strip()
        league = (m.get("league") or "").strip()
        key = (_norm_team_key(home), _norm_team_key(away), league, date_str)
        injury_by_key[key] = m
        ha, aa = len(m.get("home_absent") or []), len(m.get("away_absent") or [])
        print(f"  - {home} vs {away}  ({league})  主缺 {ha} 人，客缺 {aa} 人")

    if not matches:
        print("\n该日无比赛记录，无需匹配。")
        return
    if not on_date:
        print("\n该日无伤停记录。仪表盘会显示「暂无」；可执行: python3 main.py injury-api --date " + date_str + " --merge")
        return

    # 3) 逐场检查能否匹配
    print("\n【匹配结果】")
    comp_to_league = {"Premier League": "英超", "La Liga": "西甲", "Serie A": "意甲", "Bundesliga": "德甲", "Ligue 1": "法甲"}
    for m in matches:
        home = (m.get("home_name") or "").strip()
        away = (m.get("away_name") or "").strip()
        league = (m.get("league_cn") or "").strip()
        nh, na = _norm_team_key(home), _norm_team_key(away)
        key = (nh, na, league, date_str)
        inj = injury_by_key.get(key)
        if not inj and (nh or na):
            for k, v in injury_by_key.items():
                if k[2] != league:
                    continue
                if (k[0] in nh or nh in k[0]) and (k[1] in na or na in k[1]):
                    inj = v
                    break
        if inj:
            print(f"  ✓ {home} vs {away}  → 已匹配伤停")
        else:
            print(f"  ✗ {home} vs {away}  → 未匹配（伤停表内队名或联赛与比赛不一致时可运行: python3 main.py normalize-team-names）")


if __name__ == "__main__":
    main()
