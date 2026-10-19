import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from '../../App'
import LoginScreen from '../LoginScreen'
import { CreateGame } from '../create/CreateGame'
import { AuthProvider } from '../../contexts/AuthContext'
import { WebSocketProvider } from '../../contexts/WebSocketContext'
import { RouterProvider } from '../../router'
import { api, ApiError, setAuthToken } from '../../api/client'
import { readDraft, saveDraft } from '../../api/draft'

const IDEA = 'A bee defending its hive from a hornet that learns your patterns.'

const json = (status: number, body: unknown) => ({
    ok: status < 400, status, headers: { get: () => null }, json: async () => body,
})

beforeEach(() => { setAuthToken(null); sessionStorage.clear() })
afterEach(() => {
    cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals()
    setAuthToken(null); sessionStorage.clear(); window.history.replaceState(null, '', '/')
})

describe('from a shared game to a summoned one', () => {
    it('carries a signed-out visitor’s idea from the share page to the signup form, priced', async () => {
        window.history.replaceState(null, '', '/g/abc123def456')
        vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => Promise.resolve(
            url.endsWith('/api/billing/packages')
                ? json(200, { packages: [{ id: 'one', credits: 1, usd_cents: 500 },
                                         { id: 'ten', credits: 10, usd_cents: 5000 }] })
                : url.endsWith('/api/shares/abc123def456')
                    ? json(200, { share_id: 'abc123def456', title: 'Whisker Wizard', thumb_url: null })
                    : json(200, {}))))
        render(<AuthProvider><WebSocketProvider><App /></WebSocketProvider></AuthProvider>)

        expect(await screen.findByText(/\$5 a game/)).toBeTruthy()
        fireEvent.change(screen.getByLabelText('Your game idea'), { target: { value: IDEA } })
        fireEvent.click(screen.getByRole('button', { name: /summon it/i }))

        expect(window.location.pathname).toBe('/signup')
        expect(await screen.findByText('Create an account to summon your game')).toBeTruthy()
        expect(screen.getByText(`“${IDEA}”`)).toBeTruthy()
        expect(screen.getByRole('button', { name: /create account/i })).toBeTruthy()
        expect(readDraft()).toBe(IDEA)
    })

    it('sends a new account straight to the create page', async () => {
        saveDraft(IDEA)
        window.history.replaceState(null, '', '/signup')
        vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => Promise.resolve(
            String(url).includes('/auth/signup')
                ? json(200, { token: 'tok-1', user: { id: 'u1', handle: 'bee', role: 'user' } })
                : json(200, { id: 'u1', handle: 'bee', role: 'user', balance: 0 }))))
        render(<AuthProvider><RouterProvider><LoginScreen initialMode="signup" /></RouterProvider></AuthProvider>)

        fireEvent.change(screen.getByPlaceholderText('Handle'), { target: { value: 'bee' } })
        fireEvent.change(screen.getByPlaceholderText('Email'), { target: { value: 'bee@example.com' } })
        fireEvent.change(screen.getByPlaceholderText('Password'), { target: { value: 'a-long-password' } })
        fireEvent.click(screen.getByRole('button', { name: /create account/i }))

        await waitFor(() => expect(window.location.pathname).toBe('/new'))
    })

    it('opens the create page on the idea, keeps it through running out of credits, and spends it on success', async () => {
        saveDraft(IDEA)
        const createGame = vi.spyOn(api, 'createGame')
            .mockRejectedValueOnce(new ApiError(402, { reason: 'insufficient_credits', balance: 0, cost: 1 }))
            .mockResolvedValueOnce({ run_id: 'r1', status: 'designing' })
        const onCreated = vi.fn()
        render(<AuthProvider><RouterProvider><CreateGame onCreated={onCreated} onCancel={() => {}} /></RouterProvider></AuthProvider>)

        expect((screen.getByPlaceholderText(/snail racing game/i) as HTMLTextAreaElement).value).toBe(IDEA)
        fireEvent.click(screen.getByRole('button', { name: /summon it/i }))

        const getCredits = await screen.findByRole('link', { name: /get credits/i })
        expect(getCredits.getAttribute('href')).toBe('/credits')
        expect(readDraft()).toBe(IDEA)

        fireEvent.click(screen.getByRole('button', { name: /summon it/i }))
        await waitFor(() => expect(onCreated).toHaveBeenCalledWith('r1'))
        expect(createGame).toHaveBeenLastCalledWith(IDEA)
        expect(readDraft()).toBe('')
    })
})
