"""Unified DB schema: competitions, matches, teams, events. SQLite for MVP."""
from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    Float,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    Index,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.engine import Engine

import config

Base = declarative_base()


def get_engine() -> Engine:
    return create_engine(
        config.DATABASE_URL,
        connect_args={"check_same_thread": False} if "sqlite" in config.DATABASE_URL else {},
        echo=False,
    )


def init_db(engine: Engine | None = None) -> None:
    eng = engine or get_engine()
    Base.metadata.create_all(eng)
    # 已有 SQLite 库补列：teams.name_zh（若不存在则添加）
    if eng and "sqlite" in (eng.url.drivername or ""):
        with eng.connect() as conn:
            try:
                r = conn.execute(text("PRAGMA table_info(teams)"))
                cols = [row[1] for row in r.fetchall()]
                if "name_zh" not in cols:
                    conn.execute(text("ALTER TABLE teams ADD COLUMN name_zh VARCHAR(255)"))
                    conn.commit()
            except Exception:
                pass


class Competition(Base):
    __tablename__ = "competitions"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    country_name = Column(String(255))


class Season(Base):
    __tablename__ = "seasons"
    id = Column(Integer, primary_key=True)
    competition_id = Column(Integer, ForeignKey("competitions.id"), nullable=False)
    name = Column(String(64), nullable=False)  # e.g. 2020/2021


class Team(Base):
    __tablename__ = "teams"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    name_zh = Column(String(255), nullable=True)  # 队名中文，用于按中文名精确匹配


class Match(Base):
    __tablename__ = "matches"
    id = Column(Integer, primary_key=True)
    competition_id = Column(Integer, ForeignKey("competitions.id"))
    season_id = Column(Integer, ForeignKey("seasons.id"))
    match_date = Column(String(32))
    home_team_id = Column(Integer, ForeignKey("teams.id"))
    away_team_id = Column(Integer, ForeignKey("teams.id"))
    home_score = Column(Integer)
    away_score = Column(Integer)
    match_week = Column(Integer)
    source_id = Column(String(64), unique=True)  # e.g. statsbomb match_id as string
    raw_meta = Column(Text)  # optional JSON for home_team_name, away_team_name, etc.
    home_rank = Column(Integer, nullable=True)   # 主队当季积分榜排名（来自 league_standings）
    away_rank = Column(Integer, nullable=True)   # 客队当季积分榜排名


class Prediction(Base):
    """赛前预测记录：用于与完赛结果对比，做事后分析（期望进球 MAE、胜平负准确率）。"""
    __tablename__ = "predictions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=False, index=True)
    exp_goals_home = Column(Float, nullable=False)
    exp_goals_away = Column(Float, nullable=False)
    prob_home_win = Column(Float, nullable=False)
    prob_draw = Column(Float, nullable=False)
    prob_away_win = Column(Float, nullable=False)
    last_n = Column(Integer, nullable=False)
    competition_id = Column(Integer, ForeignKey("competitions.id"))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class PredictionLog(Base):
    """
    预测日志表：与 prediction_log.json 同源，存到 DB 后与 matches+predictions 一致。
    每条 = 一场的预测 + 可选实际比分。match_id 非空时表示来自 DB 的已完赛预测，唯一。
    """
    __tablename__ = "prediction_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=True, index=True, unique=True)  # 来自 DB 时必填且唯一
    log_date = Column(String(32), nullable=False, index=True)  # 比赛日 YYYY-MM-DD
    home_team = Column(String(255), nullable=False)
    away_team = Column(String(255), nullable=False)
    competition = Column(String(255), default="")
    exp_goals_home = Column(Float, nullable=False)
    exp_goals_away = Column(Float, nullable=False)
    prob_home_win = Column(Float, nullable=False)
    prob_draw = Column(Float, nullable=False)
    prob_away_win = Column(Float, nullable=False)
    predicted_result = Column(String(16), nullable=False)  # 主/平/客
    last_n = Column(Integer, nullable=False)
    actual_home_score = Column(Integer, nullable=True)
    actual_away_score = Column(Integer, nullable=True)
    actual_result = Column(String(16), nullable=True)  # 主/平/客
    confidence = Column(String(32), default="high")
    odds_used = Column(Boolean, default=False)
    predicted_scores_json = Column(Text, nullable=True)  # JSON 如 [[1,1],[2,1]]
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class MatchOdds(Base):
    """单场赔率（API-FOOTBALL 等）：按比赛日+主客队+联赛存储，供预测融合。"""
    __tablename__ = "match_odds"
    id = Column(Integer, primary_key=True, autoincrement=True)
    log_date = Column(String(32), nullable=False, index=True)  # YYYY-MM-DD
    home_team = Column(String(255), nullable=False)
    away_team = Column(String(255), nullable=False)
    league_cn = Column(String(64), nullable=False)
    spf_win = Column(String(32), nullable=True)
    spf_draw = Column(String(32), nullable=True)
    spf_loss = Column(String(32), nullable=True)
    source = Column(String(64), default="api_football")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("log_date", "home_team", "away_team", "league_cn", name="uq_match_odds_lookup"),
        Index("ix_match_odds_lookup", "log_date", "home_team", "away_team", "league_cn"),
    )


