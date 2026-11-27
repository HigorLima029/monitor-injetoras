import { AlertTriangle, Check, PauseCircle, Pencil, Plus } from 'lucide-react'
import { useState } from 'react'
import { addStopReason, ApiError, classifyStop, updateStopReason, usePoll } from './api'
import type { Stop, StopReason, Stops } from './api'
import { dateTime, duration } from './format'

const PERIODS = [{ h: 8, label: '8 h' }, { h: 24, label: '24 h' }, { h: 24 * 7, label: '7 dias' }]

/** Paradas da máquina aberta: o sistema registra sozinho; a pessoa confirma o motivo ou escreve o que houve. */
export function StopsPage() {
  const [hours, setHours] = useState(24)
  const [onlyOpen, setOnlyOpen] = useState(false)
  const { data, error, reload } = usePoll<Stops>(`/api/stops?hours=${hours}`, 15000)
  const reasons = usePoll<{ reasons: StopReason[] }>('/api/stop-reasons', 60000)
  const [editing, setEditing] = useState<number | null>(null)
  const [showReasons, setShowReasons] = useState(false)

  const list = (data?.stops ?? []).filter((s) => !onlyOpen || !s.reason)
  const active = (reasons.data?.reasons ?? []).filter((r) => r.active)
  const maxSec = Math.max(1, ...(data?.summary ?? []).map((a) => a.seconds))

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h2>Paradas</h2>
          <p>O monitor registra sozinho quando a máquina fica sem ciclar mais que o tempo mínimo (em Máquinas → Editar).
            Escolha o motivo ou escreva o que aconteceu.</p>
        </div>
        <span className="spacer" />
        <div className="seg" role="group" aria-label="Período">
          {PERIODS.map((p) => (
            <button key={p.h} className={hours === p.h ? 'btn primary' : 'btn'} aria-pressed={hours === p.h} onClick={() => setHours(p.h)}>{p.label}</button>
          ))}
        </div>
      </div>

      {error && !data && <div className="banner error" role="alert"><AlertTriangle size={18} aria-hidden="true" /><p>Não consegui carregar as paradas. {error}</p></div>}

      {data && (
        <section className="panel" aria-label="Tempo parado por motivo">
          <div className="panel-head">
            <h3>Tempo parado: {duration(data.total_seconds)}</h3>
            <span className="note">{data.stops.length} {data.stops.length === 1 ? 'parada' : 'paradas'}{data.unclassified > 0 && ` · ${data.unclassified} sem motivo`}</span>
          </div>
          {data.summary.length === 0
            ? <p className="muted" style={{ margin: 0 }}>Nenhuma parada no período.</p>
            : <ul className="stop-summary">
                {data.summary.map((a) => (
                  <li key={a.reason ?? ''}>
                    <span className={a.reason ? '' : 'muted'}>{a.label}</span>
                    <span className="bar" aria-hidden="true"><span style={{ width: `${(a.seconds / maxSec) * 100}%` }} className={a.reason ? '' : 'unset'} /></span>
                    <span className="num">{duration(a.seconds)} · {a.count}×</span>
                  </li>
                ))}
              </ul>}
        </section>
      )}

      <section className="panel" aria-label="Lista de paradas">
        <div className="panel-head">
          <h3>Paradas</h3>
          <span className="spacer" />
          <label className="check"><input type="checkbox" checked={onlyOpen} onChange={(e) => setOnlyOpen(e.target.checked)} />Só sem motivo</label>
        </div>
        {!data && !error && <p className="muted">Carregando…</p>}
        {data && list.length === 0 && <p className="muted" style={{ margin: 0 }}>{onlyOpen ? 'Todas as paradas do período têm motivo.' : 'Nenhuma parada registrada no período.'}</p>}
        <ul className="stop-list">
          {list.map((s) => (
            <StopRow key={s.id} s={s} reasons={active} editing={editing === s.id}
              onEdit={() => setEditing(s.id)} onDone={() => { setEditing(null); reload() }} onCancel={() => setEditing(null)} />
          ))}
        </ul>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h3>Motivos</h3>
          <span className="spacer" />
          <button className="btn link" aria-expanded={showReasons} onClick={() => setShowReasons((x) => !x)}>
            {showReasons ? 'Fechar' : 'Ver e editar a lista'}
          </button>
        </div>
        {showReasons && reasons.data && <ReasonsEditor reasons={reasons.data.reasons} onChanged={reasons.reload} />}
      </section>
    </div>
  )
}

