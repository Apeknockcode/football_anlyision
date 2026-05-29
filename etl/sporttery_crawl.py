"""
体彩网 m.sporttery.cn 足球数据抓取。
抓取指定日期的竞彩足球场次：主客队、比分、半场比分、联赛等。
数据可写入 DB 并更新 prediction_log 的实际比分。

让球数来源：体彩网 webapi.sporttery.cn（getFixedBonusV1.qry 单场固定奖金），
与官网计算器 https://www.lottery.gov.cn/jc/jsq/zqspf/ 展示的为同一套竞彩数据；
官网未提供公开 API，故采用体彩网接口。

支持三种方式：
1. requests 请求体彩 API（若存在可用的 JSON 接口）
2. Playwright 渲染页面并解析（需 pip install playwright && playwright install chromium）
3. --from-json <file>：从手动保存的 API 响应 JSON 解析（用户可从浏览器 Network 复制）
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # type: ignore

# 代理：main 里 --proxy 会调用 set_sporttery_proxy，使所有体彩 requests 显式走代理（避免仅设 env 未生效）
SPORTTERY_PROXY_URL: str | None = None


def set_sporttery_proxy(url: str | None) -> None:
    """设置体彩请求使用的代理 URL，如 http://127.0.0.1:7890。传 None 或空则不用代理。"""
    global SPORTTERY_PROXY_URL
    SPORTTERY_PROXY_URL = (url or "").strip() or None


def _sporttery_proxies() -> dict[str, str] | None:
    """返回 requests 用的 proxies 字典。优先级：set_sporttery_proxy > .env 的 SPORTTERY_PROXY > 环境变量 HTTP_PROXY/HTTPS_PROXY。"""
    url = SPORTTERY_PROXY_URL or None
    if not url:
        import os as _os
        url = (
            _os.environ.get("SPORTTERY_PROXY")
            or _os.environ.get("HTTPS_PROXY")
            or _os.environ.get("https_proxy")
            or _os.environ.get("HTTP_PROXY")
            or _os.environ.get("http_proxy")
            or ""
        ).strip() or None
    if not url:
        return None
    return {"http": url, "https": url}


# 体彩网中文队名 -> prediction_log / football-data 英文队名
SPORTTERY_TEAM_MAP: dict[str, str] = {
    "切尔西": "Chelsea FC",
    "利兹联": "Leeds United FC",
    "热刺": "Tottenham Hotspur FC",
    "托特纳姆热刺": "Tottenham Hotspur FC",
    "纽卡斯尔": "Newcastle United FC",
    "纽卡斯尔联": "Newcastle United FC",
    "埃弗顿": "Everton FC",
    "伯恩茅斯": "AFC Bournemouth",
    "伯恩茅斯队": "AFC Bournemouth",
    "西汉姆": "West Ham United FC",
    "西汉姆联": "West Ham United FC",
    "曼联": "Manchester United FC",
    "曼彻斯特联": "Manchester United FC",
    "曼城": "Manchester City FC",
    "曼彻斯特城": "Manchester City FC",
    "富勒姆": "Fulham FC",
    "诺丁汉森林": "Nottingham Forest FC",
    "诺丁汉": "Nottingham Forest FC",
    "狼队": "Wolverhampton Wanderers FC",
    "伍尔弗汉普顿": "Wolverhampton Wanderers FC",
    "水晶宫": "Crystal Palace FC",
    "伯恩利": "Burnley FC",
    "桑德兰": "Sunderland AFC",
    "利物浦": "Liverpool FC",
    "阿森纳": "Arsenal FC",
    "阿斯顿维拉": "Aston Villa FC",
    "维拉": "Aston Villa FC",
    "布莱顿": "Brighton & Hove Albion FC",
    "布伦特福德": "Brentford FC",
    "布伦特": "Brentford FC",
    "南安普顿": "Southampton FC",
    "伊普斯维奇": "Ipswich Town FC",
    "莱斯特城": "Leicester City FC",
    "巴黎圣曼": "Paris Saint-Germain FC",
    "巴黎圣日耳曼": "Paris Saint-Germain FC",
    "多特蒙德": "Borussia Dortmund",
    "美因茨": "1. FSV Mainz 05",
    "美因茨05": "1. FSV Mainz 05",
    "比萨": "AC Pisa 1909",
    "AC米兰": "AC Milan",
    "米兰": "AC Milan",
    "埃尔切": "Elche CF",
    "奥萨苏纳": "CA Osasuna",
    "摩纳哥": "AS Monaco FC",
    "南特": "FC Nantes",
    "雷恩": "Stade Rennais FC 1901",
}

# 英文系统名 -> 展示用中文名（解决如 1. FSV Mainz 05、AC Pisa 1909 等显示不佳）
TEAM_DISPLAY_CN: dict[str, str] = {
    "1. FSV Mainz 05": "美因茨",
    "AC Pisa 1909": "比萨",
}


def _norm_team(name: str) -> str:
    """体彩网队名映射到系统队名。"""
    s = (name or "").strip()
    for cn, en in SPORTTERY_TEAM_MAP.items():
        if cn in s or s in cn:
            return en
    return s if s else name


def _parse_score(text: str) -> tuple[int | None, int | None]:
    """解析 '2:2'、'1:0'、'2：1'（全角冒号）为 (主, 客)。"""
    if not text or not isinstance(text, str):
        return None, None
    s = text.strip().replace("：", ":").replace("－", "-").replace("—", "-")
    m = re.match(r"(\d+)\s*[:\-]\s*(\d+)", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _parse_fulltime_score(ft: Any) -> tuple[int | None, int | None]:
    """从 API 可能返回的多种全场比分格式解析 (主, 客)。支持 str、dict、list。"""
    if ft is None:
        return None, None
    if isinstance(ft, str):
        return _parse_score(ft)
    if isinstance(ft, dict):
        h = ft.get("home") or ft.get("homeScore") or ft.get("hostScore")
        a = ft.get("away") or ft.get("awayScore") or ft.get("guestScore")
        if h is not None and a is not None:
            try:
                return int(h), int(a)
            except (TypeError, ValueError):
                pass
        return None, None
    if isinstance(ft, (list, tuple)) and len(ft) >= 2:
        try:
            return int(ft[0]), int(ft[1])
        except (TypeError, ValueError):
            pass
    return None, None


def _parse_half(text: str) -> tuple[int | None, int | None]:
    """解析半场比分，如 '半场 1:0' 或 '1:0'。"""
    if not text:
        return None, None
    s = str(text).replace("半场", "").strip()
    return _parse_score(s)


def _normalize_score_str(s: str) -> str:
    """归一化比分字符串便于比较，如 '3:0' -> '3-0'。"""
    if not s or not isinstance(s, str):
        return ""
    return re.sub(r"\s+", "", s).replace("：", ":").strip()


def _derive_actual_match_date(
    section_date_str: str,
    match_time_str: str,
    per_match_date_str: str | None = None,
) -> str:
    """
    竞彩按「比赛编号日期」分组（如 260227），但实际开球可能是次日凌晨（02-28 01:30）。
    返回与 API 一致的实际比赛日（YYYY-MM-DD），便于 match_odds.log_date 与 matches.match_date 对齐。
    - 若接口返回了单场 matchDate 则优先用；
    - 若仅知 section 日期 + 开球时间，且时间为凌晨（00:00~11:59），则视为次日比赛。
    """
    from datetime import datetime, timedelta
    if per_match_date_str and isinstance(per_match_date_str, str) and len(per_match_date_str) >= 10:
        s = per_match_date_str[:10].replace("/", "-")
        if re.match(r"\d{4}-\d{2}-\d{2}", s):
            return s
    section = (section_date_str or "")[:10].replace("/", "-")
    if not section or len(section) < 10:
        return section_date_str[:10] if section_date_str else ""
    mt = (match_time_str or "").strip()
    # 解析时间：支持 "01:30"、"02-28 01:30"、"02:45"
    hour = None
    if re.search(r"(\d{1,2})\s*:\s*(\d{2})", mt):
        parts = re.findall(r"(\d{1,2})\s*:\s*(\d{2})", mt)
        if parts:
            hour = int(parts[0][0])
    if mt and re.match(r"\d{2}-\d{2}\s", mt):
        # "02-28 01:30" -> 取 02-28 作为日期（月-日），需结合 section 年
        try:
            mmdd = mt[:5]
            y = section[:4]
            d = datetime.strptime(f"{y}-{mmdd}", "%Y-%m-%d")
            return d.strftime("%Y-%m-%d")
        except Exception:
            pass
    if hour is not None and 0 <= hour < 12:
        try:
            d = datetime.strptime(section, "%Y-%m-%d") + timedelta(days=1)
            return d.strftime("%Y-%m-%d")
        except Exception:
            pass
    return section


WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]  # 周一=0, ..., 周日=6


def _extract_match_num(m: dict, match_date: str) -> str | None:
    """从体彩 API 单条记录提取比赛编号（如 周三007）。"""
    val = m.get("matchNum") or m.get("matchNumStr") or m.get("matchNo") or m.get("num")
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    # 已有完整格式（如 周三007）
    if re.match(r"周[一二三四五六日]\d{3}$", s):
        return s
    # 仅有数字（如 007、7 或 2007），需补周几；体彩编号通常 3 位，>999 时取后 3 位
    num_match = re.search(r"(\d+)", s)
    if num_match:
        try:
            from datetime import datetime
            dt = datetime.strptime(match_date[:10], "%Y-%m-%d")
            wd = dt.weekday()  # 0=周一
            n = int(num_match.group(1))
            if n > 999:
                n = n % 1000  # 2007 -> 007, 2004 -> 004
            return f"周{WEEKDAY_ZH[wd]}{n:03d}"
        except Exception:
            pass
    return s if len(s) <= 12 else None


def _format_match_num_fallback(match_date: str, index: int) -> str:
    """无 API 编号时，用日期+序号生成 周XNNN。"""
    try:
        from datetime import datetime
        dt = datetime.strptime(match_date[:10], "%Y-%m-%d")
        wd = dt.weekday()
        return f"周{WEEKDAY_ZH[wd]}{(index + 1):03d}"
    except Exception:
        return f"第{index + 1}场"


def _norm_rank(val: Any) -> int | str | None:
    """将 API 返回的排名转为可存库格式：整数或字符串，无效则 None。"""
    if val is None:
        return None
    if isinstance(val, int) and val > 0:
        return val
    if isinstance(val, str) and val.strip():
        s = val.strip()
        if s.isdigit():
            return int(s)
        return s
    try:
        n = int(val)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_raw_match_list(raw_list: Any, date_str: str) -> list[dict[str, Any]]:
    """从 API 返回的 matchList 解析为统一格式。"""
    matches: list[dict[str, Any]] = []
    if isinstance(raw_list, dict):
        raw_list = raw_list.get("matchList", raw_list.get("match", [])) or []
    if not isinstance(raw_list, list):
        return matches

    for m in raw_list:
        if not isinstance(m, dict):
            continue
        if is_invalid_match(m):
            continue
        home_cn = (m.get("homeTeamAbbName") or m.get("homeTeamName") or m.get("homeName") or "").strip()
        away_cn = (m.get("awayTeamAbbName") or m.get("awayTeamName") or m.get("awayName") or "").strip()
        if not home_cn and not away_cn:
            continue

        ft = m.get("sectionsNo999") or m.get("fullTimeScore") or m.get("score")
        h, a = _parse_fulltime_score(ft)
        if h is None and a is None:
            # 部分接口在顶层返回比分
            h = m.get("homeScore") or m.get("hostScore") or m.get("homeGoal")
            a = m.get("awayScore") or m.get("guestScore") or m.get("awayGoal")
            if h is not None and a is not None:
                try:
                    h, a = int(h), int(a)
                except (TypeError, ValueError):
                    h, a = None, None
            else:
                h, a = None, None

        ht = m.get("sectionsNo1") or m.get("halfTimeScore") or m.get("halfScore")
        if isinstance(ht, dict):
            hh, ha = ht.get("home"), ht.get("away")
        elif isinstance(ht, str):
            hh, ha = _parse_half(ht)
        else:
            hh, ha = None, None

        md = m.get("matchDate") or m.get("matchDateTime") or ""
        mt = m.get("matchTime") or ""
        status = (m.get("matchStatusName") or m.get("status") or "").strip()
        league = (m.get("leagueAbbName") or m.get("leagueName") or "").strip() or "英超"

        per_date = None
        if md:
            if isinstance(md, str) and len(md) >= 10:
                per_date = md[:10].replace("/", "-")
            elif isinstance(md, (int, float)):
                s = str(int(md))
                if len(s) >= 8:
                    per_date = s[:4] + "-" + s[4:6] + "-" + s[6:8]
        match_date = _derive_actual_match_date(date_str, mt or "", per_date)
        if not match_date:
            match_date = per_date or date_str

        match_num = _extract_match_num(m, match_date)
        if not match_num:
            match_num = _format_match_num_fallback(match_date, len(matches))

        odds = _extract_odds(m)
        match_id = m.get("matchId") or m.get("match_id") or m.get("id")
        if match_id is not None:
            try:
                match_id = int(match_id)
            except (TypeError, ValueError):
                match_id = None
        home_en = (m.get("homeTeamAbbEnName") or m.get("homeTeamEnName") or "").strip() or None
        away_en = (m.get("awayTeamAbbEnName") or m.get("awayTeamEnName") or "").strip() or None
        matches.append({
            "home_cn": home_cn,
            "away_cn": away_cn,
            "home": _norm_team(home_cn),
            "away": _norm_team(away_cn),
            "homeTeamAbbEnName": home_en,
            "awayTeamAbbEnName": away_en,
            "home_score": h,
            "away_score": a,
            "half_home": hh,
            "half_away": ha,
            "league": league,
            "match_date": match_date,
            "match_time": mt,
            "status": status,
            "match_num": match_num,
            "match_id": match_id,
            "odds": odds,
        })
    return matches


