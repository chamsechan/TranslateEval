import { ArrowLeftOutlined, LineChartOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Col, Collapse, Input, InputNumber, Row, Select, Skeleton, Space, Statistic, Table, Tag, Typography } from 'antd'
import { useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, formatDate, formatScore } from '../api'
import PageHeader from '../components/PageHeader'
import QueryError from '../components/QueryError'
import ScoreChart from '../components/ScoreChart'
import LanguageDetectionStats, { type LanguageDetectionSummary } from '../components/LanguageDetectionStats'
import StatusTag from '../components/StatusTag'
import DatasetSamples from '../components/DatasetSamples'
import { languageLabel, languagePairLabel } from '../languages'
import { sortableColumns } from '../tableSorting'
import { useApiQuery } from '../hooks/useApiQuery'
import { useTaskChanges } from '../hooks/useTaskChanges'
import ResultsListPage from './ResultsListPage'
import type { DatasetVersion, EvaluatorJob, Status, ThresholdSummary } from '../types'

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
interface ResultContext {
  task: { id: string; submission_id: string; run_name: string; model_family: string; checkpoint_name: string; created_at: string }
  dataset: { id: string; dataset_id: string; dataset_version_id: string; dataset_name: string; dataset_key: string; version_label: string }
  evaluator: EvaluatorJob
}

