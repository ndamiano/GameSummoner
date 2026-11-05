import React, { useEffect, useState } from 'react'
import { api } from '../../api/client'
import { Button } from '../ui/Button'
import { SectionLabel } from '../ui/Field'
import { Sigil } from '../ui/Sigil'
import type { Demo } from '../../types'

const CLAMP_AT = 160

// One demo card: the prompt that summoned it, and the game itself — playable in place. The
// pairing IS the pitch; a video would be a weaker claim than the game running.
const DemoCard: React.FC<{ demo: Demo }> = ({ demo }) => {
    const [session, setSession] = useState<string | null>(null)
    const [busy, setBusy] = useState(false)
    const [expanded, setExpanded] = useState(false)
    const stageRef = React.useRef<HTMLDivElement>(null)
    const clamped = demo.prompt.length > CLAMP_AT && !expanded

    const play = async () => {
        setBusy(true)
        // Fullscreen the stage (not the iframe — it isn't mounted yet), synchronously,
        // while the click's user activation is still valid.
        stageRef.current?.requestFullscreen?.()?.catch(() => {})
        try { setSession((await api.demoPlaySession(demo.run_id)).url) }
        catch { /* listed-but-gone demo: the card just stays un-started */ }
        finally { setBusy(false) }
    }

    return (
        <div className="border border-edge rounded-md bg-panel overflow-hidden flex flex-col">
            <div ref={stageRef} className="relative aspect-video bg-sunken grid place-items-center">
                {session
                    ? (
                        <>
                            <iframe src={session} title={demo.title}
                                allow="fullscreen; gamepad; autoplay"
                                className="absolute inset-0 w-full h-full border-0" />
                            <button onClick={() => stageRef.current?.requestFullscreen?.()?.catch(() => {})}
                                title="fullscreen"
                                className="absolute top-2 right-2 px-2 py-1 rounded bg-ink/70 text-bone/80
                                           hover:text-bone text-sm transition-colors">
                                ⛶
                            </button>
                        </>
                    )
                    : (
                        <>
                            {demo.thumb_url
                                ? <img src={demo.thumb_url} alt=""
                                    className="absolute inset-0 w-full h-full object-cover opacity-80" />
                                : <div className="absolute inset-0 opacity-60"><Sigil seed={demo.run_id} /></div>}
                            <Button variant="primary" size="md" onClick={play} disabled={busy}
                                className="relative">
                                ▶ Play it
                            </Button>
                        </>
                    )}
            </div>
            <div className="px-4 py-3 flex flex-col gap-1.5">
                <SectionLabel>The words that summoned it</SectionLabel>
                <p className="text-sm text-slate leading-relaxed">
                    {clamped ? `${demo.prompt.slice(0, CLAMP_AT).trimEnd()}… ` : `${demo.prompt} `}
                    {demo.prompt.length > CLAMP_AT && (
                        <button onClick={() => setExpanded(e => !e)}
                            className="text-dim underline hover:text-slate transition-colors">
                            {expanded ? 'less' : 'see more'}
                        </button>
                    )}
                </p>
            </div>
        </div>
    )
}

// Shared by the signed-out landing page and the signed-in /demos page.
const DemoGallery: React.FC = () => {
    const [demos, setDemos] = useState<Demo[]>([])

    useEffect(() => { api.listDemos().then(setDemos).catch(() => setDemos([])) }, [])

    return (
        <div className="flex flex-col gap-14">
            {demos.length > 0 && (
                <>
                    <div className="grid gap-6 md:grid-cols-2">
                        {demos.map(d => <DemoCard key={d.run_id} demo={d} />)}
                    </div>
                    <p className="text-xs text-dim text-center">
                        Every one of these was made by the machine from the words shown under it.
                    </p>
                </>
            )}
        </div>
    )
}

export default DemoGallery
