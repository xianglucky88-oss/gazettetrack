"""单一职责：字段规整/规范化。

本模块只负责把「值」变成稳定可比较的规范形式，不做任何合并或版本判断：

* `process(record)` 沿用基线的 ``src/name/value`` 规整，并顺带规整记录里已经
  存在的公告字段（案号、正文、附件、日期等）；
* 其余 ``normalize_*`` / ``fingerprint_*`` / ``source_*`` 是纯函数，供
  ``gazette_parse``、``versioning``、``dedupe``、``attach``、``keyer`` 复用，
  避免各阶段各写一套归一化规则。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import unicodedata
from typing import Any

_WS_RE = re.compile(r"\s+")
# NFKC 之后的全角括号已变成半角，案号形如 (2026)京01民初123号。
_CASE_NO_RE = re.compile(r"\(\s*\d{4}\s*\)\s*[一-鿿A-Za-z0-9]{1,24}号")
_DATE_RE = re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
_COMPACT_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def normalize_text(value: Any) -> str:
    """NFKC + 压缩空白；用于正文、主体、地区等自由文本。"""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    return _WS_RE.sub(" ", text).strip()


def normalize_case_no(value: Any) -> str:
    """案号比较键：去空白、统一括号/连接符、ASCII 大小写折叠。

    只做格式归一，不做「相近案号」猜测；相近判断由 dedupe 负责。
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _WS_RE.sub("", text)
    for dash in ("－", "—", "–", "﹣"):
        text = text.replace(dash, "-")
    return text.lower()


def extract_case_no(*texts: Any) -> str:
    """从正文/标题中提取案号（仅在显式字段缺失时使用）。"""
    for text in texts:
        if not text:
            continue
        match = _CASE_NO_RE.search(unicodedata.normalize("NFKC", str(text)))
        if match:
            return match.group(0)
    return ""


def normalize_date(value: Any) -> str | None:
    """把常见日期写法归一成 ``YYYY-MM-DD``；无法解析时保留原文本。"""
    if value is None:
        return None
    text = normalize_text(value)
    if not text:
        return None
    match = _DATE_RE.match(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return _dt.date(year, month, day).isoformat()
        except ValueError:
            return text
    match = _COMPACT_DATE_RE.match(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return _dt.date(year, month, day).isoformat()
        except ValueError:
            return text
    return text


def date_ordinal(value: Any) -> int | None:
    """日期比较用的序数；无法解析时返回 ``None``（不参与日期窗口判断）。"""
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)).toordinal()
    except ValueError:
        return None


def normalize_body(value: Any) -> str:
    return normalize_text(value)


def fingerprint_body(value: Any) -> str | None:
    """正文指纹；空白差异被归一，实质修订会产生不同指纹。"""
    text = normalize_body(value)
    if not text:
        return None
    return "b-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def normalize_attachments(value: Any) -> list[dict[str, Any]]:
    """附件统一成 ``[{name, sha256, size, url}]`` 并稳定排序。"""
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    items: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            size = item.get("size")
            try:
                size = int(size) if size is not None and str(size) != "" else None
            except (TypeError, ValueError):
                size = None
            items.append(
                {
                    "name": normalize_text(item.get("name") or item.get("文件名") or item.get("file")),
                    "sha256": normalize_text(item.get("sha256") or item.get("hash")).lower() or None,
                    "size": size,
                    "url": normalize_text(item.get("url")) or None,
                }
            )
        else:
            items.append({"name": normalize_text(item), "sha256": None, "size": None, "url": None})
    return sorted(
        items,
        key=lambda att: (
            att["name"],
            att["sha256"] or "",
            -1 if att["size"] is None else att["size"],
            att["url"] or "",
        ),
    )


def fingerprint_attachments(attachments: Any) -> str | None:
    """附件指纹：优先用内容哈希，其次文件名；跨地区 URL 差异不参与。"""
    items = normalize_attachments(attachments)
    if not items:
        return None
    payload = [(att["sha256"] or att["name"], att["size"]) for att in items]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return "a-" + digest[:32]


