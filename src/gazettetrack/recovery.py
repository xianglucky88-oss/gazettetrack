"""单一职责：撤销与恢复一次合并决定。

撤销不依赖重新抓取：``merge_history`` 在合并时已追加保存每条观测的完整内容与
来源快照，因此任何一次合并都可以被精确拆回原始观测。恢复则按同一份快照重新
合并，来源集合取并集，历史只追加不删除。

对外契约
--------
* ``undo_merge(con, group_key)``：删除合并 head，把每条观测写回独立记录
  （``group_key = split:<anchor>``、``undo_of_group = <group_key>``），各自
  只保留自己的来源；该 group_key 进入抑制表，重跑流水线不会再合并；
* ``restore_merge(con, group_key)``：删除拆分记录，解除抑制，按快照重新合并
  并恢复 head 与版本链；来源集合是全部观测来源的并集。
"""
from __future__ import annotations

import json
from typing import Any

from . import store as _store
from .attach import process as attach_process
from .dedupe import group_from_records, merge as dedupe_merge
from .keyer import key_of
from .models import Record
from .normalizer import source_descriptor
from .versioning import persist as persist_versions
from .versioning import process as stage_versions

_META_KEYS = (
    "case_no",
    "case_no_key",
    "entity_id",
    "entity_name",
    "entity_key",
    "matter",
    "matter_key",
    "region",
    "src",
    "is_gazette",
)


def _observations_of_history(con, group_key: str, action: str = "merge") -> list[dict[str, Any]]:
    history = _store.merge_history_for(con, group_key)
    entries = [entry for entry in history if entry["action"] == action]
    if not entries:
        raise ValueError(f"no '{action}' history for {group_key}")
    return entries[-1]["observations"]


def _record_from_observation(observation: dict[str, Any]) -> dict[str, Any]:
    """把观测快照还原成一条独立记录（来源只含自己）。"""
    descriptor = observation.get("source") or source_descriptor(observation)
    rec = {key: observation.get(key) for key in _META_KEYS if observation.get(key) is not None}
    rec["is_gazette"] = True
    rec["id"] = observation.get("id") or observation.get("anchor")
    rec["case_no"] = observation.get("case_no") or rec.get("case_no")
    rec["body"] = observation.get("body")
    rec["body_fp"] = observation.get("body_fp")
    rec["attachments"] = observation.get("attachments") or []
    rec["attachment_fp"] = observation.get("attachment_fp")
    rec["date"] = observation.get("date")
    rec["sources"] = [descriptor]
    rec["source_anchors"] = [descriptor.get("anchor")]
    rec["srcs"] = [descriptor["src"]] if descriptor.get("src") else []
    rec["regions"] = [descriptor["region"]] if descriptor.get("region") else []
    rec["revision_count"] = 1
    rec["content_state"] = "single"
    rec["attachment_state"] = "single"
    rec["group_key"] = "split:" + str(descriptor.get("anchor"))
    return rec


def _merged_records(con, group_key: str) -> list[dict[str, Any]]:
    rows = con.execute("SELECT data FROM records").fetchall()
    out = []
    for (data,) in rows:
        candidate = json.loads(data)
        if candidate.get("group_key") == group_key:
            out.append(candidate)
    return out


def _restored_records(
    con, group_key: str, observations: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    """撤销产生的拆分记录：返回 (records.key, data)。

    ``group_key`` 为 ``split:<anchor>`` 或 data 带 ``undo_of_group``；重跑流水线
    可能用同样的 key 覆盖 data，因此以表主键而不是 data 内的字段为准。
    """
    anchors = {str(o.get("anchor")) for o in observations}
    split_keys = {"split:" + anchor for anchor in anchors}
    rows = con.execute("SELECT key,data FROM records").fetchall()
    out = []
    for key, data in rows:
        candidate = json.loads(data)
        if candidate.get("undo_of_group") == group_key or candidate.get("group_key") in split_keys:
            out.append((key, candidate))
    return out


def undo_merge(con, group_key: str) -> dict[str, Any]:
    """拆分错误合并，恢复每条观测各自的来源。"""
    observations = _observations_of_history(con, group_key, "merge")

    head_key, head_version = _store.head_of(con, group_key)
    removed = []
    # matters.head_key 与 records.key 一致；同时兜底删除任何仍带该 group_key 的记录。
    for key in {head_key, *[d.get("key") for d in _merged_records(con, group_key)]}:
        if key:
            _store.delete(con, key)
            removed.append(key)
    _store.clear_head(con, group_key)
    _store.set_versions_status(con, group_key, "undone")

    restored = []
    for observation in observations:
        rec = _record_from_observation(observation)
        rec["undo_of_group"] = group_key
        key = key_of(rec)
        rec["key"] = key
        rec["head_key"] = key
        _store.upsert(con, Record(key=key, data=rec))
        restored.append({"anchor": observation.get("anchor"), "key": key, "record": rec})

    detail = {
        "group_key": group_key,
        "suppressed_merge_head": head_key,
        "head_version": head_version,
        "restored": [item["anchor"] for item in restored],
        "restored_keys": [item["key"] for item in restored],
        "removed": removed,
    }
    _store.suppress_group(con, group_key, reason="undo_merge")
    _store.log_event(
        con,
        group_key,
        "undo_merge",
        from_key=head_key,
        detail=detail,
    )
    _store.record_merge(
        con,
        group_key,
        "undo",
        [item["anchor"] for item in restored],
        observations,
        action="undo",
    )
    _store.commit(con)
    return {"group_key": group_key, "action": "undo", **detail, "records": [i["record"] for i in restored]}


def restore_merge(con, group_key: str) -> dict[str, Any]:
    """反向执行撤销：删除拆分记录、解除抑制、按快照重新合并。"""
    observations = _observations_of_history(con, group_key, "undo")

    removed = []
    for key, _rec in _restored_records(con, group_key, observations):
        _store.delete(con, key)
        removed.append(key)
    _store.delete_versions_for_keys(con, removed)

    # 用观测快照重建每条独立记录，再交给 dedupe 重新合并，以便恢复完整证据链。
    restored_records = [_record_from_observation(observation) for observation in observations]
    base_meta = dict(restored_records[0])
    group = group_from_records(restored_records, group_key=group_key)
    canonical = dedupe_merge(group)
    canonical.pop("id", None)
    base = canonical
    base["group_key"] = group_key
    base["matter_id"] = group_key
    # 保留首条观测的主体/事项元数据，防止快照缺字段时信息回退。
    for field in ("case_no", "case_no_key", "entity_id", "entity_name", "matter", "matter_key"):
        if base.get(field) is None and base_meta.get(field) is not None:
            base[field] = base_meta[field]

    versioned = stage_versions(base)
    head = persist_versions(con, versioned)
    _store.set_versions_status(con, group_key, "active")
    head = attach_process(head)

    key = key_of(head)
    head["key"] = key
    _store.upsert(con, Record(key=key, data=head))
    _store.unsuppress_group(con, group_key)

    detail = {
        "group_key": group_key,
        "restored_merge_key": key,
        "removed_split_records": sorted(set(removed)),
        "sources": head.get("source_anchors"),
    }
    _store.log_event(
        con,
        group_key,
        "restore_merge",
        to_key=key,
        detail=detail,
    )
    _store.record_merge(
        con,
        group_key,
        "restore",
        [observation.get("anchor") for observation in observations],
        observations,
        action="restore",
    )
    _store.commit(con)
    return {"group_key": group_key, "action": "restore", **detail, "record": head}
