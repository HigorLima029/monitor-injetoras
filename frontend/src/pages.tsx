import { AlertTriangle, Gauge, Package, Timer } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { Alarms, History, Info, Live, Production, TagRow } from './api'
import { num, usePoll } from './api'
import { BucketBars, mergeSeries, TrendChart } from './components/charts'
import { AlarmList, Barrel, CycleBreakdown, RangePicker, Tile } from './components/widgets'
import { dateTime, duration, fmt, fmtValue } from './format'
import type { Tokens } from './theme'

export interface PageProps {
  info: Info
  live: Live | null
  tokens: Tokens
  now: number
}

function rangeText(minutes: number) {
  return minutes >= 1440 ? `${minutes / 1440} ${minutes === 1440 ? 'dia' : 'dias'}` : `${minutes / 60} h`
}

function useHistory(tags: string[], minutes: number) {
  const url = `/api/history?tags=${encodeURIComponent(tags.join(','))}&minutes=${minutes}`
  return usePoll<History>(url, minutes > 480 ? 60000 : 15000).data
}

function scrapPct(prod: Production | null) {
  if (!prod || prod.produced <= 0) return null
  return (prod.bad / prod.produced) * 100
}

/* ---- Visão geral --------------------------------------------------------- */
export function Overview({ info, live, tokens }: PageProps) {
  const prod = usePoll<Production>('/api/production?hours=8', 30000).data
  const alarms = usePoll<Alarms>('/api/alarms?hours=24', 15000).data
  const hist = useHistory(['CycleMonitor'], 480)
  const v = live?.values ?? {}
  const active = live?.status.active_alarms.length ?? 0
  const scrap = scrapPct(prod)
  const since = (tag: string) => alarms?.events.find((e) => e.tag === tag && e.ended === null)?.started

  return (
    <div className="stack">
      <div className="grid tiles">
        <Tile icon={<Package size={16} aria-hidden="true" />} label="Peças nas últimas 8 h"
          value={fmt(prod?.produced ?? null, 0)}
          sub={`Contador da máquina: ${fmt(num(v.ProductionCount), 0)}`} />
        <Tile icon={<Timer size={16} aria-hidden="true" />} label="Último ciclo"
          value={fmt(num(v.CycleMonitor), 1)} unit="s"
          sub={prod?.avg_cycle ? `Média em 8 h: ${fmt(prod.avg_cycle, 1)} s` : 'Sem ciclos registrados ainda'} />
        <Tile icon={<Gauge size={16} aria-hidden="true" />} label="Refugo em 8 h"
          value={fmt(scrap, 1)} unit={scrap === null ? undefined : '%'}
          sub={`${fmt(prod?.bad ?? null, 0)} peças ruins`} />
        <Tile icon={<AlertTriangle size={16} aria-hidden="true" />} label="Alarmes ativos"
          value={String(active)} alert={active > 0}
          sub={`${alarms?.monitored ?? '—'} alarmes monitorados`} />
      </div>

      <section className="panel">
        <div className="panel-head">
          <h2>Canhão</h2>
          <span className="note">Temperatura real de cada zona, da alimentação ao bico</span>
        </div>
        <Barrel zones={info.zones} live={live} />
      </section>

      <div className="grid two">
        <section className="panel">
          <div className="panel-head"><h2>Tempo de ciclo</h2><span className="right">últimas 8 h</span></div>
          <TrendChart tokens={tokens} unit="s"
            rows={mergeSeries(hist?.series, ['CycleMonitor'])}
            lines={[{ key: 'CycleMonitor', label: 'Ciclo', color: tokens['series-1'] }]} />
        </section>
        <section className="panel">
          <div className="panel-head"><h2>Peças por hora</h2><span className="right">últimas 8 h</span></div>
          <BucketBars tokens={tokens} data={prod?.per_bucket ?? []} label="Peças" unit="pç"
            color={tokens['series-1']} bucketSeconds={prod?.bucket_seconds ?? 3600} />
        </section>
      </div>

      <section className="panel">
        <div className="panel-head"><h2>Alarmes ativos</h2></div>
        <AlarmList items={(alarms?.active ?? []).map((a) => {
          const s = since(a.tag)
          return { ...a, when: s ? `desde ${dateTime(s)}` : undefined }
        })} />
      </section>
    </div>
  )
}

