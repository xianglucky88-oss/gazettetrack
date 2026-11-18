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

### entity_resolve（主体消歧）
- 规范值：规范名称、统一社会信用代码/案号等标识、规范地址，由主体观察集合确定性重算；
- 稳定主体键：`ent-u-*`（信用代码锚点）/ `ent-n-*`（核心名+地区锚点），输入换序、重复导入、重启均不变；
- 同主体异名靠标识与别名合并，同名不同主体靠标识与地区拆分；无法自动判定的记录
  `entity_status=pending`，候选与人工裁决依据写入 `candidates`/`adjudication`；
- 标识一致而地址冲突：标识优先归为同一主体，冲突显式留在实体 `conflicts`（含取舍 policy
  与证据记录 id），不做静默覆盖；
- 主体注册表落 `entity_registry.json`（原子写，可由公示记录幂等重建，`make clean` 可清除）。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。