# 体彩联赛简称 -> 系统赛事名（与主库 competitions.name 一致；主库无该赛事时 comp_id=None 仍可预测）
# 不限于五大联赛：日职、韩职、澳超、德乙、荷甲等若主库已入库则 comp_id 可解析
LEAGUE_MAP: dict[str, str] = {
    "英超": "Premier League",
    "西甲": "Primera División",
    "意甲": "Serie A",
    "德甲": "Bundesliga",
    "法甲": "Ligue 1",
    "日职": "J1 League",
    "日职J1": "J1 League",
    "日乙": "J2 League",
    "韩职": "K League 1",
    "韩K联": "K League 1",
    "澳超": "A-League",
    "德乙": "2. Bundesliga",
    "荷甲": "Eredivisie",
    "英冠": "Championship",
    "葡超": "Primeira Liga",
    "欧冠": "UEFA Champions League",
    "欧罗巴": "UEFA Europa League",
    "欧协联": "UEFA Europa Conference League",
    "沙职": "Saudi Pro League",
    "美职": "MLS",
    "瑞超": "Allsvenskan",
    "挪超": "Eliteserien",
    "荷乙": "Eerste Divisie",
    "法乙": "Ligue 2",
    "亚冠精英": "AFC Champions League Elite",
    "亚冠乙": "AFC Champions League Two",
}


def parse_sporttery_response(data: dict | list, date_str: str) -> list[dict[str, Any]]:
    """从体彩 API 响应（dict 或 list）解析为统一格式。支持 webapi getMatchDataPageListV1 的 value 结构。"""
    if isinstance(data, list):
        return _parse_raw_match_list(data, date_str)
    if isinstance(data, dict):
        # 体彩 webapi 完整响应：{ success, value: { matchInfoList: [...] } } 或用户只粘贴 value 部分
        value = data.get("value") if data.get("success") else data
        if isinstance(value, dict) and (value.get("matchInfoList") is not None or "matchInfoList" in value):
            return _parse_webapi_value(value, date_str, filter_by_business_date=True)
        raw = data.get("matchList") or data.get("data") or data.get("list") or []
        return _parse_raw_match_list(raw, date_str)
    return []


def crawl_sporttery_from_json(file_path: str | Path, date_str: str) -> list[dict[str, Any]]:
    """
    从手动保存的体彩 API 响应 JSON 解析。
    用户可在浏览器打开 m.sporttery.cn/mjc/zqsj/，F12 -> Network -> 找到返回 matchList 的请求，复制 Response 保存为 JSON。
    """
    p = Path(file_path)
    if not p.is_file():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return parse_sporttery_response(data, date_str)


def _extract_odds(m: dict) -> dict[str, Any]:
    """从体彩 API 单条记录提取赔率。
    数据来源（getMatchCalculatorV1 / matchInfoList[].subMatchList[]）：
    - hhad.goalLine = 让球数；hhad.h = 让胜，hhad.d = 让平，hhad.a = 让负。
    - had（与 hhad 同级）= 胜平负：had.h = 胜，had.d = 平，had.a = 负。
    """
    out: dict[str, Any] = {}

    # 胜平负：优先从同级 had 取（h=胜, d=平, a=负）
    had = m.get("had")
    if isinstance(had, dict):
        for k, ok in [("h", "spf_win"), ("d", "spf_draw"), ("a", "spf_loss")]:
            if had.get(k) is not None:
                out[ok] = str(had[k])
    if not out.get("spf_win") and not out.get("spf_draw") and not out.get("spf_loss"):
        spf = m.get("spf") or m.get("spfOdds") or m.get("oddsSpf") or m.get("winDrawLoss")
        if isinstance(spf, dict):
            out["spf_win"] = spf.get("3") or spf.get("win") or spf.get("h")
            out["spf_draw"] = spf.get("1") or spf.get("draw") or spf.get("d")
            out["spf_loss"] = spf.get("0") or spf.get("loss") or spf.get("a")
        elif isinstance(spf, str):
            parts = [x.strip() for x in spf.replace("，", ",").split(",")]
            if len(parts) >= 3:
                out["spf_win"], out["spf_draw"], out["spf_loss"] = parts[0], parts[1], parts[2]
        # 部分接口（如 getUniformMatchResultV1）在顶层直接返回 h/d/a 胜平负赔率
        if not out.get("spf_win") and (m.get("h") is not None or m.get("d") is not None or m.get("a") is not None):
            for k, ok in [("h", "spf_win"), ("d", "spf_draw"), ("a", "spf_loss")]:
                if m.get(k) is not None:
                    out[ok] = str(m[k])

    # 让球数与让球胜平负：优先从 subMatchList[].hhad 取（goalLine=让球数，h=让胜，d=让平，a=让负）
    hhad = m.get("hhad")
    if isinstance(hhad, dict):
        gl = hhad.get("goalLine") or hhad.get("handicap") or hhad.get("let") or hhad.get("rq")
        if gl is not None:
            out["rqspf_handicap"] = int(gl) if isinstance(gl, (int, float)) and float(gl) == int(float(gl)) else gl
        for k, ok in [("h", "rqspf_win"), ("d", "rqspf_draw"), ("a", "rqspf_loss")]:
            if hhad.get(k) is not None:
                out[ok] = str(hhad[k])
    if not out.get("rqspf_handicap") and not out.get("rqspf_win"):
        rq = m.get("rqspf") or m.get("handicap") or m.get("rq") or m.get("oddsRqspf")
        if isinstance(rq, dict):
            gl = rq.get("goalLine") or rq.get("handicap") or rq.get("let") or rq.get("rq")
            if gl is not None:
                out["rqspf_handicap"] = int(gl) if isinstance(gl, (int, float)) and float(gl) == int(float(gl)) else gl
            for k, ok in [("h", "rqspf_win"), ("d", "rqspf_draw"), ("a", "rqspf_loss")]:
                if rq.get(k) is not None:
                    out[ok] = str(rq[k])
    # 部分接口把让球放在 hhadList 数组，取第一条
    hhad_list = m.get("hhadList") or m.get("rqspfList")
    if (not out.get("rqspf_handicap") or not out.get("rqspf_win")) and isinstance(hhad_list, list) and len(hhad_list) > 0 and isinstance(hhad_list[0], dict):
        first = hhad_list[0]
        gl = first.get("goalLine") or first.get("handicap")
        if gl is not None:
            out["rqspf_handicap"] = int(gl) if isinstance(gl, (int, float)) and float(gl) == int(float(gl)) else gl
        for k, ok in [("h", "rqspf_win"), ("d", "rqspf_draw"), ("a", "rqspf_loss")]:
            if first.get(k) is not None:
                out[ok] = str(first[k])
    if isinstance(m.get("rqspf"), str):
        out["rqspf_raw"] = m.get("rqspf")
    # 比分 bf
    bf = m.get("bf") or m.get("oddsBf") or m.get("scoreOdds")
    if isinstance(bf, dict):
        out["bf"] = bf
    # 总进球 zjqs
    zj = m.get("zjqs") or m.get("jq") or m.get("totalGoals") or m.get("oddsZjqs")
    if isinstance(zj, dict):
        out["zjqs"] = zj
    elif isinstance(zj, str):
        out["zjqs_raw"] = zj
    # 半全场 bqc
    bqc = m.get("bqc") or m.get("oddsBqc") or m.get("halfFull")
    if isinstance(bqc, dict):
        out["bqc"] = bqc
    elif isinstance(bqc, str):
        out["bqc_raw"] = bqc
    return out


def _parse_fixed_bonus_value(value: dict) -> dict[str, Any]:
    """
    解析体彩 getFixedBonusV1.qry 返回的 value，提取固定奖金（含让球数）。
    结构：value.oddsHistory.hhadList[].goalLine, h, d, a；hadList；crsList；ttgList；hafuList。
    """
    out: dict[str, Any] = {}
    oh = value.get("oddsHistory") or {}
    if not isinstance(oh, dict):
        return out
    # 让球胜平负 hhadList
    hhad = oh.get("hhadList") or []
    if isinstance(hhad, list) and len(hhad) > 0:
        first = hhad[0]
        if isinstance(first, dict):
            gl = first.get("goalLine")
            if gl is not None:
                try:
                    out["rqspf_handicap"] = int(float(str(gl)))
                except (ValueError, TypeError):
                    out["rqspf_handicap"] = gl
            h_val = first.get("h")
            d_val = first.get("d")
            a_val = first.get("a")
            if h_val is not None:
                out["rqspf_win"] = str(h_val)
            if d_val is not None:
                out["rqspf_draw"] = str(d_val)
            if a_val is not None:
                out["rqspf_loss"] = str(a_val)
    # 胜平负 hadList
    had = oh.get("hadList") or []
    if isinstance(had, list) and len(had) > 0:
        first = had[0]
        if isinstance(first, dict):
            for k, ok in [("h", "spf_win"), ("d", "spf_draw"), ("a", "spf_loss")]:
                if first.get(k) is not None:
                    out[ok] = str(first[k])
    # 比分 crsList
    crs = oh.get("crsList") or []
    if isinstance(crs, list):
        bf_dict: dict[str, str] = {}
        for item in crs:
            if isinstance(item, dict):
                for k, v in item.items():
                    if k and v is not None and re.match(r"^\d+[\-:]\d+$", str(k).replace(":", "-")):
                        bf_dict[str(k).replace("-", ":")] = str(v)
        if bf_dict:
            out["bf"] = bf_dict
    # 总进球 ttgList
    ttg = oh.get("ttgList") or []
    if isinstance(ttg, list):
        zj_dict: dict[str, str] = {}
        for item in ttg:
            if isinstance(item, dict):
                for k, v in item.items():
                    if k is not None and str(k).isdigit():
                        zj_dict[str(k)] = str(v)
                    elif k == "7+":
                        zj_dict["7"] = str(v) if v is not None else ""
        if zj_dict:
            out["zjqs"] = zj_dict
    # 半全场 hafuList
    hafu = oh.get("hafuList") or []
    if isinstance(hafu, list):
        bqc_dict: dict[str, str] = {}
        for item in hafu:
            if isinstance(item, dict):
                for k, v in item.items():
                    if k and v is not None and k in ("胜胜", "胜平", "胜负", "平胜", "平平", "平负", "负胜", "负平", "负负"):
                        bqc_dict[str(k)] = str(v)
        if bqc_dict:
            out["bqc"] = bqc_dict
    return out


def fetch_sporttery_fixed_bonus_raw(match_id: int) -> dict[str, Any] | None:
    """调用体彩 getFixedBonusV1.qry 返回原始 value（含 oddsHistory 完整列表，用于赔率历史）。"""
    if not requests:
        return None
    url = "https://webapi.sporttery.cn/gateway/uniform/football/getFixedBonusV1.qry"
    params = {"clientCode": "3001", "matchId": match_id}
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.sporttery.cn/",
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10, proxies=_sporttery_proxies())
        if r.status_code != 200:
            return None
        data = r.json()
        if not data.get("success"):
            return None
        value = data.get("value")
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _parse_odds_history_from_value(value: dict) -> list[dict[str, Any]]:
    """从 getFixedBonusV1 的 value.oddsHistory.hadList 解析胜平负赔率历史。"""
    out: list[dict[str, Any]] = []
    oh = value.get("oddsHistory") or {}
    had = oh.get("hadList") if isinstance(oh, dict) else []
    if not isinstance(had, list):
        return out
    for item in had:
        if not isinstance(item, dict):
            continue
        row = {
            "update_date": item.get("updateDate") or "",
            "update_time": item.get("updateTime") or "",
            "h": item.get("h"),
            "d": item.get("d"),
            "a": item.get("a"),
        }
        if row["h"] is not None or row["d"] is not None or row["a"] is not None:
            out.append(row)
    return out


def fetch_sporttery_fixed_bonus(match_id: int) -> dict[str, Any] | None:
    """
    调用体彩 getFixedBonusV1.qry 获取单场固定奖金（让球数、胜平负、比分等）。
    返回与 _extract_odds 格式一致的 odds dict，失败返回 None。
    """
    value = fetch_sporttery_fixed_bonus_raw(match_id)
    return _parse_fixed_bonus_value(value) if value else None


# 官网计算器页 https://www.lottery.gov.cn/jc/jsq/zqspf/ 使用的体彩接口（同源 webapi.sporttery.cn）
def _sporttery_api_headers(referer: str = "https://www.sporttery.cn/") -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
    }


