"""
将 football 库的数据迁移到 self_improvement_football，便于后续只使用 self_improvement_football。
迁移后可将 MONGO_DB_NAME 设为 self_improvement_football，并可选删除 football 库。
"""
from __future__ import annotations

import logging
from typing import Any

import config
from db.mongo import COLLECTIONS, get_mongo_client

logger = logging.getLogger(__name__)

SOURCE_DB_NAME = "football"
TARGET_DB_NAME = getattr(config, "MONGO_SELF_IMPROVEMENT_DB_NAME", "self_improvement_football")

# 要迁移的集合：主库通用集合 + repo 可能用到的 match_lineups、bet_records
COLLECTIONS_TO_MIGRATE = list(COLLECTIONS) + ["match_lineups", "bet_records"]


def _copy_coll(
    source_coll,
    target_coll,
    *,
    use_id_key: bool = True,
    batch_size: int = 500,
) -> tuple[int, int]:
    """
    将 source_coll 中 target_coll 尚未存在的文档复制过去。
    use_id_key: True 时用文档的 "id" 字段去重；False 时用 "_id"。
    返回 (已插入数, 跳过数)。
    """
    inserted = 0
    skipped = 0
    key_field = "id" if use_id_key else "_id"
    buffer: list[dict[str, Any]] = []

    for doc in source_coll.find({}):
        key_val = doc.get(key_field) if key_field in doc else doc.get("_id")
        if key_val is None and key_field == "id":
            # 无 id 的文档用 _id 判断
            key_val = doc.get("_id")
            check_field = "_id"
        else:
            check_field = key_field

        if target_coll.find_one({check_field: key_val}) is not None:
            skipped += 1
            continue

        # 插入副本；按 id 去重时去掉 _id 避免与目标库已有文档的 _id 冲突，目标库会生成新 _id
        to_insert = dict(doc)
        if use_id_key and "_id" in to_insert:
            del to_insert["_id"]
        buffer.append(to_insert)

        if len(buffer) >= batch_size:
            try:
                target_coll.insert_many(buffer, ordered=False)
                inserted += len(buffer)
            except Exception as e:
                # 可能有唯一键冲突，逐条插入
                for d in buffer:
                    try:
                        target_coll.insert_one(d)
                        inserted += 1
                    except Exception:
                        skipped += 1
                logger.warning("insert_many 部分失败，已逐条重试: %s", e)
            buffer = []

    if buffer:
        try:
            target_coll.insert_many(buffer, ordered=False)
            inserted += len(buffer)
        except Exception as e:
            for d in buffer:
                try:
                    target_coll.insert_one(d)
                    inserted += 1
                except Exception:
                    skipped += 1
            logger.warning("最后一批 insert_many 部分失败: %s", e)

    return inserted, skipped


def migrate_football_to_self_improvement(
    *,
    drop_source_after: bool = False,
    collections: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    将 football 库中指定集合的数据迁移到 self_improvement_football。
    默认只插入目标库中不存在的文档（按 id 或 _id 去重），不覆盖已有数据。

    drop_source_after: 迁移完成后是否删除 football 库（慎用）。
    collections: 要迁移的集合名列表，默认 COLLECTIONS_TO_MIGRATE。
    dry_run: True 时只打印将要迁移的集合与文档数，不写入。

    返回 {"ok": bool, "results": {coll: {"inserted": n, "skipped": n, "source_count": n}}, "dropped_source": bool}
    """
    client = get_mongo_client()
    if client is None:
        return {"ok": False, "error": "未配置 MONGO_URI", "results": {}, "dropped_source": False}

    source_db = client[SOURCE_DB_NAME]
    target_db = client[TARGET_DB_NAME]
    coll_list = collections or COLLECTIONS_TO_MIGRATE
    results: dict[str, dict[str, int]] = {}

    for coll_name in coll_list:
        if coll_name not in source_db.list_collection_names():
            continue
        source_coll = source_db[coll_name]
        target_coll = target_db[coll_name]
        source_count = source_coll.count_documents({})
        if source_count == 0:
            continue
        if dry_run:
            results[coll_name] = {"inserted": 0, "skipped": 0, "source_count": source_count}
            continue
        # 有 "id" 的集合用 id 去重（competitions, teams, matches 等）
        sample = source_coll.find_one({})
        use_id = sample is not None and "id" in sample
        inserted, skipped = _copy_coll(source_coll, target_coll, use_id_key=use_id)
        results[coll_name] = {"inserted": inserted, "skipped": skipped, "source_count": source_count}

    dropped = False
    if drop_source_after and not dry_run and results:
        client.drop_database(SOURCE_DB_NAME)
        dropped = True

    return {"ok": True, "results": results, "dropped_source": dropped}


def drop_football_db() -> bool:
    """删除 football 数据库。需在迁移后显式调用（或使用 migrate 的 --drop-source-db）。"""
    client = get_mongo_client()
    if client is None:
        return False
    client.drop_database(SOURCE_DB_NAME)
    return True
