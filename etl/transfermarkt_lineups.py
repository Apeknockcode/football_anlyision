"""
Transfermarkt 阵容抓取：按日拉取当日比赛列表，再请求 vorbericht（赛前用户阵型）解析预计首发，落库供模型使用。
与 jingcai_match_analysis 通过 sporttery_match_id 关联；预测时优先使用本来源（再回退懂球帝）。
"""
from __future__ import annotations

import re
import time
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore

BASE_URL = "https://www.transfermarkt.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REQUEST_TIMEOUT = 15


def _rate_limit_sec() -> float:
    try:
        import config
        return max(0.5, float(getattr(config, "TRANSFERMARKT_RATE_LIMIT_SEC", 2.5)))
    except Exception:
        return 2.5


def _max_requests_per_run() -> int:
    try:
        import config
        return max(5, int(getattr(config, "TRANSFERMARKT_MAX_REQUESTS_PER_RUN", 25)))
    except Exception:
        return 25


def _session():
    s = requests.Session() if requests else None
    if s:
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
            "Referer": f"{BASE_URL}/",
        })
    return s


def fetch_html(
    url: str, sleep_after: bool = False, rate_limit_sec: float | None = None
) -> tuple[str | None, str | None]:
    """
    请求 URL，返回 (html, error_msg)。成功时 error_msg 为 None；失败时为简短原因（如 "HTTP 403"、"请求超时"）。
    """
    if not requests:
        return None, "未安装 requests"
    try:
        r = _session().get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        out = r.text
        if sleep_after:
            sec = rate_limit_sec if rate_limit_sec is not None else _rate_limit_sec()
            time.sleep(sec)
        return out, None
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        return None, f"HTTP {code}" if code else "HTTP 错误"
    except requests.exceptions.Timeout:
        return None, "请求超时"
    except requests.exceptions.RequestException as e:
        return None, (str(e)[:80] or "网络错误")
    except Exception as e:
        return None, (str(e)[:80] or "请求异常")


def fetch_matches_for_date(match_date: str) -> tuple[list[dict[str, Any]], str | None]:
    """
    请求 Transfermarkt 当日赛程页，解析 match_id 与主客队名。
    URL: /aktuell/waspassiertheute/aktuell/new/datum/YYYY-MM-DD
    返回 (列表, error_msg)。成功时 error_msg 为 None；列表项为 {"match_id", "home_team", "away_team"}。
    """
    date_only = (match_date or "").strip()[:10]
    if len(date_only) != 10:
        return [], "无效日期"
    url = f"{BASE_URL}/aktuell/waspassiertheute/aktuell/new/datum/{date_only}"
    html, req_err = fetch_html(url, sleep_after=True)
    if req_err or not html:
        return [], f"请求失败: {req_err or '无响应'}"
    if not BeautifulSoup:
        return [], "未安装 beautifulsoup4"
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    # 链接格式: /spielbericht/index/spielbericht/123 或 /vorbericht/.../123
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        mid_match = re.search(r"/spielbericht/index/spielbericht/(\d+)", href)
        if not mid_match:
            mid_match = re.search(r"/vorbericht/index/spielbericht/(\d+)", href)
        if not mid_match:
            continue
        match_id = int(mid_match.group(1))
        # 同一场可能多处出现，去重（保留第一次出现的队名信息）
        if any(m["match_id"] == match_id for m in out):
            continue
        # 尝试从链接文本或父节点取主客队名（页面结构不一，尽量取）
        home_team, away_team = "", ""
        parent = a.parent
        for _ in range(5):
            if not parent:
                break
            text = (parent.get_text() or "").strip()
            if " - " in text or ":" in text:
                parts = re.split(r"\s*-\s*|\s*:\s*", text, maxsplit=1)
                if len(parts) >= 2 and len(parts[0]) < 50 and len(parts[1]) < 50:
                    home_team = parts[0].strip()
                    away_team = parts[1].strip()
                    break
            parent = getattr(parent, "parent", None)
        out.append({"match_id": match_id, "home_team": home_team or "", "away_team": away_team or ""})
    if not out:
        return [], "页面未解析到任何比赛链接（可能该日无赛程或页面结构已变更）"
    return out, None


