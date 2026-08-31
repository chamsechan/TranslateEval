import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider, App as AntApp, theme } from 'antd'
import { BrowserRouter } from 'react-router-dom'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import './styles.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: '#246bfd',
          colorInfo: '#246bfd',
          colorSuccess: '#10a779',
          colorWarning: '#f39b32',
          colorError: '#e5484d',
          borderRadius: 10,
          fontFamily: "Inter, 'PingFang SC', 'Microsoft YaHei', sans-serif",
        },
        components: { Card: { headerBg: 'transparent' }, Table: { headerBg: '#f7f9fc' } },
      }}
    >
      <AntApp>
        <BrowserRouter><App /></BrowserRouter>
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>,
)

