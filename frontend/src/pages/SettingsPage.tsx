import { ApiOutlined, CopyOutlined, KeyOutlined, PlusOutlined, ReloadOutlined, SafetyOutlined } from '@ant-design/icons'
import { Alert, App, Badge, Button, Card, Col, Collapse, Form, Input, InputNumber, Modal, Row, Select, Skeleton, Space, Switch, Tabs, Tag, Tooltip, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { api, formatDate } from '../api'
import PageHeader from '../components/PageHeader'
import QueryError from '../components/QueryError'
import ImportOptionsPanel from '../components/ImportOptionsPanel'
import { useApiQuery } from '../hooks/useApiQuery'
import type { EvaluatorConnection, EvaluatorProfile, PromptProfile } from '../types'

type EvaluatorForm = { name: string; evaluator_type: 'openai_compatible_llm' | 'sacrebleu_zh'; base_url?: string; model?: string; api_key?: string; concurrency?: number; timeout_seconds?: number; max_retries?: number; default_threshold: number; tokenize?: string; smooth_method?: string }
type PromptForm = { name?: string; description?: string; system_template: string; user_template: string; published: boolean }
type ConnectionState = EvaluatorConnection | { status: 'checking'; detail: string; latency_ms: null; model_available: null }

export default function SettingsPage() {
  const { message } = App.useApp()
  const evaluatorQuery = useApiQuery<EvaluatorProfile[]>('/evaluator-profiles')
  const evaluators = evaluatorQuery.data || []
  const [connections, setConnections] = useState<Record<string, ConnectionState>>({})
  const promptQuery = useApiQuery<PromptProfile[]>('/prompt-profiles')
  const prompts = promptQuery.data || []
  const [savingEvaluator, setSavingEvaluator] = useState(false)
  const [savingPrompt, setSavingPrompt] = useState(false)
  const [evaluatorOpen, setEvaluatorOpen] = useState(false)
  const [revisionProfile, setRevisionProfile] = useState<EvaluatorProfile | null>(null)
  const [promptOpen, setPromptOpen] = useState(false)
  const [versionProfile, setVersionProfile] = useState<PromptProfile | null>(null)
  const [evaluatorForm] = Form.useForm<EvaluatorForm>()
  const [promptForm] = Form.useForm<PromptForm>()
  const evaluatorType = Form.useWatch('evaluator_type', evaluatorForm)

  const checkConnection = async (profile: EvaluatorProfile) => {
    setConnections((current) => ({ ...current, [profile.id]: { status: 'checking', detail: '正在检查最新修订……', latency_ms: null, model_available: null } }))
    try {
      const result = await api<EvaluatorConnection>(`/evaluator-profiles/${profile.id}/connection-test`, { method: 'POST' })
      setConnections((current) => ({ ...current, [profile.id]: result }))
    } catch (error) {
      setConnections((current) => ({ ...current, [profile.id]: { status: 'disconnected', detail: (error as Error).message, latency_ms: null, model_available: null } }))
    }
  }
  const load = () => { evaluatorQuery.refresh(); promptQuery.refresh() }
  useEffect(() => {
    if (evaluatorQuery.data) void Promise.allSettled(evaluatorQuery.data.map(checkConnection))
  }, [evaluatorQuery.data])

  const openEvaluator = () => {
    setRevisionProfile(null)
    evaluatorForm.resetFields()
    evaluatorForm.setFieldsValue({ evaluator_type: 'openai_compatible_llm', concurrency: 8, timeout_seconds: 60, max_retries: 3, default_threshold: 8 })
    setEvaluatorOpen(true)
  }
  const openEvaluatorRevision = (profile: EvaluatorProfile) => {
    const latest = profile.revisions[0]
    const config = latest.config
    setRevisionProfile(profile)
    evaluatorForm.resetFields()
    evaluatorForm.setFieldsValue({
      evaluator_type: profile.evaluator_type,
      base_url: String(config.base_url || ''), model: String(config.model || ''), api_key: '',
      concurrency: Number(config.concurrency ?? 8), timeout_seconds: Number(config.timeout_seconds ?? 60), max_retries: Number(config.max_retries ?? 3),
      tokenize: String(config.tokenize || 'zh'), smooth_method: String(config.smooth_method || 'exp'), default_threshold: latest.default_threshold,
    })
    setEvaluatorOpen(true)
  }
  const saveEvaluator = async () => {
    try {
      const value = await evaluatorForm.validateFields()
      setSavingEvaluator(true)
      const original = revisionProfile?.revisions[0].config || {}
      const config = value.evaluator_type === 'openai_compatible_llm'
        ? { ...original, base_url: value.base_url, model: value.model, api_key: value.api_key, concurrency: value.concurrency, timeout_seconds: value.timeout_seconds, max_retries: value.max_retries, temperature: original.temperature ?? 0, max_tokens: original.max_tokens ?? 256 }
        : { ...original, tokenize: value.tokenize || 'zh', smooth_method: value.smooth_method || 'exp', effective_order: original.effective_order ?? true }
      if (revisionProfile) await api(`/evaluator-profiles/${revisionProfile.id}/revisions`, { method: 'POST', body: JSON.stringify({ config, default_threshold: value.default_threshold }) })
      else await api('/evaluator-profiles', { method: 'POST', body: JSON.stringify({ name: value.name, evaluator_type: value.evaluator_type, config, default_threshold: value.default_threshold, enabled: true }) })
      message.success(revisionProfile ? '模型服务配置已更新' : '评价器配置已创建'); setEvaluatorOpen(false); setRevisionProfile(null); evaluatorForm.resetFields(); await load()
    } catch (error) { if (error instanceof Error) message.error(error.message) } finally { setSavingEvaluator(false) }
  }
  const setEnabled = async (profile: EvaluatorProfile, enabled: boolean) => {
    try { await api(`/evaluator-profiles/${profile.id}`, { method: 'PATCH', body: JSON.stringify({ enabled }) }); await load() }
    catch (error) { message.error((error as Error).message) }
  }
  const openNewPrompt = () => { setVersionProfile(null); promptForm.resetFields(); promptForm.setFieldsValue({ published: true }); setPromptOpen(true) }
  const openPromptVersion = (profile: PromptProfile, version = profile.versions[0]) => {
    setVersionProfile(profile)
    promptForm.resetFields()
    promptForm.setFieldsValue({ system_template: version.system_template, user_template: version.user_template, published: true })
    setPromptOpen(true)
  }
  const savePrompt = async () => {
    try {
      const value = await promptForm.validateFields()
      setSavingPrompt(true)
      if (versionProfile) await api(`/prompt-profiles/${versionProfile.id}/versions`, { method: 'POST', body: JSON.stringify(value) })
      else await api('/prompt-profiles', { method: 'POST', body: JSON.stringify(value) })
      message.success(versionProfile ? '新 Prompt 版本已创建' : 'Prompt 配置已创建'); setPromptOpen(false); promptForm.resetFields(); await load()
    } catch (error) { if (error instanceof Error) message.error(error.message) } finally { setSavingPrompt(false) }
  }

  return (
    <>
      <PageHeader title="模型与评价设置" subtitle="维护导入下拉选项、评分模型服务和 Prompt 版本。" />
      <QueryError error={evaluatorQuery.error || promptQuery.error} retry={load} />
      {(evaluatorQuery.loading || promptQuery.loading) && !evaluatorQuery.data && <Skeleton active />}
      <Tabs items={[
        { key: 'import-options', label: '导入下拉选项', children: <ImportOptionsPanel /> },
        { key: 'evaluators', label: <Space><ApiOutlined />模型服务 / 评价器</Space>, children: <>
          <Alert className="settings-warning" type="warning" showIcon icon={<KeyOutlined />} message="API Key 按已确认方案明文保存在本机 SQLite" description="接口不会回传原值，页面只显示掩码，应用日志禁止打印密钥。备份数据库时请按敏感文件处理。" />
          <Alert style={{ marginTop: 12 }} type="info" showIcon message="编辑会生成新修订" description="Base URL、模型名或 API Key 变更后，新任务使用最新修订，已有任务仍保留原配置快照。" />
          <div className="section-title" style={{ marginTop: 20 }}><Typography.Title level={4}>评分模型与评价方式</Typography.Title><Button type="primary" icon={<PlusOutlined />} onClick={openEvaluator}>新增配置</Button></div>
          <Row gutter={[16, 16]}>{evaluators.map((profile) => {
            const latest = profile.revisions[0]
            const connection = connections[profile.id]
            const badgeStatus = connection?.status === 'connected' ? 'success' : connection?.status === 'disconnected' ? 'error' : connection?.status === 'checking' ? 'processing' : 'default'
            const connectionText = connection?.status === 'connected' ? '已联通' : connection?.status === 'disconnected' ? '未联通' : connection?.status === 'checking' ? '检测中' : '未检测'
            return <Col xs={24} xl={12} key={profile.id}><Card className="panel-card model-service-card" title={<Space><SafetyOutlined />{profile.name}</Space>} extra={<Space><Button size="small" onClick={() => openEvaluatorRevision(profile)}>编辑配置</Button><Tooltip title="重新检测联通状态"><Button size="small" icon={<ReloadOutlined />} loading={connection?.status === 'checking'} onClick={() => void checkConnection(profile)} /></Tooltip><Switch size="small" checked={profile.enabled} onChange={(checked) => setEnabled(profile, checked)} /></Space>}>
              <Space wrap><Tag color={profile.evaluator_type === 'sacrebleu_zh' ? 'purple' : 'blue'}>{profile.evaluator_type}</Tag><span>最新修订 r{latest?.revision}</span><span>默认阈值 {latest?.default_threshold}</span></Space>
              <Tooltip title={connection?.detail || '尚未执行检测'}><div className={`model-connection ${connection?.status || 'unchecked'}`}><Badge status={badgeStatus} text={connectionText} /><Typography.Text type="secondary">{connection?.detail || '等待检测'}{connection?.latency_ms != null ? ` · ${connection.latency_ms} ms` : ''}</Typography.Text></div></Tooltip>
              {latest && <div className="code-template" style={{ marginTop: 14 }}>{JSON.stringify(latest.config, null, 2)}</div>}
              <Typography.Text type="secondary" style={{ display: 'block', marginTop: 10 }}>历史修订 {profile.revisions.length} 个 · {formatDate(latest?.created_at)}</Typography.Text>
            </Card></Col>
          })}</Row>
        </> },
        { key: 'prompts', label: <Space><CopyOutlined />Prompt 版本</Space>, children: <>
          <div className="section-title"><Typography.Title level={4}>Prompt 配置</Typography.Title><Button type="primary" icon={<PlusOutlined />} onClick={openNewPrompt}>新建 Prompt</Button></div>
          <Collapse items={prompts.map((profile) => ({ key: profile.id, label: <Space><Typography.Text strong>{profile.name}</Typography.Text><Tag>{profile.versions.length} 版本</Tag></Space>, extra: <Button size="small" onClick={(event) => { event.stopPropagation(); openPromptVersion(profile) }}>复制最新版本</Button>, children: <div>{profile.versions.map((version) => <Card key={version.id} size="small" title={<Space>v{version.version}<Tag color={version.published ? 'success' : 'default'}>{version.published ? '已发布' : '草稿'}</Tag></Space>} extra={<Button size="small" onClick={() => openPromptVersion(profile, version)}>复制此版本</Button>} style={{ marginBottom: 12 }}><Typography.Text type="secondary">System</Typography.Text><div className="code-template">{version.system_template}</div><Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>User</Typography.Text><div className="code-template">{version.user_template}</div><Typography.Text type="secondary">创建于 {formatDate(version.created_at)} · ID {version.id}</Typography.Text></Card>)}</div> }))} />
        </> },
      ]} />

      <Modal title={revisionProfile ? `编辑 ${revisionProfile.name}` : '新增评价器配置'} width={650} confirmLoading={savingEvaluator} open={evaluatorOpen} onCancel={() => { setEvaluatorOpen(false); setRevisionProfile(null) }} onOk={saveEvaluator} okText={revisionProfile ? '保存为新修订' : '创建配置'}>
        <Form form={evaluatorForm} layout="vertical">
          <Row gutter={14}>{!revisionProfile && <Col span={14}><Form.Item name="name" label="配置名称" rules={[{ required: true }]}><Input placeholder="例如 Qwen Judge · 本地服务" /></Form.Item></Col>}<Col span={revisionProfile ? 24 : 10}><Form.Item name="evaluator_type" label="评价器类型" rules={[{ required: true }]}><Select disabled={!!revisionProfile} options={[{ label: 'OpenAI 兼容 LLM', value: 'openai_compatible_llm' }, { label: 'SacreBLEU 中文', value: 'sacrebleu_zh' }]} /></Form.Item></Col></Row>
          {evaluatorType === 'openai_compatible_llm' ? <>
            <Form.Item name="base_url" label="Base URL" rules={[{ required: true }]}><Input placeholder="https://example.com/v1/" /></Form.Item>
            <Row gutter={14}><Col span={12}><Form.Item name="model" label="评分模型名" rules={[{ required: true }]}><Input placeholder="qwen3-judge" /></Form.Item></Col><Col span={12}><Form.Item name="api_key" label={revisionProfile ? 'API Key（留空沿用）' : 'API Key'} rules={revisionProfile ? [] : [{ required: true }]}><Input.Password /></Form.Item></Col></Row>
            <Row gutter={14}><Col span={8}><Form.Item name="concurrency" label="并发数"><InputNumber min={1} max={64} style={{ width: '100%' }} /></Form.Item></Col><Col span={8}><Form.Item name="timeout_seconds" label="超时（秒）"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item></Col><Col span={8}><Form.Item name="max_retries" label="最大重试"><InputNumber min={0} max={10} style={{ width: '100%' }} /></Form.Item></Col></Row>
          </> : <Row gutter={14}><Col span={12}><Form.Item name="tokenize" label="Tokenizer" initialValue="zh"><Input /></Form.Item></Col><Col span={12}><Form.Item name="smooth_method" label="平滑方式" initialValue="exp"><Select options={['exp', 'floor', 'add-k', 'none'].map((value) => ({ value, label: value }))} /></Form.Item></Col></Row>}
          <Form.Item name="default_threshold" label="结果页默认准确阈值" rules={[{ required: true }]}><InputNumber min={0} max={evaluatorType === 'sacrebleu_zh' ? 100 : 10} step={0.1} style={{ width: '100%' }} /></Form.Item>
        </Form>
      </Modal>

      <Modal title={versionProfile ? `创建 ${versionProfile.name} 的新版本` : '新建 Prompt 配置'} width={760} confirmLoading={savingPrompt} open={promptOpen} onCancel={() => setPromptOpen(false)} onOk={savePrompt} okText="保存不可变版本">
        <Form form={promptForm} layout="vertical">
          {!versionProfile && <Row gutter={14}><Col span={12}><Form.Item name="name" label="Prompt 名称" rules={[{ required: true }]}><Input /></Form.Item></Col><Col span={12}><Form.Item name="description" label="说明"><Input /></Form.Item></Col></Row>}
          <Form.Item name="system_template" label="System Prompt" rules={[{ required: true }]}><Input.TextArea rows={5} /></Form.Item>
          <Form.Item name="user_template" label="User Prompt" extra={'可用变量：{source_language}、{source_text}、{reference_zh}、{translation_zh}。JSON 示例中的花括号请写成双花括号，如 {{"score": 8}}。'} rules={[{ required: true }]}><Input.TextArea rows={9} /></Form.Item>
          <Form.Item name="published" label="状态"><Select options={[{ label: '发布，可用于新任务', value: true }, { label: '草稿', value: false }]} /></Form.Item>
        </Form>
      </Modal>
    </>
  )
}
