# honeyguide

A terminal coding agent for small local models, built the other way round.

A strong model (Opus 5, headless, offline) charts the repository once and writes
a persistent index. A small local model, a ~3B-active Qwen MoE on a machine in
the next room, consumes that index at runtime and makes small, targeted edits
inside a harness that will not let it lie. It never explores the repository, it
never emits a free-form tool call, and it never writes an edit that has not
passed `cargo check`.

Named for the honeyguide bird, which cannot open the hive itself and so leads a
stronger partner to it. Here the roles are inverted: the strong partner charts
the territory, and the small bird works it.

## Status: design, plus a spike that runs. There is no working agent.

The Rust crates are the data model and nothing else; `cargo check` passes and
that is the entire claim. What does run is `m0/spike.py`, a throwaway harness
that implements the parts of the design the thesis depends on and has now been
run end to end against a real 14.7k-line workspace, three trials per model.

Its verdict on itself: the quality gate is **failed** and the turn-time gate is
passed by a factor of fourteen. The suite's value was never the score, though. It
found seven defects in the harness, five of which were making the model look
worse than it is, and one of which meant the gate was quietly refusing a whole
class of correct edits. Those are below.

| | |
|---|---|
| [RFC-0001](docs/rfcs/RFC-0001-honeyguide.md) | the design |
| [RFC-0002](docs/rfcs/RFC-0002-strong-model-boundary.md) | how the index pipeline drives Claude Code, which has more sharp edges than it looks like it does |
| [measurements](docs/measurements/) | what the machine actually does, including one set that was wrong |
| [prompts](prompts/) | the system prompt and action schema, and what constrained decoding does and does not enforce |
| `crates/` | the RFC's data model. `cargo check` passes and that is the entire claim |

## Why build it this way

Every general-purpose agent assumes frontier-grade native function calling, and
every one degrades badly on a 3B-active MoE: empty tool calls, markup leaking
into the content field past about five tools, and Ollama's 4096-token default
silently truncating the system prompt so the whole thing presents as "the model
can't use tools".

The first request this project ever sent to its target model makes the case
better than any survey. Asked to rename a function in `src/lib.rs`, with no file
contents supplied, it returned a schema-perfect edit action whose `search` field
was an entirely invented `src/lib.rs`: `cargo new` boilerplate, a function that
did not exist, two duplicate `mod tests` blocks, and a run-on string that ate
the token budget without terminating.

The structure was flawless. The content was fiction.

Constraint buys you well-formed nonsense. Only matching against what is actually
on disk, and only compiling the result, tells you whether the nonsense happened
to be true. So the design puts its weight on three deterministic mechanisms that
cost zero model tokens:

- **The index.** The model is handed a map so it never has to go looking.
- **Unique-match search/replace.** An edit block must match the file on disk
  exactly once, whitespace-exact, or it bounces. No fuzzy application, ever.
- **The compile gate.** Edits land in an overlay, `cargo check` runs, and only a
  clean result is offered to the user. Measured against the first real target, a
  14.7k LoC workspace: **0.77s** warm, against a model loop of about fifty
  seconds. It is the cheapest thing in the system and the only one that tells
  the truth.

## Findings worth stealing

Measured against `heretic:latest` (`Qwen3.6-35B-A3B-uncensored-heretic`, Q4_K_M)
on Ollama, with `qwen3-coder:30b` as a same-quant, same-active-size,
pure-attention control. Full method in
[`docs/measurements/`](docs/measurements/2026-08-12b-clean-rerun.md).

**A compile gate that reverts failed edits cannot accept a whole class of correct
changes.** This is the one to steal, and it took running the suite to see it.
Reverting on red sounds obviously right and quietly restricts the model to
*individually compilable* edits, which is a much smaller set than correct ones.
Ask it to add a field to a struct and fix the literals that break: adding the
field is what breaks them, so there is no single edit from green to green. The
control model proposed eight edits, every one matching the file exactly, none
fabricated, and the harness threw all eight away. Twelve turns, four minutes,
file unchanged, and a first-apply pass rate of 0/8 that looks exactly like a
model which cannot write Rust. It had written a correct edit on turn one. Edits
must accumulate; the harness rolls back when the rustc error count stops falling,
which is a measure of progress rather than of elapsed failure.

