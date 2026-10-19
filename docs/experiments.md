# Experiments — what has been tried on the build loop, and what it measured

A record of shapes that were tried against real builds and the numbers they produced. An entry
belongs here once it has been RUN; a shape that has only been argued about stays out of the repo.

The rule this file exists to serve: **measure before changing the loop.** A change that cannot point
at a row here has not earned its place.

Experiment harnesses, battery runners and renders live in `labs`, never in this repo.
What lands here is the number; what lands in `src/` is the change that earned it.

---

## The design stage (2026-08-26, local 5090, qwen3.8_27b nvfp4 via ninfer, 131K window int8 KV, thinking on)

### The question
The 2026-08-24/25 rows say a build makes the design it is given, and that the designs that built
were hand-written. Can the model write the design itself — the designer prompt
(`prompts/design.txt`) on the plain request — and does the result build as well as a human's? One
request for every arm, "Make me an f1 racing game.", the owner playing every result. All n=1.

### The builds
| arm | turns | wall | outcome |
|---|---|---|---|
| prod shape: plain request, staged construction (3 stages), 65K window, 50K per-turn cap | 27 + 7 (gate fix) + 28 + 91 = 153 | — | unplayable: the track was spaghetti from stage 1 |
| hand-written 1,137-word systems design, 50K cap | stopped by hand at 58 | 31 min | turns 0, 1 and 2 each ended at the cap with no tool call |
| same hand design, cap = window − prompt − 6K | 24 | 24 min | gate clean; closed circuit, pit branch, rivals, tyres; "extremely shallow" |
| model-written design (1,388 words; 67,949 tokens / 7 min to write), same cap | 58 | 51 min | gate clean; race select with three circuits, unlock ladder, 6 rivals, 5 laps, weather, three tyre compounds, fuel, pit box, damage, podium and classification; "a million times better" |
| model-written design, `llm.reasoning` = medium (1,290 words; 2,947 tokens / 33 s to write) | 20 | 19 min | gate clean and WRONG: the game crashes on its start button, the track generator returning null (below). Season of 5 seeded circuits, 8 racers, sector times, gears, championship points |

### What the plain request did
One Catmull-Rom sampler with three bugs (segment index confused with the parameter, a wrong t³
coefficient, a wrong tangent derivative) drew the track. The original wrote `CP[i % N]` and crashed;
the error gate's fix round changed it to `% M` — the crash gone, the geometry still wrong, which is
the gate's contract: it detects broken, and a track that draws is not broken. Stages 2 and 3
(rivals, tyres and pit) then built 120 turns on top of it. Three stages of the plain request bought
nothing a design would not have said in one paragraph.

### What the designs did
The hand design's game is a real circuit and nothing else — no replayability. The model's design
is longer by 250 words and its game has a front end, a ladder and a race weekend. Owner: track 2
"pretty good", controls need work, and the rivals do not move — their lap counter fires at the
start line, because the checkpoint radius surrounds a spawn at (0, 0). Two weaknesses in the
model's design, both open: its numbers carry no units (`topSpeed 240`), and its track paragraph
was its shortest, where the hand design gave the track a third of its words and its track was
better. The designer does not yet know which system is the hard one.

### Numbers are given, never derived
A clause the builder must DERIVE — "no bend tighter than the car can drive at pit-lane speed" —
sent it into ~150K characters of cornering-physics derivation per turn. The same clause as a value
("no turn radius under 140 px") plus the design's opening sentence, "Every number below is a given
value to write into the code as-is; none of them needs checking or deriving", removed it. Both are
in `design.txt`.

### The cap rides the window
The model writes the whole game inside its think and verifies it there before the first tool
call; the 50K-cap arm's tails show it 97%, ~100% and 74% of the way through when cut, one of them
deciding its first tool call. The stall threshold is 4, so the build survived and continued, at
five minutes a retry with nothing kept between them — a capped think re-thinks from zero. With the
cap at `n_ctx − prompt_estimate − 6K` (floor 16K; `build_steps`), turn 0 stopped on its own at
46,841 tokens, turn 1 (track.js) took 68,335 — a dead miss at 50K every time — and every later turn
was under 15K. The 6K margin is ninfer's admission rule: a request is accepted only when prompt +
max_tokens fits `--max-context`.

### reasoning_effort
ninfer's chat endpoint takes `reasoning_effort`, and the Qwen3.8 template exposes `none`, `low`,
`medium` and `xhigh` (`high` answers `reasoning_effort_not_supported`). The chat wire forwards the
canonical `reasoning` as `reasoning_effort`, and the build's effort is `settings.llm.reasoning`
like every other call.

`medium` buys the design for a twenty-third of the tokens: 2,947 against 67,949, 33 seconds against
seven minutes, for a design of the same length and MORE specific (it named its own checkpoint
indices, a rubber-band rule, and a points table). The build that followed was 20 turns against 58.
What it did not buy is a working game, and one arm each is too thin to say whether that is the
effort or the roll.

### A verifier can be impossible, and nothing says so
The medium design asked its track generator for "minimum corner radius of 40" and a re-roll on
failure. The build implemented the circumradius as `6·|cross| / (a·b·c)` — the reciprocal, a
CURVATURE — so a smooth 1,000-unit corner scored 0.003, every seed failed, and `generateTrack` fell
out of its 200 re-rolls returning null. The first screen reads `track.points` and the game dies on
its start button. Corrected to `a·b·c / (2·|cross|)`, 95 of every 200 seeds pass and the game plays.
A generator that verifies must not be able to return nothing: what the design owes is a floor —
the best candidate when the checks cannot be met — and what the harness owes is a gate that presses
the button.

### The card
131K of int8 KV is 9–10 GB over the 21.5 GB of weights, ~70 KB per token. 1M of context would need
~70 GB; ninfer does not offload KV to host RAM, and over PCIe 5 x16 it would run at ~8 tok/s if it
did. The window is at the card's ceiling.

### The gate pokes where the button is not
`error_gate` clicks the viewport CENTRE and presses Enter and Space — a canvas-drawn "press to
start". The medium build's title screen is two DOM buttons below the middle, so nothing the gate
did reached the crash, and a game that dies the instant a person presses START passed as built.
The gate stays a BROKEN detector either way: pressing the page's own buttons is mechanical, and
what happens after is still the human's to judge.

### What's open
Units in the designer's numbers. Getting the design's words onto the hardest system. Whether
`medium` costs quality or only tokens — one arm each says nothing. The rival checkpoint bug is a
design hole — the design placed no start line — as much as a build one.

---

## A 2K-word design, and the window it needs (2026-08-25/26, local 5090, qwen3.8_27b via ninfer, thinking on)

### The question
The systems designs of 2026-08-24 were 500–700 words. Does a much longer one — a top-down RPG in
2,139 words: four seeded-generator areas with exits between them, turn-based battle, three staged
quests, shop, save — build at all, and what does it need from the harness?

### The builds
| arm | main build | gate | GPU min |
|---|---|---|---|
| 65K window, bf16 KV | 110 turns, 380K gen, 134 reads / 75 edits / 13 writes; 29 compactions (787 rounds trimmed, 101 dropped) | clean on the first probe | 76 |
| 131K window, int8 KV | 61 turns, 225K gen, 32 reads / 43 edits / 12 writes; 2 compactions (80 trimmed, 0 dropped) | 3 fix rounds: 4 syntax errors, then `rnd is not a function`, then `undefined.cat` | 53 + 58 |
| 131K, "drawn in code" removed from the design | hit the 120-turn cap, no `done`; 122 KB of JS, parses and loads clean; ZERO `generate_media` calls | (a capped build is not gated) | — |

Both finished games are real RPGs of the design — four areas, exits, battles, quests — and the
owner's verdict on the first: "missing a bit, but surprisingly way better than I expected."

### What the window did
Same design, same model: half the turns, a quarter of the reads, no dropped rounds. At 65K the
~128 KB codebase (~35K tokens) is over half the window, so the model's contract-verification
passes became read → stubbed three rounds later → re-read; 60% of its turns were reads, and each
sweep still found real bugs (a missing `sfx` alias, table keys, `description` vs `desc`). At 131K
the codebase stays resident and the sweeps are short. int8 KV is what fits 131K on the card
(bf16 is 450 MB short), a confound this run did not separate from the window itself.

### What compaction did
Trimming file bodies before dropping rounds (`build_steps.compact`) carried the 65K build through
turn 40 with no round dropped where the old shape would have cut the transcript in half three
times. It cannot beat a codebase larger than the window: past that point the read-stub-reread
cycle is the cost, and the fix is the window, not the trim.

### The gate had two holes
Both surfaced on the 131K build's first fix round, which got "(no location available)" for four
syntax errors it then spent 23 turns reading for:
- `node --check` only ran on `*.js` at the game root; both RPG builds kept code under `game/`.
- node 22 module detection: `--check` on a `.js` file containing `export` retries as ESM and
  reports a PASS, so the "both parsers refuse" rule never fired on module code. `.cjs` forces the
  script parse. With both fixed the note named all four files and lines; the fix took 24 turns.

### Art was not asked for
Deleting "drawn in code" from the design did not make the model ask for art: it wrote a sprite
module and drew everything in code, as the art ledger (`docs/build_path.md`) predicts. A design
that wants art has to name it as art. Open.

### The cap
The art arm reached 120 turns writing sensible code across 17 files in 7 folders and a final
verification sweep, not looping. With thinking on and a design this size, 120 is a working
ceiling, not a runaway guard. Open: raise it, or measure what the last 40 turns bought.

## Thinking on, and what the request is made of (2026-08-24/25, local 5090, qwen3.8_27b via ninfer)

### The questions
Three, run in sequence on one six-request battery (top-down arena shooter, third-person indoor 3D
dungeon, card battle, village mystery with five NPCs, four-lane rhythm game, three-level
platformer), single-shot, no image or mesh worker, the owner playing every result:

1. Does the DEPTH of the request move the game? Three arms: the plain one-paragraph request; a
   "spec" design (~300 words of what happens on screen); a "systems" design (500–700 words: the
   systems named, every entity as a record with its fields, the content roster with values, the
   screens as a state machine, and every rule stated as a GENERATOR rather than as content).
2. What kills a build that a better request cannot save?
3. Does thinking (ninfer launched without `--no-thinking`) change the game?

### Request depth
Plain 3/6 played, spec 2/6 — noise at n=1, and the spec arm was the wrong question: it described
outcomes, not parts. The systems arm built what was written wherever the harness held: the arcade
came out as designed, the rhythm game's "one audio clock, chart derived from the song's event list"
fixed a bug two other arms could not, the apostrophe rule held. Arm C of the 2026-07-30 depth arms
(the model writes its own `DESIGN.md`) lost because the model's design was thin, not because
design-first is wrong: the ceiling is the design INPUT. One rule of writing them, learned the
expensive way: a design says generator, never content — "maps are hand-written strings" made the
platformer rewrite `data.js` two hundred times and never write the game.

### Harness deaths, and the fixes
Three of six systems builds hit the 120-step cap, all harness:
- **Identical no-op edit, repeated.** Past ~80 steps and a few compactions, a build's last 10–25 turns
  were the same `edit_file` with `old_text == new_text`, which the tool answered `ok`. Now refused
  ("changes nothing"): rhythm went cap→38 steps, gate clean.
- **Art that never lands.** With no image worker the model's `drawImage` of a 404'd webp threw every
  frame — a black game. `generate_media` now writes a placeholder at the promised path (matted disc
  for a sprite, opaque frame for a tile or scene) and the manifest's `placeholder` flag, cleared on
  landing, is what "rendered" means. Cards went black cap→27 steps, playable.
- **Compaction shape.** Trigger 0.62 × n_ctx, keep 0.33: every cut forgets ~half the transcript, and
  the loops above began after the second or third cut. ≥60% of the transcript is file bodies that are
  also on disk. Open: trim bodies of old rounds rather than drop rounds; bigger window.

### Thinking
At the build's 16K per-turn cap, thinking is unusable: turn 0 generated 16,000 tokens of reasoning,
no content, no tool call. At a 50K cap, on the systems designs:

| game | no-think | think | turns / gen tokens / GPU s (think) |
|---|---|---|---|
| rhythm ×2 | notes fall, music unrelated | notes tied to the generated music | 31 / 158K / 1498 · 26 / 109K / 1075 |
| platformer | blank (hand-map design) | plays; one gap unwinnable | 57 / 195K / 2243 |
| npcs | grass grid + crates | village with paths, well, buildings | 29 / 141K / 1396 |
| cards | black | plays after one missing import | 29 / 111K / 1248 |
| arcade | plays | plays, spawn rings, "awesome" | 22 / 82K / 704 |
| indoor3d | wall-facing, dark | first playable one in any arm; A/D inverted | 22 / 113K / 877 |
| farming (new) | — | plays after two one-line fixes | 39 / 178K / 1474 |
| bullet hell (new) | — | plays; damage rarely lands | 40 / 184K / 1638 |

No-think rhythm for scale: 39 turns, 19K tokens, 343 s. Thinking is front-loaded — 25–36K tokens
on turn 0, 18–31K on the next two, then mostly under 3K with the odd 17K spike on a design
decision — and the bigger, fewer turns re-read less (one build never compacted). Owner's verdict:
"a marked improvement from previous where basically only a couple worked at all". Adopted: thinking
on, `MAX_TOKENS` 50K.

### The gate missed two of the deaths
Cards died on PLAY (`startTurn` exported, never imported) and the gate reported clean: its poke
clicked (320, 240), the canvas-drawn button sat at the viewport centre. Farming never ran a line:
`js/main.js` imported `./lib/input.js`, which resolves under `js/`, and a module import that 404s
is a console error, not a `pageerror`. Both fixed in `error_gate.probe`: the click is at the
viewport centre, and a script the page asked for and did not get is an error with the path.

### Not measured yet
The walking sim (`compose_world`) — the world's image and mesh legs need `local_gpu.py auto`,
which cannot yet launch the llm leg with thinking. The stage planner, fix rounds and worldgen's own
llm calls all now run with thinking too, unmeasured.

---

## A helper library beside the game (2026-08-22, local 5090, qwen3.8_27b via ninfer, 6 requests × 2 arms)

### The question

The ladder is prompt line → snippet in the repo → primitive, and three ledger items from the
2026-07-27 grid — 3D scenes lit near-black, silent games, arrow-keys-only input — had each earned
a prompt line at most. The snippet rung had one existence proof, `world.js`: a loader beside the
game with its API in a comment, which a 27B wrote a real game against. Does the same shape move
the three ledger numbers against a control arm with neither the files nor the line?

### What changed

