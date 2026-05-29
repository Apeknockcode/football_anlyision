#!/usr/bin/env python3
"""
CLI entry: ingest data, list matches, generate report. 个人自用，仅 CLI。
Usage:
  python main.py ingest [--max-matches N] [--competition C] [--season S] [--force-events]
  python main.py list
  python main.py report <match_id> [--open]
  python main.py ingest-fd [--seasons 2023,2024,2025,2026]   # 五大联赛 football-data.org
  python main.py predict "主队名" "客队名" [--competition 赛事名] [--last-n 10]  # 未来比赛预测
  python main.py predict-match <match_id> [--last-n 10]   # 对某场做预测并落库（事后对比用）
  python main.py predict-eval [--today]                  # 预测 vs 实际：MAE、胜平负准确率
  python main.py eval-all                                 # 一次跑完 predict-eval + recap（含 --by-competition）
  python main.py refresh-and-eval                         # 主库+竞彩一起重算 + eval-all（调参/重训 ML 后一次更新两处）
  python main.py jc-recap-refresh-eval                    # 一键：竞彩复盘→更新校准与ML→重算竞彩预测→再评估（预测全在 self_improvement_football 时用）
  python main.py jc-segment-auto                         # 一键用复盘结果调高命中率：导出细分→用细分覆盖再评估→提示启用后今日推荐自动应用
  python main.py refresh-models                           # calibrate-probs + train-outcome-ml（样本够时更新后可跑 refresh-and-eval）
  python main.py suggest-improvements                    # 输出后续可提升方向：数据积累、API 融合、联赛调参建议
  python main.py test-api-predictions --date YYYY-MM-DD [--home 主队 --away 客队]  # 测试 API 预测接口
  python main.py prediction-log add "主队" "客队" [--competition 赛事]   # 预测并写入日志（供每日统计）
  python main.py prediction-log set-result <序号> <主队进球> <客队进球>  # 填写实际比分
  python main.py prediction-log recap [--date 日期]      # 复盘：预测 vs 实际、MAE、胜平负准确率
  python main.py prediction-log list [--date 日期]       # 列出已记录的预测（含未填比分的）
  python main.py fixtures [--league PL] [--season 2025]   # 查看未来赛程（五大联赛）
  python main.py predict-today [--date 2026-02-11] [--no-save]  # 今日五大联赛竞彩预测
  python main.py predict-jingcai --date 2026-02-12  # 仅预测 data/jingcai_fixtures.json 中该日竞足彩场次
  python main.py enrich-fd [--match-id N] [--limit 20]  # 补全 fd 场次单场统计与半场比分
  python main.py jc-sync-match-results --date YYYY-MM-DD  # 竞彩：单日补全场/半场
  python main.py jc-sync-match-results --years 2026       # 按年逐日同步（默认日间/逐场/赛果分页有休眠防封；--detail-delay 0 可关逐场间隔）
  python main.py add-match "2026-02-11" "Chelsea FC" "Leeds United FC" 2 2 [--competition "Premier League"] [--half-time 1 1]  # 自己做复盘数据，写入 DB 后可用网页复盘生成完整报告
  python main.py auto-accumulate-log [--dates 2026-02-11,2026-02-12]  # 从 DB 取场次写预测/回填比分（数据来自 API）
  python main.py injury-crawl [--url 公众号链接] [--from-image 本地图片] [--all]  # 伤停数据：公众号图片 OCR，五大联赛优先
"""

# 五大联赛 API 代码 -> 赛事名称（与 football-data 入库名称一致，便于 resolve_competition_by_name）
LEAGUE_COMPETITION_NAMES = {
    "PL": "Premier League",
    "PD": "Primera División",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
}
import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Callable

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from etl.schema import get_engine, init_db, Match, Team
from engine.report_payload import build_report_payload
from report.assembler import ReportAssembler
from report.output import save_report_md, save_report_html
from sqlalchemy import text
from sqlalchemy.orm import Session


def cmd_ingest(args: argparse.Namespace) -> int:
    from etl.ingest import run_ingest, list_seasons_for_years
    years_arg = getattr(args, "years", None)
    if years_arg is not None:
        years = [int(x.strip()) for x in str(years_arg).split(",") if x.strip()]
        if not years:
            years = [2023, 2024, 2025, 2026]
        try:
            rows = list_seasons_for_years(years)
        except Exception as e:
            print("获取赛事列表失败:", e, file=sys.stderr)
            return 1
        if not rows:
            print("未找到 %s 年的开放数据。" % years, file=sys.stderr)
            return 1
        for i, r in enumerate(rows):
            cid, sid = r["competition_id"], r["season_id"]
            name = "%s %s" % (r.get("competition_name", "?"), r.get("season_name", "?"))
            print("[%s/%s] 拉取: %s (competition=%s season=%s)" % (i + 1, len(rows), name, cid, sid))
            run_ingest(competition_id=cid, season_id=sid, max_matches=None, force_events=args.force_events, ingest_all=True)
        return 0
    run_ingest(
        competition_id=args.competition,
        season_id=args.season,
        max_matches=args.max_matches,
        force_events=getattr(args, "force_events", False),
        ingest_all=getattr(args, "all", False),
    )
    return 0


def cmd_predict(args: argparse.Namespace) -> int:
    """基于历史比分预测主队 vs 客队：期望进球、胜/平/负概率。"""
    from engine.predict import predict_match, resolve_team_by_name, resolve_competition_by_name
    engine = get_engine()
    if not config.DB_PATH.exists():
        print("尚无数据，请先执行: python main.py ingest-fd", file=sys.stderr)
        return 1
    home_id = resolve_team_by_name(engine, args.home)
    away_id = resolve_team_by_name(engine, args.away)
    if home_id is None:
        print(f"未找到主队: {args.home}", file=sys.stderr)
        return 1
    if away_id is None:
        print(f"未找到客队: {args.away}", file=sys.stderr)
        return 1
    comp_id = None
    if getattr(args, "competition", None):
        comp_id = resolve_competition_by_name(engine, args.competition)
        if comp_id is None:
            print(f"未找到赛事: {args.competition}，将不限定赛事统计", file=sys.stderr)
    result = predict_match(
        engine,
        home_id,
        away_id,
        competition_id=comp_id,
        last_n=getattr(args, "last_n", 10),
    )
    print(f"预测：{args.home} vs {args.away}")
    print(f"  期望比分：{result['predicted_score'][0]} - {result['predicted_score'][1]} （期望进球 主 {result['exp_goals_home']} / 客 {result['exp_goals_away']}）")
    print(f"  主胜 {result['prob_home_win']*100:.0f}% | 平 {result['prob_draw']*100:.0f}% | 客胜 {result['prob_away_win']*100:.0f}%")
    print(f"  （基于双方最近 {result['based_on_last_n']} 场已结束比赛）")
    return 0


def cmd_predict_match(args: argparse.Namespace) -> int:
    """对指定 match_id 做预测并落库，供事后与完赛结果对比（predict-eval）。"""
    from etl.schema import init_db
    from engine.predict import predict_match, save_prediction
    engine = get_engine()
    init_db(engine)
    if not config.DB_PATH.exists():
        print("尚无数据，请先执行: python main.py ingest-fd", file=sys.stderr)
        return 1
    match_id = int(args.match_id)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT home_team_id, away_team_id, competition_id FROM matches WHERE id = :mid"),
            {"mid": match_id},
        ).fetchone()
    if not row:
        print(f"未找到比赛 id={match_id}，请用 list 查看已入库比赛。", file=sys.stderr)
        return 1
    home_id, away_id, comp_id = row[0], row[1], row[2]
    if home_id is None or away_id is None:
        print(f"比赛 {match_id} 缺少主客队信息。", file=sys.stderr)
        return 1
    last_n = getattr(args, "last_n", 10)
    result = predict_match(engine, home_id, away_id, competition_id=comp_id, last_n=last_n)
    save_prediction(engine, match_id, result)
    home_name, away_name = "主队", "客队"
    with engine.connect() as conn2:
        meta_row = conn2.execute(text("SELECT raw_meta FROM matches WHERE id = :mid"), {"mid": match_id}).fetchone()
        if meta_row and meta_row[0]:
            try:
                meta = json.loads(meta_row[0])
                home_name = meta.get("home_team_name", home_name)
                away_name = meta.get("away_team_name", away_name)
            except Exception:
                pass
    print(f"已落库预测 match_id={match_id}：{home_name} vs {away_name}")
    print(f"  期望比分：{result['predicted_score'][0]} - {result['predicted_score'][1]} （期望进球 主 {result['exp_goals_home']} / 客 {result['exp_goals_away']}）")
    print(f"  主胜 {result['prob_home_win']*100:.0f}% | 平 {result['prob_draw']*100:.0f}% | 客胜 {result['prob_away_win']*100:.0f}%")
    print(f"  完赛后可用: python main.py predict-eval [--today] 查看预测与实际的差异。")
    return 0


def cmd_predict_eval(args: argparse.Namespace) -> int:
    """对比已落库预测与完赛结果：期望进球 MAE、胜平负准确率（事后分析）；可选按 last_n/赛事拆解。Mongo 时直接读 predictions+matches 做评估。"""
    from etl.schema import init_db, get_engine
    from engine.predict import evaluate_predictions
    try:
        from db import repo
        use_mongo = repo.use_mongo()
    except Exception:
        use_mongo = False
    engine = get_engine() if not use_mongo else None
    if not use_mongo:
        init_db(engine)
        if not config.DB_PATH.exists():
            print("尚无数据，请先执行: python main.py ingest-fd", file=sys.stderr)
            return 1
    today_only = getattr(args, "today", False)
    by_last_n = getattr(args, "by_last_n", False)
    by_competition = getattr(args, "by_competition", False)
    year = getattr(args, "year", None)
    by_year = getattr(args, "by_year", False)
    report = evaluate_predictions(
        engine,
        today_only=today_only,
        by_last_n=by_last_n,
        by_competition=by_competition,
        year=year,
        by_year=by_year,
    )
    if report["n"] == 0:
        scope = "当天" if today_only else (f"{year}年" if year else "全量历史")
        print(f"暂无{scope}已完赛的预测记录。请先用 predict-match <match_id> 对将赛/已赛比赛做预测，完赛后运行本命令。")
        return 0
    scope = "（当天预测）" if today_only else (f"（{year}年）" if year else "（全量历史）")
    print(f"预测 vs 实际 {scope}，共 {report['n']} 条")
    print(f"  期望进球 MAE：主队 {report['mae_goals_home']} | 客队 {report['mae_goals_away']} | 整体 {report['mae_goals_overall']}")
    print(f"  胜平负猜对：{report['outcome_correct']}/{report['n']}，准确率 {report['outcome_accuracy']*100:.1f}%")
    if report.get("exact_score_accuracy") is not None:
        print(f"  精确比分命中：{report.get('exact_score_correct', 0)}/{report['n']}，准确率 {report['exact_score_accuracy']*100:.1f}%")
    if report.get("brier_outcome") is not None:
        print(f"  Brier 分数（概率校准，越小越好）：{report['brier_outcome']}")
    if report.get("confusion"):
        c = report["confusion"]
        print("  混淆矩阵（预测 vs 实际）：")
        print("           实际主胜  实际平  实际客胜")
        for pred in ("H", "D", "A"):
            lbl = "预测主胜" if pred == "H" else ("预测平  " if pred == "D" else "预测客胜")
            print(f"    {lbl}:  {c[pred]['H']:6d}   {c[pred]['D']:5d}   {c[pred]['A']:6d}")
    if report.get("draw_stats"):
        ds = report["draw_stats"]
        print("  平局统计：实际平局 {} 场，预测为平 {} 场，猜对平 {} 场；平局召回率 {}，预测平精确率 {}".format(
            ds["n_actual_draw"], ds["n_pred_draw"], ds["n_correct_draw"],
            f"{ds['draw_recall']*100:.1f}%" if ds.get("draw_recall") is not None else "N/A",
            f"{ds['draw_precision']*100:.1f}%" if ds.get("draw_precision") is not None else "N/A",
        ))
    if by_last_n and report.get("by_last_n"):
        print("  按 last_n 拆解：")
        for ln in sorted(report["by_last_n"].keys()):
            g = report["by_last_n"][ln]
            ex = f", 精确比分={g.get('exact_score_accuracy')*100:.1f}%" if g.get("exact_score_accuracy") is not None else ""
            print(f"    last_n={ln}: n={g['n']}, 准确率={g['outcome_accuracy']*100:.1f}%, MAE={g['mae_goals_overall']}{ex}")
    if by_competition and report.get("by_competition"):
        print("  按赛事拆解：")
        for key in sorted(report["by_competition"].keys()):
            g = report["by_competition"][key]
            name = key.split(":", 1)[-1] if ":" in key else key
            ex = f", 精确比分={g.get('exact_score_accuracy')*100:.1f}%" if g.get("exact_score_accuracy") is not None else ""
            print(f"    {name}: n={g['n']}, 准确率={g['outcome_accuracy']*100:.1f}%, MAE={g['mae_goals_overall']}{ex}")
    if by_year and report.get("by_year"):
        print("  按年份拆解：")
        for y in sorted(report["by_year"].keys()):
            g = report["by_year"][y]
            ex = f", 精确比分={g.get('exact_score_accuracy')*100:.1f}%" if g.get("exact_score_accuracy") is not None else ""
            print(f"    {y}: n={g['n']}, 准确率={g['outcome_accuracy']*100:.1f}%, MAE={g['mae_goals_overall']}{ex}")
    return 0


def _parse_years(years_str: str | None) -> list[int] | None:
    """解析 --years 2024,2025 为 [2024, 2025]，空则返回 None。"""
    if not (years_str or "").strip():
        return None
    out = []
    for s in (years_str or "").strip().split(","):
        s = s.strip()
        if not s:
            continue
        try:
            out.append(int(s))
        except ValueError:
            continue
    return out if out else None


def cmd_backfill_predictions(args: argparse.Namespace) -> int:
    """对「已完赛但尚未有预测」的场次批量生成预测并落库。USE_MONGO 时仅操作 MongoDB matches 表；predictions 按 match_id 去重（upsert）。"""
    from db import repo
    from engine.predict import backfill_predictions_for_finished_matches
    if repo.use_mongo():
        engine = None
        dedup = getattr(args, "dedup", False)
        if dedup:
            deleted = repo.predict_dedup_predictions_mongo()
            if deleted:
                print(f"已去除 predictions 重复文档 {deleted} 条（按 match_id 保留一条）。", flush=True)
    else:
        from etl.schema import init_db, get_engine
        engine = get_engine()
        init_db(engine)
        if not config.DB_PATH.exists():
            print("尚无数据，请先执行: python main.py ingest-fd", file=sys.stderr)
            return 1
    years = _parse_years(getattr(args, "years", None))
    limit = getattr(args, "limit", None)
    n = backfill_predictions_for_finished_matches(engine, years=years, limit=limit)
    print(f"已为 {n} 场已完赛无预测的比赛落库预测。")
    print("建议执行: python main.py predict-eval [--by-competition] 查看当前命中率。")
    print("调参或 refresh-models 后执行: python main.py repredict-history && python main.py predict-eval 对比新指标。")
    return 0


def cmd_predictions_dedup(args: argparse.Namespace) -> int:
    """仅 Mongo：按 match_id 去重 predictions，每组保留一条。"""
    from db import repo
    if not repo.use_mongo():
        print("predictions-dedup 仅支持 MongoDB（请设置 USE_MONGO=true 与 MONGO_URI）。", file=sys.stderr)
        return 1
    deleted = repo.predict_dedup_predictions_mongo()
    print(f"已去除 predictions 重复文档 {deleted} 条（按 match_id 保留一条）。")
    return 0


def cmd_predict_check_consistency(args: argparse.Namespace) -> int:
    """仅 Mongo：检查 predictions 与 matchs/matches 表是否一致，并说明常见原因。"""
    from db import repo
    if not repo.use_mongo():
        print("predict-check-consistency 仅支持 MongoDB。", file=sys.stderr)
        return 1
    stats = repo.predict_consistency_stats_mongo()
    if not stats:
        return 1
    coll = stats["matches_collection"]
    n_match = stats["matches_count"]
    n_pred = stats["predictions_count"]
    matched = stats["matched_count"]
    orphans = stats["orphan_match_ids"]
    print(f"当前比赛集合名（MONGO_MATCHES_COLLECTION）: {coll}")
    print(f"比赛表（{coll}）文档数: {n_match}")
    print(f"predictions 文档数: {n_pred}")
    print(f"  - 能对上比赛的预测: {matched}")
    print(f"  - 孤儿预测（match_id 在比赛表中不存在）: {len(orphans)}")
    if orphans:
        print(f"  孤儿 match_id 示例: {orphans[:10]}{' ...' if len(orphans) > 10 else ''}")
    if len(orphans) > 0 or (n_pred > 0 and n_match > 0 and matched == 0):
        print()
        print("可能原因：")
        if coll == "matches" and n_match == 0 and n_pred > 0:
            print("  1. 你的比赛数据在集合「matchs」里，但配置未改，代码在读「matches」。")
            print("     解决：在 .env 中设置 MONGO_MATCHES_COLLECTION=matchs，然后重新对 matchs 跑预测：")
            print("     python3 main.py predict-from-mongo-matches --limit 100")
        elif len(orphans) > 0:
            print("  2. predictions 里部分 match_id 来自其他集合或旧数据，当前比赛表里没有对应 id。")
            print("     可选：删除孤儿预测以保持一致，运行 predictions-dedup 去重后再用下方命令清理。")
        print("  3. 若曾切换过 MONGO_MATCHES_COLLECTION，会出现「预测指向旧集合的 id」与「新集合」对不上的情况。")
    if getattr(args, "delete_orphans", False) and orphans:
        from db import repo as _r
        for mid in orphans:
            _r.delete_predictions_by_match_id(mid)
        print(f"\n已删除 {len(orphans)} 条孤儿预测。")
    return 0


def cmd_repredict_history(args: argparse.Namespace) -> int:
    """用当前 config 对「已有预测且已完赛」的历史场次重算预测并写回。USE_MONGO 时仅操作 MongoDB。--db-only 时只写 DB（predictions 表），不更新 prediction_log。
    当主库为 self_improvement_football（MONGO_DB_NAME 与 MONGO_SELF_IMPROVEMENT_DB_NAME 一致）时，重算 jingcai_match_analysis 已完赛场次并写回 jc_predictions。"""
    from db import repo
    from engine.predict import repredict_evaluable_matches
    if repo.use_mongo():
        engine = None
    else:
        from etl.schema import init_db, get_engine
        engine = get_engine()
        init_db(engine)
        if not config.DB_PATH.exists():
            print("尚无数据，请先执行: python main.py ingest-fd", file=sys.stderr)
            return 1
    print("正在重算 DB 中已完赛的预测（场次较多时请稍候）...", flush=True)
    # 主库即竞彩库时：用 jingcai_match_analysis 已完赛 + jc_predictions 重算
    if repo.use_mongo() and getattr(config, "MONGO_DB_NAME", "") == getattr(config, "MONGO_SELF_IMPROVEMENT_DB_NAME", ""):
        n_db = repredict_jc_evaluable_matches()
    else:
        n_db = repredict_evaluable_matches(engine)
    db_only = getattr(args, "db_only", False)
    if db_only:
        print(f"已用当前参数重算并写回：DB 预测 {n_db} 场（仅 predictions 表，未动 prediction_log）。")
        print("可运行: python main.py predict-eval [--by-competition] 查看新指标。")
        return 0
    # 主库即竞彩库时：预测在 jc_predictions，不更新 prediction_log（该表为传统主库的日志表）
    if repo.use_mongo() and getattr(config, "MONGO_DB_NAME", "") == getattr(config, "MONGO_SELF_IMPROVEMENT_DB_NAME", ""):
        print(f"已用当前参数重算并写回：jc_predictions {n_db} 场（主库为竞彩库，不更新 prediction_log）。")
        print("可运行: python main.py jc-predictions-eval [--all-time] 查看新指标。")
        return 0
    from engine.prediction_log import repredict_log_entries_with_actuals
    print("正在重算 prediction_log 中已填比分的条目...", flush=True)
    n_log = repredict_log_entries_with_actuals(engine)
    print(f"已用当前参数重算并写回：DB 预测 {n_db} 场，prediction_log {n_log} 条。")
    print("可运行以下命令查看新指标：")
    print("  python main.py predict-eval [--by-competition]")
    print("  python main.py prediction-log recap [--from-db] [--by-competition]")
    return 0


def cmd_eval_all(args: argparse.Namespace) -> int:
    """一次跑完：主库为竞彩库时跑 jc-predictions-eval；否则 predict-eval + prediction-log recap。"""
    from db import repo
    import config
    # 主库即竞彩库时：评估用 jc_predictions + jingcai_match_analysis，不查传统 matches/predictions
    if repo.use_mongo() and getattr(config, "MONGO_DB_NAME", "") == getattr(config, "MONGO_SELF_IMPROVEMENT_DB_NAME", ""):
        print("--- jc-predictions-eval（全量，jc_predictions vs jingcai_match_analysis 已完赛） ---")
        jc_eval_ns = argparse.Namespace(year=None, date_from=None, date_to=None, all_time=True)
        return cmd_jc_predictions_eval(jc_eval_ns)
    ns = argparse.Namespace(today=False, by_last_n=False, by_competition=False)
    print("--- predict-eval（全量） ---")
    rc = cmd_predict_eval(ns)
    if rc != 0:
        return rc
    print()
    ns.by_competition = True
    print("--- predict-eval --by-competition ---")
    rc = cmd_predict_eval(ns)
    if rc != 0:
        return rc
    print()
    recap_ns = argparse.Namespace(date=None, by_competition=False, from_db=True, recap_limit=200)
    print("--- prediction-log recap（DB 全量，与 predict-eval 同源：predictions+matches） ---")
    rc = cmd_prediction_log_recap(recap_ns)
    if rc != 0:
        return rc
    print()
    recap_ns.by_competition = True
    print("--- prediction-log recap --from-db --by-competition ---")
    rc = cmd_prediction_log_recap(recap_ns)
    if rc != 0:
        return rc
    try:
        if repo.use_mongo():
            print()
            recap_log_ns = argparse.Namespace(date=None, by_competition=True, from_db=False, recap_limit=200)
            print("--- prediction-log recap（prediction_log 表，Mongo 已填比分） ---")
            cmd_prediction_log_recap(recap_log_ns)
    except Exception:
        pass
    return 0


def cmd_refresh_and_eval(args: argparse.Namespace) -> int:
    """主库 + 竞彩一起重算（repredict-all）再跑 eval-all。调参或重训 ML 后一次更新两处并出完整评估。"""
    repredict_args = argparse.Namespace(
        years=getattr(args, "years", "2025,2026") or "2025,2026",
        db_only=getattr(args, "db_only", False),
        verbose=getattr(args, "verbose", False),
    )
    rc = cmd_repredict_all(repredict_args)
    if rc != 0:
        return rc
    print()
    return cmd_eval_all(args)


def cmd_jc_recap_refresh_eval(args: argparse.Namespace) -> int:
    """一键：竞彩复盘（jc-predictions-eval）→ 更新校准与 ML（calibrate-probs + train-outcome-ml）→ 重算竞彩预测（全量，按库中所有年份）→ 再评估。适用于预测全在 self_improvement_football 时。"""
    from db.self_improvement import get_self_improvement_db, jc_match_analysis_list_dates
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    export_segment = getattr(args, "export_segment_stats", None)
    years_arg = (getattr(args, "years", None) or "").strip()
    verbose = getattr(args, "verbose", False)

    # 重算竞彩预测的年份：未传 --years 时用库中全部年份
    if not years_arg or years_arg.lower() == "all":
        all_dates = jc_match_analysis_list_dates()
        seen_years = set()
        for d in all_dates:
            day = (d.get("date") or "")[:10]
            if len(day) >= 4:
                try:
                    seen_years.add(int(day[:4]))
                except ValueError:
                    pass
        years_str = ",".join(str(y) for y in sorted(seen_years)) if seen_years else "2025,2026"
    else:
        years_str = years_arg

    # 1) 复盘：当前指标 + 可选导出细分命中率
    print("========== 1/4 竞彩复盘（当前指标）==========")
    jc_eval_ns = argparse.Namespace(
        year=None, date_from=None, date_to=None, all_time=True,
        export_segment_stats=export_segment, segment_stats=None, missed_limit=500,
        recap=False, include_hit_matches=False,
    )
    rc = cmd_jc_predictions_eval(jc_eval_ns)
    if rc != 0:
        return rc
    print()

    # 2) 更新校准与 ML
    print("========== 2/4 更新校准与 ML ==========")
    calib_ns = argparse.Namespace(no_db=False, no_log=False, by_competition=False, train_date_from=None, train_date_to=None)
    rc = cmd_calibrate_probs(calib_ns)
    if rc != 0:
        print("（calibrate-probs 未更新，继续执行 train-outcome-ml）", file=sys.stderr)
    print()
    rc2 = cmd_train_outcome_ml(argparse.Namespace(competition_id=None, train_date_from=None, train_date_to=None))
    if rc2 != 0:
        print("（train-outcome-ml 未更新，继续执行后续步骤）", file=sys.stderr)
    print()

    # 3) 重算竞彩预测（全量：库中所有年份，使新校准/ML 生效）
    print("========== 3/4 重算竞彩预测（全量）==========")
    print("年份: " + years_str + ("（库中全部）" if (not years_arg or years_arg.lower() == "all") else "（--years 指定）"))
    repredict_ns = argparse.Namespace(years=years_str, db_only=False, verbose=verbose)
    rc = cmd_repredict_all(repredict_ns)
    if rc != 0:
        return rc
    print()

    # 4) 再评估
    print("========== 4/4 再评估（新指标）==========")
    rc = cmd_jc_predictions_eval(jc_eval_ns)
    if rc != 0:
        return rc
    # 若导出了细分命中率，再用 segment-stats 跑一遍评估，便于对比「启用细分覆盖」后的命中率
    if export_segment and Path(export_segment).is_file():
        print("\n========== 使用细分覆盖再评估（数据驱动自我提升）==========")
        jc_eval_with_seg = argparse.Namespace(
            year=None, date_from=None, date_to=None, all_time=True,
            export_segment_stats=None, segment_stats=export_segment, missed_limit=500,
            recap=False, include_hit_matches=False,
            export_league_rec_params=getattr(jc_eval_ns, "export_league_rec_params", None),
            export_baseline=None,
        )
        cmd_jc_predictions_eval(jc_eval_with_seg)
        print("\n提示：在 .env 或 config 中设置 JC_REC_USE_SEGMENT_STATS=true 后，今日推荐将自动使用细分覆盖。")
    return 0


def cmd_jc_segment_auto(args: argparse.Namespace) -> int:
    """一键：用复盘结果自动调高命中率。导出细分命中率 → 用细分覆盖再评估 → 提示启用后今日推荐自动应用。"""
    import config as _config
    from pathlib import Path
    seg_path = getattr(_config, "JC_REC_SEGMENT_STATS_PATH", None) or str(_config.DATA_DIR / "jc_rec_segment_hit_rates.json")
    seg_path = str(Path(seg_path).resolve())

    # 1) 全量复盘并导出「联赛×推荐类型×概率区间」细分命中率到默认路径
    print("========== 1/2 复盘并导出细分命中率 ==========")
    ns_export = argparse.Namespace(
        year=None, date_from=None, date_to=None, all_time=True,
        export_segment_stats=seg_path, segment_stats=None, missed_limit=500,
        recap=False, include_hit_matches=False, export_league_rec_params=None, export_baseline=None,
    )
    rc = cmd_jc_predictions_eval(ns_export)
    if rc != 0:
        return rc
    if not Path(seg_path).is_file():
        print("未生成细分命中率文件，请检查是否有已完赛且有预测的场次。", file=sys.stderr)
        return 1

    # 2) 用细分覆盖再评估，看「数据驱动自我提升」后的命中率
    print("\n========== 2/2 使用细分覆盖再评估 ==========")
    ns_seg = argparse.Namespace(
        year=None, date_from=None, date_to=None, all_time=True,
        export_segment_stats=None, segment_stats=seg_path, missed_limit=500,
        recap=False, include_hit_matches=False, export_league_rec_params=None, export_baseline=None,
    )
    cmd_jc_predictions_eval(ns_seg)

    enabled = getattr(_config, "JC_REC_USE_SEGMENT_STATS", False)
    print("\n" + "=" * 60)
    if enabled:
        print("已启用 JC_REC_USE_SEGMENT_STATS，今日推荐已自动使用细分覆盖。")
    else:
        print("【启用自动应用】在 .env 或 config.py 中设置：")
        print("  JC_REC_USE_SEGMENT_STATS=true")
        print("之后「今日推荐」与推荐接口将自动读取上述细分命中率，在历史表现差的细分改为推荐胜/负，从而调高命中率。")
        print("细分文件路径: %s" % seg_path)
    print("=" * 60)
    return 0


def cmd_jc_recap_auxiliary_for_2026(args: argparse.Namespace) -> int:
    """用 2021–2025 年数据为辅，导出细分命中率与联赛参数，用于提高 2026 年比赛列表单场推荐命中率。"""
    import config as _config
    from pathlib import Path
    seg_path = getattr(_config, "JC_REC_AUXILIARY_SEGMENT_STATS_PATH", None) or str(_config.DATA_DIR / "jc_rec_segment_hit_rates_2021_2025.json")
    league_path = getattr(_config, "JC_REC_AUXILIARY_LEAGUE_PARAMS_PATH", None) or str(_config.DATA_DIR / "jc_league_rec_params_2021_2025.json")
    seg_path = str(Path(seg_path).resolve())
    league_path = str(Path(league_path).resolve())

    # 1) 用 2021-2025 年数据导出细分命中率与联赛参数
    print("========== 1/2 用 2021–2025 年数据导出细分命中率与联赛参数 ==========")
    ns_aux = argparse.Namespace(
        year=None, date_from="2021-01-01", date_to="2025-12-31", all_time=False,
        export_segment_stats=seg_path, segment_stats=None, missed_limit=500,
        recap=False, include_hit_matches=False, export_league_rec_params=league_path, export_baseline=None,
    )
    rc = cmd_jc_predictions_eval(ns_aux)
    if rc != 0:
        return rc
    if not Path(seg_path).is_file():
        print("未生成辅助年细分文件，请确认 2021–2025 年有已完赛且有预测的场次。", file=sys.stderr)
        return 1

    # 2) 用上述参数评估 2026 年单场推荐命中率
    print("\n========== 2/2 用辅助年参数评估 2026 年单场推荐命中率 ==========")
    ns_2026 = argparse.Namespace(
        year=2026, date_from=None, date_to=None, all_time=False,
        export_segment_stats=None, segment_stats=seg_path, missed_limit=500,
        recap=False, include_hit_matches=False, export_league_rec_params=None, export_baseline=None,
    )
    cmd_jc_predictions_eval(ns_2026)

    use_aux = getattr(_config, "JC_REC_FOR_2026_USE_AUXILIARY", True)
    print("\n" + "=" * 60)
    print("2026 年比赛列表 / 今日推荐（日期为 2026 时）将优先使用上述 2021–2025 导出的参数。")
    if use_aux:
        print("已启用 JC_REC_FOR_2026_USE_AUXILIARY，2026 列表单场推荐已使用辅助年细分与联赛门槛。")
    else:
        print("在 .env 中设置 JC_REC_FOR_2026_USE_AUXILIARY=true 后，2026 列表将使用辅助年参数。")
    print("细分: %s  联赛参数: %s" % (seg_path, league_path))
    print("=" * 60)
    return 0


def cmd_repredict_all(args: argparse.Namespace) -> int:
    """主库 + 竞彩一起重算：先 repredict-history（主库 predictions），再按年跑 predict-jc-from-self-improvement（jc_predictions）。
    调参/改配置后一次命令即可同时更新主库与竞彩。--db-only 时只更新主库，不跑竞彩。"""
    rc = cmd_repredict_history(args)
    if rc != 0:
        return rc
    if getattr(args, "db_only", False):
        print("（--db-only：仅更新主库，未更新 jc_predictions）")
        return 0
    years_str = getattr(args, "years", "2025,2026") or "2025,2026"
    try:
        years = [int(y.strip()) for y in years_str.split(",") if y.strip()]
    except ValueError:
        print("--years 格式应为逗号分隔年份，如 2025,2026", file=sys.stderr)
        return 1
    if not years:
        years = [2025, 2026]
    print("\n--- 重算竞彩预测 jc_predictions（按年）---")
    for yr in years:
        jc_args = argparse.Namespace(
            date=None, year=yr, dates=None, include_finished=True,
            no_merge=False, last_n=10, verbose=getattr(args, "verbose", False),
        )
        rc_jc = cmd_predict_jc_from_self_improvement(jc_args)
        if rc_jc != 0:
            print(f"predict-jc-from-self-improvement --year {yr} 未完全成功。", file=sys.stderr)
    print("\nrepredict-all 完成：主库 predictions 已重算，竞彩 jc_predictions 已按年重算。可运行 predict-eval / jc-predictions-eval 查看指标。")
    return 0


def cmd_refresh_models(args: argparse.Namespace) -> int:
    """依次执行 calibrate-probs、train-outcome-ml。样本不足时各自会报错；样本够时更新校准与 ML 模型，之后可运行 refresh-and-eval 看新指标。"""
    calib_ns = argparse.Namespace(no_db=False, no_log=False)
    rc = cmd_calibrate_probs(calib_ns)
    if rc != 0:
        print("（calibrate-probs 未更新，可多积累已填比分后再跑）", file=sys.stderr)
    print()
    rc2 = cmd_train_outcome_ml(argparse.Namespace())
    if rc2 != 0:
        print("（train-outcome-ml 未更新，可多积累 predictions+赛果后再跑）", file=sys.stderr)
    if rc == 0 or rc2 == 0:
        print("建议随后运行: python3 main.py refresh-and-eval 查看新参数下的评估。")
    return 0


def cmd_data_stats(args: argparse.Namespace) -> int:
    """查看数据库数据量（比赛、预测、竞彩预测等），便于判断是否可重训 ML/校准。"""
    from db import repo
    repo.init_db()
    if repo.use_mongo():
        db = repo._db()
        db_name = getattr(config, "MONGO_DB_NAME", "football")
        self_improvement_name = getattr(config, "MONGO_SELF_IMPROVEMENT_DB_NAME", "self_improvement_football")
        # 主库即竞彩库时：比赛在 jingcai_match_analysis，预测在 jc_predictions
        if db_name == self_improvement_name:
            jc_ana = db["jingcai_match_analysis"].count_documents({})
            jc_ana_done = db["jingcai_match_analysis"].count_documents(
                {"home_score": {"$ne": None}, "away_score": {"$ne": None}}
            )
            jc_pred = db["jc_predictions"].count_documents({})
            print("当前库（MongoDB，竞彩主库）：")
            print(f"  jingcai_match_analysis: {jc_ana}，已完赛: {jc_ana_done}")
            print(f"  jc_predictions: {jc_pred}")
        else:
            coll = getattr(config, "MONGO_MATCHES_COLLECTION", "matches")
            matches_count = db[coll].count_documents({})
            finished = db[coll].count_documents(
                {"home_score": {"$ne": None}, "away_score": {"$ne": None}}
            )
            pred_count = db["predictions"].count_documents({})
            print("主库（MongoDB）：")
            print(f"  比赛总数: {matches_count}，已完赛: {finished}")
            print(f"  预测记录数: {pred_count}")
            try:
                from db.self_improvement import get_self_improvement_db
                si = get_self_improvement_db()
                if si and si.name != db_name:
                    jc_pred = si["jc_predictions"].count_documents({})
                    jc_ana = si["jingcai_match_analysis"].count_documents({})
                    jc_ana_done = si["jingcai_match_analysis"].count_documents(
                        {"home_score": {"$ne": None}, "away_score": {"$ne": None}}
                    )
                    print("竞彩库（self_improvement_football）：")
                    print(f"  jc_predictions: {jc_pred}")
                    print(f"  jingcai_match_analysis: {jc_ana}，已完赛: {jc_ana_done}")
            except Exception:
                pass
    else:
        engine = repo.get_engine()
        with engine.connect() as conn:
            m = conn.execute(text("SELECT COUNT(*) FROM matches")).scalar()
            f = conn.execute(text(
                "SELECT COUNT(*) FROM matches WHERE home_score IS NOT NULL AND away_score IS NOT NULL"
            )).scalar()
            p = conn.execute(text("SELECT COUNT(*) FROM predictions")).scalar()
        print("主库（SQLite）：")
        print(f"  比赛总数: {m}，已完赛: {f}")
        print(f"  预测记录数: {p}")
    print("数据量足够时（如预测+完赛 > 30）可运行: python3 main.py refresh-models && python3 main.py refresh-and-eval")
    return 0


def cmd_suggest_improvements(args: argparse.Namespace) -> int:
    """输出「后续可继续提升」的可行方向：数据积累建议、API 预测融合、联赛级调参建议（基于当前 eval 与 recap 按联赛统计）。"""
    from sqlalchemy import text
    from etl.schema import init_db
    from engine.predict import evaluate_predictions, generate_recap_from_db
    from engine.prediction_log import load_log, generate_recap

    engine = get_engine()
    init_db(engine)
    suggestions = []

    # 1) 数据积累
    n_db = 0
    if config.DB_PATH.exists():
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT COUNT(*) FROM predictions p
                JOIN matches m ON m.id = p.match_id
                WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            """)).fetchone()
            n_db = row[0] if row else 0
    entries = load_log()
    n_log = sum(1 for e in entries if e.get("actual_home_score") is not None and e.get("actual_away_score") is not None)

    suggestions.append("【数据积累】")
    suggestions.append(f"  当前 DB 已完赛预测 {n_db} 场，prediction_log 已填比分 {n_log} 条。")
    if n_db >= 30 or n_log >= 20:
        suggestions.append("  样本增加后建议运行: python main.py refresh-models && python main.py refresh-and-eval")
        suggestions.append("  （会更新校准与 ML 模型并用新参数重算评估，常可带来 1～2 个点提升）。")
    else:
        suggestions.append("  建议多跑 auto-accumulate-log 或 prediction-log add + set-result 积累数据，待 DB≥30 且/或 log≥20 后再跑 refresh-models。")

    # 2) API 预测融合
    suggestions.append("")
    suggestions.append("【API 预测融合】")
    if getattr(config, "PREDICT_USE_API_PREDICTIONS", False):
        suggestions.append("  已开启 API 预测融合。权重 PREDICT_API_PREDICTIONS_WEIGHT 可在 .env 中微调（如 0.2～0.3）。")
    else:
        suggestions.append("  新比赛可融合 API-Football 的 predictions 接口以提升命中率。")
        suggestions.append("  在 .env 中设置: PREDICT_USE_API_PREDICTIONS=true, PREDICT_API_PREDICTIONS_WEIGHT=0.2")
        suggestions.append("  验证接口可用: python main.py test-api-predictions --date 2026-02-15 [--home 主队 --away 客队]")

    # 3) 联赛级调参（基于 DB 与 log 的按联赛统计；--year 时仅用该年 DB 统计）
    suggestions.append("")
    year = getattr(args, "year", None)
    if year:
        suggestions.append(f"【联赛级调参建议】（基于 {year} 年 DB 数据）")
    else:
        suggestions.append("【联赛级调参建议】")
    overall_db = overall_log = None
    by_comp_db = {}
    by_comp_log = {}
    if n_db > 0:
        from engine.predict import generate_recap_from_db
        report = evaluate_predictions(engine, by_competition=True, year=year)
        overall_db = report.get("outcome_accuracy")
        by_comp_db = report.get("by_competition") or {}
    if year and n_db > 0:
        recap = generate_recap_from_db(engine, by_competition=True, limit=0, year=year)
        overall_log = overall_db  # 同年用同一整体
        by_comp_log = recap.get("by_competition") or {}
    elif n_log > 0:
        recap = generate_recap(by_competition=True)
        overall_log = recap.get("outcome_accuracy")
        by_comp_log = recap.get("by_competition") or {}

    # 合并联赛集合，用 DB 或 log 的准确率与整体对比
    all_comp = set(by_comp_db.keys()) | set(by_comp_log.keys())
    weak = []
    for comp in sorted(all_comp):
        if comp in ("_", ""):
            continue
        g_db = by_comp_db.get(comp, {})
        g_log = by_comp_log.get(comp, {})
        n = g_db.get("n", 0) or g_log.get("n", 0)
        acc = g_db.get("outcome_accuracy") or g_log.get("outcome_accuracy")
        overall = overall_db if g_db.get("n") else overall_log
        if n is not None and acc is not None and overall is not None and n >= 5 and acc < overall - 0.05:
            weak.append((comp, n, acc, overall))
    if weak:
        suggestions.append("  以下联赛当前准确率低于整体，可在 data/predict_league_params.json 中为该联赛增加或调整参数：")
        for comp, n, acc, ov in weak:
            suggestions.append(f"    · {comp}: n={n}, 准确率={acc*100:.1f}% (整体约 {ov*100:.1f}%)")
        suggestions.append("  可尝试: PREDICT_DRAW_BOOST_COEF（如 0.06～0.10）、last_n（如 6～8）、PREDICT_HOME_ADVANTAGE（如 1.03～1.05）。")
        suggestions.append("  修改后运行: python main.py repredict-history && python main.py predict-eval --by-competition" + (f" --year {year}" if year else "") + "")
    else:
        suggestions.append("  按联赛拆解后暂无「明显低于整体」的联赛（或样本不足）。可随时运行:")
        suggestions.append("  python main.py predict-eval --by-competition" + (f" --year {year}" if year else "") + " && python main.py prediction-log recap --from-db --by-competition" + (f" --year {year}" if year else "") + "")
        suggestions.append("  查看各联赛准确率与 MAE，再在 data/predict_league_params.json 中针对性微调。")

    for line in suggestions:
        print(line)
    return 0


def cmd_test_api_predictions(args: argparse.Namespace) -> int:
    """测试 API-Football 的 predictions 接口是否可用（需配置 API key）。"""
    from engine.prediction_log import load_log

    date_str = (getattr(args, "date", None) or "").strip()[:10]
    home = (getattr(args, "home", None) or "").strip()
    away = (getattr(args, "away", None) or "").strip()
    if not date_str:
        print("请指定 --date YYYY-MM-DD", file=sys.stderr)
        return 1
    if not home or not away:
        entries = [e for e in load_log() if (e.get("date") or "")[:10] == date_str and e.get("actual_home_score") is not None]
        if not entries:
            print("未找到该日已填比分的预测；请指定 --home 与 --away", file=sys.stderr)
            return 1
        e = entries[0]
        home = (e.get("home_team") or "").strip()
        away = (e.get("away_team") or "").strip()
        print(f"使用 prediction_log 中该日首条: {home} vs {away}")
    try:
        from etl.api_football_predictions import get_api_predictions_probs
        probs = get_api_predictions_probs(date_str, home, away)
        if probs is None:
            print("API 未返回预测（可能无该场 fixture 或 key 未配置/超限）")
            return 0
        print(f"API 预测概率: 主胜={probs[0]:.3f} 平={probs[1]:.3f} 客胜={probs[2]:.3f}")
        return 0
    except Exception as exc:
        print(f"请求失败: {exc}", file=sys.stderr)
        return 1


def cmd_calibrate_probs(args: argparse.Namespace) -> int:
    """从 DB 与 prediction_log 的历史预测+结果拟合概率校准参数，写入 data/calibration_params.json。--by-competition 时按联赛分组拟合并写入联赛专属参数。"""
    from db import repo
    from engine.calibration import fit_and_save
    use_db = not getattr(args, "no_db", False)
    use_log = not getattr(args, "no_log", False)
    by_competition = getattr(args, "by_competition", False)
    train_date_from = getattr(args, "train_date_from", None)
    train_date_to = getattr(args, "train_date_to", None)
    if repo.use_mongo():
        engine = None
    else:
        from etl.schema import init_db, get_engine
        engine = get_engine()
        init_db(engine)
    result = fit_and_save(
        engine=engine if (use_db and not repo.use_mongo()) else None,
        use_log=use_log,
        use_db=use_db,
        by_competition=by_competition,
        train_date_from=train_date_from,
        train_date_to=train_date_to,
    )
    if result.get("error"):
        print(result["error"], f"（当前样本 {result.get('n_samples', 0)} 条）", file=sys.stderr)
        return 1
    print(f"已用 {result['n_samples']} 条样本拟合校准参数，写入 {result['path']}")
    print("启用校准：设置环境变量 PREDICT_USE_CALIBRATION=true 后，新预测将自动应用。")
    return 0


def cmd_train_outcome_ml(args: argparse.Namespace) -> int:
    """从 DB 预测+赛果训练胜平负 ML 修正层。--competition-id 时仅用该联赛样本并保存为联赛专属模型 outcome_ml_model_{id}.joblib。"""
    from db import repo
    from engine.outcome_ml import train_and_save
    engine = repo.get_engine() if repo.use_mongo() else get_engine()
    if not repo.use_mongo():
        from etl.schema import init_db
        init_db(engine)
        if not config.DB_PATH.exists():
            print("尚无数据，请先执行: python main.py ingest-fd", file=sys.stderr)
            return 1
    competition_id = getattr(args, "competition_id", None)
    train_date_from = getattr(args, "train_date_from", None)
    train_date_to = getattr(args, "train_date_to", None)
    result = train_and_save(
        engine,
        competition_id=competition_id,
        train_date_from=train_date_from,
        train_date_to=train_date_to,
    )
    if result.get("error"):
        print(result["error"], file=sys.stderr)
        return 1
    n_main = result.get("n_main", 0)
    n_jc = result.get("n_jc", 0)
    if n_main or n_jc:
        print(f"已用 {result['n_samples']} 条样本训练 ML 修正层（主库 {n_main} + 竞彩库 {n_jc}），写入 {result['path']}，训练集准确率 {result.get('accuracy', 0):.2%}")
    else:
        print(f"已用 {result['n_samples']} 条样本训练 ML 修正层，写入 {result['path']}，训练集准确率 {result.get('accuracy', 0):.2%}")
    print("启用：设置 PREDICT_USE_OUTCOME_ML=true、PREDICT_OUTCOME_ML_WEIGHT=0.3 后，新预测将自动应用 ML 修正。")
    return 0


def cmd_fit_bqc_alpha(args: argparse.Namespace) -> int:
    """从 jc_predictions + jingcai_match_analysis 拟合半全场泊松半场权重 α，写入 data/bqc_alpha_by_competition.json。"""
    from engine.bqc_alpha_fit import fit_and_save_bqc_alpha

    r = fit_and_save_bqc_alpha(
        min_samples_per_competition=int(getattr(args, "min_samples_per_competition", 12)),
        min_global_samples=int(getattr(args, "min_global_samples", 40)),
    )
    if r.get("error"):
        print(r["error"], file=sys.stderr)
        return 1
    print("已写入 %s" % r.get("path"))
    print("  全局 default α=%s（样本数 %s）" % (r.get("default"), r.get("n_samples_global")))
    print("  按 competition_id 条目数：%s" % r.get("n_competitions"))
    return 0


def cmd_enrich_fd(args: argparse.Namespace) -> int:
    """为 fd 场次请求 GET /matches/{id}，将 statistics 与半场比分写入 raw_meta。"""
    from etl.football_data_ingest import enrich_fd_match_stats
    n = enrich_fd_match_stats(
        match_id=getattr(args, "match_id", None),
        limit=getattr(args, "limit", 20),
    )
    print(f"已补全 {n} 场。")
    return 0


def cmd_prediction_log_add(args: argparse.Namespace) -> int:
    """预测并追加到 prediction_log 表（DB），供每日统计与复盘。"""
    from engine.prediction_log import append_prediction
    engine = get_engine()
    if not config.DB_PATH.exists():
        print("尚无数据，请先执行: python main.py ingest-fd", file=sys.stderr)
        return 1
    result = append_prediction(
        engine,
        home_team=args.home,
        away_team=args.away,
        competition=getattr(args, "competition", None),
        last_n=getattr(args, "last_n", 10),
    )
    if result is None:
        print(f"未找到主队或客队，请检查名称。", file=sys.stderr)
        return 1
    print(f"已记录预测：{args.home} vs {args.away} → 期望 {result['predicted_score'][0]}-{result['predicted_score'][1]}，"
          f"主胜 {result['prob_home_win']*100:.0f}% / 平 {result['prob_draw']*100:.0f}% / 客胜 {result['prob_away_win']*100:.0f}%")
    return 0


def cmd_prediction_log_set_result(args: argparse.Namespace) -> int:
    """按日志序号填写实际比分。"""
    from engine.prediction_log import load_log, set_actual
    index = int(args.index)
    home_score, away_score = int(args.home_score), int(args.away_score)
    if not set_actual(index, home_score, away_score):
        print(f"无效序号或超出范围（当前共 {len(load_log())} 条）。", file=sys.stderr)
        return 1
    print(f"已填写第 {index} 场实际比分：{home_score} - {away_score}")
    return 0


def cmd_prediction_log_recap(args: argparse.Namespace) -> int:
    """复盘：对已填实际比分的预测做统计并输出不一致分析。--from-db 时与 predict-eval 同源（DB 全量）。"""
    from etl.schema import get_engine, init_db
    from engine.predict import generate_recap_from_db
    from engine.prediction_log import load_log, generate_recap

    only_date = getattr(args, "date", None)
    by_competition = getattr(args, "by_competition", False)
    by_year = getattr(args, "by_year", False)
    from_db = getattr(args, "from_db", False)
    recap_limit = getattr(args, "recap_limit", 200)

    year = getattr(args, "year", None)
    if from_db:
        engine = get_engine()
        init_db(engine)
        # recap_limit=0 表示展示全部详情，>0 仅展示前 N 条
        limit = None if recap_limit == 0 else (recap_limit or 200)
        report = generate_recap_from_db(engine, by_competition=by_competition, limit=limit, year=year)
    else:
        report = generate_recap(only_date=only_date, by_competition=by_competition, by_year=by_year)

    if report["n"] == 0:
        if from_db:
            print("DB 中暂无已完赛的预测记录。请先 backfill-predictions 或 repredict-history。")
        else:
            print("暂无已填写实际比分的预测。请先用 prediction-log set-result <序号> <主队进球> <客队进球> 填写。")
        return 0
    print("=" * 60)
    print("预测 vs 实际 复盘" + (f"（DB{' ' + str(year) + '年' if year else ' 全量'}，与 predict-eval 同源）" if from_db else ""))
    if only_date and not from_db:
        print(f"统计日期：{only_date}")
    print(f"场次：{report['n']} | 期望进球 MAE：主 {report['mae_home']} / 客 {report['mae_away']} / 整体 {report['mae_overall']}")
    print(f"胜平负猜对：{report['outcome_correct']}/{report['n']}，准确率 {report['outcome_accuracy']*100:.1f}%")
    if report.get("exact_score_accuracy") is not None:
        print(f"精确比分命中：{report.get('exact_score_correct', 0)}/{report['n']}，准确率 {report['exact_score_accuracy']*100:.1f}%")
    if by_competition and report.get("by_competition"):
        print("  按赛事拆解：")
        for comp_name in sorted(report["by_competition"].keys()):
            g = report["by_competition"][comp_name]
            print(f"    {comp_name}: n={g['n']}, 准确率={g['outcome_accuracy']*100:.1f}%, MAE={g['mae_overall']}")
    if (not from_db) and by_year and report.get("by_year"):
        print("  按年份拆解：")
        for y in sorted(report["by_year"].keys()):
            g = report["by_year"][y]
            print(f"    {y}: n={g['n']}, 准确率={g['outcome_accuracy']*100:.1f}%, MAE={g['mae_overall']}")
    if not from_db:
        injury_n = report.get("injury_n") or 0
        if injury_n > 0:
            orig = report.get("injury_original_correct") or 0
            adj = report.get("injury_adjusted_correct") or 0
            print(f"伤停：有伤停数据的场次 {injury_n} 场 — 原始预测命中 {orig}，伤停调整后命中 {adj}（{'有提升' if adj > orig else '无提升' if adj == orig else '调整后变差'}）")
    if from_db and report.get("details_total", report["n"]) > report.get("details_shown", len(report["details"])):
        print(f"  详情仅展示前 {report['details_shown']} 场，共 {report['details_total']} 场（可用 --recap-limit 0 展示全部）")
    print("=" * 60)
    for d in report["details"]:
        ok = "✓" if d["outcome_correct"] else "✗"
        print(f"  {ok} {d['home_team']} vs {d['away_team']}")
        print(f"      预测 {d['predicted']} | 实际 {d['actual']}")
        if not from_db and d.get("had_injury"):
            adj_ok = "✓" if d.get("injury_adjusted_correct") else "✗"
            print(f"      伤停调整后预测：{d.get('injury_adjusted_result')} {adj_ok}")
        if not d["outcome_correct"] and d.get("reason"):
            print(f"      不一致原因：{d['reason']}")
    return 0


def cmd_prediction_log_list(args: argparse.Namespace) -> int:
    """列出预测日志（含未填比分的），可选 --date 筛选。序号为全局索引，填实际比分时用此序号。"""
    from engine.prediction_log import load_log
    entries = load_log()
    only_date = getattr(args, "date", None)
    if only_date:
        entries = [(i, e) for i, e in enumerate(entries) if e.get("date") == only_date]
    else:
        entries = list(enumerate(entries))
    if not entries:
        print("暂无预测记录。使用 prediction-log add \"主队\" \"客队\" 记录预测。")
        return 0
    print("序号 | 日期       | 主队 vs 客队 | 预测期望 | 预测结果 | 实际比分")
    print("-" * 70)
    for i, e in entries:
        actual = f"{e.get('actual_home_score')}-{e.get('actual_away_score')}" if e.get("actual_home_score") is not None else "待填"
        pred_res = e.get("predicted_result", "")
        print(f"{i:4} | {e.get('date','')} | {e.get('home_team','')} vs {e.get('away_team','')} | "
              f"{e.get('exp_goals_home')}-{e.get('exp_goals_away')} | {pred_res:4} | {actual}")
    return 0


def cmd_prediction_log_compare_db(args: argparse.Namespace) -> int:
    """对比复盘数据（prediction_log 已填比分）与 DB（已完赛且有预测）是否一致。USE_MONGO 时读 MongoDB，否则读 SQLite。"""
    from db import repo
    from engine.prediction_log import compare_recap_with_db

    if repo.use_mongo():
        engine = None
    else:
        if not config.DB_PATH.exists():
            print("DB 不存在，无法对比。请先执行 ingest-fd 或 add-match 等写入 DB。", file=sys.stderr)
            return 1
        from etl.schema import get_engine, init_db
        init_db(get_engine())
        engine = get_engine()
    result = compare_recap_with_db(engine)
    db_total = result["db_total"]
    log_total = result["log_total"]
    only_db = result["only_in_db_count"]
    only_log = result["only_in_log_count"]
    consistent = result["in_both_consistent"]
    mismatch_count = result["mismatch_count"]

    print("=" * 60)
    print("复盘数据 vs DB 数据 对比")
    print("=" * 60)
    print(f"  DB 已完赛且有预测：{db_total} 场")
    print(f"  prediction_log 已填比分：{log_total} 条")
    print(f"  两边都有且数据一致：{consistent} 场")
    print(f"  两边都有但比分/预测不一致：{mismatch_count} 场")
    print(f"  仅在 DB（log 无）：{only_db} 场")
    print(f"  仅在 log（DB 无）：{only_log} 条")
    if consistent == db_total and consistent == log_total and mismatch_count == 0 and only_db == 0 and only_log == 0:
        print("\n结论：复盘数据与 DB 数据 完全一致。")
    elif mismatch_count > 0 or only_db > 0 or only_log > 0:
        print("\n结论：复盘数据与 DB 数据 不一致（存在仅在一侧或两侧数据不一致的场次）。")
        if result["mismatch"]:
            print("\n不一致场次（比分或期望进球不同）：")
            for m in result["mismatch"][:20]:
                print(f"  {m['date']} {m['home_team']} vs {m['away_team']}: DB {m['db_score']} ({m['db_exp']}) vs log {m['log_score']} ({m['log_exp']})")
            if len(result["mismatch"]) > 20:
                print(f"  ... 共 {len(result['mismatch'])} 场")
        if only_db and result["only_in_db"]:
            print("\n仅在 DB 的场次（前 10）：")
            for r in result["only_in_db"][:10]:
                print(f"  {r['date']} {r['home_team']} vs {r['away_team']} {r['home_score']}-{r['away_score']}")
        if only_log and result["only_in_log"]:
            print("\n仅在 prediction_log 的场次（前 10）：")
            for r in result["only_in_log"][:10]:
                print(f"  {r['date']} {r['home_team']} vs {r['away_team']} {r['home_score']}-{r['away_score']}")
    else:
        print("\n结论：两边场次数量一致且无冲突，数据一致。")
    print("=" * 60)
    return 0


def cmd_prediction_log_sync_from_db(args: argparse.Namespace) -> int:
    """从 DB 的 predictions + matches（已完赛）同步到 prediction_log 表，使「已填比分」与 DB 一致。"""
    from db import repo
    from engine.prediction_log import sync_prediction_log_from_db, load_log

    if not getattr(config, "PREDICTION_LOG_USE_DB", True):
        print("请设置 PREDICTION_LOG_USE_DB=true（.env）以便 prediction_log 存 DB。", file=sys.stderr)
        return 1
    engine = None
    if not repo.use_mongo():
        if not config.DB_PATH.exists():
            print("尚无 DB，请先执行: python main.py ingest-fd", file=sys.stderr)
            return 1
        from etl.schema import get_engine, init_db
        engine = get_engine()
        init_db(engine)
    inserted, updated = sync_prediction_log_from_db(engine)
    entries = load_log(engine)
    with_actual = sum(1 for e in entries if e.get("actual_home_score") is not None and e.get("actual_away_score") is not None)
    print(f"sync-from-db 完成：新增 {inserted} 条，更新 {updated} 条。")
    print(f"prediction_log 当前共 {len(entries)} 条，其中已填比分 {with_actual} 条（与 DB 已完赛预测一致）。")
    return 0


def cmd_prediction_log_dedup(args: argparse.Namespace) -> int:
    """仅 Mongo：检查或去除 prediction_log 中同一场（日期+主客队）的重复条。"""
    from db import repo
    if not repo.use_mongo():
        print("dedup 仅支持 MongoDB。当前未启用 USE_MONGO。", file=sys.stderr)
        return 1
    dry_run = getattr(args, "dry_run", False)
    info = repo.prediction_log_count_duplicates_mongo()
    print(f"prediction_log 文档总数: {info['total_docs']}")
    print(f"按 (日期, 主队, 客队) 去重后唯一场次: {info['unique_keys']}")
    print(f"存在重复的场次数: {info['duplicate_key_count']}（多出的文档数: {info['total_docs'] - info['unique_keys']}）")
    if info["duplicate_keys_sample"]:
        print("重复场次示例（最多 10 个）:")
        for (d, h, a) in info["duplicate_keys_sample"]:
            print(f"  {d}  {h} vs {a}")
    if dry_run:
        if info["duplicate_key_count"] > 0:
            print("\n若要去重请去掉 --dry-run 再执行: python main.py prediction-log dedup")
        return 0
    deleted, keys_deduped = repo.prediction_log_dedup_mongo(dry_run=False)
    print(f"\n已删除重复文档 {deleted} 条，涉及 {keys_deduped} 个场次。")
    return 0


def cmd_prediction_log_migrate_from_json(args: argparse.Namespace) -> int:
    """将 data/prediction_log.json 一次性导入到 prediction_log 表（仅在有旧 JSON 时执行一次）。"""
    from etl.schema import get_engine, init_db
    from engine.prediction_log import migrate_json_to_db, load_log

    if not config.DB_PATH.exists():
        print("尚无 DB，请先执行: python main.py ingest-fd", file=sys.stderr)
        return 1
    engine = get_engine()
    init_db(engine)
    n = migrate_json_to_db(engine)
    entries = load_log(engine)
    print(f"从 prediction_log.json 导入 {n} 条到 prediction_log 表。当前共 {len(entries)} 条。")
    return 0


def cmd_migrate_data(args: argparse.Namespace) -> int:
    """将 data 目录下有用数据一次性迁移到 DB：prediction_log、赔率、积分榜、伤停。"""
    if not config.DB_PATH.exists():
        print("尚无 DB，请先执行: python main.py ingest-fd", file=sys.stderr)
        return 1
    from etl.schema import get_engine, init_db
    engine = get_engine()
    init_db(engine)

    # 1. prediction_log
    from engine.prediction_log import migrate_json_to_db, load_log
    n_log = migrate_json_to_db(engine)
    print(f"[1/4] prediction_log: 导入 {n_log} 条")

    # 2. 赔率
    from etl.api_football_odds import migrate_odds_json_to_db
    n_odds = migrate_odds_json_to_db()
    print(f"[2/4] match_odds: 导入 {n_odds} 条")

    # 3. 积分榜
    from etl.api_football_standings import migrate_standings_json_to_db
    n_standings = migrate_standings_json_to_db()
    print(f"[3/4] league_standings: 导入 {n_standings} 条")

    # 4. 伤停（文件可能很大，分批）
    from etl.injury_db import migrate_injury_json_to_db
    n_injury = migrate_injury_json_to_db()
    print(f"[4/4] match_injury: 导入 {n_injury} 条")

    print("迁移完成。后续预测与复盘将优先从 DB 读取。")
    return 0


def cmd_mongo_clear(args: argparse.Namespace) -> int:
    """清空当前配置的 MongoDB 库中所有项目集合，便于重新拉取、重新定义。"""
    if not (getattr(config, "MONGO_URI", "") or "").strip():
        print("未配置 MONGO_URI。请在 .env 中设置 MONGO_URI（如 mongodb://localhost:27020）与可选 MONGO_DB_NAME。", file=sys.stderr)
        return 1
    try:
        from db import repo
        if not repo.use_mongo():
            print("当前未使用 MongoDB（USE_MONGO 未开启）。mongo-clear 仅用于清空 MongoDB。", file=sys.stderr)
            return 1
        from db.mongo import get_mongo_db, COLLECTIONS
        db = get_mongo_db()
        if db is None:
            return 1
        db_name = db.name
        for coll_name in COLLECTIONS:
            coll = db[coll_name]
            n = coll.count_documents({})
            coll.drop()
            print(f"  已清空 {coll_name}（原 {n} 条）")
        print(f"MongoDB 数据库「{db_name}」已清空，可重新拉取数据。")
    except Exception as e:
        print(f"清空失败: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_sync_sqlite_to_mongo(args: argparse.Namespace) -> int:
    """将 SQLite 表数据全量同步到 MongoDB，便于在 MongoDB Compass 中查看具体数据。"""
    if not (getattr(config, "MONGO_URI", "") or "").strip():
        print("未配置 MONGO_URI。请在 .env 中设置 MONGO_URI（如 mongodb://localhost:27020）与可选 MONGO_DB_NAME。", file=sys.stderr)
        return 1
    try:
        from db.mongo import sync_sqlite_to_mongo as do_sync
        counts = do_sync()
    except Exception as e:
        print(f"同步失败: {e}", file=sys.stderr)
        return 1
    print("MongoDB 同步完成（数据库名: %s）：" % getattr(config, "MONGO_DB_NAME", "football"))
    for coll, n in sorted(counts.items()):
        print(f"  {coll}: {n} 条")
    return 0


def cmd_migrate_football_to_self_improvement(args: argparse.Namespace) -> int:
    """将 football 库数据迁移到 self_improvement_football，迁移后可删除 football 并仅使用 self_improvement_football。"""
    if not (getattr(config, "MONGO_URI", "") or "").strip():
        print("未配置 MONGO_URI。请在 .env 中设置 MONGO_URI。", file=sys.stderr)
        return 1
    from db.migrate_football_to_self_improvement import migrate_football_to_self_improvement, SOURCE_DB_NAME, TARGET_DB_NAME

    dry_run = getattr(args, "dry_run", False)
    drop_after = getattr(args, "drop_source_db", False)
    yes = getattr(args, "yes", False)

    if drop_after and not dry_run and not yes:
        print("警告：--drop-source-db 将删除 football 库，且不可恢复。", file=sys.stderr)
        print("若已确认迁移结果无误，请加上 --yes 再执行。", file=sys.stderr)
        return 1

    out = migrate_football_to_self_improvement(
        drop_source_after=drop_after and not dry_run,
        dry_run=dry_run,
    )
    if not out.get("ok"):
        print(out.get("error", "迁移失败"), file=sys.stderr)
        return 1
    results = out.get("results") or {}
    if dry_run:
        print(f"【dry-run】将把 {SOURCE_DB_NAME} 中以下集合迁移到 {TARGET_DB_NAME}：")
        for coll, stats in sorted(results.items()):
            print(f"  {coll}: 源 {stats.get('source_count', 0)} 条")
        return 0
    for coll, stats in sorted(results.items()):
        print(f"  {coll}: 插入 {stats.get('inserted', 0)} 条, 跳过(已存在) {stats.get('skipped', 0)} 条, 源 {stats.get('source_count', 0)} 条")
    if out.get("dropped_source"):
        print(f"已删除源库「{SOURCE_DB_NAME}」。后续请将 .env 中 MONGO_DB_NAME 设为 {TARGET_DB_NAME}，并仅使用 self_improvement_football。")
    else:
        print(f"迁移完成。请将 .env 中 MONGO_DB_NAME={TARGET_DB_NAME} 后仅使用 self_improvement_football；确认无误后可手动删除 football 或再次执行本命令并加 --drop-source-db --yes。")
    return 0


def cmd_backfill_match_names_zh(args: argparse.Namespace) -> int:
    """仅 Mongo：用 teams 的 name/name_zh 回填 matches 文档的主客队英文/中文名（空缺的才补）。"""
    from db import repo
    if not repo.use_mongo():
        print("当前未使用 MongoDB。backfill-match-names-zh 仅支持 MongoDB。", file=sys.stderr)
        return 1
    only_if_empty = not getattr(args, "overwrite", False)
    updated, skipped = repo.backfill_matches_team_name_zh_mongo(only_if_empty=only_if_empty)
    print(f"已用 teams 表回填 matches 主客队名：更新 {updated} 条文档，跳过 {skipped} 条。")
    print("若 teams.name_zh 仍有很多为空，请先维护 data/team_name_zh_mapping.json 后执行: python scripts/update_team_name_zh.py")
    return 0


def cmd_normalize_team_names(args: argparse.Namespace) -> int:
    """统一队名：将 match_odds、match_injury 中的主客队名规范化为 teams 表队名。"""
    from etl.team_name_normalize import normalize_all
    dry_run = getattr(args, "dry_run", False)
    n_odds, n_injury, message = normalize_all(dry_run=dry_run)
    print(message)
    return 0


def cmd_link_match_odds(args: argparse.Namespace) -> int:
    """将 match_odds 与 matches 对应：先规范化队名，再回填 match_id。仅 MongoDB。"""
    from db import repo
    if not repo.use_mongo():
        print("当前未使用 MongoDB。link-match-odds 仅支持 MongoDB。", file=sys.stderr)
        return 1
    from etl.team_name_normalize import normalize_match_odds
    dry_run = getattr(args, "dry_run", False)
    print("步骤 1/2: 规范化 match_odds 队名（与 teams 一致）…")
    n_norm, msg_norm = normalize_match_odds(dry_run=dry_run)
    print(msg_norm)
    if not dry_run:
        print("步骤 2/2: 按 matches 回填 match_odds.match_id…")
        linked, total = repo.backfill_match_odds_match_id()
        print(f"matches 五大联赛共 {total} 场，其中 {linked} 场已在 match_odds 中关联 match_id。")
    else:
        print("步骤 2/2: 跳过（dry-run 不执行回填）。")
    return 0


def cmd_list_matches_without_odds(args: argparse.Namespace) -> int:
    """仅 Mongo：列出五大联赛中没有赔率信息（match_odds 无 match_id）的比赛。"""
    from db import repo
    if not repo.use_mongo():
        print("当前未使用 MongoDB。list-matches-without-odds 仅支持 MongoDB。", file=sys.stderr)
        return 1
    without, total, with_odds = repo.matches_without_odds()
    print(f"五大联赛总场次: {total}，有赔率: {with_odds}，无赔率: {len(without)}")
    if not without:
        return 0
    by_date: dict[str, list[dict]] = {}
    for m in without:
        d = (m.get("match_date") or "")[:10]
        by_date.setdefault(d, []).append(m)
    for d in sorted(by_date.keys()):
        rows = by_date[d]
        print(f"\n  {d} ({len(rows)} 场):")
        for r in rows[:20]:
            print(f"    {r.get('league_cn', '')} {r.get('home_name', '')} vs {r.get('away_name', '')} (match_id={r.get('match_id')})")
        if len(rows) > 20:
            print(f"    ... 共 {len(rows)} 场")
    out_path = getattr(args, "output", None)
    if out_path:
        import json
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(without, f, ensure_ascii=False, indent=2)
        print(f"\n已写入 {len(without)} 条到 {out_path}")
    return 0


def cmd_odds_stats(args: argparse.Namespace) -> int:
    """仅 Mongo：打印 match_odds 统计（总条数、已关联 match_id、去重比赛数），用于判断是否存在重复。"""
    from db import repo
    if not repo.use_mongo():
        print("当前未使用 MongoDB。odds-stats 仅支持 MongoDB。", file=sys.stderr)
        return 1
    st = repo.match_odds_stats()
    if not st:
        return 1
    without, total_matches, with_odds = repo.matches_without_odds()
    print("match_odds 集合统计：")
    print(f"  总文档数: {st['total_docs']}")
    print(f"  已设置 match_id 的文档数: {st['docs_with_match_id']}")
    print(f"  未设置 match_id 的文档数: {st['docs_without_match_id']}")
    print(f"  去重后的比赛数（distinct match_id）: {st['distinct_match_ids']}")
    print()
    print("五大联赛 matches：")
    print(f"  总场次: {total_matches}，有赔率: {with_odds}，无赔率: {len(without)}")
    if st["total_docs"] > st["distinct_match_ids"] and st["distinct_match_ids"] > 0:
        print()
        print(f"说明: 总文档数({st['total_docs']}) 大于去重比赛数({st['distinct_match_ids']})，说明存在重复：同一场比赛因队名拼写不同（如 API 返回 Chelsea / matches 里 Chelsea FC）被写成了多条，link-match-odds 只会给与 teams 表完全匹配的那一条填上 match_id，其余条未关联，仍占文档数。")
    if st["docs_without_match_id"] > 0:
        print()
        print(f"有 {st['docs_without_match_id']} 条文档未关联 match_id，多为队名与 teams 表不一致。可先运行 normalize-team-names 再 link-match-odds 减少未关联并让更多比赛有赔率。")
    return 0


def cmd_debug_odds_link(args: argparse.Namespace) -> int:
    """仅 Mongo：诊断为何 link-match-odds 为 0。打印 match_odds 与 matches 的样本，对比日期/队名/联赛。"""
    from db import repo
    if not repo.use_mongo():
        print("当前未使用 MongoDB。debug-odds-link 仅支持 MongoDB。", file=sys.stderr)
        return 1
    db = repo._db()
    # 先看 league_cn 是否一致
    odds_leagues = db["match_odds"].distinct("league_cn")
    print("match_odds 中的 league_cn 示例:", [x for x in odds_leagues if (x or "").strip()][:20])
    sample_odds, sample_matches = repo.match_odds_link_debug_samples(odds_limit=14, matches_per_league=6)
    print()
    print("--- match_odds 样本（未关联，用于匹配的字段）---")
    for i, o in enumerate(sample_odds[:14], 1):
        print(f"  [{i}] log_date={o.get('log_date')!r} league_cn={o.get('league_cn')!r} home_team={o.get('home_team')!r} away_team={o.get('away_team')!r}")
    print()
    print("--- matches 样本（五大联赛，用于匹配的字段）---")
    for i, m in enumerate(sample_matches[:14], 1):
        print(f"  [{i}] match_date={m.get('match_date')!r} league_cn={m.get('league_cn')!r} home_team={m.get('home_team')!r} away_team={m.get('away_team')!r}")
    print()
    print("说明: link 要求 log_date、league_cn、home_team、away_team 与 matches 侧完全一致。若队名不同（如 API 用 Chelsea，DB 用 Chelsea FC）则无法关联，需在 data/team_name_zh_mapping.json 或扩展 normalize 逻辑做映射。")
    return 0


def cmd_dedup_match_odds(args: argparse.Namespace) -> int:
    """仅 Mongo：按 (log_date, home_team, away_team, league_cn) 去重，或 --by-canonical 时按规范队名删「同场不同名」的重复。"""
    from db import repo
    if not repo.use_mongo():
        print("当前未使用 MongoDB。dedup-match-odds 仅支持 MongoDB。", file=sys.stderr)
        return 1
    dry_run = getattr(args, "dry_run", False)
    by_canonical = getattr(args, "by_canonical", False)
    if by_canonical:
        deleted, _ = repo.dedup_match_odds_by_canonical(dry_run=dry_run)
        if dry_run:
            print(f"dry-run（按规范队名）：将删除 {deleted} 条与「已关联 match_id」同场的重复文档。去掉 --dry-run 执行实际删除。")
        else:
            print(f"已按规范队名去重：删除 {deleted} 条重复文档（同场比赛、队名拼写不同）。")
        if deleted > 0 and not dry_run:
            print("建议随后执行: python main.py odds-stats 核对。")
        return 0
    deleted, groups = repo.dedup_match_odds(keep_with_match_id=True, dry_run=dry_run)
    if dry_run:
        print(f"dry-run（同键去重）：将删除 {deleted} 条重复文档，涉及 {groups} 个重复键组。去掉 --dry-run 执行实际去重。")
        return 0
    print(f"已去重：删除 {deleted} 条重复文档，涉及 {groups} 个重复键组。")
    if deleted > 0:
        print("建议随后执行: python main.py link-match-odds 与 python main.py odds-stats 核对。")
    return 0


def cmd_backfill_odds_for_missing(args: argparse.Namespace) -> int:
    """仅 Mongo：针对「无赔率」比赛所在日期，逐日拉取 API-FOOTBALL 赔率并写库，再执行 link-match-odds 关联。"""
    from datetime import date
    from db import repo
    from etl.api_football_odds import fetch_odds_for_date_from_db, RATE_LIMIT_SEC

    if not repo.use_mongo():
        print("当前未使用 MongoDB。backfill-odds-for-missing 仅支持 MongoDB。", file=sys.stderr)
        return 1
    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY。", file=sys.stderr)
        return 1
    without, total, with_odds = repo.matches_without_odds()
    if not without:
        print("当前无「无赔率」比赛，无需补全。")
        return 0
    dates = sorted(set((m.get("match_date") or "")[:10] for m in without if len((m.get("match_date") or "")[:10]) == 10))
    today_str = date.today().isoformat()
    dates = [d for d in dates if d <= today_str]
    min_date = getattr(args, "min_date", None)
    if min_date and len(str(min_date)) >= 10:
        min_date = str(min_date)[:10]
        dates = [d for d in dates if d >= min_date]
        print(f"仅处理 ≥ {min_date} 的日期，共 {len(dates)} 个。")
    total_dates = len(dates)
    limit = getattr(args, "limit", None)
    if limit is not None and limit > 0:
        dates = dates[: int(limit)]
    rate_limit = getattr(args, "rate_limit", None) or RATE_LIMIT_SEC
    dry_run = getattr(args, "dry_run", False)
    print(f"无赔率场次涉及 {total_dates} 个日期（≤今天），本次处理 {len(dates)} 个。")
    if dry_run:
        print("dry-run：仅打印将拉取的日期，不请求 API。")
        for i, d in enumerate(dates[:30], 1):
            print(f"  [{i}] {d}")
        if len(dates) > 30:
            print(f"  ... 共 {len(dates)} 个日期")
        return 0
    ok = 0
    for i, d in enumerate(dates, 1):
        try:
            _, msg = fetch_odds_for_date_from_db(d, rate_limit_sec=rate_limit, merge=True)
            print(f"[{i}/{len(dates)}] {d} {msg}")
            ok += 1
        except Exception as e:
            print(f"[{i}/{len(dates)}] {d} 失败: {e}", file=sys.stderr)
    print("赔率拉取完成，正在执行 link-match-odds 关联…")
    linked, _ = repo.backfill_match_odds_match_id()
    print(f"已关联 {linked} 场 match_id。可再次执行 list-matches-without-odds 查看剩余无赔率场次。")
    return 0


def run_sporttery_odds_for_dates(
    dates: list[str],
    dry_run: bool = False,
    progress_callback: Callable[[int, int, str, int, int, str], None] | None = None,
) -> int:
    """按日期列表从体彩网拉取竞彩场次与固定奖金赔率，写入 match_odds（source=sporttery）。不区分联赛，全部写入。
    progress_callback(i, total, date_str, matches_count, written, hint) 可选，用于后台任务上报进度。"""
    from db import repo
    from etl.sporttery_crawl import crawl_sporttery_for_date, normalize_league_to_big5

    total_written = 0
    for i, d in enumerate(dates, 1):
        try:
            matches = crawl_sporttery_for_date(d)
            written = 0
            for m in matches:
                lig_raw = (m.get("league") or "").strip()
                lig = normalize_league_to_big5(lig_raw) or lig_raw or "其他"
                odds = m.get("odds") or {}
                spf_win = (odds.get("spf_win") or "").strip() or None
                spf_draw = (odds.get("spf_draw") or "").strip() or None
                spf_loss = (odds.get("spf_loss") or "").strip() or None
                has_rqspf = odds.get("rqspf_handicap") is not None and str(odds.get("rqspf_handicap", "")).strip() != ""
                if not (spf_win and spf_draw and spf_loss) and not has_rqspf:
                    continue
                if not spf_win:
                    spf_win = "—"
                if not spf_draw:
                    spf_draw = "—"
                if not spf_loss:
                    spf_loss = "—"
                log_date = (m.get("match_date") or d)[:10]
                home = (m.get("home_cn") or m.get("home") or "").strip()
                away = (m.get("away_cn") or m.get("away") or "").strip()
                if not home or not away:
                    continue
                extra: dict = {}
                if m.get("match_id") is not None:
                    extra["sporttery_match_id"] = str(m["match_id"])
                for key in ("rqspf_handicap", "rqspf_win", "rqspf_draw", "rqspf_loss"):
                    if odds.get(key) is not None and str(odds[key]).strip():
                        extra[key] = str(odds[key]).strip()
                if not dry_run:
                    repo.match_odds_upsert_one(
                        log_date, home, away, lig,
                        spf_win, spf_draw, spf_loss, source="sporttery",
                        **extra,
                    )
                written += 1
            total_written += written
            hint = ""
            if len(matches) > 0 and written == 0:
                hint = "（该日场次无有效胜平负或让球赔率）"
            if progress_callback:
                progress_callback(i, len(dates), d, len(matches), written, hint)
        except Exception as e:
            if progress_callback:
                progress_callback(i, len(dates), d, 0, 0, f"失败: {e}")
    return total_written


def cmd_sporttery_odds_to_db(args: argparse.Namespace) -> int:
    """按日期从体彩网拉取竞彩场次与固定奖金赔率，写入 match_odds（source=sporttery），用于长期历史赔率补全。"""
    from datetime import date, timedelta, datetime

    date_str = getattr(args, "date", None) and str(args.date).strip()[:10]
    dates_str = getattr(args, "dates", None) and str(args.dates).strip()
    days = getattr(args, "days", None)
    year = getattr(args, "year", None)
    dry_run = getattr(args, "dry_run", False)

    dates: list[str] = []
    if date_str and len(date_str) == 10:
        dates = [date_str]
    elif year is not None:
        y = int(year)
        if y < 2000 or y > 2100:
            print(f"--year 需为 2000–2100 的年份，当前为 {y}。", file=sys.stderr)
            return 1
        d0 = date(y, 1, 1)
        today = date.today()
        d1 = min(date(y, 12, 31), today)  # 未过完的年份只拉到今天
        d = d0
        while d <= d1:
            dates.append(d.isoformat())
            d += timedelta(days=1)
        print(f"按年份拉取: {y} 年，共 {len(dates)} 天（{dates[0]} ~ {dates[-1]}，到今日止）", file=sys.stderr)
    elif dates_str:
        parts = [p.strip()[:10] for p in dates_str.split(",") if len(p.strip()) >= 10]
        if len(parts) >= 2:
            try:
                d0, d1 = datetime.strptime(parts[0], "%Y-%m-%d").date(), datetime.strptime(parts[1], "%Y-%m-%d").date()
                d = d0
                while d <= d1:
                    dates.append(d.isoformat())
                    d += timedelta(days=1)
            except ValueError:
                pass
        if not dates:
            print("--dates 需为 YYYY-MM-DD,YYYY-MM-DD 格式。", file=sys.stderr)
            return 1
    elif days is not None and int(days) > 0:
        today = date.today()
        for i in range(int(days)):
            d = today - timedelta(days=i)
            dates.append(d.isoformat())
        dates.sort()
        print(f"使用系统「今天」= {today.isoformat()}，最近 {int(days)} 天：{dates[0]} ~ {dates[-1]}", file=sys.stderr)
    else:
        dates = [date.today().isoformat()]

    if not dates:
        print("请指定 --date YYYY-MM-DD、--dates 起,止、--days N 或 --year 年份。", file=sys.stderr)
        return 1

    from db import repo
    if repo.use_mongo():
        db_name = getattr(config, "MONGO_DB_NAME", "football")
        print(f"写入目标: MongoDB 库「{db_name}」集合「match_odds」", file=sys.stderr)
    else:
        db_url = getattr(config, "DATABASE_URL", "sqlite:///...")
        print(f"写入目标: SQLite 表 match_odds（{db_url}）", file=sys.stderr)

    mode_hint = "（仅预览，不写入数据库）" if dry_run else "（正式写入数据库）"
    print(f"将拉取 {len(dates)} 天（{dates[0]} ~ {dates[-1]}），逐日请求体彩并写入 match_odds {mode_hint}")
    sys.stdout.flush()

    def log_progress(i: int, total: int, date_str: str, matches_count: int, written: int, hint: str) -> None:
        msg = f"当前进行到 {date_str}（第 {i}/{total} 天），本日体彩场次 {matches_count} 场，写入 match_odds {written} 场{hint}"
        print(msg)
        sys.stdout.flush()

    total_written = run_sporttery_odds_for_dates(dates, dry_run=dry_run, progress_callback=log_progress)
    if dry_run:
        print(f"预览完成: 共 {total_written} 条数据（未写入）。去掉 --dry-run 后执行将正式写入 match_odds。")
    else:
        print(f"完成: 共 {total_written} 条体彩赔率已写入 match_odds。可执行 normalize-team-names 与 link-match-odds 与 matches 关联。")
        # 验证：查询刚写入的日期范围内 source=sporttery 的条数
        try:
            if repo.use_mongo():
                coll = repo._db()["match_odds"]
                n = coll.count_documents({"source": "sporttery", "log_date": {"$gte": dates[0], "$lte": dates[-1]}})
                print(f"验证: MongoDB 库「{getattr(config, 'MONGO_DB_NAME', 'football')}」match_odds 中 {dates[0]}～{dates[-1]}（source=sporttery）共 {n} 条。", file=sys.stderr)
            else:
                from sqlalchemy import text
                from etl.schema import get_engine
                with get_engine().connect() as conn:
                    r = conn.execute(text(
                        "SELECT COUNT(*) FROM match_odds WHERE source='sporttery' AND log_date >= :d0 AND log_date <= :d1"
                    ), {"d0": dates[0], "d1": dates[-1]})
                    n = r.scalar()
                print(f"验证: SQLite match_odds 表内 {dates[0]}～{dates[-1]}（source=sporttery）共 {n} 条。", file=sys.stderr)
        except Exception as e:
            print(f"验证条数时出错（不影响已写入数据）: {e}", file=sys.stderr)
    return 0


def cmd_jc_sync_match_results(args: argparse.Namespace) -> int:
    """与仪表盘「拉取比赛结果」一致：赛果接口 getUniformMatchResultV1 + 逐场详情 getMatchHeadV1，写入 jingcai_match_analysis。"""
    import json
    import time

    from db.self_improvement import (
        _jc_sync_all_matches_have_complete_scores,
        jc_match_analysis_distinct_jc_dates_for_years,
        jc_match_analysis_list_by_jc_date,
        jc_match_analysis_sync_results_for_jc_date,
        jc_matches_load,
    )

    day = (getattr(args, "date", None) or "").strip()[:10]
    years: list[int] = []
    if getattr(args, "year", None) is not None:
        years.append(int(args.year))
    ys = getattr(args, "years", None)
    if ys:
        for p in str(ys).split(","):
            p = p.strip()
            if p.isdigit():
                years.append(int(p))
    years = sorted(set(years))

    if len(day) == 10:
        dates_to_run = [day]
    elif years:
        dates_to_run = jc_match_analysis_distinct_jc_dates_for_years(years)
        if not dates_to_run:
            print(f"库中无 {years} 年已保存的竞彩日，请先 jc-to-self-improvement / 拉取竞彩列表。", file=sys.stderr)
            return 1
    else:
        print("请指定 --date YYYY-MM-DD（单日），或 --year YYYY / --years 2026[,2025,...]（按库中该年竞彩日逐日同步）。", file=sys.stderr)
        return 1

    lim = getattr(args, "limit", None)
    if lim is not None and int(lim) > 0:
        dates_to_run = dates_to_run[: int(lim)]

    force_all = getattr(args, "force_all", False)
    if not force_all:
        inc: list[str] = []
        skip_complete = 0
        for d in dates_to_run:
            m = jc_match_analysis_list_by_jc_date(d) or jc_matches_load(d)
            if m and _jc_sync_all_matches_have_complete_scores(m):
                skip_complete += 1
                continue
            inc.append(d)
        if skip_complete:
            print(f"已跳过 {skip_complete} 个已含全场+半场的竞彩日（无需重复请求体彩；加 --force-all 可强制重拉）", flush=True)
        dates_to_run = inc

    if not dates_to_run:
        print("没有需要拉取赛果的竞彩日（均已齐比分或库中无该日列表）。", flush=True)
        return 0

    delay = float(getattr(args, "day_delay", 0) or 0)
    detail_delay = float(getattr(args, "detail_delay", 0) or 0)
    uniform_delay = float(getattr(args, "uniform_delay", 0) or 0)
    ignore_kickoff = getattr(args, "ignore_kickoff", False)
    json_out = getattr(args, "json_out", False)

    total_days = len(dates_to_run)
    print(
        f"jc-sync-match-results：共 {total_days} 个竞彩日；防封间隔：日间 {delay}s，逐场详情后 {detail_delay}s，赛果分页 {uniform_delay}s"
        f"（可设 0 关闭对应项；建议配 .env 的 SPORTTERY_PROXY）",
        flush=True,
    )
    rc = 0
    for i, d in enumerate(dates_to_run, 1):
        print(f"--- [{i}/{total_days}] {d} ---", flush=True)
        out = jc_match_analysis_sync_results_for_jc_date(
            d,
            only_missing_scores=not force_all,
            respect_kickoff=not ignore_kickoff,
            detail_delay_sec=detail_delay,
            uniform_rate_limit_sec=uniform_delay,
            backfill_fixture_id=getattr(args, "backfill_fixture_id", False),
        )
        print(out.get("message", ""), flush=True)
        if json_out:
            print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
        if out.get("failed"):
            rc = 1
        if delay > 0 and i < total_days:
            time.sleep(delay)
    return rc


def cmd_jc_match_detail(args: argparse.Namespace) -> int:
    """拉取单场对阵详情（头部、伤停、射手、比赛结果、历史交锋、特征分析），直接请求体彩 webapi。"""
    import json
    from etl.sporttery_crawl import fetch_sporttery_match_detail_via_api

    mid = getattr(args, "mid", None)
    if mid is None:
        print("请指定 --mid 体彩场次 ID（如 2037891）。", file=sys.stderr)
        return 1
    try:
        mid = int(mid)
    except (TypeError, ValueError):
        print("--mid 需为整数。", file=sys.stderr)
        return 1
    out = fetch_sporttery_match_detail_via_api(mid)
    if getattr(args, "save", False):
        from db.self_improvement import jc_match_analysis_save
        if jc_match_analysis_save(mid, out):
            print(f"已写入 self_improvement_football.jingcai_match_analysis（mid={mid}）")
        else:
            print("未配置 MongoDB（MONGO_URI），无法落库。", file=sys.stderr)
    if getattr(args, "json", False):
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"体彩场次 mid={mid} 对阵详情：")
        for key in ("match_head", "injury_suspension", "match_player", "result_history_all", "result_history_same_tournament", "match_feature"):
            val = out.get(key)
            status = "有" if val is not None and (val if isinstance(val, bool) else (len(val) if isinstance(val, (list, dict)) else 1)) else "无"
            print(f"  {key}: {status}")
        print("加 --json 可输出完整 JSON；加 --save 可写入库 self_improvement_football.jingcai_match_analysis。")
    return 0


def cmd_discover_lottery_gov_api(args: argparse.Namespace) -> int:
    """打开官网足球胜平负页面，拦截 XHR，打印疑似数据接口 URL。"""
    from etl.lottery_gov_crawl import discover_gov_api
    captured = discover_gov_api(timeout_ms=20000)
    if not captured:
        print("未拦截到疑似数据接口（可能页面超时或结构变化）。可稍后重试或直接用 lottery-gov-odds-to-db（会通过页面拦截取数）。", file=sys.stderr)
        return 0
    for i, c in enumerate(captured, 1):
        print(f"--- 接口 {i} ---")
        print("URL:", c.get("url", ""))
        print("Method:", c.get("method", "GET"))
        preview = c.get("response_preview")
        if isinstance(preview, str):
            print("Response 预览:", preview[:500] + "..." if len(preview) > 500 else preview)
        else:
            print("Response 预览:", json.dumps(preview, ensure_ascii=False)[:500] + "..." if preview else "")
    print("\n若某 URL 可复现（带日期参数直接请求），可在 etl/lottery_gov_crawl.py 中设置 LOTTERY_GOV_API_CONFIG = {\"url\": \"...\", \"method\": \"GET\", \"params_from_page\": true} 以跳过浏览器。")
    return 0


def cmd_lottery_gov_odds_to_db(args: argparse.Namespace) -> int:
    """按日期从中国体彩官网 https://www.lottery.gov.cn/jc/jsq/zqspf/ 抓取让球数等，写入 match_odds（source=lottery_gov）。通过 Playwright 打开页面并拦截接口获取数据。"""
    from datetime import date, timedelta
    from db import repo
    from etl.lottery_gov_crawl import fetch_zqspf_for_date
    from etl.sporttery_crawl import BIG5_LEAGUES

    date_str = getattr(args, "date", None) and str(args.date).strip()[:10]
    dates_str = getattr(args, "dates", None) and str(args.dates).strip()
    days = getattr(args, "days", None)
    dry_run = getattr(args, "dry_run", False)

    dates: list[str] = []
    if date_str and len(date_str) == 10:
        dates = [date_str]
    elif dates_str:
        parts = [p.strip()[:10] for p in dates_str.split(",") if len(p.strip()) >= 10]
        if len(parts) >= 2:
            from datetime import datetime
            try:
                d0, d1 = datetime.strptime(parts[0], "%Y-%m-%d").date(), datetime.strptime(parts[1], "%Y-%m-%d").date()
                d = d0
                while d <= d1:
                    dates.append(d.isoformat())
                    d += timedelta(days=1)
            except ValueError:
                pass
        if not dates:
            print("--dates 需为 YYYY-MM-DD,YYYY-MM-DD 格式。", file=sys.stderr)
            return 1
    elif days is not None and int(days) > 0:
        today = date.today()
        for i in range(int(days)):
            d = today - timedelta(days=i)
            dates.append(d.isoformat())
        dates.sort()
    else:
        dates = [date.today().isoformat()]

    if not dates:
        print("请指定 --date YYYY-MM-DD 或 --dates 起,止 或 --days N。", file=sys.stderr)
        return 1

    total_written = 0
    for i, d in enumerate(dates, 1):
        try:
            matches = fetch_zqspf_for_date(d, prefer_playwright=True)
            written = 0
            for m in matches:
                lig = (m.get("league") or "").strip()
                if lig not in BIG5_LEAGUES:
                    continue
                odds = m.get("odds") or {}
                spf_win = (odds.get("spf_win") or "").strip() or None
                spf_draw = (odds.get("spf_draw") or "").strip() or None
                spf_loss = (odds.get("spf_loss") or "").strip() or None
                if not (spf_win and spf_draw and spf_loss):
                    spf_win = spf_win or "—"
                    spf_draw = spf_draw or "—"
                    spf_loss = spf_loss or "—"
                log_date = (m.get("match_date") or d)[:10]
                home = (m.get("home_cn") or m.get("home") or "").strip()
                away = (m.get("away_cn") or m.get("away") or "").strip()
                if not home or not away:
                    continue
                extra: dict = {}
                if m.get("match_id") is not None:
                    extra["sporttery_match_id"] = str(m["match_id"])
                for key in ("rqspf_handicap", "rqspf_win", "rqspf_draw", "rqspf_loss"):
                    if odds.get(key) is not None and str(odds[key]).strip():
                        extra[key] = str(odds[key]).strip()
                if not extra.get("rqspf_handicap"):
                    continue
                if not dry_run:
                    repo.match_odds_upsert_one(
                        log_date, home, away, lig,
                        spf_win or "—", spf_draw or "—", spf_loss or "—",
                        source="lottery_gov",
                        **extra,
                    )
                written += 1
            total_written += written
            hint = ""
            if len(matches) > 0 and written == 0:
                hint = "（该日无五大联赛或无让球数）"
            print(f"[{i}/{len(dates)}] {d} 官网场次 {len(matches)} 场，写入 match_odds（source=lottery_gov）{written} 场{hint}")
        except Exception as e:
            print(f"[{i}/{len(dates)}] {d} 失败: {e}", file=sys.stderr)
    print("完成: 共 {0} 场官网赔率写入 match_odds。可执行 normalize-team-names 与 link-match-odds 与 matches 关联。".format(total_written))
    return 0


def cmd_jc_odds_to_db(args: argparse.Namespace) -> int:
    """按日期从第三方竞彩 API（JC_ODDS_*）拉取赔率并写入 match_odds（source=jc_api），用于历史赔率补全。需在 .env 配置 JC_ODDS_API_BASE、JC_ODDS_API_TOKEN。"""
    from datetime import date, timedelta
    from db import repo
    from etl.jc_odds_api import fetch_jc_odds_for_date

    if not (getattr(config, "JC_ODDS_API_BASE", None) or "").strip() or not (getattr(config, "JC_ODDS_API_TOKEN", None) or "").strip():
        print("请在 .env 中配置 JC_ODDS_API_BASE 与 JC_ODDS_API_TOKEN（第三方竞彩 API）。", file=sys.stderr)
        return 1

    date_str = getattr(args, "date", None) and str(args.date).strip()[:10]
    dates_str = getattr(args, "dates", None) and str(args.dates).strip()
    days = getattr(args, "days", None)
    dry_run = getattr(args, "dry_run", False)
    five_only = not getattr(args, "no_five_only", False)

    dates: list[str] = []
    if date_str and len(date_str) == 10:
        dates = [date_str]
    elif dates_str:
        parts = [p.strip()[:10] for p in dates_str.split(",") if len(p.strip()) >= 10]
        if len(parts) >= 2:
            from datetime import datetime
            try:
                d0, d1 = datetime.strptime(parts[0], "%Y-%m-%d").date(), datetime.strptime(parts[1], "%Y-%m-%d").date()
                d = d0
                while d <= d1:
                    dates.append(d.isoformat())
                    d += timedelta(days=1)
            except ValueError:
                pass
        if not dates:
            print("--dates 需为 YYYY-MM-DD,YYYY-MM-DD 格式。", file=sys.stderr)
            return 1
    elif days is not None and int(days) > 0:
        today = date.today()
        for i in range(int(days)):
            d = today - timedelta(days=i)
            dates.append(d.isoformat())
        dates.sort()
        print(f"使用系统「今天」= {today.isoformat()}，最近 {int(days)} 天：{dates[0]} ~ {dates[-1]}", file=sys.stderr)
    else:
        dates = [date.today().isoformat()]

    if not dates:
        print("请指定 --date YYYY-MM-DD 或 --dates 起,止 或 --days N。", file=sys.stderr)
        return 1

    BIG5 = {"英超", "西甲", "意甲", "德甲", "法甲"}
    total_written = 0
    for i, d in enumerate(dates, 1):
        try:
            items = fetch_jc_odds_for_date(d)
            written = 0
            for m in items:
                league_cn = (m.get("league_cn") or "").strip()
                if five_only and league_cn not in BIG5:
                    continue
                if not league_cn:
                    league_cn = "其他"
                odds = m.get("odds") or {}
                spf_win = (odds.get("spf_win") or "").strip() or None
                spf_draw = (odds.get("spf_draw") or "").strip() or None
                spf_loss = (odds.get("spf_loss") or "").strip() or None
                if not (spf_win and spf_draw and spf_loss):
                    continue
                log_date = (m.get("match_date") or d)[:10]
                home = (m.get("home") or "").strip()
                away = (m.get("away") or "").strip()
                if not home or not away:
                    continue
                extra: dict = {}
                for key in ("rqspf_handicap", "rqspf_win", "rqspf_draw", "rqspf_loss"):
                    if odds.get(key) is not None and str(odds[key]).strip():
                        extra[key] = str(odds[key]).strip()
                if not dry_run:
                    repo.match_odds_upsert_one(
                        log_date, home, away, league_cn,
                        spf_win, spf_draw, spf_loss, source="jc_api",
                        **extra,
                    )
                written += 1
            total_written += written
            hint = ""
            if len(items) > 0 and written == 0 and five_only:
                hint = "（该日无五大联赛或缺少胜平负）"
            print(f"[{i}/{len(dates)}] {d} 竞彩 API {len(items)} 场，写入 match_odds（source=jc_api）{written} 场{hint}")
        except Exception as e:
            print(f"[{i}/{len(dates)}] {d} 失败: {e}", file=sys.stderr)
    print(f"完成: 共 {total_written} 场竞彩 API 赔率写入 match_odds。可执行 normalize-team-names 与 link-match-odds 与 matches 关联。")
    return 0


def cmd_fixtures(args: argparse.Namespace) -> int:
    """列出指定联赛/赛季的未开赛场次，便于用 predict 做预测。"""
    from etl.football_data_ingest import get_upcoming_fixtures
    if not config.FOOTBALL_DATA_API_KEY:
        print("请配置 FOOTBALL_DATA_API_KEY 后使用 fixtures", file=sys.stderr)
        return 1
    league = getattr(args, "league", "PL")
    season = getattr(args, "season", 2025)
    rows = get_upcoming_fixtures(league, season)
    if not rows:
        print(f"暂无 {league} {season} 赛季的未开赛场次，或 API 未返回。", file=sys.stderr)
        return 0
    print(f"未来赛程 {league} {season}（未开赛）")
    print("-" * 60)
    for r in rows[:30]:
        date = (r.get("utcDate") or "")[:10]
        print(f"  {date}   {r.get('home', '?')} vs {r.get('away', '?')}")
    if len(rows) > 30:
        print(f"  ... 共 {len(rows)} 场")
    return 0


def cmd_predict_today(args: argparse.Namespace) -> int:
    """拉取今日五大联赛未开赛场次并逐场预测（可写入 prediction_log）。"""
    from datetime import date
    from etl.football_data_ingest import get_upcoming_fixtures
    from engine.prediction_log import append_prediction, load_log, save_log
    from engine.predict import resolve_team_by_name, resolve_competition_by_name, predict_match

    if not config.FOOTBALL_DATA_API_KEY:
        print("请配置 FOOTBALL_DATA_API_KEY 后使用 predict-today", file=sys.stderr)
        return 1
    if not config.DB_PATH.exists():
        print("尚无数据，请先执行: python main.py ingest-fd", file=sys.stderr)
        return 1

    target_date = getattr(args, "date", None) or date.today().isoformat()
    save_to_log = not getattr(args, "no_save", False)
    # 当前日期在 2025-2026 赛季内，用 2025
    season = 2025
    engine = get_engine()

    all_fixtures = []
    for code in config.FOOTBALL_DATA_BIG5:
        rows = get_upcoming_fixtures(code, season)
        comp_name = LEAGUE_COMPETITION_NAMES.get(code, code)
        for r in rows:
            day = (r.get("utcDate") or "")[:10]
            if day == target_date:
                all_fixtures.append({
                    "home": r.get("home", "?"),
                    "away": r.get("away", "?"),
                    "competition": comp_name,
                    "date": day,
                })

    if not all_fixtures:
        print(f"今日（{target_date}）五大联赛暂无未开赛场次。")
        return 0

    print(f"今日（{target_date}）五大联赛共 {len(all_fixtures)} 场未开赛，开始预测…")
    print("-" * 70)
    last_n = getattr(args, "last_n", 10)
    entries_before = len(load_log()) if save_to_log else 0

    for i, f in enumerate(all_fixtures):
        home, away, comp = f["home"], f["away"], f["competition"]
        if save_to_log:
            result = append_prediction(
                engine,
                home_team=home,
                away_team=away,
                competition=comp,
                last_n=last_n,
                prediction_date=target_date,
                odds=f.get("odds"),
            )
        else:
            home_id = resolve_team_by_name(engine, home)
            away_id = resolve_team_by_name(engine, away)
            if home_id is None or away_id is None:
                print(f"  [{i+1}] {home} vs {away}  — 跳过（队伍未入库）")
                continue
            comp_id = resolve_competition_by_name(engine, comp)
            result = predict_match(engine, home_id, away_id, competition_id=comp_id, last_n=last_n)
        if result is None:
            print(f"  [{i+1}] {home} vs {away}  — 跳过（队伍未入库）")
            continue
        pred_h, pred_a = result["predicted_score"][0], result["predicted_score"][1]
        outcome = "主" if pred_h > pred_a else ("客" if pred_a > pred_h else "平")
        line = (
            f"  {home} vs {away}  [{comp}]\n"
            f"    期望比分 {pred_h}-{pred_a} | 主 {result['exp_goals_home']:.2f} / 客 {result['exp_goals_away']:.2f} | "
            f"主胜 {result['prob_home_win']*100:.0f}% 平 {result['prob_draw']*100:.0f}% 客胜 {result['prob_away_win']*100:.0f}% → 预测: {outcome}"
        )
        print(line)
    if save_to_log and len(load_log()) > entries_before:
        print("-" * 70)
        print(f"已写入 prediction_log，共 {len(load_log()) - entries_before} 条。可用 prediction-log list --date {target_date} 查看。")
    return 0


def cmd_predict_jingcai(args: argparse.Namespace) -> int:
    """按 data/jingcai_fixtures.json 中配置的竞足彩场次预测：先清除该日期的旧记录，再只写入配置中的场次。"""
    from engine.prediction_log import append_prediction, load_log, remove_entries_by_date
    from engine.predict import resolve_team_by_name, resolve_competition_by_name, predict_match

    target_date = getattr(args, "date", None)
    if not target_date:
        print("请指定 --date YYYY-MM-DD", file=sys.stderr)
        return 1
    jingcai_path = config.DATA_DIR / "jingcai_fixtures.json"
    if not jingcai_path.is_file():
        print(f"未找到 {jingcai_path}，请先创建并配置竞足彩场次。", file=sys.stderr)
        return 1
    try:
        raw = json.loads(jingcai_path.read_text(encoding="utf-8"))
        fixtures_by_date = raw.get("fixtures") or raw
        matches = fixtures_by_date.get(target_date) if isinstance(fixtures_by_date, dict) else None
    except Exception as e:
        print(f"读取 jingcai_fixtures.json 失败: {e}", file=sys.stderr)
        return 1
    if not matches:
        print(f"竞足彩配置中无 {target_date} 的场次。请在 data/jingcai_fixtures.json 的 fixtures 下添加该日期。")
        return 0
    if not config.DB_PATH.exists():
        print("尚无数据，请先执行: python main.py ingest-fd", file=sys.stderr)
        return 1
    engine = get_engine()
    last_n = getattr(args, "last_n", 10)
    removed = remove_entries_by_date(target_date)
    if removed:
        print(f"已清除 {target_date} 的旧预测 {removed} 条。")
    print(f"竞足彩 {target_date} 共 {len(matches)} 场，开始预测…")
    print("-" * 70)
    ok = 0
    for i, m in enumerate(matches):
        home = m.get("home") or m.get("home_team", "?")
        away = m.get("away") or m.get("away_team", "?")
        comp = m.get("competition") or "Premier League"
        result = append_prediction(
            engine,
            home_team=home,
            away_team=away,
            competition=comp,
            last_n=last_n,
            prediction_date=target_date,
            odds=m.get("odds"),
        )
        if result is None:
            print(f"  [{i+1}] {home} vs {away}  — 跳过（队伍未入库或名称不匹配）")
            continue
        ok += 1
        pred_h, pred_a = result["predicted_score"][0], result["predicted_score"][1]
        outcome = "主" if pred_h > pred_a else ("客" if pred_a > pred_h else "平")
        print(
            f"  [{i+1}] {home} vs {away}  [{comp}]\n"
            f"    期望比分 {pred_h}-{pred_a} | 主 {result['exp_goals_home']:.2f} / 客 {result['exp_goals_away']:.2f} | "
            f"主胜 {result['prob_home_win']*100:.0f}% 平 {result['prob_draw']*100:.0f}% 客胜 {result['prob_away_win']*100:.0f}% → 预测: {outcome}"
        )
    print("-" * 70)
    print(f"已写入 prediction_log，共 {ok} 条。选 {target_date} 即可在网页查看这 {ok} 场竞足彩预测。")
    return 0


def cmd_daily_jingcai(args: argparse.Namespace) -> int:
    """自动拉取当日五大联赛未开赛场次，写入 jingcai_fixtures.json 并执行预测。数据仅从 API（football-data.org）获取。"""
    from datetime import date, timedelta
    from engine.prediction_log import append_prediction, remove_entries_by_date

    list_date = getattr(args, "date", None) or date.today().isoformat()
    try:
        d = date.fromisoformat(list_date[:10])
        match_date = (d + timedelta(days=1)).isoformat()  # 比赛日 = 列表日 + 1
    except ValueError:
        match_date = list_date

    from etl.football_data_ingest import get_upcoming_fixtures
    if not config.FOOTBALL_DATA_API_KEY:
        print("请配置 FOOTBALL_DATA_API_KEY 以从 API 获取场次。", file=sys.stderr)
        return 1
    if not config.DB_PATH.exists():
        print("尚无数据，请先执行: python main.py ingest-fd", file=sys.stderr)
        return 1
    season = 2025
    all_fixtures = []
    for code in config.FOOTBALL_DATA_BIG5:
        for r in get_upcoming_fixtures(code, season):
            if (r.get("utcDate") or "")[:10] == match_date:
                all_fixtures.append({
                    "home": r.get("home", "?"),
                    "away": r.get("away", "?"),
                    "competition": LEAGUE_COMPETITION_NAMES.get(code, code),
                })

    if not all_fixtures:
        print(f"比赛日 {match_date}（列表日 {list_date}）五大联赛暂无未开赛场次。")
        return 0

    print(f"从 football-data.org API 获取 比赛日 {match_date} 五大联赛未开赛共 {len(all_fixtures)} 场。")
    jingcai_path = config.DATA_DIR / "jingcai_fixtures.json"
    try:
        raw = json.loads(jingcai_path.read_text(encoding="utf-8")) if jingcai_path.is_file() else {}
    except Exception:
        raw = {}
    if "fixtures" not in raw:
        raw["fixtures"] = {}
    raw["fixtures"][match_date] = all_fixtures
    jingcai_path.parent.mkdir(parents=True, exist_ok=True)
    jingcai_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已更新 {jingcai_path}，比赛日 {match_date} 共 {len(all_fixtures)} 场。")

    engine = get_engine()
    last_n = getattr(args, "last_n", 10)
    removed = remove_entries_by_date(match_date)
    if removed:
        print(f"已清除 比赛日 {match_date} 的旧预测 {removed} 条。")
    print(f"竞足彩 比赛日 {match_date} 共 {len(all_fixtures)} 场，开始预测…")
    print("-" * 70)
    ok = 0
    for i, m in enumerate(all_fixtures):
        home = m.get("home", "?")
        away = m.get("away", "?")
        comp = m.get("competition") or "Premier League"
        result = append_prediction(
            engine,
            home_team=home,
            away_team=away,
            competition=comp,
            last_n=last_n,
            prediction_date=match_date,
        )
        if result is None:
            print(f"  [{i+1}] {home} vs {away}  — 跳过（队伍未入库或名称不匹配）")
            continue
        ok += 1
        pred_h, pred_a = result["predicted_score"][0], result["predicted_score"][1]
        outcome = "主" if pred_h > pred_a else ("客" if pred_a > pred_h else "平")
        print(
            f"  [{i+1}] {home} vs {away}  [{comp}]\n"
            f"    期望比分 {pred_h}-{pred_a} | 主 {result['exp_goals_home']:.2f} / 客 {result['exp_goals_away']:.2f} | "
            f"主胜 {result['prob_home_win']*100:.0f}% 平 {result['prob_draw']*100:.0f}% 客胜 {result['prob_away_win']*100:.0f}% → 预测: {outcome}"
        )
    print("-" * 70)
    print(f"已写入 prediction_log，共 {ok} 条。在网页选择日期 {list_date}（比赛日 {match_date}）即可查看。")
    return 0


def _ensure_team_by_name(session: Session, name: str) -> int:
    """按名称获取或创建队伍，返回 team id。仅 SQLite 路径使用；Mongo 用 repo.ensure_team_by_name。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("队伍名不能为空")
    row = session.query(Team).filter(Team.name == name).first()
    if row:
        return row.id
    mx = session.query(Team).order_by(Team.id.desc()).first()
    next_id = (mx.id + 1) if mx else 1
    session.add(Team(id=next_id, name=name))
    session.flush()
    return next_id


def cmd_add_match(args: argparse.Namespace) -> int:
    """手动添加一场已完赛比赛到 DB（自己做复盘数据），主客队名与 prediction_log 一致时网页点复盘可出完整报告。"""
    from db import repo
    repo.init_db()
    date_str = (getattr(args, "date", None) or "").strip()[:10]
    home_team = (getattr(args, "home_team", None) or "").strip()
    away_team = (getattr(args, "away_team", None) or "").strip()
    home_score = getattr(args, "home_score", None)
    away_score = getattr(args, "away_score", None)
    if not date_str or len(date_str) != 10:
        print("请提供比赛日 YYYY-MM-DD", file=sys.stderr)
        return 1
    if not home_team or not away_team:
        print("请提供主队、客队名称", file=sys.stderr)
        return 1
    if home_score is None or away_score is None:
        print("请提供主队进球数、客队进球数", file=sys.stderr)
        return 1
    try:
        home_score = int(home_score)
        away_score = int(away_score)
    except (TypeError, ValueError):
        print("主队/客队进球须为整数", file=sys.stderr)
        return 1
    half_time = getattr(args, "half_time", None)
    ht_home, ht_away = None, None
    if half_time and len(half_time) >= 2:
        try:
            ht_home, ht_away = int(half_time[0]), int(half_time[1])
        except (TypeError, ValueError):
            pass
    competition = (getattr(args, "competition", None) or "").strip() or "Premier League"
    season_name = (getattr(args, "season_name", None) or "").strip() or "2025/2026"
    safe = re.sub(r"[^\w\-]", "_", f"{date_str}_{home_team}_{away_team}")[:100]
    source_id = f"manual_{safe}"
    existing = repo.get_match_by_source_id(source_id)
    if existing:
        print(f"该场已存在：match_id={existing['id']}，可直接在网页点复盘。")
        return 0
    try:
        home_id = repo.ensure_team_by_name(home_team)
        away_id = repo.ensure_team_by_name(away_team)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    raw_meta = {
        "home_team_name": home_team,
        "away_team_name": away_team,
        "competition_name": competition,
        "season_name": season_name,
    }
    if ht_home is not None or ht_away is not None:
        raw_meta["half_time_home"] = ht_home
        raw_meta["half_time_away"] = ht_away
    match_dict = {
        "competition_id": None,
        "season_id": None,
        "match_date": date_str,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_score": home_score,
        "away_score": away_score,
        "match_week": None,
        "source_id": source_id,
        "raw_meta": json.dumps(raw_meta, ensure_ascii=False),
    }
    mid = repo.insert_match(match_dict)
    print(f"已添加比赛：match_id={mid}，{home_team} vs {away_team} {home_score}-{away_score}")
    print("在网页选择该比赛日并点「复盘」即可生成完整复盘报告。")
    return 0


def cmd_sync_sporttery(args: argparse.Namespace) -> int:
    """已废弃：数据改为仅从 API 获取，请使用 ingest / 回填赛果后配合 prediction-log sync-from-db 或 backfill。"""
    print("sync-sporttery 已废弃，数据请从 API 获取（如 football-data ingest、API-Football 等），赛果回填后使用 prediction-log sync-from-db 或 backfill-predictions。", file=sys.stderr)
    return 0


def run_auto_accumulate_log(dates: list[str]) -> tuple[int, int, int]:
    """
    自动为指定日期：从 DB 取五大联赛场次，未开赛且 log 无预测的写入预测，已完赛的用 DB 比分回填 log。
    数据来自 API（ingest 等），不再从体彩网抓取。返回 (新增预测条数, 回填比分条数, 处理场次数)。
    """
    from datetime import date, timedelta
    from db import repo
    from engine.prediction_log import append_prediction, has_entry_for_match, set_actual_by_match, ensure_prediction_in_db_for_match

    if not dates:
        dates = [
            (date.today() - timedelta(days=1)).isoformat(),
            date.today().isoformat(),
            (date.today() + timedelta(days=1)).isoformat(),
        ]
    from db import repo as _repo
    _repo.init_db()
    engine = _repo.get_engine()
    if not config.DB_PATH.exists() and not _repo.use_mongo():
        return 0, 0, 0
    added_predictions = 0
    updated_log = 0
    total_matches = 0
    comp_en = {"英超": "Premier League", "西甲": "La Liga", "意甲": "Serie A", "德甲": "Bundesliga", "法甲": "Ligue 1"}
    for date_str in dates:
        date_str = (date_str or "").strip()[:10]
        if not date_str or len(date_str) != 10:
            continue
        try:
            matches = repo.get_db_matches_for_date_with_scores(date_str)
        except Exception:
            continue
        for m in matches:
            total_matches += 1
            home = (m.get("home_name") or "").strip()
            away = (m.get("away_name") or "").strip()
            hs, aws = m.get("home_score"), m.get("away_score")
            mdate = (m.get("date") or date_str)[:10]
            comp = (m.get("league_cn") or "英超")
            comp_en_name = comp_en.get(comp, "Premier League")
            if hs is not None and aws is not None:
                idx = set_actual_by_match(home, away, hs, aws, match_date=mdate)
                if idx >= 0:
                    updated_log += 1
                mid = m.get("match_id")
                if mid:
                    ensure_prediction_in_db_for_match(engine, mid, mdate, home, away)
                continue
            if has_entry_for_match(home, away, mdate):
                continue
            try:
                result = append_prediction(
                    engine,
                    home_team=home,
                    away_team=away,
                    competition=comp_en_name,
                    prediction_date=mdate,
                )
                if result is not None:
                    added_predictions += 1
            except Exception:
                pass
    return added_predictions, updated_log, total_matches


def run_predict_for_date(date_str: str) -> tuple[int, int, int]:
    """
    对指定日期的 MongoDB（或 DB）五大联赛场次用模型预测，并写入 prediction_log + predictions。
    用于 Web 仪表盘展示。返回 (新增预测数, 已存在跳过数, 总场次数)。
    """
    from db import repo
    from engine.prediction_log import append_prediction, has_entry_for_match

    date_only = (date_str or "").strip()[:10]
    if not date_only or len(date_only) != 10:
        return 0, 0, 0
    repo.init_db()
    engine = repo.get_engine()
    try:
        matches = repo.get_db_matches_for_date_with_scores(date_only)
    except Exception:
        return 0, 0, 0
    comp_en = {"英超": "Premier League", "西甲": "La Liga", "意甲": "Serie A", "德甲": "Bundesliga", "法甲": "Ligue 1"}
    added, skipped, total = 0, 0, len(matches)
    for m in matches:
        home = (m.get("home_name") or "").strip()
        away = (m.get("away_name") or "").strip()
        mdate = (m.get("date") or date_only)[:10]
        comp = (m.get("league_cn") or "英超")
        comp_en_name = comp_en.get(comp, "Premier League")
        match_id = m.get("match_id")
        if has_entry_for_match(home, away, mdate):
            skipped += 1
            continue
        try:
            result = append_prediction(
                engine,
                home_team=home,
                away_team=away,
                competition=comp_en_name,
                prediction_date=mdate,
                last_n=10,
                match_id=match_id,
            )
            if result is not None:
                added += 1
        except Exception:
            pass
    return added, skipped, total


def run_repredict_for_date(date_str: str, skip_existing: bool = False) -> tuple[int, int]:
    """
    对指定日期强制重新预测：默认先删该日旧预测与 prediction_log，再逐场调用 append_prediction；
    skip_existing=True 时不删、只对「尚无预测」的场次补算（用于限额后断点续跑）。
    返回 (成功数, 总场次)。
    """
    from db import repo
    from engine.prediction_log import append_prediction

    date_only = (date_str or "").strip()[:10]
    if not date_only or len(date_only) != 10:
        return 0, 0
    repo.init_db()
    engine = repo.get_engine()
    try:
        matches = repo.get_db_matches_for_date_with_scores(date_only)
    except Exception:
        return 0, 0
    if not matches:
        return 0, 0
    comp_en = {"英超": "Premier League", "西甲": "La Liga", "意甲": "Serie A", "德甲": "Bundesliga", "法甲": "Ligue 1"}
    if not skip_existing:
        for m in matches:
            mid = m.get("match_id")
            if mid is not None:
                repo.delete_predictions_by_match_id(mid)
        repo.prediction_log_delete_by_date(date_only)
    ok = 0
    for m in matches:
        match_id = m.get("match_id")
        if skip_existing and match_id is not None and repo.predictions_exists_for_match(match_id):
            continue
        home = (m.get("home_name") or "").strip()
        away = (m.get("away_name") or "").strip()
        mdate = (m.get("date") or date_only)[:10]
        comp = (m.get("league_cn") or "英超")
        comp_en_name = comp_en.get(comp, "Premier League")
        try:
            result = append_prediction(
                engine,
                home_team=home,
                away_team=away,
                competition=comp_en_name,
                prediction_date=mdate,
                last_n=10,
                match_id=match_id,
            )
            if result is not None:
                ok += 1
        except Exception:
            pass
    return ok, len(matches)


def cmd_predict_for_date(args: argparse.Namespace) -> int:
    """对指定日期的 DB 场次做模型预测并写入 MongoDB/DB，供 Web 仪表盘展示。"""
    date_str = (getattr(args, "date", None) or "").strip()[:10]
    if not date_str or len(date_str) != 10:
        print("请提供 --date YYYY-MM-DD", file=sys.stderr)
        return 1
    try:
        added, skipped, total = run_predict_for_date(date_str)
    except Exception as e:
        print(f"执行失败: {e}", file=sys.stderr)
        return 1
    print(f"predict-for-date：比赛日 {date_str} 共 {total} 场；新增预测 {added} 条，已存在跳过 {skipped} 场。")
    print("刷新 Web 仪表盘选择该日期即可查看预测。")
    return 0


def cmd_repredict_for_date(args: argparse.Namespace) -> int:
    """对指定日期强制重新预测，分别保存模型/API/融合结果。"""
    date_str = (getattr(args, "date", None) or "").strip()[:10]
    if not date_str or len(date_str) != 10:
        print("请提供 --date YYYY-MM-DD", file=sys.stderr)
        return 1
    try:
        ok, total = run_repredict_for_date(date_str)
    except Exception as e:
        print(f"执行失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    print(f"repredict-for-date：比赛日 {date_str} 共 {total} 场，重新预测成功 {ok} 场。")
    print("请刷新 Web 仪表盘查看「模型预测」「API预测」「融合预测」三列。")
    return 0


def run_repredict_for_all(
    years: list[int] | None = None,
    rate_limit_sec: float | None = None,
    skip_existing: bool = False,
) -> tuple[int, int, int]:
    """
    对所有比赛日（或指定年份）强制重新预测。返回 (成功场次, 总场次, 处理日期数)。
    rate_limit_sec: 若指定，在本次运行期间覆盖 config.API_FOOTBALL_RATE_LIMIT_SEC（影响 API 预测请求间隔）。
    skip_existing: 为 True 时只对「尚无预测」的场次补算，不删已有预测（API 限额后第二天断点续跑用）。
    """
    import config
    from db import repo
    old_rate = getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", None)
    if rate_limit_sec is not None:
        config.API_FOOTBALL_RATE_LIMIT_SEC = rate_limit_sec
    try:
        dates = repo.get_distinct_match_dates(years=years, big5_only=True)
        if not dates:
            return 0, 0, 0
        total_ok = 0
        total_matches = 0
        for i, d in enumerate(dates):
            ok, n = run_repredict_for_date(d, skip_existing=skip_existing)
            total_ok += ok
            total_matches += n
            if (i + 1) % 50 == 0 or i + 1 == len(dates):
                print(f"  已处理 {i + 1}/{len(dates)} 个比赛日，累计 {total_ok}/{total_matches} 场", flush=True)
        return total_ok, total_matches, len(dates)
    finally:
        if rate_limit_sec is not None and old_rate is not None:
            config.API_FOOTBALL_RATE_LIMIT_SEC = old_rate


def cmd_repredict_for_all(args: argparse.Namespace) -> int:
    """对所有数据（或 --years 指定年份）强制重新预测，分别保存模型/API/融合结果。"""
    years = None
    if getattr(args, "years", None):
        years = [int(x.strip()) for x in str(args.years).split(",") if x.strip()]
    rate_limit = getattr(args, "rate_limit", None)
    skip_existing = getattr(args, "skip_existing", False)
    try:
        total_ok, total_matches, num_dates = run_repredict_for_all(
            years=years, rate_limit_sec=rate_limit, skip_existing=skip_existing
        )
    except Exception as e:
        print(f"执行失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    print(f"repredict-for-all：共 {num_dates} 个比赛日、{total_matches} 场，重新预测成功 {total_ok} 场。")
    print("请刷新 Web 仪表盘查看「模型预测」「API预测」「融合预测」三列。")
    return 0


def run_fill_api_predictions(rate_limit_sec: float | None = None, years: list[int] | None = None, limit: int | None = None, verbose: bool = False) -> tuple[int, int, int, int]:
    """
    仅 Mongo：对「已有预测但缺 api_predicted_result」的场次拉取 API 预测并只更新 API 字段。
    years 为 [2026] 时只处理该年比赛日的场次。limit 为 N 时最多只处理 N 场。verbose 时在首场「API 无预测」时打印该次完整响应。
    返回 (成功补全数, 待补全总数, 未匹配到 fixture 数, API 无预测数)。
    """
    import config
    from db import repo
    repo.init_db()
    if not repo.use_mongo():
        return 0, 0, 0, 0
    match_ids = repo.predictions_get_match_ids_missing_api(years=years)
    if not match_ids:
        return 0, 0, 0, 0
    if limit is not None and limit > 0:
        match_ids = match_ids[:limit]
    old_rate = getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", None)
    if rate_limit_sec is not None:
        config.API_FOOTBALL_RATE_LIMIT_SEC = rate_limit_sec
    try:
        from etl.api_football_predictions import get_fixture_id_for_match, fetch_predictions_for_fixture, parse_predictions_to_outcome
        import time
        filled = 0
        no_fixture = 0
        no_predictions = 0
        verbose_printed = False
        for i, match_id in enumerate(match_ids):
            if i > 0 and getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 0) > 0:
                time.sleep(config.API_FOOTBALL_RATE_LIMIT_SEC)
            trio = repo.predict_get_teams_and_comp(match_id)
            if not trio:
                continue
            home_name, away_name, comp_name = trio
            info = repo.predict_get_match_info(match_id)
            date_str = (info.get("match_date") or "")[:10] if info else ""
            if not date_str or len(date_str) != 10:
                continue
            try:
                fid = get_fixture_id_for_match(date_str, home_name, away_name)
                if not fid:
                    no_fixture += 1
                    continue
                result = fetch_predictions_for_fixture(fid, return_full=verbose)
                raw = result[0] if isinstance(result, tuple) else result
                full_data = result[1] if isinstance(result, tuple) else None
                if not raw:
                    no_predictions += 1
                    if verbose and full_data is not None and not verbose_printed:
                        print(f"[verbose] 首场 API 无预测 — match_id={match_id} {date_str} {home_name} vs {away_name}，predictions 响应:", file=sys.stderr)
                        print(json.dumps(full_data, indent=2, ensure_ascii=False)[:3000], file=sys.stderr)
                        verbose_printed = True
                    continue
                api_result, api_scores = parse_predictions_to_outcome(raw)
                if api_result is None:
                    no_predictions += 1
                    if verbose and full_data is not None and not verbose_printed:
                        print(f"[verbose] 首场解析无 winner — match_id={match_id} {date_str} {home_name} vs {away_name}，predictions 响应:", file=sys.stderr)
                        print(json.dumps(full_data, indent=2, ensure_ascii=False)[:3000], file=sys.stderr)
                        verbose_printed = True
                    continue
            except Exception:
                continue
            scores = [list(api_scores)] if api_scores else None
            if repo.predictions_update_api_fields(match_id, api_result, scores):
                filled += 1
            if (i + 1) % 20 == 0 or i + 1 == len(match_ids):
                print(f"  已处理 {i + 1}/{len(match_ids)} 场，补全 {filled} 场；未匹配到 fixture {no_fixture} 场，API 无预测 {no_predictions} 场", flush=True)
        return filled, len(match_ids), no_fixture, no_predictions
    finally:
        if rate_limit_sec is not None and old_rate is not None:
            config.API_FOOTBALL_RATE_LIMIT_SEC = old_rate


def cmd_fill_api_predictions(args: argparse.Namespace) -> int:
    """只对「已有预测但缺 API 预测」的场次拉取 API 并更新，不重算模型。API 限额后补全用。"""
    from db import repo
    if not repo.use_mongo():
        print("fill-api-predictions 仅在使用 MongoDB 时可用（USE_MONGO=true）。", file=sys.stderr)
        return 1
    rate_limit = getattr(args, "rate_limit", None)
    years = None
    if getattr(args, "years", None):
        years = [int(x.strip()) for x in str(args.years).split(",") if x.strip()]
    limit = getattr(args, "limit", None)
    verbose = getattr(args, "verbose", False)
    try:
        filled, total, no_fixture, no_predictions = run_fill_api_predictions(rate_limit_sec=rate_limit, years=years, limit=limit, verbose=verbose)
    except Exception as e:
        print(f"执行失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    if years:
        print(f"fill-api-predictions（仅 {years} 年）：待补全 {total} 场，成功补全 API 预测 {filled} 场。")
    else:
        print(f"fill-api-predictions：待补全 {total} 场，成功补全 API 预测 {filled} 场。")
    if no_fixture > 0 or no_predictions > 0:
        print(f"  未匹配到 API fixture（队名/日期/联赛）: {no_fixture} 场；API 无预测数据（多为未来场次）: {no_predictions} 场。")
    if filled > 0:
        print("请刷新 Web 仪表盘查看「API预测」列。")
    return 0


def cmd_predictions_api_stats(args: argparse.Namespace) -> int:
    """仅 Mongo：统计 predictions 中有/无 API 预测的数量，便于确认页面为何不显示 API 列。"""
    from db import repo
    repo.init_db()
    if not repo.use_mongo():
        print("predictions-api-stats 仅在使用 MongoDB 时可用（USE_MONGO=true）。", file=sys.stderr)
        return 1
    years = None
    if getattr(args, "years", None):
        years = [int(x.strip()) for x in str(args.years).split(",") if x.strip()]
    stats = repo.predictions_api_stats(years=years)
    if stats is None:
        return 1
    total, with_api = stats
    without_api = total - with_api
    scope = f"（仅 {years} 年）" if years else ""
    print(f"predictions 集合{scope}：共 {total} 条预测，其中 {with_api} 条有 API 预测、{without_api} 条无（待补全）。")
    if without_api > 0:
        print("  可运行 fill-api-predictions 只补 API 字段。")
    return 0


def _predict_one_jc_match(
    engine,
    m: dict,
    date_only: str,
    last_n: int = 10,
    verbose: bool = False,
    include_finished: bool = False,
    merge_teams_into_db: bool = True,
    jc_cn_to_en_file: dict | None = None,
    jc_cn_to_en_db: dict | None = None,
) -> bool:
    """对单场竞彩（list_by_jc_date 的一条）做预测并写入 jc_predictions。返回 True 表示已写入，False 表示跳过。"""
    from db.self_improvement import (
        jc_match_analysis_load,
        jc_predictions_save,
        get_jingcai_cn_to_en,
        get_jingcai_cn_to_en_from_file,
        parse_jingcai_analysis_for_predict,
    )
    from db import repo
    from engine.predict import predict_match, resolve_team_by_name, resolve_competition_by_name
    from etl.sporttery_crawl import LEAGUE_MAP

    if jc_cn_to_en_file is None:
        jc_cn_to_en_file = get_jingcai_cn_to_en_from_file()
    if jc_cn_to_en_db is None:
        jc_cn_to_en_db = get_jingcai_cn_to_en()

    home_cn = (m.get("home_cn") or m.get("home") or "").strip()
    away_cn = (m.get("away_cn") or m.get("away") or "").strip()
    league_cn = (m.get("league") or "").strip()

    if not include_finished and m.get("home_score") is not None and m.get("away_score") is not None:
        if verbose:
            print(f"  [跳过] {home_cn} vs {away_cn} | 联赛: {league_cn or '(空)'} | 原因: 已完赛")
        return (False, None)

    competition_en = LEAGUE_MAP.get(league_cn)
    comp_id = resolve_competition_by_name(engine, competition_en) if competition_en else None

    def _team_candidates(cn: str, raw_home_or_away: str, abb: str) -> list[str]:
        seen = set()
        out = []
        for x in (
            jc_cn_to_en_file.get(cn),
            jc_cn_to_en_db.get(cn),
            (raw_home_or_away or "").strip(),
            (abb or "").strip(),
            cn,
        ):
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    home_candidates = _team_candidates(home_cn, m.get("home"), m.get("homeTeamAbbEnName"))
    away_candidates = _team_candidates(away_cn, m.get("away"), m.get("awayTeamAbbEnName"))
    home_id = None
    for name in home_candidates:
        if name and (home_id := resolve_team_by_name(engine, name)) is not None:
            break
    away_id = None
    for name in away_candidates:
        if name and (away_id := resolve_team_by_name(engine, name)) is not None:
            break
    if merge_teams_into_db:
        if home_id is None and home_candidates and (best := home_candidates[0]):
            home_id = repo.ensure_team_by_name(best, name_zh=home_cn if (best != home_cn) else None)
        if away_id is None and away_candidates and (best := away_candidates[0]):
            away_id = repo.ensure_team_by_name(best, name_zh=away_cn if (best != away_cn) else None)
        if competition_en and comp_id is None:
            comp_id = repo.ensure_competition_by_name(competition_en)
    if home_id is None or away_id is None:
        if verbose:
            reason = "主队名未解析" if home_id is None else "客队名未解析"
            print(f"  [跳过] {home_cn} vs {away_cn} | 联赛: {league_cn} | 原因: {reason}")
        return (False, (home_cn, away_cn, league_cn))

    odds_for_match = None
    home_absent = None
    away_absent = None
    home_absent_key_count = 0
    away_absent_key_count = 0
    home_matches_next_7d = None
    away_matches_next_7d = None
    h2h_override = None
    xg_home = None
    xg_away = None
    home_rank = None
    away_rank = None
    home_team_stats_prior = None
    away_team_stats_prior = None
    home_coach_tenure_days = None
    away_coach_tenure_days = None
    mid = m.get("match_id")
    if mid is not None:
        analysis_doc = jc_match_analysis_load(mid)
        if analysis_doc:
            parsed = parse_jingcai_analysis_for_predict(analysis_doc, match_date=date_only)
            if parsed.get("odds"):
                odds_for_match = parsed["odds"]
            if parsed.get("home_absent"):
                home_absent = parsed["home_absent"]
            if parsed.get("away_absent"):
                away_absent = parsed["away_absent"]
            if parsed.get("home_matches_next_7d") is not None:
                home_matches_next_7d = parsed["home_matches_next_7d"]
            if parsed.get("away_matches_next_7d") is not None:
                away_matches_next_7d = parsed["away_matches_next_7d"]
            if parsed.get("home_absent_key_count") is not None:
                home_absent_key_count = int(parsed["home_absent_key_count"])
            if parsed.get("away_absent_key_count") is not None:
                away_absent_key_count = int(parsed["away_absent_key_count"])
            if parsed.get("h2h_override") and len(parsed["h2h_override"]) >= 5:
                h2h_override = tuple(parsed["h2h_override"])
            home_team_stats_prior = parsed.get("home_team_stats_prior")
            away_team_stats_prior = parsed.get("away_team_stats_prior")
            home_coach_tenure_days = parsed.get("home_coach_tenure_days")
            away_coach_tenure_days = parsed.get("away_coach_tenure_days")
    if odds_for_match is None and isinstance(m.get("odds"), dict):
        o = m["odds"]
        if o.get("spf_win") and o.get("spf_draw") and o.get("spf_loss"):
            odds_for_match = {"spf_win": str(o["spf_win"]), "spf_draw": str(o["spf_draw"]), "spf_loss": str(o["spf_loss"])}
    # 加载单场统计(xG)、积分榜排名等，与主库 repredict 一致（含 api_football_statistics 表）
    try:
        from engine.predict import _load_odds_injury_rank_for_match
        extra = _load_odds_injury_rank_for_match(
            engine, home_id, away_id, comp_id, date_only, match_id=mid
        )
        if extra.get("xg_home") is not None and extra.get("xg_away") is not None:
            xg_home, xg_away = extra["xg_home"], extra["xg_away"]
        if extra.get("home_rank") is not None and extra.get("away_rank") is not None:
            home_rank, away_rank = extra["home_rank"], extra["away_rank"]
        if home_matches_next_7d is None and extra.get("home_matches_next_7d") is not None:
            home_matches_next_7d = extra["home_matches_next_7d"]
        if away_matches_next_7d is None and extra.get("away_matches_next_7d") is not None:
            away_matches_next_7d = extra["away_matches_next_7d"]
        if odds_for_match is None and extra.get("odds"):
            odds_for_match = extra["odds"]
    except Exception:
        pass
    try:
        timeslice_overrides = config.get_timeslice_overrides(
            date_only,
            profile=getattr(config, "JC_TIMESLICE_PROFILE", "jc_2026"),
        )
    except Exception:
        timeslice_overrides = {}
    applied_cfg_backup: dict[str, object] = {}
    if isinstance(timeslice_overrides, dict) and timeslice_overrides:
        for k, v in timeslice_overrides.items():
            if hasattr(config, k):
                applied_cfg_backup[k] = getattr(config, k)
                setattr(config, k, v)
        if verbose:
            print(f"  [切片参数] {date_only} -> {timeslice_overrides}")
    try:
        result = predict_match(
            engine,
            home_id,
            away_id,
            competition_id=comp_id,
            last_n=last_n,
            match_date=date_only,
            odds=odds_for_match,
            home_absent=home_absent,
            away_absent=away_absent,
            home_absent_key_count=home_absent_key_count,
            away_absent_key_count=away_absent_key_count,
            home_rank=home_rank,
            away_rank=away_rank,
            xg_home=xg_home,
            xg_away=xg_away,
            home_matches_next_7d=home_matches_next_7d,
            away_matches_next_7d=away_matches_next_7d,
            h2h_override=h2h_override,
            home_team_stats_prior=home_team_stats_prior,
            away_team_stats_prior=away_team_stats_prior,
            home_coach_tenure_days=home_coach_tenure_days,
            away_coach_tenure_days=away_coach_tenure_days,
        )
    except Exception as e:
        if applied_cfg_backup:
            for k, old_v in applied_cfg_backup.items():
                setattr(config, k, old_v)
        if verbose:
            print(f"  [跳过] {home_cn} vs {away_cn} | 联赛: {league_cn} | 原因: 预测异常 ({e})")
        return (False, None)
    # 保存融合前的模型预测结果，供评估时对比「仅模型」vs「融合」命中率
    model_only_result = result.get("predicted_result")
    # 融合前计算「仅模型」单场推荐（胜/平/负/让胜/让平/让负），供评估单场推荐、让球对比
    try:
        from db.self_improvement import _single_rec_label_and_prob_base, _parse_handicap
        handicap = _parse_handicap(m.get("odds")) if isinstance(m.get("odds"), dict) else None
        ph, pd, pa = result.get("prob_home_win"), result.get("prob_draw"), result.get("prob_away_win")
        exp_h = result.get("exp_goals_home")
        exp_a = result.get("exp_goals_away")
        if ph is not None and pd is not None and pa is not None:
            model_rec_label, model_rec_prob = _single_rec_label_and_prob_base(
                float(ph), float(pd), float(pa), exp_h, exp_a, handicap
            )
            result["model_single_rec"] = model_rec_label
            result["model_single_rec_prob"] = round(float(model_rec_prob), 3)
    except Exception:
        pass
    # 若开启 API 预测融合且库中有该场 API 预测，则与模型概率线性融合后再写入
    if getattr(config, "PREDICT_USE_API_PREDICTIONS", False) and mid is not None:
        try:
            from db.self_improvement import jc_api_predictions_load_by_sporttery_match_id
            from engine.predict import fuse_probs
            api_pred = jc_api_predictions_load_by_sporttery_match_id(int(mid))
            if api_pred and api_pred.get("win_pct") is not None and api_pred.get("draw_pct") is not None and api_pred.get("loss_pct") is not None:
                wh = float(api_pred["win_pct"])
                wd = float(api_pred["draw_pct"])
                wa = float(api_pred["loss_pct"])
                if wh > 1 or wd > 1 or wa > 1:
                    wh, wd, wa = wh / 100.0, wd / 100.0, wa / 100.0
                total = wh + wd + wa
                if total > 0:
                    api_probs = (wh / total, wd / total, wa / total)
                    w_api = max(0.0, min(1.0, getattr(config, "PREDICT_API_PREDICTIONS_WEIGHT", 0.25)))
                    model_probs = (result["prob_home_win"], result["prob_draw"], result["prob_away_win"])
                    fused = fuse_probs(model_probs, api_probs, weight_model=1.0 - w_api)
                    result["prob_home_win"], result["prob_draw"], result["prob_away_win"] = fused[0], fused[1], fused[2]
                    if fused[0] >= fused[1] and fused[0] >= fused[2]:
                        result["predicted_result"] = "主"
                    elif fused[2] >= fused[1] and fused[2] >= fused[0]:
                        result["predicted_result"] = "客"
                    else:
                        result["predicted_result"] = "平"
        except Exception:
            pass
    if applied_cfg_backup:
        for k, old_v in applied_cfg_backup.items():
            setattr(config, k, old_v)
    if model_only_result is not None:
        result["model_predicted_result"] = model_only_result
    match_info = {
        "home": m.get("home") or m.get("home_cn"),
        "away": m.get("away") or m.get("away_cn"),
        "home_cn": m.get("home_cn"),
        "away_cn": m.get("away_cn"),
        "league": league_cn,
        "sporttery_match_id": m.get("match_id"),
        "match_id": m.get("match_id"),
        "odds": m.get("odds"),
    }
    if jc_predictions_save(date_only, match_info, result):
        if verbose:
            print(f"  [已预测] {home_cn} vs {away_cn} | 联赛: {league_cn}")
        return True
    if verbose:
        print(f"  [跳过] {home_cn} vs {away_cn} | 联赛: {league_cn} | 原因: 写入失败")
    return (False, None)


def run_predict_jc_from_self_improvement(
    date_str: str,
    last_n: int = 10,
    verbose: bool = False,
    include_finished: bool = False,
    merge_teams_into_db: bool = True,
) -> tuple[int, int, int]:
    """
    从 self_improvement_football.jingcai_match_analysis 按 jc_date 读取指定日期的场次，用 football 库的模型预测并写回 jc_predictions。
    不限于五大联赛：所有未完赛场次都会尝试预测；队名通过 data/jingcai_team_name_mapping.json 与 jingcai_teams 映射到主库 teams 解析。
    主库无对应赛事时 comp_id 传 None，仍按近期表现预测。verbose=True 时逐场打印联赛与状态。
    返回 (新增预测数, 跳过数, 总场数)。
    """
    from db.self_improvement import (
        jc_match_analysis_list_by_jc_date,
        jc_predictions_delete_by_date,
        get_jingcai_cn_to_en,
        get_jingcai_cn_to_en_from_file,
    )
    from db import repo

    date_only = (date_str or "").strip()[:10]
    if not date_only:
        return (0, 0, 0, {"resolution_rate": 0.0, "unresolved_sample": []})
    matches = jc_match_analysis_list_by_jc_date(date_only)
    if not matches:
        return (0, 0, 0, {"resolution_rate": 0.0, "unresolved_sample": []})
    # 先清空该日已有预测，再对本日全部场次重新预测，保证每次点击「运行预测」都是全量重算
    deleted = jc_predictions_delete_by_date(date_only)
    if verbose and deleted:
        print(f"  已清空该日 {deleted} 条旧预测，开始重新预测…")
    engine = repo.get_engine()
    jc_cn_to_en_file = get_jingcai_cn_to_en_from_file()
    jc_cn_to_en_db = get_jingcai_cn_to_en()
    added, skipped = 0, 0
    unresolved_sample: list[tuple[str, str, str]] = []
    for m in matches:
        res = _predict_one_jc_match(
            engine, m, date_only, last_n, verbose, include_finished, merge_teams_into_db,
            jc_cn_to_en_file, jc_cn_to_en_db,
        )
        if res is True:
            added += 1
        else:
            skipped += 1
            if isinstance(res, tuple) and len(res) == 2 and res[1] is not None:
                if len(unresolved_sample) < 10:
                    unresolved_sample.append(res[1])
    total = len(matches)
    stats = {
        "resolution_rate": added / total if total else 0.0,
        "unresolved_sample": unresolved_sample,
    }
    return (added, skipped, total, stats)


def repredict_jc_evaluable_matches() -> int:
    """
    对 self_improvement_football 中「已完赛」的竞彩场次（jingcai_match_analysis 有比分）
    用当前参数重算预测并写回 jc_predictions。主库为 self_improvement_football 时由 repredict-history 调用。
    返回重算并写回的场次数。
    """
    from db.self_improvement import jc_match_analysis_list_finished, get_jingcai_cn_to_en_from_file, get_jingcai_cn_to_en
    from db import repo

    finished = jc_match_analysis_list_finished()
    if not finished:
        return 0
    total = len(finished)
    print(f"  共 {total} 场（jingcai 已完赛），开始重算...", flush=True)
    engine = repo.get_engine()
    jc_cn_to_en_file = get_jingcai_cn_to_en_from_file()
    jc_cn_to_en_db = get_jingcai_cn_to_en()
    n = 0
    for i, (date_str, m) in enumerate(finished):
        if _predict_one_jc_match(
            engine, m, date_str, last_n=10, verbose=False, include_finished=True, merge_teams_into_db=True,
            jc_cn_to_en_file=jc_cn_to_en_file, jc_cn_to_en_db=jc_cn_to_en_db,
        ) is True:
            n += 1
        if (i + 1) % 50 == 0 or (i + 1) == total:
            print(f"  已重算 DB 预测 {n}/{total} 场", flush=True)
    return n


def cmd_predict_jc_from_self_improvement(args: argparse.Namespace) -> int:
    """从 self_improvement_football.jingcai_match_analysis 按 jc_date 读赛程，用 football 模型预测并写入 jc_predictions。需 MONGO_URI。支持单日、整年、日期区间。"""
    from datetime import datetime, timedelta
    from db.self_improvement import get_self_improvement_db, jc_match_analysis_list_dates
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    date_str = (getattr(args, "date", None) or "").strip()[:10]
    year = getattr(args, "year", None)
    years_str = (getattr(args, "years", None) or "").strip()
    dates_range = getattr(args, "dates", None)  # "from,to"
    last_n = int(getattr(args, "last_n", 10))
    verbose = getattr(args, "verbose", False)

    # 解析 --years 为年份列表（与 --year 二选一）
    years_list: list[int] | None = None
    if years_str:
        try:
            years_list = [int(y.strip()) for y in years_str.split(",") if y.strip()]
        except ValueError:
            print("--years 格式应为逗号分隔的年份，如 2021,2022,2023,2024,2025,2026。", file=sys.stderr)
            return 1
        if not years_list:
            print("--years 至少包含一个有效年份。", file=sys.stderr)
            return 1
        if year is not None:
            print("请只指定 --year 或 --years 其一，不要同时使用。", file=sys.stderr)
            return 1

    to_run: list[str] = []
    if years_list is not None:
        # 多个年份：收集这些年在 jingcai_match_analysis 中出现的所有 jc_date
        all_dates = jc_match_analysis_list_dates()
        year_set = set(years_list)
        for d in all_dates:
            day = (d.get("date") or "")[:10]
            if len(day) >= 4:
                try:
                    y = int(day[:4])
                    if y in year_set:
                        to_run.append(day)
                except ValueError:
                    pass
        to_run = sorted(set(to_run))
        if not to_run:
            print(f"jingcai_match_analysis 中无 {years_str} 任一年的数据。", file=sys.stderr)
            return 1
        print(f"按年批量：{years_str} 共 {len(to_run)} 个竞彩日。")
    elif year is not None:
        # 该年在 jingcai_match_analysis 中出现的所有 jc_date
        for d in jc_match_analysis_list_dates():
            day = (d.get("date") or "")[:10]
            if len(day) >= 4 and day.startswith(str(year)):
                to_run.append(day)
        to_run = sorted(set(to_run))
        if not to_run:
            print(f"jingcai_match_analysis 中无 {year} 年的数据。", file=sys.stderr)
            return 1
        print(f"按年批量：{year} 年共 {len(to_run)} 个竞彩日。")
    elif dates_range and "," in dates_range:
        parts = [p.strip()[:10] for p in dates_range.split(",", 1)]
        if len(parts) == 2 and len(parts[0]) == 10 and len(parts[1]) == 10:
            try:
                start = datetime.strptime(parts[0], "%Y-%m-%d")
                end = datetime.strptime(parts[1], "%Y-%m-%d")
                while start <= end:
                    to_run.append(start.strftime("%Y-%m-%d"))
                    start += timedelta(days=1)
                print(f"按日期区间：{parts[0]}～{parts[1]}，共 {len(to_run)} 天。")
            except ValueError:
                print("--dates 格式应为 YYYY-MM-DD,YYYY-MM-DD。", file=sys.stderr)
                return 1
        else:
            print("--dates 格式应为 YYYY-MM-DD,YYYY-MM-DD。", file=sys.stderr)
            return 1
    elif date_str:
        to_run = [date_str]
    else:
        print("请指定 --date YYYY-MM-DD、--year YYYY 或 --dates 起,止（如 2026-02-01,2026-02-29）。", file=sys.stderr)
        return 1

    include_finished = getattr(args, "include_finished", False)
    # 按年跑且涉及往年时，默认包含已完赛（否则几乎全部被跳过，无法回测）
    now_year = datetime.now().year
    past_years = (years_list or ([year] if year is not None else []))
    has_past = any(y < now_year for y in past_years)
    if past_years and has_past and not include_finished:
        include_finished = True
        label = years_str if years_list else str(year)
        print(f"往年（{label}）已自动包含已完赛场次，用于回测与评估。", file=sys.stderr)
    merge_teams_into_db = not getattr(args, "no_merge", False)
    total_added, total_skipped, total_matches = 0, 0, 0
    for d in to_run:
        if verbose:
            print(f"\n日期 {d} 场次（联赛 | 状态）：")
        res = run_predict_jc_from_self_improvement(
            d, last_n=last_n, verbose=verbose, include_finished=include_finished, merge_teams_into_db=merge_teams_into_db
        )
        added, skipped, total = res[0], res[1], res[2]
        total_added += added
        total_skipped += skipped
        total_matches += total
    rate = (total_added / total_matches * 100) if total_matches else 0
    print(f"\npredict-jc-from-self-improvement：共 {len(to_run)} 个竞彩日，总场次 {total_matches}，新增预测 {total_added} 条，跳过 {total_skipped} 场；队名解析率 {rate:.1f}%。")
    if total_matches == 0:
        print("  jingcai_match_analysis 中无对应日期的数据，请先拉取并保存对阵详情。")
    return 0


def cmd_jc_dates_diagnose(args: argparse.Namespace) -> int:
    """诊断 jingcai_match_analysis 中某年的竞彩日：列出 list_dates 结果与库中原始 jc_date/date/match_date 分布。"""
    from db.self_improvement import (
        get_self_improvement_db,
        jc_match_analysis_list_dates,
        jc_match_analysis_raw_date_counts,
    )
    db = get_self_improvement_db()
    if db is None:
        print("未配置 MONGO_URI。", file=sys.stderr)
        return 1
    year = getattr(args, "year", None)
    if year is None:
        print("请指定 --year YYYY。", file=sys.stderr)
        return 1
    print(f"当前库: {db.name}（由 MONGO_SELF_IMPROVEMENT_DB_NAME 指定）")
    all_dates = jc_match_analysis_list_dates()
    in_year = [d for d in all_dates if (d.get("date") or "").startswith(str(year) + "-")]
    print(f"list_dates 统计：{year} 年共 {len(in_year)} 个竞彩日")
    for d in in_year:
        print(f"  {d.get('date')}  场次数: {d.get('count', 0)}")
    raw = jc_match_analysis_raw_date_counts(year=year)
    if not raw:
        print(f"\n库中无 {year} 年相关日期字段（jc_date/date/match_date）。")
        return 0
    print(f"\n库中原始日期字段（仅 {year} 年）：")
    for key in ("jc_date", "date", "match_date"):
        if key not in raw or not raw[key]:
            continue
        print(f"  {key}:")
        for val, cnt in raw[key][:20]:
            print(f"    {val!r}  ->  {cnt} 条")
        if len(raw[key]) > 20:
            print(f"    ... 共 {len(raw[key])} 种取值")
    return 0


def cmd_list_jc_matches(args: argparse.Namespace) -> int:
    """列出指定日期 jingcai_match_analysis 中的场次，并标注是否五大联赛（英超/西甲/意甲/德甲/法甲）。不执行预测。"""
    from db.self_improvement import get_self_improvement_db, jc_match_analysis_list_by_jc_date
    from etl.sporttery_crawl import BIG5_LEAGUES
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    date_str = (getattr(args, "date", None) or "").strip()[:10]
    if not date_str:
        print("请指定 --date YYYY-MM-DD。", file=sys.stderr)
        return 1
    matches = jc_match_analysis_list_by_jc_date(date_str)
    if not matches:
        print(f"日期 {date_str} 在 jingcai_match_analysis 中无场次。")
        return 0
    big5_count = sum(1 for m in matches if (m.get("league") or "").strip() in BIG5_LEAGUES)
    print(f"日期 {date_str} 共 {len(matches)} 场，其中五大联赛 {big5_count} 场（英超/西甲/意甲/德甲/法甲）\n")
    print(f"  {'主队':<12} {'客队':<12} {'联赛':<8} 五大联赛")
    print("  " + "-" * 40)
    for m in matches:
        home = (m.get("home_cn") or m.get("home") or "-")[:12]
        away = (m.get("away_cn") or m.get("away") or "-")[:12]
        league = (m.get("league") or "-").strip()[:8]
        is_big5 = "是" if league in BIG5_LEAGUES else "否"
        print(f"  {home:<12} {away:<12} {league:<8} {is_big5}")
    return 0


def cmd_recommend_bet_slip(args: argparse.Namespace) -> int:
    """根据指定日期的竞彩场次与 jc_predictions 模型预测，输出下注单推荐（优化逻辑：概率差距、模型置信度、联赛分散）。"""
    from datetime import date as date_type
    from db.self_improvement import get_self_improvement_db, jc_recommend_bet_slip, jc_match_analysis_list_by_jc_date
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    date_str = (getattr(args, "date", None) or "").strip()[:10] or date_type.today().isoformat()
    import config as _cfg
    min_prob = getattr(args, "min_prob", None)
    if min_prob is None:
        min_prob = getattr(_cfg, "JC_REC_DEFAULT_MIN_PROB", 0.60)
    min_spread = getattr(args, "min_spread", 0.05)
    top = getattr(args, "top", None)
    require_high = not getattr(args, "no_require_high_confidence", False)
    big5_only = getattr(args, "big5_only", False)
    max_per_league = getattr(args, "max_per_league", 2)
    if max_per_league is not None and max_per_league <= 0:
        max_per_league = None
    matches = jc_match_analysis_list_by_jc_date(date_str)
    recommended = jc_recommend_bet_slip(
        date_str,
        min_prob=min_prob,
        min_spread=min_spread,
        top=top,
        require_high_confidence=require_high,
        big5_only=big5_only,
        max_per_league=max_per_league,
    )
    if not matches:
        print(f"日期 {date_str} 在 jingcai_match_analysis 中无场次，请先拉取竞彩列表（如 jc-to-self-improvement --date {date_str}）。", file=sys.stderr)
        return 1
    if not recommended:
        print(f"日期 {date_str} 共 {len(matches)} 场，暂无满足条件的推荐（概率≥{min_prob*100:.0f}%、与第二选项差≥{min_spread*100:.0f}%、模型高置信；默认目标命中率约 70%）。")
        print("可尝试：--no-require-high-confidence 或降低 --min-prob / --min-spread；或先运行预测。")
        return 0
    high = sum(1 for r in recommended if r.get("tier") == "high")
    print(f"【今日推荐】{date_str}（共 {len(matches)} 场，推荐 {len(recommended)} 场：首选 {high} / 可考虑 {len(recommended) - high}，与单场推荐一致取概率最高项）\n")
    print(f"  {'类型':<6} {'主队':<12} {'客队':<12} {'联赛':<6} 推荐      概率  置信")
    print("  " + "-" * 62)
    for r in recommended:
        tier = "首选" if r.get("tier") == "high" else "可考虑"
        home = (r.get("home_cn") or "-")[:12]
        away = (r.get("away_cn") or "-")[:12]
        league = (r.get("league") or "-").strip()[:6]
        rec = r.get("recommended", "-")
        hc = r.get("handicap")
        if hc is not None and rec in ("让胜", "让平", "让负"):
            rec = f"{rec}(让{hc})"
        prob = r.get("prob", 0)
        conf = r.get("confidence", "")
        print(f"  {tier:<6} {home:<12} {away:<12} {league:<6} {rec:<10} {prob*100:.0f}%  {conf}")
    return 0


def cmd_bet_record_add(args: argparse.Namespace) -> int:
    """新增一条投注花费/收益记录。"""
    from db import repo
    repo.init_db()
    try:
        rid = repo.bet_record_add(
            record_date=args.date,
            amount_spent=float(args.spend),
            amount_returned=float(getattr(args, "amount_returned", 0) or 0),
            note=getattr(args, "note", None),
        )
        print(f"已添加记录 id={rid}，日期={args.date}，花费={args.spend} 元，回报={getattr(args, 'amount_returned', 0)} 元。")
        return 0
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1


def cmd_bet_record_list(args: argparse.Namespace) -> int:
    """列出投注记录。"""
    from db import repo
    repo.init_db()
    rows = repo.bet_record_list(
        date_from=getattr(args, "date_from", None),
        date_to=getattr(args, "date_to", None),
    )
    if not rows:
        print("暂无记录。")
        return 0
    print(f"  {'id':<6} {'日期':<12} {'花费(元)':<12} {'收益(元)':<12} {'盈亏(元)':<10} 备注")
    print("  " + "-" * 62)
    for r in rows:
        profit = r["amount_returned"] - r["amount_spent"]
        note = (r.get("note") or "")[:20]
        print(f"  {r['id']:<6} {r['record_date']:<12} {r['amount_spent']:<12.2f} {r['amount_returned']:<12.2f} {profit:<+10.2f}  {note}")
    return 0


def cmd_bet_record_stats(args: argparse.Namespace) -> int:
    """统计总花费、总收益、盈亏。"""
    from db import repo
    repo.init_db()
    s = repo.bet_record_stats(
        date_from=getattr(args, "date_from", None),
        date_to=getattr(args, "date_to", None),
    )
    print("【投注统计】")
    if s["count"] == 0:
        print("  暂无记录。")
        return 0
    print(f"  记录条数：{s['count']}")
    print(f"  总花费：{s['total_spent']:.2f} 元")
    print(f"  总收益：{s['total_returned']:.2f} 元")
    print(f"  盈亏：{s['profit']:+.2f} 元")
    return 0


def cmd_jc_teams_sync(args: argparse.Namespace) -> int:
    """从 jingcai_match_analysis / jingcai_match_anlysis 整理主客队队名到 jingcai_teams（去重）。"""
    from db.self_improvement import jc_teams_sync_from_analysis
    result = jc_teams_sync_from_analysis()
    if result.get("message"):
        print(result["message"], file=sys.stderr)
        return 1
    source = result.get("source") or "jingcai_match_analysis"
    print(f"已从 {source} 整理队名到 jingcai_teams：扫描 {result['scanned_docs']} 条对阵，去重后写入 {result['total_teams']} 支队伍。")
    return 0


def cmd_jc_teams_sync_to_main(args: argparse.Namespace) -> int:
    """将 jingcai_teams 中有 name_en 的队伍同步到主库 teams 表（按需插入，已存在则跳过）。"""
    from db.self_improvement import get_self_improvement_db, jc_teams_sync_to_main_teams
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    result = jc_teams_sync_to_main_teams()
    if result.get("message"):
        print(result["message"])
    return 0


def cmd_jc_teams_list(args: argparse.Namespace) -> int:
    """列出 jingcai_teams 表中的全部队名（name_cn, name_en）。"""
    from db.self_improvement import get_self_improvement_db, jc_teams_list
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    teams = jc_teams_list()
    if not teams:
        print("jingcai_teams 表为空，请先执行 jc-teams-sync 从 jingcai_match_analysis 整理。")
        return 0
    print(f"共 {len(teams)} 支队伍（name_cn | name_en）\n  {'中文名':<16} 英文名")
    print("  " + "-" * 50)
    for t in teams:
        cn = (t.get("name_cn") or "-")[:16]
        en = (t.get("name_en") or "-")[:28]
        print(f"  {cn:<16} {en}")
    return 0


def cmd_jc_teams_check_all(args: argparse.Namespace) -> int:
    """检查 jingcai_match_analysis 全表中所有队伍是否都在 jingcai_teams；可选先执行同步补充。"""
    from db.self_improvement import (
        get_self_improvement_db,
        jc_teams_sync_from_analysis,
        jc_teams_missing_all,
    )
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    do_sync = getattr(args, "sync", False)
    if do_sync:
        result = jc_teams_sync_from_analysis()
        if result.get("message"):
            print(result["message"], file=sys.stderr)
            return 1
        source = result.get("source") or "jingcai_match_analysis"
        print(f"已从 {source} 回填 jingcai_teams：扫描 {result['scanned_docs']} 条对阵，去重后新增 {result['written']} 支队伍（共 {result['total_teams']} 支）。")
    check = jc_teams_missing_all()
    print(check.get("summary", ""))
    missing = check.get("missing") or []
    if missing:
        print(f"\n尚未在 jingcai_teams 中的队伍（共 {len(missing)} 支）：")
        for name in missing[:200]:
            print(f"  {name}")
        if len(missing) > 200:
            print(f"  ... 共 {len(missing)} 支")
        print("\n补充方式：执行 jc-teams-sync 将上述队伍写入 jingcai_teams（仅插入缺失，不覆盖已有）。")
    else:
        print("\n分析表中的队伍均已存在于 jingcai_teams。")
    return 0


def cmd_jc_teams_check_year(args: argparse.Namespace) -> int:
    """先可选执行 jingcai_teams 回填，再检查指定年份在 jingcai_match_analysis 中的队伍是否都在 jingcai_teams。"""
    from db.self_improvement import (
        get_self_improvement_db,
        jc_teams_sync_from_analysis,
        jc_teams_missing_for_year,
    )
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    year = getattr(args, "year", 2024)
    do_sync = getattr(args, "sync", False)
    if do_sync:
        result = jc_teams_sync_from_analysis()
        if result.get("message"):
            print(result["message"], file=sys.stderr)
            return 1
        source = result.get("source") or "jingcai_match_analysis"
        print(f"已从 {source} 回填 jingcai_teams：扫描 {result['scanned_docs']} 条对阵，去重后新增 {result['written']} 支队伍（共 {result['total_teams']} 支）。")
    check = jc_teams_missing_for_year(year)
    print(check.get("summary", ""))
    missing = check.get("missing") or []
    if missing:
        print(f"\n{year} 年尚未在 jingcai_teams 中的队伍（共 {len(missing)} 支）：")
        for name in missing[:100]:
            print(f"  {name}")
        if len(missing) > 100:
            print(f"  ... 共 {len(missing)} 支")
    else:
        print(f"\n{year} 年分析表中的队伍均已存在于 jingcai_teams。")
    return 0


def cmd_jc_teams_map_api_football(args: argparse.Namespace) -> int:
    """将 jingcai_teams 中的中文名映射为与主库 football（API Football）一致的英文名，写回 name_en。"""
    from db.self_improvement import get_self_improvement_db, jc_teams_map_to_api_football
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    use_api = not getattr(args, "api_search_false", False)
    rate = getattr(args, "rate_limit", 0.25)
    only_missing = not getattr(args, "process_all_teams", False)
    result = jc_teams_map_to_api_football(use_api_search=use_api, api_search_rate_limit_sec=rate, only_missing_team_id=only_missing)
    if result.get("message"):
        print(result["message"], file=sys.stderr)
        return 1
    parts = [f"表中已有 {result.get('from_existing', 0)}", f"主库精确 {result['from_db']}", f"竞彩映射表 {result['from_jingcai']}", f"前缀匹配 {result['from_prefix']}", f"中文映射反查 {result['from_mapping']}"]
    if result.get("from_override", 0) > 0:
        parts.append(f"手动 id 覆盖 {result['from_override']}")
    if result.get("from_api_search", 0) > 0:
        parts.append(f"API 按名查询补全 id {result['from_api_search']}")
    print(f"已映射 jingcai_teams 英文名与 API Football 一致：更新 {result['updated']} 条（{', '.join(parts)}）。")
    still = result.get("still_no_team_id") or []
    if still:
        print(f"有英文名但未查到 api_football_team_id 的 {len(still)} 支（可到 API-Football 官网查 id 后写入 data/jingcai_team_api_id_override.json，或改 name_en）：")
        for name_cn, name_en in still[:40]:
            print(f"  {name_cn} -> {name_en}")
        if len(still) > 40:
            print(f"  ... 共 {len(still)} 条")
    if result.get("not_found"):
        print(f"未匹配到英文名的 {result['total_not_found']} 支队伍（可补全主库 teams.name_zh、data/jingcai_team_name_mapping.json 或 data/team_name_zh_mapping.json）：")
        for name in result["not_found"][:30]:
            print(f"  {name}")
        if result["total_not_found"] > 30:
            print(f"  ... 共 {result['total_not_found']} 支")
        # 写入完整列表到文件，便于批量补全映射
        _root = __import__("pathlib").Path(__file__).resolve().parent
        unmapped_file = _root / "data" / "jingcai_unmapped_teams.txt"
        try:
            with open(unmapped_file, "w", encoding="utf-8") as f:
                f.write("\n".join(result["not_found"]))
            print(f"完整未匹配名单已写入: {unmapped_file}", file=sys.stderr)
        except Exception:
            pass
    return 0


def cmd_jc_match_analysis_backfill_en(args: argparse.Namespace) -> int:
    """将 jingcai_teams 中已整理的英文名回填到 jingcai_match_analysis 表（home_en/away_en）。"""
    from db.self_improvement import get_self_improvement_db, jc_match_analysis_backfill_english_names
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    overwrite = getattr(args, "overwrite", False)
    year = getattr(args, "year", None)
    result = jc_match_analysis_backfill_english_names(overwrite=overwrite, year=year)
    if result.get("message"):
        print(result["message"], file=sys.stderr)
        return 1
    print(f"已回填 jingcai_match_analysis 英文名：更新 {result['updated']} 条，跳过 {result['skipped']} 条，共 {result['total']} 条。")
    return 0


def cmd_jc_match_analysis_backfill_fixture_id(args: argparse.Namespace) -> int:
    """将 API Football 的 fixture_id 批量回填到 jingcai_match_analysis（不限于五大联赛）。默认先执行英文名回填再回填 fixture_id。"""
    from db.self_improvement import (
        get_self_improvement_db,
        jc_match_analysis_backfill_english_names,
        jc_match_analysis_backfill_fixture_id as backfill_fixture_id,
    )
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    rate = getattr(args, "rate_limit", 1.0)
    overwrite = getattr(args, "overwrite", False)
    no_auto_backfill_en = getattr(args, "no_auto_backfill_en", False)
    year = getattr(args, "year", None)
    date = getattr(args, "date", None)
    date_str = (date or "").strip()[:10] if date else None
    print("jc-match-analysis-backfill-fixture-id: 开始…", file=sys.stderr)
    sys.stderr.flush()
    if not no_auto_backfill_en:
        try:
            en_result = jc_match_analysis_backfill_english_names()
            if en_result.get("updated", 0) > 0:
                print(f"已先回填英文名：更新 {en_result['updated']} 条（来自 jingcai_teams 映射）。", file=sys.stderr)
        except Exception as e:
            print(f"英文名回填跳过或失败: {e}", file=sys.stderr)
    year_hint = f"（年份 {year}）" if year else ""
    print(f"正在回填 fixture_id{year_hint}… 正在扫描库并按日拉取 API，请稍候（可能需数分钟）。", file=sys.stderr)
    sys.stderr.flush()
    result = backfill_fixture_id(rate_limit_sec=rate, overwrite_existing=overwrite, year=year, date=date_str)
    if result.get("message"):
        print(result["message"], file=sys.stderr)
        return 1
    no_en = result.get("no_en", 0)
    no_in_api = result.get("no_in_api", 0)
    print("--- fixture_id 回填完成 ---", file=sys.stderr)
    days_empty = result.get("days_empty_api", 0)
    print(f"已回填 jingcai_match_analysis fixture_id：更新 {result['updated']} 条，跳过已有 {result['skipped']} 条，未匹配 {result['no_match']} 条（其中缺英文名 {no_en} 条、API 无匹配 {no_in_api} 条），共 {result['total']} 条。")
    if days_empty > 0:
        print(f"提示：有 {days_empty} 天 API 返回 0 场（时区已用 Asia/Shanghai；若仍大量为空请确认套餐是否含「按日期全联赛」fixtures）。", file=sys.stderr)
    if result["updated"] == 0 and result["no_match"] > 0:
        print("说明：本次 0 条更新 = 未匹配的场次多为「缺英文名」或「该日 API 赛程中无对应场次」（联赛/时区/队名不一致）。下面示例可帮助排查。", file=sys.stderr)
    if no_en > 0:
        print("提示：缺英文名的场次可先执行 jc-teams-map-api-football 补全 jingcai_teams，再重跑本命令（或先 jc-match-analysis-backfill-en）。", file=sys.stderr)
    for jc_date, h, a in result.get("sample_no_en", [])[:5]:
        print(f"  缺英文名示例: jc_date={jc_date} {h} vs {a}", file=sys.stderr)
    for jc_date, he, ae, _ in result.get("sample_no_in_api", [])[:5]:
        print(f"  API无匹配示例: jc_date={jc_date} {he} vs {ae}", file=sys.stderr)
    return 0


def cmd_jc_priority_backfill(args: argparse.Namespace) -> int:
    """优先回填：按顺序执行 (1) fixture_id 回填 (2) 单场统计 (3) 阵容 (4) 事件 (5) 球员单场统计。用于一次性补全竞彩相关数据。--daily 时仅处理当年、当天+前一天。"""
    from datetime import date as date_type, timedelta

    daily = getattr(args, "daily", False)
    years_arg = getattr(args, "years", None) or ""
    date_from = (getattr(args, "date_from", None) or "").strip()[:10] or None
    date_to = (getattr(args, "date_to", None) or "").strip()[:10] or None
    rate = getattr(args, "rate_limit", 0.5)
    skip_fixture_id = getattr(args, "skip_fixture_id", False)
    skip_statistics = getattr(args, "skip_statistics", False)
    skip_lineups = getattr(args, "skip_lineups", False)
    skip_events = getattr(args, "skip_events", False)
    skip_players_stats = getattr(args, "skip_players_stats", False)

    if daily:
        today = date_type.today()
        yesterday = today - timedelta(days=1)
        date_to = today.isoformat()
        date_from = yesterday.isoformat()
        years = [today.year]
        years_str = str(today.year)
        print("========== 优先回填（--daily：仅当年、当天+前一天）==========", flush=True)
    else:
        if not years_arg.strip() and not date_from and not date_to:
            print("请指定 --daily（仅当天+前一天）或 --years（如 2021,2022,...）或 --date-from/--date-to。", file=sys.stderr)
            return 1
        years = []
        if years_arg.strip():
            for s in years_arg.strip().split(","):
                s = s.strip()
                if s.isdigit():
                    years.append(int(s))
        if not years and date_from and date_to:
            try:
                y1 = int(date_from[:4])
                y2 = int(date_to[:4])
                years = list(range(min(y1, y2), max(y1, y2) + 1))
            except Exception:
                pass
        if not years:
            years = list(range(2021, 2027))  # 默认 2021～2026
        years_str = ",".join(str(y) for y in sorted(set(years)))
        date_from = date_from or (f"{min(years)}-01-01" if years else None)
        date_to = date_to or (f"{max(years)}-12-31" if years else None)
        print("========== 优先回填（顺序：fixture_id → 单场统计 → 阵容 → 事件 → 球员统计）==========", flush=True)

    print(f"年份: {years_str}  日期范围: {date_from}～{date_to}  rate_limit={rate}s", flush=True)

    from db.self_improvement import get_self_improvement_db
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1

    # (1) fixture_id 回填
    if not skip_fixture_id:
        print("\n--- [1/5] fixture_id 回填 ---", flush=True)
        from db.self_improvement import jc_match_analysis_backfill_english_names, jc_match_analysis_backfill_fixture_id
        try:
            en_result = jc_match_analysis_backfill_english_names()
            if en_result.get("updated", 0) > 0:
                print(f"已回填英文名：更新 {en_result['updated']} 条。", flush=True)
        except Exception as e:
            print(f"英文名回填跳过: {e}", flush=True)
        if daily:
            for d in (date_from, date_to):
                if d:
                    r = jc_match_analysis_backfill_fixture_id(rate_limit_sec=rate, overwrite_existing=False, year=None, date=d)
                    print(f"  {d}: 更新 {r.get('updated', 0)} 条, 跳过 {r.get('skipped', 0)} 条, 未匹配 {r.get('no_match', 0)} 条", flush=True)
        else:
            for y in sorted(set(years)):
                r = jc_match_analysis_backfill_fixture_id(rate_limit_sec=rate, overwrite_existing=False, year=y, date=None)
                print(f"  {y} 年: 更新 {r.get('updated', 0)} 条, 跳过 {r.get('skipped', 0)} 条, 未匹配 {r.get('no_match', 0)} 条", flush=True)
    else:
        print("\n--- [1/5] fixture_id 回填（已跳过）---", flush=True)

    # (2) 单场统计（--daily 时仅拉当天+前一天的场次，否则按年）
    if not skip_statistics:
        print("\n--- [2/5] 单场统计 (statistics-api) ---", flush=True)
        if daily:
            from db.self_improvement import (
                lineups_list_jingcai_to_fetch,
                load_api_football_statistics_from_db,
                save_api_football_statistics_bulk,
            )
            from etl.api_football_statistics import fetch_statistics_for_fixture
            import time as _time
            pairs = lineups_list_jingcai_to_fetch(date_from=date_from, date_to=date_to, skip_existing=False, limit=None)
            fids = [fid for _, fid in pairs]
            existing = load_api_football_statistics_from_db(fids)
            missing = [f for f in fids if f not in existing]
            by_fixture = {}
            for fid in missing:
                try:
                    rows = fetch_statistics_for_fixture(fid, rate_limit_sec=rate)
                    if rows:
                        by_fixture[fid] = rows
                    _time.sleep(rate)
                except Exception:
                    pass
            n = save_api_football_statistics_bulk(by_fixture) if by_fixture else 0
            print(f"  {date_from}～{date_to}: 共 {len(fids)} 场，缺 {len(missing)} 场，本次写入 {n} 场", flush=True)
        else:
            ns_stats = argparse.Namespace(
                jingcai_years=years_str,
                merge=True,
                rate_limit=rate,
                save_batch=50,
                resume=True,
                dry_run=False,
            )
            ret = cmd_statistics_api(ns_stats)
            if ret != 0:
                print("单场统计步骤返回非 0，继续后续步骤。", flush=True)
    else:
        print("\n--- [2/5] 单场统计（已跳过）---", flush=True)

    # (3) 阵容
    if not skip_lineups:
        print("\n--- [3/5] 阵容 (lineups-by-jingcai) ---", flush=True)
        ns_lu = argparse.Namespace(
            years=years_str,
            date_from=date_from,
            date_to=date_to,
            skip_existing=True,
            limit=None,
            dry_run=False,
            rate_limit=rate,
        )
        ret = cmd_lineups_by_jingcai(ns_lu)
        if ret != 0:
            print("阵容步骤返回非 0，继续后续步骤。", flush=True)
    else:
        print("\n--- [3/5] 阵容（已跳过）---", flush=True)

    # (4) 事件
    if not skip_events:
        print("\n--- [4/5] 单场事件 (events-by-jingcai) ---", flush=True)
        ns_ev = argparse.Namespace(
            years=years_str,
            date_from=date_from,
            date_to=date_to,
            skip_existing=True,
            limit=None,
            dry_run=False,
            rate_limit=rate,
        )
        ret = cmd_events_by_jingcai(ns_ev)
        if ret != 0:
            print("事件步骤返回非 0，继续后续步骤。", flush=True)
    else:
        print("\n--- [4/5] 事件（已跳过）---", flush=True)

    # (5) 球员单场统计
    if not skip_players_stats:
        print("\n--- [5/5] 球员单场统计 (players-stats-by-jingcai) ---", flush=True)
        ns_ps = argparse.Namespace(
            years=years_str,
            date_from=date_from,
            date_to=date_to,
            skip_existing=True,
            limit=None,
            dry_run=False,
            rate_limit=rate,
        )
        ret = cmd_players_stats_by_jingcai(ns_ps)
        if ret != 0:
            print("球员统计步骤返回非 0。", flush=True)
    else:
        print("\n--- [5/5] 球员单场统计（已跳过）---", flush=True)

    print("\n========== 优先回填流程结束 ==========", flush=True)
    return 0


def cmd_jc_match_analysis_fixture_id_handling(args: argparse.Namespace) -> int:
    """查看/导出 jingcai_match_analysis 的 fixture_id 匹配统计与未匹配列表。"""
    from db.self_improvement import (
        get_self_improvement_db,
        jc_match_analysis_fixture_id_stats,
        jc_match_analysis_unmatched_diagnose,
        jc_match_analysis_unmatched_list,
    )
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    years = getattr(args, "year", None)
    year_list = [years] if years is not None else None
    if getattr(args, "stats", False):
        stats = jc_match_analysis_fixture_id_stats(years=year_list)
        print("--- jingcai_match_analysis fixture_id 统计 ---")
        print(f"合计: 已匹配 {stats['total_matched']} 条, 未匹配 {stats['total_unmatched']} 条, 共 {stats['total']} 条")
        for y, b in (stats.get("by_year") or {}).items():
            print(f"  {y}: 已匹配 {b['matched']}, 未匹配 {b['unmatched']}, 共 {b['total']}")
        if stats["total_unmatched"] > 0:
            diag = jc_match_analysis_unmatched_diagnose(years=year_list, limit=9999)
            print("--- 未匹配原因（诊断）---")
            print(f"  缺英文名（需补 jingcai_teams / 映射）: {diag['no_en']} 条")
            print(f"  有英文名但 API 无匹配（联赛/日期/队名差异）: {diag['has_en']} 条")
            if diag.get("sample_no_en"):
                print("  缺英文名示例（前几条）:")
                for r in diag["sample_no_en"][:5]:
                    print(f"    {r.get('jc_date')} {r.get('home_cn')} vs {r.get('away_cn')} [{r.get('league')}]")
            if diag.get("sample_has_en"):
                print("  有英文名未匹配示例（前几条）:")
                for r in diag["sample_has_en"][:5]:
                    print(f"    {r.get('jc_date')} {r.get('home_en')} vs {r.get('away_en')} [{r.get('league')}]")
        return 0
    limit = getattr(args, "limit", 500)
    unmatched = jc_match_analysis_unmatched_list(years=year_list, limit=limit)
    export_path = getattr(args, "export", None)
    if export_path:
        import csv
        with open(export_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["sporttery_match_id", "jc_date", "match_date", "home_cn", "away_cn", "home_en", "away_en", "league"])
            for r in unmatched:
                w.writerow([
                    r.get("sporttery_match_id"),
                    r.get("jc_date", ""),
                    r.get("match_date", ""),
                    r.get("home_cn", ""),
                    r.get("away_cn", ""),
                    r.get("home_en", ""),
                    r.get("away_en", ""),
                    r.get("league", ""),
                ])
        print(f"已导出 {len(unmatched)} 条未匹配记录到 {export_path}", file=sys.stderr)
        return 0
    print(f"未匹配 api_football_fixture_id 的场次（共 {len(unmatched)} 条，limit={limit}）")
    for r in unmatched[:50]:
        mid = r.get("sporttery_match_id")
        jc = r.get("jc_date", "")
        home = r.get("home_cn") or r.get("home_en") or ""
        away = r.get("away_cn") or r.get("away_en") or ""
        league = r.get("league", "")
        print(f"  mid={mid} jc_date={jc} {home} vs {away} [{league}]")
    if len(unmatched) > 50:
        print(f"  ... 仅显示前 50 条，完整列表请用 --export out.csv 导出")
    return 0


def cmd_jc_match_analysis_backfill_future_matches(args: argparse.Namespace) -> int:
    """对 jingcai_match_analysis 中缺少 future_matches 的文档，仅拉取体彩 getFutureMatchesV1 并回填（不重拉整条详情）。"""
    print("开始回填 jingcai_match_analysis 未来赛事（仅拉取 getFutureMatchesV1）…", flush=True)
    from db.self_improvement import get_self_improvement_db, jc_match_analysis_backfill_future_matches as backfill_future
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    rate = getattr(args, "rate_limit", 1.0)
    overwrite = getattr(args, "overwrite", False)
    year = getattr(args, "year", None)
    date = getattr(args, "date", None)
    limit = getattr(args, "limit", None)
    if year is not None:
        print(f"筛选年份: {year}", flush=True)
    if date:
        print(f"筛选竞彩日: {date}", flush=True)

    def on_progress(current: int, total: int, updated: int, failed: int) -> None:
        if current == 1 and total > 0:
            print(f"共 {total} 条待回填，开始请求体彩 API（每场间隔 {rate} 秒）…", flush=True)
        print(f"  已处理 {current}/{total} 条，更新 {updated} 条，失败 {failed} 条", flush=True)

    result = backfill_future(
        rate_limit_sec=rate,
        overwrite_existing=overwrite,
        year=year,
        date=(date or "").strip()[:10] or None,
        limit=limit,
        progress_callback=on_progress,
    )
    if result.get("message"):
        print(result["message"], file=sys.stderr)
        return 1
    total = result["total"]
    if total == 0:
        print("无待回填文档（可能均已含 future_matches，或 --year/--date 过滤后为 0）。", flush=True)
    else:
        print(
            f"已回填 jingcai_match_analysis 未来赛事：更新 {result['updated']} 条，失败 {result['failed']} 条，"
            f"跳过 {result['skipped']} 条，共处理 {total} 条。",
            flush=True,
        )
    return 0


def cmd_jc_api_predictions_sync(args: argparse.Namespace) -> int:
    """从 jingcai_match_analysis 中有 fixture_id 的场次拉取 API Football 预测并写入 jc_api_predictions。"""
    from db.self_improvement import get_self_improvement_db, jc_api_predictions_sync_from_analysis
    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    rate = getattr(args, "rate_limit", 1.0)
    year = getattr(args, "year", None)
    date = getattr(args, "date", None)
    date_str = (date or "").strip()[:10] if date else None
    overwrite = getattr(args, "overwrite", False)
    workers = getattr(args, "workers", 1)
    result = jc_api_predictions_sync_from_analysis(
        rate_limit_sec=rate,
        year=year,
        date=date_str,
        overwrite_existing=overwrite,
        workers=workers,
    )
    if result.get("message"):
        print(result["message"], file=sys.stderr)
        return 1
    print(
        f"已同步 API Football 预测到 jc_api_predictions：更新 {result['updated']} 条，跳过已有 {result['skipped']} 条，"
        f"API 无数据 {result['api_no_data']} 条，共 {result['total']} 条。"
    )
    return 0


def cmd_jc_api_predictions_dump_raw(args: argparse.Namespace) -> int:
    """打印一条 jc_api_predictions 的 raw，用于确认 API 是否返回比分等结构。"""
    import json
    from bson import ObjectId
    from db.self_improvement import get_self_improvement_db, COLLECTION_JC_API_PREDICTIONS
    db = get_self_improvement_db()
    if db is None:
        print("未配置 MONGO_URI。", file=sys.stderr)
        return 1
    coll = db[COLLECTION_JC_API_PREDICTIONS]
    fixture = getattr(args, "fixture", None)
    if fixture is not None:
        doc = coll.find_one({"api_football_fixture_id": int(fixture)})
    else:
        doc = coll.find_one({})
    if not doc:
        print("未找到任何 jc_api_predictions 记录。", file=sys.stderr)
        return 1
    raw = doc.get("raw") or {}
    # 便于 JSON 序列化
    def _sanitize(obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(x) for x in obj]
        return obj
    print("--- raw 结构（用于确认是否有 goals/比分）---")
    print(json.dumps(_sanitize(raw), ensure_ascii=False, indent=2)[:8000])
    if len(json.dumps(raw)) > 8000:
        print("\n... (已截断)")
    return 0


def cmd_jc_eval_compare(args: argparse.Namespace) -> int:
    """对比两份 jc 评估基线（备份 vs 当前），输出各指标变化与提升/下降。"""
    import json as _json
    from pathlib import Path
    before_path = getattr(args, "before", None) or ""
    after_path = getattr(args, "after", None) or ""
    if not before_path or not after_path:
        print("请同时指定 --before 与 --after 基线文件路径。", file=sys.stderr)
        return 1
    for p, name in [(before_path, "before"), (after_path, "after")]:
        if not Path(p).exists():
            print("文件不存在: %s (%s)" % (p, name), file=sys.stderr)
            return 1
    try:
        b = _json.loads(Path(before_path).read_text(encoding="utf-8"))
        a = _json.loads(Path(after_path).read_text(encoding="utf-8"))
    except Exception as e:
        print("读取基线 JSON 失败: %s" % e, file=sys.stderr)
        return 1
    def _acc(d, key):
        v = d.get(key)
        return (v * 100.0) if isinstance(v, (int, float)) and 0 <= v <= 1 else (float(v) if v is not None else None)
    def _diff(va, vb):
        if va is None or vb is None:
            return None
        return round(va - vb, 2)
    def _row(label, key_before, key_after, fmt="%.1f%%"):
        vb = _acc(b, key_before) if key_before else b.get(key_before)
        va = _acc(a, key_after) if key_after else a.get(key_after)
        if vb is None and va is None:
            return None
        vb = vb if vb is not None else (b.get(key_before) if key_before else None)
        va = va if va is not None else (a.get(key_after) if key_after else None)
        if isinstance(vb, (int, float)) and isinstance(va, (int, float)):
            if 0 <= vb <= 1 and 0 <= va <= 1:
                vb, va = vb * 100, va * 100
            d = _diff(va, vb)
            trend = "提升" if d and d > 0 else ("下降" if d and d < 0 else "持平")
            return (label, fmt % vb if vb is not None else "-", fmt % va if va is not None else "-", ("%+.1f" % d) if d is not None else "-", trend)
        return (label, str(vb) if vb is not None else "-", str(va) if va is not None else "-", "-", "-")
    rows = []
    nb, na = b.get("n"), a.get("n")
    dn = (na or 0) - (nb or 0)
    rows.append(("场次 n", str(nb) if nb is not None else "-", str(na) if na is not None else "-", str(dn) if dn else "0", "增加" if dn > 0 else "减少" if dn < 0 else "持平"))
    rows.append(_row("胜平负命中率", "outcome_accuracy", "outcome_accuracy"))
    rows.append(_row("单场推荐命中率", "single_rec_accuracy", "single_rec_accuracy"))
    rows.append(_row("精确比分命中率", "exact_score_accuracy", "exact_score_accuracy"))
    rows.append(_row("高置信(≥45%)命中率", "outcome_accuracy_high_conf", "outcome_accuracy_high_conf"))
    rows.append(_row("让球-赔率命中率", "rq_accuracy_odds", "rq_accuracy_odds"))
    rows.append(_row("让球-模型命中率", "rq_accuracy_model", "rq_accuracy_model"))
    rows.append(_row("让球-jc_rqspf命中率", "rq_accuracy_jc_rqspf", "rq_accuracy_jc_rqspf"))
    rows.append(_row("扩展-zjqs桶命中率", "ext_zjqs_accuracy", "ext_zjqs_accuracy"))
    rows.append(_row("扩展-bf命中率", "ext_bf_accuracy", "ext_bf_accuracy"))
    rows.append(_row("扩展-bqc命中率", "ext_bqc_accuracy", "ext_bqc_accuracy"))
    tier_diffs = []  # (label, diff) 用于后续「变化最大档位」摘要
    for th in (0.50, 0.55, 0.60, 0.65, 0.70, 0.72, 0.75):
        vb = va = None
        for t in (b.get("by_single_rec_tier") or []):
            if t.get("threshold") == th:
                vb = (t.get("accuracy") or 0) * 100
                break
        for t in (a.get("by_single_rec_tier") or []):
            if t.get("threshold") == th:
                va = (t.get("accuracy") or 0) * 100
                break
        d = _diff(va, vb) if va is not None and vb is not None else None
        label = "推荐≥%d%%命中率" % int(th * 100)
        if d is not None:
            tier_diffs.append((label, d))
        trend = "提升" if d and d > 0 else ("下降" if d and d < 0 else "持平") if d is not None else "-"
        rows.append((label, "%.1f%%" % vb if vb is not None else "-", "%.1f%%" % va if va is not None else "-", ("%+.1f" % d) if d is not None else "-", trend))
    print("--- 竞彩评估对比（before=优化前/备份, after=优化后/当前）---")
    print("before: %s" % b.get("range_label") or before_path)
    print("after:  %s" % a.get("range_label") or after_path)
    print()
    print("%-28s %12s %12s %8s %6s" % ("指标", "优化前", "优化后", "变化", "结论"))
    print("-" * 70)
    for r in rows:
        if r:
            print("%-28s %12s %12s %8s %6s" % r)
    if tier_diffs:
        tier_diffs.sort(key=lambda x: abs(x[1]), reverse=True)
        print()
        print("【变化最大的档位】按变化绝对值排序（便于判断是否需调伤停/高置信门槛）：")
        for label, d in tier_diffs[:5]:
            direction = "↑" if d > 0 else "↓"
            print("  %s: %+.1f%% %s" % (label, d, direction))
    return 0


def cmd_jc_predictions_eval(args: argparse.Namespace) -> int:
    """根据 jc_predictions 与 jingcai_match_analysis 已完赛比分，统计指定年份的胜平负命中率与精确比分命中率。"""
    from db.self_improvement import get_self_improvement_db, jc_predictions_eval_report

    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    year = getattr(args, "year", None)
    date_from = (getattr(args, "date_from", None) or "").strip()[:10]
    date_to = (getattr(args, "date_to", None) or "").strip()[:10]
    all_time = getattr(args, "all_time", False)
    missed_cap = getattr(args, "missed_limit", 500)
    export_segment_path = getattr(args, "export_segment_stats", None) or None
    segment_stats_path = getattr(args, "segment_stats", None) or None
    segment_stats = None
    if segment_stats_path:
        try:
            import json
            from pathlib import Path
            raw = Path(segment_stats_path).read_text(encoding="utf-8")
            data = json.loads(raw)
            segment_stats = {"segments": data.get("segments")} if isinstance(data, dict) and data.get("segments") else None
        except Exception:
            pass
    export_league_path = getattr(args, "export_league_rec_params", None) or None
    common = {
        "missed_cap": missed_cap,
        "segment_stats": segment_stats,
        "export_segment_stats_path": export_segment_path,
        "export_league_rec_params_path": export_league_path,
    }
    if all_time:
        report = jc_predictions_eval_report(all_time=True, **common)
    elif date_from and date_to:
        report = jc_predictions_eval_report(date_from=date_from, date_to=date_to, **common)
    elif year is not None:
        report = jc_predictions_eval_report(year=int(year), **common)
    else:
        year = 2026
        report = jc_predictions_eval_report(year=int(year), **common)
    if report.get("message"):
        print(report["message"])
        return 0
    export_baseline_path = getattr(args, "export_baseline", None) or None
    if export_baseline_path and report.get("n", 0) > 0:
        import json as _json
        from pathlib import Path
        from datetime import datetime
        baseline = {
            "range_label": report.get("range_label"),
            "date_from": date_from or None,
            "date_to": date_to or None,
            "year": year,
            "export_timestamp": datetime.utcnow().isoformat() + "Z",
            "n": report["n"],
            "outcome_correct": report.get("outcome_correct"),
            "outcome_accuracy": report.get("outcome_accuracy"),
            "exact_score_correct": report.get("exact_score_correct"),
            "exact_score_accuracy": report.get("exact_score_accuracy"),
            "single_rec_ok": report.get("single_rec_ok"),
            "single_rec_n": report.get("single_rec_n"),
            "single_rec_accuracy": report.get("single_rec_accuracy"),
            "n_high_conf": report.get("n_high_conf"),
            "outcome_correct_high_conf": report.get("outcome_correct_high_conf"),
            "outcome_accuracy_high_conf": report.get("outcome_accuracy_high_conf"),
            "by_single_rec_tier": report.get("by_single_rec_tier"),
            "by_confidence_tier": report.get("by_confidence_tier"),
            "n_rq": report.get("n_rq"),
            "rq_ok_odds": report.get("rq_ok_odds"),
            "rq_accuracy_odds": report.get("rq_accuracy_odds"),
            "rq_ok_model": report.get("rq_ok_model"),
            "rq_accuracy_model": report.get("rq_accuracy_model"),
            "n_rq_jc_rqspf": report.get("n_rq_jc_rqspf"),
            "rq_ok_jc_rqspf": report.get("rq_ok_jc_rqspf"),
            "rq_accuracy_jc_rqspf": report.get("rq_accuracy_jc_rqspf"),
            "ext_zjqs_n": report.get("ext_zjqs_n"),
            "ext_zjqs_ok": report.get("ext_zjqs_ok"),
            "ext_zjqs_accuracy": report.get("ext_zjqs_accuracy"),
            "ext_bf_n": report.get("ext_bf_n"),
            "ext_bf_ok": report.get("ext_bf_ok"),
            "ext_bf_accuracy": report.get("ext_bf_accuracy"),
            "ext_bqc_n": report.get("ext_bqc_n"),
            "ext_bqc_ok": report.get("ext_bqc_ok"),
            "ext_bqc_accuracy": report.get("ext_bqc_accuracy"),
        }
        try:
            Path(export_baseline_path).parent.mkdir(parents=True, exist_ok=True)
            Path(export_baseline_path).write_text(_json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
            print("已导出评估基线到 %s，用于后续 jc-eval-compare 对比。" % export_baseline_path)
        except Exception as e:
            print("导出基线失败: %s" % e, file=sys.stderr)
    if export_segment_path:
        print(f"已写入「联赛×推荐类型×概率区间」细分命中率到 {export_segment_path}，后续可用 --segment-stats 读入做数据驱动自我提升。")
    if export_league_path:
        print(f"已写入各联赛推荐参数（含达 70% 目标所需最低概率）到 {export_league_path}，今日推荐将按联赛门槛过滤。")
    n = report["n"]
    range_label = report.get("range_label", str(year) + "年")
    if n == 0:
        print(f"{range_label}：无「有预测且已完赛」的场次（需 jc_predictions 有预测且 jingcai_match_analysis 有该场比分）。")
        diag = report.get("diagnostic")
        if diag:
            print(f"  诊断：jc_predictions 共 {diag.get('predictions_count', 0)} 条，其中带 sporttery_match_id 的 {diag.get('pred_with_sporttery_match_id', 0)} 条；"
                  f"能在 jingcai_match_analysis 中关联到的 {diag.get('matched_in_analysis', 0)} 条，其中已有比分的 {diag.get('matched_with_score', 0)} 条。")
        return 0
    # 仅输出「单场推荐命中率 + 关键配置」，用于快速查看核心指标
    if getattr(args, "single_rec_only", False):
        single_n = report.get("single_rec_n") or 0
        single_ok = report.get("single_rec_ok") or 0
        single_acc = (report.get("single_rec_accuracy") or 0) * 100
        print(f"--- 单场推荐命中率（{range_label}）---")
        print(f"  已完赛且有预测场次：{n} 场")
        print(f"  单场推荐（全部）：{single_ok}/{single_n}，命中率 {single_acc:.1f}%")
        tier_list = report.get("by_single_rec_tier") or []
        if tier_list:
            print("  单场推荐分档（推荐概率≥X%）：")
            for t in tier_list:
                th = int((t.get("threshold") or 0) * 100)
                nn = t.get("n") or 0
                ok = t.get("correct") or 0
                acc = (t.get("accuracy") or 0) * 100
                print(f"    ≥{th}%：{ok}/{nn}，{acc:.1f}%")
        print("  当前关键配置：")
        print(f"    SINGLE_REC_MIN_PROB={getattr(config, 'SINGLE_REC_MIN_PROB', 0.55)}")
        print(f"    JC_REC_DEFAULT_MIN_PROB={getattr(config, 'JC_REC_DEFAULT_MIN_PROB', 0.55)}")
        print(f"    JC_REC_HIGH_CONFIDENCE_MIN_PROB={getattr(config, 'JC_REC_HIGH_CONFIDENCE_MIN_PROB', 0.62)}")
        print(f"    PREDICT_USE_CALIBRATION={getattr(config, 'PREDICT_USE_CALIBRATION', True)}")
        print(f"    PREDICT_USE_OUTCOME_ML={getattr(config, 'PREDICT_USE_OUTCOME_ML', True)}")
        print(f"    PREDICT_OUTCOME_ML_WEIGHT={getattr(config, 'PREDICT_OUTCOME_ML_WEIGHT', 0.45)}")
        print(f"    PREDICT_ODDS_FUSION_WEIGHT_MODEL={getattr(config, 'PREDICT_ODDS_FUSION_WEIGHT_MODEL', 0.5)}")
        print(f"    PREDICT_USE_API_PREDICTIONS={getattr(config, 'PREDICT_USE_API_PREDICTIONS', False)}")
        print(f"    PREDICT_API_PREDICTIONS_WEIGHT={getattr(config, 'PREDICT_API_PREDICTIONS_WEIGHT', 0.3)}")
        return 0
    print(f"--- 竞彩预测命中率（{range_label}，jc_predictions vs jingcai_match_analysis 已完赛）---")
    print(f"  已完赛且有预测场次：{n} 场")
    # 优先展示单场推荐命中率（主指标）
    single_n = report.get("single_rec_n") or 0
    if single_n > 0:
        single_ok = report.get("single_rec_ok") or 0
        single_acc = (report.get("single_rec_accuracy") or 0) * 100
        print(f"  【主指标】单场推荐猜对：{single_ok}/{single_n}，命中率 {single_acc:.1f}%（按概率权重取最高一项）")
    print(f"  胜平负猜对：{report['outcome_correct']}/{n}，命中率 {report['outcome_accuracy']*100:.1f}%")
    model_n = report.get("model_outcome_n") or 0
    if model_n > 0:
        model_ok = report.get("model_outcome_correct") or 0
        model_acc = report.get("model_outcome_accuracy")
        model_pct = f"{model_acc*100:.1f}%" if model_acc is not None else "—"
        fused_pct = f"{report['outcome_accuracy']*100:.1f}%"
        diff = (report["outcome_accuracy"] - model_acc) * 100 if model_acc is not None else 0.0
        print(f"  【融合对比】胜平负：仅模型 {model_ok}/{model_n}，命中率 {model_pct}；融合后 {report['outcome_correct']}/{n}，命中率 {fused_pct}；融合{'提升' if diff > 0 else ('持平' if diff == 0 else '下降')} {abs(diff):.1f} 个百分点")
        # 单场推荐融合对比
        rec_n = report.get("model_single_rec_n") or 0
        if rec_n > 0:
            rec_ok = report.get("model_single_rec_ok") or 0
            rec_acc = report.get("model_single_rec_accuracy")
            rec_pct = f"{rec_acc*100:.1f}%" if rec_acc is not None else "—"
            fused_rec_ok = report.get("single_rec_ok") or 0
            fused_rec_n = report.get("single_rec_n") or 0
            fused_rec_pct = f"{(fused_rec_ok/fused_rec_n)*100:.1f}%" if fused_rec_n else "—"
            rec_diff = ((fused_rec_ok / fused_rec_n) - rec_acc) * 100 if (fused_rec_n and rec_acc is not None) else 0.0
            print(f"  【融合对比】单场推荐：仅模型 {rec_ok}/{rec_n}，命中率 {rec_pct}；融合后 {fused_rec_ok}/{fused_rec_n}，命中率 {fused_rec_pct}；融合{'提升' if rec_diff > 0 else ('持平' if rec_diff == 0 else '下降')} {abs(rec_diff):.1f} 个百分点")
        # 让球胜平负：由期望进球+让球数推导，融合不改变期望进球，故仅模型与融合一致
        n_rq = report.get("n_rq") or 0
        rq_ok = report.get("rq_ok_model") or 0
        if n_rq > 0:
            rq_pct = (rq_ok / n_rq) * 100
            print(f"  【融合对比】让球胜平负：由期望进球+让球数推导，融合不改变期望进球，故仅模型与融合一致；命中率 {rq_ok}/{n_rq}，{rq_pct:.1f}%")
    else:
        print(f"  【融合对比】当前无 model_predicted_result 记录（或均为旧数据）。重新运行「python3 main.py predict-jc-from-self-improvement --year 2026 --include-finished」后再评估可看到仅模型 vs 融合命中率对比。")
    if single_n > 0:
        tier_list = report.get("by_single_rec_tier") or []
        if tier_list:
            print("  【单场推荐分档】仅统计「推荐概率≥X%」时命中率（便于只推高置信）：")
            for t in tier_list:
                th = int((t.get("threshold") or 0) * 100)
                nn = t.get("n") or 0
                ok = t.get("correct") or 0
                acc = (t.get("accuracy") or 0) * 100
                print(f"    推荐概率≥{th}%：共 {nn} 场，命中 {ok} 场，命中率 {acc:.1f}%")
    ttg_n = report.get("ttg_n") or 0
    if ttg_n > 0:
        ttg_ok = report.get("ttg_ok") or 0
        ttg_acc = (report.get("ttg_accuracy") or 0) * 100
        print(f"  总进球精确命中：{ttg_ok}/{ttg_n}，命中率 {ttg_acc:.1f}%")
    ttg_range_n = report.get("ttg_range_n") or 0
    if ttg_range_n > 0:
        ttg_range_ok = report.get("ttg_range_ok") or 0
        ttg_range_acc = (report.get("ttg_range_accuracy") or 0) * 100
        print(f"  总进球区间命中：{ttg_range_ok}/{ttg_range_n}，命中率 {ttg_range_acc:.1f}%（区间覆盖 60% 概率）")
    ou25_n = report.get("ou25_n") or 0
    if ou25_n > 0:
        ou25_ok = report.get("ou25_ok") or 0
        ou25_acc = (report.get("ou25_accuracy") or 0) * 100
        print(f"  大小球 2.5 命中：{ou25_ok}/{ou25_n}，命中率 {ou25_acc:.1f}%（大2.5/小2.5 二选一）")
    nh = report.get("n_high_conf") or 0
    if nh > 0:
        ah = report.get("outcome_accuracy_high_conf") or 0
        ch = report.get("outcome_correct_high_conf") or 0
        print(f"  高置信度（推荐概率≥45%）：{ch}/{nh}，命中率 {ah*100:.1f}%")
    tiers = report.get("by_confidence_tier") or []
    if tiers:
        print("\n  【为接近 70% 目标】仅统计「推荐概率≥X%」的场次时：")
        for t in tiers:
            th_pct = int(t["threshold"] * 100)
            print(f"    推荐概率≥{th_pct}%：共 {t['n']} 场，命中 {t['correct']} 场，命中率 {t['accuracy']*100:.1f}%")
        print("  说明：胜平负三选一随机约 33%，业内全量常见 52–55%；70% 全量极难，可通过「只推荐高置信场次」或「只做优势联赛」提高命中率。")
    print(f"  精确比分命中：{report['exact_score_correct']}/{n}，命中率 {report['exact_score_accuracy']*100:.1f}%")
    ez = report.get("ext_zjqs_n") or 0
    if ez > 0:
        print(
            f"  【扩展-总进球桶 zjqs】best_pick：{report.get('ext_zjqs_ok', 0)}/{ez}，"
            f"命中率 {(report.get('ext_zjqs_accuracy') or 0)*100:.1f}%"
        )
    ebf = report.get("ext_bf_n") or 0
    if ebf > 0:
        print(
            f"  【扩展-比分 bf】best_pick：{report.get('ext_bf_ok', 0)}/{ebf}，"
            f"命中率 {(report.get('ext_bf_accuracy') or 0)*100:.1f}%"
        )
    ebq = report.get("ext_bqc_n") or 0
    if ebq > 0:
        print(
            f"  【扩展-半全场 bqc】best_pick（需半场比分）：{report.get('ext_bqc_ok', 0)}/{ebq}，"
            f"命中率 {(report.get('ext_bqc_accuracy') or 0)*100:.1f}%"
        )
    n_rq = report.get("n_rq") or 0
    if n_rq > 0:
        n_ro = report.get("n_rq_has_odds") or 0
        print(f"\n  【让球胜平负】有让球数且已完赛：{n_rq} 场（其中 {n_ro} 场有让球赔率）")
        if n_ro > 0:
            print(f"    按赔率最低项“预测”：{report.get('rq_ok_odds', 0)}/{n_ro}，命中率 {(report.get('rq_accuracy_odds') or 0)*100:.1f}%")
        print(f"    按模型预测比分+让球数推导：{report.get('rq_ok_model', 0)}/{n_rq}，命中率 {(report.get('rq_accuracy_model') or 0)*100:.1f}%")
        n_jcrq = report.get("n_rq_jc_rqspf") or 0
        if n_jcrq > 0:
            print(
                f"    按 jc_play_markets 让球三项(argmax，DC+hhad)：{report.get('rq_ok_jc_rqspf', 0)}/{n_jcrq}，"
                f"命中率 {(report.get('rq_accuracy_jc_rqspf') or 0)*100:.1f}%"
            )
        if report.get("by_league_rq"):
            print("    按联赛（让球）：")
            for g in report["by_league_rq"]:
                nr = g.get("n_rq") or 0
                no = g.get("n_rq_odds") or 0
                if nr == 0:
                    continue
                acc_m = (g.get("rq_accuracy_model") or 0) * 100
                acc_o = (g.get("rq_accuracy_odds") or 0) * 100 if no else 0
                print(f"      {g['league']}: n={nr} 赔率命中 {g.get('rq_ok_odds', 0)}/{no} ({acc_o:.1f}%)  模型推导 {g.get('rq_ok_model', 0)}/{nr} ({acc_m:.1f}%)")
    if report.get("by_league"):
        print("\n  按联赛（胜平负）：")
        for g in report["by_league"]:
            print(f"    {g['league']}: {g['outcome_correct']}/{g['n']}，命中率 {g['outcome_accuracy']*100:.1f}%")
    if report.get("by_league_rec"):
        print("\n  【单场推荐按联赛】目标 70% 命中率所需最低推荐概率（min_prob_70）：")
        for g in report["by_league_rec"][:25]:
            m = g.get("min_prob_70")
            m_str = f"，达 70% 门槛 {m}" if m is not None else "，历史无法达 70%"
            print(f"    {g['league']}: n={g['n']} 命中率 {g['single_rec_accuracy']*100:.1f}%{m_str}")
    # 单场推荐未命中复盘：便于「看看为什么会错」
    missed = report.get("missed_single_rec") or []
    export_missed = getattr(args, "export_missed", None)
    chunk_size = getattr(args, "export_missed_chunk_size", 0) or 0
    if export_missed and missed:
        import csv
        from pathlib import Path
        fieldnames = ["jc_date", "home_cn", "away_cn", "league", "recommended", "rec_prob", "actual_score", "actual_result", "reason"]
        if chunk_size > 0:
            # 分文件导出，避免单文件过大：stem_part001.csv, stem_part002.csv, ...
            p = Path(export_missed)
            for i in range(0, len(missed), chunk_size):
                chunk = missed[i : i + chunk_size]
                part_num = (i // chunk_size) + 1
                part_path = p.parent / f"{p.stem}_part{part_num:03d}{p.suffix}"
                with open(part_path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames)
                    w.writeheader()
                    w.writerows(chunk)
            print(f"\n已导出 {len(missed)} 条未命中复盘到 {part_num} 个文件：{p.stem}_part001{p.suffix}～{p.stem}_part{part_num:03d}{p.suffix}")
        else:
            with open(export_missed, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(missed)
            print(f"\n已导出 {len(missed)} 条未命中复盘到 {export_missed}")
    recap = getattr(args, "recap", False)
    if recap and missed:
        print("\n--- 单场推荐未命中复盘（看看为什么会错）---")
        for m in missed[:100]:
            print(f"  {m['jc_date']} {m['home_cn']} vs {m['away_cn']} [{m['league']}]")
            print(f"    推荐 {m['recommended']} {m['rec_prob']}% | 实际 {m['actual_score']} → {m['actual_result']} | {m['reason']}")
        if len(missed) > 100:
            print(f"  ... 共 {len(missed)} 条，仅展示前 100 条；完整列表可用 --export-missed 导出")
    elif recap and not missed:
        print("\n（该范围内无单场推荐未命中的场次）")
    return 0


def run_jc_to_self_improvement_for_dates(
    dates: list[str],
    dry_run: bool = False,
    progress_callback: Callable[[int, int, str, int, int, str], None] | None = None,
    sleep_sec: float = 0,
    per_date_results: list[dict] | None = None,
    *,
    prefer_result_api: bool = False,
    only_actionable: bool = False,
) -> int:
    """按 self_improvement_football 逻辑：逐日拉取体彩竞彩列表。体彩 API matchDate=竞彩日，传哪一天就返回该日列表（场次开球日为次日），保存时 jc_date=请求日。sleep_sec>0 时每次请求体彩接口后休眠，降低被拦截概率。prefer_result_api=True 时历史日期直接走赛果接口 getUniformMatchResultV1，不先请求 getMatchListV1。"""
    import time
    from db.self_improvement import (
        get_self_improvement_db,
        jc_match_analysis_backfill_fixture_id_for_jc_date,
        jc_match_analysis_delete_for_jc_date,
        jc_match_analysis_prune_jc_date_not_in_ids,
        jc_match_analysis_save,
    )
    from etl.sporttery_crawl import (
        _sporttery_list_row_still_actionable,
        crawl_sporttery_for_date,
        fetch_sporttery_match_detail_via_api,
        is_invalid_match,
    )

    if get_self_improvement_db() is None:
        raise RuntimeError("未配置 MONGO_URI，无法访问 self_improvement_football")

    total_saved = 0
    for i, d in enumerate(dates, 1):
        try:
            if sleep_sec > 0 and i > 1:
                time.sleep(sleep_sec)
            print(f"开始拉取 {d}（第 {i}/{len(dates)} 天）...", flush=True)
            # 体彩 matchDate 即竞彩日，传 d 得到该日列表（表中开球日为 d+1）
            matches = crawl_sporttery_for_date(d, prefer_result_api=prefer_result_api)
            if only_actionable:
                # 「拉取竞彩列表」按钮语义：以官网当前可售场次为准，已开奖/完赛场次不入库。
                matches = [m for m in matches if _sporttery_list_row_still_actionable(m)]
            if sleep_sec > 0:
                time.sleep(sleep_sec)
            saved_analysis = 0
            db_cleared = 0
            pruned_stale = 0
            if dry_run:
                for m in matches:
                    if m.get("match_id") is None:
                        continue
                    bdj = (m.get("businessDate") or "").strip()[:10]
                    if bdj and bdj != d[:10]:
                        continue
                    saved_analysis += 1
            elif len(matches) == 0:
                # 官网该竞彩日 0 场：清空库内该日残留（此前误用赛果/计算器回退写入的串日数据）
                try:
                    clr = jc_match_analysis_delete_for_jc_date(
                        d,
                        delete_jc_matches_doc=False,
                        delete_jc_predictions=False,
                        dry_run=False,
                    )
                    db_cleared = int(clr.get("deleted_analysis_total") or 0)
                except Exception:
                    db_cleared = 0
            else:
                keep_ids: set[int] = set()
                for m in matches:
                    mid = m.get("match_id")
                    if mid is None:
                        continue
                    try:
                        keep_ids.add(int(mid))
                    except (TypeError, ValueError):
                        pass
                if keep_ids:
                    try:
                        pruned_stale = jc_match_analysis_prune_jc_date_not_in_ids(d, keep_ids)
                    except Exception:
                        pruned_stale = 0
                for m in matches:
                    mid = m.get("match_id")
                    if mid is None:
                        continue
                    try:
                        detail = fetch_sporttery_match_detail_via_api(mid)
                        if sleep_sec > 0:
                            time.sleep(sleep_sec)
                        if is_invalid_match(detail):
                            continue
                        list_odds = m.get("odds") or {}
                        if list_odds:
                            detail["odds"] = {**(detail.get("odds") or {}), **list_odds}
                        # 竞彩日：优先用列表里的 businessDate（与官网 matchInfoList 一致）
                        bd_jc = (m.get("businessDate") or "").strip()[:10]
                        if bd_jc and bd_jc != d[:10]:
                            # 回退接口曾把其他 businessDate 的场次混进列表；勿写入所选日，避免「今天」页展示昨日赛程
                            continue
                        detail["jc_date"] = bd_jc or d
                        # 列表接口的比赛编号（周二001 等）写入文档，展示时优先用此编号，避免用详情接口的周几导致错日
                        list_mn = (m.get("match_num") or "").strip()
                        if list_mn:
                            detail["list_match_num"] = list_mn
                        if jc_match_analysis_save(mid, detail):
                            saved_analysis += 1
                    except Exception:
                        continue
            # 新写入的场次自动回填 api_football_fixture_id（该竞彩日）
            if not dry_run and saved_analysis > 0:
                try:
                    jc_match_analysis_backfill_fixture_id_for_jc_date(d, rate_limit_sec=1.0, overwrite_existing=False)
                except Exception:
                    pass
            total_saved += saved_analysis
            try:
                extra = ""
                if db_cleared:
                    extra += f"，已清空该竞彩日库内旧数据 {db_cleared} 条"
                if pruned_stale:
                    extra += f"，已删除不在本次列表中的残留 {pruned_stale} 条"
                print(f"  列表 {len(matches)} 场，写入 {saved_analysis} 场{extra}", flush=True)
            except Exception:
                pass
            if per_date_results is not None:
                per_date_results.append(
                    {
                        "date": d,
                        "list_count": len(matches),
                        "saved": saved_analysis,
                        "db_cleared": db_cleared,
                        "pruned_stale": pruned_stale,
                    }
                )
            hint = ""
            if len(matches) > 0 and saved_analysis == 0 and not dry_run:
                hint = "（本日无写入详情，可能接口异常）"
            if progress_callback:
                progress_callback(i, len(dates), d, len(matches), saved_analysis, hint)
        except Exception as e:
            if progress_callback:
                progress_callback(i, len(dates), d, 0, 0, f"失败: {e}")
    # 拉取完成后自动：把新出现的队名同步到 jingcai_teams，再按已有映射回填英文名（新一天的比赛也会带主客队英文名）
    if not dry_run and total_saved > 0:
        try:
            from db.self_improvement import jc_teams_sync_from_analysis, jc_match_analysis_backfill_english_names
            jc_teams_sync_from_analysis()
            jc_match_analysis_backfill_english_names()
        except Exception:
            pass
    return total_saved


def cmd_jc_batch_year(args: argparse.Namespace) -> int:
    """按年批量拉取竞彩：将指定年份 1 月 1 日至今日逐日拉取并写入 jingcai_match_analysis。与「拉取竞彩列表」按钮无关，仅命令行批量用。"""
    from datetime import date, timedelta

    from db.self_improvement import get_self_improvement_db

    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1

    year = getattr(args, "year", None)
    dry_run = getattr(args, "dry_run", False)
    if year is None:
        print("请指定 --year 年份，如 2026（将拉取该年 1 月 1 日至今日）。", file=sys.stderr)
        return 1

    y = int(year)
    d0 = date(y, 1, 1)
    today = date.today()
    d1 = min(date(y, 12, 31), today)
    dates = []
    d = d0
    while d <= d1:
        dates.append(d.isoformat())
        d += timedelta(days=1)

    resume = getattr(args, "resume", False)
    if resume and dates:
        from db.self_improvement import jc_match_analysis_list_dates
        existing_in_year = {x["date"] for x in jc_match_analysis_list_dates() if (x.get("date") or "").startswith(str(y) + "-")}
        dates = [d for d in dates if d not in existing_in_year]
        print(f"断点续跑：跳过 {len(existing_in_year)} 天（库中已有），剩余 {len(dates)} 天待拉取。", file=sys.stderr)

    from_date = (getattr(args, "from_date", None) or "").strip()[:10]
    to_date = (getattr(args, "to_date", None) or "").strip()[:10]
    if from_date and len(from_date) == 10:
        dates = [d for d in dates if d >= from_date]
        print(f"仅拉取 {from_date} 及之后，剩余 {len(dates)} 天。", file=sys.stderr)
    if to_date and len(to_date) == 10:
        dates = [d for d in dates if d <= to_date]
        print(f"仅拉取 {to_date} 及之前，剩余 {len(dates)} 天。", file=sys.stderr)

    if not dates:
        print(f"{y} 年暂无日期需拉取（或已过完）。", file=sys.stderr)
        return 0

    print(f"写入目标: MongoDB 库「self_improvement_football」集合 jingcai_match_analysis", file=sys.stderr)
    mode_hint = "（仅预览，不写入）" if dry_run else "（正式写入）"
    print(f"将拉取 {len(dates)} 天（{dates[0]} ~ {dates[-1]}，到今日止）{mode_hint}", file=sys.stderr)
    proxy_url = (getattr(args, "proxy", None) or "").strip()
    if proxy_url:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        from etl import sporttery_crawl as _sc
        _sc.set_sporttery_proxy(proxy_url)
        print(f"使用代理: {proxy_url}", file=sys.stderr)
    sys.stdout.flush()

    def log_progress(i: int, total: int, date_str: str, matches_count: int, saved_analysis: int, hint: str) -> None:
        msg = f"当前进行到 {date_str}（第 {i}/{total} 天），本日体彩场次 {matches_count} 场，写入 jingcai_match_analysis {saved_analysis} 场{hint}"
        print(msg)
        sys.stdout.flush()

    sleep_sec = max(0.0, float(getattr(args, "sleep", 2.0)))
    if sleep_sec > 0:
        print(f"请求间隔: 每次体彩接口调用后休眠 {sleep_sec} 秒。", file=sys.stderr)
    prefer_result_api = getattr(args, "use_result_api", False)
    if prefer_result_api:
        print("历史日期直接使用赛果接口 getUniformMatchResultV1，跳过 getMatchListV1。", file=sys.stderr)
    total_saved = run_jc_to_self_improvement_for_dates(dates, dry_run=dry_run, progress_callback=log_progress, sleep_sec=sleep_sec, prefer_result_api=prefer_result_api)
    if dry_run:
        print(f"预览完成: 共 {len(dates)} 天（未写入）。去掉 --dry-run 后执行将正式写入 self_improvement_football。")
    else:
        print(f"完成: 共 {total_saved} 条对阵详情已写入 jingcai_match_analysis。")
        if total_saved > 0:
            print("已自动执行：队名同步到 jingcai_teams、英文名回填、api_football_fixture_id 回填（每日新写入后按日补全）；新队需执行 jc-teams-map-api-football 后再跑一次本命令或 jc-match-analysis-backfill-en。")
    return 0


def cmd_jc_to_self_improvement(args: argparse.Namespace) -> int:
    """单日拉取：与「拉取竞彩列表」按钮逻辑一致，仅拉取指定竞彩日并写入 jingcai_match_analysis。"""
    from db.self_improvement import get_self_improvement_db

    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1

    day = (getattr(args, "date", None) or "").strip()[:10]
    if not day or len(day) != 10:
        print("请指定 --date 竞彩日，格式 YYYY-MM-DD，如 2023-01-01。", file=sys.stderr)
        return 1

    dry_run = getattr(args, "dry_run", False)
    sleep_sec = max(0.0, float(getattr(args, "sleep", 0.5)))
    proxy_url = (getattr(args, "proxy", None) or "").strip()
    if proxy_url:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        from etl import sporttery_crawl as _sc
        _sc.set_sporttery_proxy(proxy_url)

    total_saved = run_jc_to_self_improvement_for_dates([day], dry_run=dry_run, sleep_sec=sleep_sec)
    if dry_run:
        print(f"预览完成: 竞彩日 {day}（未写入）。")
    else:
        print(f"完成: 竞彩日 {day} 写入 {total_saved} 条至 jingcai_match_analysis。")
    return 0


def cmd_jc_match_analysis_delete_by_date(args: argparse.Namespace) -> int:
    """按竞彩日删除 jingcai_match_analysis 中错日/串日残留；可选清 jc_matches、jc_predictions。"""
    from db.self_improvement import jc_match_analysis_delete_for_jc_date

    day = (getattr(args, "date", None) or "").strip()[:10]
    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False))
    if len(day) != 10:
        print("请使用 --date YYYY-MM-DD", file=sys.stderr)
        return 1
    if not dry_run and not yes:
        print(f"将删除竞彩日 {day} 在 jingcai_match_analysis（及备用集合）中的文档。", file=sys.stderr)
        print("确认请添加 --yes；或先用 --dry-run 查看将删除条数。", file=sys.stderr)
        return 1
    try:
        out = jc_match_analysis_delete_for_jc_date(
            day,
            delete_jc_matches_doc=not bool(getattr(args, "no_jc_matches", False)),
            delete_jc_predictions=bool(getattr(args, "include_jc_predictions", False)),
            dry_run=dry_run,
        )
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1
    prefix = "【dry-run】将删除" if dry_run else "已删除"
    print(f"{prefix} 竞彩日 {day}：jingcai_match_analysis 合计 {out.get('deleted_analysis_total', 0)} 条")
    for name, info in sorted((out.get("collections") or {}).items()):
        print(f"  {name}: {info}")
    jm = out.get("jc_matches") or {}
    if jm:
        print(f"  jc_matches: {jm}")
    jp = out.get("jc_predictions") or {}
    if jp:
        print(f"  jc_predictions: {jp}")
    if dry_run:
        print("去掉 --dry-run 并加 --yes 后执行实际删除。")
    return 0


def cmd_predict_from_mongo_matches(args: argparse.Namespace) -> int:
    """对 MongoDB matches 表中的比赛做模型预测并落库，供 Web 仪表盘展示。仅 USE_MONGO 时有效。"""
    from db import repo
    if not repo.use_mongo():
        print("predict-from-mongo-matches 仅在使用 MongoDB 时可用（USE_MONGO=true）。", file=sys.stderr)
        return 1
    from engine.predict import run_predict_for_mongo_matches
    date_from = (getattr(args, "date_from", None) or "").strip()[:10] or None
    date_to = (getattr(args, "date_to", None) or "").strip()[:10] or None
    dates = getattr(args, "dates", None)
    if dates:
        parts = [p.strip()[:10] for p in str(dates).split(",") if p.strip()[:10]]
        if len(parts) >= 2:
            date_from, date_to = parts[0], parts[1]
        elif len(parts) == 1:
            date_from = date_to = parts[0]
    limit = getattr(args, "limit", None)
    force = getattr(args, "force", False)
    try:
        added, skipped, total = run_predict_for_mongo_matches(
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            force=force,
        )
    except Exception as e:
        print(f"执行失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    print(f"predict-from-mongo-matches：共 {total} 场（MongoDB matches）；新增预测 {added} 条，跳过 {skipped} 场。")
    print("刷新 Web 仪表盘按比赛日选择日期即可查看预测。")
    return 0


def cmd_auto_accumulate_log(args: argparse.Namespace) -> int:
    """自动为「昨日/今日/明日」：从 DB 取场次，未开赛写预测、已完赛用 DB 比分回填 log（数据来自 API）。可配合 cron 每日执行。"""
    from datetime import date, timedelta
    dates = getattr(args, "dates", None)
    if dates:
        dates = [d.strip()[:10] for d in dates.split(",") if d.strip()[:10]]
    else:
        dates = [
            (date.today() - timedelta(days=1)).isoformat(),
            date.today().isoformat(),
            (date.today() + timedelta(days=1)).isoformat(),
        ]
    try:
        added, updated, total = run_auto_accumulate_log(dates)
    except Exception as e:
        print(f"执行失败: {e}", file=sys.stderr)
        return 1
    print(f"auto-accumulate-log：处理 {total} 场（来自 DB）；新增预测 {added} 条，回填比分 {updated} 条。")
    print("建议每日执行一次（如 cron 0 9 * * *），并确保 ingest 等 API 已回填赛果。")
    return 0


def cmd_injury_crawl(args: argparse.Namespace) -> int:
    """伤停数据：从公众号（如亚鸣笔记）图片 OCR 解析，五大联赛优先，写入 DB（match_injury）；支持 --date 按日期选文章。"""
    from datetime import date
    from etl.injury_crawl import crawl_injury, save_injury_json

    url = getattr(args, "url", None)
    from_image = getattr(args, "from_image", None)
    include_all = getattr(args, "all", False)
    date_str = getattr(args, "date", None)
    merge = getattr(args, "merge", False)

    if from_image:
        source_url = url or config.INJURY_ARTICLE_URL or ""
    else:
        date_used = (date_str or "").strip()[:10] if date_str else date.today().isoformat()
        source_url = (url or "").strip() or config.get_injury_url_for_date(date_used) or config.INJURY_ARTICLE_URL or ""

    if not from_image and not source_url:
        print("请提供 --url 公众号链接、--from-image 本地图片、--date 使用 data/injury_urls.json 按日期选文章，或在 .env 中设置 INJURY_ARTICLE_URL。", file=sys.stderr)
        return 1

    try:
        matches = crawl_injury(url=source_url if not from_image else None, from_image=from_image, include_all=include_all)
    except Exception as e:
        print(f"解析失败: {e}", file=sys.stderr)
        return 1

    if not matches:
        print("未解析到任何伤停数据。")
        return 0

    source_url = source_url or url or config.INJURY_ARTICLE_URL or ""
    path = save_injury_json(matches, source_url=source_url, merge=merge)
    if source_url and not from_image:
        date_used = (date_str or "").strip()[:10] if date_str else date.today().isoformat()
        if len(date_used) == 10:
            config.save_injury_url_for_date(date_used, source_url)
    big5_count = sum(1 for m in matches if m.get("league") in {"英超", "西甲", "意甲", "德甲", "法甲"})
    msg = f"伤停解析完成：共 {len(matches)} 场（五大联赛 {big5_count} 场）"
    if merge:
        msg += "，已合并到现有 JSON"
    print(f"{msg}，已保存至 {path}")
    return 0


def cmd_injury_api(args: argparse.Namespace) -> int:
    """伤停数据：从 API-FOOTBALL 拉取五大联赛伤停，写入 DB（match_injury）。支持 --season 按赛季断点续拉。"""
    from datetime import date
    from etl.injury_api_football import fetch_and_save, fetch_season_and_save

    date_str = getattr(args, "date", None)
    merge = getattr(args, "merge", False)
    season_arg = getattr(args, "season", None)
    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY 或 API_FOOTBALL_KEY_2（api-sports.io 注册免费 key）。", file=sys.stderr)
        return 1
    rate_limit = getattr(args, "rate_limit", None)
    if season_arg is not None:
        try:
            season = int(season_arg)
        except (TypeError, ValueError):
            print("--season 需为整数，如 2024。", file=sys.stderr)
            return 1
        if getattr(args, "reset_season", False):
            from etl.injury_api_football import reset_season_progress
            reset_season_progress(season)
            print(f"已清除赛季 {season} 的拉取进度，开始重新拉取。")
        try:
            days_done, message = fetch_season_and_save(season, rate_limit_seconds=rate_limit)
            print(message)
            return 0
        except ValueError as e:
            print(f"错误: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"拉取失败: {e}", file=sys.stderr)
            return 1
    date_used = (date_str or "").strip()[:10] if date_str else date.today().isoformat()
    if len(date_used) != 10:
        print("日期格式需为 YYYY-MM-DD；或使用 --season 2024 按赛季断点续拉。", file=sys.stderr)
        return 1
    try:
        matches, message = fetch_and_save(date_used, merge=merge, rate_limit_seconds=rate_limit)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"拉取失败: {e}", file=sys.stderr)
        return 1
    print(message)
    return 0


def cmd_injury_api_by_db(args: argparse.Namespace) -> int:
    """伤停：按 DB 中比赛日批量拉取伤停并写入 DB（Mongo/SQLite）。只拉取 DB 里已有比赛的日期，避免空请求。"""
    from datetime import date
    from db import repo
    from etl.injury_api_football import fetch_and_save

    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY。", file=sys.stderr)
        return 1
    years_str = getattr(args, "years", None)
    if not (years_str or "").strip():
        print("请指定 --years，如 --years 2024 或 --years 2023,2024。", file=sys.stderr)
        return 1
    years = []
    for s in years_str.strip().split(","):
        s = s.strip()
        if s:
            try:
                years.append(int(s))
            except ValueError:
                pass
    if not years:
        print("--years 格式应为年份逗号分隔，如 2024 或 2023,2024。", file=sys.stderr)
        return 1
    repo.init_db()
    dates = repo.get_distinct_match_dates(years=years, big5_only=True)
    if not dates:
        print(f"DB 中无 {years} 年的五大联赛比赛日。")
        return 0
    today_str = date.today().isoformat()
    if not getattr(args, "include_future", False):
        before = len(dates)
        dates = [d for d in dates if d <= today_str]
        if before > len(dates):
            print(f"跳过未来日期 {before - len(dates)} 个（仅拉取 ≤ 今天 {today_str}）；若需包含未来可加 --include-future）。")
    if getattr(args, "skip_existing", False):
        before = len(dates)
        dates = [d for d in dates if not repo.injury_has_date(d)]
        if before > len(dates):
            print(f"跳过已有伤停的 {before - len(dates)} 个日期，待拉取 {len(dates)} 个。")
    limit = getattr(args, "limit", None)
    if limit is not None and limit > 0 and len(dates) > limit:
        dates = dates[-limit:]
        print(f"仅处理最近 {limit} 个日期（共 {len(dates)} 个）。")
    if not dates:
        print("所有日期在 match_injury 中已有伤停或均为未来日期，无需拉取。")
        return 0
    rate_limit = getattr(args, "rate_limit", None) or getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    dry_run = getattr(args, "dry_run", False)
    print(f"共 {len(dates)} 个比赛日: {dates[0]} ~ {dates[-1]}")
    if dry_run:
        for d in dates:
            print(f"  伤停 {d}")
        return 0
    ok = 0
    for i, d in enumerate(dates, 1):
        try:
            _, msg = fetch_and_save(d, merge=True, rate_limit_seconds=rate_limit)
            print(f"[{i}/{len(dates)}] {d} {msg}")
            ok += 1
        except Exception as e:
            print(f"[{i}/{len(dates)}] {d} 失败: {e}", file=sys.stderr)
    print(f"完成: {ok}/{len(dates)} 个日期已拉取并写入 DB。")
    return 0 if ok == len(dates) else 1


def cmd_injury_clean_non_api(args: argparse.Namespace) -> int:
    """伤停：删除 DB 中非 API 来源（公众号 OCR、migrate 等），仅保留 source=api_football。"""
    from etl.injury_db import delete_injury_non_api_rows
    n = delete_injury_non_api_rows()
    print(f"已删除 {n} 条非 API 来源伤停，现仅保留 API 数据。")
    return 0


def cmd_dongqiudi_lineups(args: argparse.Namespace) -> int:
    """懂球帝预计首发：按竞彩日从懂球帝拉取预计首发并落库，预测时与射手名单结合叠加「预计未首发」到 key_count。"""
    from datetime import date
    from db.self_improvement import get_self_improvement_db, jc_match_analysis_list_by_jc_date, jc_matches_load
    from etl.dongqiudi_lineups import run_crawl_for_jc_date

    if get_self_improvement_db() is None:
        print("未配置 MongoDB（MONGO_URI），无法读取竞彩场次或落库。", file=sys.stderr)
        return 1
    date_str = getattr(args, "date", None) or date.today().isoformat()
    date_only = (date_str or "").strip()[:10]
    if len(date_only) != 10:
        print("请提供有效日期 --date YYYY-MM-DD。", file=sys.stderr)
        return 1
    print(f"懂球帝阵容：读取竞彩日 {date_only} 场次…", flush=True)
    save_to_db = not getattr(args, "no_save", False)
    matches = jc_match_analysis_list_by_jc_date(date_only)
    if not matches:
        matches = jc_matches_load(date_only)
    if not matches:
        print(f"该日无竞彩场次：{date_only}。请先拉取竞彩列表并写入 jingcai_match_analysis 或 jc_matches。", flush=True)
        return 0
    rate_sec = getattr(args, "rate_limit", None)
    max_matches = getattr(args, "max_matches", None)
    save_as_lineups = not getattr(args, "no_save_as_lineups", False)
    result = run_crawl_for_jc_date(
        date_only, matches, save_to_db=save_to_db,
        rate_limit_sec=rate_sec, max_requests_per_run=max_matches,
        save_as_lineups_format=save_as_lineups,
    )
    print(result.get("message", ""))
    if save_to_db and save_as_lineups and result.get("matched", 0) > 0:
        print("  赛前阵容已按 match_lineups 相同结构写入 pre_match_lineups，可与赛后实际阵容对比。")
    if result.get("rate_limited"):
        print("  提示：已达单次请求上限，避免频繁抓取被禁 IP。可稍后再次执行或适当调大 .env 的 DONGQIUDI_MAX_REQUESTS_PER_RUN。")
    if result.get("failed"):
        for f in result["failed"][:15]:
            print(f"  失败: {f}")
        if len(result["failed"]) > 15:
            print(f"  ... 共 {len(result['failed'])} 场失败")
    return 0


def cmd_transfermarkt_lineups(args: argparse.Namespace) -> int:
    """Transfermarkt 赛前阵容：按竞彩日从 Transfermarkt 拉取 vorbericht 用户阵型并落库，预测时优先使用（再回退懂球帝）。"""
    from datetime import date
    from db.self_improvement import get_self_improvement_db, jc_match_analysis_list_by_jc_date, jc_matches_load
    from etl.transfermarkt_lineups import fetch_matches_for_league_round, run_crawl_for_jc_date

    if get_self_improvement_db() is None:
        print("未配置 MongoDB（MONGO_URI），无法读取竞彩场次或落库。", file=sys.stderr)
        return 1
    date_str = getattr(args, "date", None) or date.today().isoformat()
    date_only = (date_str or "").strip()[:10]
    if len(date_only) != 10:
        print("请提供有效日期 --date YYYY-MM-DD。", file=sys.stderr)
        return 1
    save_to_db = not getattr(args, "no_save", False)
    matches = jc_match_analysis_list_by_jc_date(date_only)
    if not matches:
        matches = jc_matches_load(date_only)
    if not matches:
        print(f"该日无竞彩场次：{date_only}。请先拉取竞彩列表并写入 jingcai_match_analysis 或 jc_matches。")
        return 0
    rate_sec = getattr(args, "rate_limit", None)
    max_matches = getattr(args, "max_matches", None)

    tm_list_override = None
    league = getattr(args, "league", None) or ""
    spieltag = getattr(args, "round", None)
    season = getattr(args, "season", None)
    if league.strip() and spieltag is not None:
        saison_id = int(season) if season is not None else int(date_only[:4])
        tm_list, league_err = fetch_matches_for_league_round(league, saison_id, spieltag)
        if league_err or not tm_list:
            print(f"联赛轮次页: {league_err or '无数据'}", file=sys.stderr)
            return 1
        tm_list_override = tm_list
        print(f"已从联赛轮次页获取 {len(tm_list)} 场，与竞彩场次匹配中…")

    result = run_crawl_for_jc_date(
        date_only, matches, save_to_db=save_to_db,
        rate_limit_sec=rate_sec, max_requests_per_run=max_matches,
        tm_list_override=tm_list_override,
    )
    print(result.get("message", ""))
    if result.get("rate_limited"):
        print("  提示：已达单次请求上限，避免频繁抓取被禁 IP。可稍后再次执行或适当调大 .env 的 TRANSFERMARKT_MAX_REQUESTS_PER_RUN。")
    if result.get("failed"):
        for f in result["failed"][:15]:
            print(f"  失败: {f}")
        if len(result["failed"]) > 15:
            print(f"  ... 共 {len(result['failed'])} 场失败")
    return 0


def cmd_lineups_by_db(args: argparse.Namespace) -> int:
    """阵容：按 DB 中比赛批量拉取完整阵容（首发+替补）并落库。断点续拉、可限次数，适配 7500 次/天等配额。"""
    from db import repo
    from etl.api_football_lineups import fetch_lineups_for_fixture

    if not repo.use_mongo():
        print("仅支持 MongoDB；当前未启用。", file=sys.stderr)
        return 1
    repo.init_db()
    years = []
    years_str = getattr(args, "years", None) or ""
    for s in years_str.strip().split(","):
        s = s.strip()
        if s and s.isdigit():
            years.append(int(s))
    if not years and not (getattr(args, "date_from", None) or getattr(args, "date_to", None)):
        print("请指定 --years 或 --date-from/--date-to，如 --years 2024,2025。", file=sys.stderr)
        return 1
    skip = getattr(args, "skip_existing", True)
    limit = getattr(args, "limit", None)
    date_from = getattr(args, "date_from", None) or None
    date_to = getattr(args, "date_to", None) or None
    rate_sec = getattr(args, "rate_limit", None) or getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    dry_run = getattr(args, "dry_run", False)

    todo = repo.lineups_list_matches_to_fetch(
        years=years if years else None,
        skip_existing=skip,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
    if not todo:
        print("无待拉取阵容的比赛（或已全部拉过）。可去掉 --skip-existing 重跑，或检查 --years/--date-from/--date-to。")
        return 0
    print(f"待拉取 {len(todo)} 场（每场 1 次 API）；本次最多 {limit or '全部'} 场；间隔 {rate_sec}s。7500 次/天约可拉 7500 场/天。")
    if dry_run:
        for i, (mid, sid) in enumerate(todo[:20], 1):
            print(f"  [{i}] match_id={mid} fixture_id={sid}")
        if len(todo) > 20:
            print(f"  ... 共 {len(todo)} 场")
        return 0
    ok = 0
    for i, (match_id, source_id) in enumerate(todo, 1):
        try:
            # source_id 可能为 "af_12345"（ingest 写入）或 "12345"
            sid_str = str(source_id).strip()
            if sid_str.isdigit():
                fid = int(sid_str)
            elif sid_str.startswith("af_") and sid_str[3:].isdigit():
                fid = int(sid_str[3:])
            else:
                fid = None
            if not fid:
                continue
            lu = fetch_lineups_for_fixture(fid, rate_limit_sec=rate_sec, full=True)
            hc = lu.get("home_starting_count")
            ac = lu.get("away_starting_count")
            repo.lineups_save(
                match_id,
                home_starting_count=hc,
                away_starting_count=ac,
                home_starting=lu.get("home_starting"),
                away_starting=lu.get("away_starting"),
                home_substitutes=lu.get("home_substitutes"),
                away_substitutes=lu.get("away_substitutes"),
            )
            ok += 1
            if i % 50 == 0 or i == len(todo):
                print(f"  已拉取 {i}/{len(todo)} 场")
        except Exception as e:
            print(f"  match_id={match_id} fixture_id={source_id} 失败: {e}", file=sys.stderr)
    print(f"完成: 成功 {ok}/{len(todo)} 场。未拉取的下次加 --skip-existing 可断点续拉。")
    return 0 if ok == len(todo) else 1


def cmd_lineups_by_jingcai(args: argparse.Namespace) -> int:
    """阵容（竞彩）：从 jingcai_match_analysis 按 api_football_fixture_id 拉取阵容，写入 self_improvement_football.match_lineups（match_id=sporttery_match_id）。"""
    from db import self_improvement as si
    from etl.api_football_lineups import fetch_lineups_for_fixture

    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    years = []
    years_str = getattr(args, "years", None) or ""
    for s in years_str.strip().split(","):
        s = s.strip()
        if s and s.isdigit():
            years.append(int(s))
    date_from = getattr(args, "date_from", None) or None
    date_to = getattr(args, "date_to", None) or None
    if not years and not date_from and not date_to:
        print("请指定 --years 或 --date-from/--date-to，如 --years 2024,2025 或 --date-from 2024-01-01 --date-to 2024-12-31。", file=sys.stderr)
        return 1
    skip = getattr(args, "skip_existing", True)
    limit = getattr(args, "limit", None)
    rate_sec = getattr(args, "rate_limit", None) or getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    dry_run = getattr(args, "dry_run", False)

    todo = si.lineups_list_jingcai_to_fetch(
        years=years if years else None,
        date_from=date_from,
        date_to=date_to,
        skip_existing=skip,
        limit=limit,
    )
    if not todo:
        print("无待拉取阵容的竞彩场次（jingcai_match_analysis 需有 api_football_fixture_id；或已全部拉过）。可去掉 --skip-existing 重跑。")
        return 0
    print(f"待拉取 {len(todo)} 场（self_improvement_football）；本次最多 {limit or '全部'} 场；间隔 {rate_sec}s。")
    if dry_run:
        for i, (mid, fid) in enumerate(todo[:20], 1):
            print(f"  [{i}] sporttery_match_id={mid} fixture_id={fid}")
        if len(todo) > 20:
            print(f"  ... 共 {len(todo)} 场")
        return 0
    ok = 0
    for i, (sporttery_match_id, fixture_id) in enumerate(todo, 1):
        try:
            lu = fetch_lineups_for_fixture(fixture_id, rate_limit_sec=rate_sec, full=True)
            # 仅在有有效数据时落库，避免因限流失败也写入空记录导致断点续拉时被跳过
            if lu.get("home_starting_count") is not None or lu.get("away_starting_count") is not None:
                si.lineups_save(
                    sporttery_match_id,
                    home_starting_count=lu.get("home_starting_count"),
                    away_starting_count=lu.get("away_starting_count"),
                    home_starting=lu.get("home_starting"),
                    away_starting=lu.get("away_starting"),
                    home_substitutes=lu.get("home_substitutes"),
                    away_substitutes=lu.get("away_substitutes"),
                )
                ok += 1
            if i % 50 == 0 or i == len(todo):
                print(f"  已拉取 {i}/{len(todo)} 场")
        except Exception as e:
            err_msg = str(e)
            # API 请求次数/套餐用尽：立即终止，已拉取的已落库，下次运行断点续拉
            if "limit" in err_msg.lower() or "超限" in err_msg or "套餐" in err_msg or "计划" in err_msg or "request limit" in err_msg.lower():
                print("", file=sys.stderr)
                print("API 请求次数已达上限，本次已保存进度。请明日再运行本命令断点续拉：", file=sys.stderr)
                print("  python3 main.py lineups-by-jingcai --years 2026 [--rate-limit 0.05]", file=sys.stderr)
                return 1
            print(f"  sporttery_match_id={sporttery_match_id} fixture_id={fixture_id} 失败: {e}", file=sys.stderr)
    print(f"完成: 成功 {ok}/{len(todo)} 场。数据已写入 self_improvement_football.match_lineups。")
    return 0 if ok == len(todo) else 1


def cmd_events_by_jingcai(args: argparse.Namespace) -> int:
    """事件（竞彩）：从 jingcai_match_analysis 按 api_football_fixture_id 拉取单场事件（进球、红黄牌、换人等），写入 self_improvement_football.api_football_events。"""
    from db import self_improvement as si
    from etl.api_football_events import fetch_events_for_fixture

    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    years = []
    years_str = getattr(args, "years", None) or ""
    for s in years_str.strip().split(","):
        s = s.strip()
        if s and s.isdigit():
            years.append(int(s))
    date_from = getattr(args, "date_from", None) or None
    date_to = getattr(args, "date_to", None) or None
    if not years and not date_from and not date_to:
        print("请指定 --years 或 --date-from/--date-to，如 --years 2024,2025 或 --date-from 2024-01-01 --date-to 2024-12-31。", file=sys.stderr)
        return 1
    skip = getattr(args, "skip_existing", True)
    limit = getattr(args, "limit", None)
    rate_sec = getattr(args, "rate_limit", None) or getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    dry_run = getattr(args, "dry_run", False)

    todo = si.events_list_jingcai_to_fetch(
        years=years if years else None,
        date_from=date_from,
        date_to=date_to,
        skip_existing=skip,
        limit=limit,
    )
    if not todo:
        print("无待拉取事件的竞彩场次（jingcai_match_analysis 需有 api_football_fixture_id；或已全部拉过）。可去掉 --skip-existing 重跑。")
        return 0
    print(f"待拉取 {len(todo)} 场事件（self_improvement_football.api_football_events）；本次最多 {limit or '全部'} 场；间隔 {rate_sec}s。")
    if dry_run:
        for i, (mid, fid) in enumerate(todo[:20], 1):
            print(f"  [{i}] sporttery_match_id={mid} fixture_id={fid}")
        if len(todo) > 20:
            print(f"  ... 共 {len(todo)} 场")
        return 0
    ok = 0
    for i, (sporttery_match_id, fixture_id) in enumerate(todo, 1):
        try:
            events = fetch_events_for_fixture(fixture_id, rate_limit_sec=rate_sec)
            si.save_api_football_events(
                api_football_fixture_id=fixture_id,
                events=events,
                sporttery_match_id=sporttery_match_id,
            )
            ok += 1
            if i % 50 == 0 or i == len(todo):
                print(f"  已拉取 {i}/{len(todo)} 场")
        except Exception as e:
            err_msg = str(e)
            if "limit" in err_msg.lower() or "超限" in err_msg or "套餐" in err_msg or "计划" in err_msg or "request limit" in err_msg.lower():
                print("", file=sys.stderr)
                print("API 请求次数已达上限，本次已保存进度。请明日再运行本命令断点续拉：", file=sys.stderr)
                print("  python3 main.py events-by-jingcai --years 2026 [--rate-limit 0.05]", file=sys.stderr)
                return 1
            print(f"  sporttery_match_id={sporttery_match_id} fixture_id={fixture_id} 失败: {e}", file=sys.stderr)
    print(f"完成: 成功 {ok}/{len(todo)} 场。数据已写入 self_improvement_football.api_football_events。")
    return 0 if ok == len(todo) else 1


def cmd_players_stats_by_jingcai(args: argparse.Namespace) -> int:
    """球员统计（竞彩）：从 jingcai_match_analysis 按 api_football_fixture_id 拉取单场球员统计（players/statistics），写入 api_football_player_statistics。"""
    from db import self_improvement as si
    from etl.api_football_players import fetch_player_statistics_for_fixture

    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    years = []
    years_str = getattr(args, "years", None) or ""
    for s in years_str.strip().split(","):
        s = s.strip()
        if s and s.isdigit():
            years.append(int(s))
    date_from = getattr(args, "date_from", None) or None
    date_to = getattr(args, "date_to", None) or None
    if not years and not date_from and not date_to:
        print("请指定 --years 或 --date-from/--date-to，如 --years 2024,2025 或 --date-from 2024-01-01 --date-to 2024-12-31。", file=sys.stderr)
        return 1
    skip = getattr(args, "skip_existing", True)
    limit = getattr(args, "limit", None)
    rate_sec = getattr(args, "rate_limit", None) or getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    dry_run = getattr(args, "dry_run", False)

    todo = si.players_stats_list_jingcai_to_fetch(
        years=years if years else None,
        date_from=date_from,
        date_to=date_to,
        skip_existing=skip,
        limit=limit,
    )
    if not todo:
        print("无待拉取球员统计的竞彩场次（jingcai_match_analysis 需有 api_football_fixture_id；或已全部拉过）。可去掉 --no-skip-existing 重跑。")
        return 0
    print(f"待拉取 {len(todo)} 场球员统计（self_improvement_football.api_football_player_statistics）；本次最多 {limit or '全部'} 场；间隔 {rate_sec}s。")
    if dry_run:
        for i, (mid, fid) in enumerate(todo[:20], 1):
            print(f"  [{i}] sporttery_match_id={mid} fixture_id={fid}")
        if len(todo) > 20:
            print(f"  ... 共 {len(todo)} 场")
        return 0
    ok = 0
    for i, (sporttery_match_id, fixture_id) in enumerate(todo, 1):
        try:
            players = fetch_player_statistics_for_fixture(fixture_id, rate_limit_sec=rate_sec)
            si.save_api_football_player_statistics(
                api_football_fixture_id=fixture_id,
                players=players,
                sporttery_match_id=sporttery_match_id,
            )
            ok += 1
            if i % 50 == 0 or i == len(todo):
                print(f"  已拉取 {i}/{len(todo)} 场")
        except Exception as e:
            err_msg = str(e)
            if "limit" in err_msg.lower() or "超限" in err_msg or "套餐" in err_msg or "计划" in err_msg or "request limit" in err_msg.lower():
                print("", file=sys.stderr)
                print("API 请求次数已达上限，本次已保存进度。请明日再运行本命令断点续拉：", file=sys.stderr)
                print("  python3 main.py players-stats-by-jingcai --years 2026 [--rate-limit 0.05]", file=sys.stderr)
                return 1
            print(f"  sporttery_match_id={sporttery_match_id} fixture_id={fixture_id} 失败: {e}", file=sys.stderr)
    print(f"完成: 成功 {ok}/{len(todo)} 场。数据已写入 self_improvement_football.api_football_player_statistics。")
    return 0 if ok == len(todo) else 1


def cmd_topscorers_api(args: argparse.Namespace) -> int:
    """射手榜：从 API-FOOTBALL 拉取指定联赛+赛季的 players/topscorers，写入 api_football_topscorers。不传 --leagues 时拉取全部联赛。"""
    from datetime import date
    from db import self_improvement as si
    from etl.api_football_players import fetch_topscorers
    from etl.api_football_teams import fetch_leagues
    from etl.injury_api_football import LEAGUE_IDS

    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY。", file=sys.stderr)
        return 1
    season_arg = getattr(args, "season", None)
    today = date.today()
    season = int(season_arg) if season_arg is not None else (today.year if today.month >= 8 else today.year - 1)
    leagues_arg = getattr(args, "leagues", None) or ""
    if leagues_arg.strip():
        try:
            league_ids = [int(x.strip()) for x in leagues_arg.split(",") if x.strip()]
        except ValueError:
            print("--leagues 需为逗号分隔的联赛 ID，如 39,140,135,78,61。", file=sys.stderr)
            return 1
    else:
        try:
            leagues = fetch_leagues(
                season=season,
                current_only=(season_arg is None),
                require_coverage="topscorers",
                rate_limit_sec=getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5),
            )
            league_ids = [int(l["id"]) for l in leagues if l.get("id") is not None]
            if not league_ids:
                league_ids = list(LEAGUE_IDS.keys())
        except Exception:
            league_ids = list(LEAGUE_IDS.keys())
    rate_sec = getattr(args, "rate_limit", None) or getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    skip_existing = getattr(args, "skip_existing", True)
    ok = 0
    n_skipped = 0
    n_fetched = 0
    skipped_zero = 0
    db = si.get_self_improvement_db()
    if skip_existing and db is not None:
        print(f"【断点续拉】赛季 {season}：共 {len(league_ids)} 个联赛；库中已有将跳过，只拉取缺失。")
    for league_id in league_ids:
        if skip_existing and db is not None and db["api_football_topscorers"].count_documents({"league_id": league_id, "season": season}) > 0:
            ok += 1
            n_skipped += 1
            continue
        try:
            players = fetch_topscorers(league_id, season, rate_limit_sec=rate_sec)
            if not players:
                skipped_zero += 1
                continue
            si.save_topscorers(
                league_id=league_id,
                season=season,
                players=players,
                league_name=LEAGUE_IDS.get(league_id),
            )
            ok += 1
            n_fetched += 1
            print(f"  联赛 {league_id} ({LEAGUE_IDS.get(league_id, '')}) {season} 赛季: {len(players)} 人")
        except Exception as e:
            err_msg = str(e)
            if "limit" in err_msg.lower() or "超限" in err_msg or "套餐" in err_msg or "计划" in err_msg or "request limit" in err_msg.lower():
                print("", file=sys.stderr)
                print("API 请求次数已达上限，本次已保存进度。明日重新执行同一命令可断点续拉（已存联赛会跳过）：", file=sys.stderr)
                print("  python3 main.py topscorers-api --season %s --rate-limit 0.3" % season, file=sys.stderr)
                return 1
            print(f"  联赛 {league_id} 失败: {e}", file=sys.stderr)
    if n_skipped or n_fetched:
        print(f"【断点续拉】跳过 {n_skipped} 个联赛（已有），本次拉取 {n_fetched} 个；合计 {ok}/{len(league_ids)}。")
    if skipped_zero:
        print(f"  其中 {skipped_zero} 个联赛无射手榜数据已跳过。")
    print(f"完成: 成功 {ok}/{len(league_ids)} 个联赛。数据已写入 self_improvement_football.api_football_topscorers。")
    return 0 if ok == len(league_ids) else 1


def _teams_api_league_season_args(args: argparse.Namespace):
    """解析 season、leagues；未传 --leagues 时从 API 拉取全部联赛列表，返回 (season, league_ids, err)。"""
    from datetime import date
    from etl.api_football_teams import fetch_leagues
    from etl.injury_api_football import LEAGUE_IDS
    season_arg = getattr(args, "season", None)
    today = date.today()
    season = int(season_arg) if season_arg is not None else (today.year if today.month >= 8 else today.year - 1)
    leagues_arg = getattr(args, "leagues", None) or ""
    if leagues_arg.strip():
        try:
            league_ids = [int(x.strip()) for x in leagues_arg.split(",") if x.strip()]
        except ValueError:
            return None, None, "--leagues 需为逗号分隔的联赛 ID，如 39,140,135,78,61。"
    else:
        # 未传 --leagues：从 API 拉取全部联赛（当前赛季）
        try:
            leagues = fetch_leagues(season=season, current_only=(season_arg is None), rate_limit_sec=getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5))
            league_ids = [int(l["id"]) for l in leagues if l.get("id") is not None]
            if not league_ids:
                league_ids = list(LEAGUE_IDS.keys())
        except Exception:
            league_ids = list(LEAGUE_IDS.keys())
    return season, league_ids, None


def _is_api_rate_limit_error(e: Exception) -> bool:
    err = str(e)
    return "limit" in err.lower() or "超限" in err or "套餐" in err or "计划" in err or "request limit" in err.lower()


def cmd_teams_statistics_api(args: argparse.Namespace) -> int:
    """球队赛季统计：拉取 teams/statistics（场均进球、控球、射门等），写入 api_football_team_statistics。遇限流时优雅退出，明日用同一命令断点续拉（--skip-existing 会跳过已存联赛+球队）。"""
    from db import self_improvement as si
    from etl.api_football_teams import fetch_teams, fetch_team_statistics
    from etl.injury_api_football import LEAGUE_IDS

    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY。", file=sys.stderr)
        return 1
    season, league_ids, err = _teams_api_league_season_args(args)
    if err:
        print(err, file=sys.stderr)
        return 1
    rate_sec = getattr(args, "rate_limit", None) or getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    skip_existing = getattr(args, "skip_existing", True)
    db = si.get_self_improvement_db()
    total_ok = 0
    total_skipped = 0
    total_fetched = 0
    leagues_skipped_no_request = 0
    if skip_existing and db is not None:
        print(f"【断点续拉】赛季 {season}：共 {len(league_ids)} 个联赛；整联赛已有数据则不请求 API，只处理有缺失的联赛。")
    for league_id in league_ids:
        existing_team_ids = set()
        if skip_existing and db is not None:
            existing_team_ids = set(
                db["api_football_team_statistics"].distinct("team_id", {"league_id": league_id, "season": season})
            )
            # 该联赛+赛季已有数据则整联赛跳过，不请求 fetch_teams，省 1 次 API（新升级球队需用 --no-skip-existing 补拉）
            if existing_team_ids:
                n_existing = len(existing_team_ids)
                total_ok += n_existing
                total_skipped += n_existing
                leagues_skipped_no_request += 1
                print(f"  联赛 {league_id} ({LEAGUE_IDS.get(league_id, '')}) {season}: 整联赛已有 {n_existing} 队，跳过（未请求 API）")
                continue
        try:
            teams = fetch_teams(league_id, season, rate_limit_sec=rate_sec)
        except Exception as e:
            if _is_api_rate_limit_error(e):
                print("", file=sys.stderr)
                print("API 请求次数已达上限，本次已保存进度。明日重新执行同一命令可断点续拉（已存球队会跳过）：", file=sys.stderr)
                print("  python3 main.py teams-statistics-api --season %s --rate-limit 0.3" % season, file=sys.stderr)
                return 1
            raise
        if not teams:
            print(f"  联赛 {league_id} ({LEAGUE_IDS.get(league_id, '')}): 无球队列表")
            continue
        n_ok = 0
        n_skip_league = 0
        n_fetch_league = 0
        for t in teams:
            tid = t.get("id")
            tname = (t.get("name") or "").strip()
            if tid is None:
                continue
            if skip_existing and int(tid) in existing_team_ids:
                n_ok += 1
                n_skip_league += 1
                continue
            try:
                stats = fetch_team_statistics(league_id, int(tid), season, rate_limit_sec=rate_sec)
                if stats:
                    si.save_team_statistics(league_id, int(tid), season, stats, team_name=tname)
                    n_ok += 1
                    n_fetch_league += 1
            except Exception as e:
                if _is_api_rate_limit_error(e):
                    print("", file=sys.stderr)
                    print("API 请求次数已达上限，本次已保存进度。明日重新执行同一命令可断点续拉（已存球队会跳过）：", file=sys.stderr)
                    print("  python3 main.py teams-statistics-api --season %s --rate-limit 0.3" % season, file=sys.stderr)
                    return 1
                print(f"  球队 {tid} ({tname}) 失败: {e}", file=sys.stderr)
        total_ok += n_ok
        total_skipped += n_skip_league
        total_fetched += n_fetch_league
        print(f"  联赛 {league_id} ({LEAGUE_IDS.get(league_id, '')}) {season}: 跳过 {n_skip_league} 队（已有），本次拉取 {n_fetch_league} 队；已存 {n_ok} 条")
    if total_skipped or total_fetched:
        print(f"【断点续拉】合计跳过 {total_skipped} 队（已有），本次拉取 {total_fetched} 队。")
    if leagues_skipped_no_request:
        print(f"  其中 {leagues_skipped_no_request} 个联赛整联赛已有数据，未请求 API。")
    print(f"完成: 共 {total_ok} 条球队赛季统计，已写入 self_improvement_football.api_football_team_statistics。")
    return 0


def cmd_squads_api(args: argparse.Namespace) -> int:
    """球队阵容：拉取 players/squads（当前阵容名单），写入 api_football_squads。遇限流时优雅退出，明日用同一命令断点续拉（--skip-existing 会跳过已存球队）。"""
    from db import self_improvement as si
    from etl.api_football_teams import fetch_teams, fetch_squads
    from etl.injury_api_football import LEAGUE_IDS

    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY。", file=sys.stderr)
        return 1
    season, league_ids, err = _teams_api_league_season_args(args)
    if err:
        print(err, file=sys.stderr)
        return 1
    rate_sec = getattr(args, "rate_limit", None) or getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    skip_existing = getattr(args, "skip_existing", True)
    db = si.get_self_improvement_db()
    existing_team_ids = set()
    if skip_existing and db is not None:
        existing_team_ids = set(int(x) for x in db["api_football_squads"].distinct("team_id") if x is not None)
    if skip_existing and existing_team_ids:
        print(f"【断点续拉】赛季 {season}：库中已有 {len(existing_team_ids)} 队，将跳过；只拉取缺失球队。")
    print("【省 API】球队列表优先从 api_football_team_statistics 读取，无数据时才请求 API。")
    ok = 0
    n_skipped = 0
    n_fetched = 0
    leagues_no_request = 0
    for league_id in league_ids:
        teams = None
        this_league_list_from_db = False
        if db is not None:
            team_ids_from_db = db["api_football_team_statistics"].distinct("team_id", {"league_id": league_id, "season": season})
            if team_ids_from_db:
                teams = [{"id": int(tid)} for tid in team_ids_from_db if tid is not None]
                this_league_list_from_db = True
        if not teams:
            try:
                teams = fetch_teams(league_id, season, rate_limit_sec=rate_sec)
            except Exception as e:
                if _is_api_rate_limit_error(e):
                    print("", file=sys.stderr)
                    print("API 请求次数已达上限，本次已保存进度。明日重新执行同一命令可断点续拉（已存球队会跳过）：", file=sys.stderr)
                    print("  python3 main.py squads-api --season %s --rate-limit 0.3" % season, file=sys.stderr)
                    return 1
                raise
        if this_league_list_from_db:
            leagues_no_request += 1
        if not teams:
            continue
        for t in teams:
            tid = t.get("id")
            tname = (t.get("name") or "").strip()
            if tid is None:
                continue
            if skip_existing and int(tid) in existing_team_ids:
                ok += 1
                n_skipped += 1
                continue
            try:
                players = fetch_squads(int(tid), rate_limit_sec=rate_sec)
                si.save_squads(int(tid), players, season=season, team_name=tname)
                existing_team_ids.add(int(tid))
                ok += 1
                n_fetched += 1
            except Exception as e:
                if _is_api_rate_limit_error(e):
                    print("", file=sys.stderr)
                    print("API 请求次数已达上限，本次已保存进度。明日重新执行同一命令可断点续拉（已存球队会跳过）：", file=sys.stderr)
                    print("  python3 main.py squads-api --season %s --rate-limit 0.3" % season, file=sys.stderr)
                    return 1
                print(f"  球队 {tid} ({tname}) 失败: {e}", file=sys.stderr)
        print(f"  联赛 {league_id} ({LEAGUE_IDS.get(league_id, '')}): {len(teams)} 队" + ("（列表来自库，未请求 API）" if this_league_list_from_db else ""))
    if n_skipped or n_fetched:
        print(f"【断点续拉】跳过 {n_skipped} 队（已有），本次拉取 {n_fetched} 队；合计 {ok} 队。")
    if leagues_no_request:
        print(f"  其中 {leagues_no_request} 个联赛的球队列表来自 api_football_team_statistics，未请求 API。")
    print(f"完成: 成功 {ok} 队阵容，已写入 self_improvement_football.api_football_squads。")
    return 0


def cmd_coaches_api(args: argparse.Namespace) -> int:
    """教练信息：拉取 coaches?team=，写入 api_football_coaches。遇限流时优雅退出，明日用同一命令断点续拉（--skip-existing 会跳过已存球队）。"""
    from db import self_improvement as si
    from etl.api_football_teams import fetch_teams, fetch_coach
    from etl.injury_api_football import LEAGUE_IDS

    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY。", file=sys.stderr)
        return 1
    season, league_ids, err = _teams_api_league_season_args(args)
    if err:
        print(err, file=sys.stderr)
        return 1
    rate_sec = getattr(args, "rate_limit", None) or getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    skip_existing = getattr(args, "skip_existing", True)
    db = si.get_self_improvement_db()
    existing_team_ids = set()
    if skip_existing and db is not None:
        existing_team_ids = set(int(x) for x in db["api_football_coaches"].distinct("team_id") if x is not None)
    if skip_existing and existing_team_ids:
        print(f"【断点续拉】赛季 {season}：库中已有 {len(existing_team_ids)} 队，将跳过；只拉取缺失球队。")
    print("【省 API】球队列表优先从 api_football_team_statistics 读取，无数据时才请求 API。")
    ok = 0
    n_skipped = 0
    n_fetched = 0
    leagues_no_request = 0
    for league_id in league_ids:
        teams = None
        this_league_list_from_db = False
        if db is not None:
            team_ids_from_db = db["api_football_team_statistics"].distinct("team_id", {"league_id": league_id, "season": season})
            if team_ids_from_db:
                teams = [{"id": int(tid)} for tid in team_ids_from_db if tid is not None]
                this_league_list_from_db = True
        if not teams:
            try:
                teams = fetch_teams(league_id, season, rate_limit_sec=rate_sec)
            except Exception as e:
                if _is_api_rate_limit_error(e):
                    print("", file=sys.stderr)
                    print("API 请求次数已达上限，本次已保存进度。明日重新执行同一命令可断点续拉（已存球队会跳过）：", file=sys.stderr)
                    print("  python3 main.py coaches-api --season %s --rate-limit 0.3" % season, file=sys.stderr)
                    return 1
                raise
        if this_league_list_from_db:
            leagues_no_request += 1
        if not teams:
            continue
        for t in teams:
            tid = t.get("id")
            tname = (t.get("name") or "").strip()
            if tid is None:
                continue
            if skip_existing and int(tid) in existing_team_ids:
                ok += 1
                n_skipped += 1
                continue
            try:
                coaches = fetch_coach(int(tid), rate_limit_sec=rate_sec)
                si.save_coach(int(tid), coaches, team_name=tname)
                existing_team_ids.add(int(tid))
                ok += 1
                n_fetched += 1
            except Exception as e:
                if _is_api_rate_limit_error(e):
                    print("", file=sys.stderr)
                    print("API 请求次数已达上限，本次已保存进度。明日重新执行同一命令可断点续拉（已存球队会跳过）：", file=sys.stderr)
                    print("  python3 main.py coaches-api --season %s --rate-limit 0.3" % season, file=sys.stderr)
                    return 1
                print(f"  球队 {tid} ({tname}) 失败: {e}", file=sys.stderr)
        print(f"  联赛 {league_id} ({LEAGUE_IDS.get(league_id, '')}): {len(teams)} 队" + ("（列表来自库，未请求 API）" if this_league_list_from_db else ""))
    if n_skipped or n_fetched:
        print(f"【断点续拉】跳过 {n_skipped} 队（已有），本次拉取 {n_fetched} 队；合计 {ok} 队。")
    if leagues_no_request:
        print(f"  其中 {leagues_no_request} 个联赛的球队列表来自 api_football_team_statistics，未请求 API。")
    print(f"完成: 成功 {ok} 队教练信息，已写入 self_improvement_football.api_football_coaches。")
    return 0


def cmd_lineup_stats(args: argparse.Namespace) -> int:
    """阵容统计：已完赛且有阵容数据的场次的场均首发人数。"""
    from db import self_improvement as si
    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    date_from = getattr(args, "date_from", None) or None
    date_to = getattr(args, "date_to", None) or None
    data = si.lineup_stats_aggregate(date_from=date_from, date_to=date_to)
    print(f"日期范围: {data.get('date_from') or '(全部)'} ~ {data.get('date_to') or '(全部)'}")
    print(f"已完赛: {data.get('total_finished', 0)} 场，有阵容数据: {data.get('total_with_lineup', 0)} 场")
    ah = data.get("avg_home_starting")
    aa = data.get("avg_away_starting")
    if ah is not None:
        print(f"场均主队首发: {ah}")
    if aa is not None:
        print(f"场均客队首发: {aa}")
    return 0


def cmd_lineup_comparison(args: argparse.Namespace) -> int:
    """阵容对比（复盘）：预计 vs 实际首发、关键球员缺阵。"""
    import json as _json
    from db import self_improvement as si
    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    date_from = getattr(args, "date_from", None) or None
    date_to = getattr(args, "date_to", None) or None
    limit = getattr(args, "limit", 200) or 200
    as_json = getattr(args, "json", False)
    items = si.lineup_comparison_report(date_from=date_from, date_to=date_to, limit=limit)
    if as_json:
        print(_json.dumps({"items": items, "count": len(items)}, ensure_ascii=False, indent=2))
        return 0
    if not items:
        print("无数据（请检查日期范围及是否已拉取阵容 lineups-by-jingcai）。")
        return 0
    print(f"共 {len(items)} 场")
    print(
        "日期\t联赛\t主队\t客队\t比分\t预计主缺\t预计客缺\t实际主首发\t实际客首发\t"
        "预计主首发人数\t预计客首发人数\t实际主关键首发\t实际客关键首发\t实际主关键缺\t实际客关键缺"
    )
    for r in items:
        score = f"{r.get('home_score')}:{r.get('away_score')}" if r.get("home_score") is not None and r.get("away_score") is not None else "—"
        print(
            f"{r.get('jc_date', '')}\t{r.get('league', '')}\t{r.get('home_cn', '')}\t{r.get('away_cn', '')}\t{score}\t"
            f"{r.get('pred_home_key_absent', '—')}\t{r.get('pred_away_key_absent', '—')}\t"
            f"{r.get('actual_home_starting_count', '—')}\t{r.get('actual_away_starting_count', '—')}\t"
            f"{r.get('pred_home_starting_count', '—')}\t{r.get('pred_away_starting_count', '—')}\t"
            f"{r.get('actual_home_key_starting', '—')}\t{r.get('actual_away_key_starting', '—')}\t"
            f"{r.get('actual_home_key_absent', '—')}\t{r.get('actual_away_key_absent', '—')}"
        )
    return 0


def cmd_lineup_key_absent_analysis(args: argparse.Namespace) -> int:
    """对比「预计主/客缺」与「实际主/客关键缺」，看是否系统性高估/低估，供调 KEY_WEIGHT 或是否用预计首发参与 key_count。"""
    from db import self_improvement as si
    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    years_str = getattr(args, "years", None) or ""
    years = [int(x.strip()) for x in years_str.split(",") if x.strip() and x.strip().isdigit()]
    date_from = getattr(args, "date_from", None) or None
    date_to = getattr(args, "date_to", None) or None
    if not years and not date_from and not date_to:
        print("请指定 --years 或 --date-from/--date-to，如 --years 2024,2025,2026 或 --date-from 2024-01-01 --date-to 2026-12-31。", file=sys.stderr)
        return 1
    by_year = getattr(args, "by_year", False)
    as_json = getattr(args, "json", False)
    driver = getattr(args, "driver", "match_lineups") or "match_lineups"
    by_league = getattr(args, "by_league", False)
    export_injury_path = getattr(args, "export_injury_params", None) or None
    if export_injury_path:
        by_league = True
    if getattr(args, "simulate_predicted_lineup", False):
        print("lineup-key-absent-analysis: 正在统计偏差并模拟「启用预计首发」（约 2 万场，请稍候）…", flush=True)
    data = si.lineup_key_absent_analysis(
        date_from=date_from,
        date_to=date_to,
        years=years if years else None,
        by_year=by_year,
        by_league=by_league,
        driver=driver,
        export_injury_params_path=export_injury_path,
        simulate_predicted_lineup=getattr(args, "simulate_predicted_lineup", False),
    )
    if as_json:
        import json as _json
        print(_json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if data.get("total_matches", 0) == 0:
        print(data.get("message", "无有效数据。"))
        return 0
    n = data["total_matches"]
    print(f"驱动: {driver}（{'以 match_lineups 已落库场次为准，覆盖全量' if driver == 'match_lineups' else '以竞彩按日列表为准'}）")
    print(f"日期范围: {data.get('date_from') or '(未指定)'} ~ {data.get('date_to') or '(未指定)'}")
    print(f"有效场次（有实际关键缺阵）: {n}")
    print()
    for side, label in [("home", "主队"), ("away", "客队")]:
        s = data.get(side, {})
        if not s:
            continue
        mean = s.get("mean")
        median = s.get("median")
        pct_over = s.get("pct_over")
        pct_under = s.get("pct_under")
        pct_exact = s.get("pct_exact")
        print(f"【{label}】预计缺 - 实际关键缺 的偏差（正=高估缺阵，负=低估缺阵）")
        if mean is not None:
            print(f"  均值: {mean}, 中位数: {median}")
        if pct_over is not None:
            print(f"  高估(>0): {pct_over}%, 低估(<0): {pct_under}%, 持平(=0): {pct_exact}%")
        print()
    if by_year and data.get("by_year"):
        print("按年统计:")
        for y, v in sorted(data["by_year"].items()):
            print(f"  {y}: n={v['n']}, 主队均值={v['home'].get('mean')}, 客队均值={v['away'].get('mean')}")
        print()
    if by_league and data.get("by_league"):
        print("按联赛统计（预计缺-实际关键缺 均值越负越低估，可对该联赛单独调高 KEY_WEIGHT）:")
        for lig, v in list(data["by_league"].items())[:30]:
            md = v.get("mean_diff")
            sug = v.get("suggested_PREDICT_INJURY_KEY_PLAYER_WEIGHT")
            suf = f"，建议权重={sug}" if sug is not None else ""
            print(f"  {lig}: n={v['n']}, 主客均差={md}{suf}")
        if len(data.get("by_league", {})) > 30:
            print(f"  ... 共 {len(data['by_league'])} 个联赛")
        if export_injury_path:
            print(f"  已导出建议到 {export_injury_path}，可用 merge-injury-params 合并进 predict_league_params.json。")
        else:
            print("  优化: 使用 --export-injury-params 导出后，运行 merge-injury-params 合并进 data/predict_league_params.json。")
        print()
    debug = data.get("_empty_league_debug") or []
    if debug:
        print("【联赛为空样本】前 %d 条联赛解析为空的文档（便于补解析路径）:" % len(debug))
        for i, d in enumerate(debug, 1):
            print("  [%d] match_id=%s jc_date=%s league_cn_raw=%r has_match_head=%s top_keys=%s" % (i, d.get("match_id"), d.get("jc_date"), d.get("league_cn_raw"), d.get("has_match_head"), d.get("top_keys")))
        print("  若 top_keys 与主集合不同，可用 main.py alt-jingcai-structure --from-file <导出的一条.json> 看完整结构后补 _get_league_cn_from_analysis_doc。\n")
    print("调参建议:")
    home_mean = (data.get("home") or {}).get("mean")
    away_mean = (data.get("away") or {}).get("mean")
    overall_mean = None
    if home_mean is not None and away_mean is not None:
        overall_mean = (home_mean + away_mean) / 2
    elif home_mean is not None:
        overall_mean = home_mean
    elif away_mean is not None:
        overall_mean = away_mean
    if overall_mean is not None:
        if overall_mean > 0.3:
            print("  当前「预计缺」系统性高于「实际关键缺」→ 可考虑降低 PREDICT_INJURY_KEY_PLAYER_WEIGHT，或保持关闭预计首发参与 key_count。")
        elif overall_mean < -0.3:
            print("  当前「预计缺」系统性低于「实际关键缺」→ 可考虑提高 PREDICT_INJURY_KEY_PLAYER_WEIGHT，或启用 JC_USE_PREDICTED_LINEUP_FOR_KEY_ABSENT 让预计首发参与 key_count。")
        else:
            print("  偏差接近 0，关键球员缺阵估计较准；可按需微调 KEY_WEIGHT 或预计首发开关。")
    sim = data.get("simulate_use_predicted_lineup")
    if sim:
        print()
        print("【若启用 JC_USE_PREDICTED_LINEUP_FOR_KEY_ABSENT】模拟偏差（预计缺会升高，与实际上更接近）:")
        for side, label in [("home", "主队"), ("away", "客队")]:
            s = sim.get(side, {})
            if s and s.get("mean") is not None:
                print(f"  {label}: 均值={s.get('mean')}, 中位数={s.get('median')}, 高估={s.get('pct_over')}%, 低估={s.get('pct_under')}%, 持平={s.get('pct_exact')}%")
        rec_text = data.get("recommendation_text")
        if rec_text:
            print()
            print("【对比结论与建议】")
            print("  " + rec_text)
        print()
        print("【两项优化说明】")
        print("  1) 启用 JC_USE_PREDICTED_LINEUP_FOR_KEY_ABSENT（.env 设为 true）→ 预计缺升高，偏差向 0 靠拢；有预计首发的场次预测更贴近实际。")
        print("  2) 提高 PREDICT_INJURY_KEY_PLAYER_WEIGHT（或按联赛在 predict_league_params.json 调高）→ 不改变预计缺数值，但预测时对缺阵反应更强；需 repredict 后 jc-evaluate 看命中率。")
    return 0


def cmd_lineup_backfill_key_absence(args: argparse.Namespace) -> int:
    """将「关键球员 vs 实际首发」写入 match_lineups：对已有完整首发的场次用 jingcai 关键球员计算并落库四字段。"""
    from db import self_improvement as si
    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    years_str = getattr(args, "years", None) or ""
    years = [int(x.strip()) for x in years_str.split(",") if x.strip() and x.strip().isdigit()]
    date_from = getattr(args, "date_from", None) or None
    date_to = getattr(args, "date_to", None) or None
    limit = getattr(args, "limit", None)
    skip_existing = not getattr(args, "no_skip_existing", False)
    result = si.lineups_backfill_key_absence(
        years=years if years else None,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        skip_existing=skip_existing,
    )
    print(f"已更新: {result['updated']} 场")
    if result.get("skipped_no_jingcai", 0) > 0:
        print(f"  跳过（无 jingcai 文档）: {result['skipped_no_jingcai']}")
    if result.get("skipped_no_lineup", 0) > 0:
        print(f"  跳过（无完整首发名单或无可算关键球员）: {result['skipped_no_lineup']}")
    if result.get("skipped_date", 0) > 0:
        print(f"  跳过（日期/年份不在范围）: {result['skipped_date']}")
    return 0


def cmd_lineup_diagnostic(args: argparse.Namespace) -> int:
    """诊断阵容与关键缺阵数据量，便于排查「数据少」问题。"""
    from db import self_improvement as si
    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    years_str = getattr(args, "years", "2021,2022,2023,2024,2025,2026") or ""
    years = [int(x.strip()) for x in years_str.split(",") if x.strip() and x.strip().isdigit()]
    data = si.lineup_diagnostic_counts(years=years if years else None)
    if data.get("error"):
        print(data["error"], file=sys.stderr)
        return 1
    print("阵容/关键缺阵数据量诊断（years=%s）" % (years or "全部"))
    print("  jingcai 总文档数（主+备用集合）: %s" % data.get("jingcai_total_docs", 0))
    print("  jingcai 指定年份内文档数: %s" % data.get("jingcai_in_years_docs", 0))
    print("  jingcai 指定年份内已完赛（有比分）: %s" % data.get("jingcai_in_years_finished", 0))
    print("  match_lineups 总条数: %s" % data.get("match_lineups_total", 0))
    print("  match_lineups 有完整首发名单: %s" % data.get("match_lineups_with_starting", 0))
    print("  match_lineups 已落库关键缺阵四字段: %s" % data.get("match_lineups_with_key_absence", 0))
    print()
    print("说明：lineup-key-absent-analysis 的「有效场次」以「竞彩按日列表 + 已完赛(有比分)」为驱动，")
    print("  只统计该路径下且 match_lineups 有阵容+关键缺阵的场次；若比 match_lineups 已落库数少，")
    print("  多为部分场次未在按日列表中或比分未解析到，可用 lineup-comparison 看明细。")
    return 0


def _print_jingcai_doc_structure(result: dict, title: str) -> None:
    import json as _json
    print("【" + title + "】")
    print("source/collection:", result.get("source") or result.get("collection"), "match_id:", result.get("match_id") or result.get("sporttery_match_id"))
    print("顶层键:", result.get("top_level_keys"))
    print("联赛相关路径:", result.get("league_paths_found") or "无")
    print("结构摘要:")
    print(_json.dumps(result.get("structure_sample"), ensure_ascii=False, indent=2, default=str))


def cmd_alt_jingcai_structure(args: argparse.Namespace) -> int:
    """从备用集合 jingcai_match_anlysis 取一条文档并打印结构；备用不存在则打主集合一条。支持 --from-file 读取本地 JSON。"""
    from db import self_improvement as si
    import json as _json
    from_file = getattr(args, "from_file", None)
    if from_file:
        result = si.get_jingcai_doc_structure_from_file(from_file)
    else:
        if si.get_self_improvement_db() is None:
            print("未配置 MONGO_URI。", file=sys.stderr)
            return 1
        result = si.get_alt_jingcai_doc_structure()
    if result.get("error"):
        print(result["error"], file=sys.stderr)
        return 1
    title = "从文件解析的文档结构" if from_file else "备用/主集合一条文档结构"
    print("【" + title + "】")
    if result.get("note"):
        print("说明:", result["note"])
    print("source/collection:", result.get("source") or result.get("collection"), "match_id:", result.get("match_id") or result.get("sporttery_match_id"))
    if result.get("league_parsed"):
        print("联赛解析结果:", result["league_parsed"])
    print("顶层键:", result.get("top_level_keys"))
    print("联赛相关路径:", result.get("league_paths_found") or "无")
    print("结构摘要:")
    print(_json.dumps(result.get("structure_sample"), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_main_jingcai_empty_structure(args: argparse.Namespace) -> int:
    """从主集合找一条「联赛解析为空」的文档并打印结构，用于补全解析路径。"""
    from db import self_improvement as si
    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI。", file=sys.stderr)
        return 1
    years = getattr(args, "years", None)
    if years is not None and isinstance(years, str):
        years = [int(y.strip()) for y in years.split(",") if y.strip()]
    result = si.get_main_jingcai_empty_league_doc_structure(
        max_checks=getattr(args, "max_checks", 500),
        years=years,
        from_lineups=getattr(args, "from_lineups", False),
    )
    if result.get("error"):
        print(result["error"], file=sys.stderr)
        return 1
    _print_jingcai_doc_structure(result, "主集合中联赛解析为空的一条文档结构")
    return 0


def cmd_lineup_league_diagnostic(args: argparse.Namespace) -> int:
    """诊断为何按联赛统计多为空或只有瑞典杯：抽样查看 jingcai 文档与 jc_matches 的实际结构。"""
    from db import self_improvement as si
    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法访问 self_improvement_football。", file=sys.stderr)
        return 1
    n = getattr(args, "sample", 5) or 5
    result = si.lineup_league_diagnostic(sample_n=n)
    if result.get("error"):
        print(result["error"], file=sys.stderr)
        return 1
    print("【联赛解析诊断】抽样 %d 场，查看 jingcai 文档与 jc_matches 中联赛字段\n" % n)
    for i, s in enumerate(result.get("samples") or [], 1):
        print("  [%d] match_id=%s  jc_date=%s  解析联赛=%s" % (i, s.get("match_id"), s.get("jc_date"), s.get("league_parsed")))
        print("      jingcai 顶层键: %s" % (s.get("jingcai_top_keys") or []))
        if s.get("match_head_keys"):
            print("      match_head 键: %s" % s["match_head_keys"])
        if s.get("pageTitle_keys"):
            print("      pageTitle 键: %s" % s["pageTitle_keys"])
        if s.get("pageTitle_league_values"):
            print("      pageTitle 联赛相关值: %s" % s["pageTitle_league_values"])
        print()
    jc = result.get("jc_matches_sample")
    if jc:
        print("  jc_matches 某日(%s) 首条: 键=%s  联赛相关=%s" % (jc.get("date"), jc.get("first_item_keys"), jc.get("league_fields")))
    print("\n%s" % (result.get("message") or ""))
    return 0


def cmd_backfill_league_cn(args: argparse.Namespace) -> int:
    """对 jingcai 分析表中缺 league_cn 的文档用当前解析逻辑写回 league_cn。"""
    from db import self_improvement as si
    if si.get_self_improvement_db() is None:
        print("未配置 MONGO_URI。", file=sys.stderr)
        return 1
    use_all = getattr(args, "all", False)
    years = None
    if not use_all:
        years_str = getattr(args, "years", None) or "2024,2025,2026"
        years = [int(y.strip()) for y in years_str.split(",") if y.strip()]
    dry_run = getattr(args, "dry_run", False)
    result = si.backfill_league_cn_in_jingcai_analysis(years=years, dry_run=dry_run)
    if result.get("error"):
        print(result["error"], file=sys.stderr)
        return 1
    print(f"league_cn 回填: 已更新 {result.get('updated', 0)} 条, 跳过 {result.get('skipped', 0)} 条, 解析仍为空 {result.get('empty_parsed', 0)} 条" + (" (dry-run)" if result.get("dry_run") else ""))
    return 0


def cmd_merge_injury_params(args: argparse.Namespace) -> int:
    """将 lineup-key-absent-analysis --export-injury-params 生成的 JSON 合并进 predict_league_params.json。"""
    from db import self_improvement as si
    injury_path = getattr(args, "injury_params", None) or ""
    league_path = getattr(args, "league_params", None) or None
    if not injury_path:
        print("请指定 --injury-params 文件路径（由 lineup-key-absent-analysis --export-injury-params 生成）。", file=sys.stderr)
        return 1
    result = si.merge_injury_params_into_predict_league(injury_path, league_path)
    if result.get("error"):
        print(result["error"], file=sys.stderr)
        return 1
    merged = result.get("merged") or []
    out_path = result.get("league_params_path") or ""
    if merged:
        print(f"已合并 PREDICT_INJURY_KEY_PLAYER_WEIGHT 到 {out_path}，联赛: {', '.join(merged)}")
    else:
        print(result.get("message", "无 for_merge 可合并（injury 文件中无已映射联赛时 for_merge 为空；请确认 config.LEAGUE_CN_TO_EN 含该联赛或先运行 lineup-key-absent-analysis --by-league 查看联赛名）。"))
    return 0


def cmd_h2h_backfill(args: argparse.Namespace) -> int:
    """历史交锋：按 DB 中主客对拉取 API-Football Head-to-Head 并写入 matches。仅处理 team_id 来自 API-Football（≥90000）的对。"""
    import time
    from db import repo
    from etl.api_football_h2h import (
        _competition_id_to_league_id,
        _our_team_id_to_api,
        fetch_h2h_fixtures,
        merge_h2h_into_matches,
    )

    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY。", file=sys.stderr)
        return 1
    years_str = getattr(args, "years", None) or ""
    years = [int(x.strip()) for x in years_str.split(",") if x.strip()]
    if not years:
        print("请指定 --years，如 --years 2024,2025。", file=sys.stderr)
        return 1
    repo.init_db()
    pairs = repo.get_distinct_h2h_pairs(years=years, limit=getattr(args, "limit", None), big5_only=True)
    af_pairs = []
    for hid, aid, cid in pairs:
        api_h = _our_team_id_to_api(hid)
        api_a = _our_team_id_to_api(aid)
        if api_h is not None and api_a is not None:
            af_pairs.append((hid, aid, cid, api_h, api_a))
    if not af_pairs:
        print("未找到 API-Football 来源的主客对（team_id >= 90000）。请先运行 ingest-api-football 拉取赛程。")
        return 0
    rate_limit = getattr(args, "rate_limit", None) or getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    dry_run = getattr(args, "dry_run", False)
    skip_if_have = getattr(args, "skip_if_have", 6)  # 默认 6：已有 ≥6 场的主客对不再请求，省次数且便于断点续跑
    per_season = getattr(args, "per_season", False)  # 是否对 years 里每个赛季各请求一次（耗 API，慎用）
    # 每组只请求 1 个赛季（默认用 years 里最大赛季），避免每组 N 次请求耗光配额
    # 若最大年份为未来赛季（如 2026/27 尚未开赛），API 会返回 0 场；改为用当前/已结束赛季或不传 season 拉全历史
    from datetime import date
    _today = date.today()
    _current_season_start = _today.year if _today.month >= 8 else _today.year - 1
    season_for_fetch = max(years) if years else 2024
    if season_for_fetch > _current_season_start:
        season_for_fetch = _current_season_start
        print(f"注意: --years 含未来赛季，API 改用当前赛季 {season_for_fetch} 拉取历史交锋（否则返回 0 场）。")
    print(f"共 {len(af_pairs)} 组主客对（仅 AF 来源），每组 1 次 API（赛季={season_for_fetch}），将拉取历史交锋并写入 matches。")
    if skip_if_have > 0:
        to_fetch = sum(1 for (hid, aid, cid) in [(p[0], p[1], p[2]) for p in af_pairs] if repo.count_h2h_matches(hid, aid, cid) < skip_if_have)
        print(f"  --skip-if-have {skip_if_have}：已有 ≥{skip_if_have} 场的主客对跳过；实际将请求 {to_fetch} 组。")
    if per_season:
        print("  --per-season：已开启，将对 --years 中每个赛季各请求 1 次（每组最多 %d 次 API）。" % len(years))
    if dry_run:
        for hid, aid, cid, api_h, api_a in af_pairs[:20]:
            n = repo.count_h2h_matches(hid, aid, cid)
            skip = "（已有 %d 场，将跳过）" % n if skip_if_have > 0 and n >= skip_if_have else ""
            print(f"  {hid} vs {aid} (comp={cid}) -> API {api_h}-{api_a} %s" % skip)
        if len(af_pairs) > 20:
            print(f"  ... 共 {len(af_pairs)} 组")
        return 0
    total_ins = 0
    total_skip = 0
    total_skipped_pairs = 0
    for i, (hid, aid, cid, api_h, api_a) in enumerate(af_pairs, 1):
        if skip_if_have > 0:
            n_exist = repo.count_h2h_matches(hid, aid, cid)
            if n_exist >= skip_if_have:
                total_skipped_pairs += 1
                print(f"[{i}/{len(af_pairs)}] {api_h}-{api_a} 已有 {n_exist} 场，跳过")
                continue
        league_id = _competition_id_to_league_id(cid)
        all_fixtures = []
        seen_fid = set()
        seasons_to_request = years if per_season else [season_for_fetch]
        pair_failed = False
        for season in seasons_to_request:
            try:
                fixtures = fetch_h2h_fixtures(api_h, api_a, league_id=league_id, season=season)
                for f in fixtures:
                    fid = f.get("fixture_id")
                    if fid and fid not in seen_fid:
                        seen_fid.add(fid)
                        all_fixtures.append(f)
                if season != seasons_to_request[-1]:
                    time.sleep(rate_limit)
            except Exception as e:
                pair_failed = True
                print(f"[{i}/{len(af_pairs)}] {api_h}-{api_a} 联赛={league_id} 赛季={season} 失败: {e}", file=sys.stderr)
                break
        if pair_failed:
            continue
        try:
            ins, skip = merge_h2h_into_matches(all_fixtures, rate_limit_sec=rate_limit)
            total_ins += ins
            total_skip += skip
            print(f"[{i}/{len(af_pairs)}] {api_h}-{api_a} 联赛={league_id} 共{len(all_fixtures)}场: 新增 {ins} 场, 已存在 {skip} 场")
        except Exception as e:
            print(f"[{i}/{len(af_pairs)}] {api_h}-{api_a} merge 失败: {e}", file=sys.stderr)
        if i < len(af_pairs):
            time.sleep(rate_limit)
    if total_skipped_pairs:
        print(f"完成: 共新增 {total_ins} 场历史交锋，已存在跳过 {total_skip} 场；{total_skipped_pairs} 组主客对因已有足够场次未请求 API。")
    else:
        print(f"完成: 共新增 {total_ins} 场历史交锋，已存在跳过 {total_skip} 场。")
    return 0


def cmd_odds_api(args: argparse.Namespace) -> int:
    """赔率：从 API-FOOTBALL 拉取指定日期五大联赛赔率，写入 DB（match_odds）。"""
    from datetime import date
    from etl.api_football_odds import fetch_odds_for_date

    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY。", file=sys.stderr)
        return 1
    date_str = getattr(args, "date", None)
    date_used = (date_str or "").strip()[:10] if date_str else date.today().isoformat()
    if len(date_used) != 10:
        print("日期格式需为 YYYY-MM-DD。", file=sys.stderr)
        return 1
    rate_limit = getattr(args, "rate_limit", None)
    merge = getattr(args, "merge", True)
    try:
        _, message = fetch_odds_for_date(date_used, rate_limit_sec=rate_limit or config.API_FOOTBALL_RATE_LIMIT_SEC, merge=merge)
        print(message)
        return 0
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


def cmd_odds_api_by_db(args: argparse.Namespace) -> int:
    """赔率：按 DB 中 2024/2025/2026 等比赛日期（或按赛季日期范围）批量拉取赔率并 merge，补全现有比赛数据。支持 SQLite 与 MongoDB。"""
    from datetime import date
    import re
    from db import repo
    from scripts.fetch_odds_by_match_dates import _parse_years
    from etl.api_football_odds import fetch_odds_for_date_from_db, RATE_LIMIT_SEC

    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY。", file=sys.stderr)
        return 1
    years_str = getattr(args, "years", None)
    years = _parse_years(years_str)
    limit = getattr(args, "limit", None)
    dry_run = getattr(args, "dry_run", False)
    rate_limit = getattr(args, "rate_limit", None) or RATE_LIMIT_SEC
    skip_existing = getattr(args, "skip_existing", False)
    include_future = getattr(args, "include_future", False)
    stop_loss_zero_streak = max(0, int(getattr(args, "stop_loss_zero_write_streak", 0) or 0))

    # 从当前 DB（SQLite 或 MongoDB）取五大联赛比赛日
    dates = repo.get_distinct_match_dates(limit=limit, years=years, big5_only=True)
    if not dates:
        print("DB 中无符合条件之 match_date（可指定 --years 2024,2025,2026 或去掉 --years 拉全部）。")
        return 0

    # 默认只处理「不晚于今天」的日期，避免对未来日大量请求却 API 无赛程/赔率导致 0 场
    today_str = date.today().isoformat()
    if not include_future:
        before = len(dates)
        dates = [d for d in dates if d <= today_str]
        if before > len(dates):
            print(f"跳过未来日期 {before - len(dates)} 个（仅拉取 ≤ 今天 {today_str} 的日期；若需包含未来可加 --include-future）。")

    if skip_existing:
        existing_dates = {str(d)[:10] for d in repo.match_odds_get_dates() if d and len(str(d)[:10]) == 10}
        before = len(dates)
        dates = [d for d in dates if d not in existing_dates]
        if before > len(dates):
            print(f"跳过已存在赔率的 {before - len(dates)} 个日期，待拉取 {len(dates)} 个日期。")
    if not dates:
        print("所有日期在 match_odds 中已有赔率或均为未来日期，无需拉取。")
        return 0

    print(f"共 {len(dates)} 个日期: {dates[0]} ~ {dates[-1]}")
    if dry_run:
        for d in dates:
            print(f"赔率 {d}")
        return 0

    ok = 0
    zero_write_streak = 0
    for i, date_str in enumerate(dates, 1):
        try:
            _, msg = fetch_odds_for_date_from_db(date_str, rate_limit_sec=rate_limit, merge=True)
            print(f"[{i}/{len(dates)}] 赔率 {date_str} {msg}")
            ok += 1
            m = re.search(r"本次写入\s*(\d+)\s*场", msg)
            wrote = int(m.group(1)) if m else None
            if wrote == 0:
                zero_write_streak += 1
            else:
                zero_write_streak = 0
            if stop_loss_zero_streak > 0 and zero_write_streak >= stop_loss_zero_streak:
                print(
                    f"触发自动止损：连续 {zero_write_streak} 天本次写入 0 场，停止后续日期请求（可调 --stop-loss-zero-write-streak）。"
                )
                break
        except Exception as e:
            print(f"[{i}/{len(dates)}] 赔率 {date_str} 失败: {e}", file=sys.stderr)
    print(f"完成: {ok}/{len(dates)} 个日期已拉取并 merge。")
    return 0 if ok == len(dates) else 1


def cmd_standings_api(args: argparse.Namespace) -> int:
    """积分榜：从 API-FOOTBALL 拉取五大联赛当季积分榜，写入 DB（league_standings）。"""
    from datetime import date
    from etl.api_football_standings import fetch_and_save_standings

    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY。", file=sys.stderr)
        return 1
    season_arg = getattr(args, "season", None)
    today = date.today()
    season = int(season_arg) if season_arg is not None else (today.year if today.month >= 8 else today.year - 1)
    rate_limit = getattr(args, "rate_limit", None)
    try:
        _, message = fetch_and_save_standings(season, rate_limit_sec=rate_limit or config.API_FOOTBALL_RATE_LIMIT_SEC)
        print(message)
        return 0
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


def cmd_standings_backfill(args: argparse.Namespace) -> int:
    """历史五大联赛积分：按 DB 中比赛赛季拉取积分榜并写入 league_standings，再为每场比赛回填 home_rank/away_rank。"""
    from etl.api_football_standings import (
        backfill_standings_for_db_matches,
        backfill_match_ranks,
    )

    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY。", file=sys.stderr)
        return 1
    rate_limit = getattr(args, "rate_limit", None) or getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    seasons_arg = getattr(args, "seasons", None)  # "2022,2023,2024" or None
    seasons = None
    if seasons_arg:
        try:
            seasons = [int(s.strip()) for s in seasons_arg.split(",") if s.strip()]
        except ValueError:
            print("--seasons 格式应为年份逗号分隔，如 2022,2023,2024", file=sys.stderr)
            return 1
    skip_fetch = getattr(args, "no_fetch", False)
    try:
        if not skip_fetch:
            done, msg = backfill_standings_for_db_matches(rate_limit_sec=rate_limit, seasons=seasons)
            print(msg)
        updated, msg2 = backfill_match_ranks()
        print(msg2)
        return 0
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


def cmd_backfill_log_support(args: argparse.Namespace) -> int:
    """根据 prediction_log 中已填比分的条目，补拉涉及日期的赔率与涉及赛季的积分榜，便于 repredict-history 后复盘更准。"""
    from engine.prediction_log import load_log

    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY。", file=sys.stderr)
        return 1
    entries = load_log()
    with_actual = [
        e for e in entries
        if e.get("actual_home_score") is not None and e.get("actual_away_score") is not None
    ]
    if not with_actual:
        print("prediction_log 中暂无已填比分的条目，无需补拉。")
        return 0
    dates = set()
    seasons = set()
    for e in with_actual:
        d = (e.get("date") or "")[:10]
        if len(d) == 10:
            dates.add(d)
            y, m = int(d[:4]), int(d[5:7])
            seasons.add(y if m >= 8 else y - 1)
    max_dates = getattr(args, "max_dates", 15) or 15
    dates_sorted = sorted(dates, reverse=True)[:max_dates]
    rate = getattr(config, "API_FOOTBALL_RATE_LIMIT_SEC", 6.5)
    from etl.api_football_odds import fetch_odds_for_date
    from etl.api_football_standings import fetch_and_save_standings
    import time
    for d in dates_sorted:
        try:
            _, msg = fetch_odds_for_date(d, rate_limit_sec=rate, merge=True)
            print(f"赔率 {d}: {msg}")
        except Exception as ex:
            print(f"赔率 {d} 失败: {ex}", file=sys.stderr)
        time.sleep(rate)
    # 积分榜文件当前仅存一季，只拉 log 中涉及的最新赛季
    if seasons:
        s = max(seasons)
        try:
            _, msg = fetch_and_save_standings(s, rate_limit_sec=rate)
            print(f"积分榜 赛季 {s}: {msg}")
        except Exception as ex:
            print(f"积分榜 赛季 {s} 失败: {ex}", file=sys.stderr)
    print("补拉完成。建议执行: python main.py repredict-history && python main.py prediction-log recap")
    return 0


def _statistics_api_logging_setup():
    """为 statistics-api 命令配置日志，便于查看执行到哪一步。"""
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(fmt)
    for name in ("statistics_api", "etl.api_football_statistics", "db.self_improvement"):
        lgr = logging.getLogger(name)
        lgr.setLevel(logging.INFO)
        if not lgr.handlers:
            lgr.addHandler(h)
    return logging.getLogger("statistics_api")


def cmd_statistics_missing_by_year(args: argparse.Namespace) -> int:
    """查看单场统计缺少年份：按年显示竞彩库中已有 api_football_fixture_id 但缺 api_football_statistics 的场次数。"""
    from db.self_improvement import get_self_improvement_db
    from etl.api_football_statistics import statistics_missing_by_year

    if get_self_improvement_db() is None:
        print("未配置 MONGO_URI，无法按竞彩库按年统计单场统计缺失。", file=sys.stderr)
        return 1
    year_arg = getattr(args, "year", None)
    years = [year_arg] if year_arg is not None else None
    out = statistics_missing_by_year(years=years)
    by_year = out.get("by_year") or {}
    print("--- 单场统计（api_football_statistics）按年覆盖 ---")
    print("说明：竞彩库中已有 fixture_id 的场次中，已写入单场统计的场次数与缺失场次数。")
    print(f"合计: 有 fixture_id {out['total_with_fixture_id']} 场, 已有统计 {out['total_has_statistics']} 场, 缺失 {out['total_missing']} 场")
    if by_year:
        print("按年:")
        for y, b in by_year.items():
            print(f"  {y}: 有 fixture_id {b['with_fixture_id']}, 已有统计 {b['has_statistics']}, 缺失 {b['missing']}")
    years_missing = out.get("years_missing") or []
    if years_missing:
        print(f"缺单场统计的年份: {', '.join(years_missing)}")
        print("补全命令示例: python main.py statistics-api --jingcai-years " + ",".join(years_missing) + " --merge --resume")
    else:
        print("所有统计年份均已覆盖，无缺失。")
    return 0


def cmd_statistics_api(args: argparse.Namespace) -> int:
    """统计：从 API-FOOTBALL 拉取比赛统计，写入 data/api_football_statistics.json，并写入 self_improvement_football.api_football_statistics（需 MONGO_URI）。场次来源可为竞彩库指定年份或按日期/年拉取五大联赛。"""
    from datetime import date, timedelta
    from etl.api_football_statistics import fetch_and_save_statistics_for_date, fetch_and_save_statistics_for_jingcai_years

    log = _statistics_api_logging_setup()

    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        log.error("请在 .env 中设置 API_FOOTBALL_KEY")
        return 1
    rate_limit = getattr(args, "rate_limit", None)
    merge = getattr(args, "merge", True)
    rate_sec = rate_limit if rate_limit is not None else config.API_FOOTBALL_RATE_LIMIT_SEC
    jingcai_years_arg = getattr(args, "jingcai_years", None)

    # 优先按 jingcai_match_analysis 指定年份拉取（需 MONGO_URI）
    if jingcai_years_arg and (jingcai_years_arg or "").strip():
        try:
            years = [int(y.strip()) for y in jingcai_years_arg.split(",") if y.strip()]
        except ValueError:
            log.error("--jingcai-years 需为逗号分隔的年份，如 2025,2026")
            return 1
        if not years:
            log.error("--jingcai-years 需至少指定一个年份")
            return 1
        if not getattr(config, "MONGO_URI", None) or not (config.MONGO_URI or "").strip():
            log.error("使用 --jingcai-years 需在 .env 中设置 MONGO_URI")
            return 1
        save_batch = getattr(args, "save_batch", 50)
        resume = getattr(args, "resume", False)
        dry_run = getattr(args, "dry_run", False)
        if dry_run:
            from db.self_improvement import jc_match_analysis_fixture_ids_for_years
            fixture_ids = jc_match_analysis_fixture_ids_for_years(years)
            print(f"[dry-run] 将拉取竞彩 {years} 年共 {len(fixture_ids)} 场统计（不请求 API）", flush=True)
            return 0
        log.info(
            "开始执行 statistics-api：模式=竞彩库年份，years=%s，merge=%s，rate_limit_sec=%s，save_batch=%s，resume=%s",
            years, merge, rate_sec, save_batch, resume,
        )
        print(
            f"statistics-api: 开始拉取竞彩 {years} 年单场统计，merge={merge}，rate_limit={rate_sec}s，每 {save_batch} 场写入一次，resume={resume}",
            flush=True,
        )
        try:
            total, with_data, message = fetch_and_save_statistics_for_jingcai_years(
                years, rate_limit_sec=rate_sec, merge=merge, save_batch_size=save_batch, resume=resume
            )
            log.info("执行完成：共 %d 场，%d 场有统计", total, with_data)
            print(message, flush=True)
            print(f"合计: {total} 场（{with_data} 场有统计）", flush=True)
            return 0
        except Exception as e:
            log.exception("statistics-api 执行失败")
            print(f"错误: {e}", file=sys.stderr)
            return 1

    # 确定要执行的日期列表：优先 --date，其次 --dates，其次 --year
    dates_to_run: list[str] = []
    date_str = getattr(args, "date", None)
    dates_arg = getattr(args, "dates", None)
    year_arg = getattr(args, "year", None)

    if date_str and (date_str or "").strip():
        date_used = (date_str or "").strip()[:10]
        if len(date_used) != 10:
            print("日期格式需为 YYYY-MM-DD。", file=sys.stderr)
            return 1
        dates_to_run = [date_used]
    elif dates_arg and (dates_arg or "").strip():
        for s in (dates_arg or "").split(","):
            d = s.strip()[:10]
            if len(d) == 10:
                dates_to_run.append(d)
        if not dates_to_run:
            print("--dates 需为逗号分隔的 YYYY-MM-DD 列表。", file=sys.stderr)
            return 1
    elif year_arg is not None:
        try:
            y = int(year_arg)
        except (TypeError, ValueError):
            print("--year 需为整数年份，如 2025。", file=sys.stderr)
            return 1
        start = date(y, 1, 1)
        end = date(y, 12, 31)
        d = start
        while d <= end:
            dates_to_run.append(d.isoformat())
            d += timedelta(days=1)
    else:
        date_used = date.today().isoformat()
        dates_to_run = [date_used]

    if getattr(args, "dry_run", False):
        print(f"[dry-run] 将按日期拉取共 {len(dates_to_run)} 天统计（不请求 API）", flush=True)
        if len(dates_to_run) <= 10:
            print(f"  日期: {', '.join(dates_to_run)}", flush=True)
        else:
            print(f"  日期范围: {dates_to_run[0]} ~ {dates_to_run[-1]}", flush=True)
        return 0
    log.info("开始执行 statistics-api：模式=按日期，共 %d 天，merge=%s，rate_limit_sec=%s", len(dates_to_run), merge, rate_sec)
    if len(dates_to_run) <= 3:
        log.info("日期列表: %s", dates_to_run)
    else:
        log.info("日期范围: %s ~ %s", dates_to_run[0], dates_to_run[-1])
    total_fetched = 0
    try:
        for i, date_used in enumerate(dates_to_run):
            log.info("处理第 %d/%d 天: %s", i + 1, len(dates_to_run), date_used)
            n, message = fetch_and_save_statistics_for_date(
                date_used,
                rate_limit_sec=rate_sec,
                merge=merge,
            )
            total_fetched += n
            print(f"[{i + 1}/{len(dates_to_run)}] {date_used}: {message}")
        log.info("执行完成：共 %d 天，%d 场有统计", len(dates_to_run), total_fetched)
        if len(dates_to_run) > 1:
            print(f"合计: {len(dates_to_run)} 天, {total_fetched} 场有统计")
        return 0
    except Exception as e:
        log.exception("statistics-api 执行失败")
        print(f"错误: {e}", file=sys.stderr)
        return 1


def cmd_ingest_fd(args: argparse.Namespace) -> int:
    """拉取 football-data.org 赛事（默认五大联赛；可用 --leagues 指定杯赛等）。"""
    from etl.football_data_ingest import run_ingest_football_data, reset_fd_data
    seasons_arg = getattr(args, "seasons", "2023,2024,2025,2026")
    seasons = [int(x.strip()) for x in str(seasons_arg).split(",") if x.strip()]
    if not seasons:
        seasons = [2023, 2024, 2025, 2026]
    leagues_arg = getattr(args, "leagues", None)
    if leagues_arg:
        leagues = [x.strip() for x in str(leagues_arg).split(",") if x.strip()]
    else:
        leagues = config.FOOTBALL_DATA_BIG5
    if getattr(args, "reset_fd_data", False):
        stats = reset_fd_data()
        print(
            "已清空 football-data 旧数据："
            f"events={stats['events']} matches={stats['matches']} "
            f"seasons={stats['seasons']} competitions={stats['competitions']} teams={stats['teams']}"
        )
    run_ingest_football_data(
        leagues=leagues,
        seasons=seasons,
        finished_only=getattr(args, "finished_only", False),
    )
    return 0


def cmd_ingest_api_history(args: argparse.Namespace) -> int:
    """
    用 API-FOOTBALL 重拉五大联赛历史完赛比赛（同源于后续伤停/赔率/积分）。
    """
    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY（或 API_FOOTBALL_KEYS）。", file=sys.stderr)
        return 1
    from etl.api_football_match_ingest import (
        ingest_finished_big5_history,
        reset_big5_history_for_seasons,
    )
    years_arg = getattr(args, "seasons", "2023,2024,2025,2026")
    seasons = [int(x.strip()) for x in str(years_arg).split(",") if x.strip()]
    if not seasons:
        seasons = [2023, 2024, 2025, 2026]
    exclude_ids: set[int] | None = None
    if getattr(args, "exclude_jingcai_big5", False):
        from db.self_improvement import jc_big5_api_football_fixture_ids

        exclude_ids = jc_big5_api_football_fixture_ids()
        print(
            f"已启用 --exclude-jingcai-big5：将跳过竞彩五大联赛已登记的 api_football_fixture_id，共 {len(exclude_ids)} 个"
        )
    if getattr(args, "reset", False):
        deleted = reset_big5_history_for_seasons(seasons)
        print(
            "已清理历史比赛："
            f"matches={deleted['matches']} events={deleted['events']} "
            f"predictions={deleted['predictions']} prediction_log={deleted['prediction_log']}"
        )
    out = ingest_finished_big5_history(
        seasons=seasons,
        rate_limit_sec=getattr(args, "rate_limit", None),
        exclude_fixture_ids=exclude_ids,
    )
    print(
        f"API 历史完赛入库完成：inserted={out['inserted']} "
        f"updated={out['updated']} skipped={out['skipped']}"
    )
    if getattr(args, "sync_mongo", False):
        rc = cmd_sync_sqlite_to_mongo(argparse.Namespace())
        if rc != 0:
            return rc
    return 0


def cmd_ingest_api_upcoming(args: argparse.Namespace) -> int:
    """
    用 API-FOOTBALL 拉取五大联赛未开赛场次（NS/TBD/TIMED）并入库，补齐未来赛程（如 2026-02-24 及以后）。
    """
    if not getattr(config, "API_FOOTBALL_KEYS", None) or not [k for k in config.API_FOOTBALL_KEYS if (k or "").strip()]:
        print("请在 .env 中设置 API_FOOTBALL_KEY（或 API_FOOTBALL_KEYS）。", file=sys.stderr)
        return 1
    from etl.api_football_match_ingest import ingest_upcoming_big5
    years_arg = getattr(args, "seasons", "2025,2026")
    seasons = [int(x.strip()) for x in str(years_arg).split(",") if x.strip()]
    if not seasons:
        seasons = [2025, 2026]
    out = ingest_upcoming_big5(
        seasons=seasons,
        rate_limit_sec=getattr(args, "rate_limit", None),
    )
    print(
        f"API 未开赛入库完成：inserted={out['inserted']} "
        f"updated={out['updated']} skipped={out['skipped']}"
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    match_id = int(args.match_id)
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT home_score, away_score FROM matches WHERE id = :mid"),
            {"mid": match_id},
        ).fetchone()
    if row and (row[0] is None or row[1] is None):
        print("该场尚未结束，无法生成复盘报告。", file=sys.stderr)
        return 1
    payload = build_report_payload(engine, match_id)
    if not payload:
        print("Match not found or no events.", file=sys.stderr)
        return 1
    assembler = ReportAssembler()
    md = assembler.generate(payload)
    reports_dir = config.DATA_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = save_report_md(md, reports_dir / f"match_{match_id}.md")
    html_path = save_report_html(md, reports_dir / f"match_{match_id}.html")
    print(f"Report saved: {md_path}")
    print(f"HTML:         {html_path}")
    if getattr(args, "open", False):
        import webbrowser
        webbrowser.open(f"file://{html_path.resolve()}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """列出已入库比赛：id、日期、主队 vs 客队、比分，便于选 match_id 做 report。"""
    engine = get_engine()
    if not config.DB_PATH.exists():
        print("尚无数据，请先执行: python main.py ingest", file=sys.stderr)
        return 1
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, match_date, home_score, away_score, raw_meta FROM matches ORDER BY id")
        ).fetchall()
    if not rows:
        print("暂无比赛记录，请先执行: python main.py ingest", file=sys.stderr)
        return 1
    print("id | 日期       | 主队 vs 客队 | 比分")
    print("-" * 60)
    for r in rows:
        mid, date, hs, aws, raw = r[0], r[1] or "", r[2], r[3], r[4] or "{}"
        try:
            meta = json.loads(raw)
        except Exception:
            meta = {}
        home = meta.get("home_team_name", "?")
        away = meta.get("away_team_name", "?")
        score = "待赛" if (hs is None or aws is None) else f"{hs}-{aws}"
        print(f"{mid:3} | {date} | {home} vs {away} | {score}")
    return 0


def cmd_seasons(args: argparse.Namespace) -> int:
    """列出 StatsBomb 开放数据中指定年份的可用赛事（用于查 competition_id / season_id 再 ingest）。"""
    years_arg = getattr(args, "years", "2023,2024,2025,2026")
    years = [int(x.strip()) for x in str(years_arg).split(",") if x.strip()]
    if not years:
        years = [2023, 2024, 2025, 2026]
    from etl.ingest import list_seasons_for_years
    try:
        rows = list_seasons_for_years(years)
    except Exception as e:
        print("获取赛事列表失败（需网络）:", e, file=sys.stderr)
        return 1
    if not rows:
        print("未找到 %s 年的开放数据，可改试: python main.py seasons --years 2022,2021" % years)
        return 0
    print("StatsBomb 开放数据中 %s 年相关赛事（拉取时用 --competition 与 --season）：" % years)
    print("-" * 70)
    print("%8s %8s %-28s %s" % ("comp_id", "season_id", "赛事", "赛季"))
    print("-" * 70)
    for r in rows:
        comp_name = (r.get("competition_name") or "?")[:28]
        print("%8s %8s %-28s %s" % (r["competition_id"], r["season_id"], comp_name, r.get("season_name", "?")))
    print()
    print("示例: python main.py ingest --competition 9 --season 281 --all   # 德甲 2023/24")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Football Analysis CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="Fetch StatsBomb open data and load into DB")
    ing.add_argument("--max-matches", type=int, default=30, help="最多拉取场数（默认 30），与 --all 二选一")
    ing.add_argument("--all", action="store_true", dest="all", help="拉满该赛季全部比赛（不限制场数）")
    ing.add_argument("--competition", type=int, default=11, help="Competition ID (default 11 La Liga)")
    ing.add_argument("--season", type=int, default=90, help="Season ID (default 90 2020/21)")
    ing.add_argument("--force-events", action="store_true", help="对已入库但缺事件的比赛重新拉取事件（可补救网络超时）")
    ing.add_argument("--years", type=str, metavar="YEAR[,YEAR...]", help="按年份拉取：拉满 2023–2026 所有可用赛事（如 --years 2023,2024,2025,2026）")
    ing.set_defaults(run=cmd_ingest)

    rep = sub.add_parser("report", help="Generate report for a match (DB match id)")
    rep.add_argument("match_id", help="比赛 ID（用 list 命令查看）")
    rep.add_argument("--open", action="store_true", help="Open HTML report in browser")
    rep.set_defaults(run=cmd_report)

    lst = sub.add_parser("list", help="列出已入库比赛（id、日期、主客队、比分），便于选 match_id")
    lst.set_defaults(run=cmd_list)

    sea = sub.add_parser("seasons", help="列出 2023–2026 等年份的可用赛事（查 comp_id/season_id 后用于 ingest）")
    sea.add_argument("--years", type=str, default="2023,2024,2025,2026", help="年份，逗号分隔，如 2023,2024")
    sea.set_defaults(run=cmd_seasons)

    fd = sub.add_parser("ingest-fd", help="拉取 football-data.org 赛事（默认五大联赛；--leagues 可拉杯赛等）")
    fd.add_argument("--seasons", type=str, default="2023,2024,2025,2026", help="赛季年份，逗号分隔")
    fd.add_argument("--leagues", type=str, default=None, help="赛事代码逗号分隔，不传则拉五大联赛。杯赛示例: CL,EL,DFB,CIT,FAC,CDR；英冠: ELC")
    fd.add_argument("--finished-only", action="store_true", help="仅写入已完赛（FINISHED）场次，不入库待赛")
    fd.add_argument("--reset-fd-data", action="store_true", help="入库前先清空已有 football-data 相关数据（fd_*）")
    fd.set_defaults(run=cmd_ingest_fd)

    api_hist = sub.add_parser(
        "ingest-api-history",
        help="用 API-FOOTBALL 重拉五大联赛历史完赛比赛（同源于伤停/赔率/积分）",
    )
    api_hist.add_argument(
        "--seasons",
        type=str,
        default="2023,2024,2025,2026",
        help="API 赛季起始年，逗号分隔（如 2021,2022,...,2026；需含 2020 才覆盖 2020/21 赛季前半）",
    )
    api_hist.add_argument("--reset", action="store_true", help="重拉前先删除这些赛季已入库的五大联赛比赛及其关联数据")
    api_hist.add_argument(
        "--exclude-jingcai-big5",
        action="store_true",
        help="跳过 jingcai_match_analysis 中五大联赛且已填 api_football_fixture_id 的场次，只补充竞彩未覆盖的五大联赛比赛",
    )
    api_hist.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次 API 请求后休眠秒数，不传则用 API_FOOTBALL_RATE_LIMIT_SEC")
    api_hist.add_argument("--sync-mongo", action="store_true", help="入库后自动执行 sync-sqlite-to-mongo 全量重写 MongoDB")
    api_hist.set_defaults(run=cmd_ingest_api_history)

    api_upcoming = sub.add_parser("ingest-api-upcoming", help="拉取五大联赛未开赛场次入库，补齐未来赛程（如 2026-02-24 及以后）")
    api_upcoming.add_argument("--seasons", type=str, default="2025,2026", help="赛季年份逗号分隔，默认 2025,2026")
    api_upcoming.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次 API 请求后休眠秒数")
    api_upcoming.set_defaults(run=cmd_ingest_api_upcoming)

    pred = sub.add_parser("predict", help="基于历史比分预测：主队 vs 客队 的期望进球与胜/平/负概率")
    pred.add_argument("home", help="主队名称（如 Arsenal FC 或 阿森纳）")
    pred.add_argument("away", help="客队名称")
    pred.add_argument("--competition", type=str, default=None, help="限定赛事（如 Premier League），可选")
    pred.add_argument("--last-n", type=int, default=10, help="参考最近 N 场，默认 10")
    pred.set_defaults(run=cmd_predict)

    pred_match = sub.add_parser("predict-match", help="对指定 match_id 做预测并落库，供事后 predict-eval 对比")
    pred_match.add_argument("match_id", help="比赛 ID（list 中可见；可为未开赛或已完赛）")
    pred_match.add_argument("--last-n", type=int, default=10, help="参考最近 N 场，默认 10")
    pred_match.set_defaults(run=cmd_predict_match)

    pred_eval = sub.add_parser("predict-eval", help="对比已落库预测与完赛结果：MAE、胜平负准确率；可选按 last_n/赛事拆解")
    pred_eval.add_argument("--today", action="store_true", help="只统计当天创建的预测；默认全量历史")
    pred_eval.add_argument("--by-last-n", action="store_true", help="按 last_n 分组输出各组准确率与 MAE")
    pred_eval.add_argument("--by-competition", action="store_true", help="按赛事分组输出各组准确率与 MAE")
    pred_eval.add_argument("--year", type=int, default=None, metavar="YEAR", help="只统计该年份的比赛，如 2025")
    pred_eval.add_argument("--by-year", action="store_true", help="按比赛年份（match_date 年）分组输出各组准确率与 MAE")
    pred_eval.set_defaults(run=cmd_predict_eval)

    repredict_h = sub.add_parser("repredict-history", help="用当前 config 对历史场次重算预测并写回 DB；仅主库 predictions，不含 jc_predictions；--db-only 时只写 predictions 表")
    repredict_h.add_argument("--db-only", action="store_true", help="只更新 DB 的 predictions 表，不触碰 prediction_log（复盘/评估用 predict-eval 与 prediction-log recap --from-db）")
    repredict_h.set_defaults(run=cmd_repredict_history)

    repredict_all_p = sub.add_parser("repredict-all", help="主库+竞彩一起重算：repredict-history 再按年跑 predict-jc-from-self-improvement，调参后一次更新两处")
    repredict_all_p.add_argument("--years", type=str, default="2025,2026", help="竞彩按年重算的年份，逗号分隔，默认 2025,2026")
    repredict_all_p.add_argument("--db-only", action="store_true", help="传给 repredict-history，只更新主库 predictions 表")
    repredict_all_p.add_argument("--verbose", "-v", action="store_true", help="竞彩预测时逐场打印")
    repredict_all_p.set_defaults(run=cmd_repredict_all)

    backfill_pred = sub.add_parser("backfill-predictions", help="对已完赛但无预测的场次批量落库预测（USE_MONGO 时仅 Mongo matches；predictions 按 match_id 去重）")
    backfill_pred.add_argument("--years", type=str, default=None, metavar="YEAR[,YEAR...]", help="只处理这些年份，如 2025 或 2024,2025；不传则全部")
    backfill_pred.add_argument("--limit", type=int, default=None, metavar="N", help="最多处理 N 场（用于试跑）")
    backfill_pred.add_argument("--dedup", action="store_true", help="仅 Mongo：先按 match_id 去重 predictions 再回填")
    backfill_pred.set_defaults(run=cmd_backfill_predictions)

    pred_dedup_p = sub.add_parser("predictions-dedup", help="仅 Mongo：按 match_id 去重 predictions 表，每组保留一条")
    pred_dedup_p.set_defaults(run=cmd_predictions_dedup)

    pred_consistency_p = sub.add_parser("predict-check-consistency", help="仅 Mongo：检查 predictions 与 matchs/matches 表是否一致")
    pred_consistency_p.add_argument("--delete-orphans", action="store_true", help="删除「match_id 在比赛表中不存在」的孤儿预测")
    pred_consistency_p.set_defaults(run=cmd_predict_check_consistency)

    predict_date_p = sub.add_parser("predict-for-date", help="对指定日期的 DB/Mongo 五大联赛场次用模型预测并写入 prediction_log，供 Web 仪表盘展示")
    predict_date_p.add_argument("--date", type=str, required=True, help="比赛日 YYYY-MM-DD")
    predict_date_p.set_defaults(run=cmd_predict_for_date)

    repredict_date_p = sub.add_parser("repredict-for-date", help="对指定日期强制重新预测：先删旧预测再重算，会分别保存 模型预测、API 预测、融合预测（需 PREDICT_USE_API_PREDICTIONS=true）")
    repredict_date_p.add_argument("--date", type=str, required=True, help="比赛日 YYYY-MM-DD")
    repredict_date_p.set_defaults(run=cmd_repredict_for_date)

    repredict_all_p = sub.add_parser("repredict-for-all", help="对所有比赛日（或 --years 指定年份）强制重新预测，分别保存 模型/API/融合 结果")
    repredict_all_p.add_argument("--years", type=str, default=None, help="可选，只处理这些年份，逗号分隔如 2024,2025,2026")
    repredict_all_p.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="API 请求后休眠秒数（仅影响拉取 API 预测时）；不传则用 .env 的 API_FOOTBALL_RATE_LIMIT_SEC，付费档可设 0.05 加速")
    repredict_all_p.add_argument("--skip-existing", action="store_true", help="只对「尚无预测」的场次补算，不删已有；API 限额后第二天断点续跑用")
    repredict_all_p.set_defaults(run=cmd_repredict_for_all)

    fill_api_p = sub.add_parser("fill-api-predictions", help="仅 Mongo：对「已有预测但缺 API 预测」的场次拉取 API 并只更新 API 字段，不重算模型；API 限额后补全用")
    fill_api_p.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="API 请求后休眠秒数，不传则用 .env 的 API_FOOTBALL_RATE_LIMIT_SEC")
    fill_api_p.add_argument("--years", type=str, default=None, help="只补指定年份比赛日的场次，逗号分隔如 2026；不传则补全部")
    fill_api_p.add_argument("--limit", type=int, default=None, metavar="N", help="最多只处理 N 场（用于试跑或诊断）")
    fill_api_p.add_argument("--verbose", action="store_true", help="首场「API 无预测」时打印该次完整 API 响应，便于排查套餐/解析问题")
    fill_api_p.set_defaults(run=cmd_fill_api_predictions)

    pred_api_stats_p = sub.add_parser("predictions-api-stats", help="仅 Mongo：统计 predictions 中有/无 API 预测条数，确认为何页面 API 列为空")
    pred_api_stats_p.add_argument("--years", type=str, default=None, help="只统计指定年份比赛日的预测，逗号分隔如 2025,2026")
    pred_api_stats_p.set_defaults(run=cmd_predictions_api_stats)

    predict_mongo_p = sub.add_parser("predict-from-mongo-matches", help="对 MongoDB matches 表中的比赛做模型预测并落库（仅 USE_MONGO）；供 Web 仪表盘展示")
    predict_mongo_p.add_argument("--dates", type=str, default=None, help="可选，日期范围 起,止 如 2026-02-01,2026-02-28")
    predict_mongo_p.add_argument("--date-from", type=str, default=None, help="可选，起始比赛日 YYYY-MM-DD")
    predict_mongo_p.add_argument("--date-to", type=str, default=None, help="可选，截止比赛日 YYYY-MM-DD")
    predict_mongo_p.add_argument("--limit", type=int, default=None, help="最多处理场数")
    predict_mongo_p.add_argument("--force", action="store_true", help="对已有预测的场次也重新预测并覆盖")
    predict_mongo_p.set_defaults(run=cmd_predict_from_mongo_matches)

    predict_jc_si_p = sub.add_parser("predict-jc-from-self-improvement", help="从 jingcai_match_analysis 按 jc_date 读赛程，用模型预测并写入 jc_predictions（不限于五大联赛）")
    predict_jc_si_p.add_argument("--date", type=str, default=None, help="单日比赛日 YYYY-MM-DD（与 --year/--years/--dates 四选一）")
    predict_jc_si_p.add_argument("--year", type=int, default=None, metavar="YYYY", help="该年所有竞彩日批量预测，如 2025")
    predict_jc_si_p.add_argument("--years", type=str, default=None, metavar="YYYY,YYYY,...", help="多个年份逗号分隔，如 2021,2022,2023,2024,2025,2026（与 --year 二选一）")
    predict_jc_si_p.add_argument("--dates", type=str, default=None, metavar="FROM,TO", help="日期区间，如 2026-02-01,2026-02-29")
    predict_jc_si_p.add_argument("--include-finished", action="store_true", help="对已完赛场次也写预测（用于回测与评估）；按年跑往年时未指定则自动开启")
    predict_jc_si_p.add_argument("--no-merge", action="store_true", help="不合并主库：主库无该队/赛事时不自动写入，仍按原逻辑跳过（默认会合并）")
    predict_jc_si_p.add_argument("--last-n", type=int, default=10, help="参考最近 N 场，默认 10")
    predict_jc_si_p.add_argument("--verbose", "-v", action="store_true", help="逐场打印联赛与预测/跳过原因")
    predict_jc_si_p.set_defaults(run=cmd_predict_jc_from_self_improvement)

    list_jc_p = sub.add_parser("list-jc-matches", help="列出指定日期 jingcai_match_analysis 中的场次及是否五大联赛（不跑预测）")
    list_jc_p.add_argument("--date", type=str, required=True, help="比赛日 YYYY-MM-DD")
    list_jc_p.set_defaults(run=cmd_list_jc_matches)

    recommend_bet_p = sub.add_parser("recommend-bet-slip", help="根据今日竞彩与 jc_predictions 推测下注单推荐（概率差距、模型置信度、联赛分散）")
    recommend_bet_p.add_argument("--date", type=str, default=None, help="竞彩日 YYYY-MM-DD，默认今天")
    recommend_bet_p.add_argument("--min-prob", type=float, default=None, metavar="P", help="最低推荐概率阈值，默认从配置 JC_REC_DEFAULT_MIN_PROB 读取（0.60 时展示命中率约 70%%）")
    recommend_bet_p.add_argument("--min-spread", type=float, default=0.06, help="推荐项与第二高概率的最小差距，默认 0.06（6%%）")
    recommend_bet_p.add_argument("--top", type=int, default=6, metavar="N", help="最多推荐几场，默认 6")
    recommend_bet_p.add_argument("--no-require-high-confidence", action="store_true", dest="no_require_high_confidence", help="不要求模型 confidence=high，也包含 low 置信预测")
    recommend_bet_p.add_argument("--big5-only", action="store_true", help="仅五大联赛")
    recommend_bet_p.add_argument("--max-per-league", type=int, default=2, metavar="N", help="取 top 时每联赛最多几场，默认 2；0 表示不限制")
    recommend_bet_p.set_defaults(run=cmd_recommend_bet_slip)

    jc_dates_diag_p = sub.add_parser("jc-dates-diagnose", help="诊断某年竞彩日：list_dates 结果与库中 jc_date/date/match_date 原始分布")
    jc_dates_diag_p.add_argument("--year", type=int, required=True, metavar="YYYY", help="年份，如 2025")
    jc_dates_diag_p.set_defaults(run=cmd_jc_dates_diagnose)

    jc_teams_sync_p = sub.add_parser("jc-teams-sync", help="从 jingcai_match_analysis 整理主客队队名到新表 jingcai_teams（去重）")
    jc_teams_sync_p.set_defaults(run=cmd_jc_teams_sync)
    jc_teams_sync_main_p = sub.add_parser("jc-teams-sync-to-main", help="将 jingcai_teams 中有 name_en 的队伍同步到主库 teams 表（按需插入）")
    jc_teams_sync_main_p.set_defaults(run=cmd_jc_teams_sync_to_main)
    jc_teams_list_p = sub.add_parser("jc-teams-list", help="列出 jingcai_teams 表中的全部队名")
    jc_teams_list_p.set_defaults(run=cmd_jc_teams_list)
    jc_teams_check_all_p = sub.add_parser("jc-teams-check-all", help="检查 jingcai_match_analysis 全表中所有队伍是否都在 jingcai_teams（可选先执行同步）")
    jc_teams_check_all_p.add_argument("--sync", action="store_true", help="先执行 jc-teams-sync 补充缺失队伍，再检查")
    jc_teams_check_all_p.set_defaults(run=cmd_jc_teams_check_all)
    jc_teams_check_year_p = sub.add_parser("jc-teams-check-year", help="检查指定年份在 jingcai_match_analysis 中的队伍是否都在 jingcai_teams（可选先执行回填）")
    jc_teams_check_year_p.add_argument("--year", type=int, default=2024, metavar="YYYY", help="年份，默认 2024")
    jc_teams_check_year_p.add_argument("--sync", action="store_true", help="先根据 jingcai_match_analysis 回填 jingcai_teams，再检查")
    jc_teams_check_year_p.set_defaults(run=cmd_jc_teams_check_year)
    jc_teams_map_p = sub.add_parser("jc-teams-map-api-football", help="将 jingcai_teams 中文名映射为英文名并补全 api_football_team_id（不限于五大联赛；缺 id 时用 API 按名查询）")
    jc_teams_map_p.add_argument("--no-api-search", action="store_true", dest="api_search_false", help="不调用 API 按名查询补全 id（仅用主库与本地映射）")
    jc_teams_map_p.add_argument("--rate-limit", type=float, default=0.25, metavar="SEC", help="API 按名查询时的请求间隔，默认 0.25")
    jc_teams_map_p.add_argument("--all", action="store_true", dest="process_all_teams", help="处理全部文档；默认仅处理没有 api_football_team_id 的文档")
    jc_teams_map_p.set_defaults(run=cmd_jc_teams_map_api_football)
    jc_analysis_backfill_p = sub.add_parser("jc-match-analysis-backfill-en", help="将 jingcai_teams 英文名回填到 jingcai_match_analysis 表（home_en/away_en）")
    jc_analysis_backfill_p.add_argument("--year", type=int, default=None, metavar="YYYY", help="仅处理该年份的数据，如 2023；不指定则处理全部")
    jc_analysis_backfill_p.add_argument("--overwrite", action="store_true", help="用表/映射覆盖已有 home_en/away_en（用于纠正错误，如 莱切斯特 误为 Lecce）")
    jc_analysis_backfill_p.set_defaults(run=cmd_jc_match_analysis_backfill_en)
    jc_fixture_backfill_p = sub.add_parser("jc-match-analysis-backfill-fixture-id", help="将 API Football 的 fixture_id 批量回填到 jingcai_match_analysis（不限于五大联赛）")
    jc_fixture_backfill_p.add_argument("--year", type=int, default=None, metavar="YYYY", help="仅处理该年份的数据，如 2026；不指定则处理全部")
    jc_fixture_backfill_p.add_argument("--date", type=str, default=None, metavar="YYYY-MM-DD", help="仅处理该竞彩日，如 2026-03-01")
    jc_fixture_backfill_p.add_argument("--rate-limit", type=float, default=1.0, metavar="SEC", help="每日期请求间隔秒数，默认 1.0")
    jc_fixture_backfill_p.add_argument("--overwrite", action="store_true", help="覆盖已有 api_football_fixture_id（默认不覆盖，已有则跳过且该日不请求 API）")
    jc_fixture_backfill_p.add_argument("--no-auto-backfill-en", action="store_true", help="不先执行英文名回填（默认会先跑 jc-match-analysis-backfill-en 再回填 fixture_id）")
    jc_fixture_backfill_p.set_defaults(run=cmd_jc_match_analysis_backfill_fixture_id)

    jc_priority_backfill_p = sub.add_parser(
        "jc-priority-backfill",
        help="优先回填：按顺序执行 fixture_id 回填 → 单场统计 → 阵容 → 事件 → 球员单场统计（只补缺不覆盖）",
    )
    jc_priority_backfill_p.add_argument("--years", type=str, default=None, metavar="YEAR,YEAR,...", help="年份逗号分隔，如 2021,2022,2023,2024,2025,2026；不传且无 date 时默认 2021～2026")
    jc_priority_backfill_p.add_argument("--date-from", type=str, default=None, metavar="YYYY-MM-DD", help="日期范围起（与 --date-to 一起可替代 --years）")
    jc_priority_backfill_p.add_argument("--date-to", type=str, default=None, metavar="YYYY-MM-DD", help="日期范围止")
    jc_priority_backfill_p.add_argument("--rate-limit", type=float, default=0.5, metavar="SEC", help="请求间隔秒数，默认 0.5")
    jc_priority_backfill_p.add_argument("--skip-fixture-id", action="store_true", help="跳过第 1 步 fixture_id 回填")
    jc_priority_backfill_p.add_argument("--skip-statistics", action="store_true", help="跳过第 2 步单场统计")
    jc_priority_backfill_p.add_argument("--skip-lineups", action="store_true", help="跳过第 3 步阵容")
    jc_priority_backfill_p.add_argument("--skip-events", action="store_true", help="跳过第 4 步事件")
    jc_priority_backfill_p.add_argument("--skip-players-stats", action="store_true", help="跳过第 5 步球员单场统计")
    jc_priority_backfill_p.add_argument("--daily", action="store_true", help="仅处理当年、当天+前一天的数据（不传 --years 时用此做每日回填）")
    jc_priority_backfill_p.set_defaults(run=cmd_jc_priority_backfill)

    jc_fixture_handling_p = sub.add_parser("jc-match-analysis-fixture-id-handling", help="查看/导出已匹配与未匹配 fixture_id 的统计及未匹配列表")
    jc_fixture_handling_p.add_argument("--stats", action="store_true", help="仅输出按年份的已匹配/未匹配统计")
    jc_fixture_handling_p.add_argument("--year", type=int, default=None, metavar="YYYY", help="仅统计/列出该年份，如 2025")
    jc_fixture_handling_p.add_argument("--limit", type=int, default=500, metavar="N", help="未匹配列表最多条数（默认 500）")
    jc_fixture_handling_p.add_argument("--export", type=str, default=None, metavar="FILE", help="将未匹配列表导出为 CSV 文件")
    jc_fixture_handling_p.set_defaults(run=cmd_jc_match_analysis_fixture_id_handling)

    jc_future_backfill_p = sub.add_parser("jc-match-analysis-backfill-future-matches", help="对 jingcai_match_analysis 中缺 future_matches 的文档仅拉取体彩未来赛程并回填，不重拉整条详情")
    jc_future_backfill_p.add_argument("--year", type=int, default=None, metavar="YYYY", help="仅处理该年份，如 2024")
    jc_future_backfill_p.add_argument("--date", type=str, default=None, metavar="YYYY-MM-DD", help="仅处理该竞彩日")
    jc_future_backfill_p.add_argument("--rate-limit", type=float, default=1.0, metavar="SEC", help="每场请求间隔秒数，默认 1.0")
    jc_future_backfill_p.add_argument("--overwrite", action="store_true", help="覆盖已有 future_matches")
    jc_future_backfill_p.add_argument("--limit", type=int, default=None, metavar="N", help="最多处理 N 条（试跑用）")
    jc_future_backfill_p.set_defaults(run=cmd_jc_match_analysis_backfill_future_matches)

    jc_api_pred_p = sub.add_parser("jc-api-predictions-sync", help="从 jingcai_match_analysis 拉取 API Football 预测并写入 jc_api_predictions")
    jc_api_pred_p.add_argument("--year", type=int, default=None, metavar="YYYY", help="仅处理该年份，如 2026")
    jc_api_pred_p.add_argument("--date", type=str, default=None, metavar="YYYY-MM-DD", help="仅处理该竞彩日，如 2026-03-01")
    jc_api_pred_p.add_argument("--rate-limit", type=float, default=1.0, metavar="SEC", help="每场请求间隔秒数，默认 1.0；付费档可设 0.05 加速")
    jc_api_pred_p.add_argument("--workers", type=int, default=1, metavar="N", help="并发请求数，默认 1；配合 --rate-limit 0.05 可显著提速")
    jc_api_pred_p.add_argument("--overwrite", action="store_true", help="覆盖已有预测记录")
    jc_api_pred_p.set_defaults(run=cmd_jc_api_predictions_sync)

    jc_api_dump_p = sub.add_parser("jc-api-predictions-dump-raw", help="打印一条 jc_api_predictions 的 raw，用于确认 API 是否返回比分")
    jc_api_dump_p.add_argument("--fixture", type=int, default=None, metavar="ID", help="指定 api_football_fixture_id，不传则取第一条")
    jc_api_dump_p.set_defaults(run=cmd_jc_api_predictions_dump_raw)

    jc_eval_p = sub.add_parser("jc-predictions-eval", help="根据 jc_predictions 与 jingcai_match_analysis 已完赛比分，统计胜平负/精确比分命中率")
    jc_eval_p.add_argument("--all-time", action="store_true", dest="all_time", help="全量统计，不按年/日期筛选")
    jc_eval_p.add_argument("--year", type=int, default=None, metavar="YYYY", help="按年统计，如 2025、2026")
    jc_eval_p.add_argument("--date-from", type=str, default=None, dest="date_from", metavar="YYYY-MM-DD", help="日期区间起点，需与 --date-to 同时使用")
    jc_eval_p.add_argument("--date-to", type=str, default=None, dest="date_to", metavar="YYYY-MM-DD", help="日期区间终点，如 2026-02-29")
    jc_eval_p.add_argument("--recap", action="store_true", help="输出单场推荐未命中复盘：每场推荐项、实际结果、简短原因，便于看为什么会错")
    jc_eval_p.add_argument("--export-missed", type=str, default=None, metavar="FILE", help="将未命中复盘导出为 CSV 文件")
    jc_eval_p.add_argument("--missed-limit", type=int, default=500, metavar="N", help="未命中复盘最多保留/导出条数，默认 500；0 表示不限制（可导出全部约 9 千+ 条）")
    jc_eval_p.add_argument("--export-missed-chunk-size", type=int, default=0, metavar="N", dest="export_missed_chunk_size", help="导出时按 N 条分文件，如 5000 则生成 stem_part001.csv、stem_part002.csv…；0 表示不分文件（默认）")
    jc_eval_p.add_argument("--export-segment-stats", type=str, default=None, metavar="FILE", dest="export_segment_stats", help="将「联赛×推荐类型×概率区间」基线命中率写入 JSON，供后续 --segment-stats 做数据驱动自我提升")
    jc_eval_p.add_argument("--segment-stats", type=str, default=None, metavar="FILE", dest="segment_stats", help="读入历史细分命中率 JSON；命中率低于阈值的细分将改推胜/负（根据未命中复盘自我提升）")
    jc_eval_p.add_argument("--export-league-rec-params", type=str, default=None, metavar="FILE", dest="export_league_rec_params", help="将各联赛单场推荐命中率及达 70%% 目标所需最低概率写入 JSON，供今日推荐按联赛门槛过滤")
    jc_eval_p.add_argument("--export-baseline", type=str, default=None, metavar="FILE", dest="export_baseline", help="将本次评估关键指标导出为基线 JSON，用于备份后与 jc-eval-compare 对比优化前后")
    jc_eval_p.add_argument("--single-rec-only", action="store_true", dest="single_rec_only", help="仅输出单场推荐命中率与关键配置（其余指标不展示）")
    jc_eval_p.set_defaults(run=cmd_jc_predictions_eval)

    jc_segment_auto_p = sub.add_parser("jc-segment-auto", help="一键用复盘结果调高命中率：导出细分命中率 → 用细分覆盖再评估 → 提示启用后今日推荐自动应用")
    jc_segment_auto_p.set_defaults(run=cmd_jc_segment_auto)

    jc_aux_2026_p = sub.add_parser("jc-recap-auxiliary-for-2026", help="用 2021–2025 年数据为辅，导出细分与联赛参数并评估 2026 单场推荐命中率；2026 列表默认使用该参数")
    jc_aux_2026_p.set_defaults(run=cmd_jc_recap_auxiliary_for_2026)

    jc_compare_p = sub.add_parser("jc-eval-compare", help="对比两份 jc 评估基线（--export-baseline 导出的 JSON），看优化前后提升或下降")
    jc_compare_p.add_argument("--before", type=str, required=True, metavar="FILE", help="优化前/备份库评估基线 JSON（恢复备份后运行 jc-predictions-eval --date-from/--date-to 并 --export-baseline 得到）")
    jc_compare_p.add_argument("--after", type=str, required=True, metavar="FILE", help="优化后/当前库评估基线 JSON（当前库运行 jc-predictions-eval 并 --export-baseline 得到）")
    jc_compare_p.set_defaults(run=cmd_jc_eval_compare)

    jc_si_p = sub.add_parser("jc-to-self-improvement", help="（单日）按 self_improvement_football 逻辑拉取指定竞彩日并写入 jingcai_match_analysis，与「拉取竞彩列表」按钮逻辑一致；按年批量请用 jc-batch-year")
    jc_si_p.add_argument("--date", type=str, required=True, metavar="YYYY-MM-DD", help="竞彩日，如 2023-01-01")
    jc_si_p.add_argument("--dry-run", action="store_true", help="仅预览，不写入")
    jc_si_p.add_argument("--sleep", type=float, default=0.5, metavar="SEC", help="每次请求体彩接口后休眠秒数，默认 0.5（与 API 单日拉取一致）")
    jc_si_p.add_argument("--proxy", type=str, default=None, metavar="URL", help="体彩请求走代理，如 http://127.0.0.1:7890")
    jc_si_p.set_defaults(run=cmd_jc_to_self_improvement)

    jc_del_analysis_p = sub.add_parser(
        "jc-match-analysis-delete-by-date",
        help="按竞彩日删除 jingcai_match_analysis 中错日/串日旧数据（及可选 jc_matches、jc_predictions）；需 --yes 或 --dry-run",
    )
    jc_del_analysis_p.add_argument("--date", type=str, required=True, metavar="YYYY-MM-DD", help="竞彩日，如 2026-03-23")
    jc_del_analysis_p.add_argument("--dry-run", action="store_true", help="只统计将删除条数，不执行删除")
    jc_del_analysis_p.add_argument("--yes", action="store_true", help="确认执行删除（与 --dry-run 二选一）")
    jc_del_analysis_p.add_argument("--no-jc-matches", action="store_true", help="不删除 jc_matches 中该日汇总文档")
    jc_del_analysis_p.add_argument("--include-jc-predictions", action="store_true", help="同时删除 jc_predictions 中该日模型预测")
    jc_del_analysis_p.set_defaults(run=cmd_jc_match_analysis_delete_by_date)

    jc_batch_p = sub.add_parser("jc-batch-year", help="按年批量拉取竞彩：将指定年份 1 月 1 日至今日逐日拉取并写入 jingcai_match_analysis（与「拉取竞彩列表」按钮无关，仅命令行批量用）")
    jc_batch_p.add_argument("--year", type=int, required=True, metavar="YYYY", help="年份，如 2026（拉取 2026-01-01～今日）")
    jc_batch_p.add_argument("--dry-run", action="store_true", help="仅预览，不写入")
    jc_batch_p.add_argument("--resume", action="store_true", help="跳过 jingcai_match_analysis 中已有数据的日期，只拉取缺失日期（断点续跑）")
    jc_batch_p.add_argument("--from-date", type=str, default=None, metavar="YYYY-MM-DD", help="只拉取该日期及之后的日期，如 2025-04-01")
    jc_batch_p.add_argument("--to-date", type=str, default=None, metavar="YYYY-MM-DD", help="只拉取该日期及之前的日期，如 2025-06-30")
    jc_batch_p.add_argument("--sleep", type=float, default=2.0, metavar="SEC", help="每次请求体彩接口后休眠秒数，降低被安全策略拦截概率，默认 2")
    jc_batch_p.add_argument("--proxy", type=str, default=None, metavar="URL", help="体彩请求走代理，如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080（socks5 需 pip install requests[socks]）")
    jc_batch_p.add_argument("--use-result-api", action="store_true", help="历史日期直接使用赛果接口 getUniformMatchResultV1，不先请求 getMatchListV1（避免先失败再回退，推荐按年拉取时使用）")
    jc_batch_p.set_defaults(run=cmd_jc_batch_year)

    eval_all_p = sub.add_parser("eval-all", help="一次跑完：predict-eval + predict-eval --by-competition + prediction-log recap + recap --by-competition")
    eval_all_p.set_defaults(run=cmd_eval_all)

    refresh_eval_p = sub.add_parser("refresh-and-eval", help="主库+竞彩一起重算（repredict-all）再跑 eval-all，调参后一次更新两处并看新指标")
    refresh_eval_p.add_argument("--years", type=str, default="2025,2026", help="竞彩按年重算的年份，逗号分隔")
    refresh_eval_p.add_argument("--db-only", action="store_true", help="只更新主库 predictions，不更新 jc_predictions")
    refresh_eval_p.add_argument("--verbose", "-v", action="store_true", help="竞彩预测时逐场打印")
    refresh_eval_p.set_defaults(run=cmd_refresh_and_eval)

    jc_recap_refresh_p = sub.add_parser("jc-recap-refresh-eval", help="一键：竞彩复盘 → 更新校准与ML → 重算竞彩预测（全量）→ 再评估（预测全在 self_improvement_football 时用）")
    jc_recap_refresh_p.add_argument("--years", type=str, default=None, metavar="YEAR[,YEAR...]", help="不传则重算库中全部年份；传 2025,2026 则只重算指定年（加快）")
    jc_recap_refresh_p.add_argument("--export-segment-stats", type=str, default=None, metavar="FILE", dest="export_segment_stats", help="复盘时导出细分命中率到 JSON，供后续 --segment-stats 使用")
    jc_recap_refresh_p.add_argument("--verbose", "-v", action="store_true", help="重算预测时逐场打印")
    jc_recap_refresh_p.set_defaults(run=cmd_jc_recap_refresh_eval)

    data_stats_p = sub.add_parser("data-stats", help="查看数据库数据量：比赛数、预测数、竞彩预测数，便于判断是否可重训 ML/校准")
    data_stats_p.set_defaults(run=cmd_data_stats)

    refresh_models_p = sub.add_parser("refresh-models", help="依次执行 calibrate-probs、train-outcome-ml；样本不足会报错，样本够时更新后可跑 refresh-and-eval")
    refresh_models_p.set_defaults(run=cmd_refresh_models)

    suggest_p = sub.add_parser("suggest-improvements", help="输出后续可提升方向：数据积累、API 预测融合、联赛级调参建议")
    suggest_p.add_argument("--year", type=int, default=None, metavar="YEAR", help="联赛建议仅基于该年 DB 数据，如 2025")
    suggest_p.set_defaults(run=cmd_suggest_improvements)

    test_api_p = sub.add_parser("test-api-predictions", help="测试 API-Football predictions 接口是否可用（需 API key）")
    test_api_p.add_argument("--date", type=str, required=True, help="比赛日期 YYYY-MM-DD")
    test_api_p.add_argument("--home", type=str, default=None, help="主队名（不填则从 prediction_log 该日取一条）")
    test_api_p.add_argument("--away", type=str, default=None, help="客队名")
    test_api_p.set_defaults(run=cmd_test_api_predictions)

    calib = sub.add_parser("calibrate-probs", help="从历史预测+结果拟合概率校准参数，写入 data/calibration_params.json")
    calib.add_argument("--no-db", action="store_true", dest="no_db", help="仅用 prediction_log，不用 DB 预测表")
    calib.add_argument("--no-log", action="store_true", dest="no_log", help="仅用 DB 预测表，不用 prediction_log")
    calib.add_argument("--by-competition", action="store_true", dest="by_competition", help="按联赛分组拟合，写入联赛专属校准（by_competition），预测时自动按 competition_id 选用")
    calib.add_argument("--train-date-from", type=str, default=None, metavar="YYYY-MM-DD", help="仅用该日期及之后的样本训练（与 --train-date-to 同时使用，用于「仅用往年数据训练」对比）")
    calib.add_argument("--train-date-to", type=str, default=None, metavar="YYYY-MM-DD", help="仅用该日期及之前的样本训练")
    calib.set_defaults(run=cmd_calibrate_probs)

    train_ml = sub.add_parser("train-outcome-ml", help="训练胜平负 ML 修正层（规则输出+赛果），需 scikit-learn；--competition-id 时训练联赛专属模型")
    train_ml.add_argument("--competition-id", type=int, default=None, metavar="ID", help="仅用该联赛样本训练并保存为 outcome_ml_model_{ID}.joblib")
    train_ml.add_argument("--train-date-from", type=str, default=None, metavar="YYYY-MM-DD", help="仅用该日期及之后的样本训练（与 --train-date-to 同时使用）")
    train_ml.add_argument("--train-date-to", type=str, default=None, metavar="YYYY-MM-DD", help="仅用该日期及之前的样本训练")
    train_ml.set_defaults(run=cmd_train_outcome_ml)

    fit_bqc = sub.add_parser(
        "fit-bqc-alpha",
        help="从已完赛竞彩（含半场比分）拟合半全场 α，写入 data/bqc_alpha_by_competition.json；预测时自动按赛事选用",
    )
    fit_bqc.add_argument("--min-samples-per-competition", type=int, default=12, metavar="N", help="单赛事最少样本才写入该赛事 α")
    fit_bqc.add_argument("--min-global-samples", type=int, default=40, metavar="N", help="全局 default 中位数最少样本数")
    fit_bqc.set_defaults(run=cmd_fit_bqc_alpha)

    plog = sub.add_parser("prediction-log", help="预测结果统计与复盘：记录预测、填实际比分、生成复盘")
    plog_sub = plog.add_subparsers(dest="plog_subcommand", required=True)
    plog_add = plog_sub.add_parser("add", help="预测并写入日志（供每日统计）")
    plog_add.add_argument("home", help="主队名称")
    plog_add.add_argument("away", help="客队名称")
    plog_add.add_argument("--competition", type=str, default=None, help="赛事名，如 Premier League")
    plog_add.add_argument("--last-n", type=int, default=10, help="参考最近 N 场，默认 10")
    plog_add.set_defaults(plog_run=cmd_prediction_log_add)
    plog_set = plog_sub.add_parser("set-result", help="按序号填写实际比分")
    plog_set.add_argument("index", help="日志序号（list 可见）")
    plog_set.add_argument("home_score", help="主队实际进球")
    plog_set.add_argument("away_score", help="客队实际进球")
    plog_set.set_defaults(plog_run=cmd_prediction_log_set_result)
    plog_recap = plog_sub.add_parser("recap", help="复盘：预测 vs 实际、MAE、胜平负准确率与不一致分析")
    plog_recap.add_argument("--date", type=str, default=None, help="只复盘该日期的预测，如 2026-02-11（仅 prediction_log 时有效）")
    plog_recap.add_argument("--by-competition", action="store_true", help="按联赛拆解准确率与 MAE")
    plog_recap.add_argument("--by-year", action="store_true", help="按年份拆解准确率与 MAE（仅 prediction_log 时有效）")
    plog_recap.add_argument("--from-db", action="store_true", help="用 DB 全量预测（与 predict-eval 同源），复盘数据与全量一致")
    plog_recap.add_argument("--year", type=int, default=None, metavar="YEAR", help="只复盘该年份（需与 --from-db 同用），如 2025")
    plog_recap.add_argument("--recap-limit", type=int, default=200, help="--from-db 时仅展示前 N 条详情，0=全部，默认 200")
    plog_recap.set_defaults(plog_run=cmd_prediction_log_recap)
    plog_list = plog_sub.add_parser("list", help="列出已记录的预测")
    plog_list.add_argument("--date", type=str, default=None, help="只列该日期的预测")
    plog_list.set_defaults(plog_run=cmd_prediction_log_list)
    plog_compare_db = plog_sub.add_parser("compare-db", help="对比复盘数据（prediction_log）与 DB 数据是否一致")
    plog_compare_db.set_defaults(plog_run=cmd_prediction_log_compare_db)
    plog_sync_db = plog_sub.add_parser("sync-from-db", help="从 DB 已完赛且有预测的场次同步到 prediction_log 表，使条数与 DB 一致（需 PREDICTION_LOG_USE_DB=true）")
    plog_sync_db.set_defaults(plog_run=cmd_prediction_log_sync_from_db)
    plog_dedup = plog_sub.add_parser("dedup", help="仅 Mongo：检查/去除 prediction_log 中同一场（日期+主客队）的重复条，每组保留一条（优先有 match_id）")
    plog_dedup.add_argument("--dry-run", action="store_true", help="只统计重复并打印，不删除")
    plog_dedup.set_defaults(plog_run=cmd_prediction_log_dedup)
    plog_migrate = plog_sub.add_parser("migrate-from-json", help="将 data/prediction_log.json 一次性导入到 prediction_log 表（仅旧数据迁移用）")
    plog_migrate.set_defaults(plog_run=cmd_prediction_log_migrate_from_json)

    bet_rec = sub.add_parser("bet-record", help="投注花费与收益记录：添加、列表、统计")
    bet_rec_sub = bet_rec.add_subparsers(dest="bet_record_subcommand", required=True)
    bet_rec_add = bet_rec_sub.add_parser("add", help="新增一条记录（花费/收益）")
    bet_rec_add.add_argument("--date", type=str, required=True, metavar="YYYY-MM-DD", help="记录日期")
    bet_rec_add.add_argument("--spend", type=float, required=True, metavar="元", help="花费金额（元）")
    bet_rec_add.add_argument("--return", type=float, default=0.0, dest="amount_returned", metavar="元", help="回报金额（元），未开奖填 0，默认 0")
    bet_rec_add.add_argument("--note", type=str, default=None, help="备注")
    bet_rec_add.set_defaults(run=cmd_bet_record_add)
    bet_rec_list = bet_rec_sub.add_parser("list", help="列出记录")
    bet_rec_list.add_argument("--from", type=str, default=None, dest="date_from", metavar="YYYY-MM-DD", help="起始日期")
    bet_rec_list.add_argument("--to", type=str, default=None, dest="date_to", metavar="YYYY-MM-DD", help="截止日期")
    bet_rec_list.set_defaults(run=cmd_bet_record_list)
    bet_rec_stats = bet_rec_sub.add_parser("stats", help="统计总花费、总收益、盈亏")
    bet_rec_stats.add_argument("--from", type=str, default=None, dest="date_from", metavar="YYYY-MM-DD", help="起始日期")
    bet_rec_stats.add_argument("--to", type=str, default=None, dest="date_to", metavar="YYYY-MM-DD", help="截止日期")
    bet_rec_stats.set_defaults(run=cmd_bet_record_stats)

    fix = sub.add_parser("fixtures", help="查看未来赛程（football-data.org 五大联赛，未开赛场次）")
    fix.add_argument("--league", type=str, default="PL", help="联赛代码：PL/PD/SA/BL1/FL1，默认 PL")
    fix.add_argument("--season", type=int, default=2025, help="赛季开始年，默认 2025")
    fix.set_defaults(run=cmd_fixtures)

    pt = sub.add_parser("predict-today", help="今日五大联赛未开赛场次预测（竞彩），可写入 prediction_log")
    pt.add_argument("--date", type=str, default=None, help="日期 YYYY-MM-DD，默认今天")
    pt.add_argument("--no-save", action="store_true", help="仅输出预测，不写入 prediction_log")
    pt.add_argument("--last-n", type=int, default=10, help="参考最近 N 场，默认 10")
    pt.set_defaults(run=cmd_predict_today)

    pj = sub.add_parser("predict-jingcai", help="按 data/jingcai_fixtures.json 仅预测竞足彩场次（会替换该日期的旧预测）")
    pj.add_argument("--date", type=str, required=True, help="日期 YYYY-MM-DD，如 2026-02-12")
    pj.add_argument("--last-n", type=int, default=10, help="参考最近 N 场，默认 10")
    pj.set_defaults(run=cmd_predict_jingcai)

    pd = sub.add_parser("daily-jingcai", help="自动拉取当日五大联赛场次并预测（数据来自 football-data.org API）")
    pd.add_argument("--date", type=str, default=None, help="日期 YYYY-MM-DD，默认今天")
    pd.add_argument("--last-n", type=int, default=10, help="参考最近 N 场，默认 10")
    pd.set_defaults(run=cmd_daily_jingcai)

    enr = sub.add_parser("enrich-fd", help="为 football-data 场次补全单场统计（射门/控球等）与半场比分，写入 raw_meta")
    enr.add_argument("--match-id", type=int, default=None, help="只补全指定 match_id；不传则按 limit 批量补全")
    enr.add_argument("--limit", type=int, default=20, help="批量补全时最多请求场数，默认 20（受 API 限频约 10 次/分钟）")
    enr.set_defaults(run=cmd_enrich_fd)

    am = sub.add_parser("add-match", help="手动添加一场已完赛比赛（自己做复盘数据），主客队名与 prediction_log 一致时网页点复盘可出完整报告")
    am.add_argument("date", help="比赛日 YYYY-MM-DD")
    am.add_argument("home_team", help="主队名称（与 prediction_log 中一致）")
    am.add_argument("away_team", help="客队名称（与 prediction_log 中一致）")
    am.add_argument("home_score", type=int, help="主队进球数")
    am.add_argument("away_score", type=int, help="客队进球数")
    am.add_argument("--competition", type=str, default="Premier League", help="赛事名称，默认 Premier League")
    am.add_argument("--half-time", type=int, nargs=2, metavar=("主", "客"), help="半场比分，如 --half-time 1 1")
    am.add_argument("--season-name", type=str, default="2025/2026", help="赛季名，默认 2025/2026")
    am.set_defaults(run=cmd_add_match)

    sync_st = sub.add_parser("sync-sporttery", help="(已废弃) 数据改为仅从 API 获取")
    sync_st.add_argument("--date", type=str, default=None, help="已忽略")
    sync_st.add_argument("--from-json", type=str, default=None, help="已忽略")
    sync_st.set_defaults(run=cmd_sync_sporttery)

    auto_acc = sub.add_parser("auto-accumulate-log", help="自动：从 DB 取场次，未开赛写预测、已完赛用 DB 比分回填 log；默认昨日/今日/明日")
    auto_acc.add_argument("--dates", type=str, default=None, help="逗号分隔日期 YYYY-MM-DD；不传则用昨日/今日/明日")
    auto_acc.set_defaults(run=cmd_auto_accumulate_log)

    inj = sub.add_parser("injury-crawl", help="伤停数据：从公众号（如亚鸣笔记）图片 OCR，五大联赛优先；支持按日期选文章")
    inj.add_argument("--date", type=str, default=None, help="比赛日 YYYY-MM-DD，按日期从 data/injury_urls.json 选文章；不传则用今天")
    inj.add_argument("--url", type=str, default=None, help="公众号文章 URL；未传则按 --date 或默认 URL")
    inj.add_argument("--from-image", type=str, default=None, help="本地图片路径，跳过网络抓取")
    inj.add_argument("--all", action="store_true", help="输出全部联赛；默认仅五大联赛")
    inj.add_argument("--merge", action="store_true", help="合并到现有 JSON（不覆盖已有场次），用于补充多日伤停")
    inj.set_defaults(run=cmd_injury_crawl)

    inj_api = sub.add_parser("injury-api", help="伤停数据：从 API-FOOTBALL 拉取五大联赛伤停（需配置 API_FOOTBALL_KEY）")
    inj_api.add_argument("--date", type=str, default=None, help="比赛日 YYYY-MM-DD；不传则用今天")
    inj_api.add_argument("--season", type=int, default=None, metavar="YEAR", help="按赛季断点续拉（如 2024）；从 data/injury_api_progress.json 记录处继续")
    inj_api.add_argument("--reset-season", action="store_true", help="与 --season 同用：清除该赛季进度后重新拉取整季")
    inj_api.add_argument("--merge", action="store_true", help="与 DB 已有伤停合并（按主客队+联赛去重）")
    inj_api.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数；不传则用 .env 的 API_FOOTBALL_RATE_LIMIT_SEC（付费档 7500 次/天可设 0.05）")
    inj_api.set_defaults(run=cmd_injury_api)
    inj_api_by_db = sub.add_parser("injury-api-by-db", help="伤停：按 DB 中比赛日批量拉取伤停并写入 DB（只拉 DB 里已有比赛的日期）")
    inj_api_by_db.add_argument("--years", type=str, required=True, metavar="YEAR[,YEAR...]", help="只拉这些年份的比赛日，如 2024 或 2023,2024（2024 年 1–7 月属 2023 赛季）")
    inj_api_by_db.add_argument("--skip-existing", action="store_true", help="跳过 match_injury 中已有该日期的日期，只拉缺失（断点续拉）")
    inj_api_by_db.add_argument("--limit", type=int, default=None, metavar="N", help="只处理最近 N 个待拉取日期（默认全部），日常补跑可设 7~14 加快")
    inj_api_by_db.add_argument("--include-future", action="store_true", help="包含未来日期（默认只拉 ≤ 今天）")
    inj_api_by_db.add_argument("--dry-run", action="store_true", help="只打印将拉取的日期列表，不请求 API")
    inj_api_by_db.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数；付费档可设 0.05~0.5 大幅加速")
    inj_api_by_db.set_defaults(run=cmd_injury_api_by_db)
    inj_clean = sub.add_parser("injury-clean-non-api", help="伤停：删除 DB 中非 API 来源（公众号 OCR 等），仅保留 source=api_football")
    inj_clean.set_defaults(run=cmd_injury_clean_non_api)

    lineups_by_db = sub.add_parser("lineups-by-db", help="阵容：按 DB 比赛批量拉取完整阵容并落库；断点续拉，可限次数（适配 7500 次/天）")
    lineups_by_db.add_argument("--years", type=str, default=None, metavar="YEAR[,YEAR...]", help="只处理这些年份的比赛，如 2024 或 2024,2025；与 --date-from/--date-to 二选一或同时用")
    lineups_by_db.add_argument("--date-from", type=str, default=None, metavar="YYYY-MM-DD", help="只处理该日期及之后的比赛")
    lineups_by_db.add_argument("--date-to", type=str, default=None, metavar="YYYY-MM-DD", help="只处理该日期及之前的比赛")
    lineups_by_db.add_argument("--skip-existing", action="store_true", default=True, help="跳过 match_lineups 中已有记录的场次（默认开启，断点续拉）")
    lineups_by_db.add_argument("--no-skip-existing", action="store_false", dest="skip_existing", help="不跳过已拉过的场次，全部重拉")
    lineups_by_db.add_argument("--limit", type=int, default=None, metavar="N", help="本次最多拉取 N 场（控制 API 次数，如 500；不传则拉完全部待拉）")
    lineups_by_db.add_argument("--dry-run", action="store_true", help="只打印待拉取场次数量与样本，不请求 API")
    lineups_by_db.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数；不传用 config API_FOOTBALL_RATE_LIMIT_SEC（7500 次/天可设 0.05~0.5）")
    lineups_by_db.set_defaults(run=cmd_lineups_by_db)

    lineups_by_jingcai = sub.add_parser("lineups-by-jingcai", help="阵容（竞彩）：从 jingcai_match_analysis 按 api_football_fixture_id 拉取阵容，写入 self_improvement_football.match_lineups")
    lineups_by_jingcai.add_argument("--years", type=str, default=None, metavar="YEAR[,YEAR...]", help="只处理这些年份，如 2024 或 2024,2025")
    lineups_by_jingcai.add_argument("--date-from", type=str, default=None, metavar="YYYY-MM-DD", help="只处理该日期及之后的比赛")
    lineups_by_jingcai.add_argument("--date-to", type=str, default=None, metavar="YYYY-MM-DD", help="只处理该日期及之前的比赛")
    lineups_by_jingcai.add_argument("--skip-existing", action="store_true", default=True, help="跳过 match_lineups 中已有记录的场次（默认开启）")
    lineups_by_jingcai.add_argument("--no-skip-existing", action="store_false", dest="skip_existing", help="不跳过已拉过的场次，全部重拉")
    lineups_by_jingcai.add_argument("--limit", type=int, default=None, metavar="N", help="本次最多拉取 N 场")
    lineups_by_jingcai.add_argument("--dry-run", action="store_true", help="只打印待拉取场次数量与样本，不请求 API")
    lineups_by_jingcai.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数")
    lineups_by_jingcai.set_defaults(run=cmd_lineups_by_jingcai)

    events_by_jingcai = sub.add_parser("events-by-jingcai", help="事件（竞彩）：从 jingcai_match_analysis 按 api_football_fixture_id 拉取单场事件，写入 api_football_events")
    events_by_jingcai.add_argument("--years", type=str, default=None, metavar="YEAR[,YEAR...]", help="只处理这些年份，如 2024 或 2024,2025")
    events_by_jingcai.add_argument("--date-from", type=str, default=None, metavar="YYYY-MM-DD", help="只处理该日期及之后的比赛")
    events_by_jingcai.add_argument("--date-to", type=str, default=None, metavar="YYYY-MM-DD", help="只处理该日期及之前的比赛")
    events_by_jingcai.add_argument("--skip-existing", action="store_true", default=True, help="跳过 api_football_events 中已有记录的场次（默认开启）")
    events_by_jingcai.add_argument("--no-skip-existing", action="store_false", dest="skip_existing", help="不跳过已拉过的场次，全部重拉")
    events_by_jingcai.add_argument("--limit", type=int, default=None, metavar="N", help="本次最多拉取 N 场")
    events_by_jingcai.add_argument("--dry-run", action="store_true", help="只打印待拉取场次数量与样本，不请求 API")
    events_by_jingcai.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数")
    events_by_jingcai.set_defaults(run=cmd_events_by_jingcai)

    players_stats_by_jingcai = sub.add_parser(
        "players-stats-by-jingcai",
        help="球员统计（竞彩）：按 api_football_fixture_id 拉取单场 players/statistics，写入 api_football_player_statistics",
    )
    players_stats_by_jingcai.add_argument("--years", type=str, default=None, metavar="YEAR[,YEAR...]", help="只处理这些年份，如 2024,2025")
    players_stats_by_jingcai.add_argument("--date-from", type=str, default=None, metavar="YYYY-MM-DD")
    players_stats_by_jingcai.add_argument("--date-to", type=str, default=None, metavar="YYYY-MM-DD")
    players_stats_by_jingcai.add_argument("--skip-existing", action="store_true", default=True, help="跳过已拉过的场次（默认开启）")
    players_stats_by_jingcai.add_argument("--no-skip-existing", action="store_false", dest="skip_existing", help="不跳过已拉过的场次")
    players_stats_by_jingcai.add_argument("--limit", type=int, default=None, metavar="N", help="本次最多拉取 N 场")
    players_stats_by_jingcai.add_argument("--dry-run", action="store_true", help="只打印待拉取场次，不请求 API")
    players_stats_by_jingcai.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数")
    players_stats_by_jingcai.set_defaults(run=cmd_players_stats_by_jingcai)

    topscorers_api = sub.add_parser(
        "topscorers-api",
        help="射手榜：拉取指定联赛+赛季的 players/topscorers，写入 api_football_topscorers；不传 --leagues 则拉取全部联赛",
    )
    topscorers_api.add_argument("--season", type=int, default=None, metavar="YEAR", help="赛季年，如 2025；不传则用当前赛季")
    topscorers_api.add_argument("--leagues", type=str, default=None, metavar="ID,ID,...", help="联赛 ID 逗号分隔；不传则从 API 拉取全部联赛")
    topscorers_api.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数")
    topscorers_api.add_argument("--skip-existing", action="store_true", default=True, help="跳过已有数据的联赛+赛季（默认开启，用于断点续拉）")
    topscorers_api.add_argument("--no-skip-existing", action="store_false", dest="skip_existing", help="不跳过，全部重新拉取")
    topscorers_api.set_defaults(run=cmd_topscorers_api)

    teams_statistics_api = sub.add_parser(
        "teams-statistics-api",
        help="球队赛季统计：拉取 teams/statistics（场均进球、控球、射门等），写入 api_football_team_statistics；不传 --leagues 则拉取全部联赛",
    )
    teams_statistics_api.add_argument("--season", type=int, default=None, metavar="YEAR", help="赛季年；不传则当前赛季")
    teams_statistics_api.add_argument("--leagues", type=str, default=None, metavar="ID,ID,...", help="联赛 ID 逗号分隔；不传则从 API 拉取全部联赛")
    teams_statistics_api.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数")
    teams_statistics_api.add_argument("--skip-existing", action="store_true", default=True, help="跳过已有数据的联赛+球队（默认开启，用于断点续拉）")
    teams_statistics_api.add_argument("--no-skip-existing", action="store_false", dest="skip_existing", help="不跳过，全部重新拉取")
    teams_statistics_api.set_defaults(run=cmd_teams_statistics_api)

    squads_api = sub.add_parser(
        "squads-api",
        help="球队阵容：拉取 players/squads，写入 api_football_squads（配合伤停/lineups 用）；不传 --leagues 则拉取全部联赛",
    )
    squads_api.add_argument("--season", type=int, default=None, metavar="YEAR", help="赛季年（用于获取该联赛该赛季球队列表）；不传则当前赛季")
    squads_api.add_argument("--leagues", type=str, default=None, metavar="ID,ID,...", help="联赛 ID 逗号分隔；不传则从 API 拉取全部联赛")
    squads_api.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数")
    squads_api.add_argument("--skip-existing", action="store_true", default=True, help="跳过已有数据的球队（默认开启，用于断点续拉）")
    squads_api.add_argument("--no-skip-existing", action="store_false", dest="skip_existing", help="不跳过，全部重新拉取")
    squads_api.set_defaults(run=cmd_squads_api)

    coaches_api = sub.add_parser(
        "coaches-api",
        help="教练信息：拉取 coaches?team=，写入 api_football_coaches（换帅/任期等粗特征）；不传 --leagues 则拉取全部联赛",
    )
    coaches_api.add_argument("--season", type=int, default=None, metavar="YEAR", help="赛季年（用于获取球队列表）；不传则当前赛季")
    coaches_api.add_argument("--leagues", type=str, default=None, metavar="ID,ID,...", help="联赛 ID 逗号分隔；不传则从 API 拉取全部联赛")
    coaches_api.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数")
    coaches_api.add_argument("--skip-existing", action="store_true", default=True, help="跳过已有数据的球队（默认开启，用于断点续拉）")
    coaches_api.add_argument("--no-skip-existing", action="store_false", dest="skip_existing", help="不跳过，全部重新拉取")
    coaches_api.set_defaults(run=cmd_coaches_api)

    lineup_stats = sub.add_parser("lineup-stats", help="阵容统计：已完赛且有阵容数据的场次的场均首发人数（self_improvement_football）")
    lineup_stats.add_argument("--date-from", type=str, default=None, metavar="YYYY-MM-DD")
    lineup_stats.add_argument("--date-to", type=str, default=None, metavar="YYYY-MM-DD")
    lineup_stats.set_defaults(run=cmd_lineup_stats)

    lineup_comparison = sub.add_parser("lineup-comparison", help="阵容对比（复盘）：预计 vs 实际首发、关键球员缺阵，输出至 stdout")
    lineup_comparison.add_argument("--date-from", type=str, default=None, metavar="YYYY-MM-DD")
    lineup_comparison.add_argument("--date-to", type=str, default=None, metavar="YYYY-MM-DD")
    lineup_comparison.add_argument("--limit", type=int, default=200, metavar="N")
    lineup_comparison.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    lineup_comparison.set_defaults(run=cmd_lineup_comparison)

    lineup_key_absent_analysis_p = sub.add_parser(
        "lineup-key-absent-analysis",
        help="对比预计主/客缺 vs 实际主/客关键缺，看是否系统性高估/低估，供调 KEY_WEIGHT 或预计首发参与 key_count",
    )
    lineup_key_absent_analysis_p.add_argument("--years", type=str, default=None, metavar="YEAR[,YEAR...]", help="年份，如 2024,2025,2026")
    lineup_key_absent_analysis_p.add_argument("--date-from", type=str, default=None, metavar="YYYY-MM-DD")
    lineup_key_absent_analysis_p.add_argument("--date-to", type=str, default=None, metavar="YYYY-MM-DD")
    lineup_key_absent_analysis_p.add_argument("--driver", type=str, default="match_lineups", choices=("match_lineups", "date"), help="match_lineups=以已落库场次为准（全量，默认）；date=以竞彩按日列表为准")
    lineup_key_absent_analysis_p.add_argument("--by-year", action="store_true", help="按年输出统计")
    lineup_key_absent_analysis_p.add_argument("--by-league", action="store_true", help="按联赛输出偏差统计，便于在 predict_league_params.json 中为联赛单独调 KEY_WEIGHT")
    lineup_key_absent_analysis_p.add_argument("--export-injury-params", type=str, default=None, metavar="FILE", dest="export_injury_params", help="导出联赛伤停权重建议到 JSON，再运行 merge-injury-params 合并进 predict_league_params.json")
    lineup_key_absent_analysis_p.add_argument("--simulate-predicted-lineup", action="store_true", dest="simulate_predicted_lineup", help="模拟启用预计首发时的偏差，用于量化两项优化的预期提升")
    lineup_key_absent_analysis_p.add_argument("--json", action="store_true", help="输出 JSON")
    lineup_key_absent_analysis_p.set_defaults(run=cmd_lineup_key_absent_analysis)

    lineup_backfill_kp = sub.add_parser(
        "lineup-backfill-key-absence",
        help="将「关键球员 vs 实际首发」写入 match_lineups：对已有首发的场次用 jingcai 关键球员计算并落库四字段",
    )
    lineup_backfill_kp.add_argument("--years", type=str, default=None, metavar="YEAR[,YEAR...]", help="只处理这些年份，如 2024,2025,2026")
    lineup_backfill_kp.add_argument("--date-from", type=str, default=None, metavar="YYYY-MM-DD")
    lineup_backfill_kp.add_argument("--date-to", type=str, default=None, metavar="YYYY-MM-DD")
    lineup_backfill_kp.add_argument("--limit", type=int, default=None, metavar="N", help="本次最多更新 N 场")
    lineup_backfill_kp.add_argument("--no-skip-existing", action="store_true", dest="no_skip_existing", help="不跳过已落库的场次，全部重算并覆盖")
    lineup_backfill_kp.set_defaults(run=cmd_lineup_backfill_key_absence)
    merge_injury_p = sub.add_parser("merge-injury-params", help="将 lineup-key-absent-analysis --export-injury-params 生成的 JSON 合并进 predict_league_params.json（按联赛调伤停权重）")
    merge_injury_p.add_argument("--injury-params", type=str, required=True, metavar="FILE", dest="injury_params", help="伤停权重建议 JSON 路径，如 data/jc_injury_weights.json")
    merge_injury_p.add_argument("--league-params", type=str, default=None, metavar="FILE", dest="league_params", help="predict_league_params.json 路径，默认 data/predict_league_params.json")
    merge_injury_p.set_defaults(run=cmd_merge_injury_params)
    backfill_league_p = sub.add_parser("backfill-league-cn", help="对 jingcai 分析表中缺 league_cn 的文档用当前解析逻辑写回 league_cn")
    backfill_league_p.add_argument("--years", type=str, default="2024,2025,2026", metavar="Y", help="仅处理这些年份，逗号分隔，默认 2024,2025,2026")
    backfill_league_p.add_argument("--all", action="store_true", dest="all", help="不按年份过滤，处理全部文档")
    backfill_league_p.add_argument("--dry-run", action="store_true", help="只统计会更新多少条，不写库")
    backfill_league_p.set_defaults(run=cmd_backfill_league_cn)

    alt_structure_p = sub.add_parser("alt-jingcai-structure", help="备用集合一条文档结构；备用不存在则打主集合一条；支持 --from-file 读本地 JSON")
    alt_structure_p.add_argument("--from-file", type=str, default=None, metavar="PATH", help="从本地 JSON 文件读一条文档（可为备用集合导出）并打印结构")
    alt_structure_p.set_defaults(run=cmd_alt_jingcai_structure)
    main_empty_p = sub.add_parser("main-jingcai-empty-structure", help="找一条联赛解析为空的文档并打印结构（默认直接扫集合；加 --from-lineups 与伤停分析同源）")
    main_empty_p.add_argument("--max-checks", type=int, default=15000, metavar="N", help="最多检查多少条，默认 15000")
    main_empty_p.add_argument("--years", type=str, default="2024,2025,2026", metavar="Y", help="仅检查这些年份的 jc_date，逗号分隔")
    main_empty_p.add_argument("--from-lineups", action="store_true", dest="from_lineups", help="从 match_lineups 顺序取 match_id 再 load（与伤停分析同源），便于找到联赛为空的场次")
    main_empty_p.set_defaults(run=cmd_main_jingcai_empty_structure)
    lineup_league_diag_p = sub.add_parser("lineup-league-diagnostic", help="诊断为何按联赛多为空/只有瑞典杯：抽样查看 jingcai 与 jc_matches 的联赛字段结构")
    lineup_league_diag_p.add_argument("--sample", type=int, default=5, metavar="N", help="抽样场次数，默认 5")
    lineup_league_diag_p.set_defaults(run=cmd_lineup_league_diagnostic)

    lineup_diag_p = sub.add_parser("lineup-diagnostic", help="诊断阵容/关键缺阵数据量，排查数据偏少原因")
    lineup_diag_p.add_argument("--years", type=str, default="2021,2022,2023,2024,2025,2026", metavar="YEAR[,YEAR...]", help="统计这些年份的 jingcai 文档，默认 2021-2026")
    lineup_diag_p.set_defaults(run=cmd_lineup_diagnostic)

    dongqiudi_lineups = sub.add_parser("dongqiudi-lineups", help="懂球帝预计首发：按竞彩日拉取懂球帝预计首发并落库，供模型关键球员缺阵叠加；同时写入 pre_match_lineups（与赛后阵容同结构）")
    dongqiudi_lineups.add_argument("--date", type=str, default=None, metavar="YYYY-MM-DD", help="竞彩日，默认今天")
    dongqiudi_lineups.add_argument("--no-save", action="store_true", help="只爬取不落库（试跑）")
    dongqiudi_lineups.add_argument("--no-save-as-lineups", action="store_true", help="不写入 pre_match_lineups（仅写 dongqiudi_lineups）")
    dongqiudi_lineups.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数，不传用 .env 的 DONGQIUDI_RATE_LIMIT_SEC（默认 2.5），降低封 IP 风险")
    dongqiudi_lineups.add_argument("--max-matches", type=int, default=None, metavar="N", help="单次最多请求 N 场 prediction 页，不传用 DONGQIUDI_MAX_REQUESTS_PER_RUN（默认 25）")
    dongqiudi_lineups.set_defaults(run=cmd_dongqiudi_lineups)

    transfermarkt_lineups = sub.add_parser("transfermarkt-lineups", help="Transfermarkt 赛前阵容：按竞彩日拉取 vorbericht 用户阵型并落库，预测时优先使用")
    transfermarkt_lineups.add_argument("--date", type=str, default=None, metavar="YYYY-MM-DD", help="竞彩日，默认今天")
    transfermarkt_lineups.add_argument("--no-save", action="store_true", help="只爬取不落库（试跑）")
    transfermarkt_lineups.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数，不传用 .env 的 TRANSFERMARKT_RATE_LIMIT_SEC（默认 2.5）")
    transfermarkt_lineups.add_argument("--max-matches", type=int, default=None, metavar="N", help="单次最多请求 N 场 vorbericht 页，不传用 TRANSFERMARKT_MAX_REQUESTS_PER_RUN（默认 25）")
    transfermarkt_lineups.add_argument("--league", type=str, default=None, metavar="SLUG", help="当当日赛程页失败时，改用联赛轮次页；如 premier-league, 1-bundesliga, la-liga, serie-a, ligue-1")
    transfermarkt_lineups.add_argument("--round", type=int, default=None, metavar="N", help="与 --league 同用：轮次 1～38")
    transfermarkt_lineups.add_argument("--season", type=int, default=None, metavar="YEAR", help="与 --league 同用：赛季开赛年，如 2024 表示 24/25；不传用 --date 的年份")
    transfermarkt_lineups.set_defaults(run=cmd_transfermarkt_lineups)

    h2h_backfill = sub.add_parser("h2h-backfill", help="历史交锋：按 DB 中主客对拉取 API-Football Head-to-Head 并写入 matches（仅 AF 来源队）")
    h2h_backfill.add_argument("--years", type=str, required=True, metavar="YEAR[,YEAR...]", help="只处理这些年份的比赛对应的主客对，如 2024,2025")
    h2h_backfill.add_argument("--limit", type=int, default=None, metavar="N", help="最多拉取 N 组主客对（默认全部）")
    h2h_backfill.add_argument("--skip-if-have", type=int, default=6, metavar="N", help="该主客对已有 ≥N 场则跳过（默认 6），省 API 且断点续跑只补不足的")
    h2h_backfill.add_argument("--per-season", action="store_true", help="对 --years 中每个赛季各请求 1 次（每组多倍 API），慎用")
    h2h_backfill.add_argument("--dry-run", action="store_true", help="只打印主客对列表及将跳过的组，不请求 API")
    h2h_backfill.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每组请求后休眠秒数")
    h2h_backfill.set_defaults(run=cmd_h2h_backfill)

    odds_api = sub.add_parser("odds-api", help="赔率：从 API-FOOTBALL 拉取指定日期五大联赛赔率，写入 DB（match_odds）")
    odds_api.add_argument("--date", type=str, default=None, help="比赛日 YYYY-MM-DD；不传则用今天")
    odds_api.add_argument("--merge", action="store_true", help="与现有 JSON 合并（同场次覆盖）")
    odds_api.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数")
    odds_api.set_defaults(run=cmd_odds_api)

    odds_api_by_db = sub.add_parser("odds-api-by-db", help="赔率：按 DB 中比赛日期批量拉取赔率并 merge，补全现有比赛数据；可 --years 2024,2025,2026 限定年份")
    odds_api_by_db.add_argument("--years", type=str, default=None, metavar="YEAR[,YEAR...]", help="只拉这些年份的比赛日，如 2024,2025,2026；不传则全部")
    odds_api_by_db.add_argument("--limit", type=int, default=None, help="只处理最近 N 个日期（默认全部）")
    odds_api_by_db.add_argument("--skip-existing", action="store_true", help="跳过 match_odds 中已有赔率的日期，只拉取缺失日期（断点续拉，节省 API 配额）")
    odds_api_by_db.add_argument("--include-future", action="store_true", help="包含未来日期（默认只拉 ≤ 今天的日期，避免大量 0 场请求）")
    odds_api_by_db.add_argument("--dry-run", action="store_true", help="只打印日期列表，不请求 API")
    odds_api_by_db.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数")
    odds_api_by_db.add_argument(
        "--stop-loss-zero-write-streak",
        type=int,
        default=0,
        metavar="N",
        help="自动止损：连续 N 天“本次写入 0 场”后停止（默认 0=关闭，建议 5）",
    )
    odds_api_by_db.set_defaults(run=cmd_odds_api_by_db)

    sporttery_odds_p = sub.add_parser("sporttery-odds-to-db", help="赔率：按日期从体彩网拉取竞彩场次与固定奖金，写入 match_odds（source=sporttery），用于长期历史赔率")
    sporttery_odds_p.add_argument("--date", type=str, default=None, metavar="YYYY-MM-DD", help="单日")
    sporttery_odds_p.add_argument("--dates", type=str, default=None, metavar="FROM,TO", help="日期范围 YYYY-MM-DD,YYYY-MM-DD（含起止）")
    sporttery_odds_p.add_argument("--days", type=int, default=None, metavar="N", help="最近 N 天（含今天）")
    sporttery_odds_p.add_argument("--year", type=int, default=None, metavar="YYYY", help="整年，如 2026 表示 2026-01-01～2026-12-31")
    sporttery_odds_p.add_argument("--dry-run", action="store_true", help="只打印拉取与拟写入数量，不写 DB")
    sporttery_odds_p.set_defaults(run=cmd_sporttery_odds_to_db)

    jc_sync_results_p = sub.add_parser(
        "jc-sync-match-results",
        help="补全赛果/半场：单日 --date，或按年 --year/--years 遍历库中该年竞彩日；先 getUniformMatchResultV1 再逐场详情（同仪表盘「拉取比赛结果」）",
    )
    jc_sync_results_p.add_argument("--date", type=str, default=None, metavar="YYYY-MM-DD", help="单日竞彩日（与 --year/--years 二选一）")
    jc_sync_results_p.add_argument("--year", type=int, default=None, metavar="YYYY", help="处理库中该年所有竞彩日，如 2026")
    jc_sync_results_p.add_argument("--years", type=str, default=None, metavar="YEAR[,YEAR...]", help="多年份逗号分隔，如 2025,2026（与 --year 可二选一或叠加）")
    jc_sync_results_p.add_argument("--limit", type=int, default=None, metavar="N", help="按年模式时最多处理前 N 个竞彩日（升序，试跑用）")
    jc_sync_results_p.add_argument(
        "--day-delay",
        type=float,
        default=0.8,
        metavar="SEC",
        help="每个竞彩日处理完后的休眠秒数（再跑下一日），默认 0.8，降频防封；单日可设 0",
    )
    jc_sync_results_p.add_argument(
        "--detail-delay",
        type=float,
        default=0.55,
        metavar="SEC",
        help="同一竞彩日内，每场 getMatchHead 等详情链请求结束后的间隔，默认 0.55（单场会连打多个子请求）；设 0 关闭",
    )
    jc_sync_results_p.add_argument(
        "--uniform-delay",
        type=float,
        default=0.18,
        metavar="SEC",
        help="getUniformMatchResultV1 分页之间的间隔，默认 0.18；设 0 关闭",
    )
    jc_sync_results_p.add_argument(
        "--force-all",
        action="store_true",
        help="强制重拉：忽略「该日已全部有全场+半场」的跳过；且对该日每场都尝试拉详情（默认只补缺比分）",
    )
    jc_sync_results_p.add_argument(
        "--ignore-kickoff",
        action="store_true",
        help="不根据开球时间跳过（默认未到开球+15 分钟不拉详情）",
    )
    jc_sync_results_p.add_argument("--json-out", action="store_true", help="每个竞彩日后打印完整 JSON（输出量大）")
    jc_sync_results_p.add_argument(
        "--backfill-fixture-id",
        action="store_true",
        help="在成功写入体彩详情后，再调 API-Football 回填 api_football_fixture_id（默认关闭，避免与「仅拉赛果」混用耗配额）",
    )
    jc_sync_results_p.set_defaults(run=cmd_jc_sync_match_results)

    jc_detail_p = sub.add_parser("jc-match-detail", help="拉取单场对阵详情：头部、伤停、射手、比赛结果、历史交锋、特征分析（体彩 webapi）")
    jc_detail_p.add_argument("--mid", type=int, required=True, metavar="ID", help="体彩场次 ID，如 2037891")
    jc_detail_p.add_argument("--json", action="store_true", help="输出完整 JSON")
    jc_detail_p.add_argument("--save", action="store_true", help="拉取后写入 MongoDB self_improvement_football.jingcai_match_analysis")
    jc_detail_p.set_defaults(run=cmd_jc_match_detail)

    lottery_gov_p = sub.add_parser("lottery-gov-odds-to-db", help="赔率：从中国体彩官网 https://www.lottery.gov.cn/jc/jsq/zqspf/ 通过页面接口抓取让球数等，写入 match_odds（source=lottery_gov）")
    lottery_gov_p.add_argument("--date", type=str, default=None, metavar="YYYY-MM-DD", help="单日")
    lottery_gov_p.add_argument("--dates", type=str, default=None, metavar="FROM,TO", help="日期范围 YYYY-MM-DD,YYYY-MM-DD")
    lottery_gov_p.add_argument("--days", type=int, default=None, metavar="N", help="最近 N 天（含今天）")
    lottery_gov_p.add_argument("--dry-run", action="store_true", help="只打印拟拉取/写入数量，不写 DB")
    lottery_gov_p.set_defaults(run=cmd_lottery_gov_odds_to_db)

    discover_gov_p = sub.add_parser("discover-lottery-gov-api", help="打开官网足球胜平负页面并拦截 XHR，打印疑似数据接口 URL（供后续配置直接请求）")
    discover_gov_p.set_defaults(run=cmd_discover_lottery_gov_api)

    jc_odds_p = sub.add_parser("jc-odds-to-db", help="赔率：按日期从第三方竞彩 API（JC_ODDS_*）拉取并写入 match_odds，用于历史赔率补全")
    jc_odds_p.add_argument("--date", type=str, default=None, metavar="YYYY-MM-DD", help="单日")
    jc_odds_p.add_argument("--dates", type=str, default=None, metavar="FROM,TO", help="日期范围 YYYY-MM-DD,YYYY-MM-DD")
    jc_odds_p.add_argument("--days", type=int, default=None, metavar="N", help="最近 N 天（含今天）")
    jc_odds_p.add_argument("--dry-run", action="store_true", help="只打印拟拉取/写入数量，不写 DB")
    jc_odds_p.add_argument("--no-five-only", action="store_true", help="不限制五大联赛，全部写入（默认仅五大联赛）")
    jc_odds_p.set_defaults(run=cmd_jc_odds_to_db)

    standings_api = sub.add_parser("standings-api", help="积分榜：从 API-FOOTBALL 拉取五大联赛当季积分榜，写入 DB（league_standings）")
    standings_api.add_argument("--season", type=int, default=None, metavar="YEAR", help="赛季年（如 2025）；不传则用当前赛季")
    standings_api.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数")
    standings_api.set_defaults(run=cmd_standings_api)

    standings_backfill = sub.add_parser("standings-backfill", help="历史五大联赛积分：按 DB 比赛赛季拉取积分榜并回填每场 home_rank/away_rank")
    standings_backfill.add_argument("--seasons", type=str, default=None, metavar="YEAR[,YEAR...]", help="只拉这些赛季年，如 2022,2023,2024；不传则从 DB 自动推断")
    standings_backfill.add_argument("--no-fetch", action="store_true", help="不拉取 API，仅用已有 league_standings 回填比赛排名")
    standings_backfill.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数")
    standings_backfill.set_defaults(run=cmd_standings_backfill)

    backfill_support = sub.add_parser("backfill-log-support", help="按 prediction_log 已填比分条目补拉涉及日期的赔率与赛季的积分榜，便于 repredict-history 后复盘更准")
    backfill_support.add_argument("--max-dates", type=int, default=15, metavar="N", help="最多补拉 N 个日期（按日期排序取最近）；默认 15，避免请求过多")
    backfill_support.set_defaults(run=cmd_backfill_log_support)

    migrate_data_p = sub.add_parser("migrate-data", help="将 data 下有用数据一次性迁移到 DB：prediction_log、赔率、积分榜、伤停")
    migrate_data_p.set_defaults(run=cmd_migrate_data)

    mongo_clear_p = sub.add_parser("mongo-clear", help="清空 MongoDB 当前库所有项目集合，便于重新拉取、重新定义；需 MONGO_URI 与 USE_MONGO")
    mongo_clear_p.set_defaults(run=cmd_mongo_clear)

    sync_mongo_p = sub.add_parser("sync-sqlite-to-mongo", help="将 SQLite 表数据全量同步到 MongoDB，便于在 Compass 中查看；需配置 MONGO_URI")
    sync_mongo_p.set_defaults(run=cmd_sync_sqlite_to_mongo)

    migrate_football_p = sub.add_parser(
        "migrate-football-to-self-improvement",
        help="将 football 库数据迁移到 self_improvement_football，之后可删除 football 并仅用 self_improvement_football",
    )
    migrate_football_p.add_argument("--dry-run", action="store_true", help="仅打印将迁移的集合与条数，不写入")
    migrate_football_p.add_argument("--drop-source-db", action="store_true", help="迁移完成后删除 football 库（慎用，需同时加 --yes）")
    migrate_football_p.add_argument("--yes", action="store_true", help="确认执行 --drop-source-db 时使用")
    migrate_football_p.set_defaults(run=cmd_migrate_football_to_self_improvement)

    backfill_zh_p = sub.add_parser("backfill-match-names-zh", help="仅 Mongo：用 teams 的 name/name_zh 补全 matches 中空缺的主客队英文/中文名")
    backfill_zh_p.add_argument("--overwrite", action="store_true", help="全部按 teams 覆盖（默认仅补当前为空的字段）")
    backfill_zh_p.set_defaults(run=cmd_backfill_match_names_zh)

    normalize_teams_p = sub.add_parser("normalize-team-names", help="统一队名：将 match_odds、match_injury 中的主客队名规范化为 teams 表队名，便于按场次精确匹配")
    normalize_teams_p.add_argument("--dry-run", action="store_true", help="仅统计拟更新条数，不写入")
    normalize_teams_p.set_defaults(run=cmd_normalize_team_names)

    link_odds_p = sub.add_parser("link-match-odds", help="仅 Mongo：将 match_odds 与 matches 对应（先规范化队名，再回填 match_id）")
    link_odds_p.add_argument("--dry-run", action="store_true", help="只做队名规范化并打印拟更新数，不写入 match_id")
    link_odds_p.set_defaults(run=cmd_link_match_odds)

    list_no_odds_p = sub.add_parser("list-matches-without-odds", help="仅 Mongo：列出五大联赛中没有赔率信息的比赛（按日期汇总）")
    list_no_odds_p.add_argument("--output", "-o", type=str, default=None, metavar="FILE", help="将无赔率比赛列表写入 JSON 文件")
    list_no_odds_p.set_defaults(run=cmd_list_matches_without_odds)

    odds_stats_p = sub.add_parser("odds-stats", help="仅 Mongo：查看 match_odds 条数、已关联/未关联 match_id、去重比赛数，判断是否有重复")
    odds_stats_p.set_defaults(run=cmd_odds_stats)

    debug_odds_link_p = sub.add_parser("debug-odds-link", help="仅 Mongo：诊断为何 link 为 0，打印 match_odds 与 matches 样本对比队名/日期/联赛")
    debug_odds_link_p.set_defaults(run=cmd_debug_odds_link)

    dedup_odds_p = sub.add_parser("dedup-match-odds", help="仅 Mongo：同键去重或 --by-canonical 按规范队名删同场不同名的重复")
    dedup_odds_p.add_argument("--dry-run", action="store_true", help="只统计将删除的条数，不实际删除")
    dedup_odds_p.add_argument("--by-canonical", action="store_true", help="按规范队名：删除未关联文档中与已关联场次「同场不同名」的重复")
    dedup_odds_p.set_defaults(run=cmd_dedup_match_odds)

    backfill_odds_missing_p = sub.add_parser("backfill-odds-for-missing", help="仅 Mongo：针对无赔率比赛所在日期逐日拉取赔率并关联，可 --limit / --min-date 控制范围")
    backfill_odds_missing_p.add_argument("--limit", type=int, default=None, metavar="N", help="只处理前 N 个日期（建议每次 30～50，避免 API 配额用尽）")
    backfill_odds_missing_p.add_argument("--min-date", type=str, default=None, metavar="YYYY-MM-DD", help="只处理该日期及之后的比赛日（如 2024-01-01 只补 2024 年起）")
    backfill_odds_missing_p.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数（默认约 6.5）")
    backfill_odds_missing_p.add_argument("--dry-run", action="store_true", help="只打印将拉取的日期，不请求 API")
    backfill_odds_missing_p.set_defaults(run=cmd_backfill_odds_for_missing)

    stats_api = sub.add_parser("statistics-api", help="统计：从 API-FOOTBALL 拉取射门/控球等统计，写入 data/api_football_statistics.json，并写入 self_improvement_football.api_football_statistics（需 MONGO_URI）。场次来源：--jingcai-years 为竞彩库；否则按日期/年拉取五大联赛")
    stats_api.add_argument("--date", type=str, default=None, help="单日比赛日 YYYY-MM-DD；不传则用今天（与 --jingcai-years 二选一）")
    stats_api.add_argument("--dates", type=str, default=None, help="多日，逗号分隔（与 --jingcai-years 二选一）")
    stats_api.add_argument("--year", type=int, default=None, metavar="YYYY", help="按年执行：该年每一天依次拉取五大联赛（与 --jingcai-years 二选一）")
    stats_api.add_argument("--jingcai-years", type=str, default=None, metavar="YYYY,YYYY", help="按 jingcai_match_analysis 中指定年份的场次拉取（需已有 api_football_fixture_id），如 2025,2026")
    stats_api.add_argument("--merge", action="store_true", help="与现有 JSON 合并")
    stats_api.add_argument("--rate-limit", type=float, default=None, metavar="SEC", help="每次请求后休眠秒数")
    stats_api.add_argument("--save-batch", type=int, default=50, metavar="N", help="每 N 场写入一次 JSON + Mongo，断点更安全（默认 50）")
    stats_api.add_argument("--resume", action="store_true", help="断点续跑：跳过 JSON/Mongo 中已有统计的 fixture_id，仅拉取缺失场次")
    stats_api.add_argument("--dry-run", action="store_true", help="仅打印将处理的场次/日期数量，不请求 API")
    stats_api.set_defaults(run=cmd_statistics_api)

    stats_missing_p = sub.add_parser("statistics-missing-by-year", help="查看单场统计缺少年份：按年显示竞彩库中已有 fixture_id 但缺 api_football_statistics 的场次数")
    stats_missing_p.add_argument("--year", type=int, default=None, metavar="YYYY", help="仅统计该年；不传则统计全部年份")
    stats_missing_p.set_defaults(run=cmd_statistics_missing_by_year)

    args = parser.parse_args()
    if args.command == "prediction-log":
        return args.plog_run(args)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
