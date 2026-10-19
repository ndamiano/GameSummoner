import React, { useEffect, useState } from 'react'
import { api } from '../../api/client'
import { readDraft, saveDraft } from '../../api/draft'
import { useAuth } from '../../contexts/AuthContext'
import { useRouter } from '../../router'
import { usd } from '../credits/CreditsPage'
import { Button } from '../ui/Button'
import { TextArea } from '../ui/Field'

export const SummonBox: React.FC<{ heading: string }> = ({ heading }) => {
    const { token } = useAuth()
    const { navigate } = useRouter()
    const [text, setText] = useState(readDraft)
    const [perGame, setPerGame] = useState<number | null>(null)

    useEffect(() => {
        api.listPackages()
            .then(r => setPerGame(Math.min(...r.packages.map(p => p.usd_cents / p.credits))))
            .catch(() => setPerGame(null))
    }, [])

    const summon = (e: React.FormEvent) => {
        e.preventDefault()
        saveDraft(text.trim())
        navigate(token ? '/new' : '/signup')
    }

    return (
        <form onSubmit={summon} className="w-full max-w-xl flex flex-col gap-3">
            <h2 className="font-display text-2xl text-center">{heading}</h2>
            <TextArea value={text} onChange={e => setText(e.target.value)} rows={3}
                aria-label="Your game idea" spellCheck={false}
                placeholder="A tea shop for forest animals where you have to remember their orders."
                className="border-l-2 border-l-ember" />
            <div className="flex items-center justify-between gap-3 flex-wrap">
                <span className="text-xs text-dim">
                    {perGame != null && `${usd(perGame)} a game · `}
                    {token ? 'your idea opens in a new game' : 'create an account next — your idea comes with you'}
                </span>
                <Button type="submit" variant="primary" size="md" disabled={!text.trim()}>
                    Summon it →
                </Button>
            </div>
        </form>
    )
}
