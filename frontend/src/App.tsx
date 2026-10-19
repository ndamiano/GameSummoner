import Layout from './components/Layout'
import LoginScreen from './components/LoginScreen'
import ResetScreen from './components/ResetScreen'
import Landing from './components/public/Landing'
import SharedGame from './components/public/SharedGame'
import ErrorBoundary from './components/ErrorBoundary'
import { useAuth } from './contexts/AuthContext'
import { RouterProvider, useRouter } from './router'

// Signed out, the site is the pitch: the landing page with playable demos, and /login to sign
// in or create an account. Signed in, every path belongs to the app.
const PublicSite = () => {
    const { path } = useRouter()
    if (path === '/login') return <LoginScreen />
    if (path === '/signup') return <LoginScreen initialMode="signup" />
    return <Landing />
}

// /reset sits above the auth gate: the link is followed from an email, where a session is exactly
// what the person does not have.
const Routes = () => {
    const { token } = useAuth()
    const { path } = useRouter()
    if (path === '/reset') return <ResetScreen />
    if (path.startsWith('/g/')) return <SharedGame shareId={path.slice(3)} />
    return token ? <Layout /> : <PublicSite />
}

function App() {
    return (
        <div className="min-h-screen bg-ink">
            <ErrorBoundary>
                <RouterProvider>
                    <Routes />
                </RouterProvider>
            </ErrorBoundary>
        </div>
    )
}

export default App
