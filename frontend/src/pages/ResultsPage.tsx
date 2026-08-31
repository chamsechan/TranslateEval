import { ArrowLeftOutlined, CheckCircleOutlined, LineChartOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Col, Empty, InputNumber, Modal, Progress, Row, Select, Skeleton, Slider, Space, Statistic, Table, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, formatDate, formatScore } from '../api'
import PageHeader from '../components/PageHeader'
import ScoreChart from '../components/ScoreChart'
import StatusTag from '../components/StatusTag'
import type { EvaluationTask, EvaluatorJob, ThresholdSummary } from '../types'

interface ResultItem {
  id: number
  sample_id: string
  source_language: string
  source_text: string
  reference_zh: string
  translation_zh: string
  predicted_language: string | null
  status: string
  cache_hit: boolean
  attempts: number
  error: string | null
  score: number | null
  score_min: number | null
  score_max: number | null
  unit: string | null
  reason: string | null
  actual_prompt_version_id: string | null
  actual_evaluator_model: string | null
  actual_base_url: string | null
}

interface ItemsResponse { total: number; page: number; page_size: number; items: ResultItem[] }
interface LanguageDetectionSummary { applicable: boolean; reason?: string; total?: number; covered?: number; coverage?: number; correct?: number; accuracy?: number | null; by_language?: Array<Record<string, unknown>>; labels?: string[]; confusion_matrix?: number[][] }
interface CompareResponse { threshold: number; strictly_comparable: boolean; warning: string | null; items: Array<ThresholdSummary & { run_name: string; model_family: string; dataset_key: string; version_label: string; evaluator_name: string }> }
interface ResultContext {
  task: { id: string; run_name: string; model_family: string; created_at: string }
  dataset: { id: string; dataset_key: string; version_label: string }
  evaluator: EvaluatorJob
}

const errorText = (error: unknown) => error instanceof Error ? error.message : '请求失败'
const isAbort = (error: unknown) => error instanceof DOMException && error.name === 'AbortError'

