import { CheckCircleOutlined, DatabaseOutlined, HistoryOutlined, PlusOutlined, WarningOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Col, Descriptions, Empty, Modal, Row, Space, Skeleton, Statistic, Tag, Timeline, Typography } from 'antd'
import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, formatDate } from '../api'
import PageHeader from '../components/PageHeader'
import DatasetSamples, { type DatasetSampleFilters } from '../components/DatasetSamples'
import QueryError from '../components/QueryError'
import ImportSource from '../components/ImportSource'
import DatasetImportEditor from '../components/DatasetImportEditor'
import { useApiQuery } from '../hooks/useApiQuery'
import { languagePairLabel } from '../languages'
import type { Dataset, DatasetVersion, ImportReport } from '../types'

const fieldLabels: Record<string, string> = { dataset_key: '数据集标识', name: '名称', version_label: '版本标签', sample_count: '样本数', source_languages: '源语种', languages: '源语种', is_new_dataset: '新数据集', content_sha256: '内容哈希', added: '新增', removed: '移除', source_changed: '源文变化', reference_changed: '参考译文变化', unchanged: '未变化' }

function VersionLanguages({ version, onSelect }: { version: DatasetVersion; onSelect: (language: string) => void }) {
  const [expanded, setExpanded] = useState(false)
  const pairs = version.language_pairs || version.source_languages.map(source_language => ({ source_language, source_name: source_language, sample_count: null }))
  const visible = expanded ? pairs : pairs.slice(0, 3)
  return <div style={{ margin: '14px 0' }}>
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 7, maxHeight: 230, overflowY: 'auto' }}>
      {visible.map(pair => <Button key={pair.source_language} type="link" style={{ padding: 0, height: 'auto', whiteSpace: 'normal', textAlign: 'left', maxWidth: '100%' }} onClick={() => onSelect(pair.source_language)}>
        <span>{languagePairLabel(pair.source_language, pair.source_name)}{pair.sample_count != null && <Typography.Text type="secondary"> · {pair.sample_count.toLocaleString()} 句</Typography.Text>}</span>
      </Button>)}
    </div>
    {pairs.length > 3 && <Button size="small" type="link" style={{ paddingLeft: 0, marginTop: 6 }} onClick={() => setExpanded(!expanded)}>{expanded ? '收起语言对' : `查看其余 ${pairs.length - 3} 个语言对`}</Button>}
  </div>
}