/* ---- Temperaturas -------------------------------------------------------- */
export function Temperatures({ info, live, tokens }: PageProps) {
  const [minutes, setMinutes] = useState(60)
  const tags = useMemo(() => info.zones.flatMap((z) => [z.actual, z.setpoint]).concat(['TempOilNow', 'TempOil']), [info])
  const hist = useHistory(tags, minutes)
  const v = live?.values ?? {}

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Temperaturas</h1>
          <p>A faixa sombreada é a tolerância programada na máquina em torno do setpoint.</p>
        </div>
        <span className="spacer" />
        <RangePicker value={minutes} onChange={setMinutes} />
      </div>
      <section className="panel"><Barrel zones={info.zones} live={live} /></section>
      <div className="grid three">
        {info.zones.map((z) => {
          const sp = num(v[z.setpoint])
          const lo = num(v[z.min]) || 10
          const hi = num(v[z.max]) || 10
          return (
            <section className="panel" key={z.label}>
              <div className="panel-head">
                <h3>{z.label}</h3>
                <span className="right num">{fmt(num(v[z.actual]), 1)} °C</span>
              </div>
              <TrendChart tokens={tokens} unit="°C" size="short"
                rows={mergeSeries(hist?.series, [z.actual, z.setpoint])}
                band={sp && sp > 0 ? [sp - lo, sp + hi] : null}
                lines={[
                  { key: z.actual, label: 'Real', color: tokens['series-1'] },
                  { key: z.setpoint, label: 'Programado', color: tokens.setpoint, dashed: true },
                ]} />
              <div className="legend">
                <span><i style={{ background: tokens['series-1'] }} />Real</span>
                <span><i className="dash" />Programado</span>
              </div>
            </section>
          )
        })}
        <section className="panel">
          <div className="panel-head"><h3>Óleo hidráulico</h3>
            <span className="right num">{fmt(num(v.TempOilNow), 1)} °C</span></div>
          <TrendChart tokens={tokens} unit="°C" size="short"
            rows={mergeSeries(hist?.series, ['TempOilNow', 'TempOil'])}
            lines={[
              { key: 'TempOilNow', label: 'Real', color: tokens['series-1'] },
              { key: 'TempOil', label: 'Programado', color: tokens.setpoint, dashed: true },
            ]} />
          <div className="legend">
            <span><i style={{ background: tokens['series-1'] }} />Real</span>
            <span><i className="dash" />Programado</span>
          </div>
        </section>
      </div>
      <p className="muted small">Período: últimas {rangeText(minutes)}.</p>
    </div>
  )
}

/* ---- Produção ------------------------------------------------------------ */
export function ProductionPage({ live, tokens }: PageProps) {
  const [minutes, setMinutes] = useState(480)
  const prod = usePoll<Production>(`/api/production?hours=${minutes / 60}`, 30000).data
  const hist = useHistory(['CycleMonitor', 'InjectMaxPress', 'InjectEndLastPos'], minutes)
  const v = live?.values ?? {}
  const target = num(v.TargetProductionCount)
  const count = num(v.ProductionCount)
  const scrap = scrapPct(prod)
  const colors = tokens as unknown as Record<string, string>

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Produção</h1>
          <p>Contagens calculadas a partir do contador da máquina lido pelo gateway.</p>
        </div>
        <span className="spacer" />
        <RangePicker value={minutes} onChange={setMinutes} />
      </div>
      <div className="grid tiles">
        <Tile label={`Peças em ${rangeText(minutes)}`} value={fmt(prod?.produced ?? null, 0)}
          sub={`${fmt(prod ? prod.produced - prod.bad : null, 0)} boas`} />
        <Tile label="Refugo" value={fmt(scrap, 1)} unit={scrap === null ? undefined : '%'}
          sub={`${fmt(prod?.bad ?? null, 0)} peças ruins`} />
        <Tile label="Ciclo médio" value={fmt(prod?.avg_cycle ?? null, 1)} unit="s"
          sub={`Último: ${fmt(num(v.CycleMonitor), 1)} s`} />
        <Tile label="Meta da ordem" value={target && target > 0 ? `${fmt(((count ?? 0) / target) * 100, 0)}` : '—'}
          unit={target && target > 0 ? '%' : undefined}
          sub={target && target > 0 ? `${fmt(count, 0)} de ${fmt(target, 0)} peças` : 'Nenhuma meta programada na máquina'} />
      </div>

      <div className="grid two">
        <section className="panel">
          <div className="panel-head"><h2>Peças por intervalo</h2>
            <span className="right">{prod?.bucket_seconds === 600 ? 'a cada 10 min' : 'por hora'}</span></div>
          <BucketBars tokens={tokens} data={prod?.per_bucket ?? []} label="Peças" unit="pç"
            color={tokens['series-1']} bucketSeconds={prod?.bucket_seconds ?? 3600} />
        </section>
        <section className="panel">
          <div className="panel-head"><h2>Último ciclo, fase por fase</h2>
            <span className="right num">{fmt(num(v.CycleMonitor), 1)} s no total</span></div>
          <CycleBreakdown live={live} colors={colors} />
        </section>
      </div>

      <div className="grid three">
        <section className="panel">
          <div className="panel-head"><h3>Tempo de ciclo</h3></div>
          <TrendChart tokens={tokens} unit="s" size="short" rows={mergeSeries(hist?.series, ['CycleMonitor'])}
            lines={[{ key: 'CycleMonitor', label: 'Ciclo', color: tokens['series-1'] }]} />
        </section>
        <section className="panel">
          <div className="panel-head"><h3>Pressão máxima de injeção</h3></div>
          <TrendChart tokens={tokens} unit="bar" size="short" rows={mergeSeries(hist?.series, ['InjectMaxPress'])}
            lines={[{ key: 'InjectMaxPress', label: 'Pressão', color: tokens['series-1'] }]} />
        </section>
        <section className="panel">
          <div className="panel-head"><h3>Colchão</h3></div>
          <TrendChart tokens={tokens} unit="mm" size="short" digits={2} tickDigits={1}
            rows={mergeSeries(hist?.series, ['InjectEndLastPos'])}
            lines={[{ key: 'InjectEndLastPos', label: 'Colchão', color: tokens['series-1'] }]} />
        </section>
      </div>
    </div>
  )
}

