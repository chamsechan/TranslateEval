import { Alert, Button } from 'antd'

export default function QueryError({ error, retry }: { error: string | null; retry: () => void }) {
  return error ? <Alert type="error" showIcon title="加载失败" description={error} action={<Button onClick={retry}>重试</Button>} style={{ marginBottom: 16 }} /> : null
}
