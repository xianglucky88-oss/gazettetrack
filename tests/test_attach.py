"""附件处理阶段测试：证据归档、分页、压缩包重名、部分失败与重试恢复。

可在有 pytest 的环境直接运行；无 pytest 时用 ``python3 -m tests.attach_runner``
（标准库实现，零依赖）运行同一批用例。
"""
import io
import json
import tempfile
import zipfile
from pathlib import Path

from src.gazettetrack import attach, attach_store
from src.gazettetrack.attach_fetch import FetchError, OfflineFetcher

PDF_MAGIC = b"%PDF-1.7\n"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ZIP_MAGIC = b"PK\x03\x04"


# ---------- 夹具辅助 ----------

def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


class Env:
    def __init__(self, max_missing_grace=3, max_hard_retries=2):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.fetcher = OfflineFetcher()
        self.ctx = attach.AttachContext(
            fetcher=self.fetcher, archive_dir=self.dir / "archive",
            db_path=self.dir / "attach.db",
            max_missing_grace=max_missing_grace,
            max_hard_retries=max_hard_retries, run_id="run-1",
        )
        self.con = self.ctx.con

    def cleanup(self):
        self.ctx.close()
        self.tmp.cleanup()

    def record(self, aid="a-1", version="v1", specs=None):
        return {"announcement_id": aid, "version": version,
                "attachments": specs or []}

    def slot(self, con, aid, version, ref, page=None):
        return attach_store.get_slot(
            con, attach.slot_key_of(aid, version, ref, page))


# ---------- 1. 基本 happy path：PDF/图片关联到正确公告，URL/页码/区域/解析状态持久化 ----------

def test_happy_path_pdf_and_image():
    env = Env()
    try:
        pdf = PDF_MAGIC + b"announcement body bytes"
        png = PNG_MAGIC + b"image bytes"
        env.fetcher.register("https://src/a-1/doc.pdf", pdf, "doc.pdf")
        env.fetcher.register("https://src/a-1/seal.png", png, "seal.png")
        rec = env.record(specs=[
            {"ref": "att-1", "name": "doc.pdf", "kind": "pdf",
             "url": "https://src/a-1/doc.pdf",
             "region": {"x": 10, "y": 20, "w": 300, "h": 400}},
            {"ref": "att-2", "name": "seal.png", "kind": "image",
             "url": "https://src/a-1/seal.png"},
        ])
        out = attach.process_attachments(rec, env.ctx)

        assert out["attach_status"] == "complete"
        assert out["_stage_attach"] == "ok"
        for item in out["attach_results"]:
            assert item["status"] == "verified"
            assert item["parse_status"] == "parsed"
            assert len(item["digests"]) == 1

        s1 = env.slot(env.con, "a-1", "v1", "att-1")
        assert s1["announcement_id"] == "a-1" and s1["version"] == "v1"
        assert s1["url"] == "https://src/a-1/doc.pdf"
        assert s1["declared_name"] == "doc.pdf"
        assert s1["kind"] == "pdf"
        assert json.loads(s1["region"]) == {"x": 10, "y": 20, "w": 300, "h": 400}
        assert s1["page_no"] is None and s1["parse_status"] == "parsed"

        # 证据按摘要归档，原文可读，blob 路径内容寻址
        digest = out["attach_results"][0]["digests"][0]
        ev = attach_store.get_evidence(env.con, digest)
        assert ev["observed_name"] == "doc.pdf" and ev["kind"] == "pdf"
        assert attach_store.read_evidence_bytes(
            env.con, digest, env.ctx.blob_root) == pdf
        assert (env.dir / "archive" / "blobs" / ev["blob_path"]).read_bytes() == pdf
    finally:
        env.cleanup()


# ---------- 2. 文件名不变内容替换：识别为新证据，旧证据可读，历史绑定不被覆盖 ----------