const parseScore = (value: string | null, max: number): number | null => value != null && value.trim() !== '' && Number.isFinite(Number(value)) && Number(value) >= 0 && Number(value) <= max ? Number(value) : null
const itemSortKeys = new Set(['sample_id', 'source_language', 'translation_zh', 'score', 'status', 'verdict', 'cache_hit', 'reason'])
const languageSortKeys = new Set(['source_language', 'passed', 'accuracy', 'count', 'coverage', 'mean', 'unscored'])
const itemStatuses = new Set(['completed', 'unscored', 'failed', 'cancelled', 'queued', 'running'])
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
  const [searchParams, setSearchParams] = useSearchParams()
  const updateFilters = (changes: Record<string, string | number | null | undefined>) => {
    setSearchParams(previous => {
      const next = new URLSearchParams(previous)
      Object.entries(changes).forEach(([key, value]) => value == null || value === '' ? next.delete(key) : next.set(key, String(value)))
      return next
    }, { replace: true })
  }
  const [retrying, setRetrying] = useState(false)
  const [datasetPreview, setDatasetPreview] = useState<{ language?: string; sample?: string } | null>(null)
  const itemsRef = useRef<HTMLDivElement>(null)
  const contextQuery = useApiQuery<ResultContext>(`/evaluator-jobs/${jobId}`)
  const { data: context, error: contextError } = contextQuery
  const max = context?.evaluator.evaluator_type === 'sacrebleu_zh' ? 100 : 10
  const threshold = parseScore(searchParams.get('threshold'), max) ?? context?.evaluator.default_threshold ?? null
  const pageSize = [20, 50, 100].includes(Number(searchParams.get('page_size'))) ? Number(searchParams.get('page_size')) : 50
  const rawPage = Number(searchParams.get('page') || 1)
  const page = Number.isSafeInteger(rawPage) && rawPage > 0 ? rawPage : 1
  const language = searchParams.get('language') || undefined
  const status = itemStatuses.has(searchParams.get('status') || '') ? searchParams.get('status')! : undefined
  const scoreOperator = scoreOperators.some(item => item.value === searchParams.get('score_operator')) ? searchParams.get('score_operator')! : 'eq'
  const onlyUnscored = !!status && status !== 'completed'
  const scoreValue = onlyUnscored ? null : parseScore(searchParams.get('score_value'), max)
  const sampleId = searchParams.get('sample_id') || ''
  const sortKey = itemSortKeys.has(searchParams.get('sort') || '') ? searchParams.get('sort')! : undefined
  const sortOrder = sortKey ? searchParams.get('direction') === 'desc' ? 'descend' : 'ascend' : null
  const languageSortKey = languageSortKeys.has(searchParams.get('language_sort') || '') ? searchParams.get('language_sort')! : undefined
  const languageSortOrder = languageSortKey ? searchParams.get('language_direction') === 'desc' ? 'descend' : 'ascend' : null
  const languageSearch = searchParams.get('language_q') || ''
  const languagePageSize = [10, 20, 40].includes(Number(searchParams.get('language_page_size'))) ? Number(searchParams.get('language_page_size')) : 10
  const rawLanguagePage = Number(searchParams.get('language_page') || 1)
  const languagePage = Number.isSafeInteger(rawLanguagePage) && rawLanguagePage > 0 ? rawLanguagePage : 1
  const returnTo = searchParams.get('return_to') || '/results'
  const listPath = /^\/results(?:\?|$)/.test(returnTo) ? returnTo : '/results'
  const versionQuery = useApiQuery<DatasetVersion>(context && datasetPreview ? `/dataset-versions/${context.dataset.dataset_version_id}` : null)
  const summaryQuery = useApiQuery<ThresholdSummary>(threshold == null ? null : `/evaluator-jobs/${jobId}/summary?threshold=${threshold}`)
  const { data: summary, error: summaryError } = summaryQuery
  const visibleLanguages = summary?.by_language.filter(item => languagePairLabel(item.source_language).toLowerCase().includes(languageSearch.trim().toLowerCase())) || []
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize), sort: sortKey || 'id', direction: sortOrder === 'descend' ? 'desc' : 'asc' })
  if (sortKey === 'verdict' && threshold != null) params.set('threshold', String(threshold))
  if (language) params.set('language', language)
  if (sampleId) params.set('sample_id', sampleId)
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
  useTaskChanges(refresh, !context || active.has(context.evaluator.status), { evaluatorJobId: jobId, minIntervalMs: 5000 })
  const back = <Space wrap>{context && <Button type="primary" onClick={() => navigate(`/submit?submission=${context.task.submission_id}`)}>再次评测</Button>}<Button onClick={refresh}>刷新结果</Button><Button icon={<ArrowLeftOutlined />} onClick={() => navigate(listPath)}>返回结果列表</Button></Space>
  if (contextError && !context) return <><PageHeader title="结果分析" subtitle="无法读取评测上下文" actions={back} /><QueryError error={contextError} retry={contextQuery.refresh} /></>
  if (!context || threshold == null) return <><PageHeader title="结果分析" subtitle="正在加载评测上下文…" actions={back} /><Card className="panel-card"><Skeleton active /></Card></>
  const retry = async () => {
    setRetrying(true)
    try {
      await api(`/evaluator-jobs/${jobId}/retry-failed`, { method: 'POST' })
      refresh()
      message.success('失败项已重新进入队列')
    } catch (error) { message.error(error instanceof Error ? error.message : '重试失败') }
    finally { setRetrying(false) }
  }
  const scrollToItems = () => {
    itemsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    itemsRef.current?.focus({ preventScroll: true })
  }
  const inspectLanguage = (value: string, unscored = false) => {
    updateFilters({ language: value, status: unscored ? 'unscored' : null, score_value: null, score_operator: null, sample_id: null, sort: null, direction: null, page: null })
    scrollToItems()
  }
  const clearFilters = () => updateFilters({ language: null, status: null, score_value: null, score_operator: null, sample_id: null, sort: null, direction: null, page: null })

  return <>
    <PageHeader title={`${context.task.run_name} · ${context.dataset.dataset_key}`} subtitle={`${context.task.model_family} · ${context.task.checkpoint_name} · ${context.evaluator.name} r${context.evaluator.revision} · ${formatDate(context.task.created_at)}`} actions={back} />
    <Space wrap size={[12, 8]} style={{ marginBottom: 16 }}>
      <StatusTag status={context.evaluator.status} />
      <Tag color={active.has(context.evaluator.status) ? 'orange' : 'blue'}>{active.has(context.evaluator.status) ? '临时统计' : '本次评测结果'}</Tag>
      <Typography.Text>数据集：{context.dataset.dataset_name} · {context.dataset.version_label}</Typography.Text>
      <Typography.Text type="secondary">评测编号 {context.task.id.slice(0, 8)}</Typography.Text>
      {context.evaluator.prompt_version_id && <Typography.Text type="secondary">Prompt {context.evaluator.prompt_version_label || context.evaluator.prompt_version_id.slice(0, 8)}</Typography.Text>}
      <Button loading={versionQuery.loading} onClick={() => setDatasetPreview({})}>查看评测数据集</Button>
      <Button onClick={scrollToItems}>查看逐句评分</Button>
    </Space>
    {context.evaluator.error && <Alert type="error" showIcon message="评价器任务失败" description={context.evaluator.error} action={context.evaluator.failed_items > 0 ? <Button danger loading={retrying} disabled={active.has(context.evaluator.status)} onClick={retry}>重试失败项</Button> : undefined} style={{ marginBottom: 16 }} />}
    {context.evaluator.status !== 'completed' && !context.evaluator.error && <Alert type="warning" showIcon message={active.has(context.evaluator.status) ? '该任务尚未完全结束' : '任务已结束，存在失败或取消项'} action={context.evaluator.failed_items > 0 ? <Button loading={retrying} disabled={active.has(context.evaluator.status)} onClick={retry}>重试失败项</Button> : undefined} description="通过率按全部样本计算，未评分计为未通过；评分完成后会更新。平均分仅基于已评分样本。" style={{ marginBottom: 16 }} />}
    <QueryError error={contextError} retry={contextQuery.refresh} /><QueryError error={summaryError} retry={summaryQuery.refresh} /><QueryError error={languageQuery.error} retry={languageQuery.refresh} /><QueryError error={versionQuery.error} retry={versionQuery.refresh} />
    <Card className="result-hero" style={{ marginBottom: 18 }}>
      <div className="result-threshold-toolbar">
        <Space wrap><strong>通过判定：得分 ≥</strong><InputNumber aria-label="通过阈值" min={0} max={max} step={0.1} value={threshold} onChange={value => updateFilters({ threshold: value })} /><span>（0–{max} {context.evaluator.evaluator_type === 'sacrebleu_zh' ? 'BLEU' : '分'}）</span></Space>
        <Space wrap><span>{threshold === context.evaluator.default_threshold ? '评价器默认阈值' : `已临时调整 · 默认 ${context.evaluator.default_threshold}`}</span>{threshold !== context.evaluator.default_threshold && <Button size="small" onClick={() => updateFilters({ threshold: null })}>恢复默认阈值</Button>}</Space>
      </div>
      {summary ? <Row align="middle" gutter={28}>
        <Col flex="150px"><div className="score-ring" style={{ '--percent': `${(summary.micro_accuracy ?? 0) * 100}%` } as React.CSSProperties}><div><strong>{summary.micro_accuracy == null ? '—' : `${(summary.micro_accuracy * 100).toFixed(1)}%`}</strong><span>加权平均通过率</span></div></div></Col>
        <Col flex="auto"><Row gutter={26}>
          <Col span={6}><Statistic title="已评分平均分（按句子）" value={summary.micro_mean ?? '—'} precision={3} suffix={`/ ${max}`} /></Col>
          <Col span={6}><Statistic title="已评分平均分（按语种）" value={summary.macro_mean ?? '—'} precision={3} /></Col>
          <Col span={6}><Statistic title="宏平均通过率" value={summary.macro_accuracy == null ? '—' : summary.macro_accuracy * 100} precision={1} suffix="%" /></Col>
          <Col span={6}><Statistic title="评分覆盖率" value={summary.coverage * 100} precision={1} suffix="%" /></Col>
        </Row></Col>
      </Row> : <Skeleton active paragraph={{ rows: 2 }} />}
      <div className="result-summary-caption">通过 {summary?.passed ?? '—'} / 总数 {summary?.total ?? '—'} · 未评分 {summary?.unscored ?? '—'}（计为未通过）。加权平均通过率 = 总通过数 / 总样本数；宏平均通过率按语种等权。</div>
    </Card>
    {summary && <Card className="panel-card" title="各语种评测结果" style={{ marginBottom: 18 }}>
      <Typography.Paragraph type="secondary">得分 ≥ {threshold} 为通过，通过率 = 通过数 / 该语种总数。点击语种查看全部句子，点击未评分数量排查异常。点击列标题：升序 → 降序 → 默认；语言对按语种代码排序。</Typography.Paragraph>
      <Input.Search aria-label="搜索语种统计" placeholder="搜索语种名称或代码" allowClear value={languageSearch} onChange={event => updateFilters({ language_q: event.target.value, language_page: null })} style={{ maxWidth: 350, marginBottom: 16 }} />
      <Table rowKey="source_language" size="small" onChange={(_, __, sorter, extra) => {
        if (extra.action !== 'sort') return
        const next = Array.isArray(sorter) ? sorter[0] : sorter
        updateFilters({ language_sort: next.order ? String(next.columnKey) : null, language_direction: next.order ? next.order === 'descend' ? 'desc' : 'asc' : null, language_page: null })
      }} pagination={{ current: languagePage, pageSize: languagePageSize, pageSizeOptions: [10, 20, 40], showSizeChanger: true, showQuickJumper: true, showTotal: total => `${total} 个语言对`, onChange: (value, size) => updateFilters({ language_page: size === languagePageSize && value > 1 ? value : null, language_page_size: size === 10 ? null : size }) }} scroll={{ x: 1000 }} dataSource={visibleLanguages} columns={sortableColumns<ThresholdSummary['by_language'][number]>([
        { title: '语言对', key: 'source_language', dataIndex: 'source_language', width: 220, render: (value: string) => <Button type="link" className="result-table-link" onClick={() => inspectLanguage(value)}>{languagePairLabel(value)}</Button> },
        { title: '通过 / 总数', key: 'passed', render: (_, row) => `${row.passed} / ${row.total}` },
        { title: '通过率', key: 'accuracy', render: (_, row) => row.accuracy == null ? '—' : `${(row.accuracy * 100).toFixed(1)}%` },
        { title: '已评分 / 总数', key: 'count', render: (_, row) => `${row.count} / ${row.total}` },
        { title: '评分覆盖率', key: 'coverage', render: (_, row) => `${(row.coverage * 100).toFixed(1)}%` },
        { title: '已评分平均分', key: 'mean', dataIndex: 'mean', render: (value: number | null) => formatScore(value) },
        { title: '未评分（计未通过）', key: 'unscored', render: (_, row) => row.unscored ? <Button danger type="link" className="result-table-link" aria-label={`查看 ${row.source_language} 未评分样本`} onClick={() => inspectLanguage(row.source_language, true)}>{row.unscored}</Button> : 0 },
        { title: '操作', fixed: 'right', width: 90, render: (_, row) => <Button type="link" className="result-table-link" onClick={() => inspectLanguage(row.source_language)}>查看明细</Button> },
      ], { source_language: row => row.source_language, passed: row => row.passed, accuracy: row => row.accuracy, count: row => row.count, coverage: row => row.coverage, mean: row => row.mean, unscored: row => row.unscored }).map(column => languageSortKeys.has(String(column.key)) ? { ...column, sortOrder: languageSortKey === column.key ? languageSortOrder : null } : column)} />
    </Card>}
    <div ref={itemsRef} tabIndex={-1} className="result-items-section" aria-label="逐句评分明细">
    <Card className="panel-card" title="逐句评分明细" style={{ marginTop: 18 }}>
      <Space wrap size={[16, 12]} className="result-filters">
        <label className="result-filter">语种<Select aria-label="语种筛选" value={language} allowClear showSearch placeholder="全部语种" style={{ width: 240 }} optionFilterProp="label" options={summary?.by_language.map((item) => ({ label: languagePairLabel(item.source_language), value: item.source_language }))} onChange={(value) => updateFilters({ language: value, page: null })} /></label>
        <label className="result-filter">评分状态<Select aria-label="评分状态筛选" value={status} allowClear placeholder="全部状态" style={{ width: 195 }} options={[{ label: '已评分', value: 'completed' }, { label: '未评分（全部）', value: 'unscored' }, { label: '评分失败', value: 'failed' }, { label: '已取消', value: 'cancelled' }, { label: '等待评分', value: 'queued' }, { label: '评分中', value: 'running' }]} onChange={(value) => updateFilters({ status: value, ...(value && value !== 'completed' ? { score_value: null } : {}), page: null })} /></label>
        <label className="result-filter">分数条件<Space.Compact><Select aria-label="分数比较方式" disabled={onlyUnscored} value={scoreOperator} style={{ width: 140 }} options={scoreOperators} onChange={(value) => updateFilters({ score_operator: value, page: null })} /><InputNumber aria-label="筛选分数" disabled={onlyUnscored} min={0} max={max} step={0.1} value={scoreValue} placeholder={`0–${max}`} style={{ width: 110 }} onChange={(value) => updateFilters({ score_value: value, page: null })} /></Space.Compact></label>
        <label className="result-filter">样本 ID<Input.Search key={sampleId} aria-label="查找评分样本 ID" defaultValue={sampleId} allowClear placeholder="输入完整样本 ID" style={{ width: 240 }} onSearch={value => updateFilters({ sample_id: value, page: null })} /></label>
        <Button onClick={clearFilters}>清除筛选</Button>
      </Space>
      <Space wrap style={{ marginTop: 12 }}><Typography.Text type="secondary">快捷筛选：</Typography.Text><Button size="small" onClick={() => updateFilters({ status: 'completed', score_operator: 'gte', score_value: threshold, page: null })}>通过句子（≥ {threshold}）</Button><Button size="small" onClick={() => updateFilters({ status: 'completed', score_operator: 'lt', score_value: threshold, page: null })}>低于阈值（&lt; {threshold}）</Button><Button size="small" onClick={() => updateFilters({ status: 'unscored', score_value: null, page: null })}>未评分</Button></Space>
      <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>当前匹配 {items?.total ?? '—'} 条。分数条件仅匹配已评分句子，等于按原始分数精确匹配；展开可查看原始分数及异常详情。点击列标题：升序 → 降序 → 默认，排序应用于全部匹配句子；源语种按语种代码排序。</Typography.Paragraph>
      <QueryError error={itemsError} retry={itemsQuery.refresh} />
      <Table sortDirections={['ascend', 'descend']} scroll={{ x: 1100, y: '60vh' }} loading={itemsQuery.loading} onChange={(_, __, sorter, extra) => {
        if (extra.action === 'sort') { const next = Array.isArray(sorter) ? sorter[0] : sorter; updateFilters({ sort: next.order ? String(next.columnKey) : null, direction: next.order ? next.order === 'descend' ? 'desc' : 'asc' : null, page: null }) }
      }} rowKey="sample_id" dataSource={items?.items || []} locale={{ emptyText: language || status || sampleId || scoreValue != null ? '没有符合当前筛选条件的句子，请调整或清除筛选。' : '暂无样本' }} pagination={{ current: page, pageSize, total: items?.total || 0, pageSizeOptions: [20, 50, 100], showSizeChanger: true, showQuickJumper: true, showTotal: total => `共 ${total.toLocaleString()} 条`, onChange: (value, size) => updateFilters({ page: size === pageSize ? value : 1, page_size: size === 50 ? null : size }) }} expandable={{ expandedRowRender: (row) => <Row gutter={14}><Col xs={24} md={8}><Typography.Text type="secondary">源文</Typography.Text><div className="code-template">{row.source_text}</div></Col><Col xs={24} md={8}><Typography.Text type="secondary">中文 GT</Typography.Text><div className="code-template">{row.reference_zh}</div></Col><Col xs={24} md={8}><Typography.Text type="secondary">候选译文</Typography.Text><div className="code-template">{row.translation_zh}</div></Col>{row.reason && <Col span={24} style={{ marginTop: 12 }}><Typography.Text type="secondary">评分理由：</Typography.Text> {row.reason}</Col>}{row.error && <Col span={24} style={{ marginTop: 12 }}><Alert type="error" showIcon message="评分异常" description={<div className="result-error-text">{row.error}</div>} /></Col>}<Col span={24} style={{ marginTop: 8 }}><Space size="large" wrap><Button size="small" onClick={() => setDatasetPreview({ language: row.source_language, sample: row.sample_id })}>核对数据集原文</Button><span>原始分数 {row.score ?? '—'} {row.unit}</span><span>评分尝试 {row.attempts} 次</span><span>实际模型 {row.actual_evaluator_model || '—'}</span><span>Prompt {row.actual_prompt_version_id || '—'}</span><span>Base URL {row.actual_base_url || '—'}</span></Space></Col></Row> }} columns={[
        { title: '样本 ID', key: 'sample_id', sorter: true, sortOrder: sortKey === 'sample_id' ? sortOrder : null, dataIndex: 'sample_id', width: 160 },
        { title: '源语种', key: 'source_language', sorter: true, sortOrder: sortKey === 'source_language' ? sortOrder : null, dataIndex: 'source_language', width: 130, render: (value: string) => <Tag>{languageLabel(value)}</Tag> },
        { title: '候选译文', key: 'translation_zh', sorter: true, sortOrder: sortKey === 'translation_zh' ? sortOrder : null, dataIndex: 'translation_zh', render: (value: string) => <div className="source-cell">{value}</div> },
        { title: '分数', key: 'score', sorter: true, sortOrder: sortKey === 'score' ? sortOrder : null, dataIndex: 'score', width: 100, render: (value: number | null, row) => <span className="score-cell" title={value == null ? '未评分' : `原始分数：${value}`}>{formatScore(value)} {row.unit}</span> },
        { title: '评分状态', key: 'status', sorter: true, sortOrder: sortKey === 'status' ? sortOrder : null, width: 110, render: (_, row) => row.status === 'unscored' ? <Tag>未评分</Tag> : <StatusTag status={row.status} /> },
        { title: '判定', key: 'verdict', sorter: true, sortOrder: sortKey === 'verdict' ? sortOrder : null, width: 160, render: (_, row) => !row.scored ? <Tag color="error">未评分，计未通过</Tag> : row.score != null && row.score >= threshold ? <Tag color="success">通过</Tag> : <Tag color="error">未达标</Tag> },
        { title: '来源', key: 'cache_hit', sorter: true, sortOrder: sortKey === 'cache_hit' ? sortOrder : null, width: 95, render: (_, row) => !row.scored ? '—' : row.cache_hit ? <Tag color="gold">缓存</Tag> : <Tag color="blue">新评分</Tag> },
        { title: '说明', key: 'reason', sorter: true, sortOrder: sortKey === 'reason' ? sortOrder : null, dataIndex: 'reason', ellipsis: true, render: (value: string | null, row) => <span className="reason-text">{value || row.error || '—'}</span> },
      ]} />
    </Card>
    </div>
    <Collapse className="panel-card" style={{ marginTop: 18 }} items={[{ key: 'charts', label: <Space><LineChartOutlined />语种图表与识别统计</Space>, children: <>
      {summary && <ScoreChart data={summary.by_language} />}
      {languageDetection && <LanguageDetectionStats data={languageDetection} />}
    </> }]} />
    {context.evaluator.evaluator_type === 'sacrebleu_zh' && <Card className="panel-card" title="标准语料级指标" style={{ marginTop: 18 }}><Typography.Paragraph type="secondary">按当前成功评分样本计算；失败和取消项不参与。任务结束后更新语料级指标。</Typography.Paragraph>{summary?.aggregates.some((item) => item.metric_name === 'corpus_bleu') ? <Space wrap>{summary.aggregates.filter((item) => item.metric_name === 'corpus_bleu').map((item, index) => <Tag key={index} color="purple">{String(item.source_language || '全部成功样本')} corpus BLEU: {formatScore(Number(item.value))} · {Number(item.sample_count)} 条</Tag>)}</Space> : <Typography.Text type="secondary">语料级指标暂未生成</Typography.Text>}</Card>}
    {datasetPreview && versionQuery.data && <DatasetSamples key={`${versionQuery.data.id}:${datasetPreview.language || ''}:${datasetPreview.sample || ''}`} version={versionQuery.data} initialLanguage={datasetPreview.language} initialSampleId={datasetPreview.sample} onClose={() => setDatasetPreview(null)} />}
  </>
}
