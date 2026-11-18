"""薄编排：loader → normalizer → gazette_parse → entity_resolve → dedupe →
versioning → attach → keyer → store。不含业务判断。

每个阶段一个模块、一个聚焦入口：

* 若模块提供 ``process_batch(records, con)``，则整批交给它（dedupe 需要全量
  候选才能聚类；entity_resolve / versioning 需要持久化别名与版本号）；
* 否则按记录逐条调用 ``process``。

编排层只负责顺序、把 store 交给需要持久化的阶段、收集各阶段自报的
``last_stats`` 成本统计、以及最终 upsert；候选门槛、合并判定、版本语义都在
各业务模块内。
"""
from __future__ import annotations

import importlib
from typing import Any

from . import loader as _loader
from . import store as _store
from .keyer import key_of
from .models import Record

STAGE_NAMES = [
    "normalizer",
    "gazette_parse",
    "entity_resolve",
    "dedupe",
    "versioning",
    "attach",
]

# 兼容基线常量名
CHAIN_NAMES = STAGE_NAMES


def run_stage(name: str, records: list[dict[str, Any]], con=None) -> list[dict[str, Any]]:
    """运行单个阶段；批处理入口优先，便于单独测试一个阶段。"""
    module = importlib.import_module("." + name, __package__)
    batch = getattr(module, "process_batch", None)
    if batch is not None:
        return list(batch(records, con))
    return [module.process(record) for record in records]


def _stage_stats(name: str) -> dict[str, Any] | None:
    module = importlib.import_module("." + name, __package__)
    stats = getattr(module, "last_stats", None)
    return dict(stats) if stats is not None else None


def run(con=None, fixture=None, records=None) -> dict[str, Any]:
    own = con is None
    con = con or _store.connect()
    try:
        if records is None:
            records = _loader.load(fixture)
        fed = len(records)
        pipeline_stats: dict[str, Any] = {}
        for name in STAGE_NAMES:
            records = run_stage(name, records, con)
            stats = _stage_stats(name)
            if stats is not None:
                pipeline_stats[name] = stats

        for record in records:
            key = key_of(record)
            # attach.key 让「键值→证据」在落库行里可见；基线记录不带该字段以免
            # 改变既有输出形状。
            if record.get("is_gazette"):
                record["key"] = key
            _store.upsert(con, Record(key=key, data=record))
        _store.commit(con)
        return {
            "fed": fed,
            "rows": _store.count(con),
            "stages": pipeline_stats,
        }
    finally:
        if own:
            con.close()
