# 导入实现独立只读审查

审查范围：`backend/app/import_index.py`、`importers.py`、相关 API 调用和新回归测试。没有修改后端实现，也没有使用真实业务库。本次未发现阻塞发布的问题。

- **历史哈希兼容**：磁盘索引按 `sample_id` 排序，数据集逐项调用已有 `sample_content_hash` 并保留换行分隔；预测保留旧版 JSON 键排序、紧凑分隔、UTF-8 与 NFC 规范化。现有测试同时覆盖输入乱序、中文 ID、组合字符和 CR/LF；结果与原实现一致。
- **并发幂等**：`_claim_import` 通过有条件的状态更新获取发布权，claim 与样本/预测写入在同一事务。等待者重新读取 report；提交 API 再次检查既有 task，避免第二个任务。已有并发 submission 测试通过，补充探针确认相同 dataset report 并发提交返回同一版本。
- **文件变化**：提交前重新读取并验证哈希、重复 ID 和完整匹配；实际写入来自已经核验的私有磁盘索引。探针确认合法 JSON 中仅修改源文或译文也被拒绝；在核验完成后、claim 前替换原文件，则仍只发布已经核验的快照，不会读入替换后的内容。
- **资源清理**：数据集使用上下文管理器，多个预测索引使用 `ExitStack`。正常退出与读取异常均删除私有索引；prepare 失败清理自身 staging。已核验/提交 staging 的保留属于现有持久记录策略。强杀进程后的 `/tmp` 索引清理未覆盖，也未在现有代码中发现启动清扫；这不影响事务回滚与后台重放，但大量反复强杀时应由部署环境或后续维护策略处理遗留临时目录。

验证：

```bash
.venv/bin/pytest -q backend/tests/test_import_scaling.py backend/tests/test_editable_imports.py backend/tests/test_background_imports.py
.venv/bin/python docs/optimization-2026-09-07/frontend/import_review_probe.py
```

三组现有回归共 **39 项通过**（16 + 19 + 4），只有现有 Starlette/httpx 弃用提示。额外隔离探针 **6 项通过**，见 [脚本](./import_review_probe.py) 与 [结果](./import-review-evidence.json)。探针只创建临时数据库和文件，并在退出时清理。本审查不替代主流程的全量测试、迁移和真实 API/Worker 验证。
