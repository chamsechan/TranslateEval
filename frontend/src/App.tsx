import {
  BarChartOutlined,
  CloudUploadOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  SettingOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons'
import { Button, Layout, Menu, Space, Spin, Tag, Typography } from 'antd'
import { lazy, Suspense, useEffect, useState } from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { api } from './api'

const DashboardPage = lazy(() => import('./pages/DashboardPage'))
const DatasetsPage = lazy(() => import('./pages/DatasetsPage'))
const ModelsPage = lazy(() => import('./pages/ModelsPage'))
const ResultsPage = lazy(() => import('./pages/ResultsPage'))
const SettingsPage = lazy(() => import('./pages/SettingsPage'))
const SubmitPage = lazy(() => import('./pages/SubmitPage'))
const TasksPage = lazy(() => import('./pages/TasksPage'))

const { Header, Sider, Content } = Layout

const menuItems = [
  { key: '/', icon: <BarChartOutlined />, label: '总览' },
  { key: '/datasets', icon: <DatabaseOutlined />, label: '数据集' },
  { key: '/submit', icon: <CloudUploadOutlined />, label: '提交评测' },
  { key: '/tasks', icon: <UnorderedListOutlined />, label: '任务队列' },
  { key: '/results', icon: <ExperimentOutlined />, label: '评测结果' },
  { key: '/models', icon: <ExperimentOutlined />, label: '模型记录' },
  { key: '/settings', icon: <SettingOutlined />, label: '模型与评价设置' },
]

export default function App() {
  const [collapsed, setCollapsed] = useState(false)
  const [serviceStatus, setServiceStatus] = useState<'checking' | 'connected' | 'worker-offline' | 'offline'>('checking')
  const navigate = useNavigate()
  const location = useLocation()
  const active = location.pathname === '/' ? '/' : `/${location.pathname.split('/')[1]}`

  useEffect(() => {
    let activeRequest = true
    const check = async () => {
      try {
        const health = await api<{ worker: { status: string } }>('/health')
        if (activeRequest) setServiceStatus(health.worker.status === 'connected' ? 'connected' : 'worker-offline')
      } catch {
        if (activeRequest) setServiceStatus('offline')
      }
    }
    void check()
    const timer = window.setInterval(check, 5000)
    return () => { activeRequest = false; window.clearInterval(timer) }
  }, [])

  const serviceText = {
    checking: '正在检查评测服务',
    connected: 'API 与 Worker 已连接',
    'worker-offline': 'API 在线 · Worker 未连接',
    offline: '评测服务不可用',
  }[serviceStatus]

  return (
    <Layout className="app-shell">
      <Sider width={236} collapsedWidth={76} collapsed={collapsed} className="app-sider">
        <div className={`brand ${collapsed ? 'brand-collapsed' : ''}`}>
          <div className="brand-mark">译</div>
          {!collapsed && <div><strong>译研评测台</strong><small>TRANSLATION LAB</small></div>}
        </div>
        <Menu theme="dark" mode="inline" selectedKeys={[active]} items={menuItems} onClick={({ key }) => navigate(key)} />
        {!collapsed && <div className="sider-foot"><Tag color="blue">LOCAL</Tag><span>SQLite · 内网模式</span></div>}
      </Sider>
      <Layout>
        <Header className="app-header">
          <Button type="text" icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} onClick={() => setCollapsed(!collapsed)} />
          <Space className="header-status">
            <span className={`live-dot ${serviceStatus}`} />
            <Typography.Text type={serviceStatus === 'connected' ? 'secondary' : 'warning'}>{serviceText}</Typography.Text>
          </Space>
        </Header>
        <Content className="app-content">
          <Suspense fallback={<div style={{ display: 'grid', placeItems: 'center', height: '60vh' }}><Spin size="large" /></div>}>
            <Routes>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/datasets" element={<DatasetsPage />} />
              <Route path="/submit" element={<SubmitPage />} />
              <Route path="/tasks" element={<TasksPage />} />
              <Route path="/results" element={<ResultsPage />} />
              <Route path="/results/:jobId" element={<ResultsPage />} />
              <Route path="/models" element={<ModelsPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </Content>
      </Layout>
    </Layout>
  )
}
