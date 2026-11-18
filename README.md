# gazettetrack — 各地法院/监管公示栏异构表格抓取，主体消歧，跨地区重复公示合并，增量与历史版本。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线、幂等性和附件证据规则检查
```

环境没有 pytest 时，附件测试可用标准库运行器执行（同一套用例，零依赖）：
```text
PYTHONPATH=. python3 tests/attach_runner.py
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`entity_resolve`, `versioning`, `gazette_parse`, `dedupe`, `attach`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

### 附件处理（attach 阶段）
`attach` 接收上游解析出的公告身份（`announcement_id`，缺省回退 `id`）与版本（`version`，
缺省回退 `ts`），读取记录上的 `attachments` 声明，完成下载校验、证据归档与状态推进，
把来源 URL、页码/区域定位、解析状态写回记录后进入现有 keyer/store 持久化流程。

附件声明结构（分页与压缩包均支持）：
```json
{
  "announcement_id": "a-1",
  "version": "2026-11-17",
  "attachments": [
    {"ref": "att-1", "name": "doc.pdf", "kind": "pdf",
     "expected_digest": "可选 sha256", "url": "https://src/a-1/doc.pdf",
     "region": {"x": 10, "y": 20, "w": 300, "h": 400}},
    {"ref": "att-2", "name": "paged.pdf", "kind": "pdf", "page_total": 2,
     "pages": [
       {"page": 1, "url": "https://src/a-1/p1.pdf", "region": {"page_label": "第1页"}},
       {"page": 2, "url": "https://src/a-1/p2.pdf"}]}
  ]
}
```

模块拆分（单一职责）：
- `attach_fetch.py`：下载抽象。默认 `OfflineFetcher` 只回放注册字节、绝不联网；
  `HttpFetcher` 基于 httpx，把 404/410 归类为 missing、其余 4xx/5xx 与传输异常归类为 transient。
- `attach_blob.py`：不可信字节处理——sha256 摘要、魔数嗅探、zip 安全展开
  （不按成员名拼路径，zip-slip 防护；重名成员按序号全部保留）。
- `attach_store.py`：附件证据 sqlite（`attachments.db`）与内容寻址 blob 归档（`archive/blobs/`）。
- `attach.py`：阶段编排与状态机，通过 `AttachContext` 注入下载器、路径与策略参数。

### 附件规则（缺失待补 vs 判定失败、摘要确认时点）
1. **内容寻址、只追加**：证据按完整字节流的 sha256 归档为 `blobs/ab/cd/<digest>`，
   文件名/URL 只是属性。附件名不变而内容替换 → 产生新证据；槽位与证据的绑定表只追加，
   重复下载永不覆盖历史版本，旧证据通过 `attach_store.read_evidence_bytes` 始终可读。
2. **摘要确认时点**：完整下载结束后才计算 sha256。声明了 `expected_digest` 时必须相等，
   不匹配则**不归档坏内容**、记 `digest_mismatch` 并进入硬校验重试（默认 2 次），
   超限判 `failed`；未声明摘要时，以实际计算值作为确认摘要登记证据。
3. **缺失待补 vs 判定失败**：
   - 瞬时错误（连接失败、5xx、离线未注册 URL）→ 槽位 `pending`，**无限期待补**，每轮重跑重试；
   - 404/410 → `pending` 待补宽限（默认 3 次尝试，`MAX_MISSING_GRACE`），超限判 `failed`；
   - 摘要不匹配等硬校验错误 → 重试窗口（默认 2 次，`MAX_HARD_RETRIES`）后判 `failed`；
   - 已 `verified` 的槽位默认跳过下载；显式 `refresh=True` 才重新校验，且只追加绑定。
4. **分批与部分失败**：公告正文与已完成附件先行落库，单槽位失败不牵连同公告其它附件。
   公告级 `attach_status`：全部验证 `complete` / 部分验证 `partial` /
   无任何已验证证据 `absent`（含全部待补或失败，保持待补语义）；失败槽位数另计。
5. **分页附件**：同一附件按 `page_no` 拆为多个槽位、共享 `group_key`，每页独立
   URL、区域定位与重试；缺页期间公告为 `partial`，补齐后转 `complete`。
6. **压缩包**：按包内序号展开全部成员（含重名成员）逐一内容寻址归档，记录成员名、
   序号与摘要；包损坏只影响解析状态（`parse_failed`），外层证据保留可追溯。
7. **解析状态**：基于魔数做结构级校验（pdf/image/archive），声明类型与魔数不符标
   `parse_failed` 但证据保留；不渲染、不执行任何下载内容。
8. **不可信数据隔离**：每次下载使用独立暂存目录，blob 只经模块句柄读取；
   `archive/` 与 `*.db` 已在 `.gitignore` 中。

## 持久化与测试
记录通过项目内 SQLite（`gazettetrack.db`）存储，使用稳定键执行幂等写入；
附件证据使用独立的只追加证据库（`attachments.db` + `archive/blobs/`），与记录库生命周期分离。
测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。
