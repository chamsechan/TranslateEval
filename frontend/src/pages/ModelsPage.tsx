import { ExperimentOutlined } from '@ant-design/icons'
import { Card, Empty, Input, Table, Tag, Typography } from 'antd'
import { useEffect, useMemo, useState } from 'react'
import { api, formatDate } from '../api'
import PageHeader from '../components/PageHeader'

interface ModelRun { id: string; run_name: string; model_family: string; checkpoint_name: string; model_version: string; notes: string; inference_platform: string; inference_mode: string; created_at: string }

export default function ModelsPage() {
  const [rows, setRows] = useState<ModelRun[]>([])
  const [query, setQuery] = useState('')
  useEffect(() => { api<ModelRun[]>('/model-runs').then(setRows) }, [])
  const filtered = useMemo(() => rows.filter((item) => [item.run_name, item.model_family, item.checkpoint_name, item.inference_platform].some((value) => value.toLowerCase().includes(query.toLowerCase()))), [rows, query])
  return (
    <>
      <PageHeader title="模型记录" subtitle="模型族、微调检查点、推理平台与解码信息随每次提交永久记录。" actions={<Input.Search allowClear placeholder="搜索模型或平台" style={{ width: 280 }} onChange={(event) => setQuery(event.target.value)} />} />
      <Card className="panel-card">
        <Table rowKey="id" dataSource={filtered} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="提交结果后会自动建立模型记录" /> }} columns={[
          { title: '运行名称', dataIndex: 'run_name', render: (value: string, row: ModelRun) => <div><Typography.Text strong><ExperimentOutlined /> {value}</Typography.Text><div><Typography.Text type="secondary" style={{ fontSize: 12 }}>{row.checkpoint_name}</Typography.Text></div></div> },
          { title: '模型族', dataIndex: 'model_family', render: (value: string) => <Tag color="blue">{value}</Tag> },
          { title: '版本', dataIndex: 'model_version', render: (value: string) => value || '—' },
          { title: '推理平台', dataIndex: 'inference_platform' },
          { title: '模式', dataIndex: 'inference_mode', render: (value: string) => value === 'auto_detect' ? <Tag color="purple">自动识别语种</Tag> : <Tag>已提供源语种</Tag> },
          { title: '备注', dataIndex: 'notes', ellipsis: true },
          { title: '提交时间', dataIndex: 'created_at', render: formatDate },
        ]} />
      </Card>
    </>
  )
}

