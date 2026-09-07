import { Button, Input, Modal, Select, Table, Typography } from 'antd'
import type { ColumnType } from 'antd/es/table'
import { useState } from 'react'
import { useApiQuery } from '../hooks/useApiQuery'
import { languagePairLabel } from '../languages'
import type { DatasetVersion, PageResponse } from '../types'
import QueryError from './QueryError'

interface Sample { sample_id: string; source_language: string; source_text: string; reference_zh: string }
export interface DatasetSampleFilters {
  language?: string
  sampleId?: string
  page: number
  sort?: string
  direction?: 'asc' | 'desc'
}
interface DatasetSamplesProps {
  version: DatasetVersion
  onClose: () => void
  initialLanguage?: string
  initialSampleId?: string
  initialPage?: number
  initialSort?: string
  initialDirection?: string
  onFiltersChange?: (filters: DatasetSampleFilters) => void
}

export default function DatasetSamples({ version, onClose, initialLanguage, initialSampleId, initialPage = 1, initialSort, initialDirection, onFiltersChange }: DatasetSamplesProps) {
  const [page, setPage] = useState(initialPage)
  const [language, setLanguage] = useState<string | undefined>(initialLanguage)
  const [sampleId, setSampleId] = useState<string | undefined>(initialSampleId)
  const [sampleDraft, setSampleDraft] = useState(initialSampleId || '')
  const [sort, setSort] = useState<string | undefined>(['sample_id', 'source_language', 'source_text', 'reference_zh'].includes(initialSort || '') ? initialSort : undefined)
  const [direction, setDirection] = useState<'asc' | 'desc'>(initialDirection === 'desc' ? 'desc' : 'asc')
  const params = new URLSearchParams({ page: String(page), page_size: '20' })
  if (language) params.set('language', language)
  if (sampleId) params.set('sample_id', sampleId)
  if (sort) { params.set('sort', sort); params.set('direction', direction) }
  const query = useApiQuery<PageResponse<Sample>>(`/dataset-versions/${version.id}/samples?${params}`)
  const pairs = version.language_pairs || []
  const options = pairs.length
    ? pairs.map(pair => ({ value: pair.source_language, label: `${languagePairLabel(pair.source_language, pair.source_name)} · ${pair.sample_count.toLocaleString()} 句` }))
    : version.source_languages.map(value => ({ value, label: languagePairLabel(value) }))
  const changeFilters = (filters: DatasetSampleFilters) => {
    setLanguage(filters.language)
    setSampleId(filters.sampleId)
    setPage(filters.page)
    onFiltersChange?.({ ...filters, sort, direction: sort ? direction : undefined })
  }
  const sortColumn = (key: string): Pick<ColumnType<Sample>, 'key' | 'sorter' | 'sortOrder'> => ({ key, sorter: true, sortOrder: sort === key ? direction === 'asc' ? 'ascend' : 'descend' : null })
  const clearFilters = () => { setSampleDraft(''); changeFilters({ page: 1 }) }

  return <Modal title={`样本预览 · ${version.dataset_name || version.dataset_key} · ${version.version_label}`} width={1100} open footer={null} onCancel={onClose}>
    <Typography.Paragraph type="secondary">数据集 {version.dataset_key} · 版本 {version.version_label} · 共 {version.sample_count.toLocaleString()} 句、{version.source_languages.length} 个语言对。源文和中文参考译文来自此版本。</Typography.Paragraph>
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, marginBottom: 12 }}>
      <Select aria-label="筛选样本语种" showSearch optionFilterProp="label" allowClear placeholder="全部语言对" style={{ flex: '0 1 350px', minWidth: 0, maxWidth: '100%' }} value={language} options={options} onChange={value => changeFilters({ language: value, sampleId, page: 1 })} />
      <Input.Search aria-label="查找样本 ID" value={sampleDraft} allowClear placeholder="输入完整样本 ID" enterButton="查找" style={{ flex: '0 1 310px', minWidth: 0, maxWidth: '100%' }} onChange={event => { setSampleDraft(event.target.value); if (!event.target.value) changeFilters({ language, page: 1 }) }} onSearch={value => changeFilters({ language, sampleId: value || undefined, page: 1 })} />
      <Button onClick={clearFilters}>清除筛选</Button>
    </div>
    <Typography.Paragraph type="secondary">当前匹配 {query.data?.total ?? '—'} 条。样本 ID 按完整内容精确匹配，可与评分明细中的样本 ID 核对。点击列标题：升序 → 降序 → 默认；语言对按语种代码排序。</Typography.Paragraph>
    <QueryError error={query.error} retry={query.refresh} />
    <Table<Sample> rowKey="sample_id" loading={query.loading} dataSource={query.data?.items || []} size="small" sortDirections={['ascend', 'descend']} onChange={(_, __, sorter, extra) => {
      if (extra.action !== 'sort') return
      const current = Array.isArray(sorter) ? sorter[0] : sorter
      const nextSort = current.order ? String(current.columnKey) : undefined
      const nextDirection = current.order === 'descend' ? 'desc' : 'asc'
      setSort(nextSort)
      setDirection(nextDirection)
      setPage(1)
      onFiltersChange?.({ language, sampleId, page: 1, sort: nextSort, direction: nextSort ? nextDirection : undefined })
    }} scroll={{ x: 1040 }} locale={{ emptyText: language || sampleId ? '没有符合当前筛选条件的样本，请调整或清除筛选。' : '此版本暂无样本' }} pagination={{ current: page, pageSize: 20, total: query.data?.total || 0, showSizeChanger: false, showTotal: total => `共 ${total} 条`, onChange: nextPage => changeFilters({ language, sampleId, page: nextPage }) }} columns={[
      { ...sortColumn('sample_id'), title: '样本 ID', dataIndex: 'sample_id', width: 160, render: (value: string) => <Typography.Text copyable style={{ overflowWrap: 'anywhere' }}>{value}</Typography.Text> },
      { ...sortColumn('source_language'), title: '语言对', dataIndex: 'source_language', width: 220, render: (value: string) => languagePairLabel(value, pairs.find(pair => pair.source_language === value)?.source_name) },
      { ...sortColumn('source_text'), title: '源文', dataIndex: 'source_text', width: 330, render: (value: string) => <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{value}</div> },
      { ...sortColumn('reference_zh'), title: '中文参考译文', dataIndex: 'reference_zh', width: 330, render: (value: string) => <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{value}</div> },
    ]} />
  </Modal>
}
