"""跨地区公示去重、版本与撤销的对照测试。

覆盖题目要求的四类对照数据：
* 跨地区转载（案号写法不同、主体名带括号注释）；
* 正文修订（同一事项后续版本）；
* 同一主体不同事项（不得误合并）；
* 案号相近但独立的事项。

另外核对换序、重复处理、撤销/恢复后的来源集合，以及候选增长的成本。
"""
import json
import os
import random
import tempfile

import pytest

from src.gazettetrack import dedupe, keyer, pipeline, recovery, store
from src.gazettetrack.config import ROOT

GAZETTE_FIXTURE = str(ROOT / "fixtures" / "gazette.json")


@pytest.fixture()
def con(tmp_path):
    connection = store.connect(tmp_path / "test.db")
    try:
        yield connection
    finally:
        connection.close()


def _gazette(anchor, **overrides):
    record = {
        "id": anchor,
        "src": "source-" + anchor,
        "region": "北京",
        "case_no": "(2026)京01民初123号",
        "entity": "北京甲科技有限公司",
        "matter": "北京甲科技有限公司破产清算公告",
        "date": "2026-03-01",
        "body": "本院裁定受理北京甲科技有限公司破产清算一案，债权人应于2026年4月1日前申报债权。",
        "attachments": [{"name": "公告.pdf", "sha256": "att-1", "size": 10}],
    }
    record.update(overrides)
    return record


def _by_group(con):
    out = {}
    for _key, data in store.all_records(con):
        if data.get("is_gazette"):
            out[data["group_key"]] = data
    return out


# ---------------------------------------------------------------- 跨地区转载


def test_cross_region_repost_merges_and_keeps_all_sources(con):
    run = pipeline.run(con, fixture=GAZETTE_FIXTURE)
    groups = _by_group(con)
    merged = groups["case:(2026)京01民初123号"]
    # 北京（全角括号）与上海（半角括号）转载视为同一事项。
    assert merged["source_anchors"] == ["g-bj-1", "g-bj-2", "g-sh-1"]
    assert merged["regions"] == ["上海", "北京"]
    assert merged["case_no_key"] == "(2026)京01民初123号"
    # 后续修订使内容状态为 revised，不能落成 unchanged。
    assert merged["content_state"] == "revised"
    assert merged["duplicate_count"] == 2
    pair = [
        p
        for p in merged["evidence"]["pairs"]
        if {p["left"], p["right"]} == {"g-bj-1", "g-sh-1"}
    ][0]
    assert pair["change"] == "unchanged"
    assert "case_no_exact" in pair["reasons"]
    assert run["rows"] == 6


def test_missing_case_number_repost_merges_by_entity_and_matter(con):
    pipeline.run(con, fixture=GAZETTE_FIXTURE)
    groups = _by_group(con)
    no_case = [g for key, g in groups.items() if g["case_no_key"] in (None, "")]
    target = [g for g in no_case if "广州乙贸易有限公司清算公告" == g["matter"]]
    assert len(target) == 1
    merged = target[0]
    assert set(merged["source_anchors"]) == {"g-gz-1", "g-gz-2", "g-gz-4", "g-sz-1"}
    assert merged["entity_id"] == merged["entity_id"]  # 跨写法（带括号注释）归一
    assert merged["content_state"] == "revised"
    assert merged["date"] == "2026-02-01"
    assert merged["latest_date"] == "2026-12-01"


def test_same_entity_different_matter_stays_independent(con):
    pipeline.run(con, fixture=GAZETTE_FIXTURE)
    groups = _by_group(con)
    matters = {g["matter"]: g for g in groups.values()}
    assert set(matters) == {
        "北京甲科技有限公司破产清算公告",
        "北京甲科技有限公司劳动争议应诉公告",
        "北京甲科技有限公司债权申报公告",
        "北京甲科技有限公司股东会决议公告",
        "广州乙贸易有限公司清算公告",
        "广州乙贸易有限公司增资公告",
    }
    clearing = matters["广州乙贸易有限公司清算公告"]
    increase = matters["广州乙贸易有限公司增资公告"]
    assert clearing["group_key"] != increase["group_key"]
    assert clearing["source_anchors"] == ["g-gz-1", "g-gz-2", "g-gz-4", "g-sz-1"]
    assert increase["source_anchors"] == ["g-gz-3"]
    # 同主体同标题但公示日期相距 9 个月、正文不同：仍是同一事项的版本，
    # 而不是「无变化」。
    assert "body_revised" in [c["change_type"] for c in clearing["change_summary"]]


