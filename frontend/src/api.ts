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

export type MachineKind = 'chy' | 'em63' | 'ads' | 'demo'

export interface MachineStatus {
  online: boolean | null
  latency_ms: number | null
  checked: number | null
  state?: string
}

export interface Machine {
  id: number
  client: string
  name: string
  kind: MachineKind
  host: string
  opcua_port: number
  namespace: number | null
  ams_net_id: string
  ads_port: number
  vnc_enabled: boolean
  vnc_port: number
  notes: string
  has_view_password: boolean
  has_control_password: boolean
  profile: 'chy' | 'em63' | 'ads'
  opcua_url: string
  status?: MachineStatus
}

/**
 * Campos enviados ao cadastrar/editar; senha em branco = não enviar (o servidor mantém a atual).
 * clear_* apaga a senha guardada. vnc_enabled null = padrão do tipo.
 */
export type MachineInput = Partial<Omit<Machine, 'id' | 'status' | 'has_view_password' | 'has_control_password' | 'profile' | 'opcua_url' | 'vnc_enabled'>> & {
  vnc_enabled?: boolean | null
  vnc_view_password?: string
  vnc_control_password?: string
  clear_vnc_view_password?: boolean
  clear_vnc_control_password?: boolean
}

export interface MachineList {
  active_id: number | null
  machines: Machine[]
}

export interface Info {
  machine_id?: number
  kind?: MachineKind
  client?: string
  machine_name: string
  opcua_url: string
  simulate: boolean
  profile?: 'chy' | 'em63' | 'ads'
  vnc?: boolean
  vnc_control?: boolean
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

export interface VncStatus {
  panel: { state: 'idle' | 'connecting' | 'connected' | 'error'; mode: 'view' | 'control'; error: string | null }
  viewers: number
  role: 'view' | 'control' | null
  control_available: boolean
  timeout: number
  controller: { me: boolean; peer: string; since: number | null; seconds_left: number } | null
  events?: { time: number; text: string }[]
}

export async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`O servidor respondeu ${res.status} em ${url}`)
  return res.json() as Promise<T>
}

/** Traduz o corpo de erro do FastAPI ({detail: texto} ou {detail: [{loc, msg}]}) em frases para a tela. */
async function errorMessages(res: Response): Promise<string[]> {
  try {
    const body = await res.json()
    if (typeof body?.detail === 'string') return [body.detail]
    if (Array.isArray(body?.detail)) {
      return body.detail.map((d: { loc?: unknown[]; msg?: string }) => {
        const campo = Array.isArray(d.loc) ? String(d.loc[d.loc.length - 1] ?? '') : ''
        return campo ? `${campo}: ${d.msg ?? 'valor inválido'}` : (d.msg ?? 'valor inválido')
      })
    }
  } catch {
    /* corpo vazio ou não-JSON */
  }
  return [`O servidor respondeu ${res.status}`]
}

export class ApiError extends Error {
  messages: string[]
  constructor(messages: string[]) {
    super(messages.join(' · '))
    this.messages = messages
  }
}

async function send<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(await errorMessages(res))
  return (res.status === 204 ? undefined : await res.json()) as T
}

export const listMachines = () => getJson<MachineList>('/api/machines')
export const saveMachine = (m: MachineInput, id?: number) =>
  id === undefined ? send<Machine>('POST', '/api/machines', m) : send<Machine>('PUT', `/api/machines/${id}`, m)
export const deleteMachine = (id: number) => send<void>('DELETE', `/api/machines/${id}`)
export const activateMachine = (id: number) => send<Machine>('POST', `/api/machines/${id}/activate`)
export const testMachine = (id: number) =>
  send<{ online: boolean; latency_ms: number | null }>('POST', `/api/machines/${id}/test`)

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
  return { data, error, reload: load, set: setData }
}

/** Valores ao vivo pelo WebSocket; se ele cair, tenta de novo e usa /api/live enquanto isso. */
export function useLive() {
  const [live, setLive] = useState<Live | null>(null)
  const [linked, setLinked] = useState(false)
  const [change, setChange] = useState<{ id: number; seq: number } | null>(null)
  const [openSeq, setOpenSeq] = useState(0)
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
        setOpenSeq((n) => n + 1)
        window.clearInterval(poll)
      }
      let changed = false
      socket.onmessage = (ev) => {
        const msg = JSON.parse(ev.data)
        if (msg.type === 'machine_changed') {
          changed = true
          setChange((c) => ({ id: msg.machine_id as number, seq: (c?.seq ?? 0) + 1 }))
        } else {
          setLive(msg as Live)
        }
      }
      socket.onclose = () => {
        setLinked(false)
        if (stopped) return
        window.clearInterval(poll)
        poll = window.setInterval(fallback, 5000)
        // Troca de máquina: o servidor fecha de propósito, então reconecta já no coletor novo.
        retry.current = window.setTimeout(open, changed ? 300 : 5000)
        if (changed) fallback()
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

  /** openSeq conta as aberturas do /ws: a partir da 2ª, é uma reconexão. */
  return { live, linked, changedTo: change?.id ?? null, changeSeq: change?.seq ?? 0, openSeq }
}

export function num(v: number | string | null | undefined): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}