Four plain-JS modules in `runtime/vendor/lib/`, each header comment its API: `input.js` (`keys`
with WASD and the arrows both aliased to up/down/left/right, `justPressed`, mouse, touch),
`audio.js` (WebAudio `sfx(name)`, `tone`, seeded `music`, self-unlocking on the first gesture),
`canvas.js` (`createCanvas` — the FIXED CANVAS line as code on the model's own `<canvas>`, with camera follow and world↔screen),
`lights.js` (`lightScene(renderer, scene)` — sun with shadows, hemisphere, ACES exposure; its doc
says to stay out of a `world.js` world). `seed_vendor` copies them to `<game>/lib/`, and ONE line
in `build.txt` says they are there and to `read_file` the one you use before importing. The
control arm had no files and no line. Same model, same gate, no image or mesh worker in either
arm.

### The run

Six requests — top-down arena shooter, third-person indoor 3D dungeon (no `compose_world`),
turn-based card battle, village with five NPC dialogue trees, four-lane rhythm game, three-level
platformer — one single-shot build per arm, serialized on one card.

### What it measured

Every build in both arms loaded clean, so the load rate had nothing to gain. The library changed
what the games HAVE:

| ledger item | control | lib |
|---|---|---|
| WASD and arrows both bound | 4/6 | 6/6 |
| any sound | 1/6 (the rhythm game, where sound is the request) | 6/6 |
| 3D scene lit | near-black: one torch glow, the hero a silhouette | lit room, shadows, readable walls and floor |

API misuse: none. Every imported name exists, every `view.*` member and `sfx` name is real, and
every build read each lib it later imported BEFORE importing it, as the line asked. The
doc-at-the-moment-of-use result (RepoCoder, DocPrompting) holds on a 27B in a build loop.

### Played (2026-08-22, by hand, every build both arms)

| request | control | lib |
|---|---|---|
| arcade | plays as asked: aim, shoot, enemies die, pickups score | dead: blank page |
| indoor3d | broken: every direction walks into the screen, nothing visible | broken but better: lit, playable, camera yaw inverted, hero faces backwards, damage from nowhere |
| cards | plays, with bugs | dead: a selected creature cannot be placed — `handleClick` has no board-placement branch |
| npcs | plays, rough | dead: no key does anything |
| rhythm | plays, badly | dead: the title overlay never leaves — CSS `#title .hidden` (descendant) for a class added to `#title` itself |
| platformer | plays; enemies embedded in platforms | plays; better — reachable platforms, stomps, coins |

Feature-present counts above were 6/6; the games that PLAY were control 4/6, lib 1/6. Two of the
four lib deaths were one lib defect: `canvas.js` created its own `position:fixed` canvas and
appended it to body AFTER the page the model wrote, so the model's HTML title screens and HUDs
(every build writes them as DOM overlays, control arm too) sat under the canvas — arcade's
"blank" was the canvas over its LAUNCH button, and npcs' Begin button was unreachable so the game
never left title mode and ignored keys. No lib call in either game was wrong. The other two were
the model's own logic bugs, one each, in games that never called the part of the lib near them.

A helper that owns the DOM collides with the page the model owns. `createCanvas` now takes the
model's own `<canvas>` element and creates and styles nothing — sizing, letterbox, camera and
world↔screen only. Rebuilt arcade and npcs on the lib arm with the new header: both used
`createCanvas(document.getElementById('game'), …)` with a `<canvas id="game">` in their HTML,
and both play — arcade at once better than its control; npcs spawned the player on an unwalkable
tile (game logic). Rhythm's and cards' bugs are not lib bugs, and were left alone.

One more header effect: every lib game, 11 of 11 call sites, plays `music.start(7, …)` — the
`7` copied from the header's example. An example value in an API doc is the value the model
ships; the header now says `music.start(seed)` and that every integer is its own tune.

### Cost, and what did not improve

| request | control steps / wall | lib steps / wall |
|---|---|---|
| arcade | 22 / 1m46s | 10 / 2m52s |
| cards | 29 / 3m21s | 8 / 2m29s |
| indoor3d | 41 / 5m03s | 89 / 4m42s (stopped by hand) |
| npcs | 16 / 2m16s | 43 / 2m53s |
| platformer | 17 / 1m32s | 34 / 2m17s |
| rhythm | 13 / 1m27s | 21 / 1m43s |

Four lib builds took more steps: 2–4 turns reading lib docs, and the bigger games then compacted
where the control did not, because the reads plus the game no longer fit one window. The two 2D
games built on `createCanvas` took fewer, writing less code. Wall-clock was roughly equal; the lib
arm's includes two one-round error-gate fixes of the games' own bugs. The next measurement is
whether terser headers — the signature, not the prose — buy the steps back. Not measured: staged
builds and `compose_world` games.

The indoor3d lib build reached a lit, complete game and then sent eighteen byte-identical failing
`edit_file` calls against its own comment block, editing from memory after a compaction; it was
stopped at step 89 with the finished game on disk. No lib call is in the failing edit. The repeat
counter reached 18 and nothing ended the build — that is the harness's open edit-loop defect, and
the control indoor3d compacted 37 rounds too.

---

## A grade as a fix note (2026-08-08, local 5090, one game)

### The question
The rubric (`docs/game_rubric.md`) produces a written judgement of a played game. The obvious next
thought is to hand it straight back to the build. Does that produce a better game?

### The run
The first graded build — a curse-shop game, graded **1 first impression / 1 considered**. Its
complaints were transcribed into `run --fix` in the owner's own words: the dispel step unreachable,
the day never ending, buttons overlapping, some text unreadable.

The fix succeeded in 26 steps / 84 s and found the true cause, which no one had diagnosed: the
Examine and Cast Spell buttons were placed at fixed rows of a question grid whose height depended on
the question count, so Cast Spell sat underneath the questions. One layout bug had produced two
apparently separate complaints, and the dispel system and end-of-day tally — both graded `absent` —
turned out to exist and be unreachable.

### What it measured
`core_loop` 1 → 2. The `dispel the curse` claim moved `absent` → `delivered`. **`legibility` went
DOWN** — reaching the spell step exposed that you must cast with no information about which spell or
ingredient applies. Every other dimension unchanged. The owner declined to re-grade it: not a better
game.

### The ruling
**A grade says what is wrong, not what to do.** It carries symptoms, so the fix it produces clears
the reported blocker and stops there — behind this one was a design problem the grade had no way to
name.

Grades are an AGGREGATE instrument aimed at the LOOP: collect them across a battery, find what
recurs, and turn that into a prompt-time change measured against a control arm. A dimension low
across many games is a work item; low on one game is noise.

### Alongside: an LLM asked to fill in the same rubric
Both graded games, same rubric, source and manifest only, no ability to run them. It answered
**6/5 both times** against the human's **1/1 both times**.

Game 1 (a shop/deduction game, blocked by a layout bug): close on what is visible in code —
`art_integration` 6 vs 6, `legibility` 6 vs 7, `moment_to_moment` 5 vs 4 — and inverted on
everything requiring the game to run: `depth` **7 vs 1** ("30+ unique items… 20-30 minutes" — real,
in `items.js`, unreachable), `visual_coherence` 7 vs 1, `character` **8 vs n/a** (it praised writing
the player never reaches).

Game 2 (a 3D village, abandoned as unplayable because movement and camera turn against each other):
`moment_to_moment` **6 vs 1**, scored as *"movement is smooth and standard FPS controls work well"* —
an assertion about the one thing that made the game unplayable. `visual_coherence` 8 vs 2,
`art_integration` 7 vs 1, `character` 7 vs 1. Its stated biggest gap was the absence of a core loop:
articulate, plausible, and not the reason the game failed.

Both games passed the error gate — they load and throw nothing — and passed this review, and are 1s
to the person playing them. Both failures were embodied rather than textual: an unreachable state,
and a control scheme that is wrong only once a body is attached to it.

**Unresolved, and it undercuts the above:** 6/5 twice is suspiciously constant. Whether the model is
reading these games or emitting a default for any competent-looking codebase is not distinguishable
from two samples that failed. The test is to hand it a game the human scores WELL; if it still says
6/5, it is measuring nothing.

---

## Sizing the canvas to the window (2026-08-08, local 5090, qwen3.6_27b via ninfer)

### The question
"Fixed canvas with no window scaling" has sat on the roadmap's recurring-defect ledger since the
2026-07-27 grid without a prompt line, because `build.txt` is pinned against additions. A real
build the same day also shipped a map drawn with the camera offset applied to BOTH the source and
destination rectangles, so the ground rendered as a strip while the sprites floated correctly —
"add a camera line too" was the obvious response. Do either of those lines earn a place?

### The shape
Two requests neither line was written for (a top-down shepherd, a side-view platformer), built
twice each: once with two candidate lines added, once with the prompt untouched. Same model, same
box, same session. Scored mechanically on three checks — canvas sized to the window, camera clamped
to the world, and the ground drawn through the same offset as the sprites — with the checks written
before the control arm finished and deliberately generous to the control.

### What it measured
| check | with the lines | without |
|---|---|---|
| canvas sized to the window | 2/2 | **0/2** |
| camera clamped to the world | 2/2 | 2/2 |
| ground shares the sprite offset | 2/2 | 2/2 |

Screenshots at 2558×1319 confirm the first row: both control games render as a small fixed box in a
field of background colour, both treatment games fill the screen.

### The ruling
**The canvas line ships; the camera line does not.** The camera defect that motivated the whole
exercise did not reproduce in either control game — one broken game out of three top-down games
built that day is not a recurring defect, and a line that costs every turn of every build must
point at a number. The scorer was checked against the original broken call to prove it can see the
bug, so the 2/2 is a real negative and not a blind check.

The general law, and the reason the surviving line is worth its cost: **a defect that only appears
at window sizes larger than the authored one is invisible to every check that runs at the authored
size.** The first headless pass over the broken game, driven at 1100×760, reported it clean.

---

## The worldgen cell recipe, generalized (2026-08-08, local 5090, 6 biomes)

### The question
The map recipe validated on one coastal-town cell (blockout → Qwen-2512 subjects → TRELLIS
sprites at one shared camera → DreamShaper terrain img2img → composite → Qwen-Edit-2511
embedding) — does it survive biomes it wasn't tuned on?

### The run
Six requests — river village, desert bazaar, volcanic mine, snow monastery, forest camp,
farm hamlet — through the full pipeline unattended. Lab code outside the service tree;
~35 min wall-clock total, 26 new object types rendered (subject ~20s, TRELLIS lift + 45°
orthographic sprite ~30s each), 6 embedding passes at 134–172s.

### What it measured
**6/6 end to end, zero pipeline failures.** Layouts solved (no plan fallbacks), terrain
differentiated per biome (lava river, snow field, sand, grass), objects kept identity
through the embedding pass, contact shadows landed. The pipeline generalizes; the failures
are all content:
- **Type resolution is biome-blind.** Every "house/hut/quarters" in every biome resolved
  to the one cached red-roof cottage — desert, volcano and snow alike. 26 of 32 types fell
  through the keyword table to raw flavor names ("The Silent Peak Monastery"). Resolution
  needs a style/biome dimension and something smarter than keywords.
- **Uniform scale lies.** Telegraph-pole street lamps, a barn-sized well — one
  VISUAL_SCALE for every kind.
- **Forest is terrain when it should be trees.** The forest band painted as flat dark
  green; a forest cell needs a tree-scatter rule, not a label.
- **The style prompt can lose to the bare-ground paint.** river_village asked for lush
  green and kept its mud at denoise 0.55.
- **Sparse density** — big empty stretches in most cells.

### The ruling
Recipe is integration-ready; every failure maps to a known work item (store type
resolution, per-kind scale table, forest scatter, terrain palette, density). Lab code and renders live outside the repo — results
recorded here, service code arrives only with the integration itself.

## The NSFW render classifier's threshold (2026-08-03, local 5090, 290 renders)

### The question
Where does the refuse threshold go for the post-gen image verdict (`assets.render_verdict`,
scores from Marqo/nsfw-image-detection-384 in `worker/safety_vision.py`)?

### The run
Every rendered asset from the local run corpus — 290 webps across 30 runs, all of it innocent
game art — through the worker's own classify path. Two preprocessing variants (alpha composited
onto black, the RGB-convert production default for matted sprites; and onto white), plus a
divergence check of the worker's transform against timm's official pipeline for this model.

### What it measured
- A hand-rolled resize/normalize sat up to **0.146** off the official pipeline's probabilities;
  `timm.data.create_transform` fed from the exported `data_config` matches to **5e-05**. The
  worker uses the latter.
- With exact preprocessing, the model still overcalls stylized game art: **28/290 (black) and
  31/290 (white) innocent renders scored ≥ 0.5** — a cow at 0.946, a cabbage at 0.923, a rock
  sprite at 0.933. Flux-schnell game sprites are out-of-distribution for a classifier trained on
  photographic/anime NSFW.
- The innocent ceiling across the corpus: **0.954**.

### The ruling
`NSFW_REFUSE_THRESHOLD = 0.98` — zero false positives on the corpus, while the pre-gen prompt
screen keeps owning steered content and the classifier owns only the unmistakable case. Verdicts
are stored on the manifest per render, so a future threshold (or a better-calibrated model for
stylized art) re-policies old assets without re-rendering anything.

---

## The audio candidates (2026-08-01, local 5090, 58 generations)

### The question
Games ship silent — "silent games (all four arcade + the deck-builder)" has sat on the open ledger
since the 2026-07-27 grid. Before spending a prompt line or a queue on it, which open-weight audio
models can we actually run, under a license we can use without thinking about it, fast enough that
art does not starve gameplay for GPU?

Two candidates were installed locally and run against a game-shaped battery. Licenses were read off
the HuggingFace repo `card_data`, not off blog posts — the trap here is models whose CODE is
permissive and whose WEIGHTS are not.

| candidate | license | what it is |
|---|---|---|
| ACE-Step 1.5 | **MIT** (code + weights) | text-to-music, native ComfyUI support since 1.5 |
| MOSS-SoundEffect v2.0 | **Apache 2.0** (code + weights) | text-to-SFX, DiT + flow matching, 48 kHz |

Ruled out on license, all of them weights-side: Stable Audio 3.0 / Small-SFX (Stability Community
License), MusicGen / AudioGen / AudioCraft (weights CC-BY-NC 4.0 against MIT code), AudioLDM 2 and
Tango 2 (NC). Permissive but wrong shape: HeartMuLa-oss-3B (Apache 2.0 both halves, but RTF ≈ 1.0 —
~90× slower than ACE-Step turbo — and lyrics-oriented), YuE (Apache 2.0, slow).

### Method
The two halves ran differently, and **that difference is itself the finding**. ACE-Step went through
ComfyUI's own native nodes over `POST /prompt` → poll `/history` → `/view`, the identical path the
production image worker already uses, with sampler settings lifted from ComfyUI's shipped templates
rather than guessed. MOSS has no native node: it ran in-process from its own pipeline in an isolated
venv, which is the TRELLIS shape — a server of its own.

ACE-Step: 5 game-music briefs (chiptune boss, pastoral village, menu loop, space combat, playful
puzzle) × 6 configs, plus a duration sweep — 34 runs. MOSS: 8 game sounds × 25/50/100 steps — 24
runs. Every cell succeeded. All 58 outputs verified byte-distinct.

### What it measured

**ACE-Step 1.5, 60 s of music, warm:**

| config | wall | peak VRAM |
|---|---|---|
| turbo-1.7b, **planner off** | **1.8 s** | 12.7 GB |
| turbo-1.7b | 4.3 s | 20.4 GB |
| turbo-4b | 5.4 s | 20.4 GB |
| xl_turbo-4b | 5.6 s | 29.3 GB |
| base-4b (50 steps) | 8.6 s | 20.4 GB |
| xl_sft-4b (50 steps) | 11.5 s | 25.4 GB |

**Duration is linear, with no coherence cliff:** turbo at 30/60/120 s → 2.3 / 4.5 / 8.9 s;
xl_turbo → 3.0 / 5.8 / 11.7 s. **Model load is negligible** — cold 4.5 s against warm 4.3 s, so a
scale-to-zero audio pod pays pod boot and almost nothing else, unlike mesh's ~116 s.

**The `generate_audio_codes` toggle is a 2.5× lever** (4.3 s → 1.8 s, and 20.4 GB → 12.7 GB). It is
the planner LLM. Whether the quality is worth 2.5× is an ear question, and both arms are in the grid
for exactly that reason.

**MOSS-SoundEffect v2.0:** 2.1–3.1 s at 25 steps, 4.0 s at 50, 8.0 s at 100 — flat 19.6 GB
regardless of step count, 48 kHz, pipeline load 7.1 s.

**Three environment findings that will bite whoever integrates this:**
- **xformers has no kernel for ACE-Step's attention on Blackwell** (`NotImplementedError` on
  `memory_efficient_attention_forward`, bf16 `(1,375,16,128)`). ComfyUI needs
  `--use-pytorch-cross-attention`. The image worker's instance would need it too.
- **The box has no ffmpeg.** MOSS's `save_audio` routes through torchaudio → torchcodec → ffmpeg and
  failed on all 24 clips while the diffusion itself was fine; writing the waveform with `soundfile`
  fixed it. ACE-Step is immune — ComfyUI writes MP3 itself, and also offers Opus.
- **xl_turbo peaks at 29.3 GB of 32.6.** It cannot co-reside with anything. Only the non-XL turbo
  variants (12.7–20.4 GB) have room to share a card.

### Measurement hygiene, recorded because it cost a re-run
The first ACE pass was contaminated twice over: **ComfyUI caches node outputs**, so a byte-identical
graph returns in 0.25 s without generating, and VRAM carried between configs until one cell hit
32.0 GB of 32.6 and measured thrash (8.4 s) rather than the model (5.5 s). The numbers above come
from a clean re-run with an unload between configs. A grid driven through ComfyUI must free between
arms or it measures its own cache.

### Ladder position — unrun, NOT implemented
The integration shape was designed and is deliberately not built. For music it is a new queue and
worker but **no new server**, since ComfyUI already serves it: a `t2music.json` workflow, a
`build_music_payload`, `save_audio` in `asset_chain.OPERATIONS`, one `MEDIA_SCHEMA` enum value, an
`EXT = {"image":"png","mesh":"glb","music":"ogg"}` table replacing the `"glb" if mesh else "png"`
ternary at its four sites, and `comfy_image` widened to collect any ComfyUI output type rather than
only `["images"]`. A separate queue rather than riding `image` because prod is already
one-queue-per-card and `start_from_manifest` already records that a one-GPU box holds one model at a
time. **The number that would overturn that is unmeasured**: whether flux1-schnell-fp8 and ACE-Step
turbo co-reside on 32 GB. The attempt ran inside ninfer's headroom and measured thrash.

MOSS is further down the ladder than music, not beside it. It needs the full TRELLIS-shaped lift —
its own server, handler kind, queue, worker and pod class, at 19.6 GB resident — while a WebAudio
oscillator costs zero infrastructure and beats a diffusion model at a 0.2 s arcade blip. The open
ledger item is not "we cannot generate SFX", it is that games come out silent; if a prompt line does
not get a five-line beep out of the model, a MOSS pod renders nothing either. MOSS earns its pod for
foley and ambience, after music ships and not alongside it.

### The ruling
Both models are good and both are usable. Nothing is being implemented yet — this entry exists so
that when it is, it starts from numbers rather than from the survey again. Artifacts live outside
the repo in `/home/nick/audio-grid` (harnesses, `results_clean.json`, `moss_results.json`, and a
results page with every clip playable); weights in `Documents/models/Audio`, symlinked into
ComfyUI's own `models/` and deliberately not into the production `models/Image` tree.

---

## The style anchor (2026-08-01, prod, qwen3.6_27b via ninfer, n_ctx 131072)

One line added to build.txt — *"A game's art shares one visual style. Pick the style before the
first generate_media call and name it in every prompt"* — after two screenshots showed the failure
it aims at: a flat-shaded procedural village hosting a photoreal TRELLIS mesh and a photoreal
portrait billboard. The hypothesis: the incoherence is per-asset style drift, not asset-ness, and
the style named in the image prompt is also what the mesh chain lifts from.

Ran as a three-game battery on prod (the mesh scale normalization and the binary/vendor read
guards landed the same day and are in every arm).

### What it measured
**Style naming: 15/15.** Snail race asked for 11 assets, every prompt carrying "cartoon (game
sprite) style"; Island Village asked for 4, every prompt carrying "warm sunset lighting / earthy
tones / minimalist". Owner judgment on the snail race: visually consistent in play.

**The 2D/3D split is WIRING, not asking and not style.** Snail race referenced 11 of 11. Island
Village referenced 0 of 4 — it asked for ground/wall/floor tiles and a sky, then shipped
flat-shaded materials anyway, past the done-nudge audit naming all four. In 2D, using art is
`drawImage`; in three.js it is TextureLoader + material plumbing the model never crossed. Ladder
position: a snippet-tier candidate, unrun.

**The model declined meshes on its own.** Island Village under the style line built its whole
village and cast procedurally — zero mesh asks, zero portrait billboards — independently arriving
at the owner's own judgment of the earlier screenshots ("way more coherent with the built 3D
models"). One game; not yet a law.

**Cursed Curation (the generate_media showcase, "dozens of distinct objects" in the request):
~15 distinct items, owner-judged best of the three.** The demand-side pull worked where
prompt-side pushes had rationed at 5.

### The ruling
The line stays — it moved style coherence at one line of per-turn cost and nothing regressed.
Coverage in 2D looks solved when the REQUEST wants art; 3D texture wiring is the open half, and
mesh usage in 3D is parked until a build shows a style-anchored mesh joining a scene it belongs in.

---

## The depth arms (2026-07-30, qwen3.6_27b, n_ctx 131072)

### The question
A game the platform builds is usually playable and shallow. The 2026-07-29 card RPG took 439 human
fix notes and still had no art, no resources and an unpolished UI — the request did not produce a
game that stood on its own. Five shapes were run against the same requests to see which, if any,
changes that.

| arm | what it changes |
|---|---|
| A | nothing — the naked driver, as baseline |
| B | the first `done` is answered with a nudge, not accepted |
| C | write `DESIGN.md` before any code, re-read it before `done` |
| D | write `PLAN.md`, then one build per numbered step |
| E | write `PLAN.md` as phases, then one build per phase |

Two rounds of two games. Round 1: an open-world card RPG (the 439-note request, verbatim) and a
colony survival sim. Round 2: a story RPG whose world must remember what the player did, and a
roguelike whose items must combine. Round 2 also ran the error gate and the batched-call line.

### What the counters said

| arm | game | turns | GPU s | files | art asks |
|---|---|---|---|---|---|
| A | narrative | 77 | 658 | 12 | 0 |
| A | roguelike | 38 | 300 | 7 | 0 |
| B | narrative | 70 | 456 | 13 | 0 |
| B | roguelike | 79 | 439 | 8 | 0 |
| C | narrative | 37 | 683 | 28 | 20 |
| C | roguelike | 55 | 359 | 18 | 8 |
| E | narrative | 367 | 2524 | 37 | 28 |
| E | roguelike | 506 | 2740 | 11 | 0 |

Round 1 (cardrpg / colony) ran the same five arms; D was run on the card RPG only, at 1222 turns and
7260 GPU seconds.

### What playing them said
**The counters are anti-correlated with the result.** Arm C won turns, files and art asks in BOTH
rounds and played worst in both: a colony that announced "Colony lost" before any input, a card game
whose combat and shop buttons did nothing, a story RPG that threw while being played, a roguelike
full of undefined values. Arms A and B looked unremarkable on every counter and produced the games
worth playing.

