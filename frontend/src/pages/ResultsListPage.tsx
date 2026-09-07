import { ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Empty, Input, InputNumber, Modal, Space, Table, Tag, Typography } from 'antd'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, formatDate, formatScore } from '../api'
import PageHeader from '../components/PageHeader'
import StatusTag from '../components/StatusTag'
import { useApiQuery } from '../hooks/useApiQuery'
import { useTaskChanges } from '../hooks/useTaskChanges'
import type { EvaluationTask, DatasetJob, EvaluatorJob, PageResponse, ThresholdSummary } from '../types'

interface ResultRow { task: EvaluationTask; dataset: DatasetJob; evaluator: EvaluatorJob }
interface CompareResponse { threshold: number; strictly_comparable: boolean; warning: string | null; items: Array<ThresholdSummary & { run_name: string; model_family: string; dataset_key: string; version_label: string; evaluator_name: string }> }
const errorText = (error: unknown) => error instanceof Error ? error.message : '请求失败'

export default function ResultsListPage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [page, setPage] = useState(1)
  const [query, setQuery] = useState('')
  const [selectedJobs, setSelectedJobs] = useState<React.Key[]>([])
  const [comparison, setComparison] = useState<CompareResponse | null>(null)
  const [compareOpen, setCompareOpen] = useState(false)
  const [compareThreshold, setCompareThreshold] = useState(8)
  const [comparing, setComparing] = useState(false)
  const { data, error: listError, loading, refresh } = useApiQuery<PageResponse<ResultRow>>(`/results/page?page=${page}&page_size=20&q=${encodeURIComponent(query)}`)
  useTaskChanges(refresh)
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
    <PageHeader title="评测结果" subtitle="选择任一数据集与评价器结果，动态调整阈值并查看逐句原始分数。" actions={<Space wrap><Input.Search allowClear placeholder="搜索运行或模型" style={{ width: 220 }} onSearch={(value) => { setQuery(value); setPage(1); setSelectedJobs([]) }} /><InputNumber min={0} step={0.1} value={compareThreshold} onChange={(value) => setCompareThreshold(value ?? 0)} addonBefore="对比阈值" /><Button type="primary" loading={comparing} disabled={selectedJobs.length < 2 || selectedJobs.length > 12} onClick={compare}>对比所选 ({selectedJobs.length})</Button></Space>} />
    {listError && <Alert type="error" showIcon message="结果列表加载失败" description={listError} action={<Button icon={<ReloadOutlined />} onClick={refresh}>重试</Button>} style={{ marginBottom: 16 }} />}
    <Card className="panel-card"><Table rowKey={(row) => row.evaluator.id} rowSelection={{ preserveSelectedRowKeys: true, selectedRowKeys: selectedJobs, onChange: setSelectedJobs, getCheckboxProps: (row) => ({ disabled: !row.evaluator.completed_items }) }} dataSource={data?.items || []} loading={loading} scroll={{ x: 850 }} pagination={{ current: page, pageSize: 20, total: data?.total || 0, showSizeChanger: false, onChange: setPage }} locale={{ emptyText: <Empty description={listError ? '暂时无法获取结果' : loading ? '正在加载…' : '暂无评测结果'} /> }} columns={[
      { title: '模型运行', render: (_, row) => <div><Typography.Text strong>{row.task.run_name}</Typography.Text><div><Typography.Text type="secondary">{row.task.model_family}</Typography.Text></div></div> },
      { title: '数据集版本', render: (_, row) => <div>{row.dataset.dataset_key}<div><Typography.Text type="secondary">{row.dataset.version_label}</Typography.Text></div></div> },
      { title: '评价方式', render: (_, row) => <Space><Tag color={row.evaluator.evaluator_type === 'sacrebleu_zh' ? 'purple' : 'blue'}>{row.evaluator.name}</Tag><span>r{row.evaluator.revision}</span></Space> },
      { title: '状态', render: (_, row) => <StatusTag status={row.evaluator.status} /> },
      { title: '覆盖', render: (_, row) => `${row.evaluator.completed_items}/${row.evaluator.total_items}` },
      { title: '提交时间', render: (_, row) => formatDate(row.task.created_at) },
      { title: '', render: (_, row) => <Button type="link" disabled={!row.evaluator.completed_items && !row.evaluator.failed_items} onClick={() => navigate(`/results/${row.evaluator.id}`)}>{row.evaluator.completed_items ? '分析结果' : '查看失败明细'}</Button> },
    ]} /></Card>
    <Modal title={`模型结果对比 · 阈值 ≥ ${comparison?.threshold ?? compareThreshold}`} width={960} open={compareOpen} footer={null} onCancel={() => setCompareOpen(false)}>
      {comparison && !comparison.strictly_comparable && <Alert type="warning" showIcon message="所选结果不是严格同口径" description={comparison.warning} style={{ marginBottom: 15 }} />}
      <Table rowKey="evaluator_job_id" pagination={false} dataSource={comparison?.items || []} columns={[
        { title: '模型运行', dataIndex: 'run_name', render: (value: string, row) => <div><Typography.Text strong>{value}</Typography.Text><div><Typography.Text type="secondary">{row.model_family}</Typography.Text></div></div> },
        { title: '数据集', render: (_, row) => `${row.dataset_key} · ${row.version_label}` },
        { title: '评价器', dataIndex: 'evaluator_name' },
        { title: '分值范围', render: (_, row) => `${row.score_min}–${row.score_max} ${row.unit}` },
        { title: '微平均', dataIndex: 'micro_mean', render: (value: number | null) => formatScore(value) },
        { title: '宏平均', dataIndex: 'macro_mean', render: (value: number | null) => formatScore(value) },
        { title: '微准确率', dataIndex: 'micro_accuracy', render: (value: number | null) => value == null ? '—' : `${(value * 100).toFixed(2)}%` },
        { title: '宏准确率', dataIndex: 'macro_accuracy', render: (value: number | null) => value == null ? '—' : `${(value * 100).toFixed(2)}%` },
        { title: '覆盖率', dataIndex: 'coverage', render: (value: number) => `${(value * 100).toFixed(2)}%` },
      ]} />
    </Modal>
  </>
}
