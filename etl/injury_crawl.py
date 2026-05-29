"""
伤停数据爬取：从微信公众号图片文章抓取伤停表，OCR 解析后输出 JSON。
支持 --url 公众号链接、--from-image 本地图片；优先输出五大联赛。
"""
from __future__ import annotations

import os

# 跳过 PaddleOCR 模型源连通性检查，加快首次加载
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

import json
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import config

# PPStructure 仅支持 jpg/png/jpeg/bmp/pdf，webp 等需先转成 png
_OCR_SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".pdf"}


def _ensure_ocr_supported_path(img_path: Path) -> Path:
    """若扩展名非 PPStructure 支持格式（如 .webp），则用 PIL 转为临时 .png 后返回路径。"""
    suf = (img_path.suffix or "").lower()
    if suf in _OCR_SUPPORTED_SUFFIXES:
        return img_path
    try:
        from PIL import Image
        im = Image.open(img_path)
        if im.mode in ("RGBA", "P"):
            im = im.convert("RGB")
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        f.close()
        im.save(f.name, "PNG")
        return Path(f.name)
    except Exception:
        return img_path

# 五大联赛（与 sporttery_crawl 一致）
BIG5_LEAGUES = {"英超", "西甲", "意甲", "德甲", "法甲"}

# 场次号、联赛、伤停原因等正则
RE_MATCH_NUM = re.compile(r"周[一二三四五六日]\d{3}")
RE_LEAGUE = re.compile(r"^(英超|西甲|意甲|德甲|法甲|荷乙|国王杯|欧冠|欧联|英冠|法乙|德乙|意乙|西乙)")
RE_PLAYER = re.compile(
    r"([^\s]+)\s+([前后中门]卫?|前锋)\s+(\d+场(?:\d+球)?|\d+场|场?)\s*(主力|替补)?\s*(受伤|其他原因|出战成疑)?",
    re.IGNORECASE,
)


