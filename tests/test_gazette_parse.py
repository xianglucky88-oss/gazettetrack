"""离线夹具驱动的 gazette_parse 测试。

覆盖：表头换名、空页、半张损坏表、旧格式、分页、重复抓取、局部重试，
以及“恢复不重写其他公告”的约束。
"""
from pathlib import Path

from src.gazettetrack import gazette_parse as gp
from src.gazettetrack import pipeline, store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "gazette"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def titles(result):
    return [entry["title"] for entry in result.entries]


def issue_codes(issues):
    return [issue["code"] for issue in issues]


# ---------------------------------------------------------------------------
# 表头换名 / 空值 / 附件线索
# ---------------------------------------------------------------------------


def test_renamed_headers_map_to_canonical_fields():
    result = gp.parse_board(read("p1_renamed.html"), source="甲县人民法院")

    assert result.stats["parsed"] == 3
    assert titles(result) == ["某某债权转让公告", "第二次拍卖公告", "执行悬赏公告"]
    first = result.entries[0]
    assert first["court"] == "甲县人民法院"
    assert first["case_no"] == "(2026)甲执1号"
    assert first["subject"] == "张三"
    assert first["publish_date"] == "2026-11-01"
    assert first["amount"] == 12005000  # 1200.5 万元
    assert first["detail_url"].endswith("/detail/j-001.html")
    assert first["source"] == "甲县人民法院"
    assert first["batch_id"] == result.batch_id
    assert first["locator"] == {
        "source": "甲县人民法院",
        "batch_id": result.batch_id,
        "page": 1,
        "row": 2,
        "table": 1,
    }


def test_null_tokens_degrade_instead_of_blocking_row():
    result = gp.parse_board(read("p1_renamed.html"), source="甲县人民法院")
    second = result.entries[1]
    assert second["amount"] is None
    assert "amount" in second["extraction"]["degraded_fields"]
    assert not result.errors


def test_attachment_clues_are_separate_from_entries():
    result = gp.parse_board(
        read("p1_renamed.html"),
        source="甲县人民法院",
        base_url="https://courts.example.org/notices/2026/",
    )

    # 条目上只挂附件线索（尚未下载），下载状态属于 attach/store 的附件通道。
    entry_clues = [clue for entry in result.entries for clue in entry["attachments"]]
    assert entry_clues
    assert all(clue["status"] in {"pending", "unresolved"} for clue in entry_clues)
    assert all("path" not in clue and "sha256" not in clue for clue in entry_clues)

    urls = {clue["url"] for clue in result.attachments}
    assert "https://courts.example.org/files/j-001.pdf" in urls
    # 纯文件名（无链接）也保留为 unresolved 线索交给 attach 阶段。
    names = {clue["filename"] for clue in result.attachments if not clue["url"]}
    assert {"公告正文.pdf", "附表.xlsx"} <= names
    unresolved = [clue for clue in result.attachments if clue["status"] == "unresolved"]
    assert all(clue["entry_key"] for clue in unresolved)

    routes = result.routes()
    assert routes["dedupe"] is result.entries
    assert routes["attach"] is result.attachments
    assert routes["store"]["errors"] == result.errors
    assert set(routes) == {"dedupe", "attach", "store"}


# ---------------------------------------------------------------------------
# 空页 / 半张坏表 / 旧格式
# ---------------------------------------------------------------------------


def test_empty_page_does_not_fail_batch():
    result = gp.parse_board(
        {
            "source": "甲县人民法院",
            "pages": [
                {"number": 1, "html": read("p1_renamed.html")},
                {"number": 2, "html": read("p2_empty.html")},
            ],
        }
    )

    assert len(result.entries) == 3
    assert [page["status"] for page in result.pages] == ["ok", "empty"]
    assert "page_empty" in issue_codes(result.warnings)
    assert not result.errors
    assert result.ok