function StopRow({ s, reasons, editing, onEdit, onDone, onCancel }: {
  s: Stop; reasons: StopReason[]; editing: boolean; onEdit: () => void; onDone: () => void; onCancel: () => void
}) {
  const [reason, setReason] = useState(s.reason ?? '')
  const [note, setNote] = useState(s.note)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string[]>([])
  // motivo desativado depois de usado continua aparecendo nesta parada
  const options = s.reason && !reasons.some((r) => r.code === s.reason)
    ? [...reasons, { code: s.reason, label: s.reason_label ?? s.reason, active: false }] : reasons

  const save = async () => {
    setBusy(true); setErr([])
    try { await classifyStop(s.id, reason || null, note); onDone() }
    catch (e) { setErr(e instanceof ApiError ? e.messages : [String(e)]) } finally { setBusy(false) }
  }

  return (
    <li className={s.open ? 'stop-row open' : 'stop-row'}>
      <div className="stop-when">
        <PauseCircle size={16} aria-hidden="true" />
        <span className="num">{dateTime(s.started)}</span>
        <span className="muted">→ {s.open ? 'parada agora' : dateTime(s.ended!)}</span>
        <b className="num">{duration(s.duration)}</b>
      </div>
      {!editing ? (
        <div className="stop-what">
          {s.reason
            ? <span className="chip">{s.reason_label}{s.auto && <span className="muted"> · sugerido</span>}</span>
            : <span className="chip warn">Sem motivo</span>}
          {s.detail && <span className="muted small">{s.detail}</span>}
          {s.note && <span className="small">“{s.note}”</span>}
          <button className="btn link" onClick={onEdit}><Pencil size={13} aria-hidden="true" />{s.reason ? 'Alterar' : 'Informar motivo'}</button>
        </div>
      ) : (
        <div className="stop-edit">
          <label className="field">
            Motivo
            <select value={reason} onChange={(e) => setReason(e.target.value)}>
              <option value="">Sem motivo</option>
              {options.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}
            </select>
          </label>
          <label className="field grow">
            O que aconteceu
            <input value={note} maxLength={500} onChange={(e) => setNote(e.target.value)}
              placeholder="Ex.: queimou a resistência da zona 3, aguardando eletricista" />
            <span className="field-hint">Se nenhum motivo serve, só escreva aqui: a parada fica como “Outro”.</span>
          </label>
          <span className="form-actions">
            <button className="btn primary" disabled={busy} onClick={save}><Check size={14} aria-hidden="true" />{busy ? 'Salvando…' : 'Salvar'}</button>
            <button className="btn" onClick={onCancel}>Cancelar</button>
          </span>
          {err.length > 0 && <p className="field-error">{err.join(' · ')}</p>}
        </div>
      )}
    </li>
  )
}

function ReasonsEditor({ reasons, onChanged }: { reasons: StopReason[]; onChanged: () => void }) {
  const [label, setLabel] = useState('')
  const [renaming, setRenaming] = useState<{ code: string; label: string } | null>(null)
  const [err, setErr] = useState<string[]>([])
  const run = async (f: () => Promise<unknown>) => {
    setErr([])
    try { await f(); onChanged() } catch (e) { setErr(e instanceof ApiError ? e.messages : [String(e)]) }
  }

  return (
    <div className="stack">
      <p className="muted small" style={{ margin: 0 }}>A lista já vem pronta. Desativar esconde o motivo das paradas novas, mas mantém nas antigas.</p>
      <ul className="reason-list">
        {reasons.map((r) => (
          <li key={r.code} className={r.active ? '' : 'muted'}>
            {renaming?.code === r.code ? (
              <span className="form-actions">
                <input aria-label={`Novo nome para ${r.label}`} value={renaming.label} onChange={(e) => setRenaming({ ...renaming, label: e.target.value })} />
                <button className="btn primary" onClick={() => run(async () => { await updateStopReason(r.code, { label: renaming.label }); setRenaming(null) })}>Salvar</button>
                <button className="btn" onClick={() => setRenaming(null)}>Cancelar</button>
              </span>
            ) : (
              <>
                <span>{r.label}{!r.active && ' (desativado)'}</span>
                <span className="spacer" />
                <button className="btn link" onClick={() => setRenaming({ code: r.code, label: r.label })}>Renomear</button>
                {r.code !== 'outro' && (
                  <button className="btn link" onClick={() => run(() => updateStopReason(r.code, { active: !r.active }))}>
                    {r.active ? 'Desativar' : 'Reativar'}
                  </button>
                )}
              </>
            )}
          </li>
        ))}
      </ul>
      <form className="form-actions" onSubmit={(e) => { e.preventDefault(); run(async () => { await addStopReason(label); setLabel('') }) }}>
        <input aria-label="Nome do novo motivo" placeholder="Novo motivo" value={label} onChange={(e) => setLabel(e.target.value)} />
        <button className="btn" type="submit" disabled={!label.trim()}><Plus size={14} aria-hidden="true" />Adicionar motivo</button>
      </form>
      {err.length > 0 && <p className="field-error">{err.join(' · ')}</p>}
    </div>
  )
}