**Neither model completed a single multi-site edit, in twelve attempts.** Rename
a method with callers in two other files, or add that struct field: 0/3 for the
driver and 0/3 for the control. Every other task in the suite ran at 2/3 and 3/3.
The failure is a cliff and it is the harness's, not the model's, which is worth
knowing before anyone spends a month choosing a better model.

**Watch what your success metric counts.** The model emits edits whose `replace`
is byte-identical to its `search`. The harness applied them, `cargo check` passed
because nothing had changed, and the metric recorded a pass. Two of five tasks in
one trial "passed the gate" that way. Separately, four of the five smoke tasks
had no real oracle, falling back to "does the workspace still compile" — which is
true of a workspace nobody has touched, and would have scored a pass for a run
that made no edit at all and said it was done.

**Decode is the binding cost, not prefill.** 359 tok/s prefill against 25.3
tok/s decode, so one generated token costs about fourteen prompt tokens. Context
is cheap and can be paid for once; output is expensive and is paid every turn.
Bound what the model *writes*, not what it reads.

**Ollama's constrained decoding enforces less than you think.** It enforces
valid JSON, the property whitelist, `enum`, `maxLength`, and the top-level
`required` list. It does **not** enforce property order, and it does **not**
enforce `required` inside `anyOf` branches. So per-tool argument requirements
cannot be expressed in a schema at all: an `anyOf` discriminated on a `const`
tool value returned `{"tool": "edit"}` and nothing else. The workable answer is
to require every field and let unused ones come back as empty strings, which
tested clean, including on cold turns where forcing the fields might have
invited fabrication and did not.

**Driving Claude Code headlessly costs ~33k tokens of preamble per invocation**
(measured 16,754 created plus 15,986 read, `ephemeral_1h`), and about 4 seconds
of spawn. So: one persistent process per run over `--input-format stream-json`,
never one per unit of work. Also: `--allowed-tools` gates *tools*, not *paths*.
Given `Write(out/**)` it wrote outside the pattern without complaint, while a
`Bash` call in the same run was correctly denied. Allow rules widen; they do not
narrow. Confine by withholding the tool.

**The chosen model's failure mode is stalling, not fabricating.** On an
identical task it scored 0/3 complete correct edits against the control's 2/3,
and every failure was a valid `read` action re-reading a file it had already
been shown. That is deterministically detectable and cheap to refuse, which is
the whole argument for putting the intelligence in the harness.

## One set of measurements here is wrong, deliberately kept

[`2026-08-12-heretic-thinkpad.md`](docs/measurements/2026-08-12-heretic-thinkpad.md)
reports 13 tok/s decode, 40-66 tok/s prefill, and concludes at some length that
a hybrid SSM model cannot reuse a KV prefix incrementally because it carries one
rolling recurrent state. The real figures are ~25 and ~359 tok/s, and prefix
reuse works, 3/3.

The cause was mundane: probes taken minutes apart, unwarmed, under Ollama's
default five-minute `keep_alive`, so the model was unloading between them and
reload time was landing inside `prompt_eval_duration`. An entire architectural
argument, including a confident rule of thumb about model selection, rested on
it.

It is kept, marked superseded, because the reasoning was tidy and plausible and
wrong, and that is more instructive than the correct numbers. The methodology
rules that came out of it are in RFC-0001 §12.1 and implemented in
`scripts/bench-clean.py`: warm the model, set `keep_alive`, fresh content per
trial, three trials minimum, report all of them, prefer end-to-end numbers.

Timing findings from that session were wrong by up to eight times. Every
*behavioural* finding from the same session reproduced exactly. That asymmetry
is the lesson.

## Prerequisites

- Rust 1.85 or newer, edition 2024.
- `rust-analyzer` as a rustup component, for the SCIP index.
- A git repository. Honeyguide refuses to start outside one.
- An Ollama server, Q4_K_M or better. Q3 degrades tool-calling before it
  degrades chat, which is the worst possible failure ordering.
- Optionally the `claude` CLI, for the semantic half of the index. Without it,
  index generation degrades to structural-only and the tool still works.

## Configuration

Copy `honeyguide.toml.example` to `honeyguide.toml`. Every key is optional and
the file documents what each default is protecting you from.

## Licence

MIT. Take anything useful; the measurements especially.
