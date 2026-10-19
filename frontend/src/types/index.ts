export interface Game {
    run_id: string
    title: string
    status: GameStatus
    built: boolean
    building: boolean
    paused: boolean
}

// 'held': the safety screen held the build for human review — frozen and shown as unavailable.
export type GameStatus = 'idle' | 'queued' | 'building' | 'fixing' | 'changing' | 'paused' | 'built' | 'held'

export interface GameDetail {
    run_id: string
    // The user's request, verbatim — kept beside whatever builds.
    ask: string
    // The text the build sends as its user message — editable here, byte for byte what runs.
    // null while the designer is still writing it from `ask`.
    prompt: string | null
    title: string
    built: boolean
    building: boolean
    status: GameStatus
    // A previous attempt left files in the game folder — what a from-scratch build discards.
    has_game: boolean
    // Compute budget for the numberless bar: fraction remaining, 0..1, or null when the game is
    // uncharged (no bar). Raw spend — especially spent_micros — is deliberately never surfaced.
    budget_pct_remaining: number | null
    share_id: string | null
}

// One row of the durable build/spec event log (GET /api/games/:id/events). Written by the backend's
// _emit: the event name is in `kind` and the fields live in `payload` (flattened) — a different
// shape from the live websocket copy, so both are normalized to a common event before folding.
export interface DurableEventRow {
    id: number
    game_id: string
    build_id: string | null
    kind: string
    payload: Record<string, any>
    created_at: number
}

// An asset the game declared in its own assets.json (GET /api/games/:id/assets). The bytes are
// fetched separately from the authed blob route (never the public /play mount), so there is no url
// here — the gallery builds an object URL from an authed fetch. `status`: ready (on disk),
// rendering (a batch is live), or pending (declared, not yet rendered).
export type AssetKind = 'sprite' | 'tile' | 'scene' | 'mesh'
// 'blocked': the render was refused by the safety screen; a redraw with a new prompt clears it.
export type AssetStatus = 'ready' | 'rendering' | 'pending' | 'blocked'
export interface GameAsset {
    id: string
    kind: AssetKind
    status: AssetStatus
    prompt: string
    // What the render came back BROKEN as, if anything — never whether it is any good.
    defect: string | null
}

// The prompt log (admin-only): every llm turn any game spent, reconstructed from the durable jobs
// rows. The index carries sizes and the head of the system prompt; the bodies are tens of KB each,
// so a turn's full text is fetched on click.

// A safety refusal as the admin surface lists it (GET /api/admin/violations). `matched` is the
// matched term(s) or refusal reason — never the flagged content itself.
export interface AdminViolation {
    id: number
    user_id: string | null
    handle: string | null
    game_id: string | null
    source: string
    category: string
    matched: string
    created_at: number
}

// Admin queue snapshot (GET /api/admin/queues): depth, the next jobs in claim order, and the
// fleet with what each worker holds. Visualization only — no spend.
export interface QueuedJob {
    id: string
    game_id: string | null
    build_id: string | null
    waiting_seconds: number
    est_seconds: number
}

// `booting` is a pod the scaler created that no worker has registered from yet: id is the pod
// id, usd_per_hour is the pod's price from the create, gpu_type is what the create stated.
export interface WorkerRow {
    id: string
    state: 'busy' | 'idle' | 'booting'
    gpu_type: string | null
    usd_per_hour: number | null
    source: string | null
    pod_id: string | null
    uptime_seconds: number
    last_seen_seconds: number
    busy_seconds: number
    job: { id: string; game_id: string | null; build_id: string | null
           running_seconds: number; est_seconds: number } | null
}

export interface Stockouts {
    last_1h: number
    last_24h: number
    last_7d: number
    last_at: number | null
}

export interface QueueRow {
    queue: string
    pending: number
    claimed: number
    oldest_pending_age_seconds: number | null
    workers_live: number
    workers_max: number
    est_seconds: number
    backlog_seconds: number
    next: QueuedJob[]
    workers: WorkerRow[]
    stockouts: Stockouts
}

// One pod life (worker row); RunPod reuses pod ids, so a pod_id can repeat. Untracked = ghost spend.
export interface CostPod {
    worker_id: string | null
    pod_id: string
    source: string | null
    tracked: boolean
    queue: string | null
    gpu_type: string | null
    usd_per_hour: number | null
    started_at: number | null
    terminated_at: number | null
    provider_usd: number | null
    disk_usd: number | null
    billed_seconds: number | null
    jobs: number
    failed: number
    exec_seconds: number
    customer_usd: number
}

export interface AdminCosts {
    days: number
    runpod_reachable: boolean
    pods: CostPod[]
    games: { n: number; avg_gpu_hours: number | null; avg_usd: number | null; avg_changes: number | null }
}

export interface AdminAnalytics {
    kinds: string[]
    days: { day: string; users: number; kinds: Record<string, number> }[]
}

export interface AdminQueues {
    queues: QueueRow[]
    totals: { pending: number; claimed: number; workers_live: number; backlog_seconds: number }
}

// Generic envelope — the event bus broadcasts many shapes. The fields the UI actually reads are
// typed as optional so a consumer can narrow by `type` without a full discriminated union; the rest
// of what the bus sends still falls through the index signature.
export type WebSocketMessage = {
    type: string
    run_id?: string
    timestamp?: string
    // build_started / build_step
    step?: number
    summary?: string
    // build_started carries wall-clock seconds (time.time()) the build began; build_step carries
    // `elapsed` seconds since that start, so the progress header can show a running timer.
    started_at?: number
    elapsed?: number
    // build_done / assets_done
    ok?: boolean
    error?: string
    rendered?: number
    [key: string]: any
}

// docs/game_rubric.md. A dimension's null score is `n/a` — not what this game is for — and is not
// a zero; nothing here sums, the verdicts are separate holistic calls.
export interface GradeClaim {
    claim: string
    verdict: 'delivered' | 'partial' | 'absent'
    note: string
}

export interface Grade {
    loads: boolean
    takes_input: boolean
    crashed: boolean
    crash_note: string
    first_impression: number | null
    first_impression_note: string
    show_someone: string
    what_is_it: string
    biggest_gap: string
    dimensions: Record<string, { score: number | null; note: string }>
    considered: number | null
    considered_note: string
    what_moved_it: string
    claims: GradeClaim[]
    unrequested: string
    play_ended: string
    play_minutes: number | null
}

export interface GradeTarget {
    run_id: string
    request: string
    previous: number
}

export interface SharedGame {
    share_id: string
    title: string
    thumb_url: string | null
}

export interface Demo {
    run_id: string
    title: string
    prompt: string
    tier: 'showcase' | 'oneshot'
    thumb_url: string | null
}
