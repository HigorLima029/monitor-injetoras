import { useCallback, useEffect, useRef, useState } from 'react'

export type MachineState = 'produzindo' | 'parada' | 'alarme' | 'offline'

export interface Status {
  connected: boolean
  state: MachineState
  last_ok: number | null
  last_error: string | null
  tag_count: number
  reads: number
  active_alarms: string[]
  server_time: number
}

export interface Live {
  status: Status
  values: Record<string, number | string | null>
}

export interface Zone {
  label: string
  actual: string
  setpoint: string
  min: string
  max: string
}

export interface Info {
  machine_name: string
  opcua_url: string
  simulate: boolean
  poll_seconds: number
  kpi_meta: Record<string, { label: string; unit: string; group: string }>
  zones: Zone[]
}

export type Point = [number, number | null]

export interface History {
  since: number
  series: Record<string, Point[]>
}

export interface Production {
  since: number
  bucket_seconds: number
  per_bucket: [number, number][]
  bad_per_bucket: [number, number][]
  produced: number
  bad: number
  avg_cycle: number | null
}

export interface AlarmEvent {
  tag: string
  label: string
  started: number
  ended: number | null
}

export interface Alarms {
  active: { tag: string; label: string }[]
  monitored: number
  events: AlarmEvent[]
}

export interface TagRow {
  tag: string
  label: string
  value: number | string | null
  unit: string
}

export async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`O servidor respondeu ${res.status} em ${url}`)
  return res.json() as Promise<T>
}

/** Busca uma URL e repete a cada `everyMs`. */
export function usePoll<T>(url: string | null, everyMs = 30000) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(async () => {
    if (!url) return
    try {
      setData(await getJson<T>(url))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [url])
  useEffect(() => {
    load()
    const id = window.setInterval(load, everyMs)
    return () => window.clearInterval(id)
  }, [load, everyMs])
  return { data, error, reload: load }
}

/** Valores ao vivo pelo WebSocket; se ele cair, tenta de novo e usa /api/live enquanto isso. */
export function useLive() {
  const [live, setLive] = useState<Live | null>(null)
  const [linked, setLinked] = useState(false)
  const retry = useRef<number | undefined>(undefined)

  useEffect(() => {
    let socket: WebSocket | null = null
    let stopped = false
    let poll: number | undefined

    const fallback = async () => {
      try {
        setLive(await getJson<Live>('/api/live'))
      } catch {
        /* backend fora do ar: o cabeçalho mostra o aviso */
      }
    }

    const open = () => {
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${proto}://${window.location.host}/ws`)
      socket.onopen = () => {
        setLinked(true)
        window.clearInterval(poll)
      }
      socket.onmessage = (ev) => setLive(JSON.parse(ev.data) as Live)
      socket.onclose = () => {
        setLinked(false)
        if (stopped) return
        window.clearInterval(poll)
        poll = window.setInterval(fallback, 5000)
        retry.current = window.setTimeout(open, 5000)
      }
    }
    fallback()
    open()
    return () => {
      stopped = true
      window.clearTimeout(retry.current)
      window.clearInterval(poll)
      socket?.close()
    }
  }, [])

  return { live, linked }
}

export function num(v: number | string | null | undefined): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}
