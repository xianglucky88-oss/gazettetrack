"""单一职责：派生稳定主键。

* 基线记录（无公示语义）保持原有行为：优先 ``id``，否则内容哈希；
* 公示记录使用「事项身份 + 内容身份」：案号、主体、事项、首次公示日期与
  正文/附件指纹共同决定键值。同一事项的后续版本因正文/附件差异得到不同键，
  但共享同一个 ``matter_id``，便于版本链与撤销。
* 键值只读规范化后的字段，输入顺序、来源顺序、阶段标记都不参与，因此
  「换序输入」与「重复处理」得到相同键。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def matter_id_of(record: dict[str, Any]) -> str | None:
    """事项身份：不含正文/附件，后续版本共享。"""
    if not record.get("is_gazette"):
        return None
    group_key = str(record.get("group_key") or "")
    if group_key and group_key.split(":", 1)[0] in {"case", "matter", "body", "split"}:
        return group_key
    case_key = record.get("case_no_key")
    if case_key:
        return "case:" + case_key
    entity, matter = record.get("entity_id"), record.get("matter_key")
    if entity and matter:
        return "matter:" + _hash([entity, matter])[:20]
    body_fp = record.get("body_fp")
    if body_fp:
        return "body:" + body_fp
    anchor = record.get("id") or record.get("group_key")
    return "anon:" + _hash(anchor)[:20] if anchor else None


def key_of(record: dict[str, Any]) -> str:
    if record.get("is_gazette"):
        matter_id = matter_id_of(record)
        payload = {
            "matter_id": matter_id,
            "case_no": record.get("case_no_key"),
            "entity": record.get("entity_id"),
            "matter": record.get("matter_key"),
            "date": record.get("date"),
            "body": record.get("body_fp"),
            "attachments": record.get("attachment_fp"),
        }
        group_key = str(record.get("group_key") or "")
        if group_key.startswith("split:"):
            # 撤销后的每条观测必须各自有独立主键，否则两条内容完全相同、
            # 日期也相同的转载会在 records 表里互相覆盖，丢掉来源。
            payload["source"] = group_key
        return "g-" + _hash(payload)[:24]
    rid = record.get("id")
    if rid:
        return str(rid)
    return _hash(record)[:16]
