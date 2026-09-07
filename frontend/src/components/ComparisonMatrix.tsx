import { Alert, Button, Input, Select, Space, Table, Typography } from 'antd'
import { useState } from 'react'
import { formatScore } from '../api'
import { languagePairLabel } from '../languages'
import type { ThresholdSummary } from '../types'

export type ComparisonRun = ThresholdSummary & { run_name: string; model_family: string; dataset_key: string; version_label: string; evaluator_name: string }
type LanguageSummary = ThresholdSummary['by_language'][number]
interface MatrixRow { language: string; results: Record<string, LanguageSummary | undefined> }

export default function ComparisonMatrix({ items, comparable, onOpen }: { items: ComparisonRun[]; comparable: boolean; onOpen: (jobId: string, language: string) => void }) {
  const [baselineId, setBaselineId] = useState(items[0]?.evaluator_job_id)
  const [metric, setMetric] = useState<'accuracy' | 'mean'>('accuracy')
  const [query, setQuery] = useState('')
  const [order, setOrder] = useState<'language' | 'delta'>('language')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const baseline = items.find(item => item.evaluator_job_id === baselineId) || items[0]
  const rows: MatrixRow[] = [...new Set(items.flatMap(item => item.by_language.map(row => row.source_language)))].map(language => ({ language, results: Object.fromEntries(items.map(item => [item.evaluator_job_id, item.by_language.find(row => row.source_language === language)])) }))
  const delta = (row: MatrixRow, run: ComparisonRun): number | null => {
    if (!baseline || (metric === 'mean' && (run.unit !== baseline.unit || run.score_min !== baseline.score_min || run.score_max !== baseline.score_max))) return null
    const value = row.results[run.evaluator_job_id]?.[metric]
    const base = row.results[baseline.evaluator_job_id]?.[metric]
    return value == null || base == null ? null : value - base
  }
  const worstDelta = (row: MatrixRow) => {
    const values = items.filter(item => item.evaluator_job_id !== baseline?.evaluator_job_id).map(item => delta(row, item)).filter((value): value is number => value != null)
    return values.length ? Math.min(...values) : null
  }
  const filtered = rows.filter(row => languagePairLabel(row.language).toLowerCase().includes(query.trim().toLowerCase())).sort((a, b) => {
    if (order === 'delta') {
      const av = worstDelta(a), bv = worstDelta(b)
      if (av == null && bv != null) return 1
      if (av != null && bv == null) return -1
      if (av != null && bv != null && av !== bv) return av - bv
    }
    return a.language.localeCompare(b.language)
  })
  return <>
    {!comparable && <Alert type="warning" showIcon message="差值仅供探索，不代表同口径的模型退化或提升" style={{ marginBottom: 12 }} />}
    <Space wrap style={{ marginBottom: 12 }}>
      <label className="result-filter">对比基线<Select aria-label="对比基线" showSearch optionFilterProp="label" style={{ width: 280 }} value={baseline?.evaluator_job_id} onChange={value => { setBaselineId(value); setPage(1) }} options={items.map(item => ({ value: item.evaluator_job_id, label: `${item.run_name} · ${item.evaluator_job_id.slice(0, 8)}` }))} /></label>
      <label className="result-filter">指标<Select aria-label="语种对比指标" style={{ width: 155 }} value={metric} options={[{ value: 'accuracy', label: '阈值通过率' }, { value: 'mean', label: '已评分平均分' }]} onChange={value => { setMetric(value); setPage(1) }} /></label>
      <label className="result-filter">排列<Select aria-label="语种对比排序" style={{ width: 200 }} value={order} options={[{ value: 'language', label: '按语种代码' }, { value: 'delta', label: '按最小基线差值（升序）' }]} onChange={value => { setOrder(value); setPage(1) }} /></label>
      <label className="result-filter">查找语种<Input.Search aria-label="搜索对比语种" allowClear placeholder="名称或代码" value={query} onChange={event => { setQuery(event.target.value); setPage(1) }} style={{ width: 200 }} /></label>
    </Space>
    <Typography.Paragraph type="secondary">通过率 = 通过数 / 该语种总样本数，未评分计未通过。均分仅统计已评分项。Δ = 当前运行 − 基线；通过率差使用百分点。不同分值范围的均分不计算差值。点击数值可查看对应语种的逐句结果。</Typography.Paragraph>
    <Table<MatrixRow> className="comparison-matrix" rowKey="language" size="small" dataSource={filtered} scroll={{ x: 220 + items.length * 220, y: '52vh' }} pagination={{ current: page, pageSize, pageSizeOptions: [10, 20, 40], showSizeChanger: true, showQuickJumper: true, showTotal: total => `共 ${total} 个语言对`, onChange: (value, size) => { setPage(size === pageSize ? value : 1); setPageSize(size) } }} columns={[
      { title: '语言对', key: 'language', width: 220, fixed: 'left', render: (_, row) => languagePairLabel(row.language) },
      ...items.map(item => ({ key: item.evaluator_job_id, width: 220, title: <div className="comparison-column-title"><Typography.Text strong>{item.run_name}{item.evaluator_job_id === baseline?.evaluator_job_id ? '（基线）' : ''}</Typography.Text><div>{item.model_family} · {item.evaluator_job_id.slice(0, 8)}</div><Typography.Text type="secondary">{item.dataset_key} · {item.version_label} · {item.evaluator_name}</Typography.Text></div>, render: (_: unknown, row: MatrixRow) => {
        const result = row.results[item.evaluator_job_id]
        if (!result) return <Typography.Text type="secondary">不含此语种</Typography.Text>
        const value = result[metric]
        const difference = delta(row, item)
        return <div>
          <Button type="link" className="result-table-link" aria-label={`查看 ${item.run_name} ${row.language} 逐句结果`} onClick={() => onOpen(item.evaluator_job_id, row.language)}>{value == null ? '—' : metric === 'accuracy' ? `${(value * 100).toFixed(1)}%` : `${formatScore(value)} / ${item.score_max}`}</Button>
          {item.evaluator_job_id !== baseline?.evaluator_job_id && <div><Typography.Text type={difference == null || difference === 0 ? 'secondary' : difference < 0 ? 'danger' : 'success'}>{difference == null ? 'Δ —' : `Δ ${difference > 0 ? '+' : ''}${(difference * (metric === 'accuracy' ? 100 : 1)).toFixed(2)}${metric === 'accuracy' ? ' 个百分点' : ' 分'}`}</Typography.Text></div>}
          <div>通过 {result.passed.toLocaleString()} / {result.total.toLocaleString()}</div>
          <Typography.Text type="secondary">覆盖 {(result.coverage * 100).toFixed(1)}% · 未评分 {result.unscored.toLocaleString()}</Typography.Text>
        </div>
      } })),
    ]} />
  </>
}
