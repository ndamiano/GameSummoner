# Purpose

This file documents the design philosophy that has been created for GameSummoner, as well as
reasoning behind why that decision has been made. It is a best effort document, if you have a better
solution, please suggest it.

If you have a question about why something is the way it is, there is likely an experiment that
explains it, or you can ask the engineer.

## Code Philosophy

### Code is extremely cheap

Now that AI agents can write an extremely large amount of code in a very short amount of time, and
that code can be relatively well trusted, it is worth examining whether we should have third party
dependencies. A new dependency adds a failure vector to our software, as well as a new avenue for
dependency injection, and just overall issues. Given this, dependencies are to be avoided, unless
they provide absolutely immense amount of effort savings.

## Agentic Philosophy

### The model isn't dumb

In general, the model is quite capable. There are many examples of what the model can do given the
right set of context and tools. This can clearly be shown, by looking at things such as the harness
differences on evals. Given that, our first suspect on errors should be context, and our priority in
changes is to try prompt, then harness, and finally, if none of this can be solved, investigate
different models.

### Model tool calling

Originally, tool calling was simple JSON tool calls. Measurements were taken, and the evidence
showed that JSON tool calls were less efficient than python tool calls. Benefits included model
self-checking, giving it the ability to run assertions, less context pollution with things like
reads not needing to enter the transcript, and a much higher "tools per turn".

While we trust the model to not be dumb, it's not foolproof, and as such we need to make decisions
about what access to give them. Our stance is that the class of model is not quite strong enough to
get full sh access, and we sandbox the python it runs. An allow list of commands is safer currently.
Inside that sandbox the model gets the file habits it already has: real files in the game folder
through open(), os and pathlib, with the kernel (Landlock) refusing every path outside it. Our own
tools are kept for what the model cannot do itself, like rendering, art and checks.

### Local is second class

In order to ensure our code does not become a rat's nest, we need to determine where the boundaries
are between enabling efficient development and maintaining the code base. Given that our code base
does not care about how a creation creates, or how the worker actually resolves the answer, just
that it does, local has been demoted to second class. Any code targeted at local is by definition
wrong, and we should seek to minimize it as much as possible.

### Parallelization is a good UX choice

When possible, we should allow parallelization. Enqueueing art, requesting models, and even some
extra LLM calls can be done in parallel while the main build is ongoing. For the user, it's an
obvious choice: they pay the same amount if their stuff gets run in seires or in parallel, so we
should always push for parallelization when possible.

### Retries are cheap

Errors happen. Sometimes the model will run 3,000 tokens and just give us nothing. Instead of trying
to salvage the call, we retry it. When things are mangled, the next thing after it predicts based on
mangled inputs. Better to just retry it.

## Game Creation Philosophy

### We cannot programmatically measure quality effectively

This is a very loose statement, but it drives several major decisions. Currently, our attempts at
measuring quality across many different attempts have been met with low success. Given that, the
decision has been made to forgo quality measurement as part of the pipeline, and instead prioritize
done over better. This is visible through things like our gates being purely functional. The play
gate attempts a few inputs, without judging quality, only finding functional defects.
