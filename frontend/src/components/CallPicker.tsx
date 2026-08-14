import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { capFirst, formatTime } from '../lib/format'
import type { CallListItem } from '../types'

interface CallPickerProps {
  calls: CallListItem[]
  value: number | null
  disabled?: boolean
  onChange: (id: number) => void
  placeholder?: string
  /** Score by default. Churn Risk shows the rating and sorts high → low. */
  chip?: 'score' | 'churn'
}

function shortName(name: string, max = 22) {
  const n = capFirst((name || '').trim())
  if (n.length <= max) return n
  return `${n.slice(0, max - 1)}…`
}

function scoreTone(score: number | null): 'good' | 'mid' | 'low' | 'none' {
  if (score == null) return 'none'
  if (score >= 80) return 'good'
  if (score >= 60) return 'mid'
  return 'low'
}

const CHURN_SORT: Record<string, number> = {
  high: 0,
  medium: 1,
  low: 2,
  none: 3,
}

function churnRank(risk: string | null | undefined): number {
  return CHURN_SORT[String(risk || '').toLowerCase()] ?? 9
}

function chipFor(
  call: CallListItem,
  mode: 'score' | 'churn',
): { text: string; className: string } | null {
  if (mode === 'churn') {
    const risk = String(call.churn_risk || '').toLowerCase()
    if (!risk) return null
    const tone =
      risk === 'high' || risk === 'medium' || risk === 'low' ? risk : 'none'
    return {
      text: capFirst(risk),
      className: `call-picker-chip is-label is-churn-${tone}`,
    }
  }
  if (call.score == null) return null
  return {
    text: String(call.score),
    className: `call-picker-chip is-${scoreTone(call.score)}`,
  }
}

export function CallPicker({
  calls,
  value,
  disabled = false,
  onChange,
  placeholder = 'Select a call…',
  chip = 'score',
}: CallPickerProps) {
  const uid = useId()
  const rootRef = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)

  const ordered = useMemo(() => {
    if (chip !== 'churn') return calls
    return [...calls].sort((a, b) => {
      const rank = churnRank(a.churn_risk) - churnRank(b.churn_risk)
      if (rank !== 0) return rank
      return b.id - a.id
    })
  }, [calls, chip])

  const selected = useMemo(
    () => ordered.find((c) => c.id === value) ?? null,
    [ordered, value],
  )
  const selectedChip = selected ? chipFor(selected, chip) : null

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (!calls.length) return null

  const triggerLabel = selected
    ? `#${selected.id} · ${shortName(selected.filename)}`
    : placeholder

  return (
    <div className={['call-picker', open ? 'is-open' : ''].join(' ')} ref={rootRef}>
      <span className="call-picker-label" id={`${uid}-label`}>
        Call
      </span>
      <button
        type="button"
        className="call-picker-trigger"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-labelledby={`${uid}-label`}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="call-picker-trigger-text" title={selected?.filename || placeholder}>
          {triggerLabel}
        </span>
        {selectedChip && (
          <span className={selectedChip.className}>{selectedChip.text}</span>
        )}
        <span className="call-picker-caret" aria-hidden="true">
          ▾
        </span>
      </button>

      {open && (
        <ul
          className="call-picker-menu"
          role="listbox"
          aria-labelledby={`${uid}-label`}
        >
          {ordered.map((c) => {
            const active = c.id === value
            const rowChip = chipFor(c, chip)
            return (
              <li key={c.id} role="presentation">
                <button
                  type="button"
                  role="option"
                  aria-selected={active}
                  className={['call-picker-option', active ? 'is-active' : '']
                    .filter(Boolean)
                    .join(' ')}
                  onClick={() => {
                    setOpen(false)
                    if (c.id !== value) onChange(c.id)
                  }}
                >
                  <span className="call-picker-option-main">
                    <span className="call-picker-option-id">#{c.id}</span>
                    <span className="call-picker-option-name" title={c.filename}>
                      {shortName(c.filename, 24)}
                    </span>
                  </span>
                  <span className="call-picker-option-meta">
                    {c.audio_seconds != null ? formatTime(c.audio_seconds) : '—'}
                    {rowChip && (
                      <span className={rowChip.className}>{rowChip.text}</span>
                    )}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
