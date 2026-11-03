import RFB from '@novnc/novnc/lib/rfb'
import { Eye, KeyRound, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'

type Estado = 'conectando' | 'senha' | 'conectado' | 'erro'

/** Tela ao vivo do painel (VNC) — só visualização. O servidor ainda descarta qualquer toque. */
export function TelaAoVivo() {
  const alvo = useRef<HTMLDivElement>(null)
  const rfb = useRef<RFB | null>(null)
  const [estado, setEstado] = useState<Estado>('conectando')
  const [detalhe, setDetalhe] = useState('')
  const [senha, setSenha] = useState('')
  const [tentativa, setTentativa] = useState(0)

  useEffect(() => {
    if (!alvo.current) return
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    setEstado('conectando')
    setDetalhe('')
    const r = new RFB(alvo.current, `${proto}://${window.location.host}/ws/vnc`, { wsProtocols: ['binary'], shared: true })
    r.viewOnly = true
    r.scaleViewport = true
    r.resizeSession = false
    r.background = 'transparent'
    r.addEventListener('connect', () => setEstado('conectado'))
    r.addEventListener('credentialsrequired', () => setEstado('senha'))
    r.addEventListener('securityfailure', (e: Event) => {
      setEstado('erro')
      setDetalhe(`Senha recusada pelo painel${(e as CustomEvent).detail?.reason ? `: ${(e as CustomEvent).detail.reason}` : ''}.`)
    })
    r.addEventListener('disconnect', (e: Event) => {
      const limpo = (e as CustomEvent).detail?.clean
      setEstado((s) => (s === 'senha' ? s : 'erro'))
      setDetalhe((d) => d || (limpo ? 'A conexão com o painel foi encerrada.' : 'Sem conexão com o VNC do painel.'))
    })
    rfb.current = r
    return () => {
      r.disconnect()
      rfb.current = null
    }
  }, [tentativa])

  const enviarSenha = (ev: FormEvent) => {
    ev.preventDefault()
    rfb.current?.sendCredentials({ password: senha })
    setSenha('')
    setEstado('conectando')
  }
  const reconectar = useCallback(() => setTentativa((n) => n + 1), [])

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h2>Tela ao vivo</h2>
          <p>A tela real da IHM, pelo VNC do painel. Só visualização: cliques e teclas não chegam à máquina.</p>
        </div>
        <span className="spacer" />
        <span className="live-flag"><Eye size={16} aria-hidden="true" /> Somente visualização</span>
        <button className="btn" onClick={reconectar}><RefreshCw size={16} aria-hidden="true" /> Reconectar</button>
      </div>

      <div className="panel vnc-panel">
        {estado === 'senha' && (
          <form className="vnc-overlay" onSubmit={enviarSenha}>
            <KeyRound size={22} aria-hidden="true" />
            <label htmlFor="vnc-senha">Senha do VNC do painel</label>
            <input id="vnc-senha" type="password" autoFocus value={senha} onChange={(e) => setSenha(e.target.value)} />
            <button className="btn primary" type="submit">Conectar à tela</button>
            <p className="muted small">A senha não é guardada.</p>
          </form>
        )}
        {estado === 'conectando' && <div className="vnc-overlay muted">Conectando ao painel…</div>}
        {estado === 'erro' && (
          <div className="vnc-overlay">
            <p>{detalhe || 'Não foi possível abrir a tela.'}</p>
            <p className="muted small">Confira se o painel está ligado e se a porta 5900 responde (VNC_HOST / VNC_PORT no .env).</p>
            <button className="btn" onClick={reconectar}><RefreshCw size={16} aria-hidden="true" /> Tentar de novo</button>
          </div>
        )}
        <div ref={alvo} className="vnc-screen" aria-label="Tela do painel" />
      </div>
    </div>
  )
}
