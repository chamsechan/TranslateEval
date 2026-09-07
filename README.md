# TranslateEval

ARI-NLP · 作者：chen_qc · 2026.09

面向小语种到中文翻译模型的单机评测记录系统。它统一管理平行语料版本、模型推理结果、SacreBLEU 与 OpenAI 兼容 LLM 评分，并通过持久化队列完成并发、缓存、取消和失败重试。

## 功能

- 数据集不可变版本、内容哈希、版本差异和严格导入报告；数据集与版本支持检索和分页，样本支持语种筛选、精确 ID 查找、页大小和跳页。
- 自动登记模型、检查点和推理参数；模型详情展示设备、精度、代码版本及解码参数。
- 同一提交可选择 BLEU 和多个 LLM 评价配置；评价器任务依次执行，单个 LLM 评价器内部并发评分。
- LLM 配置和 Prompt 保留历史版本，Prompt 可复制任意历史版本，默认使用日期标签（如 `20260907.1`）；无评测结果或任务引用的 Prompt 可删除。支持评分缓存、超时和重试。
- 持久化保存逐句原始分数；调整阈值即可查看宏/微平均分、阈值通过率与覆盖率；模型比较提供语种矩阵、基线差值和逐句下钻。
- 大文件提交在后台执行，可从顶栏“导入进度”恢复查看与重试；数据以批次核验和写入，完成前不发布半个版本或半个提交。
- 父任务和数据集子任务均可取消；Worker 重启后继续执行队列。

## 快速开始

要求 Python 3.11+、Node.js 20.x（至少 20.19）或 22.12+。以下命令均在项目根目录执行，启动脚本使用 Bash。

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

1. 打开“数据集”，导入 JSONL、目录或 ZIP；补全或修改数据集信息，核验版本差异后提交不可变版本。
2. 在“评价设置”新增 OpenAI 兼容评价器、并发数和 Prompt 版本。系统已内置 SacreBLEU 中文配置及默认 LLM Prompt。
3. 在“模型与评价设置 → 导入下拉选项”维护被测模型、设备、平台、精度 / 量化位宽和推理模式。
4. 打开“提交评测”，导入预测 JSONL、目录或 ZIP；编辑预填信息或从零填写，检查点使用文本，选择数据集版本后自动带出内容哈希。
5. 多选评价器并提交；后台完成预测导入后进入“任务队列”，观察缓存命中、进度和错误，或取消某个数据集。刷新或关闭页面不影响后台处理，顶栏“导入进度”可以恢复查看。
6. “评测结果”列表直接显示每次评测的加权通过率、通过数 / 总数、默认阈值、已评分平均分及未评分数，可搜索模型、数据集或版本并按状态筛选。点击“分析结果”，查看各语种通过率及逐句明细；按语种、状态、分数（等于、小于、小于等于、大于、大于等于）和完整样本 ID 组合筛选，展开核对译文、评分理由和异常。调整阈值及筛选后，刷新或重新打开当前链接会保留条件；从对比表进入详情沿用对比阈值。
7. 维护评分服务配置或更新 Prompt 后，从任务、结果详情或模型运行详情点击“再次评测”，复用原预测创建新任务。验证新 Prompt 的效果时开启“强制重新评分”。

“重试失败项”继续使用原任务的配置快照；“再次评测”可以选择最新配置。两者分别用于恢复原实验和创建新实验，历史记录保持可追溯。

数据集目录和版本历史使用可检索的分页表，展示“源语言 → 中文”及对应句数，点击语言对即可预览该语种样本。“查看评测结果”限定到所选数据集版本；结果中的“查看评测数据集”和逐句展开后的“核对数据集原文”也始终打开实际评测版本。样本预览支持按中文名 / 代码筛选和精确 ID 查找，语言名仅用于显示，未知代码保留原样。数据集页面的预览条件与页码也会保存到链接。早期评审见 [易用性评审记录](docs/usability-review-2026-09-07.md)，本轮页面调整见 [规模优化记录](docs/scale-optimization-2026-09-07.md)。

仓库包含可直接导入的演示文件：

- `examples/dataset/flores-demo`
- `examples/results/demo-run`

以下命令每次创建独立的临时数据库和导入目录，验证 BLEU 流程后自动清理，可重复运行：

