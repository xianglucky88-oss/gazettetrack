"""单一职责：输出可解释的合并/版本/来源报告。

供 CLI（``make run``）和测试断言使用；报告只读 store 与阶段统计，不改数据。
"""
from __future__ import annotations

from typing import Any

from . import store as _store


def explain(con, key: str) -> dict[str, Any]:
    record = _store.get(con, key)
    if record is None:
        return {"key": key, "found": False}
    return {
        "key": key,
        "found": True,
        "group_key": record.get("group_key"),
        "matter_id": record.get("matter_id"),
        "case_no": record.get("case_no"),
        "entity": record.get("entity_name"),
        "matter": record.get("matter"),
        "first_date": record.get("date"),
        "latest_date": record.get("latest_date"),
        "source_anchors": record.get("source_anchors"),
        "regions": record.get("regions"),
        "content_state": record.get("content_state"),
        "attachment_state": record.get("attachment_state"),
        "revision_count": record.get("revision_count"),
        "change_summary": record.get("change_summary"),
        "attachment_variants": record.get("attachment_variants"),
        "missing_attachments": (record.get("evidence") or {}).get("missing_attachments"),
        "pairs": [
            {
                "left": pair.get("left"),
                "right": pair.get("right"),
                "change": pair.get("change"),
                "reasons": pair.get("reasons"),
            }
            for pair in (record.get("evidence") or {}).get("pairs") or []
        ],
    }


def summary(con) -> dict[str, Any]:
    records = _store.all_records(con)
    gazette = [(k, d) for k, d in records if d.get("is_gazette")]
    return {
        "records": len(records),
        "gazettes": len(gazette),
        "suppressed_groups": _store.suppressed_groups(con),
        "groups": sorted(
            {
                d.get("group_key")
                for _, d in gazette
                if d.get("group_key")
            }
        ),
    }
