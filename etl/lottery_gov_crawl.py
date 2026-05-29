"""
中国体彩官网 https://www.lottery.gov.cn/jc/jsq/zqspf/ 足球胜平负（含让球数）数据抓取。

通过 Playwright 打开官网计算器页面，拦截页面发起的 XHR/fetch 请求，获取与
竞彩场次、让球数相关的接口地址与响应格式，再直接请求该接口获取数据。
"""
from __future__ import annotations

import json
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # type: ignore

# 官网计算器页面
LOTTERY_GOV_ZQSPF_URL = "https://www.lottery.gov.cn/jc/jsq/zqspf/"

# 若已知官网实际请求的接口（通过 discover_gov_api 发现后可填在此处），则优先直接请求，避免每次开浏览器
# 格式: {"url": "https://...", "method": "GET", "params_from_page": true}
LOTTERY_GOV_API_CONFIG: dict[str, Any] = {}


def _has_match_or_rqspf(data: dict | list) -> bool:
    """粗略判断响应是否像竞彩场次/让球数据。"""
    if isinstance(data, list):
        return len(data) > 0 and isinstance(data[0], dict)
    if isinstance(data, dict):
        s = json.dumps(data)
        return (
            "match" in s.lower()
            or "rqspf" in s.lower()
            or "goalLine" in s
            or "hhadList" in s
            or "让球" in s
            or "handicap" in s.lower()
        )
    return False


def discover_gov_api(timeout_ms: int = 15000) -> list[dict[str, Any]]:
    """
    用 Playwright 打开官网足球胜平负计算器页面，拦截网络请求，收集疑似接口的 URL 与响应体。
    返回 [{ "url": str, "method": str, "response_preview": str | dict }]。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    captured: list[dict[str, Any]] = []

    def on_response(response):
        try:
            url = response.url
            rt = response.request.resource_type
            if rt not in ("xhr", "fetch"):
                return
            body = response.text()
            if not body or len(body) > 500_000:
                return
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return
            if not _has_match_or_rqspf(data):
                return
            captured.append({
                "url": url,
                "method": response.request.method,
                "response_preview": body[:2000] if len(body) > 2000 else data,
            })
        except Exception:
            pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            )
            context.set_default_timeout(timeout_ms)
            page = context.new_page()
            page.on("response", on_response)
            page.goto(LOTTERY_GOV_ZQSPF_URL, wait_until="networkidle", timeout=timeout_ms)
            page.wait_for_timeout(5000)
            browser.close()
    except Exception:
        pass
    return captured


def _parse_generic_match_list(raw: list, date_str: str) -> list[dict[str, Any]]:
    """解析通用 matchList / data / list 结构，提取主客队、联赛、让球数。"""
    from etl.sporttery_crawl import _extract_odds, _norm_team, _parse_fulltime_score

    matches = []
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict):
            continue
        home_cn = (m.get("homeTeamAbbName") or m.get("homeTeamName") or m.get("home_cn") or m.get("home") or "").strip()
        away_cn = (m.get("awayTeamAbbName") or m.get("awayTeamName") or m.get("away_cn") or m.get("away") or "").strip()
        if not home_cn and not away_cn:
            continue
        md = m.get("matchDate") or m.get("match_date") or date_str
        match_date = (md[:10].replace("/", "-") if isinstance(md, str) and len(md) >= 10 else date_str)
        ft = m.get("sectionsNo999") or m.get("fullTimeScore") or m.get("score")
        h, a = _parse_fulltime_score(ft)
        odds = _extract_odds(m)
        match_id = m.get("matchId") or m.get("match_id") or m.get("id")
        if match_id is not None:
            try:
                match_id = int(match_id)
            except (TypeError, ValueError):
                match_id = None
        league = (m.get("leagueAbbName") or m.get("league") or "").strip() or "英超"
        matches.append({
            "home_cn": home_cn, "away_cn": away_cn,
            "home": _norm_team(home_cn), "away": _norm_team(away_cn),
            "home_score": h, "away_score": a,
            "league": league,
            "match_date": match_date,
            "match_time": m.get("matchTime") or m.get("match_time") or "",
            "status": (m.get("matchStatusName") or m.get("status") or "").strip(),
            "match_id": match_id,
            "odds": odds,
        })
    return matches


def parse_gov_api_response(body: str | dict | list, date_str: str) -> list[dict[str, Any]]:
    """
    解析官网接口返回的 JSON，得到与 sporttery_crawl 相同结构的场次列表（含 odds.rqspf_handicap）。
    若结构同体彩 webapi（value.matchInfoList），则复用体彩解析。
    """
    if isinstance(body, str):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return []
    else:
        data = body

    if isinstance(data, list):
        return _parse_generic_match_list(data, date_str)
    if isinstance(data, dict):
        # 与体彩 webapi 同结构的响应直接交给体彩解析
        from etl.sporttery_crawl import parse_sporttery_response
        if data.get("success") and isinstance(data.get("value"), dict):
            out = parse_sporttery_response(data, date_str)
            if out:
                return out
        raw = data.get("matchList") or data.get("data") or data.get("list") or []
        return _parse_generic_match_list(raw, date_str)
    return []


def fetch_gov_zqspf_via_playwright(date_str: str, timeout_ms: int = 20000) -> list[dict[str, Any]]:
    """
    通过 Playwright 打开官网页面，拦截并解析竞彩场次接口的响应。
    date_str: YYYY-MM-DD，仅用于解析结果中的 match_date；页面可能只返回当日/近期数据。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    result: list[dict[str, Any]] = []
    date_str = (date_str or "")[:10]

    def on_response(response):
        nonlocal result
        try:
            url = response.url
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            body = response.text()
            if not body or len(body) > 800_000:
                return
            data = json.loads(body)
            if not _has_match_or_rqspf(data):
                return
            parsed = parse_gov_api_response(data, date_str)
            if parsed and (not result or len(parsed) >= len(result)):
                result = parsed
        except Exception:
            pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            )
            context.set_default_timeout(timeout_ms)
            page = context.new_page()
            page.on("response", on_response)
            page.goto(LOTTERY_GOV_ZQSPF_URL, wait_until="networkidle", timeout=timeout_ms)
            page.wait_for_timeout(4000)
            browser.close()
    except Exception:
        pass
    return result