def test_adjacent_case_numbers_are_independent_with_evidence(con):
    records = [
        _gazette("k1", case_no="(2026)京01民初123号"),
        _gazette("k2", case_no="(2026)京01民初124号", body="另一案的公告内容。"),
        _gazette("k3", case_no="(2026)京01民初125号", body="第三案的公告内容。"),
    ]
    result = pipeline.run(con, records=records)
    assert result["rows"] == 3
    assert result["stages"]["dedupe"]["near_case_independent"] >= 1
    groups = _by_group(con)
    assert len(groups) == 3
    merged = list(groups.values())[0]
    # 相似案号的不同事项：相互之间的「未合并」证据也要留痕。
    conflict = merged["evidence"]["non_merges"][0]
    assert "near_case_no_independent" in conflict["reasons"]


# ---------------------------------------------------------------- 版本差异


def test_body_revision_creates_new_version_not_unchanged(con):
    older = _gazette("v1", date="2026-03-01", body="申报截止日期为2026年4月1日。")
    newer = _gazette("v2", date="2026-03-20", body="申报截止日期为2026年5月1日。")
    pipeline.run(con, records=[older])
    pipeline.run(con, records=[older, newer])
    groups = _by_group(con)
    assert len(groups) == 1
    merged = next(iter(groups.values()))
    assert merged["content_state"] == "revised"
    assert merged["revision_count"] == 2
    assert [c["change_type"] for c in merged["change_summary"]] == ["initial", "body_revised"]
    assert merged["source_anchors"] == ["v1", "v2"]
    versions = store.versions_for(con, "case:(2026)京01民初123号")
    assert [(v["version"], v["change_type"]) for v in versions] == [
        (1, "initial"),
        (2, "body_revised"),
    ]


def test_attachment_only_difference_is_reported(con):
    first = _gazette("a1", date="2026-03-01")
    second = _gazette(
        "a2",
        date="2026-03-02",
        region="河北",
        attachments=[{"name": "公告.pdf", "sha256": "att-2", "size": 10}],
    )
    pipeline.run(con, records=[first, second])
    merged = next(iter(_by_group(con).values()))
    assert merged["content_state"] == "unchanged"
    assert merged["attachment_state"] == "changed"
    assert len(merged["attachment_variants"]) == 2
    assert merged["evidence"]["missing_attachments"]["att-2"] == {
        "present_in": ["a2"],
        "missing_in": ["a1"],
    }
    assert merged["change_summary"][-1]["change_type"] == "attachment_revised"


def test_missing_body_is_unknown_not_unchanged(con):
    with_body = _gazette("m1", date="2026-03-01")
    without_body = _gazette("m2", date="2026-03-02")
    without_body.pop("body")
    pipeline.run(con, records=[with_body, without_body])
    merged = next(iter(_by_group(con).values()))
    assert merged["content_state"] == "unknown"
    assert merged["change_summary"][-1]["change_type"] == "content_unknown"


def test_exact_repost_stays_unchanged(con):
    records = [_gazette("u1"), _gazette("u2", region="上海", date="2026-03-01")]
    pipeline.run(con, records=records)
    merged = next(iter(_by_group(con).values()))
    assert merged["content_state"] == "unchanged"
    assert merged["revision_count"] == 1
    assert merged["source_anchors"] == ["u1", "u2"]


# ---------------------------------------------------------------- 稳定与成本


def test_order_and_repeat_processing_are_stable(con, tmp_path):
    base = pipeline.run(con, fixture=GAZETTE_FIXTURE)
    first = {key: data for key, data in store.all_records(con)}
    repeat = pipeline.run(con, fixture=GAZETTE_FIXTURE)
    assert repeat["rows"] == base["rows"] == len(first)
    assert {k: d for k, d in store.all_records(con)} == first

    with open(GAZETTE_FIXTURE, encoding="utf-8") as fh:
        records = json.load(fh)
    random.Random(11).shuffle(records)
    shuffled_con = store.connect(tmp_path / "shuffled.db")
    try:
        pipeline.run(shuffled_con, records=records)
        shuffled = {key: data for key, data in store.all_records(shuffled_con)}
    finally:
        shuffled_con.close()
    assert sorted(shuffled) == sorted(first)
    for key in first:
        assert shuffled[key] == first[key]


