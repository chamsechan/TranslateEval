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
import { Button, Drawer, Layout, Menu, Space, Spin, Typography } from 'antd'
import { lazy, Suspense, useEffect, useState } from 'react'
import { Link, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { api } from './api'

const ImportCommitHistory = lazy(() => import('./components/ImportCommitProgress'))
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
  const [collapsed, setCollapsed] = useState(() => window.innerWidth < 1200)
  const [importsOpen, setImportsOpen] = useState(false)
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
      <Sider breakpoint="xl" onBreakpoint={setCollapsed} width={236} collapsedWidth={76} collapsed={collapsed} className="app-sider">
        <Link to="/" className={`brand ${collapsed ? 'brand-collapsed' : ''}`} aria-label="TranslateEval · ARI-NLP 首页">
          <img className="brand-mark" src="./translate-eval-mark.svg" alt="" width={42} height={42} />
          {!collapsed && <div className="brand-copy"><strong>Translate<span>Eval</span></strong><small>ARI-NLP</small></div>}
        </Link>
        <Menu theme="dark" mode="inline" selectedKeys={[active]} items={menuItems} onClick={({ key }) => navigate(key)} />
        {!collapsed && <div className="sider-foot">
          <div className="sider-credit"><span>AUTHOR</span><strong>chen_qc</strong></div>
          <span className="sider-date">2026.09</span>
        </div>}
      </Sider>
      <Layout>
        <Header className="app-header">
          <Button aria-label={collapsed ? '展开导航' : '收起导航'} type="text" icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} onClick={() => setCollapsed(!collapsed)} />
          <Space className="header-status" wrap size={[10, 4]}>
            <Button size="small" onClick={() => setImportsOpen(true)}>导入进度</Button>
            <span className={`live-dot ${serviceStatus}`} />
            <Typography.Text type={serviceStatus === 'connected' ? 'secondary' : 'warning'}>{serviceText}</Typography.Text>
          </Space>
        </Header>
        <Drawer title="后台导入记录" width={860} open={importsOpen} onClose={() => setImportsOpen(false)} destroyOnHidden><Suspense fallback={<Spin />}><ImportCommitHistory onNavigate={() => setImportsOpen(false)} /></Suspense></Drawer>
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