def test_half_broken_table_keeps_good_rows_and_reports_bad_one():
    result = gp.parse_board(read("p3_half_broken.html"), source="乙县人民法院")

    assert titles(result) == ["乙法院变卖公告一", "乙法院变卖公告三"]
    assert [entry["row"] for entry in result.entries] == [2, 4]
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error["code"] == "required_field_missing"
    assert error["field"] == "title"
    assert (error["page"], error["row"], error["table"]) == (1, 3, 1)
    assert error["source"] == "乙县人民法院"
    assert error["batch_id"] == result.batch_id
    # 失败位置可核对，且恰好是中间那行。
    assert result.failed_locations == [
        {
            "code": "required_field_missing",
            "source": "乙县人民法院",
            "batch_id": result.batch_id,
            "page": 1,
            "row": 3,
            "table": 1,
            "field": "title",
            "blocking": True,
            "fatal": False,
        }
    ]
    # 局部坏行不算整批失败：批次仍然成功，只是不完整。
    assert result.ok
    assert not result.complete
    assert result.stats["failed_rows"] == 1
    assert "row_truncated" in issue_codes(result.warnings)


def test_legacy_layout_without_headers():
    result = gp.parse_board(read("p4_legacy.html"), source="丙区人民法院")

    assert titles(result) == ["丙法院旧版公告一", "丙法院旧版公告二"]
    assert all(entry["extraction"]["legacy"] for entry in result.entries)
    assert all(entry["extraction"]["method"] == "legacy_position" for entry in result.entries)
    assert "legacy_layout_assumed" in issue_codes(result.warnings)
    assert result.entries[0]["publish_date"] == "2025-03-01"


def test_unknown_document_returns_fatal_issue_instead_of_raising():
    result = gp.parse_board(12345, source="坏来源")
    assert not result.entries
    assert len(result.errors) == 1
    assert result.errors[0]["code"] == "document_unrecognized"
    assert result.errors[0]["fatal"]


# ---------------------------------------------------------------------------
# 分页 / 局部重试 / 恢复
# ---------------------------------------------------------------------------


def test_pagination_follows_next_link_and_merges_duplicates():
    pages = {"https://courts.example/board/p5_next.html": read("p5_next.html")}
    second = read("p5_next2.html").encode("utf-8")
    calls = []

    def fetcher(url):
        calls.append(url)
        if url.endswith("p5_next2.html"):
            return second
        return pages[url].encode("utf-8")

    result = gp.parse_board(
        "https://courts.example/board/p5_next.html",
        source="丁区人民法院",
        fetcher=fetcher,
    )

    # 第 1 页里重复出现的 D-1 合并为一条，第 2 页补上 D-3。
    assert titles(result) == ["丁法院公告一", "丁法院公告二"]
    assert result.stats["duplicates"] == 1
    assert [page["number"] for page in result.pages] == [1, 2]
    assert calls == [
        "https://courts.example/board/p5_next.html",
        "https://courts.example/board/p5_next2.html",
    ]


def test_page_failure_is_retried_locally_and_others_continue():
    def fetcher(url):
        if url.endswith("p3.html"):
            raise TimeoutError("第 3 页超时")
        return {
            "https://c.example/p1.html": read("p1_renamed.html").encode(),
            "https://c.example/p2.html": read("p2_empty.html").encode(),
        }[url]

    result = gp.parse_board(
        {
            "source": "甲县人民法院",
            "pages": [
                {"number": 2, "url": "https://c.example/p2.html"},
                {"number": 1, "url": "https://c.example/p1.html"},
                {"number": 3, "url": "https://c.example/p3.html"},
            ],
        },
        fetcher=fetcher,
        max_retries=3,
    )

    # 1、2 页照常处理；3 页失败只影响自己。
    assert len(result.entries) == 3
    assert [page["status"] for page in result.pages] == ["empty", "ok", "failed"]
    assert result.pages[2]["attempts"] == 3
    assert issue_codes(result.errors) == ["page_fetch_failed"]
    assert result.errors[0]["page"] == 3
    assert result.errors[0]["attempts"] == 3
    assert not result.errors[0]["fatal"]


