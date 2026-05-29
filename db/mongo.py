"""
MongoDB 连接与集合定义：与 SQLite 表一一对应，便于在 MongoDB Compass 中查看具体数据。
配置 MONGO_URI 后运行 main.py sync-sqlite-to-mongo 将 SQLite 数据同步到 MongoDB。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import config

# 集合名与 SQL 表名一致，便于对照
COLLECTIONS = (
    "competitions",
    "seasons",
    "teams",
    "matches",
    "predictions",
    "prediction_log",
    "match_odds",
    "league_standings",
    "match_injury",
    "events",
)

_client: Any = None


def get_mongo_client():
    """获取 MongoDB 客户端；未配置 MONGO_URI 时返回 None。"""
    if not (getattr(config, "MONGO_URI", "") or "").strip():
        return None
    global _client
    if _client is None:
        try:
            from pymongo import MongoClient
            _client = MongoClient(config.MONGO_URI)
            # 触发一次 ping 确保连接有效
            _client.admin.command("ping")
        except Exception:
            raise
    return _client


def get_mongo_db():
    """获取当前配置的 MongoDB 数据库；未配置 MONGO_URI 时返回 None。"""
    client = get_mongo_client()
    if client is None:
        return None
    return client[getattr(config, "MONGO_DB_NAME", "football")]


def _serialize_value(v: Any) -> Any:
    """将 Python 对象转为 MongoDB/JSON 友好格式（如 datetime -> ISO 字符串）。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat() + "Z" if v.tzinfo is None else v.isoformat()
    if isinstance(v, (dict, list)):
        return _serialize_doc(v)
    return v


def _serialize_doc(obj: dict | list) -> dict | list:
    """递归序列化字典/列表中的 datetime。"""
    if isinstance(obj, dict):
        return {k: _serialize_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize_value(x) for x in obj]
    return obj


def row_to_doc(row: Any) -> dict[str, Any]:
    """将 SQLAlchemy 模型实例转为 MongoDB 文档（保留原字段名；不强制 _id=id 避免 SQLite 重复 id 导致写入失败）。"""
    if hasattr(row, "__table__"):
        # ORM 模型
        doc = {}
        for c in row.__table__.columns:
            key = c.key
            val = getattr(row, key, None)
            doc[key] = _serialize_value(val)
        # 不设置 _id，由 MongoDB 自动生成，避免 events 等表存在重复 id 时 E11000
        return doc
    if hasattr(row, "_mapping"):
        # CursorResult row (Row)
        doc = dict(row._mapping)
        return _serialize_doc(doc)
    return _serialize_doc(dict(row))


# 表名 -> SQLAlchemy 模型（用于同步）
TABLE_MODEL_MAP = (
    ("competitions", "Competition"),
    ("seasons", "Season"),
    ("teams", "Team"),
    ("matches", "Match"),
    ("predictions", "Prediction"),
    ("prediction_log", "PredictionLog"),
    ("match_odds", "MatchOdds"),
    ("league_standings", "LeagueStanding"),
    ("match_injury", "MatchInjury"),
    ("events", "Event"),
)


def sync_sqlite_to_mongo(*, replace: bool = True) -> dict[str, int]:
    """
    将当前 SQLite 中所有表的数据同步到 MongoDB（全量覆盖对应 collection）。
    要求已配置 MONGO_URI。返回各集合写入的文档数 {"competitions": N, ...}。
    """
    db = get_mongo_db()
    if db is None:
        raise RuntimeError("未配置 MONGO_URI，无法同步。请在 .env 中设置 MONGO_URI 与可选 MONGO_DB_NAME。")

    from sqlalchemy.orm import Session
    from etl.schema import (
        get_engine,
        init_db,
        Competition,
        Season,
        Team,
        Match,
        Prediction,
        PredictionLog,
        MatchOdds,
        LeagueStanding,
        MatchInjury,
        Event,
    )

    engine = get_engine()
    init_db(engine)
    model_by_name = {
        "Competition": Competition,
        "Season": Season,
        "Team": Team,
        "Match": Match,
        "Prediction": Prediction,
        "PredictionLog": PredictionLog,
        "MatchOdds": MatchOdds,
        "LeagueStanding": LeagueStanding,
        "MatchInjury": MatchInjury,
        "Event": Event,
    }
    counts: dict[str, int] = {}
    with Session(engine) as session:
        for coll_name, model_name in TABLE_MODEL_MAP:
            model = model_by_name[model_name]
            rows = session.query(model).all()
            docs = [row_to_doc(r) for r in rows]
            coll = db[coll_name]
            if replace:
                coll.delete_many({})
            if docs:
                coll.insert_many(docs)
            counts[coll_name] = len(docs)
    return counts
