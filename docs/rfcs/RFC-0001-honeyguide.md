# RFC-0001: Honeyguide, an asymmetric-intelligence coding agent

| | |
|---|---|
| Status | draft |
| Author | Pete / Nixum |
| Revision | 2 (2026-08-12) |
| Depends on | `docs/research/2026-08-local-model-tui-coding-agent.md` |
| Evidence | `docs/measurements/2026-08-12-heretic-thinkpad.md` |
| Target | v0.1 (index + local loop), v0.2 (escalation) |

## Revision note

Revision 1 was written against an assumed target of Qwen3-Coder-30B-A3B on a
local Ryzen box, with llama-server and GBNF as the preferred serving path. The
actual target was then probed and is materially different, so the following
sections changed:

- **§8.2** The driver is `heretic:latest` on `pepe-thinkpad` over Tailscale:
  `Qwen3.6-35B-A3B-uncensored-heretic`, Q4_K_M, a hybrid SSM/attention MoE. It
  is not a Coder model and carries no agentic fine-tune.
- **§8.1** Ollama is the primary backend, not the fallback. That removes GBNF
  and chat-template control from the design and puts JSON-schema constraint in
  their place.
- **§8.3** New. Measured serving numbers replace the research doc's estimates.
- **§8.5** Rewritten twice. Prefill, not generation, is the constraint that
  shapes the prompt. The KV prefix cache then turned out not to serve extended
  prefixes at all, which withdrew the append-only rule and cut the prompt budget
  from ~17.7k tokens to ~4k.
- **§6** Two-phase generation is out. It costs a second full prefill for a
  fraction of a reasoning benchmark. One constrained call per turn, with
  `reasoning` as the first schema field.
- **§6.3, §7** Two new deterministic rules, both of which the very first probe
  request would have needed: read-before-edit, and bounded action strings.
- **§9** The strong model is Opus 5 via `claude -p`.
- **§15** Q1 is answered. Q5 is new, and asks whether a hybrid SSM model is
  simply the wrong shape for an agent loop.

## 1. Summary

Honeyguide is a terminal coding agent for small local models. It inverts the
usual arrangement: a strong model (Opus 5, through a Claude Max subscription in
headless mode) builds a rich, persistent index of the project **offline**, and a
small local model **consumes** that index at runtime to make small, targeted
edits under deterministic guardrails.

The local model never explores the repository, never emits a free-form tool
call, and never writes an edit that has not passed `cargo check`.

Named for the honeyguide bird, which cannot open the hive itself and so leads a
stronger partner to it. Here the roles are inverted: the strong partner charts
the territory, and the small bird works it.

## 2. Motivation

Ward failed for structural reasons, catalogued in the research doc: a
marker-based tool protocol with no output constraint, whole-file writes, no repo
index, no validation gate, no retry loop. The general-purpose agents all assume
frontier-grade native function calling and degrade badly on 3B-active MoEs:
empty tool calls, markup leaking into content past roughly five tools, silent
4096-token context truncation under Ollama defaults.

Two findings carry most of the weight.

1. Strong-model plan and context injection into a weak model's window
   consistently improves execution success, most strongly within the same model
   family (arXiv:2605.26720).
2. For weak models the winning edit format is the one that is easiest to
   **validate deterministically**, not the one that maximises model accuracy.
   Small open models "benefit little from any formatting choice" (Diff-XYZ).
   The compile gate does the quality work.

There is now a third, from our own first request to the model. Asked to rename a
function in `src/lib.rs`, with no file contents supplied, it emitted a
schema-perfect edit action whose `search` field was an entire invented
`src/lib.rs`: `cargo new` boilerplate, a function that did not exist, two
duplicate `mod tests` blocks, and a run-on string that consumed the token budget
without terminating. The structure was flawless and the content was fiction.

That single response is the argument for the whole design. Constraint gives you
well-formed nonsense. Only matching against disk, and only compiling the result,
tells you whether the nonsense was true.

## 3. Goals and non-goals

**Goals**

