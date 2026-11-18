"""单一职责：附件线索的规整（流水线阶段）。

``gazette_parse`` 只负责从公示栏里抽取附件线索，本阶段把线索整理成可落库、
可供下载的形式：统一字段、修正不安全文件名、标出缺失 URL 的线索。本阶段不做
网络 IO；下载由 ``gazette_parse.download_attachments`` 按线索逐条执行。
"""

from pathlib import Path
from urllib.parse import unquote, urlparse


def _text(value):
    return "" if value is None else str(value).strip()


def _safe_name(name):
    name = "".join(ch for ch in name if ch >= " " and ch not in '<>:"|?*')
    name = Path(name.replace("\\", "/")).name
    return name.strip(" .")[:180]


def _filename(clue):
    name = _safe_name(_text(clue.get("filename")))
    if name:
        return name
    url = _text(clue.get("url"))
    path = unquote(urlparse(url).path) if url else ""
    return _safe_name(Path(path).name) or "attachment"


def _is_attachment(clue):
    if "title" in clue or "attachments" in clue:
        return False  # 公告条目本身，不是附件线索
    keys = set(clue)
    if {"kind", "extension"} & keys:
        return True
    return {"url", "filename"} & keys and "entry_key" in keys


def process(record):
    rec = dict(record)
    if not _is_attachment(rec):
        # 普通公告记录保持原行为，不注入附件字段。
        rec.setdefault("_stage_attach", "ok")
        return rec
    url = _text(rec.get("url"))
    rec["url"] = url or None
    rec["filename"] = _filename(rec)
    rec["extension"] = Path(rec["filename"]).suffix.lower()
    rec["label"] = _text(rec.get("label")) or rec["filename"]
    if rec.get("status") not in {"pending", "unresolved", "downloaded", "failed", "reused", "skipped_existing"}:
        rec["status"] = "pending" if rec["url"] else "unresolved"
    if not rec["url"]:
        rec["status"] = "unresolved"
        rec.setdefault("error", "缺少附件 URL")
    rec.setdefault("entry_key", None)
    rec.setdefault("source", "")
    rec.setdefault("batch_id", "")
    rec["_stage_attach"] = "ok"
    return rec
