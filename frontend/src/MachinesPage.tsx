import { AlertTriangle, Check, Pencil, Plus, Trash2, Wifi, X } from 'lucide-react'
import { useMemo, useState, type FormEvent } from 'react'
import { activateMachine, ApiError, deleteMachine, saveMachine, testMachine, usePoll } from './api'
import type { Machine, MachineInput, MachineKind, MachineList } from './api'

const KIND_LABEL: Record<MachineKind, string> = {
  chy: 'CHY · CHL3200',
  em63: 'B&R · Euromap 63',
  ads: 'Beckhoff · ADS',
  demo: 'Simulada',
}

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
  notes: string
}

const EMPTY: Draft = {
  client: 'Chen Hsong', name: '', kind: 'chy', host: '', opcua_port: '4840', namespace: '',
  ams_net_id: '', ads_port: '801', vnc_enabled: false, vnc_port: '5900',
  vnc_view_password: '', vnc_control_password: '', notes: '',
}

function draftOf(m: Machine): Draft {
  return {
    client: m.client, name: m.name, kind: m.kind, host: m.host, opcua_port: String(m.opcua_port),
    namespace: m.namespace === null ? '' : String(m.namespace), ams_net_id: m.ams_net_id,
    ads_port: String(m.ads_port), vnc_enabled: m.vnc_enabled, vnc_port: String(m.vnc_port),
    vnc_view_password: '', vnc_control_password: '', notes: m.notes,
  }
}

const isPort = (v: string) => /^\d+$/.test(v) && +v >= 1 && +v <= 65535

function validate(d: Draft): Partial<Record<keyof Draft, string>> {
  const e: Partial<Record<keyof Draft, string>> = {}
  if (!d.name.trim()) e.name = 'Dê um nome para a máquina.'
  if (!d.client.trim()) e.client = 'Informe o cliente.'
  if (!d.host.trim()) e.host = 'Informe o IP ou o nome da máquina na rede.'
  const opc = d.kind === 'chy' || d.kind === 'em63'
  if (opc && !isPort(d.opcua_port)) e.opcua_port = 'Use um número de 1 a 65535.'
  if (opc && d.namespace.trim() && !/^\d+$/.test(d.namespace.trim())) e.namespace = 'Use um número inteiro.'
  if (d.kind === 'ads' && !d.ams_net_id.trim()) e.ams_net_id = 'Informe o AMS Net ID (ex.: 192.168.0.10.1.1).'
  if (d.kind === 'ads' && !isPort(d.ads_port)) e.ads_port = 'Use um número de 1 a 65535.'
  if (opc && d.vnc_enabled && !isPort(d.vnc_port)) e.vnc_port = 'Use um número de 1 a 65535.'
  return e
}

function toInput(d: Draft): MachineInput {
  const body: MachineInput = {
    client: d.client.trim(), name: d.name.trim(), kind: d.kind, host: d.host.trim(), notes: d.notes.trim(),
  }
  if (d.kind === 'chy' || d.kind === 'em63') {
    body.opcua_port = +d.opcua_port
    body.namespace = d.namespace.trim() ? +d.namespace : null
    body.vnc_enabled = d.vnc_enabled
    body.vnc_port = +d.vnc_port || 5900
    // Senha em branco = não enviar: o servidor mantém a que já existe.
    if (d.vnc_view_password) body.vnc_view_password = d.vnc_view_password
    if (d.vnc_control_password) body.vnc_control_password = d.vnc_control_password
  }
  if (d.kind === 'ads') {
    body.ams_net_id = d.ams_net_id.trim()
    body.ads_port = +d.ads_port
  }
  return body
}

type Probe = { busy: true } | { busy: false; online: boolean; latency_ms: number | null }

function Presence({ m, probe }: { m: Machine; probe?: Probe }) {
  let online = m.status?.online ?? null
  let latency = m.status?.latency_ms ?? null
  if (probe && !probe.busy) { online = probe.online; latency = probe.latency_ms }
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
      setProbes((p) => ({ ...p, [m.id]: { busy: false, ...r } }))
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
  const [d, setD] = useState<Draft>(machine ? draftOf(machine) : EMPTY)
  const [touched, setTouched] = useState<Partial<Record<keyof Draft, boolean>>>({})
  const [submitted, setSubmitted] = useState(false)
  const [serverErrors, setServerErrors] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const errors = validate(d)
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD((x) => ({ ...x, [k]: v }))
  const err = (k: keyof Draft) => (submitted || touched[k] ? errors[k] : undefined)
  const blur = (k: keyof Draft) => () => setTouched((t) => ({ ...t, [k]: true }))
  const opc = d.kind === 'chy' || d.kind === 'em63'

  const submit = async (ev: FormEvent) => {
    ev.preventDefault()
    setSubmitted(true)
    if (Object.keys(errors).length) return
    setSaving(true); setServerErrors([])
    try {
      await saveMachine(toInput(d), machine?.id)
      onSaved()
    } catch (e) {
      setServerErrors(e instanceof ApiError ? e.messages : [e instanceof Error ? e.message : String(e)])
    } finally {
      setSaving(false)
    }
  }

  const text = (k: keyof Draft, label: string, extra: { hint?: string; placeholder?: string; list?: string; inputMode?: 'numeric'; type?: string } = {}) => (
    <label className="field">
      {label}
      <input
        type={extra.type ?? 'text'} inputMode={extra.inputMode} list={extra.list}
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
        <h3>{machine ? `Editar ${machine.name}` : 'Nova máquina'}</h3>
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
          <select value={d.kind} onChange={(e) => set('kind', e.target.value as MachineKind)}>
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

      {opc && (
        <fieldset className="form-group">
          <legend>Tela ao vivo (VNC)</legend>
          <label className="check"><input type="checkbox" checked={d.vnc_enabled} onChange={(e) => set('vnc_enabled', e.target.checked)} />Mostrar a tela da máquina no monitor</label>
          {d.vnc_enabled && (
            <div className="form-grid">
              {text('vnc_port', 'Porta VNC', { inputMode: 'numeric' })}
              {text('vnc_view_password', 'Senha de visualização', {
                type: 'password', placeholder: machine?.has_view_password ? 'deixe em branco para manter' : '',
                hint: 'Só assistir a tela.',
              })}
              {text('vnc_control_password', 'Senha de operação', {
                type: 'password', placeholder: machine?.has_control_password ? 'deixe em branco para manter' : '',
                hint: 'Opcional. Quem a digitar pode assumir o controle da tela.',
              })}
            </div>
          )}
        </fieldset>
      )}

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