- **G1** Usable interactive sessions at the measured local throughput (§8.3).
- **G2** First-apply `cargo check` pass rate (FACP) of 60% or better on the
  smoke suite with the full index. This is the M0 gate; below it, revisit the
  model before building more harness.
- **G3** Well-formed action rate (WFA) of 95% or better. Schema-constrained, so
  a failure here indicates a serving-layer bug, not model drift.
- **G4** Degraded mode: index generation with no Claude access at all
  (`rust-analyzer scip` plus tree-sitter only) must still produce a usable tool.
- **G5** Small Rust projects, up to roughly 50k LoC, single workspace.

**Non-goals**

- **N1** Multi-language support in v0.x. Rust only; the index pipeline is
  rust-analyzer-specific by design.
- **N2** Autonomous multi-file feature work. The target is conversation plus
  small targeted changes.
- **N3** Editor integration. The SQ/EQ split keeps the door open; nothing more.
- **N4** Model-initiated escalation. Escalation is harness- or user-initiated
  only (§9).

## 4. Architecture

A Cargo workspace of four crates.

```
honeyguide/
  crates/
    hg-core/    # agent loop, session state, tool dispatch, compile gate, escalation
    hg-index/   # index generation, refresh, retrieval: scip + tree-sitter + claude -p
    hg-llm/     # backend abstraction, schema management, serving preflight
    hg-tui/     # ratatui frontend, talks to hg-core only via SQ/EQ
```

The data model for all four is already written down in those crates and
compiles. Where this document and the types disagree, the types are wrong and
should be corrected.

### 4.1 Submission queue and event queue

`hg-core` owns a submission queue of user intent flowing in and an event queue
of agent events flowing out. The TUI is one consumer; `hg headless -p "task"` is
another, which yields scriptable eval runs for free. The pattern is lifted from
`codex-rs`.

The event stream is the single source of truth for the transcript, the
telemetry, and the eval corpus. Every model call, tool result, and gate decision
is appended to a session JSONL log. That log is a first-class artifact from day
one, because it is also the design input for escalation (§9), and a feature
designed before its failure data exists is guesswork.

See `hg_core::Submission` and `hg_core::Event` for the exact shapes.

## 5. The index (`.agent-index/`)

Committed to the repository by default. It is documentation that happens to be
machine-readable, and a reviewer who reads it learns something.

```
.agent-index/
  manifest.toml        # schema version, per-artifact git rev, generation model
  AGENTS.md            # project brief: stack, commands, conventions, invariants, no-touch list
  repomap.txt          # tree-sitter map, PageRank-ranked, budgeted (default 1200 tokens)
  scip.sqlite          # rust-analyzer SCIP output: symbols, definitions, references, signatures
  summaries/
    <module_path>.md   # 2-5 sentences per module: responsibility, key types, invariants, gotchas
  callgraph.jsonl      # edges derived from SCIP references, for blast-radius computation
```

### 5.1 Generation: `hg index`

Three passes, in order.

1. **Structural (no LLM, always runs).** `rust-analyzer scip . --output
   index.scip`, ingested into `scip.sqlite` as `symbols(id, kind, sig, file,
   range)` and `refs(symbol_id, file, range, is_def)`. Then a tree-sitter pass
   builds a symbol graph, PageRank ranks it, and the result is budgeted into
   `repomap.txt`. Then `callgraph.jsonl` is derived from the SCIP references.
2. **Semantic (Opus 5, optional).** One persistent `claude -p --model opus`
   process per index run, fed over `--input-format stream-json`, with the
   working directory set to the project root and tools restricted to
   `Read Grep Glob`. One message per module, one response per module. The
   request carries that module's file paths and symbol signatures from pass 1;
   Claude reads whichever bodies it wants. `AGENTS.md` is requested last, once
   every summary is in hand.

   **See RFC-0002.** This boundary has more sharp edges than one bullet can
   hold: a 33k-token preamble on every invocation, an allow-list that gates
   tools but not paths, and no structured-output mode at all. Revision 1 of this
   section specified a "strict output schema", which does not exist, and per-batch
   invocations, which would pay the preamble over and over.
