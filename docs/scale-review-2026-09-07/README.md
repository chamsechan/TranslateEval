**TranslateEval 规模验证材料**

对应 [多数据集、多版本、30–40 语种规模评价](../scale-review-2026-09-07.md)。全部为独立审查材料，未修改产品逻辑或实际业务数据库。24,000 / 400,000 是单个数据版本/单评价器任务的样本规模，不是整个夹具的累计记录数。

| 材料 | 验证范围 |
| --- | --- |
| [import-worker-findings.md](import-worker-findings.md) | 实际导入、版本差异、预测提交、任务创建、Worker 装载、进度重算与内存 |
| [import_worker_probe.py](import_worker_probe.py)、[import-worker-evidence.json](import-worker-evidence.json) | 上述流程的子进程隔离探针及 20 阶段原始测量 |
| [import_lock_probe.py](import_lock_probe.py)、[import-concurrent-writer-evidence.json](import-concurrent-writer-evidence.json) | 长版本提交期间，另一个写连接 30 秒超时；探针不提交修改 |
| [cache_probe.py](cache_probe.py)、[cache-evidence.json](cache-evidence.json) | 200 个缓存内容键在 2.4 万 / 40 万历史评分库中的耗时和 ANALYZE 前后查询计划 |
| [query-findings.md](query-findings.md)、[benchmark_queries.py](benchmark_queries.py) | 使用 Core 合成合法大库后的 API 基准与分析；含分页、筛选、汇总、比较与历史结果 |
| [query-results-24000.jsonl](query-results-24000.jsonl)、[query-results-400000.jsonl](query-results-400000.jsonl) | API 原始测量；统计维护后的复测及其他条件详见文件内 case 名称 |
| [frontend-review.md](frontend-review.md) | 40 语言、多个数据集和版本下的逐页面评价与截图索引 |
| [frontend-scale.mjs](frontend-scale.mjs)、[frontend-verification.json](frontend-verification.json) | Chromium 模拟 API 的布局/交互验证；不是数据库性能测试 |

导入及缓存探针使用临时目录，执行结束清理数据库；没有真实评分调用。导入阶段限时 180 秒，当前 24k/400k 全部阶段成功。API 查询探针的参数及清理方式见脚本帮助；浏览器脚本需要本环境 Playwright/Chromium，具体启动说明见 frontend-review.md。

```bash
.venv/bin/python docs/scale-review-2026-09-07/import_worker_probe.py --help
.venv/bin/python docs/scale-review-2026-09-07/cache_probe.py
.venv/bin/python docs/scale-review-2026-09-07/benchmark_queries.py --help
```

测量环境约 23 GiB 内存，测试时可用约 20 GiB，Python 3.12。独立探针可能并行占用 CPU/磁盘，操作系统页缓存未清空，不能视作目标部署环境的稳定 p95。结果足以定位全量加载、写锁、重复扫描和长页面问题，但没有跑 40 万次真实评分或完整多人持续负载。