def test_same_name_replacement_keeps_history():
    env = Env()
    try:
        old_pdf = PDF_MAGIC + b"version one"
        new_pdf = PDF_MAGIC + b"version two content"
        url = "https://src/a-1/doc.pdf"
        env.fetcher.register(url, old_pdf, "doc.pdf")

        out1 = attach.process_attachments(env.record(version="v1", specs=[
            {"ref": "att-1", "name": "doc.pdf", "kind": "pdf", "url": url}]), env.ctx)
        d1 = out1["attach_results"][0]["digests"][0]

        # 公告升版，附件文件名与 URL 完全相同，内容已被替换
        env.fetcher.register(url, new_pdf, "doc.pdf")
        env.ctx.run_id = "run-2"
        out2 = attach.process_attachments(env.record(version="v2", specs=[
            {"ref": "att-1", "name": "doc.pdf", "kind": "pdf", "url": url}]), env.ctx)
        d2 = out2["attach_results"][0]["digests"][0]

        assert d1 != d2  # 内容变了 -> 新证据
        # 旧证据仍在且原文可读
        assert attach_store.read_evidence_bytes(
            env.con, d1, env.ctx.blob_root) == old_pdf
        assert attach_store.get_evidence(env.con, d1) is not None
        assert attach_store.get_evidence(env.con, d2)["observed_name"] == "doc.pdf"
        # 两个公告版本各自关联自己的证据
        assert env.slot(env.con, "a-1", "v1", "att-1")["status"] == "verified"
        assert env.slot(env.con, "a-1", "v2", "att-1")["status"] == "verified"
        b1 = attach_store.bindings_of(env.con, attach.slot_key_of("a-1", "v1", "att-1", None))
        assert [b["digest"] for b in b1] == [d1]
    finally:
        env.cleanup()


# ---------- 3. 重复下载相同内容：幂等复用，不新增 blob/绑定行 ----------

def test_repeat_download_same_content_is_idempotent():
    env = Env()
    try:
        pdf = PDF_MAGIC + b"stable"
        url = "https://src/a-1/doc.pdf"
        env.fetcher.register(url, pdf, "doc.pdf")
        specs = [{"ref": "att-1", "name": "doc.pdf", "kind": "pdf", "url": url}]
        out1 = attach.process_attachments(env.record(specs=specs), env.ctx)
        digest = out1["attach_results"][0]["digests"][0]
        blob_path = attach_store.get_evidence(env.con, digest)["blob_path"]

        # 强制刷新下载（默认 verified 会跳过），同内容不应产生新证据或新绑定
        env.ctx.run_id = "run-2"
        out2 = attach.process_attachments(env.record(specs=specs), env.ctx,
                                          refresh=True)
        assert out2["attach_results"][0]["digests"] == [digest]
        assert attach_store.get_evidence(env.con, digest)["blob_path"] == blob_path
        bindings = attach_store.bindings_of(
            env.con, attach.slot_key_of("a-1", "v1", "att-1", None))
        assert len(bindings) == 1
        # 两次尝试都有流水
        attempts = attach_store.attempts_of(
            env.con, attach.slot_key_of("a-1", "v1", "att-1", None))
        assert [a["result"] for a in attempts] == ["ok", "ok"]
        # blob 实体只有一份（目录不计）
        blob_files = sorted(p for p in (env.dir / "archive" / "blobs").rglob("*")
                            if p.is_file())
        assert blob_files == [
            env.dir / "archive" / "blobs" / blob_path]
    finally:
        env.cleanup()


# ---------- 3b. 同槽位刷新下载到变化内容（无声明摘要）：追加新绑定，历史版本不覆盖 ----------

def test_refresh_changed_content_appends_binding_on_same_slot():
    env = Env()
    try:
        v1 = PDF_MAGIC + b"first"
        v2 = PDF_MAGIC + b"second-different"
        url = "https://src/a-1/doc.pdf"
        env.fetcher.register(url, v1, "doc.pdf")
        specs = [{"ref": "att-1", "name": "doc.pdf", "kind": "pdf", "url": url}]
        out1 = attach.process_attachments(env.record(specs=specs), env.ctx)
        d1 = out1["attach_results"][0]["digests"][0]

        # 源站在同 URL/同文件名替换了字节，强制刷新
        env.fetcher.register(url, v2, "doc.pdf")
        env.ctx.run_id = "run-2"
        out2 = attach.process_attachments(env.record(specs=specs), env.ctx,
                                          refresh=True)
        digests = out2["attach_results"][0]["digests"]
        assert digests == [d1, attach_store_sha(v2)]  # 绑定只追加
        # 两份证据都在、都可读；blob 互不覆盖
        for d, raw in ((d1, v1), (digests[1], v2)):
            assert attach_store.read_evidence_bytes(
                env.con, d, env.ctx.blob_root) == raw
        slot_key = attach.slot_key_of("a-1", "v1", "att-1", None)
        bound_notes = [b["note"] for b in attach_store.bindings_of(env.con, slot_key)]
        assert bound_notes == [None, "re-download"]
    finally:
        env.cleanup()


