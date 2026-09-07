import { ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Empty, Input, InputNumber, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import type { ColumnType } from 'antd/es/table'
import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, formatDate, formatScore } from '../api'
import PageHeader from '../components/PageHeader'
import StatusTag from '../components/StatusTag'
import { useApiQuery } from '../hooks/useApiQuery'
import { useTaskChanges } from '../hooks/useTaskChanges'
import { sortableColumns, statusOrder } from '../tableSorting'
import type { EvaluationTask, DatasetJob, DatasetVersion, EvaluatorJob, PageResponse, Status, ThresholdSummary } from '../types'

interface ResultRow {
  task: EvaluationTask
  dataset: DatasetJob & { dataset_id: string; dataset_version_id: string }
  evaluator: EvaluatorJob
  summary: Pick<ThresholdSummary, 'threshold' | 'score_max' | 'unit' | 'micro_accuracy' | 'micro_mean' | 'passed' | 'total' | 'successful' | 'unscored' | 'coverage'>
}
interface CompareResponse { threshold: number; strictly_comparable: boolean; warning: string | null; items: Array<ThresholdSummary & { run_name: string; model_family: string; dataset_key: string; version_label: string; evaluator_name: string; status: Status }> }
type CompareRow = CompareResponse['items'][number]
interface VersionContext extends DatasetVersion { dataset_key: string; dataset_name: string }
const errorText = (error: unknown) => error instanceof Error ? error.message : '请求失败'
const statusOptions = [
  { value: 'all', label: '全部状态' },
  { value: 'completed', label: '已完成' },
  { value: 'active', label: '进行中' },
  { value: 'exception', label: '失败或取消' },
]
const percentage = (value: number | null) => value == null ? '—' : `${(value * 100).toFixed(1)}%`

