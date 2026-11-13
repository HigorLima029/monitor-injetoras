/**
 * Réplica das telas da IHM CHY (Chen Hsong) alimentada pelo OPC-UA do gateway.
 *
 * Só exibe valores. Nenhum campo é editável e nada é enviado à máquina.
 * Campos que o gateway não publica aparecem como "—".
 */
import {
  AlertTriangle, ArrowRightToLine, Columns2, Gauge, Home, LayoutGrid, RefreshCcw,
  Signal, SignalZero, Syringe, Thermometer, UserRound, Workflow, Zap,
} from 'lucide-react'
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { Alarms, Status } from '../api'
import { getJson, num, usePoll } from '../api'
import './hmi.css'

type Values = Record<string, number | string | null>
const ValuesCtx = createContext<Values>({})

const NOT_PUBLISHED = 'Este campo não é publicado pelo gateway'

/* ---- utilidades ---------------------------------------------------------- */

function fmtNum(v: number | null, digits: number) {
  if (v === null) return '—'
  return v.toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/** Número de estágios: o controlador guarda no byte baixo (ex.: 131074 → 2). */
function stages(v: number | null) {
  return v === null ? null : v & 0xffff
}

/** Caixa de valor como na IHM. tag=null => campo existe na tela mas não vem do gateway. */
function F({ tag, d = 1, wide, map, calc }: {
  tag: string | null; d?: number; wide?: boolean
  map?: Record<number, string>; calc?: (v: number | null) => number | null
}) {
  const values = useContext(ValuesCtx)
  let text: string
  let title: string
  if (tag === null) {
    text = '—'
    title = NOT_PUBLISHED
  } else {
    const raw = num(values[tag])
    const v = calc ? calc(raw) : raw
    if (map) text = v === null ? '—' : map[v] ?? `modo ${v}`
    else text = fmtNum(v, d)
    title = tag
  }
  return (
    <span className={`hf ${wide ? 'wide' : ''} ${tag === null ? 'na' : ''}`} title={title}>{text}</span>
  )
}

function Tab({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="h-tabrow">
      <span className="h-tab">{children}</span>
      {right && <span className="h-tabright">{right}</span>}
    </div>
  )
}

function Stage({ tag, calc = stages }: { tag: string | null; calc?: (v: number | null) => number | null }) {
  return <>Estág. <F tag={tag} d={0} calc={calc} /></>
}

/** Tabela Pr/Vel/Pos com colunas por estágio. */
function StageTable({ cols, rows }: {
  cols: string[]
  rows: { label: string; unit: string; tags: (string | null)[]; d?: number }[]
}) {
  return (
    <table className="h-table">
      <thead>
        <tr><th />{cols.map((c) => <th key={c}>{c}</th>)}<th className="u" /></tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.label}>
            <th scope="row">{r.label}</th>
            {r.tags.map((t, i) => <td key={i}><F tag={t} d={r.d ?? 1} /></td>)}
            <td className="u">{r.unit}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Line({ children }: { children: ReactNode }) {
  return <div className="h-line">{children}</div>
}

function L({ label, children, unit }: { label: string; children: ReactNode; unit?: string }) {
  return <span className="h-pair"><span className="h-lbl">{label}</span>{children}{unit && <span className="h-unit">{unit}</span>}</span>
}

function SubTabs({ items }: { items: string[] }) {
  return (
    <div className="h-subtabs" aria-hidden="true">
      {items.map((s, i) => <span key={`${i}-${s}`} className={i === 0 ? 'on' : ''} title={i === 0 ? undefined : 'Tela não replicada'}>{s}</span>)}
    </div>
  )
}

/* ---- telas ---------------------------------------------------------------- */

interface Screen {
  id: string
  label: string
  code: string
  icon: ReactNode
  tags: string[]
  render: () => ReactNode
  sub?: string[]
}

const ZONES = [
  { name: 'T0', now: 'TempNozzleNow', set: 'TempNozzle', hi: 'MaxTempNozzle', lo: 'MinTempNozzle', duty: 'DutyNozzle' },
  { name: 'T1', now: 'TempZ1Now', set: 'TempZ1', hi: 'MaxTempZ1', lo: 'MinTempZ1', duty: 'DutyZ1' },
  { name: 'T2', now: 'TempZ2Now', set: 'TempZ2', hi: 'MaxTempZ2', lo: 'MinTempZ2', duty: 'DutyZ2' },
  { name: 'T3', now: 'TempZ3Now', set: 'TempZ3', hi: 'MaxTempZ3', lo: 'MinTempZ3', duty: 'DutyZ3' },
]

function Barrel() {
  return (
    <div className="h-barrel">
      <span className="h-tip" aria-hidden="true" />
      {ZONES.map((z) => <span className="h-seg" key={z.name}><F tag={z.now} /></span>)}
      <span className="h-unit">°C</span>
    </div>
  )
}

function Dial({ label }: { label: string }) {
  return (
    <div className="h-dial" title={NOT_PUBLISHED}>
      <svg viewBox="0 0 120 80" aria-hidden="true"><path d="M15 70 A48 48 0 1 1 105 70" fill="none" strokeWidth="9" strokeLinecap="round" /></svg>
      <span className="h-dialv">—</span>
      <span className="h-diall">{label}</span>
    </div>
  )
}

function MachineDrawing() {
  // Silhueta genérica de injetora (não é a arte do fabricante).
  return (
    <svg className="h-machine" viewBox="0 0 320 110" aria-hidden="true">
      <rect x="6" y="18" width="92" height="56" rx="3" />
      <rect x="98" y="24" width="64" height="46" rx="2" className="dark" />
      <rect x="162" y="40" width="36" height="16" />
      <rect x="198" y="34" width="90" height="34" rx="3" />
      <path d="M228 34 l8 -22 h20 l8 22z" />
      <rect x="20" y="74" width="270" height="18" rx="2" className="base" />
      <rect x="160" y="42" width="6" height="12" className="accent" />
    </svg>
  )
}

function Flow() {
  const box = (tag: string | null, label: string) => (
    <div className="h-step" title={label}><F tag={tag} d={2} /><span>{label}</span></div>
  )
  return (
    <div className="h-flow">
      <div className="r">
        {box(null, 'Robô')}<i>→</i>{box('ClampLastTime', 'Fechamento')}<i>→</i>
        {box('NozzAdvanceLastTime', 'Avanço bico')}<i>→</i>{box('InjectLastTime', 'Injeção')}<i>→</i>
        {box('PlastLastTime', 'Dosagem')}
      </div>
      <div className="r mid">
        {box('EjectBackTime', 'Retorno extrator')}
        <div className="h-cycle"><span>Ciclo atual</span><F tag="CycleMonitor" d={2} /></div>
        <div className="h-cycle"><span>Ciclo anterior</span><F tag="ProductionCycle" d={2} /></div>
        {box('DecompTime', 'Descompressão')}
      </div>
      <div className="r">
        {box('EjectLastTime', 'Avanço extrator')}<i>←</i>{box('OpenLastTime', 'Abertura')}<i>←</i>
        {box(null, 'Macho')}<i>←</i>{box('NozzBackLastTime', 'Recuo bico')}<i>←</i>
        {box('ColdLastTime', 'Resfriamento')}
      </div>
    </div>
  )
}

function HomeScreen() {
  const values = useContext(ValuesCtx)
  const total = num(values.MaxProduction)
  const count = num(values.ProductionCount)
  const rest = total !== null && count !== null ? Math.max(total - count, 0) : null
  return (
    <>
      <section className="h-box h-hero">
        <Dial label="Pr. bar" />
        <MachineDrawing />
        <Dial label="Vel." />
        <div className="h-oil" title="TempOilNow"><Thermometer size={16} aria-hidden="true" /> <F tag="TempOilNow" /> °C</div>
      </section>
      <section className="h-box"><Barrel /></section>
      <Flow />
      <div className="h-two">
        <section className="h-box">
          <table className="h-table compact">
            <thead><tr><th /><th>Atual</th><th>Rest.</th><th>Total</th></tr></thead>
            <tbody>
              <tr><th scope="row">Contar</th><td><F tag="ProductionCount" d={0} /></td>
                <td><span className="hf" title="Total − atual">{rest === null ? '—' : fmtNum(rest, 0)}</span></td>
                <td><F tag="MaxProduction" d={0} /></td></tr>
              <tr><th scope="row">Produz. (h)</th><td><F tag={null} /></td><td><F tag={null} /></td><td><F tag={null} /></td></tr>
            </tbody>
          </table>
        </section>
        <section className="h-box h-cores">
          <div title={NOT_PUBLISHED}><Workflow size={18} aria-hidden="true" /><span /><span /><span /><span /></div>
          <div title={NOT_PUBLISHED}><Workflow size={18} aria-hidden="true" /><span /><span /><span /><span /></div>
          <p>Molde <b title={NOT_PUBLISHED}>—</b></p>
        </section>
      </div>
    </>
  )
}

const SCREENS: Screen[] = [
  {
    id: 'home', label: 'Início', code: '002', icon: <Home size={22} />,
    tags: ['TempOilNow', 'MaxProduction', 'ProductionCount', 'CycleMonitor', 'ProductionCycle', 'ClampLastTime',
      'NozzAdvanceLastTime', 'InjectLastTime', 'PlastLastTime', 'DecompTime', 'ColdLastTime', 'NozzBackLastTime',
      'OpenLastTime', 'EjectLastTime', 'EjectBackTime', ...ZONES.map((z) => z.now)],
    render: () => <HomeScreen />,
  },
  {
    id: 'molde', label: 'Molde', code: '004', icon: <Columns2 size={22} />,
    sub: ['Molde', 'Parâm de Molde', 'Rampa', 'Parâm'],
    tags: ['Clamp1Pres', 'ClampLpPres', 'ClampHpPres', 'Clamp1Speed', 'ClampLpSpeed', 'ClampHpSpeed', 'Clamp1Pos',
      'Clamp4Pos', 'Clamp5Pos', 'ClampStageSelect', 'ClampForcePres', 'ClampLastTime', 'Open1Pres', 'Open2Pres',
      'Open3Pres', 'Open4Pres', 'Open1Speed', 'Open2Speed', 'Open3Speed', 'Open4Speed', 'Open1Pos', 'Open2Pos',
      'Open3Pos', 'Open4Pos', 'OpenLastTime', 'OpenMaxLastPos'],
    render: () => (
      <>
        <section className="h-box">
          <Tab right={<Stage tag="ClampStageSelect" />}>Fecha</Tab>
          <StageTable cols={['Estág. 1', 'Baixa Pr.', 'Alta Pr.']} rows={[
            { label: 'Pr.', unit: 'bar', tags: ['Clamp1Pres', 'ClampLpPres', 'ClampHpPres'] },
            { label: 'Vel.', unit: '%', tags: ['Clamp1Speed', 'ClampLpSpeed', 'ClampHpSpeed'] },
            { label: 'Pos.', unit: 'mm', tags: ['Clamp1Pos', 'Clamp4Pos', 'Clamp5Pos'] },
          ]} />
          <Line>
            <L label="Delay" unit="s"><F tag={null} d={2} /></L>
            <L label="Pr. de fechar"><F tag="ClampForcePres" /></L>
            <L label="Tempo de fechar" unit="s"><F tag="ClampLastTime" d={2} /></L>
          </Line>
          <Line>
            <L label="Pos. cilindro óleo" unit="mm"><F tag={null} d={2} /></L>
            <L label="Pos. molde" unit="mm"><F tag={null} /></L>
          </Line>
        </section>
        <section className="h-box">
          <Tab right={<>Estág. <F tag={null} d={0} /></>}>Aberto</Tab>
          <StageTable cols={['Último', 'Estág. 3', 'Estág. 2', 'Estág. 1']} rows={[
            { label: 'Pr.', unit: 'bar', tags: ['Open4Pres', 'Open3Pres', 'Open2Pres', 'Open1Pres'] },
            { label: 'Vel.', unit: '%', tags: ['Open4Speed', 'Open3Speed', 'Open2Speed', 'Open1Speed'] },
            { label: 'Pos.', unit: 'mm', tags: ['Open4Pos', 'Open3Pos', 'Open2Pos', 'Open1Pos'] },
          ]} />
          <Line>
            <L label="Delay" unit="s"><F tag={null} d={2} /></L>
            <L label="Tempo aberto" unit="s"><F tag="OpenLastTime" d={2} /></L>
            <L label="Fim aberto" unit="mm"><F tag="OpenMaxLastPos" d={2} /></L>
          </Line>
        </section>
      </>
    ),
  },
  {
    id: 'extrator', label: 'Extrator', code: '020', icon: <ArrowRightToLine size={22} />,
    sub: ['Extrat.', 'Parâm', 'Parâm'],
    tags: ['EjectOut1Pres', 'EjectOut2Pres', 'EjectOut1Speed', 'EjectOut2Speed', 'EjectOut1Pos', 'EjectOut2Pos',
      'EjectOutStageSelect', 'EjectLastTime', 'EjectCount', 'EjectMode', 'EjectIn1Pres', 'EjectIn2Pres',
      'EjectIn1Speed', 'EjectIn2Speed', 'EjectIn1Pos', 'EjectIn2Pos', 'EjectInStageSelect', 'EjectBackTime'],
    render: () => (
      <>
        <section className="h-box">
          <Tab right={<Stage tag="EjectOutStageSelect" />}>Avanço extração</Tab>
          <StageTable cols={['Estág. 1', 'Estág. 2']} rows={[
            { label: 'Pr.', unit: 'bar', tags: ['EjectOut1Pres', 'EjectOut2Pres'] },
            { label: 'Vel.', unit: '%', tags: ['EjectOut1Speed', 'EjectOut2Speed'] },
            { label: 'Pos.', unit: 'mm', tags: ['EjectOut1Pos', 'EjectOut2Pos'] },
          ]} />
          <Line>
            <L label="Delay" unit="s"><F tag={null} d={2} /></L>
            <L label="Tempo avanço" unit="s"><F tag="EjectLastTime" d={2} /></L>
          </Line>
          <Line>
            <L label="Contag. extração"><F tag="EjectCount" d={0} /></L>
            <L label="Modo extrat."><F tag="EjectMode" map={{ 3: 'Avanç.' }} wide /></L>
          </Line>
        </section>
        <section className="h-box">
          <Tab right={<Stage tag="EjectInStageSelect" />}>Retorno extração</Tab>
          <StageTable cols={['Estág. 2', 'Estág. 1']} rows={[
            { label: 'Pr.', unit: 'bar', tags: ['EjectIn2Pres', 'EjectIn1Pres'] },
            { label: 'Vel.', unit: '%', tags: ['EjectIn2Speed', 'EjectIn1Speed'] },
            { label: 'Pos.', unit: 'mm', tags: ['EjectIn2Pos', 'EjectIn1Pos'] },
          ]} />
          <Line>
            <L label="Delay" unit="s"><F tag={null} d={2} /></L>
            <L label="Tempo retorno" unit="s"><F tag="EjectBackTime" d={2} /></L>
            <L label="Primeiro delay retorno" unit="s"><F tag={null} d={2} /></L>
          </Line>
        </section>
      </>
    ),
  },
  {
    id: 'macho', label: 'Macho', code: '033', icon: <Workflow size={22} />,
    sub: ['Macho 1/2', 'Parâm 1/2'],
    tags: ['CoreAInPres', 'CoreAInSpeed', 'CoreAOutPres', 'CoreAOutSpeed', 'CoreBInPres', 'CoreBInSpeed',
      'CoreBOutPres', 'CoreBOutSpeed', 'CoreAInPos', 'CoreAOutPos', 'CoreBInPos', 'CoreBOutPos'],
    render: () => (
      <div className="h-two">
        {(['A', 'B'] as const).map((k, i) => (
          <section className="h-box" key={k}>
            <Tab>Macho {i + 1}</Tab>
            <StageTable cols={[`${i + 1} in`, `${i + 1} out`]} rows={[
              { label: 'Tipo', unit: '', tags: [null, null] },
              { label: 'Modo contr.', unit: '', tags: [null, null] },
              { label: 'Pr.', unit: 'bar', tags: [`Core${k}InPres`, `Core${k}OutPres`] },
              { label: 'Vel.', unit: '%', tags: [`Core${k}InSpeed`, `Core${k}OutSpeed`] },
              { label: 'Tempo', unit: 's', tags: [null, null], d: 2 },
              { label: 'Contar', unit: '', tags: [null, null], d: 0 },
              { label: 'Pos. refer.', unit: 'mm', tags: [`Core${k}InPos`, `Core${k}OutPos`] },
            ]} />
          </section>
        ))}
      </div>
    ),
  },
  {
    id: 'menu', label: 'Menu', code: '056', icon: <LayoutGrid size={22} />, tags: [],
    render: () => (
      <section className="h-box h-menu">
        <p>Os menus de produção, dados e configuração da máquina não são replicados aqui: eles mexem em
          ajustes da máquina e o gateway não publica esses dados.</p>
        <p>Use as abas <b>Produção</b>, <b>Alarmes</b> e <b>Variáveis</b> do monitor para consultar contagens,
          histórico de alarmes e todas as variáveis.</p>
      </section>
    ),
  },
  {
    id: 'injecao', label: 'Injeção', code: '011', icon: <Syringe size={22} />,
    sub: ['Injecção', 'Parâm'],
    tags: ['Inject1Pres', 'Inject1Speed', 'InjectEndLastPos', 'InjectionStageSelect', 'Hold1Pres', 'Hold2Pres',
      'Hold1Speed', 'Hold2Speed', 'Hold1Time', 'Hold2Time', 'HoldPresStageSelect', 'InjectionPres', 'ColdMode',
      'Timer6', 'InjectLastTime'],
    render: () => (
      <>
        <section className="h-box">
          <Tab right={<Stage tag="InjectionStageSelect" />}>Injeção</Tab>
          <Line>
            <L label="Delay" unit="s"><F tag={null} d={2} /></L>
            <L label="Final injeção" unit="mm"><F tag="InjectEndLastPos" d={2} /></L>
          </Line>
          <StageTable cols={['Estág. 1']} rows={[
            { label: 'Pr.', unit: 'bar', tags: ['Inject1Pres'] },
            { label: 'Vel.', unit: '%', tags: ['Inject1Speed'] },
            { label: 'Tempo', unit: 's', tags: [null], d: 3 },
          ]} />
        </section>
        <section className="h-box">
          <Tab right={<Stage tag="HoldPresStageSelect" />}>Recalque</Tab>
          <Line><L label="Pr. de injeç. (comutação)" unit="bar"><F tag="InjectionPres" /></L></Line>
          <StageTable cols={['Último', 'Estág. 1']} rows={[
            { label: 'Pr.', unit: 'bar', tags: ['Hold2Pres', 'Hold1Pres'] },
            { label: 'Vel.', unit: '%', tags: ['Hold2Speed', 'Hold1Speed'] },
            { label: 'Tempo', unit: 's', tags: ['Hold2Time', 'Hold1Time'], d: 2 },
          ]} />
          <Line>
            <L label="Tempo resfr."><F tag="ColdMode" map={{ 2: 'Durant. Plast.' }} wide /></L>
            <L label="Tempo resfr." unit="s"><F tag="Timer6" /></L>
          </Line>
        </section>
      </>
    ),
  },
  {
    id: 'plast', label: 'Plast.', code: '015', icon: <RefreshCcw size={22} />,
    sub: ['Plast.', 'Parâm'],
    tags: ['Plast1Pres', 'Plast1Speed', 'Plast1Pos', 'Plast1BackPres', 'PlastStageSelect', 'PlastMode',
      'PlastLastTime', 'DecompBefPos', 'DecompPres', 'DecompSpeed', 'DecompPos', 'DecompTime'],
    render: () => (
      <>
        <section className="h-box">
          <Tab right={<Stage tag="PlastStageSelect" />}>Plast.</Tab>
          <StageTable cols={['Estág. 1']} rows={[
            { label: 'Pr.', unit: 'bar', tags: ['Plast1Pres'] },
            { label: 'Vel.', unit: '%', tags: ['Plast1Speed'] },
            { label: 'Pos.', unit: 'mm', tags: ['Plast1Pos'], d: 2 },
            { label: 'ContPr.', unit: 'bar', tags: ['Plast1BackPres'] },
          ]} />
          <Line>
            <L label="Modo de plast."><F tag="PlastMode" map={{ 1: 'Pos.' }} wide /></L>
            <L label="Tempo de plast." unit="s"><F tag="PlastLastTime" d={2} /></L>
            <L label="RPM parafuso" unit="rpm"><F tag={null} /></L>
          </Line>
        </section>
        <section className="h-box">
          <Tab>Descomp.</Tab>
          <StageTable cols={['Pr. [bar]', 'Vel. [%]', 'Pos. [mm]']} rows={[
            { label: 'Antes descomp.', unit: '', tags: [null, null, 'DecompBefPos'] },
            { label: 'Após descomp.', unit: '', tags: ['DecompPres', 'DecompSpeed', 'DecompPos'] },
          ]} />
          <Line>
            <L label="Delay antes" unit="s"><F tag={null} d={2} /></L>
            <L label="Delay após" unit="s"><F tag={null} d={2} /></L>
            <L label="Tempo descomp." unit="s"><F tag="DecompTime" d={2} /></L>
          </Line>
        </section>
      </>
    ),
  },
  {
    id: 'temp', label: 'Temp.', code: '027', icon: <Thermometer size={22} />,
    sub: ['Temp', 'Curva', 'Parâm 1', 'Parâm 2', 'Parâm 3', 'Hot runner', 'Ex Temp'],
    tags: [...ZONES.flatMap((z) => [z.now, z.set, z.hi, z.lo, z.duty]), 'KeepWarm', 'TempOilNow',
      'ScrewColdProtectTIme', 'ScrewColdProtectUserTIme'],
    render: () => (
      <section className="h-box">
        <Tab>Temp</Tab>
        <table className="h-table temp">
          <thead><tr><th />{ZONES.map((z) => <th key={z.name}>{z.name}</th>)}</tr></thead>
          <tbody>
            <tr className="actual"><th scope="row">[°C]</th>{ZONES.map((z) => <td key={z.name}><F tag={z.now} /></td>)}</tr>
            <tr><th scope="row">Set</th>{ZONES.map((z) => <td key={z.name}><F tag={z.set} /></td>)}</tr>
            <tr><th scope="row">Tol. (+)</th>{ZONES.map((z) => <td key={z.name}><F tag={z.hi} /></td>)}</tr>
            <tr><th scope="row">Tol. (−)</th>{ZONES.map((z) => <td key={z.name}><F tag={z.lo} /></td>)}</tr>
            <tr><th scope="row">Espera</th>{ZONES.map((z) => <td key={z.name}><F tag={null} /></td>)}</tr>
            <tr><th scope="row">Ar fresc. [+]</th>{ZONES.map((z) => <td key={z.name}><F tag={null} /></td>)}</tr>
            <tr><th scope="row">Proporção saída</th>{ZONES.map((z) => <td key={z.name}><F tag={z.duty} d={2} /></td>)}</tr>
          </tbody>
        </table>
        <Line>
          <L label="Manter morno"><F tag="KeepWarm" map={{ 0: 'OFF', 1: 'ON' }} wide /></L>
          <L label="Temp. do óleo" unit="°C"><F tag="TempOilNow" /></L>
        </Line>
        <Line>
          <L label="Prevenção de resfr." unit="min"><F tag="ScrewColdProtectTIme" /><F tag={null} /></L>
        </Line>
        <Line>
          <L label="Prevenção de resfr. (usuário)" unit="min"><F tag="ScrewColdProtectUserTIme" /><F tag={null} /></L>
          <L label="Detecção proteção frio" unit="min"><F tag={null} /><F tag={null} /></L>
        </Line>
      </section>
    ),
  },
  {
    id: 'rapido', label: 'Set rápido', code: '005', icon: <Zap size={22} />,
    sub: ['Set rápido 1', 'Set rápido 2', 'Set rápido 3'],
    tags: ['Clamp1Pres', 'ClampLpPres', 'ClampHpPres', 'Clamp1Speed', 'ClampLpSpeed', 'ClampHpSpeed', 'Clamp1Pos',
      'Clamp4Pos', 'Clamp5Pos', 'Open1Pres', 'Open2Pres', 'Open3Pres', 'Open4Pres', 'Open1Speed', 'Open2Speed',
      'Open3Speed', 'Open4Speed', 'Open1Pos', 'Open2Pos', 'Open3Pos', 'Open4Pos', 'Inject1Pres', 'Inject1Speed',
      'Hold1Pres', 'Hold2Pres', 'Hold1Speed', 'Hold2Speed', 'Hold1Time', 'Hold2Time', 'Timer6'],
    render: () => (
      <section className="h-box">
        <Tab>Fecha</Tab>
        <StageTable cols={['Estág. 1', 'Baixa Pr.', 'Alta Pr.']} rows={[
          { label: 'Pr.', unit: 'bar', tags: ['Clamp1Pres', 'ClampLpPres', 'ClampHpPres'] },
          { label: 'Vel.', unit: '%', tags: ['Clamp1Speed', 'ClampLpSpeed', 'ClampHpSpeed'] },
          { label: 'Pos.', unit: 'mm', tags: ['Clamp1Pos', 'Clamp4Pos', 'Clamp5Pos'] },
        ]} />
        <Tab>Aberto</Tab>
        <StageTable cols={['Estág. 4', 'Estág. 3', 'Estág. 2', 'Estág. 1']} rows={[
          { label: 'Pr.', unit: 'bar', tags: ['Open4Pres', 'Open3Pres', 'Open2Pres', 'Open1Pres'] },
          { label: 'Vel.', unit: '%', tags: ['Open4Speed', 'Open3Speed', 'Open2Speed', 'Open1Speed'] },
          { label: 'Pos.', unit: 'mm', tags: ['Open4Pos', 'Open3Pos', 'Open2Pos', 'Open1Pos'] },
        ]} />
        <Tab>Injeção</Tab>
        <StageTable cols={['Estág. 1']} rows={[
          { label: 'Pr.', unit: 'bar', tags: ['Inject1Pres'] },
          { label: 'Vel.', unit: '%', tags: ['Inject1Speed'] },
          { label: 'Tempo', unit: 's', tags: [null], d: 3 },
        ]} />
        <Tab>Recalque</Tab>
        <StageTable cols={['Estág. 2', 'Estág. 1']} rows={[
          { label: 'Pr.', unit: 'bar', tags: ['Hold2Pres', 'Hold1Pres'] },
          { label: 'Vel.', unit: '%', tags: ['Hold2Speed', 'Hold1Speed'] },
          { label: 'Tempo', unit: 's', tags: ['Hold2Time', 'Hold1Time'], d: 2 },
        ]} />
        <Line><L label="Tempo resfr." unit="s"><F tag="Timer6" /></L></Line>
      </section>
    ),
  },
]

/* ---- moldura -------------------------------------------------------------- */

function StatusBar({ status, alarmText, code }: { status: Status | null; alarmText: string | null; code: string }) {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])
  const p = (n: number) => String(n).padStart(2, '0')
  const date = `${now.getFullYear()} . ${p(now.getMonth() + 1)} . ${p(now.getDate())}`
  const time = `${p(now.getHours())} : ${p(now.getMinutes())} : ${p(now.getSeconds())}`
  const online = status?.connected ?? false
  return (
    <>
      <div className="h-status">
        <span className="h-clock">{date}&nbsp;&nbsp;{time}</span>
        <span className={`h-alarm ${alarmText ? 'on' : ''}`} role={alarmText ? 'alert' : undefined}>
          {alarmText && <><AlertTriangle size={14} aria-hidden="true" /> {alarmText}</>}
        </span>
        <span className="h-net" title={online ? 'Gateway conectado' : 'Gateway sem conexão'}>
          {online ? <Signal size={18} /> : <SignalZero size={18} />}
        </span>
        <span className="h-code">{code}</span>
        <span className="h-user" title="Nível de acesso não é publicado pelo gateway"><UserRound size={18} /> —</span>
      </div>
      <div className="h-axes" title="Posições e pressões em tempo real não são publicadas pelo gateway">
        <span>Molde <b>—</b> mm</span><span>Extrator <b>—</b> mm</span><span>Injeção <b>—</b> mm</span>
        <span>Bico <b>—</b> mm</span><span>Rosca <b>—</b> rpm</span><span><Gauge size={14} aria-hidden="true" /> <b>—</b> bar</span>
      </div>
    </>
  )
}

