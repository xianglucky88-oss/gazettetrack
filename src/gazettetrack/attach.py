"""单一职责：把证据附着到规范记录上，供键值与落库消费。

* 基线记录（无公示语义）保持原有阶段标记，输出与基线一致；
* 公示记录补一份 ``evidence_digest``：由事项身份、来源锚点集合、正文指纹、
  附件指纹和合并判定共同派生，键值不再需要重新跑一遍判定逻辑；
* 校验不变式（来源不丢、首次日期不后移、附件并集不缩水），失败时在
  ``attach_warnings`` 中记录，而不是静默吞掉。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .keyer import key_of
from .normalizer import (
    aggregate_sources,
    date_ordinal,
    fingerprint_attachments,
    fingerprint_body,
    sort_descriptors,
    source_descriptor,
)


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]


def process(record: dict[str, Any]) -> dict[str, Any]:
    rec = dict(record)
    if not rec.get("is_gazette"):
        rec.setdefault("_stage_attach", "ok")
        return rec

    sources = sort_descriptors(
        rec.get("sources")
        or [source_descriptor(rec)]
    )
    aggregate = aggregate_sources(sources)
    rec["sources"] = aggregate["sources"]
    rec["source_anchors"] = aggregate["source_anchors"]
    rec["srcs"] = aggregate["srcs"]
    rec["regions"] = aggregate["regions"]
    rec["attachments"] = aggregate["attachments"]
    rec["attachment_fp"] = aggregate["attachment_fp"]
    rec["attachment_variants"] = aggregate["attachment_variants"]

    warnings: list[str] = []
    if rec.get("date") and aggregate["date"] and rec["date"] != aggregate["date"]:
        warnings.append("first_date_not_earliest_source_date")
    if rec.get("latest_date") and aggregate["date"]:
        head_date, first_date = date_ordinal(rec["latest_date"]), date_ordinal(aggregate["date"])
        if head_date is not None and first_date is not None and head_date < first_date:
            warnings.append("latest_date_before_first_date")
    expected_fp = fingerprint_attachments(aggregate["attachments"])
    if expected_fp != aggregate["attachment_fp"]:
        warnings.append("attachment_fingerprint_mismatch")
    if rec.get("body_fp") and rec.get("body") is not None:
        if fingerprint_body(rec["body"]) != rec["body_fp"]:
            warnings.append("body_fingerprint_mismatch")
    if not rec.get("source_anchors"):
        warnings.append("missing_source_anchors")

    rec["evidence_digest"] = _digest(
        {
            "matter_id": rec.get("matter_id") or rec.get("group_key"),
            "case_no": rec.get("case_no_key"),
            "entity": rec.get("entity_id"),
            "matter": rec.get("matter_key"),
            "date": rec.get("date"),
            "body": rec.get("body_fp"),
            "attachments": rec.get("attachment_fp"),
            "source_anchors": rec.get("source_anchors"),
            "content_state": rec.get("content_state"),
            "attachment_state": rec.get("attachment_state"),
            "evidence": (rec.get("evidence") or {}).get("pairs"),
        }
    )
    rec["attach"] = {
        "source_count": len(rec["source_anchors"]),
        "region_count": len(rec["regions"]),
        "revision_count": rec.get("revision_count", 1),
        "content_state": rec.get("content_state", "single"),
        "attachment_state": rec.get("attachment_state", "single"),
        "evidence_digest": rec["evidence_digest"],
    }
    # 落库键在 attach 之后才最终确定（证据已全部附着），这里给出待写入键。
    rec["key"] = key_of(rec)
    if warnings:
        rec["attach_warnings"] = warnings
    rec["_stage_attach"] = "ok"
    return rec
