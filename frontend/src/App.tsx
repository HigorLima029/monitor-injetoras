import { AlertTriangle, Factory, Server, MonitorPlay, LayoutDashboard, ListTree, Monitor, Moon, Package, Sun, SunMoon, Thermometer } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { Info, MachineList } from './api'
import { activateMachine, getJson, usePoll, useLive } from './api'
import { MachinesPage } from './MachinesPage'
import { StateBadge } from './components/widgets'
import { HmiPanel } from './hmi/HmiPanel'
import { TelaAoVivo } from './TelaAoVivo'
import { ago } from './format'
import { AlarmsPage, Overview, ProductionPage, Temperatures, Variables, type PageProps } from './pages'
import { useTheme, type ThemeChoice } from './theme'

type PageId = 'maquinas' | 'geral' | 'painel' | 'tela' | 'temperaturas' | 'producao' | 'alarmes' | 'variaveis'

const PAGES: { id: PageId; label: string; icon: ReactNode; render: (p: PageProps) => ReactNode }[] = [
  { id: 'maquinas', label: 'Máquinas', icon: <Server size={18} />, render: () => null /* desenhada no App, que passa os callbacks */ },
  { id: 'geral', label: 'Visão geral', icon: <LayoutDashboard size={18} />, render: (p) => <Overview {...p} /> },
  { id: 'painel', label: 'Painel da máquina', icon: <Monitor size={18} />, render: (p) => <HmiPanel status={p.live?.status ?? null} /> },
  // key: trocar de máquina monta uma Tela ao vivo nova (senha e sessão do painel novo).
  { id: 'tela', label: 'Tela ao vivo', icon: <MonitorPlay size={18} />, render: (p) => <TelaAoVivo key={p.info.machine_id} machineName={p.info.machine_name} /> },
  { id: 'temperaturas', label: 'Temperaturas', icon: <Thermometer size={18} />, render: (p) => <Temperatures {...p} /> },
  { id: 'producao', label: 'Produção', icon: <Package size={18} />, render: (p) => <ProductionPage {...p} /> },
  { id: 'alarmes', label: 'Alarmes', icon: <AlertTriangle size={18} />, render: (p) => <AlarmsPage {...p} /> },
  { id: 'variaveis', label: 'Variáveis', icon: <ListTree size={18} />, render: (p) => <Variables {...p} /> },
]

const THEME_NEXT: Record<ThemeChoice, ThemeChoice> = { auto: 'light', light: 'dark', dark: 'auto' }
const THEME_TEXT: Record<ThemeChoice, string> = { auto: 'Tema: do sistema', light: 'Tema: claro', dark: 'Tema: escuro' }

function pageFromHash(): PageId {
  const h = window.location.hash.replace('#', '') as PageId
  return PAGES.some((p) => p.id === h) ? h : 'geral'
}

