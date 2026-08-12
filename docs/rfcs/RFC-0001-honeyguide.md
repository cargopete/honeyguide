# RFC-0001: Honeyguide, an asymmetric-intelligence coding agent

| | |
|---|---|
| Status | draft |
| Author | Pete / Nixum |
| Revision | 3 (2026-08-12) |
| Depends on | `docs/research/2026-08-local-model-tui-coding-agent.md` |
| Evidence | `docs/measurements/2026-08-12b-clean-rerun.md` |
| Target | v0.1 (index + local loop), v0.2 (escalation) |

## Revision note

Revision 1 assumed Qwen3-Coder-30B-A3B on a local Ryzen box with llama-server
and GBNF. Revision 2 replaced that with measurements of the real target.
**Revision 3 exists because most of revision 2's measurements were wrong.**

The morning's probes ran unwarmed, minutes apart, under Ollama's default
five-minute `keep_alive`, so the model was unloading between them and reload
time was landing inside `prompt_eval_duration`. That made the machine look five
to eight times slower than it is, and made a working prefix cache look like a
broken one. Two clean architectural arguments were built on those numbers, and
both are withdrawn:

- **Withdrawn: "prefill is the binding cost."** Decode is, by roughly 14x per
  token. §8.3, §8.5.
- **Withdrawn: "a hybrid SSM cannot reuse a prefix incrementally."** It reuses
  fine, 3/3 on a controlled re-run. §8.3, Q1.
- **Withdrawn: "field order in the schema is load-bearing."** Order is not
  enforced at all. §6.
- **Restored:** two-phase generation (§6) and a generous context budget (§8.5),
  both of which revision 2 cut on the strength of the bad numbers.

What survives from revision 2 is everything that came from *behaviour* rather
than *timing*: the model fabricating a whole file when asked to edit one it had
not read, the schema enforcement limits, and the tool surface. Behavioural
findings reproduced; timing findings did not. That is the lesson, and §12 now
says so.

Standing decisions carried forward:

- **§8.2** The driver is `heretic:latest`: `Qwen3.6-35B-A3B-uncensored-heretic`,
  Q4_K_M, hybrid SSM/attention MoE. Chosen deliberately and not up for
  re-litigation; `qwen3-coder:30b` is kept as a control, not a candidate.
- **§8.1** Ollama is the primary backend, which means JSON-schema constraint
  rather than GBNF.
- **§9** The strong model is Opus 5 via `claude -p`. See RFC-0002.

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

Revision 2 collapsed this to a single call, because a second call appeared to
cost a second full prefill of eighty seconds. It does not. Prefix reuse works
(§8.3), and phase 2's prompt is phase 1's prompt plus phase 1's output, which is
exactly the prefix extension the cache serves. Measured, an extension of that
kind costs **1.65 to 2.96 seconds**, not eighty.

So two-phase is restored, and with it the thing it was always for: the model
reasons in its own trained format, unconstrained, and only the action is forced
into a schema. That avoids the 10-30% reasoning tax constrained decoding imposes
(Tam et al.) at a cost of a couple of seconds a turn.

Revision 2 also claimed that putting `reasoning` first in a single flat object
would recover most of this. **That claim is withdrawn**: schema property order is
not enforced, and the same schema produced schema order on one run and
alphabetised order on another. There was never a guarantee that the reasoning
preceded the tool choice.

The single-call form is kept as a fallback for backends without prefix reuse,
and the schema still carries a bounded `reasoning` field so a single call
degrades gracefully. `hg_llm::Phase` models both.

`think: false` is sent on every request. The model advertises a thinking
capability, and left alone a hybrid reasoning model will deliberate for hundreds
of tokens before saying anything. At 25 tok/s that is real time. Phase 1 is the
reasoning budget and it is ours to cap.

**Output is the expensive part.** At 359 tok/s of prefill against 25.3 of
decode, a generated token costs about fourteen prompt tokens. Every cap on
reasoning length and every truncated observation is worth fourteen times its
weight in context, and that, not context size, is where a turn's wall-clock
goes.

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

Measured against the M0 target: a real change in `dipper`'s core crate
cascades to all four dependent crates and `cargo check --workspace` completes in
**0.77s** on a warm target directory (21.7s cold, 0.30s with nothing changed).
A six-turn model loop on the same task takes about fifty seconds.

The gate is therefore both the primary quality mechanism and, by roughly two
orders of magnitude, the cheapest thing in the loop. Everything else is context
plumbing.

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
- **No stalling.** An action identical to the previous one on the same target is
  refused, with the observation "you already did this; here is what you got, now
  act on it". This exists because of the driver's measured failure mode: given a
  file it had already been shown and asked to rename a method, it chose `read`
  again, three times out of three (§8.3). It is not fabricating and it is not
  malformed. It is failing to progress, which is deterministically detectable and
  costs nothing to catch.