def process(record: dict[str, Any]) -> dict[str, Any]:
    """基线字段规整 + 已存在公告字段的值归一化。"""
    rec = dict(record)
    rec["src"] = str(rec.get("src", "")).strip().lower()
    rec["name"] = str(rec.get("name", "")).strip()
    try:
        rec["value"] = int(rec.get("value", 0))
    except (TypeError, ValueError):
        rec["value"] = 0

    if rec.get("case_no"):
        rec["case_no"] = normalize_text(rec["case_no"])
        rec["case_no_key"] = normalize_case_no(rec["case_no"])
    if rec.get("case_no_key"):
        rec["case_no_key"] = normalize_case_no(rec["case_no_key"])
    if rec.get("region"):
        rec["region"] = normalize_text(rec["region"])
    if rec.get("matter"):
        rec["matter"] = normalize_text(rec["matter"])
    if rec.get("date"):
        rec["date"] = normalize_date(rec["date"])
    if rec.get("body") is not None:
        rec["body"] = normalize_body(rec["body"])
        rec["body_fp"] = fingerprint_body(rec["body"])
    if rec.get("attachments") is not None:
        rec["attachments"] = normalize_attachments(rec["attachments"])
        rec["attachment_fp"] = fingerprint_attachments(rec["attachments"])
    return rec


def source_descriptor(record: dict[str, Any]) -> dict[str, Any]:
    """单条原始公告的来源描述；合并后仍可按 anchor 追溯/撤销。"""
    anchor = str(record.get("id") or "")
    if not anchor:
        basis = {
            "src": record.get("src"),
            "case_no": record.get("case_no_key"),
            "matter": record.get("matter_key"),
            "date": record.get("date"),
            "body": record.get("body_fp"),
            "attachments": record.get("attachment_fp"),
        }
        anchor = "anon-" + hashlib.sha256(
            json.dumps(basis, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
    return {
        "anchor": anchor,
        "id": record.get("id"),
        "src": normalize_text(record.get("src")) or None,
        "region": normalize_text(record.get("region")) or None,
        "date": record.get("date"),
        "url": normalize_text(record.get("url")) or None,
        "body_fp": record.get("body_fp"),
        "attachment_fp": record.get("attachment_fp"),
        "attachments": normalize_attachments(record.get("attachments")),
    }


def _descriptor_sort_key(descriptor: dict[str, Any]) -> str:
    return json.dumps(descriptor, ensure_ascii=False, sort_keys=True, default=str)


def sort_descriptors(descriptors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for descriptor in descriptors:
        unique[_descriptor_sort_key(descriptor)] = descriptor
    return sorted(unique.values(), key=_descriptor_sort_key)


def aggregate_sources(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """把来源集合聚合成规范记录上的可解释字段。

    * ``date`` 取最早公示日（后续转载不改变首次公示日期）；
    * ``attachments`` 取并集（按名称/哈希/大小去重）；
    * ``attachment_variants`` 保留所有附件指纹，供差异证据使用。
    """
    ordered = sort_descriptors(sources)
    srcs = sorted({s["src"] for s in ordered if s.get("src")})
    regions = sorted({s["region"] for s in ordered if s.get("region")})
    dates = sorted({s["date"] for s in ordered if s.get("date")})
    attachments: list[dict[str, Any]] = []
    seen: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    variants = sorted({s["attachment_fp"] for s in ordered if s.get("attachment_fp")})
    for descriptor in ordered:
        for item in descriptor.get("attachments") or []:
            key = (item.get("name"), item.get("sha256"), item.get("size"))
            if key not in seen:
                seen[key] = item
                attachments.append(item)
    attachments.sort(
        key=lambda att: (
            att.get("name") or "",
            att.get("sha256") or "",
            -1 if att.get("size") is None else att["size"],
        )
    )
    return {
        "sources": ordered,
        "source_anchors": [s["anchor"] for s in ordered],
        "srcs": srcs,
        "regions": regions,
        "date": dates[0] if dates else None,
        "attachments": attachments,
        "attachment_fp": fingerprint_attachments(attachments),
        "attachment_variants": variants,
    }
