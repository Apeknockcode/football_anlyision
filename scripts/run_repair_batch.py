#!/usr/bin/env python3
"""
一键修复批次：
1) 补 2026 积分榜
2) 回填 fixture_id 缺口
3) 继续半场兜底（经 jc-sync-match-results）
4) 赔率探测（带自动止损）

任一步检测到 API-FOOTBALL 当日配额/套餐限制时立即中止，并输出汇总。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


RATE_LIMIT_PATTERNS = (
    "request limit for the day",
    "you have reached the request limit",
    "套餐/计划限制",
    "请求超限",
    "触达 api-football 每日请求上限",
)


@dataclass
class StepResult:
    name: str
    command: str
    ok: bool
    halted_on_limit: bool
    exit_code: int
    elapsed_sec: float
    note: str


def _contains_rate_limit(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in RATE_LIMIT_PATTERNS)


def _run_step(name: str, cmd: list[str]) -> StepResult:
    t0 = time.time()
    display_cmd = " ".join(cmd)
    print(f"\n=== {name} ===")
    print(f"$ {display_cmd}", flush=True)
    p = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    elapsed = time.time() - t0
    merged = f"{out}\n{err}"
    halted_on_limit = _contains_rate_limit(merged)
    ok = p.returncode == 0 and not halted_on_limit
    note = "ok"
    if halted_on_limit:
        note = "触发 API 配额/套餐限制，已中止后续步骤"
    elif p.returncode != 0:
        note = f"命令失败（exit={p.returncode}）"
    return StepResult(
        name=name,
        command=display_cmd,
        ok=ok,
        halted_on_limit=halted_on_limit,
        exit_code=p.returncode,
        elapsed_sec=elapsed,
        note=note,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="一键执行修复批次，并在 API 配额限制时自动中止")
    parser.add_argument("--season", type=int, default=2026, help="积分榜赛季（默认 2026）")
    parser.add_argument("--fixture-year", type=int, default=2026, help="fixture_id 回填年份（默认 2026）")
    parser.add_argument("--sync-years", type=str, default="2026", help="赛果/半场同步年份，如 2026 或 2025,2026")
    parser.add_argument("--standings-rate", type=float, default=0.3, help="积分榜 rate-limit")
    parser.add_argument("--fixture-rate", type=float, default=1.0, help="fixture_id 回填 rate-limit")
    parser.add_argument("--detail-delay", type=float, default=1.2, help="jc-sync-match-results 逐场延迟")
    parser.add_argument("--day-delay", type=float, default=2.0, help="jc-sync-match-results 日间延迟")
    parser.add_argument("--uniform-delay", type=float, default=0.35, help="jc-sync-match-results uniform 分页延迟")
    parser.add_argument("--odds-rate", type=float, default=0.3, help="赔率拉取 rate-limit")
    parser.add_argument("--odds-limit", type=int, default=30, help="赔率探测日期上限（默认 30）")
    parser.add_argument("--odds-stoploss", type=int, default=5, help="赔率自动止损：连续 0 写入天数")
    parser.add_argument("--skip-standings", action="store_true", help="跳过步骤 1")
    parser.add_argument("--skip-fixture", action="store_true", help="跳过步骤 2")
    parser.add_argument("--skip-halftime", action="store_true", help="跳过步骤 3")
    parser.add_argument("--skip-odds", action="store_true", help="跳过步骤 4")
    args = parser.parse_args()

    steps: list[tuple[str, list[str]]] = []
    if not args.skip_standings:
        steps.append((
            "步骤1/4：补积分榜",
            [
                "python3",
                "main.py",
                "standings-api",
                "--season",
                str(args.season),
                "--rate-limit",
                str(args.standings_rate),
            ],
        ))
    if not args.skip_fixture:
        steps.append((
            "步骤2/4：补 fixture_id",
            [
                "python3",
                "main.py",
                "jc-match-analysis-backfill-fixture-id",
                "--year",
                str(args.fixture_year),
                "--rate-limit",
                str(args.fixture_rate),
            ],
        ))
    if not args.skip_halftime:
        steps.append((
            "步骤3/4：赛果+半场兜底",
            [
                "python3",
                "main.py",
                "jc-sync-match-results",
                "--years",
                str(args.sync_years),
                "--detail-delay",
                str(args.detail_delay),
                "--day-delay",
                str(args.day_delay),
                "--uniform-delay",
                str(args.uniform_delay),
            ],
        ))
    if not args.skip_odds:
        steps.append((
            "步骤4/4：赔率探测+止损",
            [
                "python3",
                "main.py",
                "odds-api-by-db",
                "--years",
                str(args.sync_years),
                "--skip-existing",
                "--limit",
                str(args.odds_limit),
                "--rate-limit",
                str(args.odds_rate),
                "--stop-loss-zero-write-streak",
                str(args.odds_stoploss),
            ],
        ))

    if not steps:
        print("未选择任何步骤（全部被 skip）。")
        return 0

    results: list[StepResult] = []
    halted = False
    for name, cmd in steps:
        r = _run_step(name, cmd)
        results.append(r)
        if r.halted_on_limit:
            halted = True
            break
        if not r.ok:
            break

    print("\n=== 批次汇总 ===")
    for r in results:
        status = "OK" if r.ok else ("HALT_LIMIT" if r.halted_on_limit else "FAILED")
        print(f"- {r.name}: {status} | {r.elapsed_sec:.1f}s | {r.note}")
    if halted:
        return 2
    all_ok = all(r.ok for r in results)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

