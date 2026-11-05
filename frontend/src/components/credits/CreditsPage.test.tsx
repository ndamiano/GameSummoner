import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import CreditsPage, { usd } from './CreditsPage'
import { AuthProvider } from '../../contexts/AuthContext'
import { RouterProvider } from '../../router'
import { api, setAuthToken } from '../../api/client'

afterEach(() => {
    cleanup(); vi.restoreAllMocks(); setAuthToken(null)
    window.history.replaceState(null, '', '/')
})

const PACKAGES = [
    { id: 'one-credit', credits: 1, usd_cents: 500 },
    { id: 'five-credits', credits: 5, usd_cents: 2500 },
    { id: 'ten-credits', credits: 10, usd_cents: 5000 },
]

const catalog = () => ({ packages: PACKAGES })

const me = (balance: number) =>
    ({ id: 'u1', handle: 'alice', role: 'user', email: 'alice@example.com', private: true, balance })

const mount = () => render(
    <RouterProvider>
        <AuthProvider>
            <CreditsPage />
        </AuthProvider>
    </RouterProvider>,
)

describe('CreditsPage', () => {
    it('renders the package catalog with exact prices', async () => {
        vi.spyOn(api, 'listPackages').mockResolvedValue(catalog())
        vi.spyOn(api, 'listPurchases').mockResolvedValue([])
        mount()

        await waitFor(() => expect(screen.getAllByRole('button', { name: 'Buy' })).toHaveLength(3))
        expect(screen.getByText('$5')).toBeTruthy()
        expect(screen.getByText('$25')).toBeTruthy()
        expect(screen.getByText('$50')).toBeTruthy()
        expect(screen.getByText(/no purchases yet/i)).toBeTruthy()
    })

    it('buying sends the user to the provider checkout page', async () => {
        vi.spyOn(api, 'listPackages').mockResolvedValue(catalog())
        vi.spyOn(api, 'listPurchases').mockResolvedValue([])
        const start = vi.spyOn(api, 'startPurchase').mockResolvedValue(
            { purchase_id: 'p1', status: 'started', checkout_url: 'https://checkout.stripe.com/c/cs_1' })
        // jsdom's location.assign is non-configurable — swap the whole object for this test.
        const realLocation = window.location
        const go = vi.fn()
        Object.defineProperty(window, 'location', {
            configurable: true,
            value: { ...realLocation, assign: go, search: '' },
        })
        try {
            mount()

            await waitFor(() => expect(screen.getAllByRole('button', { name: 'Buy' })).toHaveLength(3))
            fireEvent.click(screen.getAllByRole('button', { name: 'Buy' })[1])

            await waitFor(() => expect(go).toHaveBeenCalledWith('https://checkout.stripe.com/c/cs_1'))
            expect(start).toHaveBeenCalledWith('five-credits')
        } finally {
            Object.defineProperty(window, 'location', { configurable: true, value: realLocation })
        }
    })

    it('returning from a successful checkout completes and refreshes', async () => {
        window.history.replaceState(null, '', '/credits?purchase=p1&result=success')
        setAuthToken('tok')
        vi.spyOn(api, 'me')
            .mockResolvedValueOnce(me(0))
            .mockResolvedValue(me(5))
        vi.spyOn(api, 'listPackages').mockResolvedValue(catalog())
        vi.spyOn(api, 'listPurchases')
            .mockResolvedValueOnce([])
            .mockResolvedValue([{
                id: 'p1', package_id: 'five-credits', credits: 5, usd_cents: 2500,
                status: 'completed', created_at: 1754200000, completed_at: 1754200001,
            }])
        const complete = vi.spyOn(api, 'completePurchase')
            .mockResolvedValue({ status: 'completed', credits: 5, balance: 5 })
        mount()

        await waitFor(() => expect(screen.getByText(/5 credits added/i)).toBeTruthy())
        expect(complete).toHaveBeenCalledWith('p1')
        await waitFor(() => expect(screen.getByText('5 credits')).toBeTruthy())
        expect(window.location.search).toBe('')     // params consumed, refresh-safe
    })

    it('returning from a cancelled checkout completes nothing', async () => {
        window.history.replaceState(null, '', '/credits?purchase=p1&result=cancelled')
        vi.spyOn(api, 'listPackages').mockResolvedValue(catalog())
        vi.spyOn(api, 'listPurchases').mockResolvedValue([])
        const complete = vi.spyOn(api, 'completePurchase')
        mount()

        await waitFor(() => expect(screen.getByText(/cancelled/i)).toBeTruthy())
        expect(complete).not.toHaveBeenCalled()
    })

    it('says what went wrong when checkout cannot start', async () => {
        vi.spyOn(api, 'listPackages').mockResolvedValue(catalog())
        vi.spyOn(api, 'listPurchases').mockResolvedValue([])
        vi.spyOn(api, 'startPurchase').mockRejectedValue(new Error('unknown package'))
        mount()

        await waitFor(() => expect(screen.getAllByRole('button', { name: 'Buy' })).toHaveLength(3))
        fireEvent.click(screen.getAllByRole('button', { name: 'Buy' })[0])

        await waitFor(() => expect(screen.getByText(/unknown package/i)).toBeTruthy())
    })
})

describe('usd', () => {
    it('shows whole dollars without cents and fractional exactly', () => {
        expect(usd(500)).toBe('$5')
        expect(usd(5000)).toBe('$50')
        expect(usd(550)).toBe('$5.50')
    })
})
