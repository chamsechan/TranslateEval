import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'

export function useApiQuery<T>(path: string | null) {
  const reload = useRef<(() => Promise<void>) | null>(null)
  const [state, setState] = useState<{ path: string | null; data: T | null; error: string | null; loading: boolean }>({ path: null, data: null, error: null, loading: false })
  const refresh = useCallback(() => { void reload.current?.() }, [])
  useEffect(() => {
    if (!path) return
    const controller = new AbortController()
    let busy = false
    let queued = false
    const read = async () => {
      if (busy) { queued = true; return }
      busy = true
      setState((previous) => ({ path, data: previous.path === path ? previous.data : null, error: previous.path === path ? previous.error : null, loading: true }))
      try {
        const data = await api<T>(path, { signal: controller.signal })
        if (!controller.signal.aborted) setState({ path, data, error: null, loading: false })
      } catch (error) {
        if (!controller.signal.aborted) setState((previous) => ({ ...previous, error: error instanceof Error ? error.message : '请求失败', loading: false }))
      } finally {
        busy = false
        if (queued && !controller.signal.aborted) { queued = false; void read() }
      }
    }
    reload.current = read
    void read()
    return () => { controller.abort(); if (reload.current === read) reload.current = null }
  }, [path])
  const current = state.path === path
  return { data: current ? state.data : null, error: current ? state.error : null, loading: !!path && (!current || state.loading), refresh }
}
