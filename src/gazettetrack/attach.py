"""单一职责：公示附件处理阶段。

attach 接收上游解析出的公告身份（``announcement_id``）与版本（``version``），对公告
声明的附件清单做下载校验、证据归档与状态推进，并把来源 URL、页码/区域定位、解析状态
写回记录，随后进入现有 keyer/store 持久化流程。

证据规则（与 tests/test_attach.py 对应，亦见 README“附件规则”）：
1. 内容寻址、只追加：证据按 sha256 摘要归档，文件名不变而内容变化会成为新证据；
   槽位与证据的绑定只追加，重复下载永不覆盖历史版本，旧证据始终可读。
2. 分批/部分失败：附件可分批出现，单个槽位失败不影响同公告其它附件；公告级状态按
   槽位汇总为 complete / partial / absent，正文与已完成附件先行落库。
3. 缺失 vs 失败：传输类错误（连接失败、5xx、未注册 URL）一律 pending 待补、无限重试；
   404/410 进入待补宽限（默认 3 次尝试），超限判 failed；摘要不匹配等硬校验错误
   （默认 2 次）重试后判 failed。已 verified 的槽位默认跳过，刷新下载只追加新绑定。
4. 摘要确认时点：完整字节流下载结束后计算 sha256；声明了 expected_digest 则必须相等，
   不匹配不归档、判 digest_mismatch；未声明则以计算值作为确认摘要登记证据。
5. 分页附件：同一附件按 page_no 拆成多个槽位、共享 group_key，按页独立下载与重试；
   压缩包内重名成员按包内序号全部保留，绝不互相覆盖。
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path

from . import attach_store as store
from . import attach_blob as blob
from .attach_fetch import FetchError, OfflineFetcher
from .config import (ARCHIVE_DIR, ATTACH_DB_PATH, MAX_HARD_RETRIES,
                     MAX_MISSING_GRACE)

# 进程级默认上下文：流水线无参 process(record) 使用；测试用 use_context 注入临时环境。
_DEFAULT = None


class AttachContext:
    """一组附件处理的可注入依赖（证据库连接、blob 根、下载器、策略参数）。

    附件证据库与记录库分离：证据只追加、生命周期独立，不干扰记录库的计数/幂等。
    传入 ``con`` 时复用外部连接；传入 ``db_path`` 或留空时自建连接并负责关闭。
    """

    def __init__(self, con=None, *, fetcher=None, archive_dir=ARCHIVE_DIR,
                 db_path=ATTACH_DB_PATH,
                 max_missing_grace=MAX_MISSING_GRACE,
                 max_hard_retries=MAX_HARD_RETRIES, run_id=None):
        self._con = con
        self._db_path = db_path
        self._owns_con = con is None
        self.fetcher = fetcher or OfflineFetcher()
        self.archive_dir = Path(archive_dir)
        self.blob_root = self.archive_dir / "blobs"
        self.max_missing_grace = max_missing_grace
        self.max_hard_retries = max_hard_retries
        self.run_id = run_id or uuid.uuid4().hex[:12]

    @property
    def con(self):
        # 懒连接：无附件声明的记录不会在磁盘上创建空证据库
        if self._con is None:
            self._con = store.connect(self._db_path)
        return self._con

    def close(self):
        if self._owns_con and self._con is not None:
            self._con.close()
            self._con = None


@contextmanager
def use_context(con=None, **kwargs):
    global _DEFAULT
    prev = _DEFAULT
    ctx = AttachContext(con, **kwargs)
    _DEFAULT = ctx
    try:
        yield ctx
    finally:
        _DEFAULT = prev
        ctx.close()


def set_default_context(ctx: AttachContext):
    """供编排层注入已构建的上下文（流水线复用，不在编排层 new 第二份依赖）。"""
    global _DEFAULT
    _DEFAULT = ctx


def _ctx() -> AttachContext:
    if _DEFAULT is None:
        raise RuntimeError(
            "attach context not configured: wrap pipeline.run() with "
            "attach.use_context(con, ...) or call attach.process_attachments(...)"
        )
    return _DEFAULT


# ---------------- 槽位键与分页分组 ----------------

def slot_key_of(announcement_id, version, attach_ref, page_no):
    page_part = "whole" if page_no is None else f"p{int(page_no):04d}"
    return f"{announcement_id}|{version}|{attach_ref}|{page_part}"


def group_key_of(announcement_id, version, attach_ref):
    return f"{announcement_id}|{version}|{attach_ref}"


def _expand_pages(spec):
    """把一个附件声明展开为若干槽位声明（分页附件每页一槽）。"""
    pages = spec.get("pages")
    if not pages:
        yield {
            "attach_ref": str(spec["ref"]),
            "page_no": None,
            "page_total": None,
            "region": spec.get("region"),
            "url": spec["url"],
        }
        return
    for page in pages:
        yield {
            "attach_ref": str(spec["ref"]),
            "page_no": int(page["page"]),
            "page_total": spec.get("page_total") or len(pages),
            "region": page.get("region", spec.get("region")),
            "url": page["url"],
        }


# ---------------- 单槽位处理 ----------------

def _stage_dir(ctx: AttachContext, slot_key: str) -> Path:
    """每个附件使用独立的空暂存目录（不可信数据隔离）。"""
    safe = slot_key.replace("|", "_").replace("/", "_")
    d = Path(ctx.archive_dir) / "staging" / ctx.run_id / safe
    d.mkdir(parents=True, exist_ok=True)
    for child in d.iterdir():  # 保证“新空目录”语义
        child.unlink()
    return d


def _handle_fetch_failure(ctx, slot, exc: FetchError):
    con = ctx.con
    store.record_attempt(
        con, slot_key=slot["slot_key"], run_id=ctx.run_id, url=slot["url"],
        result=exc.subtype, http_status=exc.http_status, error=str(exc),
    )
    attempts = store.bump_attempts(con, slot["slot_key"])
    if exc.subtype == "missing":
        if attempts >= ctx.max_missing_grace:
            store.mark_slot(con, slot["slot_key"], "failed",
                            last_error=f"missing after {attempts} attempts: {exc}")
        else:
            store.mark_slot(con, slot["slot_key"], "pending",
                            last_error=f"missing (grace {attempts}/{ctx.max_missing_grace}): {exc}")
    else:
        # 瞬时错误永远待补，等待下一轮重试恢复
        store.mark_slot(con, slot["slot_key"], "pending",
                        last_error=f"transient: {exc}")


def _archive_members(ctx, parent_digest: str, payload: bytes, slot_key: str):
    """安全展开压缩包：重名成员按序号全部归档；返回 (成员数, 全部解析是否通过)。"""
    con = ctx.con
    ok = True
    count = 0
    try:
        for index, raw_name, data in blob.iter_zip_members(payload):
            count += 1
            digest = blob.sha256_hex(data)
            rel = store.archive_blob(digest, data, ctx.blob_root)
            parse_status, _ = blob.structural_parse(data, None)
            if parse_status == "parse_failed":
                ok = False
            store.register_evidence(
                con, digest=digest, blob_path=rel, size_bytes=len(data),
                observed_name=blob.safe_member_name(raw_name, index),
                kind=blob.sniff_kind(data), parse_status=parse_status,
            )
            store.record_member(
                con, parent_digest=parent_digest, member_index=index,
                member_name=raw_name, member_digest=digest, blob_path=rel,
                parse_status=parse_status,
            )
    except Exception as exc:  # 包损坏：外层证据保留，槽位解析失败
        return count, False, f"archive unreadable: {exc}"
    return count, ok, None


def _process_slot(ctx: AttachContext, slot: dict, refresh: bool):
    con = ctx.con
    existing = store.get_slot(con, slot["slot_key"])
    if existing and existing["status"] == "verified" and not refresh:
        return "verified"  # 已归档证据默认不重复下载，历史绑定原样保留

    # 独立空暂存目录（本实现字节在内存中传递，目录用于落盘隔离与后续扩展）
    _stage_dir(ctx, slot["slot_key"])

    try:
        result = ctx.fetcher.fetch(slot["url"])
    except FetchError as exc:
        _handle_fetch_failure(ctx, slot, exc)
        return store.get_slot(con, slot["slot_key"])["status"]

    digest = blob.sha256_hex(result.content)
    expected = slot.get("expected_digest")
    if expected and digest != expected:
        store.record_attempt(
            con, slot_key=slot["slot_key"], run_id=ctx.run_id, url=slot["url"],
            result="digest_mismatch", http_status=result.http_status,
            bytes_seen=len(result.content), digest_seen=digest,
            error=f"expected {expected}, got {digest}",
        )
        attempts = store.bump_attempts(con, slot["slot_key"])
        if attempts >= ctx.max_hard_retries:
            store.mark_slot(con, slot["slot_key"], "failed",
                            last_error=f"digest_mismatch after {attempts} attempts")
        else:
            store.mark_slot(con, slot["slot_key"], "pending",
                            last_error=f"digest_mismatch (retry {attempts}/{ctx.max_hard_retries})")
        return store.get_slot(con, slot["slot_key"])["status"]

    # 摘要确认通过：内容寻址归档（同内容幂等复用，绝不覆盖）
    parse_status, warn = blob.structural_parse(result.content, slot.get("kind"))
    rel = store.archive_blob(digest, result.content, ctx.blob_root)
    store.register_evidence(
        con, digest=digest, blob_path=rel, size_bytes=len(result.content),
        observed_name=result.filename, kind=blob.sniff_kind(result.content),
        parse_status=parse_status,
    )
    prior_bindings = store.bindings_of(con, slot["slot_key"])
    store.bind_evidence(
        con, slot["slot_key"], digest,
        note="re-download" if prior_bindings else None,
    )
    store.record_attempt(
        con, slot_key=slot["slot_key"], run_id=ctx.run_id, url=slot["url"],
        result="ok", http_status=result.http_status,
        bytes_seen=len(result.content), digest_seen=digest,
        error=warn,
    )
    store.bump_attempts(con, slot["slot_key"])

    # 压缩包：成员全部展开归档（重名按序号保留）；包损坏只影响解析状态
    if blob.sniff_kind(result.content) == "archive":
        n, members_ok, arc_err = _archive_members(ctx, digest, result.content,
                                                  slot["slot_key"])
        if arc_err or not members_ok:
            parse_status = "parse_failed"
            store.mark_slot(con, slot["slot_key"], "verified",
                            parse_status="parse_failed",
                            last_error=arc_err or f"{n} members, some unparseable")
        else:
            store.mark_slot(con, slot["slot_key"], "verified",
                            parse_status="parsed")
    else:
        store.mark_slot(con, slot["slot_key"], "verified",
                        parse_status=parse_status,
                        last_error=warn)
    return "verified"


# ---------------- 公告级入口 ----------------

def _announcement_identity(record):
    aid = record.get("announcement_id") or record.get("id")
    version = record.get("version") or record.get("ts") or "v1"
    if not aid:
        raise ValueError("record lacks announcement identity (announcement_id/id)")
    return str(aid), str(version)


def process_attachments(record: dict, ctx: AttachContext, *, refresh=False) -> dict:
    """显式上下文入口：处理一条公告记录的全部附件声明，返回注解后的记录。"""
    aid, version = _announcement_identity(record)
    specs = record.get("attachments") or []
    if not specs:
        # 无附件声明：不触碰证据库/磁盘（避免生成空证据库），直接透传
        rec = dict(record)
        rec["announcement_id"] = aid
        rec["version"] = version
        rec["attach_results"] = []
        rec["attach_status"] = "absent"
        rec["attach_failed_slots"] = 0
        rec["_stage_attach"] = "ok"
        return rec
    con = ctx.con
    annotated = []
    # 复用流水线连接时其可能已处于事务中（sqlite3 默认隐式开启），用 savepoint 嵌套。
    con.execute("SAVEPOINT attach_process")
    try:
        for spec in specs:
            gk = group_key_of(aid, version, spec["ref"])
            for page in _expand_pages(spec):
                slot_key = slot_key_of(aid, version, page["attach_ref"],
                                       page["page_no"])
                slot = {
                    "slot_key": slot_key,
                    "announcement_id": aid,
                    "version": version,
                    "attach_ref": page["attach_ref"],
                    "page_no": page["page_no"],
                    "page_total": page["page_total"],
                    "region": store.encode_region(page["region"]),
                    "url": page["url"],
                    "declared_name": str(spec.get("name") or blob.name_from_url(page["url"])),
                    "expected_digest": spec.get("expected_digest"),
                    "kind": spec.get("kind"),
                    "group_key": gk,
                }
                store.upsert_slot(con, slot)
                status = _process_slot(ctx, slot, refresh)
                row = store.get_slot(con, slot_key)
                annotated.append({
                    "slot_key": slot_key,
                    "ref": slot["attach_ref"],
                    "page": page["page_no"],
                    "region": page["region"],
                    "url": page["url"],
                    "declared_name": slot["declared_name"],
                    "status": status,
                    "parse_status": row["parse_status"],
                    "attempts": row["attempts"],
                    "last_error": row["last_error"],
                    "digests": [b["digest"] for b in store.bindings_of(con, slot_key)],
                })
        con.execute("RELEASE SAVEPOINT attach_process")
    except Exception:
        con.execute("ROLLBACK TO SAVEPOINT attach_process")
        con.execute("RELEASE SAVEPOINT attach_process")
        raise

    statuses = {a["status"] for a in annotated}
    if not annotated:
        attach_status = "absent"
    elif statuses == {"verified"}:
        attach_status = "complete"
    elif "verified" in statuses:
        attach_status = "partial"
    else:
        attach_status = "absent"  # 全部待补/失败：证据尚缺，保持待补语义
    failed = [a for a in annotated if a["status"] == "failed"]

    rec = dict(record)
    rec["announcement_id"] = aid
    rec["version"] = version
    rec["attach_results"] = annotated
    rec["attach_status"] = attach_status
    rec["attach_failed_slots"] = len(failed)
    rec["_stage_attach"] = "ok"
    return rec


def process(record: dict) -> dict:
    """流水线阶段入口：使用 use_context 配置的默认上下文。"""
    ctx = _ctx()
    return process_attachments(record, ctx)
