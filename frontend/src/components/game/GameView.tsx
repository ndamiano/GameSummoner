import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api, buildErrorMessage } from '../../api/client'
import { track } from '../../api/track'
import { useWebSocket } from '../../contexts/WebSocketContext'
import { useAuth } from '../../contexts/AuthContext'
import { useRunBuildStream } from '../../hooks/useRunBuildStream'
import type { GameDetail, WebSocketMessage } from '../../types'
import { Button } from '../ui/Button'
import { Pill } from '../ui/Pill'
import { AskLine, PromptBox } from './PromptBox'
import { BuiltPanel } from './BuiltPanel'
import { ErrorFixModal } from './ErrorFixModal'
import { WorkingPanel } from './WorkingPanel'
import { useAssets } from './useAssets'
import { useConsoleReports } from './useConsoleReports'
import { budgetFraction, shouldAdoptPrompt, stageFor } from './state'

const STATUS_PILL: Record<string, { label: string; tone: 'live' | 'wait' | 'idle' }> = {
    running: { label: 'summoning', tone: 'wait' },
    fixing: { label: 'mending', tone: 'wait' },
    changing: { label: 'changing', tone: 'wait' },
    paused: { label: 'paused', tone: 'idle' },
    built: { label: 'built', tone: 'live' },
    failed: { label: 'gone awry', tone: 'idle' },
    held: { label: 'unavailable', tone: 'idle' },
}

