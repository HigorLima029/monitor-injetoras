/**
 * Réplica simulada das telas do painel ESTO (Spark totalmente elétrica, servos Inovance),
 * alimentada pelo OPC-UA do próprio painel (variáveis do opcuaparam.xml, lidas como INO.<nome>).
 *
 * Só exibe valores: nenhum campo é editável e nada é enviado à máquina.
 * Os valores aparecem como o painel publica no OPC-UA (sem conversão de escala).
 */
import {
  AlertTriangle, ArrowDownUp, Activity, Columns2, Home, ListChecks, Syringe, Thermometer, Waves,
} from 'lucide-react'
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { Alarms, Status } from '../api'
import { getJson, num, usePoll } from '../api'
import './esto.css'

type Values = Record<string, number | string | null>
const ValuesCtx = createContext<Values>({})

const ino = (name: string) => `INO.${name}`

function fmt(v: number | null, d: number) {
  if (v === null) return '—'
  return v.toLocaleString('pt-BR', { minimumFractionDigits: d, maximumFractionDigits: d })
}

/** Caixa de valor laranja, como no painel. */
function V({ tag, d = 1, unit }: { tag: string; d?: number; unit?: string }) {
  const values = useContext(ValuesCtx)
  const v = num(values[tag])
  return (
    <span className="e-v" title={tag.replace(/^INO\./, '')}>
      <b>{fmt(v, d)}</b>{unit && <i>{unit}</i>}
    </span>
  )
}

function Box({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="e-box">
      <h3 className="e-title">{title}</h3>
      {children}
    </section>
  )
}

