"""单一职责：本地 sqlite3 持久化与历史表。

表结构
------
* ``records(key, data)`` —— 当前可查询的规范记录（每个事项一个 head）；
* ``versions(matter_id, version, key, content_sig, data, status)`` ——
  只追加的版本身份，保留被取代版本的完整数据，供撤销与来源恢复；
* ``matters(matter_id, head_key, current_version)`` —— 事项当前 head 指针；
* ``version_events`` —— 版本计算/回退审计；
* ``merge_history`` / ``merge_suppressed`` —— 合并决定与错误合并撤销；
* ``entity_aliases`` —— 主体别名到 ``entity_id`` 的稳定映射。

``upsert`` / ``count`` 保持基线语义供旧调用方使用。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions(
    matter_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    key TEXT NOT NULL,
    content_sig TEXT NOT NULL,
    change_type TEXT NOT NULL,
    data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    PRIMARY KEY (matter_id, version)
);
CREATE INDEX IF NOT EXISTS versions_by_sig ON versions(matter_id, content_sig);
CREATE INDEX IF NOT EXISTS versions_by_key ON versions(key);
CREATE TABLE IF NOT EXISTS matters(
    matter_id TEXT PRIMARY KEY,
    head_key TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS version_events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    matter_id TEXT NOT NULL,
    action TEXT NOT NULL,
    from_key TEXT,
    to_key TEXT,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_history(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key TEXT NOT NULL,
    decision TEXT NOT NULL,
    members TEXT NOT NULL,
    observations TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'merge',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_suppressed(
    group_key TEXT PRIMARY KEY,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_aliases(
    identity_key TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    display_name TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=None):
    con = sqlite3.connect(str(path or DB_PATH))
    con.executescript(_SCHEMA)
    return con


def upsert(con, record):
    con.execute(
        "INSERT INTO records(key,data) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
        (record.key, json.dumps(record.data, ensure_ascii=False, sort_keys=True)),
    )


def delete(con, key):
    con.execute("DELETE FROM records WHERE key=?", (key,))


def get(con, key):
    row = con.execute("SELECT data FROM records WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def commit(con):
    con.commit()


def count(con):
    return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]


def put_version(
    con,
    matter_id: str,
    version: int,
    key: str,
    content_sig: str,
    change_type: str,
    data: dict,
    status: str = "active",
) -> None:
    con.execute(
        "INSERT INTO versions(matter_id,version,key,content_sig,change_type,data,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(matter_id,version) DO UPDATE SET "
        "key=excluded.key, content_sig=excluded.content_sig, change_type=excluded.change_type, "
        "data=excluded.data, status=excluded.status",
        (
            matter_id,
            version,
            key,
            content_sig,
            change_type,
            json.dumps(data, ensure_ascii=False, sort_keys=True, default=str),
            status,
            _now(),
        ),
    )


def versions_for(con, matter_id):
    rows = con.execute(
        "SELECT version,key,content_sig,change_type,data,status,created_at "
        "FROM versions WHERE matter_id=? ORDER BY version",
        (matter_id,),
    ).fetchall()
    return [
        {
            "version": r[0],
            "key": r[1],
            "content_sig": r[2],
            "change_type": r[3],
            "data": json.loads(r[4]),
            "status": r[5],
            "created_at": r[6],
        }
        for r in rows
    ]


def set_head(con, matter_id: str, key: str, version: int) -> None:
    con.execute(
        "INSERT INTO matters(matter_id,head_key,current_version,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(matter_id) DO UPDATE SET "
        "head_key=excluded.head_key, current_version=excluded.current_version, "
        "updated_at=excluded.updated_at",
        (matter_id, key, version, _now()),
    )


def head_of(con, matter_id):
    row = con.execute(
        "SELECT head_key,current_version FROM matters WHERE matter_id=?", (matter_id,)
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def log_event(con, matter_id, action, from_key=None, to_key=None, detail=None) -> None:
    con.execute(
        "INSERT INTO version_events(matter_id,action,from_key,to_key,detail,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            matter_id,
            action,
            from_key,
            to_key,
            json.dumps(detail or {}, ensure_ascii=False, sort_keys=True, default=str),
            _now(),
        ),
    )


def events_for(con, matter_id):
    rows = con.execute(
        "SELECT action,from_key,to_key,detail,created_at FROM version_events "
        "WHERE matter_id=? ORDER BY seq",
        (matter_id,),
    ).fetchall()
    return [
        {
            "action": r[0],
            "from_key": r[1],
            "to_key": r[2],
            "detail": json.loads(r[3]),
            "created_at": r[4],
        }
        for r in rows
    ]


# ---------------------------------------------------------------- merges


def record_merge(con, group_key, decision, members, observations, action="merge") -> None:
    """追加一条合并历史；内容与上一条完全相同的 merge 不重复记录。

    undo / restore 每次都追加（它们本身是审计事件），只有幂等重跑产生的
    相同 merge 会被折叠，避免历史表随重跑线性膨胀。
    """
    members_json = json.dumps(members, ensure_ascii=False, sort_keys=True)
    observations_json = json.dumps(
        observations, ensure_ascii=False, sort_keys=True, default=str
    )
    if action == "merge":
        # 只有当「最近一次状态变化」就是等价的 merge 时才折叠；撤销后重新合并、
        # 或 restore 之后再合并，都是新的审计事件，必须保留。
        last_state = con.execute(
            "SELECT decision,members,observations,action FROM merge_history "
            "WHERE group_key=? AND action IN ('merge','restore','undo') "
            "ORDER BY seq DESC LIMIT 1",
            (group_key,),
        ).fetchone()
        if last_state and last_state[3] == "merge" and (
            last_state[0],
            last_state[1],
            last_state[2],
        ) == (decision, members_json, observations_json):
            return
    con.execute(
        "INSERT INTO merge_history(group_key,decision,members,observations,action,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (group_key, decision, members_json, observations_json, action, _now()),
    )


def merge_history_for(con, group_key):
    rows = con.execute(
        "SELECT seq,decision,members,observations,action,created_at FROM merge_history "
        "WHERE group_key=? ORDER BY seq",
        (group_key,),
    ).fetchall()
    return [
        {
            "seq": r[0],
            "decision": r[1],
            "members": json.loads(r[2]),
            "observations": json.loads(r[3]),
            "action": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]


def suppress_group(con, group_key, reason=""):
    con.execute(
        "INSERT INTO merge_suppressed(group_key,reason,created_at) VALUES(?,?,?) "
        "ON CONFLICT(group_key) DO UPDATE SET reason=excluded.reason, created_at=excluded.created_at",
        (group_key, reason, _now()),
    )


def unsuppress_group(con, group_key):
    con.execute("DELETE FROM merge_suppressed WHERE group_key=?", (group_key,))


def suppressed_groups(con):
    return sorted(row[0] for row in con.execute("SELECT group_key FROM merge_suppressed"))


def clear_head(con, matter_id: str) -> None:
    con.execute("DELETE FROM matters WHERE matter_id=?", (matter_id,))


def set_versions_status(con, matter_id: str, status: str) -> None:
    con.execute("UPDATE versions SET status=? WHERE matter_id=?", (status, matter_id))


def delete_versions_for_keys(con, keys) -> None:
    for key in {k for k in keys if k}:
        con.execute("DELETE FROM versions WHERE key=?", (key,))
