export function getApiBase(): string {
  let base = window.location.pathname.replace(/\/+$/, '')
  if (base.endsWith('/index.html') || base === '/index.html') {
    base = base.slice(0, -'/index.html'.length)
  }
  return `${base}/api`
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  const response = await fetch(`${getApiBase()}${normalizedPath}`, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...init?.headers,
    },
  })
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`
    try {
      const body = await response.json()
      if (typeof body.detail === 'string') detail = body.detail
      else if (Array.isArray(body.detail)) detail = body.detail.map((item: { loc?: Array<string | number>; msg?: string }) => `${item.loc?.filter((part) => part !== 'body').join('.') || '参数'}：${item.msg || '格式无效'}`).join('；')
    } catch {
      // Keep the status-based error when the body is not JSON.
    }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

export function formatDate(value?: string | null): string {
  return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '—'
}

export function formatScore(value?: number | null, digits = 2): string {
  return value == null ? '—' : value.toFixed(digits)
}
