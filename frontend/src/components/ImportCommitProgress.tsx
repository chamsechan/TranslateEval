import { Alert, App, Button, Card, Space, Spin, Table, Tag, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, formatDate, getApiBase } from '../api'
import { useApiQuery } from '../hooks/useApiQuery'
import QueryError from './QueryError'

export interface ImportCommitJob {
  id: string
  report_id: string
  kind: 'dataset' | 'submission'
  status: 'queued' | 'running' | 'completed' | 'failed'
  phase: 'queued' | 'validating' | 'writing' | 'completed' | 'failed'
  error: string | null
  result: { dataset_version_id?: string; content_sha256?: string; task_id?: string; submission_id?: string } | null
  created_at?: string
  started_at?: string | null
  finished_at?: string | null
}
const storageKey = (kind: ImportCommitJob['kind']) => `translateeval:${getApiBase()}:import-job:${kind}`
export function rememberedImportJob(kind: ImportCommitJob['kind']): string | null {
  try { return localStorage.getItem(storageKey(kind)) } catch { return null }
}
export function forgetImportJob(kind: ImportCommitJob['kind'], id: string) {
  try { if (rememberedImportJob(kind) === id) localStorage.removeItem(storageKey(kind)) } catch { /* Storage is optional; the URL remains shareable. */ }
}
export async function startImportCommit(reportId: string, body: Record<string, unknown> = {}): Promise<ImportCommitJob> {
  const job = await api<ImportCommitJob>(`/import-reports/${encodeURIComponent(reportId)}/commit-job`, { method: 'POST', body: JSON.stringify(body) })
  try { localStorage.setItem(storageKey(job.kind), job.id) } catch { /* The status URL still restores the job. */ }
  window.dispatchEvent(new Event('translateeval:import-jobs-changed'))
  return job
}
const labels = { queued: '等待处理', running: '后台处理中', completed: '已完成', failed: '失败' }
const phases = { queued: '等待开始', validating: '重新核验文件与版本', writing: '写入数据与建立任务', completed: '处理完成', failed: '处理失败' }
const running = (job: ImportCommitJob | null) => !!job && ['queued', 'running'].includes(job.status)

export function ImportCommitStatus({ jobId, onCompleted, onDismiss, onNavigate }: { jobId: string; onCompleted?: (job: ImportCommitJob) => void; onDismiss?: () => void; onNavigate?: () => void }) {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const query = useApiQuery<ImportCommitJob>(`/import-commit-jobs/${encodeURIComponent(jobId)}`)
  const job = query.data
  const open = (path: string) => { navigate(path); onNavigate?.() }
  const callback = useRef(onCompleted)
  callback.current = onCompleted
  const completed = useRef<string | null>(null)
  const [retrying, setRetrying] = useState(false)
  useEffect(() => {
    if (!job || running(job)) {
      const timer = window.setInterval(query.refresh, 1500)
      return () => window.clearInterval(timer)
    }
    if (job.status === 'completed' && completed.current !== job.id) {
      completed.current = job.id
      forgetImportJob(job.kind, job.id)
      callback.current?.(job)
    }
  }, [job, query.refresh])
  const retry = async () => {
    if (!job) return
    setRetrying(true)
    try { await startImportCommit(job.report_id); query.refresh(); message.success('已重新提交后台处理') }
    catch (error) { message.error(error instanceof Error ? error.message : '重新提交失败') }
    finally { setRetrying(false) }
  }
  return <Card size="small" className="import-commit-progress" style={{ marginBottom: 18 }} title="后台导入进度" extra={onDismiss && <Button size="small" onClick={onDismiss}>收起进度</Button>}>
    <QueryError error={query.error} retry={query.refresh} />
    {!job && query.loading && <Spin size="small" />}
    {job && <>
      <Space wrap><Tag color={job.status === 'failed' ? 'error' : job.status === 'completed' ? 'success' : 'processing'}>{labels[job.status]}</Tag><Typography.Text>{job.kind === 'dataset' ? '数据集导入' : '预测导入与评测提交'} · {phases[job.phase]}</Typography.Text><Typography.Text type="secondary">编号 {job.id.slice(0, 8)}</Typography.Text>{running(job) && <Spin size="small" />}</Space>
      <Typography.Paragraph type="secondary" style={{ marginTop: 10, marginBottom: 10 }}>{running(job) ? '可以离开或刷新页面，后台会继续处理；顶栏“导入进度”可恢复查看。' : `创建于 ${formatDate(job.created_at)}${job.finished_at ? ` · 结束于 ${formatDate(job.finished_at)}` : ''}`}</Typography.Paragraph>
      {job.error && <Alert type="error" showIcon message="导入未完成" description={<div className="result-error-text">{job.error}</div>} action={<Button loading={retrying} onClick={retry}>重试导入</Button>} />}
      {job.status === 'failed' && !job.error && <Button loading={retrying} onClick={retry}>重试导入</Button>}
      {job.status === 'completed' && <Space wrap>{job.result?.task_id && <Button type="primary" onClick={() => open(`/tasks?task=${encodeURIComponent(job.result!.task_id!)}`)}>查看评测任务</Button>}{job.result?.dataset_version_id && <Button onClick={() => open(`/datasets?version=${encodeURIComponent(job.result!.dataset_version_id!)}`)}>查看导入版本</Button>}</Space>}
    </>}
  </Card>
}

export default function ImportCommitHistory({ onNavigate }: { onNavigate?: () => void }) {
  const query = useApiQuery<ImportCommitJob[]>('/import-commit-jobs?limit=20')
  const [selected, setSelected] = useState<string | null>(null)
  useEffect(() => {
    const changed = () => query.refresh()
    window.addEventListener('translateeval:import-jobs-changed', changed)
    const timer = window.setInterval(query.refresh, 3000)
    return () => { window.removeEventListener('translateeval:import-jobs-changed', changed); window.clearInterval(timer) }
  }, [query.refresh])
  return <>
    <Typography.Paragraph type="secondary">最近 20 次后台导入。关闭页面不影响处理，失败项可使用原提交配置重试。</Typography.Paragraph>
    <QueryError error={query.error} retry={query.refresh} />
    {selected && <ImportCommitStatus key={selected} jobId={selected} onDismiss={() => setSelected(null)} onCompleted={query.refresh} onNavigate={onNavigate} />}
    <Table<ImportCommitJob> size="small" rowKey="id" loading={query.loading} dataSource={query.data || []} pagination={false} scroll={{ x: 650, y: '55vh' }} columns={[
      { title: '编号', width: 90, render: (_, job) => <Typography.Text title={job.id}>{job.id.slice(0, 8)}</Typography.Text> },
      { title: '类型', render: (_, job) => job.kind === 'dataset' ? '数据集' : '预测与评测' },
      { title: '状态', render: (_, job) => <Tag color={job.status === 'failed' ? 'error' : job.status === 'completed' ? 'success' : 'processing'}>{labels[job.status]}</Tag> },
      { title: '阶段', render: (_, job) => phases[job.phase] },
      { title: '提交时间', dataIndex: 'created_at', render: formatDate },
      { title: '操作', render: (_, job) => <Button size="small" type="link" onClick={() => setSelected(job.id)}>查看进度</Button> },
    ]} />
  </>
}
