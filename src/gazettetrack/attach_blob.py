"""单一职责：不可信附件字节的处理——摘要、类型嗅探、压缩包安全展开。

本模块不访问网络、不碰数据库；所有输入都是已下载到独立暂存目录的不可信数据。
绝不按成员名拼路径（zip-slip 防护），重名成员由调用方按序号区分，全部保留。
"""
from __future__ import annotations

import hashlib
import posixpath
import re

# 声明类型 -> 文件头魔数（只做结构级可解析性判断，不引入第三方解析器）
_MAGIC = [
    ("pdf", b"%PDF-"),
    ("image", b"\xff\xd8\xff"),          # jpeg
    ("image", b"\x89PNG\r\n\x1a\n"),     # png
    ("image", b"GIF87a"),
    ("image", b"GIF89a"),
    ("archive", b"PK\x03\x04"),          # zip/jar/office 包
    ("archive", b"PK\x05\x06"),          # 空 zip
]


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sniff_kind(payload: bytes) -> str | None:
    for kind, magic in _MAGIC:
        if payload.startswith(magic):
            return kind
    return None


def name_from_url(url: str) -> str:
    tail = url.rsplit("/", 1)[-1].split("?", 1)[0]
    return tail or "unnamed"


def structural_parse(payload: bytes, declared_kind: str | None) -> tuple[str, str | None]:
    """返回 (parse_status, warning)。只验证魔数结构，不渲染、不执行内容。"""
    observed = sniff_kind(payload)
    if observed is None:
        return "unparsed", "content-type not recognized by magic bytes"
    if declared_kind and observed != declared_kind:
        return "parse_failed", f"declared kind {declared_kind!r} but magic says {observed!r}"
    return "parsed", None


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_member_name(raw: str, index: int) -> str:
    """把 zip 成员名压成单层安全文件名；冲突（含重名）由 index 前缀消除。"""
    base = posixpath.basename(raw.replace("\\", "/").rstrip("/")) or "unnamed"
    base = _SAFE_NAME.sub("_", base).strip("._") or "unnamed"
    return f"{index:04d}_{base}"


def iter_zip_members(payload: bytes):
    """惰性产出 (index, 原始成员名, 成员字节)，目录与异常成员跳过。

    重复名、重复条目全部保留，由 index 区分；损坏条目抛出由调用方按 parse_failed 处理。
    """
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        index = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            # zip-slip 防护：绝不使用成员名做真实路径，这里只做绝对路径/越界检测并告警
            if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                name = posixpath.basename(name) or "unsafe-member"
            data = zf.read(info)  # 受 zipfile 上限保护，读取在暂存目录语义之外
            yield index, name, data
            index += 1
