"""App config: paths, DB, LLM. Override via env (e.g. .env) for cloud later."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

# Base paths
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "football.db"

# Ensure dirs
DATA_DIR.mkdir(exist_ok=True)
RAW_DIR.mkdir(exist_ok=True)

# DB URL (SQLite for MVP; can switch to POSTGRES_DSN later)
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH!s}")

# MongoDB：用于以文档形式查看数据；设为 true 时项目所有 DB 读写走 MongoDB（需配置 MONGO_URI）
MONGO_URI = os.getenv("MONGO_URI", "")  # 例如 mongodb://localhost:27020 或 mongodb+srv://...
# 主库名：repo 的 matches/teams/competitions/predictions/match_odds 等均读写此库。
# 提高命中率且以竞彩为主时，建议设为主库 self_improvement_football（.env: MONGO_DB_NAME=self_improvement_football）
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "football")
# True 时项目所有 DB 读写走 MongoDB（需已配置 MONGO_URI）；默认在配置了 MONGO_URI 时为 true
USE_MONGO = os.getenv("USE_MONGO", "true").lower() in ("1", "true", "yes") and bool((MONGO_URI or "").strip())
# MongoDB 比赛集合名（若实际库中为 matchs 可设 MONGO_MATCHES_COLLECTION=matchs）
MONGO_MATCHES_COLLECTION = os.getenv("MONGO_MATCHES_COLLECTION", "matches")
# 竞彩/自进化库名（与主库同 MONGO_URI，仅库名不同）。与 MONGO_DB_NAME 一致时，预测与竞彩数据同库（推荐）
MONGO_SELF_IMPROVEMENT_DB_NAME = os.getenv("MONGO_SELF_IMPROVEMENT_DB_NAME", "self_improvement_football")

# LLM: provider and key (optional for MVP if using mock)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")  # openai | ollama | mock
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")  # for custom/Chinese APIs
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

# StatsBomb open data (GitHub)
STATSBOMB_BASE = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"

# football-data.org（五大联赛等，免费需注册）
FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "cc1b50acd86e49528decb13bb3386b21")
FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"
# 五大联赛 API 代码
FOOTBALL_DATA_BIG5 = ["PL", "PD", "SA", "BL1", "FL1"]  # 英超 西甲 意甲 德甲 法甲
# 欧冠（免费档可用）；其余杯赛如欧联/德国杯/意杯等需付费订阅，本项目仅拉取欧冠
FOOTBALL_DATA_CUP = "CL"

# 体彩请求代理：拉取竞彩列表(getMatchListV1) 等需访问 webapi.sporttery.cn，若请求无响应可在 .env 配置 SPORTTERY_PROXY（如 http://127.0.0.1:7890）

# 第三方竞彩赔率 API（让球、胜平负、比分、半全场等）。不配置则只使用体彩，无让球/赔率。
# 在 .env 中配置 base、token、path（以服务商文档为准），例如：
#    JC_ODDS_API_BASE=https://api.example.com
#    JC_ODDS_API_TOKEN=你的token
#    JC_ODDS_API_PATH=/sport/api/v1/lot/jc/odds
JC_ODDS_API_BASE = os.getenv("JC_ODDS_API_BASE", "")
JC_ODDS_API_TOKEN = os.getenv("JC_ODDS_API_TOKEN", "")
JC_ODDS_API_PATH = os.getenv("JC_ODDS_API_PATH", "/sport/api/v1/lot/jc/odds")
# 若 token 需放 header 或日期参数名不同，可覆盖：
JC_ODDS_TOKEN_IN_HEADER = os.getenv("JC_ODDS_TOKEN_IN_HEADER", "").lower() in ("1", "true", "yes")
JC_ODDS_DATE_PARAM = os.getenv("JC_ODDS_DATE_PARAM", "start_time")  # 或 matchDate、date 等

# 伤停数据：公众号文章 URL（可手动更新，或通过 --url 传入）
# 来源建议：亚鸣笔记等公众号每日发布的伤停表
# 仪表盘 /predictions 请求时是否自动在后台拉取「该日无伤停记录」的 API-FOOTBALL 伤停（会消耗 key 次数）。默认 false，避免重复请求；需要时请用 main.py injury-api 或 POST /injury-api 按日/按赛季拉取
PREDICTIONS_AUTO_FETCH_INJURY = os.getenv("PREDICTIONS_AUTO_FETCH_INJURY", "false").lower() in ("1", "true", "yes")
INJURY_ARTICLE_URL = os.getenv(
    "INJURY_ARTICLE_URL",
    "https://mp.weixin.qq.com/s/zKSbuHObbnJgG_S1QUnwxg",  # 最新场次伤停（可替换为当日文章）
)
# 按日期选择伤停文章：data/injury_urls.json 中 by_date["YYYY-MM-DD"] 优先于 default_url
INJURY_URLS_PATH = DATA_DIR / "injury_urls.json"
INJURY_SOURCE_NAME = "亚鸣笔记"

# API-FOOTBALL（api-sports.io）伤停/赛程/赔率等；免费档 100 次/天；支持多 key，某 key 超限后自动换下一个
# 赔率：免费档在「可用赛季」上有限制，付费档通常可查更多赛季；若 odds 返回空（本次写入 0 场）可在后台确认或联系支持
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")
API_FOOTBALL_KEY_2 = os.getenv("API_FOOTBALL_KEY_2", "")
# 多 key 列表：优先用 API_FOOTBALL_KEYS（逗号分隔），否则用 API_FOOTBALL_KEY + API_FOOTBALL_KEY_2
_keys_env = (os.getenv("API_FOOTBALL_KEYS") or "").strip()
if _keys_env:
    API_FOOTBALL_KEYS = [k.strip() for k in _keys_env.split(",") if k.strip()]
else:
    API_FOOTBALL_KEYS = [k for k in (API_FOOTBALL_KEY, API_FOOTBALL_KEY_2) if (k or "").strip()]
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
# 每次请求后休眠秒数。免费档约 10 次/分钟用 6.5；付费档 7500 次/天可设 0.05～0.1 加快拉取
API_FOOTBALL_RATE_LIMIT_SEC = float(os.getenv("API_FOOTBALL_RATE_LIMIT_SEC", "6.5"))
# 按日期拉赛程时使用的时区（API 按该时区把比赛归到某日），默认欧洲伦敦，与五大联赛比赛日一致
# 竞彩 fixture_id 回填（jc-match-analysis-backfill-fixture-id）内已固定使用 Asia/Shanghai，与 竞彩日 对齐，不限于五大联赛
API_FOOTBALL_TIMEZONE = os.getenv("API_FOOTBALL_TIMEZONE", "Europe/London")

# 懂球帝抓取：每次请求后休眠秒数，降低封 IP 风险。建议 2～5 秒；日拉取次数不宜过多
DONGQIUDI_RATE_LIMIT_SEC = float(os.getenv("DONGQIUDI_RATE_LIMIT_SEC", "2.5"))
# 单次命令最多请求多少场 prediction 页（列表页不计），防止一次跑太多触发限流
DONGQIUDI_MAX_REQUESTS_PER_RUN = int(os.getenv("DONGQIUDI_MAX_REQUESTS_PER_RUN", "25"))

# Transfermarkt 阵容抓取：请求间隔与单次上限，降低封 IP 风险（与懂球帝同理）
TRANSFERMARKT_RATE_LIMIT_SEC = float(os.getenv("TRANSFERMARKT_RATE_LIMIT_SEC", "2.5"))
TRANSFERMARKT_MAX_REQUESTS_PER_RUN = int(os.getenv("TRANSFERMARKT_MAX_REQUESTS_PER_RUN", "25"))

# 阵容数据来源：True=预测时用懂球帝/TM 赛前预计首发叠加「关键球员未在预计首发」到 key_count；False=仅用伤停名单中的关键球员。基于 2024–2026 lineup-key-absent-analysis 结论（预计缺系统性低于实际关键缺）默认开启以缩小差距。
# 对比用：临时切回旧默认时设 OLD_JC_PREDICT_PARAMS=1（KEY_WEIGHT=2.0, 预计首发不参与）
_USE_OLD_JC_PREDICT_PARAMS = os.getenv("OLD_JC_PREDICT_PARAMS", "").strip() in ("1", "true", "yes")
JC_USE_PREDICTED_LINEUP_FOR_KEY_ABSENT = os.getenv("JC_USE_PREDICTED_LINEUP_FOR_KEY_ABSENT", "false" if _USE_OLD_JC_PREDICT_PARAMS else "true").lower() in ("1", "true", "yes")

# ---------- 比分预测相关（engine.predict） ----------
# 主/客场样本不足时回退混合统计的阈值
PREDICT_MIN_VENUE_SAMPLES = int(os.getenv("PREDICT_MIN_VENUE_SAMPLES", "2"))
# 联赛基准：用于收缩的联赛近期场数
PREDICT_LEAGUE_AVG_LAST_N = int(os.getenv("PREDICT_LEAGUE_AVG_LAST_N", "50"))
# 样本不足时向联赛平均收缩：队伍样本数低于此值时启用收缩（提高会更多向联赛平均收缩）
PREDICT_LEAGUE_SHRINKAGE_MIN_SAMPLES = int(os.getenv("PREDICT_LEAGUE_SHRINKAGE_MIN_SAMPLES", "6"))
# 伤停：每约几人下调 5%（分母），总下调上限；关键球员缺阵按人数×KEY 权重计入（有数据时传入 key 数量）。KEY 权重基于 lineup-key-absent-analysis 结论（预计缺较实际关键缺系统性偏低约 2）从 2.0 调高至 3.0 以加强伤停对期望进球的下调。
PREDICT_INJURY_DOWN_PER_N = float(os.getenv("PREDICT_INJURY_DOWN_PER_N", "2"))
PREDICT_INJURY_CAP = float(os.getenv("PREDICT_INJURY_CAP", "0.15"))
PREDICT_INJURY_KEY_PLAYER_WEIGHT = float(os.getenv("PREDICT_INJURY_KEY_PLAYER_WEIGHT", "3.0" if not _USE_OLD_JC_PREDICT_PARAMS else "2.0"))
# 平局修正系数（势均力敌时提高平局概率；略升可减少漏判平局）
PREDICT_DRAW_BOOST_COEF = float(os.getenv("PREDICT_DRAW_BOOST_COEF", "0.22"))
# Dixon-Coles 低比分相关 rho（负值表示 0-0/1-1 等更常见）
PREDICT_DC_RHO = float(os.getenv("PREDICT_DC_RHO", "-0.13"))
# 历史交锋权重：h2h_exp * blend + venue_exp * (1-blend)
PREDICT_H2H_BLEND = float(os.getenv("PREDICT_H2H_BLEND", "0.3"))
# 休息不足 N 天视为疲劳，对该队期望进球乘 FATIGUE_FACTOR；未来 7 天内比赛数 >= PREDICT_FATIGUE_MATCHES_NEXT_7D 时也视为赛程密集
PREDICT_FATIGUE_DAYS_THRESHOLD = int(os.getenv("PREDICT_FATIGUE_DAYS_THRESHOLD", "3"))
PREDICT_FATIGUE_FACTOR = float(os.getenv("PREDICT_FATIGUE_FACTOR", "0.95"))
PREDICT_FATIGUE_MATCHES_NEXT_7D = int(os.getenv("PREDICT_FATIGUE_MATCHES_NEXT_7D", "2"))  # 未来 7 天内 >= 2 场则施加疲劳系数
# 联赛主场优势乘数（主队期望进球乘此系数，>1）
PREDICT_HOME_ADVANTAGE = float(os.getenv("PREDICT_HOME_ADVANTAGE", "1.05"))
# 多 last_n 融合：用 last_n=5 与 10 的权重；可选加入 15（PREDICT_EXTENDED_LAST_N=15）
PREDICT_FUSION_WEIGHT_5 = float(os.getenv("PREDICT_FUSION_WEIGHT_5", "0.4"))
PREDICT_FUSION_WEIGHT_10 = float(os.getenv("PREDICT_FUSION_WEIGHT_10", "0.6"))
PREDICT_FUSION_WEIGHT_15 = float(os.getenv("PREDICT_FUSION_WEIGHT_15", "0.2"))
PREDICT_EXTENDED_LAST_N = int(os.getenv("PREDICT_EXTENDED_LAST_N", "0"))  # 0=仅 5+10 融合；15=三档 5/10/15 融合
PREDICT_USE_FUSION = os.getenv("PREDICT_USE_FUSION", "true").lower() in ("1", "true", "yes")
# 赔率融合：模型概率与赔率隐含概率线性融合，weight_model 为模型权重（0~1）；提高模型权重有利于冲高胜平负命中率
PREDICT_ODDS_FUSION_WEIGHT_MODEL = float(os.getenv("PREDICT_ODDS_FUSION_WEIGHT_MODEL", "0.55"))
PREDICT_USE_ODDS_FUSION = os.getenv("PREDICT_USE_ODDS_FUSION", "true").lower() in ("1", "true", "yes")
# 预测比分：在「最终」胜平负概率（含赔率融合/校准/ML）对应的结果子集内取 DC 网格众数，避免与 1X2 矛盾（通常略高于全局众数的比分命中率）
PREDICT_SCORE_ALIGN_TO_1X2 = os.getenv("PREDICT_SCORE_ALIGN_TO_1X2", "true").lower() in ("1", "true", "yes")
# 竞彩总进球 zjqs：dc_grid=仅 DC 截断网格求和；poisson_sum=独立泊松下 T~Poisson(λ主+λ客) 无截断；blend=二者线性混合（默认）
PREDICT_ZJQS_MODE = os.getenv("PREDICT_ZJQS_MODE", "blend").strip().lower()
PREDICT_ZJQS_BLEND_DC_WEIGHT = float(os.getenv("PREDICT_ZJQS_BLEND_DC_WEIGHT", "0.22"))
PREDICT_ZJQS_USE_ODDS_FUSION = os.getenv("PREDICT_ZJQS_USE_ODDS_FUSION", "true").lower() in ("1", "true", "yes")
PREDICT_ZJQS_ODDS_FUSION_WEIGHT_MODEL = float(os.getenv("PREDICT_ZJQS_ODDS_FUSION_WEIGHT_MODEL", "0.42"))
# 让球 rqspf：默认用 DC 比分网格上的净胜球分布（与胜平负一致）；BLEND_DC_WEIGHT=1 为纯网格，0 为纯泊松；可与 hhad 三项赔率融合
PREDICT_RQSPF_USE_DC_GRID = os.getenv("PREDICT_RQSPF_USE_DC_GRID", "true").lower() in ("1", "true", "yes")
PREDICT_RQSPF_BLEND_DC_WEIGHT = float(os.getenv("PREDICT_RQSPF_BLEND_DC_WEIGHT", "1.0"))
PREDICT_RQSPF_USE_ODDS_FUSION = os.getenv("PREDICT_RQSPF_USE_ODDS_FUSION", "true").lower() in ("1", "true", "yes")
PREDICT_RQSPF_ODDS_FUSION_WEIGHT_MODEL = float(os.getenv("PREDICT_RQSPF_ODDS_FUSION_WEIGHT_MODEL", "0.35"))
# 竞彩比分 bf：与 crs 开售赔率融合（键为 "1:0" 等）；至少 PREDICT_BF_CRS_MIN_KEYS 项才融合
PREDICT_BF_USE_CRS_FUSION = os.getenv("PREDICT_BF_USE_CRS_FUSION", "true").lower() in ("1", "true", "yes")
PREDICT_BF_CRS_FUSION_WEIGHT_MODEL = float(os.getenv("PREDICT_BF_CRS_FUSION_WEIGHT_MODEL", "0.38"))
PREDICT_BF_CRS_MIN_KEYS = int(os.getenv("PREDICT_BF_CRS_MIN_KEYS", "8"))
# 半全场：拟合 α 输出路径（fit-bqc-alpha）；无文件时用 PREDICT_BQC_ALPHA_DEFAULT
PREDICT_BQC_ALPHA_PATH = os.getenv("PREDICT_BQC_ALPHA_PATH", str(DATA_DIR / "bqc_alpha_by_competition.json"))
PREDICT_BQC_ALPHA_DEFAULT = float(os.getenv("PREDICT_BQC_ALPHA_DEFAULT", "0.45"))
PREDICT_BQC_ALPHA_CLIP_MIN = float(os.getenv("PREDICT_BQC_ALPHA_CLIP_MIN", "0.38"))
PREDICT_BQC_ALPHA_CLIP_MAX = float(os.getenv("PREDICT_BQC_ALPHA_CLIP_MAX", "0.52"))
# 赔率质量：隐含概率总和应在此区间，否则不参与融合（典型 overround 约 1.02～1.15）
PREDICT_ODDS_IMPLIED_SUM_MIN = float(os.getenv("PREDICT_ODDS_IMPLIED_SUM_MIN", "0.85"))
PREDICT_ODDS_IMPLIED_SUM_MAX = float(os.getenv("PREDICT_ODDS_IMPLIED_SUM_MAX", "1.35"))
# 赔率时效：超过此小时数则不参与融合（不设或 0 表示不限制）
PREDICT_ODDS_MAX_AGE_HOURS = os.getenv("PREDICT_ODDS_MAX_AGE_HOURS", "")
try:
    PREDICT_ODDS_MAX_AGE_HOURS = float(PREDICT_ODDS_MAX_AGE_HOURS) if PREDICT_ODDS_MAX_AGE_HOURS else None
except ValueError:
    PREDICT_ODDS_MAX_AGE_HOURS = None
# 按联赛覆盖预测参数：JSON 文件路径，格式 {"Serie A": {"last_n": 5, "PREDICT_HOME_ADVANTAGE": 1.03}, ...}
PREDICT_LEAGUE_PARAMS_PATH = os.getenv("PREDICT_LEAGUE_PARAMS_PATH", str(DATA_DIR / "predict_league_params.json"))
JC_TIMESLICE_PARAMS_PATH = os.getenv("JC_TIMESLICE_PARAMS_PATH", str(DATA_DIR / "jc_timeslice_params.json"))
JC_TIMESLICE_PROFILE = os.getenv("JC_TIMESLICE_PROFILE", "rolling_halfyear_v1")
# 低置信度标记：主/客近期样本数任一侧低于此值，或期望进球差绝对值低于下一项时标为 low，便于前端「仅供参考」
PREDICT_LOW_CONFIDENCE_MIN_SAMPLES = int(os.getenv("PREDICT_LOW_CONFIDENCE_MIN_SAMPLES", "5"))
PREDICT_LOW_CONFIDENCE_MAX_SPREAD = float(os.getenv("PREDICT_LOW_CONFIDENCE_MAX_SPREAD", "0.25"))
# 概率校准：是否对胜平负概率做校准（需先运行 calibrate-probs 生成 data/calibration_params.json）
PREDICT_USE_CALIBRATION = os.getenv("PREDICT_USE_CALIBRATION", "true").lower() in ("1", "true", "yes")
PREDICT_CALIBRATION_PATH = os.getenv("PREDICT_CALIBRATION_PATH", str(DATA_DIR / "calibration_params.json"))
# xG：若有近期 xG 数据可传入 predict_match，与形式期望进球按此比例混合（0=不用 xG）
PREDICT_USE_XG = os.getenv("PREDICT_USE_XG", "false").lower() in ("1", "true", "yes")
PREDICT_XG_BLEND = float(os.getenv("PREDICT_XG_BLEND", "0.3"))
# 2.2 ML 修正层：在规则输出上接小模型做胜平负修正（需先 train-outcome-ml）；提高 WEIGHT 可让 ML 更主导（0.45 在 2026 评估中单场推荐较 0.4 提升约 0.3%）
PREDICT_USE_OUTCOME_ML = os.getenv("PREDICT_USE_OUTCOME_ML", "true").lower() in ("1", "true", "yes")
PREDICT_OUTCOME_ML_WEIGHT = float(os.getenv("PREDICT_OUTCOME_ML_WEIGHT", "0.35"))
PREDICT_OUTCOME_ML_PATH = os.getenv("PREDICT_OUTCOME_ML_PATH", str(DATA_DIR / "outcome_ml_model.joblib"))
PREDICT_OUTCOME_ML_RANDOM_STATE = int(os.getenv("PREDICT_OUTCOME_ML_RANDOM_STATE", "55"))
PREDICT_OUTCOME_ML_N_ESTIMATORS = int(os.getenv("PREDICT_OUTCOME_ML_N_ESTIMATORS", "100"))
PREDICT_OUTCOME_ML_MAX_DEPTH = int(os.getenv("PREDICT_OUTCOME_ML_MAX_DEPTH", "4"))
PREDICT_OUTCOME_ML_MIN_SAMPLES_LEAF = int(os.getenv("PREDICT_OUTCOME_ML_MIN_SAMPLES_LEAF", "8"))
# 积分榜排名微调：有主客排名时对期望进球做小幅调整（主队排名更好则略升主队、降客队）
PREDICT_USE_STANDINGS_NUDGE = os.getenv("PREDICT_USE_STANDINGS_NUDGE", "true").lower() in ("1", "true", "yes")
PREDICT_STANDINGS_NUDGE_FACTOR = float(os.getenv("PREDICT_STANDINGS_NUDGE_FACTOR", "0.04"))
# 边界场次略偏主：当 0 < 主队期望进球-客队 <= SPREAD 时，主队期望乘 (1+NUDGE)；预测输出 is_boundary 供产品标为「仅供参考」
PREDICT_BOUNDARY_HOME_SPREAD = float(os.getenv("PREDICT_BOUNDARY_HOME_SPREAD", "0.28"))
PREDICT_BOUNDARY_HOME_NUDGE = float(os.getenv("PREDICT_BOUNDARY_HOME_NUDGE", "0.05"))
# API-Football 预测融合：与模型胜平负概率线性融合，weight 为 API 权重（0~1）
PREDICT_USE_API_PREDICTIONS = os.getenv("PREDICT_USE_API_PREDICTIONS", "true").lower() in ("1", "true", "yes")
PREDICT_API_PREDICTIONS_WEIGHT = float(os.getenv("PREDICT_API_PREDICTIONS_WEIGHT", "0.30"))
# 近期形式权重：0=线性加权，0<decay<1 时用指数衰减（最近场次权重大），如 0.85
PREDICT_FORM_WEIGHT_DECAY = float(os.getenv("PREDICT_FORM_WEIGHT_DECAY", "0"))
# 比分网格最大进球数（每侧 0..MAX_GOALS-1），默认 8 即 8×8
PREDICT_MAX_GOALS = int(os.getenv("PREDICT_MAX_GOALS", "8"))
# 实际进球−xG 微调：有 xG 时按近期（实际−xG）×FACTOR 微调期望，CAP 为单侧上下限，0=关闭
PREDICT_ACTUAL_XG_NUDGE_FACTOR = float(os.getenv("PREDICT_ACTUAL_XG_NUDGE_FACTOR", "0.05"))
PREDICT_ACTUAL_XG_NUDGE_CAP = float(os.getenv("PREDICT_ACTUAL_XG_NUDGE_CAP", "0.15"))
# 连续客场：客队最近 CONSECUTIVE_AWAY_MIN 场及以上均为客场时，期望乘 FACTOR（如 0.98）
PREDICT_CONSECUTIVE_AWAY_MIN = int(os.getenv("PREDICT_CONSECUTIVE_AWAY_MIN", "2"))
PREDICT_CONSECUTIVE_AWAY_FACTOR = float(os.getenv("PREDICT_CONSECUTIVE_AWAY_FACTOR", "0.98"))

# 预测日志：True = 仅用 DB（prediction_log 表），不读写 prediction_log.json；False = 仅用 JSON（不推荐）
PREDICTION_LOG_USE_DB = os.getenv("PREDICTION_LOG_USE_DB", "true").lower() in ("1", "true", "yes")
# 单场推荐：仅当推荐概率≥此阈值时视为高置信（single_rec_confidence=high），便于前端只展示高置信或统计分档命中率；主推高置信时建议 0.55
SINGLE_REC_MIN_PROB = float(os.getenv("SINGLE_REC_MIN_PROB", "0.55"))
# 让球让平增强：当期望进球差与让球数之和的绝对值 <= 此阈值时，认为「双方接近」，对让平概率做加成，减少误推让胜/让负（未命中多为「实际让平」）
PREDICT_RQ_DRAW_BOOST_THRESHOLD = float(os.getenv("PREDICT_RQ_DRAW_BOOST_THRESHOLD", "0.60"))
PREDICT_RQ_DRAW_BOOST = float(os.getenv("PREDICT_RQ_DRAW_BOOST", "0.12"))
# 让球推荐保守：只有让胜/让负概率 >= 此值时才允许出让球推荐，否则在胜/负与最佳差在 gap 内时优先出胜/负（提高单场推荐命中率：0.65 更保守）
SINGLE_REC_RQ_MIN_PROB = float(os.getenv("SINGLE_REC_RQ_MIN_PROB", "0.65"))
# 让负单独门槛：让负误推占未命中约一半，仅当让负概率 >= 此值时才推荐让负（否则优先出「负」），0.68 更保守
SINGLE_REC_RQ_MIN_PROB_LET_FU = float(os.getenv("SINGLE_REC_RQ_MIN_PROB_LET_FU", "0.68"))
# 接近盘优先让平：当让平概率与让胜/让负最高项差距 <= 此值时，优先推荐让平（减少「实际让平」类未命中），0.05 覆盖更多接近盘
SINGLE_REC_RQ_PREFER_DRAW_WHEN_CLOSE = float(os.getenv("SINGLE_REC_RQ_PREFER_DRAW_WHEN_CLOSE", "0.05"))
# 数据驱动自我提升：按「联赛×推荐类型×概率区间」历史命中率覆盖；该文件由 jc-predictions-eval --export-segment-stats 生成
# 为优先保证单场推荐命中率，默认关闭细分覆盖（不读该文件）；设为 true 时启用，全量命中率可能略降、高置信子集命中率可略升
JC_REC_USE_SEGMENT_STATS = os.getenv("JC_REC_USE_SEGMENT_STATS", "false").strip().lower() in ("1", "true", "yes")
JC_REC_SEGMENT_STATS_PATH = os.getenv("JC_REC_SEGMENT_STATS_PATH", str(DATA_DIR / "jc_rec_segment_hit_rates.json"))
# 当某细分历史命中率低于此阈值且样本数>=JC_REC_SEGMENT_MIN_N 时，该细分下改推胜/负；默认 0.35 仅改最差细分以减轻全量命中率下降
JC_REC_SEGMENT_HIT_RATE_FLOOR = float(os.getenv("JC_REC_SEGMENT_HIT_RATE_FLOOR", "0.35"))
JC_REC_SEGMENT_MIN_N = int(os.getenv("JC_REC_SEGMENT_MIN_N", "80"))
# 今日推荐默认最低推荐概率：0.55 时场次更多、命中率约 62–65%（多推几场）
JC_REC_DEFAULT_MIN_PROB = float(os.getenv("JC_REC_DEFAULT_MIN_PROB", "0.55"))
# 高置信门槛：推荐概率 ≥ 此值才标为「首选/高置信」；0.62 时首选场次更多、命中率约 65% 左右
JC_REC_HIGH_CONFIDENCE_MIN_PROB = float(os.getenv("JC_REC_HIGH_CONFIDENCE_MIN_PROB", "0.62"))
# 按联赛微调命中率（目标 70%）：该文件由 jc-predictions-eval --export-league-rec-params 生成；今日推荐时仅保留 rec_prob >= 该联赛 min_prob_70 的场次
JC_REC_LEAGUE_PARAMS_PATH = os.getenv("JC_REC_LEAGUE_PARAMS_PATH", str(DATA_DIR / "jc_league_rec_params.json"))
JC_REC_TARGET_HIT_RATE = float(os.getenv("JC_REC_TARGET_HIT_RATE", "0.70"))
JC_REC_LEAGUE_MIN_N = int(os.getenv("JC_REC_LEAGUE_MIN_N", "20"))  # 计算 min_prob_70 时联赛最少样本数
# 2026 年比赛列表单场推荐：用 2021–2025 年数据为辅，提高 2026 命中率
# 设为 true 且日期为 2026 时，今日推荐/2026 列表加载下方辅助年导出的细分与联赛参数
JC_REC_FOR_2026_USE_AUXILIARY = os.getenv("JC_REC_FOR_2026_USE_AUXILIARY", "true").strip().lower() in ("1", "true", "yes")
JC_REC_AUXILIARY_SEGMENT_STATS_PATH = os.getenv("JC_REC_AUXILIARY_SEGMENT_STATS_PATH", str(DATA_DIR / "jc_rec_segment_hit_rates_2021_2025.json"))
JC_REC_AUXILIARY_LEAGUE_PARAMS_PATH = os.getenv("JC_REC_AUXILIARY_LEAGUE_PARAMS_PATH", str(DATA_DIR / "jc_league_rec_params_2021_2025.json"))

# 竞彩联赛中文名 -> predict_league_params.json 英文键名（用于按联赛调伤停权重合并）
# 导出 injury 时仅映射到此处的联赛会进入 for_merge；merge-injury-params 会写入/更新对应英文键的 PREDICT_INJURY_KEY_PLAYER_WEIGHT
LEAGUE_CN_TO_EN: dict[str, str] = {
    # 五大联赛
    "英超": "Premier League", "西甲": "La Liga", "意甲": "Serie A", "德甲": "Bundesliga", "法甲": "Ligue 1",
    # 主流联赛
    "英冠": "Championship", "荷甲": "Eredivisie", "葡超": "Primeira Liga", "德乙": "2. Bundesliga",
    "澳超": "A-League", "日职": "J1 League", "韩职": "K League 1", "美职": "MLS",
    "瑞超": "Allsvenskan", "挪超": "Eliteserien", "荷乙": "Eerste Divisie", "法乙": "Ligue 2", "日乙": "J2 League",
    "沙职": "Saudi Pro League",
    # 欧战 / 亚冠
    "欧冠": "UEFA Champions League", "欧罗巴": "UEFA Europa League", "欧协联": "UEFA Europa Conference League",
    "亚冠精英": "AFC Champions League Elite", "亚冠乙": "AFC Champions League Two",
    # 杯赛等（合并时会写入 predict_league_params.json，引擎按英文键读取）
    "瑞典杯": "Swedish Cup",
}


def get_league_overrides() -> dict:
    """从 data/predict_league_params.json 读取按联赛覆盖的预测参数。键为赛事名，值为参数字典。"""
    path = Path(PREDICT_LEAGUE_PARAMS_PATH)
    if not path.is_file():
        return {}
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_timeslice_overrides(date_str: str | None, profile: str | None = None) -> dict:
    """按日期读取时间切片参数覆盖。"""
    if not date_str or len(date_str) < 10:
        return {}
    date_key = date_str[:10]
    path = Path(JC_TIMESLICE_PARAMS_PATH)
    if not path.is_file():
        return {}
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    entries = None
    if isinstance(data, dict):
        profiles = data.get("profiles")
        selected_profile = profile or JC_TIMESLICE_PROFILE
        if isinstance(profiles, dict) and isinstance(profiles.get(selected_profile), list):
            entries = profiles.get(selected_profile)
        elif isinstance(data.get("slices"), list):
            entries = data.get("slices")
    elif isinstance(data, list):
        entries = data
    if not isinstance(entries, list):
        return {}

    month_day = date_key[5:10]

    def _in_month_day_range(md: str, md_from: str, md_to: str) -> bool:
        # 支持跨年区间：例如 11-15 ~ 02-20
        if md_from <= md_to:
            return md_from <= md <= md_to
        return md >= md_from or md <= md_to

    for item in entries:
        if not isinstance(item, dict):
            continue
        params = item.get("params")
        if not isinstance(params, dict):
            continue

        date_from = str(item.get("date_from") or "")[:10]
        date_to = str(item.get("date_to") or "")[:10]
        if date_from and date_to and date_from <= date_key <= date_to:
            return params

        month_day_from = str(item.get("month_day_from") or "")[:5]
        month_day_to = str(item.get("month_day_to") or "")[:5]
        if (
            len(month_day_from) == 5
            and len(month_day_to) == 5
            and _in_month_day_range(month_day, month_day_from, month_day_to)
        ):
            return params
    return {}


def get_injury_url_for_date(date_str: str | None) -> str:
    """按日期取伤停文章 URL。date_str 为 YYYY-MM-DD，若为 None 或无效则用 default_url。"""
    if not date_str or len(date_str) < 10:
        return INJURY_ARTICLE_URL
    date_key = date_str[:10]
    if not INJURY_URLS_PATH.is_file():
        return INJURY_ARTICLE_URL
    try:
        import json
        data = json.loads(INJURY_URLS_PATH.read_text(encoding="utf-8"))
        by_date = data.get("by_date") or {}
        if date_key in by_date and by_date[date_key]:
            return by_date[date_key]
        return data.get("default_url") or INJURY_ARTICLE_URL
    except Exception:
        return INJURY_ARTICLE_URL


def save_injury_url_for_date(date_str: str, url: str) -> None:
    """将某日使用的伤停文章 URL 写入配置，便于按日期自动换文章。"""
    import json
    date_key = date_str[:10]
    if len(date_key) != 10:
        return
    data = {"source_name": INJURY_SOURCE_NAME, "default_url": INJURY_ARTICLE_URL, "by_date": {}}
    if INJURY_URLS_PATH.is_file():
        try:
            data = json.loads(INJURY_URLS_PATH.read_text(encoding="utf-8"))
            if not isinstance(data.get("by_date"), dict):
                data["by_date"] = {}
        except Exception:
            pass
    data.setdefault("source_name", INJURY_SOURCE_NAME)
    data.setdefault("default_url", INJURY_ARTICLE_URL)
    data["by_date"][date_key] = url
    INJURY_URLS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
