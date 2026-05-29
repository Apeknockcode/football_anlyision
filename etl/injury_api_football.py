"""
API-FOOTBALL（api-sports.io）伤停数据拉取。
按日期拉取当日五大联赛赛程 + 各联赛伤停列表，合并为与 injury_suspension.json 一致的格式（按主客队匹配）。
免费档：fixtures 按日期仅开放近期；injuries 仅开放 2022–2024 赛季。对 2022–2024 日期走「历史路径」：先拉 injuries 再按 fixture 日期过滤，再按 fixture id 拉主客队。
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import config

# 断点续拉进度文件：记录各赛季已拉取到的最后日期（含）
INJURY_API_PROGRESS_PATH: Path = config.DATA_DIR / "injury_api_progress.json"

# 五大联赛在 API-FOOTBALL 中的 league id
LEAGUE_IDS: dict[int, str] = {
    39: "英超",   # Premier League
    140: "西甲",  # La Liga
    135: "意甲",  # Serie A
    78: "德甲",   # Bundesliga
    61: "法甲",   # Ligue 1
}
# 免费档 injuries 仅开放这些赛季；这些日期的赛程需走「历史路径」（按 injuries 里的 fixture 日期过滤 + 按 id 拉主客队）
FREE_TIER_INJURY_SEASONS = (2022, 2023, 2024)


def _season_date_range(season: int) -> list[str]:
    """某赛季欧洲联赛日期列表（约 8 月初至次年 5 月底），返回 YYYY-MM-DD 列表。"""
    start = datetime(season, 8, 1)
    end = datetime(season + 1, 5, 31)
    out: list[str] = []
    d = start
    while d <= end:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def _load_progress() -> dict[str, str]:
    """读取断点续拉进度：{"season_2024": "2024-11-20"} 表示 2024 赛季已拉到 2024-11-20（含）。"""
    if not INJURY_API_PROGRESS_PATH.is_file():
        return {}
    try:
        data = json.loads(INJURY_API_PROGRESS_PATH.read_text(encoding="utf-8"))
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_progress(progress: dict[str, str]) -> None:
    """写入断点续拉进度。"""
    INJURY_API_PROGRESS_PATH.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def reset_season_progress(season: int) -> None:
    """清除某赛季的拉取进度，下次 --season 会从该赛季首日重新拉取。"""
    progress = _load_progress()
    key = f"season_{season}"
    if key in progress:
        del progress[key]
        _save_progress(progress)


def _season_year_from_date(date_str: str) -> int:
    """从日期 YYYY-MM-DD 得到当前赛季年份（欧洲联赛多为 8 月开赛）。"""
    try:
        y, m, _ = (date_str or "")[:10].split("-")
        year, month = int(y), int(m)
        return year if month >= 8 else year - 1
    except Exception:
        return datetime.now().year


# 请求超时秒数（拉取 lineups 等接口偶发较慢，适当放宽以减少 ReadTimeout）
REQUEST_TIMEOUT = getattr(config, "API_FOOTBALL_REQUEST_TIMEOUT", 90)


def _request(
    path: str,
    params: dict[str, str | int] | None = None,
    max_retries: int = 3,
    quiet: bool = False,
) -> dict[str, Any]:
    """请求 API-FOOTBALL，返回解析后的 JSON。支持多 key：某 key 返回 429 或免费档限制时自动换下一个 key 重试；超时/连接错误会重试后换 key。quiet=True 时不打印「使用 key」日志，便于批量回填时输出简洁。"""
    keys = [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]
    if not keys:
        raise ValueError("未配置 API_FOOTBALL_KEY（或 API_FOOTBALL_KEYS），请在 .env 中设置")
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except ImportError:
        raise ImportError("需要 requests：pip install requests")
    url = f"{config.API_FOOTBALL_BASE.rstrip('/')}/{path.lstrip('/')}"
    no_proxy = {"http": None, "https": None}
    last_err: Exception | None = None
    timeout_retries = 2  # 同一 key 超时后重试次数（共最多 3 次请求）
    for idx, key in enumerate(keys):
        key = (key or "").strip()
        if not key:
            continue
        key_hint = f"key {idx + 1}/{len(keys)} (末4位 …{key[-4:]})" if len(key) >= 4 else "key"
        if not quiet:
            print(f"API-FOOTBALL: 使用 {key_hint}", file=sys.stderr)
        headers = {"x-apisports-key": key}
        session = requests.Session()
        # read=0：不在读超时上重试，避免单次请求等 3×timeout；超时由上层循环重试并打日志
        retry = Retry(
            total=max_retries,
            read=0,
            backoff_factor=2,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=["GET"],
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        for attempt in range(timeout_retries + 1):
            try:
                r = session.get(
                    url,
                    headers=headers,
                    params=params or {},
                    timeout=REQUEST_TIMEOUT,
                    proxies=no_proxy,
                )
                break
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.Timeout,
                OSError,
            ) as e:
                last_err = e
                if isinstance(e, requests.exceptions.Timeout) and attempt < timeout_retries:
                    if not quiet:
                        print(f"API-FOOTBALL: {key_hint} 请求超时，{attempt + 1}/{timeout_retries + 1} 次，重试…", file=sys.stderr)
                    time.sleep(2 * (attempt + 1))
                    continue
                continue
        else:
            continue
        if r.status_code == 429:
            last_err = ValueError("API-FOOTBALL 请求超限（10 次/分钟或 100 次/天）")
            print(f"API-FOOTBALL: {key_hint} 超限，换下一 key", file=sys.stderr)
            continue  # 换下一个 key 重试
        if r.status_code != 200:
            raise ValueError(f"API-FOOTBALL 返回 {r.status_code}: {r.text[:200]}")
        out = r.json() if r.text else {}
        if not isinstance(out, dict):
            return {"response": []}
        errors = out.get("errors")
        if isinstance(errors, dict) and errors:
            msg = str(errors)[:200]
            endpoint_msg = errors.get("endpoint")
            endpoint_msg_l = str(endpoint_msg or "").lower()
            if endpoint_msg and "does not exist" in endpoint_msg_l:
                raise ValueError(f"API-FOOTBALL 接口不存在：{endpoint_msg}")
            # 参数错误（如缺少 season）：换 key 无效，直接抛出
            if "season" in errors or "required" in msg.lower():
                raise ValueError(f"API-FOOTBALL 参数错误：{msg}")
            # 套餐/计划限制（plan、date 等）：可尝试换 key
            limit_msg = errors.get("plan") or errors.get("date") or msg
            last_err = ValueError(f"API-FOOTBALL 套餐/计划限制：{limit_msg}")
            print(f"API-FOOTBALL: {key_hint} 返回限制({limit_msg[:50]}…)，换下一 key", file=sys.stderr)
            continue
        if "response" in out:
            return out
        return {"response": []}
    if last_err:
        raise last_err
    raise ValueError("未配置有效的 API_FOOTBALL_KEY，请在 .env 中设置")


def fetch_fixtures(date_str: str) -> list[dict[str, Any]]:
    """
    拉取指定日期的赛程（仅五大联赛）。
    返回 [{"league_id", "league_cn", "home_name", "away_name"}, ...]。
    """
    date_only = (date_str or "")[:10]
    if len(date_only) != 10:
        return []
    data = _request("fixtures", {"date": date_only})
    rows = data.get("response") or []
    out = []
    for item in rows:
        league = item.get("league") or {}
        lid = league.get("id")
        if lid not in LEAGUE_IDS:
            continue
        teams = item.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        home_name = (home.get("name") or "").strip()
        away_name = (away.get("name") or "").strip()
        if not home_name and not away_name:
            continue
        fix = item.get("fixture") or {}
        fix_date_raw = (fix.get("date") or "").strip()
        fixture_date = fix_date_raw[:10] if len(fix_date_raw) >= 10 else ""
        out.append({
            "league_id": lid,
            "league_cn": LEAGUE_IDS[lid],
            "home_name": home_name,
            "away_name": away_name,
            "fixture_date": fixture_date,
            "match_time": fix_date_raw,
        })
    return out


def fetch_injuries_for_league(league_id: int, season: int) -> list[dict[str, Any]]:
    """
    拉取某联赛当季伤停列表。
    返回 [{"team_name", "player_name", "reason"}, ...]。
    """
    data = _request("injuries", {"league": league_id, "season": season})
    rows = data.get("response") or []
    out = []
    for item in rows:
        team = item.get("team") or {}
        player = item.get("player") or {}
        team_name = (team.get("name") or "").strip()
        player_name = (player.get("name") or "").strip()
        reason = (player.get("reason") or player.get("type") or item.get("reason") or "").strip() or "伤停"
        if not team_name or not player_name:
            continue
        out.append({"team_name": team_name, "player_name": player_name, "reason": reason or "伤停"})
    return out


def _fetch_injuries_raw_with_fixture(league_id: int, season: int) -> list[dict[str, Any]]:
    """拉取某联赛当季伤停（含 fixture id/date、team id），用于历史路径按日期过滤。"""
    data = _request("injuries", {"league": league_id, "season": season})
    rows = data.get("response") or []
    out = []
    for item in rows:
        fix = item.get("fixture") or {}
        fix_id = fix.get("id")
        fix_date = (fix.get("date") or "")[:10]
        team = item.get("team") or {}
        player = item.get("player") or {}
        team_id = team.get("id")
        team_name = (team.get("name") or "").strip()
        player_name = (player.get("name") or "").strip()
        reason = (player.get("reason") or player.get("type") or "").strip() or "伤停"
        if fix_id is None or not team_name or not player_name:
            continue
        out.append({
            "fixture_id": fix_id,
            "fixture_date": fix_date,
            "team_id": team_id,
            "team_name": team_name,
            "player_name": player_name,
            "reason": reason or "伤停",
        })
    return out


def fetch_fixture_by_id(fixture_id: int, max_retries: int = 2) -> dict[str, Any] | None:
    """按 id 拉取单场赛程，返回 {league_id, home_id, home_name, away_id, away_name} 或 None。"""
    data = None
    for attempt in range(max_retries + 1):
        try:
            data = _request("fixtures", {"id": fixture_id})
            break
        except Exception:
            if attempt == max_retries:
                return None
            time.sleep(3)
    if not data:
        return None
    rows = data.get("response") or []
    if not rows:
        return None
    item = rows[0]
    league = item.get("league") or {}
    lid = league.get("id")
    teams = item.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    fix = item.get("fixture") or {}
    fix_date_raw = (fix.get("date") or "").strip()
    fixture_date = fix_date_raw[:10] if len(fix_date_raw) >= 10 else ""
    return {
        "league_id": lid,
        "home_id": home.get("id"),
        "home_name": (home.get("name") or "").strip(),
        "away_id": away.get("id"),
        "away_name": (away.get("name") or "").strip(),
        "fixture_date": fixture_date,
        "match_time": fix_date_raw,
    }


def _norm_team_key(name: str) -> str:
    return (name or "").strip().replace(" ", "").lower()


def _build_matches_historical(
    date_str: str,
    rate_limit_seconds: float,
) -> list[dict[str, Any]]:
    """历史路径：免费档仅开放 2022–2024 赛季 injuries。按联赛拉伤停，按 fixture 日期过滤，再按 fixture id 拉主客队。"""
    date_only = (date_str or "")[:10]
    if len(date_only) != 10:
        return []
    season = _season_year_from_date(date_str)
    if season not in FREE_TIER_INJURY_SEASONS:
        return []
    # fixture_id -> [(team_id, team_name, player_name, reason), ...]，同一场可能有多条（主客队各有伤停）
    fixture_injuries: dict[int, list[tuple[Any, str, str, str]]] = defaultdict(list)
    fixture_league: dict[int, int] = {}
    for league_id in LEAGUE_IDS:
        raw = _fetch_injuries_raw_with_fixture(league_id, season)
        time.sleep(rate_limit_seconds)
        for row in raw:
            if (row.get("fixture_date") or "")[:10] != date_only:
                continue
            fix_id = row.get("fixture_id")
            if fix_id is None:
                continue
            fixture_league[fix_id] = league_id
            fixture_injuries[fix_id].append((
                row.get("team_id"),
                (row.get("team_name") or "").strip(),
                (row.get("player_name") or "").strip(),
                (row.get("reason") or "伤停").strip(),
            ))
    # 免费档 100 次/天，单日限制处理场次以免超时与超量（5 次 injuries + 每场 1 次 fixture id）
    max_fixtures_per_date = 25
    matches = []
    for fix_id in sorted(fixture_injuries.keys())[:max_fixtures_per_date]:
        lid = fixture_league.get(fix_id)
        if lid not in LEAGUE_IDS:
            continue
        detail = fetch_fixture_by_id(fix_id)
        time.sleep(rate_limit_seconds)
        if not detail or detail.get("league_id") not in LEAGUE_IDS:
            continue
        home_id = detail.get("home_id")
        away_id = detail.get("away_id")
        home_name = detail.get("home_name") or ""
        away_name = detail.get("away_name") or ""
        rows = fixture_injuries[fix_id]
        home_absent = [{"name": n, "reason": r} for (tid, _tn, n, r) in rows if tid == home_id]
        away_absent = [{"name": n, "reason": r} for (tid, _tn, n, r) in rows if tid == away_id]
        if not home_absent and not away_absent:
            continue
        matches.append({
            "match_num": "",
            "league": LEAGUE_IDS[lid],
            "home": home_name,
            "away": away_name,
            "home_absent": home_absent,
            "away_absent": away_absent,
            "fixture_date": detail.get("fixture_date") or date_only,
            "match_time": detail.get("match_time") or "",
        })
    return matches


def build_injury_matches(
    date_str: str,
    rate_limit_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """
    按日期拉取当日五大联赛赛程与各联赛伤停，合并为 injury_suspension 单场格式。
    免费档对 2022–2024 赛季不开放 fixtures 按日期，故直接走历史路径（injuries 按联赛+赛季，再按 fixture 日期过滤、按 id 拉主客队）。
    """
    if rate_limit_seconds is None:
        rate_limit_seconds = config.API_FOOTBALL_RATE_LIMIT_SEC
    date_only = (date_str or "")[:10]
    season = _season_year_from_date(date_str)
    if season in FREE_TIER_INJURY_SEASONS:
        return _build_matches_historical(date_only, rate_limit_seconds)
    fixtures = fetch_fixtures(date_str)
    time.sleep(rate_limit_seconds)
    # 按联赛聚合伤停：league_id -> team_name -> [{"name", "reason"}]
    injuries_by_league: dict[int, dict[str, list[dict]]] = {}
    for league_id in LEAGUE_IDS:
        if league_id not in {f["league_id"] for f in fixtures}:
            continue
        raw = fetch_injuries_for_league(league_id, season)
        time.sleep(rate_limit_seconds)
        by_team: dict[str, list[dict]] = {}
        for row in raw:
            tn = (row.get("team_name") or "").strip()
            if not tn:
                continue
            by_team.setdefault(tn, []).append({
                "name": (row.get("player_name") or "").strip(),
                "reason": (row.get("reason") or "伤停").strip(),
            })
        injuries_by_league[league_id] = by_team
    matches = []
    for f in fixtures:
        lid = f["league_id"]
        home_name = f["home_name"]
        away_name = f["away_name"]
        by_team = injuries_by_league.get(lid) or {}
        home_absent = by_team.get(home_name) or []
        away_absent = by_team.get(away_name) or []
        if not home_absent and not away_absent:
            continue
        matches.append({
            "match_num": "",
            "league": f["league_cn"],
            "home": home_name,
            "away": away_name,
            "home_absent": home_absent,
            "away_absent": away_absent,
            "fixture_date": f.get("fixture_date") or "",
            "match_time": f.get("match_time") or "",
        })
    return matches


def fetch_and_save(
    date_str: str,
    merge: bool = False,
    rate_limit_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """
    拉取 API-FOOTBALL 伤停并写入 data/injury_suspension.json。
    返回 (matches, message)。
    """
    from etl.injury_crawl import save_injury_json

    date_only = (date_str or "")[:10]
    if len(date_only) != 10:
        return [], "日期格式需为 YYYY-MM-DD"
    matches = build_injury_matches(date_only, rate_limit_seconds=rate_limit_seconds)
    path = save_injury_json(
        matches,
        source_url=config.API_FOOTBALL_BASE or "",
        merge=merge,
        source="api_football",
    )
    big5 = sum(1 for m in matches if m.get("league") in LEAGUE_IDS.values())
    try:
        from db import repo
        store = "DB（Mongo）" if repo.use_mongo() else "DB（SQLite）"
    except Exception:
        store = "DB"
    msg = f"伤停已更新：共 {len(matches)} 场（五大联赛 {big5} 场），已保存至 {store}"
    return matches, msg


def fetch_season_and_save(
    season: int,
    rate_limit_seconds: float | None = None,
) -> tuple[int, str]:
    """
    按赛季断点续拉伤停：从进度文件记录的上次日期之后继续拉取，每日 merge 写入 injury_suspension.json。
    返回 (本轮拉取的天数, 摘要信息)。若遇 429 等异常会先保存进度再抛出，下次执行从断点继续。
    """
    if rate_limit_seconds is None:
        rate_limit_seconds = config.API_FOOTBALL_RATE_LIMIT_SEC
    # 付费档可拉任意赛季（2025/2026 等）；免费档仅 2022–2024 在 API 侧有数据

    all_dates = _season_date_range(season)
    progress = _load_progress()
    key = f"season_{season}"
    last_done = (progress.get(key) or "").strip()[:10]
    if last_done:
        # 只拉 last_done 之后的日期
        try:
            idx = all_dates.index(last_done)
            dates_to_do = all_dates[idx + 1:]
        except ValueError:
            dates_to_do = all_dates
    else:
        dates_to_do = all_dates

    if not dates_to_do:
        return 0, f"赛季 {season} 已全部拉取完毕，无需继续。"

    from_str = f"从 {dates_to_do[0]} 起" if last_done else "从赛季首日"
    # 可选：打印到 stderr 便于看到续拉起点（主逻辑在 main 里 print message）
    total_matches = 0
    last_success_date: str | None = None
    for i, date_str in enumerate(dates_to_do):
        try:
            matches, _ = fetch_and_save(date_str, merge=True)
            total_matches += len(matches)
            last_success_date = date_str
            progress[key] = date_str
            _save_progress(progress)
        except ValueError as e:
            err_msg = str(e)
            if "429" in err_msg or "超限" in err_msg:
                if last_success_date:
                    progress[key] = last_success_date
                    _save_progress(progress)
                raise ValueError(
                    f"API 请求已达今日限额，已保存进度到 {last_success_date}。明日请重新执行同一命令从断点继续。"
                ) from e
            raise
        if i < len(dates_to_do) - 1:
            time.sleep(rate_limit_seconds)

    summary = (
        f"赛季 {season} 本轮拉取 {len(dates_to_do)} 天（{from_str}，至 {last_success_date or '无'}），"
        f"共 {total_matches} 场伤停已合并至 data/injury_suspension.json。"
    )
    if last_success_date and last_success_date == dates_to_do[-1]:
        summary += " 该赛季已全部拉完。"
    return len(dates_to_do), summary
