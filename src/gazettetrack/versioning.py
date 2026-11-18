"""单一职责：为公示公告维护可重建的版本历史。

模块把每次抓取到的公告观测（observation）归并进一条按来源证据排序的版本链，
并识别以下生命周期事件：

* ``first_publish``       首次发布
* ``update``              正文/标题内容更新
* ``attachment_replace``  正文未变、附件发生变化（附件替换）
* ``retract``             撤回 / 撤销 / 作废
* ``republish``           撤回后的重新发布

设计要点
--------
1. **观测与版本分离**：``version_observations`` 保存去重后的原始观测，
   ``version_history`` 完全由观测重新推导。乱序到达、先收到撤回再补到原文、
   部分抓取恢复都不需要“就地打补丁”，重放时会自然得到同一结果。
2. **排序必须有来源依据**：优先使用来源时间，其次使用来源提供的序号/修订号，
   再次使用事件语义（发布 → 撤回 → 重新发布），然后使用抓取时间，
   最后才用内容指纹做确定性兜底。每个版本都记录 ``order_evidence``，
   说明这次先后到底由哪个来源字段决定。
3. **版本键稳定且唯一**：``version_id`` 派生自公告身份、生命周期、撤回周期
   和内容指纹。重复抓取同一观测不会新增版本；撤回后重新发布即使正文完全相同，
   也因为撤回周期不同而得到新的版本键，不会覆盖原来的发布版本。
4. **撤回事实不可覆盖**：版本链保留 ``retract`` 版本，记录上的
   ``ever_retracted`` / ``retraction_count`` 由整条链推导，重新发布不会抹掉。
5. **从历史重建**：``current_from_history`` / ``replay`` / ``materialize_current``
   既能从已落库的观测重建版本链，也能从版本历史重建当前有效版本。
   连续运行与恢复重放的 ``effective_state`` 完全一致。

对外常用入口
------------
* ``process(record)``             流水线阶段：写入观测并返回带版本信息的记录
* ``normalize(record)`` / ``observe(record)``  纯函数：规范化一条观测
* ``build_chain(observations)``   纯函数：为单个公告推导版本链
* ``build_history(observations)`` 纯函数：按公告分组推导版本历史
* ``ingest`` / ``ingest_many``    写入观测并刷新对应版本链
* ``rebuild`` / ``replay``        从观测重建版本历史（恢复重放）
* ``history`` / ``current_from_history`` / ``effective_state``
* ``materialize_current``         从版本历史写回现有 ``records`` 存储
* ``finish(con)``                 流水线收尾钩子：物化当前有效版本
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
# 生命周期与状态常量
# ---------------------------------------------------------------------------

FIRST_PUBLISH = "first_publish"
UPDATE = "update"
ATTACHMENT_REPLACE = "attachment_replace"
RETRACT = "retract"
REPUBLISH = "republish"

VERSION_EVENTS = (FIRST_PUBLISH, UPDATE, ATTACHMENT_REPLACE, RETRACT, REPUBLISH)
PUBLISH_EVENTS = (FIRST_PUBLISH, UPDATE, ATTACHMENT_REPLACE, REPUBLISH)

STATUS_ACTIVE = "active"
STATUS_RETRACTED = "retracted"

# 观测意图比最终事件更原始：publish 会根据历史推导成 first/update/republish。
INTENT_PUBLISH = "publish"
INTENT_REPUBLISH = "republish"
INTENT_RETRACT = "retract"
INTENT_ATTACHMENT_REPLACE = "attachment_replace"
INTENT_UPDATE = "update"

_INTENT_RANK = {
    INTENT_PUBLISH: 0,
    INTENT_UPDATE: 0,
    INTENT_ATTACHMENT_REPLACE: 0,
    INTENT_RETRACT: 1,
    INTENT_REPUBLISH: 2,
}

_ID_FIELDS = ("gazette_id", "notice_id", "publication_id", "announcement_id",
              "bulletin_id", "record_id", "id")
_SOURCE_FIELDS = ("src", "source", "site", "channel", "publisher")
_TITLE_FIELDS = ("title", "name", "subject", "headline", "notice_title")
_BODY_FIELDS = ("body", "content", "text", "summary", "description",
                "abstract", "notice_body", "正文")
_ATTACHMENT_FIELDS = ("attachments", "files", "annexes", "enclosures",
                      "attachment", "attachment_url", "file_url", "附件")
_SOURCE_TIME_FIELDS = ("published_at", "publish_time", "publication_time",
                       "publication_date", "notice_date", "announced_at",
                       "release_time", "source_time", "date", "dt", "ts")
_OBSERVED_FIELDS = ("observed_at", "fetched_at", "crawled_at", "captured_at",
                    "ingest_time")
_SEQ_FIELDS = ("source_seq", "sequence", "seq", "revision", "revision_no",
               "revision_id", "revision_index", "version_no", "notice_no",
               "issue_no", "order", "source_order")
_BATCH_FIELDS = ("parse_batch", "parse_batch_id", "batch_id", "crawl_batch",
                 "_parse_batch")
_EVENT_FIELDS = ("event", "action", "kind", "op", "announcement_type",
                 "release_type", "notice_type", "type")
_STATUS_FIELDS = ("status", "state", "biz_status", "notice_status",
                  "publication_status", "有效性")

_RETRACT_TOKENS = (
    "retract", "retracted", "withdraw", "withdrawn", "revoke", "revoked",
    "cancel", "cancelled", "canceled", "annul", "annulled", "void",
    "invalid", "supersede", "superseded", "作废", "撤回", "撤销", "撤消",
    "终止", "无效", "失效", "下架", "删除", "取消",
)
_REPUBLISH_TOKENS = (
    "republish", "re-publish", "re_publish", "republished", "reissue",
    "re-issue", "reissued", "重新发布", "恢复发布", "再次发布", "重新公示",
    "恢复公示", "重新公告",
)
_ATTACH_REPLACE_TOKENS = (
    "attachment_replace", "replace_attachment", "attachment_change",
    "attachment_update", "attachment_replacement", "附件替换", "附件变更",
    "附件更新", "更换附件",
)
_UPDATE_TOKENS = (
    "update", "updated", "amend", "amended", "revise", "revised", "change",
    "changed", "modify", "modified", "correction", "corrected", "更正",
    "变更", "修改", "更新", "修订", "补充",
)

_CONTENT_FIELDS = ("title", "body", "value", "amount", "price", "location",
                   "court", "subject", "details", "result", "case_no",
                   "标的", "金额", "法院", "案号")

_UTC = timezone.utc


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _first(mapping: Mapping[str, Any], names: Iterable[str], default=None):
    """按字段名列表取值，兼容大小写和常见前后缀（如 ``_``）。"""
    if not isinstance(mapping, Mapping):
        return default
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
        lname = name.lower()
        if lname in lowered and lowered[lname] not in (None, ""):
            return lowered[lname]
        for key, value in lowered.items():
            if key.lstrip("_") == lname and value not in (None, ""):
                return value
    return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _has_any(mapping: Mapping[str, Any], names: Iterable[str]) -> bool:
    """字段是否出现在记录中（用于区分“未抓到”与“显式为空”）。"""
    if not isinstance(mapping, Mapping):
        return False
    keys = {str(k).lower() for k in mapping}
    return any(name.lower() in keys or name.lower() in
               {key.lstrip("_") for key in keys} for name in names)


def _parse_time(value: Any):
    """返回 ``(iso_utc, epoch)``；无法识别时返回 ``(None, None)``。"""
    if value is None or value == "":
        return None, None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
        dt = dt.astimezone(_UTC)
        return dt.isoformat(timespec="seconds"), dt.timestamp()
    if isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=_UTC)
        return dt.isoformat(timespec="seconds"), dt.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e12:          # 毫秒时间戳
            seconds /= 1000.0
        try:
            dt = datetime.fromtimestamp(seconds, tz=_UTC)
        except (OverflowError, OSError, ValueError):
            return None, None
        return dt.isoformat(timespec="seconds"), dt.timestamp()

    raw = str(value).strip()
    if not raw:
        return None, None
    if re.fullmatch(r"\d{8}", raw):
        try:
            dt = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=_UTC)
            return dt.isoformat(timespec="seconds"), dt.timestamp()
        except ValueError:
            pass
    if re.fullmatch(r"\d+(\.\d+)?", raw):
        return _parse_time(float(raw))

    candidate = raw.replace("Z", "+00:00").replace("z", "+00:00")
    candidate = re.sub(r"\s+", " ", candidate)
    for parser in (
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S"),
        lambda s: datetime.strptime(s, "%Y/%m/%d %H:%M:%S"),
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M"),
        lambda s: datetime.strptime(s, "%Y-%m-%d"),
        lambda s: datetime.strptime(s.replace("/", "-"), "%Y-%m-%d"),
    ):
        try:
            dt = parser(candidate)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
        dt = dt.astimezone(_UTC)
        return dt.isoformat(timespec="seconds"), dt.timestamp()
    return None, None


def _seq_sort(value):
    """把来源序号/修订号规整成可比较的元组；缺失值排在最后。"""
    if value is None or value == "":
        return (1, 0, "")
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        return (0, 0, f"{float(value):030.10f}")
    raw = str(value).strip()
    match = re.search(r"-?\d+(?:\.\d+)?", raw)
    if match:
        return (0, 0, f"{float(match.group()):030.10f}")
    return (0, 1, raw)


def _seq_json(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    return str(value)


def _has_token(text: str, tokens: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(tok in lowered for tok in tokens)


def _parse_flag(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "partial",
                                          "incomplete", "truncated", "是"}


# ---------------------------------------------------------------------------
# 观测规范化
# ---------------------------------------------------------------------------

def _content_view(record: Mapping[str, Any]) -> dict:
    view = {}
    for name in _CONTENT_FIELDS:
        value = _first(record, (name,))
        if value not in (None, ""):
            view[name] = value
    return view


def _normalize_attachments(value) -> list[dict]:
    if value in (None, "", []):
        return []
    if isinstance(value, Mapping):
        value = [value]
    elif isinstance(value, str):
        parts = [p.strip() for p in re.split(r"[,;，；\n]", value) if p.strip()]
        value = parts or [value]
    elif not isinstance(value, (list, tuple, set)):
        value = [value]

    normalized = []
    for item in value:
        if isinstance(item, Mapping):
            name = _text(_first(item, ("name", "filename", "title", "file_name")))
            ref = _text(_first(item, ("url", "href", "uri", "path", "ref",
                                      "download_url", "id")))
            version = _text(_first(item, ("version", "revision", "etag",
                                          "checksum", "sha256", "version_id")))
            digest = _text(_first(item, ("fingerprint", "digest", "hash")))
            body = {"name": name, "ref": ref, "version": version,
                    "digest": digest}
            body = {k: v for k, v in body.items() if v}
            if not body:
                body = {"raw": _canonical(item)}
            body["fingerprint"] = _fingerprint(body)
            normalized.append(body)
        else:
            text = _text(item)
            if text:
                normalized.append({"ref": text,
                                   "fingerprint": _fingerprint({"ref": text})})
    return sorted(normalized, key=_canonical)


def _detect_intent(record: Mapping[str, Any]) -> str:
    event = _text(_first(record, _EVENT_FIELDS))
    status = _text(_first(record, _STATUS_FIELDS))
    for text in (event, status):
        if not text:
            continue
        low = text.lower()
        if _has_token(low, _REPUBLISH_TOKENS):
            return INTENT_REPUBLISH
        if _has_token(low, _RETRACT_TOKENS):
            return INTENT_RETRACT
        if _has_token(low, _ATTACH_REPLACE_TOKENS):
            return INTENT_ATTACHMENT_REPLACE
        if _has_token(low, _UPDATE_TOKENS):
            return INTENT_UPDATE
    return INTENT_PUBLISH


def normalize(record: Mapping[str, Any]) -> dict:
    """把一条原始记录规范化为可持久化、可重放的观测。"""
    if not isinstance(record, Mapping):
        raise TypeError("record must be a mapping")

    notice_id = _text(_first(record, _ID_FIELDS))
    source = _text(_first(record, _SOURCE_FIELDS)).lower()
    title = _text(_first(record, _TITLE_FIELDS))
    body = _text(_first(record, _BODY_FIELDS))
    content = _content_view(record)
    if title and "title" not in content:
        content["title"] = title
    if body and "body" not in content:
        content["body"] = body

    if not notice_id:
        notice_id = "auto-" + _fingerprint({"source": source,
                                            "title": title,
                                            "content": content})[:16]

    intent = _detect_intent(record)
    source_time_raw = _first(record, _SOURCE_TIME_FIELDS)
    source_time, source_epoch = _parse_time(source_time_raw)
    observed_raw = _first(record, _OBSERVED_FIELDS)
    observed_at, _ = _parse_time(observed_raw)
    source_seq = _seq_json(_first(record, _SEQ_FIELDS))
    parse_batch = _text(_first(record, _BATCH_FIELDS))

    attachments = _normalize_attachments(_first(record, _ATTACHMENT_FIELDS))
    body_fp = _fingerprint(content) if content else None
    attachment_fp = _fingerprint(attachments) if attachments else None
    explicit_partial = bool(_parse_flag(
        _first(record, ("partial", "incomplete", "truncated", "is_partial",
                        "partial_fetch", "capture_incomplete"))))
    if explicit_partial:
        partial = True
    elif intent == INTENT_RETRACT:
        partial = False
    elif intent == INTENT_ATTACHMENT_REPLACE:
        # 附件替换只要求附件存在；没有附件才是抓取不完整。
        partial = not attachments
    else:
        # 没有正文内容（也没有附件）无法判断内容是否变化，按部分抓取处理。
        partial = not content and not attachments

    observation_id = "obs-" + _fingerprint([
        notice_id, source, intent, source_time, source_seq,
        body_fp, attachment_fp, bool(partial), parse_batch,
    ])[:24]

    return {
        "observation_id": observation_id,
        "notice_id": notice_id,
        "source": source,
        "intent": intent,
        "title": title,
        "body": body,
        "content": content,
        "content_present": bool(content),
        "body_fingerprint": body_fp,
        "attachments": attachments,
        "attachments_present": _has_any(record, _ATTACHMENT_FIELDS),
        "attachment_fingerprint": attachment_fp,
        "attachment_versions": [a["fingerprint"] for a in attachments],
        "partial": bool(partial),
        "source_time": source_time,
        "source_time_raw": _text(source_time_raw) or None,
        "source_epoch": source_epoch,
        "source_seq": source_seq,
        "observed_at": observed_at,
        "parse_batch": parse_batch,
        "payload": dict(record),
    }


# 兼容别名：语义上就是“记录一条观测”
observe = normalize


# ---------------------------------------------------------------------------
# 排序与生命周期推导
# ---------------------------------------------------------------------------

def _sort_key(observation: Mapping[str, Any]):
    has_time = 0 if observation.get("source_time") else 1
    return (
        has_time,
        observation.get("source_time") or "",
        _seq_sort(observation.get("source_seq")),
        _INTENT_RANK.get(observation.get("intent"), 0),
        observation.get("observed_at") or "",
        observation.get("body_fingerprint") or "",
        observation.get("observation_id") or "",
    )


def _sort_components(observation: Mapping[str, Any]) -> dict:
    """记录排序依据，便于审计“同日先后靠什么决定”。"""
    return {
        "source_time": observation.get("source_time"),
        "source_seq": observation.get("source_seq"),
        "event_rank": _INTENT_RANK.get(observation.get("intent"), 0),
        "intent": observation.get("intent"),
        "observed_at": observation.get("observed_at"),
        "body_fingerprint": observation.get("body_fingerprint"),
        "observation_id": observation.get("observation_id"),
    }


_COMPONENT_ORDER = ("source_time", "source_seq", "event_rank", "observed_at",
                    "body_fingerprint", "observation_id")


def _order_evidence(previous, current) -> dict:
    if previous is None:
        return {"primary": "chain_start", "compared_with_previous": False,
                "components": _sort_components(current)}
    prev = _sort_components(previous)
    cur = _sort_components(current)
    primary = "identical"
    for name in _COMPONENT_ORDER:
        if name == "source_seq":
            if _seq_sort(prev["source_seq"]) != _seq_sort(cur["source_seq"]):
                primary = "source_seq"
                break
        elif prev[name] != cur[name]:
            primary = name
            break
    return {"primary": primary, "compared_with_previous": True,
            "components": cur}


def _merge(version: dict, observation: Mapping[str, Any]) -> None:
    """把一条属于同一版本的观测并入版本（保留批次与指纹证据）。"""
    batches = version.setdefault("parse_batches", [])
    batch = observation.get("parse_batch") or ""
    if batch and batch not in batches:
        batches.append(batch)
        batches.sort()
    ids = version.setdefault("observation_ids", [])
    if observation.get("observation_id") not in ids:
        ids.append(observation["observation_id"])
    if observation.get("partial"):
        version["partial_fetches"] = int(version.get("partial_fetches", 0)) + 1
    # 部分抓取先到、随后补到完整正文：原地升级首个版本，避免伪造 update。
    # 若残缺观测没有来源时间，允许完整观测补全（同一 notice/source 快照）。
    same_identity = (
        version.get("source_time") == observation.get("source_time")
        and _seq_sort(version.get("source_seq"))
        == _seq_sort(observation.get("source_seq")))
    fills_missing_time = (
        version.get("source_time") is None
        and _seq_sort(version.get("source_seq"))
        == _seq_sort(observation.get("source_seq")))
    if (version.get("partial") and not observation.get("partial")
            and (same_identity or fills_missing_time)):
        version["body_fingerprint"] = observation.get("body_fingerprint")
        version["attachment_fingerprint"] = observation.get("attachment_fingerprint")
        version["attachment_versions"] = list(observation.get("attachment_versions") or [])
        version["title"] = observation.get("title") or version.get("title")
        version["content"] = dict(observation.get("content") or {})
        version["attachments"] = list(observation.get("attachments") or [])
        version["partial"] = False
        version["payload"] = dict(observation.get("payload") or {})
        if observation.get("source_time") and not version.get("source_time"):
            version["source_time"] = observation.get("source_time")
            version["source_time_raw"] = observation.get("source_time_raw")
    version["observations"] = len(ids)
    version["parse_batches"] = sorted(set(version.get("parse_batches") or []))


def _new_version(observation: Mapping[str, Any], event: str, era: int,
                 previous: dict | None, seq: int,
                 content_source: Mapping[str, Any] | None = None) -> dict:
    source = content_source or observation
    version = {
        "version_id": None,   # 链定稿后统一派生，保证重放/升级后仍稳定唯一
        "notice_id": observation["notice_id"],
        "ordinal": seq,
        "event": event,
        "era": era,
        "source": observation.get("source"),
        "source_time": observation.get("source_time"),
        "source_time_raw": observation.get("source_time_raw"),
        "source_seq": observation.get("source_seq"),
        "observed_at": observation.get("observed_at"),
        "parse_batches": sorted({observation.get("parse_batch") or ""} - {""}),
        "body_fingerprint": source.get("body_fingerprint"),
        "attachment_fingerprint": source.get("attachment_fingerprint"),
        "attachment_versions": list(source.get("attachment_versions") or []),
        "title": source.get("title") or "",
        "content": dict(source.get("content") or {}),
        "attachments": list(source.get("attachments") or []),
        "partial": bool(source.get("partial")),
        "order_evidence": _order_evidence(previous, observation),
        "observation_ids": [observation["observation_id"]],
        "observations": 1,
        "payload": dict(source.get("payload") or {}),
    }
    return version


def effective_state(chain: list[Mapping[str, Any]], notice_id: str | None = None) -> dict:
    """从版本链推导当前有效版本（可在没有观测、只有历史时使用）。"""
    chain = sorted(chain, key=lambda v: int(v.get("ordinal", 0)))
    if notice_id is None:
        notice_id = chain[-1]["notice_id"] if chain else None

    status = STATUS_ACTIVE
    effective = None
    last_pub = None
    retracted_at = None
    ever_retracted = False
    retraction_count = 0

    for version in chain:
        event = version["event"]
        if event == RETRACT:
            status = STATUS_RETRACTED
            effective = None
            retracted_at = version.get("source_time")
            ever_retracted = True
            retraction_count += 1
        elif event in PUBLISH_EVENTS:
            status = STATUS_ACTIVE
            effective = version
            last_pub = version

    return {
        "notice_id": notice_id,
        "status": status,
        "effective_version_id": effective["version_id"] if effective else None,
        "effective_event": effective["event"] if effective else None,
        "effective_ordinal": effective.get("ordinal") if effective else None,
        "last_published_version_id": last_pub["version_id"] if last_pub else None,
        "body_fingerprint": effective.get("body_fingerprint") if effective else None,
        "attachment_fingerprint": (effective.get("attachment_fingerprint")
                                   if effective else None),
        "attachment_versions": list(effective.get("attachment_versions") or [])
        if effective else [],
        "source_time": effective.get("source_time") if effective else None,
        "retracted_at": retracted_at,
        "ever_retracted": ever_retracted,
        "retraction_count": retraction_count,
        "version_count": len(chain),
        "chain_tail": chain[-1]["version_id"] if chain else None,
    }


def build_chain(observations: Iterable[Mapping[str, Any]],
                notice_id: str | None = None) -> list[dict]:
    """按来源证据为单个公告推导完整版本链（纯函数）。"""
    prepared = []
    for item in observations:
        if "observation_id" in item and "intent" in item:
            prepared.append(dict(item))
        else:
            prepared.append(normalize(item))
    if notice_id is not None:
        prepared = [o for o in prepared if o.get("notice_id") == notice_id]
    prepared.sort(key=_sort_key)

    chain: list[dict] = []
    era = 0
    status = STATUS_ACTIVE
    last_publish: dict | None = None
    prior_obs = None

    def _same_source_identity(observation, version):
        return (observation.get("source_time") == version.get("source_time")
                and _seq_sort(observation.get("source_seq"))
                == _seq_sort(version.get("source_seq")))

    for observation in prepared:
        intent = observation.get("intent", INTENT_PUBLISH)

        if intent == INTENT_RETRACT:
            if status == STATUS_RETRACTED and chain and chain[-1]["event"] == RETRACT:
                # 同一次撤回被重复抓到：并入原撤回版本，不新增版本键。
                _merge(chain[-1], observation)
                prior_obs = _sort_components(observation)
                continue
            era += 1
            version = _new_version(observation, RETRACT, era, prior_obs,
                                   len(chain) + 1)
            chain.append(version)
            status = STATUS_RETRACTED
            prior_obs = _sort_components(observation)
            continue

        # 部分抓取不能断言正文变化：并入当前版本，避免伪造 update。
        # 首次观测就残缺时仍建立 first_publish（标记 partial），
        # 等同一来源快照的完整正文到达再原地升级。
        if observation.get("partial") and last_publish is not None:
            _merge(last_publish, observation)
            prior_obs = _sort_components(observation)
            continue

        # 先抓到残缺版本、随后补到同一来源快照的完整正文：原地升级首个版本。
        if (last_publish is not None and last_publish.get("partial")
                and status == STATUS_ACTIVE
                and _same_source_identity(observation, last_publish)):
            _merge(last_publish, observation)
            prior_obs = _sort_components(observation)
            continue

        if last_publish is None:
            event = FIRST_PUBLISH
        elif status == STATUS_RETRACTED:
            # 撤回后的重新发布：即使正文指纹完全相同也必须成为新版本，
            # 旧的 retract 版本保留，撤回事实不可被覆盖。
            era += 1
            event = REPUBLISH
        else:
            content_changed = (observation.get("body_fingerprint")
                               != last_publish.get("body_fingerprint"))
            # 只有真正抓到了附件字段，附件变化才算数；否则视为未抓到。
            attach_changed = (
                bool(observation.get("attachments_present", True))
                and observation.get("attachment_fingerprint")
                != last_publish.get("attachment_fingerprint"))
            if intent == INTENT_ATTACHMENT_REPLACE and attach_changed:
                event = ATTACHMENT_REPLACE
            elif attach_changed and not content_changed:
                event = ATTACHMENT_REPLACE
            elif content_changed:
                event = UPDATE
            elif intent == INTENT_UPDATE:
                event = UPDATE
            elif intent == INTENT_REPUBLISH:
                event = UPDATE
            else:
                # 与当前有效版本完全一致：重复抓取，并入已有版本。
                _merge(last_publish, observation)
                prior_obs = _sort_components(observation)
                continue

        version = _new_version(observation, event, era, prior_obs,
                               len(chain) + 1)
        chain.append(version)
        if event in PUBLISH_EVENTS:
            last_publish = version
            status = STATUS_ACTIVE
        prior_obs = _sort_components(observation)

    _finalize_version_ids(chain)
    return chain


def _finalize_version_ids(chain: list[dict]) -> None:
    """链定稿后派生稳定版本键；保证同一链内键唯一、重放可复现。

    身份要素包含公告、事件、撤回周期（era）、来源时间、来源序号与内容指纹。
    撤回后重新发布因 era 不同而必然得到新键，即使正文指纹完全相同；
    同事件、同快照的极端情况再用 ordinal 做确定性消歧。
    """
    used: set[str] = set()
    for version in chain:
        material = [
            version["notice_id"], version["event"], version["era"],
            version.get("source_time"), version.get("source_seq"),
            version.get("body_fingerprint"),
            version.get("attachment_fingerprint"),
        ]
        version_id = "ver-" + _fingerprint(material)[:24]
        if version_id in used:
            material.append(version["ordinal"])
            version_id = "ver-" + _fingerprint(material)[:24]
        used.add(version_id)
        version["version_id"] = version_id


def build_history(records: Iterable[Mapping[str, Any]]) -> dict[str, list[dict]]:
    """按公告分组推导版本历史（纯函数）。

    输入既可包含原始记录，也可包含已规范化观测。
    """
    grouped: dict[str, list] = {}
    for item in records:
        observation = (item if "observation_id" in item and "intent" in item
                       else normalize(item))
        grouped.setdefault(observation["notice_id"], []).append(observation)
    return {notice: build_chain(observations)
            for notice, observations in grouped.items()}


# 语义别名
derive_events = build_history


# ---------------------------------------------------------------------------
# 持久化：观测表 + 版本历史表
# ---------------------------------------------------------------------------

OBSERVATION_TABLE = "version_observations"
HISTORY_TABLE = "version_history"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def ensure_schema(con) -> None:
    """创建版本历史所需的表；可在旧库上幂等升级。"""
    con.execute(
        "CREATE TABLE IF NOT EXISTS records("
        " key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {OBSERVATION_TABLE}("
        " observation_id TEXT PRIMARY KEY,"
        " notice_id TEXT NOT NULL,"
        " source TEXT,"
        " payload TEXT NOT NULL)")
    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_obs_notice ON "
        f"{OBSERVATION_TABLE}(notice_id)")
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {HISTORY_TABLE}("
        " version_id TEXT PRIMARY KEY,"
        " notice_id TEXT NOT NULL,"
        " ordinal INTEGER NOT NULL,"
        " event TEXT NOT NULL,"
        " payload TEXT NOT NULL,"
        " UNIQUE(notice_id, ordinal))")
    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_hist_notice ON "
        f"{HISTORY_TABLE}(notice_id)")
    return None


def _ensure_conn(con):
    if con is None:
        from .config import DB_PATH
        con = sqlite3.connect(DB_PATH)
        ensure_schema(con)
        return con, True
    ensure_schema(con)
    return con, False


def store_observation(con, observation: Mapping[str, Any]) -> str:
    """幂等写入观测；同一观测重复抓取不会产生新行。"""
    con.execute(
        f"INSERT INTO {OBSERVATION_TABLE}(observation_id,notice_id,source,payload)"
        " VALUES(?,?,?,?)"
        " ON CONFLICT(observation_id) DO UPDATE SET"
        " notice_id=excluded.notice_id, source=excluded.source,"
        " payload=excluded.payload",
        (observation["observation_id"], observation["notice_id"],
         observation.get("source") or "", _json(observation)))
    return observation["observation_id"]


def load_observations(con, notice_id: str | None = None) -> list[dict]:
    if notice_id is None:
        rows = con.execute(
            f"SELECT payload FROM {OBSERVATION_TABLE}").fetchall()
    else:
        rows = con.execute(
            f"SELECT payload FROM {OBSERVATION_TABLE} WHERE notice_id=?",
            (notice_id,)).fetchall()
    return [json.loads(payload) for payload, in rows]


def _replace_history(con, notice_id: str, chain: list[Mapping[str, Any]]) -> None:
    """用重新推导的链整体替换该公告的历史（幂等，重放安全）。"""
    con.execute(f"DELETE FROM {HISTORY_TABLE} WHERE notice_id=?", (notice_id,))
    for version in chain:
        con.execute(
            f"INSERT INTO {HISTORY_TABLE}(version_id,notice_id,ordinal,event,payload)"
            " VALUES(?,?,?,?,?)",
            (version["version_id"], notice_id, int(version["ordinal"]),
             version["event"], _json(version)))


def _refresh_notice(con, notice_id: str) -> list[dict]:
    chain = build_chain(load_observations(con, notice_id), notice_id)
    _replace_history(con, notice_id, chain)
    return chain


def ingest(con, record: Mapping[str, Any]) -> dict:
    """写入一条观测并刷新其公告的版本链，返回该观测对应的版本。

    同一观测重复写入不会新增版本；撤回后重新发布使用新的版本键。
    """
    con, own = _ensure_conn(con)
    try:
        observation = (record if _looks_normalized(record) else normalize(record))
        store_observation(con, observation)
        chain = _refresh_notice(con, observation["notice_id"])
        if own:
            con.commit()
        for version in chain:
            if observation["observation_id"] in version["observation_ids"]:
                return version
        # 观测必然落在链中的某个版本；保底返回链尾。
        return chain[-1] if chain else dict(observation)
    finally:
        if own:
            con.close()


def ingest_many(con, records: Iterable[Mapping[str, Any]]) -> list[dict]:
    """批量写入观测，并按公告各刷新一次版本链。"""
    con, own = _ensure_conn(con)
    try:
        observations = []
        for record in records:
            observation = (record if _looks_normalized(record)
                           else normalize(record))
            store_observation(con, observation)
            observations.append(observation)
        touched = []
        seen = set()
        for observation in observations:
            notice_id = observation["notice_id"]
            if notice_id not in seen:
                seen.add(notice_id)
                touched.append(notice_id)
        for notice_id in touched:
            _refresh_notice(con, notice_id)
        results = []
        for observation in observations:
            version = _version_for_observation(con, observation)
            if version is not None:
                results.append(version)
        if own:
            con.commit()
        return results
    finally:
        if own:
            con.close()


def _version_for_observation(con, observation: Mapping[str, Any]):
    chain = history(con, observation["notice_id"])
    for version in chain:
        if observation["observation_id"] in version["observation_ids"]:
            return version
    return chain[-1] if chain else None


def _looks_normalized(record: Mapping[str, Any]) -> bool:
    return (isinstance(record, Mapping) and "observation_id" in record
            and "intent" in record and "notice_id" in record)


def history(con, notice_id: str) -> list[dict]:
    """读取某公告已持久化的版本链（按 ordinal 排序）。"""
    ensure_schema(con)
    rows = con.execute(
        f"SELECT payload FROM {HISTORY_TABLE} WHERE notice_id=?"
        " ORDER BY ordinal", (notice_id,)).fetchall()
    return [json.loads(payload) for payload, in rows]


def notices(con) -> list[str]:
    """系统内已知的全部公告：观测表与版本历史表的并集。"""
    ensure_schema(con)
    rows = con.execute(
        f"SELECT notice_id FROM {OBSERVATION_TABLE}"
        f" UNION SELECT notice_id FROM {HISTORY_TABLE}"
        " ORDER BY notice_id").fetchall()
    return [row[0] for row in rows]


def current_from_history(history_rows: Iterable[Mapping[str, Any]],
                         notice_id: str | None = None) -> dict:
    """完全从版本历史重建当前有效状态（不依赖观测表）。"""
    chain = [dict(v) for v in history_rows]
    return effective_state(chain, notice_id)


def effective(con, notice_id: str) -> dict:
    """读取持久化历史并推导当前有效状态。"""
    return current_from_history(history(con, notice_id), notice_id)


def rebuild(con, notice_id: str | None = None) -> dict[str, list[dict]]:
    """从已落库的观测重建版本历史（恢复重放的主入口）。

    若某公告的观测已被清理（只保留历史），保留其既有版本链而不清空，
    这样“从历史重建当前有效版”不会因观测缺失而丢失事实。
    """
    ensure_schema(con)
    target = [notice_id] if notice_id is not None else notices(con)
    result = {}
    for nid in target:
        observations = load_observations(con, nid)
        if observations:
            result[nid] = _refresh_notice(con, nid)
        else:
            result[nid] = history(con, nid)
    return result


# 别名：恢复重放
replay = rebuild


def effective_states(con) -> dict[str, dict]:
    """导出全部公告的当前有效状态，便于比较连续运行与恢复重放。"""
    return {nid: effective(con, nid) for nid in notices(con)}


def materialize_current(con, notice_id: str | None = None) -> dict[str, dict]:
    """把从历史重建的当前有效版本写回现有 ``records`` 存储。

    这样即使只保留历史，现有存储也能从历史重建当前有效版；
    同时确保 ``records`` 的键与 ``version_id`` 一致，不重复占用。
    """
    from .keyer import key_of
    from .models import Record
    from . import store as _store

    target = [notice_id] if notice_id is not None else notices(con)
    written = {}
    for nid in target:
        chain = history(con, nid)
        state = current_from_history(chain, nid)
        written[nid] = state
        if state["effective_version_id"] is None:
            # 已撤回且未重新发布：现有存储里不应再保留该公告的有效记录。
            for key, data in _store.all_records(con):
                if isinstance(data, Mapping) and _same_notice(data, nid):
                    _store.delete(con, key)
            continue
        version = next((v for v in chain
                        if v["version_id"] == state["effective_version_id"]),
                       chain[-1])
        payload = _materialized_payload(version, state)
        _store.upsert(con, Record(key=key_of(payload), data=payload))
    commit(con)
    return written


def _same_notice(data: Mapping[str, Any], notice_id: str) -> bool:
    return any(_text(data.get(name)) == notice_id
               for name in ("gazette_id", "notice_id", "publication_id",
                            "announcement_id", "id"))


def _materialized_payload(version: Mapping[str, Any],
                          state: Mapping[str, Any]) -> dict:
    payload = dict(version.get("payload") or {})
    payload.setdefault("id", version["notice_id"])
    payload["notice_id"] = version["notice_id"]
    payload["version_id"] = version["version_id"]
    payload["lifecycle"] = version["event"]
    payload["notice_status"] = state["status"]
    payload["effective_version_id"] = state["effective_version_id"]
    payload["ever_retracted"] = state["ever_retracted"]
    payload["body_fingerprint"] = version.get("body_fingerprint")
    payload["attachment_fingerprint"] = version.get("attachment_fingerprint")
    payload["attachment_versions"] = list(version.get("attachment_versions") or [])
    payload["source_time"] = version.get("source_time")
    payload["_versioning"] = True
    return payload


def commit(con) -> None:
    try:
        con.commit()
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# 流水线阶段入口
# ---------------------------------------------------------------------------

def process(record, con=None):
    """流水线阶段：把记录作为观测写入版本历史，并回填版本字段。

    ``con`` 可由流水线注入以共享事务；单独调用时会连接项目 SQLite 存储并提交。
    """
    if con is None:
        from . import store as _store
        con = _store.connect()
        ensure_schema(con)
        return _process_with(con, record, own=True)
    ensure_schema(con)
    return _process_with(con, record, own=False)


def process_con(con, record):
    """流水线使用的共享连接版本：在同一事务内写入观测并刷新版本链。"""
    ensure_schema(con)
    return _process_with(con, record, own=False)


def _process_with(con, record, own: bool) -> dict:
    observation = normalize(record)
    store_observation(con, observation)
    chain = _refresh_notice(con, observation["notice_id"])
    version = next((v for v in chain
                    if observation["observation_id"] in v["observation_ids"]),
                   chain[-1] if chain else observation)
    state = effective_state(chain, observation["notice_id"])
    rec = dict(record)
    # 无显式 id 时用公告身份作为稳定主键，确保与历史物化写入的键一致。
    if not rec.get("id"):
        rec["id"] = observation["notice_id"]
    rec["notice_id"] = observation["notice_id"]
    rec["observation_id"] = observation["observation_id"]
    rec["version_id"] = version.get("version_id")
    rec["lifecycle"] = version.get("event")
    rec["version_ordinal"] = version.get("ordinal")
    rec["parse_batches"] = list(version.get("parse_batches") or [])
    rec["body_fingerprint"] = version.get("body_fingerprint")
    rec["attachment_versions"] = list(version.get("attachment_versions") or [])
    rec["effective_version_id"] = state["effective_version_id"]
    rec["notice_status"] = state["status"]
    rec["ever_retracted"] = state["ever_retracted"]
    rec["version_history"] = [
        {"version_id": v["version_id"], "ordinal": v.get("ordinal"),
         "event": v["event"], "body_fingerprint": v.get("body_fingerprint"),
         "attachment_fingerprint": v.get("attachment_fingerprint"),
         "source_time": v.get("source_time"),
         "order_evidence": v.get("order_evidence")}
        for v in chain
    ]
    if own:
        commit(con)
        con.close()
    return rec


def finish(con=None) -> dict[str, dict]:
    """流水线收尾：从历史物化当前有效版本到现有存储。"""
    con, own = _ensure_conn(con)
    try:
        result = materialize_current(con)
        commit(con)
        return result
    finally:
        if own:
            con.close()
