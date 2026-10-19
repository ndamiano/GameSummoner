import React, { useState } from 'react'
import { api, ApiError } from '../api/client'
import { useAuth } from '../contexts/AuthContext'
import { Link, useRouter } from '../router'
import { readDraft } from '../api/draft'
import { Button } from './ui/Button'
import { TextInput } from './ui/Field'
import Footer from './public/Footer'

// The login gate, and open signup behind it.

// The 400/409 detail is already a human sentence — which of the things went wrong is the
// server's to say, so show its words rather than re-derive them from the status.
const signupError = (e: unknown): string => {
    if (e instanceof ApiError && typeof e.body === 'string' && e.body) {
        return e.body.charAt(0).toUpperCase() + e.body.slice(1) + '.'
    }
    return 'Could not create the account.'
}

const Frame: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    <div className="min-h-screen flex flex-col bg-ink">
        <div className="flex-1 flex items-center justify-center px-6 py-8">
            <div className="w-80 flex flex-col gap-4">
                <div className="font-display text-2xl tracking-wide text-center flex items-center justify-center gap-2.5">
                    <span className="w-[18px] h-[18px] rotate-45 border-[1.5px] border-ember relative
                                     after:absolute after:inset-[3px] after:bg-ember after:opacity-50" />
                    GameSummoner
                </div>
                {children}
            </div>
        </div>
        <Footer />
    </div>
)

// The answer is the same whether or not the address has an account, and so is what is shown.
const ForgotPassword: React.FC<{ onBack: () => void }> = ({ onBack }) => {
    const [email, setEmail] = useState('')
    const [sent, setSent] = useState(false)
    const [busy, setBusy] = useState(false)
    const [error, setError] = useState<string | null>(null)

    const submit = async (e: React.FormEvent) => {
        e.preventDefault()
        setBusy(true)
        setError(null)
        try {
            await api.forgotPassword(email)
            setSent(true)
        } catch (err) {
            setError(err instanceof ApiError && err.status === 429
                ? 'Too many attempts — wait a few minutes and try again.'
                : 'Could not send the reset link.')
        } finally {
            setBusy(false)
        }
    }

    if (sent) {
        return (
            <Frame>
                <p className="text-sm text-slate text-center leading-relaxed">
                    If that address has an account, a reset link is on its way.
                </p>
                <button type="button" onClick={onBack}
                    className="text-xs text-dim text-center hover:text-slate transition-colors">
                    Back to sign in
                </button>
            </Frame>
        )
    }

    return (
        <Frame>
            <form onSubmit={submit} className="flex flex-col gap-4">
                <p className="text-sm text-slate text-center leading-relaxed">
                    Enter your email and we will send a link to set a new password.
                </p>
                <TextInput type="email" value={email} onChange={e => setEmail(e.target.value)}
                    placeholder="Email" autoFocus autoComplete="email" />

                {error && <div className="text-fail text-sm">{error}</div>}

                <Button type="submit" variant="primary" size="md" disabled={busy || !email}>
                    {busy ? 'Sending…' : 'Send reset link'}
                </Button>
                <button type="button" onClick={onBack}
                    className="text-xs text-dim text-center hover:text-slate transition-colors">
                    Back to sign in
                </button>
            </form>
        </Frame>
    )
}

const LoginScreen: React.FC<{ initialMode?: 'login' | 'signup' }> = ({ initialMode = 'login' }) => {
    const { login, signup } = useAuth()
    const { navigate } = useRouter()
    const [mode, setMode] = useState<'login' | 'signup' | 'forgot'>(initialMode)
    const idea = readDraft()
    const [handle, setHandle] = useState('')
    const [password, setPassword] = useState('')
    const [email, setEmail] = useState('')
    const [error, setError] = useState<string | null>(null)
    const [busy, setBusy] = useState(false)

    const submit = async (e: React.FormEvent) => {
        e.preventDefault()
        setBusy(true)
        setError(null)
        try {
            if (mode === 'login') await login(handle, password)
            else await signup(handle, password, email)
            navigate(mode === 'signup' || idea ? '/new' : '/', { replace: true })
        } catch (err) {
            if (err instanceof ApiError && err.status === 429) {
                setError('Too many attempts — wait a few minutes and try again.')
            } else {
                setError(mode === 'login'
                    ? 'That handle and password do not match an account.'
                    : signupError(err))
            }
        } finally {
            setBusy(false)
        }
    }

    const switchMode = (to: 'login' | 'signup' | 'forgot') => {
        setMode(to)
        setError(null)
    }

    if (mode === 'forgot') return <ForgotPassword onBack={() => switchMode('login')} />

    const ready = mode === 'login'
        ? handle && password
        : handle && password && email

    return (
        <Frame>
            <form onSubmit={submit} className="flex flex-col gap-4">
                {idea && (
                    <div className="text-center flex flex-col gap-1.5">
                        <p className="text-sm text-bone">
                            {mode === 'signup' ? 'Create an account to summon your game' : 'Sign in to summon your game'}
                        </p>
                        <p className="text-xs text-slate italic line-clamp-2">“{idea}”</p>
                    </div>
                )}
                <TextInput type="text" value={handle} onChange={e => setHandle(e.target.value)}
                    placeholder="Handle" autoFocus autoComplete="username" />
                {mode === 'signup' && (
                    <TextInput type="email" value={email} onChange={e => setEmail(e.target.value)}
                        placeholder="Email" autoComplete="email" />
                )}
                <TextInput type="password" value={password} onChange={e => setPassword(e.target.value)}
                    placeholder="Password"
                    autoComplete={mode === 'login' ? 'current-password' : 'new-password'} />
                {mode === 'signup' && (
                    <p className="text-xs text-dim -mt-2">At least 10 characters.</p>
                )}

                {error && <div className="text-fail text-sm">{error}</div>}

                <Button type="submit" variant="primary" size="md" disabled={busy || !ready}>
                    {mode === 'login'
                        ? (busy ? 'Signing in…' : 'Sign in')
                        : (busy ? 'Creating account…' : 'Create account')}
                </Button>

                {mode === 'login' ? (
                    <>
                        <button type="button" onClick={() => switchMode('forgot')}
                            className="text-xs text-dim text-center hover:text-slate transition-colors">
                            Forgot password?
                        </button>
                        <button type="button" onClick={() => switchMode('signup')}
                            className="text-xs text-dim text-center hover:text-slate transition-colors">
                            New here? Create an account
                        </button>
                    </>
                ) : (
                    <button type="button" onClick={() => switchMode('login')}
                        className="text-xs text-dim text-center hover:text-slate transition-colors">
                        Already have an account? Sign in
                    </button>
                )}

                <Link to="/" className="text-xs text-dim text-center hover:text-slate transition-colors">
                    ← Back to the demos
                </Link>

                <p className="text-xs text-dim text-center leading-relaxed">
                    By {mode === 'login' ? 'signing in' : 'creating an account'} you confirm you are 18+ and accept the{' '}
                    <a href="/terms.html" target="_blank" rel="noreferrer" className="underline hover:text-slate">terms</a>
                    {' '}and{' '}
                    <a href="/privacy.html" target="_blank" rel="noreferrer" className="underline hover:text-slate">privacy note</a>.
                </p>
            </form>
        </Frame>
    )
}

export default LoginScreen
