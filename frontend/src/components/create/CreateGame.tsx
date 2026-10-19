import React, { useEffect, useState } from 'react'
import { api, ApiError, buildErrorMessage } from '../../api/client'
import { clearDraft, readDraft, saveDraft } from '../../api/draft'
import { Link } from '../../router'
import { track } from '../../api/track'
import { useAuth } from '../../contexts/AuthContext'
import { Button } from '../ui/Button'
import { TextArea } from '../ui/Field'

// Openings that produce games, shown so an empty box is a starting line rather than a blank.
const EXAMPLES = [
    'A crab running a small bank, approving or denying loans to sea creatures based on their collateral.',
    'Stack increasingly unreasonable objects on a very patient turtle.',
    'A tea shop for forest animals where you have to remember their orders.',
    "You're a firefly; the only control makes you glow, attracting moths and alerting frogs.",
]

const shorten = (s: string, n = 46) => (s.length <= n ? s : `${s.slice(0, n).trimEnd()}…`)

export const CreateGame: React.FC<{ onCreated: (runId: string) => void; onCancel: () => void }> = ({
    onCreated, onCancel,
}) => {
    const [text, setText] = useState(readDraft)
    const [busy, setBusy] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [broke, setBroke] = useState(false)

    const edit = (next: string) => { setText(next); saveDraft(next) }
    const { refreshBalance } = useAuth()

    useEffect(() => { track('create_opened') }, [])

    const create = async () => {
        setBusy(true); setError(null); setBroke(false)
        try {
            const { run_id } = await api.createGame(text)
            clearDraft()
            onCreated(run_id)
        } catch (e) {
            if (e instanceof ApiError && e.status === 402) setBroke(true)
            else setError(buildErrorMessage(e, 'Could not start the design'))
        } finally { setBusy(false); refreshBalance() }
    }

    return (
        <div className="h-full overflow-y-auto">
            <div className="max-w-2xl mx-auto px-6 py-10 flex flex-col gap-5">
                <button onClick={onCancel}
                    className="text-xs text-slate hover:text-bone transition-colors self-start">
                    ← All games
                </button>

                <div className="flex flex-col gap-1.5 text-center">
                    <h2 className="font-display text-3xl">What should exist?</h2>
                    <p className="text-sm text-slate">
                        Your words become a systems design, and the game is built from it.
                    </p>
                </div>

                <TextArea value={text} onChange={e => edit(e.target.value)} disabled={busy}
                    rows={5} autoFocus spellCheck={false}
                    placeholder="A snail racing game where the race runs over four in-game days."
                    className="border-l-2 border-l-ember" />

                <div className="flex items-center gap-3 flex-wrap">
                    <Button variant="primary" size="md" onClick={create} disabled={busy || !text.trim()}>
                        {busy ? 'Starting…' : 'Summon it · 1 credit'}
                    </Button>
                    <span className="text-xs text-dim">
                        One credit covers the design and the build. It takes a while.
                    </span>
                </div>

                {error && <p className="text-fail text-sm">{error}</p>}
                {broke && (
                    <div className="bg-panel border border-edge rounded-md p-4 flex items-center justify-between gap-3 flex-wrap">
                        <p className="text-sm text-bone">You need a credit to summon this. Your idea is kept while you get one.</p>
                        <Link to="/credits"
                            className="rounded bg-ember text-ember-ink hover:bg-ember/90 font-bold text-sm px-4 py-2 transition-colors">
                            Get credits →
                        </Link>
                    </div>
                )}

                <div className="flex flex-col gap-2 mt-2">
                    <span className="text-xs text-dim">Or start from one of these</span>
                    <div className="flex flex-wrap gap-2">
                        {EXAMPLES.map(e => (
                            <button key={e} onClick={() => edit(e)} disabled={busy}
                                className="text-sm text-slate border border-edge rounded-full px-3 py-1
                                           hover:text-bone hover:border-ember/50 transition-colors disabled:opacity-40">
                                {shorten(e)}
                            </button>
                        ))}
                    </div>
                </div>
            </div>
        </div>
    )
}
