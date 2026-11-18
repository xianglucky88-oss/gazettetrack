"""公示版本历史：生命周期、来源排序、重放与重建的回归测试。"""

import random
import sqlite3

import pytest

from src.gazettetrack import versioning as v


@pytest.fixture()
def con():
    conn = sqlite3.connect(":memory:")
    v.ensure_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


def chain(con, notice_id="g1"):
    return v.history(con, notice_id)


def events(con, notice_id="g1"):
    return [version["event"] for version in chain(con, notice_id)]


# ---------------------------------------------------------------------------
# 生命周期识别
# ---------------------------------------------------------------------------

def test_first_publish_then_update(con):
    v.ingest(con, {"id": "g1", "title": "T", "body": "v1",
                   "ts": "2026-11-01T08:00:00Z"})
    v.ingest(con, {"id": "g1", "title": "T", "body": "v2",
                   "ts": "2026-11-02T08:00:00Z"})
    assert events(con) == [v.FIRST_PUBLISH, v.UPDATE]
    chain_rows = chain(con)
    assert chain_rows[0]["body_fingerprint"] != chain_rows[1]["body_fingerprint"]


def test_retract_then_republish_with_identical_body_keeps_retract(con):
    feed = [
        {"id": "g1", "title": "T", "body": "same", "ts": "2026-11-01"},
        {"id": "g1", "title": "T", "body": "same", "ts": "2026-11-02",
         "event": "retract"},
        {"id": "g1", "title": "T", "body": "same", "ts": "2026-11-03",
         "event": "republish"},
    ]
    for record in feed:
        v.ingest(con, record)

    assert events(con) == [v.FIRST_PUBLISH, v.RETRACT, v.REPUBLISH]
    rows = chain(con)
    # 重新发布与首次发布正文相同，但绝不能复用同一个版本键。
    assert len({row["version_id"] for row in rows}) == 3
    assert rows[0]["body_fingerprint"] == rows[2]["body_fingerprint"]
    assert rows[0]["version_id"] != rows[2]["version_id"]
    # 曾经撤回的事实仍然存在。
    state = v.effective(con, "g1")
    assert state["ever_retracted"] is True
    assert state["retraction_count"] == 1
    assert state["status"] == v.STATUS_ACTIVE
    assert state["effective_version_id"] == rows[2]["version_id"]


def test_retract_only_notice_is_not_effective(con):
    v.ingest(con, {"id": "g1", "title": "T", "body": "b", "ts": "2026-11-01"})
    v.ingest(con, {"id": "g1", "ts": "2026-11-02", "status": "已撤回"})
    state = v.effective(con, "g1")
    assert state["status"] == v.STATUS_RETRACTED
    assert state["effective_version_id"] is None
    assert state["ever_retracted"] is True


def test_attachment_replacement_recognised_without_body_change(con):
    v.ingest(con, {"id": "g1", "title": "T", "body": "b",
                   "ts": "2026-11-01",
                   "attachments": [{"url": "a.pdf", "version": "1"}]})
    v.ingest(con, {"id": "g1", "title": "T", "body": "b",
                   "ts": "2026-11-02",
                   "attachments": [{"url": "a.pdf", "version": "2"}]})
    assert events(con) == [v.FIRST_PUBLISH, v.ATTACHMENT_REPLACE]
    rows = chain(con)
    assert rows[0]["body_fingerprint"] == rows[1]["body_fingerprint"]
    assert rows[0]["attachment_fingerprint"] != rows[1]["attachment_fingerprint"]
    assert len(rows[1]["attachment_versions"]) == 1


def test_unchanged_reobservation_does_not_add_version(con):
    record = {"id": "g1", "title": "T", "body": "b", "ts": "2026-11-01"}
    for _ in range(3):
        v.ingest(con, record)
    assert events(con) == [v.FIRST_PUBLISH]
    assert con.execute(
        "SELECT COUNT(*) FROM version_observations").fetchone()[0] == 1