def test_resume_retries_only_failed_page_and_preserves_other_entries():
    state = {"p3_fail": True}

    def fetcher(url):
        page = url.rsplit("/", 1)[-1]
        if page == "p3_half_broken.html" and state["p3_fail"]:
            raise ConnectionError("down")
        return read(page).encode("utf-8")

    document = {
        "source": "乙县人民法院",
        "pages": [
            {"number": 1, "url": "https://c.example/p1_renamed.html"},
            {"number": 2, "url": "https://c.example/p3_half_broken.html"},
        ],
    }
    first = gp.parse_board(document, fetcher=fetcher, max_retries=1)
    assert [page["status"] for page in first.pages] == ["ok", "failed"]
    assert titles(first) == ["某某债权转让公告", "第二次拍卖公告", "执行悬赏公告"]
    preserved = list(first.entries)

    state["p3_fail"] = False
    calls = []

    def retry_fetcher(url):
        calls.append(url)
        return read(url.rsplit("/", 1)[-1]).encode("utf-8")

    # 补抓只需要原批次结果：不用重新提交整份文档。
    recovered = gp.parse_board(
        {"pages": []},
        fetcher=retry_fetcher,
        resume_from=first,
        max_retries=1,
    )

    assert calls == ["https://c.example/p3_half_broken.html"]
    assert [page["status"] for page in recovered.pages] == ["ok", "partial"]
    # 未重试的页面条目被原样复用（同一对象），不会重写。
    assert recovered.entries[:3] == preserved
    assert recovered.entries[0] is first.entries[0]
    assert titles(recovered)[3:] == ["乙法院变卖公告一", "乙法院变卖公告三"]
    assert recovered.batch_id == first.batch_id
    assert recovered.metadata["recovered_pages"] == [2]
    assert {issue["code"] for issue in recovered.errors} == {"required_field_missing"}


def test_duplicate_fetch_is_stable_for_keys_batch_and_store(tmp_path):
    con = store.connect(tmp_path / "dup.db")
    try:
        first = pipeline.ingest(
            read("p1_renamed.html"), con=con, source="甲县人民法院"
        )
        second = pipeline.ingest(
            read("p1_renamed.html"), con=con, source="甲县人民法院"
        )
    finally:
        con.close()

    assert first["batch_id"] == second["batch_id"]
    assert first["written"] == second["written"] == 3
    assert first["rows"] == second["rows"] == store.count(store.connect(tmp_path / "dup.db"))


# ---------------------------------------------------------------------------
# 字段契约：哪些字段可降级、哪些错误必须阻断
# ---------------------------------------------------------------------------


def test_contract_decides_blocking_boundary():
    rows = [["X-1", "有标题但日期坏", "not-a-date", "某法院", "(2026)X1号", "某人", "10", "/d/1"]]
    headers = ["序号", "标题", "发布日期", "法院", "案号", "主体", "金额", "链接"]

    default = gp.parse_rows(rows, headers=headers, source="契约默认")
    assert len(default.entries) == 1
    assert default.entries[0]["publish_date"] is None
    assert "invalid_date" in issue_codes(default.warnings)
    assert not default.errors

    strict = gp.parse_rows(
        rows,
        headers=headers,
        source="契约严格",
        contract={"required": ["title", "publish_date"]},
    )
    assert not strict.entries
    assert issue_codes(strict.errors) == ["invalid_date"]
    assert strict.errors[0]["field"] == "publish_date"
    assert strict.errors[0]["blocking"]

    # block_on_invalid 只对“有值但非法”生效，空值仍按 nullable 降级。
    blanks = [["X-2", "只有标题", "", "某法院", "", "", "", ""]]
    lenient = gp.parse_rows(
        blanks,
        headers=headers,
        source="契约宽松",
        contract={"required": ["title"], "block_on_invalid": ["amount"]},
    )
    assert len(lenient.entries) == 1
    assert lenient.entries[0]["amount"] is None


# ---------------------------------------------------------------------------
# 附件下载：单个失败不阻断，重复抓取不重写
# ---------------------------------------------------------------------------