**Satisfy, do not refuse, wherever the harness already knows the answer.** M0
tested the refusal design directly and it failed: told "you have not read this
file, read it first", the model did not go and read it. It re-sent the identical
edit eleven times, collecting a refusal each turn until the cap. Twelve turns,
eighty-nine seconds, nothing achieved, and a perfect 12/12 well-formed action
rate throughout.

A refusal is only useful to a model that can act on it. This one cannot
reliably, so any precondition the harness can *satisfy* should be satisfied
instead:

| Precondition | Old behaviour | Revised |
|---|---|---|
| `edit` on an unread path | refuse, "read it first" | return the file contents, note the edit was not applied, invite a retry |
| identical action repeated | refuse each time | refuse, and **abort the turn after three**, rather than burning the cap |
| missing required arguments | refuse, naming them | unchanged; nothing to satisfy |

With that change plus §5.3 pre-loading, the same task went from twelve turns and
no edit to five turns and a clean first-apply pass.

The generalisation is worth stating because it cuts against the instinct: a
deterministic guardrail should where possible hand the model what it was missing,
not tell it what it did wrong. Correction assumes a model that can act on
feedback. Provision assumes nothing.

The last rule is the clearest illustration of the whole thesis. The chosen model
scored 0/3 where the control scored 2/3, and the gap is not a quality the
harness has to hope for. It is a loop failure the harness can simply refuse to
allow.

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

Measured against `pepe-thinkpad` on 2026-08-12, three trials of each figure
against a warmed model with fresh content. Method and raw output in
[`docs/measurements/2026-08-12b-clean-rerun.md`](../measurements/2026-08-12b-clean-rerun.md).

An earlier revision of this section carried figures that were five to eight
times worse and a prefix-cache finding that was simply wrong. Both came from
unwarmed probes minutes apart, where model reload time was being folded into
`prompt_eval_duration`. The superseded document is kept, marked, because the
architectural reasoning built on it was tidy and confident and entirely
unfounded.

| Quantity | `heretic:latest` | `qwen3-coder:30b` (control) |
|---|---|---|
| Prefill, median of 3 | **359 tok/s** | 467 tok/s |
| Decode, median of 3 | **25.3 tok/s** | 19.5 tok/s |
| Incremental prefix reuse | **3/3 yes** | 3/3 yes |
| Six-turn wall clock | **49.9s, 50.5s** | 56.9s, 52.5s |
| Complete + correct edit | **0/3** | 2/3 |
| Cold load | ~26s | ~18s |
| Prompt above `num_ctx` | HTTP 400, rejected rather than truncated | |

The host carries an 8 GB RTX 2000 Ada that Ollama partially offloads to
(`68%/32%` CPU/GPU), which is most of why these figures are healthier than a
pure-CPU estimate would suggest.

Three things follow.

**Decode is the binding cost, not prefill.** At 359 tok/s against 25.3, one
generated token costs about fourteen prompt tokens. The lever is how much the
model *writes*, not how much it reads. Every bound on `reasoning`, every
truncated observation, every terse summary is worth roughly fourteen times its
weight in context.

**Prefix reuse works**, so a frozen prefix is paid once per session rather than
once per turn:

```
first turn   ~=  prompt_tokens / 360  +  generated_tokens / 25
later turns  ~=  new_tokens_only / 360  +  generated_tokens / 25
```

**heretic is the faster model end to end**, which is counterintuitive and worth
stating. `qwen3-coder` prefills 30% faster, `heretic` decodes 30% faster, decode
dominates, and heretic finishes a six-turn task in about 50 seconds against 52
to 57. The chosen driver costs nothing in speed.

What it does cost is quality: 0/3 complete correct edits against the control's
2/3, on an identical task. §6.3 addresses the specific way it fails.

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

Revision 2 cut this budget from ~17.7k tokens to ~4k, on the belief that every
token in the prefix was re-ingested at 50 tok/s on every single turn. Prefix
reuse works and prefill runs at 359 tok/s, so that belief was wrong twice over
and the budget is restored.

| Slice | Budget | Cost, first turn only |
|---|---|---|
| System prompt and tool semantics | ~1k (measured: 410 for the prompt itself) | 3s |
| `AGENTS.md` | ~1k | 3s |
| `repomap.txt` | ~1.2k | 3s |
| Task index slice: signatures and summaries | ~2.5k | 7s |
| Function bodies in the blast radius | ~5k | 14s |
| History and observations | ~6k, truncated per tool | grows |
| **Total prompt** | **~16k** | **~45s, once** |

Forty-five seconds to load a session's context, paid on the first turn and then
served from cache, is a fair price for an index rich enough to stop the model
guessing. Q3, whether the repo map earns its 1,200 tokens, is now a question
about whether a weak model is confused by it rather than about seconds.

