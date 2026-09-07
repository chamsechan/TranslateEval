import { Alert, App, Button, Card, Col, Form, Input, Modal, Row, Select, Space, Typography } from 'antd'
import { useState } from 'react'
import { api } from '../api'
import { useApiQuery } from '../hooks/useApiQuery'
import type { Dataset, DatasetVersion, ImportOption, ImportOptionCategory, ImportReport } from '../types'
import ImportOptionsPanel, { optionLabels } from './ImportOptionsPanel'
import QueryError from './QueryError'

function VersionFields({ index, datasets, root }: { index: number; datasets: Dataset[]; root: boolean }) {
  const form = Form.useFormInstance()
  const key = Form.useWatch(['datasets', index, 'dataset_key'], form)
  const hash = Form.useWatch(['datasets', index, 'dataset_content_sha256'], form)
  const dataset = datasets.find((item) => item.key === key)
  const query = useApiQuery<DatasetVersion[]>(dataset ? `/datasets/${dataset.id}/versions` : null)
  const versions = query.data || []
  return <Card size="small" style={{ marginBottom: 12 }}>
    <Row gutter={16}>
      <Col xs={24} md={10}><Form.Item name={[index, 'dataset_key']} label="数据集" rules={[{ required: true }]}>
        <Select showSearch optionFilterProp="label" disabled={!root} options={[
          ...datasets.map((item) => ({ value: item.key, label: `${item.name} · ${item.key}` })),
          ...(key && !dataset ? [{ value: key, label: `${key}（尚未导入）` }] : []),
        ]} placeholder="选择预测对应的数据集" onChange={() => form.setFieldValue(['datasets', index, 'dataset_content_sha256'], undefined)} />
      </Form.Item></Col>
      <Col xs={24} md={14}><Form.Item name={[index, 'dataset_content_sha256']} label="数据集版本" rules={[{ required: true, message: '请选择数据集版本' }, { validator: async (_, value) => { if (value && query.data && !versions.some((item) => item.content_sha256 === value)) throw new Error('哈希未匹配，请选择已导入的版本') } }]}>
        <Select showSearch optionFilterProp="label" loading={query.loading} disabled={!dataset} placeholder="请选择版本，自动填写内容哈希" options={[
          ...versions.map((item) => ({ value: item.content_sha256, label: `${item.version_label} · ${item.sample_count} 条 · ${item.content_sha256.slice(0, 10)}` })),
          ...(hash && !versions.some((item) => item.content_sha256 === hash) ? [{ value: hash, label: `未匹配哈希：${hash.slice(0, 12)}…`, disabled: true }] : []),
        ]} />
      </Form.Item></Col>
    </Row>
    <QueryError error={query.error} retry={query.refresh} />
    {key && !dataset && <Alert type="warning" message="请先在数据集页面导入对应语料，再刷新数据集列表。" />}
    {hash && <Typography.Paragraph type="secondary" style={{ overflowWrap: 'anywhere', marginBottom: 0 }}>内容哈希：{hash}</Typography.Paragraph>}
  </Card>
}