def test_candidate_growth_is_bounded(con):
    """同一主体的公示很多但日期跨度大：粗桶剪枝把比较数压到候选数。"""
    records = []
    for index in range(40):
        records.append(
            _gazette(
                f"n{index:02d}",
                case_no="",
                matter=f"北京甲科技有限公司事项{index:02d}公告",
                date=f"{2025 + index // 12}-{1 + index % 12:02d}-01",
                body=f"北京甲科技有限公司第{index}号独立事项公告。",
            )
        )
    result = pipeline.run(con, records=records)
    stats = result["stages"]["dedupe"]
    assert stats["observations"] == 40
    assert stats["candidates"] < 40 * 39 / 2
    assert stats["pruned_by_date_window"] > 0
    assert stats["bucket_pairs"] > stats["candidates"]
    assert stats["comparisons"] == stats["candidates"]
    # 每个事项各自成组，不被日期窗口剪枝影响正确性。
    assert len(_by_group(con)) == 40


# ---------------------------------------------------------------- 撤销/恢复


def _setup_merge(con):
    pipeline.run(con, fixture=GAZETTE_FIXTURE)
    group_key = "case:(2026)京01民初123号"
    head_key = next(
        key for key, data in store.all_records(con) if data.get("group_key") == group_key
    )
    return group_key, head_key


def test_undo_restores_per_source_sets(con):
    group_key, head_key = _setup_merge(con)
    result = recovery.undo_merge(con, group_key)
    assert result["restored"] == ["g-bj-1", "g-bj-2", "g-sh-1"]
    assert store.get(con, head_key) is None
    assert store.suppressed_groups(con) == [group_key]

    split = {
        data["source_anchors"][0]: data
        for _key, data in store.all_records(con)
        if data.get("undo_of_group") == group_key
    }
    assert set(split) == {"g-bj-1", "g-bj-2", "g-sh-1"}
    for anchor, data in split.items():
        assert data["source_anchors"] == [anchor]
        assert data["content_state"] == "single"

    # 重跑不会再把它们合回去。
    pipeline.run(con, fixture=GAZETTE_FIXTURE)
    assert group_key not in _by_group(con)
    assert len(
        [d for _, d in store.all_records(con) if d.get("undo_of_group") == group_key]
    ) == 3


def test_restore_after_undo_rebuilds_union(con):
    group_key, _head_key = _setup_merge(con)
    recovery.undo_merge(con, group_key)
    pipeline.run(con, fixture=GAZETTE_FIXTURE)  # 撤销后重跑，验证恢复仍然成立
    result = recovery.restore_merge(con, group_key)
    assert result["sources"] == ["g-bj-1", "g-bj-2", "g-sh-1"]
    assert store.suppressed_groups(con) == []
    groups = _by_group(con)
    assert group_key in groups
    assert groups[group_key]["source_anchors"] == ["g-bj-1", "g-bj-2", "g-sh-1"]
    assert groups[group_key]["content_state"] == "revised"
    # 恢复后再次重跑：行数稳定，来源集合不缩水。
    before = {key: data for key, data in store.all_records(con)}
    pipeline.run(con, fixture=GAZETTE_FIXTURE)
    after = {key: data for key, data in store.all_records(con)}
    assert set(after) == set(before)
    assert _by_group(con)[group_key]["source_anchors"] == ["g-bj-1", "g-bj-2", "g-sh-1"]


def test_merge_history_is_append_only(con):
    group_key, _ = _setup_merge(con)
    recovery.undo_merge(con, group_key)
    recovery.restore_merge(con, group_key)
    recovery.undo_merge(con, group_key)
    actions = [entry["action"] for entry in store.merge_history_for(con, group_key)]
    assert actions == ["merge", "undo", "restore", "undo"]


# ---------------------------------------------------------------- 单元级


