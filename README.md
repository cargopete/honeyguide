# honeyguide

A terminal coding agent for small local models, built the other way round.

A strong model (Opus 5, headless, offline) charts the repository once and writes
a persistent index. A small local model (a ~3B-active Qwen MoE on a machine in
the next room) consumes that index at runtime and makes small, targeted edits
inside a harness that will not let it lie. It never explores the repository, it
never emits a free-form tool call, and it never writes an edit that has not
passed `cargo check`.

Named for the honeyguide bird, which cannot open the hive itself and so leads a
stronger partner to it. Here the roles are inverted: the strong partner charts
the territory, and the small bird works it.

## Status

**Design only.** There is no working agent yet. What is in this repository is the
specification, the data model in Rust (it compiles, it does nothing), and the
measurements the specification is built on.

- [`docs/rfcs/RFC-0001-honeyguide.md`](docs/rfcs/RFC-0001-honeyguide.md) is the design.
- [`docs/measurements/`](docs/measurements/) is what the target machine actually does.
- [`docs/research/`](docs/research/) is the prior survey, archived unedited.
- `crates/` carries the RFC's data model as Rust types. `cargo check` passes and
  that is the entire claim.

## Why

Every general-purpose agent assumes frontier-grade native function calling, and
every one of them degrades badly on a 3B-active MoE: empty tool calls, markup
leaking into the content field once you expose more than about five tools, and
Ollama's 4096-token default silently truncating the system prompt so that the
whole thing presents as "the model can't use tools".

The first request this project ever sent to its target model makes the case
better than the survey does. Asked to rename a function in `src/lib.rs`, with no
file contents supplied, it returned a schema-perfect edit action whose `search`
field was an entire invented `src/lib.rs`: `cargo new` boilerplate, a function
that did not exist, two duplicate `mod tests` blocks, and a run-on string that
ate the token budget without terminating.

The structure was flawless. The content was fiction.

Constraint buys you well-formed nonsense. Only matching against what is actually
on disk, and only compiling the result, tells you whether the nonsense happened
to be true. So the design puts almost all of its weight on three deterministic
mechanisms that cost zero model tokens:

- **The index.** The model is handed a map so it never has to go looking.
- **Unique-match search/replace.** An edit block must match the file on disk
  exactly once, whitespace-exact, or it bounces. No fuzzy application, ever.
- **The compile gate.** Edits land in an overlay, `cargo check` runs, and only a
  clean result is ever offered to the user.

## The machine, measured

`heretic:latest` on a ThinkPad over Tailscale: `Qwen3.6-35B-A3B-uncensored-heretic`,
Q4_K_M, 256 experts with 8 active, a hybrid SSM/attention MoE with one full
attention layer in four.

| | |
|---|---|
| Generation | ~13 tok/s |
| Prefill | ~40-66 tok/s, no better at larger batches |
| Cold load | ~26s |

Prefill costs three to five times what generation does, so a 15k-token prompt
takes six and a half minutes just to ingest. That makes the KV prefix cache
load-bearing rather than an optimisation.

It does not bear the load. Repeat a prompt byte-for-byte and it is free (77.2s
becomes 0.2s). Append six tokens and it recomputes all 5,142 of them. A control
run against `qwen3:8b` on the same server in the same minute pays **1.2 seconds**
for the same kind of extension, so this is the model's architecture and not the
server: a hybrid SSM carries one rolling recurrent state rather than a per-token
cache, and there is no position to rewind to.

An agent loop is nothing but repeated passes over a slowly growing prefix, which
is close to the worst case for that trade. So the design pays a full prefill
every turn, and everything follows from trying not to: **one model call per turn
instead of two**, a **~4k prompt budget** where the first draft assumed 17k, and
turns rather than tokens as the unit of cost. It also produces a rule of thumb
worth more than the rest of this repository: for a local agent loop, prefer a
pure-attention MoE to a hybrid SSM one of the same active size, and grep the
GGUF metadata for `ssm.` before you pull anything.

See [the measurement notes](docs/measurements/2026-08-12-heretic-thinkpad.md)
for method and raw numbers, and `scripts/serving-probe.py` if you want to run it
against your own box.

## Prerequisites

- Rust 1.85 or newer, edition 2024.
- `rust-analyzer` as a rustup component (`rustup component add rust-analyzer`),
  for the SCIP index.
- A git repository. Honeyguide refuses to start outside one.
- An Ollama server reachable over the network, with a Q4_K_M or better quant.
- Optionally the `claude` CLI, for the semantic half of the index. Without it,
  index generation degrades to structural-only and the tool still works.

## Configuration

Copy `honeyguide.toml.example` to `honeyguide.toml`. Every key is optional and
the file documents what each default is protecting you from.

## Licence

MIT.
