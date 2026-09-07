import { Alert, App, AutoComplete, Button, Col, Form, Input, Row, Space, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Dataset, ImportReport } from '../types'

export default function DatasetImportEditor({ draft, datasets, onValidated, onBack, onBusy }: {
  draft: ImportReport; datasets: Dataset[]; onValidated: (report: ImportReport) => void; onBack: () => void; onBusy?: (busy: boolean) => void
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(false)
  const key = Form.useWatch('dataset_key', form)
  const existing = datasets.find((item) => item.key === key)
  useEffect(() => {
    if (existing) form.setFieldsValue({ name: existing.name, description: existing.description })
  }, [existing, form])
  const validate = async () => {
    try {
      const values = await form.validateFields()
      setLoading(true)
      onBusy?.(true)
      const value = await api<ImportReport>(`/import-reports/${draft.id}/validate`, { method: 'POST', body: JSON.stringify({ manifest: { ...draft.manifest, ...values } }) })
      onValidated(value)
    } catch (error) { if (error instanceof Error) message.error(error.message) } finally { setLoading(false); onBusy?.(false) }
  }
  return <>
    <Alert type="info" showIcon message="确认数据集信息" description="元信息可以在此补全或修改；样本 ID、源文和参考译文仍以导入文件为准。" style={{ marginBottom: 18 }} />
    {!!draft.report.errors.length && <Alert type="warning" showIcon message="读取提示" description={draft.report.errors.map((item) => String(item.message)).join('\n')} style={{ marginBottom: 18, whiteSpace: 'pre-wrap' }} />}
    <Form form={form} layout="vertical" initialValues={draft.manifest} disabled={loading}>
      <Row gutter={16}>
        <Col xs={24} sm={12}><Form.Item name="dataset_key" label="数据集标识" rules={[{ required: true }, { pattern: /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,119}$/, message: '使用字母、数字、点、下划线或短横线' }]}><AutoComplete options={datasets.map((item) => ({ value: item.key, label: `${item.name} · ${item.key}` }))} onSelect={(value) => { const item = datasets.find((dataset) => dataset.key === value); if (item) form.setFieldsValue({ name: item.name, description: item.description }) }} placeholder="选择已有数据集或输入新标识" /></Form.Item></Col>
        <Col xs={24} sm={12}><Form.Item name="name" label="数据集名称" rules={[{ required: true, whitespace: true }]}><Input disabled={loading || !!existing} maxLength={200} /></Form.Item></Col>
      </Row>
      {existing && <Typography.Paragraph type="secondary">将为「{existing.name}」导入新版本；已有数据集的名称和说明保持原记录。</Typography.Paragraph>}
      <Form.Item name="version_label" label="版本标签" rules={[{ required: true, whitespace: true }]}><Input maxLength={120} placeholder="例如 v1 或 2026-09-07" /></Form.Item>
      <Form.Item name="description" label="数据集说明"><Input.TextArea disabled={loading || !!existing} rows={2} /></Form.Item>
      <Form.Item name="change_note" label="版本变更备注"><Input.TextArea rows={2} /></Form.Item>
      <Form.Item label="源语种" extra="从样本的 source_language 自动汇总。">
        <Space wrap>{draft.report.detected_languages?.length
          ? draft.report.detected_languages.map((code) => <Tag key={code}>{code}</Tag>)
          : <Typography.Text type="secondary">未读取到源语种</Typography.Text>}</Space>
      </Form.Item>
    </Form>
    <Space><Button disabled={loading} onClick={onBack}>更换文件</Button><Button type="primary" loading={loading} onClick={validate}>核验数据集与版本变化</Button></Space>
  </>
}