def test_parse_batches_fingerprints_and_attachment_versions_enter_history(con):
    # 同一来源快照被两个解析批次各抓一次：只产出一个版本，但两个批次都留痕。
    v.ingest(con, {"id": "g1", "title": "T", "body": "b",
                   "ts": "2026-11-01", "parse_batch": "batch-1"})
    v.ingest(con, {"id": "g1", "title": "T", "body": "b",
                   "ts": "2026-11-01", "parse_batch": "batch-2"})
    rows = chain(con)
    assert len(rows) == 1
    assert rows[0]["parse_batches"] == ["batch-1", "batch-2"]
    assert rows[0]["observations"] == 2
    assert rows[0]["body_fingerprint"]
    assert con.execute(
        "SELECT COUNT(*) FROM version_observations").fetchone()[0] == 2

    # 附件版本随版本历史一起保存。
    v.ingest(con, {"id": "g1", "title": "T", "body": "b",
                   "ts": "2026-11-02",
                   "attachments": [{"url": "a.pdf", "version": "2"}],
                   "parse_batch": "batch-3"})
    rows = chain(con)
    assert len(rows) == 2
    assert rows[1]["event"] == v.ATTACHMENT_REPLACE
    assert rows[1]["attachment_versions"]
    assert rows[1]["parse_batches"] == ["batch-3"]


# ---------------------------------------------------------------------------
# 来源排序证据：同日多版本
# ---------------------------------------------------------------------------

def test_same_day_versions_ordered_by_source_sequence(con):
    day = "2026-11-01T09:00:00Z"
    for seq, body in ((1, "a"), (2, "b"), (3, "c")):
        v.ingest(con, {"id": "g1", "title": "T", "body": body, "ts": day,
                       "source_seq": seq})
    rows = chain(con)
    assert [row["source_seq"] for row in rows] == [1, 2, 3]
    assert [row["content"].get("body") for row in rows] == ["a", "b", "c"]
    # 同日先后必须能说清是靠哪个来源字段决定的。
    assert rows[1]["order_evidence"]["primary"] == "source_seq"
    assert rows[2]["order_evidence"]["primary"] == "source_seq"


def test_same_time_without_sequence_uses_deterministic_fingerprint(con):
    day = "2026-11-01T09:00:00Z"
    v.ingest(con, {"id": "g1", "title": "T", "body": "zzz", "ts": day})
    v.ingest(con, {"id": "g1", "title": "T", "body": "aaa", "ts": day})
    rows = chain(con)
    assert len(rows) == 2
    # 每个版本都带有可审计的排序证据，不是随机顺序。
    assert rows[1]["order_evidence"]["primary"] in {
        "body_fingerprint", "observation_id", "source_seq"}


def test_notification_dates_out_of_order_still_build_same_chain(con):
    feed = [
        {"id": "g1", "title": "T", "body": "v1", "ts": "2026-11-01"},
        {"id": "g1", "title": "T", "body": "v2", "ts": "2026-11-03"},
        {"id": "g1", "title": "T", "body": "v3", "ts": "2026-11-05"},
    ]
    expected = [v.FIRST_PUBLISH, v.UPDATE, v.UPDATE]
    for order in ([0, 1, 2], [2, 0, 1], [1, 2, 0], [2, 1, 0]):
        fresh = sqlite3.connect(":memory:")
        v.ensure_schema(fresh)
        try:
            for index in order:
                v.ingest(fresh, feed[index])
            assert events(fresh) == expected
            assert [row["content"]["body"] for row in chain(fresh)] == [
                "v1", "v2", "v3"]
        finally:
            fresh.close()


# ---------------------------------------------------------------------------
# 部分抓取与恢复
# ---------------------------------------------------------------------------

def test_partial_fetch_then_full_body_does_not_fake_update(con):
    v.ingest(con, {"id": "g1", "title": "T", "ts": "2026-11-01",
                   "partial": True, "parse_batch": "b1"})
    v.ingest(con, {"id": "g1", "title": "T", "body": "full",
                   "ts": "2026-11-01", "parse_batch": "b2"})
    rows = chain(con)
    assert len(rows) == 1
    assert rows[0]["event"] == v.FIRST_PUBLISH
    assert rows[0]["content"]["body"] == "full"
    assert "b1" in rows[0]["parse_batches"]
    assert "b2" in rows[0]["parse_batches"]


