"""主体消歧（entity_resolve）测试。

三组资料验证自动确认与待审边界：
- fixtures/entity_cross_region.json  跨地区同名（含跨地区重复公示）
- fixtures/entity_rename.json        历史名称变更
- fixtures/entity_no_id.json         缺标识（简称/歧义/同名不同主体）
另覆盖：标识一致而地址冲突的取舍留痕、输入换序、重复导入、候选保存后重启、旧数据。
"""
import json
from pathlib import Path

import pytest

from src.gazettetrack import entity_resolve as er

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def registry_tmp(tmp_path, monkeypatch):
    """每个测试用独立注册表文件，并清空进程内缓存。"""
    monkeypatch.setattr(er, "REGISTRY_PATH", tmp_path / "registry.json")
    er._reset_cache()
    yield
    er._reset_cache()


def read_registry():
    return json.loads(Path(er.REGISTRY_PATH).read_text(encoding="utf-8"))


def snapshot(records):
    """记录级快照：主体引用 + 状态 + 候选，用于跨顺序/跨运行比对。"""
    out = {}
    for r in records:
        res = er.process(r)
        out[r["id"]] = (
            res["entity_id"],
            res["entity_status"],
            res["canonical_name"],
            tuple(c["entity_id"] for c in res["candidates"]),
        )
    return out


# ---------- 三组资料：自动确认与待审边界 ----------
def test_cross_region_same_name():
    recs = load("entity_cross_region.json")
    out = {r["id"]: er.process(r) for r in recs}
    # 同一统一社会信用代码的跨地区重复公示 → 同一主体
    assert out["cr-001"]["entity_id"] == out["cr-003"]["entity_id"]
    # 同名但标识不同 → 不同主体
    assert out["cr-001"]["entity_id"] != out["cr-002"]["entity_id"]
    # 同名但缺标识且地区不同 → 独立主体，不并入
    assert out["cr-004"]["entity_status"] == "auto"
    assert out["cr-004"]["entity_id"] not in {
        out["cr-001"]["entity_id"], out["cr-002"]["entity_id"]}
    # 来源证据保留：两条公示的观察都在
    reg = read_registry()
    ent = reg["entities"][out["cr-001"]["entity_id"]]
    assert [o["record_id"] for o in ent["observations"]] == ["cr-001", "cr-003"]
    assert ent["identifiers"]["uscc"] == "91330106MA27XW3A1B"
    assert ent["identifiers"]["case_nos"] == ["(2026)浙01执1234号"]


def test_historical_rename():
    recs = load("entity_rename.json")
    out = {r["id"]: er.process(r) for r in recs}
    ids = {o["entity_id"] for o in out.values()}
    assert len(ids) == 1  # 同一标识：改名前后是同一主体
    ent = read_registry()["entities"][ids.pop()]
    # 规范名取最新全称；历史名与简称进入别名留痕
    assert ent["canonical_name"] == "杭州云图智能科技有限公司"
    assert "杭州云图信息技术有限公司" in ent["aliases"]
    assert "云图智能" in ent["aliases"]
    assert len(ent["observations"]) == 3


def test_missing_identifiers():
    recs = load("entity_no_id.json")
    out = {r["id"]: er.process(r) for r in recs}
    # 缺标识：简称与全称、地址兼容 → 自动合并为同一主体
    assert out["ni-001"]["entity_id"] == out["ni-002"]["entity_id"]
    assert out["ni-002"]["entity_status"] == "auto"
    # 缺标识：同名但跨地区 → 不同主体
    assert out["ni-003"]["entity_id"] != out["ni-001"]["entity_id"]
    # 歧义简称（同时匹配两个主体）→ 待审，候选与裁决依据保留
    pend = out["ni-005"]
    assert pend["entity_status"] == "pending"
    assert pend["entity_id"] is None
    assert len(pend["candidates"]) == 2
    assert pend["adjudication"]["status"] == "pending"
    assert pend["adjudication"]["decided_by"] == "manual-required"
    for cand in pend["candidates"]:
        assert cand["matched"] and cand["entity_id"] and cand["canonical_name"]
    # 候选保存到注册表，供人工裁决
    reg = read_registry()
    assert reg["pending"]["ni-005"]["candidates"] == pend["candidates"]


# ---------- 标识一致而地址冲突：显式取舍，不做静默覆盖 ----------
def test_identifier_match_with_address_conflict_is_explicit():
    a = {"id": "ac-1", "src": "hz-court", "name": "恒信建设集团有限公司",
         "uscc": "91330100MA27GH8J6K", "address": "浙江省杭州市江干区凯旋路200号",
         "ts": "2026-01-01T00:00:00Z"}
    b = {"id": "ac-2", "src": "su-court", "name": "恒信建设集团有限公司",
         "uscc": "91330100MA27GH8J6K", "address": "江苏省苏州市工业园区星湖街328号",
         "ts": "2026-07-01T00:00:00Z"}
    ra, rb = er.process(a), er.process(b)
    # 取舍规则：标识一致 → 同一主体（标识优先于地址）
    assert ra["entity_id"] == rb["entity_id"]
    ent = read_registry()["entities"][ra["entity_id"]]
    # 但冲突不隐藏在一次覆盖里：两个地址都保留，冲突显式留痕
    assert len(ent["addresses"]) == 2
    assert len(ent["conflicts"]) == 1
    conf = ent["conflicts"][0]
    assert conf["field"] == "address"
    assert conf["resolution"] == "kept-single-entity"
    assert conf["policy"] == "uscc-anchor-priority"
    assert set(conf["values"]) == {a["address"], b["address"]}
    assert conf["evidence"][a["address"]] == ["ac-1"]
    assert conf["evidence"][b["address"]] == ["ac-2"]
    # 规范地址按确定性规则取舍：最新公示时间优先
    assert ent["canonical_address"] == b["address"]
    # 记录级裁决依据同样可见
    assert rb["adjudication"]["conflicts"] == ent["conflicts"]
    assert rb["adjudication"]["policy"] == "uscc-anchor-priority"


