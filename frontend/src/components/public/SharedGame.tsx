import React, { useEffect, useRef, useState } from 'react'
import { api } from '../../api/client'
import { trackShareView } from '../../api/track'
import { Link } from '../../router'
import type { SharedGame as Share } from '../../types'
import { Button } from '../ui/Button'
import { Sigil } from '../ui/Sigil'
import Footer from './Footer'

const SharedGame: React.FC<{ shareId: string }> = ({ shareId }) => {
    const [share, setShare] = useState<Share | null>(null)
    const [missing, setMissing] = useState(false)
    const [session, setSession] = useState<string | null>(null)
    const [busy, setBusy] = useState(false)
    const stageRef = useRef<HTMLDivElement>(null)

    useEffect(() => { trackShareView(shareId) }, [shareId])

    useEffect(() => {
        api.getShare(shareId).then(s => { setShare(s); document.title = `${s.title} · GameSummoner` })
            .catch(() => setMissing(true))
    }, [shareId])

    const play = async () => {
        setBusy(true)
        stageRef.current?.requestFullscreen?.()?.catch(() => {})
        try { setSession((await api.sharePlaySession(shareId)).url) }
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
                <Link to="/login" className="text-sm text-slate hover:text-bone transition-colors">
                    Make your own
                </Link>
            </header>

            <main className="flex-1 max-w-5xl w-full mx-auto px-4 md:px-6 py-10 flex flex-col gap-6">
                {missing ? (
                    <p className="text-center text-slate py-20">This game isn’t shared anymore.</p>
                ) : (
                    <>
                        <h1 className="font-display text-3xl text-center">{share?.title ?? ''}</h1>
                        <div ref={stageRef}
                            className="relative aspect-video rounded-md border border-edge bg-sunken overflow-hidden grid place-items-center">
                            {session ? (
                                <iframe src={session} title={share?.title ?? 'the game'}
                                    allow="fullscreen; gamepad; autoplay"
                                    className="absolute inset-0 w-full h-full border-0" />
                            ) : (
                                <>
                                    {share?.thumb_url
                                        ? <img src={share.thumb_url} alt=""
                                            className="absolute inset-0 w-full h-full object-cover opacity-70" />
                                        : <div className="absolute inset-0 opacity-60"><Sigil seed={shareId} /></div>}
                                    <Button variant="primary" size="lg" onClick={play} disabled={busy || !share}
                                        className="relative">
                                        ▶ Play
                                    </Button>
                                </>
                            )}
                        </div>
                    </>
                )}
                <section className="text-center flex flex-col items-center gap-3 pt-6">
                    <p className="text-slate max-w-xl">
                        Made with GameSummoner: describe a game in a sentence and it becomes a real,
                        playable browser game.
                    </p>
                    <Link to="/"
                        className="rounded bg-ember text-ember-ink hover:bg-ember/90 font-bold text-sm px-4 py-2 transition-colors">
                        Make your own →
                    </Link>
                </section>
            </main>

            <Footer />
        </div>
    )
}

export default SharedGame
