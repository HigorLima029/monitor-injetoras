import {
  Bar, BarChart, CartesianGrid, ComposedChart, Line, ReferenceArea,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import type { Point } from '../api'
import { clock, fmt } from '../format'
import type { Tokens } from '../theme'

export interface LineSpec {
  key: string
  label: string
  color: string
  dashed?: boolean
}

type Row = { t: number } & Record<string, number | null | undefined>

/** Junta várias séries [[ts, v]] numa tabela por timestamp. */
export function mergeSeries(series: Record<string, Point[]> | undefined, keys: string[]): Row[] {
  const rows = new Map<number, Row>()
  for (const k of keys) {
    for (const [t, v] of series?.[k] ?? []) {
      const row = rows.get(t) ?? { t }
      row[k] = v
      rows.set(t, row)
    }
  }
  return [...rows.values()].sort((a, b) => a.t - b.t)
}

function hasData(rows: Row[], keys: string[]) {
  return rows.some((r) => keys.some((k) => typeof r[k] === 'number'))
}

interface TipProps {
  active?: boolean
  label?: number
  payload?: { dataKey?: string | number; value?: number | null }[]
  lines: LineSpec[]
  unit: string
  digits: 0 | 1 | 2
}

function Tip({ active, label, payload, lines, unit, digits }: TipProps) {
  if (!active || !payload?.length || label === undefined) return null
  return (
    <div className="tooltip">
      <div className="t">{clock(label)}</div>
      {lines.map((l) => {
        const p = payload.find((x) => x.dataKey === l.key)
        if (!p) return null
        return (
          <div className="row" key={l.key}>
            <span className="sw" style={{ background: l.color }} />
            {l.label}: <b>{fmt(p.value ?? null, digits)} {unit}</b>
          </div>
        )
      })}
    </div>
  )
}

export function Empty({ text }: { text: string }) {
  return <div className="empty-chart">{text}</div>
}

interface TrendProps {
  rows: Row[]
  lines: LineSpec[]
  unit: string
  tokens: Tokens
  size?: 'short' | 'tall'
  digits?: 0 | 1 | 2
  band?: [number, number] | null
  emptyText?: string
  tickDigits?: 0 | 1 | 2
}

/** Linha no tempo com crosshair + tooltip. Setpoint vem como linha tracejada. */
export function TrendChart({ rows, lines, unit, tokens, size, digits = 1, band, emptyText, tickDigits = 0 }: TrendProps) {
  const keys = lines.map((l) => l.key)
  return (
    <div className={`chart ${size ?? ''}`}>
      {!hasData(rows, keys) ? (
        <Empty text={emptyText ?? 'Ainda sem leituras neste período. Os pontos aparecem conforme o gateway responde.'} />
      ) : (
        <ResponsiveContainer>
          <ComposedChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: -8 }}>
            <CartesianGrid vertical={false} stroke={tokens.grid} />
            <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} scale="time"
              tickFormatter={clock} stroke={tokens.rule} tickLine={false} minTickGap={40} />
            <YAxis stroke={tokens.rule} tickLine={false} axisLine={false} width={48}
              domain={['auto', 'auto']} tickFormatter={(v: number) => fmt(v, tickDigits)} />
            {band && (
              <ReferenceArea y1={band[0]} y2={band[1]} fill={tokens.band} stroke="none" ifOverflow="extendDomain" />
            )}
            <Tooltip
              cursor={{ stroke: tokens['ink-3'], strokeWidth: 1 }}
              content={<Tip lines={lines} unit={unit} digits={digits} />}
              isAnimationActive={false}
            />
            {lines.map((l) =>
              l.dashed ? (
                <Line key={l.key} dataKey={l.key} stroke={l.color} strokeWidth={1.5} strokeDasharray="5 4"
                  dot={false} isAnimationActive={false} connectNulls type="stepAfter" />
              ) : (
                <Line key={l.key} dataKey={l.key} stroke={l.color} strokeWidth={2} dot={false}
                  activeDot={{ r: 4, strokeWidth: 2, stroke: tokens.panel }} isAnimationActive={false}
                  connectNulls type="monotone" />
              ),
            )}
          </ComposedChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}

interface BarsProps {
  data: [number, number][]
  label: string
  unit: string
  color: string
  tokens: Tokens
  bucketSeconds: number
}

/** Barras por intervalo (ex.: peças por hora). */
export function BucketBars({ data, label, unit, color, tokens, bucketSeconds }: BarsProps) {
  const rows = data.map(([t, v]) => ({ t, v }))
  const fmtBucket = (t: number) => `${clock(t)}–${clock(t + bucketSeconds)}`
  return (
    <div className="chart">
      {!rows.some((r) => r.v > 0) ? (
        <Empty text="Nenhuma peça contada neste período. As barras aparecem quando a máquina produzir." />
      ) : (
        <ResponsiveContainer>
          <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: -8 }} barCategoryGap={2}>
            <CartesianGrid vertical={false} stroke={tokens.grid} />
            <XAxis dataKey="t" tickFormatter={clock} stroke={tokens.rule} tickLine={false} minTickGap={30} />
            <YAxis stroke={tokens.rule} tickLine={false} axisLine={false} width={48} allowDecimals={false} />
            <Tooltip
              cursor={{ fill: tokens.grid }}
              isAnimationActive={false}
              content={({ active, payload }) => {
                const p = payload?.[0]
                if (!active || !p) return null
                const row = p.payload as { t: number; v: number }
                return (
                  <div className="tooltip">
                    <div className="t">{fmtBucket(row.t)}</div>
                    {label}: <b>{fmt(row.v, 0)} {unit}</b>
                  </div>
                )
              }}
            />
            <Bar dataKey="v" fill={color} radius={[4, 4, 0, 0]} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