# 联赛轮次页：当「当日赛程页」被拒或无数据时，可用此备选。附录见 docs/阵容数据抓取设计.md
LEAGUE_SPIELTAG = {
    "premier-league": "GB1",
    "1-bundesliga": "L1",
    "la-liga": "ES1",
    "serie-a": "IT1",
    "ligue-1": "FR1",
    "champions-league": "CL",
}


def fetch_matches_for_league_round(
    league_slug: str,
    saison_id: int,
    spieltag: int,
    wettbewerb_id: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """
    从联赛轮次页（spieltag）解析该轮全部 match_id。当当日赛程页失败时可作备选。
    league_slug 示例: premier-league, 1-bundesliga；saison_id 为开赛年如 2024 表示 24/25 赛季。
    返回 (列表, error_msg)，结构同 fetch_matches_for_date。
    """
    wb = wettbewerb_id or LEAGUE_SPIELTAG.get(league_slug.strip().lower())
    if not wb:
        return [], f"未知联赛: {league_slug}，可用: {list(LEAGUE_SPIELTAG.keys())}"
    url = f"{BASE_URL}/{league_slug}/spieltag/wettbewerb/{wb}?saison_id={saison_id}&spieltag={spieltag}"
    html, req_err = fetch_html(url, sleep_after=True)
    if req_err or not html:
        return [], f"请求失败: {req_err or '无响应'}"
    if not BeautifulSoup:
        return [], "未安装 beautifulsoup4"
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        mid_match = re.search(r"/spielbericht/index/spielbericht/(\d+)", href)
        if not mid_match:
            mid_match = re.search(r"/vorbericht/index/spielbericht/(\d+)", href)
        if not mid_match:
            continue
        match_id = int(mid_match.group(1))
        if match_id in seen:
            continue
        seen.add(match_id)
        home_team, away_team = "", ""
        parent = a.parent
        for _ in range(5):
            if not parent:
                break
            text = (parent.get_text() or "").strip()
            if " - " in text or ":" in text:
                parts = re.split(r"\s*-\s*|\s*:\s*", text, maxsplit=1)
                if len(parts) >= 2 and len(parts[0]) < 50 and len(parts[1]) < 50:
                    home_team = parts[0].strip()
                    away_team = parts[1].strip()
                    break
            parent = getattr(parent, "parent", None)
        out.append({"match_id": match_id, "home_team": home_team or "", "away_team": away_team or ""})
    if not out:
        return [], "该轮页面未解析到任何比赛链接"
    return out, None


def fetch_vorbericht_lineup(match_id: int, rate_limit_sec: float | None = None) -> dict[str, Any] | None:
    """
    请求 Transfermarkt 赛前预览页（vorbericht），解析「User's formations」中的两队预计首发球员名与阵型。
    返回 {"home_team": str, "away_team": str, "predicted_home_starting": [str], "predicted_away_starting": [str], "formation_home": str, "formation_away": str} 或 None。
    """
    url = f"{BASE_URL}/vorbericht/index/spielbericht/{match_id}"
    sec = rate_limit_sec if rate_limit_sec is not None else _rate_limit_sec()
    html, _ = fetch_html(url, sleep_after=True, rate_limit_sec=sec)
    if not html or not BeautifulSoup:
        return None
    return parse_vorbericht_html(html)


def parse_vorbericht_html(html: str) -> dict[str, Any] | None:
    """从 vorbericht 页 HTML 解析 User's formations：两队阵型 + 球员名列表。"""
    if not BeautifulSoup:
        return None
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, Any] = {
        "home_team": "",
        "away_team": "",
        "predicted_home_starting": [],
        "predicted_away_starting": [],
        "formation_home": None,
        "formation_away": None,
    }
    # 球员链接: /profil/spieler/数字
    all_player_links = soup.find_all("a", href=re.compile(r"/profil/spieler/\d+"))
    player_names = []
    for a in all_player_links:
        name = (a.get_text() or "").strip()
        if name and len(name) < 40 and name not in ("Post your formation now",):
            player_names.append(name)
    # 通常前 11 为主队，后 11 为客队（用户阵型各 11 人）
    if len(player_names) >= 22:
        out["predicted_home_starting"] = player_names[:11]
        out["predicted_away_starting"] = player_names[11:22]
    elif len(player_names) >= 11:
        out["predicted_home_starting"] = player_names[:11]
        out["predicted_away_starting"] = player_names[11:]
    else:
        out["predicted_home_starting"] = player_names[:11]
        out["predicted_away_starting"] = []

    # 阵型: 数字-数字-数字
    formation_re = re.compile(r"^(\d-\d-\d(-\d)?)$")
    for elem in soup.find_all(string=formation_re):
        s = (elem.strip() if hasattr(elem, "strip") else str(elem)).strip()
        if formation_re.match(s):
            if out["formation_home"] is None:
                out["formation_home"] = s
            else:
                out["formation_away"] = s
                break

    # 队名：从 startseite/verein 链接取
    for a in soup.find_all("a", href=re.compile(r"/startseite/verein/\d+")):
        t = (a.get_text() or "").strip()
        if t and len(t) < 50 and "Post your" not in t:
            if not out["home_team"]:
                out["home_team"] = t
            else:
                out["away_team"] = t
                break
    return out


