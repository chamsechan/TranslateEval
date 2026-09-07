# 查询、摘要与比较修复验收

日期：2026-09-07。总报告见 [容量优化报告](../../scale-optimization-2026-09-07.md)，修复前证据见 [原查询评审](../../scale-review-2026-09-07/query-findings.md)。本目录保留独立合成数据库上的原始输出与可复跑脚本；未修改业务数据库，未调用真实 LLM。

## 实现与一致性契约

- 终态任务在 `AggregateScore` 中保存内部 `query_summary_v1` 摘要，包含各语言计数、均分、默认阈值通过数、有效样本集合与实际评分来源摘要。Worker 终态提交前生成；历史任务可使用 `python -m app.read_models` 预计算，也可首次读取时补建。`public_aggregates` 隐藏所有内部摘要及构建标记。
- 比较、概览、通过率与有效评分过滤共用 `score_validity.py`：只接受匹配评测类型、单位和固定量表的有效分数。LLM 严格为 0–10；BLEU 为 0–100，允许并规范化 1e-9 边界误差。严格比较验证完整有效覆盖、样本集合、请求配置与实际来源；相同缓存来源信息按样本匹配，不以请求配置代替真实评分来源。
- 摘要构建以 `yield_per=1000` 流式读取窄列，逐行维护固定数量的计数与摘要，不装载全部 `ScoreResult` ORM 对象及评语、响应文本。终态列表和数值排序直接读取摘要；活动任务仍实时汇总。
- 新动态阈值精确计算通过数，并最多保留 8 个动态阈值结果。写入前检查摘要 generation；并发重试使 generation 失效时，从一致的实时查询重新计算全部字段，避免旧覆盖率与新通过数混用。
- 17 个 SQLite 触发器对任务状态、评分、预测、样本与评测配置的相关变更删除派生摘要和正在构建的 token。即使首次构建尚无摘要，构建期间的变更也会删除 token，阻止旧快照发布。短事务发布检查 token 与任务状态/更新时间；历史构建不在整个扫描期间持有写锁。
- 样本明细先计数和选取一页 ID，再加载该页内容；默认排序去掉非空字段多余的 `IS NULL` 排序。分数、评语等全量排序仍需扫描相关数据，但排序中间结果只保留窄列。
- 版本语言计数保存为 `DatasetVersion.language_counts`；新导入写入、0006 回填旧版本，NULL 数据使用 SQL 回退。0006 幂等添加 `ix_items_job_status_id` 和 `ix_items_score_result`，固化触发器 DDL，执行 `ANALYZE`；降级同时清理摘要和构建 token。连接缓存限定为 32 MiB，常规维护使用有界 `PRAGMA optimize`。

实现入口：`backend/app/read_models.py`、`read_model_schema.py`、`score_validity.py`、`queries.py`、`api.py`、`database.py`、`models.py` 与 `backend/alembic/versions/0006_query_read_models.py`。0007 后台导入迁移由导入部分实现，本次实际迁移验证包含它。

## 基准口径

一个合成数据集、两个完整版本，每版本 40 个语种 × 10,000 条，共 400,000 条。初始两个完整评测任务共 800,000 条 `DatasetSample`、`Prediction`、`EvaluationItem`、`ScoreResult`；扩展到六个历史任务后有 2,400,000 条 `EvaluationItem`，后四任务复用已有评分，`ScoreResult` 仍为 800,000 条。

脚本以 Core 分批生成夹具，绕过导入和 Worker，因此生成耗时不代表导入或真实评分吞吐。每个 API case 在独立子进程运行，使用 FastAPI TestClient 和实际路由，单 case 超时 90 秒，断言 HTTP 200 与总量、语言数、比较条件。时间包括查询、读取及序列化，不含 Python 模块导入和建库。未清空操作系统缓存，单机共享 CPU，无并发负载；结果不能当作并发 SLA。

RSS 使用各子进程的 `ru_maxrss`，表中峰值是整个 case 进程的最大常驻内存，并非请求净增量；进程基线约 74 MiB。`slowest_cursor_execute_seconds` 只记录 DBAPI execute，不包含取行、ORM 解码或 JSON 序列化，不能代替 API 总耗时。

## 400,000 条 / 两任务结果

除单列的历史首次构建外，修复后为摘要已就绪状态。修复前取原评审未显式 ANALYZE 的记录；原评审另测 ANALYZE 后仍存在相同查询瓶颈。

