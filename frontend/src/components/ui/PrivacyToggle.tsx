import React from 'react'

const NOTE = {
    private: 'Built only on hardware we rent. Nothing leaves our service.',
    quality: 'Outside model providers. Better games, but they see what you send.',
}

export const PrivacyToggle: React.FC<{
    private: boolean
    onChange: (next: boolean) => void
    disabled?: boolean
    note?: boolean
}> = ({ private: isPrivate, onChange, disabled, note = true }) => {
    const side = (on: boolean) => (on ? 'text-bone' : 'text-dim')
    return (
        <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2 text-sm">
                <span className={side(isPrivate)}>Private</span>
                <button type="button" role="switch" aria-checked={!isPrivate} aria-label="Quality"
                    disabled={disabled} onClick={() => onChange(!isPrivate)}
                    className={`relative w-10 h-5 rounded-full border transition-colors disabled:opacity-40
                                ${isPrivate ? 'bg-sunken border-edge' : 'bg-ember/30 border-ember/60'}`}>
                    <span className={`absolute top-0.5 w-3.5 h-3.5 rounded-full transition-all
                                      ${isPrivate ? 'left-0.5 bg-slate' : 'left-[1.3rem] bg-ember'}`} />
                </button>
                <span className={side(!isPrivate)}>Quality</span>
            </div>
            {note && <span className="text-xs text-dim">{isPrivate ? NOTE.private : NOTE.quality}</span>}
        </div>
    )
}
