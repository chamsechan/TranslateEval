import { ApiOutlined, CheckCircleOutlined, CloudUploadOutlined, FileSearchOutlined, FolderOpenOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Checkbox, Col, Descriptions, Form, Input, Row, Select, Skeleton, Space, Steps, Switch, Tag, Typography, Upload } from 'antd'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import PageHeader from '../components/PageHeader'
import QueryError from '../components/QueryError'
import { useApiQuery } from '../hooks/useApiQuery'
import type { EvaluatorProfile, ImportReport, PromptProfile } from '../types'

const { Dragger } = Upload
interface ExistingSubmission { id: string; summary: Record<string, string | number>; datasets: Array<{ dataset_key: string; version_label: string; prediction_count: number }> }
const summaryLabels: Record<string, string> = { run_name: '运行名称', model_family: '模型族', platform: '推理平台', dataset_count: '数据集数', prediction_count: '预测条数' }

export default function SubmitPage() {
  const [params] = useSearchParams()
  const submissionId = params.get('submission')
  return <SubmissionForm key={submissionId || 'new'} submissionId={submissionId} />
}

function SubmissionForm({ submissionId }: { submissionId: string | null }) {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [step, setStep] = useState(submissionId ? 1 : 0)
  const [path, setPath] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [report, setReport] = useState<ImportReport | null>(null)
  const existingQuery = useApiQuery<ExistingSubmission>(submissionId ? `/submissions/${submissionId}` : null)
  const existing = existingQuery.data
  const evaluatorQuery = useApiQuery<EvaluatorProfile[]>('/evaluator-profiles?enabled_only=true')
  const evaluators = evaluatorQuery.data || []
  const promptQuery = useApiQuery<PromptProfile[]>('/prompt-profiles')
  const prompts = promptQuery.data || []
  const initialized = useRef(false)
  const [selected, setSelected] = useState<string[]>([])
  const [promptByRevision, setPromptByRevision] = useState<Record<string, string>>({})
  const [force, setForce] = useState(false)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!initialized.current && evaluatorQuery.data) {
      const bleu = evaluatorQuery.data.find((item) => item.evaluator_type === 'sacrebleu_zh')?.revisions[0]
      if (bleu) setSelected([bleu.id])
      initialized.current = true
    }
  }, [evaluatorQuery.data])

  const publishedPrompts = useMemo(() => prompts.flatMap((profile) => profile.versions.filter((version) => version.published).map((version) => ({ value: version.id, label: `${profile.name} · v${version.version}` }))), [prompts])

  const validate = async () => {
    setLoading(true)
    try {
      let value: ImportReport
      if (file) { const body = new FormData(); body.append('file', file); value = await api('/submission-imports/validate-upload', { method: 'POST', body }) }
      else value = await api('/submission-imports/validate', { method: 'POST', body: JSON.stringify({ path }) })
      setReport(value)
      if (value.report.valid) { setStep(1); message.success('推理结果与数据集版本完全匹配') }
      else message.error(`核验失败：${value.report.errors.length} 项问题`)
    } catch (error) { message.error((error as Error).message) } finally { setLoading(false) }
  }

  const toggleEvaluator = (revisionId: string, checked: boolean, type: string) => {
    setSelected((current) => checked ? [...current, revisionId] : current.filter((id) => id !== revisionId))
    if (checked && type === 'openai_compatible_llm' && !promptByRevision[revisionId] && publishedPrompts[0]) setPromptByRevision((current) => ({ ...current, [revisionId]: publishedPrompts[0].value }))
  }

  const submit = async () => {
    if (!report && !existing) return
    if (!selected.length) return message.warning('至少选择一种评价方式')
    const selections = selected.map((id) => {
      const profile = evaluators.find((item) => item.revisions[0]?.id === id)
      return { evaluator_revision_id: id, prompt_version_id: profile?.evaluator_type === 'openai_compatible_llm' ? promptByRevision[id] : null }
    })
    if (selections.some((item) => item.prompt_version_id === undefined)) return message.warning('请为所有 LLM 评价器选择 Prompt 版本')
    setLoading(true)
    try {
      const endpoint = existing ? `/submissions/${existing.id}/evaluations` : `/submission-imports/${report!.id}/commit`
      const value = await api<{ task_id: string }>(endpoint, { method: 'POST', body: JSON.stringify({ evaluators: selections, force_reevaluate: force }) })
      message.success('评测任务已进入工作队列')
      navigate(`/tasks?task=${value.task_id}`)
    } catch (error) { message.error((error as Error).message) } finally { setLoading(false) }
  }

  return (
    <>
      <PageHeader title={submissionId ? '再次评测' : '提交评测'} subtitle={submissionId ? '复用已保存的预测，选择最新模型配置或其他 Prompt，创建独立评测任务。' : '核验标准结果目录，选择数据集版本与一个或多个评价器。'} />
      <QueryError error={existingQuery.error} retry={existingQuery.refresh} />
      {existingQuery.loading && !existing && <Skeleton active />}
      <QueryError error={evaluatorQuery.error || promptQuery.error} retry={() => { evaluatorQuery.refresh(); promptQuery.refresh() }} />
      <Card className="panel-card" style={{ marginBottom: 20 }}><Steps current={step} items={[{ title: '导入与核验', icon: <FileSearchOutlined /> }, { title: '选择评价方式', icon: <ApiOutlined /> }, { title: '进入评分队列', icon: <CheckCircleOutlined /> }]} /></Card>
      {step === 0 && <Card className="panel-card" title="1. 选择推理结果目录">
        <Alert type="info" showIcon message="统一结果协议" description="根目录包含 result_info.json；每个数据集子目录包含 predictions.jsonl。系统按 dataset_content_sha256 自动定位正确版本。" style={{ marginBottom: 20 }} />
        <Row gutter={20}>
          <Col span={12}>
            <Form layout="vertical"><Form.Item label="服务器目录或 ZIP 路径"><Input disabled={loading} size="large" prefix={<FolderOpenOutlined />} placeholder="/data/results/qwen3-exp-042" value={path} onChange={(event) => { setPath(event.target.value); setFile(null) }} /></Form.Item></Form>
          </Col>
          <Col span={12}>
            <Dragger disabled={loading} fileList={file ? [{ uid: 'result-zip', name: file.name, status: 'done' }] : []} accept=".zip" maxCount={1} beforeUpload={(value) => { setFile(value); setPath(''); return false }} onRemove={() => setFile(null)} style={{ height: 112 }}><p className="ant-upload-drag-icon" style={{ margin: 0 }}><CloudUploadOutlined /></p><p style={{ margin: 3 }}>或拖入结果 ZIP</p></Dragger>
          </Col>
        </Row>
        <Button type="primary" size="large" block loading={loading} disabled={!path && !file} onClick={validate}>核验模型信息、数据集版本和预测 ID</Button>
        {report && !report.report.valid && <Alert type="error" showIcon message="未通过核验，数据库没有写入" description={<div className="code-template" style={{ marginTop: 10 }}>{report.report.errors.map((item) => JSON.stringify(item, null, 2)).join('\n')}</div>} style={{ marginTop: 20 }} />}
      </Card>}

      {step === 1 && (report || existing) && <Space direction="vertical" size={18} style={{ width: '100%' }}>
        <Card className="panel-card" title={<Space><SafetyCertificateOutlined style={{ color: '#10a779' }} />{existing ? '已保存的推理结果' : '核验摘要'}</Space>} extra={<Button disabled={loading} type="link" onClick={() => { if (submissionId) navigate('/submit'); else { setStep(0); setReport(null) } }}>{existing ? '导入其他结果' : '更换目录'}</Button>}>
          <Descriptions bordered column={{ xs: 1, sm: 2, xl: 4 }} size="small" items={Object.entries(existing?.summary || report?.report.summary || {}).map(([key, value]) => ({ key, label: summaryLabels[key] || key, children: String(value) }))} />
          <Row gutter={12} style={{ marginTop: 16 }}>{(existing?.datasets || report?.report.datasets)?.map((item) => <Col xs={24} md={8} key={String(item.dataset_key)}><Card size="small"><Space style={{ width: '100%', justifyContent: 'space-between' }}><Typography.Text strong>{String(item.dataset_key)}</Typography.Text><Tag color="success">匹配</Tag></Space><div style={{ marginTop: 8 }}><Typography.Text type="secondary">版本 {String(item.version_label)} · {String(item.prediction_count)} 条</Typography.Text></div></Card></Col>)}</Row>
        </Card>
        <Card className="panel-card" title="2. 选择评价方式" extra={<Typography.Text type="secondary">可多选，评价器依次执行</Typography.Text>}>
          <Row gutter={[14, 14]}>
            {evaluators.map((profile) => {
              const revision = profile.revisions[0]; if (!revision) return null
              const checked = selected.includes(revision.id)
              return <Col span={12} key={profile.id}><Card size="small" style={{ borderColor: checked ? '#7da8ff' : undefined, background: checked ? '#f7faff' : undefined }}>
                <Space align="start" style={{ width: '100%', justifyContent: 'space-between' }}><Checkbox checked={checked} onChange={(event) => toggleEvaluator(revision.id, event.target.checked, profile.evaluator_type)}><Typography.Text strong>{profile.name}</Typography.Text></Checkbox><Tag color={profile.evaluator_type === 'sacrebleu_zh' ? 'purple' : 'blue'}>{profile.evaluator_type === 'sacrebleu_zh' ? 'BLEU' : 'LLM'}</Tag></Space>
                <div style={{ margin: '8px 0 0 24px', color: '#718096', fontSize: 12 }}>配置修订 r{revision.revision} · 默认阈值 {revision.default_threshold}</div>
                {checked && profile.evaluator_type === 'openai_compatible_llm' && <div style={{ margin: '12px 0 0 24px' }}><Select style={{ width: '100%' }} placeholder="选择 Prompt 版本" options={publishedPrompts} value={promptByRevision[revision.id]} onChange={(value) => setPromptByRevision((current) => ({ ...current, [revision.id]: value }))} /></div>}
              </Card></Col>
            })}
          </Row>
          {!evaluators.some((item) => item.evaluator_type === 'openai_compatible_llm') && <Alert type="warning" showIcon message="尚未配置 OpenAI 兼容评价器" description="当前可以运行 BLEU；在评价设置页面添加 Base URL、模型名和 API Key 后即可多选 LLM 评分。" style={{ marginTop: 16 }} />}
          <Card size="small" style={{ marginTop: 18, background: '#fafbfd' }}><Space style={{ width: '100%', justifyContent: 'space-between' }}><div><Typography.Text strong>强制重新评分</Typography.Text><div><Typography.Text type="secondary">关闭时按“语句对 + 评分模型名”复用最近成功评分，忽略 Prompt 和服务地址的变化。验证新 Prompt 的效果时请开启。</Typography.Text></div></div><Switch checked={force} onChange={setForce} /></Space></Card>
          <Button type="primary" size="large" block loading={loading} onClick={submit} style={{ marginTop: 18 }}>提交并进入工作队列</Button>
        </Card>
      </Space>}
    </>
  )
}
