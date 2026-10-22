# The build path, module by module

`CLAUDE.md` holds the laws this code exists to serve; this file is the map. Module docstrings are
the authority on contracts — what is here is the shape and the reasons that are not visible from any
one file. `docs/architecture.md` is the layer above (processes, state, trust boundaries).

```
runtime/
  vendor/three.module.js  vendored three.js (MIT, self-contained) + GLTFLoader — copied into every
                         game folder at seed, so a 3D game imports a renderer with no network fetch.
  vendor/lib/*.js        the helper library, copied to <game>/lib/ at seed: input.js (WASD and
                         arrows aliased, mouse, touch), audio.js (WebAudio synth, no files: eight
                         named effects, and `sound`/`noise`/`seq`/`loop` for the ones a game
                         invents — a footstep, an engine that rises with speed, an alarm),
                         canvas.js (the fixed, scaling canvas with a camera), lights.js (sun +
                         sky + exposure for a three.js scene that is not a world).
                         Each header comment is its API; build.txt tells the model to read the
                         one it uses.
  games/<run_id>/        staged games, served at /play

src/
  maestro/
    codegen/             THE build path:
      build_chain.py     THE build DRIVER — what a finished llm turn does next. A build is a linear
                         chain of `llm` jobs tagged metadata.stage="build"; /worker/complete routes
                         here. advance() runs ALL local work — tool dispatch, staging —
                         synchronously and SUSPENDS only at a real inference (enqueue one llm job +
                         return; the process is free to die). WHERE A GAME STANDS is two columns
                         on its games row and nowhere else: `stage` (designing, building, checking)
                         and `status` (running, paused, failed, succeeded, held). Every change goes
                         through `games.move`, one guarded UPDATE that only lands while the row is
                         still where the caller saw it, so of two racing callers exactly one wins:
                         one finalize moves building→checking and starts the gates, a gate's fix
                         (`kickoff(after=build)`) starts only while that build is still the game's
                         latest and the game still running, and a landed turn whose build is not
                         the latest is dropped. A per-run flock serializes advance across every
                         process that drives builds (control plane, CLI, TUI). kickoff/pause/
                         resume/is_running are the API/CLI entry points. `kickoff(fresh=True)` is
                         the FROM-SCRATCH build: empty the game folder, then seed. Only the button
                         asks for it — a plain re-trigger, a resume and a fix all carry the folder
                         forward on purpose. It snapshots before deleting and preempts the art the
                         last attempt is still waiting on, since a render in flight would land in
                         the new folder and write itself into a manifest that never asked for it.
                         Without it a second attempt opens on the dead build's half-written files:
                         the model reads them, believes them, and re-asks for art it already has
                         under new ids (measured 2026-08-01: three naming schemes for one cast, 40
                         renders, no finished game). `pause` parks the game at whatever stage it
                         is in (the owner over their own runs, an operator over ANY —
                         `routers/admin.py`): every pending job on every queue is cancelled, the
                         queue admits nothing new for a paused game, a claimed turn is left to its
                         worker and applied when it lands, and whatever is playable is staged so
                         it can be played meanwhile. `resume` picks it up in the stage it stopped
                         in: a design starts again, a build re-sends its turn, checking runs the
                         gates again. Gates run on a thread of the control plane, so a restart
                         re-runs them for every game left running in checking (`recover`). The
                         reaper re-drives a running build with no job in flight (one feed line
                         every 5s, forever). A refused compute budget PREEMPTS the run's
                         still-pending asset jobs before giving up — gameplay beats art. A landed
                         turn is ARCHIVED to the run dir before its jobs row is emptied
                         (`turn_log`) — append first, so there is never a moment with neither copy.
      build_steps.py     the turn MACHINE: step(spec, run_dir, tools, cursor, result, error)
                         -> Infer|Done.
                         A `result` of None is NO TURN TO APPLY (a resume, a reaper re-drive) and
                         re-asks from the transcript as it stands; `{}` is a turn that ran and
                         answered with nothing, which the nudge branch handles; an `error` is a turn
                         the WORKER could not deliver, which is neither. Collapsing any two of the
                         three scolds the model for a reply it never sent and burns a turn.
                         Owns the ONE tool schema — the model writes a PROGRAM and `pyexec.runner`
                         runs it — the LEDGER of what that program called (which is what the feed
                         line, the repeat note and the `done` signal are all read off, since a
                         program's calls are not in its arguments),
                         the seeded opening every build starts from (two programs from
                         `prompts/build_seed/` run for real, then its nudge; kept right after the
                         request through every compaction, labs/open-shim),
                         the transcript (each turn's thinking kept until the next cut, because the
                         server's cache is the sequence as generated), compaction (`CLAUDE.md`; the
                         note it ends with carries
                         `code_map`, and replaces the previous compaction's note rather than
                         stacking a stale one beside it), and
                         every way a reply TOO BIG TO LAND arrives. A reply cut off before any tool
                         call is DISCARDED and the turn re-sent for the whole window, carrying nothing
                         into the transcript, and compacted first when the transcript is past the
                         keep fraction, since that is what shrank the room the reply ran out of; the other two are answered with the one remedy (write it in
                         pieces) because the model can act on them: the call whose ARGUMENTS stop
                         mid-write (unreadable, so nothing ran — reported as the truncation it is,
                         never as the missing `path` it parses to), and the 500 the inference
                         server's own tool-call parser returns for an oversized call. Measured 2026-08-01: one 64 KB
                         write_file reported as `KeyError: 'path'`, resent identically, four server
                         500s scored as silence, a dead build at step 28. A failing call resent with
                         identical arguments is COUNTED and the count told back; what to do instead
                         is the model's call. That count is keyed PER CALL, not against the previous
                         one: a model stuck on an edit re-reads the file between attempts, and a
                         succeeding read between two identical failures must not clear the ledger
                         (measured 2026-07-29: 12 identical failing edits, each scored as the first).
                         A read reaches the transcript as the FILE (`<file path=…>`), never
                         serialized — a JSON body shows every quote as \" and the model copies that
                         into old_text, where it matches nothing (measured 2026-07-28: 17 of one
                         build's 32 turns, resent byte-identical).
      build_state.py     the durable build CURSOR, on the build's own row (builds.cursor) — step
                         count, the growing transcript, and the
                         read→edit tool grounding, rehydrated into build_tools each completion (a
                         fresh process would else refuse a resumed edit). Job metadata carries only
                         {stage,run_id,build_id}; the row is the single copy the completion
                         reloads, advances, rewrites. Where the game stands is never here.
      turn_log.py        the run dir's own copy of the conversation (runs/<id>/turns.jsonl), and
                         the only one that lasts: a turn's request IS the transcript so far, so a
                         payload per jobs row stored the same conversation once per turn (951 MB of
                         `jobs.payload`, 26 MB for one 117-turn build). The system prompt and the
                         eight schemas are byte-identical every turn, so they ride ONE `meta` record
                         and a `turn` record carries only what that turn ADDED — turn k is
                         `system + tools + concat(added[0..k])`. A `compact` record carries the
                         rounds `build_steps.compact` trimmed to stubs and dropped, and the note
                         it ended with; the thinking drop, the newest-copy-wins pass and the
                         read-only-round drop are
                         functions of the transcript alone, so a replay shows what was really
                         sent. A FIX appends its own meta and never truncates.
      file_state.py      what the build has READ and whether it still says the same thing: every
                         read records the whole file's digest, and a compaction's note lists the
                         files that are byte for byte what the model was shown. Rendered at the
                         cut, never per turn — a per-turn block changes the prompt prefix and
                         costs more in lost cache than the read it saves.
      code_map.py        the project as one message: every source file, what it imports, and each
                         declaration — exported or private, and the functions one level inside it
                         — with the line range it occupies. Regex over JavaScript, no parser; a
                         file wrapped in one column-0 closure is mapped one level in. A markdown
                         file is mapped by its headings with their ranges, so the design's
                         sections survive a compaction as `read_file` calls rather than a whole
                         re-read. Art, `world/` and the vendored renderer are left out. It is what the
                         compaction note re-grounds the model on, and with `read_file`'s `offset`
                         and `lines` it turns "read the file" into "read lines 68-120".
      archive.py         the run dir's OFF-BOX copy. tools/s3.py is a minimal SigV4 client over
                         `requests` — a wrong signature is a loud 403 and the payload hash rides
                         the request, so a signing bug cannot silently succeed. A settled finalize
                         (no gate fix kicked, not paused) uploads the run dir as one
                         tar.gz,
                         minus a world's `world_build/` — the world a game plays was published
                         into the game folder, and the stages' working material is hundreds of
                         megabytes nothing reads back. `evict` reclaims local disk and REFUSES without a verified remote
                         copy or under an active build; `rehydrate` pulls it back and re-stages;
                         `ensure_local` hooks play-session/build/change so an evicted game is a
                         download away. A boundary like snapshots: an unconfigured bucket logs and
                         stands aside.
      snapshots.py       the game folder's HISTORY, in git: `runs/<id>/game.git` is the repo and
                         `game/` its work tree, so nothing appears inside the folder the model
                         lists, staging copies and /play serves. A commit at every finalize that
                         produced a playable game, and one before a fix edits — a fix re-edits code
                         that already worked, so the last playable version is exactly what it can
                         destroy. Everything in the folder is committed, art included: a restore
                         that leaves half the game at another version is not a restore. git is a
                         BOUNDARY — a snapshot that cannot be taken is logged and the build carries
                         on, since losing history is not a reason to lose a game.
      pyexec/            where a model-written PROGRAM runs. `runner.run(code, tools, root)`
                         answers with what it printed, the ledger of what it called, and whether
                         it was refused, raised or ran out of time. The program works in the game
                         folder with real files: open(), os, pathlib, glob and shutil reach it and
                         nothing else (labs/open-shim).
                         landlock.py — the ruleset a process puts on ITSELF: the game folder read
                         and write, Python's own library read only, every other path refused.
                         seccomp.py — the filter a process installs on ITSELF (`prctl`, via
                         ctypes, no dependency and no privileges): exec, socket, clone, mount,
                         links, chmod, signals and the rest denied outright. Opening and changing
                         paths is left to Landlock; renders, art and the other tools go to the
                         parent.
                         child.py — imports first, Landlock and the filter second, model code
                         third, in the game folder; a process that cannot be confined exits rather
                         than running the program free.
                         rpc.py — length-prefixed JSON over one socket, one call in flight.
                         runner.py — spawns the child with a scrubbed environment, serves each
                         call from `build_tools`, caps the calls and the wall clock, and runs
                         `check` first as a courtesy, never as the boundary (`CLAUDE.md`).
      tools.py           list_files / read_file / write_file / edit_file / generate_media /
                         compose_world / check_syntax / play / check_off / done — the smallest surface that works,
                         and kept that way. They are FUNCTIONS the program calls, so a failure
                         comes back as a value it can read rather than an exception that abandons
                         the rest of the program. `read_file` returns the WHOLE file however long:
                         the ceiling was a context guard, and a read lands in a variable, not the
                         window. `check_syntax` is the error gate's own module parser per file —
                         node reads `export` in a `.js` as a CommonJS failure and answers "retry as
                         a module" with status 0, so a game checked as a script is always clean —
                         and it resolves every local path a file names, index.html included, so a
                         `./lib/audio.js` written from a subfolder is reported rather than shipped:
                         the file parses, and a browser reports the missing module on the console
                         instead of as the uncaught exception the error gate watches for. Art still
                         queued for rendering is not a missing file.
                         `play` loads the game in the error gate's own headless chromium — same
                         ephemeral server, same dead proxy, same pageerror wiring — waits for
                         `window.__game`, runs the program's JavaScript in the page and hands
                         back what it returned with every uncaught error and console line that
                         landed meanwhile; `await __press(key, ms)` inside it holds a real key,
                         so a check reaches the game through its keydown handler rather than
                         around it. It runs in its own process (`play.py`) that is killed at the
                         deadline, because a page that never yields hangs the driver too, and the
                         thread serving the call is the control plane's. It answers what the page
                         DID, never whether that was good — the tests it runs are the design's own.
                         `check_off` ticks definition-of-done rows (a list, or one) in the game
                         folder's design/design.md and answers with what is still unchecked:
                         the record of what is finished lives on disk, where a compaction cannot
                         take it. Bookkeeping only; it never looks at the game, and `done` does
                         not consult it.
                         `done` does nothing but carry its summary: the driver reads it off the
                         ledger.
                         compose_world is worldgen's build face: one 3D world per game, refused
                         a second time because the game is already written against the first
                         one's metres and regions. It ANSWERS AT ONCE with the path the world
                         will appear at and builds behind the call, the way generate_media does,
                         because a program's wall-clock budget is 120 s and a world's ground
                         alone takes about fifteen minutes — a tool that waited could only be
                         killed mid-call, and the code to load the world never written (measured
                         2026-09-10). A placeholder flat world is written before the answer, so
                         `loadWorld` works the moment the model writes it; each of the four legs
                         publishes over it by rename. A path is resolved and must land inside the
                         game folder. Every failure is REPORTED to the model as text (a missing
                         argument names itself, and says what the call DID send) and never guessed
                         at: substituting a default for a missing `path` sent every write in a run
                         to one file. What an argument must BE is declared on the signature, so
                         every problem in one call is reported together — a model that learns one
                         bad argument per turn spends a turn on each.
                         `read_file` on a missing path the manifest lists as requested answers
                         PENDING — keep the path, do not re-ask — never "no such file": the
                         error reading as a failed ask made a build re-request its whole set
                         under "-v2" ids (11 duplicates, 2026-08-03), and ok=True keeps the
                         repeat ledger from scolding a legitimate second look.
                         `read_file` returns a WINDOW of whole lines from a 1-based `offset`. Both
                         halves are load-bearing: a window cut mid-line is text the model cannot
                         reproduce and it copies the cut into old_text, and without `offset` the
                         tail past the ceiling is unreachable, so an edit there can never land
                         (measured 2026-07-29: 12 byte-identical edits, 248 of 249 chars matching,
                         the 249th the cut). A window that stops short says where the rest is and
                         that a file this size is worth splitting.
      staging.py         where a game lives (runs/<id>/game/) and how it reaches the browser: copy
                         the folder to runtime/games/<run_id>/. No bundle, no transform. The copy
                         is built BESIDE the live one and swapped in by rename: the live one is
                         what someone is playing, and deleting it first 404s every request during
                         the copy and — when a render lands mid-delete — fails half-way, leaving
                         the played game with some of its art gone (2026-09-10: 31 of 55 assets).
                         `has_authored_files` discounts the seed, so it says whether a BUILD wrote
                         anything — which is what offers the from-scratch button. Also the SEED
                         (seed_vendor): the game folder starts holding the vendored renderer and
                         `lib/` and nothing else, since everything placed there steers the
                         first list_files. It runs on EVERY kickoff and never overwrites.
      assets.py          the ASSET stage — `request_media` is what the game's generate_media call
                         runs. The ask is STRUCTURED: `kind` (sprite, actor, tile, scene, mesh),
                         `subject` (prose, no style words), `style` (the game's one phrase) and
                         `details` — the facts prose cannot carry: body plan and view.
                         `compose_prompt` renders them to the one prose string the samplers see
                         (style, subject, view), so where the style sits is a measurable edit
                         here rather than whatever the builder typed, and `render_kind` draws an
                         actor as a sprite. Then: enqueue ONE `comfy`
                         job, record the ask in assets.json, write a
                         PLACEHOLDER at the path (a matted disc for a sprite, an opaque frame for
                         a tile or scene, none for a mesh) and answer with the path. The game
                         never loads a file that is not there — a `drawImage` of a broken image
                         throws every frame — and the manifest's `placeholder` flag, cleared by
                         the landing op, is what "rendered" means everywhere a bare file check
                         used to (`landed`). Nothing plans, rewrites or inspects the game's source. `kind`
                         picks the model, the workflow and what a landed render owes: sprite is
                         matted and autocropped because the game draws it ON its own background,
                         scene keeps the whole frame because it IS the background, a tile is
                         quilted seamless (`tools/quilting.py`, in `asset_chain._save_flat` —
                         soft, a quilt that throws saves the raw render), mesh chains image →
                         TRELLIS.2 on the same ComfyUI box (its image leg is a Qwen-Image subject render, framed and
                         matted, in a hand-painted game-asset style the player never sees —
                         TRELLIS lifts a stylized cut-out far better than a photo or an anime
                         sprite, `docs/experiments.md` 2026-09-03) and lands normalized to 1 unit at its longest side, which the
                         tool's answer states: placement code cannot discover scale any other
                         way, and an untold model shipped a knee-high lighthouse. Rendering every
                         kind through one item-icon path matted a game's floor tiles down to a
                         handful of planks — it said "tile" in every prompt and nothing could
                         hear it. A refused compute budget is answered as "draw this one with
                         code instead", never left as a path that will never fill. `check_render` reads the alpha of what landed against the kind
                         that was asked for and records a `defect` on the manifest entry: a matte
                         that never ran, one that ate the subject, a background full of holes. It
                         may only find BROKEN — whether a picture suits the game is the same human
                         question as whether the game plays right, so the gallery SHOWS the defect
                         and nothing acts on it. An already-rendered file, a blocked prompt
                         and a refused budget are all answered, never retried blind. A REPEATED id
                         is answered ONCE and then obeyed: the first repeat
                         says "not requeued, call again to replace it", the second re-renders and
                         rewrites the entry's prompt. Silently keeping the old picture was worse
                         than either, because "redraw these" got agreement and no new art.
                         Also owns start_from_manifest (the TOP-UP: re-render what the record says
                         is still missing) and the single-asset regenerate — the user's text is a
                         CHANGE NOTE merged (one small llm call) with the entry's original prompt,
                         so "give him a red cape" keeps the goblin; img2img seeds from the render.
                         A top-up RESUMES a mesh from its `<id>.src.png` if one is there, so the
                         subject render already paid for is not drawn again.
      design.py          the DESIGN STAGE — decisions, then one writer, between the user's words
                         and the build. One `decide` job asks JEV (a calibrated decision model, its
                         own queue) every decision in prompts/design/decisions.json in order, each
                         answer appended to the state the next question sees: the frame (2d/3d,
                         camera, time, movement, session, failure, tone, size, endings), yes/no on
                         every mechanic in prompts/design/mechanics.md, how sure JEV is that the
                         first playable version needs each kept mechanic, then look and sound. The
                         image runs the chain without knowing what a game is. The kept mechanics are
                         banded on the first-playable probability (core >= 0.6, supporting >= 0.3,
                         extra below) and one llm call (write.txt) writes the spec from the banded
                         state and the ask, with fixed sections (scope with tiers, conventions,
                         contracts, visual, gameplay, characters, audio, UX, debug API, tests, build
                         order, definition of done, sanity); tier 1 is exactly the core mechanics,
                         already in the chosen art and sound. A reply that stops before the last
                         section is continued (write_continue.txt appended to the same prompt with
                         the text so far), up to three times. The decide result is kept as
                         runs/<id>/design/decisions.json; the spec is runs/<id>/design.md, seeded
                         into the game folder as design/design.md beside docs/ (the generate_media
                         and compose_world guidance). Neither folder is published: they are ours, not
                         the game's. `request` is the two-line prompt (prompts/request.txt) naming
                         the ask and the spec; it is absent while the design is pending. Web: the
                         decide job then the write job, tagged metadata.stage="design" and .step,
                         each completion enqueuing the next; the last writes `request`,
                         emits `prompt_proposed` and KICKS OFF THE BUILD — no human reads the design
                         first. A step that comes back empty is asked once more; one that fails, or
                         comes back empty again (a decide job that fails, twice), writes `request = ask` and builds on that. A kickoff the budget refuses leaves the run designed and
                         idle, and the page's Build button retries it. CLI: the same chain,
                         synchronous, and the build is the CLI's own next call. The
                         create call is where the game is charged — the credit, and the
                         compute-seconds grant the queue meters against (`db_store.charge_game`,
                         through `tools/execution_context.run_scope`): the design is the game's
                         first inference, on a build row of its own (`kind=design`), and the
                         build that follows never charges again. Measured 2026-08-26
                         (`docs/experiments.md`): the model's design of a plain request built a
                         game "a million times better" than three stages of the request itself.
      error_gate.py      the ERROR GATE — after a playable finalize, open the run dir's `game/`
                         (what was just staged) in a headless browser (playwright chromium, an ephemeral static server),
                         screenshot it, ask the model what the title screen offers
                         (prompts/probe_targets.txt: buttons as pixel centres, keys the screen
                         names — at most MAX_TARGETS of each, one llm turn at low effort,
                         charged to the game), press each on its own fresh page, and re-enter
                         the fix machine with the FIRST uncaught error and its address. A model
                         that cannot be asked or answers off-shape leaves the fixed poke (one
                         click at the viewport centre + Enter + Space). The address — a script the page asked for and did not get (an import that
                         404s, which stops the module graph without throwing) counts as one, with
                         the path as its address. One error per fix
                         build; node --check supplies the file:line a browser SyntaxError omits
                         (tried as module then script — authored games import three); a
                         redeclaration note lists every declaration site; a parse note says fix
                         the whole file. Stops on MAX_ROUNDS, on the same error two rounds
                         running, and starts no fix once the game is paused. A probe that cannot
                         run logs and stands aside — the gate is a boundary like snapshots.
                         Round state in runs/<id>/error_gate.json.
      play_gate.py       the PLAY GATE (branch experiment) — after the error gate is clean, play
                         the staged game in the same no-egress headless browser: one persistent
                         page, one session of MAX_TURNS llm turns. Each turn the model sees the
                         current screenshot, presses ONE input (key, click, or a held key; a tap is
                         held TAP_MS, because a 0 ms press never shows to a game that polls whether
                         a key is down), states
                         the VISIBLE change it expects, and next turn says met / unmet / unclear
                         about its own prediction (prompts/play_turn.txt — the design rides along
                         so the model knows the documented controls). The session's page also
                         hears `pageerror`, reusing the error gate's own dedup and file:line
                         address — an error is attributed to whichever turn's input was last
                         pressed when it arrived, or to "load" (logged only, attached to no turn)
                         when it lands before turn 1. A closing call (prompts/play_verdict.txt)
                         folds the session into runs/<id>/play_report.json: `broken` — inputs it
                         pressed and WATCHED fail, biased to under-report (unclear is not broken,
                         failed-once-worked-later is not broken), PLUS one fact for every turn that
                         threw regardless of what the model's own summary says, carrying the
                         error's message and address — and `judgment`, a written impression that
                         only ever reaches the human, never a fix note (a grade as a fix note made
                         nothing better — experiments.md 2026-08-08). One broken fact re-enters the
                         fix machine per round (prompts/play_fix_note.txt, which states what the
                         page threw and where when the fact carries an error); a thrown fact is
                         preferred over one without, since the exception alone satisfies the
                         guardrail. Stops at MAX_ROUNDS or the same fact twice running. A session
                         that cannot run logs and stands — boundary, like the error gate. Round
                         state in runs/<id>/play_gate.json.
      asset_chain.py     what a finished asset job does NEXT — the names in its `metadata.then`: a
                         CONTINUATION to enqueue (mesh_from_image), OPERATIONS on
                         this result (save_sprite / save_flat / decimate), and the
                         batch's FINALIZE. It owns those
                         names so the queue stays a generic transport that never learns what an
                         asset is. Two finalizes: `assets` lands the art and leaves the build's
                         row to the build machine (a build turn's generate_media), `art_build`
                         lands it and ends the row, for a batch that IS its build (a top-up, a
                         regenerate). Every save runs the SAFETY policy first (`_admit`): the worker
                         attached NSFW scores to the render, `assets.render_verdict` decides, and
                         a refused (or verdict-less) render is deleted, marked on the manifest and
                         recorded as a violation — a refused mesh source never reaches TRELLIS.
      artifact_screen.py the artifact TEXT gate — the game folder's authored text through the same
                         narrow screen as every input seam, at finalize before staging. A hit
                         HOLDS the build: status `held`, not staged, not archived, play/build/change
                         refused, a neutral message to the owner, a row in the violations table
                         for the admin panel. The image side of the same policy is asset_chain's
                         `_admit` above.
      prompts/           build.txt (the one system prompt every build turn reads),
                         request.txt (the build's user message), design/ (the decide chain, the
                         mechanics catalogue, the writer and its continuation suffix), play_turn.txt / play_verdict.txt /
                         play_fix_note.txt (the play gate's session, closing report and fix
                         note), probe_targets.txt (the gate asking
                         where to press), error_gate_note.txt (the fix note an
                         uncaught error becomes).
      run.py             create_run / propose_prompt (the designer, synchronously) / set_prompt /
                         run_build (CLI: kickoff +
                         block-poll the game's status) / change_from_note + the CLI. The web build/change path is
                         fire-and-forget through build_chain.kickoff, not run.py.
    services.py          parse_args — any argument shape a local model returns → a dict.
                         parse_args_checked also says whether anything was READABLE: a call cut off
                         at the output cap and a call that carries no arguments both answer {}, and
                         only one of them is a failure.
    tool_calls.py        recovering a tool call the model wrote as TEXT, when the server's own
                         parser didn't claim it. A registry of encodings (Hermes/Qwen, DeepSeek,
                         Mistral, Llama python-tag, two XML forms, named object, arg-shape), tried
                         strictest first. A parser only wins if EVERY call it found names an offered
                         tool and carries that tool's required arguments.
    state.py             RunState — durable per-run dir <working_dir>/runs/<run_id>/ (spec.json —
                         `ask` the user's words, `request` what builds — and the game/ folder). Ownership + charge state live in db/, not the run dir.
  worldgen/              the 3D world behind the compose_world build tool: the worldclaw pipeline,
                         stage for stage, with every GPU call on maestro's queues. build.py runs the nine stages in order — scene, terrain-plan,
                         terrain-assets, construct, terrain-refine, regional-plan, objects,
                         scene-refine, final-render — and `start_at`/`stop_after` make any run of
                         them resumable, which is what lets one world be built in legs. planning/
                         and terrain/ and objects/ are the stages themselves; llm.py, backends/
                         images.py and backends/meshes.py are the only ways out to a card, each a
                         job on the llm or comfy queue. Each stage names its own sampling
                         temperature (the grounding pass is greedy, the planners are not) and its
                         own mesh seed, and both ride the job all the way to the server.
                         terrain/render.py is both the refinement loop's eyes and the contract:
                         it writes world.json and draws it in headless chromium through
                         runtime/vendor/world.js — the SAME loader the game imports, so the agent
                         never judges a picture the game cannot reproduce. Every path in
                         world.json is relative to the folder it sits in, which is what makes the
                         folder copyable into a game.
                         Each region is covered in a PAIR of squares, not one: the surface the
                         plan names and the variant it wears through to — grass to bare earth,
                         sand to cracked clay — both drawn as ground seen straight down (asked
                         for a lakebed, a diffusion model draws a lake, far bank and all, which
                         then tiles as a lattice of little lakes) and both quilted seamless by
                         `tools.quilting.quilt_tile`. The shader blends the pair by a fixed
                         recipe in world metres — low-frequency noise for the patches, slope to
                         carry a steep face over on its own — with the threshold above the
                         noise's mean, so a pasture reads as pasture with earth in it and not the
                         reverse. Nothing about the recipe is per-world: world.json carries the
                         two paths and one scale, and a region with no variant draws its base
                         alone. The ground is a MeshStandardMaterial with the splat injected into
                         its shader rather than a shader of its own, because a raw shader
                         receives no shadow map, fog or tone mapping — the sun has to cast onto
                         the terrain as it does onto everything else. On top of the blend the
                         material derives rock on steep faces, snow on the highest shallow ground
                         of regions the plan called high, and a two-scale albedo mix that stops
                         one tile reading as one tile over eight hundred metres — all from the
                         height field and the material already there, so none needs an asset or
                         a plan field. The stochastic tiling taps textureGrad on the unbroken
                         UV's gradients: a per-cell offset jump otherwise picks the smallest mip
                         and draws a dashed lattice over every close view.
                         compose.py is the build face. The tool BLOCKS through `construct` — the
                         first stage at which a world exists to write at all, since world.json
                         names the height field, the region materials and the scatter — and
                         answers with size_m and the regions in metres, because the model places
                         gameplay by region the turn it hears back. The rest runs on one thread
                         for that run, in three legs (terrain-refine / regional-plan..objects /
                         scene-refine..final-render), and PUBLISHES after each.
                         The publish contract: `<run_dir>/world_build/` holds the intermediates
                         (~275 MB of concept images, region compositions and refinement views) and
                         no game ever reads it; `<game>/world/` holds only what world.json names —
                         the json, heightmap.f32, the weight textures, each region's two albedos
                         and their normals, and the GLBs — at the same relative paths, so staging, archiving
                         and playing carry the world with the game and the game fetches nothing
                         outside its folder. world.json is rewritten from the world as it stands
                         at every publish and replaced by rename, so a browser never reads one
                         naming a file that is not there yet. A world with no meshes yet still
                         COLLIDES: the loader reads collision from stated heights, never from a
                         GLB. Nothing here blocks `done` — a build finalizes on a game whose
                         ground already loads while the scenery is still rendering — and a leg
                         that fails is logged and left, because the pipeline resumes by stage.
                         Locally the three queues are drained by `scripts/local_gpu.py auto`
                         (`docs/local_dev.md`).
```

