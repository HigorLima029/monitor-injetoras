/**
 * Réplica simulada das telas do painel da Spark (painel ESTO, servos Inovance), montada a partir de
 * fotos do painel real (em português) e alimentada pelo OPC-UA do próprio painel (INO.<nome>).
 *
 * Só exibe valores: nenhum campo é editável e nada é enviado à máquina.
 * Campos que o painel não publica no OPC-UA aparecem vazios ou como "—".
 */
import {
  AlertTriangle, ArrowLeftToLine, Columns2, Gauge, Hand, Home, ListChecks, Thermometer, Workflow, Zap,
} from 'lucide-react'
import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { Alarms, Status } from '../api'
import { getJson, num, usePoll } from '../api'
import './esto.css'

type Values = Record<string, number | string | null>
const ValuesCtx = createContext<Values>({})
const NOT_PUBLISHED = 'O painel não publica este dado no OPC-UA'

/**
 * Fator para converter o valor cru do OPC-UA na unidade da tela (padrão 1).
 * Conferido com fotos do painel real (26/11): posições e tempos /100, velocidades /10,
 * pressões e rpm sem conversão, Tp Ciclo/Prod Per /10. Os "Real" seguem o mesmo padrão
 * dos programados (confirmar com a máquina ciclando).
 */
const n1to = (pre: string, a: number, b: number, suf: string) => Array.from({ length: b - a + 1 }, (_, i) => `${pre}${a + i}${suf}`)
const SCALE: Record<string, number> = Object.fromEntries([
  ...[...n1to('ClosePos#', 1, 3, ' Set'), 'CloseLowPos Set', ...n1to('OpenPos#', 1, 4, ' Set'), 'OpenPosEnd Set',
    ...n1to('InjEndPos#', 1, 4, ' Set'), 'TurnHoldPos Set', ...n1to('ChargeEndPos#', 1, 3, ' Set'), ...n1to('HoldTm#', 1, 4, ' Set'),
    'InjTm Set', 'Cool TimeL Set', 'SuckBackDistance Set', 'Charge After InjPos Set',
    'Inj Tm Real', 'Charge Tm Real', 'Close Tm Real', 'Open Tm Real', 'Eject Tm Real', 'NozAdv Tm Real', 'NozRet Tm Real',
    'SuckBack Time Real', 'RestmillPos Real', 'InjStartPos Real', 'HoldStartPos Real', 'Charge Pos Real', 'Open EndPos Real',
    'SuckBack EndPos Real', 'Hold MoveDist Real', 'Inject Min Pos'].map((n) => [n, 0.01]),
  ...[...n1to('CloseSpd#', 1, 3, ' Set'), 'CloseLowSpd Set', 'CloseHiSpd Set', ...n1to('OpenSpd#', 1, 4, ' Set'), 'OpenSpdEnd Set',
    ...n1to('InjSpd#', 1, 5, ' Set'), ...n1to('HoldSpd#', 1, 4, ' Set'), 'SuckBackSpd Set', 'Charge After InjSpd Set',
    ...n1to('EjtAdvPos#', 1, 2, ' Set'), ...n1to('EjtRetPos#', 1, 2, ' Set'), 'CycTime Real', 'ProdTm Real',
    'TurnHold Spd Real', 'Inj Avg Spd Real', 'Inject Max Speed'].map((n) => [n, 0.1]),
  ...['HiPrsLockProtTm Set', 'CloseLowPrs Tm Real', 'CloseHiPrs Tm Real'].map((n) => [n, 0.001]),
])

const ino = (name: string) => `INO.${name}`

function fmt(v: number | null | undefined, d: number) {
  if (v === null || v === undefined) return '—'
  return v.toFixed(d)
}

function scaled(values: Values, name: string): number | null {
  const raw = num(values[ino(name)])
  return raw === null ? null : raw * (SCALE[name] ?? 1)
}

function useVal(name: string | null): number | null {
  const values = useContext(ValuesCtx)
  return name === null ? null : scaled(values, name)
}

/** Caixa de valor como no painel. name=null => campo existe na tela mas não vem do OPC-UA. */
function F({ name, d = 2 }: { name: string | null; d?: number }) {
  const v = useVal(name)
  return (
    <span className={`e-f ${name === null ? 'na' : ''}`} title={name ?? NOT_PUBLISHED}>
      {name === null ? '' : fmt(v, d)}
    </span>
  )
}

/** Texto simples (sem caixa), usado no cabeçalho e nos indicadores. */
function T({ name, d = 2 }: { name: string | null; d?: number }) {
  const v = useVal(name)
  return <b title={name ?? NOT_PUBLISHED}>{name === null ? '—' : fmt(v, d)}</b>
}

const range = (a: number, b: number) => Array.from({ length: b - a + 1 }, (_, i) => a + i)

/** end=true: linha de posição final; o último estágio em uso termina na comutação e fica apagado. */
interface GridRow { label: string; names: (string | null)[]; d?: number; end?: boolean }
/** Quantos estágios estão em uso (variável do painel) e o estágio de cada coluna (null = sempre visível). */
interface Seg { name: string; stage: (number | null)[] }

