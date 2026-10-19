# GameSummoner

GameSummoner is a platform for using AI to make games. We take the user's input, turn it into a
design, and then feed that design into a model, giving it access to all of the tools it needs to
build the game. The main selling points are quality and ease of use. The goal is they come to our
website, type in a prompt that describes at a high level what game they want, click go, and soon it
simply exists.

The north star is any game at high quality. No matter what you type in, a game should come out, even
if it's gibberish. Of the two bars, GameSummoner has met the "any game" portion, and are actively
researching and trying to improve the quality that GameSummoner provides.

The pipeline runs on rented cards with open-weight well licensed models. The current cards and
models are RTX PRO 6000 with Qwen3.8 Flash-Next in production, and 5090 with Qwen3.8 Flash-Next
(IQ3_S, served by Basalt) for local testing.

This file exists to describe how we work on GameSummoner, and how to make changes to it.

Historically, this tool was originally to be named Maestro, but domain constraints required us to
switch.

| Where to look | For |
|---|---|
| `docs/vision.md` | the destination and the non-goals |
| `docs/roadmap.md` | where we stand against it |
| `docs/build_path.md` | the build path module by module |
| `docs/architecture.md` | processes, state, trust boundaries |
| `docs/local_dev.md` | settings, model servers, how to run anything |
| `docs/models.md` | every model weight in prod, its source, size and license obligations |
| `docs/experiments.md` | what each change to the loop actually measured |
| `docs/ideas.md` | changes parked until they pay for their complexity |
| `labs/*` | previously run experiment details, must be pulled separately |

---

## How it works

GameSummoner's whole implementation is 2 high level steps:

1. Build a design from the prompt given. The expectation is that the user will type, effectively,
garbage. Given that, the first step is to take their input, pull out any requirements from what they
typed, and build a design that encompasses them, plus everything else needed for a functioning game.

2. Build the game. This is a very broad statement, but in the high level flow, that's all there is
to it. Take the design, provide it to the large language model, give the LLM the ability to call
tools such as generate image, generate 3d model, type check your work, etc. and let it go until it
tells us that it's done.

## How to work on this repo

### UIUX feature development

When working on features such as new UX provide the engineer your suggestions, and work with them to
plan what the UX will look like, why it is better than what currently exists, if anything, and how
it will improve the user experience. If the user experience is not improved by a UX feature, it does
not get developed.

### AI feature development

When working on features such as system prompts or build tools, the first step is an experiment
(detailed further in this document.) Once a new feature has been proven, the second step is
designing the implementation for the feature. The design philosophies of GameSummoner can be found
in `docs/design-philosophy.md`, read it when you get to this stage.

#### Prompts must be climbable

One of the ways we get better is by improving the prompts that go into our LLM. This requires us to
be able to quickly and easily update the prompts. As such, all prompts must be in their own text
files so they can be easily edited to climb.

### Experimentation

Experimentation should be done in several steps, with the help of an engineer. The first step is to
verify the request can make improvements in our pipeline. If there isn't any evidence, push back and
ask "Why?" Once it has been determined to be potentially beneficial, create a unique folder in labs
for the run. This folder will be where all work related to the experiment will be documented in
detail and actually run from. Copy the template markdown file from `labs/template.md`. The next step
is documenting what is expected to change, what's being measured and why the changes are expected.

From there, run the experiment a single time to prove it functions. If a control is needed, use
previous runs to compare the single time experiment to. If the experiment has failed, determine if
the issue was implementation (which gets a fix and rerun) or an idea failure. Append these results
to the append only section in the documentation.

If the experiment has been proven to work, the next step is to validate on a small battery of tests.
The battery should be representative, and if the results confirm the hypothesis and earlier testing,
and a control is warranted, a control may be run. Once the whole experiment is finished, wrap up the
experiment document, and fill in a new section in `docs/experiments.md`.

### Updating documents

In order to update any of the documents with exception of experiments.md, explicit signoff from one
of the engineers is required. The document change must have evidence that the document is wrong, or
reasoning for why it should be changed, which should be recorded in the git commit message.

### Backwards compatibility

GameSummoner does not support backwards compatibility. Seeing as there is nothing provided outside
of our control, there is no reason to provide backwards compatibility. If a database table needs to
be changed, it should be validated locally, then a script run to backup production, and then update
prod's table. The script should be committed to git, with a commit after it, deleting it from the
repo as a way of maintaining transparency and history.

### Testing

Before presenting code, ensure all tests pass. For tests, test names should be explicit about what
is being tested, and fewer, richer, data based tests should be prioritized. Tests that exist "for
coverage" are not allowed, and should be either turned into a proper functionality test, or deleted.

### Code review

Before presenting code, review the diff. Ensure the changes are narrow, built in the right place,
and alert the engineers if something is found that needs to be fixed that is outside of the current
scope. New code should be tested appropriately, preferring well defined unit tests, and docs should
be reviewed and edited to ensure they haven't gone stale due to the changes.

### Comments

In general, no comments should be left in the code. A single line docstring at the top of a file
that explains in short detail what a file does, or a small multiline for files that are complicated
is acceptable, but no more. Any comments in line will be scrutinized and require engineer signoff.

### Error handling
Validate things at boundaries. Internal to our systems we're in complete control, no need to
validate things that cannot be broken.

### Commit messages

Commit messages should have a title that is a short summary of the commit, and if a reader could not
tell what the commit does, a short explanation.