/** Tabela com colunas por estágio (1, 2, 3...) e uma linha por grandeza. */
function Stages({ cols, rows }: {
  cols: string[]
  rows: { label: string; unit: string; names: (string | null)[]; d?: number }[]
}) {
  return (
    <table className="e-table">
      <thead>
        <tr><th />{cols.map((c) => <th key={c}>{c}</th>)}<th className="u" /></tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.label}>
            <th scope="row">{r.label}</th>
            {r.names.map((n, i) => <td key={i}>{n === null ? <span className="e-v na">—</span> : <V tag={ino(n)} d={r.d ?? 1} />}</td>)}
            <td className="u">{r.unit}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** Linha "rótulo: valor unidade" usada nas telas de monitoramento. */
function Row({ label, name, unit, d = 2 }: { label: string; name: string; unit: string; d?: number }) {
  return (
    <div className="e-row">
      <span>{label}</span>
      <V tag={ino(name)} d={d} unit={unit} />
    </div>
  )
}

const ZONES = [1, 2, 3, 4, 5, 6, 7]
// Mesma ordem da Visão geral (Temp1 = bico); a 7ª ainda não foi identificada na máquina.
const ZONE_NAMES = ['Bico', 'Zona 1', 'Zona 2', 'Zona 3', 'Zona 4', 'Zona 5', 'Temp. 7']
const range = (a: number, b: number) => Array.from({ length: b - a + 1 }, (_, i) => a + i)

/* ---- telas ---------------------------------------------------------------- */

interface Screen {
  id: string
  label: string
  icon: ReactNode
  names: string[]
  render: () => ReactNode
}

const TIMES: [string, string][] = [
  ['Fechamento', 'Close Tm Real'], ['Fech. baixa pressão', 'CloseLowPrs Tm Real'],
  ['Fech. alta pressão', 'CloseHiPrs Tm Real'], ['Avanço do carro', 'NozAdv Tm Real'],
  ['Injeção', 'Inj Tm Real'], ['Dosagem', 'Charge Tm Real'], ['Descompressão', 'SuckBack Time Real'],
  ['Recuo do carro', 'NozRet Tm Real'], ['Abertura', 'Open Tm Real'], ['Extrator', 'Eject Tm Real'],
]

function ModeBadge() {
  const values = useContext(ValuesCtx)
  const s = num(values[ino('Operate State')])
  const text = s === null ? '—' : ({ 0: 'Manual', 3: 'Semiautomático', 5: 'Automático (fotocélula)', 9: 'Automático (tempo)' } as Record<number, string>)[s] ?? `Código ${s}`
  return <span className={`e-mode ${s !== null && s !== 0 ? 'auto' : ''}`}>{text}</span>
}

function Barrel() {
  return (
    <div className="e-barrel">
      {ZONES.map((z, i) => (
        <div className="e-zone" key={z}>
          <span className="e-zname">{ZONE_NAMES[i]}</span>
          <V tag={ino(`Temp${z} Real`)} d={0} unit="°C" />
          <span className="e-zset">Prog. <V tag={ino(`Temp${z} Set`)} d={0} /></span>
        </div>
      ))}
    </div>
  )
}

const SCREENS: Screen[] = [
  {
    id: 'main', label: 'Principal', icon: <Home size={22} />,
    names: ['Operate State', ...ZONES.flatMap((z) => [`Temp${z} Real`, `Temp${z} Set`]), ...TIMES.map((t) => t[1]),
      'CycTime Real', 'ProdTm Real', 'OpenCnt Hi Real', 'OpenCnt Low Real', 'Max InjPrs Real', 'RestmillPos Real'],
    render: () => (
      <>
        <Box title="Canhão — temperatura real e programada"><Barrel /></Box>
        <Box title="Tempos das ações do último ciclo">
          <div className="e-grid">
            {TIMES.map(([label, name]) => <Row key={name} label={label} name={name} unit="s" />)}
          </div>
        </Box>
      </>
    ),
  },
  {
    id: 'temp', label: 'Temperatura', icon: <Thermometer size={22} />,
    names: ZONES.flatMap((z) => [`Temp${z} Real`, `Temp${z} Set`]),
    render: () => (
      <Box title="Temperatura do canhão">
        <Stages cols={ZONE_NAMES} rows={[
          { label: 'Programado', unit: '°C', names: ZONES.map((z) => `Temp${z} Set`), d: 0 },
          { label: 'Real', unit: '°C', names: ZONES.map((z) => `Temp${z} Real`), d: 0 },
        ]} />
      </Box>
    ),
  },
  {
    id: 'inj', label: 'Injeção', icon: <Syringe size={22} />,
    names: [...range(1, 5).flatMap((n) => [`InjPrs#${n} Set`, `InjSpd#${n} Set`]), ...range(1, 4).map((n) => `InjEndPos#${n} Set`),
      ...range(1, 4).flatMap((n) => [`HoldPrs#${n} Set`, `HoldSpd#${n} Set`, `HoldTm#${n} Set`]),
      'InjTm Set', 'TurnHoldPos Set', 'TurnHold Prs Real', 'TurnHold Spd Real', 'Inj Avg Spd Real', 'Max InjPrs Real',
      'Max HoldPrs Real', 'Hold MoveDist Real', 'InjStartPos Real', 'HoldStartPos Real', 'RestmillPos Real'],
    render: () => (
      <>
        <Box title="Injeção">
          <Stages cols={range(1, 5).map((n) => `Estág. ${n}`)} rows={[
            { label: 'Pressão', unit: 'MPa', names: range(1, 5).map((n) => `InjPrs#${n} Set`) },
            { label: 'Velocidade', unit: 'mm/s', names: range(1, 5).map((n) => `InjSpd#${n} Set`) },
            { label: 'Posição final', unit: 'mm', names: [...range(1, 4).map((n) => `InjEndPos#${n} Set`), null], d: 2 },
          ]} />
          <div className="e-line">
            <Row label="Tempo de injeção" name="InjTm Set" unit="s" />
            <Row label="Posição de comutação" name="TurnHoldPos Set" unit="mm" />
          </div>
        </Box>
        <Box title="Recalque">
          <Stages cols={range(1, 4).map((n) => `Estág. ${n}`)} rows={[
            { label: 'Pressão', unit: 'MPa', names: range(1, 4).map((n) => `HoldPrs#${n} Set`) },
            { label: 'Velocidade', unit: 'mm/s', names: range(1, 4).map((n) => `HoldSpd#${n} Set`) },
            { label: 'Tempo', unit: 's', names: range(1, 4).map((n) => `HoldTm#${n} Set`), d: 2 },
          ]} />
        </Box>
        <Box title="Último ciclo">
          <div className="e-grid">
            <Row label="Início da injeção" name="InjStartPos Real" unit="mm" />
            <Row label="Início do recalque" name="HoldStartPos Real" unit="mm" />
            <Row label="Colchão (fim da injeção)" name="RestmillPos Real" unit="mm" />
            <Row label="Pressão na comutação" name="TurnHold Prs Real" unit="MPa" d={1} />
            <Row label="Velocidade na comutação" name="TurnHold Spd Real" unit="mm/s" d={1} />
            <Row label="Velocidade média" name="Inj Avg Spd Real" unit="mm/s" d={1} />
            <Row label="Pressão máx. injeção" name="Max InjPrs Real" unit="MPa" d={1} />
            <Row label="Pressão máx. recalque" name="Max HoldPrs Real" unit="MPa" d={1} />
            <Row label="Curso no recalque" name="Hold MoveDist Real" unit="mm" />
          </div>
        </Box>
      </>
    ),
  },
  {
    id: 'dos', label: 'Dosagem', icon: <Waves size={22} />,
    names: [...range(1, 3).flatMap((n) => [`ChargeBackPrs#${n} Set`, `ChargeSpd#${n} Set`, `ChargeEndPos#${n} Set`]),
      'SuckBackSpd Set', 'SuckBackDistance Set', 'Charge Pos Real', 'Charge Tm Real', 'SuckBack Time Real', 'SuckBack EndPos Real'],
    render: () => (
      <>
        <Box title="Dosagem">
          <Stages cols={range(1, 3).map((n) => `Estág. ${n}`)} rows={[
            { label: 'Contrapressão', unit: 'MPa', names: range(1, 3).map((n) => `ChargeBackPrs#${n} Set`) },
            { label: 'Velocidade', unit: 'rpm', names: range(1, 3).map((n) => `ChargeSpd#${n} Set`) },
            { label: 'Posição final', unit: 'mm', names: range(1, 3).map((n) => `ChargeEndPos#${n} Set`), d: 2 },
          ]} />
        </Box>
        <Box title="Descompressão">
          <div className="e-line">
            <Row label="Velocidade" name="SuckBackSpd Set" unit="mm/s" d={1} />
            <Row label="Distância" name="SuckBackDistance Set" unit="mm" />
          </div>
        </Box>
        <Box title="Último ciclo">
          <div className="e-grid">
            <Row label="Fim da dosagem" name="Charge Pos Real" unit="mm" />
            <Row label="Tempo de dosagem" name="Charge Tm Real" unit="s" />
            <Row label="Tempo de descompressão" name="SuckBack Time Real" unit="s" />
            <Row label="Fim da descompressão" name="SuckBack EndPos Real" unit="mm" />
          </div>
        </Box>
      </>
    ),
  },
  {
    id: 'molde', label: 'Molde', icon: <Columns2 size={22} />,
    names: [...range(1, 3).flatMap((n) => [`CloseSpd#${n} Set`, `ClosePos#${n} Set`]), 'CloseLowSpd Set', 'CloseHiSpd Set', 'CloseLowPos Set',
      ...range(1, 4).flatMap((n) => [`OpenSpd#${n} Set`, `OpenPos#${n} Set`]), 'OpenSpdEnd Set', 'OpenPosEnd Set', 'Open EndPos Real'],
    render: () => (
      <>
        <Box title="Fechamento">
          <Stages cols={['Estág. 1', 'Estág. 2', 'Estág. 3', 'Baixa pr.', 'Alta pr.']} rows={[
            { label: 'Velocidade', unit: 'mm/s', names: ['CloseSpd#1 Set', 'CloseSpd#2 Set', 'CloseSpd#3 Set', 'CloseLowSpd Set', 'CloseHiSpd Set'] },
            { label: 'Posição', unit: 'mm', names: ['ClosePos#1 Set', 'ClosePos#2 Set', 'ClosePos#3 Set', 'CloseLowPos Set', null], d: 2 },
          ]} />
        </Box>
        <Box title="Abertura">
          <Stages cols={['Estág. 1', 'Estág. 2', 'Estág. 3', 'Estág. 4', 'Final']} rows={[
            { label: 'Velocidade', unit: 'mm/s', names: [...range(1, 4).map((n) => `OpenSpd#${n} Set`), 'OpenSpdEnd Set'] },
            { label: 'Posição', unit: 'mm', names: [...range(1, 4).map((n) => `OpenPos#${n} Set`), 'OpenPosEnd Set'], d: 2 },
          ]} />
          <div className="e-line"><Row label="Fim da abertura (último ciclo)" name="Open EndPos Real" unit="mm" /></div>
        </Box>
      </>
    ),
  },
  {
    id: 'extr', label: 'Extrator', icon: <ArrowDownUp size={22} />,
    names: range(1, 2).flatMap((n) => [`EjtAdvSpd#${n} Set`, `EjtAdvPos#${n} Set`, `EjtRetSpd#${n} Set`, `EjtRetPos#${n} Set`]).concat(['Eject Tm Real']),
    render: () => (
      <Box title="Extrator">
        <Stages cols={['Avanço 1', 'Avanço 2', 'Recuo 1', 'Recuo 2']} rows={[
          { label: 'Velocidade', unit: 'mm/s', names: ['EjtAdvSpd#1 Set', 'EjtAdvSpd#2 Set', 'EjtRetSpd#1 Set', 'EjtRetSpd#2 Set'] },
          { label: 'Posição', unit: 'mm', names: ['EjtAdvPos#1 Set', 'EjtAdvPos#2 Set', 'EjtRetPos#1 Set', 'EjtRetPos#2 Set'], d: 2 },
        ]} />
        <div className="e-line"><Row label="Tempo do extrator (último ciclo)" name="Eject Tm Real" unit="s" /></div>
      </Box>
    ),
  },
  {
    id: 'qc', label: 'Monitor', icon: <Activity size={22} />,
    names: ['CycTime Real', 'ProdTm Real', ...TIMES.map((t) => t[1]), 'InjStartPos Real', 'HoldStartPos Real', 'RestmillPos Real',
      'Charge Pos Real', 'Open EndPos Real', 'TurnHold Prs Real', 'TurnHold Spd Real', 'SuckBack EndPos Real', 'Inj Avg Spd Real',
      'Max InjPrs Real', 'Max HoldPrs Real', 'Hold MoveDist Real'],
    render: () => (
      <Box title="Monitoramento de qualidade — último ciclo">
        <div className="e-grid">
          <Row label="Tempo de ciclo" name="CycTime Real" unit="s" />
          <Row label="Tempo de produto" name="ProdTm Real" unit="s" />
          {TIMES.map(([label, name]) => <Row key={name} label={`Tempo: ${label.toLowerCase()}`} name={name} unit="s" />)}
          <Row label="Início da injeção" name="InjStartPos Real" unit="mm" />
          <Row label="Início do recalque" name="HoldStartPos Real" unit="mm" />
          <Row label="Colchão" name="RestmillPos Real" unit="mm" />
          <Row label="Fim da dosagem" name="Charge Pos Real" unit="mm" />
          <Row label="Fim da descompressão" name="SuckBack EndPos Real" unit="mm" />
          <Row label="Fim da abertura" name="Open EndPos Real" unit="mm" />
          <Row label="Pressão na comutação" name="TurnHold Prs Real" unit="MPa" d={1} />
          <Row label="Velocidade na comutação" name="TurnHold Spd Real" unit="mm/s" d={1} />
          <Row label="Velocidade média injeção" name="Inj Avg Spd Real" unit="mm/s" d={1} />
          <Row label="Pressão máx. injeção" name="Max InjPrs Real" unit="MPa" d={1} />
          <Row label="Pressão máx. recalque" name="Max HoldPrs Real" unit="MPa" d={1} />
          <Row label="Curso no recalque" name="Hold MoveDist Real" unit="mm" />
        </div>
      </Box>
    ),
  },
  {
    id: 'alarm', label: 'Alarmes', icon: <ListChecks size={22} />,
    names: [],
    render: () => <AlarmScreen />,
  },
]

function AlarmScreen() {
  const alarms = usePoll<Alarms>('/api/alarms?hours=24', 5000).data
  const fmtTime = (ts: number) => new Date(ts * 1000).toLocaleString('pt-BR')
  return (
    <>
      <Box title="Alarmes ativos">
        {!alarms ? <p className="e-empty">Carregando…</p>
          : alarms.active.length === 0 ? <p className="e-empty">Nenhum alarme ativo.</p>
            : <ul className="e-alarms">{alarms.active.map((a) => <li key={a.tag}><AlertTriangle size={15} aria-hidden="true" /> {a.label}</li>)}</ul>}
      </Box>
      <Box title="Histórico (24 h)">
        {alarms && alarms.events.length > 0 ? (
          <table className="e-table e-hist">
            <thead><tr><th>Alarme</th><th>Início</th><th>Fim</th></tr></thead>
            <tbody>
              {alarms.events.slice(0, 30).map((e, i) => (
                <tr key={`${e.tag}-${e.started}-${i}`}>
                  <td>{e.label}</td><td>{fmtTime(e.started)}</td><td>{e.ended ? fmtTime(e.ended) : 'ativo'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="e-empty">Nenhum alarme nas últimas 24 h.</p>}
      </Box>
    </>
  )
}

/* ---- moldura -------------------------------------------------------------- */

const HEADER_NAMES = ['Inj Tm Real', 'RestmillPos Real', 'Charge Pos Real', 'CycTime Real', 'ProdTm Real', 'Operate State']

function Header({ status, alarmText }: { status: Status | null; alarmText: string | null }) {
  const values = useContext(ValuesCtx)
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])
  const hi = num(values[ino('OpenCnt Hi Real')])
  const lo = num(values[ino('OpenCnt Low Real')])
  const count = hi === null && lo === null ? null : (hi ?? 0) * 65536 + (lo ?? 0)
  const online = status?.connected ?? false
  return (
    <>
      <div className={`e-bar ${alarmText ? 'alarm' : ''}`} role={alarmText ? 'alert' : undefined}>
        <span className="e-logo" aria-hidden="true">CH</span>
        <span className="e-bartext">{alarmText ?? (online ? 'Sem alarmes' : 'Sem conexão com o painel')}</span>
        <span className="e-clock">{now.toLocaleDateString('pt-BR')} {now.toLocaleTimeString('pt-BR')}</span>
      </div>
      <div className="e-head">
        <div className="e-hcol">
          <span>Tempo de injeção <V tag={ino('Inj Tm Real')} d={2} unit="s" /></span>
          <span>Colchão <V tag={ino('RestmillPos Real')} d={2} unit="mm" /></span>
          <span>Fim da dosagem <V tag={ino('Charge Pos Real')} d={2} unit="mm" /></span>
        </div>
        <div className="e-hcol">
          <span>Tempo de ciclo <V tag={ino('CycTime Real')} d={2} unit="s" /></span>
          <span>Tempo de produto <V tag={ino('ProdTm Real')} d={2} unit="s" /></span>
          <span>Contador <span className="e-v"><b>{fmt(count, 0)}</b></span></span>
        </div>
        <div className="e-hmode"><ModeBadge /></div>
      </div>
    </>
  )
}

export function EstoPanel({ status }: { status: Status | null }) {
  const [screenId, setScreenId] = useState('main')
  const screen = SCREENS.find((s) => s.id === screenId) ?? SCREENS[0]
  const [values, setValues] = useState<Values>({})
  const alarms = usePoll<Alarms>('/api/alarms?hours=1', 10000).data
  const alarmText = alarms?.active[0]?.label ?? null
  const tagParam = useMemo(
    () => [...new Set([...HEADER_NAMES, 'OpenCnt Hi Real', 'OpenCnt Low Real', ...screen.names])].map(ino).join(','),
    [screen],
  )

  useEffect(() => {
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
      <div className="esto" aria-label="Réplica simulada da tela do painel da máquina">
        <ValuesCtx.Provider value={values}>
          <Header status={status} alarmText={alarmText} />
          <div className="e-body">{screen.render()}</div>
        </ValuesCtx.Provider>
        <nav className="e-nav" aria-label="Telas do painel">
          {SCREENS.map((s) => (
            <button key={s.id} className={s.id === screen.id ? 'on' : ''} aria-current={s.id === screen.id ? 'page' : undefined}
              onClick={() => setScreenId(s.id)} title={s.label}>
              {s.icon}<span>{s.label}</span>
            </button>
          ))}
        </nav>
      </div>
      <aside className="hmi-note">
        <h2>Como ler esta tela</h2>
        <p>É uma réplica simulada das telas do painel da Spark (painel ESTO, servos Inovance), com os valores reais
          lidos do OPC-UA do painel a cada poucos segundos. Trocar de tela aqui não muda o painel físico, e nenhum
          valor pode ser alterado.</p>
        <p>Os números aparecem como o painel publica. Se algum estiver com a vírgula no lugar errado em relação à tela
          da máquina, a escala daquele campo precisa de ajuste. Passe o mouse sobre um valor para ver a variável de origem.</p>
        <p>Campos com <span className="e-v na">—</span> existem na tela, mas o painel não publica esse dado.</p>
      </aside>
    </div>
  )
}
