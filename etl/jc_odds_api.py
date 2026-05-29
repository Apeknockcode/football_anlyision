"""
第三方竞彩赔率 API 接入模块。
获取让球数（rq）、胜平负、比分、总进球、半全场等，用于补全体彩网缺失的 rqspf 等字段。

配置（config.py 或 .env）：
- JC_ODDS_API_BASE: API 根地址，如 https://api.xxx.com
- JC_ODDS_API_TOKEN: 鉴权 token
- JC_ODDS_API_PATH: 竞彩指数路径，默认 /sport/api/v1/lot/jc/odds

常见返回格式（以常见第三方竞彩 API 为例）：
- rq: "-2,3.00,4.05,1.79" -> 让球数,主胜,平,客胜
- spf: "1.85,3.20,4.00" -> 主胜,平,客胜
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

try:
    import requests
except ImportError:
    requests = None

import config


def _parse_rq_str(s: str) -> dict[str, Any] | None:
    """
    解析让球字符串为统一 odds 格式。
    格式示例: "-2,3.00,4.05,1.79" 或 "1,1.72,3.45,3.90"
    """
    if not s or not isinstance(s, str):
        return None
    parts = [x.strip() for x in s.replace("，", ",").split(",")]
    if len(parts) < 4:
        return None
    try:
        hc = float(parts[0])
        if hc == int(hc):
            hc = int(hc)
        return {
            "rqspf_handicap": hc,
            "rqspf_win": parts[1],
            "rqspf_draw": parts[2],
            "rqspf_loss": parts[3],
        }
    except (ValueError, IndexError):
        return None


# 联赛名 → 五大联赛中文（用于 match_odds.league_cn）
_LEAGUE_TO_CN: dict[str, str] = {
    "英超": "英超", "Premier League": "英超", "PL": "英超", "英格兰超级联赛": "英超",
    "西甲": "西甲", "La Liga": "西甲", "西甲": "西甲", "西班牙甲级联赛": "西甲",
    "意甲": "意甲", "Serie A": "意甲", "意大利甲级联赛": "意甲",
    "德甲": "德甲", "Bundesliga": "德甲", "德国甲级联赛": "德甲",
    "法甲": "法甲", "Ligue 1": "法甲", "法国甲级联赛": "法甲",
}


def _norm_league_cn(league_raw: str) -> str:
    """将 API 返回的联赛名规范为五大联赛中文，无法识别时返回空（调用方可不写或写「其他」）。"""
    if not league_raw:
        return ""
    key = league_raw.strip()
    if key in _LEAGUE_TO_CN:
        return _LEAGUE_TO_CN[key]
    for k, v in _LEAGUE_TO_CN.items():
        if k in key or key in k:
            return v
    return ""


def _parse_spf_str(s: str) -> dict[str, Any] | None:
    """解析胜平负字符串。格式: "1.85,3.20,4.00" -> 主胜,平,客胜"""
    if not s or not isinstance(s, str):
        return None
    parts = [x.strip() for x in s.replace("，", ",").split(",")]
    if len(parts) < 3:
        return None
    return {"spf_win": parts[0], "spf_draw": parts[1], "spf_loss": parts[2]}


def _parse_bf_obj(bf: dict | str | list) -> dict[str, str] | None:
    """解析比分赔率，返回 { "1:0": "6.50", "2:1": "7.20", ... }"""
    if isinstance(bf, dict):
        out = {}
        for k, v in bf.items():
            if k and v is not None:
                out[str(k).strip()] = str(v)
        return out if out else None
    if isinstance(bf, str):
        # 可能格式 "1:0@6.50,2:1@7.20"
        out = {}
        for pair in bf.replace("，", ",").split(","):
            m = re.match(r"([\d\-]+)\s*:\s*([\d\-]+)\s*[@=]\s*([\d.]+)", pair.strip())
            if m:
                out[f"{m.group(1)}:{m.group(2)}"] = m.group(3)
        return out if out else None
    return None


def _parse_bqc_obj(bqc: dict | str) -> dict[str, str] | None:
    """解析半全场赔率，如 {"胜胜": "3.2", "胜平": "15.0", ...}"""
    if isinstance(bqc, dict):
        out = {str(k).strip(): str(v) for k, v in bqc.items() if k and v is not None}
        return out if out else None
    return None


def _parse_zjqs_obj(zj: dict | str) -> dict[str, str] | None:
    """解析总进球数，如 {"0": "12", "1": "5.2", ...}"""
    if isinstance(zj, dict):
        out = {str(k).strip(): str(v) for k, v in zj.items() if k is not None and v is not None}
        return out if out else None
    return None


def _norm_name_for_match(name: str) -> str:
    """队名归一化用于匹配：去空格、小写、移除多余符号。"""
    if not name or not isinstance(name, str):
        return ""
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", (name or "").strip().lower())
    return s


def _match_team_pair(a1: str, a2: str, b1: str, b2: str) -> bool:
    """判断 (a1 vs a2) 与 (b1 vs b2) 是否为同一场比赛（主客可交换）。"""
    n1, n2 = _norm_name_for_match(a1), _norm_name_for_match(a2)
    m1, m2 = _norm_name_for_match(b1), _norm_name_for_match(b2)
    if not n1 or not n2 or not m1 or not m2:
        return False
    # 主客一致
    if (n1 == m1 and n2 == m2) or (n1 == m2 and n2 == m1):
        return True
    # 一方包含
    if (n1 in m1 or m1 in n1) and (n2 in m2 or m2 in n2):
        return True
    if (n1 in m2 or m2 in n1) and (n2 in m1 or m1 in n2):
        return True
    return False


def fetch_jc_odds_for_date(
    date_str: str,
    page: int = 1,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    """
    按日期拉取第三方竞彩赔率。
    date_str: YYYY-MM-DD
    返回: [{ home, away, match_date?, odds }, ...]
    odds 格式与 sporttery_crawl._extract_odds 一致，便于合并。
    """
    base = (config.JC_ODDS_API_BASE or "").strip()
    token = (config.JC_ODDS_API_TOKEN or "").strip()
    path = (config.JC_ODDS_API_PATH or "/sport/api/v1/lot/jc/odds").strip()
    if not base or not token:
        return []

    if not requests:
        return []

    url = base.rstrip("/") + path
    date_param = getattr(config, "JC_ODDS_DATE_PARAM", None) or "start_time"
    token_in_header = getattr(config, "JC_ODDS_TOKEN_IN_HEADER", False)

    params: dict[str, Any] = {"page": page, "per_page": per_page}
    if not token_in_header:
        params["token"] = token
    # 日期筛选：start_time/timestamp 用时间戳；matchDate/date 用 YYYY-MM-DD 字符串
    try:
        date_val: int | str = date_str[:10]
        if date_param.lower() in ("start_time", "timestamp", "time"):
            date_val = int(datetime.strptime(str(date_val), "%Y-%m-%d").timestamp())
        params[date_param] = date_val
    except Exception:
        pass

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; FootballAnalysis/1.0)",
        "Accept": "application/json",
    }
    if token_in_header:
        headers["Authorization"] = f"Bearer {token}"
        headers["Token"] = token  # 部分服务商用 Token 头

    result: list[dict[str, Any]] = []

    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []

    # 兼容多种响应结构：data.list / data.data / result / list
    items = []
    if isinstance(data, dict):
        d = data.get("data") or data
        items = d.get("list") or d.get("data") or d.get("matches") or []
        if not items and isinstance(d, list):
            items = d
    elif isinstance(data, list):
        items = data

    if not isinstance(items, list):
        return []

    for raw in items:
        if not isinstance(raw, dict):
            continue
        # 兼容多种 API：homeName/homeTeam/home_team、awayName 等
        home = (raw.get("home") or raw.get("homeTeam") or raw.get("home_team") or raw.get("homeName") or raw.get("host") or "").strip()
        away = (raw.get("away") or raw.get("awayTeam") or raw.get("away_team") or raw.get("awayName") or raw.get("guest") or "").strip()
        if not home or not away:
            continue

        md = raw.get("match_date") or raw.get("matchDate") or raw.get("matchTime") or raw.get("match_time") or raw.get("date")
        match_date = date_str[:10]
        if md:
            if isinstance(md, str) and len(md) >= 10:
                match_date = md[:10].replace("/", "-")
            elif isinstance(md, (int, float)):
                try:
                    match_date = datetime.fromtimestamp(int(md)).strftime("%Y-%m-%d")
                except Exception:
                    pass

        odds: dict[str, Any] = {}

        # 胜平负
        spf = raw.get("spf") or raw.get("winDrawLoss")
        parsed = _parse_spf_str(spf) if isinstance(spf, str) else None
        if parsed:
            odds.update(parsed)
        elif isinstance(spf, dict):
            odds["spf_win"] = spf.get("3") or spf.get("win")
            odds["spf_draw"] = spf.get("1") or spf.get("draw")
            odds["spf_loss"] = spf.get("0") or spf.get("loss")

        # 让球胜平负（核心）；常见字段 rqspfOdds、handicapOdds、rq
        rq = raw.get("rq") or raw.get("rqspf") or raw.get("handicap") or raw.get("rqspfOdds") or raw.get("handicapOdds") or raw.get("letBall")
        rq_parsed = _parse_rq_str(rq) if isinstance(rq, str) else None
        if rq_parsed:
            odds.update(rq_parsed)
        elif isinstance(rq, dict):
            odds["rqspf_handicap"] = rq.get("handicap") or rq.get("rq") or rq.get("let")
            odds["rqspf_win"] = rq.get("3") or rq.get("win")
            odds["rqspf_draw"] = rq.get("1") or rq.get("draw")
            odds["rqspf_loss"] = rq.get("0") or rq.get("loss")

        # 比分
        bf = raw.get("bf") or raw.get("scoreOdds") or raw.get("bfOdds")
        bf_parsed = _parse_bf_obj(bf)
        if bf_parsed:
            odds["bf"] = bf_parsed

        # 总进球
        jq = raw.get("jq") or raw.get("zjqs") or raw.get("totalGoals") or raw.get("jqOdds")
        zj_parsed = _parse_zjqs_obj(jq) if isinstance(jq, (dict, str)) else None
        if zj_parsed:
            odds["zjqs"] = zj_parsed

        # 半全场
        bqc = raw.get("bqc") or raw.get("halfFull") or raw.get("bqcOdds")
        bqc_parsed = _parse_bqc_obj(bqc)
        if bqc_parsed:
            odds["bqc"] = bqc_parsed

        # 联赛中文（用于写入 match_odds.league_cn）
        league_raw = (raw.get("league") or raw.get("leagueName") or raw.get("leagueAbbName") or raw.get("competition") or "").strip()
        league_cn = _norm_league_cn(league_raw)

        result.append({
            "home": home,
            "away": away,
            "match_date": match_date,
            "odds": odds,
            "league_cn": league_cn,
        })

    return result


def merge_odds_into_matches(
    matches: list[dict[str, Any]],
    jc_odds_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    将第三方赔率合并入体彩 match 列表。
    按主客队、日期匹配；第三方有而体彩无的字段（尤其是 rqspf）会补充进去。
    """
    if not jc_odds_list:
        return matches

    for m in matches:
        odds = m.get("odds") or {}
        home = (m.get("home") or m.get("home_cn") or "").strip()
        away = (m.get("away") or m.get("away_cn") or "").strip()
        match_date = (m.get("match_date") or "")[:10]

        for jc in jc_odds_list:
            if (jc.get("match_date") or "")[:10] != match_date:
                continue
            if not _match_team_pair(home, away, jc.get("home") or "", jc.get("away") or ""):
                continue

            jo = jc.get("odds") or {}
            # 补充体彩缺失的字段，不覆盖体彩已有
            if odds.get("rqspf_handicap") is None and jo.get("rqspf_handicap") is not None:
                odds["rqspf_handicap"] = jo["rqspf_handicap"]
            if odds.get("rqspf_win") is None and jo.get("rqspf_win") is not None:
                odds["rqspf_win"] = jo["rqspf_win"]
            if odds.get("rqspf_draw") is None and jo.get("rqspf_draw") is not None:
                odds["rqspf_draw"] = jo["rqspf_draw"]
            if odds.get("rqspf_loss") is None and jo.get("rqspf_loss") is not None:
                odds["rqspf_loss"] = jo["rqspf_loss"]
            if not odds.get("spf_win") and jo.get("spf_win"):
                odds["spf_win"] = jo["spf_win"]
            if not odds.get("spf_draw") and jo.get("spf_draw"):
                odds["spf_draw"] = jo["spf_draw"]
            if not odds.get("spf_loss") and jo.get("spf_loss"):
                odds["spf_loss"] = jo["spf_loss"]
            if not odds.get("bf") and jo.get("bf"):
                odds["bf"] = jo["bf"]
            if not odds.get("zjqs") and jo.get("zjqs"):
                odds["zjqs"] = jo["zjqs"]
            if not odds.get("bqc") and jo.get("bqc"):
                odds["bqc"] = jo["bqc"]
            break

        m["odds"] = odds

    return matches
