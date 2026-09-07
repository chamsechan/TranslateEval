import { Alert, App, AutoComplete, Button, Card, Col, Form, Input, Modal, Row, Select, Space, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { api } from '../api'
import { useApiQuery } from '../hooks/useApiQuery'
import type { Dataset, DatasetVersion, ImportOption, ImportOptionCategory, ImportReport } from '../types'
import ImportOptionsPanel, { optionLabels } from './ImportOptionsPanel'
import QueryError from './QueryError'

function VersionFields({ index, datasets, root, directories, onRemove, removable }: { index: number; datasets: Dataset[]; root: boolean; directories: string[]; onRemove: () => void; removable: boolean }) {
  const form = Form.useFormInstance()
  const key = Form.useWatch(['datasets', index, 'dataset_key'], form)
  const hash = Form.useWatch(['datasets', index, 'dataset_content_sha256'], form)
  const dataset = datasets.find((item) => item.key === key)
  const query = useApiQuery<DatasetVersion[]>(dataset ? `/datasets/${dataset.id}/versions` : null)
  const versions = query.data || []
  return <Card size="small" style={{ marginBottom: 12 }} extra={<Button danger size="small" disabled={!removable} onClick={onRemove}>删除此数据集条目</Button>}>
    <Row gutter={16}>
      <Col xs={24} md={10}><Form.Item name={[index, 'dataset_key']} label="数据集" rules={[{ required: true }]}>
        <Select showSearch optionFilterProp="label" options={[
          ...datasets.filter(item => root || directories.includes(item.key)).map((item) => ({ value: item.key, label: `${item.name} · ${item.key}` })),
          ...(key && (!dataset || (!root && !directories.includes(key))) ? [{ value: key, label: `${key}（${!dataset ? '尚未导入' : '未匹配预测目录'}）`, disabled: true }] : []),
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
    {!root && key && !directories.includes(key) && <Alert type="warning" showIcon message="此条目没有同名预测目录。请选择实际存在的目录对应数据集，或删除多余条目。" style={{ marginBottom: 12 }} />}
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
  const directories = draft.report.prediction_directories || []
  const rootOnly = !!draft.report.root_predictions && directories.length === 0
  const inference = draft.manifest.inference && typeof draft.manifest.inference === 'object' ? draft.manifest.inference as Record<string, unknown> : {}
  const initialValues = { ...draft.manifest, inference, decoding_text: JSON.stringify(inference.decoding || {}, null, 2) }
  const currentValues = Form.useWatch([], form)
  const currentInference = currentValues?.inference || inference
  const importOptions = optionsQuery.data || []
  const sdkOptions = [...new Set(importOptions.filter((item) => item.category === 'device' && item.platform === currentInference.platform && item.enabled && item.sdk).map((item) => item.sdk))].map((value) => ({ value }))
  useEffect(() => {
    if (!form.getFieldValue(['inference', 'mode']) && optionsQuery.data?.some((item) => item.category === 'inference_mode' && item.value === 'default' && item.enabled)) form.setFieldValue(['inference', 'mode'], 'default')
  }, [form, optionsQuery.data])
  const changePlatform = (platform: string) => {
    const selectedDevice = importOptions.find((item) => item.category === 'device' && item.value === form.getFieldValue(['inference', 'device']))
    const clearDevice = !!selectedDevice?.platform && selectedDevice.platform !== platform
    const hadSdk = !!form.getFieldValue(['inference', 'sdk']) || !!form.getFieldValue(['inference', 'sdk_version'])
    form.setFieldValue(['inference', 'sdk'], '')
    form.setFieldValue(['inference', 'sdk_version'], '')
    if (clearDevice) form.setFieldValue(['inference', 'device'], undefined)
    if (clearDevice || hadSdk) message.info(`平台已变更，已清空${clearDevice ? '不匹配的设备及 ' : ''}SDK 信息，请重新确认。`)
  }
  const changeDevice = (value?: string) => {
    const device = importOptions.find((item) => item.category === 'device' && item.value === value)
    if (!device) return
    const previousPlatform = form.getFieldValue(['inference', 'platform'])
    const platformChanged = !!device.platform && device.platform !== previousPlatform
    if (device.platform) form.setFieldValue(['inference', 'platform'], device.platform)
    if (platformChanged || device.sdk) {
      const previousSdk = form.getFieldValue(['inference', 'sdk'])
      const previousVersion = form.getFieldValue(['inference', 'sdk_version'])
      form.setFieldValue(['inference', 'sdk'], device.sdk || '')
      form.setFieldValue(['inference', 'sdk_version'], device.sdk_version || '')
      if (platformChanged || previousSdk !== (device.sdk || '') || previousVersion !== (device.sdk_version || '')) message.info('已根据设备更新平台、SDK 和版本，请确认实际使用的信息。')
    } else if (device.sdk_version) {
      form.setFieldValue(['inference', 'sdk_version'], device.sdk_version)
    }
  }
  const changeSdk = () => {
    if (form.getFieldValue(['inference', 'sdk_version'])) {
      form.setFieldValue(['inference', 'sdk_version'], '')
      message.info('SDK 已变更，请重新填写对应的版本。')
    }
  }
  const dropdown = (category: ImportOptionCategory, name: string | string[], required = false) => {
    const current = Array.isArray(name) ? currentValues?.[name[0]]?.[name[1]] : currentValues?.[name]
    const options = importOptions.filter((item) => item.category === category && (category !== 'device' || !currentInference.platform || !item.platform || item.platform === currentInference.platform || item.value === current))
    return <Form.Item name={name} label={optionLabels[category]} rules={required ? [{ required: true, message: `请选择${optionLabels[category]}` }] : []}>
      <Select showSearch allowClear={!required} optionFilterProp="label" loading={optionsQuery.loading} placeholder={`选择${optionLabels[category]}`} onChange={category === 'platform' ? changePlatform : category === 'device' ? changeDevice : undefined} options={[
        ...options.map((item) => {
          const platformUnavailable = category === 'device' && !!item.platform && item.platform !== currentInference.platform && !importOptions.some((platform) => platform.category === 'platform' && platform.value === item.platform && platform.enabled)
          return { value: item.value, label: `${item.label}${!item.enabled ? '（已停用）' : platformUnavailable ? '（所属平台不可选）' : ''}`, disabled: (!item.enabled || platformUnavailable) && item.value !== current }
        }),
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
        ...draft.manifest, ...manifest, inference: { ...inference, ...manifest.inference, device: manifest.inference.device || '', sdk: manifest.inference.sdk || '', sdk_version: manifest.inference.sdk_version || '', precision: manifest.inference.precision || '', generated_at: manifest.inference.generated_at || null, decoding },
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
        <Col xs={24} md={12}><Form.Item name={['inference', 'sdk']} label="SDK" extra="可选择当前平台已有的 SDK，或输入实际使用的 SDK。"><AutoComplete disabled={!currentInference.platform || loading} options={sdkOptions} onChange={changeSdk} filterOption={(input, option) => String(option?.value || '').toLowerCase().includes(input.toLowerCase())} placeholder={currentInference.platform ? '选择或输入该平台的 SDK' : '请先选择平台'} maxLength={200} /></Form.Item></Col>
        <Col xs={24} md={12}><Form.Item name={['inference', 'sdk_version']} label="SDK 版本"><Input maxLength={200} placeholder="填写实际使用的 SDK 版本" /></Form.Item></Col>
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
      <Typography.Paragraph type="secondary">{rootOnly ? '根目录 predictions.jsonl 关联一个数据集；多余的元信息条目可删除。' : '目录名与数据集标识对应。可删除多余元信息条目，或重新选择已有预测目录对应的数据集；不会移动或改名文件。'}</Typography.Paragraph>
      <Form.List name="datasets" rules={[{ validator: async (_, value) => { if (!value?.length) throw new Error('至少保留一个数据集'); if (rootOnly && value.length !== 1) throw new Error('根目录预测文件只能关联一个数据集，请删除多余条目') } }]}>{(fields, { add, remove }, { errors }) => <>
        {fields.map((field) => <VersionFields key={field.key} index={field.name} datasets={datasetQuery.data || []} root={rootOnly} directories={directories} removable={fields.length > 1} onRemove={() => remove(field.name)} />)}
        {!!directories.length && <Button onClick={() => { const selected = new Set((form.getFieldValue('datasets') || []).map((entry: { dataset_key?: string }) => entry?.dataset_key)); for (const key of directories) if (!selected.has(key)) add({ dataset_key: key }) }}>补齐预测目录条目</Button>}
        <Form.ErrorList errors={errors} />
      </>}</Form.List>
    </Form>
    <Space><Button disabled={loading} onClick={onBack}>更换文件</Button><Button type="primary" loading={loading} onClick={validate}>核验模型信息、数据集版本和预测 ID</Button></Space>
    <Modal title="维护导入下拉选项" width={860} open={manage} footer={null} onCancel={() => { setManage(false); optionsQuery.refresh() }} destroyOnHidden><ImportOptionsPanel /></Modal>
  </>
}
