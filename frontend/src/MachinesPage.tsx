import { AlertTriangle, Boxes, Check, History, Pencil, Plus, Trash2, Wifi, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { activateMachine, ApiError, deleteMachine, getJson, mountMold, saveMachine, testMachine, usePoll } from './api'
import type { Machine, MachineInput, MachineKind, MachineList, Mold, MoldMount } from './api'
import { dateTime } from './format'

const KIND_LABEL: Record<MachineKind, string> = {
  chy: 'CHY · CHL3200',
  em63: 'B&R · Euromap 63',
  ads: 'Beckhoff · ADS',
  inovance: 'Inovance · painel Chen Hsong',
  demo: 'Simulada',
}

/** Tipos que falam OPC-UA direto (IP + porta + namespace), com Tela ao vivo por VNC. */
const isOpc = (k: MachineKind) => k === 'chy' || k === 'em63' || k === 'inovance'

interface Draft {
  client: string
  name: string
  kind: MachineKind
  host: string
  opcua_port: string
  namespace: string
  ams_net_id: string
  ads_port: string
  vnc_enabled: boolean
  vnc_port: string
  vnc_view_password: string
  vnc_control_password: string
  clear_view: boolean
  clear_control: boolean
  notes: string
  stop_min: string
}

const EMPTY: Draft = {
  client: 'Chen Hsong', name: '', kind: 'chy', host: '', opcua_port: '4840', namespace: '',
  ams_net_id: '', ads_port: '801', vnc_enabled: false, vnc_port: '5900',
  vnc_view_password: '', vnc_control_password: '', clear_view: false, clear_control: false, notes: '', stop_min: '2',
}

function draftOf(m: Machine): Draft {
  return {
    client: m.client, name: m.name, kind: m.kind, host: m.host, opcua_port: String(m.opcua_port),
    namespace: m.namespace === null ? '' : String(m.namespace), ams_net_id: m.ams_net_id,
    ads_port: String(m.ads_port), vnc_enabled: m.vnc_enabled, vnc_port: String(m.vnc_port),
    vnc_view_password: '', vnc_control_password: '', clear_view: false, clear_control: false, notes: m.notes,
    stop_min: String(+(m.stop_min_seconds / 60).toFixed(1)),
  }
}

/** Padrão de tela por tipo (o mesmo do servidor: só o Euromap 63 liga a tela sozinho). */
const vncDefault = (k: MachineKind) => k === 'em63'  // ads: desligada, o usuário liga

const isPort = (v: string) => /^\d+$/.test(v) && +v >= 1 && +v <= 65535

/**
 * Na edição, trocar o IP ou a porta VNC faz o servidor apagar as senhas guardadas que não forem
 * digitadas de novo (senão o painel novo receberia a resposta da senha antiga).
 */
function retargets(d: Draft, orig: Draft | null): boolean {
  if (!orig) return false
  const opc = isOpc(d.kind)
  return orig.host.trim() !== d.host.trim() || (opc && (+d.vnc_port || 5900) !== +orig.vnc_port)
}

function validate(d: Draft): Partial<Record<keyof Draft, string>> {
  const e: Partial<Record<keyof Draft, string>> = {}
  if (!d.name.trim()) e.name = 'Dê um nome para a máquina.'
  if (!d.client.trim()) e.client = 'Informe o cliente.'
  if (!d.host.trim()) e.host = 'Informe o IP ou o nome da máquina na rede.'
  const opc = isOpc(d.kind)
  if (opc && !isPort(d.opcua_port)) e.opcua_port = 'Use um número de 1 a 65535.'
  if (opc && d.namespace.trim() && !/^\d+$/.test(d.namespace.trim())) e.namespace = 'Use um número inteiro.'
  if (d.kind === 'ads' && !d.ams_net_id.trim()) e.ams_net_id = 'Informe o AMS Net ID (ex.: 192.168.0.10.1.1).'
  if (d.kind === 'ads' && !isPort(d.ads_port)) e.ads_port = 'Use um número de 1 a 65535.'
  if (d.kind === 'ads' && d.vnc_enabled) {
    if (d.vnc_view_password.length > 8) e.vnc_view_password = 'Use até 8 caracteres.'
    if (d.vnc_control_password.length > 8) e.vnc_control_password = 'Use até 8 caracteres.'
  }
  if (opc && d.vnc_enabled && !isPort(d.vnc_port)) e.vnc_port = 'Use um número de 1 a 65535.'
  const min = Number(d.stop_min.replace(',', '.'))
  if (!(min >= 0.5 && min <= 1440)) e.stop_min = 'Use de 0,5 a 1440 minutos.'
  return e
}

/**
 * Corpo do POST/PUT. Na edição só vão os campos que mudaram (PUT parcial); assim o namespace e a tela
 * que o servidor devolve já resolvidos pelo tipo não viram valores fixos. Namespace em branco = padrão do tipo.
 */
function toInput(d: Draft, orig: Draft | null): MachineInput {
  const kindChanged = !!orig && orig.kind !== d.kind
  const changed = (k: keyof Draft) => !orig || kindChanged || orig[k] !== d[k]
  const body: MachineInput = {}
  const base = (['client', 'name', 'kind', 'host', 'notes'] as const)
  for (const k of base) if (!orig || orig[k] !== d[k]) body[k] = d[k].trim() as never
  if (!orig || orig.stop_min !== d.stop_min) body.stop_min_seconds = Math.round(Number(d.stop_min.replace(',', '.')) * 60)
  if (isOpc(d.kind)) {
    if (changed('opcua_port')) body.opcua_port = +d.opcua_port
    if (changed('namespace')) body.namespace = d.namespace.trim() ? +d.namespace : null
    if (changed('vnc_enabled')) body.vnc_enabled = d.vnc_enabled
    if (changed('vnc_port')) body.vnc_port = +d.vnc_port || 5900
    // Senha em branco = não enviar: o servidor mantém a que já existe. "Remover" apaga a guardada.
    const target = retargets(d, orig)  // trocando o alvo, o servidor já apaga o que não for digitado
    if (d.clear_view && !target) body.clear_vnc_view_password = true
    else if (d.vnc_view_password) body.vnc_view_password = d.vnc_view_password
    if (d.clear_control && !target) body.clear_vnc_control_password = true
    else if (d.vnc_control_password) body.vnc_control_password = d.vnc_control_password
  } else if (d.kind === 'ads') {
    // Beckhoff: o CERHost roda no servidor (porta interna, sem campo). Só liga/desliga e as senhas desta tela.
    if (changed('vnc_enabled')) body.vnc_enabled = d.vnc_enabled
    const target = retargets(d, orig)
    if (d.clear_view && !target) body.clear_vnc_view_password = true
    else if (d.vnc_view_password) body.vnc_view_password = d.vnc_view_password
    if (d.clear_control && !target) body.clear_vnc_control_password = true
    else if (d.vnc_control_password) body.vnc_control_password = d.vnc_control_password
  } else if (kindChanged) {
    // Simulada não tem Tela ao vivo: volta ao padrão do tipo.
    body.vnc_enabled = null
  }
  if (d.kind === 'ads') {
    if (changed('ams_net_id')) body.ams_net_id = d.ams_net_id.trim()
    if (changed('ads_port')) body.ads_port = +d.ads_port
  }
  return body
}

type Probe = { busy: true } | { busy: false; online: boolean; latency_ms: number | null; at: number }

function Presence({ m, probe }: { m: Machine; probe?: Probe }) {
  let online = m.status?.online ?? null
  let latency = m.status?.latency_ms ?? null
  // O teste manual vale até o servidor registrar uma verificação mais nova.
  if (probe && !probe.busy && probe.at > (m.status?.checked ?? 0)) { online = probe.online; latency = probe.latency_ms }
  const cls = probe?.busy ? 'checking' : online === null ? 'unknown' : online ? 'on' : 'off'
  const text = probe?.busy ? 'Testando…' : online === null ? 'Não verificada'
    : online ? (latency !== null ? `Online · ${Math.round(latency)} ms` : 'Online') : 'Offline'
  return <span className={`presence ${cls}`}><i aria-hidden="true" />{text}</span>
}

export function MachinesPage({ onOpened, onChanged }: { onOpened: () => void; onChanged?: () => void }) {
  const { data, error, reload } = usePoll<MachineList>('/api/machines', 10000)
  const [editing, setEditing] = useState<Machine | 'new' | null>(null)
  const [probes, setProbes] = useState<Record<number, Probe>>({})
  const [pending, setPending] = useState<number | null>(null)
  const [confirmDel, setConfirmDel] = useState<number | null>(null)
  const [notice, setNotice] = useState<string[]>([])

  const groups = useMemo(() => {
    const map = new Map<string, Machine[]>()
    for (const m of data?.machines ?? []) map.set(m.client, [...(map.get(m.client) ?? []), m])
    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b, 'pt-BR'))
  }, [data])
  const clients = groups.map(([c]) => c)
  const refresh = () => { reload(); onChanged?.() }
  const fail = (e: unknown) => setNotice(e instanceof ApiError ? e.messages : [e instanceof Error ? e.message : String(e)])

  const open = async (m: Machine) => {
    setPending(m.id); setNotice([])
    try { await activateMachine(m.id); reload(); onOpened() } catch (e) { fail(e) } finally { setPending(null) }
  }
  const test = async (m: Machine) => {
    setProbes((p) => ({ ...p, [m.id]: { busy: true } }))
    try {
      const r = await testMachine(m.id)
      setProbes((p) => ({ ...p, [m.id]: { busy: false, ...r, at: Date.now() / 1000 } }))
    } catch (e) {
      setProbes((p) => Object.fromEntries(Object.entries(p).filter(([k]) => Number(k) !== m.id)))
      fail(e)
    }
  }
  const remove = async (m: Machine) => {
    setConfirmDel(null); setNotice([])
    try { await deleteMachine(m.id); if (editing !== 'new' && editing?.id === m.id) setEditing(null); refresh() } catch (e) { fail(e) }
  }

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h2>Máquinas</h2>
          <p>Cadastre as injetoras e escolha qual o monitor acompanha. O monitor só lê os dados; nada é enviado à máquina.</p>
        </div>
        <span className="spacer" />
        <button className="btn primary" onClick={() => { setEditing('new'); setNotice([]) }}><Plus size={16} aria-hidden="true" />Nova máquina</button>
      </div>

      {error && !data && <div className="banner error" role="alert"><AlertTriangle size={18} aria-hidden="true" /><p>Não consegui carregar a lista de máquinas. {error}</p></div>}
      {notice.length > 0 && (
        <div className="banner error" role="alert">
          <AlertTriangle size={18} aria-hidden="true" />
          <div>{notice.map((n) => <p key={n}>{n}</p>)}</div>
        </div>
      )}

      <div className={`machines-layout${editing ? ' with-form' : ''}`}>
        <div className="stack">
          {data && data.machines.length === 0 && (
            <div className="panel"><p className="muted" style={{ margin: 0 }}>Nenhuma máquina cadastrada ainda. Use <b>Nova máquina</b> para começar; o tipo <b>Simulada</b> serve para experimentar.</p></div>
          )}
          {!data && !error && <p className="muted">Carregando…</p>}
          {groups.map(([client, list]) => (
            <section key={client} className="panel" aria-label={`Cliente ${client}`}>
              <div className="panel-head"><h3>{client}</h3><span className="note">{list.length === 1 ? '1 máquina' : `${list.length} máquinas`}</span></div>
              <ul className="machine-list">
                {list.map((m) => {
                  const active = data?.active_id === m.id
                  return (
                    <li key={m.id} className={active ? 'machine-row active' : 'machine-row'}>
                      <div className="machine-main">
                        <div className="machine-title">
                          <strong>{m.name}</strong>
                          {active && <span className="chip">Aberta</span>}
                        </div>
                        <div className="machine-sub">
                          <span>{KIND_LABEL[m.kind]}</span>
                          <span className="num">{m.host}</span>
                          <Presence m={m} probe={probes[m.id]} />
                        </div>
                        <MoldControl m={m} onChanged={reload} onError={fail} />
                      </div>
                      <div className="machine-actions">
                        <button className="btn primary" disabled={active || pending === m.id} onClick={() => open(m)}>
                          {pending === m.id ? 'Abrindo…' : 'Abrir'}
                        </button>
                        <button className="btn" onClick={() => { setEditing(m); setNotice([]) }}><Pencil size={14} aria-hidden="true" />Editar</button>
                        <button className="btn" disabled={probes[m.id]?.busy} onClick={() => test(m)}><Wifi size={14} aria-hidden="true" />Testar conexão</button>
                        {confirmDel === m.id ? (
                          <span className="confirm">
                            <button className="btn danger" onClick={() => remove(m)}>Apagar “{m.name}”</button>
                            <button className="btn" onClick={() => setConfirmDel(null)}>Cancelar</button>
                          </span>
                        ) : (
                          <button className="btn" disabled={active} onClick={() => setConfirmDel(m.id)}
                            title={active ? 'Abra outra máquina antes de apagar esta.' : undefined}>
                            <Trash2 size={14} aria-hidden="true" />Apagar
                          </button>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ul>
            </section>
          ))}
        </div>

        {editing && (
          <MachineForm
            key={editing === 'new' ? 'new' : editing.id}
            machine={editing === 'new' ? null : editing}
            clients={clients}
            onClose={() => setEditing(null)}
            onSaved={() => { setEditing(null); refresh() }}
          />
        )}
      </div>
    </div>
  )
}

function MachineForm({ machine, clients, onClose, onSaved }: {
  machine: Machine | null; clients: string[]; onClose: () => void; onSaved: () => void
}) {
  const orig = useMemo(() => (machine ? draftOf(machine) : null), [machine])
  const [d, setD] = useState<Draft>(orig ?? { ...EMPTY, vnc_enabled: vncDefault(EMPTY.kind) })
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    const h = heading.current
    if (!h) return
    h.focus({ preventScroll: true })
    if (window.matchMedia('(max-width: 820px)').matches) h.scrollIntoView({ block: 'start' })
  }, [])
  const [touched, setTouched] = useState<Partial<Record<keyof Draft, boolean>>>({})
  const [submitted, setSubmitted] = useState(false)
  const [serverErrors, setServerErrors] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const errors = validate(d)
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD((x) => ({ ...x, [k]: v }))
  const err = (k: keyof Draft) => (submitted || touched[k] ? errors[k] : undefined)
  const blur = (k: keyof Draft) => () => setTouched((t) => ({ ...t, [k]: true }))
  const opc = isOpc(d.kind)
  const hadPassword = !!machine && (machine.has_view_password || machine.has_control_password)
  const retarget = retargets(d, orig) && hadPassword
  const keepHint = (has: boolean | undefined) => (has && !retarget ? 'deixe em branco para manter' : '')

  const submit = async (ev: FormEvent) => {
    ev.preventDefault()
    setSubmitted(true)
    if (Object.keys(errors).length) return
    setSaving(true); setServerErrors([])
    try {
      await saveMachine(toInput(d, orig), machine?.id)
      onSaved()
    } catch (e) {
      setServerErrors(e instanceof ApiError ? e.messages : [e instanceof Error ? e.message : String(e)])
    } finally {
      setSaving(false)
    }
  }

  const text = (k: keyof Draft, label: string, extra: { hint?: string; placeholder?: string; list?: string; inputMode?: 'numeric'; type?: string; disabled?: boolean; maxLength?: number } = {}) => (
    <label className="field">
      {label}
      <input
        type={extra.type ?? 'text'} inputMode={extra.inputMode} list={extra.list} disabled={extra.disabled} maxLength={extra.maxLength}
        value={d[k] as string} placeholder={extra.placeholder}
        autoComplete={extra.type === 'password' ? 'new-password' : 'off'}
        aria-invalid={err(k) ? true : undefined}
        onChange={(e) => set(k, e.target.value as never)} onBlur={blur(k)}
      />
      {err(k) ? <span className="field-error">{err(k)}</span> : extra.hint && <span className="field-hint">{extra.hint}</span>}
    </label>
  )

  return (
    <form className="panel machine-form" onSubmit={submit} noValidate aria-label={machine ? `Editar ${machine.name}` : 'Nova máquina'}>
      <div className="panel-head">
        <h3 ref={heading} tabIndex={-1}>{machine ? `Editar ${machine.name}` : 'Nova máquina'}</h3>
        <button type="button" className="btn icon right" onClick={onClose} aria-label="Fechar formulário" title="Fechar"><X size={16} aria-hidden="true" /></button>
      </div>

      {serverErrors.length > 0 && (
        <div className="banner error" role="alert"><AlertTriangle size={18} aria-hidden="true" /><div>{serverErrors.map((s) => <p key={s}>{s}</p>)}</div></div>
      )}

      <div className="form-grid">
        {text('client', 'Cliente', { list: 'clientes-conhecidos' })}
        <datalist id="clientes-conhecidos">{clients.map((c) => <option key={c} value={c} />)}</datalist>
        {text('name', 'Nome da máquina', { placeholder: 'Ex.: Injetora 03' })}
        <label className="field">
          Tipo
          <select value={d.kind} onChange={(e) => { const k = e.target.value as MachineKind; setD((x) => ({ ...x, kind: k, namespace: '', vnc_enabled: vncDefault(k) })) }}>
            {(Object.keys(KIND_LABEL) as MachineKind[]).map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
          </select>
        </label>
        {text('host', d.kind === 'demo' ? 'IP ou nome (qualquer valor)' : 'IP ou nome na rede', { placeholder: '192.168.0.50' })}

        {opc && (
          <>
            {text('opcua_port', 'Porta OPC-UA', { inputMode: 'numeric' })}
            {text('namespace', 'Namespace OPC-UA', { inputMode: 'numeric', placeholder: 'padrão do tipo', hint: 'Deixe em branco para usar o padrão do tipo.' })}
          </>
        )}
        {d.kind === 'ads' && (
          <>
            {text('ams_net_id', 'AMS Net ID', { placeholder: '192.168.0.10.1.1' })}
            {text('ads_port', 'Porta ADS', { inputMode: 'numeric' })}
          </>
        )}
      </div>

      {(opc || d.kind === 'ads') && (
        <fieldset className="form-group">
          <legend>{opc ? 'Tela ao vivo (VNC)' : 'Tela ao vivo (CERHost)'}</legend>
          <label className="check"><input type="checkbox" checked={d.vnc_enabled} onChange={(e) => set('vnc_enabled', e.target.checked)} />{opc ? 'Mostrar a tela da máquina no monitor' : 'Mostrar a tela da Beckhoff (CERHost) no monitor'}</label>
          {retarget && (
            <p className="retarget-hint" role="status"><AlertTriangle size={14} aria-hidden="true" /> Ao trocar o IP ou a porta, digite as senhas de novo.</p>
          )}
          {d.vnc_enabled && (
            <div className="form-grid">
              {opc && text('vnc_port', 'Porta VNC', { inputMode: 'numeric' })}
              <div className="stack-tight">
                {text('vnc_view_password', 'Senha de visualização', {
                  type: 'password', maxLength: d.kind === 'ads' ? 8 : undefined, placeholder: keepHint(machine?.has_view_password),
                  hint: opc ? 'Só assistir a tela.' : 'Só assistir a tela. Senhas desta tela no Monitor, até 8 caracteres.', disabled: d.clear_view && !retarget,
                })}
                {machine?.has_view_password && !retarget && (
                  <label className="check"><input type="checkbox" checked={d.clear_view}
                    onChange={(e) => setD((x) => ({ ...x, clear_view: e.target.checked, vnc_view_password: '' }))} />Remover senha de visualização</label>
                )}
              </div>
              <div className="stack-tight">
                {text('vnc_control_password', 'Senha de operação', {
                  type: 'password', maxLength: d.kind === 'ads' ? 8 : undefined, placeholder: keepHint(machine?.has_control_password),
                  hint: opc ? 'Opcional. Quem a digitar pode assumir o controle da tela.' : 'Opcional. Quem a digitar pode assumir o controle da tela. Até 8 caracteres.', disabled: d.clear_control && !retarget,
                })}
                {machine?.has_control_password && !retarget && (
                  <label className="check"><input type="checkbox" checked={d.clear_control}
                    onChange={(e) => setD((x) => ({ ...x, clear_control: e.target.checked, vnc_control_password: '' }))} />Remover senha de operação</label>
                )}
              </div>
            </div>
          )}
        </fieldset>
      )}

      <label className="field">
        Contar parada a partir de (minutos)
        <input inputMode="decimal" value={d.stop_min} onChange={(e) => set('stop_min', e.target.value)}
          onBlur={blur('stop_min')} aria-invalid={err('stop_min') ? true : undefined} />
        {err('stop_min') ? <span className="field-error">{err('stop_min')}</span>
          : <span className="field-hint">Paradas mais curtas que isso não entram na lista (micro-paradas).</span>}
      </label>

      <label className="field">
        Observações
        <textarea rows={2} value={d.notes} onChange={(e) => set('notes', e.target.value)} />
      </label>

      <div className="form-actions">
        <button type="submit" className="btn primary" disabled={saving}><Check size={16} aria-hidden="true" />{saving ? 'Salvando…' : machine ? 'Salvar alterações' : 'Cadastrar máquina'}</button>
        <button type="button" className="btn" onClick={onClose}>Cancelar</button>
      </div>
    </form>
  )
}

/** Molde montado na máquina: trocar (fica no histórico) e ver as trocas anteriores. */
function MoldControl({ m, onChanged, onError }: { m: Machine; onChanged: () => void; onError: (e: unknown) => void }) {
  const [picking, setPicking] = useState(false)
  const [molds, setMolds] = useState<Mold[] | null>(null)
  const [choice, setChoice] = useState('')
  const [busy, setBusy] = useState(false)
  const [history, setHistory] = useState<MoldMount[] | null>(null)
  const cur = m.mold ?? null

  const startPick = async () => {
    setPicking(true); setChoice(cur?.mold_id != null ? String(cur.mold_id) : '')
    try { setMolds((await getJson<{ molds: Mold[] }>('/api/molds')).molds) } catch (e) { onError(e); setPicking(false) }
  }
  const confirm = async () => {
    setBusy(true)
    try {
      const r = await mountMold(m.id, choice === '' ? null : Number(choice))
      setPicking(false); if (history) setHistory(r.history); onChanged()
    } catch (e) { onError(e) } finally { setBusy(false) }
  }
  const toggleHistory = async () => {
    if (history) { setHistory(null); return }
    try { setHistory((await getJson<{ history: MoldMount[] }>(`/api/machines/${m.id}/mold`)).history) } catch (e) { onError(e) }
  }
  const free = (x: Mold) => !x.mounted_on || x.mounted_on.machine_id === m.id

  return (
    <div className="mold-line">
      <span className="machine-sub">
        <Boxes size={14} aria-hidden="true" />
        {cur
          ? <span>Molde <b className="num">{cur.mold_code}</b>{cur.mold_name && ` · ${cur.mold_name}`} · <b className="num">{cur.active_cavities}</b> {cur.active_cavities === 1 ? 'peça' : 'peças'} por ciclo</span>
          : <span className="muted">Sem molde: conta 1 peça por ciclo</span>}
        {!picking && <button className="btn link" onClick={startPick}>Trocar molde</button>}
        <button className="btn link" onClick={toggleHistory} aria-expanded={!!history}><History size={13} aria-hidden="true" />Histórico</button>
      </span>
      {picking && (
        <span className="form-actions">
          <label className="field inline">
            <span className="sr-label">Molde para {m.name}</span>
            <select value={choice} onChange={(e) => setChoice(e.target.value)} disabled={!molds}>
              <option value="">Sem molde (1 peça por ciclo)</option>
              {molds?.filter(free).map((x) => <option key={x.id} value={x.id}>{x.code}{x.name && ` · ${x.name}`} · {x.active_cavities} cav.</option>)}
            </select>
          </label>
          <button className="btn primary" disabled={busy || !molds || choice === (cur?.mold_id != null ? String(cur.mold_id) : '')} onClick={confirm}>
            {busy ? 'Trocando…' : 'Confirmar troca'}
          </button>
          <button className="btn" onClick={() => setPicking(false)}>Cancelar</button>
          {molds && molds.length === 0 && <span className="field-hint">Cadastre um molde na aba Moldes.</span>}
        </span>
      )}
      {history && (
        history.length === 0
          ? <p className="muted small">Nenhuma troca de molde registrada.</p>
          : <ul className="mold-history">
              {history.map((h) => (
                <li key={h.id}>
                  <b className="num">{h.mold_code}</b>{h.mold_name && ` · ${h.mold_name}`} · {h.active_cavities} cav.
                  <span className="muted"> — {dateTime(h.mounted_at)} até {h.removed_at ? dateTime(h.removed_at) : 'agora'}</span>
                </li>
              ))}
            </ul>
      )}
    </div>
  )
}