export function HmiPanel({ status }: { status: Status | null }) {
  const [screenId, setScreenId] = useState('home')
  const screen = SCREENS.find((s) => s.id === screenId) ?? SCREENS[0]
  const [values, setValues] = useState<Values>({})
  const alarms = usePoll<Alarms>('/api/alarms?hours=1', 10000).data
  const alarmText = alarms?.active[0]?.label ?? null
  const tagParam = useMemo(() => screen.tags.join(','), [screen])

  useEffect(() => {
    if (!tagParam) return
    let alive = true
    const load = async () => {
      try {
        const r = await getJson<{ values: Values }>(`/api/values?tags=${encodeURIComponent(tagParam)}`)
        if (alive) setValues((prev) => ({ ...prev, ...r.values }))
      } catch {
        /* o cabeçalho do monitor já avisa quando o backend cai */
      }
    }
    load()
    const id = window.setInterval(load, 3000)
    return () => { alive = false; window.clearInterval(id) }
  }, [tagParam])

  return (
    <div className="hmi-wrap">
      <div className="hmi" aria-label="Réplica da tela do painel da máquina">
        <StatusBar status={status} alarmText={alarmText} code={screen.code} />
        <ValuesCtx.Provider value={values}>
          <div className="h-body">{screen.render()}</div>
        </ValuesCtx.Provider>
        {screen.sub && <SubTabs items={screen.sub} />}
        <nav className="h-nav" aria-label="Telas do painel">
          {SCREENS.map((s) => (
            <button key={s.id} className={`${s.id === screen.id ? 'on' : ''} ${s.id === 'menu' ? 'menu' : ''}`}
              aria-current={s.id === screen.id ? 'page' : undefined} onClick={() => setScreenId(s.id)} title={s.label}>
              {s.icon}<span>{s.label}</span>
            </button>
          ))}
        </nav>
      </div>
      <aside className="hmi-note">
        <h2>Como ler esta tela</h2>
        <p>É uma réplica das telas do painel CHY com os valores reais lidos do gateway, atualizados a cada poucos
          segundos. Trocar de tela aqui não muda a tela do painel físico, e nenhum valor pode ser alterado.</p>
        <p>Campos com <span className="hf na">—</span> existem no painel, mas o gateway não publica esse dado
          (posições em tempo real, delays, senhas). Passe o mouse sobre um valor para ver a variável de origem.</p>
      </aside>
    </div>
  )
}