def fetch_sporttery_match_calculator_v1(
    date_str: str | None = None,
    referer: str = "https://www.lottery.gov.cn/jc/jsq/zqspf/",
) -> dict[str, Any] | None:
    """
    调用 getMatchCalculatorV1.qry（官网足球胜平负计算器页用）。
    poolCode=hhad,had 表示要让球胜平负(hhad)+胜平负(had)。可选 matchDate 按日筛选。
    返回 API 原始 data（含 success, value），失败返回 None。
    """
    if not requests:
        return None
    url = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchCalculatorV1.qry"
    params: dict[str, str] = {"channel": "c", "poolCode": "hhad,had"}
    if date_str and len(date_str) >= 10:
        params["matchDate"] = date_str[:10]
    try:
        r = requests.get(url, params=params, headers=_sporttery_api_headers(referer), timeout=15, proxies=_sporttery_proxies())
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def fetch_sporttery_match_list_v1(
    date_str: str | None = None,
    referer: str = "https://www.sporttery.cn/",
) -> dict[str, Any] | None:
    """
    调用 getMatchListV1.qry（体彩统一比赛列表接口，与官网竞彩列表页一致）。
    clientCode=3001。优先用「不传 matchDate」拿全量再按 businessDate 过滤（在你处请求成功的方式），
    失败再试 matchDate=YYYY-MM-DD、matchDate=YYMMDD。
    返回 API 原始 data（含 success, value.matchInfoList 等），失败返回 None。
    """
    if not requests:
        return None
    url = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchListV1.qry"
    base_params: dict[str, Any] = {"clientCode": "3001"}
    want = (date_str or "")[:10] if date_str and len(date_str) >= 10 else None

    def _info_list_items(val):
        raw = (val or {}).get("matchInfoList") or []
        if isinstance(raw, dict):
            return list(raw.values())
        return raw if isinstance(raw, list) else []

    def _has_want_day(data: dict) -> bool:
        if not want or not data or not data.get("success") or not isinstance(data.get("value"), dict):
            return False
        return any(
            _normalize_business_date(day.get("businessDate")) == want
            for day in _info_list_items(data["value"]) if isinstance(day, dict)
        )

    def _do_get(req_params: dict) -> tuple[dict[str, Any] | None, int]:
        try:
            r = requests.get(
                url,
                params=req_params,
                headers=_sporttery_api_headers(referer),
                timeout=15,
                proxies=_sporttery_proxies(),
            )
            if r.status_code != 200:
                return None, r.status_code
            return r.json(), r.status_code
        except Exception:
            return None, -1

    try:
        # 1）优先：不传 matchDate，拿全量（在你处成功的请求方式），解析时按 businessDate 过滤
        data, status = _do_get(base_params)
        if data and data.get("success") and isinstance(data.get("value"), dict):
            if not want or _has_want_day(data):
                return data
        if status != 200 and status != -1:
            try:
                print(f"  [getMatchListV1] 全量请求 HTTP {status}，尝试带日期参数…", flush=True)
            except Exception:
                pass
        # 2）再试 matchDate=YYYY-MM-DD
        if want:
            data, status = _do_get({**base_params, "matchDate": want})
            if data and data.get("success") and isinstance(data.get("value"), dict) and _has_want_day(data):
                return data
        # 3）再试 matchDate=YYMMDD（260303）
        if want:
            try:
                from datetime import datetime as _dt
                yyymmdd = _dt.strptime(want, "%Y-%m-%d").strftime("%y%m%d")
                data, _ = _do_get({**base_params, "matchDate": yyymmdd})
                if data and data.get("success") and isinstance(data.get("value"), dict) and _has_want_day(data):
                    return data
            except Exception:
                pass
        try:
            print(f"  [getMatchListV1] 多种请求均未成功，使用回退接口", flush=True)
        except Exception:
            pass
        return None
    except Exception as e:
        try:
            print(f"  [getMatchListV1] 请求异常: {type(e).__name__}: {e}，使用回退接口", flush=True)
        except Exception:
            pass
        return None


def fetch_sporttery_uniform_match_result_v1(
    match_begin_date: str,
    match_end_date: str,
    page_no: int = 1,
    page_size: int = 30,
    league_id: str = "",
    referer: str = "https://www.sporttery.cn/",
) -> dict[str, Any] | None:
    """
    调用 getUniformMatchResultV1.qry（赛果开奖页用），按日期区间拉取历史赛果。
    支持历史日期（如 2025 年），与 getMatchCalculatorV1 互补。
    返回 API 原始 data（含 success, value.matchResult 等），失败返回 None。
    """
    if not requests:
        return None
    url = "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"
    params: dict[str, Any] = {
        "matchBeginDate": match_begin_date[:10],
        "matchEndDate": match_end_date[:10],
        "leagueId": league_id,
        "pageSize": page_size,
        "pageNo": page_no,
        "isFix": 0,
        "matchPage": 1,
        "pcOrWap": 1,
    }
    try:
        r = requests.get(url, params=params, headers=_sporttery_api_headers(referer), timeout=25, proxies=_sporttery_proxies())
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def fetch_uniform_match_results_by_match_id_for_dates(
    dates: list[str] | set[str],
    *,
    page_size: int = 30,
    max_pages_per_date: int = 50,
    rate_limit_sec: float = 0.12,
) -> dict[int, dict[str, Any]]:
    """
    对多个「比赛日 / 查询日」依次请求 getUniformMatchResultV1（分页），合并为 matchId -> 比分字段。
    用于仪表盘「拉取比赛结果」：该接口同时返回 sectionsNo1（半场）与 sectionsNo999（全场）。
    """
    import time

    norm_dates: list[str] = sorted({(d or "")[:10] for d in dates if d and len((d or "")[:10]) == 10})
    out: dict[int, dict[str, Any]] = {}
    for d in norm_dates:
        page_no = 1
        while page_no <= max_pages_per_date:
            data = fetch_sporttery_uniform_match_result_v1(
                match_begin_date=d,
                match_end_date=d,
                page_no=page_no,
                page_size=page_size,
            )
            if rate_limit_sec > 0:
                time.sleep(rate_limit_sec)
            if not data or not data.get("success"):
                break
            value = data.get("value")
            if not isinstance(value, dict):
                break
            page_matches = _parse_uniform_match_result_value(value, d)
            if not page_matches:
                break
            for m in page_matches:
                mid = m.get("match_id")
                if mid is None:
                    continue
                try:
                    mid_i = int(mid)
                except (TypeError, ValueError):
                    continue
                out[mid_i] = {
                    "home_score": m.get("home_score"),
                    "away_score": m.get("away_score"),
                    "half_home": m.get("half_home"),
                    "half_away": m.get("half_away"),
                }
            if len(page_matches) < page_size:
                break
            page_no += 1
    return out


def is_invalid_match(item: dict[str, Any]) -> bool:
    """API 标记为无效场次（sectionsNo999=无效场次 或 poolStatus=Refund）则不写入库。"""
    s999 = item.get("sectionsNo999") or item.get("fullTimeScore") or item.get("score") or ""
    if isinstance(s999, str) and "无效场次" in s999:
        return True
    ps = item.get("poolStatus") or ""
    if isinstance(ps, str) and str(ps).strip().lower() == "refund":
        return True
    mh = item.get("match_head")
    if isinstance(mh, dict):
        ps_mh = mh.get("poolStatus") or ""
        if isinstance(ps_mh, str) and str(ps_mh).strip().lower() == "refund":
            return True
        pt = mh.get("pageTitle") or mh
        if isinstance(pt, dict):
            fcg = pt.get("fullCourtGoal") or pt.get("sectionsNo999") or mh.get("sectionsNo999")
            if isinstance(fcg, str) and "无效场次" in fcg:
                return True
        if isinstance(mh.get("sectionsNo999"), str) and "无效场次" in (mh.get("sectionsNo999") or ""):
            return True
    return False


def _parse_uniform_match_result_value(value: dict, date_str: str) -> list[dict[str, Any]]:
    """解析 getUniformMatchResultV1 的 value：matchResult 数组，字段 allHomeTeam/allAwayTeam、matchDate、matchId、sectionsNo1/sectionsNo999、h/d/a 等。无效场次不加入列表。"""
    matches: list[dict[str, Any]] = []
    raw_list = value.get("matchResult") or value.get("matchList") or []
    for m in raw_list:
        if not isinstance(m, dict):
            continue
        if is_invalid_match(m):
            continue
        home_cn = (m.get("allHomeTeam") or m.get("homeTeamAbbName") or m.get("homeTeamName") or "").strip()
        away_cn = (m.get("allAwayTeam") or m.get("awayTeamAbbName") or m.get("awayTeamName") or "").strip()
        if not home_cn and not away_cn:
            continue
        ft = m.get("sectionsNo999") or m.get("fullTimeScore") or m.get("score")
        h, a = _parse_fulltime_score(ft)
        if h is None and a is None:
            h = m.get("homeScore") or m.get("hostScore") or m.get("homeGoal")
            a = m.get("awayScore") or m.get("guestScore") or m.get("awayGoal")
            if h is not None and a is not None:
                try:
                    h, a = int(h), int(a)
                except (TypeError, ValueError):
                    h, a = None, None
            else:
                h, a = None, None
        ht = m.get("sectionsNo1") or m.get("halfTimeScore") or m.get("halfScore") or m.get("htScore")
        hh, ha = None, None
        if isinstance(ht, str):
            hh, ha = _parse_half(ht)
        elif isinstance(ht, dict):
            try:
                hh = int(ht["home"]) if ht.get("home") is not None else None
                ha = int(ht["away"]) if ht.get("away") is not None else None
            except (TypeError, ValueError, KeyError):
                hh, ha = None, None
        md = m.get("matchDate") or ""
        per_date = (md[:10].replace("/", "-") if isinstance(md, str) and len(md) >= 10 else None) or date_str[:10]
        mt = (m.get("matchTime") or "").strip()
        match_date = _derive_actual_match_date(date_str, mt, per_date) or per_date or date_str[:10]
        match_num = _extract_match_num(m, match_date)
        if not match_num:
            match_num = _format_match_num_fallback(match_date, len(matches))
        odds = _extract_odds(m)
        match_id = m.get("matchId") or m.get("match_id") or m.get("id")
        if match_id is None:
            mn = m.get("matchNum")
            if isinstance(mn, (int, float)) or (isinstance(mn, str) and str(mn).isdigit()):
                try:
                    match_id = int(mn)
                except (TypeError, ValueError):
                    pass
        if match_id is not None:
            try:
                match_id = int(match_id)
            except (TypeError, ValueError):
                match_id = None
        home_en = (m.get("homeTeamAbbEnName") or m.get("homeTeamEnName") or "").strip() or None
        away_en = (m.get("awayTeamAbbEnName") or m.get("awayTeamEnName") or "").strip() or None
        status = (m.get("matchStatusName") or m.get("status") or "").strip() or "已完成"
        out = {
            "home_cn": home_cn,
            "away_cn": away_cn,
            "home": _norm_team(home_cn),
            "away": _norm_team(away_cn),
            "homeTeamAbbEnName": home_en,
            "awayTeamAbbEnName": away_en,
            "home_score": h,
            "away_score": a,
            "half_home": hh,
            "half_away": ha,
            "league": (m.get("leagueNameAbbr") or m.get("leagueAbbName") or m.get("leagueName") or "").strip() or "英超",
            "match_date": match_date,
            "match_time": mt,
            "status": status,
            "match_num": match_num,
            "match_id": match_id,
            "odds": odds,
        }
        out["businessDate"] = per_date  # 竞彩日，存库时用作 jc_date
        matches.append(out)
    return matches


def fetch_sporttery_support_rate_v1(
    match_ids: list[int],
    pool_code: str = "hhad,had",
    referer: str = "https://www.lottery.gov.cn/jc/jsq/zqspf/",
) -> dict[str, Any] | None:
    """
    调用 getSupportRateV1.qry（官网计算器页用），按场次 ID 拉取支持率/赔率。
    poolCode=hhad,had 含让球胜平负(hhad)+胜平负(had)。返回 API 原始 data，失败返回 None。
    """
    if not requests or not match_ids:
        return None
    url = "https://webapi.sporttery.cn/gateway/jc/common/getSupportRateV1.qry"
    params = {"matchIds": ",".join(str(mid) for mid in match_ids), "poolCode": pool_code, "sportType": "1"}
    try:
        r = requests.get(url, params=params, headers=_sporttery_api_headers(referer), timeout=20, proxies=_sporttery_proxies())
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


# 体彩对阵详情页使用的 webapi 接口（可直接 GET 获取 JSON，无需 Playwright）
# 文档：https://www.sporttery.cn/jc/zqdz/index.html?showType=2&mid=xxx
SPORTTERY_DETAIL_API_BASE = "https://webapi.sporttery.cn/gateway/uniform/football"


