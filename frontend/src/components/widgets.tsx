import { AlertTriangle, ArrowDown, ArrowUp, Check, CircleSlash } from 'lucide-react'
import type { ReactNode } from 'react'
import type { Live, MachineState, Zone } from '../api'
import { num } from '../api'
import { fmt } from '../format'

const STATE_TEXT: Record<MachineState, string> = {
  produzindo: 'Produzindo',
  parada: 'Parada',
  alarme: 'Em alarme',
  offline: 'Sem conexão',
}

export function StateBadge({ state }: { state: MachineState }) {
  return (
    <span className={`state ${state}`} role="status">
      <span className="dot" aria-hidden="true" />
      {STATE_TEXT[state]}
    </span>
  )
}

export function Tile({ label, value, unit, sub, icon, alert }: {
  label: string; value: string; unit?: string; sub?: ReactNode; icon?: ReactNode; alert?: boolean
}) {
  return (
    <section className={`panel tile ${alert ? 'alert' : ''}`}>
      <div className="tile-label">{icon}{label}</div>
      <div className="tile-value">{value}{unit && <small>{unit}</small>}</div>
      {sub && <div className="tile-sub">{sub}</div>}
    </section>
  )
}

export const RANGES = [
  { label: '1 h', minutes: 60 },
  { label: '8 h', minutes: 480 },
  { label: '24 h', minutes: 1440 },
  { label: '7 dias', minutes: 10080 },
]

export function RangePicker({ value, onChange }: { value: number; onChange: (m: number) => void }) {
  return (
    <div className="segmented" role="group" aria-label="Período">
      {RANGES.map((r) => (
        <button key={r.minutes} aria-pressed={value === r.minutes} onClick={() => onChange(r.minutes)}>
          {r.label}
        </button>
      ))}
    </div>
  )
}

type ZoneState = 'ok' | 'baixa' | 'alta' | 'off'

export function zoneState(actual: number | null, sp: number | null, lo: number | null, hi: number | null): ZoneState {
  if (actual === null || sp === null || sp <= 0 || actual <= 0) return 'off'
  const below = lo && lo > 0 ? lo : 10
  const above = hi && hi > 0 ? hi : 10
  if (actual < sp - below) return 'baixa'
  if (actual > sp + above) return 'alta'
  return 'ok'
}

const ZONE_FLAG: Record<ZoneState, { text: string; icon: ReactNode }> = {
  ok: { text: 'Na faixa', icon: <Check size={14} aria-hidden="true" /> },
  baixa: { text: 'Abaixo da faixa', icon: <ArrowDown size={14} aria-hidden="true" /> },
  alta: { text: 'Acima da faixa', icon: <ArrowUp size={14} aria-hidden="true" /> },
  off: { text: 'Sem aquecimento', icon: <CircleSlash size={14} aria-hidden="true" /> },
}

/** O canhão desenhado da alimentação (zona 5) até o bico, cada zona com real × setpoint. */
export function Barrel({ zones, live }: { zones: Zone[]; live: Live | null }) {
  const ordered = [...zones].reverse() // Zona 5 (perto do funil) ... Zona 1, Bico
  const v = live?.values ?? {}
  return (
    <div className="barrel">
      <div className="hopper" aria-hidden="true">
        <svg width="34" height="30" viewBox="0 0 34 30"><path d="M2 2h30l-11 16v10h-8V18z" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" /></svg>
        Funil
      </div>
      <div className="zones">
        {ordered.map((z) => {
          const actual = num(v[z.actual])
          const sp = num(v[z.setpoint])
          const st = zoneState(actual, sp, num(v[z.min]), num(v[z.max]))
          const pct = actual && sp ? Math.min(100, (actual / sp) * 100) : 0
          return (
            <div key={z.label} className={`zone ${st}`}>
              <div className="zone-name">{z.label}</div>
              <div className="zone-temp">{fmt(actual, 0)}<small> °C</small></div>
              <div className="zone-sp">Programado {fmt(sp, 0)} °C</div>
              <div className="zone-meter" aria-hidden="true"><span style={{ width: `${pct}%` }} /></div>
              <div className="zone-flag">{ZONE_FLAG[st].icon}{ZONE_FLAG[st].text}</div>
            </div>
          )
        })}
      </div>
      <div className="nozzle" aria-hidden="true" />
    </div>
  )
}

export function AlarmList({ items }: { items: { tag: string; label: string; when?: string }[] }) {
  if (!items.length) {
    return <div className="ok-line"><Check size={18} aria-hidden="true" /> Nenhum alarme ativo agora.</div>
  }
  return (
    <ul className="alarm-list">
      {items.map((a) => (
        <li key={a.tag}>
          <AlertTriangle size={18} aria-hidden="true" />
          <span>{a.label}</span>
          {a.when && <span className="when">{a.when}</span>}
        </li>
      ))}
    </ul>
  )
}

export const CYCLE_PHASES: { tag: string; label: string; color: string }[] = [
  { tag: 'ClampLastTime', label: 'Fechamento', color: 'series-1' },
  { tag: 'InjectLastTime', label: 'Injeção', color: 'series-2' },
  { tag: 'HoldLastTime', label: 'Recalque', color: 'series-3' },
  { tag: 'PlastLastTime', label: 'Dosagem', color: 'series-4' },
  { tag: 'ColdLastTime', label: 'Resfriamento', color: 'series-5' },
  { tag: 'OpenLastTime', label: 'Abertura', color: 'series-6' },
  { tag: 'EjectLastTime', label: 'Extração', color: 'series-7' },
]

/** Composição do último ciclo, fase por fase. */
export function CycleBreakdown({ live, colors }: { live: Live | null; colors: Record<string, string> }) {
  const v = live?.values ?? {}
  const parts = CYCLE_PHASES.map((p) => ({ ...p, s: num(v[p.tag]) ?? 0 }))
  const total = parts.reduce((a, p) => a + p.s, 0)
  if (total <= 0) {
    return <div className="empty-chart" style={{ height: 120 }}>As fases do ciclo aparecem depois do primeiro ciclo completo.</div>
  }
  return (
    <div>
      <div className="cycle-bar" role="img" aria-label="Divisão do último ciclo por fase">
        {parts.filter((p) => p.s > 0).map((p) => (
          <div key={p.tag} title={`${p.label}: ${fmt(p.s, 2)} s`}
            style={{ flex: p.s, background: colors[p.color] }} />
        ))}
      </div>
      <div className="cycle-list">
        {parts.map((p) => (
          <div key={p.tag}>
            <i style={{ background: colors[p.color] }} />
            {p.label}
            <b>{fmt(p.s, 2)} s</b>
          </div>
        ))}
      </div>
    </div>
  )
}
