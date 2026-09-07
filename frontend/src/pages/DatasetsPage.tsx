import { CheckCircleOutlined, HistoryOutlined, PlusOutlined, WarningOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Descriptions, Input, Modal, Space, Skeleton, Statistic, Table, Tag, Typography } from 'antd'
import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { formatDate } from '../api'
import PageHeader from '../components/PageHeader'
import DatasetSamples, { type DatasetSampleFilters } from '../components/DatasetSamples'
import QueryError from '../components/QueryError'
import ImportSource from '../components/ImportSource'
import DatasetImportEditor from '../components/DatasetImportEditor'
import { forgetImportJob, ImportCommitStatus, rememberedImportJob, startImportCommit } from '../components/ImportCommitProgress'
import { useApiQuery } from '../hooks/useApiQuery'
import { languagePairLabel } from '../languages'
import type { Dataset, DatasetVersion, ImportReport } from '../types'

const fieldLabels: Record<string, string> = { dataset_key: '数据集标识', name: '名称', version_label: '版本标签', sample_count: '样本数', source_languages: '源语种', languages: '源语种', is_new_dataset: '新数据集', content_sha256: '内容哈希', added: '新增', removed: '移除', source_changed: '源文变化', reference_changed: '参考译文变化', both_changed: '源文与参考均变化', source_and_reference_changed: '源文与参考均变化', unchanged: '未变化' }

