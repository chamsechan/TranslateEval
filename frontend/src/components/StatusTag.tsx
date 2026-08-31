import { Tag } from 'antd'
import type { Status } from '../types'

const statusMap: Record<Status, { color: string; label: string }> = {
  queued: { color: 'default', label: '等待中' },
  preprocessing: { color: 'processing', label: '预处理中' },
  running: { color: 'processing', label: '评分中' },
  cancelling: { color: 'warning', label: '取消中' },
  cancelled: { color: 'default', label: '已取消' },
  completed: { color: 'success', label: '已完成' },
  partial_cancelled: { color: 'warning', label: '部分取消' },
  partial_failed: { color: 'warning', label: '部分失败' },
  failed: { color: 'error', label: '失败' },
}

export default function StatusTag({ status }: { status: Status }) {
  const value = statusMap[status] || { color: 'default', label: status }
  return <Tag className="status-pill" color={value.color}>{value.label}</Tag>
}
