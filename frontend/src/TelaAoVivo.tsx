import RFB from '@novnc/novnc/lib/rfb'
import { Expand, Eye, Hand, KeyRound, LockOpen, RefreshCw, Shrink, Users } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { getJson, type VncStatus } from './api'

type Estado = 'conectando' | 'senha' | 'conectado' | 'erro'

/** Identificador desta aba no hub VNC (crypto.getRandomValues funciona mesmo em http na rede local). */
function novoSid(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

/** Motivo do último fechamento pelo servidor (ex.: "máquina trocada"), para a tela nova da próxima máquina. */
let fechadaPor: { motivo: string; quando: number } | null = null

/** Botão de tecla do painel: manda a tecla (com Ctrl, se pedir) direto pelo VNC. */
type TeclaPainel = { nome: string; rotulo: string; keysym: number; code: string; ctrl?: boolean }

/**
 * Beckhoff (XMold): cada tecla física de função é Ctrl + letra no CERHost.
 * Mesma ordem do teclado do painel: 1ª fileira Ctrl+Q..R, 2ª fileira Ctrl+A..F.
 */
const TECLAS_BECKHOFF: TeclaPainel[] = [
  ['q', 'Visão geral'], ['w', 'Molde'], ['e', 'Injeção'], ['r', 'Temperatura'],
  ['a', 'Extrator'], ['s', 'Núcleo'], ['d', 'Dosagem'], ['f', 'Ajustes'],
].map(([l, nome]) => ({ nome, rotulo: `Ctrl+${l.toUpperCase()}`, keysym: l.charCodeAt(0), code: `Key${l.toUpperCase()}`, ctrl: true }))

/**
 * B&R (Chen Hsong): pelo VNC o painel só aceita F1–F10, Home e PgUp (além de números, setas e Enter).
 * F1–F6 são as abas acima das teclas. F7 desliga o VNC e F8 não faz nada: ficam de fora.
 */
const F = (n: number) => ({ keysym: 0xffbe + n - 1, code: `F${n}`, rotulo: `F${n}` })
const TECLAS_BR: TeclaPainel[] = [
  { nome: 'Aba 1', ...F(1) }, { nome: 'Aba 2', ...F(2) }, { nome: 'Aba 3', ...F(3) },
  { nome: 'Aba 4', ...F(4) }, { nome: 'Aba 5', ...F(5) }, { nome: 'Aba 6', ...F(6) },
  { nome: 'Controle', ...F(9) }, { nome: 'Voltar', ...F(10) },
  { nome: 'Visão geral', rotulo: 'Home', keysym: 0xff50, code: 'Home' },
  { nome: 'Configurações', rotulo: 'PgUp', keysym: 0xff55, code: 'PageUp' },
]

function teclasDo(kind?: string): TeclaPainel[] {
  if (kind === 'ads') return TECLAS_BECKHOFF
  if (kind === 'em63') return TECLAS_BR
  return []
}
const XK_CONTROL_L = 0xffe3

type TecladoNavegador = { lock?: (teclas?: string[]) => Promise<void>; unlock?: () => void }
function teclado(): TecladoNavegador | undefined {
  return (navigator as Navigator & { keyboard?: TecladoNavegador }).keyboard
}

function mmss(segundos: number): string {
  const s = Math.max(0, Math.round(segundos))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

/**
 * Tela ao vivo do painel pelo hub VNC do Monitor.
 * A senha digitada define o papel: a de visualização só assiste; a de operação pode assumir o controle.
 * O servidor só repassa toques de quem está com o controle; os de todo o resto são descartados lá.
 */
export function TelaAoVivo({ machineName, kind }: { machineName?: string; kind?: string }) {
  const alvo = useRef<HTMLDivElement>(null)
  const quadro = useRef<HTMLDivElement>(null)
  const [telaCheia, setTelaCheia] = useState(false)
  const [travaTeclado, setTravaTeclado] = useState(false)
  const rfb = useRef<RFB | null>(null)
  const [sid, setSid] = useState(novoSid)
  const [anterior] = useState(() => {
    const f = fechadaPor
    fechadaPor = null
    return f && Date.now() - f.quando < 60000 ? f.motivo : ''
  })
  const [estado, setEstado] = useState<Estado>('conectando')
  const [detalhe, setDetalhe] = useState('')
  const [senha, setSenha] = useState('')
  const [status, setStatus] = useState<VncStatus | null>(null)
  const [confirmar, setConfirmar] = useState(false)
  const [aviso, setAviso] = useState('')
  const [enviando, setEnviando] = useState(false)

  const sidAtual = useRef(sid)
  useEffect(() => {
    if (!alvo.current) return
    // Aba nova no hub: o que veio do status da conexão anterior não vale mais.
    sidAtual.current = sid
    setStatus(null)
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    setEstado('conectando')
    setDetalhe('')
    // O WebSocket é nosso (o noVNC aceita um já criado) para ler o motivo do fechamento.
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/vnc?sid=${sid}`, ['binary'])
    let motivo = ''
    ws.addEventListener('close', (e) => { motivo = e.reason })
    const r = new RFB(alvo.current, ws, { shared: true })
    r.viewOnly = true
    r.scaleViewport = true
    r.resizeSession = false
    r.background = 'transparent'
    r.addEventListener('connect', () => setEstado('conectado'))
    r.addEventListener('credentialsrequired', () => setEstado('senha'))
    r.addEventListener('securityfailure', (e: Event) => {
      setEstado('erro')
      setDetalhe((e as CustomEvent).detail?.reason || 'Senha recusada.')
    })
    r.addEventListener('disconnect', (e: Event) => {
      const limpo = (e as CustomEvent).detail?.clean
      if (motivo === 'máquina trocada') fechadaPor = { motivo, quando: Date.now() }
      setEstado((s) => (s === 'senha' ? s : 'erro'))
      setDetalhe((d) => d || (motivo ? `A tela foi fechada pelo monitor: ${motivo}.`
        : limpo ? 'A conexão com o painel foi encerrada.' : 'Sem conexão com a Tela ao vivo.'))
    })
    rfb.current = r
    return () => {
      r.disconnect()
      rfb.current = null
    }
  }, [sid])

  const atualizar = useCallback(async () => {
    try {
      const novo = await getJson<VncStatus>(`/api/vnc/status?sid=${sid}`)
      if (sidAtual.current === sid) setStatus(novo)  // resposta de uma conexão anterior é ignorada
    } catch {
      /* a próxima rodada tenta de novo */
    }
  }, [sid])

  // Antes de conectar também se lê o status: na Beckhoff ele diz em que passo está a ponte do CERHost.
  useEffect(() => {
    if (estado !== 'conectado') setConfirmar(false)
    void atualizar()
    const id = window.setInterval(atualizar, 2000)
    return () => window.clearInterval(id)
  }, [estado, atualizar])

  const ativo = estado === 'conectado'  // controle e papel só valem com a tela conectada
  const operando = ativo && status?.controller?.me === true
  useEffect(() => {
    if (!rfb.current) return
    rfb.current.viewOnly = !operando
    if (operando) rfb.current.focus()
  }, [operando])

  /** Manda a tecla direto para o painel (não passa pelo teclado do navegador). */
  const teclaPainel = (t: TeclaPainel) => {
    const r = rfb.current
    if (!r || !operando) return
    if (t.ctrl) r.sendKey(XK_CONTROL_L, 'ControlLeft', true)
    r.sendKey(t.keysym, t.code, true)
    r.sendKey(t.keysym, t.code, false)
    if (t.ctrl) r.sendKey(XK_CONTROL_L, 'ControlLeft', false)
    r.focus()
  }
  const teclas = teclasDo(kind)

  // Tela cheia + trava de teclado (Chrome/Edge): aí até Ctrl+W vai para o painel. Segure Esc para sair.
  const alternarTelaCheia = async () => {
    if (document.fullscreenElement) {
      await document.exitFullscreen().catch(() => undefined)
      return
    }
    try {
      await quadro.current?.requestFullscreen()
    } catch {
      setAviso('O navegador não deixou abrir em tela cheia.')
      return
    }
    try {
      await teclado()?.lock?.()
      setTravaTeclado(Boolean(teclado()?.lock))
    } catch {
      setTravaTeclado(false)
    }
    rfb.current?.focus()
  }
  useEffect(() => {
    const mudou = () => {
      const cheia = document.fullscreenElement === quadro.current && quadro.current !== null
      setTelaCheia(cheia)
      if (!cheia) {
        teclado()?.unlock?.()
        setTravaTeclado(false)
      }
    }
    document.addEventListener('fullscreenchange', mudou)
    return () => {
      document.removeEventListener('fullscreenchange', mudou)
      teclado()?.unlock?.()
    }
  }, [])

  const controle = async (action: 'take' | 'release') => {
    setEnviando(true)
    setAviso('')
    try {
      const res = await fetch('/api/vnc/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sid, action }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        setAviso(body.detail || `O servidor respondeu ${res.status}.`)
      }
      await atualizar()
    } catch {
      setAviso('Sem resposta do servidor.')
    } finally {
      setEnviando(false)
      setConfirmar(false)
    }
  }

  const enviarSenha = (ev: FormEvent) => {
    ev.preventDefault()
    rfb.current?.sendCredentials({ password: senha })
    setSenha('')
    setEstado('conectando')
  }
  const reconectar = useCallback(() => setSid(novoSid()), [])

  // Beckhoff: enquanto a ponte não conecta, o estado dela explica melhor que o erro genérico de VNC.
  const ponte = status?.bridge && status.bridge.state !== 'connected' ? status.bridge : null
  const ponteFalhou = ponte?.state === 'missing' || ponte?.state === 'error'
  const ponteTexto = !ponte ? '' : ponteFalhou
    ? (ponte.error || 'Não foi possível abrir a tela da Beckhoff.')
    : ponte.state === 'connecting' ? 'Conectando ao painel…' : 'Abrindo a tela da Beckhoff…'
  const ponteOk = status?.bridge?.state === 'connected'
  // 'stopped' também pode ficar assim para sempre (ex.: ponte desligada): oferece tentar de novo.
  const ponteTentar = ponteFalhou || ponte?.state === 'stopped'
  // O erro só conta como "ponte não estava pronta" se ele aconteceu com a ponte fora do ar; então,
  // quando ela ficar pronta, tenta de novo sozinho. Outros erros (senha errada...) ficam na tela.
  const erroPorPonte = useRef(false)
  useEffect(() => {
    if (estado !== 'erro') { erroPorPonte.current = false; return }
    if (ponte) erroPorPonte.current = true
    else if (ponteOk && erroPorPonte.current) {
      erroPorPonte.current = false
      reconectar()
    }
  }, [ponte, ponteOk, estado, reconectar])

  const outro = ativo && status?.controller && !status.controller.me ? status.controller : null
  const podeOperar = ativo && status?.role === 'control' && status.control_available

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h2>Tela ao vivo{machineName && <> · {machineName}</>}</h2>
          <p>A tela real da IHM, pelo VNC do painel. A senha de visualização só mostra a tela; a de operação permite assumir o controle.</p>
        </div>
        <span className="spacer" />
        {status && ativo && (
          <span className="live-flag" title="Pessoas assistindo agora">
            <Users size={16} aria-hidden="true" /> {status.viewers} assistindo
          </span>
        )}
        {!operando && (
          <span className="live-flag"><Eye size={16} aria-hidden="true" /> Somente visualização</span>
        )}
        {podeOperar && !operando && !outro && !confirmar && (
          <button className="btn primary" onClick={() => setConfirmar(true)} disabled={enviando}>
            <Hand size={16} aria-hidden="true" /> Assumir controle
          </button>
        )}
        <button className="btn" onClick={reconectar}><RefreshCw size={16} aria-hidden="true" /> Reconectar</button>
      </div>

      {confirmar && (
        <div className="vnc-bar warn" role="alertdialog" aria-label="Confirmar controle">
          <span>Ao assumir, seus cliques e teclas vão para a máquina. Só opere com alguém acompanhando no local.</span>
          <span className="spacer" />
          <button className="btn primary" onClick={() => controle('take')} disabled={enviando} autoFocus>
            <Hand size={16} aria-hidden="true" /> Assumir controle da máquina
          </button>
          <button className="btn" onClick={() => setConfirmar(false)}>Cancelar</button>
        </div>
      )}
      {operando && status?.controller && (
        <div className="vnc-bar warn" role="status">
          <Hand size={18} aria-hidden="true" />
          <span>
            <b>Você está operando a máquina.</b> Cliques e teclas vão para o painel. Sem uso, volta para
            visualização em {mmss(status.controller.seconds_left)}.
          </span>
          <span className="spacer" />
          <button className="btn" onClick={() => controle('release')} disabled={enviando}>
            <LockOpen size={16} aria-hidden="true" /> Liberar controle
          </button>
        </div>
      )}
      {outro && (
        <div className="vnc-bar" role="status">
          <Hand size={18} aria-hidden="true" />
          <span>Em operação por <b>{outro.peer}</b>. Você continua vendo a tela, sem mexer.</span>
        </div>
      )}
      {anterior && !aviso && (
        <div className="vnc-bar" role="status">
          <span>A tela da máquina anterior foi fechada ({anterior}). Esta é a tela de <b>{machineName ?? 'outra máquina'}</b>; entre com a senha deste painel.</span>
        </div>
      )}
      {aviso && <div className="vnc-bar" role="alert">{aviso}</div>}

      <div ref={quadro} className={`vnc-quadro${telaCheia ? ' cheia' : ''}`}>
      <div className="vnc-tools">
        {teclas.length > 0 && (
          <div className={`panel-keys${teclas.length > 8 ? ' cinco' : ''}`} role="group" aria-label="Teclas do painel">
            {teclas.map((t) => (
              <button key={t.code} className="btn panel-key" disabled={!operando}
                title={operando ? `${t.rotulo} no painel` : 'Assuma o controle para usar as teclas do painel'}
                onClick={() => teclaPainel(t)}>
                <span>{t.nome}</span><kbd>{t.rotulo}</kbd>
              </button>
            ))}
          </div>
        )}
        <span className="spacer" />
        {telaCheia && (
          <span className="muted small">
            {travaTeclado ? 'Teclado preso no painel. Para sair, segure Esc.' : 'Para sair, aperte Esc. Neste navegador, Ctrl+W e parecidos continuam com o navegador: use os botões.'}
          </span>
        )}
        <button className="btn" onClick={() => void alternarTelaCheia()}>
          {telaCheia ? <><Shrink size={16} aria-hidden="true" /> Sair da tela cheia</> : <><Expand size={16} aria-hidden="true" /> Tela cheia</>}
        </button>
      </div>
      <div className={`panel vnc-panel${operando ? ' operando' : ''}`}>
        {estado === 'senha' && !ponte && (
          <form className="vnc-overlay" onSubmit={enviarSenha}>
            <KeyRound size={22} aria-hidden="true" />
            <label htmlFor="vnc-senha">Senha do painel (visualização ou operação)</label>
            <input id="vnc-senha" type="password" autoFocus value={senha} onChange={(e) => setSenha(e.target.value)} />
            <button className="btn primary" type="submit">Conectar à tela</button>
            <p className="muted small">A senha não é guardada no navegador.</p>
          </form>
        )}
        {ponte && estado !== 'conectado' && estado !== 'erro' && (
          ponteTentar ? (
            <div className="vnc-overlay" role="alert">
              <p>{ponteTexto}</p>
              <button className="btn" onClick={reconectar}><RefreshCw size={16} aria-hidden="true" /> Tentar de novo</button>
            </div>
          ) : <div className="vnc-overlay muted" role="status">{ponteTexto}</div>
        )}
        {!ponte && estado === 'conectando' && <div className="vnc-overlay muted">Conectando ao painel…</div>}
        {estado === 'erro' && (
          <div className="vnc-overlay" role="alert">
            <p>{ponteFalhou ? ponteTexto : detalhe || 'Não foi possível abrir a tela.'}</p>
            {!ponte && <p className="muted small">Confira se o painel está ligado, se a porta VNC responde e as senhas no cadastro da máquina (página Máquinas).</p>}
            <button className="btn" onClick={reconectar}><RefreshCw size={16} aria-hidden="true" /> Tentar de novo</button>
          </div>
        )}
        <div ref={alvo} className="vnc-screen" aria-label="Tela do painel" />
      </div>
      </div>
    </div>
  )
}