The rest of the platform is build-path-agnostic: `auth/` (identity, bearer sessions,
open signup — the only self-serve account path, per-IP throttled — the
/play handoff + per-game grant-cookie gate, `playgrants.py`; an account carries an EMAIL, which
exists to recover it: `/auth/forgot` mails a single-use link that answers identically for an address
with no account, and setting a password ends every session the account has), `billing/`
(the credit ledger, the packages a credit is bought in, the Stripe provider, and what a job is
estimated to cost), `db/`
(games/builds/events/jobs/workers + the compute budget), `workqueue/` (the enqueue-side client
and the queue reaper), `worker/` (the pull-side GPU worker),
`scaler/` (the RunPod autoscaler), `api/` (FastAPI routers), `llm_clients/`, `tools/`, `config/`,
and `frontend/` (the React SPA, served same-origin by the API).

---

## Where inference happens

Only inside a build, as a chain of `llm` jobs, with art riding the `comfy` queue off the same
transport: sprite, tile and scene renders, and meshes through TRELLIS.2 on the same ComfyUI. The request paths, the completion dispatch and how
a new stage registers are in `docs/architecture.md` § Request paths.

---

## The art ledger

`generate_media` entered UNMEASURED (2026-07-28) and is still unsettled — art is the one capability
no `write_file` can stand in for. Counted over the 35 staged games (2026-08-01): 19 called it at all,
308 asks, 27 of them meshes across 6 builds — and the asks RATION. One 3D village asked for five NPC
portraits and a lighthouse while building five shops, a farm, lamp posts and every interior out of 27
code primitives. So the prompt does not tell the model to draw a plain shape at the spot, and the tool
does not price a render in minutes: both frame art as a thing that might not arrive, and a model
that believes that draws a prism and moves on.

What settles it is COVERAGE — how much of what the player sees got art — not call-at-all. A build
whose art never lands renders its placeholders (2026-08-24: before them, a card game drew a
never-landed webp and was black every frame).

Coverage, counted (2026-08-01, a static audit over the 35 staged games — since removed with the
done nudge it rode on): 352 assets asked for,
**128 rendered and never referenced by the game's source**, and **115 `assets/…` paths referenced
that were never asked for**. One ghost game asked for all 11 of its assets before writing a line of
code, wrote the game with 89 canvas primitives, read its own `assets.json`, and loaded none of them.
One card game rendered 67 and used none, while shipping 114 paths under an `assets/cards/` folder
that does not exist. The tool answers with a path and never learns whether the path was used, so
until the audit nothing in the loop could see either half. Orphaning is NOT the recent prompt edit: a
build carrying the older "draw a plain shape at that spot" line orphaned 7 of its 8.
