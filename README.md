# gazettetrack — 各地法院/监管公示栏异构表格抓取，主体消歧，跨地区重复公示合并，增量与历史版本。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线和幂等性检查
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`entity_resolve`, `versioning`, `gazette_parse`, `dedupe`, `attach`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 模块说明
业务模块按独立阶段组织，通用模块负责加载、规范化、主键、存储和编排。
流水线按配置顺序调用处理阶段，输入输出均遵循项目定义的记录结构。

## 公示版本历史（`versioning.py`）
每次抓取结果先规范化为一条**观测**（正文指纹、附件指纹/版本、解析批次、来源时间、
来源序号），再按来源证据推导版本链，识别 `first_publish` / `update` /
`attachment_replace` / `retract` / `republish` 五种生命周期事件。

- **排序有来源依据**：来源时间 → 来源序号/修订号 → 事件语义 → 抓取时间 →
  内容指纹兜底；每个版本都带 `order_evidence` 记录先后由哪个字段决定。
- **观测与版本分离**：`version_observations` 保存观测，`version_history` 由观测重放推导，
  因此通知日期乱序、先收到撤回再补到原文、部分抓取中断恢复都得到一致结果。
- **撤回事实不可覆盖**：撤回版本保留在链中，`ever_retracted` / `retraction_count`
  由整条链推导；撤回后重新发布即使正文相同也会得到新的版本键。
- **可从历史重建**：`rebuild`/`replay` 从观测重建版本链，
  `current_from_history`/`materialize_current` 仅凭版本历史即可重建当前有效版并写回
  `records`；流水线收尾自动物化，连续运行与恢复重放的有效状态一致。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。