export const GameView: React.FC<{ runId: string; onChanged: () => void; onBack: () => void }> = ({
    runId, onChanged, onBack,
}) => {
    const { subscribe } = useWebSocket()
    const { refreshBalance } = useAuth()
    const stream = useRunBuildStream(runId)
    const [detail, setDetail] = useState<GameDetail | null>(null)
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [building, setBuilding] = useState(false)
    const [status, setStatus] = useState<string>('idle')
    const [acting, setActing] = useState(false)
    const [renderPending, setRenderPending] = useState(false)
    const [fixNote, setFixNote] = useState('')
    const [promptText, setPromptText] = useState('')
    const serverPrompt = useRef<string | null>(null)
    const [elapsedSec, setElapsedSec] = useState(0)
    // Bumped on assets_done so the manifest and every rendered file are re-read.
    const [assetsVersion, setAssetsVersion] = useState(0)
    const assets = useAssets(runId, assetsVersion)
    // A live play session: the handoff URL in the iframe, and the game origin whose reporter
    // messages we accept. Cleared whenever a build/fix starts — the game under it is changing.
    const [session, setSession] = useState<{ url: string; origin: string } | null>(null)
    const [errorsOpen, setErrorsOpen] = useState(false)
    const { reports, clear: clearReports } = useConsoleReports(
        session ? (session.origin || window.location.origin) : null)

    const closePlay = useCallback(() => {
        setSession(null); setErrorsOpen(false); clearReports()
    }, [clearReports])

    const load = useCallback(() => {
        let cancelled = false
        setLoading(true)
        setError(null)
        api.getGame(runId)
            .then(d => {
                if (cancelled) return
                setDetail(d); setBuilding(d.building); setStatus(d.status)
                // Only adopt the server's text when it actually changed — this poll runs every 10s
                // while building, and re-seeding on each one would wipe an edit mid-keystroke.
                // Compared against the value from BEFORE this response: a functional updater would
                // run after the ref was reassigned and so never see a change.
                if (shouldAdoptPrompt(serverPrompt.current, d.prompt)) setPromptText(d.prompt ?? '')
                serverPrompt.current = d.prompt
            })
            .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load game') })
            .finally(() => { if (!cancelled) setLoading(false) })
        return () => { cancelled = true }
    }, [runId])

    useEffect(() => { setRenderPending(false); closePlay(); return load() }, [load, closePlay])

    useEffect(() => {
        if (!building || stream.startedAt == null) { setElapsedSec(0); return }
        const start = stream.startedAt
        const tick = () => setElapsedSec(Date.now() / 1000 - start)
        tick()
        const id = setInterval(tick, 1000)
        return () => clearInterval(id)
    }, [building, stream.startedAt])

    // While building, re-pull the detail on a slow tick so the compute meter drains — the budget
    // moves on job completions, which no websocket event carries.
    useEffect(() => {
        if (!building) return
        const id = setInterval(load, 10000)
        return () => clearInterval(id)
    }, [building, load])

    useEffect(() => {
        const unsub = subscribe(runId, (msg: WebSocketMessage) => {
            switch (msg.type) {
                case 'prompt_proposed':
                case 'prompt_updated':
                    load(); break
                case 'build_started':
                    setBuilding(true); setStatus('running'); break
                case 'build_paused':
                    setStatus('paused'); break
                case 'build_resumed':
                    setStatus('running'); break
                case 'build_done':
                    setBuilding(false); load(); onChanged(); break
                case 'assets_done':
                    setRenderPending(false); setAssetsVersion(v => v + 1); load(); onChanged(); break
            }
        })
        return unsub
    }, [runId, subscribe, load, onChanged])

    // Once the real render signal is live, drop the optimistic pending flag.
    useEffect(() => { if (stream.skinning) setRenderPending(false) }, [stream.skinning])

    const act = async (fn: () => Promise<unknown>, errMsg: string, reload = true) => {
        setActing(true); setError(null)
        try { await fn(); if (reload) load() }
        catch (e) { setError(e instanceof Error ? `${errMsg}: ${e.message}` : errMsg) }
        finally { setActing(false) }
    }

    // `fresh` empties the game folder first. A retry otherwise opens on the last attempt's files,
    // which the model reads and believes — and then re-asks for art it already has.
    const startBuild = async (fresh: boolean) => {
        setActing(true); setBuilding(true); setStatus('running'); closePlay()
        try {
            await (fresh ? api.regenerateGame : api.buildGame)(runId, promptText)
            track('build_started', { run_id: runId, source: 'game', fresh })
            onChanged()
        }
        catch (e) {
            setBuilding(false); setStatus('idle')
            setError(buildErrorMessage(e, 'Build failed'))
        }
        finally { setActing(false); refreshBalance() }  // a build spends credits — resync the header
    }
    const build = () => startBuild(false)
    const regenerate = () => startBuild(true)
    const pause = () => { setStatus('paused'); act(() => api.pauseGame(runId), 'Pause failed', false) }
    const buff = () => act(() => api.resumeGame(runId), 'Buff failed')
    const resume = () => { setStatus('running'); act(() => api.resumeGame(runId), 'Resume failed', false) }
    const stop = () => act(() => api.stopGame(runId), 'Stop failed', false)
    const renderArt = () => act(async () => { setRenderPending(true); await api.renderAssets(runId) }, 'Render failed', false)
    const sendFix = (note: string, source = 'note') => {
        closePlay()
        act(async () => {
            await api.changeGame(runId, note)
            track('change_sent', { run_id: runId, source, length: note.length })
            setBuilding(true); setStatus('changing')
        }, 'Change failed', false)
    }
    const submitFix = () => {
        const note = fixNote.trim()
        if (!note) return
        setFixNote('')
        sendFix(note)
    }
    // Each mount of the game gets its own single-use handoff URL — a reused one 403s.
    const play = () => act(async () => {
        setSession(await api.playSession(runId))
        track('game_played', { run_id: runId, mode: 'embed' })
    }, 'Could not start the game', false)
    const openTab = () => act(async () => {
        const s = await api.playSession(runId)
        track('game_played', { run_id: runId, mode: 'tab' })
        window.open(s.url, '_blank', 'noopener')
    }, 'Could not start the game', false)

    if (loading && !detail) return <div className="p-8 text-slate text-sm">Loading…</div>
    if (error && !detail) return <div className="p-8 text-fail text-sm">{error}</div>
    if (!detail) return null

    const held = detail.status === 'held'
    const stage = held ? 'held' : stageFor(building, detail.built, detail.prompt == null)
    const awry = !building && !held && detail.status === 'failed'
    const pill = held ? STATUS_PILL.held
        : STATUS_PILL[building ? status : awry ? 'failed' : detail.built ? 'built' : 'idle']
    const budget = budgetFraction(detail.budget_pct_remaining)

    return (
        <div className="h-full overflow-y-auto">
            <div className="max-w-6xl mx-auto px-6 py-6 flex flex-col gap-6">
                <div className="flex items-start justify-between gap-4 flex-wrap">
                    <div className="flex flex-col gap-2 min-w-0">
                        <button onClick={onBack}
                            className="text-xs text-slate hover:text-bone transition-colors self-start">
                            ← All games
                        </button>
                        <div className="flex items-center gap-3 flex-wrap">
                            <h2 className="font-display text-2xl leading-tight">{detail.title || detail.run_id}</h2>
                            {pill && <Pill label={pill.label} tone={pill.tone} />}
                        </div>
                    </div>

                    <div className="flex gap-2 flex-wrap items-center">
                        {building && (status === 'running' || status === 'fixing' || status === 'changing') && (
                            <Button onClick={pause} disabled={acting}>Pause</Button>
                        )}
                        {building && status === 'paused' && (
                            <Button variant="primary" onClick={resume} disabled={acting}>Resume</Button>
                        )}
                        {building && (
                            <Button variant="ghost" onClick={stop} disabled={acting}>Stop and keep it</Button>
                        )}
                        {!building && !held && detail.built && (
                            <Button variant="ghost" onClick={build} disabled={acting}>Rebuild from the request</Button>
                        )}
                        {!building && !held && detail.has_game && (
                            <Button variant="ghost" onClick={regenerate} disabled={acting}>Start over from scratch</Button>
                        )}
                    </div>
                </div>

                {error && <p className="text-fail text-sm">{error}</p>}

                {awry && (
                    <div role="status" className="bg-panel border border-wait/40 rounded-md p-4 flex items-center justify-between gap-4 flex-wrap">
                        <p className="text-sm text-bone">
                            {detail.built
                                ? "The spell seems to have gone awry. You can play what's here, but it may not work properly."
                                : 'The spell fizzled before anything playable appeared.'}
                        </p>
                        {detail.built && (
                            <Button variant="primary" onClick={buff} disabled={acting}>
                                Spend mana to buff the summoning
                            </Button>
                        )}
                    </div>
                )}

                {stage === 'held' && (
                    <div className="max-w-2xl bg-panel border border-edge rounded-md p-6 text-sm text-slate">
                        Something went wrong with this build — we're looking into it.
                    </div>
                )}

                {stage === 'building' && (
                    <WorkingPanel runId={runId} paused={status === 'paused'}
                        step={stream.progress?.step ?? null} summary={stream.progress?.summary ?? null}
                        elapsedSec={elapsedSec} assets={assets} assetsVersion={assetsVersion}
                        budget={budget} prompt={detail.prompt ?? detail.ask} feed={stream.feed} />
                )}

                {stage === 'built' && (
                    <BuiltPanel runId={runId} detail={detail} assets={assets} assetsVersion={assetsVersion}
                        budget={budget} rendering={stream.skinning || renderPending} acting={acting}
                        onRender={renderArt} note={fixNote} setNote={setFixNote} onFix={submitFix}
                        promptText={promptText} setPromptText={setPromptText}
                        sessionUrl={session?.url ?? null} onPlay={play} onOpenTab={openTab}
                        errorCount={reports.length} onShowErrors={() => setErrorsOpen(true)} />
                )}

                {errorsOpen && reports.length > 0 && (
                    <ErrorFixModal reports={reports} busy={acting}
                        onSend={note => sendFix(note, 'errors')} onClose={() => setErrorsOpen(false)} />
                )}

                {stage === 'designing' && (
                    <div className="max-w-2xl flex flex-col gap-4">
                        <AskLine ask={detail.ask} />
                        <div role="status" className="bg-panel border border-edge rounded-md p-6 flex items-center gap-3">
                            <span aria-hidden="true" className="w-2 h-2 rounded-full bg-ember animate-pulse" />
                            <span className="text-sm text-slate">
                                Writing the design from your request — a few minutes. The build starts on its own once it lands.
                            </span>
                        </div>
                    </div>
                )}

                {stage === 'ready' && (
                    <div className="max-w-2xl flex flex-col gap-4">
                        <PromptBox ask={detail.ask} prompt={detail.prompt ?? ''}
                            text={promptText} onChange={setPromptText} />
                        <div className="flex items-center gap-3">
                            <Button variant="primary" size="md" onClick={build} disabled={acting}>
                                Build it
                            </Button>
                            {detail.has_game && (
                                <Button variant="ghost" size="md" onClick={regenerate} disabled={acting}>
                                    Start over from scratch
                                </Button>
                            )}
                        </div>
                    </div>
                )}
            </div>
        </div>
    )
}