def test_attachment_download_isolated_and_idempotent(tmp_path):
    result = gp.parse_board(
        read("p1_renamed.html"),
        source="甲县人民法院",
        base_url="https://courts.example/notices/2026/",
    )
    payloads = {
        "https://courts.example/files/j-001.pdf": (FIXTURES / "attachments" / "j-001.pdf").read_bytes(),
    }
    attempts = {"fail": 1}

    def fetcher(url):
        if url not in payloads:
            raise FileNotFoundError(url)
        if attempts["fail"]:
            attempts["fail"] -= 1
            raise ConnectionError("transient")
        return payloads[url]

    first = gp.download_attachments(result, tmp_path, fetcher=fetcher, max_retries=2)
    assert first["downloaded"] == 1
    assert first["failed"] == 0
    assert first["unresolved"] == 2
    written = tmp_path / "j-001.pdf"
    assert written.read_bytes() == payloads["https://courts.example/files/j-001.pdf"]
    before = written.stat().st_mtime_ns

    # 重复抓取：已存在且哈希一致的附件直接复用，不重写文件。
    again = gp.download_attachments(result, tmp_path, fetcher=fetcher, max_retries=1)
    assert again["reused"] == 1
    assert written.stat().st_mtime_ns == before

    # 单个附件写失败不影响其他附件：这里另建目录，只有 j-001 能下载。
    summary = gp.download_attachments(
        [{"url": "https://missing.example/x.pdf", "filename": "x.pdf"},
         {"url": "https://courts.example/files/j-001.pdf", "filename": "j-001-copy.pdf"}],
        tmp_path / "second",
        fetcher=lambda url: payloads.get(url) or (_ for _ in ()).throw(FileNotFoundError(url)),
        max_retries=1,
    )
    assert summary["failed"] == 1
    assert summary["downloaded"] == 1


# ---------------------------------------------------------------------------
# 落库：来源/批次随条目保留，错误单独存
# ---------------------------------------------------------------------------


def test_store_keeps_source_batch_attachments_and_errors(tmp_path):
    con = store.connect(tmp_path / "gazette.db")
    try:
        out = pipeline.ingest(read("p3_half_broken.html"), con=con, source="乙县人民法院")
        rows = store.all_records(con)
        stored_issues = store.issues(con, out["batch_id"])
        stored_attachments = store.attachments(con, out["batch_id"])
    finally:
        con.close()

    assert out["fed"] == 2
    assert out["errors"] == 1
    for _, data in rows:
        assert data["source"] == "乙县人民法院"
        assert data["batch_id"] == out["batch_id"]
        assert data["locator"]["source"] == "乙县人民法院"
    blocking = [issue for issue in stored_issues if issue["severity"] == "error"]
    assert len(blocking) == 1
    assert blocking[0]["row"] == 3
    assert blocking[0]["field"] == "title"
    # 警告也随批次保留，便于核对截断位置，但不影响入库。
    assert any(issue["code"] == "row_truncated" for issue in stored_issues)
    assert stored_attachments == []


def test_resume_then_ingest_does_not_rewrite_other_entries(tmp_path):
    state = {"fail": True}

    def fetcher(url):
        name = url.rsplit("/", 1)[-1]
        if name == "p3_half_broken.html" and state["fail"]:
            raise TimeoutError("down")
        return read(name).encode()

    con = store.connect(tmp_path / "resume.db")
    try:
        document = {
            "source": "乙县人民法院",
            "pages": [
                {"number": 1, "url": "https://c.example/p1_renamed.html"},
                {"number": 2, "url": "https://c.example/p3_half_broken.html"},
            ],
        }
        first = pipeline.ingest(document, con=con, fetcher=fetcher, max_retries=1)
        before = {key: data for key, data in store.all_records(con) if data["batch_id"] == first["batch_id"]}
        assert set(before) == {entry["entry_key"] for entry in first["result"].entries}

        state["fail"] = False
        again = pipeline.ingest(
            {"pages": []},
            con=con,
            fetcher=fetcher,
            resume_from=first["result"],
            max_retries=1,
        )
        after = {key: data for key, data in store.all_records(con) if data["batch_id"] == again["batch_id"]}
    finally:
        con.close()

    assert again["batch_id"] == first["batch_id"]
    assert before.items() <= after.items()  # 原有条目 JSON 完全未被改写
    assert len(after) == len(before) + 2  # 只新增恢复页的两条
    assert again["errors"] == 1  # 坏行仍被记录，但不阻塞


