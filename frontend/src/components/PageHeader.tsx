import { Typography } from 'antd'
import type { ReactNode } from 'react'

export default function PageHeader({ title, subtitle, actions }: { title: string; subtitle: string; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div><Typography.Title level={2}>{title}</Typography.Title><div className="page-subtitle">{subtitle}</div></div>
      {actions}
    </div>
  )
}

