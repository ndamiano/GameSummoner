import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import Landing from './Landing'
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
})

describe('landing demo tiers', () => {
    it('groups demos under their tier headings, showcase first', async () => {
        vi.spyOn(api, 'listDemos').mockResolvedValue([
            { run_id: 'aaa', title: 'Duel', prompt: 'a duel game', tier: 'showcase', thumb_url: null },
            { run_id: 'bbb', title: 'Fox', prompt: 'a fox game', tier: 'oneshot', thumb_url: null },
        ])
        renderLanding()
        await waitFor(() => screen.getByText('a fox game'))

        const showcase = screen.getByText('Iterated')
        const oneshot = screen.getByText('One-shotted')
        expect(showcase.compareDocumentPosition(oneshot) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
        expect(oneshot.compareDocumentPosition(screen.getByText('a fox game')) &
            Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    })

    it('hides a tier with no demos in it', async () => {
        vi.spyOn(api, 'listDemos').mockResolvedValue([
            { run_id: 'bbb', title: 'Fox', prompt: 'a fox game', tier: 'oneshot', thumb_url: null },
        ])
        renderLanding()
        await waitFor(() => screen.getByText('a fox game'))
        expect(screen.queryByText('Iterated')).toBeNull()
    })

    it('clamps a long prompt behind see-more and expands it on click', async () => {
        const long = 'w'.repeat(300)
        vi.spyOn(api, 'listDemos').mockResolvedValue([
            { run_id: 'ccc', title: 'Shop', prompt: long, tier: 'showcase', thumb_url: null },
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
            { run_id: 'ddd', title: 'Fox', prompt: 'a fox game', tier: 'oneshot',
              thumb_url: '/api/demos/ddd/thumb' },
        ])
        const { container } = renderLanding()
        await waitFor(() => screen.getByText('a fox game'))
        expect(container.querySelector('img')?.getAttribute('src')).toBe('/api/demos/ddd/thumb')
    })
})
