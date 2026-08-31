# 译研评测台

面向小语种到中文翻译模型的单机评测记录系统。它统一管理平行语料版本、模型推理结果、SacreBLEU 与 OpenAI 兼容 LLM 评分，并通过持久化队列完成并发、缓存、取消和失败重试。

## 功能

- 数据集不可变版本、内容哈希、版本差异和严格导入报告。
- `result_info.json` 自动登记模型族、检查点、备注、推理平台、模式与解码参数。
- 同一提交可并行选择 BLEU 和多个 LLM 评价配置。
- OpenAI 兼容评分支持 Prompt 历史、配置修订、1–64 并发、超时和重试。
- 按“源语种 + 源文 + GT + 候选译文 + 评分模型名”复用 LLM 历史评分，可强制重评。
- 永久保存逐句原始分数；前端任意选择 `score ≥ threshold`，即时计算宏/微平均分与准确率。
- 父任务和数据集子任务均可取消；Worker 重启后继续执行队列。
- 为未来自动语种识别保留 `predicted_language`，支持准确率、覆盖率和混淆矩阵统计。

## 快速开始

要求 Python 3.11+、Node.js 20+。

```bash
./scripts/setup.sh
./scripts/dev.sh
```

开发页面为 `http://localhost:5173`，API 文档为 `http://localhost:8000/docs`。

也可以构建前端并由 FastAPI 单端口提供：

```bash
./scripts/start.sh
```

此时访问 `http://localhost:8000`。Worker 与 API 为独立进程，共享 `var/translation_eval.db`。

## 推荐工作流

1. 打开“数据集”，导入目录或 ZIP；检查版本差异后提交不可变版本。
2. 在“评价设置”新增 OpenAI 兼容评价器、并发数和 Prompt 版本。系统已内置 SacreBLEU 中文配置及默认 LLM Prompt。
3. 打开“提交评测”，导入标准结果目录；系统按内容哈希匹配数据集版本。
4. 多选评价器并提交；在“任务队列”观察缓存命中、进度和错误，或取消某个数据集。
5. 在“评测结果”调整阈值、查看逐语种/逐句结果，或选择多个结果做同口径对比。

仓库包含可直接导入的演示文件：

- `examples/dataset/flores-demo`
- `examples/results/demo-run`

可以用独立临时数据库验证完整 BLEU 闭环：

```bash
TRANSLATION_EVAL_DATABASE_URL=sqlite:////tmp/translation-eval-smoke.db \
  .venv/bin/python scripts/smoke_demo.py
```

## 数据集协议

```text
dataset-root/
├── dataset_info.json
└── samples.jsonl
```

`dataset_info.json`：

```json
{
  "schema_version": 1,
  "dataset_key": "flores-devtest",
  "name": "FLORES DevTest",
  "version_label": "2026-08-30",
  "change_note": "刷新中文 GT",
  "description": "可选说明",
  "source_languages": [{"code": "de", "name_zh": "德语"}]
}
```

`samples.jsonl` 每行：

```json
{"sample_id":"de-000001","source_language":"de","source_text":"Guten Morgen","reference_zh":"早上好"}
```

`sample_id` 在同一数据集的不同版本间应保持稳定。导入采用 UTF-8 严格模式；重复 ID、未声明语种和空字段会阻止整批写入。

## 推理结果协议

```text
result-root/
├── result_info.json
└── <dataset_key>/
    └── predictions.jsonl
```

`result_info.json` 的完整示例见 `examples/results/demo-run/result_info.json`。关键字段：

```json
{
  "schema_version": 1,
  "run_name": "qwen3-exp-042",
  "model_family": "qwen3-0.6b",
  "checkpoint_name": "qwen3-0.6b-lora-step-8000",
  "model_version": "step-8000",
  "model_notes": "33语种混合微调",
  "inference": {
    "platform": "nvidia-1080ti",
    "device": "GPU",
    "precision": "fp16",
    "mode": "source_language_provided",
    "generated_at": "2026-08-30T12:00:00Z",
    "code_revision": "optional-git-sha",
    "decoding": {"temperature": 0, "top_p": 1, "max_new_tokens": 512}
  },
  "datasets": [{
    "dataset_key": "flores-devtest",
    "dataset_content_sha256": "64位数据集版本哈希"
  }]
}
```

`predictions.jsonl` 每行：

```json
{"sample_id":"de-000001","translation_zh":"早上好","predicted_language":"de"}
```

`predicted_language` 可省略。只有 `inference.mode=auto_detect` 时才统计语种识别能力；否则页面明确显示 N/A。

## 评分口径

- LLM 保存 0–10 浮点原始分及简短理由；非法 JSON 或越界分数会重试，不会截断。
- BLEU 保存 0–100 逐句分，并额外记录整体和逐语种 corpus BLEU、SacreBLEU signature。
- 微平均按所有成功样本等权；宏平均先按语种计算，再对语种等权。
- 阈值准确率分母是成功评分数；失败和取消不进入分母，但页面始终显示覆盖率。
- 缓存按已确认的宽松口径忽略 Base URL 和 Prompt 版本。每条缓存分仍显示实际模型、Base URL 和 Prompt 来源。

## 配置和运维

可用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TRANSLATION_EVAL_DATABASE_URL` | `sqlite:///var/translation_eval.db` | SQLAlchemy 数据库地址 |
| `TRANSLATION_EVAL_IMPORT_DIR` | `var/imports` | 导入暂存目录 |
| `TRANSLATION_EVAL_WORKER_CONCURRENCY` | `32` | Worker 全局 LLM 并发上限 |
| `TRANSLATION_EVAL_WORKER_POLL_SECONDS` | `0.5` | 空队列检查间隔 |
| `TRANSLATION_EVAL_WORKER_HEARTBEAT_FILE` | `var/worker-heartbeat` | Worker 健康心跳文件路径 |
| `TRANSLATION_EVAL_PORT` | `8000` | `start.sh` 服务端口 |

API Key 按需求明文保存在 SQLite；页面只回显掩码，但数据库备份仍包含密钥。备份时复制 SQLite 文件及其 `-wal`/`-shm` 文件，或停服后只复制主数据库文件。

数据库升级：

```bash
.venv/bin/alembic upgrade head
```

测试和构建：

```bash
.venv/bin/pytest
npm --prefix frontend run build
```
