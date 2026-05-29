"""
懂球帝预计首发抓取：按比赛列表页或 schedule API 解析 match_id，再请求 API（data/detail/match）或 prediction/lineup 页得到预计/实际首发，落库供模型使用。
与 jingcai_match_analysis 通过 sporttery_match_id 或 (jc_date, home_cn, away_cn) 关联。
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

BASE_URL = "https://m.dongqiudi.com"
# 懂球帝内部 API（参考 Sz-Jason-Chen/DongQiuDiCrawler、url documentation.md）
API_BASE = "https://api.dongqiudi.com"
SPORT_DATA_BASE = "https://sport-data.dongqiudi.com"
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
REQUEST_TIMEOUT = 15
# 懂球帝 sport-data 联赛 competition_id（与 m.dongqiudi.com/stat/N 一致：英超1、意甲2、西甲3、德甲4、欧冠5、法甲10）
DONGQIUDI_COMPETITION_IDS = (1, 2, 3, 4, 5, 10)  # 英超、意甲、西甲、德甲、欧冠、法甲


def _rate_limit_sec() -> float:
    """每次请求后休眠秒数，优先从 config 读取，降低频繁抓取被禁 IP 风险。"""
    try:
        import config
        return max(0.5, float(getattr(config, "DONGQIUDI_RATE_LIMIT_SEC", 2.5)))
    except Exception:
        return 2.5


def _normalize_lineup_items(items: list[Any]) -> list[dict[str, Any]]:
    """将名单（str 或 dict）转为与 match_lineups 一致：每项 {player_id, player_name, number}。"""
    out: list[dict[str, Any]] = []
    for x in items or []:
        if isinstance(x, dict):
            name = (x.get("player_name") or x.get("person") or x.get("name") or "").strip() or None
            num = x.get("number") or x.get("jersey_number")
            if num is not None and not isinstance(num, int):
                try:
                    num = int(num)
                except (TypeError, ValueError):
                    num = None
            out.append({"player_id": x.get("player_id") or x.get("id"), "player_name": name, "number": num})
        elif isinstance(x, str) and (x or "").strip():
            out.append({"player_id": None, "player_name": (x or "").strip(), "number": None})
    return out


def pred_to_lineup_format(pred: dict[str, Any]) -> dict[str, Any]:
    """
    将懂球帝/HTML 解析的 pred 转为与 match_lineups 相同结构，便于写入 pre_match_lineups。
    返回含 home_starting, away_starting, home_substitutes, away_substitutes（list[dict]），
    以及 home_starting_count, away_starting_count。
    """
    fmt = pred.get("_lineup_format")
    if fmt:
        return {
            "home_starting": fmt.get("home_starting") or [],
            "away_starting": fmt.get("away_starting") or [],
            "home_substitutes": fmt.get("home_substitutes") or [],
            "away_substitutes": fmt.get("away_substitutes") or [],
            "home_starting_count": len(fmt.get("home_starting") or []),
            "away_starting_count": len(fmt.get("away_starting") or []),
        }
    # 来自 HTML 或旧 API：predicted_* / home_substitutes 为 list[str]
    home_s = _normalize_lineup_items(list(pred.get("predicted_home_starting") or []))
    away_s = _normalize_lineup_items(list(pred.get("predicted_away_starting") or []))
    home_sub = _normalize_lineup_items(list(pred.get("home_substitutes") or []))
    away_sub = _normalize_lineup_items(list(pred.get("away_substitutes") or []))
    return {
        "home_starting": home_s,
        "away_starting": away_s,
        "home_substitutes": home_sub,
        "away_substitutes": away_sub,
        "home_starting_count": len(home_s),
        "away_starting_count": len(away_s),
    }


def _session():
    s = requests.Session() if requests else None
    if s:
        s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/json"})
    return s


def fetch_json(url: str, sleep_after: bool = False, rate_limit_sec: float | None = None) -> dict[str, Any] | list[Any] | None:
    """请求懂球帝 API，返回解析后的 JSON；失败返回 None。"""
    if not requests:
        return None
    try:
        r = _session().get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        out = r.json()
        if sleep_after:
            sec = rate_limit_sec if rate_limit_sec is not None else _rate_limit_sec()
            time.sleep(sec)
        return out
    except Exception:
        return None


def fetch_match_detail_api(
    dongqiudi_match_id: int,
    rate_limit_sec: float | None = None,
) -> dict[str, Any] | None:
    """
    请求懂球帝单场详情 API，解析主客队名与首发/替补名单（含预计或实际）。
    接口：api.dongqiudi.com/data/detail/match/{id}，含 matchFormation.team_A/B.lineups + sub。
    返回与 parse_prediction_html 一致结构；无阵容时返回 None 或名单为空。
    """
    url = f"{API_BASE}/data/detail/match/{dongqiudi_match_id}"
    data = fetch_json(url, sleep_after=True, rate_limit_sec=rate_limit_sec)
    if not data or not isinstance(data, dict):
        return None
    sample = data.get("matchSample") or {}
    formation = data.get("matchFormation") or {}
    team_a = formation.get("team_A") or {}
    team_b = formation.get("team_B") or {}
    home_name = (sample.get("team_A_name") or "").strip()
    away_name = (sample.get("team_B_name") or "").strip()
    def _to_starting_item(p: dict) -> str | dict:
        name = (p.get("person") or "").strip()
        if not name:
            return ""
        num = p.get("number") or p.get("jersey_number")
        if num is not None and not isinstance(num, int):
            try:
                num = int(num)
            except (TypeError, ValueError):
                num = None
        if num is not None:
            return {"player_name": name, "number": num, "player_id": p.get("id") or None}
        return name

    def _to_list(items: list, max_len: int = 15) -> list:
        out: list[Any] = []
        for p in (items or [])[:max_len]:
            if not p:
                continue
            v = _to_starting_item(p) if isinstance(p, dict) else (p if isinstance(p, str) else "")
            if v:
                out.append(v)
        return out

    home_starting_raw = _to_list(team_a.get("lineups") or [], 15)
    away_starting_raw = _to_list(team_b.get("lineups") or [], 15)
    home_sub_raw = _to_list(team_a.get("sub") or [], 12)
    away_sub_raw = _to_list(team_b.get("sub") or [], 12)
    # 兼容下游：仍提供 list[str] 的 predicted_*，若为 dict 则只取 player_name
    home_starting = [x.get("player_name", x) if isinstance(x, dict) else x for x in home_starting_raw]
    away_starting = [x.get("player_name", x) if isinstance(x, dict) else x for x in away_starting_raw]
    home_sub = [x.get("player_name", x) if isinstance(x, dict) else x for x in home_sub_raw]
    away_sub = [x.get("player_name", x) if isinstance(x, dict) else x for x in away_sub_raw]
    return {
        "home_team": home_name,
        "away_team": away_name,
        "predicted_home_starting": home_starting,
        "predicted_away_starting": away_starting,
        "formation_home": team_a.get("formation") or None,
        "formation_away": team_b.get("formation") or None,
        "home_substitutes": home_sub,
        "away_substitutes": away_sub,
        "_lineup_format": {
            "home_starting": _normalize_lineup_items(home_starting_raw),
            "away_starting": _normalize_lineup_items(away_starting_raw),
            "home_substitutes": _normalize_lineup_items(home_sub_raw),
            "away_substitutes": _normalize_lineup_items(away_sub_raw),
        },
    }


def fetch_match_list_by_date_api(
    jc_date: str,
    rate_limit_sec: float | None = None,
    max_leagues: int = 3,
    gameweeks: tuple[int, ...] = (27, 28, 29),
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """
    从懂球帝 schedule API 按日期聚合比赛列表（当 HTML 列表页解析不到链接时使用）。
    请求 sport-data 的 seasons + schedule（按 round_id+gameweek），只取 date_utc 等于 jc_date 的场次。
    返回 [{"match_id": int, "match_date": str, "home_team": str, "away_team": str}, ...]。
    max_leagues: 最多请求几个联赛（competition_id 顺序取）；gameweeks: 请求哪几轮（减少请求量）。
    verbose: 是否打印进度（联赛/轮次），避免长时间无输出被误认为卡住。
    """
    date_only = (jc_date or "").strip()[:10]
    if not date_only or not requests:
        return []
    sec = rate_limit_sec if rate_limit_sec is not None else _rate_limit_sec()
    out: list[dict[str, Any]] = []
    seen_mid: set[int] = set()
    comp_ids = list(DONGQIUDI_COMPETITION_IDS)[:max_leagues]
    for comp_idx, comp_id in enumerate(comp_ids):
        if verbose:
            print(f"    schedule API 联赛 {comp_idx + 1}/{len(comp_ids)} (id={comp_id}) …", flush=True)
        seasons_url = f"{SPORT_DATA_BASE}/soccer/biz/data/seasons?competition_id={comp_id}&app=dqd&version=0&lang=zh-cn"
        seasons_data = fetch_json(seasons_url, sleep_after=True, rate_limit_sec=sec)
        if not isinstance(seasons_data, list) or not seasons_data:
            continue
        for season_item in seasons_data[:2]:
            season_id = season_item.get("season_id")
            if not season_id:
                continue
            schedule_list_url = f"{SPORT_DATA_BASE}/soccer/biz/data/schedule?season_id={season_id}&app=dqd&platform=&version=0&lang=zh-cn"
            schedule_list = fetch_json(schedule_list_url, sleep_after=True, rate_limit_sec=sec)
            if not isinstance(schedule_list, dict):
                continue
            content = schedule_list.get("content") or {}
            rounds_list = content.get("rounds") or []
            round_id = None
            for r in rounds_list:
                params = r.get("params") or {}
                gw = params.get("gameweek")
                if gw is not None and isinstance(gw, int) and 1 <= gw <= 42:
                    round_id = params.get("round_id")
                    break
            if not round_id and rounds_list:
                round_id = (rounds_list[0].get("params") or {}).get("round_id")
            if not round_id:
                continue
            for gw in gameweeks:
                schedule_url = (
                    f"{SPORT_DATA_BASE}/soccer/biz/data/schedule?season_id={season_id}&round_id={round_id}"
                    f"&gameweek={gw}&app=dqd&platform=&version=0&lang=zh-cn"
                )
                sched = fetch_json(schedule_url, sleep_after=True, rate_limit_sec=sec)
                if not isinstance(sched, dict):
                    continue
                content = sched.get("content") or {}
                for m in content.get("matches") or []:
                    mid = m.get("match_id")
                    date_utc = (m.get("date_utc") or "")[:10]
                    if date_utc != date_only or not mid:
                        continue
                    try:
                        mid_int = int(mid)
                    except (TypeError, ValueError):
                        continue
                    if mid_int in seen_mid:
                        continue
                    seen_mid.add(mid_int)
                    out.append({
                        "match_id": mid_int,
                        "match_date": date_only,
                        "home_team": (m.get("team_A_name") or "").strip(),
                        "away_team": (m.get("team_B_name") or "").strip(),
                    })
        if verbose and comp_ids:
            print(f"    当前共 {len(out)} 场。", flush=True)
    return out


def fetch_html(url: str, sleep_after: bool = False, rate_limit_sec: float | None = None) -> str | None:
    """请求懂球帝页面，返回 HTML 或 None。sleep_after 为 True 时请求后休眠，降低封 IP 风险。"""
    if not requests:
        return None
    try:
        r = _session().get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        out = r.text
        if sleep_after:
            sec = rate_limit_sec if rate_limit_sec is not None else _rate_limit_sec()
            time.sleep(sec)
        return out
    except Exception:
        return None


def parse_match_list_from_html(html: str) -> list[dict[str, Any]]:
    """
    从懂球帝比赛列表页 HTML 解析出 (match_id, date, home_team, away_team)。
    返回 [{"match_id": int, "match_date": "YYYY-MM-DD"|None, "home_team": str|None, "away_team": str|None}, ...]。
    """
    out: list[dict[str, Any]] = []
    if not html or not BeautifulSoup:
        return out
    soup = BeautifulSoup(html, "html.parser")
    current_date: str | None = None
    # 日期：常见为 2026-03-06 或 2026-03-06 星期五
    date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")
    # 所有含 matchDetail 的链接
    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        mid_match = re.search(r"/matchDetail/(\d+)", href)
        if not mid_match:
            continue
        match_id = int(mid_match.group(1))
        link_text = (a.get_text() or "").strip()
        home_team: str | None = None
        away_team: str | None = None
        if link_text and ("vs" in link_text or " " in link_text or "\n" in link_text):
            parts = re.split(r"\s+vs\s+|\s+-\s+|\n", link_text, maxsplit=1)
            if len(parts) >= 2:
                home_team = parts[0].strip()
                away_team = parts[1].strip()
            else:
                parts = link_text.split()
                if len(parts) >= 2:
                    home_team = parts[0]
                    away_team = parts[-1]
        # 尝试从父节点找日期（同一区块内的日期标题）
        prev = a.find_previous(string=date_re)
        if prev and date_re.search(str(prev)):
            m = date_re.search(str(prev))
            if m:
                current_date = m.group(1)
        out.append({
            "match_id": match_id,
            "match_date": current_date,
            "home_team": home_team,
            "away_team": away_team,
        })
    return out


def parse_match_ids_and_dates_from_html(html: str) -> list[tuple[int, str | None]]:
    """
    仅解析 match_id 与所属日期（按文档顺序，遇到日期标题则更新当前日期）。
    返回 [(match_id, YYYY-MM-DD or None), ...]。轻量解析，不依赖链接内队名。
    """
    out: list[tuple[int, str | None]] = []
    if not html or not BeautifulSoup:
        return out
    soup = BeautifulSoup(html, "html.parser")
    current_date: str | None = None
    date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")
    # 按文档顺序遍历：先找日期标题，再找 matchDetail 链接
    for elem in soup.descendants:
        if elem.name and elem.get_text():
            text = elem.get_text().strip()
            if date_re.match(text):
                m = date_re.search(text)
                if m:
                    current_date = m.group(1)
        if elem.name == "a" and elem.get("href"):
            href = elem.get("href", "")
            mid_match = re.search(r"/matchDetail/(\d+)", href)
            if mid_match:
                out.append((int(mid_match.group(1)), current_date))
    return out


def fetch_prediction_for_match(
    dongqiudi_match_id: int,
    rate_limit_sec: float | None = None,
) -> dict[str, Any] | None:
    """
    请求懂球帝单场阵容：优先用 API（data/detail/match）解析首发/替补，失败再请求 prediction/lineup 页 HTML。
    返回 {"home_team": str, "away_team": str, "predicted_home_starting": [str], "predicted_away_starting": [str], "formation_home": str|None, "formation_away": str|None, ...} 或 None。
    """
    sec = rate_limit_sec if rate_limit_sec is not None else _rate_limit_sec()
    api_result = fetch_match_detail_api(dongqiudi_match_id, rate_limit_sec=sec)
    if api_result and (api_result.get("predicted_home_starting") or api_result.get("predicted_away_starting")):
        return api_result
    url = f"{BASE_URL}/matchDetail/{dongqiudi_match_id}/prediction"
    html = fetch_html(url, sleep_after=True, rate_limit_sec=sec)
    if not html:
        url_lineup = f"{BASE_URL}/matchDetail/{dongqiudi_match_id}/lineup"
        html = fetch_html(url_lineup, sleep_after=True, rate_limit_sec=sec)
    if not html:
        return api_result
    return parse_prediction_html(html, dongqiudi_match_id)


def parse_prediction_html(html: str, match_id: int | None = None) -> dict[str, Any] | None:
    """
    从 prediction 或 lineup 页 HTML 解析主客队名与预计首发（球员名列表）。
    懂球帝页面结构可能含「预计首发」、阵型、球员名；此处用常见 class/文案做兼容解析。
    """
    if not html or not BeautifulSoup:
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
    # 标题中常含「主 vs 客」或「主 - 客」
    title = soup.find("title")
    if title and title.get_text():
        t = title.get_text().strip()
        for sep in (" vs ", " - ", " VS ", "：", ":"):
            if sep in t:
                parts = re.split(re.escape(sep), t, maxsplit=1)
                if len(parts) >= 2:
                    out["home_team"] = parts[0].strip()
                    out["away_team"] = parts[1].strip()
                    break
    # 预计首发：常见为两列名单或带「预计首发」的区块内列表
    def _extract_names_from_block(block) -> list[str]:
        names: list[str] = []
        if not block:
            return names
        for node in (block.find_all(["p", "span", "li", "div"]) if hasattr(block, "find_all") else [block]):
            text = (node.get_text() if hasattr(node, "get_text") else str(node)).strip()
            if not text or len(text) > 30 or re.match(r"^\d+$", text):
                continue
            if any(k in text for k in ("预计", "首发", "阵型", "教练", "vs", "比赛")):
                continue
            names.append(text)
        return names[:15]  # 最多 11 首发 + 少量替补

    # 尝试按 class 或文案定位
    for hint in ["预计首发", "prediction", "lineup", "首发"]:
        block = soup.find(string=re.compile(hint))
        if block and block.parent:
            parent = block.parent
            for _ in range(5):
                if parent is None:
                    break
                next_divs = parent.find_all("div", recursive=False) if hasattr(parent, "find_all") else []
                if len(next_divs) >= 2:
                    out["predicted_home_starting"] = _extract_names_from_block(next_divs[0])
                    out["predicted_away_starting"] = _extract_names_from_block(next_divs[1])
                    break
                parent = getattr(parent, "parent", None)
        if out["predicted_home_starting"] or out["predicted_away_starting"]:
            break

    # 若无明确两列，尝试整页中成段的短句（球员名通常 2～4 个汉字或英文名）
    if not out["predicted_home_starting"] and not out["predicted_away_starting"]:
        all_text = soup.get_text() if hasattr(soup, "get_text") else ""
        # 简单启发：连续 2～4 个汉字或英文单词可能是球员名（与「预计」「首发」等相邻的段落）
        lines = [ln.strip() for ln in all_text.splitlines() if 2 <= len(ln.strip()) <= 20 and " " not in ln.strip()[:2]]
        for line in lines:
            if re.match(r"^[\u4e00-\u9fff·]+$", line) or (line and line[0].isalpha()):
                if not out["predicted_home_starting"] or len(out["predicted_home_starting"]) < 11:
                    out["predicted_home_starting"].append(line)
                elif len(out["predicted_away_starting"]) < 11:
                    out["predicted_away_starting"].append(line)
                if len(out["predicted_home_starting"]) >= 11 and len(out["predicted_away_starting"]) >= 11:
                    break

    return out


def _norm_team_name(s: str) -> str:
    """队名归一化用于模糊匹配。"""
    if not s:
        return ""
    s = (s or "").strip().lower().replace(" ", "").replace("　", "")
    for suffix in ("队", "fc", "cf", "联", "城", "竞技", "俱乐部"):
        if len(s) > len(suffix) and s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


# 竞彩/体彩常用简写 -> 懂球帝或通用写法中的关键字（归一化后用于包含匹配）
TEAM_ALIASES: dict[str, list[str]] = {
    "曼联": ["曼彻斯特联", "曼联"],
    "西汉姆联": ["西汉姆", "西汉姆联"],
    "布赖顿": ["布莱顿", "布赖顿"],
    "门兴": ["门兴格拉德巴赫", "门兴"],
    "伊普斯": ["伊普斯维奇", "伊普斯"],
    "莱红牛": ["莱比锡", "rb莱比锡", "莱比锡红牛"],
    "沃夫斯堡": ["沃尔夫斯堡", "狼堡"],
    "墨胜利": ["墨尔本胜利", "墨尔本"],
    "比利亚雷": ["比利亚雷亚尔", "比利亚雷"],
    "杜塞多夫": ["杜塞尔多夫"],
    "莱比锡": ["莱比锡", "rb莱比锡", "莱红牛"],
    "沃尔夫斯堡": ["沃尔夫斯堡", "沃夫斯堡", "狼堡"],
    "霍芬海姆": ["霍芬海姆", "霍村"],
    "勒沃库森": ["勒沃库森", "药厂"],
    "斯图加特": ["斯图加特"],
    "奥格斯堡": ["奥格斯堡"],
    "美因茨": ["美因茨"],
    "弗赖堡": ["弗赖堡", "弗莱堡"],
    "海登海姆": ["海登海姆"],
    "赫尔城": ["赫尔城", "赫尔"],
    "米尔沃尔": ["米尔沃尔"],
    "奥萨苏纳": ["奥萨苏纳"],
    "马洛卡": ["马洛卡", "马略卡"],
    "卡利亚里": ["卡利亚里"],
    "科莫": ["科莫"],
    "纽伦堡": ["纽伦堡"],
    "帕德博恩": ["帕德博恩"],
    "不伦瑞克": ["不伦瑞克", "布伦瑞克"],
    "汉堡": ["汉堡"],
    "光州fc": ["光州", "光州fc"],
    "仁川联": ["仁川", "仁川联"],
    "悉尼fc": ["悉尼", "悉尼fc"],
    "大阪樱花": ["大阪樱花", "樱花"],
    "清水鼓动": ["清水鼓动", "清水"],
    "鹿岛鹿角": ["鹿岛", "鹿岛鹿角"],
    "东京绿茵": ["东京绿茵", "东京"],
}


def _norm_and_aliases(name: str) -> list[str]:
    """返回归一化队名及其别名列表，用于与懂球帝队名做包含匹配。"""
    n = _norm_team_name(name)
    out = [n] if n else []
    if not n:
        return out
    for jc_key, aliases in TEAM_ALIASES.items():
        jc_norm = _norm_team_name(jc_key)
        if jc_norm and (n == jc_norm or n in jc_norm or jc_norm in n):
            for a in aliases:
                a_norm = _norm_team_name(a)
                if a_norm and a_norm not in out:
                    out.append(a_norm)
            break
    return out


def _team_names_match(jc_name: str, dq_name: str) -> bool:
    """竞彩队名与懂球帝队名是否可视为同一队（含别名、包含关系）。"""
    jc_candidates = _norm_and_aliases(jc_name)
    dq_norm = _norm_team_name(dq_name)
    if not dq_norm:
        return False
    for c in jc_candidates:
        if not c:
            continue
        if (c in dq_norm or dq_norm in c):
            return True
    return False


def fuzzy_match_jc_to_dongqiudi(
    jc_home: str,
    jc_away: str,
    dongqiudi_matches: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """
    在懂球帝比赛列表中模糊匹配一场 (jc_home, jc_away)。
    dongqiudi_matches 每项含 home_team, away_team, match_id。
    返回匹配到的那条或 None。使用队名别名与包含关系匹配。
    """
    if not dongqiudi_matches:
        return None
    if not (jc_home or jc_away):
        return None
    for m in dongqiudi_matches:
        dh = (m.get("home_team") or "").strip()
        da = (m.get("away_team") or "").strip()
        if not dh and not da:
            continue
        if _team_names_match(jc_home, dh) and _team_names_match(jc_away, da):
            return m
        if _team_names_match(jc_home, da) and _team_names_match(jc_away, dh):
            return m
    return None


def _max_requests_per_run() -> int:
    """单次命令最多请求多少场 prediction 页，防止一次跑太多触发限流/封 IP。"""
    try:
        import config
        return max(5, int(getattr(config, "DONGQIUDI_MAX_REQUESTS_PER_RUN", 25)))
    except Exception:
        return 25


def run_crawl_for_jc_date(
    jc_date: str,
    jc_matches: list[dict[str, Any]],
    save_to_db: bool = True,
    rate_limit_sec: float | None = None,
    max_requests_per_run: int | None = None,
    save_as_lineups_format: bool = True,
) -> dict[str, Any]:
    """
    按竞彩日拉取懂球帝预计首发并落库。
    jc_matches: 该日竞彩场次列表，每项含 match_id(sporttery_match_id), home_cn, away_cn。
    rate_limit_sec: 每次请求后休眠秒数，不传则用 config DONGQIUDI_RATE_LIMIT_SEC，降低封 IP 风险。
    max_requests_per_run: 单次最多请求多少场 prediction 页，超出后不再请求新场次（列表页不计）。
    save_as_lineups_format: 为 True 时同时写入 pre_match_lineups（与 match_lineups 相同结构），便于赛前/赛后对比。
    返回 {"saved": n, "matched": n, "failed": [], "message": str, "rate_limited": bool}。
    """
    from db.self_improvement import dongqiudi_lineups_save, pre_lineups_save

    date_only = (jc_date or "").strip()[:10]
    if not date_only:
        return {"saved": 0, "matched": 0, "failed": [], "message": "无效日期", "rate_limited": False}
    saved = 0
    matched = 0
    failed: list[str] = []
    rate_limited = False
    sec = rate_limit_sec if rate_limit_sec is not None else _rate_limit_sec()
    max_req = max_requests_per_run if max_requests_per_run is not None else _max_requests_per_run()
    request_count = 0  # 只统计 prediction/lineup 页请求数，列表页不计

    print(f"懂球帝阵容拉取 {date_only}：请求间隔 {sec}s、单次最多 {max_req} 场阵容请求（防封 IP）。", flush=True)
    print("正在获取比赛列表…", flush=True)
    html = fetch_html(f"{BASE_URL}/match", sleep_after=True, rate_limit_sec=sec)
    id_and_dates: list[tuple[int, str | None]] = []
    dongqiudi_list: list[dict[str, Any]] = []
    if html:
        id_and_dates = parse_match_ids_and_dates_from_html(html)
        dongqiudi_list = parse_match_list_from_html(html)
        print(f"  列表页解析到 {len(id_and_dates)} 个链接、{len(dongqiudi_list)} 场带队名。", flush=True)
    match_ids_for_date = [mid for mid, d in id_and_dates if d == date_only]
    if not match_ids_for_date:
        match_ids_for_date = [mid for mid, _ in id_and_dates]
    dongqiudi_by_date = [m for m in dongqiudi_list if m.get("match_date") == date_only] if dongqiudi_list else []
    if not dongqiudi_by_date and dongqiudi_list:
        dongqiudi_by_date = dongqiudi_list
    if not id_and_dates and not dongqiudi_list:
        print("  列表页无链接，改用 schedule API 按日期拉取（6 联赛、多轮次，约 2～4 分钟）…", flush=True)
        dongqiudi_list = fetch_match_list_by_date_api(
            date_only, rate_limit_sec=sec,
            max_leagues=6, gameweeks=(26, 27, 28, 29, 30),
            verbose=True,
        )
        dongqiudi_by_date = [m for m in dongqiudi_list if (m.get("match_date") or "")[:10] == date_only] or dongqiudi_list
        match_ids_for_date = [m["match_id"] for m in dongqiudi_list if m.get("match_id")]
        print(f"  schedule API 返回 {len(dongqiudi_list)} 场。", flush=True)

    list_hint = ""
    if not match_ids_for_date and not dongqiudi_by_date:
        list_hint = "（列表页未解析到比赛链接且 schedule API 未返回该日场次，可稍后重试或检查网络）"
    elif not match_ids_for_date:
        list_hint = "（无该日 match_id 列表，将无法逐场拉取阵容）"

    total_jc = len([j for j in jc_matches if j.get("match_id") is not None])
    print(f"该日竞彩 {total_jc} 场，开始逐场匹配并拉取阵容（每场约 {sec:.1f}s）…", flush=True)
    for idx, jc in enumerate(jc_matches):
        sporttery_match_id = jc.get("match_id")
        home_cn = (jc.get("home_cn") or jc.get("home") or "").strip()
        away_cn = (jc.get("away_cn") or jc.get("away") or "").strip()
        if sporttery_match_id is None:
            continue
        try:
            sporttery_match_id = int(sporttery_match_id)
        except (TypeError, ValueError):
            failed.append(f"{home_cn} vs {away_cn} (无效 mid)")
            continue

        if request_count >= max_req:
            rate_limited = True
            failed.append(f"{home_cn} vs {away_cn} (已达单次请求上限 {max_req}，请稍后再跑或调大 DONGQIUDI_MAX_REQUESTS_PER_RUN)")
            continue

        print(f"  [{idx + 1}/{total_jc}] {home_cn} vs {away_cn} …", flush=True)
        # 优先用列表页解析出的 (match_id, home, away) 做模糊匹配
        dq = fuzzy_match_jc_to_dongqiudi(home_cn, away_cn, dongqiudi_by_date)
        pred = None
        list_has_team_names = any((m.get("home_team") or m.get("away_team")) for m in dongqiudi_by_date)
        if not dq and not list_has_team_names:
            # 仅当列表无队名时：按 match_id 逐个请求详情取队名再匹配（消耗请求额度）
            for mid in match_ids_for_date[:50]:
                if request_count >= max_req:
                    rate_limited = True
                    break
                pred = fetch_prediction_for_match(mid, rate_limit_sec=sec)
                request_count += 1
                if not pred:
                    continue
                ph = (pred.get("home_team") or "").strip()
                pa = (pred.get("away_team") or "").strip()
                if _team_names_match(home_cn, ph) and _team_names_match(away_cn, pa):
                    dq = {"match_id": mid, "home_team": ph, "away_team": pa}
                    break
            else:
                pred = None
        if not dq:
            failed.append(f"{home_cn} vs {away_cn} (未匹配到懂球帝)")
            continue

        dq_mid = dq.get("match_id")
        if dq_mid is None:
            failed.append(f"{home_cn} vs {away_cn} (无 dq match_id)")
            continue
        matched += 1
        if not pred:
            if request_count >= max_req:
                rate_limited = True
                failed.append(f"{home_cn} vs {away_cn} (已达单次请求上限 {max_req})")
                continue
            pred = fetch_prediction_for_match(int(dq_mid), rate_limit_sec=sec)
            request_count += 1
        if not pred:
            failed.append(f"{home_cn} vs {away_cn} (预测页解析失败)")
            continue
        ph = (pred.get("home_team") or "").strip() or home_cn
        pa = (pred.get("away_team") or "").strip() or away_cn
        home_starting = pred.get("predicted_home_starting") or []
        away_starting = pred.get("predicted_away_starting") or []
        if save_to_db:
            ok = dongqiudi_lineups_save(
                sporttery_match_id=sporttery_match_id,
                jc_date=date_only,
                home_cn=home_cn,
                away_cn=away_cn,
                dongqiudi_match_id=int(dq_mid),
                predicted_home_starting=home_starting,
                predicted_away_starting=away_starting,
                formation_home=pred.get("formation_home"),
                formation_away=pred.get("formation_away"),
            )
            if ok:
                saved += 1
        if save_to_db and save_as_lineups_format:
            lineup_data = pred_to_lineup_format(pred)
            pre_lineups_save(
                match_id=sporttery_match_id,
                home_starting_count=lineup_data.get("home_starting_count"),
                away_starting_count=lineup_data.get("away_starting_count"),
                home_starting=lineup_data.get("home_starting"),
                away_starting=lineup_data.get("away_starting"),
                home_substitutes=lineup_data.get("home_substitutes"),
                away_substitutes=lineup_data.get("away_substitutes"),
                source="dongqiudi",
            )

    msg = f"日期 {date_only}: 匹配 {matched} 场，落库 {saved} 场，失败 {len(failed)} 场"
    if list_hint:
        msg += " " + list_hint
    if rate_limited:
        msg += f"（已达单次请求上限 {max_req}，避免封 IP）"
    return {
        "saved": saved,
        "matched": matched,
        "failed": failed,
        "message": msg,
        "rate_limited": rate_limited,
    }
