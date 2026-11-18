import json

from src.gazettetrack import keyer, loader, pipeline, store
from src.gazettetrack.config import FIXTURES


def test_pipeline_runs_and_idempotent():
    a = pipeline.run()
    b = pipeline.run()   # 重跑不增行（幂等 upsert）
    assert a["fed"] == 4
    assert b["rows"] == a["rows"] == 3   # r-003 重复被去重


def test_store_roundtrip():
    con = store.connect()
    try:
        assert store.count(con) >= 0
    finally:
        con.close()


def test_baseline_fixture_output_shape_unchanged():
    """既有输入输出兼容：基线记录不进入公示语义，字段与键值保持原样。"""
    con = store.connect()
    try:
        pipeline.run(con)
        record = store.get(con, "r-001")
        assert record["id"] == "r-001"
        assert record["name"] == "Record One"
        assert record["value"] == 10
        assert "sources" not in record
        assert "group_key" not in record
        assert keyer.key_of({"id": "r-001", "anything": 1}) == "r-001"
    finally:
        con.close()


def test_loader_reads_configured_fixture():
    with open(FIXTURES, encoding="utf-8") as fh:
        assert json.load(fh) == loader.load()
