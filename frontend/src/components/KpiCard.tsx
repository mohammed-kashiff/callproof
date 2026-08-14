interface KpiCardProps {
  label: string
  value: string
  hint?: string
  tone?: 'default' | 'good' | 'warn' | 'bad'
  fill?: boolean
}

export function KpiCard({ label, value, hint, tone = 'default', fill = false }: KpiCardProps) {
  return (
    <article className={['kpi-card', `tone-${tone}`, fill ? 'is-fill' : ''].filter(Boolean).join(' ')}>
      <p className="kpi-label">{label}</p>
      <p className="kpi-value">{value}</p>
      {hint ? <p className="kpi-hint">{hint}</p> : null}
    </article>
  )
}
