# 前端复核证据

2026-09-07 对当前构建运行 Chromium 实测。所有写操作使用独立临时 SQLite 和 imports；评分请求只发给本地假服务。未使用真实 API Key，也未修改产品代码。

- `evidence-1.json`：数据集核验后修改需重新核验；最终版本标签按修改值提交。
- `evidence-2.json`：结果核验后修改需重新核验，生成不同 report ID；运行名按修改值提交。
- `evidence-results.json`：阈值、语种、分数条件、完整 ID 经刷新保留；预览实际数据版本；再次评测创建新任务。
- `evidence-settings.json`：通过 UI 新建评分器、编辑为 r2、API Key 留空沿用、创建 Prompt、选择 BLEU + LLM 和强制评分。
- `evidence-worker.json`：BLEU 与本地假 LLM 均完成 6 条，强制评分缓存命中为 0，假 LLM 实收 6 次请求。
- `evidence-bad-manifest*.json` / `bad-manifest.png`：目录名正确、JSON 数据集 key 拼错时，UI 留下不可修改/删除的错误项；仅通过 API 修正元信息即可核验成功。
- `evidence-cancel-error.json` / `cancel-error.png`：拦截父任务取消接口返回 HTTP 500；页面无错误反馈，对话框保持，浏览器产生未捕获错误。此为故障注入，不表示正常 API 会主动返回 500。
- `evidence-comparison.json` / `comparison.png`：同配置 BLEU 对比和从对比进入详情的阈值传递。

重跑（先完成项目安装及前端构建）：

```bash
.venv/bin/python docs/system-review-2026-09-07/frontend/reproduce.py
```

依赖项目 `.venv`、Node.js、Playwright 及 Chromium。脚本的 Playwright/Chromium 默认路径对应本次运行环境，可用 `REVIEW_PLAYWRIGHT_MODULE` / `REVIEW_CHROMIUM_EXE` 指定其他绝对路径。`templates/` 由 runner 替换路径和端口后执行，不直接运行。`REVIEW_OUTPUT_DIR=/tmp/frontend-review-output` 可保留重跑 JSON 和截图。服务使用本地随机空闲端口，退出自动停止服务并清理临时数据库和目录。

真实远程服务的模型质量、兼容性、账单及网络条件不在这组 UI 测试结论内。