# ---------- 4. 缺失附件：404 宽限期待补，超限判失败；瞬时错误一直待补可恢复 ----------

def test_missing_grace_then_failure_and_transient_recovery():
    env = Env(max_missing_grace=3)
    try:
        url404 = "https://src/a-1/missing.pdf"
        url_flaky = "https://src/a-1/flaky.pdf"
        env.fetcher.register(url_flaky, PDF_MAGIC + b"later", "flaky.pdf")

        def fail_script(u, n):
            if u == url404:
                return FetchError("not found", subtype="missing", http_status=404)
            if u == url_flaky and n < 3:  # 前两次瞬时失败，第三次成功
                return FetchError("connection reset", subtype="transient")
            return None

        env.fetcher._fail_script = fail_script
        specs = [
            {"ref": "att-miss", "name": "missing.pdf", "kind": "pdf", "url": url404},
            {"ref": "att-flaky", "name": "flaky.pdf", "kind": "pdf", "url": url_flaky},
        ]
        r1 = attach.process_attachments(env.record(specs=specs), env.ctx)
        miss, flaky = r1["attach_results"]
        assert miss["status"] == "pending" and "grace 1/3" in miss["last_error"]
        assert flaky["status"] == "pending"
        assert r1["attach_status"] == "absent"  # 没有任何已验证证据

        r2 = attach.process_attachments(env.record(specs=specs), env.ctx)
        miss = next(x for x in r2["attach_results"] if x["ref"] == "att-miss")
        flaky = next(x for x in r2["attach_results"] if x["ref"] == "att-flaky")
        assert miss["status"] == "pending" and "grace 2/3" in miss["last_error"]
        assert flaky["status"] == "pending"

        env.ctx.run_id = "run-2"
        r3 = attach.process_attachments(env.record(specs=specs), env.ctx)
        miss = next(x for x in r3["attach_results"] if x["ref"] == "att-miss")
        flaky = next(x for x in r3["attach_results"] if x["ref"] == "att-flaky")
        assert miss["status"] == "failed" and miss["attempts"] == 3
        assert flaky["status"] == "verified" and flaky["attempts"] == 3
        assert r3["attach_status"] == "partial"  # 一个失败、一个成功
        assert r3["attach_failed_slots"] == 1

        # 失败槽位保留尝试流水；成功槽位证据可读
        attempts = attach_store.attempts_of(
            env.con, attach.slot_key_of("a-1", "v1", "att-miss", None))
        assert [a["result"] for a in attempts] == ["missing"] * 3
        assert all(a["http_status"] == 404 for a in attempts)
    finally:
        env.cleanup()


# ---------- 5. 摘要不匹配：不归档坏内容，重试窗口后判 failed ----------

