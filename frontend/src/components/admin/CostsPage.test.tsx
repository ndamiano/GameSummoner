import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { CostSummary, PodTable, sortPods } from './CostsPage'
import type { Sort } from './CostsPage'
import type { CostPod } from '../../types'

const pod: CostPod = {
    worker_id: 'w-art',
    pod_id: 'p-art',
    source: 'runpod',
    tracked: true,
    queue: 'comfy',
    gpu_type: 'NVIDIA GeForce RTX 5090',
    usd_per_hour: 1,
    started_at: 1000,
    terminated_at: 2800,
    provider_usd: 0.6,
    disk_usd: 0.1,
    billed_seconds: 1800,
    jobs: 3,
    failed: 1,
    exec_seconds: 900,
    customer_usd: 0.1,
}

const ghost: CostPod = {
    ...pod, worker_id: null, pod_id: 'boot-looper', tracked: false, queue: null, gpu_type: null, usd_per_hour: null,
    started_at: null, terminated_at: null, provider_usd: 1.5, disk_usd: 0, billed_seconds: null,
    jobs: 0, failed: 0, exec_seconds: 0, customer_usd: 0,
}

const ec2: CostPod = {
    ...pod, worker_id: 'w-ec2', pod_id: 'eu-north-1/i-0abc', source: 'aws', queue: 'llm', gpu_type: 'g7e.2xlarge',
    usd_per_hour: 1.2, provider_usd: 0.6, disk_usd: 0, customer_usd: 1,
}

const busy: CostPod = { ...pod, worker_id: 'w-llm', pod_id: 'p-llm', provider_usd: 2, customer_usd: 3, jobs: 40 }

const ids = () => screen.getAllByRole('row').slice(1).map(r => r.querySelector('td')!.textContent)

describe('PodTable', () => {
    afterEach(cleanup)

    it('shows what each pod cost beside what it billed', () => {
        render(<PodTable pods={[pod, ghost]} sort={{ key: 'cost', desc: true }} onSort={() => {}} />)
        expect(screen.getByText('p-art').closest('tr')!.textContent).toBe('p-art$0.60$0.10350%−$0.50')
        expect(screen.getByText(/boot-looper/).closest('tr')!.textContent).toContain('ghost')
    })

    it('sorts by the clicked column and flips on a second click', () => {
        let sort: Sort = { key: 'cost', desc: true }
        const { rerender } = render(<PodTable pods={[pod, ghost, busy]} sort={sort} onSort={s => { sort = s }} />)
        expect(ids()).toEqual(['p-llm', 'boot-looper ghost', 'p-art'])

        fireEvent.click(screen.getByText('margin'))
        rerender(<PodTable pods={[pod, ghost, busy]} sort={sort} onSort={s => { sort = s }} />)
        expect(ids()).toEqual(['p-llm', 'p-art', 'boot-looper ghost'])

        fireEvent.click(screen.getByText(/margin/))
        expect(sort).toEqual({ key: 'margin', desc: false })
    })
})

describe('sortPods', () => {
    it('puts rows with no value last in either direction', () => {
        const noLedger = { ...pod, pod_id: 'p-none', provider_usd: null }
        for (const desc of [true, false]) {
            const sorted = sortPods([noLedger, pod, busy], { key: 'cost', desc })
            expect(sorted[sorted.length - 1].pod_id).toBe('p-none')
        }
    })
})

describe('CostSummary', () => {
    afterEach(cleanup)

    it('totals RunPod against billed and counts the ghosts', () => {
        render(<CostSummary pods={[pod, ghost]} reachable />)
        expect(screen.getByText('$2.10')).toBeTruthy()
        expect(screen.getByText('−$2.00')).toBeTruthy()
        expect(screen.getByText('2 (1 ghost)')).toBeTruthy()
    })

    it('counts an EC2 life in the cost and the margin', () => {
        render(<CostSummary pods={[pod, ec2]} reachable />)
        expect(screen.getByText('$1.20')).toBeTruthy()
        expect(screen.getByText('−$0.10')).toBeTruthy()
    })

    it('states no RunPod total when the ledger is unreachable', () => {
        render(<CostSummary pods={[{ ...pod, provider_usd: null, billed_seconds: null }]} reachable={false} />)
        expect(screen.getAllByText('—').length).toBe(3)
    })
})
