# gazettetrack — 各地法院/监管公示栏异构表格抓取，主体消歧，跨地区重复公示合并，增量与历史版本。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线、解析器与幂等性检查
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`entity_resolve`, `versioning`, `gazette_parse`, `dedupe`, `attach`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 模块说明
业务模块按独立阶段组织，通用模块负责加载、规范化、主键、存储和编排。
流水线按配置顺序调用处理阶段，输入输出均遵循项目定义的记录结构。

### 异构公示栏解析（`gazette_parse`）
`parse_board(document, ...)` 接收 HTML 文本 / URL / 分页列表 / 表格列表 / 行列表，
产出 `ParseResult`，把三类产物分开交给下游：

| 产物 | 内容 | 下游 |
| --- | --- | --- |
| `result.entries` | 结构化公告条目（含 `source` / `batch_id` / `page` / `row` / `locator`） | `dedupe` → `keyer` → `store` |
| `result.attachments` | 附件线索（URL、文件名、类型、状态），不含下载内容 | `attach` → `store` |
| `result.errors` / `result.warnings` | 带定位信息的局部错误与降级提示 | `store` 的批次错误表 / 告警 |

`result.routes()` 返回同名的显式三路交付；`pipeline.ingest(document, ...)` 是落库入口。

**局部失败不扩散**

- 一行坏数据：该行跳过并记录 `page/row/table/field`，同页其余行照常入库。
- 一页抓取失败：按 `max_retries` 重试该页；耗尽后该页标记 `failed`，其他页照常。
- 一张表无法识别表头 / 解析异常：只跳过该表。
- 整份文档无法识别：返回单条 `document_unrecognized` fatal 错误，而不是抛异常。

**字段契约**：`FieldContract`（`required` / `block_on_invalid` / `nullable` / `guaranteed`）
决定降级边界 —— 必填字段缺失或非法只阻断当前条目；可选字段非法降级为 `None` 并记警告。
默认只要求 `title`，可按业务收紧，例如 `{"required": ["title", "publish_date"]}`。

**异构兼容**：表头别名（中英文、全半角、括号单位、模糊拼写）、无表头旧格式位置兜底、
`colspan`/`rowspan`、`<thead>`/重复表头、嵌套表格、GB18030 编码、空值词（`—`/`无`/`N/A`）、
日期与金额多种写法、表头单位换算（「金额（万元）」）。

**分页与重复抓取**：跟随「下一页」链接（有页数上限、URL 去重）；条目主键由公告内容派生，
与页码/行号/批次无关，重复抓取得到同一主键，可幂等落库；批内重复自动合并。
`resume_from=上一次结果` 只重试失败/空页并合并，未重试页面的条目原样复用。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；解析批次、附件线索与解析错误
分别保存在 `batches` / `attachments` / `issues` 表。测试夹具位于 `fixtures/gazette/`
（表头换名、空页、半张损坏表、旧格式、分页、附件），运行方式以 `Makefile` 为准。
