# TranslateEval 页面与导入流程优化验证

本次保持 TranslateEval / ARI-NLP / chen_qc 品牌，完成多数据集、多版本、多语种相关页面调整，并接入后台导入提交。

## 变更

- 数据集默认使用可搜索、排序、分页的紧凑目录；版本历史改为可搜索分页表。展开版本可查找语言、查看哈希和进入对应版本样本；导入核验仍展示差异，并说明源文/参考变更计数可能重叠。
- 样本与逐句结果支持 20/50/100 条每页和跳页。页大小、页码、语种和原有筛选写入 URL，刷新可恢复；表内纵向滚动限制长文本和 100 行的页面高度。
- 结果详情的 SSE 仅订阅当前评价器任务，自动刷新最短间隔 5 秒并合并期间事件；手动刷新立即执行，离开页面取消待处理刷新。其他页面保留全局订阅。
- 多数据集任务默认折叠，展开后每次显示 5 个数据集，支持查找及只看异常。整任务取消失败显示实际错误，保留弹窗并允许重试。
- 对比页新增语种×模型运行矩阵：指标选择、基线选择、差值、语种搜索、按最小基线差值排序和逐句下钻。缺失语种不会按零分补齐，不同分值范围不计算均分差；非严格可比时差值明确仅供探索。总体指标页保留。
- 阈值相关文案统一为“通过/通过率”。语种识别单独展示总体准确率、已识别样本准确率、覆盖率，以及总体/已识别样本召回率，均说明分母。
- 语种图可搜索选择语言、按通过率或均分排序，默认显示 20 项，通过滑块查看其余语种。
- 目录导入可删除多余 manifest 条目，重选真实预测目录对应的数据集并补齐条目；没有新增任意目录重映射功能。
- LLM 缓存设置提供兼容模式 `content_model` 与严格模式 `strict_revision`。历史缺字段配置仍按兼容模式展示；强制重新评分提示与之对应。
- 数据集与新预测提交使用后台 commit-job。URL 和 localStorage 保存任务 ID；刷新可恢复阶段状态，完成后刷新目录或进入评测队列。顶栏“导入进度”显示最近 20 个任务，支持恢复失败项并使用服务端原请求重试。已有 submission 的再次评测保留原 evaluations API。

## 验证与结果

`npm run build` 通过；`git diff --check -- frontend/src` 通过。

可重复浏览器脚本：[verify.mjs](./verify.mjs)。最终结果：[verification.json](./verification.json)，`passed: true`、`errors: []`。所有 API 均在 Playwright 内被模拟，未访问真实模型服务。本记录验证交互与请求契约，不证明后端大库耗时、磁盘吞吐或评分质量；真实 API/Worker 集成由主验证流程另行检查。

测试假设为 20 数据集、每集 20 版本、40 语言、每语言 10000 句；复杂队列假设 20 任务×12 数据集×2 评价器。这里的“40 万句”是模拟总数，接口实际只返回当前页。

1440×1000 下，与前次相同模拟形态相比：

| 页面 | 优化前高度 | 优化后高度 |
|---|---:|---:|
| 20 数据集目录 | 3440px | 1025px（10 行/页） |
| 20 版本历史模态 | 6917px | 661px（10 行/页） |
| 20 个复杂任务队列 | 33564px | 5244px（默认折叠） |

脚本还断言：

- 搜索 `v01` 后在旧版本内查找菲律宾语，下钻 URL 保持旧版本与 `tl`。
- 样本选择 100 条/页并跳至第 500 页，刷新仍请求 `page=500&page_size=100`。
- 逐句筛选 `tl`、100 条/页、第 50 页，刷新恢复语言、页码和大小。
- 只有一半样本带语种预测且全部猜对时，总体识别准确率显示 50%，已识别准确率显示 100%，覆盖率为 50%。
- 1024 宽度图表初始显示 20 个完整轴标签，并保留滑块。
- 取消接口第一次 503 时显示错误，第二次在同一弹窗重试成功，无未处理页面异常。
- 对比矩阵正确显示基线 88%、当前 50%、差值 −38 个百分点，以及第三模型“不含此语种”；排序和语言下钻保持阈值。
- 删除多余 manifest 项后核验请求只含实际 `corpus-01`。
- 新 submission 和 dataset 后台提交后立即刷新，仍可恢复；submission 完成后导航到原 task ID。
- 从全局历史恢复失败导入，使用空 body 重试已保存的服务端请求，完成后可查看版本并关闭抽屉。
- 编辑评价器为严格模式后提交 `cache_policy: strict_revision`。
- 持续每秒发送一次 SSE 事件，10.5 秒内 11 个事件只产生 3 次 summary 请求；验证订阅带 `evaluator_job_id`、手动刷新立即、离开页面取消尾随刷新，以及其他页面继续全局订阅。SSE 传输通过浏览器 EventSource 替身模拟，此断言验证前端调用与合并逻辑。

复跑方式：在 `frontend/` 执行 `npm run build`，将 `frontend/dist` 的稳定副本通过 `python3 -m http.server 4188 --bind 127.0.0.1 --directory <dist副本>` 提供，然后在仓库根目录运行 `node docs/optimization-2026-09-07/frontend/verify.mjs`。脚本使用本环境 Playwright 与 Chromium 的绝对路径，其他环境需替换开头模块路径和 `executablePath`。

## 调用边界

- 数据集目录与版本表仍从现有数组 API 获取元数据，再在浏览器分页；样本/结果明细继续向后端请求当前页。本次没有把浏览器分页描述为后端元数据分页。
- 后台提交使用 `POST /import-reports/{report_id}/commit-job`，状态使用 `GET /import-commit-jobs/{id}`，全局记录使用 `GET /import-commit-jobs?limit=20`。失败重试空 body 依赖服务端复用原 request 的契约。
- 40 语言比较使用已有 `/results/compare` 的 `by_language`，没有新增或模拟产品中的抽样、导出、分布式调度能力。

## 页面证据

[紧凑数据集](./datasets-20-compact.png) · [版本表](./versions-20-compact.png) · [旧版本语种查找](./old-version-language-search.png) · [样本跳页](./samples-size100-page500.png) · [逐句筛选](./result-language-size100.png)

[折叠任务](./tasks-collapsed.png) · [任务内检索](./task-dataset-search.png) · [取消错误反馈](./cancel-failure-feedback.png) · [语种与基线对比](./comparison-language-baseline.png) · [图表缩放](./chart-1024-zoom.png)

[修复导入条目](./import-extra-entry-removed.png) · [独立变更维度](./overlapping-version-diff.png) · [严格缓存](./strict-cache-policy.png) · [后台预测导入刷新恢复](./async-submission-refresh.png) · [后台数据集完成](./async-dataset-completed.png) · [历史导入重试](./async-history-retry.png)