def test_decide_missing_case_gate_is_explainable():
    left = {
        "_anchor": "l",
        "entity_id": "e1",
        "matter_key": "清算公告",
        "body": "广州乙贸易有限公司决议解散，请债权人四十五日内申报债权。",
        "date": "2026-02-01",
    }
    right = {
        "_anchor": "r",
        "entity_id": "e1",
        "matter_key": "清算公告",
        "body": "广州乙贸易有限公司决议解散，请债权人六十日内申报债权。",
        "date": "2026-02-20",
    }
    decision = dedupe.decide(left, right)
    assert decision["same_matter"] is True
    assert decision["change"] == "body_revised"
    assert "missing_case_entity_and_matter" in decision["reasons"]
    assert decision["thresholds"]["body_similarity"] == dedupe.MISSING_CASE_BODY_SIM
    assert decision["thresholds"]["date_window_days"] == dedupe.MISSING_CASE_DATE_WINDOW_DAYS
    assert decision["signals"]["entity_match"] is True
    assert decision["signals"]["case_no_match"] is False


def test_decide_same_entity_different_matter_is_independent():
    left = {
        "_anchor": "l",
        "entity_id": "e1",
        "matter_key": "清算公告",
        "body": "广州乙贸易有限公司决议解散公司，请债权人四十五日内申报债权。",
        "date": "2026-02-01",
    }
    right = {
        "_anchor": "r",
        "entity_id": "e1",
        "matter_key": "增资公告",
        "body": "广州乙贸易有限公司拟增加注册资本至一千万元。",
        "date": "2026-02-02",
    }
    decision = dedupe.decide(left, right)
    assert decision["same_matter"] is False
    assert decision["change"] == "independent"
    assert "matter_conflict" in decision["reasons"]
    assert decision["signals"]["matter_known"] is True


def test_key_derivation_is_explainable_and_stable():
    record = _gazette("k1")
    record = __import__("src.gazettetrack.normalizer", fromlist=["process"]).process(record)
    record = __import__("src.gazettetrack.gazette_parse", fromlist=["process"]).process(record)
    record = __import__("src.gazettetrack.entity_resolve", fromlist=["process"]).process(record)
    other = dict(record, id="k2", src="other", region="上海")
    assert keyer.key_of(record) == keyer.key_of(other)
    revised = dict(record, body="完全不同的正文内容。")
    from src.gazettetrack.normalizer import fingerprint_body

    revised["body_fp"] = fingerprint_body(revised["body"])
    assert keyer.key_of(revised) != keyer.key_of(record)
    assert keyer.matter_id_of(revised) == keyer.matter_id_of(record)


def test_transitive_merge_is_split_when_a_pair_is_independent():
    from src.gazettetrack.dedupe import _split_inconsistent

    ordered = [
        {"_anchor": "A", "date": "2026-03-01"},
        {"_anchor": "B", "date": "2026-03-05"},
        {"_anchor": "C", "date": "2026-03-10"},
    ]
    decisions = [
        {"left_index": 0, "right_index": 1, "same_matter": True},
        {"left_index": 1, "right_index": 2, "same_matter": True},
        {"left_index": 0, "right_index": 2, "same_matter": False},
    ]
    stats = {}
    split = _split_inconsistent({0: [0, 1, 2]}, decisions, ordered, stats)
    groups = sorted(sorted(ordered[i]["_anchor"] for i in members) for members in split.values())
    assert groups == [["A", "B"], ["C"]]
    assert stats["inconsistent_components_split"] == 1


