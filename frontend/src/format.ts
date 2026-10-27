const nf0 = new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 0 })
const nf1 = new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 1, minimumFractionDigits: 1 })
const nf2 = new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 2 })

export function fmt(v: number | null | undefined, digits: 0 | 1 | 2 = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return (digits === 0 ? nf0 : digits === 1 ? nf1 : nf2).format(v)
}

export function fmtValue(v: number | string | null | undefined): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'string') return v || '(vazio)'
  return Number.isInteger(v) ? nf0.format(v) : nf2.format(v)
}

export function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })
}

export function dateTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString('pt-BR', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} min ${s % 60} s`
  const h = Math.floor(m / 60)
  return `${h} h ${m % 60} min`
}

export function ago(ts: number | null, now: number): string {
  if (!ts) return 'nunca'
  const s = Math.max(0, now - ts)
  if (s < 5) return 'agora'
  return `há ${duration(s)}`
}
