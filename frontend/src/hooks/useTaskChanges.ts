import { useEffect, useRef, useState } from 'react'
import { getApiBase } from '../api'

export function useTaskChanges(onChange: () => void, enabled = true) {
  const callback = useRef(onChange)
  callback.current = onChange
  const [connected, setConnected] = useState(false)
  useEffect(() => {
    if (!enabled) return
    let online = false
    const source = new EventSource(`${getApiBase()}/tasks/changes`)
    source.addEventListener('tasks-changed', () => {
      online = true
      setConnected(true)
      callback.current()
    })
    source.onerror = () => { online = false; setConnected(false) }
    const fallback = window.setInterval(() => { if (!online) callback.current() }, 10000)
    return () => { source.close(); window.clearInterval(fallback) }
  }, [enabled])
  return connected
}