def test_digest_mismatch_fails_and_keeps_good_evidence():
    env = Env(max_hard_retries=2)
    try:
        good = PDF_MAGIC + b"good"
        tampered = PDF_MAGIC + b"tampered!!"
        url = "https://src/a-1/doc.pdf"
        env.fetcher.register(url, tampered, "doc.pdf")
        expected = attach_store_sha(good)
        specs = [{"ref": "att-1", "name": "doc.pdf", "kind": "pdf",
                  "url": url, "expected_digest": expected}]

        r1 = attach.process_attachments(env.record(specs=specs), env.ctx)
        assert r1["attach_results"][0]["status"] == "pending"
        assert r1["attach_results"][0]["digests"] == []  # 坏内容未归档
        r2 = attach.process_attachments(env.record(specs=specs), env.ctx)
        assert r2["attach_results"][0]["status"] == "failed"
        assert r2["attach_status"] == "absent"
        # evidence 表没有任何登记，blob 目录为空
        assert list((env.dir / "archive" / "blobs").rglob("*")) == []

        # 源站修复内容后，新槽位（公告升版）携带相同期望摘要即可正常归档
        env.fetcher.register(url, good, "doc.pdf")
        env.ctx.run_id = "run-2"
        r3 = attach.process_attachments(
            env.record(version="v2", specs=specs), env.ctx)
        assert r3["attach_results"][0]["status"] == "verified"
        assert r3["attach_results"][0]["digests"] == [expected]
    finally:
        env.cleanup()


def attach_store_sha(payload):
    from src.gazettetrack.attach_blob import sha256_hex
    return sha256_hex(payload)


# ---------- 6. 分页附件：每页独立槽位/URL/页码区域，缺页保持 partial，补齐转 complete ----------

def test_paginated_attachment_partial_then_complete():
    env = Env()
    try:
        p1 = PDF_MAGIC + b"page1"
        p2 = PDF_MAGIC + b"page2"
        env.fetcher.register("https://src/a-1/doc-p1.pdf", p1)
        # p2 暂未注册 -> 离线下载器报瞬时错误（待补）
        specs = [{
            "ref": "att-1", "name": "doc.pdf", "kind": "pdf",
            "page_total": 2,
            "pages": [
                {"page": 1, "url": "https://src/a-1/doc-p1.pdf",
                 "region": {"page_label": "第1页"}},
                {"page": 2, "url": "https://src/a-1/doc-p2.pdf",
                 "region": {"page_label": "第2页"}},
            ],
        }]
        r1 = attach.process_attachments(env.record(specs=specs), env.ctx)
        assert r1["attach_status"] == "partial"
        s1 = env.slot(env.con, "a-1", "v1", "att-1", page=1)
        s2 = env.slot(env.con, "a-1", "v1", "att-1", page=2)
        assert s1["status"] == "verified" and s1["page_no"] == 1 and s1["page_total"] == 2
        assert s2["status"] == "pending" and s2["page_no"] == 2
        assert s1["group_key"] == s2["group_key"]  # 同组分页
        assert json.loads(s1["region"]) == {"page_label": "第1页"}

        # 第二批下载完成，补齐缺页
        env.fetcher.register("https://src/a-1/doc-p2.pdf", p2)
        env.ctx.run_id = "run-2"
        r2 = attach.process_attachments(env.record(specs=specs), env.ctx)
        assert r2["attach_status"] == "complete"
        assert env.slot(env.con, "a-1", "v1", "att-1", page=2)["status"] == "verified"
        # 第一页未重复下载（verified 跳过，attempts 保持 1）
        assert env.slot(env.con, "a-1", "v1", "att-1", page=1)["attempts"] == 1
    finally:
        env.cleanup()


# ---------- 7. 压缩包内重名文件：按序号全部保留、各自摘要，zip-slip 名被净化 ----------

