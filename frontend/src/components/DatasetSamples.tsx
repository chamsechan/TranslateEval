import { Modal, Select, Table, Typography } from 'antd'
import { useState } from 'react'
import { useApiQuery } from '../hooks/useApiQuery'
import type { DatasetVersion, PageResponse } from '../types'
import QueryError from './QueryError'

interface Sample { sample_id: string; source_language: string; source_text: string; reference_zh: string }

export default function DatasetSamples({ version, onClose }: { version: DatasetVersion; onClose: () => void }) {
  const [page, setPage] = useState(1)
  const [language, setLanguage] = useState<string | undefined>()
  const query = useApiQuery<PageResponse<Sample>>(`/dataset-versions/${version.id}/samples?page=${page}&page_size=20${language ? `&language=${encodeURIComponent(language)}` : ''}`)
  return <Modal title={`样本预览 · ${version.version_label}`} width={1000} open footer={null} onCancel={onClose}>
    <Typography.Paragraph type="secondary">源文和中文参考译文来自此不可变版本。</Typography.Paragraph>
    <Select aria-label="筛选样本语种" allowClear placeholder="全部语种" style={{ width: 180, marginBottom: 16 }} value={language} options={version.source_languages.map(value => ({ value, label: value }))} onChange={value => { setLanguage(value); setPage(1) }} />
    <QueryError error={query.error} retry={query.refresh} />
    <Table rowKey="sample_id" loading={query.loading} dataSource={query.data?.items || []} size="small" scroll={{ x: 700 }} pagination={{ current: page, pageSize: 20, total: query.data?.total || 0, showSizeChanger: false, onChange: setPage }} columns={[
      { title: '样本 ID', dataIndex: 'sample_id', width: 160 },
      { title: '语种', dataIndex: 'source_language', width: 80 },
      { title: '源文', dataIndex: 'source_text', width: 330 },
      { title: '中文参考译文', dataIndex: 'reference_zh', width: 330 },
    ]} />
  </Modal>
}