3. **Degraded (G4).** If the `claude` binary is absent, or `--no-llm` is passed,
   pass 2 is skipped. `AGENTS.md` is stubbed from `Cargo.toml` metadata and
   detected commands, summaries are omitted, and retrieval falls back to
   structural-only.

The manifest records the git rev each artifact was generated at.

### 5.2 Incremental refresh: `hg index --refresh`

1. `git diff --name-only <manifest.rev>..HEAD` gives changed files, and from
   them the owning modules.
2. Re-run scip, re-ingest, regenerate the repo map. A full re-index is fine at
   50k LoC.
3. Re-summarise **only** the changed modules, through a single `claude -p`
   process (RFC-0002 §3). Never one process per file: each one costs four
   seconds and a 33,000-token preamble before it has read anything.

At runtime, if `manifest.rev != HEAD` and the changed files intersect the task's
blast radius, the harness prefixes the context with a one-line staleness warning
and uses on-disk content as truth for edit matching (§7). The index is a map. It
is never ground truth for file contents.

### 5.3 Retrieval

No embeddings in v0.1. Retrieval is structural and runs in the harness, before
the first model turn:

1. Extract candidate symbols from the user message, by lexical match against
   `symbols.sig` and fuzzy match on identifiers.
2. Pull the matching signatures, the defining modules' summaries, one hop of
   call-graph neighbours (the blast radius), and `repomap.txt`.
3. Assemble under the context budget (§8.5). Function bodies are included only
   for symbols in the direct blast radius, and are fetched from disk at
   assembly time.

Embedding fallback (fastembed over the summaries) is v0.3, and only if
structural retrieval demonstrably misses on the smoke suite. Do not build it
speculatively.

The assembled context is **frozen for the duration of the task**, and retrieval
answers to a wall-clock budget rather than a token one. Every token it selects
is paid for again on every subsequent turn (§8.5), so the question at assembly
time is not "might this be relevant" but "is this worth a fifth of a second per
turn for the rest of the session". Most things are not.

## 6. Agent loop

Strict ReAct, one tool per turn, **one model call per turn**.

```
loop:
    prompt = frozen_prefix + history + last_observation

    one schema-constrained generation of a single action object,
    whose first field is `reasoning` and whose remaining fields are the call

    parse (infallible by construction) -> check preconditions -> dispatch
    observation = tool result, truncated to the per-tool cap
    if action == finish: end turn
    if repair_count(current_edit) >= max_repairs: EscalationSuggested, halt, wait
```

This is not what revision 1 specified, and the change is forced by measurement.

The textbook arrangement is two phases: generate reasoning unconstrained, then
switch to constrained decoding for the action, so that the 10-30% reasoning tax
from constrained decoding (Tam et al.) never applies to the thinking. It is the
right design, and on this hardware we cannot have it.

Phase 2's prompt is phase 1's prompt plus phase 1's output, which is a strict
prefix extension, and the prefix cache on this model does not serve extensions
(§8.3). So two phases means two full prefills, and at 5k context that is 160
seconds of a turn instead of 80. Paying to double the dominant cost, in order to
recover a fraction of a reasoning benchmark, is not a trade worth making.

Instead: one call, with `reasoning` as the first field of the schema and a
`maxLength` on it. The model still emits its reasoning before it commits to a
tool, which is most of what phase separation buys, and it costs one prefill.
Field order in the schema is load-bearing here rather than cosmetic.

Two-phase generation stays in the design for any backend where partial prefix
reuse works, which means a pure-attention model or llama-server with slot reuse.
`hg_llm::Phase` exists for that reason. It is dormant, not deleted.

`think: false` is sent on every request. The model advertises a thinking
capability, and a hybrid reasoning model left to its own devices will spend
hundreds of tokens deliberating before it says anything, at 13 tok/s. The
`reasoning` field is the deliberation budget, and it is ours to cap.

**Turns are the unit of cost.** Every turn pays a full prefill, so the way to
make a session fast is not to shave tokens but to remove round-trips. This is
the strongest argument in the document for the index: a `read` turn that the
harness could have pre-empted by loading the blast radius is a minute and a half
that the user waits for information we already had on disk.

