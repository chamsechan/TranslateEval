评分与统计专项审查，2026-09-07。审查对象为当前代码，未修改产品代码或连接真实 LLM。完整 HTTP/Worker 探针见 [metrics-probe.py](metrics-probe.py)，实际输出见 [metrics-evidence.json](metrics-evidence.json)。从仓库根目录运行 `.venv/bin/python docs/system-review-2026-09-07/metrics-probe.py` 可复现；脚本在导入应用前配置临时数据库与暂存目录，退出后清理。

**已复现的实现问题**

1. **P1：BLEU 配置与评分输出缺少边界校验，任务显示完成却没有完整有效分数。** `backend/app/evaluators/bleu.py:34` 直接保留任意 `smooth_value`；`:40–48` 把任意有限或非有限浮点结果包装为宣称范围为 0–100 的得分，未验证范围。公开 API 接受 `{"smooth_method":"floor","smooth_value":100}`（HTTP 200）。用仓库原始 6 句示例数据经真实 Worker 执行，持久化得到 160.6857、561.2222 BLEU；任务报告 `completed`、`completed_items=6`，但公开 summary 返回 `successful=4`、`unscored=2`、`coverage=66.7%`、`aggregates=[]`。这里 summary 的范围防御确实生效；缺陷是上游把无效分记为成功，使任务状态、覆盖率和聚合互相矛盾，而且没有可重试失败项。`smooth_value="bad"` 同样 HTTP 200，实际逐句执行出现永久错误。建议在配置层按平滑方式校验数值类型、有限性与合理范围，并在保存分数前统一执行结果尺度检查；非法结果应进入失败状态。

2. **P2：比较检查把“有 completed 记录”误当作“有有效评分”。** `backend/app/queries.py:97–115` 仅按状态、记录数检查完整性，未使用 summary 所用的 `scored_item_condition`。对上一项两个实际执行过的任务，summary 均只有 4/6 有效评分，但 `comparison_checks` 返回 `strictly_comparable=true`、`warning=null`。建议比较页与统计页共享有效分判定，检查实际有效评分覆盖，而非 completed 记录数。

3. **P2：LLM 参数允许保存不能执行的配置。** `backend/app/evaluators/llm.py:166–178` 未验证 timeout/temperature 的有限性，也未约束 max_tokens 为正数。公开 API 接受字符串 `"temperature":"nan"` 并保存 Python NaN；真实请求构造逻辑随后在 JSON 序列化阶段失败，错误为 `Out of range float values are not JSON compliant: nan`，实际发出请求为 0 次。独立直接调用也确认 `max_tokens=-10`、`timeout_seconds="inf"` 被接收。应在配置保存时拦截，不让整批任务到 Worker 后才失败。反向核验：concurrency、timeout_seconds、max_retries 为 null 均被正常拒绝为 HTTP 400，并带有明确 detail；并不存在该路径的 500。

4. **P2：语种识别 recall 漏掉未输出语言的样本，准确率的条件分母也未充分说明。** `backend/app/queue.py:767–801` 先筛选有 predicted_language 的样本，再以该集合构造混淆矩阵和 recall 分母。6 句中只让 1 句德语预测正确，其余 5 句不输出语种时，返回 accuracy=100%、coverage=16.7%；德语共有 2 句，recall 却为 100%，按全体真实德语样本计算应为 50%。`frontend/src/pages/ResultsPage.tsx:216` 显示“自动语种识别：准确率”，没有说明仅按已输出语种计算。建议保留并明确命名条件准确率，同时新增以全部样本为分母的识别准确率；recall 分母使用实际该语种总数，混淆矩阵增加未输出列。条件准确率本身是一种可选定义，并非除法实现错误。

**设计风险与尚未达到的评测有效性目标**

- **缓存命中不代表执行过当前实验配置。** `backend/app/queue.py:276–323` 的键明确不含 Prompt、Base URL、temperature 等；README 已明确声明该策略，已有回归测试验证缓存来源和比较告警，因此这是既定设计而非实现偏离。同一个评分模型名绑定不同端点或 Prompt 时，后执行的配置可直接复用先前配置的分数；改 Prompt 而忘记 force 会使实验对比失效。建议默认严格按评分语义配置计算缓存键，把宽松跨配置复用作为显式选项。当前逐句实际来源记录和比较告警是有效补救，但不能令被跳过的配置实际执行。

- **“阈值通过率”不能自动等同翻译正确率。** 默认阈值来自 `backend/app/seed.py:80` 的 20 BLEU，没有发现人工标注校准依据。探针使用当前默认配置：参考“我支持这个决定。”，含否定的候选“我不支持这个决定。”得到 75.0624，超过默认阈值；参考“请关闭窗户。”，同义候选“请把窗关上。”得到 11.4787，低于默认阈值。页面称其“正确数/准确率”容易让读者把字符串重合阈值当作语义正确判定。这是指标解释问题，不能据此认定 SacreBLEU 算错；应优先命名为阈值通过率，保留 corpus BLEU，并以人工集校准语义评分和阈值。

- **LLM 的实际测量有效性尚未验证。** 默认 Prompt (`backend/app/evaluators/llm.py:21–27`) 只有准确性、完整性、流畅性概括，没有各档评分锚点、错误严重程度示例或显式权重。当前代码与测试可证明调用协议和数值解析工作，不能证明指定 judge 对每种小语种都与人工判断一致。应建立按语种/领域分层的人工校准集，测量相关性、阈值精确率/召回率及多次评分稳定性；针对待评文本中的指令也需要专门鲁棒性案例。专业译者按 MQM 错误类型与严重程度标注是可参考的研究路径：[Freitag et al., 2021](https://aclanthology.org/2021.tacl-1.87/)。

- **比较目前缺少不确定性估计。** `comparison_checks` 检查配置和样本来源可比性，不计算置信区间或配对显著性；同一数据集上的 0.1 分差异不能仅凭此断言模型改进。可在严格有效样本相同的前提下加入配对 bootstrap；[SacreBLEU 官方实现](https://github.com/mjpost/sacrebleu) 已提供相关置信区间和配对检验能力。

**已核验合理并正常的方面**

- 默认中文 `zh` tokenizer、逐句 effective_order、分别计算逐句均值与 corpus BLEU、保存 signature 的路径合理；中文 tokenizer 和签名的用途与 [SacreBLEU 官方说明](https://github.com/mjpost/sacrebleu) 一致。逐句 BLEU 平均没有冒充 corpus BLEU。
- `backend/app/queries.py:232–290` 的微/宏平均和阈值统计与 README 一致：均分仅对有效成功评分求平均，阈值通过率以全体样本为分母，宏平均通过率包含全未评分语种；覆盖率单独报告。全未评分不伪造 0 分，BLEU 满分附近浮点噪声有专门修正。
- `backend/app/normalization.py:9–38` 使用 NFC 和换行统一、保留有意义的空格，内容哈希按 sample_id 排序，对输入行顺序不敏感。源文、参考、候选、语种共同参与缓存键，能避免仅凭样本 ID 复用错误评分。
- LLM score 的布尔值、数字字符串、非有限值和越界值拒绝逻辑完整；非法响应可重试、明确拒绝永久失败。配置快照和实际评分来源独立保存，改变阈值不会触发重评分。真实服务可达性及真实 judge 质量没有在本次无外部调用检查中验证。
