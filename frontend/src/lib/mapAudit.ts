import { API } from './api'
import type {
  AuditReport,
  CheckType,
  ChurnLevel,
  CriterionFinding,
  PerformanceBand,
  TranscriptSegment,
  Verdict,
} from '../types'

const FRACTION: Record<string, number> = {
  pass: 1,
  partial: 0.5,
  fail: 0,
  unverified: 0,
  error: 0,
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' ? (v as Record<string, unknown>) : {}
}

function asString(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback
}

function asNumber(v: unknown, fallback = 0): number {
  const n = Number(v)
  return Number.isFinite(n) ? n : fallback
}

function mapVerdict(raw: unknown): Verdict {
  const v = String(raw || '').toLowerCase()
  if (v === 'pass') return 'PASS'
  if (v === 'partial') return 'PARTIAL'
  if (v === 'fail') return 'FAIL'
  if (v === 'unverified') return 'UNVERIFIED'
  if (v === 'n/a' || v === 'na') return 'N/A'
  if (v.includes('gate')) return 'GATE FAIL'
  return 'UNVERIFIED'
}

function mapCheckType(method: unknown): CheckType {
  const m = String(method || '')
  if (m === 'deterministic_hybrid') return 'BOTH'
  if (m === 'llm' || m.includes('llm')) return 'AI'
  return 'RULE'
}

function mapBand(grade: unknown, score: number): PerformanceBand {
  const g = String(grade || '')
  if (
    g === 'Star Performer' ||
    g === 'Excelling' ||
    g === 'Solid Performer' ||
    g === 'Developing' ||
    g === 'Needs Improvement' ||
    g === 'Needs Immediate Attention' ||
    g === 'Excellent' ||
    g === 'Good' ||
    g === 'Poor'
  ) {
    return g as PerformanceBand
  }
  if (score >= 90) return 'Excellent'
  if (score >= 75) return 'Good'
  if (score >= 60) return 'Needs Improvement'
  return 'Poor'
}

function mapChurn(level: unknown): ChurnLevel {
  const v = String(level || '').toLowerCase()
  if (v === 'high' || v === 'medium' || v === 'low' || v === 'none') return v
  return 'none'
}

function feedbackLines(items: unknown): string[] {
  if (!Array.isArray(items)) return []
  return items
    .map((it) => {
      const row = asRecord(it)
      const summary = asString(row.summary)
      const quote = asString(row.quote)
      if (summary && quote) return `${summary} — “${quote}”`
      return summary || quote
    })
    .filter(Boolean)
}

export function emptyReport(): AuditReport {
  return {
    callId: '',
    numericCallId: null,
    fileName: '',
    durationSec: 0,
    analyzedAt: '',
    agentName: 'Agent',
    customerLabel: 'Customer',
    overallScore: 0,
    band: 'Poor',
    grade: '',
    gateFailed: false,
    flagged: false,
    reviewSolved: false,
    rubricLabel: 'v8',
    auditMode: '',
    audioUrl: null,
    criteria: [],
    churn: { level: 'none', quote: '', timestamp: 0, reasoning: '' },
    feedback: { aboutAgent: [], aboutProduct: [], status: 'skipped' },
    summary: { headline: '', narrative: '', actionItems: [] },
    transcript: [],
  }
}

export function mapAudit(raw: unknown): AuditReport {
  const audit = asRecord(raw)
  const callId = asNumber(audit.call_id)
  const score = asNumber(audit.score)
  const grade = asString(audit.grade)
  const agent = asString(audit.agent_speaker, 'speaker_1')
  const segments = Array.isArray(audit.segments) ? audit.segments : []
  const findings = Array.isArray(audit.findings) ? audit.findings : []
  const recap = asRecord(audit.recap)
  const churn = asRecord(audit.churn)
  const feedback = asRecord(audit.feedback)
  const managerReview = Array.isArray(audit.manager_review) ? audit.manager_review : []

  const segBySeq = new Map<number, Record<string, unknown>>()
  for (const s of segments) {
    const row = asRecord(s)
    segBySeq.set(asNumber(row.seq), row)
  }

  const transcript: TranscriptSegment[] = segments.map((s, i) => {
    const row = asRecord(s)
    const speaker = asString(row.speaker)
    return {
      id: String(row.seq ?? i),
      speaker: speaker === agent ? 'agent' : 'customer',
      start: asNumber(row.start),
      end: asNumber(row.end),
      text: asString(row.text),
    }
  })

  const criteria: CriterionFinding[] = findings.map((f, i) => {
    const row = asRecord(f)
    const verdict = mapVerdict(row.verdict)
    const weight = asNumber(row.weight)
    const points =
      row.points != null
        ? asNumber(row.points)
        : (FRACTION[String(row.verdict || '').toLowerCase()] ?? 0) * weight
    const seq = row.evidence_seq == null ? null : asNumber(row.evidence_seq)
    const seg = seq != null ? segBySeq.get(seq) : undefined
    const fail = verdict === 'FAIL' || verdict === 'PARTIAL' || verdict === 'GATE FAIL'
    return {
      id: asString(row.id, `finding-${i}`),
      name: asString(row.name, 'Criterion'),
      weight,
      checkType: mapCheckType(row.method),
      isGate: Boolean(row.is_gate),
      verdict,
      pointsEarned: points,
      pointsPossible: weight,
      rationale: asString(row.why) || asString(row.reasoning),
      evidenceQuote: asString(row.evidence_text) || null,
      evidenceTimestamp: seg ? asNumber(seg.start) : null,
      coachingTip: fail ? asString(row.why) || asString(row.reasoning) || null : null,
    }
  })

  const actions = Array.isArray(recap.action_items)
    ? recap.action_items
        .map((it) => {
          const row = asRecord(it)
          return asString(row.task) || asString(it)
        })
        .filter(Boolean)
    : []

  const evidenceSeq = churn.evidence_seq == null ? null : asNumber(churn.evidence_seq)
  const churnSeg = evidenceSeq != null ? segBySeq.get(evidenceSeq) : undefined

  return {
    callId: callId ? `call-${callId}` : '',
    numericCallId: callId || null,
    fileName: asString(audit.filename, callId ? `call-${callId}` : ''),
    durationSec: asNumber(audit.audio_seconds),
    analyzedAt: asString(audit.analyzed_at),
    agentName: agent.replace(/_/g, ' '),
    customerLabel: 'Customer',
    overallScore: score,
    band: mapBand(grade, score),
    grade,
    gateFailed: managerReview.length > 0 || Boolean(audit.flagged),
    flagged: Boolean(audit.flagged) || Boolean(audit.manual_review) || managerReview.length > 0,
    reviewSolved: Boolean(audit.review_solved),
    rubricLabel: asString(audit.rubric_id) || asString(audit.rubric) || 'v8',
    auditMode: asString(audit.audit_mode),
    audioUrl: callId ? `${API}/api/calls/${callId}/audio` : null,
    criteria,
    churn: {
      level: mapChurn(churn.risk),
      quote: asString(churn.evidence_text) || asString(churn.reasoning),
      timestamp: churnSeg ? asNumber(churnSeg.start) : 0,
      reasoning: asString(churn.reasoning),
    },
    feedback: {
      aboutAgent: feedbackLines(feedback.agent),
      aboutProduct: feedbackLines(feedback.product),
      status: asString(feedback.status, 'skipped'),
    },
    summary: {
      headline: asString(recap.tldr) || asString(recap.headline) || grade || 'Scorecard',
      narrative: asString(recap.summary),
      actionItems: actions,
    },
    transcript,
  }
}
