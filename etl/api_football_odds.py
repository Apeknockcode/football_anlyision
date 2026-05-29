"""
API-FOOTBALL 赔率拉取：按日期或 fixture 拉取胜平负赔率，写入 data/api_football_odds.json，供预测多源赔率融合。
支持两种模式：按 API 日期拉全量（原逻辑）/ 按 DB 比赛拉取（以 DB 为基准，只拉 DB 中有的场次）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from etl.injury_api_football import LEAGUE_IDS, _request

ODDS_JSON_PATH: Path = config.DATA_DIR / "api_football_odds.json"
RATE_LIMIT_SEC = getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)

# DB 中 competition 名称（football-data.org 等）到五大联赛中文名的映射，用于「按 DB 比赛拉赔率」
DB_COMPETITION_TO_LEAGUE_CN: dict[str, str] = {
    "Premier League": "英超",
    "La Liga": "西甲",
    "Primera Division": "西甲",
    "Primera División": "西甲",
    "Serie A": "意甲",
    "Bundesliga": "德甲",
    "1. Bundesliga": "德甲",
    "Ligue 1": "法甲",
}


def _fixtures_with_id(date_str: str) -> list[dict[str, Any]]:
    """拉取指定日期的五大联赛赛程，含 fixture_id。使用 timezone 使「日期」按欧洲当地比赛日对齐。"""
    date_only = (date_str or "")[:10]
    if len(date_only) != 10:
        return []
    params: dict[str, str | int] = {"date": date_only}
    tz = getattr(config, "API_FOOTBALL_TIMEZONE", None) or "Europe/London"
    if tz:
        params["timezone"] = tz
    data = _request("fixtures", params)
    rows = data.get("response") or []
    out = []
    for item in rows:
        league = item.get("league") or {}
        lid = league.get("id")
        if lid not in LEAGUE_IDS:
            continue
        fix = item.get("fixture") or {}
        fid = fix.get("id")
        if not fid:
            continue
        teams = item.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        home_name = (home.get("name") or "").strip()
        away_name = (away.get("name") or "").strip()
        if not home_name and not away_name:
            continue
        fix_date_raw = (fix.get("date") or "").strip()
        fixture_date = fix_date_raw[:10] if len(fix_date_raw) >= 10 else date_only
        out.append({
            "fixture_id": fid,
            "league_id": lid,
            "league_cn": LEAGUE_IDS[lid],
            "home_name": home_name,
            "away_name": away_name,
            "fixture_date": fixture_date,
        })
    return out


def fixtures_with_id_all_leagues(
    date_str: str,
    quiet: bool = False,
    timezone: str | None = None,
) -> list[dict[str, Any]]:
    """拉取指定日期的全部联赛赛程（不筛联赛、不限于五大联赛），含 fixture_id。用于竞彩场次按主客队英文名匹配 API Football。
    timezone: 指定时区使「日期」按该时区归日；None 则用 config.API_FOOTBALL_TIMEZONE。竞彩日建议传 Asia/Shanghai。"""
    date_only = (date_str or "")[:10]
    if len(date_only) != 10:
        return []
    params: dict[str, str | int] = {"date": date_only}
    tz = timezone if timezone is not None else (getattr(config, "API_FOOTBALL_TIMEZONE", None) or "Europe/London")
    if tz:
        params["timezone"] = tz
    data = _request("fixtures", params, quiet=quiet)
    rows = data.get("response") or []
    out = []
    for item in rows:
        league = item.get("league") or {}
        lid = league.get("id")
        fix = item.get("fixture") or {}
        fid = fix.get("id")
        if not fid:
            continue
        teams = item.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        home_name = (home.get("name") or "").strip()
        away_name = (away.get("name") or "").strip()
        if not home_name and not away_name:
            continue
        fix_date_raw = (fix.get("date") or "").strip()
        fixture_date = fix_date_raw[:10] if len(fix_date_raw) >= 10 else date_only
        out.append({
            "fixture_id": fid,
            "league_id": lid,
            "league_cn": LEAGUE_IDS.get(lid, ""),
            "home_name": home_name,
            "away_name": away_name,
            "fixture_date": fixture_date,
        })
    return out


def fixtures_with_id_by_league_season(
    league_id: int,
    season: int,
    quiet: bool = False,
) -> list[dict[str, Any]]:
    """按联赛+赛季拉取该赛季全部赛程（含 fixture_id）。用于历史日期按日拉不到时的回退匹配（如 2022 年）。"""
    data = _request("fixtures", {"league": league_id, "season": season}, quiet=quiet)
    rows = data.get("response") or []
    out = []
    for item in rows:
        fix = item.get("fixture") or {}
        fid = fix.get("id")
        if not fid:
            continue
        teams = item.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        home_name = (home.get("name") or "").strip()
        away_name = (away.get("name") or "").strip()
        if not home_name and not away_name:
            continue
        fix_date_raw = (fix.get("date") or "").strip()
        fixture_date = fix_date_raw[:10] if len(fix_date_raw) >= 10 else ""
        out.append({
            "fixture_id": fid,
            "league_id": league_id,
            "league_cn": LEAGUE_IDS.get(league_id, ""),
            "home_name": home_name,
            "away_name": away_name,
            "fixture_date": fixture_date,
        })
    return out


def _season_year_from_date(date_str: str) -> int:
    """从日期 YYYY-MM-DD 得到欧洲联赛赛季年（8 月开赛）。"""
    try:
        y, m, _ = (date_str or "")[:10].split("-")
        year, month = int(y), int(m)
        return year if month >= 8 else year - 1
    except Exception:
        return datetime.now().year


def _parse_asian_handicap_from_bet(bet: dict) -> dict[str, str] | None:
    """
    从单条 bet（Asian Handicap）解析让球与主客赔率。
    返回 {"ah_handicap": "-0.5", "ah_home": "1.90", "ah_away": "1.95"} 或 None。
    API 常见 value 格式：Home -0.5 / Away +0.5，或 Home 0 / Away 0 等。
    """
    values = bet.get("values") or []
    if len(values) < 2:
        return None
    home_odd = away_odd = handicap_str = None
    for v in values:
        val = (v.get("value") or "").strip()
        odd = v.get("odd") or v.get("odds")
        if odd is None:
            continue
        odd_str = str(odd).strip()
        if not odd_str:
            continue
        val_lower = val.lower()
        if "home" in val_lower or val_lower.startswith("1"):
            home_odd = odd_str
            # 从 "Home -0.5" / "1 -0.5" 等提取让球数
            parts = val.replace("Home", "").replace("Away", "").replace("1", "").replace("2", "").strip().split()
            if parts:
                handicap_str = parts[0]
        elif "away" in val_lower or val_lower.startswith("2"):
            away_odd = odd_str
            if handicap_str is None:
                parts = val.replace("Home", "").replace("Away", "").replace("1", "").replace("2", "").strip().split()
                if parts:
                    handicap_str = parts[0]
    if home_odd and away_odd:
        return {
            "ah_handicap": (handicap_str or "0").strip(),
            "ah_home": home_odd,
            "ah_away": away_odd,
        }
    return None


def _parse_odds_from_response(data: dict) -> dict[str, Any] | None:
    """
    从 API-Football odds 单条 response 解析胜平负赔率，可选亚赔。
    返回 {"spf_win", "spf_draw", "spf_loss", "ah_handicap", "ah_home", "ah_away"?} 或 None。
    """
    bookmakers = data.get("bookmakers") or []
    out: dict[str, Any] = {}
    for bm in bookmakers:
        bets = bm.get("bets") or []
        for bet in bets:
            name = (bet.get("name") or "").strip().lower()
            if name in ("match winner", "home/draw/away", "1x2", "full time result"):
                values = bet.get("values") or []
                win_val = draw_val = loss_val = None
                for v in values:
                    val = (v.get("value") or "").strip().lower()
                    odd = v.get("odd") or v.get("odds")
                    if odd is None:
                        continue
                    odd_str = str(odd).strip()
                    if not odd_str:
                        continue
                    if val in ("home", "1", "win"):
                        win_val = odd_str
                    elif val in ("draw", "x", "tie"):
                        draw_val = odd_str
                    elif val in ("away", "2", "loss"):
                        loss_val = odd_str
                if win_val and draw_val and loss_val:
                    out["spf_win"] = win_val
                    out["spf_draw"] = draw_val
                    out["spf_loss"] = loss_val
                    break
            elif "asian" in name or "handicap" in name:
                ah = _parse_asian_handicap_from_bet(bet)
                if ah and not out.get("ah_home"):
                    out["ah_handicap"] = ah.get("ah_handicap", "")
                    out["ah_home"] = ah.get("ah_home", "")
                    out["ah_away"] = ah.get("ah_away", "")
        if out.get("spf_win"):
            break
    return out if out.get("spf_win") else None


def fetch_odds_for_fixture(fixture_id: int, rate_limit_sec: float = RATE_LIMIT_SEC) -> dict[str, Any] | None:
    """拉取单场赔率，返回 {spf_win, spf_draw, spf_loss, ah_handicap?, ah_home?, ah_away?} 或 None。"""
    data = _request("odds", {"fixture": fixture_id})
    time.sleep(rate_limit_sec)
    rows = data.get("response") or []
    if not rows:
        return None
    return _parse_odds_from_response(rows[0])


def fetch_odds_by_date_league_season(
    date_str: str,
    league_id: int,
    season: int,
    rate_limit_sec: float = RATE_LIMIT_SEC,
) -> dict[int, dict[str, str]]:
    """
    按「日期+联赛+赛季」拉取该日该联赛所有场次的赔率（付费档常用方式）。
    返回 {fixture_id: {spf_win, spf_draw, spf_loss}, ...}。
    """
    date_only = (date_str or "")[:10]
    if len(date_only) != 10:
        return {}
    data = _request("odds", {"date": date_only, "league": league_id, "season": season})
    time.sleep(rate_limit_sec)
    rows = data.get("response") or []
    out: dict[int, dict[str, str]] = {}
    for item in rows:
        fix = item.get("fixture") or {}
        fid = fix.get("id")
        if not fid:
            continue
        odds = _parse_odds_from_response(item)
        if odds:
            out[int(fid)] = odds
    return out


def get_db_matches_for_date(date_str: str) -> list[dict[str, Any]]:
    """
    从 DB 取指定日期的五大联赛比赛（主客队名、联赛中文名）。
    用于「按 DB 比赛拉赔率」：以 DB 为基准，只拉 DB 中存在的场次。
    """
    from db import repo
    return repo.get_db_matches_for_date(date_str)


def _match_db_to_fixture(
    db_home: str, db_away: str, league_cn: str,
    api_fixtures: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """用 DB 主客队名 + 联赛在 API 赛程中做模糊匹配，返回匹配到的 fixture 信息。"""
    home_n = (db_home or "").strip().lower().replace(" ", "")
    away_n = (db_away or "").strip().lower().replace(" ", "")
    if not home_n and not away_n:
        return None
    for f in api_fixtures:
        if (f.get("league_cn") or "").strip() != league_cn:
            continue
        h = (f.get("home_name") or "").strip().lower().replace(" ", "")
        a = (f.get("away_name") or "").strip().lower().replace(" ", "")
        if (home_n in h or h in home_n) and (away_n in a or a in away_n):
            return f
    return None


def fetch_odds_for_date_from_db(
    date_str: str,
    rate_limit_sec: float = RATE_LIMIT_SEC,
    merge: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    """
    按 DB 中该日期的五大联赛比赛拉取赔率：先查 DB 场次，再调 API 赛程并匹配，只拉匹配到的 fixture 赔率。
    这样即使用户当日 API 限流返回空赛程，也能看到「DB 该日 N 场」便于排查；且以 DB 为基准不依赖 API 全量赛程。
    返回 (entries, message)。merge=True 时与现有 JSON 合并。
    """
    date_only = (date_str or "")[:10]
    if len(date_only) != 10:
        return [], "日期格式需为 YYYY-MM-DD"

    db_matches = get_db_matches_for_date(date_only)
    if not db_matches:
        # DB 该日无五大联赛，无需请求 API
        old_entries: list[dict[str, Any]] = _load_odds_entries_from_db() if merge else []
        merged_total = len(old_entries)
        return old_entries, f"赔率：{date_only} DB 该日 0 场（无五大联赛），未请求 API，当前 DB 共 {merged_total} 场"

    api_fixtures = _fixtures_with_id(date_only)
    time.sleep(rate_limit_sec)

    # 付费档可能需按「日期+联赛+赛季」拉赔率，先试该方式（每日期 5 次请求）
    season = _season_year_from_date(date_only)
    odds_by_fid: dict[int, dict[str, str]] = {}
    for league_id in LEAGUE_IDS:
        odds_by_fid.update(
            fetch_odds_by_date_league_season(date_only, league_id, season, rate_limit_sec)
        )

    entries: list[dict[str, Any]] = []
    matched = 0
    for dm in db_matches:
        f = _match_db_to_fixture(
            dm["home_name"], dm["away_name"], dm["league_cn"],
            api_fixtures,
        )
        if not f:
            continue
        matched += 1
        fid = f["fixture_id"]
        odds = odds_by_fid.get(fid)
        if not odds:
            # 按日期+联赛+赛季未拿到时，再试按 fixture 单场拉（兼容仅按 fixture 有数据的套餐）
            odds = fetch_odds_for_fixture(fid, rate_limit_sec)
        if not odds:
            continue
        entries.append({
            "fixture_id": fid,
            "league_id": f["league_id"],
            "league_cn": f["league_cn"],
            "home": f["home_name"],
            "away": f["away_name"],
            "date": f["fixture_date"],
            "spf_win": odds.get("spf_win"),
            "spf_draw": odds.get("spf_draw"),
            "spf_loss": odds.get("spf_loss"),
            "ah_handicap": odds.get("ah_handicap"),
            "ah_home": odds.get("ah_home"),
            "ah_away": odds.get("ah_away"),
        })

    # 与 DB 已有赔率合并
    old_entries = _load_odds_entries_from_db() if merge else []
    key_to_entry: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for e in old_entries:
        if e.get("league_cn") and (e.get("home") or e.get("away")):
            k = (e.get("league_cn"), (e.get("home") or "").strip(), (e.get("away") or "").strip(), (e.get("date") or "")[:10])
            key_to_entry[k] = e
    for e in entries:
        k = (e.get("league_cn"), (e.get("home") or "").strip(), (e.get("away") or "").strip(), (e.get("date") or "")[:10])
        key_to_entry[k] = e
    entries = list(key_to_entry.values())

    _save_odds_to_db(entries)
    merged_total = len(entries)
    this_batch = len([e for e in entries if (e.get("date") or "")[:10] == date_only])
    if matched > 0 and this_batch == 0:
        hint = "（已尝试 odds?fixture= 与 odds?date&league&season= 均无数据；请在 API-FOOTBALL 后台确认套餐是否包含历史赔率 Historical Odds）"
    else:
        hint = ""
    msg = (
        f"赔率已更新：{date_only} DB 该日 {len(db_matches)} 场，API 匹配 {matched} 场，本次写入 {this_batch} 场，"
        f"合并后共 {merged_total} 场，已写入 DB（match_odds）{hint}"
    )
    return entries, msg


def fetch_odds_for_date(
    date_str: str,
    rate_limit_sec: float = RATE_LIMIT_SEC,
    merge: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    """
    拉取指定日期五大联赛所有场次的赔率，写入 data/api_football_odds.json。
    返回 (entries, message)。merge=True 时与现有 JSON 合并（同场次覆盖）。
    """
    date_only = (date_str or "")[:10]
    if len(date_only) != 10:
        return [], "日期格式需为 YYYY-MM-DD"
    fixtures = _fixtures_with_id(date_only)
    time.sleep(rate_limit_sec)
    entries = []
    for f in fixtures:
        fid = f["fixture_id"]
        odds = fetch_odds_for_fixture(fid, rate_limit_sec)
        if not odds:
            continue
        entries.append({
            "fixture_id": fid,
            "league_id": f["league_id"],
            "league_cn": f["league_cn"],
            "home": f["home_name"],
            "away": f["away_name"],
            "date": f["fixture_date"],
            "spf_win": odds.get("spf_win"),
            "spf_draw": odds.get("spf_draw"),
            "spf_loss": odds.get("spf_loss"),
            "ah_handicap": odds.get("ah_handicap"),
            "ah_home": odds.get("ah_home"),
            "ah_away": odds.get("ah_away"),
        })
    # 与 DB 已有赔率合并
    old_entries: list[dict[str, Any]] = _load_odds_entries_from_db() if merge else []
    key_to_entry: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for e in old_entries:
        if e.get("league_cn") and (e.get("home") or e.get("away")):
            k = (e.get("league_cn"), (e.get("home") or "").strip(), (e.get("away") or "").strip(), (e.get("date") or "")[:10])
            key_to_entry[k] = e
    for e in entries:
        k = (e.get("league_cn"), (e.get("home") or "").strip(), (e.get("away") or "").strip(), (e.get("date") or "")[:10])
        key_to_entry[k] = e
    entries = list(key_to_entry.values())

    merged_total = len(entries)
    this_batch = len([e for e in entries if (e.get("date") or "")[:10] == date_only])
    # 仅当本日有新数据或合并结果有变化时才写 DB，避免「本次 0 场」时全表无意义重写
    if this_batch > 0:
        _save_odds_to_db(entries)
    if this_batch == 0:
        msg = f"赔率已更新：{date_only} 本次 0 场（该日 API 无五大联赛赛程或赔率），合并后共 {merged_total} 场，已跳过写入（DB 无变化）"
    else:
        msg = f"赔率已更新：{date_only} 本次 {this_batch} 场，合并后共 {merged_total} 场，已写入 DB（match_odds）"
    return entries, msg


def _load_odds_entries_from_db() -> list[dict[str, Any]]:
    """从 match_odds 表加载全部赔率，返回与 JSON entries 同结构的列表（合并时用）。"""
    try:
        from db import repo
        if repo.use_mongo():
            return repo.match_odds_list_all()
        from sqlalchemy import text
        from etl.schema import get_engine, init_db
        if not config.DB_PATH.exists():
            return []
        engine = get_engine()
        init_db(engine)
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT log_date, home_team, away_team, league_cn, spf_win, spf_draw, spf_loss FROM match_odds ORDER BY id"),
            ).fetchall()
        return [
            {
                "date": (r[0] or "")[:10],
                "home": (r[1] or "").strip(),
                "away": (r[2] or "").strip(),
                "league_cn": (r[3] or "").strip(),
                "spf_win": r[4],
                "spf_draw": r[5],
                "spf_loss": r[6],
            }
            for r in rows
            if (r[3] or "").strip()
        ]
    except Exception:
        return []


def migrate_odds_json_to_db(batch_size: int = 2000) -> int:
    """将 data/api_football_odds.json 一次性导入 match_odds 表，返回导入条数。"""
    if not ODDS_JSON_PATH.is_file():
        return 0
    try:
        data = json.loads(ODDS_JSON_PATH.read_text(encoding="utf-8"))
        entries = data.get("entries") or []
    except Exception:
        return 0
    if not entries:
        return 0
    total = 0
    for i in range(0, len(entries), batch_size):
        chunk = entries[i : i + batch_size]
        _save_odds_to_db(chunk)
        total += len(chunk)
    return total


def _save_odds_to_db(entries: list[dict[str, Any]]) -> None:
    """将赔率条目写入 match_odds 表，便于统一从 DB 读取。"""
    if not entries:
        return
    try:
        from db import repo
        if repo.use_mongo():
            for e in entries:
                log_date = (e.get("date") or "")[:10]
                home = (e.get("home") or "").strip() or "?"
                away = (e.get("away") or "").strip() or "?"
                league = (e.get("league_cn") or "").strip()
                if not log_date or not league:
                    continue
                repo.match_odds_upsert_one(
                    log_date, home, away, league,
                    str(e.get("spf_win") or ""), str(e.get("spf_draw") or ""), str(e.get("spf_loss") or ""),
                    "api_football",
                    ah_handicap=e.get("ah_handicap"),
                    ah_home=e.get("ah_home"),
                    ah_away=e.get("ah_away"),
                )
            return
        from datetime import datetime
        from sqlalchemy import text
        from etl.schema import get_engine, init_db
        import config
        if not config.DB_PATH.exists():
            return
        engine = get_engine()
        init_db(engine)
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with engine.connect() as conn:
            for e in entries:
                log_date = (e.get("date") or "")[:10]
                home = (e.get("home") or "").strip() or "?"
                away = (e.get("away") or "").strip() or "?"
                league = (e.get("league_cn") or "").strip()
                if not log_date or not league:
                    continue
                params = {
                    "log_date": log_date, "home_team": home, "away_team": away, "league_cn": league,
                    "spf_win": e.get("spf_win"), "spf_draw": e.get("spf_draw"), "spf_loss": e.get("spf_loss"),
                    "created_at": now,
                }
                r = conn.execute(
                    text("""
                        UPDATE match_odds SET spf_win=:spf_win, spf_draw=:spf_draw, spf_loss=:spf_loss
                        WHERE log_date=:log_date AND home_team=:home_team AND away_team=:away_team AND league_cn=:league_cn
                    """),
                    {k: v for k, v in params.items() if k != "created_at"},
                )
                if r.rowcount == 0:
                    conn.execute(
                        text("""
                            INSERT INTO match_odds (log_date, home_team, away_team, league_cn, spf_win, spf_draw, spf_loss, source, created_at)
                            VALUES (:log_date, :home_team, :away_team, :league_cn, :spf_win, :spf_draw, :spf_loss, 'api_football', :created_at)
                        """),
                        params,
                    )
            conn.commit()
    except Exception:
        pass


def load_odds_for_match(home: str, away: str, league_cn: str, date_str: str | None = None) -> dict[str, str] | None:
    """
    按主客队+联赛查找赔率，供预测融合使用。优先从 DB（match_odds）读，无则从 JSON 读。
    date_str 可选，不传则匹配任意日期（取第一条）。
    返回 {spf_win, spf_draw, spf_loss} 或 None。
    """
    try:
        from db import repo
        if repo.use_mongo():
            ret = repo.match_odds_load_for_match(home, away, (league_cn or "").strip(), (date_str or "")[:10] if date_str else None)
            if not ret or not (ret.get("spf_win") and ret.get("spf_draw") and ret.get("spf_loss")):
                return None
            return {"spf_win": str(ret["spf_win"]), "spf_draw": str(ret["spf_draw"]), "spf_loss": str(ret["spf_loss"])}
    except Exception:
        pass
    home_n = (home or "").strip().lower().replace(" ", "")
    away_n = (away or "").strip().lower().replace(" ", "")
    league = (league_cn or "").strip()
    date_prefix = (date_str or "")[:10] if date_str else None
    try:
        from sqlalchemy import text
        from etl.schema import get_engine, init_db
        import config
        if config.DB_PATH.exists():
            engine = get_engine()
            init_db(engine)
            with engine.connect() as conn:
                if date_prefix:
                    rows = conn.execute(
                        text("SELECT home_team, away_team, spf_win, spf_draw, spf_loss FROM match_odds WHERE league_cn = :league AND log_date = :d ORDER BY id DESC"),
                        {"league": league, "d": date_prefix},
                    ).fetchall()
                else:
                    rows = conn.execute(
                        text("SELECT home_team, away_team, spf_win, spf_draw, spf_loss FROM match_odds WHERE league_cn = :league ORDER BY log_date DESC, id DESC LIMIT 500"),
                        {"league": league},
                    ).fetchall()
                for r in rows:
                    h = (r[0] or "").strip().lower().replace(" ", "")
                    a = (r[1] or "").strip().lower().replace(" ", "")
                    if (home_n in h or h in home_n) and (away_n in a or a in away_n):
                        win, draw, loss = r[2], r[3], r[4]
                        if win and draw and loss:
                            return {"spf_win": str(win), "spf_draw": str(draw), "spf_loss": str(loss)}
    except Exception:
        pass
    return None
