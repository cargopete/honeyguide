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

## Status: design only. There is no working agent.

Nothing here runs yet. What exists is a specification, the data model as Rust
types that compile and do nothing, and a set of measurements against a real
local model. It is public because the measurements are the useful part and
nobody else seems to have published them.

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