class LeagueStanding(Base):
    """积分榜单行：联赛+赛季+队伍排名，供预测时排名特征。"""
    __tablename__ = "league_standings"
    id = Column(Integer, primary_key=True, autoincrement=True)
    league_id = Column(Integer, nullable=False, index=True)  # API-FOOTBALL league id
    season = Column(Integer, nullable=False, index=True)
    team_name = Column(String(255), nullable=False)
    rank = Column(Integer, nullable=True)
    points = Column(Integer, nullable=True)
    goals_diff = Column(Integer, nullable=True)
    form = Column(String(64), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (Index("ix_league_standings_league_season", "league_id", "season"),)


class MatchInjury(Base):
    """单场伤停：按比赛日+主客队+联赛存主客队缺阵名单（JSON），供预测下调。"""
    __tablename__ = "match_injury"
    id = Column(Integer, primary_key=True, autoincrement=True)
    log_date = Column(String(32), nullable=False, index=True)
    home_team = Column(String(255), nullable=False)
    away_team = Column(String(255), nullable=False)
    league_cn = Column(String(64), nullable=False)
    home_absent_json = Column(Text, nullable=True)  # JSON array
    away_absent_json = Column(Text, nullable=True)
    source = Column(String(64), default="api_football")
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (Index("ix_match_injury_lookup", "log_date", "home_team", "away_team", "league_cn"),)


class BetRecord(Base):
    """
    投注花费与收益记录：每一条为一次下注。
    下注日 record_date 登记花费 amount_spent；收益 amount_returned 一般次日或开奖后到账，可先填 0 后通过接口补填。
    盈亏 = amount_returned - amount_spent。
    """
    __tablename__ = "bet_records"
    id = Column(Integer, primary_key=True, autoincrement=True)
    record_date = Column(String(32), nullable=False, index=True)  # 下注日 YYYY-MM-DD
    amount_spent = Column(Float, nullable=False, default=0.0)      # 花费（元）
    amount_returned = Column(Float, nullable=False, default=0.0)  # 收益（元），未开奖可填 0，次日或开奖后可编辑补填
    note = Column(String(512), nullable=True)                     # 备注（如「今日一单」「跟推荐」）
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Event(Base):
    """Unified event: one row per event, flattened for analysis."""
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=False, index=True)
    source_event_id = Column(String(64))  # upstream id
    index = Column(Integer)
    period = Column(Integer)
    minute = Column(Integer)
    second = Column(Integer)
    event_type = Column(String(64), nullable=False, index=True)  # Pass, Shot, Carry, etc.
    team_id = Column(Integer, ForeignKey("teams.id"))
    team_name = Column(String(255))
    player_id = Column(Integer)
    player_name = Column(String(255))
    position_name = Column(String(128))
    location_x = Column(Float)
    location_y = Column(Float)
    end_location_x = Column(Float)
    end_location_y = Column(Float)
    duration = Column(Float)
    under_pressure = Column(Boolean, default=False)
    outcome = Column(String(64))  # Complete, Incomplete, Goal, Saved, etc.
    outcome_id = Column(Integer)
    raw_type_id = Column(Integer)
    raw_json = Column(Text)  # optional full payload for extensibility

    __table_args__ = (Index("ix_events_match_type", "match_id", "event_type"),)