def test_archive_duplicate_names_all_retained():
    env = Env()
    try:
        member_a = PDF_MAGIC + b"inner-a"
        member_b = PNG_MAGIC + b"inner-b-different"
        evil = PDF_MAGIC + b"evil"
        zipped = make_zip([
            ("dir/report.pdf", member_a),
            ("dir/report.pdf", member_b),          # 重名成员
            ("../../etc/evil.pdf", evil),          # 路径穿越名，必须被净化
        ])
        assert zipped.startswith(ZIP_MAGIC)
        env.fetcher.register("https://src/a-1/bundle.zip", zipped, "bundle.zip")
        specs = [{"ref": "att-z", "name": "bundle.zip", "kind": "archive",
                  "url": "https://src/a-1/bundle.zip"}]
        out = attach.process_attachments(env.record(specs=specs), env.ctx)
        slot = out["attach_results"][0]
        assert slot["status"] == "verified" and slot["parse_status"] == "parsed"

        parent = slot["digests"][0]
        members = attach_store.members_of(env.con, parent)
        assert len(members) == 3
        indexes = [m["member_index"] for m in members]
        assert indexes == [0, 1, 2]
        # 重名成员两条都在，摘要不同，blob 不同
        same_name = [m for m in members if m["member_name"] == "dir/report.pdf"]
        assert len(same_name) == 2
        assert {m["member_digest"] for m in same_name} == {
            attach_store_sha(member_a), attach_store_sha(member_b)}
        # 落盘名带序号前缀，无目录穿越；成员 blob 均为普通文件
        for m in members:
            p = env.dir / "archive" / "blobs" / m["blob_path"]
            assert p.is_file()
            assert ".." not in m["blob_path"] and not m["blob_path"].startswith(("/", "\\"))
        evil_row = members[2]
        assert ".." not in evil_row["member_name"]
        assert evil_row["member_name"].endswith("evil.pdf")
        # 成员证据原文可读
        assert attach_store.read_evidence_bytes(
            env.con, same_name[0]["member_digest"], env.ctx.blob_root
        ) in (member_a, member_b)
    finally:
        env.cleanup()


# ---------- 8. 无附件声明：透传不报错，状态 absent；流水线整体幂等 ----------

def test_no_attachments_and_pipeline_compatibility():
    env = Env()
    try:
        out = attach.process_attachments(env.record(specs=[]), env.ctx)
        assert out["attach_status"] == "absent"
        assert out["attach_results"] == []
        assert out["_stage_attach"] == "ok"
    finally:
        env.cleanup()


# ---------- 9. 来源 URL/公告关联/历史绑定可查询，旧归档证据长期可读 ----------

def test_source_url_and_bindings_queryable():
    env = Env()
    try:
        pdf = PDF_MAGIC + b"x"
        env.fetcher.register("https://src/x/v.pdf", pdf, "v.pdf")
        specs = [{"ref": "r1", "name": "v.pdf", "kind": "pdf",
                  "url": "https://src/x/v.pdf"}]
        attach.process_attachments(
            env.record(aid="ann-9", version="2026-11-17", specs=specs), env.ctx)
        slots = attach_store.slots_of_announcement(env.con, "ann-9", "2026-11-17")
        assert len(slots) == 1 and slots[0]["url"] == "https://src/x/v.pdf"
        bindings = attach_store.bindings_of(env.con, slots[0]["slot_key"])
        assert len(bindings) == 1 and bindings[0]["bound_at"]
        digest = bindings[0]["digest"]
        assert attach_store.read_evidence_bytes(
            env.con, digest, env.ctx.blob_root) == pdf
    finally:
        env.cleanup()


# ---------- 10. 类型声明与魔数不符：解析失败但证据保留（可追溯） ----------

def test_kind_mismatch_marks_parse_failed_but_keeps_evidence():
    env = Env()
    try:
        png = PNG_MAGIC + b"actually-png"
        env.fetcher.register("https://src/a-1/fake.pdf", png, "fake.pdf")
        specs = [{"ref": "att-1", "name": "fake.pdf", "kind": "pdf",
                  "url": "https://src/a-1/fake.pdf"}]
        out = attach.process_attachments(env.record(specs=specs), env.ctx)
        item = out["attach_results"][0]
        assert item["status"] == "verified"       # 下载与摘要确认成功
        assert item["parse_status"] == "parse_failed"
        assert "magic says" in item["last_error"]
        # 证据仍归档可读
        assert attach_store.read_evidence_bytes(
            env.con, item["digests"][0], env.ctx.blob_root) == png
    finally:
        env.cleanup()


# ---------- 12. 意外异常：savepoint 回滚，连接仍可用，已下载 blob 不登记为证据 ----------