```bash
(
  set -e
  smoke_dir="$(mktemp -d)"
  trap 'rm -rf "$smoke_dir"' EXIT
  TRANSLATION_EVAL_DATABASE_URL="sqlite:///$smoke_dir/eval.db" \
  TRANSLATION_EVAL_IMPORT_DIR="$smoke_dir/imports" \
    .venv/bin/python scripts/smoke_demo.py
)
```

## 数据集协议

界面导入时，`dataset_info.json` 可选：提供时将内容预填到可编辑表单，未提供时从零填写。源语种始终从样本的 `source_language` 自动汇总，无需在 JSON 中维护 `source_languages`，导入表单只读展示检测到的语种代码。中文名称沿用平台已有语种名称，新语种默认使用代码。可以直接上传 `samples.jsonl`，或导入下列目录 / ZIP。新版本沿用已有数据集名称和说明，版本标签和变更备注独立填写。

```text
dataset-root/
├── dataset_info.json  # 界面导入时可选
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
  "description": "可选说明"
}
```

`samples.jsonl` 每行：

```json
{"sample_id":"de-000001","source_language":"de","source_text":"Guten Morgen","reference_zh":"早上好"}
```

`sample_id` 在同一数据集的不同版本间应保持稳定。导入采用 UTF-8 严格模式；重复 ID、无效语种代码、空 ID、空源文或空参考译文会阻止整批写入。同一数据集不能重复导入已有版本标签或相同样本内容。

语种代码去除首尾空白并转为小写，支持 `de`、`zh-Hant-TW` 和 `eng_Latn` 等形式：首段为 2–8 个英文字母，后续连字符或下划线分隔的段为 1–8 个字母或数字，总长不超过 35。格式合法的未知代码可以导入，不限于预置语言。源文和参考译文同时变化时会分别计入两个维度，并显示“源文与参考译文均变化”，计数允许重叠。

## 推理结果协议

界面导入时，`result_info.json` 可选。单个数据集可以直接上传 `predictions.jsonl`，在表单中选择对应数据集及版本；多个数据集使用下面的子目录结构，子目录名对应 `dataset_key`。选择版本后自动填写其内容哈希，无需手工复制。JSON 中已有哈希若未匹配，需明确选择正确版本后重新核验。

模型、设备、平台、精度 / 量化位宽、推理模式使用可搜索下拉框；检查点为文本输入。选项支持新增、修改显示名称、启用 / 停用，以及编辑窗口内的红色删除按钮，选项值创建后固定。有导入结果的选项删除前需要确认，删除保留历史结果，重启后也不会自动恢复。导入 JSON 中尚未维护的值会预填为临时选项，成功提交后加入选项库；已停用的导入原值可以保留。维护界面可从导入表单直接打开。

设备包含选项值、设备名称（如英伟达1080Ti、英伟达2080Ti、爱芯650）、所属平台、SDK 和 SDK 版本。SDK 选项按平台区分；导入时可从设备配置带出信息，并将 `inference.sdk`、`inference.sdk_version` 保存到本次运行快照。推理模式默认提供 `default`（默认）、`thinking`（思考）、`non_thinking`（不思考）。

所有修改都要重新核验。提交保存最终元信息快照，选项名称或推理模式统计设置的后续变更不改写历史导入。源 JSON 文件不会被修改。

```text
result-root/
├── result_info.json  # 界面导入时可选
└── <dataset_key>/
    └── predictions.jsonl
```

完整结果清单的必填字段如下；界面预填也接受缺少字段的 JSON，再通过表单补齐。模型版本、备注和完整推理参数见 [演示清单](examples/results/demo-run/result_info.json)：

```json
{
  "schema_version": 1,
  "run_name": "qwen3-exp-042",
  "model_family": "qwen3-0.6b",
  "checkpoint_name": "qwen3-0.6b-lora-step-8000",
  "inference": {
    "platform": "nvidia-1080ti",
    "mode": "default"
  },
  "datasets": [{
    "dataset_key": "flores-devtest",
    "dataset_content_sha256": "64位数据集版本哈希"
  }]
}
```

将 `dataset_content_sha256` 的占位文字替换为已导入数据集版本的 64 位十六进制内容哈希，可在“数据集”的版本列表复制。该值由系统对样本内容规范化后计算，不是对 JSONL 文件直接执行 SHA-256；`dataset_key` 和内容哈希必须共同匹配已导入版本。

