import React, { useEffect } from 'react'
import { track } from '../api/track'
import Studio from './Studio'
import AdminPanel from './AdminPanel'
import SettingsPage from './settings/SettingsPage'
import CreditsPage from './credits/CreditsPage'
import DemoGallery from './public/DemoGallery'
import { useAuth } from '../contexts/AuthContext'
import { Link, parseRoute, useRouter } from '../router'

const NAV: { to: string; label: string; adminOnly?: boolean }[] = [
    { to: '/', label: 'Games' },
    { to: '/demos', label: 'Demos' },
    { to: '/admin', label: 'Admin', adminOnly: true },
]

const Layout: React.FC = () => {
    const { path, navigate } = useRouter()
    const { user, balance } = useAuth()
    const route = parseRoute(path)

    useEffect(() => track('page_view', { path }), [path])

    // The admin surfaces are operator-only. The server enforces it (require_admin → 403); this
    // just keeps a non-admin from landing on an empty error page via a typed URL.
    const isAdmin = user?.role === 'admin'
    const blocked = route.kind === 'admin' && user != null && !isAdmin
    useEffect(() => { if (blocked) navigate('/', { replace: true }) }, [blocked, navigate])

    const nav = NAV.filter(n => isAdmin || !n.adminOnly)
    const navActive = (to: string) =>
        to === '/' ? ['library', 'create', 'game'].includes(route.kind)
            : to === '/admin' ? route.kind === 'admin' : path === to

    return (
        <div className="h-screen flex flex-col overflow-hidden bg-ink">
            <header className="flex-shrink-0 border-b border-edge bg-sunken px-4 py-2.5
                               flex items-center justify-between gap-4">
                <div className="flex items-center gap-5">
                    <Link to="/" className="font-display text-lg tracking-wide flex items-center gap-2.5
                                            hover:text-ember transition-colors">
                        <span className="w-[18px] h-[18px] rotate-45 border-[1.5px] border-ember relative
                                         after:absolute after:inset-[3px] after:bg-ember after:opacity-50" />
                        GameSummoner
                    </Link>
                    {nav.length > 1 && (
                        <nav className="flex gap-1">
                            {nav.map(n => (
                                <Link key={n.to} to={n.to}
                                    className={`px-3 py-1 rounded text-sm transition-colors ${
                                        navActive(n.to) ? 'bg-bone/10 text-bone' : 'text-slate hover:text-bone'
                                    }`}>
                                    {n.label}
                                </Link>
                            ))}
                        </nav>
                    )}
                </div>

                <div className="flex items-center gap-4 text-sm">
                    <Link to="/credits" title="credit balance — buy credits"
                        className={`flex items-center gap-2 font-mono text-xs transition-colors ${
                            route.kind === 'credits' ? 'text-bone' : 'hover:text-bone'
                        }`}>
                        <span className="w-2 h-2 rounded-full bg-mana" />
                        {balance ?? '—'} <span className="text-slate">credits</span>
                    </Link>
                    {user && (
                        <Link to="/settings" title="settings"
                            className={`transition-colors ${
                                route.kind === 'settings' ? 'text-bone' : 'text-slate hover:text-bone'
                            }`}>
                            {user.handle}
                        </Link>
                    )}
                </div>
            </header>

            <main className="flex-1 overflow-hidden">
                {route.kind === 'settings' && <SettingsPage />}
                {route.kind === 'demos' && (
                    <div className="h-full overflow-y-auto">
                        <div className="max-w-5xl mx-auto px-6 py-10">
                            <DemoGallery />
                        </div>
                    </div>
                )}
                {route.kind === 'credits' && <CreditsPage />}
                {route.kind === 'admin' && isAdmin && <AdminPanel tab={route.tab} />}
                {(route.kind === 'library' || route.kind === 'create' || route.kind === 'game') && (
                    <Studio route={route} />
                )}
            </main>
        </div>
    )
}

export default Layout