def _fetch_sporttery_detail_api(path: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """请求体彩对阵详情相关接口，返回完整响应；失败返回 None。"""
    if not requests:
        return None
    url = f"{SPORTTERY_DETAIL_API_BASE}/{path}"
    try:
        r = requests.get(url, params=params, headers=_sporttery_api_headers("https://www.sporttery.cn/"), timeout=15, proxies=_sporttery_proxies())
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or not data.get("success"):
            return None
        return data
    except Exception:
        return None


def fetch_sporttery_match_head_v1(sporttery_match_id: int) -> dict[str, Any] | None:
    """
    对阵头部信息（图一：主客队名、排名、赛季总/主客场成绩等）。
    GET getMatchHeadV1.qry?source=web&sportteryMatchId=xxx
    返回 API 原始响应；value 中含 pageTitle / wbsjStats 等。
    """
    return _fetch_sporttery_detail_api("getMatchHeadV1.qry", {"source": "web", "sportteryMatchId": sporttery_match_id})


def fetch_sporttery_injury_suspension_v1(sporttery_match_id: int) -> dict[str, Any] | None:
    """
    主客队伤停信息。
    GET getInjurySuspensionV1.qry?sportteryMatchId=xxx
    """
    return _fetch_sporttery_detail_api("getInjurySuspensionV1.qry", {"sportteryMatchId": sporttery_match_id})


def fetch_sporttery_match_player_v1(sporttery_match_id: int, term_limits: int = 3) -> dict[str, Any] | None:
    """
    主客队射手信息（最近 N 名）。
    GET getMatchPlayerV1.qry?sportteryMatchId=xxx&termLimits=3
    """
    return _fetch_sporttery_detail_api(
        "getMatchPlayerV1.qry", {"sportteryMatchId": sporttery_match_id, "termLimits": term_limits}
    )


def fetch_sporttery_match_result_v1(
    sporttery_match_id: int,
    term_limits: int = 10,
    tournament_flag: int = 0,
    home_away_flag: int = 1,
) -> dict[str, Any] | None:
    """
    主客队相同主客场的比赛结果（主队主场 vs 客队客场等）。
    GET getMatchResultV1.qry?sportteryMatchId=xxx&termLimits=10&tournamentFlag=0&homeAwayFlag=1
    tournamentFlag: 0=全部赛制, 1=同赛制；homeAwayFlag: 1=同主客场。
    """
    return _fetch_sporttery_detail_api(
        "getMatchResultV1.qry",
        {
            "sportteryMatchId": sporttery_match_id,
            "termLimits": term_limits,
            "tournamentFlag": tournament_flag,
            "homeAwayFlag": home_away_flag,
        },
    )


def fetch_sporttery_result_history_v1(
    sporttery_match_id: int,
    term_limits: int = 10,
    tournament_flag: int = 0,
    home_away_flag: int = 1,
) -> dict[str, Any] | None:
    """
    主客队相同主客场的历史交锋。
    tournamentFlag=0: 全部赛制历史交锋；tournamentFlag=1: 相同赛制历史交锋。
    GET getResultHistoryV1.qry?sportteryMatchId=xxx&termLimits=10&tournamentFlag=0|1&homeAwayFlag=1
    """
    return _fetch_sporttery_detail_api(
        "getResultHistoryV1.qry",
        {
            "sportteryMatchId": sporttery_match_id,
            "termLimits": term_limits,
            "tournamentFlag": tournament_flag,
            "homeAwayFlag": home_away_flag,
        },
    )


def fetch_sporttery_match_feature_v1(sporttery_match_id: int, term_limits: int = 10) -> dict[str, Any] | None:
    """
    特征分析（近 N 场交锋、同主客交锋、近 N 场战况、场均进/失球等）。
    GET getMatchFeatureV1.qry?termLimits=10&sportteryMatchId=xxx
    """
    return _fetch_sporttery_detail_api(
        "getMatchFeatureV1.qry", {"termLimits": term_limits, "sportteryMatchId": sporttery_match_id}
    )


def fetch_sporttery_future_matches_v1(sporttery_match_id: int) -> dict[str, Any] | None:
    """
    主客队未来赛程（value 含 home/away，各含 matchList）。
    GET getFutureMatchesV1.qry?sportteryMatchId=xxx
    """
    return _fetch_sporttery_detail_api(
        "getFutureMatchesV1.qry", {"sportteryMatchId": sporttery_match_id}
    )


def fetch_sporttery_match_detail_via_api(sporttery_match_id: int) -> dict[str, Any]:
    """
    通过体彩 webapi 直接拉取单场对阵详情（无需 Playwright）。
    依次请求：getMatchHeadV1、getInjurySuspensionV1、getMatchPlayerV1、
    getResultHistoryV1（全部赛制+同赛制）、getMatchFeatureV1、getFutureMatchesV1，返回各接口 value 的合集。
    不含相同主客场比赛结果（getMatchResultV1）。
    用于 API 或脚本一次性拉取某场（mid）的完整详情。
    """
    out: dict[str, Any] = {
        "sporttery_match_id": sporttery_match_id,
        "match_head": None,
        "injury_suspension": None,
        "match_player": None,
        "result_history_all": None,
        "result_history_same_tournament": None,
        "match_feature": None,
        "future_matches": None,
    }
    head = fetch_sporttery_match_head_v1(sporttery_match_id)
    if head and isinstance(head.get("value"), dict):
        out["match_head"] = head["value"]
        # 从 match_head 提取比赛时间作为顶层字段，便于存库后直接查看/筛选
        pt = head["value"].get("pageTitle") or head["value"]
        if isinstance(pt, dict):
            mt = pt.get("matchDateTime") or pt.get("matchDate") or pt.get("matchTime") or ""
            if mt:
                out["match_time"] = mt if isinstance(mt, str) else str(mt)
            # 若有日期部分可单独存为 match_date（YYYY-MM-DD），便于按日筛选
            if out.get("match_time"):
                mt_str = out["match_time"]
                if " " in mt_str or "T" in mt_str:
                    d = (mt_str.split("T")[0].split(" ")[0]).strip()[:10]
                elif len(mt_str) >= 10 and mt_str[:4].isdigit():
                    d = mt_str[:10]
                else:
                    d = ""
                if d and len(d) >= 10:
                    out["match_date"] = d
    inj = fetch_sporttery_injury_suspension_v1(sporttery_match_id)
    if inj and isinstance(inj.get("value"), (dict, list)):
        out["injury_suspension"] = inj["value"]
    player = fetch_sporttery_match_player_v1(sporttery_match_id, term_limits=3)
    if player and isinstance(player.get("value"), (dict, list)):
        out["match_player"] = player["value"]
    hist_all = fetch_sporttery_result_history_v1(sporttery_match_id, term_limits=10, tournament_flag=0, home_away_flag=1)
    if hist_all and isinstance(hist_all.get("value"), (dict, list)):
        out["result_history_all"] = hist_all["value"]
    hist_same = fetch_sporttery_result_history_v1(sporttery_match_id, term_limits=10, tournament_flag=1, home_away_flag=1)
    if hist_same and isinstance(hist_same.get("value"), (dict, list)):
        out["result_history_same_tournament"] = hist_same["value"]
    feature = fetch_sporttery_match_feature_v1(sporttery_match_id, term_limits=10)
    if feature and isinstance(feature.get("value"), dict):
        out["match_feature"] = feature["value"]
    future = fetch_sporttery_future_matches_v1(sporttery_match_id)
    if future and isinstance(future.get("value"), dict):
        out["future_matches"] = future["value"]
    # 让球数、让胜负平赔率、胜负平赔率：从 getFixedBonusV1 拉取并写入 odds，便于存库后列表展示
    bonus = fetch_sporttery_fixed_bonus(sporttery_match_id)
    out["odds"] = bonus if bonus else {}
    return out


def _parse_calculator_value(value: dict, date_str: str) -> list[dict[str, Any]]:
    """解析 getMatchCalculatorV1 的 value，得到与 crawl 一致格式的场次列表（含 odds.rqspf_*）。接口返回 matchInfoList（与 getMatchDataPageListV1 同结构）；仅返回 businessDate 与请求日期一致的那一日场次。"""
    # 优先：matchInfoList[].subMatchList（官网计算器/资讯页同源），按 businessDate 过滤
    if value.get("matchInfoList") is not None:
        return _parse_webapi_value(value, date_str, filter_by_business_date=True)
    matches: list[dict[str, Any]] = []
    raw_list = value.get("matchList") or value.get("list") or value.get("data")
    if isinstance(value, list):
        raw_list = value
    if not isinstance(raw_list, list):
        return matches
    for m in raw_list:
        if not isinstance(m, dict):
            continue
        home_cn = (m.get("homeTeamAbbName") or m.get("homeTeamName") or m.get("home_cn") or "").strip()
        away_cn = (m.get("awayTeamAbbName") or m.get("awayTeamName") or m.get("away_cn") or "").strip()
        if not home_cn and not away_cn:
            continue
        match_id = m.get("matchId") or m.get("match_id") or m.get("id")
        if match_id is not None:
            try:
                match_id = int(match_id)
            except (TypeError, ValueError):
                match_id = None
        odds = _extract_odds(m)
        # 胜平负 had
        had = m.get("had") or m.get("spf")
        if isinstance(had, dict):
            for k, ok in [("h", "spf_win"), ("d", "spf_draw"), ("a", "spf_loss")]:
                if had.get(k) is not None:
                    odds[ok] = str(had[k])
        # 让球胜平负：hhad 可能为对象 { goalLine, h, d, a } 或数组 [ { goalLine, h, d, a } ]
        hhad = m.get("hhad") or m.get("rqspf") or m.get("handicap")
        if isinstance(hhad, dict):
            gl = hhad.get("goalLine") or hhad.get("handicap") or hhad.get("let")
            if gl is not None:
                odds["rqspf_handicap"] = int(gl) if isinstance(gl, (int, float)) and float(gl) == int(float(gl)) else gl
            for k, ok in [("h", "rqspf_win"), ("d", "rqspf_draw"), ("a", "rqspf_loss")]:
                if hhad.get(k) is not None:
                    odds[ok] = str(hhad[k])
        elif isinstance(hhad, list) and len(hhad) > 0:
            first = hhad[0] if isinstance(hhad[0], dict) else None
            if first and (first.get("goalLine") is not None or first.get("handicap") is not None):
                gl = first.get("goalLine") or first.get("handicap")
                if gl is not None:
                    odds["rqspf_handicap"] = int(gl) if isinstance(gl, (int, float)) and float(gl) == int(float(gl)) else gl
                for k, ok in [("h", "rqspf_win"), ("d", "rqspf_draw"), ("a", "rqspf_loss")]:
                    if first.get(k) is not None:
                        odds[ok] = str(first[k])
        md = m.get("matchDate") or m.get("match_date") or ""
        per_date = (md[:10].replace("/", "-") if isinstance(md, str) and len(md) >= 10 else None)
        mt = (m.get("matchTime") or "").strip()
        match_date = _derive_actual_match_date(date_str, mt, per_date) or per_date or date_str
        home_en = (m.get("homeTeamAbbEnName") or m.get("homeTeamEnName") or "").strip() or None
        away_en = (m.get("awayTeamAbbEnName") or m.get("awayTeamEnName") or "").strip() or None
        matches.append({
            "home_cn": home_cn, "away_cn": away_cn,
            "home": _norm_team(home_cn), "away": _norm_team(away_cn),
            "homeTeamAbbEnName": home_en,
            "awayTeamAbbEnName": away_en,
            "league": (m.get("leagueAbbName") or m.get("league") or "").strip() or "英超",
            "match_date": match_date, "match_time": mt or m.get("matchTime") or "", "status": (m.get("matchStatusName") or "").strip(),
            "match_id": match_id,
            "odds": odds,
        })
    return matches


def _parse_support_rate_value(value: Any) -> dict[int, dict[str, Any]]:
    """解析 getSupportRateV1 的 value，返回 { match_id: { rqspf_handicap, rqspf_win, rqspf_draw, rqspf_loss, spf_win, spf_draw, spf_loss } }。
    支持 value 为 list、或 dict 内 list/matchList/data、或 matchInfoList[].subMatchList 展平。"""
    out: dict[int, dict[str, Any]] = {}

    def collect_items(val: Any) -> list[dict]:
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            if "matchInfoList" in val:
                items = []
                for day in val.get("matchInfoList") or []:
                    items.extend((day.get("subMatchList") or []))
                return [x for x in items if isinstance(x, dict)]
            return collect_items(val.get("list") or val.get("matchList") or val.get("data") or [])
        return []

    items = value if isinstance(value, list) else collect_items(value) if isinstance(value, dict) else []
    if not items and isinstance(value, dict) and not value.get("matchInfoList"):
        # value 为 dict 且 key 为 matchId 的格式：{ "2037954": { had, hhad }, ... }
        for mid_str, item in value.items():
            if isinstance(item, dict) and (item.get("had") or item.get("hhad")):
                items.append({"matchId": mid_str, **item})
    if not items:
        return out

    for item in items:
        if not isinstance(item, dict):
            continue
        mid = item.get("matchId") or item.get("match_id") or item.get("id")
        if mid is None:
            continue
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            continue
        odds: dict[str, Any] = {}
        had = item.get("had")
        if isinstance(had, dict):
            for k, ok in [("h", "spf_win"), ("d", "spf_draw"), ("a", "spf_loss")]:
                if had.get(k) is not None:
                    odds[ok] = str(had[k])
        hhad = item.get("hhad") or item.get("rqspf")
        if isinstance(hhad, dict):
            gl = hhad.get("goalLine") or hhad.get("handicap")
            if gl is not None:
                odds["rqspf_handicap"] = int(gl) if isinstance(gl, (int, float)) and float(gl) == int(float(gl)) else gl
            for k, ok in [("h", "rqspf_win"), ("d", "rqspf_draw"), ("a", "rqspf_loss")]:
                if hhad.get(k) is not None:
                    odds[ok] = str(hhad[k])
        elif isinstance(hhad, list) and len(hhad) > 0:
            first = hhad[0]
            if isinstance(first, dict):
                gl = first.get("goalLine") or first.get("handicap")
                if gl is not None:
                    odds["rqspf_handicap"] = int(gl) if isinstance(gl, (int, float)) and float(gl) == int(float(gl)) else gl
                for k, ok in [("h", "rqspf_win"), ("d", "rqspf_draw"), ("a", "rqspf_loss")]:
                    if first.get(k) is not None:
                        odds[ok] = str(first[k])
        if odds:
            out[mid] = odds
    return out


def enrich_matches_from_calculator_api(
    matches: list[dict[str, Any]],
    date_str: str,
    referer: str = "https://www.lottery.gov.cn/jc/jsq/zqspf/",
) -> None:
    """
    用官网计算器接口补全让球数：先试 getMatchCalculatorV1（按日）；若无则用 getSupportRateV1（按场次 ID）。
    直接修改 matches 中每场的 odds，填入 rqspf_handicap / rqspf_win 等。
    """
    if not matches:
        return
    date_str = (date_str or "")[:10]
    # 1) 若 getMatchCalculatorV1 的 subMatch 里带了 had/hhad，先合并
    data = fetch_sporttery_match_calculator_v1(date_str, referer=referer)
    if data and data.get("success") and isinstance(data.get("value"), dict):
        calc_list = _parse_calculator_value(data["value"], date_str)
        if calc_list:
            by_id = {m["match_id"]: m for m in calc_list if m.get("match_id") is not None}
            for m in matches:
                mid = m.get("match_id")
                if mid is None:
                    continue
                calc_match = by_id.get(mid)
                if calc_match and calc_match.get("odds"):
                    odds = m.get("odds") or {}
                    _merge_fixed_bonus_into_odds(odds, calc_match["odds"])
                    m["odds"] = odds
    # 2) 用 getSupportRateV1 按 match_ids 拉取（计算器接口 subMatch 常无 had/hhad，必须用此接口补全让球/胜平负）
    ids = [m["match_id"] for m in matches if m.get("match_id") is not None]
    if not ids:
        return
    support = fetch_sporttery_support_rate_v1(ids, referer=referer)
    if not support or not support.get("success"):
        return
    value = support.get("value")
    rate_by_id = _parse_support_rate_value(value) if value is not None else {}
    for m in matches:
        mid = m.get("match_id")
        if mid is None:
            continue
        extra = rate_by_id.get(mid)
        if not extra:
            continue
        odds = m.get("odds") or {}
        _merge_fixed_bonus_into_odds(odds, extra)
        m["odds"] = odds


# 体彩对阵详情页（排名、赛季成绩、主客队名等）URL
# 同一页面：showType=2 为前端展示用（如 https://www.sporttery.cn/jc/zqdz/index.html?showType=2&mid=2037891），
# showType=1&mid=xxx 用于抓取；页面加载时会请求 getMatchHeadV1、getMatchFeatureV1 等接口，可截获 JSON 解析。
SPORTTERY_ZQDZ_URL = "https://www.sporttery.cn/jc/zqdz/index.html"


def fetch_sporttery_match_detail(match_id: int) -> dict[str, Any] | None:
    """
    抓取体彩单场对阵详情：排名、赛季总/主客场成绩、胜平负赔率历史、主客队简称/英文名。
    先从 getFixedBonusV1 取赔率历史，再用 Playwright 打开对阵详情页（zqdz）截获接口响应或解析内嵌数据。
    页面上会请求 getMatchHeadV1 等接口，可从中解析 pageTitle（含 homeTeamShortName/awayTeamShortName、
    homeTeamAbbEnName/awayTeamAbbEnName 等）。返回字段包括 home_ranking, away_ranking, home_season, away_season,
    home_home, away_away, odds_history；若截获到 pageTitle 则还有 home_team_short, away_team_short,
    home_team_en, away_team_en。失败或无有效数据时返回 None。
    """
    result: dict[str, Any] = {
        "home_ranking": None,
        "away_ranking": None,
        "home_season": None,
        "away_season": None,
        "home_home": None,
        "away_away": None,
        "home_team_short": None,
        "away_team_short": None,
        "home_team_en": None,
        "away_team_en": None,
        "odds_history": [],
        "feature_recent10_home": None,
        "feature_recent10_away": None,
        "feature_same_h2h_home": None,
        "feature_same_h2h_away": None,
        "feature_form10_home": None,
        "feature_form10_away": None,
        "feature_same_form_home": None,
        "feature_same_form_away": None,
        "avg_goals_home": None,
        "avg_goals_away": None,
        "avg_conceded_home": None,
        "avg_conceded_away": None,
        "h2h_20_summary": None,
        "h2h_matches": [],
        "standings": [],
    }
    value = fetch_sporttery_fixed_bonus_raw(match_id)
    if value:
        result["odds_history"] = _parse_odds_history_from_value(value)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return result if (result["odds_history"] or result["home_ranking"] is not None) else None

    url = f"{SPORTTERY_ZQDZ_URL}?showType=1&mid={match_id}"
    captured: list[dict] = []

    def _on_response(response):
        try:
            if response.status != 200:
                return
            body = response.text()
            if not body or len(body) < 80:
                return
            # 全部抓取：任意可解析的 JSON 都保留（体彩详情页可能多个接口）
            data = json.loads(body)
            if isinstance(data, dict) and data:
                captured.append(data)
            elif isinstance(data, list) and data:
                captured.append({"list": data})
        except Exception:
            pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = context.new_page()
            page.on("response", _on_response)
            page.goto(url, wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(4000)

            # 依次点击不同类目，触发各 tab 的数据请求以便全部抓取
            tab_texts = ["赛事前瞻", "赛事资讯", "固定奖金", "比分直播"]
            for tab_text in tab_texts:
                try:
                    tab = page.get_by_text(tab_text, exact=True).first
                    if tab.count() > 0:
                        tab.click()
                        page.wait_for_timeout(1800)
                except Exception:
                    pass
            # 历史交锋筛选：点击「近10场」「近20场」等，触发不同数据
            for filter_text in ["近20场", "近10场", "相同赛制"]:
                try:
                    btn = page.get_by_text(filter_text, exact=True).first
                    if btn.count() > 0:
                        btn.click()
                        page.wait_for_timeout(1500)
                except Exception:
                    pass

            if not captured:
                try:
                    for var_name in ["__NUXT__", "__INITIAL_STATE__", "__PRELOADED_STATE__", "pageTitle"]:
                        js_data = page.evaluate(
                            "() => { try { return window[arguments[0]] || null; } catch(e) { return null; } }",
                            var_name,
                        )
                        if js_data and isinstance(js_data, dict):
                            captured.append(js_data)
                            break
                except Exception:
                    pass
            if not captured:
                # 从页面 HTML 中查找内嵌 JSON（如 __INITIAL_DATA__ = {...}）
                try:
                    html = page.content()
                    for pattern in [
                        r"window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>",
                        r"pageTitle\s*[=:]\s*(\{.*?\});",
                        r"wbsjStats\s*[=:]\s*(\{.*?\});",
                        r'"wbsjStats"\s*:\s*(\{[^}]+\{[^}]*\}[^}]*\})',
                    ]:
                        m = re.search(pattern, html, re.DOTALL)
                        if m:
                            try:
                                data = json.loads(m.group(1))
                                if isinstance(data, dict):
                                    captured.append(data)
                                    break
                            except Exception:
                                pass
                except Exception:
                    pass
            browser.close()
    except Exception:
        pass

    def _find_wbsj(obj: Any, depth: int = 0) -> dict | None:
        if depth > 5 or obj is None:
            return None
        if isinstance(obj, dict):
            if "wbsjStats" in obj:
                return obj.get("wbsjStats")
            for v in obj.values():
                found = _find_wbsj(v, depth + 1)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = _find_wbsj(item, depth + 1)
                if found:
                    return found
        return None

    for data in captured:
        wbsj = data.get("wbsjStats") if isinstance(data, dict) else None
        if not isinstance(wbsj, dict):
            wbsj = _find_wbsj(data)
        pt = data.get("pageTitle") or data.get("data") or data if isinstance(data, dict) else data
        if isinstance(pt, dict) and not wbsj:
            wbsj = pt.get("wbsjStats")
        if not isinstance(wbsj, dict):
            continue
        home_s = wbsj.get("home")
        away_s = wbsj.get("away")
        if isinstance(home_s, dict):
            r = home_s.get("ranking")
            if r is not None and str(r).strip() != "":
                result["home_ranking"] = str(r).strip()
            sw = home_s.get("sWinGoalMatchCnt")
            sd = home_s.get("sDrawMatchCnt")
            sl = home_s.get("sLossGoalMatchCnt")
            if sw is not None or sd is not None or sl is not None:
                result["home_season"] = f"{sw or '-'}胜 {sd or '-'}平 {sl or '-'}负"
            hw = home_s.get("sHomeWinGoalMatchCnt")
            hd = home_s.get("sHomeDrawMatchCnt")
            hl = home_s.get("sHomeLossGoalMatchCnt")
            if hw is not None or hd is not None or hl is not None:
                result["home_home"] = f"{hw or '-'}胜 {hd or '-'}平 {hl or '-'}负"
        if isinstance(away_s, dict):
            r = away_s.get("ranking")
            if r is not None and str(r).strip() != "":
                result["away_ranking"] = str(r).strip()
            sw = away_s.get("sWinGoalMatchCnt")
            sd = away_s.get("sDrawMatchCnt")
            sl = away_s.get("sLossGoalMatchCnt")
            if sw is not None or sd is not None or sl is not None:
                result["away_season"] = f"{sw or '-'}胜 {sd or '-'}平 {sl or '-'}负"
            aw = away_s.get("sAwayWinGoalMatchCnt")
            ad = away_s.get("sAwayDrawMatchCnt")
            al = away_s.get("sAwayLossGoalMatchCnt")
            if aw is not None or ad is not None or al is not None:
                result["away_away"] = f"{aw or '-'}胜 {ad or '-'}平 {al or '-'}负"
        break

    # 从 captured 中解析 pageTitle（对阵详情页模板数据：主客队简称、英文名等）
    for data in captured:
        pt = None
        if isinstance(data, dict):
            pt = data.get("pageTitle") or (data.get("value") or {}).get("pageTitle")
        if not isinstance(pt, dict):
            continue
        if not result.get("home_team_short") and (pt.get("homeTeamShortName") or pt.get("homeTeamAbbName")):
            result["home_team_short"] = (pt.get("homeTeamShortName") or pt.get("homeTeamAbbName") or "").strip()
        if not result.get("away_team_short") and (pt.get("awayTeamShortName") or pt.get("awayTeamAbbName")):
            result["away_team_short"] = (pt.get("awayTeamShortName") or pt.get("awayTeamAbbName") or "").strip()
        if not result.get("home_team_en") and (pt.get("homeTeamAbbEnName") or pt.get("homeTeamEnName") or pt.get("homeTeamNameEn")):
            result["home_team_en"] = (pt.get("homeTeamAbbEnName") or pt.get("homeTeamEnName") or pt.get("homeTeamNameEn") or "").strip()
        if not result.get("away_team_en") and (pt.get("awayTeamAbbEnName") or pt.get("awayTeamEnName") or pt.get("awayTeamNameEn")):
            result["away_team_en"] = (pt.get("awayTeamAbbEnName") or pt.get("awayTeamEnName") or pt.get("awayTeamNameEn") or "").strip()
        if result.get("home_team_short") and result.get("away_team_short"):
            break

    # 从 captured 中递归提取特征分析、历史交锋（键名可能为驼峰或拼音）
    def _find_any(obj: Any, keys: list[str], depth: int = 0) -> Any:
        if depth > 6 or obj is None:
            return None
        if isinstance(obj, dict):
            for k in keys:
                if k in obj:
                    return obj[k]
            for v in obj.values():
                r = _find_any(v, keys, depth + 1)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = _find_any(item, keys, depth + 1)
                if r is not None:
                    return r
        return None

    def _str_win_draw_loss(d: dict | None) -> str | None:
        if not d or not isinstance(d, dict):
            return None
        w, draw, l = d.get("win") or d.get("winCnt") or d.get("w"), d.get("draw") or d.get("drawCnt") or d.get("d"), d.get("loss") or d.get("lossCnt") or d.get("l")
        if w is not None or draw is not None or l is not None:
            return f"{w or 0}胜 {draw or 0}平 {l or 0}负"
        s = d.get("text") or d.get("desc") or d.get("value") or ""
        if isinstance(s, str) and ("胜" in s or "平" in s or "负" in s):
            return s
        return None

    for data in captured:
        # 特征分析：近10场交锋、同主客交锋、近10场战况、同主客战况
        key_to_result = [
            (["recent10", "recent10Encounters", "recent10H2H", "jf10"], "feature_recent10_home", "feature_recent10_away"),
            (["sameVenue", "sameVenueEncounters", "sameVenueH2H", "sameHomeAway"], "feature_same_h2h_home", "feature_same_h2h_away"),
            (["form10", "recent10Form", "recent10Status", "recent10Match"], "feature_form10_home", "feature_form10_away"),
            (["sameVenueForm", "sameVenueForm", "sameVenueStatus"], "feature_same_form_home", "feature_same_form_away"),
        ]
        for key_choices, result_h, result_a in key_to_result:
            for root_key in ["featureAnalysis", "feature", "analysis", "jfStats", "recentEncounters", "data"]:
                root = _find_any(data, [root_key])
                if not isinstance(root, dict):
                    continue
                home_node = root.get("home") or root.get("homeTeam") or root.get("主队")
                away_node = root.get("away") or root.get("awayTeam") or root.get("客队")
                for node, res_key in [(home_node, result_h), (away_node, result_a)]:
                    if not isinstance(node, dict) or result.get(res_key):
                        continue
                    val = None
                    for k in key_choices:
                        val = node.get(k)
                        if val is not None:
                            break
                    if isinstance(val, str) and ("胜" in val or "平" in val or "负" in val):
                        result[res_key] = val
                    else:
                        s = _str_win_draw_loss(val if isinstance(val, dict) else None)
                        if s:
                            result[res_key] = s
        # 场均进球/失球
        root = _find_any(data, ["featureAnalysis", "feature", "avgGoals", "averageGoals"])
        if isinstance(root, dict):
            for side, rk_goals, rk_conceded in [
                ("home", "avg_goals_home", "avg_conceded_home"),
                ("away", "avg_goals_away", "avg_conceded_away"),
            ]:
                node = root.get(side) or root.get("homeTeam" if side == "home" else "awayTeam")
                if isinstance(node, dict) and not result.get(rk_goals):
                    g = node.get("avgGoals") or node.get("avgScored") or node.get("goalsFor") or node.get("score")
                    c = node.get("avgConceded") or node.get("goalsAgainst") or node.get("conceded")
                    if g is not None:
                        result[rk_goals] = g if isinstance(g, (int, float)) else (float(g) if isinstance(g, str) else None)
                    if c is not None:
                        result[rk_conceded] = c if isinstance(c, (int, float)) else (float(c) if isinstance(c, str) else None)
        # 历史交锋 近20场 摘要：主队 5胜25% 3平15% 12负60%
        h2h = _find_any(data, ["h2h", "historyEncounters", "history", "head2head", "jf20"])
        if isinstance(h2h, dict) and not result.get("h2h_20_summary"):
            hw = h2h.get("homeWin") or h2h.get("homeWinCnt") or h2h.get("主胜")
            hd = h2h.get("draw") or h2h.get("drawCnt") or h2h.get("平")
            aw = h2h.get("awayWin") or h2h.get("awayWinCnt") or h2h.get("客胜")
            total = h2h.get("total") or h2h.get("totalCnt") or 20
            if hw is not None or hd is not None or aw is not None:
                try:
                    t = max(1, int(total) if isinstance(total, (int, float)) else 20)
                    hw, hd, aw = int(hw or 0), int(hd or 0), int(aw or 0)
                    result["h2h_20_summary"] = f"主队 {hw}胜{round(100*hw/t)}% {hd}平{round(100*hd/t)}% {aw}负{round(100*aw/t)}%"
                except (TypeError, ValueError):
                    pass

    # 全部抓取：从所有 captured 中递归收集历史交锋列表、积分榜等
    def _collect_match_like(obj: Any, out: list, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(obj, list):
            for item in obj:
                if not isinstance(item, dict):
                    continue
                home = item.get("homeTeamName") or item.get("homeTeam") or item.get("主队") or item.get("hostName")
                away = item.get("awayTeamName") or item.get("awayTeam") or item.get("客队") or item.get("guestName")
                if not home and not away:
                    continue
                score = item.get("score") or item.get("fullTimeScore") or item.get("sectionsNo999") or item.get("比分")
                date_str = item.get("matchDate") or item.get("date") or item.get("比赛日期") or ""
                if isinstance(score, dict):
                    h, a = score.get("home"), score.get("away")
                    score_str = f"{h}:{a}" if h is not None and a is not None else str(score)
                else:
                    score_str = str(score) if score is not None else ""
                if home or away:
                    out.append({"date": date_str[:10] if date_str else "", "home": home or "", "away": away or "", "score": score_str})
            return
        if isinstance(obj, dict):
            for v in obj.values():
                _collect_match_like(v, out, depth + 1)

    def _collect_standings_like(obj: Any, out: list, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(obj, list):
            for item in obj:
                if not isinstance(item, dict):
                    continue
                rank = item.get("ranking") or item.get("rank") or item.get("排名")
                name = item.get("teamName") or item.get("name") or item.get("队名") or item.get("teamAbbName")
                pts = item.get("points") or item.get("积分")
                if rank is not None and name:
                    out.append({"rank": rank, "team": name, "points": pts})
            return
        if isinstance(obj, dict):
            for v in obj.values():
                _collect_standings_like(v, out, depth + 1)

    for data in captured:
        _collect_match_like(data, result["h2h_matches"], 0)
        _collect_standings_like(data, result["standings"], 0)

    # 去重并限制条数
    seen_h2h = set()
    unique_h2h = []
    for m in result["h2h_matches"]:
        key = (m.get("date"), m.get("home"), m.get("away"))
        if key not in seen_h2h and (m.get("home") or m.get("away")):
            seen_h2h.add(key)
            unique_h2h.append(m)
    result["h2h_matches"] = unique_h2h[:20]
    seen_standings = set()
    unique_standings = []
    for s in result["standings"]:
        key = (s.get("rank"), s.get("team"))
        if key not in seen_standings:
            seen_standings.add(key)
            unique_standings.append(s)
    result["standings"] = unique_standings[:20]

    return result


def _merge_fixed_bonus_into_odds(odds: dict[str, Any], bonus: dict[str, Any]) -> None:
    """将固定奖金并入 odds，仅补充缺失字段，不覆盖已有。"""
    for k, v in bonus.items():
        if v is None:
            continue
        if odds.get(k) is None:
            odds[k] = v


def _enrich_matches_with_fixed_bonus(matches: list[dict[str, Any]]) -> None:
    """对每场有 match_id 的比赛请求 getFixedBonusV1，补齐让球数等固定奖金。"""
    for m in matches:
        mid = m.get("match_id")
        if mid is None:
            continue
        bonus = fetch_sporttery_fixed_bonus(mid)
        if bonus:
            odds = m.get("odds") or {}
            _merge_fixed_bonus_into_odds(odds, bonus)
            m["odds"] = odds


def _normalize_business_date(business_date: Any) -> str | None:
    """将 matchInfoList[].businessDate 转为 YYYY-MM-DD 便于与请求日期比对。支持 260227、2026-02-27 等。"""
    if business_date is None:
        return None
    s = str(business_date).strip()
    if not s:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    if len(s) >= 6 and s[:6].isdigit():
        # 260227 -> 2026-02-27
        try:
            y = "20" + s[0:2]
            m = s[2:4]
            d = s[4:6]
            return f"{y}-{m}-{d}"
        except Exception:
            pass
    return None


def _parse_webapi_value(value: dict, date_str: str, filter_by_business_date: bool = False) -> list[dict[str, Any]]:
    """解析 getMatchListV1 / getMatchCalculatorV1 的 value。
    结构：value.matchInfoList 为按日分组的列表，每项含 businessDate（竞彩日）和 subMatchList（该竞彩日下的比赛）。
    拉取的是 matchInfoList[].businessDate 这个时间下的比赛：按父级 businessDate 找到对应项，取其 subMatchList 全部即可；
    subMatchList 中每条自带的 businessDate 与竞彩日无关（可能是开球日等），不参与筛选。"""
    matches: list[dict[str, Any]] = []
    raw_list = value.get("matchInfoList") or []
    # 兼容接口返回对象 {"0": {...}, "1": {...}} 的情况
    if isinstance(raw_list, dict):
        info_list = list(raw_list.values())
    else:
        info_list = raw_list if isinstance(raw_list, list) else []
    want_date = (date_str or "")[:10] if filter_by_business_date else None
    for day in info_list:
        bd = _normalize_business_date(day.get("businessDate"))
        # 按竞彩日筛选时：父级无 businessDate 的分组不能假定等于请求日，否则会把多日赛程误挂到所选日
        if want_date is not None and bd is None:
            continue
        if want_date is not None and bd is not None and bd != want_date:
            continue
        # 拉取的是 matchInfoList[].businessDate 该竞彩日下的比赛；subMatchList 中条目的 businessDate 与父级无关，整段取用不按子项过滤
        raw_subs = day.get("subMatchList") or []
        sub_list = list(raw_subs.values()) if isinstance(raw_subs, dict) else (raw_subs if isinstance(raw_subs, list) else [])
        # 该 matchInfoList 项对应的竞彩日（API 权威字段）
        jc_date_from_api = bd if bd else (date_str[:10] if date_str else None)
        for m in sub_list:
            if not isinstance(m, dict):
                continue
            home_cn = (m.get("homeTeamAbbName") or m.get("homeTeamName") or "").strip()
            away_cn = (m.get("awayTeamAbbName") or m.get("awayTeamName") or "").strip()
            if not home_cn and not away_cn:
                continue
            ft = m.get("sectionsNo999") or m.get("fullTimeScore") or m.get("score")
            h, a = _parse_fulltime_score(ft)
            if h is None and a is None:
                h = m.get("homeScore") or m.get("hostScore") or m.get("homeGoal")
                a = m.get("awayScore") or m.get("guestScore") or m.get("awayGoal")
                if h is not None and a is not None:
                    try:
                        h, a = int(h), int(a)
                    except (TypeError, ValueError):
                        h, a = None, None
                else:
                    h, a = None, None
            ht = m.get("sectionsNo1") or m.get("halfTimeScore") or m.get("htScore")
            hh, ha = _parse_half(ht) if isinstance(ht, str) else (None, None)
            if hh is None and isinstance(ht, dict):
                hh, ha = ht.get("home"), ht.get("away")
            md = m.get("matchDate") or day.get("matchDate") or ""
            per_date = (md[:10].replace("/", "-") if isinstance(md, str) and len(md) >= 10 else None)
            mt = (m.get("matchTime") or "").strip()
            match_date = _derive_actual_match_date(date_str, mt, per_date)
            if not match_date:
                match_date = per_date or date_str
            match_num = _extract_match_num(m, match_date)
            if not match_num:
                match_num = _format_match_num_fallback(match_date, len(matches))
            # 让球数/胜平负取自 subMatchList[].hhad（goalLine,h,d,a）与同级 had（h,d,a）
            odds = _extract_odds(m)
            match_id = m.get("matchId") or m.get("match_id") or m.get("id")
            if match_id is None:
                mn = m.get("matchNum")
                if isinstance(mn, (int, float)) or (isinstance(mn, str) and mn.isdigit()):
                    match_id = mn
            if match_id is not None:
                try:
                    match_id = int(match_id)
                except (TypeError, ValueError):
                    match_id = None
            home_en = (m.get("homeTeamAbbEnName") or m.get("homeTeamEnName") or "").strip() or None
            away_en = (m.get("awayTeamAbbEnName") or m.get("awayTeamEnName") or "").strip() or None
            out = {
                "home_cn": home_cn, "away_cn": away_cn,
                "home": _norm_team(home_cn), "away": _norm_team(away_cn),
                "homeTeamAbbEnName": home_en,
                "awayTeamAbbEnName": away_en,
                "home_score": h, "away_score": a, "half_home": hh, "half_away": ha,
                "league": (m.get("leagueAbbName") or "").strip() or "英超",
                "match_date": match_date, "match_time": m.get("matchTime") or "", "status": (m.get("matchStatusName") or "").strip(),
                "match_num": match_num,
                "match_id": match_id,
                "odds": odds,
            }
            if jc_date_from_api:
                out["businessDate"] = jc_date_from_api  # 竞彩日，来自 matchInfoList[].businessDate，存库时用作 jc_date
            matches.append(out)
    return matches


def _sporttery_list_row_still_actionable(m: dict[str, Any]) -> bool:
    """
    getMatchListV1 子场：是否仍算「当前可参与」的开售行。
    接口常把已完赛且带比分的场次留在当日 matchInfoList 里，若全部计入会导致「官网已无在售」时 Web 仍显示昨日赛程。
    """
    h, a = m.get("home_score"), m.get("away_score")
    if h is not None and a is not None:
        try:
            int(h)
            int(a)
            return False
        except (TypeError, ValueError):
            pass
    st = (m.get("status") or "").strip()
    if st:
        for kw in ("已开奖", "全场完", "比赛取消", "无效场次", "比赛中断", "弃赛", "延期", "待定赛果"):
            if kw in st:
                return False
        if st in ("完赛", "已完成", "取消", "无效"):
            return False
    return True


def sporttery_official_match_ids_for_jc_date(jc_date: str) -> set[int] | None:
    """
    从 getMatchListV1 解析指定竞彩日（matchInfoList[].businessDate）下的开售场次 matchId 集合。
    仅包含仍「可售/未以全场比分结束」的场次，与官网「当前在售」心智一致。
    请求失败返回 None（调用方勿据此清空库内展示）；成功但该日无可售场次时返回空 set。
    """
    want = (jc_date or "").strip()[:10]
    if len(want) != 10:
        return None
    data = fetch_sporttery_match_list_v1(want, referer="https://www.sporttery.cn/")
    if not data or not data.get("success") or not isinstance(data.get("value"), dict):
        return None
    value = data["value"]
    lst = _parse_webapi_value(value, want, filter_by_business_date=True)
    ids: set[int] = set()
    for m in lst:
        if not _sporttery_list_row_still_actionable(m):
            continue
        mid = m.get("match_id")
        if mid is None:
            continue
        try:
            ids.add(int(mid))
        except (TypeError, ValueError):
            continue
    return ids


def _enrich_jc_matches_english_names(matches: list[dict[str, Any]]) -> None:
    """为 jc_matches 中缺少英文名的比赛补全 homeTeamAbbEnName / awayTeamAbbEnName。
    优先用 jingcai_teams 表（已整理的 中文→英文），其次 SPORTTERY_TEAM_MAP，再 DB teams.name_zh→name，最后已有 home/away。
    """
    jc_cn_to_en: dict[str, str] = {}
    try:
        from db.self_improvement import get_jingcai_cn_to_en
        jc_cn_to_en = get_jingcai_cn_to_en()
    except Exception:
        pass
    try:
        from db.repo import get_team_by_name_zh
    except ImportError:
        get_team_by_name_zh = None
    for m in matches:
        home_cn = (m.get("home_cn") or "").strip()
        away_cn = (m.get("away_cn") or "").strip()
        if not m.get("homeTeamAbbEnName"):
            en = jc_cn_to_en.get(home_cn) or SPORTTERY_TEAM_MAP.get(home_cn)
            if not en and get_team_by_name_zh and home_cn:
                t = get_team_by_name_zh(home_cn)
                if t and t.get("name"):
                    en = (t.get("name") or "").strip()
            if not en:
                en = m.get("home")
            if en:
                m["homeTeamAbbEnName"] = en
        if not m.get("awayTeamAbbEnName"):
            en = jc_cn_to_en.get(away_cn) or SPORTTERY_TEAM_MAP.get(away_cn)
            if not en and get_team_by_name_zh and away_cn:
                t = get_team_by_name_zh(away_cn)
                if t and t.get("name"):
                    en = (t.get("name") or "").strip()
            if not en:
                en = m.get("away")
            if en:
                m["awayTeamAbbEnName"] = en


# 五大联赛（用于筛选竞彩场次）
BIG5_LEAGUES = {"英超", "西甲", "意甲", "德甲", "法甲"}

# 体彩可能返回的长联赛名 -> 五大联赛简称，便于识别并写入
SPORTTERY_LEAGUE_TO_BIG5: dict[str, str] = {
    "英格兰超级联赛": "英超", "英超": "英超",
    "西班牙甲级联赛": "西甲", "西甲": "西甲",
    "意大利甲级联赛": "意甲", "意甲": "意甲",
    "德国甲级联赛": "德甲", "德甲": "德甲",
    "法国甲级联赛": "法甲", "法甲": "法甲",
}


def normalize_league_to_big5(lig: str) -> str:
    """体彩返回的联赛名转为五大联赛简称，若不是五大联赛则原样返回。"""
    s = (lig or "").strip()
    return SPORTTERY_LEAGUE_TO_BIG5.get(s) or s


def get_sporttery_fixtures_for_prediction(
    date_str: str,
    five_leagues_only: bool = True,
) -> list[dict[str, Any]]:
    """
    从体彩 API 获取指定日期的未开赛场次，用于预测。
    five_leagues_only: 只返回五大联赛；否则返回全部未开赛。
    返回 [{home, away, competition, home_cn, away_cn}, ...]
    """
    out = _try_api_fetch(date_str)
    result = []
    for m in out:
        if m.get("home_score") is not None and m.get("away_score") is not None:
            continue  # 已完赛，跳过
        lig = (m.get("league") or "").strip()
        if five_leagues_only and lig not in BIG5_LEAGUES:
            continue
        result.append({
            "home": m.get("home") or m.get("home_cn"),
            "away": m.get("away") or m.get("away_cn"),
            "competition": LEAGUE_MAP.get(lig, "Premier League") if lig else "Premier League",
            "home_cn": m.get("home_cn"),
            "away_cn": m.get("away_cn"),
        })
    return result


def _try_api_fetch(date_str: str) -> list[dict[str, Any]]:
    """尝试通过 requests 请求体彩接口。"""
    if not requests:
        return []
    match_num = date_str.replace("-", "")[2:]  # 2026-02-10 -> 260210
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://m.sporttery.cn",
        "Referer": "https://m.sporttery.cn/",
    }
    # 竞彩「资讯」页与「计算器」页可能用 jc/getMatchList 或 webapi；先试 jc 再试 webapi，以尽量与官网列表一致
    urls = [
        (f"https://webapi.sporttery.cn/gateway/jc/football/getMatchList.qry?matchDate={match_num}", "jc"),
        (f"https://webapi.sporttery.cn/gateway/uniform/fb/getMatchDataPageListV1.qry?method=all&pageSize=50&matchDate={date_str}", "webapi"),
        (f"https://m.sporttery.cn/api/mjc/zqsj?matchNumDate={match_num}", "mjc"),
        (f"https://info.sporttery.cn/football/match_list.php?d={date_str}", "info"),
    ]
    for url, api_type in urls:
        try:
            r = requests.get(url, headers=headers, timeout=12, proxies=_sporttery_proxies())
            if r.status_code != 200:
                continue
            data = r.json()
            if api_type == "webapi" and data.get("success") and isinstance(data.get("value"), dict):
                out = _parse_webapi_value(data["value"], date_str, filter_by_business_date=True)
            else:
                # jc/getMatchList 等可能返回 { success, value: { matchList } } 或直接 matchList
                raw = data.get("matchList") or data.get("data") or data.get("list")
                if not raw and data.get("success") and isinstance(data.get("value"), dict):
                    raw = data["value"].get("matchList") or data["value"].get("list") or data["value"].get("data")
                if not raw:
                    raw = data
                out = _parse_raw_match_list(raw, date_str)
            if out:
                return out
        except Exception:
            continue
    return []


def crawl_sporttery_for_date(
    date_str: str,
    from_json: str | Path | None = None,
    json_body: dict | list | None = None,
    *,
    prefer_result_api: bool = False,
) -> list[dict[str, Any]]:
    """
    抓取体彩网指定日期的足球场次。
    date_str: YYYY-MM-DD
    from_json: 若提供，则从该 JSON 文件解析（用户可手动保存 API 响应）
    json_body: 若提供，则直接解析该 dict/list（Web 粘贴 JSON 用）
    prefer_result_api: 为 True 且为历史日期时，跳过 getMatchListV1，直接使用赛果接口 getUniformMatchResultV1（按年批量拉取时可用，避免先请求失败再回退）
    返回列表，每项: {home_cn, away_cn, home, away, home_score, away_score, half_home, half_away, league, match_date, match_time, status}
    """
    if json_body is not None:
        out = parse_sporttery_response(json_body, date_str)
        if out:
            _enrich_matches_with_fixed_bonus(out)
            enrich_matches_from_calculator_api(out, date_str)
            _enrich_jc_matches_english_names(out)
            return out
    if from_json:
        out = crawl_sporttery_from_json(from_json, date_str)
        if out:
            _enrich_matches_with_fixed_bonus(out)
            enrich_matches_from_calculator_api(out, date_str)
            _enrich_jc_matches_english_names(out)
            return out

    from datetime import date as _date
    _today = _date.today().isoformat()
    _is_historical = (date_str or "")[:10] < _today

    # 未指定「优先赛果」时，先尝试体彩统一比赛列表接口 getMatchListV1（与官网竞彩列表页一致）
    # 指定 prefer_result_api 且为历史日期时，直接走下方赛果接口，避免先请求失败再回退
    if not (prefer_result_api and _is_historical):
        try:
            data = fetch_sporttery_match_list_v1(date_str, referer="https://www.sporttery.cn/")
            _dbg = lambda *a: None
            try:
                _dbg = lambda msg: print(f"  [getMatchListV1] {msg}", flush=True)
            except Exception:
                pass
            if not data:
                _dbg("请求无响应，使用回退接口")
            elif not data.get("success"):
                _dbg("请求 success=False，使用回退接口")
            elif not isinstance(data.get("value"), dict):
                _dbg("value 非 dict，使用回退接口")
            else:
                value = data["value"]
                want = (date_str or "")[:10]
                # 仅有在「缺少 matchInfoList」时才走赛果/计算器等回退；若接口成功返回了该键（含空列表/空对象），
                # 则视为官网对该竞彩日的权威开售列表，避免回退接口按比赛日/开奖日拉回其它日场次（用户见「无竞彩仍显示多场」）。
                if "matchInfoList" not in value or value.get("matchInfoList") is None:
                    _dbg("value 无 matchInfoList 键或为 null，使用回退接口")
                elif not value.get("matchInfoList"):
                    if _is_historical:
                        _dbg(f"竞彩日 {want}：历史日期 matchInfoList 为空，继续尝试赛果接口回退")
                    else:
                        _dbg(f"竞彩日 {want}：getMatchListV1 的 matchInfoList 为空，视为官方该日无开售足球竞彩")
                        return []
                else:
                    info_list = list(value["matchInfoList"].values()) if isinstance(value["matchInfoList"], dict) else (
                        value["matchInfoList"] if isinstance(value["matchInfoList"], list) else []
                    )
                    bd_list = [_normalize_business_date(d.get("businessDate")) for d in info_list if isinstance(d, dict)]
                    _dbg(f"matchInfoList 共 {len(info_list)} 项，businessDate 有: {bd_list}")
                    out_list = _parse_webapi_value(value, date_str, filter_by_business_date=True)
                    if out_list:
                        _dbg(f"竞彩日 {want} 解析到 {len(out_list)} 场")
                        _enrich_matches_with_fixed_bonus(out_list)
                        enrich_matches_from_calculator_api(out_list, date_str)
                        _enrich_jc_matches_english_names(out_list)
                        return out_list
                    if _is_historical:
                        _dbg(f"竞彩日 {want}：历史日期在 matchInfoList 中无匹配 businessDate，继续尝试赛果接口回退")
                    else:
                        _dbg(f"竞彩日 {want}：matchInfoList 中无匹配 businessDate 的场次，视为官方该日 0 场（不使用回退）")
                        return []
        except Exception as e:
            try:
                print(f"  [getMatchListV1] 异常: {e}", flush=True)
            except Exception:
                pass

    # 历史日期用赛果接口 getUniformMatchResultV1（官网「竞赛程赛果」同源），或作为 getMatchListV1 的回退
    if _is_historical:
        try:
            _out: list[dict[str, Any]] = []
            _seen_ids: set[int] = set()
            _page_no = 1
            _page_size = 30
            _max_pages = 50
            while _page_no <= _max_pages:
                _data = fetch_sporttery_uniform_match_result_v1(
                    match_begin_date=date_str,
                    match_end_date=date_str,
                    page_no=_page_no,
                    page_size=_page_size,
                )
                if not _data or not _data.get("success"):
                    break
                _value = _data.get("value") if isinstance(_data.get("value"), dict) else _data
                if not isinstance(_value, dict):
                    break
                _page_matches = _parse_uniform_match_result_value(_value, date_str)
                if not _page_matches:
                    break
                if _page_no >= 2 and all(m.get("match_id") is None for m in _page_matches):
                    break
                _new = [m for m in _page_matches if m.get("match_id") not in _seen_ids]
                if not _new:
                    break
                for m in _new:
                    if m.get("match_id") is not None:
                        _seen_ids.add(m["match_id"])
                _out.extend(_new)
                if len(_page_matches) < _page_size:
                    break
                _page_no += 1
            if _out:
                print(f"  赛果接口返回 {len(_out)} 场（共 {_page_no} 页）", flush=True)
                _enrich_matches_with_fixed_bonus(_out)
                enrich_matches_from_calculator_api(_out, date_str)
                _enrich_jc_matches_english_names(_out)
                return _out
        except Exception:
            pass

    # 1. 官网「计算器/资讯」同源接口 getMatchCalculatorV1，与页面「周五 260227 共13场」一致
    try:
        for try_with_date in (True, False):
            data = fetch_sporttery_match_calculator_v1(
                date_str if try_with_date else None,
                referer="https://www.lottery.gov.cn/jc/jsq/zqspf/",
            )
            if not data or not data.get("success") or not isinstance(data.get("value"), dict):
                continue
            calc_list = _parse_calculator_value(data["value"], date_str)
            if try_with_date is False and calc_list:
                # 未传日期时接口返回多日，只保留请求日期的场次（按实际比赛日或编号日）
                match_num = date_str.replace("-", "")[2:]
                filtered = [
                    m for m in calc_list
                    if (m.get("match_date") or "")[:10] == date_str[:10]
                    or (m.get("match_num") or "").endswith(match_num[-4:])
                ]
                if filtered:
                    calc_list = filtered
            if calc_list:
                enrich_matches_from_calculator_api(calc_list, date_str, referer="https://www.lottery.gov.cn/jc/jsq/zqspf/")
                _enrich_jc_matches_english_names(calc_list)
                return calc_list
            # 若带日期无数据，下一轮 try_with_date=False 会再试一次不传日期
    except Exception:
        pass

    # 2. 赛果开奖接口 getUniformMatchResultV1（支持历史日期，如 2025 年；非历史日或上面未命中时再试）
    try:
        out: list[dict[str, Any]] = []
        page_no = 1
        page_size = 30
        while True:
            data = fetch_sporttery_uniform_match_result_v1(
                match_begin_date=date_str,
                match_end_date=date_str,
                page_no=page_no,
                page_size=page_size,
            )
            if not data or not data.get("success"):
                break
            value = data.get("value") if isinstance(data.get("value"), dict) else data
            if not isinstance(value, dict):
                break
            page_matches = _parse_uniform_match_result_value(value, date_str)
            if not page_matches:
                break
            out.extend(page_matches)
            if len(page_matches) < page_size:
                break
            page_no += 1
        if out:
            _enrich_matches_with_fixed_bonus(out)
            enrich_matches_from_calculator_api(out, date_str)
            _enrich_jc_matches_english_names(out)
            return out
    except Exception:
        pass

    # 3. 回退：旧列表接口（getMatchDataPageListV1 / mjc/zqsj 等，可能返回欧罗巴等另一批）
    out = _try_api_fetch(date_str)
    if out:
        _enrich_matches_with_fixed_bonus(out)
        enrich_matches_from_calculator_api(out, date_str)
        _enrich_jc_matches_english_names(out)
        return out

    # 4. 尝试 Playwright（若未安装或 Chromium 不可用则静默返回空，不抛错）
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    base_url = "https://m.sporttery.cn/mjc/zqsj/"
    url = f"{base_url}?tab=concern"
    matches: list[dict[str, Any]] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
            )
            page = context.new_page()
            api_data: list[dict] = []

            def handle_response(response):
                try:
                    ct = response.headers.get("content-type", "")
                    if response.status == 200 and ("json" in ct or "application" in ct):
                        body = response.text()
                        if body and ("match" in body.lower() or "home" in body.lower() or "sections" in body.lower()):
                            data = json.loads(body)
                            if isinstance(data, dict) and (data.get("matchList") or data.get("data") or data.get("list")):
                                api_data.append(data)
                            elif isinstance(data, list) and len(data) > 0:
                                api_data.append({"matchList": data})
                except Exception:
                    pass

            page.on("response", handle_response)
            try:
                page.goto(url, wait_until="networkidle", timeout=20000)
                page.wait_for_timeout(2000)
                for data in api_data:
                    raw = data.get("matchList") or data.get("data") or data.get("list")
                    matches.extend(_parse_raw_match_list(raw, date_str))
            except Exception:
                pass
            finally:
                browser.close()

        if not matches:
            matches = _extract_from_dom(date_str, base_url)
    except Exception:
        # Playwright 未安装 Chromium 或启动失败时，静默返回空
        matches = []

    # 去重
    seen = set()
    out = []
    for m in matches:
        key = (m.get("home_cn"), m.get("away_cn"), m.get("match_date"))
        if key not in seen:
            seen.add(key)
            out.append(m)

    _enrich_matches_with_fixed_bonus(out)
    enrich_matches_from_calculator_api(out, date_str)
    _enrich_jc_matches_english_names(out)
    return out


def _extract_from_dom(date_str: str, _base_url: str) -> list[dict[str, Any]]:
    """备用：从页面 DOM 提取（当 API 拦截未成功时）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    matches = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_extra_http_headers({"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"})
        try:
            page.goto(_base_url + "?tab=concern", wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(3000)

            # 尝试选择日期：点击日期筛选
            date_sel = page.locator("[class*='date'], [class*='Date'], [data-date]").first
            if date_sel.count() > 0:
                date_sel.click()
                page.wait_for_timeout(1000)
                # 选择目标日期
                target = date_str.replace("-", "")
                page.locator(f"text={target[:4]},{target[4:6]},{target[6:8]}").first.click()
                page.wait_for_timeout(2000)

            # 比赛卡片：常见结构
            cards = page.locator("[class*='match'], [class*='Match'], [class*='item']").all()
            for card in cards[:50]:
                txt = card.inner_text()
                if "VS" not in txt and "vs" not in txt.lower():
                    continue
                lines = txt.split("\n")
                home_cn = away_cn = ""
                home_s, away_s = None, None
                half_h, half_a = None, None
                for i, line in enumerate(lines):
                    line = line.strip()
                    if re.match(r"^\d+[:\-]\d+$", line):
                        if home_s is None:
                            parts = re.split(r"[:\-]", line)
                            if len(parts) == 2:
                                home_s, away_s = int(parts[0]), int(parts[1])
                        elif "半场" in " ".join(lines[max(0, i - 2) : i + 2]):
                            parts = re.split(r"[:\-]", line.replace("半场", "").strip())
                            if len(parts) == 2:
                                half_h, half_a = int(parts[0]), int(parts[1])
                    if "VS" in line or "vs" in line:
                        parts = re.split(r"\s*(?:VS|vs)\s*", line)
                        if len(parts) >= 2:
                            home_cn, away_cn = parts[0].strip(), parts[1].strip()
                if home_cn or away_cn:
                    match_num = _format_match_num_fallback(date_str, len(matches))
                    matches.append({
                        "home_cn": home_cn,
                        "away_cn": away_cn,
                        "home": _norm_team(home_cn),
                        "away": _norm_team(away_cn),
                        "home_score": home_s,
                        "away_score": away_s,
                        "half_home": half_h,
                        "half_away": half_a,
                        "league": "英超",
                        "match_date": date_str,
                        "match_time": "",
                        "status": "已完成" if home_s is not None else "",
                        "match_num": match_num,
                    })
        except Exception:
            pass
        finally:
            browser.close()

    return matches


# 体彩网 PC 端 赛果开奖 页
SPORTTERY_WWW_RESULTS_URL = "https://www.sporttery.cn/jc/zqsgkj/"
# 体彩网手机端 足球数据-赛果 tab，用于核对与抓取赛果
SPORTTERY_M_RESULT_URL = "https://m.sporttery.cn/mjc/zqsj/?tab=result"


def crawl_sporttery_results_from_mobile(date_str: str) -> list[dict[str, Any]]:
    """
    从体彩网手机端「足球数据」赛果页 (m.sporttery.cn/mjc/zqsj/?tab=result) 抓取指定日期的赛果，
    拦截页面请求的 JSON 解析，返回带比分的场次列表。与 crawl_sporttery_for_date 相同格式。
    """
    date_str = (date_str or "").strip()[:10]
    if not date_str or len(date_str) != 10:
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    api_matches: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()

    def on_response(response):
        try:
            ct = response.headers.get("content-type", "") or ""
            if response.status != 200 or "json" not in ct.lower():
                return
            body = response.text()
            if not body or ("match" not in body.lower() and "sections" not in body.lower() and "value" not in body.lower()):
                return
            data = json.loads(body)
            if isinstance(data, dict) and data.get("success") and isinstance(data.get("value"), dict):
                out = _parse_webapi_value(data["value"], date_str)
            else:
                raw = data.get("matchList") or data.get("data") or data.get("list") if isinstance(data, dict) else None
                if raw is None and isinstance(data, list):
                    raw = data
                if raw is not None:
                    out = _parse_raw_match_list(raw, date_str)
                else:
                    return
            for m in out:
                if m.get("home_score") is None and m.get("away_score") is None:
                    continue
                mdate = (m.get("match_date") or date_str)[:10]
                if mdate != date_str:
                    continue
                key = (m.get("home_cn") or m.get("home"), m.get("away_cn") or m.get("away"), mdate)
                if key not in seen_keys:
                    seen_keys.add(key)
                    api_matches.append(m)
        except Exception:
            pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
                viewport={"width": 390, "height": 844},
            )
            page = context.new_page()
            page.on("response", on_response)
            try:
                page.goto(SPORTTERY_M_RESULT_URL, wait_until="networkidle", timeout=20000)
                page.wait_for_timeout(3500)
            except Exception:
                pass
            finally:
                browser.close()
    except Exception:
        return []

    return api_matches


def crawl_sporttery_results_from_www(date_str: str) -> list[dict[str, Any]]:
    """
    从体彩网 PC 端「足球赛果开奖」页 (www.sporttery.cn/jc/zqsgkj/) 抓取指定日期的赛果，
    用于填充仪表盘的实际比分与对错。优先拦截页面请求的 JSON，否则解析 DOM 表格。
    返回与 crawl_sporttery_for_date 相同格式的列表。
    """
    date_str = (date_str or "").strip()[:10]
    if not date_str or len(date_str) != 10:
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    api_matches: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()

    def on_response(response):
        try:
            ct = response.headers.get("content-type", "") or ""
            if response.status != 200 or "json" not in ct.lower():
                return
            body = response.text()
            if not body or ("match" not in body.lower() and "sections" not in body.lower() and "value" not in body.lower()):
                return
            data = json.loads(body)
            if isinstance(data, dict) and data.get("success") and isinstance(data.get("value"), dict):
                out = _parse_webapi_value(data["value"], date_str)
            else:
                raw = data.get("matchList") or data.get("data") or data.get("list") if isinstance(data, dict) else None
                if raw is None and isinstance(data, list):
                    raw = data
                if raw is not None:
                    out = _parse_raw_match_list(raw, date_str)
                else:
                    return
            for m in out:
                if m.get("home_score") is None and m.get("away_score") is None:
                    continue
                key = (m.get("home_cn") or m.get("home"), m.get("away_cn") or m.get("away"), m.get("match_date") or date_str)
                if key not in seen_keys:
                    seen_keys.add(key)
                    api_matches.append(m)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        page.on("response", on_response)
        try:
            page.goto(SPORTTERY_WWW_RESULTS_URL, wait_until="networkidle", timeout=25000)
            page.wait_for_timeout(3000)
        except Exception:
            pass
        finally:
            browser.close()

    if api_matches:
        return api_matches

    # 无 JSON 时从 DOM 解析赛果表格
    return _parse_www_results_dom(date_str)


def _parse_www_results_dom(date_str: str) -> list[dict[str, Any]]:
    """从 www.sporttery.cn 赛果开奖页的 DOM 表格解析赛果。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    matches: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        })
        try:
            page.goto(SPORTTERY_WWW_RESULTS_URL, wait_until="networkidle", timeout=25000)
            page.wait_for_timeout(3500)
            # 表格：常见为 table 或 div 网格，含 主队、客队、全场比分、半场比分
            rows = page.locator("table tbody tr, [class*='result'] tr, [class*='table'] tr").all()
            for row in rows[:80]:
                txt = row.inner_text()
                if "VS" not in txt and "vs" not in txt.lower():
                    continue
                cells = row.locator("td").all()
                if len(cells) < 4:
                    continue
                cell_texts = [c.inner_text().strip() for c in cells]
                full_score = half_score = None
                home_cn = away_cn = ""
                league = "英超"
                match_num = _format_match_num_fallback(date_str, len(matches))
                for i, s in enumerate(cell_texts):
                    score_clean = re.sub(r"\s+", "", s)
                    if re.match(r"^\d+[:\-]\d+$", score_clean):
                        ctx = " ".join(cell_texts[max(0, i - 2) : i + 3])
                        if "半场" in ctx:
                            half_score = _parse_score(s)
                        elif full_score is None:
                            full_score = _parse_score(s)
                    if "VS" in s or "vs" in s.lower():
                        parts = re.split(r"\s*(?:VS|vs)\s*", s, maxsplit=1)
                        if len(parts) >= 2:
                            home_cn = parts[0].replace("（让球）", "").strip()
                            away_cn = parts[1].strip()
                    if s in BIG5_LEAGUES or s in LEAGUE_MAP:
                        league = s
                if not home_cn and not away_cn:
                    for s in cell_texts:
                        if "VS" in s or "vs" in s:
                            parts = re.split(r"\s*(?:VS|vs)\s*", s, maxsplit=1)
                            if len(parts) >= 2:
                                home_cn, away_cn = parts[0].replace("（让球）", "").strip(), parts[1].strip()
                                break
                if not home_cn and not away_cn:
                    continue
                h, a = full_score if isinstance(full_score, tuple) else (None, None)
                hh, ha = half_score if isinstance(half_score, tuple) else (None, None)
                if h is None or a is None:
                    continue
                matches.append({
                    "home_cn": home_cn,
                    "away_cn": away_cn,
                    "home": _norm_team(home_cn),
                    "away": _norm_team(away_cn),
                    "home_score": h,
                    "away_score": a,
                    "half_home": hh,
                    "half_away": ha,
                    "league": league,
                    "match_date": date_str,
                    "match_time": "",
                    "status": "已完成",
                    "match_num": match_num,
                    "odds": {},
                })
        except Exception:
            pass
        finally:
            browser.close()

    return matches
