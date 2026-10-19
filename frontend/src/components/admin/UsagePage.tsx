import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { AdminAnalytics, HotShare, UsageCounts } from '../../types'
import { Refresh, Stat, fmtStamp, fmtUsd } from './format'

const DAYS = 14
const TOP_REFERRERS = 3

const plays = (c: UsageCounts): number => c.demo_plays + c.share_plays + c.own_plays

const playSplit = (c: UsageCounts): string =>
    `demo: ${c.demo_plays}, share: ${c.share_plays}, own: ${c.own_plays}`

const COLUMNS: { label: string; title: string; value: (c: UsageCounts) => number; hover?: (c: UsageCounts) => string }[] = [
    { label: 'landing', title: 'signed-out landing page loads', value: c => c.landing },
    { label: 'signups', title: 'accounts created', value: c => c.signups },
    { label: 'DAU', title: 'daily active users', value: c => c.active },
    { label: 'plays', title: 'demo + share + own plays — hover a cell for the split', value: plays, hover: playSplit },
    { label: 'share views', title: 'shared-game pages opened (plays are counted under plays)', value: c => c.share_views },
    { label: 'builds', title: 'builds started', value: c => c.builds },
]

const th = 'font-semibold pb-1 px-2 text-center first:text-left'
const td = 'py-0.5 px-2 text-center first:text-left'

const HotList: React.FC<{ shares: HotShare[] }> = ({ shares }) => (
    <table className="w-full text-sm font-mono">
        <thead>
            <tr className="text-slate text-xs">
                <th className={th}>game</th>
                <th className={th}>owner</th>
                <th className={th}>views</th>
                <th className={th}>plays</th>
                <th className={th}>from</th>
                <th className={th}>last</th>
            </tr>
        </thead>
        <tbody className="text-bone">
            {shares.map(s => (
                <tr key={s.run_id}>
                    <td className={td}>
                        {s.share_id
                            ? <a href={`/g/${s.share_id}`} target="_blank" rel="noreferrer"
                                className="hover:text-ember transition-colors">{s.title}</a>
                            : <span className="text-slate" title="no longer shared">{s.title}</span>}
                    </td>
                    <td className={td}>{s.owner ?? '—'}</td>
                    <td className={td}>{s.views}</td>
                    <td className={td}>{s.plays}</td>
                    <td className={`${td} text-slate`}>
                        {s.referrers.slice(0, TOP_REFERRERS).map(([host, n]) => `${host} ${n}`).join(' · ')}
                    </td>
                    <td className={`${td} text-slate`}>{fmtStamp(s.last_at)}</td>
                </tr>
            ))}
        </tbody>
    </table>
)

// Usage is fetched once and on demand, like costs — nobody sizes a fleet off page views.
const UsagePage: React.FC = () => {
    const [data, setData] = useState<AdminAnalytics | null>(null)
    const [busy, setBusy] = useState(false)

    const load = useCallback(async () => {
        setBusy(true)
        try { setData(await api.getAdminAnalytics(DAYS)) }
        catch { /* leave the last snapshot up */ }
        finally { setBusy(false) }
    }, [])

    useEffect(() => { load() }, [load])

    if (!data) return <div className="text-slate text-xs">Loading usage…</div>
    const t = data.totals
    const avgDau = (data.days.reduce((n, d) => n + d.active, 0) / data.days.length).toFixed(1)

    return (
        <div className="space-y-4">
            <div className="bg-ink border border-edge rounded-lg p-4 space-y-4">
                <div className="flex items-baseline justify-between">
                    <div className="text-slate text-xs font-semibold">Usage — last {DAYS} days</div>
                    <Refresh busy={busy} onClick={load} />
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-4 text-center">
                    <Stat label="users" value={`${t.users} (+${t.signups})`} hint="all accounts (new this window)" />
                    <Stat label="avg DAU" value={avgDau} hint="average daily active users" />
                    <Stat label="landing" value={t.landing} />
                    <Stat label="plays" value={plays(t)} hint={playSplit(t)} />
                    <Stat label="share views" value={t.share_views} />
                    <Stat label="builds" value={t.builds} />
                    <Stat label="purchases" value={`${t.purchases} · ${fmtUsd(t.revenue_cents / 100)}`}
                        hint={`completed, refunds excluded — ${t.checkouts} checkouts opened`} />
                </div>
                <div className="overflow-x-auto">
                    <table className="w-full text-sm font-mono">
                        <thead>
                            <tr className="text-slate text-xs">
                                <th className={th}>day</th>
                                {COLUMNS.map(c => <th key={c.label} title={c.title} className={th}>{c.label}</th>)}
                            </tr>
                        </thead>
                        <tbody className="text-bone">
                            {data.days.map(d => (
                                <tr key={d.day}>
                                    <td className={td}>{d.day}</td>
                                    {COLUMNS.map(c => (
                                        <td key={c.label} title={c.hover?.(d)}
                                            className={`${td} ${c.value(d) ? '' : 'text-slate/50'}`}>
                                            {c.value(d)}
                                        </td>
                                    ))}
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>

            <div className="bg-ink border border-edge rounded-lg p-4 space-y-3">
                <div className="text-slate text-xs font-semibold">Shared hot list — by views, last {DAYS} days</div>
                {data.shares.length === 0
                    ? <div className="text-slate text-xs">No shared game has been viewed yet.</div>
                    : <div className="overflow-x-auto"><HotList shares={data.shares} /></div>}
            </div>
        </div>
    )
}

export default UsagePage
