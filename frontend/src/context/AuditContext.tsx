import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { API, readError } from '../lib/api'
import { emptyReport, mapAudit } from '../lib/mapAudit'
import { zipAudioFiles } from '../lib/zipAudio'
import { getHearFfmpeg, transcodeHearCopy } from '../hearTranscode'
import type {
  AuditReport,
  BulkJob,
  CallListItem,
  JobStatus,
  PipelineStatus,
  PipelineStepId,
} from '../types'

const MAX_UPLOAD_MB = 25
const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
export const MAX_BULK_FILES = 100

const idleStatuses = (): Record<PipelineStepId, PipelineStatus> => ({
  upload: 'idle',
  transcribe: 'idle',
  evaluate: 'idle',
  score: 'idle',
  report: 'idle',
})

function statusesForJobs(
  jobs: BulkJob[],
  running: boolean,
): { statuses: Record<PipelineStepId, PipelineStatus>; active: PipelineStepId | null } {
  if (!running && !jobs.some((j) => j.status !== 'queued' && j.status !== 'failed')) {
    return { statuses: idleStatuses(), active: null }
  }
  const has = (s: JobStatus) => jobs.some((j) => j.status === s)
  const allTerminal = jobs.every((j) => j.status === 'done' || j.status === 'failed' || j.status === 'queued')
  if (!running && allTerminal) {
    return {
      statuses: {
        upload: 'done',
        transcribe: 'done',
        evaluate: 'done',
        score: 'done',
        report: 'done',
      },
      active: null,
    }
  }
  if (has('transcoding') || has('uploading')) {
    return {
      statuses: {
        upload: 'done',
        transcribe: 'active',
        evaluate: 'idle',
        score: 'idle',
        report: 'idle',
      },
      active: 'transcribe',
    }
  }
  if (has('auditing')) {
    return {
      statuses: {
        upload: 'done',
        transcribe: 'done',
        evaluate: 'done',
        score: 'active',
        report: 'idle',
      },
      active: 'score',
    }
  }
  if (has('queued') && running) {
    return {
      statuses: {
        upload: 'active',
        transcribe: 'idle',
        evaluate: 'idle',
        score: 'idle',
        report: 'idle',
      },
      active: 'upload',
    }
  }
  return { statuses: idleStatuses(), active: null }
}

interface AuditContextValue {
  report: AuditReport
  rawAudit: Record<string, unknown> | null
  statuses: Record<PipelineStepId, PipelineStatus>
  activeStep: PipelineStepId | null
  running: boolean
  showReport: boolean
  scoreAnimate: boolean
  seekTo: number | null
  jobs: BulkJob[]
  bulkNote: string | null
  uploadError: string | null
  calls: CallListItem[]
  flaggedCount: number
  queueFiles: (fileList: FileList | File[]) => void
  removeQueued: (key: string) => void
  startImport: () => Promise<void>
  selectCall: (id: number) => Promise<void>
  flagCurrent: () => Promise<void>
  loadFeedback: () => Promise<void>
  feedbackLoading: boolean
  clearCache: () => Promise<void>
  exportScorecard: () => Promise<void>
  onSeek: (seconds: number) => void
  onSeekHandled: () => void
}

const AuditContext = createContext<AuditContextValue | null>(null)

async function uploadOneFile(file: File): Promise<number> {
  const fd = new FormData()
  fd.append('file', file)
  const r = await fetch(`${API}/api/upload`, { method: 'POST', body: fd })
  if (!r.ok) throw new Error(await readError(r, 'Upload failed'))
  const data = (await r.json()) as { call_id: number }
  return data.call_id
}

async function fetchAuditJson(id: number): Promise<Record<string, unknown>> {
  const r = await fetch(`${API}/api/calls/${id}/audit`)
  if (!r.ok) throw new Error('Could not load the audit for this call.')
  return r.json() as Promise<Record<string, unknown>>
}

