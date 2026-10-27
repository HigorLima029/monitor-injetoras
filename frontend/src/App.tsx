import { AlertTriangle, Factory, LayoutDashboard, ListTree, Monitor, Moon, Package, Sun, SunMoon, Thermometer } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import type { Info } from './api'
import { usePoll, useLive } from './api'
import { StateBadge } from './components/widgets'
import { HmiPanel } from './hmi/HmiPanel'
import { ago } from './format'
import { AlarmsPage, Overview, ProductionPage, Temperatures, Variables, type PageProps } from './pages'
import { useTheme, type ThemeChoice } from './theme'

type PageId = 'geral' | 'painel' | 'temperaturas' | 'producao' | 'alarmes' | 'variaveis'

const PAGES: { id: PageId; label: string; icon: ReactNode; render: (p: PageProps) => ReactNode }[] = [
  { id: 'geral', label: 'Visão geral', icon: <LayoutDashboard size={18} />, render: (p) => <Overview {...p} /> },
  { id: 'painel', label: 'Painel da máquina', icon: <Monitor size={18} />, render: (p) => <HmiPanel status={p.live?.status ?? null} /> },
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
  const { live, linked } = useLive()
  const { data: info, error: infoError } = usePoll<Info>('/api/info', 300000)
  const [now, setNow] = useState(() => Date.now() / 1000)

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000)
    const onHash = () => setPage(pageFromHash())
    window.addEventListener('hashchange', onHash)
    return () => { window.clearInterval(id); window.removeEventListener('hashchange', onHash) }
  }, [])

  const go = (id: PageId) => { window.location.hash = id; setPage(id) }
  const status = live?.status
  const activeCount = status?.active_alarms.length ?? 0
  const current = PAGES.find((p) => p.id === page) ?? PAGES[0]

  return (
    <div className="shell">
      <nav className="rail" aria-label="Seções">
        <div className="brand">
          <Factory size={26} aria-hidden="true" />
          <div><strong>Monitor de Injetoras</strong><span>OPC-UA · gateway Mega</span></div>
        </div>
        <div className="nav">
          {PAGES.map((p) => (
            <button key={p.id} aria-current={page === p.id ? 'page' : undefined} onClick={() => go(p.id)}>
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
          <span>Somente leitura: este painel nunca altera a máquina.</span>
        </div>
      </nav>

      <main className="main">
        <header className="machine-bar">
          <h1>{info?.machine_name ?? 'Injetora'}</h1>
          <StateBadge state={status?.state ?? 'offline'} />
          {info?.simulate && <span className="sim-flag">Dados simulados</span>}
          <span className="spacer" />
          <div className="meta">
            <span>Última leitura <b>{ago(status?.last_ok ?? null, now)}</b></span>
            <span>Gateway <b>{info?.opcua_url.replace('opc.tcp://', '') ?? '—'}</b></span>
            <span>Variáveis <b>{status?.tag_count ?? '—'}</b></span>
          </div>
        </header>

        {infoError && (
          <div className="banner error" role="alert">
            <AlertTriangle size={18} aria-hidden="true" />
            <p>O servidor do monitor não respondeu. Confira se o backend está rodando (veja o README) e recarregue a página.</p>
          </div>
        )}
        {!infoError && status && !status.connected && (
          <div className="banner" role="alert">
            <AlertTriangle size={18} aria-hidden="true" />
            <p>
              Sem conexão com o gateway. O monitor tenta de novo a cada 5 s.
              {status.last_error && <> Último erro: <span className="muted">{status.last_error}</span>.</>}
              {' '}Confira a rede (Wi-Fi do gateway, rota até a máquina) e o endereço em OPCUA_URL.
            </p>
          </div>
        )}
        {!linked && !infoError && <p className="muted small">Reconectando ao fluxo ao vivo…</p>}

        {info ? current.render({ info, live, tokens, now }) : !infoError && <p className="muted">Carregando…</p>}
      </main>
    </div>
  )
}
