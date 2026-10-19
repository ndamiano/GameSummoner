const KEY = 'gs.draft'

export function readDraft(): string {
    try { return sessionStorage.getItem(KEY) ?? '' } catch { return '' }
}

export function saveDraft(text: string): void {
    try { sessionStorage.setItem(KEY, text) } catch { return }
}

export function clearDraft(): void {
    try { sessionStorage.removeItem(KEY) } catch { return }
}
