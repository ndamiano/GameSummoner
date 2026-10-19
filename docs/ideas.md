# Ideas for later

Changes that look right but don't pay for their complexity yet. Each says what it buys and what
would make it worth doing.

## Boot the llm box while decide runs

A build's first llm job waits behind its decide job, so the llm box only starts booting once the
design decisions are in. Starting it when the decide job is enqueued overlaps the two boots: about
140 s of decide (125 s of it EC2 boot) against about 240 s of llm boot, so roughly two minutes off
the start of every cold build. Needs the scaler to start a queue's box on another queue's job.

Worth it when cold starts are a visible share of build time to paying users.

## `local_gpu auto` keeps the llm leg through a long gap

`auto` releases the card when the held queue has been empty for `IDLE_TICKS` polls (6 s), even when
nothing else wants it. A build turn that runs long, such as a compaction or the gates after the
last step, empties the llm queue past that window, and the next turn pays a ~40 s Basalt reload.
Holding the card until another queue has work or `--idle-exit` fires removes it.

Worth it when local builds hit it more than the twice in 77 steps measured on 2026-10-18.

## Referral credits

A person shares a referral link; when someone signs up through it, both get double credits on the
new person's next purchase. It turns existing users into the acquisition channel without giving
away free builds, since the bonus only lands on money actually spent.

Worth it once there are paying customers to do the referring.
