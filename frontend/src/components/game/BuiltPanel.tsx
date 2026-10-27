import React from 'react'
import type { GameAsset, GameDetail } from '../../types'
import { Button } from '../ui/Button'
import { Meter } from '../ui/Meter'
import { Sigil } from '../ui/Sigil'
import { AssetGallery } from './AssetGallery'
import { FixNote } from './FixNote'
import { PromptBox } from './PromptBox'
import { ShareLink } from './ShareLink'
import { useAssetBlob } from './useAssets'

// The staged game itself is the largest thing on the screen. Before Play it shows its own first
// image (or its sigil, when the build rendered no art); Play swaps in the game, live in an
// iframe, so the console reporter's messages land on this page.
const Stage: React.FC<{
    runId: string
    cover: GameAsset | null
    version: number
    sessionUrl: string | null
    onPlay: () => void
}> = ({ runId, cover, version, sessionUrl, onPlay }) => {
    const url = useAssetBlob(runId, cover, version)
    const frameRef = React.useRef<HTMLIFrameElement>(null)

    return (
        <div className="relative aspect-video rounded-md border border-edge bg-sunken overflow-hidden grid place-items-center">
            {sessionUrl ? (
                <>
                    <iframe ref={frameRef} src={sessionUrl} title="the game"
                        allow="fullscreen; gamepad; autoplay"
                        className="absolute inset-0 w-full h-full border-0" />
                    <button onClick={() => frameRef.current?.requestFullscreen()}
                        title="fullscreen"
                        className="absolute top-2 right-2 px-2 py-1 rounded bg-ink/70 text-bone/80
                                   hover:text-bone text-sm transition-colors">
                        ⛶
                    </button>
                </>
            ) : (
                <>
                    {url
                        ? <img src={url} alt="" className="absolute inset-0 w-full h-full object-cover opacity-70" />
                        : <div className="absolute inset-0"><Sigil seed={runId} /></div>}
                    <Button variant="primary" size="lg" onClick={onPlay}
                        className="relative shadow-[0_10px_40px_-12px_rgb(var(--c-ember)/0.9)]">
                        ▶ Play
                    </Button>
                </>
            )}
        </div>
    )
}

export const BuiltPanel: React.FC<{
    runId: string
    detail: GameDetail
    assets: GameAsset[] | null
    assetsVersion: number
    budget: number | null
    rendering: boolean
    acting: boolean
    onRender: () => void
    note: string
    setNote: (v: string) => void
    onFix: () => void
    promptText: string
    setPromptText: (v: string) => void
    sessionUrl: string | null
    onPlay: () => void
    onOpenTab: () => void
    errorCount: number
    onShowErrors: () => void
}> = ({
    runId, detail, assets, assetsVersion, budget, rendering, acting, onRender,
    note, setNote, onFix, promptText, setPromptText,
    sessionUrl, onPlay, onOpenTab, errorCount, onShowErrors,
}) => {
    const cover = assets?.find(a => a.kind !== 'mesh' && a.status === 'ready') ?? null

    return (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
            <div className="flex flex-col gap-6 min-w-0">
                <div className="flex flex-col gap-2">
                    <Stage runId={runId} cover={cover} version={assetsVersion}
                        sessionUrl={sessionUrl} onPlay={onPlay} />
                    <div className="flex items-center justify-between gap-3 flex-wrap">
                        {errorCount > 0 ? (
                            <button onClick={onShowErrors}
                                className="text-sm text-wait hover:text-bone transition-colors">
                                ⚠ The game hit {errorCount === 1 ? 'a console error' : `${errorCount} console errors`} — summon a mending?
                            </button>
                        ) : <span />}
                        <div className="flex items-center gap-3">
                            <button onClick={onOpenTab}
                                className="text-xs text-slate hover:text-bone transition-colors">
                                Open in its own tab ↗
                            </button>
                        </div>
                    </div>
                </div>
                <FixNote note={note} setNote={setNote} onSubmit={onFix} busy={acting} />
                <PromptBox ask={detail.ask} prompt={detail.prompt ?? ''} text={promptText} onChange={setPromptText} />
            </div>

            <div className="flex flex-col gap-6 min-w-0">
                <ShareLink runId={runId} initial={detail.share_id} />
                {budget != null && (
                    <section className="flex flex-col gap-2">
                        <Meter value={budget} label="Mana left" />
                    </section>
                )}
                <AssetGallery runId={runId} version={assetsVersion} rendering={rendering}
                    onRender={onRender} acting={acting} />
            </div>
        </div>
    )
}
