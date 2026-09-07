**系统审查证据与复现**

对应 [当前设计与功能的系统性评价](../system-review-2026-09-07.md)，基线 `9b8c59eaeb027956c96d2dd39e6fa0d7909fecda`。这些文件是审查探针，不是修改后的产品回归测试；它们记录并复现当前缺陷，因此不能把脚本成功退出理解为“产品没有问题”。

| 文件 | 内容 |
| --- | --- |
| [import_probe.py](import_probe.py)、[import-evidence.json](import-evidence.json) | 无效语种导入、同时修改源文/GT 的差异统计、两个 Session 交错提交同一报告 |
| [metrics-probe.py](metrics-probe.py)、[metrics-evidence.json](metrics-evidence.json) | 参数边界、真实 BLEU Worker 越界评分、有效评分/比较分歧、语种识别分母、默认 BLEU 语义反例 |
| [metrics-findings.md](metrics-findings.md) | 评分专项的详细分析 |
| [queue_repro.py](queue_repro.py)、[queue_evidence.json](queue_evidence.json) | 重复 Worker 调用、在途筛选、取消后恢复、无网络评分的 SQLite 基准 |
| [frontend/README.md](frontend/README.md) | 实际 Chromium 正常工作流、取消错误反馈、导入元信息修正缺口的 JSON、截图及自含复现 runner |

在仓库根目录使用已安装的开发环境运行：

```bash
.venv/bin/python docs/system-review-2026-09-07/import_probe.py
.venv/bin/python docs/system-review-2026-09-07/metrics-probe.py
.venv/bin/python docs/system-review-2026-09-07/queue_repro.py --benchmark
```

这三个脚本分别创建临时数据库和暂存目录，使用本地 BLEU 或假评分器；不向外部 LLM 发送请求，不改实际业务数据库。队列基准使用合成样本扩充规模，分数是固定模拟值，不能用于判断翻译质量。

导入并发探针通过两个 Session 同时读到 validated 报告后交错提交，稳定模拟读状态竞争，不依赖线程时序；Worker 重复执行探针调用与启动时相同的恢复和处理函数，不是两个操作系统进程的压力测试。

基础验证实际结果：172 项 pytest 通过；生产构建成功；隔离新库 Alembic upgrade/check 成功；README BLEU demo 为 6/6 完成，corpus BLEU 43.5523。详细命令和限制见主报告。