def fetch_gov_zqspf_direct(url: str, date_str: str, method: str = "GET", **kwargs: Any) -> list[dict[str, Any]]:
    """已知接口 URL 时直接 requests 请求。"""
    if not requests:
        return []
    date_str = (date_str or "")[:10]
    headers = kwargs.pop("headers", {}) or {}
    headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0")
    headers.setdefault("Referer", LOTTERY_GOV_ZQSPF_URL)
    try:
        if method.upper() == "GET":
            r = requests.get(url, headers=headers, timeout=15, **kwargs)
        else:
            r = requests.post(url, headers=headers, timeout=15, **kwargs)
        if r.status_code != 200:
            return []
        return parse_gov_api_response(r.text, date_str)
    except Exception:
        return []


def fetch_zqspf_for_date(date_str: str, prefer_playwright: bool = True) -> list[dict[str, Any]]:
    """
    获取指定日期（或官网返回的近期）足球胜平负场次（含让球数）。
    优先用官网页内使用的体彩接口：getMatchCalculatorV1 / getSupportRateV1（见 sporttery_crawl），
    再试 LOTTERY_GOV_API_CONFIG；最后才用 Playwright 拦截。
    """
    date_str = (date_str or "")[:10]
    # 官网计算器页实际请求的体彩接口（同源 webapi.sporttery.cn）
    try:
        from etl.sporttery_crawl import (
            fetch_sporttery_match_calculator_v1,
            _parse_calculator_value,
            _try_api_fetch,
            enrich_matches_from_calculator_api,
        )
        data = fetch_sporttery_match_calculator_v1(
            date_str, referer="https://www.lottery.gov.cn/jc/jsq/zqspf/"
        )
        if data and data.get("success") and isinstance(data.get("value"), dict):
            out = _parse_calculator_value(data["value"], date_str)
            if out:
                return out
        # 用体彩场次列表 + getSupportRateV1 补让球数
        list_matches = _try_api_fetch(date_str)
        if list_matches:
            enrich_matches_from_calculator_api(
                list_matches, date_str,
                referer="https://www.lottery.gov.cn/jc/jsq/zqspf/",
            )
            if any((m.get("odds") or {}).get("rqspf_handicap") is not None for m in list_matches):
                return list_matches
    except Exception:
        pass
    cfg = LOTTERY_GOV_API_CONFIG
    if cfg.get("url"):
        url = cfg["url"]
        method = cfg.get("method", "GET")
        params = cfg.get("params") if "params" in cfg else None
        if params is None and cfg.get("params_from_page"):
            params = {"matchDate": date_str, "date": date_str}
        out = fetch_gov_zqspf_direct(url, date_str, method=method, params=params)
        if out:
            return out
    if prefer_playwright:
        return fetch_gov_zqspf_via_playwright(date_str)
    return []