export default function App() {
  const [page, setPage] = useState<PageId>(pageFromHash)
  const { choice, setChoice, tokens } = useTheme()
  const { live, linked, changedTo, changeSeq, openSeq } = useLive()
  const { data: info, error: infoError, reload: reloadInfo, set: setInfo } = usePoll<Info>('/api/info', 30000)
  const { data: machines, reload: reloadMachines } = usePoll<MachineList>('/api/machines', 30000)
  const [now, setNow] = useState(() => Date.now() / 1000)
  const [switchError, setSwitchError] = useState<string | null>(null)
  const [changedNotice, setChangedNotice] = useState<string | null>(null)
  // Máquina que esta aba está mostrando: a faixa "mudou" só aparece se a nova for outra
  // (reabrir a mesma máquina, ex. depois de editar o IP ou reiniciar, não é uma troca).
  const shownId = useRef<number | null>(null)
  useEffect(() => { if (info) shownId.current = info.machine_id ?? null }, [info])

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000)
    const onHash = () => setPage(pageFromHash())
    window.addEventListener('hashchange', onHash)
    return () => { window.clearInterval(id); window.removeEventListener('hashchange', onHash) }
  }, [])

  const go = (id: PageId) => { window.location.hash = id; setPage(id) }
  const status = live?.status
  const activeCount = status?.active_alarms.length ?? 0
  const noMachine = machines !== null && machines.active_id === null

  // A máquina aberta mudou (por esta aba ou por outra pessoa): recarrega tudo e avisa por 8 s.
  // Logo depois da troca o servidor ainda pode estar fechando a máquina anterior (503 em 4G):
  // tenta o /api/info de novo, cada vez esperando mais, por uns 30 s, até vir a máquina nova.
  useEffect(() => {
    if (changeSeq === 0) return
    let stale = false
    const wait = (ms: number) => new Promise((done) => window.setTimeout(done, ms))
    const before = shownId.current
    ;(async () => {
      reloadMachines()
      let fresh: Info | null = null
      for (const ms of [0, 500, 1000, 2000, 4000, 8000, 8000, 8000]) {
        if (ms) await wait(ms)
        if (stale) return
        const got = await getJson<Info>('/api/info').catch(() => null)
        if (stale) return
        if (got && (changedTo === null || got.machine_id === changedTo)) { fresh = got; break }
      }
      if (fresh) setInfo(fresh)
      reloadInfo()
      const list = await getJson<MachineList>('/api/machines').catch(() => null)
      if (stale) return
      reloadMachines()
      const name = fresh?.machine_name ?? list?.machines.find((m) => m.id === changedTo)?.name
      const reallyChanged = changedTo === null || before === null || changedTo !== before
      if (name && reallyChanged) setChangedNotice(`A máquina aberta mudou para ${name}.`)
    })()
    return () => { stale = true }
  }, [changeSeq, changedTo, reloadInfo, reloadMachines, setInfo])

  // O fluxo ao vivo voltou depois de cair: a máquina pode ter mudado enquanto isso.
  useEffect(() => {
    if (openSeq > 1) { reloadInfo(); reloadMachines() }
  }, [openSeq, reloadInfo, reloadMachines])

  useEffect(() => {
    if (!changedNotice) return
    const id = window.setTimeout(() => setChangedNotice(null), 8000)
    return () => window.clearTimeout(id)
  }, [changedNotice])

  // A réplica da tela é da IHM CHY; num painel B&R/Beckhoff ela não se aplica.
  const pages = noMachine
    ? PAGES.filter((p) => p.id === 'maquinas')
    : PAGES.filter((p) =>
        (p.id !== 'painel' || info?.kind === 'chy' || info?.kind === 'demo' || (info?.kind === undefined && info?.profile === 'chy')) &&
        (p.id !== 'tela' || info?.vnc === true))
  // Sem máquina aberta só existe Máquinas; com ela, uma página que deixou de existir cai na Visão geral.
  const current = pages.find((p) => p.id === page) ?? pages.find((p) => p.id === 'geral') ?? pages[0]
  const currentId = current.id

  // Ao mudar de tipo, uma página que não existe mais no novo tipo volta para a Visão geral.
  useEffect(() => {
    if (!info || noMachine) return
    if (page !== currentId) go(currentId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info?.machine_id, info?.kind, info?.vnc, noMachine])
  useEffect(() => {
    if (noMachine && page !== 'maquinas') go('maquinas')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [noMachine])

  const opened = () => { setSwitchError(null); reloadInfo(); reloadMachines() }
  const switchTo = async (id: number) => {
    try {
      await activateMachine(id)
      opened()
    } catch (e) {
      setSwitchError(e instanceof Error ? e.message : String(e))
      reloadMachines()
    }
  }
  const brandSub = info?.profile === 'em63' ? 'OPC-UA · painel B&R'
    : info?.profile === 'ads' ? 'ADS · painel Beckhoff' : 'OPC-UA · gateway Mega'
  // 503 = nenhuma máquina aberta (ainda não sabemos pelo /api/machines): não é servidor fora do ar.
  const mgrDown = infoError && !noMachine && !infoError.includes(' 503 ')

  return (
    <div className="shell">
      <nav className="rail" aria-label="Seções">
        <div className="brand">
          <Factory size={26} aria-hidden="true" />
          <div><strong>Monitor de Injetoras</strong><span>{brandSub}</span></div>
        </div>
        <div className="nav">
          {pages.map((p) => (
            <button key={p.id} aria-current={currentId === p.id ? 'page' : undefined} onClick={() => go(p.id)}>
              {p.icon}{p.label}
              {p.id === 'alarmes' && activeCount > 0 && <span className="count" aria-label={`${activeCount} ativos`}>{activeCount}</span>}
            </button>
          ))}
        </div>
        <div className="rail-foot">
          <button onClick={() => setChoice(THEME_NEXT[choice])} title="Trocar tema">
            {choice === 'dark' ? <Moon size={15} /> : choice === 'light' ? <Sun size={15} /> : <SunMoon size={15} />}
            {THEME_TEXT[choice]}
          </button>
          <span>
            {info?.vnc_control
              ? 'Dados só leitura. Operar a tela exige a senha de operação, um operador por vez.'
              : 'Somente leitura: este painel nunca altera a máquina.'}
          </span>
        </div>
      </nav>

      <main className="main">
        <header className="machine-bar">
          <h1>{noMachine ? 'Nenhuma máquina aberta' : (info?.machine_name ?? 'Injetora')}</h1>
          {!noMachine && <StateBadge state={status?.state ?? 'offline'} />}
          {info?.simulate && !noMachine && <span className="sim-flag">Dados simulados</span>}
          {machines && machines.machines.length > 0 && (
            <label className="picker">
              <span>Máquina</span>
              <select
                value={machines.active_id ?? ''}
                onChange={(e) => switchTo(Number(e.target.value))}
                aria-label="Máquina aberta"
              >
                {machines.active_id === null && <option value="" disabled>Escolha uma máquina</option>}
                {machines.machines.map((m) => <option key={m.id} value={m.id}>{m.name} · {m.client}</option>)}
              </select>
            </label>
          )}
          <span className="spacer" />
          {!noMachine && (
            <div className="meta">
              <span>Última leitura <b>{ago(status?.last_ok ?? null, now)}</b></span>
              <span>{info?.kind === 'ads' ? 'Controlador' : 'Gateway'} <b>{info?.opcua_url.replace(/^(opc\.tcp|ads):\/\//, '') ?? '—'}</b></span>
              <span>Variáveis <b>{status?.tag_count ?? '—'}</b></span>
            </div>
          )}
        </header>

        {switchError && (
          <div className="banner error" role="alert">
            <AlertTriangle size={18} aria-hidden="true" />
            <p>Não consegui trocar de máquina. {switchError}</p>
          </div>
        )}
        {changedNotice && (
          <div className="banner info" role="status">
            <Server size={18} aria-hidden="true" />
            <p>{changedNotice}</p>
          </div>
        )}
        {mgrDown && (
          <div className="banner error" role="alert">
            <AlertTriangle size={18} aria-hidden="true" />
            <p>O servidor do monitor não respondeu. Confira se o backend está rodando (veja o README) e recarregue a página.</p>
          </div>
        )}
        {noMachine && currentId === 'maquinas' && (
          <div className="banner" role="status">
            <AlertTriangle size={18} aria-hidden="true" />
            <p>Nenhuma máquina aberta. Cadastre uma ou abra uma já cadastrada para ver os dados.</p>
          </div>
        )}
        {!mgrDown && !noMachine && status && !status.connected && (
          <div className="banner" role="alert">
            <AlertTriangle size={18} aria-hidden="true" />
            <p>
              Sem conexão com {info?.kind === 'ads' ? 'o controlador' : 'o gateway'}. O monitor tenta de novo a cada 5 s.
              {status.last_error && <> Último erro: <span className="muted">{status.last_error}</span>.</>}
              {' '}Confira a rede e o endereço da máquina em Máquinas.
            </p>
          </div>
        )}
        {!linked && !mgrDown && !noMachine && <p className="muted small">Reconectando ao fluxo ao vivo…</p>}

        {currentId === 'maquinas'
          ? <MachinesPage onOpened={opened} onChanged={() => { reloadMachines(); reloadInfo() }} />
          : info ? current.render({ info, live, tokens, now }) : !infoError && <p className="muted">Carregando…</p>}
      </main>
    </div>
  )
}
