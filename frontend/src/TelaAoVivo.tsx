import RFB from '@novnc/novnc/lib/rfb'
import { Eye, Hand, KeyRound, LockOpen, RefreshCw, Users } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { getJson, type VncStatus } from './api'

type Estado = 'conectando' | 'senha' | 'conectado' | 'erro'

/** Identificador desta aba no hub VNC (crypto.getRandomValues funciona mesmo em http na rede local). */
function novoSid(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
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
export function TelaAoVivo() {
  const alvo = useRef<HTMLDivElement>(null)
  const rfb = useRef<RFB | null>(null)
  const [sid, setSid] = useState(novoSid)
  const [estado, setEstado] = useState<Estado>('conectando')
  const [detalhe, setDetalhe] = useState('')
  const [senha, setSenha] = useState('')
  const [status, setStatus] = useState<VncStatus | null>(null)
  const [confirmar, setConfirmar] = useState(false)
  const [aviso, setAviso] = useState('')
  const [enviando, setEnviando] = useState(false)

  useEffect(() => {
    if (!alvo.current) return
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    setEstado('conectando')
    setDetalhe('')
    const r = new RFB(alvo.current, `${proto}://${window.location.host}/ws/vnc?sid=${sid}`, { wsProtocols: ['binary'], shared: true })
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
      setEstado((s) => (s === 'senha' ? s : 'erro'))
      setDetalhe((d) => d || (limpo ? 'A conexão com o painel foi encerrada.' : 'Sem conexão com a Tela ao vivo.'))
    })
    rfb.current = r
    return () => {
      r.disconnect()
      rfb.current = null
    }
  }, [sid])

  const atualizar = useCallback(async () => {
    try {
      setStatus(await getJson<VncStatus>(`/api/vnc/status?sid=${sid}`))
    } catch {
      /* a próxima rodada tenta de novo */
    }
  }, [sid])

  useEffect(() => {
    if (estado !== 'conectado') {
      setStatus(null)
      setConfirmar(false)
      return
    }
    void atualizar()
    const id = window.setInterval(atualizar, 2000)
    return () => window.clearInterval(id)
  }, [estado, atualizar])

  const operando = status?.controller?.me === true
  useEffect(() => {
    if (!rfb.current) return
    rfb.current.viewOnly = !operando
    if (operando) rfb.current.focus()
  }, [operando])

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

  const outro = status?.controller && !status.controller.me ? status.controller : null
  const podeOperar = status?.role === 'control' && status.control_available

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h2>Tela ao vivo</h2>
          <p>A tela real da IHM, pelo VNC do painel. A senha de visualização só mostra a tela; a de operação permite assumir o controle.</p>
        </div>
        <span className="spacer" />
        {status && (
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
      {aviso && <div className="vnc-bar" role="alert">{aviso}</div>}

      <div className={`panel vnc-panel${operando ? ' operando' : ''}`}>
        {estado === 'senha' && (
          <form className="vnc-overlay" onSubmit={enviarSenha}>
            <KeyRound size={22} aria-hidden="true" />
            <label htmlFor="vnc-senha">Senha do painel (visualização ou operação)</label>
            <input id="vnc-senha" type="password" autoFocus value={senha} onChange={(e) => setSenha(e.target.value)} />
            <button className="btn primary" type="submit">Conectar à tela</button>
            <p className="muted small">A senha não é guardada no navegador.</p>
          </form>
        )}
        {estado === 'conectando' && <div className="vnc-overlay muted">Conectando ao painel…</div>}
        {estado === 'erro' && (
          <div className="vnc-overlay">
            <p>{detalhe || 'Não foi possível abrir a tela.'}</p>
            <p className="muted small">Confira se o painel está ligado, se a porta 5900 responde e as senhas no .env (VNC_VIEW_PASSWORD / VNC_CONTROL_PASSWORD).</p>
            <button className="btn" onClick={reconectar}><RefreshCw size={16} aria-hidden="true" /> Tentar de novo</button>
          </div>
        )}
        <div ref={alvo} className="vnc-screen" aria-label="Tela do painel" />
      </div>
    </div>
  )
}
