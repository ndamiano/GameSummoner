import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ShareLink } from './ShareLink'
import SharedGame from '../public/SharedGame'
import { RouterProvider } from '../../router'
import { AuthProvider } from '../../contexts/AuthContext'
import { api } from '../../api/client'

afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
})

describe('sharing a game', () => {
    it('makes a link, keeps it on update, and takes it down', async () => {
        const share = vi.spyOn(api, 'shareGame').mockResolvedValue({ share_id: 'abc123def456' })
        vi.spyOn(api, 'unshareGame').mockResolvedValue({ share_id: null })
        render(<ShareLink runId="r1" initial={null} />)

        fireEvent.click(screen.getByRole('button', { name: 'Share a link' }))
        const link = await waitFor(() => screen.getByLabelText('share link') as HTMLInputElement)
        expect(link.value).toBe(`${window.location.origin}/g/abc123def456`)

        fireEvent.click(screen.getByRole('button', { name: 'Update shared copy' }))
        await waitFor(() => expect(share).toHaveBeenCalledTimes(2))
        expect((screen.getByLabelText('share link') as HTMLInputElement).value).toContain('/g/abc123def456')

        fireEvent.click(screen.getByRole('button', { name: 'Stop sharing' }))
        await waitFor(() => screen.getByRole('button', { name: 'Share a link' }))
        expect(screen.queryByLabelText('share link')).toBeNull()
    })

    it('a shared link plays with no account, and a dead one says so', async () => {
        vi.spyOn(api, 'getShare').mockResolvedValue({ share_id: 'abc123def456', title: 'Fox Run', thumb_url: null })
        const session = vi.spyOn(api, 'sharePlaySession').mockResolvedValue({ url: 'https://play.test/handoff?t=x', origin: '' })
        render(<AuthProvider><RouterProvider><SharedGame shareId="abc123def456" /></RouterProvider></AuthProvider>)

        await waitFor(() => screen.getByRole('heading', { name: 'Fox Run' }))
        fireEvent.click(screen.getByRole('button', { name: '▶ Play' }))
        const frame = await waitFor(() => screen.getByTitle('Fox Run') as HTMLIFrameElement)
        expect(frame.src).toBe('https://play.test/handoff?t=x')
        expect(session).toHaveBeenCalledWith('abc123def456')

        cleanup()
        vi.spyOn(api, 'getShare').mockRejectedValue(new Error('404'))
        render(<AuthProvider><RouterProvider><SharedGame shareId="gone" /></RouterProvider></AuthProvider>)
        await waitFor(() => screen.getByText('This game isn’t shared anymore.'))
    })
})
