"""单一职责：本地 sqlite3 持久化，提供幂等 upsert / 计数 / 全量读取。"""
import json, sqlite3
from . import config

def connect(db_path=None):
    path = db_path or config.DB_PATH  # 动态读取，允许测试/运行期覆盖配置
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    return con

def upsert(con, record):
    con.execute("INSERT INTO records(key,data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (record.key, json.dumps(record.data, ensure_ascii=False)))

def commit(con): con.commit()

def count(con): return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]
