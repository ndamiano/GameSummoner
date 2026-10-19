import React, { useEffect } from 'react'
import DemoGallery from './DemoGallery'
import Footer from './Footer'
import { SummonBox } from './SummonBox'
import { Link } from '../../router'
import { trackLanding } from '../../api/track'

const Landing: React.FC = () => {
    useEffect(() => { trackLanding() }, [])
    return (
        <div className="min-h-screen bg-ink">
            <header className="border-b border-edge bg-sunken px-6 py-3 flex items-center justify-between">
                <span className="font-display text-lg tracking-wide flex items-center gap-2.5">
                    <span className="w-[18px] h-[18px] rotate-45 border-[1.5px] border-ember relative
                                     after:absolute after:inset-[3px] after:bg-ember after:opacity-50" />
                    GameSummoner
                </span>
                <Link to="/login" className="text-sm text-slate hover:text-bone transition-colors">
                    Sign in
                </Link>
            </header>

            <main className="max-w-5xl mx-auto px-6 py-14 flex flex-col gap-14">
                <section className="text-center flex flex-col items-center gap-4">
                    <h1 className="font-display text-4xl md:text-5xl leading-tight max-w-2xl">
                        Describe a game and GameSummoner summons it into reality.
                    </h1>
                    <p className="text-slate max-w-xl leading-relaxed">
                        The game you imagine — a sentence, a paragraph, however you'd say it to a
                        friend — becomes a real, playable browser game. Art and all.
                    </p>
                </section>

                <section className="flex justify-center">
                    <SummonBox heading="What should exist?" />
                </section>

                <DemoGallery />
            </main>

            <Footer />
        </div>
    )
}

export default Landing
