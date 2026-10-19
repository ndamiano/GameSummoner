import React, { useCallback, useEffect, useState } from 'react'
import { track } from '../../api/track'
import { api, type CreditPackage, type PurchaseRow } from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import { readDraft } from '../../api/draft'
import { Link } from '../../router'
import { Button } from '../ui/Button'
import { SectionLabel } from '../ui/Field'
import { Pill } from '../ui/Pill'

// Money stays exact and bland: whole dollars without cents, otherwise two decimals.
export const usd = (cents: number): string =>
    cents % 100 === 0 ? `$${cents / 100}` : `$${(cents / 100).toFixed(2)}`

const when = (epochSeconds: number): string =>
    new Date(epochSeconds * 1000).toLocaleDateString(undefined,
        { year: 'numeric', month: 'short', day: 'numeric' })

const CreditsPage: React.FC = () => {
    const { balance, refreshBalance } = useAuth()
    const [packages, setPackages] = useState<CreditPackage[]>([])
    const [history, setHistory] = useState<PurchaseRow[]>([])
    const [buying, setBuying] = useState<string | null>(null)
    const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null)
    const idea = readDraft()

    useEffect(() => {
        api.listPackages().then(r => setPackages(r.packages)).catch(() => {})
        api.listPurchases().then(setHistory).catch(() => {})

        // Back from the provider's payment page: the result params say which purchase and how
        // it went. Completion re-asks the provider server-side, so a hand-typed success URL
        // grants nothing.
        const params = new URLSearchParams(window.location.search)
        const purchase = params.get('purchase')
        if (!purchase) return
        window.history.replaceState(null, '', window.location.pathname)
        if (params.get('result') !== 'success') {
            setMessage({ text: 'The purchase was cancelled.', ok: false })
            return
        }
        api.completePurchase(purchase)
            .then(async done => {
                await refreshBalance()
                setHistory(await api.listPurchases())
                setMessage({ text: `${done.credits} credit${done.credits === 1 ? '' : 's'} added.`, ok: true })
            })
            .catch(e => setMessage({
                text: e instanceof Error ? e.message : 'The purchase did not complete.', ok: false }))
    }, [refreshBalance])

    useEffect(() => { track('credits_opened') }, [])

    const buy = useCallback(async (pkg: CreditPackage) => {
        setBuying(pkg.id); setMessage(null)
        track('purchase_started', { package_id: pkg.id })
        try {
            const { checkout_url } = await api.startPurchase(pkg.id)
            window.location.assign(checkout_url)
        } catch (e) {
            setMessage({ text: e instanceof Error ? e.message : 'The purchase could not start.', ok: false })
            setBuying(null)
        }
    }, [])

    return (
        <div className="h-full overflow-y-auto">
            <div className="max-w-xl mx-auto px-6 py-8 flex flex-col gap-8">
                <div className="flex items-baseline justify-between">
                    <h2 className="font-display text-2xl">Credits</h2>
                    <span className="flex items-center gap-2 font-mono text-sm">
                        <span className="w-2 h-2 rounded-full bg-mana" />
                        {balance ?? '—'} <span className="text-slate">credits</span>
                    </span>
                </div>

                {idea && (
                    <div className="bg-panel border border-edge rounded-md p-4 flex items-center justify-between gap-3 flex-wrap">
                        <p className="text-sm text-slate min-w-0">
                            Your idea is waiting: <span className="italic text-bone">“{idea.length > 80 ? `${idea.slice(0, 80)}…` : idea}”</span>
                        </p>
                        <Link to="/new"
                            className={`rounded font-bold text-sm px-4 py-2 transition-colors ${(balance ?? 0) > 0
                                ? 'bg-ember text-ember-ink hover:bg-ember/90' : 'border border-edge text-slate hover:text-bone'}`}>
                            Back to it →
                        </Link>
                    </div>
                )}

                <section className="flex flex-col gap-3">
                    <SectionLabel>Buy credits</SectionLabel>
                    <p className="text-sm text-slate">One credit summons one game build.</p>
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                        {packages.map(pkg => (
                            <div key={pkg.id}
                                className="border border-edge rounded-md bg-sunken px-4 py-4
                                           flex flex-col items-center gap-2">
                                <span className="flex items-center gap-2 font-display text-xl">
                                    <span className="w-2 h-2 rounded-full bg-mana" />
                                    {pkg.credits}
                                </span>
                                <span className="text-xs text-slate">
                                    {pkg.credits === 1 ? 'credit' : 'credits'}
                                </span>
                                <span className="font-mono text-sm text-bone">{usd(pkg.usd_cents)}</span>
                                <Button variant="primary" size="sm" disabled={buying !== null}
                                    onClick={() => buy(pkg)}>
                                    {buying === pkg.id ? 'Buying…' : 'Buy'}
                                </Button>
                            </div>
                        ))}
                    </div>
                    {message && (
                        <span className={`text-sm ${message.ok ? 'text-live' : 'text-fail'}`}>
                            {message.text}
                        </span>
                    )}
                </section>

                <section className="flex flex-col gap-3">
                    <SectionLabel>Purchase history</SectionLabel>
                    {history.length === 0 ? (
                        <p className="text-sm text-dim">No purchases yet.</p>
                    ) : (
                        <div className="border border-edge rounded-md bg-sunken divide-y divide-edge">
                            {history.map(row => (
                                <div key={row.id}
                                    className="px-4 py-2.5 flex items-baseline justify-between gap-4 text-sm">
                                    <span className="text-slate">{when(row.created_at)}</span>
                                    <span className="flex items-baseline gap-3">
                                        <span className="text-bone">
                                            {row.credits} {row.credits === 1 ? 'credit' : 'credits'}
                                        </span>
                                        <span className="font-mono text-slate">{usd(row.usd_cents)}</span>
                                        {row.status !== 'completed' && <Pill label={row.status} tone="wait" />}
                                    </span>
                                </div>
                            ))}
                        </div>
                    )}
                </section>
            </div>
        </div>
    )
}

export default CreditsPage
