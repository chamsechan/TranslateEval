import { ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Empty, Input, Pagination, Segmented, Space, Spin } from 'antd'
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import PageHeader from '../components/PageHeader'
import QueryError from '../components/QueryError'
import TaskCard from '../components/TaskCard'
import { useApiQuery } from '../hooks/useApiQuery'
import { useTaskChanges } from '../hooks/useTaskChanges'
import type { EvaluationTask, PageResponse } from '../types'

const groups: Record<string, string> = { 活动: 'active', 全部: 'all', 已完成: 'completed', 异常: 'exception' }

export default function TasksPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const focusedTask = searchParams.get('task')
  const [filter, setFilter] = useState(focusedTask ? '全部' : '活动')
  const [page, setPage] = useState(1)
  const [query, setQuery] = useState('')
  const params = new URLSearchParams({ page: String(page), page_size: '20', group: focusedTask ? 'all' : groups[filter], q: query })
  if (focusedTask) params.set('task_id', focusedTask)
  const { data, error, loading, refresh } = useApiQuery<PageResponse<EvaluationTask>>(`/tasks/page?${params}`)
  const connected = useTaskChanges(refresh)

  return <>
    <PageHeader title="任务队列" subtitle="查看全部历史或活动任务，关闭页面后任务会继续执行。" actions={<Space wrap><Segmented options={Object.keys(groups)} value={filter} onChange={(value) => { setFilter(value); setPage(1); setSearchParams({}) }} /><Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button></Space>} />
    {focusedTask && <Alert type="info" showIcon title="正在显示本次提交的任务" action={<Button onClick={() => { setSearchParams({}); setPage(1) }}>查看全部任务</Button>} style={{ marginBottom: 16 }} />}
    <Input.Search allowClear placeholder="搜索运行、模型或平台，按回车查询" onSearch={(value) => { setQuery(value); setPage(1) }} style={{ maxWidth: 420, marginBottom: 16 }} />
    <Alert type={connected ? 'success' : 'info'} showIcon title={connected ? '实时进度已连接' : '正在连接实时进度，断线期间每 10 秒自动刷新'} style={{ marginBottom: 16 }} />
    <QueryError error={error} retry={refresh} />
    <Spin spinning={loading}>
      <Space direction="vertical" size={15} style={{ width: '100%', minHeight: 100 }}>
        {data?.items.map((task) => <TaskCard key={task.id} task={task} onChange={refresh} />)}
        {!loading && !error && !data?.items.length && <Empty className="empty-soft" description="当前筛选下没有任务" />}
      </Space>
    </Spin>
    <Pagination current={page} pageSize={20} total={data?.total || 0} showSizeChanger={false} showTotal={(total) => `共 ${total} 个任务`} onChange={setPage} style={{ marginTop: 20 }} />
  </>
}