# ---------- 输入换序 / 重复导入 / 重启 ----------
@pytest.mark.parametrize("dataset", ["entity_cross_region.json",
                                     "entity_rename.json",
                                     "entity_no_id.json"])
def test_order_independence(dataset, tmp_path, monkeypatch):
    recs = load(dataset)
    orders = [recs, list(reversed(recs)), recs[2:] + recs[:2]]
    snapshots = []
    for i, order in enumerate(orders):
        monkeypatch.setattr(er, "REGISTRY_PATH", tmp_path / ("reg-%d.json" % i))
        er._reset_cache()
        snap = None
        for _ in range(2):  # 稳态：重复导入后收敛
            snap = snapshot(order)
        snapshots.append((snap, read_registry()))
    assert snapshots[0] == snapshots[1] == snapshots[2]


def test_duplicate_import_and_restart():
    recs = load("entity_no_id.json")
    first = snapshot(recs)
    reg_after_first = read_registry()
    # 重复导入：引用不变，注册表不变（幂等）
    second = snapshot(recs)
    assert first == second
    assert read_registry() == reg_after_first
    # 观察不重复
    ent = read_registry()["entities"][first["ni-001"][0]]
    rids = [o["record_id"] for o in ent["observations"]]
    assert sorted(rids) == sorted(set(rids))
    # 模拟重启：清进程内缓存，注册表文件仍在
    er._reset_cache()
    third = snapshot(recs)
    assert first == third
    assert read_registry() == reg_after_first
    # 候选保存后重启不丢
    assert "ni-005" in read_registry()["pending"]


# ---------- 其他边界 ----------
def test_unidentified_record_joins_identified_entity():
    a = er.process({"id": "u-1", "name": "宏发商贸有限公司",
                    "uscc": "91330106MA27XW3A1B",
                    "address": "杭州市西湖区文三路100号", "ts": "2026-01-01"})
    b = er.process({"id": "u-2", "name": "宏发商贸有限公司",
                    "address": "杭州市西湖区文三路100号", "ts": "2026-02-01"})
    assert b["entity_status"] == "auto"
    assert b["entity_id"] == a["entity_id"]
    assert "uscc-entity-name-match" in b["matched_via"]


def test_invalid_uscc_falls_back_to_name_anchor():
    out = er.process({"id": "x-1", "name": "杭州云图科技有限公司",
                      "uscc": "INVALID", "address": "杭州市西湖区文三路100号"})
    assert out["entity_status"] == "auto"
    assert out["entity_id"].startswith("ent-n-")
    assert "invalid-uscc-ignored:INVALID" in out["adjudication"]["notes"]


def test_same_name_same_region_distinct_identifiers_noted():
    e1 = er.process({"id": "c-1", "name": "宏发商贸有限公司",
                     "uscc": "91330106MA27XW3A1B", "address": "杭州市西湖区文三路100号"})
    e2 = er.process({"id": "c-2", "name": "宏发商贸有限公司",
                     "uscc": "91330106MA99BBBB2C", "address": "杭州市西湖区文二路55号"})
    assert e1["entity_id"] != e2["entity_id"]
    reg = read_registry()
    assert reg["entities"][e1["entity_id"]]["notes"] == ["name-clash:" + e2["entity_id"]]
    assert reg["entities"][e2["entity_id"]]["notes"] == ["name-clash:" + e1["entity_id"]]


def test_case_no_corroborates_cross_region_republication():
    a = er.process({"id": "cn-1", "name": "杭州云图科技有限公司",
                    "address": "杭州市西湖区文三路100号",
                    "case_no": "（2026）浙01执1234号", "ts": "2026-01-01"})
    b = er.process({"id": "cn-2", "name": "云图科技",
                    "address": "北京市海淀区中关村大街1号",
                    "case_no": "（2026）浙01执1234号", "ts": "2026-02-01"})
    # 案号一致且名称兼容：跨地区重复公示合并，地址冲突留痕
    assert b["entity_id"] == a["entity_id"]
    assert "case-no-corroborated" in b["matched_via"]
    ent = read_registry()["entities"][a["entity_id"]]
    assert ent["conflicts"] and ent["conflicts"][0]["policy"] == "case-no-corroborated"


def test_insufficient_identity_pends():
    out = er.process({"id": "z-1", "src": "nowhere"})
    assert out["entity_status"] == "pending"
    assert out["entity_id"] is None
    assert "insufficient-identity" in out["adjudication"]["reasons"]


# ---------- 流水线集成与旧数据 ----------
def test_registered_in_pipeline():
    from src.gazettetrack import pipeline
    assert "entity_resolve" in pipeline.CHAIN_NAMES


def test_pipeline_legacy_data(tmp_path, monkeypatch):
    """旧数据（无标识字段的 fixtures/input.json）经全链路仍可幂等落库。"""
    from src.gazettetrack import pipeline, store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(er, "REGISTRY_PATH", tmp_path / "reg.json")
    er._reset_cache()
    a = pipeline.run()
    b = pipeline.run()  # 重跑不增行
    assert a["fed"] == 4
    assert b["rows"] == a["rows"] == 3
    con = store.connect()
    try:
        rows = dict(store.all_records(con))
    finally:
        con.close()
    assert len(rows) == 3
    for data in rows.values():
        assert data["entity_status"] == "auto"
        assert data["entity_id"] and data["canonical_name"]
        assert data["adjudication"]["status"] == "auto"
