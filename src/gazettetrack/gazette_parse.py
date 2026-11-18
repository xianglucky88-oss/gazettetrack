"""单一职责：把异构公示表格记录解析成统一公告字段。

只做「解析」：别名取值、案号补全、事项/日期/正文/附件字段落地，并把派生
键交给 normalizer 的纯函数。不做候选、不做合并、不做版本排序。

支持的原始字段别名（大小写/中英文均可）::

    案号: case_no / case_number / caseNo / 案号
    主体: entity / subject / party / 主体 / 主体名称 / 当事人
    事项: matter / title / 事项 / 标题 / 公示事项
    地区: region / locality / 地区 / 区域
    日期: date / publish_date / publishDate / 公示日期 / 发布日期 / 公告日期
    正文: body / content / text / 正文 / 内容
    附件: attachments / attachment / 附件
    来源: source / src / 来源

解析结果保留原始字段，另写 ``case_no_key`` / ``matter_key`` /
``entity_name`` / ``body_fp`` / ``attachment_fp`` / ``is_gazette``。
"""
from __future__ import annotations

from typing import Any

from .normalizer import (
    extract_case_no,
    fingerprint_attachments,
    fingerprint_body,
    normalize_attachments,
    normalize_case_no,
    normalize_date,
    normalize_text,
)

CASE_FIELDS = ("case_no", "case_number", "caseNo", "案号", "案 号")
ENTITY_FIELDS = ("entity", "subject", "party", "主体", "主体名称", "当事人")
MATTER_FIELDS = ("matter", "title", "事项", "标题", "公示事项")
REGION_FIELDS = ("region", "locality", "地区", "区域", "公示地区")
DATE_FIELDS = ("date", "publish_date", "publishDate", "公示日期", "发布日期", "公告日期")
BODY_FIELDS = ("body", "content", "text", "正文", "内容")
ATTACHMENT_FIELDS = ("attachments", "attachment", "附件")
SOURCE_FIELDS = ("source", "src", "来源")


def _first(record: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = record.get(name)
        if value is not None and normalize_text(value) != "":
            return value
    return None


def looks_like_gazette(record: dict[str, Any]) -> bool:
    """判断记录是否进入了公示语义（用于与基线简单记录保持兼容）。"""
    if record.get("is_gazette"):
        return True
    for names in (CASE_FIELDS, ENTITY_FIELDS, MATTER_FIELDS, BODY_FIELDS, ATTACHMENT_FIELDS):
        if _first(record, names) is not None:
            return True
    return bool(record.get("case_no_key") or record.get("body_fp") or record.get("attachment_fp"))


def process(record: dict[str, Any]) -> dict[str, Any]:
    rec = dict(record)
    if not looks_like_gazette(rec):
        # 基线 fixtures（id/src/name/value/...）不进入公告语义，输出保持原样
        # （含基线阶段标记）。
        rec.setdefault("_stage_gazette_parse", "naive")
        return rec

    rec["is_gazette"] = True

    case_no = _first(rec, CASE_FIELDS) or rec.get("case_no")
    matter = _first(rec, MATTER_FIELDS)
    body = _first(rec, BODY_FIELDS)
    entity = _first(rec, ENTITY_FIELDS)
    region = _first(rec, REGION_FIELDS)
    published = _first(rec, DATE_FIELDS)
    attachments = _first(rec, ATTACHMENT_FIELDS)
    source = _first(rec, SOURCE_FIELDS)

    if case_no is None:
        case_no = extract_case_no(matter, body, rec.get("name"))
        if case_no:
            rec["case_no_extracted"] = True
    if case_no:
        rec["case_no"] = normalize_text(case_no)
        rec["case_no_key"] = normalize_case_no(case_no)

    if entity is not None:
        rec["entity_name"] = normalize_text(entity)
    elif rec.get("entity_name"):
        rec["entity_name"] = normalize_text(rec["entity_name"])

    if matter is not None:
        rec["matter"] = normalize_text(matter)
    elif rec.get("matter"):
        rec["matter"] = normalize_text(rec["matter"])
    if rec.get("matter"):
        rec["matter_key"] = rec["matter"].casefold()

    if region is not None:
        rec["region"] = normalize_text(region)
    if published is not None:
        rec["date"] = normalize_date(published)
    elif rec.get("date"):
        rec["date"] = normalize_date(rec["date"])

    if body is not None:
        rec["body"] = normalize_text(body)
        rec["body_fp"] = fingerprint_body(rec["body"])
    elif rec.get("body_fp") is None and rec.get("body") is not None:
        rec["body"] = normalize_text(rec["body"])
        rec["body_fp"] = fingerprint_body(rec["body"])

    if attachments is not None:
        rec["attachments"] = normalize_attachments(attachments)
        rec["attachment_fp"] = fingerprint_attachments(rec["attachments"])
    elif rec.get("attachments") is not None:
        rec["attachments"] = normalize_attachments(rec["attachments"])
        rec["attachment_fp"] = fingerprint_attachments(rec["attachments"])

    if source is not None:
        rec["src"] = normalize_text(source).lower()
    rec.setdefault("_stage_gazette_parse", "ok")
    return rec