/* ---- Alarmes ------------------------------------------------------------- */
export function AlarmsPage({ now }: PageProps) {
  const [minutes, setMinutes] = useState(1440)
  const alarms = usePoll<Alarms>(`/api/alarms?hours=${minutes / 60}`, 10000).data
  const events = alarms?.events ?? []
  const ranking = useMemo(() => {
    const m = new Map<string, { label: string; n: number; secs: number }>()
    for (const e of events) {
      const r = m.get(e.tag) ?? { label: e.label, n: 0, secs: 0 }
      r.n += 1
      r.secs += (e.ended ?? now) - e.started
      m.set(e.tag, r)
    }
    return [...m.values()].sort((a, b) => b.secs - a.secs).slice(0, 8)
  }, [events, now])
  const maxSecs = Math.max(1, ...ranking.map((r) => r.secs))

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Alarmes</h1>
          <p>Cada alarme da máquina vira um evento com início e fim, registrado enquanto este monitor estiver ligado.</p>
        </div>
        <span className="spacer" />
        <RangePicker value={minutes} onChange={setMinutes} />
      </div>
      <section className="panel">
        <div className="panel-head"><h2>Ativos agora</h2></div>
        <AlarmList items={alarms?.active ?? []} />
      </section>
      <div className="grid two">
        <section className="panel">
          <div className="panel-head"><h2>Mais tempo parado por alarme</h2><span className="right">{rangeText(minutes)}</span></div>
          {!ranking.length ? (
            <div className="empty-chart" style={{ height: 120 }}>Nenhum alarme registrado neste período.</div>
          ) : (
            <table>
              <thead><tr><th>Alarme</th><th className="right">Vezes</th><th className="right">Tempo total</th></tr></thead>
              <tbody>
                {ranking.map((r) => (
                  <tr key={r.label}>
                    <td>
                      {r.label}
                      <div className="zone-meter"><span style={{ width: `${(r.secs / maxSecs) * 100}%`, background: 'var(--critical)' }} /></div>
                    </td>
                    <td className="value">{r.n}</td>
                    <td className="value">{duration(r.secs)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
        <section className="panel">
          <div className="panel-head"><h2>Histórico</h2><span className="right">{events.length} eventos</span></div>
          {!events.length ? (
            <div className="empty-chart" style={{ height: 120 }}>Nenhum alarme registrado neste período.</div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Alarme</th><th>Início</th><th className="right">Duração</th></tr></thead>
                <tbody>
                  {events.map((e) => (
                    <tr key={`${e.tag}-${e.started}`}>
                      <td>{e.label}</td>
                      <td className="num">{dateTime(e.started)}</td>
                      <td className="value">{e.ended === null
                        ? <span className="pill-active">ativo há {duration(now - e.started)}</span>
                        : duration(e.ended - e.started)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

/* ---- Variáveis ----------------------------------------------------------- */
export function Variables(_: PageProps) {
  const [q, setQ] = useState('')
  const [nonZero, setNonZero] = useState(false)
  const data = usePoll<{ total: number; rows: TagRow[] }>(`/api/tags?q=${encodeURIComponent(q)}`, 10000).data
  const rows = (data?.rows ?? []).filter((r) => !nonZero || (r.value !== 0 && r.value !== null && r.value !== ''))

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Variáveis do gateway</h1>
          <p>Tudo o que o servidor OPC-UA do gateway publica. Os valores são só lidos, nunca alterados.</p>
        </div>
      </div>
      <section className="panel">
        <div className="toolbar">
          <label className="field">
            Buscar pelo nome
            <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="ex.: Temp, Inject, Warn" />
          </label>
          <label className="check">
            <input type="checkbox" checked={nonZero} onChange={(e) => setNonZero(e.target.checked)} />
            Mostrar só valores diferentes de zero
          </label>
          <span className="muted small" style={{ marginLeft: 'auto' }}>
            {rows.length} de {data?.total ?? '—'} variáveis
          </span>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Nome no gateway</th><th>Descrição</th><th className="right">Valor</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.tag} className={r.value ? 'nonzero' : ''}>
                  <td className="tag">{r.tag}</td>
                  <td>{r.label}</td>
                  <td className="value">{fmtValue(r.value)}{r.unit ? ` ${r.unit}` : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