type BatchCallRow = {
  call_id?: number | null
  score?: number | null
  status?: string
  error?: string | null
  filename?: string
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

async function fetchBatchStatus(batchId: string): Promise<{
  status: string
  calls: BatchCallRow[]
}> {
  const r = await fetch(`${API}/api/batches/${encodeURIComponent(batchId)}`)
  if (!r.ok) throw new Error(await readError(r, 'Could not read batch status.'))
  return r.json() as Promise<{ status: string; calls: BatchCallRow[] }>
}

async function uploadBatchZip(
  files: File[],
  onProgress?: (calls: BatchCallRow[]) => void,
): Promise<BatchCallRow[]> {
  const blob = await zipAudioFiles(files)
  const fd = new FormData()
  fd.append('file', blob, 'batch.zip')
  let r: Response
  try {
    r = await fetch(`${API}/api/upload-batch`, { method: 'POST', body: fd })
  } catch {
    throw new Error(
      'Batch upload was interrupted (network or tunnel timeout). Check Agents Pulse — some calls may still be processing.',
    )
  }
  if (!r.ok) throw new Error(await readError(r, 'Batch upload failed'))
  const data = (await r.json()) as {
    batch_id?: string
    status?: string
    calls?: BatchCallRow[]
  }
  if (Array.isArray(data.calls) && data.status && data.status !== 'processing') {
    onProgress?.(data.calls)
    return data.calls
  }
  const batchId = data.batch_id
  if (!batchId) throw new Error('Batch upload did not return a batch id.')
  onProgress?.(data.calls || [])
  for (;;) {
    await sleep(1500)
    let snap: { status: string; calls: BatchCallRow[] }
    try {
      snap = await fetchBatchStatus(batchId)
    } catch {
      throw new Error(
        'Lost connection while the batch was processing. Check Agents Pulse — calls may still appear as they finish.',
      )
    }
    onProgress?.(snap.calls || [])
    if (snap.status === 'done' || snap.status === 'error') {
      return snap.calls || []
    }
  }
}

export function AuditProvider({ children }: { children: ReactNode }) {
  const [rawAudit, setRawAudit] = useState<Record<string, unknown> | null>(null)
  const [report, setReport] = useState<AuditReport>(emptyReport)
  const [jobs, setJobs] = useState<BulkJob[]>([])
  const [bulkNote, setBulkNote] = useState<string | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [showReport, setShowReport] = useState(false)
  const [scoreAnimate, setScoreAnimate] = useState(false)
  const [seekTo, setSeekTo] = useState<number | null>(null)
  const [calls, setCalls] = useState<CallListItem[]>([])
  const [feedbackLoading, setFeedbackLoading] = useState(false)

  const applyAudit = useCallback((json: Record<string, unknown>) => {
    setRawAudit(json)
    setReport(mapAudit(json))
    setShowReport(true)
    requestAnimationFrame(() => setScoreAnimate(true))
  }, [])

  const refreshCalls = useCallback(async () => {
    try {
      const r = await fetch(`${API}/api/calls`)
      if (!r.ok) return [] as CallListItem[]
      const data = (await r.json()) as CallListItem[]
      setCalls(data)
      return data
    } catch {
      return [] as CallListItem[]
    }
  }, [])

  useEffect(() => {
    refreshCalls().catch(() => {})
  }, [refreshCalls])

  const patchJob = useCallback((key: string, patch: Partial<BulkJob>) => {
    setJobs((prev) =>
      prev.map((j) => {
        if (j.key !== key) return j
        const next: BulkJob = { ...j, ...patch }
        const active =
          next.status === 'transcoding' ||
          next.status === 'uploading' ||
          next.status === 'auditing'
        if (active && !next.startedAt) next.startedAt = Date.now()
        if (
          (next.status === 'done' || next.status === 'failed') &&
          next.finishedAt == null
        ) {
          next.finishedAt = Date.now()
          if (next.startedAt) next.elapsedMs = next.finishedAt - next.startedAt
        }
        return next
      }),
    )
  }, [])

  const queueFiles = useCallback(
    (fileList: FileList | File[]) => {
      if (running) return
      const incoming = Array.from(fileList || []).filter(Boolean)
      if (!incoming.length) return
      setUploadError(null)
      setJobs((prev) => {
        const hasQueued = prev.some((j) => j.status === 'queued')
        const base = hasQueued ? prev : []
        const seen = new Set(base.map((j) => j.fingerprint).filter(Boolean))
        const room = Math.max(0, MAX_BULK_FILES - base.length)
        if (room === 0) {
          setBulkNote(`Queue is full (${MAX_BULK_FILES} files).`)
          return prev
        }
        const added: BulkJob[] = []
        let skippedDup = 0
        for (const f of incoming) {
          if (added.length >= room) break
          const fingerprint = `${f.name}:${f.size}:${f.lastModified}`
          if (seen.has(fingerprint)) {
            skippedDup += 1
            continue
          }
          seen.add(fingerprint)
          const tooBig = f.size > MAX_UPLOAD_BYTES
          added.push({
            key: `${Date.now()}-${base.length + added.length}-${f.name}`,
            name: f.name || `file-${base.length + added.length + 1}`,
            sizeMb: (f.size / (1024 * 1024)).toFixed(1),
            fingerprint,
            status: tooBig ? 'failed' : 'queued',
            callId: null,
            score: null,
            error: tooBig
              ? `File too large (${(f.size / (1024 * 1024)).toFixed(1)} MB). Maximum is ${MAX_UPLOAD_MB} MB.`
              : null,
            file: f,
            viaZip: false,
          })
        }
        const overflow = incoming.length - added.length - skippedDup
        const notes: string[] = []
        if (overflow > 0) {
          notes.push(
            `Only ${room} more file${room === 1 ? '' : 's'} fit (max ${MAX_BULK_FILES}).`,
          )
        }
        if (skippedDup) {
          notes.push(`${skippedDup} duplicate${skippedDup === 1 ? '' : 's'} skipped.`)
        }
        setBulkNote(notes.length ? notes.join(' ') : null)
        return base.concat(added)
      })
    },
    [running],
  )

  const removeQueued = useCallback(
    (key: string) => {
      if (running) return
      setJobs((prev) => prev.filter((j) => j.key !== key))
    },
    [running],
  )

  const startImport = useCallback(async () => {
    if (running) return
    const work = jobs.filter((j) => j.status === 'queued' && j.file)
    if (!work.length) {
      setUploadError(`Add at least one file within the ${MAX_UPLOAD_MB} MB limit.`)
      return
    }
    const viaZip = work.length > 1
    setUploadError(null)
    setScoreAnimate(false)
    setShowReport(false)
    setJobs((prev) =>
      prev.map((j) => (work.some((w) => w.key === j.key) ? { ...j, viaZip } : j)),
    )
    setRunning(true)

    let lastOkId: number | null = null
    let lastAudit: Record<string, unknown> | null = null

    try {
      if (!viaZip) {
        const job = work[0]
        patchJob(job.key, { status: 'uploading', error: null })
        try {
          const callId = await uploadOneFile(job.file as File)
          patchJob(job.key, { status: 'auditing', callId })
          const auditJson = await fetchAuditJson(callId)
          patchJob(job.key, {
            status: 'done',
            callId,
            score: asScore(auditJson),
          })
          lastOkId = callId
          lastAudit = auditJson
        } catch (e) {
          patchJob(job.key, {
            status: 'failed',
            error: e instanceof Error ? e.message : 'Import failed',
          })
        }
      } else {
        try {
          await getHearFfmpeg()
          const ready: Array<{ job: BulkJob; file: File }> = []
          for (let i = 0; i < work.length; i++) {
            const job = work[i]
            patchJob(job.key, { status: 'transcoding', error: null })
            try {
              const wav = await transcodeHearCopy(job.file as File, i)
              ready.push({ job, file: wav })
              patchJob(job.key, { status: 'uploading' })
            } catch (e) {
              patchJob(job.key, {
                status: 'failed',
                error: e instanceof Error ? e.message : 'Hear transcode failed',
              })
            }
          }
          if (ready.length === 1) {
            const { job, file } = ready[0]
            const callId = await uploadOneFile(file)
            patchJob(job.key, { status: 'auditing', callId })
            const auditJson = await fetchAuditJson(callId)
            patchJob(job.key, {
              status: 'done',
              callId,
              score: asScore(auditJson),
            })
            lastOkId = callId
            lastAudit = auditJson
          } else if (ready.length >= 2) {
            ready.forEach(({ job }) => patchJob(job.key, { status: 'uploading' }))
            const applyBatchRows = (rows: BatchCallRow[]) => {
              ready.forEach((item, i) => {
                const row = rows[i]
                if (!row) return
                if (row.status === 'ok' && row.call_id != null) {
                  patchJob(item.job.key, {
                    status: 'done',
                    callId: row.call_id,
                    score: row.score ?? null,
                    error: null,
                  })
                  lastOkId = row.call_id
                  return
                }
                if (row.status === 'error') {
                  patchJob(item.job.key, {
                    status: 'failed',
                    error: row.error || 'Import failed',
                    callId: row.call_id != null ? row.call_id : null,
                  })
                  return
                }
                if (row.status === 'auditing') {
                  patchJob(item.job.key, {
                    status: 'auditing',
                    callId: row.call_id != null ? row.call_id : null,
                  })
                  return
                }
                if (row.status === 'transcribing') {
                  patchJob(item.job.key, { status: 'uploading' })
                }
              })
            }
            const rows = await uploadBatchZip(
              ready.map((entry) => entry.file),
              applyBatchRows,
            )
            applyBatchRows(rows)
          }
        } catch (e) {
          const msg = e instanceof Error ? e.message : 'Import failed'
          setUploadError(msg)
          setJobs((prev) =>
            prev.map((j) => {
              if (!work.some((w) => w.key === j.key) || j.status === 'done') return j
              const finishedAt = Date.now()
              return {
                ...j,
                status: 'failed' as const,
                error: j.error || msg,
                finishedAt,
                elapsedMs: j.startedAt ? finishedAt - j.startedAt : j.elapsedMs,
              }
            }),
          )
        }
      }

      await refreshCalls()
      if (lastOkId != null) {
        if (!lastAudit) {
          try {
            lastAudit = await fetchAuditJson(lastOkId)
          } catch {
            lastAudit = null
          }
        }
        if (lastAudit) applyAudit(lastAudit)
      }
    } finally {
      setRunning(false)
    }
  }, [applyAudit, jobs, patchJob, refreshCalls, running])

  const selectCall = useCallback(async (id: number) => {
    setUploadError(null)
    try {
      const json = await fetchAuditJson(id)
      applyAudit(json)
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : 'Could not load this call.')
    }
  }, [applyAudit])

  const flagCurrent = useCallback(async () => {
    const id = report.numericCallId
    if (id == null || report.flagged) return
    const r = await fetch(`${API}/api/calls/${id}/flag`, { method: 'POST' })
    if (!r.ok) throw new Error(await readError(r, 'Could not flag this call.'))
    setReport((prev) => ({ ...prev, flagged: true, gateFailed: true }))
    await refreshCalls()
  }, [refreshCalls, report.flagged, report.numericCallId])

  const loadFeedback = useCallback(async () => {
    const id = report.numericCallId
    if (id == null || feedbackLoading) return
    setFeedbackLoading(true)
    try {
      const r = await fetch(`${API}/api/calls/${id}/feedback`, { method: 'POST' })
      if (!r.ok) throw new Error(await readError(r, 'Could not load feedback.'))
      const data = (await r.json()) as { feedback?: unknown }
      setRawAudit((prev) => {
        const next = { ...(prev || {}), feedback: data.feedback }
        setReport(mapAudit(next))
        return next
      })
    } finally {
      setFeedbackLoading(false)
    }
  }, [feedbackLoading, report.numericCallId])

  const clearCache = useCallback(async () => {
    if (running) return
    const ok = window.confirm(
      'Clear cache? This deletes all stored transcripts, scorecards, and playback audio. You will need to upload recordings again.',
    )
    if (!ok) return
    const r = await fetch(`${API}/api/cache/clear`, { method: 'POST' })
    if (!r.ok) throw new Error(await readError(r, 'Could not clear cache.'))
    setCalls([])
    setJobs([])
    setRawAudit(null)
    setReport(emptyReport())
    setShowReport(false)
    setScoreAnimate(false)
    setBulkNote(null)
    setUploadError(null)
  }, [running])

  const exportScorecard = useCallback(async () => {
    if (running) return
    const r = await fetch(`${API}/api/calls/export-scorecard`)
    if (!r.ok) throw new Error(await readError(r, 'Could not export scorecard.'))
    const blob = await r.blob()
    let name = 'callproof-scorecard.xls'
    const cd = r.headers.get('Content-Disposition') || ''
    const m = /filename="([^"]+)"/.exec(cd)
    if (m) name = m[1]
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = name
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }, [running])

  const onSeek = useCallback((seconds: number) => {
    setShowReport(true)
    setSeekTo(seconds)
  }, [])

  const onSeekHandled = useCallback(() => setSeekTo(null), [])

  const pipe = statusesForJobs(jobs, running)
  const flaggedCount = calls.filter((c) => c.flagged && !c.review_solved).length

  const value = useMemo(
    () => ({
      report,
      rawAudit,
      statuses: pipe.statuses,
      activeStep: running ? pipe.active : pipe.active,
      running,
      showReport,
      scoreAnimate,
      seekTo,
      jobs,
      bulkNote,
      uploadError,
      calls,
      flaggedCount,
      queueFiles,
      removeQueued,
      startImport,
      selectCall,
      flagCurrent,
      loadFeedback,
      feedbackLoading,
      clearCache,
      exportScorecard,
      onSeek,
      onSeekHandled,
    }),
    [
      report,
      rawAudit,
      pipe.statuses,
      pipe.active,
      running,
      showReport,
      scoreAnimate,
      seekTo,
      jobs,
      bulkNote,
      uploadError,
      calls,
      flaggedCount,
      queueFiles,
      removeQueued,
      startImport,
      selectCall,
      flagCurrent,
      loadFeedback,
      feedbackLoading,
      clearCache,
      exportScorecard,
      onSeek,
      onSeekHandled,
    ],
  )

  return <AuditContext.Provider value={value}>{children}</AuditContext.Provider>
}

function asScore(audit: Record<string, unknown>): number | null {
  const n = Number(audit.score)
  return Number.isFinite(n) ? n : null
}

export function useAudit() {
  const ctx = useContext(AuditContext)
  if (!ctx) throw new Error('useAudit must be used within AuditProvider')
  return ctx
}

export { MAX_UPLOAD_MB }
