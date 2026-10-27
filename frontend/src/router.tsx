import React, { createContext, useCallback, useContext, useEffect, useState } from 'react'

// The URL is the view state: real paths, so the browser's back button and middle-click work.
// Hand-rolled on the History API — the route table is six paths, not worth a dependency.

interface RouterValue {
    path: string
    navigate: (to: string, opts?: { replace?: boolean }) => void
}

const RouterContext = createContext<RouterValue | null>(null)

export const RouterProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const [path, setPath] = useState(window.location.pathname)

    useEffect(() => {
        const onPop = () => setPath(window.location.pathname)
        window.addEventListener('popstate', onPop)
        return () => window.removeEventListener('popstate', onPop)
    }, [])

    const navigate = useCallback((to: string, { replace = false }: { replace?: boolean } = {}) => {
        if (to === window.location.pathname) return
        if (replace) window.history.replaceState(null, '', to)
        else window.history.pushState(null, '', to)
        setPath(to)
    }, [])

    return <RouterContext.Provider value={{ path, navigate }}>{children}</RouterContext.Provider>
}

export const useRouter = (): RouterValue => {
    const ctx = useContext(RouterContext)
    if (!ctx) throw new Error('useRouter must be used within RouterProvider')
    return ctx
}

// A real anchor, so middle-click/new-tab work; plain left-clicks stay in the SPA.
export const Link: React.FC<React.AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }> = ({
    to, onClick, children, ...rest
}) => {
    const { navigate } = useRouter()
    return (
        <a href={to} {...rest}
            onClick={e => {
                onClick?.(e)
                if (e.defaultPrevented || e.button !== 0) return
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
                e.preventDefault()
                navigate(to)
            }}>
            {children}
        </a>
    )
}

export const ADMIN_TABS = ['queues', 'costs', 'usage', 'violations'] as const
export type AdminTab = typeof ADMIN_TABS[number]

export type Route =
    | { kind: 'library' }
    | { kind: 'create' }
    | { kind: 'game'; runId: string }
    | { kind: 'settings' }
    | { kind: 'credits' }
    | { kind: 'demos' }
    | { kind: 'admin'; tab: AdminTab }

// Unknown paths fall back to the library rather than a 404 page — every dead link in a tool this
// small should land somewhere useful.
export const parseRoute = (path: string): Route => {
    if (path === '/new') return { kind: 'create' }
    if (path === '/settings') return { kind: 'settings' }
    if (path === '/credits') return { kind: 'credits' }
    if (path === '/demos') return { kind: 'demos' }
    if (path === '/admin') return { kind: 'admin', tab: 'queues' }
    const admin = path.match(/^\/admin\/([a-z]+)$/)
    if (admin && (ADMIN_TABS as readonly string[]).includes(admin[1])) {
        return { kind: 'admin', tab: admin[1] as AdminTab }
    }
    const game = path.match(/^\/game\/([A-Za-z0-9_-]+)$/)
    if (game) return { kind: 'game', runId: game[1] }
    return { kind: 'library' }
}