export default function ResultsListPage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const rawPage = Number(searchParams.get('page') || 1)
  const page = Number.isSafeInteger(rawPage) && rawPage > 0 ? rawPage : 1
  const query = searchParams.get('q') || ''
  const versionId = searchParams.get('dataset_version_id') || ''
  const requestedStatus = searchParams.get('status_group') || 'all'
  const statusGroup = statusOptions.some((option) => option.value === requestedStatus) ? requestedStatus : 'all'
  const requestedSort = searchParams.get('sort') || ''
  const sort = ['run_name', 'dataset', 'micro_accuracy', 'micro_mean', 'status', 'created_at'].includes(requestedSort) ? requestedSort : ''
  const direction = searchParams.get('direction') === 'desc' ? 'desc' : 'asc'
  const [selectedJobs, setSelectedJobs] = useState<React.Key[]>([])
  const [comparison, setComparison] = useState<CompareResponse | null>(null)
  const [compareOpen, setCompareOpen] = useState(false)
  const [compareThreshold, setCompareThreshold] = useState(8)
  const [comparing, setComparing] = useState(false)
  const requestParams = new URLSearchParams({ page: String(page), page_size: '20', q: query, status_group: statusGroup })
  if (versionId) requestParams.set('dataset_version_id', versionId)
  if (sort) { requestParams.set('sort', sort); requestParams.set('direction', direction) }
  const { data, error: listError, loading, refresh } = useApiQuery<PageResponse<ResultRow>>(`/results/page?${requestParams}`)
  const versionQuery = useApiQuery<VersionContext>(versionId ? `/dataset-versions/${encodeURIComponent(versionId)}` : null)
  useTaskChanges(refresh)
  const updateFilters = (values: Record<string, string | undefined>, changePage = false, preserveSelection = false) => {
    setSearchParams((previous) => {
      const next = new URLSearchParams(previous)
      if (!changePage) next.delete('page')
      for (const [key, value] of Object.entries(values)) {
        if (value && !(key === 'status_group' && value === 'all') && !(key === 'page' && value === '1')) next.set(key, value)
        else next.delete(key)
      }
      return next
    })
    if (!changePage && !preserveSelection) setSelectedJobs([])
  }
  const sortColumn = (key: string): Pick<ColumnType<ResultRow>, 'key' | 'sorter' | 'sortOrder'> => ({ key, sorter: true, sortOrder: sort === key ? direction === 'asc' ? 'ascend' : 'descend' : null })
  const openResult = (jobId: string, unscored = false, threshold?: number) => {
    const suffix = searchParams.toString()
    const params = new URLSearchParams({ return_to: `/results${suffix ? `?${suffix}` : ''}` })
    if (unscored) params.set('status', 'unscored')
    if (threshold != null) params.set('threshold', String(threshold))
    navigate(`/results/${jobId}?${params}`)
  }
  const compare = async () => {
    setComparing(true)
    try {
      const value = await api<CompareResponse>('/results/compare', { method: 'POST', body: JSON.stringify({ evaluator_job_ids: selectedJobs, threshold: compareThreshold }) })
      setComparison(value)
      setCompareOpen(true)
    } catch (error) {
      message.error(errorText(error))
    } finally { setComparing(false) }
  }
  return <>
    <PageHeader title="评测结果" subtitle="直接查看每次评测的加权准确率，进入分析结果核对各语言对和逐句评分。" actions={<Space wrap><InputNumber aria-label="对比阈值" min={0} max={100} step={0.1} value={compareThreshold} onChange={(value) => setCompareThreshold(value ?? 0)} addonBefore="对比阈值" style={{ width: 200 }} /><Button type="primary" loading={comparing} disabled={selectedJobs.length < 2 || selectedJobs.length > 12} onClick={compare}>对比所选 ({selectedJobs.length})</Button></Space>} />
    {listError && <Alert type="error" showIcon message="结果列表加载失败" description={listError} action={<Button icon={<ReloadOutlined />} onClick={refresh}>重试</Button>} style={{ marginBottom: 16 }} />}
    <Card className="panel-card">
      <Space wrap style={{ marginBottom: 12 }}>
        <Input.Search key={query} aria-label="搜索评测结果" allowClear defaultValue={query} placeholder="搜索运行、模型、数据集或版本" style={{ width: 285 }} onSearch={(value) => updateFilters({ q: value.trim() })} />
        <Select aria-label="筛选评测状态" value={statusGroup} style={{ width: 150 }} options={statusOptions} onChange={(value) => updateFilters({ status_group: value })} />
        {versionId && <Tag color="blue" closable onClose={() => updateFilters({ dataset_version_id: undefined })}>数据版本：{versionQuery.data ? `${versionQuery.data.dataset_key} · ${versionQuery.data.version_label}` : versionQuery.loading ? '加载中…' : versionId}</Tag>}
        {(query || versionId || statusGroup !== 'all') && <Button onClick={() => updateFilters({ q: undefined, dataset_version_id: undefined, status_group: undefined })}>清除筛选</Button>}
        <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
      </Space>
      {versionQuery.error && <Alert type="warning" showIcon message="筛选的数据版本无法读取" description={versionQuery.error} action={<Button onClick={versionQuery.refresh}>重试</Button>} style={{ marginBottom: 12 }} />}
      <Typography.Paragraph type="secondary" style={{ marginBottom: 14 }}>共 {data?.total ?? '—'} 条评测记录。准确率 = 正确数 / 总样本数，未评分计失败；每行使用该评价修订的默认阈值，平均分仅统计已评分样本。进行中的结果会继续更新。点击列标题：升序 → 降序 → 默认。</Typography.Paragraph>
      <Table<ResultRow> size="small" sortDirections={['ascend', 'descend']} onChange={(_, __, sorter, extra) => {
        if (extra.action !== 'sort') return
        const current = Array.isArray(sorter) ? sorter[0] : sorter
        updateFilters({ sort: current.order ? String(current.columnKey) : undefined, direction: current.order ? current.order === 'ascend' ? 'asc' : 'desc' : undefined }, false, true)
      }} rowKey={(row) => row.evaluator.id} rowSelection={{ columnWidth: 42, preserveSelectedRowKeys: true, selectedRowKeys: selectedJobs, onChange: setSelectedJobs, getCheckboxProps: (row) => ({ disabled: !row.summary.total }) }} dataSource={data?.items || []} loading={loading} scroll={{ x: 1120 }} pagination={{ current: page, pageSize: 20, total: data?.total || 0, showSizeChanger: false, onChange: (value) => updateFilters({ page: String(value) }, true) }} locale={{ emptyText: <Empty description={listError ? '暂时无法获取结果' : loading ? '正在加载…' : query || versionId || statusGroup !== 'all' ? '没有符合筛选条件的评测结果' : '暂无评测结果'} /> }} columns={[
        { ...sortColumn('run_name'), title: '模型运行', width: 175, render: (_, row) => <div style={{ overflowWrap: 'anywhere' }}><Typography.Text strong>{row.task.run_name}</Typography.Text><div><Typography.Text type="secondary">{row.task.model_family}</Typography.Text></div><Typography.Text type="secondary" style={{ fontSize: 12 }}>评测 {row.task.id.slice(0, 8)}</Typography.Text></div> },
        { ...sortColumn('dataset'), title: '数据集与评价方式', width: 208, render: (_, row) => <div style={{ overflowWrap: 'anywhere' }}><Button type="link" style={{ height: 'auto', padding: 0, whiteSpace: 'normal', textAlign: 'left' }} onClick={() => navigate(`/datasets?version=${encodeURIComponent(row.dataset.dataset_version_id)}`)}>{row.dataset.dataset_key} · {row.dataset.version_label}</Button><div style={{ marginTop: 4 }}><Tag color={row.evaluator.evaluator_type === 'sacrebleu_zh' ? 'purple' : 'blue'} style={{ whiteSpace: 'normal', marginInlineEnd: 4 }}>{row.evaluator.name}</Tag><Typography.Text type="secondary">r{row.evaluator.revision}</Typography.Text></div>{row.evaluator.prompt_version_id && <Typography.Text type="secondary" style={{ fontSize: 12 }}>Prompt {row.evaluator.prompt_version_label || row.evaluator.prompt_version_id.slice(0, 8)}</Typography.Text>}</div> },
        { ...sortColumn('micro_accuracy'), title: '加权平均准确率', width: 170, render: (_, row) => <div><Typography.Text strong style={{ fontSize: 18 }}>{percentage(row.summary.micro_accuracy)}</Typography.Text><div>正确 {row.summary.passed} / 总数 {row.summary.total}</div><Typography.Text type="secondary" style={{ fontSize: 12 }}>默认 ≥ {row.summary.threshold} · 0–{row.summary.score_max}{row.summary.unit === 'BLEU' ? ' BLEU' : ' 分'}</Typography.Text></div> },
        { ...sortColumn('micro_mean'), title: '已评分平均分', width: 125, render: (_, row) => <div><Typography.Text strong>{formatScore(row.summary.micro_mean)}</Typography.Text><Typography.Text type="secondary"> / {row.summary.score_max}</Typography.Text><div><Typography.Text type="secondary" style={{ fontSize: 12 }}>{row.summary.successful} 条已评分</Typography.Text></div></div> },
        { ...sortColumn('status'), title: '评分状态', width: 132, render: (_, row) => <div><StatusTag status={row.evaluator.status} /><div>{row.summary.unscored ? <Button danger type="link" style={{ height: 'auto', padding: 0 }} aria-label={`查看 ${row.task.run_name} 未评分样本`} onClick={() => openResult(row.evaluator.id, true)}>未评分 {row.summary.unscored} 条</Button> : <Typography.Text type="secondary">未评分 0 条</Typography.Text>}</div><Typography.Text type="secondary" style={{ fontSize: 12 }}>覆盖 {percentage(row.summary.coverage)}</Typography.Text></div> },
        { ...sortColumn('created_at'), title: '提交时间', width: 140, render: (_, row) => formatDate(row.task.created_at) },
        { title: '结果详情', fixed: 'right', width: 106, render: (_, row) => <Button type="link" style={{ paddingInline: 4 }} onClick={() => openResult(row.evaluator.id)}>分析结果</Button> },
      ]} />
    </Card>
    <Modal title={`模型结果对比 · 阈值 ≥ ${comparison?.threshold ?? compareThreshold}`} width={1180} open={compareOpen} footer={null} onCancel={() => setCompareOpen(false)}>
      <Typography.Paragraph type="secondary">准确率按总样本数计算，未评分计失败。加权平均准确率 = 总正确数 / 总样本数；宏平均准确率按语种等权。平均分仅基于已评分样本。下表统一使用对比阈值，可能与列表中的默认阈值不同。点击列标题：升序 → 降序 → 默认。</Typography.Paragraph>
      {comparison && !comparison.strictly_comparable && <Alert type="warning" showIcon message="所选结果不是严格同口径" description={comparison.warning} style={{ marginBottom: 15 }} />}
      <Table<CompareRow> rowKey="evaluator_job_id" size="small" scroll={{ x: 1330 }} pagination={false} dataSource={comparison?.items || []} columns={sortableColumns<CompareRow>([
        { key: 'run_name', title: '模型运行', width: 170, dataIndex: 'run_name', render: (value: string, row) => <div style={{ overflowWrap: 'anywhere' }}><Typography.Text strong>{value}</Typography.Text><div><Typography.Text type="secondary">{row.model_family}</Typography.Text></div></div> },
        { key: 'dataset', title: '数据集 / 评价器', width: 200, render: (_, row) => <div>{row.dataset_key} · {row.version_label}<div><Typography.Text type="secondary">{row.evaluator_name}</Typography.Text></div></div> },
        { key: 'status', title: '状态', width: 110, render: (_, row) => <StatusTag status={row.status} /> },
        { key: 'micro_accuracy', title: '加权平均准确率', width: 158, render: (_, row) => <div><Typography.Text strong>{percentage(row.micro_accuracy)}</Typography.Text><div>正确 {row.passed} / 总数 {row.total}</div></div> },
        { key: 'unscored', title: '未评分 / 覆盖率', width: 132, render: (_, row) => <div>{row.unscored} 条未评分<div><Typography.Text type="secondary">覆盖 {percentage(row.coverage)}</Typography.Text></div></div> },
        { key: 'score_max', title: '分值范围', width: 115, render: (_, row) => `${row.score_min}–${row.score_max}${row.unit === 'BLEU' ? ' BLEU' : ' 分'}` },
        { key: 'micro_mean', title: '已评分平均分', width: 130, dataIndex: 'micro_mean', render: (value: number | null) => formatScore(value) },
        { key: 'macro_mean', title: '宏平均分', width: 105, dataIndex: 'macro_mean', render: (value: number | null) => formatScore(value) },
        { key: 'macro_accuracy', title: '宏平均准确率', width: 130, dataIndex: 'macro_accuracy', render: (value: number | null) => percentage(value) },
        { title: '结果详情', fixed: 'right', width: 106, render: (_, row) => <Button type="link" style={{ paddingInline: 4 }} onClick={() => openResult(row.evaluator_job_id, false, comparison?.threshold)}>分析结果</Button> },
      ], {
        run_name: row => row.run_name,
        dataset: row => `${row.dataset_key} · ${row.version_label} · ${row.evaluator_name}`,
        status: row => statusOrder[row.status] ?? 99,
        micro_accuracy: row => row.micro_accuracy,
        unscored: row => row.unscored,
        score_max: row => row.score_max,
        micro_mean: row => row.micro_mean,
        macro_mean: row => row.macro_mean,
        macro_accuracy: row => row.macro_accuracy,
      })} />
    </Modal>
  </>
}