`predictions.jsonl` 每行：

```json
{"sample_id":"de-000001","translation_zh":"早上好","predicted_language":"de"}
```

预测必须完整覆盖对应数据集版本的全部 `sample_id`，每个 ID 恰好一条；缺失、重复、未知 ID 或空白译文会阻止整批导入。

`predicted_language` 可省略，提供时使用相同的语种代码格式。思考模式与语种识别统计分别记录，新模式可通过 `inference.detects_language` 指定是否统计语种识别，默认不统计。历史 `auto_detect`、`source_language_provided` 及自定义模式继续兼容原有统计设置；旧记录未保存该字段时仍按 `mode=auto_detect` 判断。页面同时展示总体准确率（缺失识别计错）、已识别样本准确率及覆盖率，逐语种召回也区分这两个分母；混淆矩阵通过 API 获取。

API 的可编辑流程：`POST /api/{dataset|submission}-imports/prepare`（服务器路径）或 `prepare-upload`（ZIP / JSONL）读取草稿，再调用 `POST /api/import-reports/{id}/validate`，请求体为 `{"manifest": {最终元信息}}`。核验通过后使用新报告 ID 调用 `POST /api/import-reports/{id}/commit-job`；数据集请求体为 `{}`，预测提交包含 `evaluators` 和 `force_reevaluate`。返回 202 和后台任务 ID，使用 `GET /api/import-commit-jobs/{id}` 查看状态与完成结果，列表接口为 `GET /api/import-commit-jobs?limit=20`。失败后再次 POST 同一报告即可重试，空请求体复用上次配置。运行中或已完成的重复请求返回同一任务。

草稿不可直接提交。原有同步 commit、`validate` / `validate-upload` 接口继续兼容；后台提交复用相同的核验和原子发布规则。API 进程负责后台导入，独立 Worker 负责评分。API 重启后会恢复中断的导入请求；已经发布的数据不会因恢复而重复创建。

## 评分口径

- LLM 保存 0–10 数值原始分及简短理由；score 必须是有限 JSON 数值，布尔值和数字字符串均不接受。非法 JSON、空响应、非数值或越界分数按配置重试；明确的拒绝评分响应直接记为失败。
- BLEU 保存 0–100 逐句分，并额外记录整体和逐语种 corpus BLEU、SacreBLEU signature。任务结束（包括部分失败或取消）时按当前成功样本更新聚合并显示样本数；重试进行中不展示上一轮聚合。
- 微平均按所有成功样本等权；宏平均先按语种计算，再对有成功评分的语种等权。
- 单语种通过率为该语种 `score ≥ threshold` 的成功评分数除以该语种总样本数。加权平均通过率（兼容 API 字段 `micro_accuracy`）按各语种总样本数加权；宏平均通过率对数据集全部语种等权，全未评分语种按 0 计入。它是阈值指标，不等同于经人工确认的翻译准确率。
- 未评分样本（评分失败、取消、等待中、评分中或尚未建立评分记录）在通过率中计为未通过，保留原始任务状态供排查，不伪造 0 分。评分覆盖率为成功评分数除以总样本数；平均分及 corpus BLEU 仍仅使用成功评分样本。调整通过阈值不改变原始分数或明细的分数筛选条件。
- 逐句明细包含数据集的全部样本。“未评分（全部）”可筛出所有未获得有效评分的句子；数值筛选只匹配成功评分的实际得分，等于按未四舍五入的分数精确比较。BLEU 满分附近小于 1e-9 的浮点误差统一按 100 展示和比较，新评分也按此规范落库；历史记录不自动改写。API `GET /api/evaluator-jobs/{id}/items` 支持 `language`、`item_status`（含 `unscored`），以及成对提供的 `score_operator=eq|lt|lte|gt|gte` 和 `score_value`；筛选后再统计总数、排序及分页。
- LLM 默认 `cache_policy=content_model` 按“源语种 + 源文 + GT + 候选译文 + 评分模型名”复用最近的合法历史评分，保留跨配置复用行为。设置为 `strict_revision` 时，还必须匹配评价器修订、Prompt 和服务地址。每条缓存分仍显示实际来源；验证新配置可以选择严格缓存或“强制重新评分”。

