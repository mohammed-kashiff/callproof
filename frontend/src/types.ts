export type Verdict =
  | 'PASS'
  | 'PARTIAL'
  | 'FAIL'
  | 'UNVERIFIED'
  | 'N/A'
  | 'GATE FAIL'

export type CheckType = 'RULE' | 'AI' | 'BOTH'

export type PerformanceBand =
  | 'Excellent'
  | 'Good'
  | 'Needs Improvement'
  | 'Poor'
  | 'Star Performer'
  | 'Excelling'
  | 'Solid Performer'
  | 'Developing'
  | 'Needs Immediate Attention'

export type ChurnLevel = 'none' | 'low' | 'medium' | 'high'

export type PipelineStepId =
  | 'upload'
  | 'transcribe'
  | 'evaluate'
  | 'score'
  | 'report'

export type PipelineStatus = 'idle' | 'active' | 'done' | 'error'

export type JobStatus =
  | 'queued'
  | 'transcoding'
  | 'uploading'
  | 'auditing'
  | 'done'
  | 'failed'

export interface CriterionFinding {
  id: string
  name: string
  weight: number
  checkType: CheckType
  isGate: boolean
  verdict: Verdict
  pointsEarned: number
  pointsPossible: number
  rationale: string
  evidenceQuote: string | null
  evidenceTimestamp: number | null
  coachingTip: string | null
}

export interface TranscriptSegment {
  id: string
  speaker: 'agent' | 'customer'
  start: number
  end: number
  text: string
}

export interface CustomerFeedback {
  aboutAgent: string[]
  aboutProduct: string[]
  status?: string
}

export interface CallSummary {
  headline: string
  narrative: string
  actionItems: string[]
}

export interface ChurnRisk {
  level: ChurnLevel
  quote: string
  timestamp: number
  reasoning?: string
}

export interface AuditReport {
  callId: string
  numericCallId: number | null
  fileName: string
  durationSec: number
  analyzedAt: string
  agentName: string
  customerLabel: string
  overallScore: number
  band: PerformanceBand
  grade: string
  gateFailed: boolean
  flagged: boolean
  reviewSolved: boolean
  rubricLabel: string
  auditMode: string
  audioUrl: string | null
  criteria: CriterionFinding[]
  churn: ChurnRisk
  feedback: CustomerFeedback
  summary: CallSummary
  transcript: TranscriptSegment[]
}

export interface BulkJob {
  key: string
  name: string
  sizeMb: string
  fingerprint: string
  status: JobStatus
  callId: number | null
  score: number | null
  error: string | null
  file?: File
  viaZip?: boolean
  startedAt?: number | null
  finishedAt?: number | null
  elapsedMs?: number | null
}

export interface CallListItem {
  id: number
  filename: string
  status: string | null
  audio_seconds: number | null
  created_at: string | null
  has_audit: boolean
  score: number | null
  grade: string | null
  flagged: boolean
  review_solved: boolean
  churn_risk?: string | null
  cost?: { total_usd?: number } | null
}

export interface PyaiStatus {
  ok: boolean
  healthy: boolean
  label?: string
  quota_label?: string
  usage_label?: string
  env?: string | null
  pyai_actions?: number
  pyai_polls?: number
  claude_hits?: number
  cost_today?: {
    pyai_usd?: number
    claude_usd?: number
    total_usd?: number
    pyai_basis?: string
    claude_hits?: number
    label?: string
  } | null
}

export interface FlaggedCallRow {
  id: number
  filename: string
  score: number | null
  grade: string
  agent_name: string
  audio_seconds: number | null
  solved: boolean
  reasons: string
  created_at: string | null
  audited_at: string | null
}