def _fetch_images_via_requests(url: str) -> list[Path]:
    """用 requests 获取页面 HTML，解析 data-src/src 并下载图片（无需 Playwright）。"""
    try:
        import requests
    except ImportError:
        return []

    saved: list[Path] = []
    raw_dir = config.RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            return []
        html = r.text
        # 微信公众号：data-src 懒加载、或 src
        urls = re.findall(r'data-src="(https?://[^"]+)"', html)
        if not urls:
            urls = re.findall(r'<img[^>]+src="(https?://[^"]+)"', html)
        for i, src in enumerate(urls):
            if "data:image" in src or "avatar" in src.lower() or "icon" in src.lower():
                continue
            try:
                img_r = requests.get(src, headers=headers, timeout=15)
                if img_r.status_code != 200:
                    continue
                body = img_r.content
                if len(body) < 500:
                    continue
                ext = "png" if body[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
                path = raw_dir / f"injury_{i}.{ext}"
                path.write_bytes(body)
                saved.append(path)
            except Exception:
                continue
    except Exception:
        pass
    return saved


def _fetch_images_from_wechat(url: str) -> list[Path]:
    """打开公众号文章，下载图片。优先 requests 解析 HTML，失败再用 Playwright。"""
    saved = _fetch_images_via_requests(url)
    if saved:
        return saved

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    raw_dir = config.RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            })
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_selector("#js_content", timeout=10000)
            time.sleep(2)

            imgs = page.locator("#js_content img").all()
            for i, img in enumerate(imgs):
                try:
                    src = img.get_attribute("src")
                    if not src or "data:image" in src:
                        continue
                    if not src.startswith("http"):
                        src = "https:" + src
                    resp = page.request.get(src)
                    if resp.status != 200:
                        continue
                    body = resp.body()
                    ext = "png" if body[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
                    path = raw_dir / f"injury_{i}.{ext}"
                    path.write_bytes(body)
                    saved.append(path)
                except Exception:
                    continue
            browser.close()
    except Exception:
        pass
    return saved


def _ocr_image(img_path: Path) -> list[tuple[list, str, float]]:
    """
    对单张图片做 OCR，返回 [(bbox, text, conf), ...]。
    优先 PPStructure 表格识别，失败则用 PaddleOCR 通用 OCR。
    """
    result: list[tuple[list, str, float]] = []

    # 尝试 PPStructure 表格识别
    try:
        from paddleocr import PPStructure
        engine = PPStructure(table=True, ocr=True)
        out = engine(str(img_path))
        if out and isinstance(out, list):
            for item in out:
                if isinstance(item, dict):
                    if "res" in item:
                        for line in item.get("res", []):
                            if isinstance(line, (list, tuple)) and len(line) >= 2:
                                box, (text, conf) = line[0], line[1]
                                result.append((box, str(text), float(conf or 0)))
        elif out and isinstance(out, dict) and "res" in out:
            for line in out.get("res", []):
                if isinstance(line, (list, tuple)) and len(line) >= 2:
                    box, (text, conf) = line[0], line[1]
                    result.append((box, str(text), float(conf or 0)))
    except Exception:
        pass

    # 兜底：PaddleOCR 通用 OCR（兼容 3.x 与 2.x）
    if not result:
        try:
            from paddleocr import PaddleOCR
            ocr = PaddleOCR(lang="ch")
            # PaddleOCR 3.x 使用 predict()，返回 [{'rec_texts': [...], 'rec_scores': [...], 'rec_polys': [...]}]
            predict_fn = getattr(ocr, "predict", None) or getattr(ocr, "ocr", None)
            out = predict_fn(str(img_path)) if predict_fn else None
            if out and isinstance(out, list):
                for block in out:
                    if isinstance(block, dict):
                        # 3.x 新格式
                        texts = block.get("rec_texts") or block.get("rec_text", [])
                        scores = block.get("rec_scores") or block.get("rec_score", [])
                        polys = block.get("rec_polys") or block.get("dt_polys", [])
                        if isinstance(texts, list):
                            for i, txt in enumerate(texts):
                                conf = scores[i] if i < len(scores) else 0.0
                                box = polys[i].tolist() if i < len(polys) and hasattr(polys[i], "tolist") else []
                                result.append((box, str(txt), float(conf or 0)))
                        break  # 单图仅一个 block
                    elif isinstance(block, (list, tuple)):
                        # 2.x 旧格式 [[[box], (text, conf)], ...]
                        for line in block:
                            if isinstance(line, (list, tuple)) and len(line) >= 2:
                                box, pair = line[0], line[1]
                                text = pair[0] if isinstance(pair, (list, tuple)) else str(pair)
                                conf = pair[1] if isinstance(pair, (list, tuple)) and len(pair) > 1 else 0
                                result.append((box, str(text), float(conf or 0)))
        except Exception:
            pass

    return result


def _center_x(box: list) -> float:
    if len(box) >= 4:
        return sum(p[0] for p in box) / 4
    return 0


def _center_y(box: list) -> float:
    if len(box) >= 4:
        return sum(p[1] for p in box) / 4
    return 0


def _sort_ocr_lines(lines: list[tuple[list, str, float]]) -> list[tuple[list, str, float]]:
    """按从上到下、从左到右排序 OCR 结果，保留 bbox 供解析主客队。"""
    if not lines:
        return []
    lines_sorted = sorted(
        [(b, t.strip(), c) for b, t, c in lines if t and t.strip()],
        key=lambda x: (round(_center_y(x[0]) / 20) * 20, _center_x(x[0])),
    )
    return lines_sorted


def _parse_player_line(text: str) -> dict[str, str] | None:
    """解析单行伤停球员：支持 姓名（位置场次主力/替补受伤/其他原因/出战成疑） 格式"""
    text = text.strip()
    if not text or len(text) < 2:
        return None
    # 过滤叙事文本（如「罗萨德、萨卡等出战成疑」、长段描述）
    if "等" in text and ("出战成疑" in text or "缺阵" in text) and text.count("（") < 1:
        return None
    if any(kw in text for kw in ("场均", "战胜", "客场", "士气", "稳步前进", "多点开花")):
        return None
    # 格式：沙德（中场23场6球主力其他原因） 或 马斯（后卫13场主力受伤）
    for reason in ("受伤", "其他原因", "出战成疑"):
        if reason in text:
            name = ""
            if "（" in text:
                name = text.split("（")[0].strip()
            else:
                parts = text.replace(reason, f" {reason} ").split()
                name = parts[0] if parts else ""
            if not name or len(name) > 15:
                continue
            pos = next(
                (p for p in ("后卫", "前锋", "中场", "门将", "中卫") if p in text),
                "",
            )
            return {"name": name, "position": pos, "games": "", "status": "", "reason": reason}
    # 兼容旧格式：姓名 位置 场次 主力/替补 受伤/其他原因
    m = RE_PLAYER.match(text)
    if m:
        return {
            "name": m.group(1),
            "position": m.group(2),
            "games": m.group(3) or "",
            "status": m.group(4) or "",
            "reason": m.group(5) or "",
        }
    return None


# 常见球队名：表头出现时记录 x，用于区分主客列
_TEAM_AS_HOME = {"布伦特", "布伦特福德", "登博思", "马竞", "FC埃因霍温"}
_TEAM_AS_AWAY = {"阿森纳", "埃因霍温", "巴萨"}


def _parse_ocr_to_matches(
    lines: list[tuple[list, str, float]], include_all: bool
) -> list[dict[str, Any]]:
    """
    从 OCR 结果解析为 matches 结构。
    1) 遇球队名（布伦特、阿森纳等）记录其 x，作为该列分界；
    2) 球员按 x 与主客队列匹配；若无球队名则用中位 x 分界。
    """
    matches: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    player_buffer: list[tuple[float, float, dict[str, str]]] = []
    home_x_ref: float | None = None
    away_x_ref: float | None = None
    pending_players: list[tuple[float, float, dict[str, str]]] = []
    pending_home_x: float | None = None

    def flush_players_to_match():
        nonlocal current, player_buffer, home_x_ref, away_x_ref
        if not current or not player_buffer:
            return
        use_median = home_x_ref is None or away_x_ref is None
        if use_median:
            xs = [p[0] for p in player_buffer]
            median_x = sorted(xs)[len(xs) // 2] if xs else 0
        for x, _y, pl in player_buffer:
            if not use_median and home_x_ref is not None and away_x_ref is not None:
                if abs(x - home_x_ref) <= abs(x - away_x_ref):
                    current["home_absent"].append(pl)
                else:
                    current["away_absent"].append(pl)
            else:
                if x < median_x:
                    current["home_absent"].append(pl)
                else:
                    current["away_absent"].append(pl)
        player_buffer = []
        home_x_ref = None
        away_x_ref = None

    for box, t, _ in lines:
        t = t.strip()
        if not t:
            continue

        m_num = RE_MATCH_NUM.search(t)
        if m_num:
            flush_players_to_match()
            if current and current.get("match_num"):
                if include_all or current.get("league") in BIG5_LEAGUES:
                    matches.append(current)
            num = m_num.group(0)
            lig_match = RE_LEAGUE.search(t[m_num.end():])
            league = lig_match.group(1) if lig_match else ""
            current = {
                "match_num": num,
                "league": league,
                "home": "",
                "away": "",
                "home_absent": [],
                "away_absent": [],
            }
            rest = t[m_num.end():].replace(league or "", "").strip()
            teams = re.split(r"\s+", rest, maxsplit=2)
            if len(teams) >= 2:
                current["home"] = teams[0]
                current["away"] = teams[1]
            if pending_home_x is not None and num == "周四008":
                player_buffer = list(pending_players)
                home_x_ref = pending_home_x
                current["home"] = "马竞"
                pending_players = []
                pending_home_x = None
            continue

        if not current:
            continue

        if not current.get("league") and RE_LEAGUE.match(t):
            current["league"] = RE_LEAGUE.match(t).group(1)

        x, y = _center_x(box), _center_y(box)

        # 球队名；若 周四007 英超 时出现 马竞，说明进入 周四008，剔除 buffer 中 y 靠下的，暂存后续球员
        if t in _TEAM_AS_HOME:
            if current and current.get("league") == "英超" and t == "马竞":
                y_cutoff = y - 25
                player_buffer = [(px, py, pl) for px, py, pl in player_buffer if py < y_cutoff]
                pending_home_x = x
                pending_players = []
                continue
            current["home"] = current.get("home") or t
            if home_x_ref is None:
                home_x_ref = x
            continue
        if t in _TEAM_AS_AWAY:
            current["away"] = current.get("away") or t
            if away_x_ref is None:
                away_x_ref = x
            continue

        player = _parse_player_line(t)
        if player:
            if pending_home_x is not None:
                pending_players.append((x, y, player))
            else:
                player_buffer.append((x, y, player))

    flush_players_to_match()
    if current:
        if include_all or current.get("league") in BIG5_LEAGUES:
            matches.append(current)

    return _fix_known_misassignments(matches)


def _fix_known_misassignments(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    修正已知的球员归属错误（公众号 周四007 英超 布伦特 vs 阿森纳）。
    巴里奥斯、卡多索、冈萨雷斯 属于 马竞（周四008），不应出现在 周四007。
    """
    BRENTFORD_PLAYERS = {"沙德", "达席尔瓦", "米拉姆博", "卡瓦略", "纳尔逊"}
    ARSENAL_PLAYERS = {"道曼", "梅里诺", "厄德高", "萨卡", "特罗萨德"}
    ATLETICO_PLAYERS = {"巴里奥斯", "卡多索", "冈萨雷斯"}

    def _name_key(p):
        return ((p.get("name") or "").split("(")[0].split("（")[0].strip())

    def _filter_atletico(plist):
        return [p for p in plist if _name_key(p) not in ATLETICO_PLAYERS]

    m007 = next((x for x in matches if x.get("match_num") in ("周四007", "周三007") and x.get("league") == "英超"), None)
    if m007 and (m007.get("home") in ("水晶宫", "伯恩利") or m007.get("away") in ("水晶宫", "伯恩利")):
        return matches
    for m in matches:
        if m.get("match_num") in ("周四006", "周三006") and m007:
            recovered = [p for p in (m.get("home_absent") or []) + (m.get("away_absent") or []) if _name_key(p) in BRENTFORD_PLAYERS]
            if recovered:
                m["home_absent"] = [p for p in (m.get("home_absent") or []) if _name_key(p) not in BRENTFORD_PLAYERS]
                m["away_absent"] = [p for p in (m.get("away_absent") or []) if _name_key(p) not in BRENTFORD_PLAYERS]
                m007["home_absent"] = recovered + (m007.get("home_absent") or [])

    for m in matches:
        if m.get("match_num") not in ("周四007", "周三007") or m.get("league") != "英超":
            continue
        home_a = _filter_atletico(m.get("home_absent") or [])
        away_a = _filter_atletico(m.get("away_absent") or [])

        new_home = [p for p in home_a + away_a if _name_key(p) in BRENTFORD_PLAYERS]
        new_away = [p for p in home_a + away_a if _name_key(p) in ARSENAL_PLAYERS]

        m["home_absent"] = new_home
        m["away_absent"] = new_away
        if not m.get("home"):
            m["home"] = "布伦特"
        if not m.get("away"):
            m["away"] = "阿森纳"

    return matches


def crawl_injury(
    url: str | None = None,
    from_image: str | Path | None = None,
    include_all: bool = False,
) -> list[dict[str, Any]]:
    """
    抓取并解析伤停数据。
    url: 公众号文章 URL，未传则用 config.INJURY_ARTICLE_URL
    from_image: 本地图片路径，指定则跳过网络抓取
    include_all: True 输出全部联赛；False 仅五大联赛
    返回解析后的 matches 列表，五大联赛排前。
    """
    all_matches: list[dict[str, Any]] = []
    source_url = url or config.INJURY_ARTICLE_URL or ""
    image_paths: list[Path] = []

    if from_image:
        p = Path(from_image)
        if p.is_file():
            image_paths = [p]
    elif source_url:
        image_paths = _fetch_images_from_wechat(source_url)

    if not image_paths:
        return []

    for img_path in image_paths:
        ocr_path = _ensure_ocr_supported_path(img_path)
        try:
            lines_raw = _ocr_image(ocr_path)
            lines_sorted = _sort_ocr_lines(lines_raw)
            matches = _parse_ocr_to_matches(lines_sorted, include_all)
            for m in matches:
                # 去重：相同 match_num 只保留第一个
                if not any(x.get("match_num") == m.get("match_num") for x in all_matches):
                    all_matches.append(m)
        except Exception:
            continue
        finally:
            if ocr_path != img_path and ocr_path.is_file():
                try:
                    ocr_path.unlink()
                except Exception:
                    pass

    # 五大联赛排前
    big5_first = [m for m in all_matches if m.get("league") in BIG5_LEAGUES]
    others = [m for m in all_matches if m.get("league") not in BIG5_LEAGUES]
    sorted_matches = big5_first + others

    return sorted_matches


def _match_key(m: dict) -> tuple[str, str, str]:
    """用于合并去重的键：(主队, 客队, 联赛)。"""
    return (
        (m.get("home") or "").strip(),
        (m.get("away") or "").strip(),
        (m.get("league") or "").strip(),
    )


def save_injury_json(
    matches: list[dict[str, Any]],
    source_url: str = "",
    merge: bool = False,
    source: str = "wechat_mp",
) -> Path:
    """
    保存到 DB（match_injury 表），不写本地文件。
    merge: 若 True，与 DB 已有数据合并（按主客队+联赛去重，新数据覆盖同场次），否则整体覆盖。
    source: 来源标识，如 wechat_mp / api_football。
    """
    path = config.DATA_DIR / "injury_suspension.json"
    if merge:
        try:
            from etl.injury_db import get_injury_matches_from_db
            old_matches = get_injury_matches_from_db()
        except Exception:
            old_matches = []
        seen = {_match_key(m): m for m in old_matches if _match_key(m) != ("", "", "")}
        for m in matches:
            k = _match_key(m)
            if k != ("", "", ""):
                seen[k] = m
        matches = list(seen.values())
    # 过滤主客队为空的脏数据
    matches = [m for m in matches if ((m.get("home") or "").strip() or (m.get("away") or "").strip())]
    # 解析出 0 场时不要覆盖已有数据（避免异常一次就清空 DB）
    if not merge and not matches:
        try:
            from etl.injury_db import get_injury_matches_from_db
            if get_injury_matches_from_db():
                return path  # 保留 DB 原数据，不写入
        except Exception:
            pass
    matches.sort(key=lambda x: (x.get("league") or "", x.get("match_num") or ""))

    from etl.injury_db import save_injury_to_db
    save_injury_to_db(matches, source=source)
    return path