## 配置和运维

LLM 评价器可配置 1–64 并发，实际并发取该值与 Worker 全局上限的较小值，默认全局上限为 32。

Worker 按最多 512 条读取待评项，逐响应组落库并增量维护进度；真实发出的请求显示为“评分中”。单个数据库只允许运行一个评分 Worker，重复启动会被系统锁拒绝，退出或崩溃后锁自动释放。数据库和心跳文件应放在同一台主机的本地文件系统。

编辑评价器配置时保留未修改参数；API 创建修订时，`config` 可仅包含需修改的字段，但仍须提供 `default_threshold`。编辑时 API Key 留空沿用；配置或默认阈值变化会创建新修订，已有任务继续引用原修订。

Prompt 版本标签留空时按北京时间生成 `YYYYMMDD.N`，同一配置当天的序号递增，也可填写自定义标签。System Prompt 和 User Prompt 均可留空，对应角色不会出现在发送给评分服务的 `messages` 中。已有评测结果、评分缓存或任务引用的版本及其配置不能删除。

可用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TRANSLATION_EVAL_DATABASE_URL` | 项目根目录下的 `var/translation_eval.db` | SQLAlchemy 数据库地址，默认使用 SQLite |
| `TRANSLATION_EVAL_IMPORT_DIR` | 项目根目录下的 `var/imports` | 导入暂存目录 |
| `TRANSLATION_EVAL_WORKER_CONCURRENCY` | `32` | Worker 全局 LLM 并发上限 |
| `TRANSLATION_EVAL_WORKER_POLL_SECONDS` | `0.5` | 空队列检查间隔 |
| `TRANSLATION_EVAL_WORKER_HEARTBEAT_FILE` | 项目根目录下的 `var/worker-heartbeat` | Worker 健康心跳文件路径 |
| `TRANSLATION_EVAL_PORT` | `8000` | `start.sh` 服务端口 |

API Key 明文保存在数据库；页面只回显掩码，数据库备份仍包含密钥。

运行中的 SQLite 数据库应使用 [在线备份](https://sqlite.org/backup.html)，不能直接逐个复制数据库和 `-wal`/`-shm` 文件。以下命令使用 Python 内置 SQLite 备份默认数据库；自定义数据库地址时需替换源文件路径，并为备份选择新的目标文件名：

```bash
.venv/bin/python - <<'PY'
import sqlite3
from contextlib import closing

with closing(sqlite3.connect("file:var/translation_eval.db?mode=ro", uri=True)) as source:
    with closing(sqlite3.connect("var/translation_eval-backup.db")) as target:
        source.backup(target)
PY
```

也可正常关闭 API、Worker 和其他数据库连接，确认 `-wal` 已消失后，仅复制主数据库文件；异常退出后不要删除或遗漏 WAL。

更新已有安装时，先备份数据库并停止 API、Worker，再执行迁移、重新构建前端并重启服务。`start.sh` 会执行迁移和构建，`dev.sh` 也会在启动前执行迁移；需要单独迁移时运行：

```bash
.venv/bin/alembic upgrade head
```

当前迁移包括 `0006` 的派生摘要/索引和 `0007` 的后台导入任务。已有终态任务会在首次查询时补建摘要，大任务第一次打开可能仍需等待；可在升级后主动预计算，原始评分不变：

```bash
PYTHONPATH=backend .venv/bin/python -m app.read_models
```

新任务完成时自动生成摘要；重试、评分或样本变化会使派生摘要失效。动态阈值仍精确统计，单任务最多保留 8 个最近阈值的派生结果。SQLite 每连接使用有界的 32 MiB 页面缓存，并在评分结束后维护查询统计。导入保持原子事务，SQLite 的同时写入仍会串行等待，多数据集的大文件提交耗时取决于总样本量。

历史结果不会随代码或配置更新自动改写；修正异常评分可从原提交开启“强制重新评分”再次评测。当前规模修复与验证见 [多数据集与规模优化记录](docs/scale-optimization-2026-09-07.md)，较早的变更见 [早期优化记录](docs/optimization-2026-09-07.md)。

测试和构建：

```bash
.venv/bin/pytest
npm --prefix frontend run build
```
