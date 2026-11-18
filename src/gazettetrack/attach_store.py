"""单一职责：附件证据的持久化（sqlite）与内容寻址 blob 归档。

设计原则：
- ``attachments`` 描述公告版本声明的附件槽位（期望），状态机推进、永不静默删除；
- ``evidence`` 是按 sha256 内容寻址、只追加（append-only）的证据事实，文件名/来源 URL
  只是属性，同名不同内容天然成为两份证据，旧证据始终可读；
- ``attachment_evidence`` 记录槽位在某个时间点绑定了哪份证据，只追加，因此重复下载
  不会覆盖历史版本；
- ``download_attempts`` 完整记录每次下载尝试，支撑待补重试与部分失败恢复；
- ``archive_members`` 记录压缩包内每个成员（含重名成员）的落盘信息。

blob 文件以 ``ab/cd/<digest>`` 内容寻址存放；同内容永不重复落盘。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import BLOB_DIR as DEFAULT_BLOB_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments(
    slot_key        TEXT PRIMARY KEY,   -- 稳定槽位键：公告身份+版本+附件标识+页码
    announcement_id TEXT NOT NULL,      -- 公告身份（attach 阶段接收的解析身份）
    version         TEXT NOT NULL,      -- 公告版本
    attach_ref      TEXT NOT NULL,      -- 公告内附件标识（序号或源站 id）
    page_no         INTEGER,            -- 分页附件的页码；整份附件为 NULL
    page_total      INTEGER,            -- 分页附件的总页数（可空）
    region          TEXT,               -- 页码/区域定位（页码之外的坐标区域，JSON）
    url             TEXT NOT NULL,      -- 来源 URL
    declared_name   TEXT NOT NULL,      -- 公告声明的文件名
    expected_digest TEXT,               -- 源站/上一轮已知摘要（可空）
    kind            TEXT,               -- 声明类型：pdf / image / archive
    group_key       TEXT NOT NULL,      -- 分页附件的分组键；非分页等同 slot_key
    status          TEXT NOT NULL,      -- pending（待补）/verified（已验证）/failed（终态失败）
    parse_status    TEXT NOT NULL,      -- unparsed/parsed/parse_failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    first_seen      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence(
    digest        TEXT PRIMARY KEY,      -- sha256 内容摘要（确认后写入）
    blob_path     TEXT NOT NULL,        -- 内容寻址相对路径
    size_bytes    INTEGER NOT NULL,
    observed_name TEXT NOT NULL,        -- 下载实际得到的文件名
    kind          TEXT,                 -- 魔数嗅探类型
    parse_status  TEXT NOT NULL,        -- unparsed/parsed/parse_failed
    archived_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachment_evidence(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_key    TEXT NOT NULL,
    digest      TEXT NOT NULL,
    bound_at    TEXT NOT NULL,
    note        TEXT,
    UNIQUE(slot_key, digest)
);

CREATE TABLE IF NOT EXISTS download_attempts(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_key      TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    url           TEXT NOT NULL,
    result        TEXT NOT NULL,        -- transient/missing/digest_mismatch/ok
    http_status   INTEGER,
    bytes_seen    INTEGER NOT NULL DEFAULT 0,
    digest_seen   TEXT,
    error         TEXT,
    attempted_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archive_members(
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_digest   TEXT NOT NULL,      -- 所属压缩包证据摘要
    member_index    INTEGER NOT NULL,   -- 包内序号：重名成员靠它区分
    member_name     TEXT NOT NULL,
    member_digest   TEXT NOT NULL,
    blob_path       TEXT NOT NULL,
    parse_status    TEXT NOT NULL,
    archived_at     TEXT NOT NULL,
    UNIQUE(parent_digest, member_index, member_digest)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


# ---------------- 附件槽位（期望） ----------------

def get_slot(con, slot_key):
    row = con.execute(
        "SELECT * FROM attachments WHERE slot_key=?", (slot_key,)
    ).fetchone()
    return dict(row) if row else None


def upsert_slot(con, slot: dict) -> None:
    """登记/刷新附件槽位。已存在槽位的状态机字段保持不动（只更新声明信息）。"""
    now = utcnow()
    existing = get_slot(con, slot["slot_key"])
    if existing is None:
        con.execute(
            """INSERT INTO attachments(slot_key, announcement_id, version, attach_ref,
                   page_no, page_total, region, url, declared_name, expected_digest,
                   kind, group_key, status, parse_status, attempts, last_error,
                   first_seen, updated_at)
               VALUES(:slot_key,:announcement_id,:version,:attach_ref,:page_no,:page_total,
                   :region,:url,:declared_name,:expected_digest,:kind,:group_key,
                   'pending','unparsed',0,NULL,:now,:now)""",
            {**slot, "now": now},
        )
        return
    con.execute(
        """UPDATE attachments SET url=:url, declared_name=:declared_name,
               expected_digest=:expected_digest, kind=:kind, region=:region,
               page_total=:page_total, updated_at=:now
           WHERE slot_key=:slot_key""",
        {**slot, "now": now},
    )


def mark_slot(con, slot_key, status, parse_status=None, last_error=None) -> None:
    sets = ["status=:status", "updated_at=:now"]
    params = {"slot_key": slot_key, "status": status, "now": utcnow()}
    if parse_status is not None:
        sets.append("parse_status=:parse_status")
        params["parse_status"] = parse_status
    if last_error is not None:
        sets.append("last_error=:last_error")
        params["last_error"] = last_error
    con.execute(
        f"UPDATE attachments SET {', '.join(sets)} WHERE slot_key=:slot_key", params
    )


def bump_attempts(con, slot_key) -> int:
    con.execute(
        "UPDATE attachments SET attempts=attempts+1, updated_at=:now WHERE slot_key=:key",
        {"now": utcnow(), "key": slot_key},
    )
    return get_slot(con, slot_key)["attempts"]


def slots_of_announcement(con, announcement_id, version):
    rows = con.execute(
        "SELECT * FROM attachments WHERE announcement_id=? AND version=? ORDER BY attach_ref, page_no",
        (announcement_id, version),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------- 下载尝试流水 ----------------

def record_attempt(con, *, slot_key, run_id, url, result, http_status=None,
                   bytes_seen=0, digest_seen=None, error=None) -> None:
    con.execute(
        """INSERT INTO download_attempts(slot_key, run_id, url, result, http_status,
               bytes_seen, digest_seen, error, attempted_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (slot_key, run_id, url, result, http_status, bytes_seen, digest_seen,
         error, utcnow()),
    )


