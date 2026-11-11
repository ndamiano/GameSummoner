import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import Landing from './Landing'
import DemoGame from './DemoGame'
import { RouterProvider } from '../../router'
import { api } from '../../api/client'
import { AuthProvider } from '../../contexts/AuthContext'

const renderLanding = () =>
    render(
        <AuthProvider>
            <RouterProvider>
                <Landing />
            </RouterProvider>
        </AuthProvider>,
    )

afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
})

describe('landing demos', () => {
    it('shows every demo in the listed order with no tier headings', async () => {
        vi.spyOn(api, 'listDemos').mockResolvedValue([
            { run_id: 'aaa', title: 'Duel', prompt: 'a duel game', thumb_url: null },
            { run_id: 'bbb', title: 'Fox', prompt: 'a fox game', thumb_url: null },
        ])
        renderLanding()
        await waitFor(() => screen.getByText('a fox game'))

        expect(screen.getByText('a duel game').compareDocumentPosition(screen.getByText('a fox game')) &
            Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
        expect(screen.queryByText('Iterated')).toBeNull()
        expect(screen.queryByText('One-shotted')).toBeNull()
    })

    it('clamps a long prompt behind see-more and expands it on click', async () => {
        const long = 'w'.repeat(300)
        vi.spyOn(api, 'listDemos').mockResolvedValue([
            { run_id: 'ccc', title: 'Shop', prompt: long, thumb_url: null },
        ])
        renderLanding()
        const more = await waitFor(() => screen.getByRole('button', { name: 'see more' }))
        expect(screen.queryByText(long)).toBeNull()
        fireEvent.click(more)
        expect(screen.getByText(new RegExp(long))).toBeTruthy()
        expect(screen.getByRole('button', { name: 'less' })).toBeTruthy()
    })

    it('uses the thumb as the card face when one is given', async () => {
        vi.spyOn(api, 'listDemos').mockResolvedValue([
            { run_id: 'ddd', title: 'Fox', prompt: 'a fox game',
              thumb_url: '/api/demos/ddd/thumb' },
        ])
        const { container } = renderLanding()
        await waitFor(() => screen.getByText('a fox game'))
        expect(container.querySelector('img')?.getAttribute('src')).toBe('/api/demos/ddd/thumb')
    })

    it('copies each card’s own direct link', async () => {
        const writeText = vi.fn().mockResolvedValue(undefined)
        vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
        vi.spyOn(api, 'listDemos').mockResolvedValue([
            { run_id: 'aaa', title: 'Duel', prompt: 'a duel game', thumb_url: null },
            { run_id: 'bbb', title: 'Fox', prompt: 'a fox game', thumb_url: null },
        ])
        renderLanding()
        const links = await waitFor(() => {
            const found = screen.getAllByRole('button', { name: 'Copy link' })
            expect(found).toHaveLength(2)
            return found
        })

        fireEvent.click(links[1])
        await waitFor(() => screen.getByRole('button', { name: 'Copied' }))
        expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/d/bbb`)
        expect(screen.getAllByRole('button', { name: 'Copy link' })).toHaveLength(1)
    })

    it('a demo link shows the prompt and plays with no account, and a dead one says so', async () => {
        vi.spyOn(api, 'getDemo').mockResolvedValue(
            { run_id: 'aaa', title: 'Duel', prompt: 'a duel game', thumb_url: null })
        const session = vi.spyOn(api, 'demoPlaySession')
            .mockResolvedValue({ url: 'https://play.test/handoff?t=x', origin: '' })
        render(<AuthProvider><RouterProvider><DemoGame runId="aaa" /></RouterProvider></AuthProvider>)

        await waitFor(() => screen.getByRole('heading', { name: 'Duel' }))
        expect(screen.getByText('a duel game')).toBeTruthy()
        fireEvent.click(screen.getByRole('button', { name: '▶ Play' }))
        const frame = await waitFor(() => screen.getByTitle('Duel') as HTMLIFrameElement)
        expect(frame.src).toBe('https://play.test/handoff?t=x')
        expect(session).toHaveBeenCalledWith('aaa')

        cleanup()
        vi.spyOn(api, 'getDemo').mockRejectedValue(new Error('404'))
        render(<AuthProvider><RouterProvider><DemoGame runId="gone" /></RouterProvider></AuthProvider>)
        await waitFor(() => screen.getByText('This demo isn’t here anymore.'))
    })
})
