import { ArrowRightOutlined, CloudUploadOutlined, DatabaseOutlined, ExperimentOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { Button, Card, Col, Empty, Row, Skeleton, Space, Statistic, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import PageHeader from '../components/PageHeader'
import TaskCard from '../components/TaskCard'
import type { Dataset, EvaluationTask } from '../types'

interface Dashboard { datasets: number; dataset_versions: number; model_runs: number; active_tasks: number; completed_tasks: number }

export default function DashboardPage() {
  const navigate = useNavigate()
  const [data, setData] = useState<Dashboard | null>(null)
  const [tasks, setTasks] = useState<EvaluationTask[]>([])
  const [datasets, setDatasets] = useState<Dataset[]>([])

  const load = async () => {
    const [dashboard, taskRows, datasetRows] = await Promise.all([
      api<Dashboard>('/dashboard'), api<EvaluationTask[]>('/tasks?limit=3'), api<Dataset[]>('/datasets'),
    ])
    setData(dashboard); setTasks(taskRows); setDatasets(datasetRows)
  }
  useEffect(() => { void load() }, [])

  return (
    <>
      <PageHeader title="总览" subtitle="管理语料版本、跟踪评测队列并洞察模型质量。" actions={<Button type="primary" icon={<CloudUploadOutlined />} onClick={() => navigate('/submit')}>提交新评测</Button>} />
      <Card className="hero-panel" style={{ marginBottom: 20 }}>
        <div className="hero-grid">
          <div>
            <div className="hero-badge">TRANSLATION EVALUATION STUDIO</div>
            <Typography.Title level={2} style={{ margin: '10px 0 8px' }}>让每一次模型迭代都可复现、可解释</Typography.Title>
            <div className="muted-light">统一记录 33 个及更多小语种的语料版本、模型推理信息和逐句原始得分。阈值可随时调整，历史结果始终保留。</div>
            <Space style={{ marginTop: 22 }}>
              <Button type="primary" size="large" onClick={() => navigate('/submit')}>开始评测 <ArrowRightOutlined /></Button>
              <Button ghost size="large" onClick={() => navigate('/datasets')}>管理数据集</Button>
            </Space>
          </div>
          <div className="hero-orbit"><strong>33+</strong></div>
        </div>
      </Card>
      {!data ? <Skeleton active /> : (
        <Row gutter={[16, 16]}>
          {[
            { title: '数据集', value: data.datasets, icon: <DatabaseOutlined />, color: '#246bfd' },
            { title: '不可变版本', value: data.dataset_versions, icon: <ThunderboltOutlined />, color: '#6f56d9' },
            { title: '模型运行', value: data.model_runs, icon: <ExperimentOutlined />, color: '#10a779' },
            { title: '活动任务', value: data.active_tasks, icon: <CloudUploadOutlined />, color: '#f39b32' },
          ].map((item) => <Col span={6} key={item.title}><Card className="metric-card" style={{ '--metric-color': item.color } as React.CSSProperties}><Statistic title={<Space>{item.icon}{item.title}</Space>} value={item.value} /></Card></Col>)}
        </Row>
      )}
      <Row gutter={20} style={{ marginTop: 22 }}>
        <Col span={16}>
          <div className="section-title"><Typography.Title level={4}>最近评测</Typography.Title><Button type="link" onClick={() => navigate('/tasks')}>查看全部</Button></div>
          <Space direction="vertical" size={14} style={{ width: '100%' }}>
            {tasks.map((task) => <TaskCard key={task.id} task={task} onChange={load} />)}
            {!tasks.length && <Empty className="empty-soft" description="还没有评测任务" />}
          </Space>
        </Col>
        <Col span={8}>
          <div className="section-title"><Typography.Title level={4}>语料资产</Typography.Title><Button type="link" onClick={() => navigate('/datasets')}>版本管理</Button></div>
          <Card className="panel-card">
            {datasets.slice(0, 6).map((dataset, index) => (
              <div key={dataset.id} style={{ padding: '13px 0', borderBottom: index < Math.min(datasets.length, 6) - 1 ? '1px solid #edf0f5' : 0 }}>
                <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                  <div><Typography.Text strong>{dataset.name}</Typography.Text><div><Typography.Text type="secondary" style={{ fontSize: 12 }}>{dataset.key}</Typography.Text></div></div>
                  <div style={{ textAlign: 'right' }}><Typography.Text>{dataset.latest_version?.sample_count.toLocaleString() || 0}</Typography.Text><div><Typography.Text type="secondary" style={{ fontSize: 11 }}>{dataset.latest_version?.version_label || '—'}</Typography.Text></div></div>
                </Space>
              </div>
            ))}
            {!datasets.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据集" />}
          </Card>
        </Col>
      </Row>
    </>
  )
}

