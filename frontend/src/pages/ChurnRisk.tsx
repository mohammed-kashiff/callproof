import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { CallPicker } from '../components/CallPicker'
import { ChurnCue } from '../components/LoopCues'
import { SketchWallpaper } from '../components/SketchWallpaper'
import { KpiCard } from '../components/KpiCard'
import { Workspace, callNoteScopeKey } from '../components/Workspace'
import { capFirst, capWords, formatTime } from '../lib/format'
import { API, readError } from '../lib/api'
import { useAudit } from '../context/AuditContext'
import type { ChurnLevel } from '../types'

const LEVELS: { level: ChurnLevel; label: string; hint: string }[] = [
  { level: 'none', label: 'None', hint: 'No churn language detected' },
  { level: 'low', label: 'Low', hint: 'Mild dissatisfaction, no switch threat' },
  { level: 'medium', label: 'Medium', hint: 'Explicit provider-switch risk' },
  { level: 'high', label: 'High', hint: 'Imminent cancel / escalate language' },
]

const MARKED_CHURN = new Set<ChurnLevel>(['low', 'medium', 'high'])

function isMarkedChurnRisk(risk: string | null | undefined): boolean {
  return MARKED_CHURN.has(String(risk || '').toLowerCase() as ChurnLevel)
}

export function ChurnRisk() {
  const navigate = useNavigate()
  const { report, showReport, calls, selectCall, running, onSeek } = useAudit()
  const { churn } = report
  const [switching, setSwitching] = useState(false)
  const [emailing, setEmailing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const callLabel = capFirst(report.fileName || report.callId || 'Current call')
  const agentLabel = capWords(report.agentName)
  const callIdLabel =
    report.numericCallId != null ? `Call #${report.numericCallId}` : report.callId || ''

  const churnRiskCalls = useMemo(
    () => calls.filter((c) => isMarkedChurnRisk(c.churn_risk)),
    [calls],
  )
  const viewingChurn = showReport && isMarkedChurnRisk(churn.level)
  const canEmailStakeholder = churn.level === 'medium' || churn.level === 'high'

  const onPickCall = (id: number) => {
    if (id === report.numericCallId) return
    setError(null)
    setSwitching(true)
    void selectCall(id)
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : 'Could not open that call.'),
      )
      .finally(() => setSwitching(false))
  }

  const sendStakeholderEmail = () => {
    const id = report.numericCallId
    if (id == null || emailing || !canEmailStakeholder) return
    setError(null)
    setEmailing(true)
    void (async () => {
      try {
        const r = await fetch(`${API}/api/calls/${id}/stakeholder-email/compose`)
        if (!r.ok) {
          throw new Error(await readError(r, 'Could not draft the stakeholder email.'))
        }
        const data = (await r.json()) as { gmail_url?: string }
        const url = (data.gmail_url || '').trim()
        if (!url.startsWith('https://mail.google.com/')) {
          throw new Error('Could not open the stakeholder email draft.')
        }
        window.open(url, '_blank', 'noopener,noreferrer')
      } catch (e: unknown) {
        setError(
          e instanceof Error ? e.message : 'Could not draft the stakeholder email.',
        )
      } finally {
        setEmailing(false)
      }
    })()
  }

  const callPicker =
    churnRiskCalls.length > 0 ? (
      <CallPicker
        calls={churnRiskCalls}
        value={report.numericCallId}
        disabled={running || switching}
        onChange={onPickCall}
      />
    ) : null

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Loop / Retention</p>
          <h1>Churn Risk</h1>
        </div>
        {!viewingChurn ? callPicker : null}
      </header>

      {error && (
        <p className="upload-error" role="alert">
          {error}
        </p>
      )}

      {!viewingChurn && (
        <div className="empty-card is-pulse">
          <SketchWallpaper variant="churn" />
          <ChurnCue />
          <p className="empty-title">No churn language yet</p>
          <p className="empty-copy">
            {churnRiskCalls.length
              ? 'Pick a call above to score retention risk.'
              : 'Ingest a recording to score retention risk before renewal.'}
          </p>
        </div>
      )}

      {viewingChurn && (
        <>
          <div className="call-context-banner" role="status">
            <div className="call-context-copy">
              <p className="call-context-kicker">Churn risk for this call</p>
              <p className="call-context-title" title={callLabel}>
                {callLabel}
              </p>
              <p className="call-context-meta">
                {[callIdLabel, agentLabel ? `Agent · ${agentLabel}` : null]
                  .filter(Boolean)
                  .join(' · ')}
              </p>
              {callPicker}
            </div>
            <div className="call-context-actions">
              <button
                type="button"
                className="ghost-btn call-context-link"
                disabled={running || switching || emailing || !canEmailStakeholder}
                title={
                  canEmailStakeholder
                    ? 'Open a Gmail draft for this churn alert'
                    : 'Stakeholder email is available for medium and high churn risk'
                }
                onClick={sendStakeholderEmail}
              >
                {emailing ? 'Drafting email…' : 'Send an email to stakeholder'}
              </button>
            </div>
          </div>

          <section className="churn-reason-box" aria-label="Why this call was flagged">
            <p className="churn-reason-kicker">Why this was flagged</p>
            <p className="churn-reason-body">
              {capFirst(churn.reasoning) ||
                'No churn reasoning was recorded for this call.'}
            </p>
          </section>

          <div className="kpi-strip">
            <KpiCard
              label="Rating"
              value={capFirst(churn.level)}
              hint="From The Driving Quote"
              tone={
                churn.level === 'high' ? 'bad' : churn.level === 'medium' ? 'warn' : 'good'
              }
              fill
            />
            <KpiCard
              label="Agent"
              value={agentLabel || '—'}
              hint={capFirst(report.fileName)}
            />
            <KpiCard
              label="Actions"
              value={String(report.summary.actionItems.length)}
              hint="To Close This Loop"
            />
          </div>

          <Workspace
            noteScopeKey={callNoteScopeKey(report.numericCallId, report.callId)}
            tabs={[
              {
                id: 'scale',
                label: 'Scale',
                panel: (
                  <div className="risk-meter" role="list">
                    {LEVELS.map((band) => (
                      <div
                        key={band.level}
                        role="listitem"
                        className={[
                          'risk-seg',
                          `churn-${band.level}`,
                          band.level === churn.level ? 'is-current' : '',
                        ]
                          .filter(Boolean)
                          .join(' ')}
                      >
                        <span className={`risk-seg-label churn-level churn-${band.level}`}>
                          {band.label}
                        </span>
                        <span>{band.hint}</span>
                      </div>
                    ))}
                  </div>
                ),
              },
              {
                id: 'quote',
                label: 'Quote',
                panel: (
                  <blockquote className="evidence">
                    {churn.quote ? <p>“{churn.quote}”</p> : <p>No driving quote on this call.</p>}
                    {churn.timestamp > 0 && (
                      <button
                        type="button"
                        className="timestamp-btn"
                        onClick={() => {
                          onSeek(churn.timestamp)
                          navigate('/agents-pulse')
                        }}
                      >
                        Play at {formatTime(churn.timestamp)}
                      </button>
                    )}
                  </blockquote>
                ),
              },
              {
                id: 'actions',
                label: 'Close The Loop',
                panel: (
                  <ol className="action-items">
                    {report.summary.actionItems.map((item) => (
                      <li key={item}>{capFirst(item)}</li>
                    ))}
                  </ol>
                ),
              },
            ]}
          />
        </>
      )}
    </>
  )
}
