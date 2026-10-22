import React, { useEffect, useState } from 'react'
import { api } from '../../api/client'
import type { Game } from '../../types'
import { Pill } from '../ui/Pill'
import { Sigil } from '../ui/Sigil'

// A game's own first rendered image, as its cover. Roughly half of all builds render no art at
// all, and the manifest is the only place that says which — so a card asks, and falls back to its
// sigil when the answer is none.
const useCover = (runId: string, built: boolean): string | null => {
    const [url, setUrl] = useState<string | null>(null)

    useEffect(() => {
        if (!built) { setUrl(null); return }
        let live = true
        let made: string | null = null
        api.getGameAssets(runId)
            .then(assets => {
                const cover = assets.find(a => a.kind !== 'mesh' && a.status === 'ready')
                if (!cover || !live) return null
                return api.getAssetBlobUrl(runId, cover.id)
            })
            .then(u => {
                if (!u) return
                if (live) { made = u; setUrl(u) } else URL.revokeObjectURL(u)
            })
            .catch(() => { /* no cover is a normal outcome, not an error to report */ })
        return () => { live = false; if (made) URL.revokeObjectURL(made) }
    }, [runId, built])

    return url
}

export const GameCard: React.FC<{ game: Game; onOpen: () => void }> = ({ game, onOpen }) => {
    const cover = useCover(game.run_id, game.built)
    const title = game.title || game.run_id

    return (
        <button onClick={onOpen}
            className="group text-left bg-panel border border-edge rounded-md overflow-hidden
                       hover:border-ember/50 transition-colors flex flex-col">
            <div className="relative aspect-[4/3] bg-sunken border-b border-edge overflow-hidden">
                {cover
                    ? <img src={cover} alt="" className="w-full h-full object-cover" />
                    : <Sigil seed={game.run_id} />}
                {game.built && (
                    <span className="absolute bottom-2 right-2 bg-ember text-ember-ink text-xs font-bold
                                     rounded px-2 py-1 opacity-90 group-hover:opacity-100">
                        ▶ Play
                    </span>
                )}
            </div>
            <div className="px-3 py-2.5 flex flex-col gap-1.5">
                <span className="text-sm font-semibold leading-snug line-clamp-2">{title}</span>
                {game.status === 'held'
                    ? <Pill label="unavailable" tone="idle" />
                    : game.paused
                        ? <Pill label="paused" tone="idle" />
                        : game.building
                        ? <Pill label="summoning" tone="wait" />
                        : game.built
                            ? <Pill label="built" tone="live" />
                            : <Pill label="not built" tone="idle" />}
            </div>
        </button>
    )
}