Ranked by the human who played them: B/colony best overall, then B/narrative, then A/roguelike.
A and B were close enough on both rounds that nothing structural separates them.

### What came out of it, and what did not
**Adopted:** the done-nudge (arm B). Best or joint-best on three of four games, and it costs one
turn. Also the batched-call line in `build.txt`, and the turn cap at 120 — 80 capped mid-phase on
large games.

**Not adopted:** C, D and E. C is covered above. D and E buy depth by splitting authoring across
builds, and both shipped load-blocking defects that single-build arms did not: two files each
declaring the same top-level `const`, in separate transcripts, with no way for either to see the
other. A, B and C are one build each and collided zero times.

**Splitting does not prevent interface MISUSE, only redeclaration.** E's `combat_ui.js` and
`combat_engine.js` were written in the same phase and the same transcript, and `combat_ui` still
called `engine.playerPlayCard` on an object exporting `CombatEngine.playerPlayCard`. One transcript
prevents two files declaring the same name; nothing here prevents calling a name that was never
defined.

**A plan phase that says "testing" costs a full turn cap.** Three E runs, two of them capped at 120
without ever calling `done`, both on a phase whose text asked the model to verify behaviour — which
it has no way to observe, since nothing in the harness loads the page. The phase that said "polish"
without "testing" converged at 75.

---

## The error gate (2026-07-30)

### The question
Three of the nine round-1 games did not load at all, and nothing in the pipeline saw it. Every arm
is blind in the same place: nothing ever opens the page.

### The shape
Load the staged game in a headless browser after the build finalizes, collect uncaught exceptions
and unhandled rejections, and re-enter the fix machine with what threw. An uncaught exception is one
of the few signals that satisfies the BROKEN-not-bad guardrail — `this._doIdle is not a function` can
only be met by defining it.

404s are excluded deliberately: art lands after the code that draws it, and a missing sprite is a
fallback shape, not a broken game.

### What it measured
Every cell that threw went to zero, unattended — nine of nine across both rounds, including a game
with seven load-blocking defects. Three things had to be true, each measured separately:

**One error per fix build.** D/cardrpg: seven errors in one note closed NONE in two rounds; the same
seven sent singly closed all seven in five rounds.

**Every error needs an address.** A SyntaxError reaches the browser with an EMPTY stack, so node
supplies file and line, and a cross-file declaration scan supplies the pair for a redeclaration that
neither tool can locate alone.

**A parser reports one error per file, so a repeated defect grinds.** A/narrative had 67 broken
object literals plus 10 unquoted keys across four files. Located notes alone moved it 3 → 3 over six
rounds, fixing one INSTANCE each. Adding one sentence — a file stops parsing at its first error, so
fix every occurrence rather than the line named — moved it 3 → 0 in three rounds, clearing 13 then
52 then 10 instances. Convergence went from per-instance to per-file.

### Its limit, and the ruling
**The gate does not simulate play.** It loads the page and drives it only far enough to get past a
title screen. C/narrative threw repeatedly while being played and the gate called it clean; that
class belongs to a play-time listener where a person supplies the interaction. Teaching the probe to
click more would be a "must DO X" gate wearing a disguise.

So the gate moved three games from *does not open* to *opens and responds*, and did nothing for a
roguelike whose floors never vary, a card game whose combat button is inert, or a colony that loses
instantly. Those are still human findings.

---

## Recovering a tool call the model wrote as text (2026-07-30)

`_xml_calls` dropped any recovered call that carried no arguments, so `<function=list_files>` — the
only argument-less tool, and the first one a fix build reaches for — was reported to the model as
"no tool call landed", followed by a nudge telling it to write `index.html` over a game that already
existed. Three fix builds in a row died at step 4 before this was found. `_usable` already rejects a
name that is not an offered tool, so the guard bought nothing.

---

## Worldclaw pipeline through the queues (2026-08-21, local 5090, qwen3.8_27b via ninfer)

### The question
The worldclaw spike built its eleven stages against models it dialed directly. Does the whole
pipeline still run end to end when every GPU call is a job on maestro's queues, on one card that
holds one model at a time?

### What it did
One prompt — a fishing village on a rocky coast — to a finished world, `scripts/local_gpu.py auto`
switching the card between the three queues as the stages asked for them.

| stage | seconds |
|---|---|
| scene | 34 |
| terrain-plan | 101 |
| terrain-assets | 767 |
| construct | 1 |
| terrain-refine | 2039 |
| regional-plan | 36 |
| objects | 3257 |
| scene-refine | 173 |
| final-render | 48 |

≈107 minutes in total. terrain-refine's 2039 s is mostly not work: ~26 minutes of it was an unserved
queue while the local ninfer was crashing on launch — the stage itself is about 7 minutes. Jobs: llm
72 done / 1 failed, image 58, mesh 45 done / 1 failed, across 39 model swaps (28 of them into a
queue that already had work waiting).

### The three migration defects
- **Image and mesh results land as FILES, not base64.** Both backends read a worker result as an
  inline payload; the worker deposits a blob and reports its path (`entry["file"]`, `glb_file`).
- **TRELLIS answers `/health` before it is warm.** A job sent into the warmup killed the server. The
  switcher now waits for `"warm": true` rather than for the port to answer.
- **ninfer needs `--vision` for the refine stages** — they judge renders. With `--vision` the NVFP4
  artifact no longer fits on a 5090 at 98304 ctx; the plain `qwen3_8_27b.ninfer` (18.2 GB) does, at
  29.5 GB resident.

### What the world looks like
The terrain reads as a coastal headland. The "village" is about six small meshes clustered mid-plain
and does not read as a village; the rocky coast reads as sand; the water is a flat plane. Honest
verdict: a place, not yet the place that was asked for.

### Open, and not migration defects
- **The scattered pine prototype reconstructed as a flat billboard slab**, and every scatter copy
  inherits it, so the pine forest is a field of slabs. One bad TRELLIS reconstruction, upstream of
  anything the port changed.
- **One mesh regeneration 500'd** with `Input type (float) and bias type (c10::Half)`.

### A game built in it (run 9c3c07df4201, same day, same box)

Request: a third-person exploration game in a fishing village — collect five lost floats, return
them to the harbourmaster's hut, show a counter. The model called `compose_world` once in its
first reply (a 120 m world, seed 7), alongside two `generate_media` calls, and the tool answered
after 16.6 min (scene + terrain plan 1.3 min of llm, terrain assets 14 min of image jobs on one
card, construct + six scatter meshes 2 min) with six regions it then placed the game by: spawn at
the village centre, floats by the boats, on the beach and at the shore. The rest of the build was
2m20s of llm across turns 1–19; `built` at 19 steps, 0 compactions, error gate clean at round 0,
zero console errors once the sprites had landed. The background legs finished 32 min after
`built` (terrain-refine 736 s, regional-plan + objects 1261 s, scene-refine + final render 51 s)
and re-published into `game/world/` (71 MB) without the game noticing. Jobs: llm 67, image 36,
mesh 16, none failed. Played headless: the player walks the generated ground on `heightAt`, is
stopped by `blocking`, a float beacon and the hut stand where the regions said.

What was wrong is the game's, not the world's: the player is a rotated plane rather than a
billboard, so it vanishes edge-on after a sideways step; the objects leg dropped all five
harbour-cove objects (no ground under them) and the village got benches and stones, no boats;
the pines are the same billboard prototype as the first world (this build predates the
one-specimen schema line, so it is the next world that measures that fix).

### The control: the lab's own prompt through the fixed queue path (same day, same box)

Auditing the migration against the lab found five regressions, all in the seams and none in the
stage code: the local switcher served the groupwise-int ninfer artifact instead of the nvfp4 one
the lab measured on (nvfp4 + `--vision` fits at 65535 ctx, not 98304); the llm shim swallowed every
stage's `temperature` so planners and judges ran at 0.7 (the object-grounding pass lost its 0.0);
a path off by one meant `decimate.mjs` never ran; the per-image TRELLIS seed was dropped, so
regenerating a rejected mesh returned the same mesh; and `max_tokens: 50000` rode against a 65535
window. With those fixed, the lab's medieval-village prompt verbatim: layout read-back within 7
points of plan on every region (the broken runs were 8% against 30% and 49% against 15%), 1934
scatter placements over an 800 m world (lab: 977 over 600 m), 52/52 objects reconstructed and
placed across three regions (lab: 17 in one), terrain-refine 162 s, 78.9 min from the
terrain-assets resume. The village close-up reads as the lab's did: half-timbered houses, a tower,
trees, cattle. What differs is the planner's taste on this seed — dry yellow grass and a sea level
set below the lake basin, so no water plane — not the pipeline.

Two things killed the desktop on the way and are now launch rules in docs/local_dev.md: the TRELLIS
server's `/dev/shm` weight staging (14 GB resident) and the `1024_cascade` tier (two pipelines
warmed, ~36 GB host RSS). Host RAM, not the card, is the local ceiling.

## The terrain renderer as a lit surface (2026-08-22, three worlds already built, CPU/SwiftShader)

### The question

The ring views the refinement judges look at — and the ground a generated game draws — read as flat
paint buckets: no shadows anywhere, no snow on the "snow-capped" mountains, contour rings on every
gentle slope, one tile smeared over hundreds of metres, and a dashed white lattice over every close
view. How much of that is the material and how much is the terrain being outside three's lighting?

### What changed, in `runtime/vendor/world.js` only

The splat moved from a `RawShaderMaterial` onto `MeshStandardMaterial` through `onBeforeCompile`, so
the ground is lit by the same lights, in the same units, and receives the same shadow map as the
GLBs standing on it. The terrain now casts as well as receives. Sun 3.1, hemisphere 0.45, ambient
0.9, ACES at exposure 1.25. On top of the blend: the slope rule, the snow rule off the world's own
relief, and the two-scale albedo with a hue drift — all described in `docs/build_path.md`.

Two defects were found on the way and are the larger half of the result. Every stochastic tap became
a `textureGrad` on the gradients of the UNBROKEN uv: the per-cell offsets jump, and the implicit
derivative of a jump reads as "this fragment covers the whole texture", which is what drew the
dashed lattice. And `loadWorld` now awaits its textures before it hands back a world — three binds an
empty 1x1 for a texture still in flight, which draws the whole ground BLACK, and a world with no
meshes to wait for renders its first frame straight into that gap.

### The contour rings are the pipeline's, not the renderer's

`ctrl_medieval` has no terrace operator and its `heightmap.npy` holds 927,015 distinct float32
values over a million samples, so the height field is not quantised as a whole. `heightfield.fbm`
is: it round-trips each noise lattice through a uint8 PIL image before the bicubic upsample, and
`fbm(10.0, 1024, 2)` returns 1504 distinct values with every large gap exactly 2/255 of the band's
amplitude. Bicubic over quantised levels leaves flat shelves, which a hillshade of the raw numpy
draws as concentric contour lines around every hill — visible in the data before any renderer
touches it. Fixed in the same change: the lattice now rides PIL's float mode through the bicubic
(clamped, since a float resize overshoots where uint8 saturated), and `fbm(10.0, 1024, 2)` returns
over a hundred thousand distinct values. The renderer change had already hidden the rings in the
frames — a 2 cm shelf that the raw shader's bare lambert turned into a hard line disappears under
the standard BRDF's fill and ACES — but a hidden shelf is still a shelf a player walks on.

### Before and after

Same three worlds, same cameras, 960x600. The mountains gain shape: a cast shadow off the range onto
the plain, dark rock on the steep faces and snow on the shallow high ground, where before the whole
massif was one white paint bucket. The plains lose their contour rings entirely and gain visible
patchiness instead of one flat yellow. The close view of a peak loses the dashed lattice completely.
The village world gains tree shadows on the ground, which is the first thing in it that says the
sun has a direction.

What did NOT improve: the desert's diagonal repeat striping is still there (2 m tiles over 800 m is
400 repeats and the macro tap does not hide the moiré); the shaded side of a peak reads cool
blue-grey and darker than the ambient doctrine wants; the medieval lake basin is still a blotchy
teal bed, and that is `sea_level_m: -500.0` in a world whose lowest ground is -2.3 m — the loader
correctly refuses a water plane hung below the terrain, so the basin is a dry lakebed texture. That
is the planner's number, not the renderer's, and no per-basin plane was invented here.

### Frame times (SwiftShader, one CPU render per camera)

| world | before | after |
|---|---|---|
| ctrl_medieval, 800 m, 5 regions | 13.7 s | 16.5 s |
| e2e_village, 400 m, 4 regions | 9.0 s | 10.3 s |
| run 9c3c07df4201, 120 m | 1.5 s | 3.5 s |

Roughly 20% on the big worlds — two albedo taps instead of one, plus a shadow map pass — and more
than double on the small one, where the shadow pass is most of a cheap frame. On a GPU none of this
is a budget question; it matters only because the refinement loop renders its ring on CPU.

---

## A pair of ground textures per region (2026-08-22, local 5090, one 120 m world A/B)

### The question

Every region was covered in ONE generated square. Two things were wrong with that at once and it
was not obvious which mattered: the square was often not a material at all — asked for a lakebed the
model draws a lake, asked for a pine forest it draws a canopy from the air — and even a good square
is the same square metre everywhere in a region however cleverly it is sampled. Does asking for the
ground straight down, and asking for TWO of them, produce a ground that reads as ground?

### What changed

Three things, in one arm because none of them is separable from the frames:

1. **The plan carries two surfaces.** `RegionMaterial` gained `variant` — the worn second ground of
   the same region — and both it and `surface` now say what they are: "the ground underfoot in this
   region, as a square of surface seen straight down: soil, needle litter, sand, shingle, turf,
   bare rock, pavers". The old `surface` description asked what the ground *is*, and one planner
   answered "forest", which is why a region came back carpeted in an aerial photograph of tree
   canopy. `scale_m` covers both.
2. **The prompt asks for a surface, not a picture.** `TEXTURE_TEMPLATE` now opens and closes on the
   same law: a square of ground filling the frame, camera at knee height pointing straight down,
   one or two metres across, no horizon, no sky, no water's edge, no view of a place. The negative
   gained `horizon, landscape, scenery, aerial view, shoreline, water's edge, far bank`. Every
   render is then quilted onto a torus by `tools.quilting.quilt_tile` (1536 render → 768 tile), so
   the tile is seamless by construction rather than by luck.
3. **The shader blends the pair** by a fixed recipe in world metres — `patches = fbm(m * 0.025)`,
   `detail = fbm(m * 0.6)`, `wear = smoothstep(0.62, 0.85, patches*0.7 + detail*0.3 + slope*1.2)` —
   under the macro mix, hue drift, slope-rock and snow rules that were already there. Nothing is
   per-world; a region naming no variant draws its base alone.

### The run

Run `9c3c07df4201`'s world (120 m coastal village, 6 regions), copied to a lab folder and rendered
with its existing materials as the BEFORE. Then `terrain-plan` re-run so the planner filled the new
field, and `terrain-assets` regenerated: **12 material renders, 17.3 min wall-clock** for plan +
materials + subjects + reconstruction on one card, materials at roughly 75 s each including the
quilt.

The planner filled `variant` for all six regions with no coaxing, and every one of them is the
intended thing: harbour sand → "wet dark sand at the waterline with thin pebbles showing through",
pine humus → "exposed grey bedrock and scree where the humus has thinned", village stone → "bare
grey gravel and worn stone where the grass has thinned".

### What it measured

**The prompt change is the big half, and it is not subtle.** Of the six BEFORE albedos, three were
pictures of places rather than materials: the pine forest was an aerial photograph of tree canopy,
the village was a road with kerb stones and grass verges running down it, the pebble beach was a
shoreline with a wet strip and a dry band. All three tiled as those shapes. The six AFTER bases are
uniform surfaces at grain scale — needle humus with cones, gravel with grass tufts, dense shingle —
with nothing in the frame large enough to be picked out.

**The pair shows at walking distance and not much above it.** In the close view the hillsides carry
a legible grain and change material across the slope where before they were one smeared grey-brown
with directional streaks. In the ring views, 60 m up, the wear field reads as ordinary patchiness
and could be mistaken for the hue drift that was already there — which is the honest limit of this
measurement: the recipe earns its cost at the camera a player uses, not at the camera the refinement
loop judges from.

**What did not improve.** The quilt leaves faint horizontal banding in some tiles — the block rows
of the synthesis, visible on close inspection of a flat wet-pebble variant, invisible in a frame.
One variant (wet pebbles) came back near-black, which is what the phrase asked for but darker than
the ground wants.

### The threshold is the whole of the recipe (one iteration, and it was needed)

The demo's numbers — `smoothstep(0.42, 0.62, ...)` with the slope at 1.5 — were ported as they
stood and are WRONG on a real world, for a reason worth writing down: an fbm of four octaves at
amplitude 0.5 averages about 0.47, so a threshold centred on 0.5 makes the average patch of flat
ground *half variant*. On the medieval world that rendered the village as unbroken red-orange earth
at eye level, its grass-and-flagstone base nowhere in the frame, and the ring views as orange
blotching. Raised to `smoothstep(0.55, 0.75, ...)` with the slope at 1.2 — above the noise's own
mean, so the base is the region and the variant is where it has gone — the same frames show dry
grass with earth showing through it, and the top-down view goes from mottled orange to a ground
with patches in it. The demo was a flat plane with two textures of the same value; nothing in it
could show this.

### A second world, for the failure that motivated the prompt