export default function SubmissionImportEditor({ draft, onValidated, onBack }: {
  draft: ImportReport; onValidated: (report: ImportReport) => void; onBack: () => void
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const optionsQuery = useApiQuery<ImportOption[]>('/import-options')
  const datasetQuery = useApiQuery<Dataset[]>('/datasets')
  const [manage, setManage] = useState(false)
  const [loading, setLoading] = useState(false)
  const inference = draft.manifest.inference && typeof draft.manifest.inference === 'object' ? draft.manifest.inference as Record<string, unknown> : {}
  const initialValues = { ...draft.manifest, inference, decoding_text: JSON.stringify(inference.decoding || {}, null, 2) }
  const currentValues = Form.useWatch([], form)
  const dropdown = (category: ImportOptionCategory, name: string | string[], required = false) => {
    const current = Array.isArray(name) ? currentValues?.[name[0]]?.[name[1]] : currentValues?.[name]
    const options = (optionsQuery.data || []).filter((item) => item.category === category)
    return <Form.Item name={name} label={optionLabels[category]} rules={required ? [{ required: true, message: `请选择${optionLabels[category]}` }] : []}>
      <Select showSearch allowClear={!required} optionFilterProp="label" loading={optionsQuery.loading} placeholder={`选择${optionLabels[category]}`} options={[
        ...options.map((item) => ({ value: item.value, label: `${item.label}${!item.enabled ? '（已停用）' : ''}`, disabled: !item.enabled && item.value !== current })),
        ...(typeof current === 'string' && current && !options.some((item) => item.value === current) ? [{ value: current, label: `${current}（导入值）` }] : []),
      ]} />
    </Form.Item>
  }
  const validate = async () => {
    try {
      const values = await form.validateFields()
      const { decoding_text, ...manifest } = values
      const decoding = JSON.parse(decoding_text || '{}')
      if (!decoding || typeof decoding !== 'object' || Array.isArray(decoding)) throw new Error('解码参数必须是 JSON 对象')
      setLoading(true)
      const value = await api<ImportReport>(`/import-reports/${draft.id}/validate`, { method: 'POST', body: JSON.stringify({ manifest: {
        ...draft.manifest, ...manifest, inference: { ...inference, ...manifest.inference, device: manifest.inference.device || '', precision: manifest.inference.precision || '', generated_at: manifest.inference.generated_at || null, decoding },
      } }) })
      onValidated(value)
    } catch (error) { if (error instanceof Error) message.error(error.message) } finally { setLoading(false) }
  }
  return <>
    <Alert type="info" showIcon message="确认模型与推理信息" description={`${draft.report.has_manifest ? 'JSON 中的信息已预填，可以修改。' : '请填写运行名称和检查点，并选择模型与推理信息。'}下拉框的新导入值将在成功提交后加入选项库。`} style={{ marginBottom: 18 }} />
    <Space wrap style={{ marginBottom: 18 }}><Button onClick={() => setManage(true)}>维护下拉选项</Button><Button onClick={() => { optionsQuery.refresh(); datasetQuery.refresh() }}>刷新选项与数据集</Button></Space>
    <QueryError error={optionsQuery.error || datasetQuery.error} retry={() => { optionsQuery.refresh(); datasetQuery.refresh() }} />
    {draft.status === 'draft' && !!draft.report.errors.length && <Alert type="warning" showIcon message="读取提示" description={draft.report.errors.map((item) => String(item.message)).join('\n')} style={{ marginBottom: 18 }} />}
    <Form form={form} layout="vertical" initialValues={initialValues} disabled={loading}>
      <Row gutter={16}>
        <Col xs={24} md={12}><Form.Item name="run_name" label="运行名称" rules={[{ required: true, whitespace: true }]}><Input maxLength={200} /></Form.Item></Col>
        <Col xs={24} md={12}>{dropdown('model', 'model_family', true)}</Col>
        <Col xs={24} md={12}><Form.Item name="checkpoint_name" label="检查点" rules={[{ required: true, whitespace: true }]}><Input maxLength={240} placeholder="例如 qwen3-lora-step-8000" /></Form.Item></Col>
        <Col xs={24} md={12}><Form.Item name="model_version" label="模型版本"><Input maxLength={120} /></Form.Item></Col>
        <Col xs={24} md={12}>{dropdown('platform', ['inference', 'platform'], true)}</Col>
        <Col xs={24} md={12}>{dropdown('device', ['inference', 'device'])}</Col>
        <Col xs={24} md={12}>{dropdown('precision', ['inference', 'precision'])}</Col>
        <Col xs={24} md={12}>{dropdown('inference_mode', ['inference', 'mode'], true)}</Col>
      </Row>
      <Form.Item name="model_notes" label="模型备注"><Input.TextArea rows={2} /></Form.Item>
      <Row gutter={16}>
        <Col xs={24} md={12}><Form.Item name={['inference', 'generated_at']} label="推理时间"><Input placeholder="例如 2026-09-07T12:00:00Z" /></Form.Item></Col>
        <Col xs={24} md={12}><Form.Item name={['inference', 'code_revision']} label="代码版本"><Input /></Form.Item></Col>
      </Row>
      <Form.Item name="decoding_text" label="解码参数（JSON，可选）"><Input.TextArea rows={3} /></Form.Item>
      <Typography.Title level={5}>预测对应的数据集版本</Typography.Title>
      <Form.List name="datasets">{(fields) => <>{fields.map((field) => <VersionFields key={field.key} index={field.name} datasets={datasetQuery.data || []} root={!!draft.report.root_predictions} />)}</>}</Form.List>
    </Form>
    <Space><Button disabled={loading} onClick={onBack}>更换文件</Button><Button type="primary" loading={loading} onClick={validate}>核验模型信息、数据集版本和预测 ID</Button></Space>
    <Modal title="维护导入下拉选项" width={860} open={manage} footer={null} onCancel={() => { setManage(false); optionsQuery.refresh() }} destroyOnHidden><ImportOptionsPanel /></Modal>
  </>
}
