import { ExperimentOutlined } from '@ant-design/icons'
import { Button, Card, Descriptions, Empty, Input, Modal, Skeleton, Space, Table, Tag, Typography } from 'antd'
import type { ColumnType } from 'antd/es/table'
import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { formatDate } from '../api'
import PageHeader from '../components/PageHeader'
import QueryError from '../components/QueryError'
import { useApiQuery } from '../hooks/useApiQuery'
import type { PageResponse } from '../types'

interface ModelRun { id: string; run_name: string; model_family: string; checkpoint_name: string; model_version: string; notes: string; inference_platform: string; inference_mode: string; created_at: string }
interface ModelRunDetail extends ModelRun { result_info: { inference: Record<string, unknown> }; submissions: Array<{ id: string; created_at: string }> }

export default function ModelsPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [selected, setSelected] = useState<string | null>(null)
  const detailQuery = useApiQuery<ModelRunDetail>(selected ? `/model-runs/${selected}` : null)
  const detail = detailQuery.data
  const inference = detail?.result_info.inference || {}
  const query = searchParams.get('q') || ''
  const rawPage = Number(searchParams.get('page') || '1')
  const page = Number.isSafeInteger(rawPage) && rawPage > 0 ? rawPage : 1
  const requestedSort = searchParams.get('sort') || ''
  const sort = ['run_name', 'model_family', 'model_version', 'inference_platform', 'inference_mode', 'notes', 'created_at'].includes(requestedSort) ? requestedSort : ''
  const direction = searchParams.get('direction') === 'desc' ? 'desc' : 'asc'
  const requestParams = new URLSearchParams({ page: String(page), page_size: '20', q: query })
  if (sort) { requestParams.set('sort', sort); requestParams.set('direction', direction) }
  const { data, error, loading, refresh } = useApiQuery<PageResponse<ModelRun>>(`/model-runs/page?${requestParams}`)
  const updateFilters = (values: Record<string, string | undefined>, changePage = false) => setSearchParams(previous => {
    const next = new URLSearchParams(previous)
    if (!changePage) next.delete('page')
    for (const [key, value] of Object.entries(values)) {
      if (value && !(key === 'page' && value === '1')) next.set(key, value)
      else next.delete(key)
    }
    return next
  })
  const sortColumn = (key: string): Pick<ColumnType<ModelRun>, 'key' | 'sorter' | 'sortOrder'> => ({ key, sorter: true, sortOrder: sort === key ? direction === 'asc' ? 'ascend' : 'descend' : null })
  return (
    <>
      <PageHeader title="模型记录" subtitle="模型族、微调检查点、推理平台与解码信息随每次提交永久记录。" actions={<Input.Search key={query} aria-label="搜索模型记录" allowClear defaultValue={query} placeholder="搜索模型或平台" style={{ width: 280 }} onSearch={(value) => updateFilters({ q: value.trim() })} />} />
      <QueryError error={error} retry={refresh} />
      <Card className="panel-card">
        <Typography.Paragraph type="secondary">点击列标题：升序 → 降序 → 默认。</Typography.Paragraph>
        <Table<ModelRun> rowKey="id" loading={loading} sortDirections={['ascend', 'descend']} onChange={(_, __, sorter, extra) => {
          if (extra.action !== 'sort') return
          const current = Array.isArray(sorter) ? sorter[0] : sorter
          updateFilters({ sort: current.order ? String(current.columnKey) : undefined, direction: current.order ? current.order === 'ascend' ? 'asc' : 'desc' : undefined })
        }} scroll={{ x: 850 }} dataSource={data?.items || []} pagination={{ current: page, pageSize: 20, total: data?.total || 0, showSizeChanger: false, onChange: value => updateFilters({ page: String(value) }, true) }} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={error ? '暂时无法获取模型记录' : loading ? '正在加载…' : '提交结果后会自动建立模型记录'} /> }} columns={[
          { ...sortColumn('run_name'), title: '运行名称', dataIndex: 'run_name', render: (value: string, row: ModelRun) => <div><Typography.Text strong><ExperimentOutlined /> {value}</Typography.Text><div><Typography.Text type="secondary" style={{ fontSize: 12 }}>{row.checkpoint_name}</Typography.Text></div></div> },
          { ...sortColumn('model_family'), title: '模型族', dataIndex: 'model_family', render: (value: string) => <Tag color="blue">{value}</Tag> },
          { ...sortColumn('model_version'), title: '版本', dataIndex: 'model_version', render: (value: string) => value || '—' },
          { ...sortColumn('inference_platform'), title: '推理平台', dataIndex: 'inference_platform' },
          { ...sortColumn('inference_mode'), title: '模式', dataIndex: 'inference_mode', render: (value: string) => <Tag>{({ default: '默认', thinking: '思考', non_thinking: '不思考', auto_detect: '自动识别语种（历史模式）', source_language_provided: '已提供源语种（历史模式）' } as Record<string, string>)[value] || value}</Tag> },
          { ...sortColumn('notes'), title: '备注', dataIndex: 'notes', ellipsis: true },
          { ...sortColumn('created_at'), title: '提交时间', dataIndex: 'created_at', render: formatDate },
          { title: '操作', render: (_, row) => <Button type="link" onClick={() => setSelected(row.id)}>运行详情</Button> },
        ]} />
      </Card>
      <Modal title={`运行详情 · ${detail?.run_name || ''}`} width={780} open={!!selected} onCancel={() => setSelected(null)} footer={null}>
        <QueryError error={detailQuery.error} retry={detailQuery.refresh} />
        {detailQuery.loading && !detail && <Skeleton active />}
        {detail && <>
          <Descriptions bordered column={{ xs: 1, sm: 2 }} size="small" items={[
            { key: 'family', label: '模型族', children: detail.model_family },
            { key: 'checkpoint', label: '检查点', children: detail.checkpoint_name },
            { key: 'platform', label: '推理平台', children: detail.inference_platform },
            { key: 'version', label: '模型版本', children: detail.model_version || '—' },
            ...Object.entries({ device: '设备', sdk: 'SDK', sdk_version: 'SDK 版本', precision: '精度', generated_at: '推理时间', code_revision: '代码版本' }).map(([key, label]) => ({ key, label, children: String(inference[key] || '—') })),
            { key: 'notes', label: '备注', children: detail.notes || '—', span: 2 },
          ]} />
          <Typography.Title level={5}>解码参数</Typography.Title>
          <div className="code-template">{JSON.stringify(inference.decoding || {}, null, 2)}</div>
          <Space wrap style={{ marginTop: 18 }}>{detail.submissions.map((submission) => <Button key={submission.id} type="primary" onClick={() => navigate(`/submit?submission=${submission.id}`)}>再次评测{detail.submissions.length > 1 ? ` · ${formatDate(submission.created_at)}` : ''}</Button>)}</Space>
        </>}
      </Modal>
    </>
  )
}