/** Grade "rótulo + colunas de estágio", como as tabelas Fecha/Abri/Injeca do painel. */
function Grid({ title, cols, rows, side, seg }: { title: string; cols: string[]; rows: GridRow[]; side?: ReactNode; seg?: Seg }) {
  const values = useContext(ValuesCtx)
  const used = seg ? num(values[ino(seg.name)]) : null
  // Estágio acima do número em uso fica apagado, como no painel.
  const off = (i: number, end?: boolean) => {
    if (used === null || seg === undefined || seg.stage[i] === null) return false
    const st = seg.stage[i] as number
    return end ? st >= used : st > used
  }
  return (
    <section className="e-sec e-gridsec">
      <h3 className="e-tag">{title}</h3>
      <div className="e-gridwrap">
        <table className="e-grid">
          <thead><tr><th />{cols.map((c, i) => <th key={`${c}-${i}`}>{c}</th>)}</tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.label}>
                <th scope="row">{r.label}</th>
                {r.names.map((n, i) => <td key={i}><F name={off(i, r.end) ? null : n} d={r.d ?? 2} /></td>)}
              </tr>
            ))}
          </tbody>
        </table>
        {side && <div className="e-side">{side}</div>}
      </div>
    </section>
  )
}

function Pair({ label, name, unit, d = 2 }: { label: string; name: string | null; unit?: string; d?: number }) {
  return (
    <div className="e-pair">
      {label && <span>{label}</span>}<F name={name} d={d} />{unit && <i>{unit}</i>}
    </div>
  )
}

/** Faixa de indicadores (Temp Fech, Temp Aber, CrossHd Pos...). */
function Stats({ items }: { items: { label: string; name: string | null; unit: string; d?: number }[] }) {
  return (
    <div className="e-stats">
      {items.map((s) => (
        <div key={s.label}><T name={s.name} d={s.d ?? 2} /><span>{s.label}</span><i>{s.unit}</i></div>
      ))}
    </div>
  )
}

/* ---- dados das telas ------------------------------------------------------- */

const TEMP_SETS = range(1, 7).map((z) => `Temp${z} Set`)
const TEMP_REALS = range(1, 7).map((z) => `Temp${z} Real`)

const CLOSE_ROWS: GridRow[] = [
  { label: 'FinPos(mm)', names: ['ClosePos#1 Set', 'ClosePos#2 Set', 'ClosePos#3 Set', 'CloseLowPos Set', null] },
  { label: 'Veloc(%)', names: ['CloseSpd#1 Set', 'CloseSpd#2 Set', 'CloseSpd#3 Set', 'CloseLowSpd Set', 'CloseHiSpd Set'], d: 1 },
  { label: 'Max Temp(s)', names: [null, null, null, 'HiPrsLockProtTm Set', null], d: 3 },
]
const OPEN_ROWS: GridRow[] = [
  { label: 'FinPos(mm)', names: ['OpenPosEnd Set', 'OpenPos#4 Set', 'OpenPos#3 Set', 'OpenPos#2 Set', 'OpenPos#1 Set'] },
  { label: 'Veloc(%)', names: ['OpenSpdEnd Set', 'OpenSpd#4 Set', 'OpenSpd#3 Set', 'OpenSpd#2 Set', 'OpenSpd#1 Set'], d: 1 },
]
const INJ_ROWS: GridRow[] = [
  { label: 'PosFim(mm)', names: ['TurnHoldPos Set', null, 'InjEndPos#4 Set', 'InjEndPos#3 Set', 'InjEndPos#2 Set', 'InjEndPos#1 Set'], end: true },
  { label: 'Veloc(mm/s)', names: [null, 'InjSpd#5 Set', 'InjSpd#4 Set', 'InjSpd#3 Set', 'InjSpd#2 Set', 'InjSpd#1 Set'], d: 1 },
  // O painel mostra uma pressão só (limite da injeção), na coluna #1.
  { label: 'Press(bar)', names: [null, null, null, null, null, 'InjPrs#1 Set'], d: 0 },
]
const HOLD_ROWS: GridRow[] = [
  { label: 'Temp(s)', names: range(1, 4).reverse().map((n) => `HoldTm#${n} Set`) },
  { label: 'Press(bar)', names: range(1, 4).reverse().map((n) => `HoldPrs#${n} Set`), d: 0 },
  // O painel mostra uma velocidade só no recalque, na coluna #1.
  { label: 'Veloc(mm/s)', names: [null, null, null, 'HoldSpd#1 Set'], d: 1 },
]
const DOS_ROWS: GridRow[] = [
  { label: 'Pos(mm)', names: range(1, 3).map((n) => `ChargeEndPos#${n} Set`) },
  { label: 'BkPr.(bar)', names: range(1, 3).map((n) => `ChargeBackPrs#${n} Set`), d: 0 },
  { label: 'Vlc(rpm)', names: range(1, 3).map((n) => `ChargeSpd#${n} Set`), d: 0 },
]
const EJ_ADV_ROWS: GridRow[] = [
  { label: 'FinPos(mm)', names: ['EjtAdvPos#1 Set', 'EjtAdvPos#2 Set'], d: 1 },
  { label: 'Veloc(%)', names: ['EjtAdvSpd#1 Set', 'EjtAdvSpd#2 Set'], d: 0 },
  { label: 'Press(bar)', names: [null, null], d: 0 },
]
const EJ_RET_ROWS: GridRow[] = [
  { label: 'FinPos(mm)', names: ['EjtRetPos#2 Set', 'EjtRetPos#1 Set'], d: 1 },
  { label: 'Veloc(%)', names: ['EjtRetSpd#2 Set', 'EjtRetSpd#1 Set'], d: 0 },
  { label: 'Press(bar)', names: [null, null], d: 0 },
]
const INJ_SEG: Seg = { name: 'Inject Segments Set', stage: [null, 5, 4, 3, 2, 1] }
const HOLD_SEG: Seg = { name: 'Hold Segments Set', stage: [4, 3, 2, 1] }
const DOS_SEG: Seg = { name: 'Charge Segments Set', stage: [1, 2, 3] }
const CLOSE_SEG: Seg = { name: 'Close Segments Set', stage: [1, 2, 3, null, null] }
const OPEN_SEG: Seg = { name: 'Open Segments Set', stage: [null, 4, 3, 2, 1] }
const EJ_ADV_SEG: Seg = { name: 'EjeAdv Segments Set', stage: [1, 2] }
const EJ_RET_SEG: Seg = { name: 'EjeRet Segments Set', stage: [2, 1] }
const SEG_NAMES = [INJ_SEG, HOLD_SEG, DOS_SEG, CLOSE_SEG, OPEN_SEG, EJ_ADV_SEG, EJ_RET_SEG].map((g) => g.name)
const rowNames = (rows: GridRow[]) => rows.flatMap((r) => r.names).filter((n): n is string => n !== null)

