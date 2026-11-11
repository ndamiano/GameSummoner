import React, { useEffect, useRef, useState } from 'react'
import { api } from '../../api/client'
import { Link } from '../../router'
import type { Demo } from '../../types'
import { Button } from '../ui/Button'
import { SectionLabel } from '../ui/Field'
import { Sigil } from '../ui/Sigil'
import Footer from './Footer'
import { SummonBox } from './SummonBox'
import { useAuth } from '../../contexts/AuthContext'

const DemoGame: React.FC<{ runId: string }> = ({ runId }) => {
    const [demo, setDemo] = useState<Demo | null>(null)
    const [missing, setMissing] = useState(false)
    const [session, setSession] = useState<string | null>(null)
    const [busy, setBusy] = useState(false)
    const stageRef = useRef<HTMLDivElement>(null)
    const { token } = useAuth()

    useEffect(() => {
        api.getDemo(runId).then(d => { setDemo(d); document.title = `${d.title} · GameSummoner` })
            .catch(() => setMissing(true))
    }, [runId])

    const play = async () => {
        setBusy(true)
        stageRef.current?.requestFullscreen?.()?.catch(() => {})
        try { setSession((await api.demoPlaySession(runId)).url) }
        catch { setMissing(true) }
        finally { setBusy(false) }
    }

    return (
        <div className="min-h-screen bg-ink flex flex-col">
            <header className="border-b border-edge bg-sunken px-6 py-3 flex items-center justify-between">
                <Link to="/" className="font-display text-lg tracking-wide flex items-center gap-2.5">
                    <span className="w-[18px] h-[18px] rotate-45 border-[1.5px] border-ember relative
                                     after:absolute after:inset-[3px] after:bg-ember after:opacity-50" />
                    GameSummoner
                </Link>
                <Link to={token ? '/new' : '/signup'} className="text-sm text-slate hover:text-bone transition-colors">
                    Make your own
                </Link>
            </header>

            <main className="flex-1 max-w-5xl w-full mx-auto px-4 md:px-6 py-10 flex flex-col gap-6">
                {missing ? (
                    <p className="text-center text-slate py-20">This demo isn’t here anymore.</p>
                ) : (
                    <>
                        <h1 className="font-display text-3xl text-center">{demo?.title ?? ''}</h1>
                        <div ref={stageRef}
                            className="relative aspect-video rounded-md border border-edge bg-sunken overflow-hidden grid place-items-center">
                            {session ? (
                                <iframe src={session} title={demo?.title ?? 'the game'}
                                    allow="fullscreen; gamepad; autoplay"
                                    className="absolute inset-0 w-full h-full border-0" />
                            ) : (
                                <>
                                    {demo?.thumb_url
                                        ? <img src={demo.thumb_url} alt=""
                                            className="absolute inset-0 w-full h-full object-cover opacity-70" />
                                        : <div className="absolute inset-0 opacity-60"><Sigil seed={runId} /></div>}
                                    <Button variant="primary" size="lg" onClick={play} disabled={busy || !demo}
                                        className="relative">
                                        ▶ Play
                                    </Button>
                                </>
                            )}
                        </div>
                        {demo && (
                            <div className="flex flex-col gap-1.5">
                                <SectionLabel>The words that summoned it</SectionLabel>
                                <p className="text-sm text-slate leading-relaxed">{demo.prompt}</p>
                            </div>
                        )}
                    </>
                )}
                <section className="text-center flex flex-col items-center gap-4 pt-6">
                    <p className="text-slate max-w-xl">
                        Made with GameSummoner: describe a game in a sentence and it becomes a real,
                        playable browser game.
                    </p>
                    <SummonBox heading="What would you summon?" />
                </section>
            </main>

            <Footer />
        </div>
    )
}

export default DemoGame