The rules that follow are not the ones revision 2 derived.

**The conversation is append-only, and this matters again.** Turn N's prompt
should be a strict prefix extension of turn N-1's, because the cache serves
extensions at 1.65 to 2.96 seconds rather than re-ingesting at 45. Revision 2
withdrew this rule on the strength of a broken measurement. It is reinstated.

**The prefix is frozen** for the duration of a task. Re-running retrieval
mid-task rewrites the prefix and throws the cache away, which costs the full 45
seconds again for a marginally better context.

**Compaction is explicit and rare.** It rewrites the prompt, so it forfeits the
cache and costs a full re-ingest. It happens on `/compact` or at a hard
threshold, never quietly every turn, and it is deterministic and
template-driven: files touched, edits applied and bounced with one-line reasons,
current goal. No LLM summarisation call, because generation is the expensive
resource here and bookkeeping is not worth 25 tok/s of it.

**The real lever is output length.** One generated token costs about fourteen
prompt tokens. Bound the `reasoning` field, truncate observations hard per tool,
cap summaries. That is where a turn's time actually goes, and it is the one
place where being stingy pays fourteen to one.

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

### 12.1 Measurement discipline

Written into the spec because ignoring it cost a whole revision.

**Timing findings are fragile; behavioural findings are not.** Every timing
number in revision 2 was wrong by five to eight times. Every behavioural
finding from the same session reproduced exactly: the model fabricating a file
it had not read, the schema enforcing `required` but not order, the tool
surface. When a measurement is going to drive an architectural decision, prefer
the behavioural one.

Rules for any number that enters this document:

1. **Warm the model first.** Ollama can fold model load time into
   `prompt_eval_duration`, which is what makes a healthy machine look ruined.
2. **Set `keep_alive` explicitly.** The five-minute default unloads the model
   between probes taken minutes apart, which is precisely how the above happens.
3. **Fresh content per trial.** Reusing a filler body silently measures the
   prefix cache instead of prefill.
4. **Three trials minimum, and report all of them.** A single sample of anything
   in this domain is worthless.
5. **Prefer end-to-end.** A six-turn wall-clock number is hard to fool. A
   tokens-per-second number is easy to fool and was fooled.
6. **Record the host state.** Reboots, GPU offload split, and what else was
   resident. The host rebooted mid-session and nobody noticed for an hour.

`scripts/bench-clean.py` implements 1 through 4. `scripts/serving-probe.py`
predates the lesson and does not; it is kept for the identity and schema checks,
which are behavioural.

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

Q1, Q4 and Q5 are closed. They are kept rather than deleted because how they
closed is the most useful thing in this document.

- **Q1: prefix-cache behaviour. CLOSED, yes.** Extensions are served in 1.65 to
  2.96 seconds against ~18 for a fresh prompt of the same size, 3/3. Revision 2
  claimed the opposite at length and was measuring an unwarmed model reloading.
- **Q2: overlay mechanism.** Still open. A `cp -al` hardlink forest is fragile
  if any tool writes in place; the likely answer is a per-edited-file copy with
  a shared `--target-dir`. Cheap to settle now that the gate is known to cost
  0.77s on the M0 target.
- **Q3: does the repo map help or hurt?** Still open, but now a question about
  comprehension rather than seconds: 1,200 tokens costs about 3 seconds, once.
  Aider's warning that weak models are confused by large maps is the live
  concern. Smoke-test at 0, 400 and 1,200 during M2.
- **Q4: heretic against a pure-attention control. CLOSED.** Not by argument but
  by decision: heretic is chosen for capability reasons that sit outside this
  document, and the measurements no longer contest it. It is *faster* end to end
  (49.9s against 52-57s on a six-turn task) because decode dominates and it
  decodes 30% quicker. It is worse on quality, 0/3 complete correct edits against
  2/3, and §6.3 exists to address the specific way it fails.
  `qwen3-coder:30b` stays installed as a **control**: when first-apply pass rate
  drops, re-running the same eval against a pure-attention, non-abliterated model
  of the same active size and quant is how you tell a model regression from a
  harness bug. It earned that role by doing exactly that job today.
- **Q5: is a hybrid SSM the wrong shape for an agent loop? CLOSED, no.** This
  was built entirely on the withdrawn no-reuse finding. Reuse works, the
  end-to-end six-turn number favours the hybrid, and the rule of thumb this once
  proposed ("prefer pure attention, grep the GGUF for `ssm.`") should be
  disregarded. It was a good rule about a fact that was not true.
- **Q6: does the §6.3 stall rule close the quality gap?** New, and the one that
  matters. The driver's failure is repetition rather than fabrication, so a
  deterministic refusal ought to convert a wasted turn into a corrected one.
  Untested. M0 answers it.
