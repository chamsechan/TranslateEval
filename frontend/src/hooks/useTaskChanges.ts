import { useEffect, useRef, useState } from 'react'
import { getApiBase } from '../api'

interface TaskChangesOptions {
  evaluatorJobId?: string
  minIntervalMs?: number
}

export function useTaskChanges(onChange: () => void, enabled = true, options: TaskChangesOptions = {}) {
  const callback = useRef(onChange)
  callback.current = onChange
  const [connected, setConnected] = useState(false)
  const { evaluatorJobId, minIntervalMs = 0 } = options
  useEffect(() => {
    setConnected(false)
    if (!enabled) return
    let online = false
    let pending: number | undefined
    let lastChange = -Infinity
    const interval = Math.max(0, minIntervalMs)
    const notify = () => {
      if (pending !== undefined) window.clearTimeout(pending)
      pending = undefined
      lastChange = performance.now()
      callback.current()
    }
    const scheduleChange = () => {
      const remaining = interval - (performance.now() - lastChange)
      if (remaining <= 0) notify()
      else if (pending === undefined) pending = window.setTimeout(notify, remaining)
    }
    const query = evaluatorJobId ? `?${new URLSearchParams({ evaluator_job_id: evaluatorJobId })}` : ''
    const source = new EventSource(`${getApiBase()}/tasks/changes${query}`)
    source.addEventListener('tasks-changed', () => {
      online = true
      setConnected(true)
      scheduleChange()
    })
    source.onerror = () => { online = false; setConnected(false) }
    const fallback = window.setInterval(() => { if (!online) scheduleChange() }, 10000)
    return () => {
      source.close()
      window.clearInterval(fallback)
      if (pending !== undefined) window.clearTimeout(pending)
    }
  }, [enabled, evaluatorJobId, minIntervalMs])
  return connected
}