/** Ações do ciclo na ordem do gráfico Timp(s) da Vista geral. */
const ACTIONS: { label: string; name: string; icon: ReactNode }[] = [
  { label: 'Fechamento', name: 'Close Tm Real', icon: <Columns2 size={16} /> },
  { label: 'Avanço do bico', name: 'NozAdv Tm Real', icon: <ArrowLeftToLine size={16} /> },
  { label: 'Injeção', name: 'Inj Tm Real', icon: <Zap size={16} /> },
  { label: 'Dosagem', name: 'Charge Tm Real', icon: <Workflow size={16} /> },
  { label: 'Descompressão', name: 'SuckBack Time Real', icon: <Workflow size={16} /> },
  { label: 'Recuo do bico', name: 'NozRet Tm Real', icon: <ArrowLeftToLine size={16} className="flip" /> },
  { label: 'Abertura', name: 'Open Tm Real', icon: <Columns2 size={16} /> },
  { label: 'Extrator', name: 'Eject Tm Real', icon: <Gauge size={16} /> },
]

/** Colunas do Monitor de produção (Moni1 do painel). */
const MONI_COLS: { label: string; name: string; d: number }[] = [
  { label: 'ProdTemp', name: 'ProdTm Real', d: 1 },
  { label: 'TempInj', name: 'Inj Tm Real', d: 2 },
  { label: 'TempDos', name: 'Charge Tm Real', d: 2 },
  { label: 'PosFimInjec', name: 'RestmillPos Real', d: 2 },
  { label: 'TmPr.BxFch', name: 'CloseLowPrs Tm Real', d: 3 },
]

/* ---- blocos ---------------------------------------------------------------- */

/** Barras de aquecimento (Temp Aquec): traços acesos conforme real/programado. */
function Heaters() {
  const values = useContext(ValuesCtx)
  return (
    <div className="e-heat">
      <div className="e-heat-legend" aria-hidden="true">
        <span className="err">Erro</span><span className="fast">Fast</span><span className="pid">PID</span>
        <span className="stop">Stop</span><span className="dis">Disabi</span>
      </div>
      {range(1, 7).map((z) => {
        const real = scaled(values, `Temp${z} Real`)
        const set = scaled(values, `Temp${z} Set`)
        const on = set !== null && set > 0
        const lit = on && real !== null ? Math.max(0, Math.min(5, Math.round((real / set) * 5))) : 0
        return (
          <div className={`e-zone ${on ? 'on' : ''}`} key={z} title={`Temp${z}: real ${fmt(real, 0)} / programado ${fmt(set, 0)} °C`}>
            <div className="e-ticks">{range(1, 5).reverse().map((i) => <i key={i} className={i <= lit ? 'lit' : ''} />)}</div>
            <b>{real === null ? '—' : Math.round(real)}</b>
          </div>
        )
      })}
    </div>
  )
}