export default function DatasetsPage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const datasetQuery = useApiQuery<Dataset[]>('/datasets')
  const datasets = datasetQuery.data || []
  const load = datasetQuery.refresh
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [draft, setDraft] = useState<ImportReport | null>(null)
  const [report, setReport] = useState<ImportReport | null>(null)
  const [selected, setSelected] = useState<Dataset | null>(null)
  const versionQuery = useApiQuery<DatasetVersion[]>(selected ? `/datasets/${selected.id}/versions` : null)
  const versions = versionQuery.data || []
  const requestedVersionId = params.get('version')
  const requestedLanguage = params.get('language') || undefined
  const requestedSampleId = params.get('sample') || undefined
  const requestedSort = params.get('sample_sort') || undefined
  const requestedDirection = params.get('sample_direction') || undefined
  const samplePageNumber = Number(params.get('sample_page') || '1')
  const requestedPage = Number.isSafeInteger(samplePageNumber) && samplePageNumber > 0 ? samplePageNumber : 1
  const sampleVersionQuery = useApiQuery<DatasetVersion>(requestedVersionId ? `/dataset-versions/${encodeURIComponent(requestedVersionId)}` : null)
  const sampleVersion = sampleVersionQuery.data

  const resetModal = () => { setOpen(false); setReport(null); setDraft(null) }
  const commit = async () => {
    if (!report) return
    setLoading(true)
    try {
      await api(`/dataset-imports/${report.id}/commit`, { method: 'POST' })
      message.success('数据集版本已导入'); resetModal(); await load()
    } catch (error) { message.error((error as Error).message) } finally { setLoading(false) }
  }
  const openSamples = (version: DatasetVersion, language?: string) => {
    setParams(previous => {
      const next = new URLSearchParams(previous)
      next.set('version', version.id)
      if (language) next.set('language', language)
      else next.delete('language')
      next.delete('sample')
      next.delete('sample_page')
      next.delete('sample_sort')
      next.delete('sample_direction')
      return next
    })
  }
  const closeSamples = () => setParams(previous => {
    const next = new URLSearchParams(previous)
    for (const key of ['version', 'language', 'sample', 'sample_page', 'sample_sort', 'sample_direction']) next.delete(key)
    return next
  }, { replace: true })
  const updateSampleFilters = (filters: DatasetSampleFilters) => setParams(previous => {
    const next = new URLSearchParams(previous)
    if (filters.language) next.set('language', filters.language)
    else next.delete('language')
    if (filters.sampleId) next.set('sample', filters.sampleId)
    else next.delete('sample')
    if (filters.page > 1) next.set('sample_page', String(filters.page))
    else next.delete('sample_page')
    if (filters.sort) {
      next.set('sample_sort', filters.sort)
      next.set('sample_direction', filters.direction || 'asc')
    } else {
      next.delete('sample_sort')
      next.delete('sample_direction')
    }
    return next
  }, { replace: true })
  const viewResults = (version: DatasetVersion) => navigate(`/results?dataset_version_id=${encodeURIComponent(version.id)}`)

  return (
    <>
      <PageHeader title="数据集" subtitle="查看每个版本的语言对、样本数量和评测结果。点击语言对可直接核对源文与中文参考译文。" actions={<Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>导入数据集</Button>} />
      <QueryError error={datasetQuery.error} retry={load} />
      {datasetQuery.loading && !datasetQuery.data && <Skeleton active />}
      <Row gutter={[16, 16]}>
        {datasets.map((dataset) => (
          <Col xs={24} md={12} xl={8} key={dataset.id}>
            <Card className="dataset-card" style={{ height: '100%' }}>
              <Space align="start" style={{ width: '100%', justifyContent: 'space-between' }}>
                <Space align="start"><div className="dataset-icon"><DatabaseOutlined /></div><div><Typography.Title level={5} style={{ margin: '1px 0 2px' }}>{dataset.name}</Typography.Title><Typography.Text type="secondary">{dataset.key}</Typography.Text></div></Space>
                <Tag color="blue">{dataset.version_count} 版本</Tag>
              </Space>
              <Row gutter={12} style={{ marginTop: 24 }}>
                <Col span={12}><Statistic title="最新版样本数" value={dataset.latest_version?.sample_count || 0} valueStyle={{ fontSize: 21 }} /></Col>
                <Col span={12}><Statistic title="语言对" value={dataset.latest_version?.source_languages.length || 0} valueStyle={{ fontSize: 21 }} /></Col>
              </Row>
              <div style={{ marginTop: 18 }}><Typography.Text type="secondary">最新版本 </Typography.Text><Typography.Text strong>{dataset.latest_version?.version_label || '—'}</Typography.Text></div>
              {dataset.latest_version && <VersionLanguages key={dataset.latest_version.id} version={dataset.latest_version} onSelect={language => openSamples(dataset.latest_version!, language)} />}
              <Space wrap size={[8, 8]} style={{ marginTop: 8 }}>
                <Button icon={<HistoryOutlined />} onClick={() => setSelected(dataset)}>版本历史</Button>
                <Button disabled={!dataset.latest_version} onClick={() => dataset.latest_version && openSamples(dataset.latest_version)}>查看样本</Button>
                <Button type="primary" disabled={!dataset.latest_version} onClick={() => dataset.latest_version && viewResults(dataset.latest_version)}>查看评测结果</Button>
              </Space>
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
        <Typography.Paragraph type="secondary">每个版本独立展示语言对和样本数。查看评测结果时仅显示使用该版本的记录。</Typography.Paragraph>
        <Timeline items={versions.map((version, index) => ({ color: index === 0 ? 'blue' : 'gray', children: <Card size="small" style={{ marginBottom: 10 }}>
          <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}><Typography.Text strong>{version.version_label}</Typography.Text>{index === 0 && <Tag color="blue">最新</Tag>}</Space>
          <div style={{ margin: '8px 0', color: '#66758a' }}>{version.change_note || '无版本备注'}</div>
          <Space wrap size="large"><span>{version.sample_count.toLocaleString()} 句对</span><span>{version.source_languages.length} 个语言对</span><span>{formatDate(version.created_at)}</span></Space>
          <VersionLanguages key={version.id} version={version} onSelect={language => openSamples(version, language)} />
          <Space wrap><Button onClick={() => openSamples(version)}>查看样本</Button><Button type="primary" onClick={() => viewResults(version)}>查看评测结果</Button></Space>
          <details style={{ marginTop: 12 }}><summary style={{ cursor: 'pointer', color: '#66758a' }}>版本内容哈希</summary><Typography.Paragraph className="hash-text" copyable={{ text: version.content_sha256 }} style={{ marginTop: 8, overflowWrap: 'anywhere' }}>{version.content_sha256}</Typography.Paragraph></details>
        </Card> }))} />
      </Modal>
      {requestedVersionId && !sampleVersion && <Modal title="样本预览" width={900} open footer={null} onCancel={closeSamples}><QueryError error={sampleVersionQuery.error} retry={sampleVersionQuery.refresh} />{sampleVersionQuery.loading && <Skeleton active />}</Modal>}
      {sampleVersion && <DatasetSamples key={JSON.stringify([sampleVersion.id, requestedLanguage, requestedSampleId, requestedPage, requestedSort, requestedDirection])} version={sampleVersion} initialLanguage={requestedLanguage} initialSampleId={requestedSampleId} initialPage={requestedPage} initialSort={requestedSort} initialDirection={requestedDirection} onFiltersChange={updateSampleFilters} onClose={closeSamples} />}
    </>
  )
}
