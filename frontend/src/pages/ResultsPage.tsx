import { ArrowLeftOutlined, CheckCircleOutlined, LineChartOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Col, InputNumber, Progress, Row, Select, Skeleton, Slider, Space, Statistic, Table, Tag, Typography } from 'antd'
import { useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, formatScore } from '../api'
import PageHeader from '../components/PageHeader'
import QueryError from '../components/QueryError'
import ScoreChart from '../components/ScoreChart'
import StatusTag from '../components/StatusTag'
import { useApiQuery } from '../hooks/useApiQuery'
import { useTaskChanges } from '../hooks/useTaskChanges'
import ResultsListPage from './ResultsListPage'
import type { EvaluatorJob, Status, ThresholdSummary } from '../types'

interface ResultItem {
  id: number | string
  sample_id: string
  source_language: string
  source_text: string
  reference_zh: string
  translation_zh: string
  predicted_language: string | null
  status: Status | 'unscored'
  scored: boolean
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
interface ResultContext {
  task: { id: string; submission_id: string; run_name: string; model_family: string; created_at: string }
  dataset: { id: string; dataset_key: string; version_label: string }
  evaluator: EvaluatorJob
}

const active = new Set(['queued', 'preprocessing', 'running', 'cancelling'])
const scoreOperators = [
  { label: '等于 =', value: 'eq' },
  { label: '小于 <', value: 'lt' },
  { label: '小于等于 ≤', value: 'lte' },
  { label: '大于 >', value: 'gt' },
  { label: '大于等于 ≥', value: 'gte' },
]

export default function ResultsPage() {
  const { jobId } = useParams()
  return jobId ? <ResultDetail key={jobId} jobId={jobId} /> : <ResultsListPage />
}

function ResultDetail({ jobId }: { jobId: string }) {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [chosenThreshold, setThreshold] = useState<number | null>(null)
  const [draft, setDraftThreshold] = useState<number | null>(null)
  const [page, setPage] = useState(1)
  const [language, setLanguage] = useState<string | undefined>()
  const [status, setStatus] = useState<string | undefined>()
  const [scoreOperator, setScoreOperator] = useState('eq')
  const [scoreValue, setScoreValue] = useState<number | null>(null)
  const [sortOrder, setSortOrder] = useState<'ascend' | 'descend' | null>(null)
  const [retrying, setRetrying] = useState(false)
  const itemsRef = useRef<HTMLDivElement>(null)
  const contextQuery = useApiQuery<ResultContext>(`/evaluator-jobs/${jobId}`)
  const { data: context, error: contextError } = contextQuery
  const threshold = chosenThreshold ?? context?.evaluator.default_threshold ?? null
  const draftThreshold = draft ?? threshold
  const summaryQuery = useApiQuery<ThresholdSummary>(threshold == null ? null : `/evaluator-jobs/${jobId}/summary?threshold=${threshold}`)
  const { data: summary, error: summaryError } = summaryQuery
  const params = new URLSearchParams({ page: String(page), page_size: '50', sort: sortOrder ? 'score' : 'id', direction: sortOrder === 'descend' ? 'desc' : 'asc' })
  if (language) params.set('language', language)
  if (status) params.set('item_status', status)
  if (scoreValue != null) {
    params.set('score_operator', scoreOperator)
    params.set('score_value', String(scoreValue))
  }
  const itemsQuery = useApiQuery<ItemsResponse>(`/evaluator-jobs/${jobId}/items?${params}`)
  const { data: items, error: itemsError } = itemsQuery
  const languageQuery = useApiQuery<LanguageDetectionSummary>(context ? `/dataset-jobs/${context.dataset.id}/language-detection-summary` : null)
  const languageDetection = languageQuery.data
  const refresh = () => { contextQuery.refresh(); summaryQuery.refresh(); itemsQuery.refresh() }
  useTaskChanges(refresh, !context || active.has(context.evaluator.status))
  const back = <Space wrap>{context && <Button type="primary" onClick={() => navigate(`/submit?submission=${context.task.submission_id}`)}>再次评测</Button>}<Button onClick={refresh}>刷新结果</Button><Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/results')}>返回结果列表</Button></Space>
  if (contextError && !context) return <><PageHeader title="结果分析" subtitle="无法读取评测上下文" actions={back} /><QueryError error={contextError} retry={contextQuery.refresh} /></>
  if (!context || threshold == null || draftThreshold == null) return <><PageHeader title="结果分析" subtitle="正在加载评测上下文…" actions={back} /><Card className="panel-card"><Skeleton active /></Card></>
  const max = summary?.score_max || (context.evaluator.evaluator_type === 'sacrebleu_zh' ? 100 : 10)
  const retry = async () => {
    setRetrying(true)
    try {
      await api(`/evaluator-jobs/${jobId}/retry-failed`, { method: 'POST' })
      refresh()
      message.success('失败项已重新进入队列')
    } catch (error) { message.error(error instanceof Error ? error.message : '重试失败') }
    finally { setRetrying(false) }
  }
  const inspectLanguage = (value: string, unscored = false) => {
    setLanguage(value)
    setStatus(unscored ? 'unscored' : undefined)
    setScoreValue(null)
    setScoreOperator('eq')
    setSortOrder(null)
    setPage(1)
    itemsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    itemsRef.current?.focus({ preventScroll: true })
  }
  const clearFilters = () => {
    setLanguage(undefined)
    setStatus(undefined)
    setScoreValue(null)
    setScoreOperator('eq')
    setSortOrder(null)
    setPage(1)
  }
  const onlyUnscored = !!status && status !== 'completed'

  return <>
    <PageHeader title={`${context.task.run_name} · ${context.dataset.dataset_key}`} subtitle={`${context.evaluator.name} · 数据版本 ${context.dataset.version_label}`} actions={back} />
    {context.evaluator.error && <Alert type="error" showIcon message="评价器任务失败" description={context.evaluator.error} action={context.evaluator.failed_items > 0 ? <Button danger loading={retrying} disabled={active.has(context.evaluator.status)} onClick={retry}>重试失败项</Button> : undefined} style={{ marginBottom: 16 }} />}
    {context.evaluator.status !== 'completed' && !context.evaluator.error && <Alert type="warning" showIcon message={active.has(context.evaluator.status) ? '该任务尚未完全结束' : '任务已结束，存在失败或取消项'} action={context.evaluator.failed_items > 0 ? <Button loading={retrying} disabled={active.has(context.evaluator.status)} onClick={retry}>重试失败项</Button> : undefined} description="准确率按全部样本计算，未评分计为未通过；评分完成后会更新。平均分仅基于已评分样本。" style={{ marginBottom: 16 }} />}
    <QueryError error={contextError} retry={contextQuery.refresh} /><QueryError error={summaryError} retry={summaryQuery.refresh} /><QueryError error={languageQuery.error} retry={languageQuery.refresh} />
    <Card className="result-hero" style={{ marginBottom: 18 }}>
      {summary ? <Row align="middle" gutter={28}>
        <Col flex="150px"><div className="score-ring" style={{ '--percent': `${(summary.micro_accuracy ?? 0) * 100}%` } as React.CSSProperties}><div><strong>{summary.micro_accuracy == null ? '—' : `${(summary.micro_accuracy * 100).toFixed(1)}%`}</strong><span>加权平均准确率</span></div></div></Col>
        <Col flex="auto"><Row gutter={26}>
          <Col span={6}><Statistic title="微平均分" value={summary.micro_mean ?? '—'} precision={3} suffix={`/ ${max}`} /></Col>
          <Col span={6}><Statistic title="宏平均分" value={summary.macro_mean ?? '—'} precision={3} /></Col>
          <Col span={6}><Statistic title="宏平均准确率" value={summary.macro_accuracy == null ? '—' : summary.macro_accuracy * 100} precision={1} suffix="%" /></Col>
          <Col span={6}><Statistic title="评分覆盖率" value={summary.coverage * 100} precision={1} suffix="%" /></Col>
        </Row></Col>
      </Row> : <Skeleton active paragraph={{ rows: 2 }} />}
      <div className="result-summary-caption">正确 {summary?.passed ?? '—'} / 总数 {summary?.total ?? '—'} · 未评分 {summary?.unscored ?? '—'}（计为未通过）。加权平均准确率 = 总正确数 / 总样本数；宏平均准确率按语种等权。</div>
    </Card>
    {summary && <Card className="panel-card" title="各语种评测结果" style={{ marginBottom: 18 }}>
      <Typography.Paragraph type="secondary">得分 ≥ {threshold} 为正确，准确率 = 正确数 / 该语种总数。点击语种查看全部句子，点击未评分数量排查异常。</Typography.Paragraph>
      <Table rowKey="source_language" size="small" pagination={false} scroll={{ x: 900 }} dataSource={summary.by_language} columns={[
        { title: '语种', dataIndex: 'source_language', render: (value: string) => <Button type="link" className="result-table-link" onClick={() => inspectLanguage(value)}>{value}</Button> },
        { title: '正确 / 总数', render: (_, row) => `${row.passed} / ${row.total}` },
        { title: '准确率', sorter: (a, b) => (a.accuracy ?? 0) - (b.accuracy ?? 0), render: (_, row) => row.accuracy == null ? '—' : `${(row.accuracy * 100).toFixed(1)}%` },
        { title: '已评分 / 总数', render: (_, row) => `${row.count} / ${row.total}` },
        { title: '评分覆盖率', render: (_, row) => `${(row.coverage * 100).toFixed(1)}%` },
        { title: '已评分平均分', dataIndex: 'mean', render: (value: number | null) => formatScore(value) },
        { title: '未评分（计失败）', render: (_, row) => row.unscored ? <Button danger type="link" className="result-table-link" aria-label={`查看 ${row.source_language} 未评分样本`} onClick={() => inspectLanguage(row.source_language, true)}>{row.unscored}</Button> : 0 },
        { title: '操作', render: (_, row) => <Button type="link" className="result-table-link" onClick={() => inspectLanguage(row.source_language)}>查看明细</Button> },
      ]} />
    </Card>}
    {languageDetection && <Card className="panel-card" size="small" style={{ marginBottom: 18 }}>
      {languageDetection.applicable ? <Row gutter={24} align="middle"><Col><Tag color="purple">自动语种识别</Tag></Col><Col><Statistic title="识别准确率" value={languageDetection.accuracy == null ? '—' : languageDetection.accuracy * 100} precision={2} suffix="%" /></Col><Col><Statistic title="识别覆盖率" value={(languageDetection.coverage ?? 0) * 100} precision={2} suffix="%" /></Col><Col><Typography.Text type="secondary">正确 {languageDetection.correct}/{languageDetection.covered}，混淆矩阵数据已保留。</Typography.Text></Col></Row> : <Space><Tag>语种识别 N/A</Tag><Typography.Text type="secondary">{languageDetection.reason}</Typography.Text></Space>}
    </Card>}
    <Row gutter={[18, 18]}>
      <Col xs={24} xl={17}><Card className="panel-card" title={<Space><LineChartOutlined />逐语种得分与准确率</Space>}>{summary ? <ScoreChart data={summary.by_language} /> : <Skeleton active />}</Card></Col>
      <Col xs={24} xl={7}><Card className="panel-card" title="动态准确阈值" style={{ height: '100%' }}>
        <Typography.Paragraph type="secondary">按 <Typography.Text code>score ≥ threshold</Typography.Text> 判断正确。未评分计为未通过，分母始终为总样本数。调整阈值即时更新准确率。</Typography.Paragraph>
        <div className="threshold-control"><Slider min={0} max={max} step={0.1} value={draftThreshold} onChange={setDraftThreshold} onChangeComplete={setThreshold} style={{ flex: 1, minWidth: 0 }} /><InputNumber aria-label="准确阈值" min={0} max={max} step={0.1} value={draftThreshold} onChange={(value) => { const next = value ?? 0; setDraftThreshold(next); setThreshold(next) }} /></div>
        <Card size="small" style={{ background: '#f7faff' }}><Statistic title={`分数 ≥ ${threshold}`} value={summary?.micro_accuracy == null ? '—' : summary.micro_accuracy * 100} precision={2} suffix="%" prefix={<CheckCircleOutlined />} /></Card>
        <div style={{ marginTop: 15 }}><Progress percent={(summary?.coverage ?? 0) * 100} size="small" /><Typography.Text type="secondary">已评分 {summary?.successful ?? '—'} · 未评分 {summary?.unscored ?? '—'}（含评分失败 {summary?.failed ?? '—'}、取消 {summary?.cancelled ?? '—'}）</Typography.Text></div>
      </Card></Col>
    </Row>
    {context.evaluator.evaluator_type === 'sacrebleu_zh' && <Card className="panel-card" title="标准语料级指标" style={{ marginTop: 18 }}><Typography.Paragraph type="secondary">按当前成功评分样本计算；失败和取消项不参与。任务结束后更新语料级指标。</Typography.Paragraph>{summary?.aggregates.some((item) => item.metric_name === 'corpus_bleu') ? <Space wrap>{summary.aggregates.filter((item) => item.metric_name === 'corpus_bleu').map((item, index) => <Tag key={index} color="purple">{String(item.source_language || '全部成功样本')} corpus BLEU: {formatScore(Number(item.value))} · {Number(item.sample_count)} 条</Tag>)}</Space> : <Typography.Text type="secondary">语料级指标暂未生成</Typography.Text>}</Card>}
    <div ref={itemsRef} tabIndex={-1} className="result-items-section" aria-label="逐句评分明细">
    <Card className="panel-card" title="逐句评分明细" style={{ marginTop: 18 }}>
      <Space wrap size={[16, 12]} className="result-filters">
        <label className="result-filter">语种<Select aria-label="语种筛选" value={language} allowClear showSearch placeholder="全部语种" style={{ width: 150 }} options={summary?.by_language.map((item) => ({ label: item.source_language, value: item.source_language }))} onChange={(value) => { setLanguage(value); setPage(1) }} /></label>
        <label className="result-filter">评分状态<Select aria-label="评分状态筛选" value={status} allowClear placeholder="全部状态" style={{ width: 195 }} options={[{ label: '已评分', value: 'completed' }, { label: '未评分（全部）', value: 'unscored' }, { label: '评分失败', value: 'failed' }, { label: '已取消', value: 'cancelled' }, { label: '等待评分', value: 'queued' }, { label: '评分中', value: 'running' }]} onChange={(value) => { setStatus(value); if (value && value !== 'completed') setScoreValue(null); setPage(1) }} /></label>
        <label className="result-filter">分数条件<Space.Compact><Select aria-label="分数比较方式" disabled={onlyUnscored} value={scoreOperator} style={{ width: 140 }} options={scoreOperators} onChange={(value) => { setScoreOperator(value); setPage(1) }} /><InputNumber aria-label="筛选分数" disabled={onlyUnscored} min={0} max={max} step={0.1} value={scoreValue} placeholder={`0–${max}`} style={{ width: 110 }} onChange={(value) => { setScoreValue(value); setPage(1) }} /></Space.Compact></label>
        <Button onClick={clearFilters}>清除筛选</Button>
      </Space>
      <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>当前匹配 {items?.total ?? '—'} 条。分数条件仅匹配已评分句子，等于按原始分数精确匹配；展开可查看原始分数及异常详情。</Typography.Paragraph>
      <QueryError error={itemsError} retry={itemsQuery.refresh} />
      <Table scroll={{ x: 1100 }} loading={itemsQuery.loading} onChange={(_, __, sorter, extra) => {
        if (extra.action === 'sort') { const next = Array.isArray(sorter) ? sorter[0] : sorter; setSortOrder(next.order || null); setPage(1) }
      }} rowKey="sample_id" dataSource={items?.items || []} locale={{ emptyText: language || status || scoreValue != null ? '没有符合当前筛选条件的句子，请调整或清除筛选。' : '暂无样本' }} pagination={{ current: page, pageSize: 50, total: items?.total || 0, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <Row gutter={14}><Col xs={24} md={8}><Typography.Text type="secondary">源文</Typography.Text><div className="code-template">{row.source_text}</div></Col><Col xs={24} md={8}><Typography.Text type="secondary">中文 GT</Typography.Text><div className="code-template">{row.reference_zh}</div></Col><Col xs={24} md={8}><Typography.Text type="secondary">候选译文</Typography.Text><div className="code-template">{row.translation_zh}</div></Col>{row.reason && <Col span={24} style={{ marginTop: 12 }}><Typography.Text type="secondary">评分理由：</Typography.Text> {row.reason}</Col>}{row.error && <Col span={24} style={{ marginTop: 12 }}><Alert type="error" showIcon message="评分异常" description={<div className="result-error-text">{row.error}</div>} /></Col>}<Col span={24} style={{ marginTop: 8 }}><Space size="large" wrap><span>原始分数 {row.score ?? '—'} {row.unit}</span><span>评分尝试 {row.attempts} 次</span><span>实际模型 {row.actual_evaluator_model || '—'}</span><span>Prompt {row.actual_prompt_version_id || '—'}</span><span>Base URL {row.actual_base_url || '—'}</span></Space></Col></Row> }} columns={[
        { title: '样本 ID', dataIndex: 'sample_id', width: 160 },
        { title: '语种', dataIndex: 'source_language', width: 80, render: (value: string) => <Tag>{value}</Tag> },
        { title: '候选译文', dataIndex: 'translation_zh', render: (value: string) => <div className="source-cell">{value}</div> },
        { title: '分数', dataIndex: 'score', width: 100, sorter: true, sortOrder, render: (value: number | null, row) => <span className="score-cell" title={value == null ? '未评分' : `原始分数：${value}`}>{formatScore(value)} {row.unit}</span> },
        { title: '评分状态', width: 110, render: (_, row) => row.status === 'unscored' ? <Tag>未评分</Tag> : <StatusTag status={row.status} /> },
        { title: '判定', width: 160, render: (_, row) => !row.scored ? <Tag color="error">未评分，计失败</Tag> : row.score != null && row.score >= threshold ? <Tag color="success">正确</Tag> : <Tag color="error">未达标</Tag> },
        { title: '来源', width: 95, render: (_, row) => !row.scored ? '—' : row.cache_hit ? <Tag color="gold">缓存</Tag> : <Tag color="blue">新评分</Tag> },
        { title: '说明', dataIndex: 'reason', ellipsis: true, render: (value: string | null, row) => <span className="reason-text">{value || row.error || '—'}</span> },
      ]} />
    </Card>
    </div>
  </>
}
