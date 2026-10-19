import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setAuthToken } from './client'
import { flush, track, trackLanding, trackShareView } from './track'

const lastBody = (fetchMock: ReturnType<typeof vi.fn>): unknown[] =>
    JSON.parse(fetchMock.mock.calls.at(-1)![1].body)

describe('track', () => {
    let fetchMock: ReturnType<typeof vi.fn>

    beforeEach(() => {
        vi.useFakeTimers()
        setAuthToken('tok123')
        fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) })
        vi.stubGlobal('fetch', fetchMock)
    })

    afterEach(() => {
        flush()
        fetchMock.mockClear()
        setAuthToken(null)
        vi.unstubAllGlobals()
        vi.useRealTimers()
    })

    it('batches queued events into one flush on the interval', () => {
        track('page_view', { path: '/' })
        track('create_opened')
        expect(fetchMock).not.toHaveBeenCalled()

        vi.advanceTimersByTime(10_000)

        expect(fetchMock).toHaveBeenCalledTimes(1)
        const [url, init] = fetchMock.mock.calls[0]
        expect(url).toBe('/api/events')
        expect(init.keepalive).toBe(true)
        expect(init.headers.Authorization).toBe('Bearer tok123')
        const rows = lastBody(fetchMock) as { kind: string; ts: number }[]
        expect(rows.map(r => r.kind)).toEqual(['page_view', 'create_opened'])
        expect(rows[0].ts).toBeTypeOf('number')
    })

    it('lifts run_id out of the payload onto the row', () => {
        track('game_played', { run_id: 'r1', mode: 'embed' })
        flush()
        expect(lastBody(fetchMock)).toEqual([
            expect.objectContaining({ kind: 'game_played', run_id: 'r1', payload: { mode: 'embed' } }),
        ])
    })

    it('flushes on pagehide', () => {
        track('page_view', { path: '/new' })
        window.dispatchEvent(new Event('pagehide'))
        expect(fetchMock).toHaveBeenCalledTimes(1)
    })

    it('sends nothing when the queue is empty', () => {
        flush()
        expect(fetchMock).not.toHaveBeenCalled()
    })

    it('drops events silently when signed out', () => {
        setAuthToken(null)
        track('page_view', { path: '/' })
        flush()
        expect(fetchMock).not.toHaveBeenCalled()
    })

    it('swallows network failure — nothing escapes to the app', async () => {
        fetchMock.mockRejectedValue(new Error('offline'))
        track('page_view', { path: '/' })
        expect(() => flush()).not.toThrow()
        await vi.runAllTimersAsync()   // let the rejection settle; an unhandled one fails the run
    })

    it('does not re-send already-flushed events', () => {
        track('page_view', { path: '/' })
        flush()
        track('change_sent', { run_id: 'r1' })
        flush()
        expect(fetchMock).toHaveBeenCalledTimes(2)
        expect((lastBody(fetchMock) as { kind: string }[]).map(r => r.kind)).toEqual(['change_sent'])
    })

    it('posts landing and share views at once, without a token, carrying the referrer', () => {
        setAuthToken(null)
        trackLanding()
        trackShareView('abc_123-XY')
        expect(fetchMock.mock.calls.map(c => c[0]))
            .toEqual(['/api/events/landing', '/api/shares/abc_123-XY/view'])
        for (const [, init] of fetchMock.mock.calls) {
            expect(init.headers.Authorization).toBeUndefined()
            expect(JSON.parse(init.body)).toEqual({ referrer: document.referrer })
        }
    })
})
