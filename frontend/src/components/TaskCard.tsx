import { CloseOutlined, DatabaseOutlined, ExperimentOutlined } from '@ant-design/icons'
import { App, Button, Card, Progress, Space, Tooltip, Typography } from 'antd'
import { useNavigate } from 'react-router-dom'
import { api, formatDate } from '../api'
import type { EvaluationTask } from '../types'
import StatusTag from './StatusTag'

const active = new Set(['queued', 'preprocessing', 'running', 'cancelling'])

export default function TaskCard({ task, onChange }: { task: EvaluationTask; onChange?: () => void }) {
  const { modal, message } = App.useApp()
  const navigate = useNavigate()
  const progress = task.total_items ? Math.round(((task.completed_items + task.failed_items + task.cancelled_items) / task.total_items) * 100) : 0

  const cancelTask = () => modal.confirm({
    title: '取消整个评测任务？',
    content: '未开始的评分项会被取消，已经发出的 LLM 请求可能仍会进入全局缓存。',
    okText: '确认取消',
    okButtonProps: { danger: true },
    cancelText: '继续评分',
    onOk: async () => {
      await api(`/tasks/${task.id}/cancel`, { method: 'POST' })
      message.success('已提交取消请求')
      onChange?.()
    },
  })

  const cancelDataset = async (jobId: string) => {
    await api(`/dataset-jobs/${jobId}/cancel`, { method: 'POST' })
    message.success('该数据集正在取消')
    onChange?.()
  }

  const retryEvaluator = async (jobId: string) => {
    try {
      await api(`/evaluator-jobs/${jobId}/retry-failed`, { method: 'POST' })
      message.success('失败项已重新进入队列')
      onChange?.()
    } catch (error) {
      message.error((error as Error).message)
    }
  }

  return (
    <Card
      className={`task-card ${task.status}`}
      title={<Space><Typography.Text strong>{task.run_name}</Typography.Text><StatusTag status={task.status} /></Space>}
      extra={active.has(task.status) && <Tooltip title="取消任务"><Button danger type="text" icon={<CloseOutlined />} onClick={cancelTask} /></Tooltip>}
    >
      <div className="task-meta">
        <span><ExperimentOutlined /> {task.model_family}</span>
        <span>平台 {task.platform}</span>
        <span>创建 {formatDate(task.created_at)}</span>
        <span>缓存 {task.cached_items.toLocaleString()}</span>
        <span>失败 {task.failed_items.toLocaleString()}</span>
      </div>
      <Progress percent={progress} status={['failed', 'partial_failed'].includes(task.status) ? 'exception' : task.status === 'completed' ? 'success' : active.has(task.status) ? 'active' : 'normal'} style={{ marginTop: 17 }} />
      {task.dataset_jobs.map((job) => (
        <div className="job-row" key={job.id}>
          <Space style={{ width: '100%', justifyContent: 'space-between' }}>
            <Space><DatabaseOutlined /><Typography.Text strong>{job.dataset_key}</Typography.Text><Typography.Text type="secondary">{job.version_label}</Typography.Text><StatusTag status={job.status} /></Space>
            <Space>
              <Typography.Text type="secondary">{job.completed_items}/{job.total_items}</Typography.Text>
              {active.has(job.status) && <Button size="small" danger type="text" onClick={() => cancelDataset(job.id)}>取消数据集</Button>}
            </Space>
          </Space>
          {job.evaluator_jobs.map((evaluator) => (
            <div className="evaluator-chip" key={evaluator.id}>
              <Space><span>{evaluator.name} · r{evaluator.revision}</span><StatusTag status={evaluator.status} /><span>命中缓存 {evaluator.cached_items}</span></Space>
              <Space>
                <span>{evaluator.completed_items}/{evaluator.total_items}</span>
                {evaluator.failed_items > 0 && <Button size="small" onClick={() => retryEvaluator(evaluator.id)}>重试失败项</Button>}
                {evaluator.completed_items > 0 && <Button size="small" type="link" onClick={() => navigate(`/results/${evaluator.id}`)}>查看结果</Button>}
              </Space>
              {evaluator.error && <Typography.Text type="danger" ellipsis={{ tooltip: evaluator.error }} className="evaluator-error">{evaluator.error}</Typography.Text>}
            </div>
          ))}
        </div>
      ))}
    </Card>
  )
}
