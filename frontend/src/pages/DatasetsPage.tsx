import { CheckCircleOutlined, DatabaseOutlined, HistoryOutlined, PlusOutlined, WarningOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Col, Descriptions, Empty, Modal, Row, Space, Skeleton, Statistic, Tag, Timeline, Typography } from 'antd'
import { useState } from 'react'
import { api, formatDate } from '../api'
import PageHeader from '../components/PageHeader'
import DatasetSamples from '../components/DatasetSamples'
import QueryError from '../components/QueryError'
import ImportSource from '../components/ImportSource'
import DatasetImportEditor from '../components/DatasetImportEditor'
import { useApiQuery } from '../hooks/useApiQuery'
import type { Dataset, DatasetVersion, ImportReport } from '../types'

const fieldLabels: Record<string, string> = { dataset_key: '数据集标识', name: '名称', version_label: '版本标签', sample_count: '样本数', source_languages: '源语种', languages: '源语种', is_new_dataset: '新数据集', content_sha256: '内容哈希', added: '新增', removed: '移除', source_changed: '源文变化', reference_changed: '参考译文变化', unchanged: '未变化' }

export default function DatasetsPage() {
  const { message } = App.useApp()
  const datasetQuery = useApiQuery<Dataset[]>('/datasets')
  const datasets = datasetQuery.data || []
  const load = datasetQuery.refresh
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [draft, setDraft] = useState<ImportReport | null>(null)
  const [report, setReport] = useState<ImportReport | null>(null)
  const [selected, setSelected] = useState<Dataset | null>(null)
  const [sampleVersion, setSampleVersion] = useState<DatasetVersion | null>(null)
  const versionQuery = useApiQuery<DatasetVersion[]>(selected ? `/datasets/${selected.id}/versions` : null)
  const versions = versionQuery.data || []

  const resetModal = () => { setOpen(false); setReport(null); setDraft(null) }
  const commit = async () => {
    if (!report) return
    setLoading(true)
    try {
      await api(`/dataset-imports/${report.id}/commit`, { method: 'POST' })
      message.success('数据集版本已导入'); resetModal(); await load()
    } catch (error) { message.error((error as Error).message) } finally { setLoading(false) }
  }
  const showVersions = async (dataset: Dataset) => {
    setSelected(dataset)
  }

  return (
    <>
      <PageHeader title="数据集" subtitle="使用不可变快照管理平行语料，并在写入前严格核验每一条变化。" actions={<Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>导入数据集</Button>} />
      <QueryError error={datasetQuery.error} retry={load} />
      {datasetQuery.loading && !datasetQuery.data && <Skeleton active />}
      <Row gutter={[16, 16]}>
        {datasets.map((dataset) => (
          <Col xs={24} md={12} xl={8} key={dataset.id}>
            <Card className="dataset-card" role="button" tabIndex={0} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); void showVersions(dataset) } }} onClick={() => showVersions(dataset)} style={{ cursor: 'pointer' }}>
              <Space align="start" style={{ width: '100%', justifyContent: 'space-between' }}>
                <Space align="start"><div className="dataset-icon"><DatabaseOutlined /></div><div><Typography.Title level={5} style={{ margin: '1px 0 2px' }}>{dataset.name}</Typography.Title><Typography.Text type="secondary">{dataset.key}</Typography.Text></div></Space>
                <Tag color="blue">{dataset.version_count} 版本</Tag>
              </Space>
              <Row gutter={12} style={{ marginTop: 24 }}>
                <Col span={12}><Statistic title="样本数" value={dataset.latest_version?.sample_count || 0} valueStyle={{ fontSize: 21 }} /></Col>
                <Col span={12}><Statistic title="语种数" value={dataset.latest_version?.source_languages.length || 0} valueStyle={{ fontSize: 21 }} /></Col>
              </Row>
              <div style={{ marginTop: 18 }}><Typography.Text type="secondary">最新版本 </Typography.Text><Typography.Text strong>{dataset.latest_version?.version_label || '—'}</Typography.Text></div>
              {dataset.latest_version && <div className="hash-text" style={{ marginTop: 7 }}>{dataset.latest_version.content_sha256.slice(0, 28)}…</div>}
            </Card>
          </Col>
        ))}
      </Row>
      {!datasetQuery.loading && !datasetQuery.error && !datasets.length && <Empty className="empty-soft" description="还没有数据集，先导入 samples.jsonl 并填写数据集信息" />}

      <Modal title="导入数据集或新版本" width={760} open={open} onCancel={() => { if (!loading) resetModal() }} footer={report ? [<Button key="back" disabled={loading} onClick={() => setReport(null)}>修改并重新核验</Button>, report.report.valid && <Button key="commit" type="primary" loading={loading} onClick={commit}>确认写入不可变版本</Button>] : null}>
        {!report ? (draft ? <DatasetImportEditor draft={draft} datasets={datasets} onBusy={setLoading} onBack={() => setDraft(null)} onValidated={(value) => { setDraft(value); setReport(value) }} /> : <ImportSource kind="dataset" onPrepared={setDraft} onBusy={setLoading} />) : <div className={report.report.valid ? 'validation-ok' : 'validation-error'} style={{ paddingLeft: 18 }}>
          <Alert type={report.report.valid ? 'success' : 'error'} showIcon icon={report.report.valid ? <CheckCircleOutlined /> : <WarningOutlined />} message={report.report.valid ? '核验通过' : '核验失败'} description={report.report.valid ? '数据结构、ID 与样本语种均有效。' : `发现 ${report.report.errors.length} 项问题，数据库未发生变化。`} />
          {report.report.summary && <Descriptions bordered size="small" column={{ xs: 1, sm: 2 }} style={{ marginTop: 18 }} items={Object.entries(report.report.summary).map(([key, value]) => ({ key, label: fieldLabels[key] || key, children: Array.isArray(value) ? value.join(', ') : String(value) }))} />}
          {report.report.diff && <Card size="small" title="相对最新版本的变化" style={{ marginTop: 16 }}><Space wrap size="large">{['added', 'removed', 'source_changed', 'reference_changed', 'unchanged'].map((key) => <Statistic key={key} title={fieldLabels[key]} value={Number(report.report.diff?.[key] || 0)} valueStyle={{ fontSize: 18 }} />)}</Space></Card>}
          {!!report.report.errors.length && <Card size="small" title="错误明细" style={{ marginTop: 16 }}><div className="code-template">{report.report.errors.map((item) => JSON.stringify(item, null, 2)).join('\n')}</div></Card>}
        </div>}
      </Modal>

      <Modal title={<Space><HistoryOutlined />版本历史 · {selected?.name}</Space>} width={720} open={!!selected} footer={null} onCancel={() => setSelected(null)}>
        <QueryError error={versionQuery.error} retry={versionQuery.refresh} />
        {versionQuery.loading && <Skeleton active />}
        <Timeline items={versions.map((version, index) => ({ color: index === 0 ? 'blue' : 'gray', children: <Card size="small" style={{ marginBottom: 10 }}><Space style={{ width: '100%', justifyContent: 'space-between' }}><Typography.Text strong>{version.version_label}</Typography.Text>{index === 0 && <Tag color="blue">最新</Tag>}</Space><div style={{ margin: '8px 0', color: '#66758a' }}>{version.change_note || '无版本备注'}</div><Space wrap size="large"><span>{version.sample_count.toLocaleString()} 句对</span><span>{version.source_languages.length} 语种</span><span>{formatDate(version.created_at)}</span></Space><Typography.Paragraph className="hash-text" copyable={{ text: version.content_sha256 }} style={{ marginTop: 8, overflowWrap: 'anywhere' }}>{version.content_sha256}</Typography.Paragraph><Button onClick={() => setSampleVersion(version)}>预览样本</Button></Card> }))} />
      </Modal>
      {sampleVersion && <DatasetSamples key={sampleVersion.id} version={sampleVersion} onClose={() => setSampleVersion(null)} />}
    </>
  )
}