# ---------------------------------------------------------------------------
# 混合批次：表头换名 + 空页 + 半张坏表 + 旧格式放在同一批次
# ---------------------------------------------------------------------------


def mixed_document():
    return {
        "source": "混合公示栏",
        "pages": [
            {"number": 1, "html": read("p1_renamed.html")},
            {"number": 2, "html": read("p2_empty.html")},
            {"number": 3, "html": read("p3_half_broken.html")},
            {"number": 4, "html": read("p4_legacy.html")},
        ],
    }


def test_mixed_batch_reports_success_count_and_failure_locations():
    result = gp.parse_board(mixed_document())

    # 成功条目：3（换名表头）+ 0（空页）+ 2（坏表幸存行）+ 2（旧格式）= 7
    assert len(result.entries) == 7
    assert result.stats["parsed"] == 7
    assert result.stats["failed_rows"] == 1
    assert [page["status"] for page in result.pages] == ["ok", "empty", "partial", "ok"]

    # 失败位置精确到页/行/表/字段；空页只是警告。
    assert [(i["page"], i["row"], i["table"], i["code"], i["field"]) for i in result.errors] == [
        (3, 3, 1, "required_field_missing", "title")
    ]
    assert result.ok and not result.complete
    assert "page_empty" in issue_codes(result.warnings)
    assert "legacy_layout_assumed" in issue_codes(result.warnings)

    # 每条成功条目都能回溯来源与批次。
    assert {entry["source"] for entry in result.entries} == {"混合公示栏"}
    assert {entry["batch_id"] for entry in result.entries} == {result.batch_id}
    assert sorted(entry["page"] for entry in result.entries) == [1, 1, 1, 3, 3, 4, 4]


def test_mixed_batch_is_stable_across_repeated_fetch_and_page_order():
    first = gp.parse_board(mixed_document())
    again = gp.parse_board(mixed_document())
    reordered = gp.parse_board(
        {**mixed_document(), "pages": list(reversed(mixed_document()["pages"]))}
    )

    assert first.batch_id == again.batch_id
    assert [entry["entry_key"] for entry in first.entries] == [
        entry["entry_key"] for entry in again.entries
    ]
    # 页序变化不改变条目主键集合，只改变条目在结果中的顺序。
    assert {entry["entry_key"] for entry in first.entries} == {
        entry["entry_key"] for entry in reordered.entries
    }


def test_resume_ingest_reports_written_and_skipped(tmp_path):
    state = {"fail": True}
    pages = {
        "https://c.example/p1.html": (FIXTURES / "p1_renamed.html").read_bytes(),
        "https://c.example/p3.html": (FIXTURES / "p3_half_broken.html").read_bytes(),
    }

    def fetcher(url):
        if url.endswith("p3.html") and state["fail"]:
            raise TimeoutError("down")
        return pages[url]

    con = store.connect(tmp_path / "counts.db")
    try:
        document = {
            "source": "乙县人民法院",
            "pages": [
                {"number": 1, "url": "https://c.example/p1.html"},
                {"number": 2, "url": "https://c.example/p3.html"},
            ],
        }
        first = pipeline.ingest(document, con=con, fetcher=fetcher, max_retries=1)
        assert first["written"] == 3 and first["skipped"] == 0

        state["fail"] = False
        resumed = pipeline.ingest(
            {"pages": []},
            con=con,
            fetcher=fetcher,
            resume_from=first["result"],
            max_retries=1,
        )
    finally:
        con.close()

    # 3 条原有公告被跳过、不重写；只写入恢复页的 2 条。
    assert resumed["written"] == 2
    assert resumed["skipped"] == 3
    assert resumed["rows"] == 5
