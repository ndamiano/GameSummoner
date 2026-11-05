import React, { createContext, useContext, useCallback, useEffect, useState } from 'react'
import { api, getAuthToken, setAuthToken, setUnauthorizedHandler } from '../api/client'

interface AuthUser {
    id: string
    handle: string
    role: string
    // Only /auth/me carries it; the login and signup answers do not.
    email?: string
    private?: boolean
}

interface AuthContextValue {
    token: string | null
    user: AuthUser | null
    balance: number | null
    login: (handle: string, password: string) => Promise<void>
    signup: (handle: string, password: string, email: string) => Promise<void>
    resetPassword: (resetToken: string, newPassword: string) => Promise<void>
    changePassword: (currentPassword: string, newPassword: string) => Promise<void>
    logout: () => Promise<void>
    refreshBalance: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const [token, setToken] = useState<string | null>(() => getAuthToken())
    const [user, setUser] = useState<AuthUser | null>(null)
    const [balance, setBalance] = useState<number | null>(null)

    const clear = useCallback(() => {
        setToken(null)
        setUser(null)
        setBalance(null)
    }, [])

    const logout = useCallback(async () => {
        await api.logout()   // revoke server-side before dropping the local token
        setAuthToken(null)
        clear()
    }, [clear])

    useEffect(() => {
        setUnauthorizedHandler(clear)
        return () => setUnauthorizedHandler(null)
    }, [clear])

    const refreshBalance = useCallback(async () => {
        if (!getAuthToken()) return
        try {
            const me = await api.me()
            setUser({ id: me.id, handle: me.handle, role: me.role, email: me.email, private: me.private })
            setBalance(me.balance)
        } catch {
            // A 401 is handled centrally (clears the token); nothing else to do here.
        }
    }, [])

    useEffect(() => {
        if (token) refreshBalance()
    }, [token, refreshBalance])

    const login = useCallback(async (handle: string, password: string) => {
        const res = await api.login(handle, password)
        setAuthToken(res.token)
        setUser(res.user)
        setToken(res.token)
    }, [])

    const signup = useCallback(async (handle: string, password: string, email: string) => {
        const res = await api.signup(handle, password, email)
        setAuthToken(res.token)
        setUser(res.user)
        setToken(res.token)
    }, [])

    const resetPassword = useCallback(async (resetToken: string, newPassword: string) => {
        const res = await api.resetPassword(resetToken, newPassword)
        setAuthToken(res.token)
        setUser(res.user)
        setToken(res.token)
    }, [])

    // The old token is revoked the moment the change lands, and the websocket reconnects with
    // whatever this holds — so the replacement has to reach the context, not only the api client.
    const changePassword = useCallback(async (currentPassword: string, newPassword: string) => {
        const res = await api.changePassword(currentPassword, newPassword)
        setToken(res.token)
    }, [])

    return (
        <AuthContext.Provider value={{
            token, user, balance, login, signup, resetPassword, changePassword, logout, refreshBalance,
        }}>
            {children}
        </AuthContext.Provider>
    )
}

export const useAuth = (): AuthContextValue => {
    const ctx = useContext(AuthContext)
    if (!ctx) throw new Error('useAuth must be used within AuthProvider')
    return ctx
}
