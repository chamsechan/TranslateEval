import { App, AutoComplete, Button, Form, Input, Modal, Select, Space, Switch, Table, Tabs, Tag, Typography } from 'antd'
import { useState } from 'react'
import { api, ApiError } from '../api'
import { useApiQuery } from '../hooks/useApiQuery'
import type { ImportOption, ImportOptionCategory } from '../types'
import QueryError from './QueryError'

export const optionLabels: Record<ImportOptionCategory, string> = { model: '模型', device: '设备', platform: '平台', precision: '精度 / 量化位宽', inference_mode: '推理模式' }

export default function ImportOptionsPanel() {
  const { message, modal } = App.useApp()
  const query = useApiQuery<ImportOption[]>('/import-options')
  const [category, setCategory] = useState<ImportOptionCategory>('model')
  const [editing, setEditing] = useState<ImportOption | null>(null)
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [form] = Form.useForm<ImportOption>()
  const formCategory = Form.useWatch('category', form)
  const platform = Form.useWatch('platform', form)
  const options = query.data || []
  const platformOptions = options.filter((item) => item.category === 'platform')
  const sdkOptions = [...new Set(options.filter((item) => item.category === 'device' && item.platform === platform && item.enabled && item.sdk).map((item) => item.sdk))].map((value) => ({ value }))
  const begin = (option?: ImportOption) => {
    setEditing(option || null)
    form.resetFields()
    form.setFieldsValue(option || { category, enabled: true, platform: '', sdk: '', sdk_version: '' })
    setOpen(true)
  }
  const save = async () => {
    try {
      const { category: optionCategory, value, label, enabled, platform: optionPlatform, sdk, sdk_version } = await form.validateFields()
      const values = { category: optionCategory, value, label, enabled, ...(optionCategory === 'device' ? { platform: optionPlatform || '', sdk: sdk || '', sdk_version: sdk_version || '' } : {}) }
      setSaving(true)
      await api(editing ? `/import-options/${editing.id}` : '/import-options', {
        method: editing ? 'PATCH' : 'POST', body: JSON.stringify(values),
      })
      message.success('选项已保存'); setOpen(false); query.refresh()
    } catch (error) { if (error instanceof Error) message.error(error.message) } finally { setSaving(false) }
  }
  const remove = async (option: ImportOption, confirmed = false) => {
    setDeleting(true)
    try {
      await api(`/import-options/${option.id}${confirmed ? '?confirm=true' : ''}`, { method: 'DELETE' })
      message.success('选项已删除')
      setOpen(false)
      query.refresh()
    } catch (error) {
      if (error instanceof ApiError && error.code === 'confirmation_required' && !confirmed) confirmRemove(option)
      else if (error instanceof Error) message.error(error.message)
    } finally { setDeleting(false) }
  }
  const confirmRemove = (option: ImportOption) => modal.confirm({
    title: `确认删除${optionLabels[option.category]}“${option.label}”？`,
    content: '该选项已有结果。删除后将不再出现在选项库中，历史结果仍会保留。',
    okText: '确认删除', cancelText: '取消', okButtonProps: { danger: true },
    onOk: () => remove(option, true),
  })
  const changePlatform = () => {
    if (form.getFieldValue('sdk') || form.getFieldValue('sdk_version')) {
      form.setFieldsValue({ sdk: '', sdk_version: '' })
      message.info('平台已变更，请重新填写该平台的 SDK 和版本。')
    }
  }
  const changeSdk = () => {
    if (form.getFieldValue('sdk_version')) {
      form.setFieldValue('sdk_version', '')
      message.info('SDK 已变更，请重新填写对应的版本。')
    }
  }
  const busy = saving || deleting
  return <>
    <Typography.Paragraph type="secondary">维护被测模型及推理信息的下拉选项。停用后不再供新导入选择；历史记录保留导入时的信息。检查点在导入时填写文本。</Typography.Paragraph>
    <QueryError error={query.error} retry={query.refresh} />
    <Tabs activeKey={category} onChange={(key) => setCategory(key as ImportOptionCategory)} items={Object.entries(optionLabels).map(([key, label]) => ({ key, label }))} tabBarExtraContent={<Button type="primary" onClick={() => begin()}>新增选项</Button>} />
    {category === 'inference_mode' && <Typography.Paragraph type="secondary">推理模式用于区分默认、思考和不思考。</Typography.Paragraph>}
    <Table rowKey="id" loading={query.loading} dataSource={options.filter((item) => item.category === category)} pagination={{ pageSize: 8 }} scroll={{ x: category === 'device' ? 860 : 580 }} columns={[
      { title: category === 'device' ? '设备名称' : '显示名称', dataIndex: 'label' },
      { title: '选项值', dataIndex: 'value', render: (value: string) => <Typography.Text code>{value}</Typography.Text> },
      ...(category === 'device' ? [
        { title: '平台', dataIndex: 'platform', render: (value: string) => platformOptions.find((item) => item.value === value)?.label || value || '—' },
        { title: 'SDK', dataIndex: 'sdk', render: (value: string) => value || '—' },
        { title: 'SDK 版本', dataIndex: 'sdk_version', render: (value: string) => value || '—' },
      ] : []),
      { title: '状态', dataIndex: 'enabled', render: (value: boolean) => <Tag color={value ? 'success' : 'default'}>{value ? '启用' : '停用'}</Tag> },
      { title: '操作', key: 'actions', render: (_: unknown, option: ImportOption) => <Button type="link" onClick={() => begin(option)}>编辑</Button> },
    ]} />
    <Modal title={editing ? '编辑下拉选项' : '新增下拉选项'} open={open} onCancel={() => { if (!busy) setOpen(false) }} footer={<div style={{ display: 'flex', justifyContent: 'space-between' }}>
      <div>{editing && <Button danger loading={deleting} disabled={saving} onClick={() => editing.has_results ? confirmRemove(editing) : void remove(editing)}>删除{optionLabels[editing.category]}</Button>}</div>
      <Space><Button disabled={busy} onClick={() => setOpen(false)}>取消</Button><Button type="primary" loading={saving} disabled={deleting} onClick={save}>保存</Button></Space>
    </div>}>
      <Form form={form} layout="vertical" disabled={busy}>
        <Form.Item name="category" label="分类" rules={[{ required: true }]}><Select disabled={!!editing} options={Object.entries(optionLabels).map(([value, label]) => ({ value, label }))} /></Form.Item>
        <Form.Item name="value" label="选项值" extra="与 JSON 中的值对应，创建后固定。" rules={[{ required: true, whitespace: true }]}><Input disabled={!!editing} maxLength={formCategory === 'inference_mode' ? 40 : formCategory === 'platform' ? 160 : 200} /></Form.Item>
        <Form.Item name="label" label={formCategory === 'device' ? '设备名称' : '显示名称'} rules={[{ required: true, whitespace: true }]}><Input maxLength={200} placeholder={formCategory === 'device' ? '例如英伟达1080Ti、英伟达2080Ti、爱芯650' : formCategory === 'inference_mode' ? '默认、思考或不思考' : undefined} /></Form.Item>
        {formCategory === 'device' && <>
          <Form.Item name="platform" label="平台" dependencies={['sdk', 'sdk_version']} rules={[{ validator: async (_, value) => { if (!value && (form.getFieldValue('sdk')?.trim() || form.getFieldValue('sdk_version')?.trim())) throw new Error('填写 SDK 信息时请选择对应的平台') } }]}>
            <Select showSearch allowClear optionFilterProp="label" placeholder="选择设备对应的平台" onChange={changePlatform} options={[
              ...platformOptions.map((item) => ({ value: item.value, label: `${item.label}${!item.enabled ? '（已停用）' : ''}`, disabled: !item.enabled && item.value !== platform })),
              ...(platform && !platformOptions.some((item) => item.value === platform) ? [{ value: platform, label: `${platform}（历史值）` }] : []),
            ]} />
          </Form.Item>
          <Form.Item name="sdk" label="SDK" extra="各平台的 SDK 分别维护，可选择已有 SDK 或输入新值。"><AutoComplete disabled={!platform || busy} options={sdkOptions} onChange={changeSdk} filterOption={(input, option) => String(option?.value || '').toLowerCase().includes(input.toLowerCase())} placeholder={platform ? '选择或输入该平台的 SDK' : '请先选择平台'} maxLength={200} /></Form.Item>
          <Form.Item name="sdk_version" label="SDK 版本"><Input maxLength={200} /></Form.Item>
        </>}
        <Space><Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item></Space>
      </Form>
    </Modal>
  </>
}
