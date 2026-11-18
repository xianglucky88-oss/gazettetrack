"""单一职责：版本识别、版本链持久化。

规则
----
* 内容签名 ``content_sig`` = 正文指纹 + 附件指纹，不含来源地区/抓取时间，
  因此跨地区转载得到同一签名；
* 同一签名再次出现 → ``unchanged``（无变化）或 ``repost_of_previous_version``
  （旧版本被再次转载），只补来源，不新建版本；
* 新签名 → 新版本号 = 该事项当前最大版本 + 1，并按上一版本的内容给出
  ``body_revised`` / ``attachment_revised`` / ``body_and_attachment_revised``；
  正文修订绝不会落成 ``unchanged``，附件差异也不会被忽略；
* 每个事项的 head 是最新版本；head 记录的来源集合是所有版本来源的并集，
  因此新版本不会丢失历史来源；
* 撤消/恢复在 ``recovery`` 中基于合并历史完成，本模块只负责版本表本身。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import store as _store
from .keyer import key_of, matter_id_of
from .normalizer import (
    aggregate_sources,
    date_ordinal,
    sort_descriptors,
    source_descriptor,
)


def content_sig(revision: dict[str, Any]) -> str:
    """内容签名：正文指纹 + 附件指纹（不含来源）。"""
    payload = {
        "body": revision.get("body_fp"),
        "attachments": revision.get("attachment_fp"),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]


def _describe_change(previous: dict[str, Any] | None, current: dict[str, Any]) -> str:
    if previous is None:
        return "initial"
    if not previous.get("body_fp") or not current.get("body_fp"):
        # 任一侧正文缺失时无法声称「内容未变」，标为未知而不是 body_revised。
        return "content_unknown"
    body_changed = previous.get("body_fp") != current.get("body_fp")
    attachment_changed = previous.get("attachment_fp") != current.get("attachment_fp")
    if body_changed and attachment_changed:
        return "body_and_attachment_revised"
    if body_changed:
        return "body_revised"
    if attachment_changed:
        return "attachment_revised"
    return "metadata_revised"


def _revision_inputs(record: dict[str, Any]) -> list[dict[str, Any]]:
    """展开待版本化的修订；相同内容签名合并来源（同义转载）。"""
    raw = record.get("revisions")
    if not raw:
        raw = [
            {
                "body": record.get("body"),
                "body_fp": record.get("body_fp"),
                "attachments": record.get("attachments") or [],
                "attachment_fp": record.get("attachment_fp"),
                "date": record.get("date"),
                "sources": record.get("sources") or [source_descriptor(record)],
            }
        ]

    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in raw:
        sig = content_sig(item)
        if sig not in merged:
            merged[sig] = {
                "content_sig": sig,
                "body": item.get("body"),
                "body_fp": item.get("body_fp"),
                "attachments": item.get("attachments") or [],
                "attachment_fp": item.get("attachment_fp"),
                "date": item.get("date"),
                "sources": [],
            }
            order.append(sig)
        entry = merged[sig]
        entry["sources"] = sort_descriptors(entry["sources"] + list(item.get("sources") or []))
        dates = [d for d in (entry.get("date"), item.get("date")) if d]
        entry["date"] = min(dates) if dates else entry.get("date")

    revisions = [merged[sig] for sig in order]
    # 版本顺序 = 公示日期顺序；同日按签名，保证与输入顺序无关。
    revisions.sort(key=lambda rev: (date_ordinal(rev.get("date")) or 0, rev["content_sig"]))
    previous = None
    for number, revision in enumerate(revisions, start=1):
        revision["version"] = number
        revision["source_anchors"] = [s["anchor"] for s in revision["sources"]]
        revision["change_type"] = _describe_change(previous, revision)
        previous = revision
    return revisions


def _content_state(revisions: list[dict[str, Any]], previous: str | None = None) -> str:
    if len(revisions) <= 1:
        # 合并了多条观测但内容签名一致：是「重复转载、无变化」，不是单条记录。
        return "unchanged" if previous == "unchanged" else "single"
    known = [rev for rev in revisions if rev.get("body_fp")]
    if len(known) != len(revisions):
        return "unknown"
    if len({rev["body_fp"] for rev in known}) == 1:
        return "unchanged"
    return "revised"


def process(record: dict[str, Any]) -> dict[str, Any]:
    """纯函数：展开版本、选定 head、汇总来源（不落库）。"""
    if not record.get("is_gazette"):
        # 基线简单记录不进入版本语义，输出保持基线形状。
        return dict(record)

    revisions = _revision_inputs(record)
    if not revisions:
        return dict(record)

    head = revisions[-1]
    rec = dict(record)
    rec.pop("revisions", None)
    rec["body"] = head.get("body")
    rec["body_fp"] = head.get("body_fp")
    rec["attachments"] = head.get("attachments") or []
    rec["attachment_fp"] = head.get("attachment_fp")
    # date 保持「首次公示日」作为身份的一部分；最新修订日单独放 latest_date。
    rec["latest_date"] = head.get("date")
    rec["date"] = record.get("date") or revisions[0].get("date")
    rec["revisions"] = revisions
    rec["revision_count"] = len(revisions)
    rec["version"] = len(revisions)
    rec["matter_id"] = record.get("matter_id") or matter_id_of(record)

    union_sources = sort_descriptors(
        [source for revision in revisions for source in revision.get("sources") or []]
    )
    aggregate = aggregate_sources(union_sources)
    rec["sources"] = aggregate["sources"]
    rec["source_anchors"] = aggregate["source_anchors"]
    rec["srcs"] = aggregate["srcs"]
    rec["regions"] = aggregate["regions"]
    rec["body_variants"] = [
        {"body_fp": rev.get("body_fp"), "version": rev["version"], "date": rev.get("date")}
        for rev in revisions
        if rev.get("body_fp")
    ]
    rec["content_state"] = _content_state(revisions, record.get("content_state"))
    rec["attachment_variants"] = sorted(
        {rev.get("attachment_fp") for rev in revisions if rev.get("attachment_fp")}
    )
    rec["change_summary"] = [
        {"version": rev["version"], "change_type": rev["change_type"]} for rev in revisions
    ]
    rec["_stage_versioning"] = "ok"
    return rec


def _revision_record(record: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
    """单个版本的落库形状（版本表中的 data 列）。"""
    rev = {
        key: record.get(key)
        for key in (
            "case_no",
            "case_no_key",
            "entity_id",
            "entity_name",
            "entity_key",
            "matter",
            "matter_key",
            "region",
            "src",
            "group_key",
            "is_gazette",
        )
        if record.get(key) is not None
    }
    rev.update(
        {
            "is_gazette": True,
            "matter_id": record.get("matter_id"),
            "version": revision["version"],
            "content_sig": revision["content_sig"],
            "body": revision.get("body"),
            "body_fp": revision.get("body_fp"),
            "attachments": revision.get("attachments") or [],
            "attachment_fp": revision.get("attachment_fp"),
            "date": revision.get("date"),
            "sources": revision.get("sources") or [],
            "source_anchors": revision.get("source_anchors") or [],
        }
    )
    return rev


def persist(con, record: dict[str, Any]) -> dict[str, Any]:
    """写入版本链并把 head 指向最新版本；返回可直接 upsert 的 head 记录。

    版本号按公示日期排序（同日按内容签名），因此「先抓到新版再补到旧版」
    与「按时间顺序抓取」得到完全相同的版本链；重复处理同一内容只补来源。
    """
    rec = dict(record)
    matter_id = rec.get("matter_id") or matter_id_of(rec)
    if not matter_id:
        return rec
    if str(matter_id).startswith(("split:", "plain:")):
        # 未合并的独立观测不进入事项版本链，避免污染版本历史。
        return rec
    rec["matter_id"] = matter_id
    revisions = rec.get("revisions") or _revision_inputs(rec)

    existing = {version["content_sig"]: version for version in _store.versions_for(con, matter_id)}
    old_head_key, _ = _store.head_of(con, matter_id)
    new_sigs: set[str] = set()

    for revision in revisions:
        sig = revision["content_sig"]
        rev_record = _revision_record(rec, revision)
        current = existing.get(sig)
        if current is not None:
            # 内容已存在：只补来源，不新建版本。
            merged_sources = sort_descriptors(
                (current["data"].get("sources") or []) + (revision.get("sources") or [])
            )
            data = dict(current["data"])
            data["sources"] = merged_sources
            data["source_anchors"] = [s["anchor"] for s in merged_sources]
            current["data"] = data
            continue
        new_sigs.add(sig)
        existing[sig] = {
            "version": 0,
            "key": key_of(rev_record),
            "content_sig": sig,
            "change_type": "initial",
            "data": dict(rev_record, sources=sort_descriptors(revision.get("sources") or [])),
            "status": "active",
        }

    if not existing:
        return rec

    ordered = sorted(
        existing.values(),
        key=lambda version: (
            date_ordinal(version["data"].get("date")) or 0,
            version["content_sig"],
        ),
    )
    previous_data = None
    for number, version in enumerate(ordered, start=1):
        version["version"] = number
        change_type = _describe_change(previous_data, version["data"])
        version["change_type"] = change_type
        version["data"]["version"] = number
        version["data"]["change_type"] = change_type
        previous_data = version["data"]

    # 版本链按日期重排：先清空该事项的版本行再按新顺序写回，避免版本号漂移。
    con.execute("DELETE FROM versions WHERE matter_id=?", (matter_id,))
    for version in ordered:
        _store.put_version(
            con,
            matter_id,
            version["version"],
            version["key"],
            version["content_sig"],
            version["change_type"],
            version["data"],
            version["status"],
        )
    for version in ordered:
        if version["content_sig"] in new_sigs:
            previous_key = ordered[version["version"] - 2]["key"] if version["version"] > 1 else None
            _store.log_event(
                con,
                matter_id,
                "new_version",
                from_key=previous_key,
                to_key=version["key"],
                detail={
                    "version": version["version"],
                    "change_type": version["change_type"],
                    "content_sig": version["content_sig"],
                },
            )

    all_versions = _store.versions_for(con, matter_id)
    head_version = all_versions[-1]
    head_data = head_version["data"]

    union_sources: list[dict[str, Any]] = []
    for version in all_versions:
        union_sources.extend(version["data"].get("sources") or [])
    aggregate = aggregate_sources(union_sources)

    head = dict(rec)
    head.update(
        {
            "matter_id": matter_id,
            "is_gazette": True,
            "version": head_version["version"],
            "content_sig": head_version["content_sig"],
            "body": head_data.get("body"),
            "body_fp": head_data.get("body_fp"),
            # 附件与 attach 阶段保持一致：head 记录用「全部来源的附件并集」，
            # 否则版本落库键会与最终落库键不一致（附件差异事项会踩到）。
            "attachments": aggregate["attachments"],
            "attachment_fp": aggregate["attachment_fp"],
            "attachment_variants": sorted(
                {
                    version["data"].get("attachment_fp")
                    for version in all_versions
                    if version["data"].get("attachment_fp")
                }
            ),
            "latest_date": head_data.get("date"),
            "date": aggregate["date"],
            "sources": aggregate["sources"],
            "source_anchors": aggregate["source_anchors"],
            "srcs": aggregate["srcs"],
            "regions": aggregate["regions"],
            "revisions": [
                {
                    "version": version["version"],
                    "key": version["key"],
                    "content_sig": version["content_sig"],
                    "change_type": version["change_type"],
                    "date": version["data"].get("date"),
                    "body_fp": version["data"].get("body_fp"),
                    "attachment_fp": version["data"].get("attachment_fp"),
                    "sources": [s["anchor"] for s in version["data"].get("sources") or []],
                }
                for version in all_versions
            ],
            "revision_count": len(all_versions),
            "change_summary": [
                {"version": version["version"], "change_type": version["change_type"]}
                for version in all_versions
            ],
            "content_state": _content_state(
                [{"body_fp": version["data"].get("body_fp")} for version in all_versions],
                rec.get("content_state"),
            ),
        }
    )
    head["attachment_state"] = "single" if len(head["attachment_variants"]) <= 1 else "changed"

    # records 表与 matters.head_key 用同一个键（key_of(head)），
    # 版本表里每条版本另有自己的 key，二者不要混用。
    head_key = key_of(head)
    head["head_key"] = head_key
    head["_stage_versioning"] = "ok"

    previous_row = _store.get(con, old_head_key) if old_head_key else None
    if old_head_key and old_head_key != head_key:
        _store.delete(con, old_head_key)
    _store.set_head(con, matter_id, head_key, head_version["version"])
    # 幂等重跑不追加审计事件：只有 head 键、版本或来源集合真的变化才记录。
    if (
        previous_row is None
        or old_head_key != head_key
        or previous_row.get("version") != head_version["version"]
        or previous_row.get("source_anchors") != head["source_anchors"]
    ):
        _store.log_event(
            con,
            matter_id,
            "head_updated",
            from_key=old_head_key,
            to_key=head_key,
            detail={"version": head_version["version"], "sources": head["source_anchors"]},
        )
    return head


def observations_of(record: dict[str, Any]) -> list[dict[str, Any]]:
    """合并前的观测快照：每条来源自己的内容与来源，供撤销使用。"""
    observations = []
    for revision in record.get("revisions") or []:
        for source in revision.get("sources") or []:
            observation = {
                key: record.get(key)
                for key in (
                    "case_no",
                    "case_no_key",
                    "entity_id",
                    "entity_name",
                    "entity_key",
                    "matter",
                    "matter_key",
                )
                if record.get(key) is not None
            }
            observation.update(
                {
                    "anchor": source.get("anchor"),
                    "id": source.get("id") or source.get("anchor"),
                    "src": source.get("src"),
                    "region": source.get("region"),
                    "date": revision.get("date"),
                    "body": revision.get("body"),
                    "body_fp": revision.get("body_fp"),
                    "attachments": revision.get("attachments") or [],
                    "attachment_fp": revision.get("attachment_fp"),
                    "source": source,
                }
            )
            observations.append(observation)
    return observations


last_stats: dict[str, Any] = {}


def process_batch(records: list[dict[str, Any]], con=None) -> list[dict[str, Any]]:
    """批处理入口：展开版本 → 持久化版本链 → 返回各事项 head。"""
    global last_stats
    heads = []
    stats = {"matters": 0, "versions": 0, "unchanged": 0, "revised": 0}
    for record in records:
        staged = process(record)
        head = persist(con, staged) if con is not None else staged
        heads.append(head)
        if head.get("is_gazette"):
            stats["matters"] += 1
            stats["versions"] += int(head.get("revision_count") or 0)
            if head.get("content_state") == "unchanged":
                stats["unchanged"] += 1
            elif head.get("content_state") == "revised":
                stats["revised"] += 1
    last_stats = stats
    return heads
