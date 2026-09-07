import { App, Button, Form, Input, Modal, Select, Space, Switch, Table, Tabs, Tag, Typography } from 'antd'
import { useState } from 'react'
import { api } from '../api'
import { useApiQuery } from '../hooks/useApiQuery'
import type { ImportOption, ImportOptionCategory } from '../types'
import QueryError from './QueryError'

export const optionLabels: Record<ImportOptionCategory, string> = { model: '模型', device: '设备', platform: '平台', precision: '精度 / 量化位宽', inference_mode: '推理模式' }

export default function ImportOptionsPanel() {
  const { message } = App.useApp()
  const query = useApiQuery<ImportOption[]>('/import-options')
  const [category, setCategory] = useState<ImportOptionCategory>('model')
  const [editing, setEditing] = useState<ImportOption | null>(null)
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<ImportOption>()
  const formCategory = Form.useWatch('category', form)
  const begin = (option?: ImportOption) => {
    setEditing(option || null)
    form.resetFields()
    form.setFieldsValue(option || { category, enabled: true, detects_language: false })
    setOpen(true)
  }
  const save = async () => {
    try {
      const values = await form.validateFields()
      setSaving(true)
      await api(editing ? `/import-options/${editing.id}` : '/import-options', {
        method: editing ? 'PATCH' : 'POST', body: JSON.stringify(values),
      })
      message.success('选项已保存'); setOpen(false); query.refresh()
    } catch (error) { if (error instanceof Error) message.error(error.message) } finally { setSaving(false) }
  }
  return <>
    <Typography.Paragraph type="secondary">维护被测模型及推理信息的下拉选项。停用后不再供新导入选择；历史记录保留导入时的信息。检查点在导入时填写文本。</Typography.Paragraph>
    <QueryError error={query.error} retry={query.refresh} />
    <Tabs activeKey={category} onChange={(key) => setCategory(key as ImportOptionCategory)} items={Object.entries(optionLabels).map(([key, label]) => ({ key, label }))} tabBarExtraContent={<Button type="primary" onClick={() => begin()}>新增选项</Button>} />
    <Table rowKey="id" loading={query.loading} dataSource={(query.data || []).filter((item) => item.category === category)} pagination={{ pageSize: 8 }} scroll={{ x: 580 }} columns={[
      { title: '显示名称', dataIndex: 'label' },
      { title: '选项值', dataIndex: 'value', render: (value: string) => <Typography.Text code>{value}</Typography.Text> },
      ...(category === 'inference_mode' ? [{ title: '语种识别统计', dataIndex: 'detects_language', render: (value: boolean) => value ? '统计' : '不统计' }] : []),
      { title: '状态', dataIndex: 'enabled', render: (value: boolean) => <Tag color={value ? 'success' : 'default'}>{value ? '启用' : '停用'}</Tag> },
      { title: '操作', key: 'actions', render: (_: unknown, option: ImportOption) => <Button type="link" onClick={() => begin(option)}>编辑</Button> },
    ]} />
    <Modal title={editing ? '编辑下拉选项' : '新增下拉选项'} open={open} onCancel={() => { if (!saving) setOpen(false) }} onOk={save} confirmLoading={saving}>
      <Form form={form} layout="vertical" disabled={saving}>
        <Form.Item name="category" label="分类" rules={[{ required: true }]}><Select disabled={!!editing} options={Object.entries(optionLabels).map(([value, label]) => ({ value, label }))} onChange={() => form.setFieldValue('detects_language', false)} /></Form.Item>
        <Form.Item name="value" label="选项值" extra="与 JSON 中的值对应，创建后固定。" rules={[{ required: true, whitespace: true }]}><Input disabled={!!editing} maxLength={formCategory === 'inference_mode' ? 40 : formCategory === 'platform' ? 160 : 200} /></Form.Item>
        <Form.Item name="label" label="显示名称" rules={[{ required: true, whitespace: true }]}><Input maxLength={200} /></Form.Item>
        {formCategory === 'inference_mode' && <Form.Item name="detects_language" label="统计模型输出的语种识别结果" valuePropName="checked" extra="启用后根据 predicted_language 计算准确率和覆盖率；修改只影响后续核验的新导入。"><Switch /></Form.Item>}
        <Space><Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item></Space>
      </Form>
    </Modal>
  </>
}
