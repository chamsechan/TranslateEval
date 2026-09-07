import { CloudUploadOutlined, FolderOpenOutlined } from '@ant-design/icons'
import { Alert, App, Button, Col, Form, Input, Row, Upload } from 'antd'
import { useState } from 'react'
import { api } from '../api'
import type { ImportReport } from '../types'

export default function ImportSource({ kind, onPrepared, onBusy }: { kind: 'dataset' | 'submission'; onPrepared: (report: ImportReport) => void; onBusy?: (busy: boolean) => void }) {
  const { message } = App.useApp()
  const [path, setPath] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [loading, setLoading] = useState(false)
  const prepare = async () => {
    setLoading(true)
    onBusy?.(true)
    try {
      let value: ImportReport
      if (file) {
        const body = new FormData(); body.append('file', file)
        value = await api(`/${kind}-imports/prepare-upload`, { method: 'POST', body })
      } else value = await api(`/${kind}-imports/prepare`, { method: 'POST', body: JSON.stringify({ path }) })
      onPrepared(value)
    } catch (error) { message.error((error as Error).message) } finally { setLoading(false); onBusy?.(false) }
  }
  return <>
    <Alert type="info" showIcon message="先读取文件，再编辑导入信息" description={kind === 'dataset' ? '支持 samples.jsonl、数据集目录或 ZIP。dataset_info.json 可选，有则预填，无则在下一步填写；源语种可从样本汇总。' : '支持 predictions.jsonl、结果目录或 ZIP。result_info.json 可选，有则预填，无则在下一步选择模型、推理信息和数据集版本。'} style={{ marginBottom: 18 }} />
    <Row gutter={[20, 16]}>
      <Col xs={24} md={12}><Form layout="vertical"><Form.Item label="服务器目录、ZIP 或 JSONL 路径" htmlFor={`${kind}-source-path`}><Input id={`${kind}-source-path`} disabled={loading} size="large" prefix={<FolderOpenOutlined />} value={path} onChange={(event) => { setPath(event.target.value); setFile(null) }} placeholder={kind === 'dataset' ? '/data/corpora/flores-demo' : '/data/results/demo-run'} /></Form.Item></Form></Col>
      <Col xs={24} md={12}><Upload.Dragger disabled={loading} fileList={file ? [{ uid: 'import-file', name: file.name, status: 'done' }] : []} accept=".zip,.jsonl" maxCount={1} beforeUpload={(value) => { setFile(value); setPath(''); return false }} onRemove={() => setFile(null)}><p className="ant-upload-drag-icon" style={{ margin: 0 }}><CloudUploadOutlined /></p><p>或选择 ZIP / JSONL 文件</p></Upload.Dragger></Col>
    </Row>
    <Button type="primary" size="large" block loading={loading} disabled={!path.trim() && !file} onClick={prepare} style={{ marginTop: 18 }}>读取并填写导入信息</Button>
  </>
}