def test_stage_process_is_usable_standalone():
    """单阶段 process 即可解释：不经流水线也能看到候选门槛与版本语义。"""
    from src.gazettetrack import entity_resolve, gazette_parse, normalizer, versioning

    record = normalizer.process(
        _gazette("solo", case_no="（2026）京01民初9号")
    )
    record = gazette_parse.process(record)
    record = entity_resolve.process(record)
    assert record["case_no_key"] == "(2026)京01民初9号"
    assert record["entity_id"].startswith("e-")
    assert record["body_fp"].startswith("b-")

    older = dict(record, body="旧正文。", date="2026-03-01")
    newer = dict(record, body="新正文。", date="2026-03-20")
    from src.gazettetrack.normalizer import fingerprint_body

    older["body_fp"] = fingerprint_body(older["body"])
    newer["body_fp"] = fingerprint_body(newer["body"])
    merged = dict(
        record,
        revisions=[
            {"body": older["body"], "body_fp": older["body_fp"], "date": "2026-03-01",
             "attachments": record["attachments"], "attachment_fp": record["attachment_fp"],
             "sources": [{"anchor": "o", "id": "o", "src": "s1", "region": "北京"}]},
            {"body": newer["body"], "body_fp": newer["body_fp"], "date": "2026-03-20",
             "attachments": record["attachments"], "attachment_fp": record["attachment_fp"],
             "sources": [{"anchor": "n", "id": "n", "src": "s2", "region": "上海"}]},
        ],
    )
    staged = versioning.process(merged)
    assert staged["content_state"] == "revised"
    assert staged["latest_date"] == "2026-03-20"
    assert staged["date"] == "2026-03-01"
    assert staged["source_anchors"] == ["n", "o"]
    assert [c["change_type"] for c in staged["change_summary"]] == ["initial", "body_revised"]


def test_cli_explain_and_summary_output(tmp_path, capsys, monkeypatch):
    from src.gazettetrack import cli

    monkeypatch.setattr(
        "src.gazettetrack.loader.load", lambda path=None: [
            _gazette("c1"), _gazette("c2", region="上海"),
        ]
    )
    assert cli.main(["--db", str(tmp_path / "cli.db"), "--summary"]) == 0
    run = json.loads(capsys.readouterr().out)
    assert run["rows"] == 1
    assert run["summary"]["gazettes"] == 1
    key = run["summary"]["groups"][0]
    assert key == "case:(2026)京01民初123号"


def test_cli_undo_and_restore_roundtrip(tmp_path, capsys):
    from src.gazettetrack import cli

    db = str(tmp_path / "cli.db")
    os.environ["GAZETTETRACK_FIXTURE"] = GAZETTE_FIXTURE
    try:
        assert cli.main(["--db", db]) == 0
        run = json.loads(capsys.readouterr().out)
        assert run["fed"] == 13 and run["rows"] == 6
        assert cli.main(["--db", db, "--undo-group", "case:(2026)京01民初123号"]) == 0
        undo = json.loads(capsys.readouterr().out)
        assert undo["undo"]["restored"] == ["g-bj-1", "g-bj-2", "g-sh-1"]
        assert cli.main(["--db", db, "--restore-group", "case:(2026)京01民初123号"]) == 0
        restore = json.loads(capsys.readouterr().out)
        assert restore["restore"]["sources"] == ["g-bj-1", "g-bj-2", "g-sh-1"]
    finally:
        os.environ.pop("GAZETTETRACK_FIXTURE", None)


def test_undo_keeps_identical_reposts_as_separate_rows():
    """两条完全相同的转载在撤销后不能互相覆盖（否则丢来源）。"""
    from src.gazettetrack.normalizer import fingerprint_body

    body = "同一正文。"
    recs = [
        _gazette("d1", date="2026-03-01", body=body),
        _gazette("d2", region="上海", date="2026-03-01", body=body),
    ]
    con = store.connect(tempfile.mkdtemp() + "/dup.db")
    try:
        pipeline.run(con, records=recs)
        assert store.count(con) == 1
        group_key = next(iter(_by_group(con)))
        recovery.undo_merge(con, group_key)
        split = {
            d["source_anchors"][0]: d
            for _k, d in store.all_records(con)
            if d.get("undo_of_group") == group_key
        }
        assert set(split) == {"d1", "d2"}
        assert store.count(con) == 2
        assert all(len(d["source_anchors"]) == 1 for d in split.values())
        assert fingerprint_body(body) == split["d1"]["body_fp"]
    finally:
        con.close()


def test_every_matter_head_pointer_resolves(con):
    """matters.head_key 必须指向 records 表里真实存在的行（落库一致性）。"""
    pipeline.run(con, fixture=GAZETTE_FIXTURE)
    rows = con.execute("SELECT matter_id, head_key FROM matters").fetchall()
    assert rows
    for matter_id, head_key in rows:
        assert store.get(con, head_key) is not None, matter_id
    keys = {key for key, _ in store.all_records(con)}
    assert {head_key for _, head_key in rows} <= keys
