"""单一职责：主体消歧，产出稳定的 ``entity_id``。

同一主体在不同地区公示栏里会写成「某某有限公司」「某某有限公司（以下简称…）」
或带统一社会信用代码。本阶段只解决「谁是同一个主体」：

1. 若记录自带 ``entity_id`` / ``credit_code``，以统一社会信用代码为最强身份；
2. 否则用规范化主体名（NFKC、去空白、去常见企业后缀/括号注释、去标点）；
3. 名称键相同时合并为同一 ``entity_id``，并把别名累积在记录上。

本阶段不判断公示事项是否相同——那是 ``dedupe`` 的职责；因此「同主体不同事项」
不会被这里误合并。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

_SUFFIXES = (
    "有限责任公司",
    "股份有限公司",
    "有限公司",
    "集团有限公司",
    "公司",
    "合伙企业",
    "事务所",
    "研究院",
    "中心",
    "厂",
)
_BRACKET_RE = re.compile(r"[（(【\[].*?[）)】\]]")
_PUNCT_RE = re.compile(r"[\s,，。.;；:：、·'\"“”‘’\-—_/\\|]+")
_CREDIT_RE = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{18}$")


def normalize_entity_name(name: Any) -> str:
    """主体名比较键：NFKC、去括号注释、去标点空白、统一大小写、去企业后缀。"""
    text = unicodedata.normalize("NFKC", str(name or ""))
    text = _BRACKET_RE.sub("", text)
    text = _PUNCT_RE.sub("", text).casefold()
    for suffix in _SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text


def normalize_credit_code(value: Any) -> str:
    text = re.sub(r"[\s\-]", "", unicodedata.normalize("NFKC", str(value or ""))).upper()
    return text if _CREDIT_RE.match(text) else ""


def register(con, record: dict[str, Any]) -> dict[str, Any]:
    """在持久化别名表中登记主体，返回带 ``entity_id`` 的记录副本。

    ``entity_aliases`` 让同一主体名键在不同批次间也稳定指向同一个 id；
    ``entity_id`` 一旦生成不因名称写法变化而漂移。
    """
    rec = dict(record)
    name = rec.get("entity_name") or rec.get("entity") or rec.get("subject")
    if name is None and not rec.get("entity_id"):
        return rec

    credit = normalize_credit_code(rec.get("credit_code") or rec.get("统一社会信用代码"))
    name_key = normalize_entity_name(name) if name is not None else ""
    if not credit and not name_key:
        return rec

    aliases = []
    if credit:
        aliases.append("credit:" + credit)
    if name_key:
        aliases.append("name:" + name_key)
    identity_key = aliases[0]

    existing = None
    if con is not None:
        for alias in aliases:
            row = con.execute(
                "SELECT entity_id FROM entity_aliases WHERE identity_key=?", (alias,)
            ).fetchone()
            if row:
                existing = row[0]
                break
    entity_id = existing or rec.get("entity_id")
    if not entity_id:
        entity_id = "e-" + hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:16]
    if con is not None:
        # 名称键与信用代码键互为别名，始终指向同一 id。
        for alias in aliases:
            con.execute(
                "INSERT OR IGNORE INTO entity_aliases(identity_key, entity_id, display_name) "
                "VALUES(?,?,?)",
                (alias, entity_id, str(name or credit)),
            )

    rec["entity_id"] = entity_id
    if name is not None:
        rec["entity_name"] = str(name).strip()
    rec["entity_key"] = identity_key
    rec.setdefault("_stage_entity_resolve", "ok")
    return rec


def process(record: dict[str, Any]) -> dict[str, Any]:
    """纯函数形态：不落库，只补 ``entity_id``/``entity_key``（供测试与编排使用）。"""
    return register(None, record)


def process_batch(records: list[dict[str, Any]], con=None) -> list[dict[str, Any]]:
    """批处理入口：逐条登记主体别名；空主体记录原样通过。"""
    resolved = []
    for record in records:
        resolved.append(register(con, record))
    return resolved
