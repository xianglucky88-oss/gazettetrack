"""单一职责：本地 sqlite3 持久化，提供幂等 upsert / 计数 / 全量读取。

除公告记录本身，还保存解析批次（来源、批次号、统计）、附件线索和解析错误，
使一条记录的失败不会影响同批其他记录的写入。
"""
import hashlib
import json
import sqlite3
from typing import Any, Iterable, Mapping

from .config import DB_PATH


def connect(path=None):
    con = sqlite3.connect(path or DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS batches("
        "batch_id TEXT PRIMARY KEY, source TEXT, data TEXT NOT NULL)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS issues("
        "batch_id TEXT, source TEXT, code TEXT, page INTEGER, row INTEGER, "
        "table_no INTEGER, field TEXT, data TEXT NOT NULL, "
        "PRIMARY KEY(batch_id, code, page, row, table_no, field))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS attachments("
        "id TEXT PRIMARY KEY, entry_key TEXT, batch_id TEXT, source TEXT, "
        "url TEXT, data TEXT NOT NULL)"
    )
    return con


def upsert(con, record):
    con.execute("INSERT INTO records(key,data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (record.key, json.dumps(record.data, ensure_ascii=False)))


def upsert_batch(con, batch: Mapping[str, Any], issues: Iterable[Mapping[str, Any]] = ()):
    """写入/更新批次元信息；同一批次重跑时错误列表以本次为准。"""
    batch_id = str(batch.get("batch_id") or "")
    if not batch_id:
        raise ValueError("batch 缺少 batch_id")
    source = str(batch.get("source") or "")
    con.execute(
        "INSERT INTO batches(batch_id,source,data) VALUES(?,?,?) "
        "ON CONFLICT(batch_id) DO UPDATE SET source=excluded.source, data=excluded.data",
        (batch_id, source, json.dumps(_jsonable(batch), ensure_ascii=False)),
    )
    con.execute("DELETE FROM issues WHERE batch_id=?", (batch_id,))
    for issue in issues:
        con.execute(
            "INSERT OR REPLACE INTO issues"
            "(batch_id,source,code,page,row,table_no,field,data) VALUES(?,?,?,?,?,?,?,?)",
            (
                batch_id,
                str(issue.get("source") or source),
                str(issue.get("code") or ""),
                issue.get("page"),
                issue.get("row"),
                issue.get("table"),
                issue.get("field"),
                json.dumps(_jsonable(issue), ensure_ascii=False),
            ),
        )


def attachment_id(clue: Mapping[str, Any]) -> str:
    payload = "|".join(
        str(clue.get(key) or "")
        for key in ("source", "entry_key", "url", "filename")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def upsert_attachment(con, clue: Mapping[str, Any]):
    con.execute(
        "INSERT INTO attachments(id,entry_key,batch_id,source,url,data) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
        (
            attachment_id(clue),
            clue.get("entry_key"),
            clue.get("batch_id"),
            clue.get("source"),
            clue.get("url"),
            json.dumps(_jsonable(clue), ensure_ascii=False),
        ),
    )


def commit(con): con.commit()


def count(con): return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def count_batches(con): return con.execute("SELECT COUNT(*) FROM batches").fetchone()[0]


def issues(con, batch_id=None):
    sql = "SELECT data FROM issues"
    params: tuple[Any, ...] = ()
    if batch_id is not None:
        sql += " WHERE batch_id=?"
        params = (batch_id,)
    sql += " ORDER BY page, row, table_no, code"
    return [json.loads(data) for (data,) in con.execute(sql, params)]


def attachments(con, batch_id=None):
    sql = "SELECT data FROM attachments"
    params: tuple[Any, ...] = ()
    if batch_id is not None:
        sql += " WHERE batch_id=?"
        params = (batch_id,)
    return [json.loads(data) for (data,) in con.execute(sql, params)]


def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
