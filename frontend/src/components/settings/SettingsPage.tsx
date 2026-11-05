import React, { useState } from 'react'
import { api } from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import { Link } from '../../router'
import { Button } from '../ui/Button'
import { SectionLabel, TextInput } from '../ui/Field'
import { PrivacyToggle } from '../ui/PrivacyToggle'

const Row: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
    <div className="flex items-baseline justify-between gap-4 text-sm">
        <span className="text-slate">{label}</span>
        <span className="text-bone">{children}</span>
    </div>
)

const SettingsPage: React.FC = () => {
    const { user, balance, logout, refreshBalance, changePassword: applyNewPassword } = useAuth()
    const [current, setCurrent] = useState('')
    const [next, setNext] = useState('')
    const [confirm, setConfirm] = useState('')
    const [busy, setBusy] = useState(false)
    const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null)
    const [newEmail, setNewEmail] = useState('')
    const [emailPassword, setEmailPassword] = useState('')
    const [emailBusy, setEmailBusy] = useState(false)
    const [emailMessage, setEmailMessage] = useState<{ text: string; ok: boolean } | null>(null)

    const [privacyBusy, setPrivacyBusy] = useState(false)
    const [privacyError, setPrivacyError] = useState<string | null>(null)

    const canSubmit = current && next && confirm && !busy
    const canSubmitEmail = newEmail && emailPassword && !emailBusy

    const changePassword = async () => {
        if (next !== confirm) {
            setMessage({ text: 'The new passwords do not match.', ok: false })
            return
        }
        setBusy(true); setMessage(null)
        try {
            await applyNewPassword(current, next)
            setCurrent(''); setNext(''); setConfirm('')
            setMessage({ text: 'Password changed.', ok: true })
        } catch (e) {
            setMessage({ text: e instanceof Error ? e.message : 'Could not change the password', ok: false })
        } finally { setBusy(false) }
    }

    const changePrivacy = async (isPrivate: boolean) => {
        setPrivacyBusy(true); setPrivacyError(null)
        try {
            await api.setPrivacy(isPrivate)
            await refreshBalance()
        } catch (e) {
            setPrivacyError(e instanceof Error ? e.message : 'Could not change the setting')
        } finally { setPrivacyBusy(false) }
    }

    const changeEmail = async () => {
        setEmailBusy(true); setEmailMessage(null)
        try {
            await api.changeEmail(emailPassword, newEmail)
            setNewEmail(''); setEmailPassword('')
            await refreshBalance()
            setEmailMessage({ text: 'Email changed.', ok: true })
        } catch (e) {
            setEmailMessage({ text: e instanceof Error ? e.message : 'Could not change the email', ok: false })
        } finally { setEmailBusy(false) }
    }

    return (
        <div className="h-full overflow-y-auto">
            <div className="max-w-xl mx-auto px-6 py-8 flex flex-col gap-8">
                <h2 className="font-display text-2xl">Settings</h2>

                <section className="flex flex-col gap-3">
                    <SectionLabel>Account</SectionLabel>
                    <div className="border border-edge rounded-md bg-sunken px-4 py-3 flex flex-col gap-2">
                        <Row label="Handle">{user?.handle ?? '—'}</Row>
                        <Row label="Email">{user?.email ?? '—'}</Row>
                        <Row label="Role">{user?.role ?? '—'}</Row>
                        <Row label="Credits">
                            <span className="flex items-baseline gap-3">
                                <span>{balance ?? '—'}</span>
                                <Link to="/credits" className="text-ember text-xs hover:underline">
                                    Buy credits
                                </Link>
                            </span>
                        </Row>
                    </div>
                </section>

                <section className="flex flex-col gap-3">
                    <SectionLabel>Privacy</SectionLabel>
                    <div className="border border-edge rounded-md bg-sunken px-4 py-3 flex flex-col gap-3">
                        <div className="flex items-center justify-between gap-4 flex-wrap text-sm">
                            <span className="text-slate">New games</span>
                            <PrivacyToggle private={user?.private ?? true} onChange={changePrivacy}
                                disabled={privacyBusy || !user} note={false} />
                        </div>
                        <p className="text-xs text-dim">
                            <span className="text-slate">Private:</span> built only on hardware we rent.
                            Nothing leaves our service.
                        </p>
                        <p className="text-xs text-dim">
                            <span className="text-slate">Quality:</span> the design and the code are
                            written by an outside model provider, currently DeepSeek. Better games, but
                            they see what you send and may learn from it.
                        </p>
                        <p className="text-xs text-dim">
                            You can choose again for each game when you summon it, and a game keeps
                            the choice it was made with.
                        </p>
                        {privacyError && <p className="text-fail text-sm">{privacyError}</p>}
                    </div>
                </section>

                <section className="flex flex-col gap-3">
                    <SectionLabel>Change email</SectionLabel>
                    <form className="flex flex-col gap-2.5"
                        onSubmit={e => { e.preventDefault(); if (canSubmitEmail) changeEmail() }}>
                        <TextInput type="email" autoComplete="email" placeholder="New email"
                            value={newEmail} onChange={e => setNewEmail(e.target.value)} disabled={emailBusy} />
                        <TextInput type="password" autoComplete="current-password" placeholder="Current password"
                            value={emailPassword} onChange={e => setEmailPassword(e.target.value)} disabled={emailBusy} />
                        <div className="flex items-center gap-3">
                            <Button type="submit" variant="primary" size="md" disabled={!canSubmitEmail}>
                                {emailBusy ? 'Changing…' : 'Change email'}
                            </Button>
                            {emailMessage && (
                                <span className={`text-sm ${emailMessage.ok ? 'text-live' : 'text-fail'}`}>
                                    {emailMessage.text}
                                </span>
                            )}
                        </div>
                    </form>
                </section>

                <section className="flex flex-col gap-3">
                    <SectionLabel>Change password</SectionLabel>
                    <form className="flex flex-col gap-2.5"
                        onSubmit={e => { e.preventDefault(); if (canSubmit) changePassword() }}>
                        <TextInput type="password" autoComplete="current-password" placeholder="Current password"
                            value={current} onChange={e => setCurrent(e.target.value)} disabled={busy} />
                        <TextInput type="password" autoComplete="new-password" placeholder="New password"
                            value={next} onChange={e => setNext(e.target.value)} disabled={busy} />
                        <TextInput type="password" autoComplete="new-password" placeholder="New password, again"
                            value={confirm} onChange={e => setConfirm(e.target.value)} disabled={busy} />
                        <div className="flex items-center gap-3">
                            <Button type="submit" variant="primary" size="md" disabled={!canSubmit}>
                                {busy ? 'Changing…' : 'Change password'}
                            </Button>
                            {message && (
                                <span className={`text-sm ${message.ok ? 'text-live' : 'text-fail'}`}>
                                    {message.text}
                                </span>
                            )}
                        </div>
                    </form>
                </section>

                <section className="flex flex-col gap-3">
                    <SectionLabel>Session</SectionLabel>
                    <div>
                        <Button variant="danger" size="md" onClick={logout}>Sign out</Button>
                    </div>
                </section>

                <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-dim border-t border-edge pt-4">
                    <a href="/about.html" target="_blank" rel="noreferrer" className="hover:text-slate transition-colors">About</a>
                    <a href="/terms.html" target="_blank" rel="noreferrer" className="hover:text-slate transition-colors">Terms</a>
                    <a href="/privacy.html" target="_blank" rel="noreferrer" className="hover:text-slate transition-colors">Privacy</a>
                    <a href="mailto:support@gamesummoner.com" className="hover:text-slate transition-colors">
                        support@gamesummoner.com
                    </a>
                </div>
            </div>
        </div>
    )
}

export default SettingsPage
