import { AlertTriangle, Pencil, Plus, Trash2, X } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { ApiError, deleteMold, saveMold, usePoll } from './api'
import type { Mold } from './api'

interface Draft { code: string; name: string; cavities: string; active: string; notes: string }

const EMPTY: Draft = { code: '', name: '', cavities: '1', active: '', notes: '' }

const isInt = (v: string, min: number, max: number) => /^\d+$/.test(v) && +v >= min && +v <= max

function validate(d: Draft): Partial<Record<keyof Draft, string>> {
  const e: Partial<Record<keyof Draft, string>> = {}
  if (!d.code.trim()) e.code = 'Informe o código do molde.'
  if (!isInt(d.cavities, 1, 512)) e.cavities = 'Use um número de 1 a 512.'
  else if (d.active !== '' && !isInt(d.active, 1, +d.cavities)) e.active = `Use um número de 1 a ${d.cavities}.`
  return e
}

/** Cadastro de moldes: as cavidades ativas definem quantas peças cada ciclo da máquina vale. */
export function MoldsPage() {
  const { data, error, reload } = usePoll<{ molds: Mold[] }>('/api/molds', 15000)
  const [editing, setEditing] = useState<Mold | 'new' | null>(null)
  const [confirmDel, setConfirmDel] = useState<number | null>(null)
  const [notice, setNotice] = useState<string[]>([])
  const fail = (e: unknown) => setNotice(e instanceof ApiError ? e.messages : [e instanceof Error ? e.message : String(e)])

  const remove = async (m: Mold) => {
    setConfirmDel(null); setNotice([])
    try { await deleteMold(m.id); if (editing !== 'new' && editing?.id === m.id) setEditing(null); reload() } catch (e) { fail(e) }
  }

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h2>Moldes</h2>
          <p>A máquina conta ciclos. Com o molde montado, cada ciclo vale tantas peças quanto as cavidades ativas dele.
            Monte o molde na máquina em <b>Máquinas</b>.</p>
        </div>
        <span className="spacer" />
        <button className="btn primary" onClick={() => { setEditing('new'); setNotice([]) }}><Plus size={16} aria-hidden="true" />Novo molde</button>
      </div>

      {error && !data && <div className="banner error" role="alert"><AlertTriangle size={18} aria-hidden="true" /><p>Não consegui carregar os moldes. {error}</p></div>}
      {notice.length > 0 && (
        <div className="banner error" role="alert"><AlertTriangle size={18} aria-hidden="true" /><div>{notice.map((n) => <p key={n}>{n}</p>)}</div></div>
      )}

      <div className={`machines-layout${editing ? ' with-form' : ''}`}>
        <section className="panel" aria-label="Moldes cadastrados">
          {!data && !error && <p className="muted">Carregando…</p>}
          {data && data.molds.length === 0 && (
            <p className="muted" style={{ margin: 0 }}>Nenhum molde cadastrado. Sem molde montado, a máquina conta 1 peça por ciclo.</p>
          )}
          {data && data.molds.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th scope="col">Código</th><th scope="col">Nome</th><th scope="col" className="num">Cavidades</th>
                    <th scope="col" className="num">Ativas</th><th scope="col">Montado em</th><th scope="col">Ações</th></tr>
                </thead>
                <tbody>
                  {data.molds.map((m) => (
                    <tr key={m.id}>
                      <td className="num">{m.code}</td>
                      <td>{m.name || <span className="muted">—</span>}</td>
                      <td className="num">{m.cavities}</td>
                      <td className="num">{m.active_cavities}{m.active_cavities < m.cavities && <span className="muted"> ({m.cavities - m.active_cavities} tampada{m.cavities - m.active_cavities > 1 ? 's' : ''})</span>}</td>
                      <td>{m.mounted_on ? m.mounted_on.machine_name : <span className="muted">Nenhuma</span>}</td>
                      <td>
                        <span className="form-actions">
                          <button className="btn" onClick={() => { setEditing(m); setNotice([]) }}><Pencil size={14} aria-hidden="true" />Editar</button>
                          {confirmDel === m.id ? (
                            <span className="confirm">
                              <button className="btn danger" onClick={() => remove(m)}>Apagar molde {m.code}</button>
                              <button className="btn" onClick={() => setConfirmDel(null)}>Cancelar</button>
                            </span>
                          ) : (
                            <button className="btn" disabled={!!m.mounted_on} onClick={() => setConfirmDel(m.id)}
                              title={m.mounted_on ? 'Retire o molde da máquina antes de apagar.' : undefined}>
                              <Trash2 size={14} aria-hidden="true" />Apagar
                            </button>
                          )}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {editing && (
          <MoldForm key={editing === 'new' ? 'new' : editing.id} mold={editing === 'new' ? null : editing}
            onClose={() => setEditing(null)} onSaved={() => { setEditing(null); reload() }} />
        )}
      </div>
    </div>
  )
}

function MoldForm({ mold, onClose, onSaved }: { mold: Mold | null; onClose: () => void; onSaved: () => void }) {
  const [d, setD] = useState<Draft>(mold
    ? { code: mold.code, name: mold.name, cavities: String(mold.cavities), active: String(mold.active_cavities), notes: mold.notes }
    : EMPTY)
  const [touched, setTouched] = useState<Partial<Record<keyof Draft, boolean>>>({})
  const [busy, setBusy] = useState(false)
  const [errs, setErrs] = useState<string[]>([])
  const v = validate(d)
  const set = (k: keyof Draft) => (e: { target: { value: string } }) => setD({ ...d, [k]: e.target.value })
  const blur = (k: keyof Draft) => () => setTouched({ ...touched, [k]: true })
  const show = (k: keyof Draft) => (touched[k] ? v[k] : undefined)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (Object.keys(v).length) { setTouched({ code: true, cavities: true, active: true }); return }
    setBusy(true); setErrs([])
    try {
      // cavidades ativas em branco = todas
      await saveMold({ code: d.code.trim(), name: d.name.trim(), cavities: +d.cavities,
        active_cavities: d.active === '' ? +d.cavities : +d.active, notes: d.notes }, mold?.id)
      onSaved()
    } catch (err) {
      setErrs(err instanceof ApiError ? err.messages : [String(err)])
    } finally { setBusy(false) }
  }

  const field = (k: keyof Draft, label: string, hint?: string, mode?: 'numeric') => (
    <label className="field">
      {label}
      <input value={d[k]} onChange={set(k)} onBlur={blur(k)} inputMode={mode} aria-invalid={show(k) ? true : undefined} />
      {show(k) ? <span className="field-error">{show(k)}</span> : hint && <span className="field-hint">{hint}</span>}
    </label>
  )

  return (
    <form className="panel form-grid" onSubmit={submit} aria-label={mold ? `Editar molde ${mold.code}` : 'Novo molde'}>
      <div className="panel-head"><h3>{mold ? `Editar molde ${mold.code}` : 'Novo molde'}</h3><span className="spacer" />
        <button type="button" className="btn icon" onClick={onClose} aria-label="Fechar"><X size={16} /></button></div>
      {field('code', 'Código', 'Ex.: 9000001')}
      {field('name', 'Nome', 'Ex.: Copinho 200 ml')}
      {field('cavities', 'Cavidades', 'Quantas peças o molde faz por ciclo.', 'numeric')}
      {field('active', 'Cavidades ativas', 'Em branco = todas. Diminua se alguma cavidade estiver tampada.', 'numeric')}
      {mold?.mounted_on && d.active !== String(mold.active_cavities) && (
        <p className="field-hint">O molde está montado em {mold.mounted_on.machine_name}: a contagem nova vale a partir de agora e fica no histórico de trocas.</p>
      )}
      <label className="field">Observações<textarea value={d.notes} onChange={set('notes')} rows={2} /></label>
      {errs.length > 0 && <div className="banner error" role="alert"><AlertTriangle size={16} aria-hidden="true" /><div>{errs.map((x) => <p key={x}>{x}</p>)}</div></div>}
      <div className="form-actions">
        <button className="btn primary" type="submit" disabled={busy}>{busy ? 'Salvando…' : mold ? 'Salvar molde' : 'Cadastrar molde'}</button>
        <button className="btn" type="button" onClick={onClose}>Cancelar</button>
      </div>
    </form>
  )
}
