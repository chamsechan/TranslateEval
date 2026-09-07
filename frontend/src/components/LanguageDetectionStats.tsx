import { Col, Input, Row, Statistic, Table, Typography } from 'antd'
import { useState } from 'react'
import { languageLabel } from '../languages'

interface LanguageDetectionRow {
  source_language: string
  sample_count?: number
  covered?: number
  coverage?: number | null
  precision?: number | null
  recall?: number | null
  recall_overall?: number | null
}
export interface LanguageDetectionSummary {
  applicable: boolean
  reason?: string
  total?: number
  covered?: number
  coverage?: number
  correct?: number
  accuracy?: number | null
  accuracy_on_covered?: number | null
  accuracy_overall?: number | null
  by_language?: LanguageDetectionRow[]
}
const percent = (value: number | null | undefined) => value == null ? '—' : `${(value * 100).toFixed(2)}%`

export default function LanguageDetectionStats({ data }: { data: LanguageDetectionSummary }) {
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  if (!data.applicable) return <Typography.Paragraph type="secondary">语种识别不适用：{data.reason}</Typography.Paragraph>
  const overall = data.accuracy_overall ?? (data.total && data.correct != null ? data.correct / data.total : null)
  const coveredAccuracy = data.accuracy_on_covered ?? data.accuracy
  const rows = (data.by_language || []).filter(row => languageLabel(row.source_language).toLowerCase().includes(query.trim().toLowerCase()))
  return <section aria-label="语种识别统计">
    <Typography.Title level={5}>语种识别统计</Typography.Title>
    <Row gutter={[20, 16]}>
      <Col xs={24} md={8}><Statistic title="总体识别准确率（缺失预测计错误）" value={percent(overall)} /><Typography.Text type="secondary">正确识别 {data.correct ?? '—'} / 总样本 {data.total ?? '—'}</Typography.Text></Col>
      <Col xs={24} md={8}><Statistic title="已识别样本准确率" value={percent(coveredAccuracy)} /><Typography.Text type="secondary">正确识别 {data.correct ?? '—'} / 已识别 {data.covered ?? '—'}</Typography.Text></Col>
      <Col xs={24} md={8}><Statistic title="识别覆盖率" value={percent(data.coverage)} /><Typography.Text type="secondary">已识别 {data.covered ?? '—'} / 总样本 {data.total ?? '—'}</Typography.Text></Col>
    </Row>
    {!!data.by_language?.length && <>
      <Typography.Paragraph type="secondary" style={{ marginTop: 16 }}>总体召回率 = 正确识别该语种 / 该语种全部样本；已识别样本召回率仅以有语种预测的样本为分母。精确率 = 正确识别该语种 / 预测为该语种的样本。</Typography.Paragraph>
      <Input.Search aria-label="搜索识别语种" placeholder="搜索语种名称或代码" value={query} allowClear onChange={event => { setQuery(event.target.value); setPage(1) }} style={{ maxWidth: 340, marginBottom: 12 }} />
      <Table<LanguageDetectionRow> rowKey="source_language" size="small" scroll={{ x: 880 }} dataSource={rows} pagination={{ current: page, pageSize: 10, showSizeChanger: false, showQuickJumper: true, hideOnSinglePage: true, onChange: setPage }} columns={[
        { title: '源语种', dataIndex: 'source_language', render: (value: string) => languageLabel(value) },
        { title: '已识别 / 总样本', render: (_, row) => `${row.covered ?? '—'} / ${row.sample_count ?? '—'}` },
        { title: '覆盖率', dataIndex: 'coverage', render: percent },
        { title: '总体召回率', dataIndex: 'recall_overall', render: percent, sorter: (a, b) => (a.recall_overall ?? -1) - (b.recall_overall ?? -1) },
        { title: '已识别样本召回率', dataIndex: 'recall', render: percent },
        { title: '精确率', dataIndex: 'precision', render: percent },
      ]} />
    </>}
  </section>
}