def _norm_team(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", "", s)
    for x in ("fc", "cf", "cfc", "afc", "sc", "united", "city", "hotspur", "spurs"):
        if s.endswith(x) and len(s) > len(x):
            s = s[: -len(x)]
    return s


def fuzzy_match_jc_to_tm(
    jc_home: str,
    jc_away: str,
    jc_home_en: str | None,
    jc_away_en: str | None,
    tm_matches: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """将竞彩场次与 Transfermarkt 当日列表模糊匹配。优先用英文名，否则用中文名与 TM 队名匹配。"""
    if not tm_matches:
        return None
    # 优先用英文名匹配（TM 多为英文/德文）
    h = _norm_team((jc_home_en or jc_home or ""))
    a = _norm_team((jc_away_en or jc_away or ""))
    if not h and not a:
        return None
    for m in tm_matches:
        th = _norm_team((m.get("home_team") or ""))
        ta = _norm_team((m.get("away_team") or ""))
        if not th and not ta:
            continue
        if (h in th or th in h) and (a in ta or ta in a):
            return m
        if (h in ta or ta in h) and (a in th or th in a):
            return m
    return None


def run_crawl_for_jc_date(
    jc_date: str,
    jc_matches: list[dict[str, Any]],
    save_to_db: bool = True,
    rate_limit_sec: float | None = None,
    max_requests_per_run: int | None = None,
    tm_list_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    按竞彩日拉取 Transfermarkt 赛前用户阵型并落库。
    jc_matches 每项含 match_id(sporttery_match_id), home_cn, away_cn, home_en, away_en（可选）。
    tm_list_override: 若提供则不再请求当日赛程页，直接使用该列表（如来自联赛轮次页）。
    返回 {"saved": n, "matched": n, "failed": [], "message": str, "rate_limited": bool}。
    """
    from db.self_improvement import transfermarkt_lineups_save

    date_only = (jc_date or "").strip()[:10]
    if not date_only:
        return {"saved": 0, "matched": 0, "failed": [], "message": "无效日期", "rate_limited": False}
    saved = 0
    matched = 0
    failed: list[str] = []
    rate_limited = False
    sec = rate_limit_sec if rate_limit_sec is not None else _rate_limit_sec()
    max_req = max_requests_per_run if max_requests_per_run is not None else _max_requests_per_run()
    request_count = 0

    if tm_list_override is not None:
        tm_list = tm_list_override
    else:
        tm_list, date_err = fetch_matches_for_date(date_only)
        if date_err or not tm_list:
            msg = f"当日赛程: {date_err or '无数据'}"
            return {"saved": 0, "matched": 0, "failed": [msg], "message": msg, "rate_limited": False}

    for jc in jc_matches:
        sporttery_match_id = jc.get("match_id")
        home_cn = (jc.get("home_cn") or jc.get("home") or "").strip()
        away_cn = (jc.get("away_cn") or jc.get("away") or "").strip()
        home_en = (jc.get("home_en") or "").strip() or None
        away_en = (jc.get("away_en") or "").strip() or None
        if sporttery_match_id is None:
            continue
        try:
            sporttery_match_id = int(sporttery_match_id)
        except (TypeError, ValueError):
            failed.append(f"{home_cn} vs {away_cn} (无效 mid)")
            continue

        if request_count >= max_req:
            rate_limited = True
            failed.append(f"{home_cn} vs {away_cn} (已达单次请求上限 {max_req})")
            continue

        tm = fuzzy_match_jc_to_tm(home_cn, away_cn, home_en, away_en, tm_list)
        lineup_prefetched: dict[str, Any] | None = None
        # 当日程页无队名时，按 match_id 逐个请求 vorbericht 用页内队名匹配
        if not tm and (home_en or away_en or home_cn or away_cn):
            for cand in tm_list:
                if request_count >= max_req:
                    rate_limited = True
                    break
                lineup_prefetched = fetch_vorbericht_lineup(int(cand["match_id"]), rate_limit_sec=sec)
                request_count += 1
                if not lineup_prefetched:
                    continue
                ph = _norm_team((lineup_prefetched.get("home_team") or ""))
                pa = _norm_team((lineup_prefetched.get("away_team") or ""))
                jh = _norm_team((home_en or home_cn or ""))
                ja = _norm_team((away_en or away_cn or ""))
                if (jh in ph or ph in jh) and (ja in pa or pa in ja):
                    tm = cand
                    break
                if (jh in pa or pa in jh) and (ja in ph or ph in ja):
                    tm = cand
                    break
            else:
                lineup_prefetched = None
        if not tm:
            failed.append(f"{home_cn} vs {away_cn} (未匹配到 Transfermarkt 当日场次)")
            continue

        tm_mid = tm.get("match_id")
        if tm_mid is None:
            continue
        if lineup_prefetched is not None:
            lineup = lineup_prefetched
        else:
            request_count += 1
            lineup = fetch_vorbericht_lineup(int(tm_mid), rate_limit_sec=sec)
        if not lineup or (not lineup.get("predicted_home_starting") and not lineup.get("predicted_away_starting")):
            failed.append(f"{home_cn} vs {away_cn} (vorbericht 解析无阵容)")
            continue
        matched += 1
        ph = (lineup.get("home_team") or "").strip() or home_cn
        pa = (lineup.get("away_team") or "").strip() or away_cn
        home_starting = lineup.get("predicted_home_starting") or []
        away_starting = lineup.get("predicted_away_starting") or []
        if save_to_db:
            ok = transfermarkt_lineups_save(
                sporttery_match_id=sporttery_match_id,
                jc_date=date_only,
                home_cn=home_cn,
                away_cn=away_cn,
                transfermarkt_match_id=int(tm_mid),
                predicted_home_starting=home_starting,
                predicted_away_starting=away_starting,
                formation_home=lineup.get("formation_home"),
                formation_away=lineup.get("formation_away"),
            )
            if ok:
                saved += 1

    msg = f"日期 {date_only}: 匹配 {matched} 场，落库 {saved} 场，失败 {len(failed)} 场"
    if rate_limited:
        msg += f"（已达单次请求上限 {max_req}）"
    return {"saved": saved, "matched": matched, "failed": failed, "message": msg, "rate_limited": rate_limited}
