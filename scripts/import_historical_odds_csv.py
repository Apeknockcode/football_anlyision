#!/usr/bin/env python3
"""
将历史赔率 CSV（如 Football-Data.co.uk 下载的）合并进 data/api_football_odds.json。
格式要求：CSV 含主队、客队、日期、以及主胜/平/客胜赔率列（列名可指定）。
用法:
  python3 scripts/import_historical_odds_csv.py --csv data/england_e0_2324.csv --league-cn 英超
  python3 scripts/import_historical_odds_csv.py --csv data/odds.csv --league-cn 西甲 --date-col Date --home-col Home --away-col Away --win-col B365H --draw-col B365D --loss-col B365A
Football-Data.co.uk 列名示例：Date, HomeTeam, AwayTeam, B365H, B365D, B365A（或 PSCH, PSCD, PSCA 等）。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config

ODDS_JSON_PATH = config.DATA_DIR / "api_football_odds.json"


def _normalize_date(s: str) -> str | None:
    """DD/MM/YYYY 或 YYYY-MM-DD -> YYYY-MM-DD。"""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    # YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    # DD/MM/YYYY (football-data.co.uk)
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


def load_csv_entries(
    csv_path: Path,
    league_cn: str,
    date_col: str = "Date",
    home_col: str = "HomeTeam",
    away_col: str = "AwayTeam",
    win_col: str = "B365H",
    draw_col: str = "B365D",
    loss_col: str = "B365A",
) -> list[dict]:
    """从 CSV 读入，返回与 api_football_odds entries 同结构的列表。"""
    entries = []
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return entries
        # 列名大小写不敏感匹配
        upper_names = {k.upper(): k for k in reader.fieldnames}
        def get_col(name: str) -> str | None:
            return upper_names.get(name.upper()) if name else None

        dcol = get_col(date_col) or get_col("Date")
        hcol = get_col(home_col) or get_col("HomeTeam")
        acol = get_col(away_col) or get_col("AwayTeam")
        wcol = get_col(win_col) or get_col("B365H") or get_col("PSCH")
        ddraw = get_col(draw_col) or get_col("B365D") or get_col("PSCD")
        lcol = get_col(loss_col) or get_col("B365A") or get_col("PSCA")

        if not all([dcol, hcol, acol]):
            print(f"缺少必要列：Date/HomeTeam/AwayTeam 或你指定的列名。表头: {reader.fieldnames}", file=sys.stderr)
            return entries
        if not any([wcol, ddraw, lcol]):
            for cand in ["B365H", "B365D", "B365A", "PSCH", "PSCD", "PSCA", "WHH", "WHD", "WHA"]:
                if get_col(cand):
                    if not wcol:
                        wcol = get_col("B365H") or get_col("PSCH") or get_col("WHH")
                    if not ddraw:
                        ddraw = get_col("B365D") or get_col("PSCD") or get_col("WHD")
                    if not lcol:
                        lcol = get_col("B365A") or get_col("PSCA") or get_col("WHA")
                    break
        if not all([wcol, ddraw, lcol]):
            print(f"未找到赔率列（B365H/B365D/B365A 或 PSCH/PSCD/PSCA 等）。表头: {reader.fieldnames}", file=sys.stderr)
            return entries

        for row in reader:
            date_str = _normalize_date(row.get(dcol, ""))
            if not date_str:
                continue
            home = (row.get(hcol) or "").strip()
            away = (row.get(acol) or "").strip()
            if not home and not away:
                continue
            try:
                win = str(float(row.get(wcol, ""))).strip() if row.get(wcol) else ""
                draw = str(float(row.get(ddraw, ""))).strip() if row.get(ddraw) else ""
                loss = str(float(row.get(lcol, ""))).strip() if row.get(lcol) else ""
            except (ValueError, TypeError):
                continue
            if not (win and draw and loss):
                continue
            entries.append({
                "home": home,
                "away": away,
                "date": date_str,
                "league_cn": league_cn,
                "spf_win": win,
                "spf_draw": draw,
                "spf_loss": loss,
            })
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description="将历史赔率 CSV 合并进 api_football_odds.json")
    parser.add_argument("--csv", required=True, type=Path, help="CSV 文件路径（如 Football-Data.co.uk 下载）")
    parser.add_argument("--league-cn", required=True, choices=["英超", "西甲", "意甲", "德甲", "法甲"], help="联赛中文名")
    parser.add_argument("--date-col", default="Date", help="日期列名（默认 Date）")
    parser.add_argument("--home-col", default="HomeTeam", help="主队列名")
    parser.add_argument("--away-col", default="AwayTeam", help="客队列名")
    parser.add_argument("--win-col", default="B365H", help="主胜赔率列（默认 B365H）")
    parser.add_argument("--draw-col", default="B365D", help="平局赔率列")
    parser.add_argument("--loss-col", default="B365A", help="客胜赔率列")
    parser.add_argument("--dry-run", action="store_true", help="只打印将导入的条数，不写入")
    args = parser.parse_args()

    if not args.csv.is_file():
        print(f"文件不存在: {args.csv}", file=sys.stderr)
        return 1

    entries = load_csv_entries(
        args.csv,
        args.league_cn,
        date_col=args.date_col,
        home_col=args.home_col,
        away_col=args.away_col,
        win_col=args.win_col,
        draw_col=args.draw_col,
        loss_col=args.loss_col,
    )
    if not entries:
        print("未解析到任何有效行。")
        return 0

    print(f"从 CSV 解析到 {len(entries)} 条（联赛：{args.league_cn}），日期范围: {entries[0]['date']} ~ {entries[-1]['date']}")
    if args.dry_run:
        return 0

    # 与现有 JSON 合并
    existing_entries: list[dict] = []
    if ODDS_JSON_PATH.is_file():
        try:
            data = json.loads(ODDS_JSON_PATH.read_text(encoding="utf-8"))
            existing_entries = list(data.get("entries") or [])
        except Exception:
            pass

    key_to_entry: dict[tuple[str, str, str, str], dict] = {}
    for e in existing_entries:
        if e.get("league_cn") and (e.get("home") or e.get("away")):
            k = (e.get("league_cn"), (e.get("home") or "").strip(), (e.get("away") or "").strip(), (e.get("date") or "")[:10])
            key_to_entry[k] = e
    for e in entries:
        k = (e["league_cn"], e["home"].strip(), e["away"].strip(), e["date"][:10])
        key_to_entry[k] = e

    merged = list(key_to_entry.values())
    out = {
        "updated_at": datetime.now().isoformat(),
        "source": "api_football",
        "source_url": (getattr(config, "API_FOOTBALL_BASE", "") or "").rstrip("/") + "/odds",
        "entries": merged,
    }
    ODDS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    ODDS_JSON_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已合并写入 {ODDS_JSON_PATH}，共 {len(merged)} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