A fresh medieval build (`scene`..`terrain-assets`, 5 regions, 800 m, 16.5 min) as the check on the
one-world A/B. The planner filled all five variants correctly ("bare tawny earth and short dry
stubble where the grass has thinned", "loose grey scree where the snow has thinned") and — the
point of the exercise — described the LAKEBED as "shallow silt and shingle lakebed with reed-lined
banks" and got back a flat teal surface rather than the photograph of a lake with a far bank that
this world's previous build produced. At eye level the lake region reads as continuous water-silt
surface with no repeating shoreline in it.

Two more turns on the same frames (`medieval_after3`, `medieval_after4`): at 0.55/0.75 the
variant still owned the village and the plains at eye level, so the band moved to 0.62/0.85 and
the base came back as the region; and the softness of the ground within a few metres of the
camera was the 8x macro tap mixing in at up to 68% regardless of distance — it now scales with the
detail fade (`* (1 - 0.75 * gTerrainDetail)`), so the fine tap carries the near ground and the wide
tap takes over only where the detail is dropped. Not fixed: a village base tile whose flagstones
are still large enough to read as an arrangement when tiled.

### The checkpoint

Kept on Qwen-Image-2512, NOT switched to the DreamShaper checkpoint the tile bake-off preferred.
Worldgen's `ImageModel` builds one Qwen graph (`UNETLoader` + the Qwen text encoder); DreamShaperXL
is an SDXL checkpoint reached through a different graph entirely, so switching is a second arm and
not a parameter. The failures measured here were the PROMPT drawing a scene instead of a surface and
the tile not being tileable, and both are fixed without touching the model. The routing question is
still open and belongs in its own run.

## Worldgen object-stage findings, moved out of the docstrings (2026-08-22, local 5090)

These numbers were carried as module docstrings in `worldgen/objects/` (ground, subject, generate,
place, extract) and are recorded here so the code keeps one line of WHY each. All are from the
cliff-city and jungle-town worlds built during the WorldClaw spike and its migration.

### Finding objects in a composition (`objects/ground.py`)

- One region briefed for 15 objects had 4 painted; across 3 regions a third of what was in the
  pictures (people, dome, statue, benches) had no category in the plan at all. SAM3 asked eight
  times for a water jar returned eight courtyards. The grounding pass therefore asks the vision
  model what IS there rather than checking the plan off.
- A single JSON array on the densest scene: ~25 real objects, then identical barrels stepping
  right by 15 units off the image edge until the token limit — ~200 junk entries. Hence one tool
  call per object.
- Naming the pixel dimensions in the user message: 20–22 objects vs 12–13 without, over 3 trials
  each; the difference was entirely objects under 30 px.
- Generic vocabulary: 30 objects vs 20 for a world-specific noun list.
- Refusing the first `finish_objects`: jungle village 10 → 33 objects.
- A near-to-far sweep or a smallest-first instruction: recall of <30 px objects went to exactly 0.
- Rewriting the four one-line Field descriptions into fuller ones: 35 → 11 objects, reproducibly;
  a "not 'market stall'" example made it record a row of stalls. Descriptions stay terse.

### Drawing the subject rather than cropping it (`objects/subject.py`, `objects/generate.py`)

- Foliage and open lattices reconstruct as flat cards at any crop resolution, while closed opaque
  volumes reconstruct fine at 76 px: a 331 px fence failed and a 76 px barrel succeeded. Subjects
  are redrawn from a description, not cropped.
- Redrawn from description, thickness-to-length: fence 0.03 → 0.69, shack 0.50 → 0.84.
- A separate describing pass over crops wrote one sentence per KIND (7 stalls → 7 copies of one),
  so the description comes from the grounding pass that saw each instance.
- A fence mask 3x wider than tall drawn on a square canvas came back as a mesh 0.87 as tall as
  wide; drawn on a canvas of its own shape, 0.37. The canvas takes the mask's aspect.

### Placing (`objects/place.py`, `objects/extract.py`)

- Cliff city: bottom-of-box vs bottom-of-mask anchors sat 0.28 m apart in the median over every
  instance; the mask bottom is the anchor.
- Jungle town: box-measured sizes overread (bench 5.5 m, barrel 3.2 m), hence the 0.75 planner
  blend clamped at 1.35x.
- A vision-model size estimate for the same temple moved 15 m → 30 m across two consecutive calls;
  sizes are not asked for twice.

## 2026-08-23/24 — the LLM-layered map replaces blockout (scene-gen lab → `src/scenegen`)

Standalone lab, qwen3.8_27b local, DreamShaperXL Turbo. 14 places total across the runs.

### Layout (`scenegen/layout.py`)

- Word2World's recipe transfers to a 27B only as small single-purpose calls with JSON skeletons
  and a one-error-at-a-time reask: 12/12 maps completed, largest walkable component 100% on all
  12, walkable ground 32–96%. Median 10 calls / ~15k tokens / 37s per map.
- The same model asked to rewrite a full-resolution 24x32 grid emitted uniform fill (768 of one
  symbol), twice — compose_scene's founding "the model can't hand-write tile maps" reproduced.
  Every stage edits through its own small representation.
- Layout coherence by eye: ~9/12 on the original battery, ~6/10 on ten unfamiliar one-shot
  places — layout, not paint, is where quality is lost now.
- Self-critique rounds measured flat (scores do not climb round to round) and their `fill:true`
  rect fix-ops caused the worst artifacts of the run; the promoted pipeline has none.
- Vision-critique → full regenerate on all 14: accurate concrete critiques, but ~2-3 improved
  vs ~7 regressed — a repair tool for structurally-broken maps that damages good ones. Not
  promoted. Vision best-of-3 picked defensibly on 3 of 4 places and hallucinated structure to
  justify the fourth where all three candidates were weak. Not promoted yet.

### Ground paint (`scenegen/paint.py`, `scenegen/paintspec.py`)

- Flat color guides need denoise >=0.7 before turbo grows texture, and structure dies there
  (pier → rock arch, clearing → pond). Per-pixel jitter (±22) lets texture emerge at 0.55 with
  boundaries pixel-true to the grid. Coarse blotch noise is worse than none: a blotch straddling
  a boundary reads as "terrain crosses here".
- One masked ConditioningSetMask prompt per region beats a global prompt on all 4 test maps —
  the global prompt made every region compete for the same words ("stone" pulled the pier,
  walls and floor all to one gray). Mask feather must scale with region thickness: a fixed 24px
  feather diluted a thin treasury strip below its neighbours' conditioning and it painted as
  floor.
- A second global img2img at 0.35 deepens texture and unifies lighting; 0.45+ smears material
  identity back toward mush.
- The guide color anchors the final hue at 0.55 — no prompt wording overrode a traffic-cone
  `#f1c40f` sand until the guide hex changed. The LLM paintspec (per-region hex + material
  phrase, with the tileset hex passed as intent) matched or beat a hand-curated table on all 4
  originals; without the intent hint it turned a gold treasury into tasteful slate.
- Whole-map unify img2img over a sprite composite: harmful at 0.3 (sprites mush), dead end.

### Sprites (not yet promoted — bench only)

- Qwen-2512 + worldgen's subject_style.txt + one contextual descriptive sentence produced
  clean game assets for all 7 bench items including the character; the same items through
  NetaYume with a bare one-word prompt gave a briefcase for a fisherman and deck chairs for a
  pier. The subject leg keeps its existing prompt for now; the bench is the evidence for the
  next pass.

## 2026-08-27/28 — Qwen3.8-Flash-Next (180B MoE, NVFP4) on a rented B200, the 10-cell battery

Question: is a frontier-class open model, served remotely, worth ~5× the per-hour cost of the
27B on a 5090? Ten one-shot builds through the unchanged pipeline (designer → build_chain →
gate), LLM-only like the prior arms, `reasoning: medium`, 131K window, 200-step cap.

Serving. `RadixArk/Qwen3.8-Flash-Next-NVFP4` (135 GB) on SGLang branch `qwen4-main-squashed`,
1× B200, `--language-only`, README launch flags. Decode 148–163 tok/s at the client, 189 at
the server, single stream; 60K-token prefill in seconds. Maestro needed zero code changes —
the worker's `--target` is the whole integration. Tool calls and the reasoning split both
worked out of the box. Cold start on a fresh pod: weights 2:41 from local disk, then ~10 min of
JIT + autotune (cached after).

Not viable on SM120 (2× RTX PRO 6000): vLLM has no `qwen4_exp` at all; SGLang's branch hung in
three successive unported kernels (QSA decode, vision tower, PLE n-gram hash) after CUDA-13
toolchain surgery — 13 launches, no first token. B200 is what the checkpoint was qualified on.

| cell | steps | ok | wall | Nick |
|---|---|---|---|---|
| arcade | 77 | yes | 12 min | fine (the 27B is also fine here) |
| indoor3d | 200 (cap) | no | 23 min | dies on load: `no castle layout found` — generator's verifier rejected every seed |
| cards | 101 | yes | 16 min | "an *actual prototype* … the kind of thing (with some art) I'd put in front of someone" — nothing else has done this |
| npcs | 139 | yes | 19 min | "still not good" |
| rhythm | 179 | yes | 32 min | fine |
| platformer | 181 | yes | 22 min | reachability — not winnable |
| voxel | 200 (cap) | no | 26 min | loads clean |
| f1 | 200 (cap) | no | 26 min | "by *far* the best one we've gotten" — lapped every prior F1 |
| idle | 74 | yes | 11 min | too busy, no unfolding, long stretch with nothing for the player to do (likely the request, not the model) |
| openworld3d | 200 (cap) | no | 37 min | loads clean; not yet played |

- 6/10 called `done`; 4/10 ran to the cap. Every capped build was in a self-audit loop —
  "everything looks consistent, one thing…" — reading files in chunks and making small real
  edits, 2–3 s a turn, never deciding it was finished. The cap, not a crash, ended them; a
  capped build skips the gate and staging, so the one load-dead game (indoor3d) was the one
  the pipeline never judged. All nine others load clean under the headless probe.
- Verdict (Nick): "The quality when it works, is wayy better, but it still fails a buncha."
  Two cells (cards, f1) are the best of their kind Maestro has produced; the rest sit at or
  near the 27B, with the same failure modes — verifiers that never pass, levels that can't be
  won, cap-outs. Whether that clears 5× per inference-hour is open; the ceiling is real.
- Art never landed: 126 `generate_media` calls across the ten builds, 0 renders (LLM-only arm,
  no image worker), so every requested sprite and tile shipped as the checkerboard
  placeholder — voxel is a pink void, f1's grass and kerbs are checkerboards. A confound on
  every "feel" judgement, not on "unwinnable". Backfilled 2026-08-28 on the local 5090: the 126
  jobs requeued (status → pending AND `created_at` → now, or the 1800 s reaper re-fails them on
  sight) and drained at ~10.5 s/sprite, 6 s/tile, 126/126 landed; the six staged games re-staged.
  Flash-Next's art requests differ from the 27B's: a per-game style prefix, hex colours inline,
  the view named ("seen from BEHIND"), and "transparent background, isolated single subject" on
  every sprite — the matting came out clean where the 27B's prompts usually don't.
- What the run says about the pipeline rather than the model: the seeded-generator +
  verifier clause is where games die on every model (castle layout, platform reachability), and
  the first-`done`-is-answered rule assumes a model that calls `done`; this one audits until
  stopped, so a "you are finished" nudge — not a bigger cap — is the untested arm.
- Ops: two pods burned before a token was served — an Iceland RunPod DC with 250 kB/s to
  PyPI, and a network-volume `/workspace` that made venv unpack and weight load 10× slower.
  Probe a pod's DC bandwidth and `mount` in its first minute.

## 2026-08-28 — The Flash-Next DESIGNS built by the local 27B (arm `transplant27b`)

Question: how much of Flash-Next's ceiling is the design it wrote, and how much the builder?
The ten Flash-Next systems designs (`spec.request` of the flashnext runs, 8.6–9.4K chars each)
were copied verbatim into fresh runs — no designer call, ask preserved — and built by
qwen3.8_27b QUASAR NVFP4 on the local 5090 (ninfer fork, thinking on, xhigh, 131K int8 KV),
serially, with the image and mesh queues drained between builds by `local_gpu.py auto`.

| cell | Flash-Next own build | 27B on Flash-Next's design |
|---|---|---|
| arcade | 77 steps, ok, 12 min | 27 steps, ok, 9 min |
| indoor3d | cap (200), load-dead | 40 steps, ok, 27 min (castle generator's verifier passes) |
| cards | 101 steps, ok, 16 min | build + 11-step gate fix (`undefined.slice`), ok, 35 min |
| npcs | 139 steps, ok, 19 min | build + 22-step gate fix, ok, 29 min |
| rhythm | 179 steps, ok, 32 min | 28 steps, ok, 19 min |
| platformer | 181 steps, ok, 22 min | 21 steps, ok, 20 min |
| voxel | cap (200) | build + 19-step gate fix, ok, 38 min — `sfx.select is not a function` on input, past the gate |
| f1 | cap (200) | 40 steps, ok, 34 min |
| idle | 74 steps, ok, 11 min | build + 9-step gate fix, ok, 20 min |
| openworld3d | cap (200) | build + 8-step gate fix (terrain `height` missing from record), ok, 25 min |

- 10/10 called `done` and passed the gate; 5/10 needed a gate fix round (Flash-Next: 0/6 of
  its finished builds did). 126/126 art requests landed and were staged (`auto` swapped the
  card to the image queue between builds, ~1 min a swap, 5 meshes for indoor3d).
- Every Flash-Next cap-out (indoor3d, voxel, f1, openworld3d) finished on the 27B in 40 steps
  or fewer, from the same design. The self-audit loop is the big model's habit, not the
  design's fault.
- The 27B runs are single-turn-per-file: 21–40 build steps against Flash-Next's 74–181. The
  compaction count is 0–1 against Flash-Next's 1–8.
- Times are wall-clock including the image swaps, so not comparable to the B200 numbers.
- Play verdicts (Nick): arcade fine — the only one. rhythm fine but thin ("we need to ask for
  more"). cards about 70% of the way there. npcs did what was asked but is poorly designed.
  platformer: invisible enemies, extremely floaty player. idle: the passive-income upgrades do
  not work. voxel: looks bad, crashes on switching to a block. f1 did not load (the headless
  gate passed it — swiftshader vs real WebGL). indoor3d: the same issues as before.
  openworld3d bad.
- Verdict: **the design alone does not carry.** The cells split two ways. Where Flash-Next's
  own build played and the 27B's did not on the SAME design (cards, platformer, idle), the
  builder is the ceiling — the design named the system and the 27B shipped it dead. Where
  both models failed or came out thin (indoor3d, voxel, f1, openworld3d; npcs, rhythm), the
  design itself is the ceiling and no builder downstream fixes it — those are the cells to
  take to the designer prompt, not to a bigger model. The 27B on Flash-Next's designs plays
  about where the 27B on its own designs plays, so at this model size the designer swap buys
  nothing; the 5× is in the build.
- Ops: `scripts/local_gpu.py` defaults `NINFER_BIN` to mainline ninfer, which refuses the
  QUASAR artifact (`tensor descriptor does not match target contract: text/token_embedding`);
  the fork at `ninfer-quasar` serves it. `local_gpu.py llm` exits the moment the queue empties
  (between builds); `--idle-exit` is `auto`-only.

---

## 2026-08-28 — The gate asks where the button is (local 5090, qwen3.8_27b via ninfer `--vision`)

### The question
A one-line ask ("a village mystery where I talk to villagers to find who stole something") built
in 64 turns, the gate reported clean, and the game died the instant a person pressed PLAY — twice
over, on two successive fix rounds (`g.gain.exponentialRampToTimeValue is not a function`, then
`Village generation failed for seed 101`). PLAY was a DOM button at (550, 396); the poke clicked
(640, 360) and pressed Enter and Space, which the title ignored. Can the model read the screenshot
and say where to press, and would that have caught the two deaths?

### The naked call
One request to ninfer, the 1280×720 title screenshot as an `image_url` part, `reasoning_effort`
low, the JSON skeleton in the prompt. 2.1 s, 243 tokens: the three buttons with centres inside
each one. Then five games end to end (screenshot → model → click each target on a fresh page):

| game | title | model's answer | secs |
|---|---|---|---|
| npcs (DOM buttons) | Play / Continue / Help | 3 targets, all inside their buttons | 2.9 |
| arcade (canvas) | "press Space" | no targets, keys [Space] | 2.3 |
| cards (canvas) | START DUEL | 1 target at (640, 505), keys [Space] | 1.6 |
| rhythm (canvas) | lane keys | keys [D, F, J, K] | 2.7 |
| transplant RPG (canvas) | Begin | 1 target at (640, 535), keys [W, A, S, D, E, J, Esc] | 4.1 |

Every click and key changed the screen except the ones a title legitimately ignores (Continue with
no save; WASD before Begin). One wart: the model names keys as a screen prints them (`Esc`), and
playwright wants its own names — a table maps them.

### Against the deaths
The two pre-fix snapshots of the npcs run, through the real `error_gate.probe` on the queue:

| snapshot | fixed poke | model-named press |
|---|---|---|
| after the build (audio typo) | clean | `g.gain.exponentialRampToTimeValue is not a function` on Play |
| after fix round 1 (literal layout) | clean | `Village generation failed for seed 101` on Play |
| after fix round 3 | clean | clean |

24 s per probe (three page loads at 3 s settle each; the llm turn is 2–3 s of it).

### What shipped
`error_gate.probe` screenshots the loaded page, asks `prompts/probe_targets.txt`, and presses each
answer on its own fresh page — a title that leaves on the first press would otherwise hide what the
rest do. At most six clicks and six keys. The fixed poke stays as the fallback when the model cannot
be asked. The gate still detects BROKEN only: pressing what the screen offers is mechanical, and
what happens after remains the human's to judge.

### Not measured
Whether a second screen (the one PLAY leads to) is worth a second ask — the two deaths here were
both on the first press. A model that lists the HUD as targets on a game that starts without a
title; the cap bounds the cost, not the aim.

---

## 2026-08-28 — Broad strokes first (local 5090, qwen3.8_27b via ninfer, thinking on, 131K window)

### The question
"build minecraft" through the designer made an 18-system, 3,051-word design. Built, turn 0 spent
102K tokens of reasoning — the whole game thought through in its head — and ended with no tool
call and no content (run `e5351a382355`, killed). The doctrine's answer was "the fix is the
design"; the owner's was to try the loop first: tell the builder to think in broad strokes before
the first call and in depth when each file is written.

### The line
One line in `build.txt`, beside "Work in small steps": *The first reply settles only the file
layout and the records the files share, in broad strokes; each system is thought through in depth
when its own file is written, not before.*

### The builds
| arm | turn 0 | outcome |
|---|---|---|
| control: same design, no line | 102K reasoning tokens, no tool call | killed at turn 1 |
| broad strokes | 78K tokens, a file layout in the reply and 18 tool calls (list_files + 17 art asks) | ok, 44 steps, 32 min; ten modules at 2–17K tokens each; one gate round (three.js imported from `js/`), then clean |

Turn 0 still drafted every system in its think — "Let me plan carefully… Main systems: WORLD…
PLAYER…" — but stopped short of writing the code there and acted.

### A request the line was not written for
"a tower defense game" → a 16-system, 3,224-word design, built with and without the line:

| arm | turn 0 | steps | wall | compactions | gate |
|---|---|---|---|---|---|
| broad strokes | 58K tokens, 4 calls (list + three lib reads) | 31 | 20 min | 0 | clean on the first probe |
| control, no line | 68K tokens, 18 calls (list + 17 art asks) | 49 | 29 min | 1 | `Identifier 'bx' has already been declared`, one fix round, clean |

Both acted on turn 0, so this pair does not test the overflow. The owner played both: both do
what the design says, and the CONTROL plays better — the design's systems were poor to begin with
("pretty poor"), and the arm that spent 18 more steps on them shipped the better game. Fewer
steps is not the number. The line's one win is the overflow case (voxel, n=1); on a design that
fits the cap it may cost depth, which is the trade the owner said not to make. Verdict on the line
waits on a second overflowing design; if a fitting design again plays worse with it, it goes.

---

## 2026-08-29 — Systems are verbs (local 5090, qwen3.8_27b via ninfer, thinking on, 131K window)

### The question
The 2026-08-28 transplant grid split its cells two ways, and in the cells where the DESIGN was the
ceiling (npcs, rhythm) the designs read the same: `CASE FILE, VILLAGE, VILLAGER, SCHEDULE,
DIALOGUE, CLUE, SUSPICION, THIEF, ACCUSATION, PROGRESSION, PLAYER` — every noun in the game a
system, each paragraph a field list, and the thing the player actually does (question, catch a
lie, accuse) owned by no paragraph. The prompt asked for it: "one paragraph per system" beside
"every thing in the game is a RECORD with named fields" and a 10–18 count. The designer resolved
that by making the records the systems. The owner's read: two kinds of thing are mixed — game
systems (journal, accusation) and code systems (player, NPC, dialogue) — and the code ones are the
builder's job.

### The change
`design.txt` rewritten around one law: a system is what the player DOES or what happens to them,
never a kind of object. No count. The first system is the core loop and its paragraph is the
longest; the hardest system gets the most words after it. Numbers carry units. Records are named
only where a rule reads their fields. The opponent is a system of its own. Generators + verifiers,
rosters, screens/audio/art kept as they were. Old prompt kept in the lab as
`design_prompt_nouns.txt`.

### The arm
The two design-ceiling cells, one-line asks, designed and built by the 27B; control = the `short`
builds of 2026-08-28 (same asks, noun designer, already played).

| cell | design | build | gate |
|---|---|---|---|
| npcs `a8a5f99a5c95` | 2,747 w, 278 s; systems: TALKING AND CROSS-EXAMINING VILLAGERS (698 w), MOVING THROUGH THE VILLAGE, SEARCHING FOR EVIDENCE, THE VILLAGE CLOCK, THE THIEF'S COVER-UP, ACCUSING THE THIEF, CASE PROGRESSION | 60 turns, 1 compaction, 32 art landed, 12 js files / 71 KB | `Cannot read properties of null (reading 'seed')` — one fix round, 11 turns, clean |
| rhythm `1929bf18eb4e` | 2,668 w, 330 s; PLAYING A SONG (473 w), NOTE TIMING AND JUDGMENT (484 w), SONG SELECT AND UNLOCKS, GENERATED MUSIC, NOTE CHART GENERATOR, RESULTS AND PROGRESSION | 17 turns, 11 min, 5 art | clean on the first probe |

The npcs loop paragraph is mechanics end to end: six dialogue options with time costs (5 s / 8 s /
10 s), "Show a clue" gated on a clue found, a contradiction = a held clue whose
`contradictedAlibiPlace` matches the stated alibi → suspicion +30, villagers of reliability 1/2/3
leaking different truths, the thief lying below suspicion 30. One villager record, named once, with
the fields those rules read. The control's ACCUSATION paragraph was 130 words of field names.

### Verdict
Owner played both against their controls: "the system ones are way better". Shipped.

### What the gate caught
The npcs design wrote a verifier its own numbers cannot pass: unique alibi times across 7–9
villagers from a roster of 3 times, so every seed re-rolled and the case generator returned null.
The fix round deleted the check. Same class as the control's "Village generation failed for seed
101" — the designer's counts are not checked against each other, and a verifier the given values
cannot satisfy is a crash the gate sees, not a design the builder can save. Open.

### Also shipped
`run --fix` renamed `run --change` (`/api/games/{id}/change`, status `changing`, cursor kind
`change`). A person who played a game that works asks for a change; a fix is what the error gate
sends. The request text now says "asked for this change… keeping everything else playing as it
does" instead of "reported this… fix exactly that". Not measured — a name, not a mechanism.

---

## 2026-08-29 — Records and rules (local 5090, qwen3.8_27b via ninfer, thinking on, 131K window)

### The question
The verbs designer (above) fixed the noun designs, but its examples ("moving through the village",
"a clock running out") were a description of a part of the game and a single rule, not systems —
and both were the motivating game's own words. The owner's read: this is normalization. Combat
does not OWN hp; hp is a fact about a creature, and the trap, the potion and the sword all write
it. That is ECS, and MVC says the same: records are the model, systems are the rules that change
them, screens are the view.

### The change
`design.txt` rewritten in three parts. RECORDS: one paragraph per kind of thing, its fields and
given values, rosters listed here; a field lives on the thing it is a fact about. SYSTEMS: one
paragraph per system, made of RULES — condition and effect on named record fields, with numbers;
generators are systems whose verifier must be satisfiable by the counts above. CORE LOOP: a cycle
through the systems by name. Then SCREENS / AUDIO / ART unchanged. Every battery-game example
was stripped (the verbs prompt carried "questioning a villager", "accusing the thief", a clue
record; the first draft of this one carried more), and the remaining examples are genres not on
the battery. Old prompt kept in the lab as `design_prompt_verbs.txt`.

Two clauses added during the day, each after a design showed the gap: (1) a roster is the KINDS
a game draws from — which member is the culprit, the layout or the route is the generator's,
never written in the roster (the first npcs design hard-coded the thief and all ten clues, and
its generator only jittered building positions); (2) the first line says 2D or 3D, with a rule
for which, and in a 3D game outdoors the landscape is a GENERATED WORLD named in one sentence
with no size or count, the systems placing things by its regions — because neither 2D design had
any way to know `compose_world` existed, and `build.txt` offers it only to a 3D game.

### Designs
| ask | arm | words | secs | read |
|---|---|---|---|---|
| npcs | ecs (tainted) | 2,776 | 229 | rules clean, two win paths (suspicion 70 / thief stress 80); the CASE was a roster — thief id 5, ten clues, alibis all literal |
| npcs | ecs2 (clean + roster clause) | 2,726 | 222 | generator chooses the thief from the seed, roles, four-phase schedules under location capacity, draws 8 clues with ≥4 on the thief; went turn-based (4 actions/day) |
| rhythm | ecs | 2,749 | 211 | LCG given, drums/bass/lead by beat index, holds with lane blocking, health fail, unlock ladder; verifier counts checked (song 1 ≈21 notes ≥16, song 8 ≈230 ≤260) |
| rhythm | ecs2 | 2,510 | 212 | no holds, one 94 s song, chart a Bernoulli roll every 0.25 s — variance, and neither prompt asks the chart to follow the music |
| openworld | ecs | 2,567 | 160 | NUT_KIND and TREE rosters as kinds; flood-fill verifier; raccoon patrol/chase/lost/cooldown with bush concealment; freshness decay, hunger, 3 days |
| skyrim ("an open world RPG like skyrim") | ecs | 4,287 | 224 | 13 records, 12 systems, top-down 2D; use-based skill XP, armor floor 1, weather × speed, fog × aggro, pack aggro, wolves flee; worldgen is per-tile noise; quest givers placed, never talked to |
| skyrim | ecs3 (2D/3D clause) | 4,109 | 288 | 3D; WORLD system opens with the compose_world sentence; enemies by biome, NPCs/loot/quests per region, DIALOGUE present — and ALSO `sizeX 2000 m, sizeY 1400 m, regionCount 6` and a six-region roster with radii |

In all seven: no noun-systems, no rule-less system paragraph, every verifier satisfiable by its
own counts. The old failure (a verifier the numbers cannot pass) did not recur.

### Builds
| cell | run | steps | wall | compact | gate | art |
|---|---|---|---|---|---|---|
| openworld ecs | `59f3606e683c` | 30 | 841 s | 0 | clean first probe | 16/16 |
| openworld short (control) | `f8a8db2b6aa5` | — | 1,431 s | — | — | — |
| skyrim ecs (2D) | `e8d51252d08e` | 33 | 1,310 s | 1 | clean first probe | 25/25 |
| skyrim ecs3 (3D) | `d89a69a0f3a3` | 185 + 23 fix | 6,650 s | 5 | 2 rounds: `Unexpected token ')'` ui.js:157, then `Cannot access 'inWater' before initialization` | 0/28 (see below), 28/28 on top-up |

Turn 0 on the 4.3K-word designs: 42K and 22K reasoning tokens, then acted (list + lib reads) —
no overflow, the 2026-08-28 pattern. The builder's file layout mirrored the design's shape in
every build: a data/records module first, systems, then render/ui.

**openworld ecs, played:** the owner: "ended up better than expected". One defect the gate cannot
see: `ui.start()` — the function that hides the title overlay — declared, wired to the button,
never called from `startGame()`; the game ran under the title. Same for `resume`/`restart`.
The `draw_hand` class of 2026-07-27, and nothing throws.

**skyrim ecs3, the world:** `compose_world` called on turn 1 with the design's sentence verbatim
(hills, passes, peaks, valley, farmland, swamp; ruins, villages, watchtowers, cave mouths). The
world answered 600 m and five regions. The builder then honoured the DESIGN's numbers: `terrain.js`
scales the world by 2000/600 in x and 1400/600 in z (non-uniform — trees and slopes squashed),
heights ×3, and `regionAt` uses the design's six named regions, not the world's. "Every number
below is a given value to write into the code as-is" did exactly that. The clause "never a size
or a count" reached the designer and it wrote the sizes anyway — the record shape ("the World
record carries…") outpulled the sentence. Open: a generated world's record carries no size and no
region roster; regions by category only, and the world's are the game's. Clause added to
`design.txt` the next morning, and the stretch taken out of this game with `run --change`: the
note named the mechanism (drop the scale, game space is `world.sizeM`, place by `world.regionAt`,
map the six design regions onto the world's five by category) and the builder rewrote
`terrain.js` and the generator in 31 steps, then one gate round for a shadowed import
(`const worldRegions = worldRegions()`), 11 turns. The world at its own size reads as a place —
hills, trees at true scale — where the stretched one read as a smear.

**skyrim ecs3, the steps:** first `done` at 168. After each compaction the model re-read every
module to re-ground, found something, edited — and edited against a stale picture of files whose
read it had lost: turns 55–64, six `old_text was not found` on `world3d.js`; the lighting block
replaced at 99–101 and again at 126–134 because the first round was gone; turn 103 discovered it
had never added `world.group` to the scene. Then two gate rounds. Round 1: a one-character typo
(`el.onclick = () {`) at the address the gate named; the fixer read the whole 260-line file six
times ("looks balanced at a glance") and never quoted line 157, then fixed it at turn 11 by
rewriting the file from scratch. Round 2 (a TDZ with a stack) closed in 11 turns. Open, on the
gate side and still BROKEN-only: quote the text at the address, not only the address.

**skyrim ecs3, the art:** all 28 renders failed `pending longer than 1800s with no worker` — the
reaper's `MAESTRO_STALE_PENDING_SECONDS`. One card; `local_gpu auto` held it on `llm` through
this build's long turns and the worldgen legs, so the image queue waited past the rule. Prod has
a pod per queue and the rule is right there; on the one-card box a long world build trips it.
`run --assets` topped up 28/28 in ~6 min.

### Verdict
Shape shipped: records / systems-as-rules / core loop, no battery examples. Owner's play verdicts
on the skyrim builds pending. Open, in order: the generated-world record clause; the gate note
quoting the line; the post-compaction re-read loop.

---

## 2026-09-02 — Flash-Next to prod: Pennyroyal on one RTX PRO 6000

### The question
Flash-Next was the best builder in every battery it entered (cards and f1 best-ever on the B200,
2026-08-27; f1 "one of the better ones" on a rented 6000 Pro, 2026-08-31), and the local 27B on
its designs was a way to have the designs without the model. The 6000 Pro run served it through
llama.cpp at 110 tok/s. The fork jpezzulli/sglang-rtxpro6000 ("Pennyroyal") claimed the same
card at ~2× decode and ~4× prefill WITH vision, which llama.cpp could only do with an mmproj.
Two questions, in order: does it build a game end to end, and can a pod reach serving inside
the ≤180 s from create that the autoscaler's scale-from-zero shape assumes.

### Serving
Trialed 2026-09-01/02 on a PRO 6000 SE at $2.09/hr: reasoning split, tool calls and the play
gate's screenshot turn all work natively. Decode 203 tok/s short-context, ~100 at 54K; two
concurrent streams 1.7× aggregate at 54K, four collapse (the mamba cache evicts and re-prefills).
Three builds through it: kraken 51 turns / 16.5 min, lightswitch 75 / 24 min, both gate-clean
with art from the local image queue, and f1 58 steps / 19.5 min, gate 0 rounds, ~$0.68 — against
~$1 for the 27B on a 5090. `--ple-offload-embedding` is load-bearing (without it the weights
alone exceed 96 GB); the canonical serve script carries it and the image execs that script.

### Boot
First true number from the network volume: **451 s** launch to healthy, and a full chain of
~435 s from create. Attributed, in the order it fell:

| thief | cost | fix |
|---|---|---|
| namespace helper hashing the whole 126 GB checkpoint (no HF download metadata beside it) | ~170 s, silent | real metadata written on the volume once; +187 s → +16 s to "load weight begin" |
| NVFP4 repack at load, CPU-bound (NVMe was 1.7× faster than the volume, not 10×) | 102–123 s | `--load-format prepacked`: the post-repack state dumped once to one flat file, restored in 21–55 s; tokens identical to a normal load |
| FlashInfer fused_moe cutlass JIT on every fresh pod (the cache the image baked was keyed to older flags) | ~285–300 s | compiled ops promoted to the AOT dir, which is the only place the JIT trusts without rebuilding |
| first-launch cgroup OOM at 188 GB (pack read filling page cache) | ~50% of pods, one relaunch | fadvise on the pack reader; three-attempt loop kept in the entrypoint |
| image pull, 28 GB | 53–110 s | two-stage squash to 16.7 GB (rm in a later layer is a whiteout): system cudnn/nccl/NPP/cuFFT gone, CUDA libs symlinked into the pip wheels |

The fork's `.git` is load-bearing: the serve script runs `git rev-parse` under `set -e`, and
the diet that removed it died in 3 s on three pods.

Final image, three fresh pods: **192 / 97 / 220 s create-to-serving, average 170 s** — under the
gate. The engine phase is 84–105 s and stable (restore 36–55 s, the spread is volume contention);
the rest is provisioning and the pull, which a host that already holds the image skips.

### The first two prod builds (2026-09-03, the cards request, EU-RO-1 Workstation Edition)
Same request twice through the deployed stack, one setting apart. With `LLM_REASONING=none`
(prod's value since the 27B days): 200 steps in 7m21s, `reasoning_tokens` 0 on every turn,
40–50 completion tokens per turn, 108 edits and 86 twenty-line reads, never called `done`, a
title screen and ~2,100 lines of code one missing export from loading. With `medium`: 39 steps,
7m53s, `done` on its own with a summary that named every module's exports as checked, and a
playing game — hand fanned with art, AI opponent taking its turn, mana pips, End Turn — with zero
console errors. Boot: 205 s create-to-serving on the first pod (a host that had never pulled the
image), 110 s on the second (warm host). The thinking measurement of 2026-08-25 reproduces on
the new engine. A fourth build through the engine-on-volume image (`llm-v16`, 7.0 GB against
16.7): 129 s create-to-serving on a host that had never pulled it, a playing game at 47 steps,
and the gate ran on the same pod as the build once llm idle exit was 60 s.

### Verdict
Promoted. The llm image is Pennyroyal on the RTX PRO 6000 alone; the ninfer/llama.cpp 27B image
and its two-engine boot are gone from the repo, and the 27B stays local on the 5090 for testing.
The llm volume is in another datacenter than the art, so the llm queue carries its own
`network_volume_ids` — a list, one volume per datacenter, because the Workstation Edition
(cheaper, faster) exists only in EU-RO-1 and US-NC-2's Server Edition stock has droughts. The
EU-RO-1 volume was provisioned the same night: the pack copied volume-to-volume in 9 minutes
once the shape was right (`docs/deploy.md`), after five GPU pods and four gateway-upload pods
that were not. Open: the loader patch rides the image as a tarball until it is upstreamed
to the fork; SE capacity in US-NC-2 has twenty-minute droughts, and the volume pins the
datacenter, so a drought is a wait.

## 2026-09-03 — Three image-to-3D candidates against TRELLIS 2 (local 5090)

### The question
`docs/technology_analysis.md` held three 3D candidates: TripoSplat (image → gaussians),
img2threejs (image → procedural three.js), LATO.2 (mesh → vertex + topology flows). Does any
of them make a better game prop than the TRELLIS 2 `512` pipeline from the same input?

### The arm
Five subjects from the selecting-mesh lab (watchtower, stone cottage, barrel, broken cart, oak),
each with its ComfyUI subject image and its TRELLIS 2 `512` mesh already on disk. Every
candidate got the same five; renders from three fixed orbit views (gsplat for the splats, open3d
for the meshes). The oak is a bad input — cropped, busy background — and every tool failed on it
alike, so it counts for nothing.

| candidate | input | per subject | VRAM | output |
|---|---|---|---|---|
| TripoSplat (MIT, code + weights) | image | 6.9–8.0 s, 5.5 s load | 4.6 GiB | 3DGS `.ply`, 32K–262K gaussians |
| LATO.2 (MIT code; weights on HF, no separate licence) | TRELLIS mesh | ~9 s | ~8 GB | `.obj`, 2000 or 5000 vertices |
| img2threejs (Apache-2.0) | image + a frontier agent | ~11 min, ~100K tokens, 24 tool calls | none | 1,100 lines of TypeScript |

### What came out
**TripoSplat** is by far the most faithful picture: the watchtower has its roof, rails and
stairs, the cottage its thatch, chimneys and window frames, the barrel its hoops and rivets —
where the TRELLIS mesh is a blob with the right silhouette. 32K gaussians (2.2 MB) is visually
the 262K (18 MB) result. It is disqualified on two counts a game cannot forgive: the output is
a splat, not a mesh, so nothing collides with it and three.js needs a separate renderer for
it; and the lighting is BAKED — the barrel's unlit side is dark from every angle because the
photo's was. A prop that cannot be relit cannot sit in a scene.

**LATO.2** takes the TRELLIS mesh, voxelises it and regenerates vertices and connectivity. At
2000 vertices the watchtower's open frame came back as filled walls, the cottage as faceted
noise, every mesh non-watertight with more faces than twice its vertex count; 5000 vertices
changed nothing. The readme warns of holes and wrong connectivity and expects scale to fix it.
Its DINOv2 conditioning calls xformers in fp32, which has no sm_120 kernel; `XFORMERS_DISABLED=1`
puts DINO on plain attention and the sparse blocks still route through xformers in fp16.

**img2threejs** is not a model. It is a Claude Code skill: an agent reads the image, writes a
sculpt spec against a 2.9K-line validator, and a 4.1K-line generator emits a three.js factory,
pass by pass behind vision-scored gates. On the barrel the agent stopped at the first pass with
its own Tier-1 gate red (silhouette IoU 0.67 against a 0.85 bar) and scored the result 0.62:
reads as a barrel, hoops gloss-black, rivets invisible. Two silent generator defects on the way
(attached cylinders emitted at zero length, texture maps written as absolute paths). What it
measures is the frontier agent driving it, which is not the model that builds our games.

### Pixal3D, the same afternoon
Pixal3D (TencentARC, MIT code and weights) is TRELLIS 2's backbone with pixel back-projection
for image fidelity. It had been cloned and launched on 2026-08-20 and left no result: its
pipeline constructs a `briaai/RMBG-2.0` background remover at init, and that repo is gated
(and non-commercial), so the process dies before the first sample. Our subject images carry
alpha, which the pipeline honours, so the lab checkout tolerates a missing rembg. The default
1536 cascade OOMs the 5090 at 26 GB inside the shape stage; 1024 cascade with `--low_vram`
(weights staged CPU→GPU per stage) runs. No flash_attn in the venv; `ATTN_BACKEND=sdpa` works.

Same five subjects, and the TRELLIS 2 `512` server re-timed on the same card the same hour:

| | TRELLIS 2 `512`, resident | Pixal3D 1024 cascade, low_vram |
|---|---|---|
| load | once: 8 s + 11 s warmup | 41–44 s per process |
| preprocess + camera | in sample | 5 s (MoGe FOV estimate) |
| sampling | 6.5–10 s | 30 s |
| to_glb | 1.2–2.2 s | 20 s (a 1024 grid to remesh; the 15K target does not shorten it) |
| per subject | **9–12 s** | **~55 s warm, ~100 s as run** |

Geometry is a real step up: the watchtower keeps its balusters, stairs and roof plank stack
through a 15K decimation where the 512 mesh is a blob with the right silhouette; the cottage
has its thatch ridge, chimney and door recess. Two catches. One subject in five (the
watchtower) came out with metallic = 1.0 across the whole texture — the export is the same
`o_voxel.to_glb` our server calls, so the texture model predicted it — and a fully metallic
prop renders black in a scene with no environment map. And Pixal3D has no 512 tier, so on a
32 GB card the choice is TRELLIS-512 at ~10 s against Pixal3D-1024 at ~55 s; the resolution
is most of that gap, and TRELLIS 2's own 1024 cascade would pay it too.

### Verdict
None replaces TRELLIS 2. Splats are out as a class until one can be turned into a lit,
collidable mesh; LATO.2's output is broken at every count it offers; img2threejs is an idea
about procedural props, not a tool; Pixal3D is TRELLIS 2 at 1024 with a 5× bill and a
metallic roll of the dice. All four stay investigated, none adopted.

What Pixal3D did show is that a 1024 cascade FITS a 5090 when the stages are offloaded
between steps. Our server drops to `512` because 1024 OOMs CuMesh on a fresh 32 GB card.
Same weights, per-stage offload, four times the voxels for ~30 s more a mesh — that is the
open lever, on the model we already run.

## 2026-09-03 — What TRELLIS 2 wants: input style and the 1024 tier (local 5090)

### The question
The meshes are fine, not great. Two levers are ours without touching the model: the picture it
is handed, and the tier it runs at. TRELLIS 2 has no multi-image conditioning, so a multi-view
path (Pixal3D, Sep 2026) is not open to it.

### The arm
Five subjects (watchtower, cottage, barrel, cart, oak) drawn in five styles at one seed, each
through the server at `512` (texture 1024) and `1024_cascade` (texture 2048), fifty meshes. Four
styles go through the lab's Qwen-Image path with the same framing sentence and only the style
clause changed — A photographic (the lab's subject prompt today), B low-poly game asset, C
hand-painted game asset, D clean CG render — and E is the PRODUCT recipe exactly as
`build_image_job` emits it for a mesh: NetaYume through the item workflow, "masterpiece, best
quality" in front, photo in the negative, matted by TRELLIS's own BiRefNet.

### What came out
**The product's own recipe is the worst input of the five.** NetaYume is an anime checkpoint;
asked for a watchtower it drew a clock tower on wheels, its barrel is a sphere, its cart is a
line drawing with debris, and every mesh inherits the drift. The item recipe was tuned for
sprites, where an anime lean is the point; a mesh source image needs volume, and it is the only
kind whose picture is never seen by the player.

**Clean render styles reconstruct best.** B (low-poly) and D (CG render) come back as the
picture: crisp planes, the right rails and wheels, no smearing. C (hand-painted) keeps its
painterly texture through the bake and reads as a finished game prop — the best-looking meshes
on the sheet. A (photographic) is faithful but muted, and photographic subjects are the ones
that arrive cropped (the oak, twice today).

**The 1024 tier does not earn its 4×.** Geometry at 1024 is not visibly better than 512 on a
prop-sized subject at a 50K decimation, and its TEXTURES are worse in every row: darker,
desaturated, the hand-painted oak turned near-black, and the CG-render oak lost its whole
canopy to a bare grey trunk. 512 kept the colour of every input. 46 s against 10 s a mesh,
and it holds ~45 GB of host RAM resident — the run that measured it took the desktop down once
before a watchdog was put on it (`docs/local_dev.md` already says `--ptype 512` on the 60 GB box;
this is the number behind it).

### Verdict
Stay at `512`. Change the mesh kind's SOURCE IMAGE: route it through Qwen-Image with the
framing sentence and a stylized-render clause (hand-painted or clean CG, never anime, never
photographic), not through the sprite recipe. That is a `_kind_recipe` change and one sentence
of prompt, measured here on five subjects. Wired the same day (`comfyui_tools`: the mesh kind
renders through `txt2img_subject.json` with the framing fixed and the style clause read from
`MESH_STYLE`, an `img2img_subject.json` beside it for regenerate). Hand-painted is live; low-poly
is the other clause in `MESH_STYLES`, one name away, because the two reconstructed alike and
which one a game should look like is a taste call not yet made. The battery of builds is owed.

## 2026-09-04 — The designer's 2D/3D call: six phrasings of one clause (Flash-Next, prod)

### The question
A one-shot "open world game where you explore an island and find treasure" came back from the
prod designer as a 2D tile map with `compose_world` never mentioned. The clause telling the
designer about the world tool was in the prompt. Is the tilt the clause's wording, or the model's?

### The arm
Design stage only, no builds: 8 asks (the island, the racing career, the monster quests, a farm
with seasons, a card battler, a permadeath dungeon crawler, a haunted forest at night, a
derelict space station) × 6 variants of the one paragraph in `design.txt` that describes the
choice, 48 design jobs enqueued as a batch on the prod llm queue while pods were warm. Read
off each design: the first line's 2D/3D and whether `compose_world` appears anywhere.

- **A** the shipped clause: "a 3D game set in open countryside may instead take its landscape
  from compose_world…", ending on "A 2D game's map is a generator the builder writes in code".
- **B** same content, 2D sentence first, world sentence last, "may instead" removed.
- **C** B, plus the first line must name where a 3D landscape comes from.
- **D** a rule: walked/sailed/driven settings are 3D via `compose_world`, board/grid/lane/screen
  games are 2D, indoor 3D is code.
- **E** "Pick 2D or 3D by what fits this game best; neither is the default", then B's sentences.
- **F** "Before anything else, decide whether this game is better in 2D or 3D, judged by what the
  player would see and do in the best version of it; neither is the default", then B's sentences.

### What came out

| ask | A | B | C | D | E | F |
|---|---|---|---|---|---|---|
| island | 2D | 3D w | 2D | 3D w | 3D w | 3D w |
| racing | 2D | 2D | 2D | 3D w | 2D | 2D |
| quests | 2D | 2D | 2D | 3D w | 2D | 2D |
| farm | 2D | 2D | 2D | 2D | 2D | 2D |
| cards | 2D | 2D | 2D | 2D | 2D | 2D |
| dungeon | 2D | 2D | 2D | 2D | 2D | 2D |
| forest | 2D | 2D | 2D | 3D w | 2D | 3D w |
| station | 2D | 2D | 2D | 3D | 2D | 3D |

(w = `compose_world` named in the design.) Design length was the same in every arm, ~18K
characters; no arm produced an empty or malformed design.

**Order and hedging change nothing.** A, B and C together: one 3D design in 24. Moving the
world sentence last and dropping "may instead" did not move the model, so the tilt is not
"the most recent concrete clause wins" — this model is not distracted, it defaults to 2D when
the choice is left implicit.

**A rule moves the most and moves too much.** D: five of eight 3D, including racing and the
monster quests, which are good 2D games (the shipped Contract of the Demon Lord is one).

**Asking for the decision is what works.** E flips the island alone; F flips the island, the
forest and the station, and F's station is 3D built in code, no world — the indoor case
handled without a rule saying so. Cards, farm and dungeon stay 2D under both.

### Verdict
F is `design.txt`. The designer keeps the call; the prompt now asks it to make the call first,
with neither answer the default. Measured on designs, not games: the first F-designed 3D builds
are the next reading, the island rerun first.

## 2026-09-04 — MiniMax-H3 image-to-video: what a clip can hold (RTX 5090)

MiniMax-H3, pruned int8, via ComfyUI 0.30.1, 768² frames, 20 steps, ~1.3 s/frame:

- A 73-frame PINNED clip (first frame = last frame = the source still) runs 60–75 s and holds
  identity perfectly across the whole clip; the lower-body diff signal peaks TWICE — a real
  two-stride walk, not a sway.
- A 22-frame pinned clip runs ~20 s and holds one stride — enough for an idle.
- One 209-frame TOUR clip, scripted front → right → back → left → front, runs 266 s: the turns
  land in script order, the back view is plausible, and drift is minor and localized (a shield
  emblem, a sword's tint, a slight scale creep) rather than identity loss.
- A back-view still cut from the tour seeds a 73-frame walk as well as the original front-on art
  does — the tour's frames are good enough to re-seed from, not just to look at.
- Judging motion from an 8-frame strip called two real walks wrong. The per-frame diff signal (or
  the clip itself) is what to judge; a sampled strip throws away the frames the motion is in.

- A 22-frame PINNED TURNTABLE ("turns in place to face the right side of the screen, then away,
  then the left side, then back; no walking") runs ~20 s and yields the other three facings —
  the 209-frame tour did the same job at 13× the cost, and the walking it carried was never
  used.

**Scheme chosen:** one turntable clip per character (yields the three other facing stills) plus
one pinned clip per (direction, animation) — never a time-scripted prompt asking for several
beats in one clip. `worker/anim_sheet.py` and `tools/comfyui_tools.build_anim_payload` carry
this scheme.

**Which turntable frame is which facing** — measured on the three characters of the first
anim-driven build (knight, troll, specter, 2026-09-04): the model turns at a steady rate, so
the quarter marks of the moving span (the frames between the pins) are the right profile, the
back and the left profile, on all three. Scoring frames by silhouette instead — the back as
the most mirror-symmetric middle frame, the profiles as the most lopsided frames unlike the
front — missed by two to three frames on the knight and the troll, because a held sword or
club makes the true back lopsided; the shipped knight sheet drew the back when walking left.
The frames also come in identical pairs (the model's effective rate is half the clip's), so
"nearest frame" is a two-frame window either way.

**Cost per character at full spec** (four directions, walk/idle/attack): the first build ran
walk and attack at 73 frames (76 s each) and measured 13.4 minutes a character, 97% of it
generation and 75% of that the eight 73-frame clips. Four arms on the knight still, same
model, 2026-09-04 afternoon:

| arm | frames | s | what came back |
|---|---|---|---|
| walk, pinned | 22 | 36 | one stride, legs alternate, closes on itself (last vs first 1.0) |
| attack, pinned | 22 | 36 | wind-up 3 frames, held 8, return 3, closes; no smears |
| "walk left, hard cut, walk right", unpinned | 39 | 42 | a real cut at the midpoint (diff 48.7) — to a MIRROR of the same front-facing walk; the facing follows the seed still, not the prompt |
| four facings, three hard cuts, unpinned | 73 | 72 | no cuts: held front 13 frames, then walked WHILE turning — right ~26 frames, back ~10, left ~15 |

Walk and attack ship at 22 frames, 8 cells at 12 fps: 13 clips × ~36 s ≈ 8 minutes a
character. Cuts do not replace the turntable — the model will not change facing on a prompt —
but the last arm says one clip can carry every facing's walk if it is segmented by facing;
parked. The next lever is the fused turbo checkpoint (4 steps against 20, same weights and
license), unmeasured.

**Attack was the weak animation at 73 frames.** Walk and idle rows came back clean on every
facing; the 73-frame attack rows carried MiniMax's slash smears (a sword drawn at three times
its size for a frame, a club that becomes a torch) on every character. The 22-frame attack
has no room to drift; verified on the knight, the troll and specter rows are the next look.

**Where the 36 s went.** The video leg launched ComfyUI with `--cache-none`, and under it every
prompt re-requested the 32B text encoder, the VAE and the 21 GB transformer; the two do not fit
32 GB together, so each clip paid a swap. With node caching on, the same 22-frame clip: 20 s at
20 steps, 12 s at 10, 8 s at 6, on the dense checkpoint; 8 s at 4 steps on the fused turbo
checkpoint (`MATLOWAI/minimax-h3-fused-turbo-int8-convrot`, a lightx2v 8-step LoRA and a
motion LoRA fused in). The turbo's attack ghosts the sword at 4 and at 8 steps — the fused motion
LoRA, not the step count — where the dense checkpoint at 6 steps came back clean; seed moved
attack quality more than steps did (two 10-step seeds: one smear, one clean). The turntable
is the clip that does NOT survive six steps: the knight dissolved into a red blob twice
mid-turn and its profile stills seeded every side view as a 3/4 back. Shipped: caching on, the
turn at 20 steps (34 s, once), loops at 6. 13 clips ≈ 2 minutes a character (measured 110 s).
The turbo stays a lab file.

**Cuts.** On the turbo, "walking facing the camera, halfway a hard cut to walking seen from
behind" CUT — frames 0–29 front, 30–38 back, no turn — where the same ask for side profiles
mirrored the front walk instead. A cut is real; a profile is what the model will not turn to
on a prompt, so the turntable stays.

## 2026-09-04 — Sprites render through Qwen (local 5090)

The art lab's sprite re-bake-off (`labs/art-lab`, prompts from real builds) had
NetaYume losing to Qwen-Image-2512 on characters, and the routing never shipped: only mesh
subjects moved. Now every non-tile kind — sprite, scene, anim still, mesh subject — renders
through the one Qwen subject graph, prose verbatim (no danbooru quality tags), with the
lab's frame negative. The demo build's own knight and troll prompts, re-rendered: coherent
armour and a readable face where NetaYume's knight was a helmet on a smear; 20 s a still
with the model resident. The anime item workflows are deleted.


## 2026-09-06 — The play critic's impressions as a change note (prod, Flash-Next via Pennyroyal)

The play gate ends with a `judgment.impressions` paragraph — depth, clarity, pacing, "for a
human, changes nothing" — that lands in `play_report.json` and nobody reads. Read against the
owner's own verdicts on six prod games, it named every mechanics-and-tuning complaint the owner
had (guppies flee the size-8 player, the racer stalls at 0 m/s off-track, the brawler's swing
lands only with the monster in front) and was blind to the two things the owner ranked first:
art that does not fit (foreground kelp, clashing tiles — the verdict prompt never asks) and the
game's meta-structure (systems that work but mean nothing; the non-playing parts of a card
game — out of reach of ten turns). Its tone is positive on every game, a 1 included, so it
ranks nothing.

The arm re-tests the 2026-08-08 ruling ("a grade says what is wrong, not what to do") with a
critic that played the game from screenshots instead of reading the source: the paragraph,
verbatim, as `--change` on three built games the owner had rated, against the untouched
control, played blind side by side.

| game | owner | change | verdict |
|---|---|---|---|
| fish `d008f1b32bef` | 3 | 12 steps | better, barely |
| racer `1ef4497514ab` | 2 | 62 steps | better, barely |
| brawler `f3569203412c` | 4 | 44 steps | worse: the complaint was the harness's ("landing damage felt uncertain") — hitting was trivially easy for a person — and the build tuned a game that was not broken |

What this measured is narrower than the 2026-08-08 ruling: a REVIEW — positives and negatives,
no proposal, from a player that presses ten keys with no reaction time — handed straight to the
fixer. Where the review named a real tuning symptom the fix bought a little; where its
difficulty was the gate's own, the build tuned a game that was fine. It says nothing about
whether the model can PROPOSE an improvement when asked for one: the paragraph above is a
critique, not a "what and how", and no arm here asked for the latter. That arm is next
(`fish`, local 27B): the same review, once as the change note and once first turned into an
improvement prompt by the model, against control. Separately, the verdict prompt never asks
about the art — the one thing the critic can see and the owner ranked first.

**The proposer arm (same day, `fish`, local 27B via ninfer).** The review was first handed to
the model as a designer with the game's systems design beside it, asked for a CHANGE NOTE with
numbers; it wrote two: guppy `flee_distance` 60 → 35 and `base_speed` 90 → 75. That note as
`--change` on the same fish: 10 steps, 46 s, one line of `species.js` changed, exactly the two
numbers, and `done`. Played against control: not enough to warrant it. The review-as-note arm
on the local card was stopped before its first turn — the prod row above already covers it.

Ruling: the ceiling is the CRITIC, not the fixer and not the proposer. A player that has a few
seconds of a game — ten key presses, no reaction time, no second screen — has nothing to say
that a build should spend a turn on, and a proposer given only that review can only sharpen its
one tuning symptom. Feeding the play gate's judgment back into a build is closed until the
player can play. The gate stays what it is: it detects broken.

## 2026-09-06 — Stage 1 on prod: six one-shot games, two players (Flash-Next via Pennyroyal)

### The question

Whether a game asked for on the rented card comes back running — loads, takes input, holds
up under play — with the loop as it stands (records-and-rules design, thinking on, the helper
library after its fix, `generate_media` with kinds). The last play-rate number was the library
arm's 1/6 (2026-08-22), before the fix and on the local 27B.

### The arm

Six games, each one request and no change note, through the public front door. The owner made
four: a racing game with a career mode, upgrades and races unlocked by winnings; a game of quests
to slay monsters; a fish eating fish to grow; an open world island with treasure to find. A
friend, unprompted and unhelped, made two overnight and this morning — a Zelda-like and a visual
novel; the requests are theirs and are not copied here.

### What came out

Six of six ran, took input and did not crash. The racing game came back a full game of its
design — career, upgrades, unlock ladder — with bugs that were fun rather than fatal, and not
the game the owner had pictured from two sentences, which two sentences do not earn. The
monster-quest game was not what was asked for and was the one the owner enjoyed most; the fish
game was solid with cosmetic faults and held the owner for twenty-five minutes. Both are now
demos on the front page as what a one-shot gives. The island functioned and was not good: it
came back flat, as did all six — no build called `compose_world`, and none called
`compose_scene`. The friend's two both played; their word on it: "Gorgeous graphics though. And
TONS of improvements in the UI of the visual novel so far."

Art: the model chose generated art where a picture was worth it and code where it was not, and
the owner was content with the call on every game.

### Verdict

Stage 1's running bar is met on prod, and `generate_media` is settled — the tool stays, on the
owner's judgement of what the games looked like, which is the only judgement the roadmap ever
named for it. `compose_scene` came out the same day: uncalled in six prod games and in every
battery since it landed, it was schema paid on every turn for nothing; the asset store went
with it, the scene chain having been its only caller. `compose_world` stays,
because the open question is not the tool but the designer: an open-world island, asked for
without the word 3D, came back 2D, and why the 2D/3D call lands flat when the ask does not
decide it is the next thing to measure.

## 2026-09-06 — Cost per finished game off rented pods (prod, Flash-Next via Pennyroyal)

### The question

The stage 3 number: what a one-shot game costs on the rented cards at the current caps, with
the failure rate beside it. Nothing measured on the owner's box stands in — cold start and the
serving stack are in the pod's seconds.

### The arm

The six stage-1 games above, costed from RunPod's per-pod billing (2026-09-03..06, grouped by
pod) joined to the platform's `workers.pod_id`. Each pod's real bill is split across the jobs
it ran by exec-second share, so a pod's boot, idle and shutdown land on the games that used it
and a pod that did no work lands on nobody. LLM jobs name their build; art jobs name their game,
so art is per game. Two builds on one llm pod overlap in exec seconds, which is why the split is
by share and not seconds × rate. Pod rates in the window: 6000 Pro WK $1.91–2.24/h, 5090 ~$1.00/h,
4500 ~$0.73/h.

### What came out

| game | builds to a game | llm | art | one-shot | with fix/change |
|---|---|---|---|---|---|
| monster quests | 1 | $0.81 | $0.06 | **$0.87** | $1.31 |
| racing career | 1 | $1.64 | $0.06 | **$1.70** | $3.12 |
| island | 1 | $0.79 | $0.09 | **$0.87** | $0.87 |
| fish | 1 | $1.13 | $0.58 | **$1.71** | $1.83 |
| visual novel | 2 | $2.21 | $0.37 | **$2.59** | $2.59 |
| Zelda-like | 2 | $1.81 | $1.30 | **$3.10** | $3.97 |

Mean one-shot **$1.81 a game**, range $0.87–3.10; $2.28 with the fixes and change notes the
owner sent. Art is 23% of the one-shot cost.

Six of six delivered, and two of six took two builds: both of the stranger's games hit the
200-step cap on the first build and ran again. Those cap-outs are $1.33 and $1.48 that
delivered nothing — about half of what each of those games cost. One change build failed at
77 steps ($0.87), and the same game ended with fifteen failed art jobs.

Utilization over the window, every pod: llm 79% (6.1 h worked of 7.8 h billed, $16.93); image
41% ($2.08); video 58% ($1.30); ten pods of seventy-two did no work at all, $0.94 of it, the
owner's own test pods included. Art pods boot for one to three minutes of work, and that is
the whole of their waste — small next to a cap-out.

### Verdict

The stage 3 number exists: **$1–2 a game one-shot, $1.81 mean**, on pods that are already 79%
busy on the llm queue. The lever is not boot overhead and not the art queue. It is the
step-cap: a build that runs to 200 steps costs as much as a finished game and ships nothing,
and it hit two of six. What those two builds spent their steps on is the next thing to read.


---

## 2026-09-07 — The model writes PROGRAMS instead of tool calls (local 5090, qwen3.8_27b nvfp4 via ninfer, 131K window, reasoning medium)

### The question
CodeAct (arXiv 2402.01030) has the model emit a Python program that calls the tools, instead of one
JSON tool call per turn. Does that shape work on the 27B, what does it cost, and what breaks?

### Turn 0, one design (a noodle-shop management game), n=1 per cell
| arm | wall | completion | finish | what it did |
|---|---|---|---|---|
| seven JSON schemas (prod) | 47 s | 6,993 | tool_calls | 4 × read_file |
| one `python` tool | 86 s | 11,323 | tool_calls | one program: a loop over 4 reads |
| a fenced ```python block, no `tools` param, `stop: ["\n```\n"]` | 51 s | 7,722 | stop | one program, then a constraint solver |
| the same fenced block with no stop | 217 s | 38,761 | stop | 64 programs — it played both sides of the conversation until it ran out of things to invent |

`stop` works per request on ninfer's HTTP API, and is NOT matched during reasoning: the think ran
23,395 chars containing six code fences of its own and none of them ended the turn. The `python`
tool needs no stop string at all — `</tool_call>` is a trained stop — so that is the shape built.
Across 15 samples over four designs the two encodings produced the same programs; the fenced form
lost one turn in five to the model reverting to `<function=list_files>` XML when no `tools` param
was present, and both hit the same turn-0 runaway (60K completion, no program) on the fattest design.

### What the encoding costs, measured against the server's own tokenizer
| | tokens |
|---|---|
| 24,168 chars of game.js as raw text | 8,173 |
| the same as a JSON string | 8,424 (+3.1%) |
| the same as `write_file` arguments | 8,434 (+3.2%) |
| seven tool schemas, every turn | 1,831 |
| one `python` schema, every turn | 266 |

So the JSON tax is the SCHEMAS (1,565 tokens of window back, permanently), not the escaping. Both
sit in the cached prefix, so what they cost is window occupancy — which is what triggers compaction.

### Two moderated builds of the same design, every program approved by hand
| | v1 | v2 |
|---|---|---|
| turns | 29, never finished | 37, called `done` |
| tool calls | 35 in the first 8 turns (4.4/turn) | 99 (2.7/turn) |
| prompt at the end | 52K / 131K | 72K / 131K, no compaction ever needed |
| failed calls | 1 (a no-op edit, which ABORTED 10 later edits) | 0 |
| art asks | 22, two of them from one loop over a roster | 22 |

v1 ran with reads capped at 20K, tool failures raised, no syntax check and 1×1 placeholder art.
Every one of those was a defect of the harness, and v2 fixed all four: reads uncapped, failures
returned as `ERROR:` strings, a `check_syntax` function, and prod's own `write_placeholder`.

What the model did with it, unprompted: wrote a backtracking solver for a 14-night schedule with
constraints, wrote asserts for it, ran them, and caught `AssertionError: ('Ryo', 4)` — a wrong
count already written to disk. Indexed a 1,047-line file by regex and printed 31 lines rather than
re-reading it. Emitted 41,918 chars of JavaScript in one raw string that `node --check` accepted
first try, with zero backslashes needed. Found and fixed a `green onion`/`scallion` mismatch in its
own roster. Called `check_syntax` 9 times in v2, unprompted.

44 `edit_file` calls in v1, 43 landing first try on multi-line anchors reproduced from memory: the
escaping failure that JSON-serialized reads caused (2026-07-29) does not reappear when the file is
a Python string.

### What broke, and what each one changed
- **A raise abandons the rest of the program.** One no-op edit in a batch of 16 applied 5 and never
  attempted 10. The model recovered unassisted in one turn — but every tool now RETURNS its error.
- **It reached for the machine twice.** `subprocess.run(["node", "--check", …])` to check its own
  JavaScript, and `open("/tmp/sched.json", "w")` for scratch. Neither was hostile; both are why the
  program runs confined. Refusing the first cost four turns of hand-reading 1,000 lines, which is
  what earned `check_syntax`.
- **Silent truncation is worse in a variable than in the window.** The 20K read cap let the model
  count occurrences over 43% of a file and read the zeroes as missing edits; under JSON the note is
  unmissable because the content IS the message. The cap is gone.
- **A load-dead game the gate catches.** v2 shipped `main.js` importing `./lib/canvas.js` from
  inside `js/`, so three libraries 404'd and nothing ran — a black page with no uncaught exception.
  The error gate's script-404 rule names it exactly ("the page asked for js/lib/canvas.js … the
  path in the import that names it is wrong"); the session had no gate, so it stopped at `done`.

### Verdict
Built. The turn count is the lever — 99 calls in 37 turns is work that would have been ~99 turns —
and the second-order win is that a read need not enter the transcript at all. What is NOT measured
yet: any of this on Flash-Next through SGLang, which is what prod runs, and whether a full battery
holds the shape. Neither the fenced encoding nor a persistent namespace is built: the model reused
no bound name in either build, retyping a schedule it had computed two turns earlier, and a
persistent namespace would also defeat the static pre-check by leaving names bound across programs.


## 2026-09-08 — the game's sound is the lib's, said once in build.txt

`lib/audio.js` shipped 2026-08-22 and games kept writing WebAudio beside it. Scored over every
finished game built since (`imports lib/audio.js` against `new AudioContext` in the game's own
files, the lib's own source excluded):

| corpus | n | own WebAudio | imports the lib |
|---|---|---|---|
| since the lib shipped | 93 | 65 (70%) | 40 (43%) |
| since the lib line last changed (2026-09-04) | 13 | 9 (69%) | 6 (46%) |

Thirteen games did both. The rate does not move across the two windows, so it is the standing
behaviour and not a drift of the prompt.

The treatment is two changes: `audio.js` gains `sound`, `noise`, `seq` and `loop` — a game that
needs an engine rising with speed can no longer be told to use eight fixed effects — and one line
of `build.txt` says the game's sound IS the lib and no game makes its own AudioContext.

Four asks (arcade shooter, platformer, racing, rhythm), one build each:

| ask | run | imports the lib | own WebAudio | lib calls |
|---|---|---|---|---|
| shooter | `26ee08765ca5` | yes | 0 | 10 |
| platformer | `08da5d27dec3` | yes | 0 | 17 |
| racing | `d5a839254fae` | yes | 0 | 9 |
| rhythm | `7f7fef4772fa` | yes | 0 | 9 |

4/4, every one with no AudioContext of its own; p ≈ 0.3⁴ ≈ 0.008 against the 70% base rate. The
control arm is the corpus: a matched control build of the shooter ask on HEAD (`f71adaa6d516`)
hand-rolled a `sfx.js` with two AudioContexts, and four more control builds were dropped once the
93-game base rate made them redundant — an arm of four cannot say what the corpus already says.

Two builds of a control arm ran before it was dropped, and the second is the one worth keeping:
on the platformer ask it spent 79 minutes and never finished, one turn running the 131K window dry
(`finish=context_capacity`, 54,841 tokens generated, no tool call) after hand-simulating its own
code for three turns and transcribing a whole file into the transcript. The harness discarded the
dead turn and nudged for smaller writes, which is why it cost 6.7 minutes rather than the window.
Treatment built the same ask in 24m31s.

NOT measured: whether the sound is any GOOD. Both halves of the metric are use, not quality — a
game that hand-rolls beautiful audio scores zero here and a game that imports the lib to make noise
scores well. That judgement is a person playing it.

## 2026-09-08 — a turn that runs out of room (local 5090, qwen3.8_27b quasar via ninfer, 131K window)

**Before.** A turn whose reply hit the output cap without emitting a tool call was told so — an
empty assistant message and a user message saying nothing was saved, write the file in smaller
pieces — and asked again with the whole window as its cap.

**Why we ran it.** Cap-outs were visible in the logs, and an accounting of where a build's
generated tokens go put them at 17.3% of the program era locally, from 38 turns. That number came
out of chasing reads first: post-compaction re-reads are the bigger line, but five interventions
against them moved nothing, so the next target was the turns that produce no tool call at all —
zero-output waste, where the model spends minutes and the build gets nothing.

**How.** The turn re-runner (`labs/replay`) rebuilds the transcript a build really sent
at a chosen turn and asks the model what it would do from there, k times. The six positions where
one build (`8c824b3e2976`) ran the window dry were replayed 6 times each, and the worst turn in the
corpus (`90a89ba593ee` turn 14, 105,867 tokens, no tool call) 6 times.

**Result.** The failure does not live in the transcript. Those six positions fail 2 times in 36
when replayed, and turn 14 resamples to a median of 1,838 generated tokens with a maximum of 4,343
— a 57× outlier against its own prompt. The telling is what carries it: at one position, with the
cut-off exchange in context the model generated 14,160 tokens against 1,934 with it stripped (n=4
each), which is consistent with the recorded build failing five more times after its first nudge
while clean replays of those same positions failed twice in 36. Capping alone is not the answer
either — an 8K ceiling, below p90 of legitimate turns, truncated 5 of 8 samples into total failure.

**What changed, and why.** The reply is discarded whole and the same prompt sent again: it is a
failure of the inference, and nothing the model wrote is worth keeping or reporting. The first
attempt is capped at 30K, the retry gets the window. 30K sits above p99 of the turns that produced
a tool call on both models — 22,138 on the 27B, 11,051 on Flash-Next, whose longest turn ever
recorded is 17,212 — so prod never reaches it, and locally it truncates 4 turns in 945, each of
which retries with everything the window has left. What it buys is a bound: a runaway costs 30K
instead of 105K, and a build cannot spend six turns being told it failed. It does NOT fix the
reason a turn runs away, which remains unexplained — this is a bound on the damage, not a cure.

## 2026-09-08 — the compaction note cannot stop a re-read (local 5090, qwen3.8_27b quasar via ninfer)

**Before.** Compaction ends by re-grounding the model on the code map, and `file_state.py` adds a
block naming every file the build has read and whether it is still byte for byte what was read.
Whether that block worked had never been measured.

**Why we ran it.** 90 of 95 post-compaction reads in a prod build were of files already read. The
build arm staged for it (block on/off, two asks, four builds) was abandoned when its widest ask —
a 3D open-world RPG, 62 steps, 82 minutes — finished with ZERO compactions at 131K, which would
have made both arms byte-identical.

**Result.** Replayed instead, over six real compaction points at 18 samples an arm: 8/18 turns
re-read with the block, 9/17 without. Four other attacks on the same turn moved it no further — a
rule ("must NOT be read again") instead of a fact, the code map deleted entirely, the map alone
with all prose stripped, and the file bodies pasted into the note. Nor did delivering those same
bodies as a completed `read_file` round, which produced the most reads of any arm (19 against a
baseline 16). Handing the model the exact bytes does not stop it fetching them.

**What changed.** Nothing yet. The block stays until it is deleted deliberately; what this rules
out is fixing the re-read by writing something better in the note. The reads themselves are not
cheap — 31% of generated tokens locally and 52% on prod, where 77% of reads are of files already
read — and on prod they are concentrated in gate fixes and changes, which start from an empty
transcript and must read the game back. That is a cold-start problem, not a compaction one, and
none of these arms touched it. Every measurement here is on the 27B; prod's model reads more and
repeats more, and has not been probed.

## 2026-09-10 — compose_world could never have worked from inside a program

**What broke.** A build asked for a 3D world unprompted for the first time (DeepSeek Flash, local
box). The world was built in full — 900 m of terrain, six regions, ten instance kinds, meshes — and
staged into the game at 34 MB. The game referenced it NOWHERE. The race rendered a bare ground plane
and a skybox.

**Why.** `pyexec.runner.TIMEOUT_SECONDS` is 120 s and the deadline covers time spent inside tools.
`compose_world` blocked through the `construct` stage — about fifteen minutes — to return the world's
size and regions. So the program that called it was killed every time, by construction: the world
landed, the program died, and the turn that would have written the loader went with it. The model
read `world/world.json` two turns later, then 27 compactions buried it and the rewrite pass at steps
73–100 re-authored every file without it.

Billing was correct throughout, which is what makes it a real loss: all 334 jobs carried the game's
`game_id`, so the run paid for 1,246 s of image and 159 s of mesh work — about 23 minutes of GPU —
for a world it was structurally prevented from using.

**What changed.** The tool now answers immediately with the path, like `generate_media`, and every
stage runs behind it in four publishing legs. Nothing was lost by answering early: `world.sizeM` and
`world.regions` are read off the loaded world in the browser, which is where a game was always going
to get them. A placeholder flat world — one region, zero heights, valid in every field
`runtime/vendor/world.js` indexes — is written before the answer, so `loadWorld` succeeds the moment
the model writes it and the built ground replaces it by rename.

**Not measured.** Whether a build now wires the world. That is the next arm, and it is the whole
question: this run is the only evidence either way, and in it the model never got the chance.

## 2026-09-10 — the compaction that never compacted (local box, DeepSeek Flash, 131K window)

**What we found.** A 214-turn build compacted 30 times. Every one of them recorded `trimmed: 0`:
the tier that replaces file bodies with stubs never ran, and all 30 went straight to the last
resort, dropping 88 whole rounds — the rounds where the model watched itself write the files it
was still editing.

**Why.** `_written_in` matched `write_file(path=<literal>, content=<literal>)` and nothing else.
Every one of the build's 24 writes was `write_file('js/sim.js', head + partC + partD)` — path
positional, body behind a name, because a 40 KB file does not fit in one reply and is written in
labelled sections. Two independent misses, so nothing was ever stubbable. `build.txt` documents the
signature positionally, so the model was following our own documentation out of view of our own
compactor.

**What changed.** NOTHING YET — compaction is being worked on as its own pass, so the diagnosis is
what this entry is for. What a fix has to cover: positional arguments as well as keyword ones, and a
body reached through the shapes a model writes one in — the literal in the call, a name bound once
to a literal, those added together — while a name assigned twice or built in a loop stays whole,
which is the case the doctrine was written for. Measured against the transcript that exposed it,
that much would make 14 programs stubbable and recover 138 KB, 21% of all program bytes, and they
are the big authoring turns.

**Why it stayed hidden.** Every compaction test wrote its fixture in the keyword form. The tests
encoded our idiom rather than the model's, so they passed while the mechanism did nothing — so the
fix needs fixtures taken from a real transcript, not from how we would have written the call.

**Not measured.** What a build looks like with the tier actually working. The next run raised the
window from 131,072 to 400,000 instead and never compacted at all, so the tier has still never run.

## 2026-09-11 — every turn re-prefilled the whole window (local 5090, qwen3.8_27b quasar via ninfer)

**What we found.** Timing a compaction arm, a branch of 94 turns spent 21 minutes, half of it in
prefill, on prompts that only ever grew. The server reported 14% of prompt tokens cached, and on
nearly every request of a build the cached count was the same number (16,377): the request and
nothing after it. Every ninfer log back to August shows the same shape — 0 to 6K cached.

**Why.** The server's cached sequence is the turn as the model generated it, thinking included.
The build stored each reply without its `reasoning_content`, so the next prompt diverged from that
sequence at the first assistant turn. Qwen3.8's recurrent layers cannot resume from an arbitrary
prefix, only from a checkpoint the engine saved, and without `--preserve-thinking` ninfer saves
its one checkpoint at the first assistant turn after the latest user message — right after the
request.

**Measured.** One real turn of a 49K-token build prompt, replayed on an idle server: with the
thinking sent back and `preserve_thinking` on, the next request reused 49,361 of 49,378 tokens
(`append_frontier`) and returned its first token in 90 ms; the same pair without it reused 7,134
and took 6.7 s. Estimated over the branch above, prefill falls from 10.6 minutes to ~0.7.
`preserve_thinking` alone, with the thinking still stripped, reused nothing.

**What changed.** Each turn's thinking rides the transcript until the next compaction, which
drops all of it: a cut already breaks the prefix, and kept past it the thinking — ~91K tokens
against 220K of everything else on one 112-turn change build, 4–10% of what a design build added
between cuts — would fill the window. ninfer runs with `--preserve-thinking`. This is also the
model's own default: Qwen3.8's card enables preserved thinking for all workloads, and the vendors
that train with it (OpenAI, DeepSeek, MiniMax) require or recommend sending it back.

**Prod never hit the wall.** Its builds dropped the thinking too, but over 951 prod turns turn time
tracked only the tokens added since the last turn (~7.7K tok/s), not prompt size: a 70K+ prompt
turned as fast as a 20K one. SGLang resumes from the end of every prompt, so it never needed the
carry-back for the cache; what it gains is the model's default.

**Not measured.** Whether seeing its own earlier thinking changes what the model does over a long
build — it now sees why it did something instead of only that it did. The published gains are
modest for coding (2–5 points) and none of them is a 100–200-turn loop under compaction.

## 2026-09-11 — a cut kept two thirds of the window, not one (local 5090, qwen3.8_27b quasar)

**What we found.** A design build compacted seven times in 95 turns, the cuts 5 to 17 turns apart,
where the same design had cut three and seven times in whole builds. Each cut took the prompt from
~112K to ~83K and the build refilled it in a handful of turns.

**Why.** The keep target is a third of the window, but `compact` measures characters, and the
target was converted at a fixed 4 chars a token. This transcript ran at 2.9, so "a third" was
~60K tokens, not ~43K. Two things kept the cuts shallower still: when superseded bodies and
read-only rounds alone recover 30%, a cut stops there, and the trim tier never ran (below), so
past that the only deeper cut is dropping whole rounds. The thinking now kept between cuts was not
it: stripping it saved 2–11% at each cut and changed no cut's outcome.

**What changed.** The ratio is measured: every turn the server counts sets chars-per-token from
what was sent, and the target is converted at that. 3 until the first count.

**Not measured.** The build that follows.

## 2026-09-11 — the trim tier reads the writes a model actually makes

**What changed.** The stub matcher takes `write_file`'s path and body by position or by name, and
a body that is a literal, a name bound once to one, or those added together; the literal is cut
where it sits, in the call or in the one assignment that names it. A name assigned twice, a body
read back and replaced, a formatted or joined body stays whole. The fixtures are the shapes of real
transcripts: turn 9 and turn 29 of 8988a28a746e and the read-modify-write of f7f816519d16.

**Measured, by matcher alone.** 8988a28a746e (30 compactions, every one `trimmed: 0`): 14 of 192
programs stubbable, 138 KB of 652 KB of program bytes (21%) — what the entry above predicted.
f7f816519d16 (nine compactions by turn 133, every one `trimmed: 0`): 18 of 149 programs, 312 KB of
360 KB (87%). Every stubbed program still parses.

**Not measured.** A build with the tier working.

## 2026-09-12 — a nudge at ten no-write turns (local 5090, qwen3.8_27b quasar, branched from prod Flash-Next positions)

**The question.** Four prod builds each had a stretch where the model stopped writing and read
instead — 20 turns on 345fcb132db8, 12 on e153dddc53b6, all 68 on the fix that capped out and
shipped nothing. Does a user message at ten consecutive no-write turns ("if you are chasing a
specific bug say which and keep going; otherwise call done") end the circling, or end the build
early?

**The arm.** `replay.branch`: six fire points (74a9f0 at 108, 145, 166; d6741e at 69; the fix
builds a736b5 and dab5b2 at 9), 12 turns forward, k=3, control against nudge, on the local 27B.
Scored on turn count first, then on two bugs that a grep can see: at d6741e@69 `main.js:243` uses
`PLAYER_W` and never imports it (a ReferenceError on retry from a checkpoint); at a736b5@9
`gen.js:166` calls `surfaceAt(solidGround.concat(floats), cx, def)` against
`surfaceAt(ground, floats, x)`.

**What came out.** On turns the nudge wins everywhere: 8/9 branches at the 74a9f0 positions
called `done` within 2–5 turns against control's 7–12, and the real build had spent its 70 turns
after 108 on seven cosmetic edits before finishing at 198 of 200. On the bugs it loses. d6741e:
control fixed the import 3/3 in 12 turns, with 2–5 write turns each; nudge fixed it 1/4 and
called `done` at turns 72–75 with the crash in — the real build fixed it at 72, three turns after
the fire. a736b5: control 0/3, nudge 2/3, and the two floor arms whose nudge never fired (so:
control reruns) 3/3 and 2/3 — a 0/3 against 3/3 between identical arms is the noise floor at
k=3. dab5b2, the cap-out the nudge was written for: fired 3/3, changed nothing 3/3. The nudge arm
also spent `done_nudged`, so its first `done` skipped the art audit; that alone is ~6 of control's
extra turns.

**Offline, every recorded build (43 with the python tool, 6 prod).** The counter fires 44 times
in 22 builds. A write follows within 5 turns in 45% of fires, later in 41%, never in 14% — and
the six nevers are four fix builds that never edited (two of them called `done` on their own)
and two lab builds killed by hand. On prod's five mid-build fires, 4 of 5 wrote within 5 turns.
SWE-agent's doom-loop rule (the same call fingerprint three times in twenty) fires 0 times in
43 builds when the fingerprint is the whole program, and in 42 of 43 at turns 2–19 when it is
tool+path. The same tool OUTPUT three times in twenty fires 8 times in 7 builds and 7 of them
wrote within 5 turns. Nothing the model does before a real edit looks different from what it
does before never editing; dab5b2 printed a different slice of the same file on each of its 66
turns.

**Verdict.** Not shipped. A no-write streak is not BROKEN: it is the shape of a fix pass as often
as it is the shape of a stall, and a message that pushes toward `done` ends the pass. A ReferenceError
six screens deep is past what the error gate presses, so shipping early and gating does not
recover it. If the counter goes anywhere it goes in as telemetry: log the fire and whether a
write follows, on every prod build, and revisit with a base rate.

**What the corpus pointed at instead.** Fix builds pay for their opening. a736b5's first edit came
at turn 30 with 65K in the prompt: seven turns printed whole files (13–20K characters each), four
were refused reaches for `open`, `subprocess`, `exec` and `__name__`, ten ported the stage
generator to Python to prove a bug it had printed side by side at turn 12. Across the 22 fix
builds, 12 reached for a runtime at least once (40 refusals over 53 builds, `open` and `os`
first). And the play gate's page has no `pageerror` handler: the TypeError that froze stage
select was thrown on its page and never entered the fix note. The error gate wires that
handler; the play gate opens a raw page.

## 2026-09-12 — what a fix build opens on (local 5090, qwen3.8_27b quasar, branched from prod fix openings)

**The question.** a736b5 spent 30 turns and 65K of prompt finding a one-line bug. Two candidates
for what it was missing, each a change to the fix build's first user message: the exception the
play gate's page threw, with its address; and the code map of the game. Both, either, neither.

**The arm.** `replay.branch` from position 0 of two prod fix builds. a736b5 (Enter on stage select
did nothing; the page had thrown `TypeError: floats is not iterable` at gen.js:166): 15 turns,
k=4, four arms — the note as recorded, the note plus the map, the note plus one line "What the
page threw: <message> at <file>:<line>", both — and a 25-turn rerun of the first two. Ground
truth is a grep for `surfaceAt(solidGround, floats, cx)`. 9febdb (the camera should scroll
earlier; nothing threw): 15 turns, k=4, note against note plus map; ground truth is `updateCamera`
or the camera record changed in main.js and nothing else touched.

**What came out, a736b5.**

| arm | fixed | first edit at turn | prompt at first edit |
|---|---|---|---|
| note, 15 turns | 2/4 | 13, 15 | 38K, 41K |
| note, 25 turns | 0/4 | — | — |
| note + map, 15 turns | 0/4 | one wrong edit at 3, then `done` | 7K |
| note + map, 25 turns | 1/4 | 19 | 40K |
| note + thrown line, 15 turns | 4/4 | 4, 5, 5, 6 | 7–21K |
| both, 15 turns | 4/4 | 4, 5, 5, 7 | 9–15K |

All eight thrown-line branches changed that one line of gen.js and nothing else, and called
`done` by turn 6–9. The wrong edit under the map added `justPressed('Enter')` beside the alias
that already bound it, and called `done` with the throw still in.

**What came out, 9febdb.** Note alone: a camera fix in 4/4, first edit at turns 4, 5, 4, 5. Note
plus map: 4/4, at turns 7, 7, 4, 9. Two branches in each arm took the deadzone to zero on the
camera record instead of rewriting `updateCamera`, which is the same fix.

**Verdict.** The play gate's page now hears `pageerror` and the fix note carries what it threw
and where (`play_gate.py`, `play_fix_note.txt`): 8/8 against 2/8, the edit at turn 4–7 against
13–19. The map is not a lever on either fix — a throw makes it redundant, a change note the 27B
already reads straight to, and it bought the one confident wrong fix in the set — so it did not
ship; the diff is kept in the lab. Its motivating numbers (first edit at 47–110K of prompt) are
from Flash-Next and DeepSeek fixes, which this harness cannot run; if it comes back it comes back
as a prod-side arm on prompt tokens at first edit.

**Not measured.** A prod fix build with the thrown line in its note. Whether the play gate's
model, shown a page that threw, still reports the same fact as "did nothing" — the folded fact
is preferred regardless, so the note is right either way.

====================================================================================================

## 2026-09-13 — llm slots: what the prefix cache and decode do at 2 and 4 builds per pod

**Question.** Does a Pro 6000 pod keep four builds' prefix caches warm, and what does the
fourth slot cost per stream?

**Setup.** Synthetic ramp on a prod-image pod (Pennyroyal, Flash-Next): 1/2/4 streams grown to
~120K tokens over 100 turns, then 2,048 tokens of decode at ~104K. One pod, 25 minutes.

**Result.**

| streams | cache misses | decode per stream | aggregate | compute per game | cost to user |
|---|---|---|---|---|---|
| 1 | 0 | 128 | 128 | 100% | 100% |
| 2 | 0 | 114 | 229 | 56% | 112% |
| 4 | 0 | 79–88 | ~330 | 39% | 153% |

**Verdict.** The cache is not the limit: no evictions at four (mamba pool 16 of 24 slots used;
raising it to 96 changes nothing and costs a third of KV). The fourth slot costs the customer
+53% of exec-seconds against +12% at two.

**Ruling.** `llm.slots` stays 2: ~45% lower compute per game for a build ~12% longer. Prod pods get 
`--enable-cache-report` so recorded builds carry cached tokens per turn.

**Details.** `labs/conc-replay/README.md`.

====================================================================================================

## 2026-09-14 — a design team and a spec on disk (local 5090, qwen3.8_27b quasar via ninfer)

**Question.** Does a sequential design team (gameplay → visual → engineering → integrator) with
the merged spec handed to the builder as files, under a build prompt written for that, make
better games than the one-shot design in the prompt?

**Setup.** Five asks, four designer calls per ask at xhigh, each later one fed the earlier docs. 
the existing integrator prompt merges them. The merged spec was 
split into one file per section under `game/design/` and the build prompt reorganised into
how-you-act / environment / rules with tool docs moved to `docs/*.md`; the user message is two
lines naming the ask and the folder. 200-step cap, no other change. Nick played the six results
and the earlier one-shot builds of the same prompts.

**Result.**

| ask | steps | outcome | Nick |
|---|---:|---|---|
| island | 174 | done | great bones, barren map |
| station | 145 | done | best station yet, objective unclear |
| collector | 200 | capped, playable | cleanest collector ever; selling broken in both runs |
| platformer | 200 | capped, playable | legitimately good |
| forest | 200 | broken | hand-authored map + validator ran the output cap dry 14× |

"Blown away … all really good"; the one-shot builds of the same prompts: collector "awful",
island "awful", platformer "good". Every run read the whole spec before writing (six prompt
and spec shapes tried, none changed that; the model's own account: cross-reference anxiety,
reading feels like progress). The two that finished tested as they wrote; the capped three wrote
everything by turn ~25 then spent 175 turns on one play() probe per turn. Zero Node/npm
attempts under the new prompt against 5/5 Node harnesses in the engineering docs. The done nudge
cost 6 turns median across 34 old builds and never capped one; it capped one here.

**Verdict.** The team + spec-on-disk + layered prompt is the new baseline: 2 of 5 done and 4 of 5
playable where one-shot produced games Nick would not ship. What it does not fix: content the
spec leaves to the builder (levels, maps, a first minute), and a builder that verifies one probe
per turn until the cap.

**Ruling.** Integrate: design stage becomes four calls, the spec lands as files, the build prompt
is rewritten in the layered shape, the done nudge goes for spec builds. Open before it ships:
tests-as-files + one in-page runner as a prompt line; level/content ownership in the gameplay
prompt; the integrator's Node-harness carry-over and its 20K-token stop on Flash-Next
(continuation call).

**Details.** `labs/spec-skeleton/README.md` (§"Integrator on the p2 triples" onward), run ids in
`labs/spec-skeleton/builds.tsv`.

====================================================================================================

## 2026-09-14 — DFlash2 drafter on the local 27B (5090, qwen3.8_27b quasar via ninfer)

**Question.** Does the DFlash2 K=7 drafter beat MTP3 on the calls we actually make, with the
QUASAR checkpoint it was not trained against?

**Setup.** The station design chain (`--new`, four design calls at xhigh, one request at a
time) on the same QUASAR weights: MTP3 from the old artifact vs DFlash2 from the v2 artifact that
appends the z-lab companion to byte-identical base payloads. Engine: Mirko's ninfer fork at
`8debca3`. Control run 17c92bff6bda, arm run a149981e8c49.

**Result.**

| call | prompt | decode MTP3 → DFlash2 (tok/s) | × | accepted of 7 |
|---|---|---|---|---|
| gameplay | 413 | 159 → 203 | 1.27 | 34% |
| visual | 11.5K | 147 → 200 | 1.36 | 33% |
| engineering | 11.6K | 139 → 202 | 1.45 | 34% |
| integrator | 47.0K | 136 → 221 | 1.62 | 39% |

Chain wall 29m27s → 16m15s; token-weighted decode 145 → 205 (1.41×), the rest is a shorter
draft. GPU footprint unchanged at 131K int8 KV with vision.

**Verdict.** DFlash2 holds ~200 tok/s regardless of sequence length where MTP3 sags, so the gain
grows with exactly the long calls a design and a build are made of. Acceptance matches upstream's
base-model figure: QUASAR takes the base-trained drafter without loss.

**Ruling.** Adopted for local: the v2 artifact under the same filename and model id,
`ninfer-quasar` at `8debca3`, `local_gpu.py` serves `--spec dflash2 --draft-tokens 7`. Prod is
Flash-Next on SGLang and has no companion; untouched.

**Details.** `labs/ninfer-dflash2/README.md`.

====================================================================================================

## 2026-09-15 — the design in the code map, one compaction note, a check_off tool (local 5090, qwen3.8_27b quasar via ninfer)

**Question.** After a compaction the builder re-read the whole design and lost its section 9 tests
(control `f91ade83c0d0`: 41 whole reads, 1 of 121 tests named after the design, 478 turns). Does
putting the design's headings with line ranges in the compaction note fix that, and does a
`check_off(rows)` tool end the box-by-box Definition-of-Done crawl that capped builds after a green
suite?

**Setup.** Whole prod path on this box: design chain → build at cap 200 → error and play gates.
`code_map` maps `.md` files by heading; `compact` keeps one note, not one per compaction (it had
been stacking them). Five fresh asks overnight, then one more ask with `check_off` (a list or one
row, ticks `- [ ]` in the game's design/design.md, answers what is unchecked; no done nudge) and a
cut-off reply compacting before its re-send. Nick played all six.

**Result.**

| run | ask | design reads whole / ranged | tests design-named | end | gates | Nick |
|---|---|---|---|---|---|---|
| 9c62c1d1e58c | tower defense | 0 / 33 | 58/58 | done 120 | clean | good first pass |
| e9d964fc34c7 | fishing lake | 1 / 23 | 65/66 | done 100 | clean | good first pass |
| 112ec65202b2 | rhythm platformer | 2 / 21 | 1/3 | cap 200 | not run | really bad: black screen, loud |
| 202198c78de5 | space salvage | 18 / 31 | 35/36 | cap 200 | not run | a delight |
| 9e6f192bd791 | pipe puzzle | 10 / 23 | 0 | died 96, 4 cut-offs | not run | — |
| 194354ba2b2e | museum heist (+check_off) | — | — | done 129, 2 fixes | clean | good first pass |

The two cap-outs spent 40–55 turns after a green suite reading one DoD row per turn. With
`check_off`, the heist build still probed ~25 rows with `play`, then ticked all 65 in one
`write_file` of the design and called `check_off` on rows already ticked: green to done in 19
turns. The design was otherwise untouched. The cut-off re-send at turn 20 landed with the full
window (64K tokens out); the compact-first branch did not fire.

**Verdict.** With headings in the map and one note, every build read the design by range and
every build that wrote tests named them after the design; the control did neither. Five asks
against one control, so this is a direction, not a measured rate. `check_off` did not become the bookkeeping path, and nothing says a done nudge or a
read-only design would earn its upkeep while a build ticks only the DoD and ends.

**Ruling.** Merge all of it: markdown headings in the map, one compaction note, check_off, compact
before a cut-off re-send, one retry for an empty design step, and a 200 ms play-gate tap (a 0 ms
press never shows to a game polling key state). Not added: a done nudge over unchecked rows, a
read-only design; revisit only if a build ticks rows it never built and ships broken, or edits
the design beyond the DoD.

**Details.** `labs/design-map/README.md`, `labs/dod-checkoff/README.md`.

====================================================================================================

## 2026-10-13 — fresh-context refine passes on a design (local 5090, Flash-Next IQ3 "basalt" via coding-agent)

**Question.** The 2026-09-14 verdict left content the spec does not settle to the builder. If a
model in a fresh context is asked what it needs to build the game, and to decide it, does the
design it leaves behind build a better game? Arms differ only in N, the refine passes after each
stage.

**Setup.** No GameSummoner path: Nick's `coding-agent` with `--shell` (bash in bubblewrap, no
network), each pass a fresh process with one prompt file. One seed design from the one-input
roguelike ask (CADENCE: tap count is the verb, tap rate is the intensity), then "Add a visual
design", "Add sound design", "Build the game", with "What do you need to build the game in /
what is unclear about visuals, sound in DESIGN.md? Make decisions and update the markdown" as the
refine pass. A driver sends "continue" when a basalt judge reads a turn's end as an announcement
(the harness ends a turn on any text-only reply). One commit per pass. Nick played both builds.

**Result.**

| arm | design lines | build | calls | own tests | hand fixes | Nick |
|---|---:|---|---:|---|---:|---|
| r0, N=0 | 338 → 800 | Python, PIL + tkinter | 199, cap 200 | claimed green; 3 suites fail on rerun | 1 crash (idle Brace) + 1 launch | stuck by room 3 |
| r1, N=1 | 338 → 1507 | browser, plain JS | 535, cap 2000 | 59/59, reproduces | 1 crash (first cue) | complete and playable; pivot facing reads as random |

The design refine pass (3 min, 11 calls) found contradictions any literal build would ship: every
multi-press burst was Frantic by construction (commit window 260 ms vs tiers at 260/700 ms), idle
Brace farmed Guard forever, Guard and "step into a monster" were undefined. It also picked the
stack; r0's came from a PIL figure script the visual pass wrote. The visual and sound refines
turned into measurement: pixel coordinates, contrast ratios, dBFS to a tenth, a cue synthesiser
whose WAV writer dropped the samples (44-byte files, uncaught by the sound refine). Pivot
feedback, Nick's main complaint, got one table row. Every design pass also wrote code the prompt
did not ask for. Both builds crashed only in the one layer they could not run headless (the Tk
window, the WebAudio shell).

**Verdict.** Inconclusive on the question: one game per arm, and the round cap, prompt wording
and driver changed between arms, so r1 being playable cannot be pinned on refinement. Refine passes
improve what the model can measure, not what a player feels.

**Ruling.** Lab closed, nothing merged. Worth testing on the prod harness: one fresh-context
"what do you need to build this" pass on the design, counted by contradictions found and runs
that play through.

**Details.** `labs/refine-passes/README.md`, one commit per pass in the labs repo.