export default function ResultsPage() {
  const { message } = App.useApp()
  const { jobId } = useParams()
  const navigate = useNavigate()
  const [tasks, setTasks] = useState<EvaluationTask[]>([])
  const [listError, setListError] = useState<string | null>(null)
  const [context, setContext] = useState<ResultContext | null>(null)
  const [contextError, setContextError] = useState<string | null>(null)
  const [summary, setSummary] = useState<ThresholdSummary | null>(null)
  const [summaryError, setSummaryError] = useState<string | null>(null)
  const [items, setItems] = useState<ItemsResponse | null>(null)
  const [itemsError, setItemsError] = useState<string | null>(null)
  const [threshold, setThreshold] = useState<number | null>(null)
  const [draftThreshold, setDraftThreshold] = useState<number | null>(null)
  const [page, setPage] = useState(1)
  const [language, setLanguage] = useState<string | undefined>()
  const [status, setStatus] = useState<string | undefined>()
  const [languageDetection, setLanguageDetection] = useState<LanguageDetectionSummary | null>(null)
  const [selectedJobs, setSelectedJobs] = useState<React.Key[]>([])
  const [comparison, setComparison] = useState<CompareResponse | null>(null)
  const [compareOpen, setCompareOpen] = useState(false)
  const [compareThreshold, setCompareThreshold] = useState(8)

  const loadTasks = async () => {
    try {
      setListError(null)
      setTasks(await api<EvaluationTask[]>('/tasks?limit=200'))
    } catch (error) {
      setListError(errorText(error))
    }
  }

  useEffect(() => {
    if (!jobId) void loadTasks()
  }, [jobId])

  useEffect(() => {
    if (!jobId) return
    const controller = new AbortController()
    setContext(null)
    setContextError(null)
    setSummary(null)
    setItems(null)
    api<ResultContext>(`/evaluator-jobs/${jobId}`, { signal: controller.signal })
      .then((value) => {
        setContext(value)
        setThreshold(value.evaluator.default_threshold)
        setDraftThreshold(value.evaluator.default_threshold)
      })
      .catch((error) => { if (!isAbort(error)) setContextError(errorText(error)) })
    return () => controller.abort()
  }, [jobId])

  useEffect(() => {
    if (!context) return
    const controller = new AbortController()
    setLanguageDetection(null)
    api<LanguageDetectionSummary>(`/dataset-jobs/${context.dataset.id}/language-detection-summary`, { signal: controller.signal })
      .then(setLanguageDetection)
      .catch(() => undefined)
    return () => controller.abort()
  }, [context])

  useEffect(() => {
    if (!jobId || threshold == null) return
    const controller = new AbortController()
    setSummary(null)
    setSummaryError(null)
    api<ThresholdSummary>(`/evaluator-jobs/${jobId}/summary?threshold=${threshold}`, { signal: controller.signal })
      .then(setSummary)
      .catch((error) => { if (!isAbort(error)) setSummaryError(errorText(error)) })
    return () => controller.abort()
  }, [jobId, threshold])

  useEffect(() => {
    if (!jobId) return
    const controller = new AbortController()
    const params = new URLSearchParams({ page: String(page), page_size: '50' })
    if (language) params.set('language', language)
    if (status) params.set('item_status', status)
    setItems(null)
    setItemsError(null)
    api<ItemsResponse>(`/evaluator-jobs/${jobId}/items?${params}`, { signal: controller.signal })
      .then(setItems)
      .catch((error) => { if (!isAbort(error)) setItemsError(errorText(error)) })
    return () => controller.abort()
  }, [jobId, page, language, status])

  if (!jobId) {
    const rows = tasks.flatMap((task) => task.dataset_jobs.flatMap((dataset) => dataset.evaluator_jobs.map((evaluator) => ({ task, dataset, evaluator }))))
    const compare = async () => {
      try {
        const value = await api<CompareResponse>('/results/compare', { method: 'POST', body: JSON.stringify({ evaluator_job_ids: selectedJobs, threshold: compareThreshold }) })
        setComparison(value)
        setCompareOpen(true)
      } catch (error) {
        message.error(errorText(error))
      }
    }
    return <>
      <PageHeader title="评测结果" subtitle="选择任一数据集与评价器结果，动态调整阈值并查看逐句原始分数。" actions={<Space><InputNumber min={0} step={0.1} value={compareThreshold} onChange={(value) => setCompareThreshold(value ?? 0)} addonBefore="对比阈值" /><Button type="primary" disabled={selectedJobs.length < 2} onClick={compare}>对比所选 ({selectedJobs.length})</Button></Space>} />
      {listError && <Alert type="error" showIcon message="结果列表加载失败" description={listError} action={<Button icon={<ReloadOutlined />} onClick={loadTasks}>重试</Button>} style={{ marginBottom: 16 }} />}
      <Card className="panel-card"><Table rowKey={(row) => row.evaluator.id} rowSelection={{ selectedRowKeys: selectedJobs, onChange: setSelectedJobs, getCheckboxProps: (row) => ({ disabled: !row.evaluator.completed_items }) }} dataSource={rows} locale={{ emptyText: <Empty description={listError ? '暂时无法获取结果' : '暂无评测结果'} /> }} columns={[
        { title: '模型运行', render: (_, row) => <div><Typography.Text strong>{row.task.run_name}</Typography.Text><div><Typography.Text type="secondary">{row.task.model_family}</Typography.Text></div></div> },
        { title: '数据集版本', render: (_, row) => <div>{row.dataset.dataset_key}<div><Typography.Text type="secondary">{row.dataset.version_label}</Typography.Text></div></div> },
        { title: '评价方式', render: (_, row) => <Space><Tag color={row.evaluator.evaluator_type === 'sacrebleu_zh' ? 'purple' : 'blue'}>{row.evaluator.name}</Tag><span>r{row.evaluator.revision}</span></Space> },
        { title: '状态', render: (_, row) => <StatusTag status={row.evaluator.status} /> },
        { title: '覆盖', render: (_, row) => `${row.evaluator.completed_items}/${row.evaluator.total_items}` },
        { title: '提交时间', render: (_, row) => formatDate(row.task.created_at) },
        { title: '', render: (_, row) => <Button type="link" disabled={!row.evaluator.completed_items} onClick={() => navigate(`/results/${row.evaluator.id}`)}>分析结果</Button> },
      ]} /></Card>
      <Modal title={`模型结果对比 · 阈值 ≥ ${comparison?.threshold ?? compareThreshold}`} width={960} open={compareOpen} footer={null} onCancel={() => setCompareOpen(false)}>
        {comparison && !comparison.strictly_comparable && <Alert type="warning" showIcon message="所选结果不是严格同口径" description={comparison.warning} style={{ marginBottom: 15 }} />}
        <Table rowKey="evaluator_job_id" pagination={false} dataSource={comparison?.items || []} columns={[
          { title: '模型运行', dataIndex: 'run_name', render: (value: string, row) => <div><Typography.Text strong>{value}</Typography.Text><div><Typography.Text type="secondary">{row.model_family}</Typography.Text></div></div> },
          { title: '数据集', render: (_, row) => `${row.dataset_key} · ${row.version_label}` },
          { title: '评价器', dataIndex: 'evaluator_name' },
          { title: '微平均', dataIndex: 'micro_mean', render: (value: number | null) => formatScore(value) },
          { title: '宏平均', dataIndex: 'macro_mean', render: (value: number | null) => formatScore(value) },
          { title: '微准确率', dataIndex: 'micro_accuracy', render: (value: number | null) => value == null ? '—' : `${(value * 100).toFixed(2)}%` },
          { title: '宏准确率', dataIndex: 'macro_accuracy', render: (value: number | null) => value == null ? '—' : `${(value * 100).toFixed(2)}%` },
          { title: '覆盖率', dataIndex: 'coverage', render: (value: number) => `${(value * 100).toFixed(2)}%` },
        ]} />
      </Modal>
    </>
  }

  const back = <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/results')}>返回结果列表</Button>
  if (contextError) return <><PageHeader title="结果分析" subtitle="无法读取评测上下文" actions={back} /><Alert type="error" showIcon message="结果加载失败" description={contextError} /></>
  if (!context || threshold == null || draftThreshold == null) return <><PageHeader title="结果分析" subtitle="正在加载评测上下文…" actions={back} /><Card className="panel-card"><Skeleton active /></Card></>

  const max = summary?.score_max ?? (context.evaluator.evaluator_type === 'sacrebleu_zh' ? 100 : 10)
  const retry = async () => {
    try {
      await api(`/evaluator-jobs/${jobId}/retry-failed`, { method: 'POST' })
      setContext({ ...context, evaluator: { ...context.evaluator, status: 'queued', error: null } })
      message.success('失败项已重新进入队列')
    } catch (error) {
      message.error(errorText(error))
    }
  }

  return <>
    <PageHeader title={`${context.task.run_name} · ${context.dataset.dataset_key}`} subtitle={`${context.evaluator.name} · 数据版本 ${context.dataset.version_label}`} actions={back} />
    {context.evaluator.error && <Alert type="error" showIcon message="评价器任务失败" description={context.evaluator.error} action={context.evaluator.failed_items > 0 ? <Button danger onClick={retry}>重试失败项</Button> : undefined} style={{ marginBottom: 16 }} />}
    {context.evaluator.status !== 'completed' && !context.evaluator.error && <Alert type="warning" showIcon message="该任务尚未完全结束" description="以下统计仅基于当前成功评分的样本，并始终显示覆盖率。" style={{ marginBottom: 16 }} />}
    {summaryError && <Alert type="error" showIcon message="统计数据加载失败" description={summaryError} style={{ marginBottom: 16 }} />}
    <Card className="result-hero" style={{ marginBottom: 18 }}>
      {summary ? <Row align="middle" gutter={28}>
        <Col flex="150px"><div className="score-ring" style={{ '--percent': `${(summary.micro_accuracy ?? 0) * 100}%` } as React.CSSProperties}><div><strong>{summary.micro_accuracy == null ? '—' : `${(summary.micro_accuracy * 100).toFixed(1)}%`}</strong><span>阈值准确率</span></div></div></Col>
        <Col flex="auto"><Row gutter={26}>
          <Col span={6}><Statistic title="微平均分" value={summary.micro_mean ?? undefined} precision={3} suffix={`/ ${max}`} /></Col>
          <Col span={6}><Statistic title="宏平均分" value={summary.macro_mean ?? undefined} precision={3} /></Col>
          <Col span={6}><Statistic title="宏准确率" value={summary.macro_accuracy == null ? undefined : summary.macro_accuracy * 100} precision={1} suffix="%" /></Col>
          <Col span={6}><Statistic title="评分覆盖率" value={summary.coverage * 100} precision={1} suffix="%" /></Col>
        </Row></Col>
      </Row> : <Skeleton active paragraph={{ rows: 2 }} />}
    </Card>
    {languageDetection && <Card className="panel-card" size="small" style={{ marginBottom: 18 }}>
      {languageDetection.applicable ? <Row gutter={24} align="middle"><Col><Tag color="purple">自动语种识别</Tag></Col><Col><Statistic title="识别准确率" value={(languageDetection.accuracy ?? 0) * 100} precision={2} suffix="%" /></Col><Col><Statistic title="识别覆盖率" value={(languageDetection.coverage ?? 0) * 100} precision={2} suffix="%" /></Col><Col><Typography.Text type="secondary">正确 {languageDetection.correct}/{languageDetection.covered}，混淆矩阵数据已保留。</Typography.Text></Col></Row> : <Space><Tag>语种识别 N/A</Tag><Typography.Text type="secondary">{languageDetection.reason}</Typography.Text></Space>}
    </Card>}
    <Row gutter={18}>
      <Col span={17}><Card className="panel-card" title={<Space><LineChartOutlined />逐语种得分与准确率</Space>}>{summary ? <ScoreChart data={summary.by_language} /> : <Skeleton active />}</Card></Col>
      <Col span={7}><Card className="panel-card" title="动态准确阈值" style={{ height: '100%' }}>
        <Typography.Paragraph type="secondary">原始逐句分数不变。准确样本按 <Typography.Text code>score ≥ threshold</Typography.Text> 即时统计。</Typography.Paragraph>
        <Space align="center" style={{ width: '100%', margin: '14px 0' }}><Slider min={0} max={max} step={0.1} value={draftThreshold} onChange={setDraftThreshold} onChangeComplete={setThreshold} style={{ flex: 1, minWidth: 150 }} /><InputNumber min={0} max={max} step={0.1} value={draftThreshold} onChange={(value) => { const next = value ?? 0; setDraftThreshold(next); setThreshold(next) }} /></Space>
        <Card size="small" style={{ background: '#f7faff' }}><Statistic title={`分数 ≥ ${threshold}`} value={summary?.micro_accuracy == null ? undefined : summary.micro_accuracy * 100} precision={2} suffix="%" prefix={<CheckCircleOutlined />} /></Card>
        <div style={{ marginTop: 15 }}><Progress percent={(summary?.coverage ?? 0) * 100} size="small" /><Typography.Text type="secondary">成功 {summary?.successful ?? '—'} · 失败 {summary?.failed ?? '—'} · 取消 {summary?.cancelled ?? '—'}</Typography.Text></div>
      </Card></Col>
    </Row>
    {!!summary?.aggregates.length && <Card className="panel-card" title="标准语料级指标" style={{ marginTop: 18 }}><Space wrap>{summary.aggregates.filter((item) => item.metric_name === 'corpus_bleu').map((item, index) => <Tag key={index} color="purple">{String(item.source_language || '全部')} corpus BLEU: {formatScore(Number(item.value))}</Tag>)}</Space></Card>}
    <Card className="panel-card" title="逐句原始结果" style={{ marginTop: 18 }} extra={<Space><Select allowClear placeholder="语种" style={{ width: 110 }} options={summary?.by_language.map((item) => ({ label: item.source_language, value: item.source_language }))} onChange={(value) => { setLanguage(value); setPage(1) }} /><Select allowClear placeholder="状态" style={{ width: 120 }} options={[{ label: '成功', value: 'completed' }, { label: '失败', value: 'failed' }, { label: '已取消', value: 'cancelled' }]} onChange={(value) => { setStatus(value); setPage(1) }} /></Space>}>
      {itemsError && <Alert type="error" showIcon message="逐句结果加载失败" description={itemsError} style={{ marginBottom: 12 }} />}
      <Table loading={!items && !itemsError} rowKey="id" dataSource={items?.items || []} pagination={{ current: page, pageSize: 50, total: items?.total || 0, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <Row gutter={14}><Col span={8}><Typography.Text type="secondary">源文</Typography.Text><div className="code-template">{row.source_text}</div></Col><Col span={8}><Typography.Text type="secondary">中文 GT</Typography.Text><div className="code-template">{row.reference_zh}</div></Col><Col span={8}><Typography.Text type="secondary">候选译文</Typography.Text><div className="code-template">{row.translation_zh}</div></Col>{row.reason && <Col span={24} style={{ marginTop: 12 }}><Typography.Text type="secondary">评分理由：</Typography.Text> {row.reason}</Col>}<Col span={24} style={{ marginTop: 8 }}><Space size="large"><span>实际模型 {row.actual_evaluator_model || '—'}</span><span>Prompt {row.actual_prompt_version_id || '—'}</span><span>Base URL {row.actual_base_url || '—'}</span></Space></Col></Row> }} columns={[
        { title: '样本 ID', dataIndex: 'sample_id', width: 160 },
        { title: '语种', dataIndex: 'source_language', width: 80, render: (value: string) => <Tag>{value}</Tag> },
        { title: '候选译文', dataIndex: 'translation_zh', render: (value: string) => <div className="source-cell">{value}</div> },
        { title: '分数', dataIndex: 'score', width: 100, sorter: (a, b) => (a.score ?? 0) - (b.score ?? 0), render: (value: number | null, row) => <span className="score-cell">{formatScore(value)} {row.unit}</span> },
        { title: '判定', width: 90, render: (_, row) => row.score == null ? '—' : row.score >= threshold ? <Tag color="success">准确</Tag> : <Tag color="error">未达标</Tag> },
        { title: '来源', width: 95, render: (_, row) => row.cache_hit ? <Tag color="gold">缓存</Tag> : <Tag color="blue">新评分</Tag> },
        { title: '说明', dataIndex: 'reason', ellipsis: true, render: (value: string | null, row) => <span className="reason-text">{value || row.error || '—'}</span> },
      ]} />
    </Card>
  </>
}