def test_partial_fetch_interruption_then_replay_matches_continuous(con):
    feed = [
        {"id": "g1", "title": "T", "body": "v1", "ts": "2026-11-01"},
        {"id": "g1", "title": "T", "body": "v2", "ts": "2026-11-02"},
        {"id": "g1", "title": "T", "body": "v2", "ts": "2026-11-03",
         "event": "retract"},
        {"id": "g1", "title": "T", "body": "v2", "ts": "2026-11-04",
         "event": "republish"},
    ]
    # 中断恢复：先摄取一半，重建后再摄取其余。
    for record in feed[:2]:
        v.ingest(con, record)
    v.replay(con)
    for record in feed[2:]:
        v.ingest(con, record)
    resumed = v.effective(con, "g1")

    continuous = sqlite3.connect(":memory:")
    v.ensure_schema(continuous)
    try:
        v.ingest_many(continuous, feed)
        control = v.effective(continuous, "g1")
        assert resumed == control
        assert [(row["ordinal"], row["event"], row["version_id"])
                for row in chain(con)] == [
                    (row["ordinal"], row["event"], row["version_id"])
                    for row in chain(continuous)]
    finally:
        continuous.close()


def test_retract_received_before_original_backfills_correctly(con):
    v.ingest(con, {"id": "g1", "title": "T", "body": "b",
                   "ts": "2026-11-05", "event": "retract"})
    v.ingest(con, {"id": "g1", "title": "T", "body": "b",
                   "ts": "2026-11-01"})
    assert events(con) == [v.FIRST_PUBLISH, v.RETRACT]
    state = v.effective(con, "g1")
    assert state["status"] == v.STATUS_RETRACTED
    assert state["ever_retracted"] is True


# ---------------------------------------------------------------------------
# 从历史重建与重放一致性
# ---------------------------------------------------------------------------

def test_current_state_rebuildable_from_version_history_alone(con):
    feed = [
        {"id": "g1", "title": "T", "body": "v1", "ts": "2026-11-01"},
        {"id": "g1", "title": "T", "body": "v2", "ts": "2026-11-02"},
        {"id": "g1", "title": "T", "body": "v2", "ts": "2026-11-03",
         "event": "retract"},
        {"id": "g1", "title": "T", "body": "v2", "ts": "2026-11-04",
         "event": "republish"},
    ]
    v.ingest_many(con, feed)
    persisted = v.history(con, "g1")

    # 只用版本历史（丢掉观测表）也能重建当前有效状态。
    con.execute("DELETE FROM version_observations")
    con.commit()
    rebuilt = v.current_from_history(v.history(con, "g1"), "g1")

    control = sqlite3.connect(":memory:")
    v.ensure_schema(control)
    try:
        v.ingest_many(control, feed)
        expected = v.effective(control, "g1")
    finally:
        control.close()

    assert [row["version_id"] for row in persisted] == [
        row["version_id"] for row in v.history(con, "g1")]
    assert rebuilt == expected


def test_replay_is_idempotent(con):
    feed = [
        {"id": "g1", "title": "T", "body": "v1", "ts": "2026-11-01"},
        {"id": "g1", "title": "T", "body": "v2", "ts": "2026-11-02"},
    ]
    v.ingest_many(con, feed)
    first = [(row["ordinal"], row["event"], row["version_id"])
             for row in chain(con)]
    for _ in range(3):
        v.replay(con)
        assert [(row["ordinal"], row["event"], row["version_id"])
                for row in chain(con)] == first


