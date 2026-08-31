import { ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Empty, Segmented, Space } from 'antd'
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import PageHeader from '../components/PageHeader'
import TaskCard from '../components/TaskCard'
import type { EvaluationTask } from '../types'

export default function TasksPage() {
  const [tasks, setTasks] = useState<EvaluationTask[]>([])
  const [filter, setFilter] = useState('活动')
  const [connected, setConnected] = useState(false)
  const load = async () => setTasks(await api<EvaluationTask[]>('/tasks?limit=100'))

  useEffect(() => {
    void load()
    const source = new EventSource('/api/tasks/events')
    source.addEventListener('tasks', (event) => { setTasks(JSON.parse((event as MessageEvent).data)); setConnected(true) })
    source.onerror = () => setConnected(false)
    return () => source.close()
  }, [])
  const filtered = useMemo(() => tasks.filter((task) => {
    if (filter === '活动') return ['queued', 'preprocessing', 'running', 'cancelling'].includes(task.status)
    if (filter === '已完成') return task.status === 'completed'
    if (filter === '异常') return ['failed', 'partial_failed', 'partial_cancelled', 'cancelled'].includes(task.status)
    return true
  }), [tasks, filter])

  return (
    <>
      <PageHeader title="任务队列" subtitle="任务持久化在 SQLite 中，关闭页面或重启 Worker 都不会丢失进度。" actions={<Space><Segmented options={['活动', '全部', '已完成', '异常']} value={filter} onChange={setFilter} /><Button icon={<ReloadOutlined />} onClick={load}>刷新</Button></Space>} />
      <Alert type={connected ? 'success' : 'warning'} showIcon message={connected ? '实时进度已连接' : '实时连接暂时中断，仍可手动刷新'} style={{ marginBottom: 16 }} />
      <Space direction="vertical" size={15} style={{ width: '100%' }}>
        {filtered.map((task) => <TaskCard key={task.id} task={task} onChange={load} />)}
        {!filtered.length && <Empty className="empty-soft" description="当前筛选下没有任务" />}
      </Space>
    </>
  )
}