### 6.1 Tool surface

Five tools. A hard ceiling, not a starting point: past roughly five, Qwen-class
MoEs abandon the trained call format and leak markup into the content field
(Goose #6883).

| Tool | Signature | Notes |
|---|---|---|
| `read` | `read(path, start?, end?)` | Line-numbered slice. Also marks the path as seen (§6.3). |
| `search` | `search(query)` | `scip.sqlite` first, ripgrep fallback, 20 hits max. |
| `edit` | `edit(path, search, replace)` | Proposes an edit into the gate (§6.2). |
| `check` | `check()` | Runs the check command in the overlay, returns rustc diagnostics. |
| `finish` | `finish(summary)` | Ends the turn. |

`search` hits the index first, and that is precisely what removes the model's
need for `ls` and `cat`. There is **no shell tool** in v0.1. Every capability
removed is a failure mode removed; revisit only with eval evidence.

### 6.2 The compile gate

Edits never touch the working tree directly.

1. At session start, build an overlay of the project (Q2).
2. `edit` applies to the overlay, then `cargo check --message-format json` runs
   workspace-scoped.
3. On pass, emit `EditProposed` with a diff. The TUI offers approve or reject;
   auto-approve is configurable. On approval, replay onto the working tree with a
   fresh unique-match check against the real file, which guards against an
   external edit landing in between.
4. On failure, emit `EditBounced`, feed the rustc errors back verbatim as the
   observation, and increment the repair count.

`cargo check` on a warm target directory for a small project takes 1 to 5
seconds. A single model turn takes considerably longer than that. The gate is
therefore both the primary quality mechanism and the cheapest thing in the loop;
everything else is context plumbing.

### 6.3 Deterministic preconditions

Checked by the harness before dispatch, costing zero model tokens. A refusal
here is an observation like any other, and the model gets another turn.

- **Read before edit.** An `edit` whose `path` has not been `read` in this
  session is refused outright, with the observation "you have not read this
  file; read it first". This is a direct response to the observed failure: a
  cold model asked to edit a file it has not seen will confidently invent its
  entire contents. Refusing costs nothing; letting it through costs a wasted
  turn at best.
- **Bounded strings.** Every string field in the action schema carries a
  `maxLength`. Unbounded, the model will run a `search` string until it hits the
  token cap, which it did on the first request ever sent to it.
- **Bounded edits.** A `replace` longer than `gate.max_edit_lines` bounces with
  an instruction to split, which prevents the whole-file-rewrite regression.
- **Truncation is fatal, not repairable.** If generation stopped at the token
  cap, the action is discarded rather than parsed. Half a JSON object tells you
  nothing, and guessing the rest is how harnesses corrupt files.

## 7. Edit format

Search/replace blocks, chosen for validatability rather than for accuracy.

- `search` must match the on-disk file (overlay state) **exactly once**.
- Zero matches bounces with "not found; the nearest fuzzy match is at line N",
  and shows it. Fuzzy matching is used to write the error message and for
  nothing else.
- Multiple matches bounces with the match locations and an instruction to extend
  the search span.
- Whitespace-exact. No fuzzy application, ever. Fuzzy apply is how harnesses
  silently corrupt files.

## 8. The local model and its server

### 8.1 Backends

`hg-llm` abstracts over two, of which only the first exists in v0.1.

- **Ollama** (primary). What is actually running. Gives JSON-schema constraint
  through `format`, per-request `num_ctx`, and per-request `keep_alive`. Costs
  us GBNF, chat-template control, and explicit slot management.
- **llama-server** (planned). Buys GBNF and `--jinja`, at the price of running
  our own server. `ik_llama.cpp` is configuration-compatible and is the
  recommended CPU build for MoE, given the sparse-activation overhead in
  llama.cpp #19480, which is worst for exactly this hybrid architecture.

### 8.2 The model

`heretic:latest`, served from `pepe-thinkpad:11434` over Tailscale. From the
GGUF metadata:

| Property | Value |
|---|---|
| Base | `Qwen/Qwen3.6-35B-A3B` |
| Fine-tune | `llmfan46/Qwen3.6-35B-A3B-uncensored-heretic`, GGUF by mradermacher |
| Architecture | `qwen35moe`, 40 blocks, hybrid SSM plus attention, `full_attention_interval = 4` |
| Experts | 256 total, 8 used, expert FFN 512, so roughly 3B active |
| Quantisation | `file_type = 15`, Q4_K_M, 21.2 GB on disk |
| Context | 262144 native |
| Attention | 16 heads, 2 KV heads, key/value length 256 |
| Sampling defaults in metadata | temp 1.0, top_k 20, top_p 0.95 |

Three things follow.

**It is not a Coder model.** It is a general instruct model that has been
abliterated. There is no agentic or coding fine-tune underneath, so the
community's roughly 96% well-formed-tool-call figure for Qwen3-Coder-30B does
not transfer, and neither does its edit-format competence. Schema constraint
covers the format. Nothing covers the judgement except the gate.

**Q4_K_M is the quant floor and we are exactly on it.** Q3 and below degrade
tool-calling before they degrade chat, which is the failure mode hardest to
notice and worst to inherit.

**The hybrid SSM layers are a design input, not trivia.** Only one layer in four
is full attention; the rest carry recurrent state. That is the architecture
family llama.cpp #19480 flags as worst for CPU MoE throughput, and recurrent
state is why prefix-cache behaviour across requests is a thing to measure rather
than assume (Q1).

The abliteration is an accepted, unmeasured risk. There is no published data on
whether heretic-class abliteration preserves structured-output reliability. Q4
makes measuring it a deliverable rather than a hope.

### 8.3 Measured baseline

Measured against `pepe-thinkpad` on 2026-08-12. Full method and raw output in
`docs/measurements/2026-08-12-heretic-thinkpad.md`.

| Quantity | Measured |
|---|---|
| Decode | 13.0 tok/s |
| Prefill, 5.1k prompt | 66 tok/s (77.2s) |
| Prefill, 15.4k prompt | 40 tok/s (383.7s) |
| Cold load | 26.1s |
| Prompt above `num_ctx` | HTTP 400, rejected rather than truncated |
| Schema-constrained emission | valid, first attempt, field order respected |
| Native tool call, 3 tools | correct tool, correct argument, no content leakage |
| KV prefix cache, identical prompt | 77.2s becomes 0.2s |
| KV prefix cache, prompt extended by 6 tokens | 81.7s, a full recompute |
| Same test, `qwen3:8b` (pure attention, same host) | extension costs **1.2s** |

Two of those lines carry the design.

**Prefill is the binding cost, and it gets worse with length.** It runs at
roughly three to five times decode, but it degrades from 66 tok/s at 5k to 40
tok/s at 15k. Prompt processing on a bandwidth-bound CPU MoE is not the cheap,
parallel phase it is on a GPU.

**The prefix cache is exact-match only, and it is the model's fault.** An
identical prompt is free. A prompt extended by six tokens is a full recompute. A
control run against `qwen3:8b` on the same server in the same minute reuses the
prefix incrementally and pays 1.2 seconds for a five-token extension, so Ollama
is willing and able; this model cannot be served that way. A hybrid SSM carries
one rolling recurrent state rather than a per-token cache, so there is no
position to rewind to.

That finding is the expensive one, and §6 and §8.5 are written around it. It
also outranks everything else in this document, including the abliteration
question: over a ten-turn session, a pure-attention model of the same class pays
one full prefill and then seconds, while this one pays minutes every turn. The
recommendation, offered once and then set aside because the driver is chosen
(§8.2), is that Q4's A/B should be run early and judged on wall-clock first.

The working figure for planning is:

```
turn_seconds  ~=  prompt_tokens / 50  +  generated_tokens / 13
```

Every token in the frozen prefix therefore costs about 20ms **on every turn**,
not once. A 1,200-token repo map is 24 seconds of every turn a session ever
takes. That is the budget the index has to justify itself against.

### 8.4 Serving preflight

`hg-llm` probes the server at startup and **refuses to run** on a failed probe
rather than degrading silently. A context window quietly truncated to 4096
presents as "the model cannot use tools", and that misdiagnosis has cost the
wider community entire weekends.

The probe asserts:

- `num_ctx` is honoured at or above `llm.params.min_ctx`. Note that
  `OLLAMA_NUM_PARALLEL` divides the server's context across slots, so the
  advertised number is not the number you get.
- The model answers a trivial schema-constrained request with valid JSON.
- Sampling parameters are sent explicitly. The Modelfile carries no `PARAMETER`
  lines, so anything we omit is inherited from whatever Ollama defaults to that
  week, and its default temperature is not ours.
- `keep_alive` is set generously (default 30m). Cold load is measured in tens of
  seconds, and a model unloaded between turns turns a 20-second turn into a
  50-second one for no reason at all.

Results are shown by `/model`.

### 8.5 Context budget and prompt discipline

The context budget is not about what the model can hold. It is about what we can
afford to send, **every single turn**.

Revision 1 assumed a 32k working window and an append-only discipline that would
let the prefix cache absorb most of the cost. The cache does not work that way
here (§8.3), so there is no absorbing it: each turn pays
`prompt_tokens / 50` seconds before the model says a word.

That reframes the budget as a wall-clock allowance. At a target of roughly 90
seconds per turn, of which generation takes 15 to 25, the prompt has to come in
under about 4k tokens.

| Slice | Rev 1 | Rev 2 | Cost per turn at 50 tok/s |
|---|---|---|---|
| System prompt and tool semantics | ~1.5k | ~700 | 14s |
| `AGENTS.md` | ~1k | ~500, the invariants only | 10s |
| `repomap.txt` | ~1.2k | ~400, or omitted (Q3) | 8s |
| Task index slice: signatures and summaries | ~2k | ~800 | 16s |
| Function bodies in the blast radius | ~4k | ~1k, the target function and callers | 20s |
| History and observations | ~8k | ~600, aggressively truncated | 12s |
| **Total prompt** | ~17.7k | **~4k** | **~80s** |

Every number in the middle column is a claim that this slice is worth its
seconds, and the smoke suite is how those claims get tested. The `repomap.txt`
line is the weakest of them: 400 tokens is 8 seconds of every turn forever, for
a breadth-first overview that a weak model may not even use well. Q3 now has a
wall-clock answer to give, not just an accuracy one.

Two rules survive from revision 1, for different reasons than before.

**The prefix is still frozen**, not to preserve a cache but because re-running
retrieval mid-task cannot pay for itself: better context does not repay a second
full prefill in the same session.

**Compaction is still explicit.** It is deterministic and template-driven
(files touched, edits applied and bounced with one-line reasons, current goal),
with no LLM summarisation call, because at 13 tok/s you do not spend generation
on bookkeeping. It now has a second justification: compaction that *shrinks* the
prompt pays for itself immediately, on the very next turn, which makes it
cheaper here than it would be on a machine with a working prefix cache. This is
the one place where the bad news makes something better.

The append-only rule from revision 1 is **withdrawn**. It bought prefix-cache
hits, there are no prefix-cache hits, and it was constraining how observations
could be merged and truncated for no return.

## 9. Escalation (v0.2)

Not to be built before v0.1 telemetry exists.

Harness- or user-initiated only. There is no `consult_oracle` tool, because a
weak model will either overuse one or invoke it with useless context.

Triggers: `max_repairs` bounced repairs on one edit; the same rustc error code
twice across different repair attempts; or the user typing `/escalate`. On
trigger the core emits `EscalationSuggested`, the TUI asks, and nothing happens
without explicit confirmation, because it spends Max quota.

The package is one `claude -p --model opus` call containing the task, the index
slices that were in context, every attempted search/replace with its
diagnostics, and current file contents for the blast radius. It reuses the
transport and the containment rules of RFC-0002, with one difference: the user
is watching, so the 4-second floor and the multi-turn latency are visible rather
than amortised, and the TUI has to say what it is waiting for.

The output is either a plan (an ordered edit list with rationale, injected into
the local loop as an authoritative-plan message, which is the plan-injection
pattern the research supports) or a direct patch, which enters the same compile
gate and is provenance-tagged in the transcript and the diff view. Which of the
two is asked for is decided by the harness, not the model, so there is nothing
to parse: a plan request gets a plan.

## 10. TUI

ratatui, immediate-mode, with insta snapshot tests, following `codex-tui`.

Layout: a transcript pane with reasoning collapsed by default and toggled with
`r`; a diff pane on `EditProposed` with approve `y` and reject `n`; and a status
line carrying model, backend, tokens per second, context fill, prefix-cache hit
rate, index staleness, and repair count.

Slash commands: `/index`, `/compact`, `/undo`, `/model`, and `/escalate` at
v0.2.

The prefix-cache hit rate is on the status line rather than buried in telemetry
because it is the number that explains why a turn was fast or slow, and a user
who can see it will learn the shape of the machine they are driving.

Honeyguide requires a git repository and refuses to start otherwise. `/undo` and
index refresh both depend on it. Not negotiable.

## 11. Configuration

`honeyguide.toml` at the project root, all keys optional. See
`honeyguide.toml.example` for the annotated defaults.

## 12. Telemetry and evals

Per-session JSONL, derived from the event log: every prompt and completion pair,
tool call and result, gate decision, repair chain, token count, and wall-clock
per turn.

Two headline metrics, tracked across releases:

- **WFA**, well-formed action rate. Should sit near 100% under schema
  constraint. A drop means a serving bug, not model drift.
- **FACP**, first-apply `cargo check` pass rate. The real quality number, gated
  at 60% for M0.

A third is worth watching on this hardware: **prefill cache hit rate**, since it
is the difference between a usable session and an unusable one, and it will
silently collapse the first time someone rewrites the prompt prefix.

The smoke suite (`hg eval run`, headless) is roughly ten scripted tasks against
two or three pinned small Rust crates, each with a `cargo test` oracle. Seed
set: rename a function across the crate; add a struct field and fix all
constructors; fix a planted failing test; add a `Display` impl; extract a
function; and three comprehension questions graded by must-mention assertions.

Run per PR in CI against a pinned model, quant, and serving configuration. The
Aider polyglot Rust subset is a secondary, occasional benchmark and not CI: too
slow at local speeds, and harness variance makes cross-leaderboard comparison
meaningless anyway.

## 13. Milestones

- **M0, thesis spike.** A throwaway script, not a product. Three tools, schema
  constraint, a hand-written index for one crate, five smoke tests. Two gates
  now, not one: FACP at 60% or better, **and** a median turn under 120 seconds
  on a realistic multi-turn task. The second gate is the one §8.3 put there, and
  a tool that is correct but takes four minutes a turn has failed just as
  surely as one that is fast and wrong. Below either gate, pull the model A/B
  (Q4) forward before writing any more harness.
- **M1, index pipeline.** `hg-index` complete, including degraded mode and
  `--refresh`. The deliverable is `hg index` run against nuthatch producing an
  `.agent-index/` a human would actually read.
- **M2, core loop.** `hg-core` and `hg-llm`: SQ/EQ, ReAct, two-phase generation,
  five tools, the gate, repair, compaction, JSONL telemetry, headless mode.
  Smoke suite green.
- **M3, TUI.** ratatui frontend, diff approval, status line, slash commands.
  This is v0.1.
- **M4, escalation.** Designed from at least two weeks of v0.1 session logs.
  This is v0.2.

## 14. Alternatives considered

- **Fork OpenCode, Crush, or Goose.** Rejected. All are built around native
  function calling and broad tool surfaces; retrofitting constrained two-phase
  generation and index-first context flow is a rewrite of their core loop.
  Borrow ideas, specifically Goose's toolshim lessons and Aider's repo map and
  editblock coder, not code.
- **Code-as-action (CodeAct).** Better success and turn efficiency in the
  literature, but it needs a sandboxed interpreter and enormously expands the
  failure surface for a 3B-active model. Held in reserve. The five-tool loop
  must fail first.
- **Unified diffs.** Rejected. Small models confuse the marker characters with
  ordinary code, and udiff application is inherently fuzzy. Validatability wins.
- **Embeddings-first retrieval.** Rejected for v0.1. SCIP gives exact answers to
  exactly the questions the harness asks: who calls X, what is the signature of
  Y. Semantic fuzz is a fallback, not a foundation.
- **Native tool calling via Ollama's `tools` parameter.** Measured rather than
  assumed, and it works better than the survey feared: with three tools the
  model selected the right one, passed a sensible argument, and left `content`
  empty, with no markup leakage. The chat template renders the Qwen3-Coder XML
  format correctly, so the Hermes mismatch of Ollama #14493 does not apply here.

  Rejected anyway, for one specific reason. Ollama's tool-parameter schemas are
  advisory: they are rendered into the prompt and are not enforced during
  decoding. The `format` path is enforced. On the first request this project
  ever made, the model ran a string until it hit the token cap, and the thing
  that stops that recurring is a `maxLength` the decoder actually honours.
  Native calling gives us a well-chosen tool with unbounded arguments; schema
  constraint gives us bounded arguments. We need the bounds more.
- **Model-initiated escalation.** Rejected. Deterministic triggers only.

## 15. Open questions

- **Q1: prefix-cache behaviour across requests. ANSWERED, and badly.** Exact
  repeats are free (77.2s becomes 0.2s). Extensions are not: six extra tokens
  cost a full recompute of all 5,142. There is no partial prefix reuse on this
  model, which is what a single rolling recurrent state implies. Consequences
  are taken in §6 (one call per turn, not two) and §8.5 (a 4k prompt budget and
  the withdrawal of the append-only rule). The question this leaves open is
  narrower, and the control has answered that too: `qwen3:8b` on the same host
  pays 1.2s for a five-token extension, so it is the architecture, not the
  server. Nothing about the serving stack will fix it. What remains open is
  whether llama-server or `ik_llama.cpp` handle recurrent state any better,
  which is worth an hour before it is worth a model change.
- **Q2: overlay mechanism.** A `cp -al` hardlink forest is fragile if any tool
  writes in place. The likely answer is a per-edited-file copy with a shared
  `--target-dir`, but it needs testing against `cargo check`'s own behaviour.
- **Q3: does the repo map help or hurt at this scale?** Aider warns that weak
  models get confused by large maps. There is now a second, sharper form of the
  question: at 50 tok/s of prefill, a 1,200-token map costs 24 seconds of every
  turn for the life of the session. Smoke-test at 0, 400, and 1,200 tokens
  during M2, and require it to earn those seconds rather than merely not hurt.
- **Q4: heretic against stock Qwen3-Coder-30B-A3B, reframed.** Revision 1 asked
  a quality question: what does abliteration cost in first-apply pass rate?
  That still matters and nobody appears to have published it. But Q1 has added a
  second axis that may dominate. Qwen3-Coder-30B-A3B is a pure-attention MoE, so
  if partial prefix reuse works for it on the same server, it wins turns that
  cost seconds against turns that cost minutes, and no plausible quality delta
  compensates for that. Measure both at M0: FACP, and wall-clock per turn over a
  realistic multi-turn task. Publish both in the README.
- **Q5: is a hybrid SSM model simply the wrong shape for an agent loop?** The
  generalisation of Q4, and the control says probably yes. Recurrent state buys
  long-context efficiency within a single forward pass and costs incremental
  prefix reuse across passes. An agent loop is nothing but repeated passes over
  a slowly growing prefix, which is close to the worst case for that trade.
  Stated as a selection criterion: **for a local agent loop, prefer a
  pure-attention MoE over a hybrid SSM MoE of the same active size, and check
  `ssm.*` in the GGUF metadata before pulling anything.** That belongs somewhere
  more visible than an RFC, and once M0 has confirmed it on a real task, it
  should go there.