def attempts_of(con, slot_key):
    rows = con.execute(
        "SELECT * FROM download_attempts WHERE slot_key=? ORDER BY id", (slot_key,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------- 内容寻址证据（append-only） ----------------

def get_evidence(con, digest):
    row = con.execute("SELECT * FROM evidence WHERE digest=?", (digest,)).fetchone()
    return dict(row) if row else None


def archive_blob(digest, payload: bytes, blob_root=DEFAULT_BLOB_DIR) -> str:
    """把完整字节流以内容寻址方式落盘；同内容幂等复用，绝不覆盖。

    返回相对 blob 根的路径。先写同目录临时文件再原子改名，防止半截文件被当作证据。
    """
    import os
    blob_root = Path(blob_root)
    rel = f"{digest[:2]}/{digest[2:4]}/{digest}"
    final = blob_root / rel
    if final.exists() and final.stat().st_size == len(payload):
        return rel
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f".{digest}.tmp-{os.getpid()}")
    tmp.write_bytes(payload)
    os.replace(tmp, final)
    return rel


def register_evidence(con, *, digest, blob_path, size_bytes, observed_name,
                      kind, parse_status) -> bool:
    """登记证据元数据。已存在则原样保留（append-only），返回是否为新登记。"""
    if get_evidence(con, digest) is not None:
        return False
    con.execute(
        """INSERT INTO evidence(digest, blob_path, size_bytes, observed_name, kind,
               parse_status, archived_at)
           VALUES(?,?,?,?,?,?,?)""",
        (digest, blob_path, size_bytes, observed_name, kind, parse_status, utcnow()),
    )
    return True


def bind_evidence(con, slot_key, digest, note=None) -> None:
    """槽位绑定证据；同一绑定幂等，历史绑定行保留（重复下载可追溯）。"""
    con.execute(
        """INSERT OR IGNORE INTO attachment_evidence(slot_key, digest, bound_at, note)
           VALUES(?,?,?,?)""",
        (slot_key, digest, utcnow(), note),
    )


def bindings_of(con, slot_key):
    rows = con.execute(
        "SELECT * FROM attachment_evidence WHERE slot_key=? ORDER BY id", (slot_key,)
    ).fetchall()
    return [dict(r) for r in rows]


def read_evidence_bytes(con, digest, blob_root=DEFAULT_BLOB_DIR) -> bytes:
    """读取历史归档证据原文（旧证据始终可读）。"""
    ev = get_evidence(con, digest)
    if ev is None:
        raise KeyError(f"evidence not found: {digest}")
    return (Path(blob_root) / ev["blob_path"]).read_bytes()


# ---------------- 压缩包成员 ----------------

def record_member(con, *, parent_digest, member_index, member_name, member_digest,
                  blob_path, parse_status) -> None:
    con.execute(
        """INSERT OR IGNORE INTO archive_members(parent_digest, member_index,
               member_name, member_digest, blob_path, parse_status, archived_at)
           VALUES(?,?,?,?,?,?,?)""",
        (parent_digest, member_index, member_name, member_digest, blob_path,
         parse_status, utcnow()),
    )


def members_of(con, parent_digest):
    rows = con.execute(
        "SELECT * FROM archive_members WHERE parent_digest=? ORDER BY member_index",
        (parent_digest,),
    ).fetchall()
    return [dict(r) for r in rows]


def encode_region(region) -> str | None:
    if region is None:
        return None
    return json.dumps(region, ensure_ascii=False, sort_keys=True)