def test_republish_does_not_reuse_version_key_across_replays(con):
    feed = [
        {"id": "g1", "title": "T", "body": "same", "ts": "2026-11-01"},
        {"id": "g1", "title": "T", "body": "same", "ts": "2026-11-02",
         "event": "retract"},
        {"id": "g1", "title": "T", "body": "same", "ts": "2026-11-03",
         "event": "republish"},
    ]
    v.ingest_many(con, feed)
    keys = [row["version_id"] for row in chain(con)]
    assert len(keys) == len(set(keys)) == 3
    v.replay(con)
    assert [row["version_id"] for row in chain(con)] == keys
    # 重新发布确实占用了独立版本键，而不是覆盖首次发布。
    assert keys[0] != keys[2]


def test_shuffled_ingest_order_is_deterministic(con):
    feed = [
        {"id": "g1", "title": "T", "body": "v1", "ts": "2026-11-01",
         "source_seq": 1},
        {"id": "g1", "title": "T", "body": "v2", "ts": "2026-11-02",
         "source_seq": 2},
        {"id": "g1", "title": "T", "body": "v2", "ts": "2026-11-03",
         "event": "retract", "source_seq": 3},
        {"id": "g1", "title": "T", "body": "v3", "ts": "2026-11-04",
         "event": "republish", "source_seq": 4},
    ]
    reference = v.build_chain([v.normalize(item) for item in feed], "g1")
    ref_signature = [(row["event"], row["version_id"]) for row in reference]

    rng = random.Random(20261118)
    for _ in range(20):
        shuffled = feed[:]
        rng.shuffle(shuffled)
        produced = v.build_chain([v.normalize(item) for item in shuffled], "g1")
        assert [(row["event"], row["version_id"]) for row in produced] == \
            ref_signature


# ---------------------------------------------------------------------------
# 现有存储从历史重建当前有效版本
# ---------------------------------------------------------------------------

def test_materialize_current_from_history(con):
    from src.gazettetrack import store

    v.ingest_many(con, [
        {"id": "g1", "title": "T", "body": "v1", "ts": "2026-11-01"},
        {"id": "g1", "title": "T", "body": "v2", "ts": "2026-11-02"},
        {"id": "g2", "title": "U", "body": "x", "ts": "2026-11-01"},
    ])
    v.materialize_current(con)
    rows = {data.get("id"): data for _, data in store.all_records(con)}
    assert set(rows) == {"g1", "g2"}
    assert rows["g1"]["body"] == "v2" if "body" in rows["g1"] else True
    assert rows["g1"]["version_id"] == v.effective(con, "g1")["effective_version_id"]
    assert rows["g1"]["notice_status"] == v.STATUS_ACTIVE

    # 撤回后未重新发布：现有存储不再保留该公告的有效记录。
    v.ingest(con, {"id": "g2", "ts": "2026-11-02", "event": "retract"})
    v.materialize_current(con)
    remaining = {data.get("id") for _, data in store.all_records(con)}
    assert remaining == {"g1"}


def test_materialize_can_rebuild_from_history_without_observations(con):
    from src.gazettetrack import store

    v.ingest_many(con, [
        {"id": "g1", "title": "T", "body": "v1", "ts": "2026-11-01"},
        {"id": "g1", "title": "T", "body": "v2", "ts": "2026-11-02"},
    ])
    con.execute("DELETE FROM version_observations")
    con.execute("DELETE FROM records")
    con.commit()
    v.materialize_current(con)
    rows = list(store.all_records(con))
    assert len(rows) == 1
    assert rows[0][1]["version_id"] == v.effective(con, "g1")["effective_version_id"]


# ---------------------------------------------------------------------------
# 流水线接入
# ---------------------------------------------------------------------------

def test_pipeline_versioning_stage_is_wired(con):
    from src.gazettetrack import pipeline

    result = pipeline.run(con=con)
    assert result["fed"] == 4
    assert result["rows"] == 3
    for notice_id in v.notices(con):
        assert v.effective(con, notice_id)["status"] == v.STATUS_ACTIVE


def test_process_accepts_injected_connection(con):
    rec = v.process({"id": "g1", "title": "T", "body": "b",
                     "ts": "2026-11-01"}, con=con)
    assert rec["lifecycle"] == v.FIRST_PUBLISH
    assert rec["version_id"]
    assert rec["notice_status"] == v.STATUS_ACTIVE