def test_unexpected_error_rolls_back_and_connection_reusable():
    env = Env()
    try:
        pdf = PDF_MAGIC + b"ok"
        env.fetcher.register("https://src/a-1/good.pdf", pdf, "good.pdf")
        good = [{"ref": "att-ok", "name": "good.pdf", "kind": "pdf",
                 "url": "https://src/a-1/good.pdf"}]
        out = attach.process_attachments(env.record(specs=good), env.ctx)
        assert out["attach_status"] == "complete"

        class Boom(Exception):
            pass

        def boom(url):
            raise Boom("disk vanished")

        env.fetcher.fetch = boom  # type: ignore[assignment]
        bad = [{"ref": "att-bad", "name": "bad.pdf", "kind": "pdf",
                "url": "https://src/a-1/bad.pdf"}]
        try:
            attach.process_attachments(env.record(specs=bad), env.ctx)
            raise AssertionError("expected Boom")
        except Boom:
            pass

        # 连接未被毒化：新事务可用，先前证据仍在，坏槽位未残留
        rows = attach_store.slots_of_announcement(env.con, "a-1", "v1")
        refs = {r["attach_ref"] for r in rows}
        assert refs == {"att-ok"}
        again = attach.process_attachments(env.record(specs=good), env.ctx)
        assert again["attach_status"] == "complete"
    finally:
        env.cleanup()


# ---------- 11. 端到端：接入现有 pipeline + store，重跑幂等，证据跨连接可读 ----------

def test_pipeline_end_to_end_with_attachments():
    import json
    from src.gazettetrack import pipeline, store as rec_store
    from src.gazettetrack.attach_fetch import OfflineFetcher

    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    db_path = base / "flow.db"
    archive_dir = base / "archive"
    fixture = base / "input.json"
    pdf = PDF_MAGIC + b"pipeline pdf"
    records = [
        {"id": "g-1", "src": "alpha", "name": "Gazette One", "value": 1,
         "version": "2026-11-17", "status": "active",
         "attachments": [
             {"ref": "a1", "name": "notice.pdf", "kind": "pdf",
              "url": "https://src/g-1/notice.pdf"}]},
        {"id": "g-2", "src": "beta", "name": "Gazette Two", "value": 2,
         "version": "2026-11-17", "status": "active", "attachments": []},
    ]
    fixture.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    fetcher = OfflineFetcher()
    fetcher.register("https://src/g-1/notice.pdf", pdf, "notice.pdf")

    from src.gazettetrack import loader, config
    monkeypatched = loader.FIXTURES
    loader.FIXTURES = fixture
    config.FIXTURES = fixture
    # store.connect 的路径来自 config.DB_PATH，临时改写
    prev_db = config.DB_PATH
    config.DB_PATH = db_path
    try:
        flow_con = rec_store.connect()
        ctx1 = attach.AttachContext(
            fetcher=fetcher, archive_dir=archive_dir,
            db_path=base / "attachments.db", run_id="e2e-1")
        r1 = pipeline.run(flow_con, attach_context=ctx1)
        ctx2 = attach.AttachContext(
            fetcher=fetcher, archive_dir=archive_dir,
            db_path=base / "attachments.db", run_id="e2e-2")
        r2 = pipeline.run(flow_con, attach_context=ctx2)  # 重跑：verified 跳过
        assert r1["fed"] == r2["fed"] == 2
        assert r1["rows"] == r2["rows"] == 2

        # 记录库的公告带附件注解；附件证据在独立的附件库中
        rows = {k: d for k, d in rec_store.all_records(flow_con)}
        g1 = rows["g-1"]
        assert g1["attach_status"] == "complete"
        assert g1["attach_results"][0]["url"] == "https://src/g-1/notice.pdf"
        digest = g1["attach_results"][0]["digests"][0]

        # 模拟下次启动用新连接：历史证据仍可读，blob 在磁盘上
        flow_con.close()
        ctx1.close()
        ctx2.close()
        con2 = attach_store.connect(base / "attachments.db")
        ev = attach_store.get_evidence(con2, digest)
        assert ev is not None and ev["observed_name"] == "notice.pdf"
        assert attach_store.read_evidence_bytes(con2, digest,
                                                archive_dir / "blobs") == pdf
        slots = attach_store.slots_of_announcement(con2, "g-1", "2026-11-17")
        assert slots and slots[0]["status"] == "verified"
        con2.close()
    finally:
        config.DB_PATH = prev_db
        loader.FIXTURES = monkeypatched
        config.FIXTURES = monkeypatched
        tmp.cleanup()