/** Gráfico de tempos das ações do último ciclo (Timp(s) da Vista geral). */
function Timeline() {
  const values = useContext(ValuesCtx)
  const times = ACTIONS.map((a) => scaled(values, a.name))
  const total = Math.max(times.reduce<number>((s, t) => s + (t ?? 0), 0), 0.001)
  let start = 0
  return (
    <section className="e-sec">
      <div className="e-time">
        <div className="e-time-left">
          <span className="e-h">Timp(s)</span>
          <span>Tp Ciclo</span><em><T name="CycTime Real" d={1} /> s</em>
          <span>Prod Per</span><em><T name="ProdTm Real" d={1} /> s</em>
        </div>
        <div className="e-time-rows">
          {ACTIONS.map((a, i) => {
            const t = times[i] ?? 0
            const left = (start / total) * 100
            const width = t > 0 ? Math.max((t / total) * 100, 1) : 0
            start += t
            return (
              <div className="e-trow" key={a.label} title={`${a.label} (${a.name})`}>
                <span className="ic" aria-label={a.label}>{a.icon}</span>
                <span className="bar"><i style={{ left: `${left}%`, width: `${width}%` }} /></span>
                <span className="v">{fmt(times[i], 2)} s</span>
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}

function MachineDrawing() {
  // Silhueta genérica de injetora elétrica com joelheira (não é a arte do fabricante).
  return (
    <svg className="e-machine" viewBox="0 0 400 120" aria-hidden="true">
      <rect x="8" y="54" width="40" height="10" rx="2" />
      <rect x="44" y="26" width="18" height="66" rx="2" />
      <path d="M62 36 L95 59 L62 82 M95 36 L62 59 L95 82" fill="none" strokeWidth="5" className="toggle" />
      <rect x="95" y="24" width="16" height="70" rx="2" />
      <rect x="111" y="32" width="30" height="54" className="mold" />
      <rect x="141" y="32" width="30" height="54" className="mold" />
      <rect x="171" y="22" width="18" height="74" rx="2" />
      <rect x="44" y="30" width="145" height="4" />
      <rect x="44" y="84" width="145" height="4" />
      <path d="M195 58 l14 -5 v12 z" className="barrel" />
      <rect x="209" y="50" width="95" height="18" rx="3" className="barrel" />
      <path d="M262 50 l-8 -22 h28 l-8 22 z" />
      <rect x="304" y="40" width="60" height="38" rx="4" />
      <rect x="200" y="96" width="190" height="8" rx="2" />
    </svg>
  )
}

/* ---- telas ---------------------------------------------------------------- */

function PrincScreen() {
  const [tab, setTab] = useState<'maq' | 'io' | 'servo'>('maq')
  return (
    <>
      <Timeline />
      <section className="e-sec">
        <h3 className="e-h">Temp Aquec (°C)</h3>
        <Heaters />
      </section>
      <section className="e-sec e-state">
        {tab === 'maq' ? (
          <div className="e-maq">
            <div className="e-gauges">
              <div className="e-gauge" title={NOT_PUBLISHED}><Gauge size={40} aria-hidden="true" /><b>—</b><span>ton</span></div>
              <div className="e-gauge" title="InjPrs Sensor Real"><Gauge size={40} aria-hidden="true" /><T name="InjPrs Sensor Real" d={0} /><span>bar</span></div>
            </div>
            <MachineDrawing />
          </div>
        ) : (
          <p className="e-na">
            {tab === 'io' ? 'Os estados de entrada e saída (portas, fins de curso)' : 'Os dados dos servos (corrente, torque, temperatura)'}
            {' '}não são publicados pelo painel no OPC-UA.
          </p>
        )}
        <div className="e-subtabs" role="tablist">
          {([['maq', 'Estad. Maq.'], ['io', 'I/O Statu'], ['servo', 'Estad.Servo']] as const).map(([id, label]) => (
            <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? 'on' : ''} onClick={() => setTab(id)}>{label}</button>
          ))}
        </div>
      </section>
    </>
  )
}

function FechScreen() {
  return (
    <>
      <Grid title="Fcha" cols={['#1', '#2', '#3', 'BxPrs', 'A.Pr.']} rows={CLOSE_ROWS} seg={CLOSE_SEG} />
      <Grid title="Abri" cols={['Fim', '#4', '#3', '#2', '#1']} rows={OPEN_ROWS} seg={OPEN_SEG} />
      <Stats items={[
        { label: 'Temp Fech', name: 'Close Tm Real', unit: 's' },
        { label: 'Temp Aber', name: 'Open Tm Real', unit: 's' },
        { label: 'Fim abertura', name: 'Open EndPos Real', unit: 'mm' },
        { label: 'TorqMaxFch', name: null, unit: '%', d: 1 },
      ]} />
      <section className="e-sec e-params">
        <h3 className="e-h">Tempos do último ciclo</h3>
        <Pair label="Fech. baixa pressão" name="CloseLowPrs Tm Real" unit="s" d={3} />
        <Pair label="Fech. alta pressão" name="CloseHiPrs Tm Real" unit="s" d={3} />
      </section>
    </>
  )
}

function ExtrScreen() {
  return (
    <>
      <div className="e-two">
        <Grid title="Av Extr." cols={['#1', '#2']} rows={EJ_ADV_ROWS} seg={EJ_ADV_SEG} />
        <Grid title="Ret Extr." cols={['#2', '#1']} rows={EJ_RET_ROWS} seg={EJ_RET_SEG} />
      </div>
      <Stats items={[
        { label: 'Tempo Extr', name: 'Eject Tm Real', unit: 's' },
        { label: 'Cont Extr', name: null, unit: '', d: 0 },
        { label: 'Pos Avan', name: null, unit: 'mm' },
        { label: 'AvExtrTorqMax', name: null, unit: '%', d: 1 },
      ]} />
    </>
  )
}

/** Machos (Mch A..F): uso, pressão e velocidade de entrada e saída. */
const CORES = ['A', 'B', 'C', 'D', 'E', 'F']
const coreNames = (c: string) => [`Core ${c} Use Set`, `Core ${c} In Prs Set`, `Core ${c} Out Prs Set`, `Core ${c} In Spd Set`, `Core ${c} Out Spd Set`]

function UseCell({ name }: { name: string }) {
  const v = useVal(name)
  return <span className="e-f e-txt" title={name}>{v === null ? '—' : v === 0 ? 'Sem Uso' : 'Uso'}</span>
}

function CoreGroup({ cores }: { cores: string[] }) {
  return (
    <section className="e-sec e-gridsec">
      <div className="e-gridwrap">
        <table className="e-grid">
          <thead>
            <tr><th />{cores.map((c) => <th key={c} colSpan={2}>Mch {c}</th>)}</tr>
            <tr><th />{cores.map((c) => [<th key={`${c}i`}>In</th>, <th key={`${c}o`}>Out</th>])}</tr>
          </thead>
          <tbody>
            <tr><th scope="row">Selec Func</th>{cores.map((c) => <td key={c} colSpan={2}><UseCell name={`Core ${c} Use Set`} /></td>)}</tr>
            <tr><th scope="row">Press(bar)</th>{cores.map((c) => [
              <td key={`${c}i`}><F name={`Core ${c} In Prs Set`} d={0} /></td>, <td key={`${c}o`}><F name={`Core ${c} Out Prs Set`} d={0} /></td>])}</tr>
            <tr><th scope="row">Veloc(%)</th>{cores.map((c) => [
              <td key={`${c}i`}><F name={`Core ${c} In Spd Set`} d={0} /></td>, <td key={`${c}o`}><F name={`Core ${c} Out Spd Set`} d={0} /></td>])}</tr>
          </tbody>
        </table>
      </div>
    </section>
  )
}

function MchScreen() {
  return <><CoreGroup cores={CORES.slice(0, 3)} /><CoreGroup cores={CORES.slice(3)} /></>
}

function DosagGrid() {
  return (
    <Grid title="Dosag" cols={['#1', '#2', '#3']} rows={DOS_ROWS} seg={DOS_SEG} side={
      <>
        <span className="e-side-t">Desc Tras</span>
        <Pair label="" name="Charge After InjPos Set" unit="mm" />
        <Pair label="" name="Charge After InjSpd Set" unit="mm/s" d={1} />
      </>
    } />
  )
}

function InjecScreen() {
  return (
    <>
      <Grid title="Injeca" cols={['Com.Pr.', '#5', '#4', '#3', '#2', '#1']} rows={INJ_ROWS} seg={INJ_SEG} />
      <Grid title="Reca" cols={['#4', '#3', '#2', '#1']} rows={HOLD_ROWS} seg={HOLD_SEG} />
      <section className="e-sec e-params">
        <h3 className="e-h">Parametro</h3>
        <Pair label="Tempo Injec" name="InjTm Set" unit="s" />
        <Pair label="Temp Resf Mold" name="Cool TimeL Set" unit="s" />
      </section>
      <DosagGrid />
      <section className="e-sec e-params">
        <h3 className="e-h">Último ciclo</h3>
        <Pair label="Início injeção" name="InjStartPos Real" unit="mm" />
        <Pair label="Início recalque" name="HoldStartPos Real" unit="mm" />
        <Pair label="PosFimInjec" name="RestmillPos Real" unit="mm" />
        <Pair label="Fim da dosagem" name="Charge Pos Real" unit="mm" />
        <Pair label="Pr. TurnReca" name="TurnHold Prs Real" unit="bar" d={0} />
        <Pair label="Vel TurnReca" name="TurnHold Spd Real" unit="mm/s" d={1} />
        <Pair label="Pr. máx. injeção" name="Max InjPrs Real" unit="bar" d={0} />
        <Pair label="Pr. máx. recalque" name="Max HoldPrs Real" unit="bar" d={0} />
        <Pair label="Vel. média injeção" name="Inj Avg Spd Real" unit="mm/s" d={1} />
      </section>
    </>
  )
}

/** Linha de temperaturas programadas; zona desligada (real = 0) aparece apagada, como no painel. */
function TempSetRow({ label }: { label: string }) {
  const values = useContext(ValuesCtx)
  return (
    <div className="e-setrow"><span>{label}</span>
      {range(1, 7).map((z) => {
        const real = num(values[ino(`Temp${z} Real`)])
        return <F key={z} name={real === 0 ? null : `Temp${z} Set`} d={0} />
      })}
    </div>
  )
}

function TempScreen() {
  return (
    <section className="e-sec">
      <h3 className="e-h">Aquece</h3>
      <Heaters />
      <TempSetRow label="Set Val. (°C)" />
      <div className="e-setrow"><span>Real (°C)</span>{TEMP_REALS.map((n) => <F key={n} name={n} d={0} />)}</div>
    </section>
  )
}

function RapScreen() {
  return (
    <>
      <section className="e-sec">
        <TempSetRow label="Temp Set(°C)" />
      </section>
      <Grid title="Fech" cols={['#1', '#2', '#3', 'BxPrs', 'AlPr.']} rows={CLOSE_ROWS} seg={CLOSE_SEG} />
      <Grid title="Abri" cols={['Fim', '#4', '#3', '#2', '#1']} rows={OPEN_ROWS} seg={OPEN_SEG} />
      <Grid title="Injec" cols={['TurnReca', '#5', '#4', '#3', '#2', '#1']} rows={INJ_ROWS}
        seg={INJ_SEG} side={<>
          <Pair label="Tempo Injec" name="InjTm Set" unit="s" />
          <Pair label="Temp Resf Mold" name="Cool TimeL Set" unit="s" />
        </>} />
      <Grid title="Reca" cols={['#4', '#3', '#2', '#1']} rows={HOLD_ROWS} seg={HOLD_SEG} />
      <DosagGrid />
      <div className="e-two">
        <Grid title="Av Extr" cols={['#1', '#2']} rows={EJ_ADV_ROWS.slice(0, 2)} seg={EJ_ADV_SEG} />
        <Grid title="Ret Extr" cols={['#2', '#1']} rows={EJ_RET_ROWS.slice(0, 2)} seg={EJ_RET_SEG} />
      </div>
    </>
  )
}

interface Shot { count: number; v: Record<string, number | null> }

/** Monitor de produção: uma linha por ciclo, registrada enquanto a tela está aberta. */
function MoniScreen({ shots }: { shots: Shot[] }) {
  const stats = MONI_COLS.map((c) => {
    const xs = shots.map((s) => s.v[c.name]).filter((x): x is number => x !== null && x !== undefined)
    if (xs.length === 0) return null
    return { avg: xs.reduce((a, b) => a + b, 0) / xs.length, max: Math.max(...xs), min: Math.min(...xs) }
  })
  return (
    <section className="e-sec">
      <p className="e-note">Uma linha por ciclo, registrada enquanto esta tela do Monitor está aberta (até 50 ciclos).</p>
      <table className="e-moni">
        <thead><tr><th>Shot NO.</th>{MONI_COLS.map((c) => <th key={c.name} title={c.name}>{c.label}</th>)}</tr></thead>
        <tbody>
          {shots.length === 0 && <tr><td colSpan={MONI_COLS.length + 1} className="e-na">Aguardando o próximo ciclo da máquina…</td></tr>}
          {shots.map((s) => (
            <tr key={s.count}><td>{s.count}</td>{MONI_COLS.map((c) => <td key={c.name}>{fmt(s.v[c.name], c.d)}</td>)}</tr>
          ))}
        </tbody>
        {shots.length > 0 && (
          <tfoot>
            {(['avg', 'max', 'min'] as const).map((k) => (
              <tr key={k}>
                <th>{k === 'avg' ? 'Media' : k === 'max' ? 'Max.' : 'Min.'}</th>
                {stats.map((s, i) => <td key={MONI_COLS[i].name}>{s === null ? '—' : fmt(s[k], MONI_COLS[i].d)}</td>)}
              </tr>
            ))}
          </tfoot>
        )}
      </table>
    </section>
  )
}

function AlarmScreen() {
  const alarms = usePoll<Alarms>('/api/alarms?hours=24', 5000).data
  const fmtTime = (ts: number) => new Date(ts * 1000).toLocaleString('pt-BR')
  return (
    <>
      <section className="e-sec">
        <h3 className="e-h">Alarmes ativos</h3>
        {!alarms ? <p className="e-na">Carregando…</p>
          : alarms.active.length === 0 ? <p className="e-na">Nenhum alarme ativo.</p>
            : <ul className="e-alarms">{alarms.active.map((a) => <li key={a.tag}><AlertTriangle size={15} aria-hidden="true" /> {a.label}</li>)}</ul>}
      </section>
      <section className="e-sec">
        <h3 className="e-h">Histórico (24 h)</h3>
        {alarms && alarms.events.length > 0 ? (
          <table className="e-moni e-hist">
            <thead><tr><th>Alarme</th><th>Início</th><th>Fim</th></tr></thead>
            <tbody>
              {alarms.events.slice(0, 30).map((e, i) => (
                <tr key={`${e.tag}-${e.started}-${i}`}><td>{e.label}</td><td>{fmtTime(e.started)}</td><td>{e.ended ? fmtTime(e.ended) : 'ativo'}</td></tr>
              ))}
            </tbody>
          </table>
        ) : <p className="e-na">Nenhum alarme nas últimas 24 h.</p>}
      </section>
    </>
  )
}

interface Screen { id: string; label: string; title: string; page: string; icon: ReactNode; names: string[] }

const SCREENS: Screen[] = [
  { id: 'princ', label: 'PRINC', title: 'Vi Geral', page: '11', icon: <Home size={22} />,
    names: [...ACTIONS.map((a) => a.name), 'CycTime Real', 'ProdTm Real', ...TEMP_REALS, ...TEMP_SETS] },
  { id: 'fech', label: 'Fech.', title: 'Fech', page: '101', icon: <Columns2 size={22} />,
    names: [...rowNames(CLOSE_ROWS), ...rowNames(OPEN_ROWS), 'Close Tm Real', 'Open Tm Real', 'Open EndPos Real',
      'CloseLowPrs Tm Real', 'CloseHiPrs Tm Real'] },
  { id: 'extr', label: 'Extr.', title: 'Extr.', page: '102', icon: <Gauge size={22} />,
    names: [...rowNames(EJ_ADV_ROWS), ...rowNames(EJ_RET_ROWS), 'Eject Tm Real'] },
  { id: 'mch', label: 'Mch', title: 'Mch', page: '103', icon: <Workflow size={22} />, names: CORES.flatMap(coreNames) },
  { id: 'injec', label: 'Injec', title: 'Injeca', page: '201', icon: <Zap size={22} />,
    names: [...rowNames(INJ_ROWS), ...rowNames(HOLD_ROWS), ...rowNames(DOS_ROWS), 'InjTm Set', 'Charge After InjPos Set',
      'Charge After InjSpd Set', 'Cool TimeL Set', 'SuckBackDistance Set', 'SuckBackSpd Set', 'InjStartPos Real', 'HoldStartPos Real', 'Charge Pos Real', 'TurnHold Prs Real',
      'TurnHold Spd Real', 'Max InjPrs Real', 'Max HoldPrs Real', 'Inj Avg Spd Real'] },
  { id: 'temp', label: 'Temp', title: 'Temperatura', page: '301', icon: <Thermometer size={22} />, names: [...TEMP_REALS, ...TEMP_SETS] },
  { id: 'rap', label: 'Rap.', title: 'Rapi', page: '401', icon: <Workflow size={22} />,
    names: [...TEMP_SETS, ...TEMP_REALS, ...[CLOSE_ROWS, OPEN_ROWS, INJ_ROWS, HOLD_ROWS, DOS_ROWS, EJ_ADV_ROWS, EJ_RET_ROWS].flatMap(rowNames),
      'InjTm Set', 'Charge After InjPos Set', 'Charge After InjSpd Set', 'Cool TimeL Set', 'HiPrsLockProtTm Set'] },
  { id: 'moni', label: 'Moni', title: 'Monitor Produc', page: '431', icon: <Gauge size={22} />, names: [] },
  { id: 'alarm', label: 'Alarm', title: 'Alarmes', page: '—', icon: <ListChecks size={22} />, names: [] },
]

/* ---- moldura -------------------------------------------------------------- */

const HEADER_NAMES = [...SEG_NAMES, 'InjPrs Sensor Real', 'Operate State', 'Inj Tm Real', 'RestmillPos Real', 'CycTime Real', 'ProdTm Real',
  'OpenCnt Hi Real', 'OpenCnt Low Real', ...MONI_COLS.map((c) => c.name)]

const MODES: Record<number, string> = { 0: 'Manual', 3: 'Semi-auto', 5: 'Auto', 9: 'Auto tempo' }

function countOf(values: Values): number | null {
  const hi = num(values[ino('OpenCnt Hi Real')])
  const lo = num(values[ino('OpenCnt Low Real')])
  return hi === null && lo === null ? null : (hi ?? 0) * 65536 + (lo ?? 0)
}

function Header({ screen, alarmText, online }: { screen: Screen; alarmText: string | null; online: boolean }) {
  const values = useContext(ValuesCtx)
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])
  const state = num(values[ino('Operate State')])
  const count = countOf(values)
  const p = (n: number) => String(n).padStart(2, '0')
  const stamp = `${now.getFullYear()}.${p(now.getMonth() + 1)}.${p(now.getDate())}  ${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`
  const mode = state === null ? '—' : MODES[state] ?? `Modo ${state}`
  return (
    <>
      <div className={`e-bar ${alarmText ? 'alarm' : ''}`} role={alarmText ? 'alert' : undefined}>
        <span className="e-logo" aria-hidden="true">CH</span>
        <span className="e-bartext">{alarmText ?? stamp}</span>
        <span className="e-net">{online ? 'online' : 'offline'}</span>
      </div>
      <div className="e-titlebar">
        <span className="e-ptitle">{screen.title}</span>
        <span className="e-mold" title={NOT_PUBLISHED}>Molde —</span>
        <span>Pag <b className="e-pag">{screen.page}</b></span>
      </div>
      <div className="e-head">
        <div className={`e-modeic ${state !== null && state !== 0 ? 'auto' : ''}`}>
          <span><Hand size={26} aria-hidden="true" /></span><em>{mode}</em>
        </div>
        <dl>
          <dt>PosFech.</dt><dd><T name={null} /> mm</dd>
          <dt>Pos.Rosca</dt><dd><T name={null} /> mm</dd>
          <dt>PosExtr.</dt><dd><T name={null} d={1} /> mm</dd>
        </dl>
        <dl>
          <dt>Tempo Inj.</dt><dd><T name="Inj Tm Real" /> s</dd>
          <dt>PosFimInjec</dt><dd><T name="RestmillPos Real" /> mm</dd>
          <dt>Com.Cont.P</dt><dd><T name={null} d={0} /> bar</dd>
        </dl>
        <dl>
          <dt>Tp Ciclo</dt><dd><T name="CycTime Real" /> s</dd>
          <dt>Prod Per</dt><dd><T name="ProdTm Real" /> s</dd>
          <dt>Contador Prod</dt><dd><b>{count === null ? '—' : count}</b></dd>
        </dl>
      </div>
    </>
  )
}

export function EstoPanel({ status }: { status: Status | null }) {
  const [screenId, setScreenId] = useState('princ')
  const screen = SCREENS.find((s) => s.id === screenId) ?? SCREENS[0]
  const [values, setValues] = useState<Values>({})
  const [shots, setShots] = useState<Shot[]>([])
  const lastCount = useRef<number | null>(null)
  const alarms = usePoll<Alarms>('/api/alarms?hours=1', 10000).data
  const alarmText = alarms?.active[0]?.label ?? null
  const tagParam = useMemo(() => [...new Set([...HEADER_NAMES, ...screen.names])].map(ino).join(','), [screen])

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const r = await getJson<{ values: Values }>(`/api/values?tags=${encodeURIComponent(tagParam)}`)
        if (!alive) return
        setValues((prev) => ({ ...prev, ...r.values }))
        // Um ciclo novo (contador mudou) vira uma linha do Monitor de produção.
        const count = countOf(r.values)
        if (count === null) return
        if (lastCount.current !== null && count > lastCount.current) {
          const v: Record<string, number | null> = {}
          for (const c of MONI_COLS) v[c.name] = scaled(r.values, c.name)
          setShots((prev) => [{ count, v }, ...prev].slice(0, 50))
        }
        lastCount.current = count
      } catch {
        /* o cabeçalho do monitor já avisa quando o backend cai */
      }
    }
    load()
    const id = window.setInterval(load, 3000)
    return () => { alive = false; window.clearInterval(id) }
  }, [tagParam])

  const bodies: Record<string, ReactNode> = {
    princ: <PrincScreen />, fech: <FechScreen />, extr: <ExtrScreen />, mch: <MchScreen />, injec: <InjecScreen />,
    temp: <TempScreen />, rap: <RapScreen />, moni: <MoniScreen shots={shots} />, alarm: <AlarmScreen />,
  }

  return (
    <div className="hmi-wrap">
      <div className="esto" aria-label="Réplica simulada da tela do painel da máquina">
        <ValuesCtx.Provider value={values}>
          <Header screen={screen} alarmText={alarmText} online={status?.connected ?? false} />
          <div className="e-body">{bodies[screen.id]}</div>
        </ValuesCtx.Provider>
        <nav className="e-nav" aria-label="Telas do painel">
          {SCREENS.map((s) => (
            <button key={s.id} className={s.id === screen.id ? 'on' : ''} aria-current={s.id === screen.id ? 'page' : undefined}
              onClick={() => setScreenId(s.id)} title={s.title}>
              {s.icon}<span>{s.label}</span>
            </button>
          ))}
        </nav>
      </div>
      <aside className="hmi-note">
        <h2>Como ler esta tela</h2>
        <p>É uma réplica simulada das telas do painel da Spark, montada a partir de fotos do painel real e preenchida com
          os valores lidos do OPC-UA a cada poucos segundos. Trocar de tela aqui não muda o painel físico, e nenhum valor
          pode ser alterado.</p>
        <p>Campos vazios ou com <b>—</b> existem no painel, mas ele não publica esse dado no OPC-UA (posições em tempo
          real, servos, entradas e saídas, nome do molde). Passe o mouse sobre um valor para ver a variável de origem.</p>
      </aside>
    </div>
  )
}
