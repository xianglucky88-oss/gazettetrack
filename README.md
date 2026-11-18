# gazettetrack — 各地法院/监管公示栏异构表格抓取，主体消歧，跨地区重复公示合并，增量与历史版本。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → 各业务阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量、落库行数和各阶段成本统计
make test    # 运行项目测试：基线兼容 + 跨地区去重/版本/撤销对照
```
可选参数（CLI）：

```text
GAZETTETRACK_FIXTURE=fixtures/gazette.json make run         # 换一份离线样本
python3 -m gazettetrack.cli --db /tmp/x.db --summary        # 落库摘要
python3 -m gazettetrack.cli --db /tmp/x.db --explain <key>   # 单条记录的解释报告
python3 -m gazettetrack.cli --db /tmp/x.db --undo-group <group_key>
python3 -m gazettetrack.cli --db /tmp/x.db --restore-group <group_key>
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`entity_resolve`, `versioning`, `gazette_parse`, `dedupe`, `attach`, `recovery`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli / report`。

阶段顺序：`normalizer → gazette_parse → entity_resolve → dedupe → versioning → attach → keyer → store`。
需要全量候选或持久化（主体别名、版本号、合并历史）的阶段提供 `process_batch(records, con)`，
其余阶段仍是逐条 `process(record)`；两种形态都可以独立调用。

## 各阶段职责
- `normalizer`：值级归一（NFKC、空白/日期/案号/附件/正文指纹），不做任何合并判断。
- `gazette_parse`：异构表格别名解析成统一公告字段；基线记录原样通过。
- `entity_resolve`：主体消歧，名称键/统一社会信用代码 → 稳定 `entity_id`（别名表持久化）。
- `dedupe`：候选分桶 + 可解释的两两判定 + 事项组合并；只回答「是不是同一事项」。
- `versioning`：内容签名与版本链；后续版本、重复转载、附件差异分别落不同版本语义。
- `attach`：把来源集合、差异证据与 `evidence_digest` 附着到记录上，并校验不变式。
- `recovery`：基于合并历史快照撤销/恢复一次合并。

## 缺案号时的候选门槛
没有案号时主体是唯一强身份，因此门槛更严（宁可漏合也不误合）：

1. 只有共享候选桶（`entity_id` / `matter_key` / `body_fp` / 案号）的记录才会比较；
   主体、案号前缀这类粗桶再按公示日期窗剪枝。附件指纹只作证据、不开桶。
2. 两侧都有主体时必须 `entity_id` 相同；主体缺失只允许「正文指纹与事项标题都相同」。
3. 事项标题明确相同 → 同一事项（后续正文差异按版本处理）；
   事项标题明确不同 → 独立事项，正文再像也不合并。
4. 仅正文相似度可用时，要求相似度 `>= 0.80` 且公示日期间距 `<= 90` 天。
5. 一侧正文缺失时标 `content_unknown`，绝不声称「无变化」。

判定结果连同 `reasons` / `signals` / `thresholds` 一起写入 `evidence`，可用
`--explain <key>` 回放；被判为独立的事项也会留下 `non_merges` 证据。

## 版本与历史
- 内容签名 = 正文指纹 + 附件指纹；跨地区转载同签名 → `unchanged`。
- 新签名 → 新版本（`body_revised` / `attachment_revised` / `body_and_attachment_revised`），
  版本号按公示日期排序，先抓到新版本再补旧版本也得到同一条版本链。
- head 记录的 `sources` 是所有版本来源的并集；每条版本保留自己的来源集合。
- `merge_history` 追加保存每条观测的完整快照，`undo_merge` 可拆回单条并恢复各自来源，
  `restore_merge` 可反向恢复；重复操作不会膨胀历史。

## 持久化与测试
- `records`：每个事项一个 head；`versions` / `matters` / `version_events`：版本链与审计；
  `merge_history` / `merge_suppressed`：合并决定与撤销抑制；`entity_aliases`：主体别名。
- 迁移兼容：启动时 `CREATE TABLE IF NOT EXISTS`，旧库（仅 `records`）可直接继续使用；
  基线记录的键值与字段形状保持不变。
- 测试夹具：`fixtures/input.json`（基线）与 `fixtures/gazette.json`（跨地区转载、
  正文修订、附件差异、同主体不同事项、相近案号、缺失正文）。运行方式以 `Makefile` 为准。