function VersionLanguages({ version, onSelect }: { version: DatasetVersion; onSelect: (language: string) => void }) {
  const [expanded, setExpanded] = useState(false)
  const [query, setQuery] = useState('')
  const pairs = version.language_pairs || version.source_languages.map(source_language => ({ source_language, source_name: source_language, sample_count: null }))
  const filtered = pairs.filter(pair => languagePairLabel(pair.source_language, pair.source_name).toLowerCase().includes(query.trim().toLowerCase()))
  const visible = expanded ? filtered : pairs.slice(0, 3)
  return <div style={{ margin: '14px 0' }}>
    {expanded && <Input.Search aria-label="搜索版本语种" placeholder="搜索语种名称或代码" allowClear value={query} onChange={event => setQuery(event.target.value)} style={{ maxWidth: 340, marginBottom: 12 }} />}
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 7, maxHeight: 230, overflowY: 'auto' }}>
      {expanded && !visible.length && <Typography.Text type="secondary">未找到匹配语种</Typography.Text>}
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
  const [storedJobId, setStoredJobId] = useState(() => rememberedImportJob('dataset'))
  const importJobId = params.get('import_job') || storedJobId
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
  const [versionSearch, setVersionSearch] = useState('')
  const [versionPage, setVersionPage] = useState(1)
  const [versionPageSize, setVersionPageSize] = useState(10)
  const datasetSearch = params.get('q') || ''
  const datasetPageSize = [10, 20, 50].includes(Number(params.get('page_size'))) ? Number(params.get('page_size')) : 10
  const rawDatasetPage = Number(params.get('page') || 1)
  const datasetPage = Number.isSafeInteger(rawDatasetPage) && rawDatasetPage > 0 ? rawDatasetPage : 1
  const filteredDatasets = datasets.filter(dataset => `${dataset.name} ${dataset.key} ${dataset.description}`.toLowerCase().includes(datasetSearch.trim().toLowerCase()))
  const filteredVersions = versions.filter(version => `${version.version_label} ${version.change_note || ''} ${version.content_sha256}`.toLowerCase().includes(versionSearch.trim().toLowerCase()))
  const showVersions = (dataset: Dataset) => { setVersionSearch(''); setVersionPage(1); setSelected(dataset) }
  const updateDirectory = (changes: Record<string, string | number | undefined>) => setParams(previous => {
    const next = new URLSearchParams(previous)
    Object.entries(changes).forEach(([key, value]) => value == null || value === '' ? next.delete(key) : next.set(key, String(value)))
    return next
  }, { replace: true })
  const requestedVersionId = params.get('version')
  const requestedLanguage = params.get('language') || undefined
  const requestedSampleId = params.get('sample') || undefined
  const requestedSort = params.get('sample_sort') || undefined
  const requestedDirection = params.get('sample_direction') || undefined
  const samplePageNumber = Number(params.get('sample_page') || '1')
  const requestedPageSize = [20, 50, 100].includes(Number(params.get('sample_page_size'))) ? Number(params.get('sample_page_size')) : 20
  const requestedPage = Number.isSafeInteger(samplePageNumber) && samplePageNumber > 0 ? samplePageNumber : 1
  const sampleVersionQuery = useApiQuery<DatasetVersion>(requestedVersionId ? `/dataset-versions/${encodeURIComponent(requestedVersionId)}` : null)
  const sampleVersion = sampleVersionQuery.data

  const resetModal = () => { setOpen(false); setReport(null); setDraft(null) }
  const commit = async () => {
    if (!report) return
    setLoading(true)
    try {
      const job = await startImportCommit(report.id)
      setStoredJobId(job.id)
      updateDirectory({ import_job: job.id })
      message.success('已开始后台导入'); resetModal()
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
    for (const key of ['version', 'language', 'sample', 'sample_page', 'sample_page_size', 'sample_sort', 'sample_direction']) next.delete(key)
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
    if (filters.pageSize && filters.pageSize !== 20) next.set('sample_page_size', String(filters.pageSize))
    else next.delete('sample_page_size')
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
      {importJobId && <ImportCommitStatus key={importJobId} jobId={importJobId} onCompleted={() => { setStoredJobId(null); load() }} onDismiss={() => { forgetImportJob('dataset', importJobId); setStoredJobId(null); updateDirectory({ import_job: undefined }) }} />}
      <QueryError error={datasetQuery.error} retry={load} />
      {datasetQuery.loading && !datasetQuery.data && <Skeleton active />}
      <Card className="panel-card">
        <Input.Search aria-label="搜索数据集" placeholder="搜索名称、标识或描述" allowClear value={datasetSearch} onChange={event => updateDirectory({ q: event.target.value, page: undefined })} style={{ maxWidth: 420, marginBottom: 16 }} />
        <Table<Dataset> rowKey="id" size="small" loading={datasetQuery.loading} dataSource={filteredDatasets} scroll={{ x: 1060 }} pagination={{ current: Math.min(datasetPage, Math.max(1, Math.ceil(filteredDatasets.length / datasetPageSize))), pageSize: datasetPageSize, pageSizeOptions: [10, 20, 50], showSizeChanger: true, showQuickJumper: true, showTotal: total => `共 ${total} 个数据集`, onChange: (page, size) => updateDirectory({ page: size === datasetPageSize ? page : 1, page_size: size }) }} locale={{ emptyText: datasetSearch ? '没有匹配的数据集' : '还没有数据集，请先导入' }} expandable={{ expandedRowRender: dataset => <div><Typography.Paragraph type="secondary">{dataset.description || '暂无数据集描述'}</Typography.Paragraph>{dataset.latest_version && <VersionLanguages key={dataset.latest_version.id} version={dataset.latest_version} onSelect={language => openSamples(dataset.latest_version!, language)} />}</div>, rowExpandable: dataset => !!dataset.latest_version }} columns={[
          { title: '数据集', key: 'name', width: 220, sorter: (a, b) => a.name.localeCompare(b.name), render: (_, dataset) => <div><Typography.Text strong>{dataset.name}</Typography.Text><div><Typography.Text type="secondary">{dataset.key}</Typography.Text></div></div> },
          { title: '最新版本', width: 140, render: (_, dataset) => dataset.latest_version?.version_label || '—' },
          { title: '版本数', width: 90, sorter: (a, b) => a.version_count - b.version_count, render: (_, dataset) => dataset.version_count },
          { title: '样本数', width: 120, sorter: (a, b) => (a.latest_version?.sample_count || 0) - (b.latest_version?.sample_count || 0), render: (_, dataset) => (dataset.latest_version?.sample_count || 0).toLocaleString() },
          { title: '语言对', width: 90, render: (_, dataset) => dataset.latest_version?.source_languages.length || 0 },
          { title: '更新时间', width: 150, sorter: (a, b) => (a.latest_version?.created_at || '').localeCompare(b.latest_version?.created_at || ''), render: (_, dataset) => formatDate(dataset.latest_version?.created_at) },
          { title: '操作', width: 280, fixed: 'right', render: (_, dataset) => <Space wrap size={4}><Button size="small" onClick={() => showVersions(dataset)}>版本历史</Button><Button size="small" disabled={!dataset.latest_version} onClick={() => dataset.latest_version && openSamples(dataset.latest_version)}>查看样本</Button><Button size="small" type="link" disabled={!dataset.latest_version} onClick={() => dataset.latest_version && viewResults(dataset.latest_version)}>评测结果</Button></Space> },
        ]} />
      </Card>

      <Modal title="导入数据集或新版本" width={760} open={open} onCancel={() => { if (!loading) resetModal() }} footer={report ? [<Button key="back" disabled={loading} onClick={() => setReport(null)}>修改并重新核验</Button>, report.report.valid && <Button key="commit" type="primary" loading={loading} onClick={commit}>确认写入不可变版本</Button>] : null}>
        {!report ? (draft ? <DatasetImportEditor draft={draft} datasets={datasets} onBusy={setLoading} onBack={() => setDraft(null)} onValidated={(value) => { setDraft(value); setReport(value) }} /> : <ImportSource kind="dataset" onPrepared={setDraft} onBusy={setLoading} />) : <div className={report.report.valid ? 'validation-ok' : 'validation-error'} style={{ paddingLeft: 18 }}>
          <Alert type={report.report.valid ? 'success' : 'error'} showIcon icon={report.report.valid ? <CheckCircleOutlined /> : <WarningOutlined />} message={report.report.valid ? '核验通过' : '核验失败'} description={report.report.valid ? '数据结构、ID 与样本语种均有效。' : `发现 ${report.report.errors.length} 项问题，数据库未发生变化。`} />
          {report.report.summary && <Descriptions bordered size="small" column={{ xs: 1, sm: 2 }} style={{ marginTop: 18 }} items={Object.entries(report.report.summary).map(([key, value]) => ({ key, label: fieldLabels[key] || key, children: Array.isArray(value) ? value.join(', ') : String(value) }))} />}
          {report.report.diff && <Card size="small" title="相对最新版本的变化" style={{ marginTop: 16 }}><Typography.Paragraph type="secondary">源文与参考译文是独立变更维度，计数可重叠；“均变化”表示两者同时修改。</Typography.Paragraph><Space wrap size="large">{['added', 'removed', 'source_changed', 'reference_changed', 'both_changed', 'unchanged'].map((key) => <Statistic key={key} title={fieldLabels[key]} value={Number(report.report.diff?.[key] || 0)} valueStyle={{ fontSize: 18 }} />)}</Space></Card>}
          {!!report.report.errors.length && <Card size="small" title="错误明细" style={{ marginTop: 16 }}><div className="code-template">{report.report.errors.map((item) => JSON.stringify(item, null, 2)).join('\n')}</div></Card>}
        </div>}
      </Modal>

      <Modal title={<Space><HistoryOutlined />版本历史 · {selected?.name}</Space>} width={1100} open={!!selected} footer={null} onCancel={() => setSelected(null)}>
        <QueryError error={versionQuery.error} retry={versionQuery.refresh} />
        <Typography.Paragraph type="secondary">每个不可变版本独立保留样本与评测结果。展开行可检索语言对、查看内容哈希。</Typography.Paragraph>
        <Input.Search aria-label="搜索数据集版本" placeholder="搜索版本标签、备注或内容哈希" value={versionSearch} allowClear onChange={event => { setVersionSearch(event.target.value); setVersionPage(1) }} style={{ maxWidth: 420, marginBottom: 16 }} />
        <Table<DatasetVersion> rowKey="id" size="small" loading={versionQuery.loading} dataSource={filteredVersions} scroll={{ x: 950 }} pagination={{ current: versionPage, pageSize: versionPageSize, pageSizeOptions: [10, 20, 50], showSizeChanger: true, showQuickJumper: true, showTotal: total => `共 ${total} 个版本`, onChange: (page, size) => { setVersionPage(size === versionPageSize ? page : 1); setVersionPageSize(size) } }} expandable={{ expandedRowRender: version => <><VersionLanguages key={version.id} version={version} onSelect={language => openSamples(version, language)} /><Typography.Paragraph className="hash-text" copyable={{ text: version.content_sha256 }} style={{ overflowWrap: 'anywhere' }}>内容哈希：{version.content_sha256}</Typography.Paragraph></> }} columns={[
          { title: '版本', width: 150, render: (_, version) => <Space wrap><Typography.Text strong>{version.version_label}</Typography.Text>{version.id === versions[0]?.id && <Tag color="blue">最新</Tag>}</Space> },
          { title: '版本备注', dataIndex: 'change_note', render: value => value || '无版本备注' },
          { title: '样本数', width: 110, dataIndex: 'sample_count', sorter: (a, b) => a.sample_count - b.sample_count, render: value => value.toLocaleString() },
          { title: '语言对', width: 80, render: (_, version) => version.source_languages.length },
          { title: '创建时间', width: 150, dataIndex: 'created_at', sorter: (a, b) => a.created_at.localeCompare(b.created_at), render: formatDate },
          { title: '操作', width: 195, fixed: 'right', render: (_, version) => <Space size={4}><Button size="small" onClick={() => openSamples(version)}>查看样本</Button><Button size="small" type="link" onClick={() => viewResults(version)}>评测结果</Button></Space> },
        ]} />
      </Modal>
      {requestedVersionId && !sampleVersion && <Modal title="样本预览" width={900} open footer={null} onCancel={closeSamples}><QueryError error={sampleVersionQuery.error} retry={sampleVersionQuery.refresh} />{sampleVersionQuery.loading && <Skeleton active />}</Modal>}
      {sampleVersion && <DatasetSamples key={JSON.stringify([sampleVersion.id, requestedLanguage, requestedSampleId, requestedPage, requestedPageSize, requestedSort, requestedDirection])} version={sampleVersion} initialLanguage={requestedLanguage} initialSampleId={requestedSampleId} initialPage={requestedPage} initialPageSize={requestedPageSize} initialSort={requestedSort} initialDirection={requestedDirection} onFiltersChange={updateSampleFilters} onClose={closeSamples} />}
    </>
  )
}