| 操作 | 修复前，秒 | 修复后，秒 | 说明 |
| --- | ---: | ---: | --- |
| 数据集列表 | 0.578 | 0.064 | 使用版本语言摘要 |
| 版本列表 | 1.048 | 0.063 | 两版本，40 个语种 |
| 默认阈值概览 | 2.675 | 0.072 | 终态摘要 |
| 明细首页 | 3.942 | 0.123 | 每页 50 条 |
| 明细末页 | 11.070 | 0.142 | 第 8,000 页 |
| 明细按分数排序 | 4.439 | 2.676 | 仍有全量扫描与排序 |
| 单语种明细 | 0.189 | 0.085 | 单语种 10,000 条 |
| 精确样本 ID | 0.080 | 0.089 | 返回 1 条，小量波动 |
| 结果列表，两行 | 5.386 | 0.114 | 默认排序 |
| 均分排序取一行 | 7.703 | 0.145 | 排序使用持久化摘要 |
| 两任务比较 | 40.817 | 0.085 | 峰值 RSS 2,492.2 → 76.6 MiB |
| 新动态阈值 7.5 | — | 2.098 | 首次精确通过数计算 |
| 相同动态阈值重复 | — | 0.069 | 复用阈值计数 |

历史终态任务首次补建摘要的两任务比较为 **25.318 秒 / 峰值 113.6 MiB**，然后降至表中的约 0.085 秒。此耗时尚未完全移除，升级时可预计算以避免首次页面承担它；新任务由 Worker 终态生成。原始记录分别见 `cold-legacy-400000.jsonl`、`warm-400000.jsonl`、`warm-counts-dynamic-400000.jsonl`。后一个文件的数据集/版本结果是在进一步移除多余回退条件后的最终实现。

## 六个历史任务

| 操作 | 修复前已 ANALYZE，秒 | 修复后摘要已就绪，秒 |
| --- | ---: | ---: |
| 结果列表，六行 | 18.254 | 0.185 |
| 均分排序取一行 | 20.670 | 0.121 |
| 两任务比较 | — | 0.085 |
| 新动态阈值 7.5 | — | 2.251 |
| 相同动态阈值重复 | — | 0.072 |
| 明细按分数排序 | — | 2.618 |

六任务的全部终态摘要以实际 CLI `--rebuild` 预计算，共 **65.853 秒**，之后再运行各独立 API case。数据库约 **3,328 MiB**，包含两完整版本、800,000 条评分及六任务明细；不是单任务存储估算。原始输出见 `history6-400000.jsonl`、`precompute-history6.jsonl`、`warm-history6-400000.jsonl`。

## 回归与迁移

查询专项最终命令：

```sh
.venv/bin/pytest backend/tests/test_query_read_models.py backend/tests/test_result_inspection.py backend/tests/test_table_sorting.py backend/tests/test_result_navigation.py --maxfail=2
```

结果 **78 passed in 36.48s**。覆盖无效评分与量表、混合实际来源、严格比较、内部摘要不可见、动态阈值准确性/容量、取消重试失效、动态阈值计算中重试、首次构建期间的评分/预测/样本/明细并发修改、旧 schema 升级与重复升级、降级清理，以及明细 SQL 形状。最终 `git diff --check` 通过。

40 万条夹具实际运行 `alembic upgrade head` 到 **0007**，确认两个版本语言计数均 400,000、两个新索引、17 个触发器、后台导入表，以及已有两条摘要；见 `migration-400000.json`。夹具原为当前 metadata 建表，这项验证覆盖幂等/大表迁移；真正缺列缺索引旧 schema 的升级由回归测试单独覆盖。

SQL 轨迹压缩包 `query-sql-traces.zip` 保留两任务/六任务末次查询 SQL，`explain-history6.json` 保留六任务计划。同名轨迹会被后续 case 覆盖，所以包内不能视为首次构建的 SQL 轨迹。

## 复跑

以下命令使用独立临时路径。约需 3.3 GiB 数据库空间，生成阶段和预计算各需数分钟；脚本拒绝覆盖已有夹具，禁止把夹具放进仓库。

```sh
.venv/bin/python docs/optimization-2026-09-07/queries/benchmark_queries.py seed /tmp/translateeval-query-probe.db --samples 400000
.venv/bin/python docs/optimization-2026-09-07/queries/benchmark_queries.py run /tmp/translateeval-query-probe.db --cases compare_two
TRANSLATION_EVAL_DATABASE_URL=sqlite:////tmp/translateeval-query-probe.db .venv/bin/alembic upgrade head
.venv/bin/python docs/optimization-2026-09-07/queries/benchmark_queries.py run /tmp/translateeval-query-probe.db
.venv/bin/python docs/optimization-2026-09-07/queries/benchmark_queries.py history /tmp/translateeval-query-probe.db --jobs 6
PYTHONPATH=backend TRANSLATION_EVAL_DATABASE_URL=sqlite:////tmp/translateeval-query-probe.db .venv/bin/python -m app.read_models --rebuild
.venv/bin/python docs/optimization-2026-09-07/queries/benchmark_queries.py run /tmp/translateeval-query-probe.db --cases results_default results_mean_one compare_two summary_dynamic summary_dynamic items_score
```

本次合成库及 sidecar 已在归档 SQL 轨迹后清理，清理记录见 `cleanup.json`。低成本缩小复跑可将样本数改成 24,000（每语种 600 条）；它无法替代上限规模验证。多个活跃任务的实时聚合、超出六个历史任务的库规模、多用户并发、网络延迟均不在本次查询基准覆盖范围内。
