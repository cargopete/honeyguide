# RFC-0001: Honeyguide, an asymmetric-intelligence coding agent

| | |
|---|---|
| Status | draft |
| Author | Pete / Nixum |
| Revision | 4 (2026-08-13) |
| Depends on | `docs/research/2026-08-local-model-tui-coding-agent.md` |
| Evidence | `docs/measurements/2026-08-13-m0-suite.md`, `docs/measurements/2026-08-12b-clean-rerun.md` |
| Target | v0.1 (index + local loop), v0.2 (escalation) |

## Revision note

**Revision 4 exists because M0 was run in full and the compile gate, as
specified, was the thing holding the model back.** §6.2 said an edit that fails
`cargo check` is reverted. That makes every change with no compiling intermediate
state unreachable — adding a struct field breaks the literals that construct it,
so there is no single edit from green to green — and the model can never climb
out, because each turn the harness undoes what it just did. Measured: eight
edits, every one matching the file uniquely, none fabricated, all eight thrown
away, and a first-apply rate of 0/8 that reads exactly like a model which cannot
write Rust.

Edits now accumulate and the harness owns the retreat (§6.2). Five further
deterministic rules came out of the same suite (§6.3), one of which — refusing an
`edit` whose `replace` equals its `search` — was correcting the headline metric
rather than the model's behaviour. §12 accordingly demotes FACP to a diagnostic
and makes the oracle completion rate the quality number. Q6 is closed, and Q7 is
new and is now the question the project turns on: **neither model completed a
single multi-site cascading edit, in twelve attempts.**

Revision 1 assumed Qwen3-Coder-30B-A3B on a local Ryzen box with llama-server
and GBNF. Revision 2 replaced that with measurements of the real target.
**Revision 3 existed because most of revision 2's measurements were wrong.**

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

1. At session start, copy the working tree to an overlay (`cp -Rc`, falling back
   to `cp -R`), sharing one `--target-dir` with the working tree. Q2, closed:
   0.03s to build, 0.29s per gate once warm, and no fingerprint thrashing.
2. `edit` applies to the overlay, then `cargo check --workspace --all-targets
   --message-format json` runs.
3. **On red, the edit stays.** Emit `EditBounced`, feed the rustc errors back
   verbatim as the observation, and increment the repair count. The overlay is
   now red and the harness knows it.
4. **After three red gates without the error count falling, the harness rolls
   the overlay back** to the last state that compiled and says so. The trigger is
   lack of progress, not elapsed redness. The bookkeeping is the harness's, not
   the model's.
5. On green, emit `EditProposed` with the diff against the working tree. The TUI
   offers approve or reject; auto-approve is configurable. On approval, replay
   onto the working tree with a fresh unique-match check against the real file,
   which guards against an external edit landing in between.
6. `finish` is refused while the overlay is red (§6.3).

Measured against the M0 target: a real change in `dipper`'s core crate
cascades to all four dependent crates and `cargo check --workspace` completes in
**0.77s** on a warm target directory (21.7s cold, 0.30s with nothing changed).
A six-turn model loop on the same task takes about fifty seconds.

The gate is therefore both the primary quality mechanism and, by roughly two
orders of magnitude, the cheapest thing in the loop. Everything else is context
plumbing.

**Revision 4, and it is a correction rather than a refinement.** Rule 3 used to
read "on failure, revert the edit". M0 showed that this makes a whole class of
correct changes unreachable, and the class is not an exotic one: it is every
change with no compiling intermediate state.

The control model was asked to add a field to `Record` and fix the literals that
break. Adding the field is what breaks them, so there is no single edit that
takes the file from green to green. It proposed eight edits. All eight matched
the file uniquely, so not one of them was fabricated. All eight were reverted.
Twelve turns, four minutes, and the file ended exactly as it started. The model
wrote a correct first edit on turn one and the harness threw it away, then threw
away its next seven attempts to repair the consequences.

A gate that reverts on red does not accept correct changes. It accepts
*individually compilable* changes, which is a strictly smaller set, and it is a
ratchet the model cannot climb because every rung is red by construction. Worse,
the failure is invisible in the metric: FACP reads 0/8 and looks exactly like a
model that cannot write Rust.

So the gate keeps the edit and reports the errors, which was always the useful
half, and the harness owns the retreat. The model is never asked to remember how
to undo anything, which is the same principle as §6.3's provision rule. A red
overlay is a state the harness tracks, not a mistake it punishes.

**What the retreat triggers on took two attempts.** The rule was first written as
three consecutive red gates, and that is wrong for the same reason revert-on-red
was wrong: a cascading change is red on every intermediate turn by construction.
The rename task needs four edits (the method, `dipper-cli`, and two test callers)
and is therefore red three times running on its way to green. The rollback fired
on the turn before success and threw away a correct rename. Measured, not
reasoned: it happened on the first trial of the first run.

The trigger is instead the rustc error count, taken from the diagnostics the gate
already has. Falling means progress and resets the counter; three turns without
it falling means the model is going nowhere and the harness restores the last
green state. On the rename task the count runs 3, 2, 1, 0 as the callers are
fixed, so a model doing the work correctly is never interrupted, while one
flailing is stopped after three turns. The count is also worth handing back:
"2 errors, down from 3" is a progress signal the model gets for nothing.

`--all-targets` is part of the same correction, and it is worth setting down how
it was established, because the first version of this paragraph was wrong.

Plain `cargo check` does not typecheck `#[cfg(test)]` code. Measured directly: a
type error planted inside a `#[cfg(test)]` module in `dipper-index` gives
`cargo check --workspace` an exit code of **0**, and `--all-targets` an exit code
of **101**. So the gate as originally specified can pass an edit that the
`cargo test` oracle then fails, which is the worst ordering available, since the
harness would report a green gate on a broken crate.

The claim originally offered as evidence was that M0's rename task demonstrated
this, its callers living in a `mod tests` in the edited file. That was not true.
The rename was caught by plain `cargo check`, because `dipper-cli` also calls
`Catalogue::count` and the workspace build sees it. The general point stands and
is now measured on its own terms; the example did not, and repeating §12.1, a
behavioural claim is worth exactly as much as the probe that established it.

Cost, three trials each on a warm target directory: **0.11s** with nothing
changed, **0.31s** after a change to the core crate that compiles and cascades to
all four dependents. Both are cheaper than the 0.77s recorded earlier for plain
`cargo check`, which is a difference in what was changed rather than a saving
from `--all-targets`. And a red gate is cheaper still, because compilation stops
at the first error: an edit that breaks its own caller comes back in 0.20s.
Widening the gate to see test code costs nothing worth counting.

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
| identical `read` repeated | refuse | **serve the next unseen window** of the file; refuse only once the whole file has been shown |
| missing required arguments | refuse, naming them | unchanged; nothing to satisfy |

With that change plus §5.3 pre-loading, the same task went from twelve turns and
no edit to five turns and a clean first-apply pass.

Three further rules came out of the full five-task M0 suite, and each one is a
failure the harness can settle without asking the model anything.

- **`finish` is a claim, and the harness can check it.** The control model
  searched, read, searched, read, ran `check`, and declared the task complete
  having made no edit at all. Every action was well-formed, permitted and
  non-repeating; nothing above catches it, because nothing about it was wrong
  except that it was untrue. A `finish` is therefore refused when no edit has
  been applied in the session, and refused while the overlay is red (§6.2). Both
  are facts the harness already holds.
- **A green gate must say what happens next.** Told only "edit applied and
  `cargo check` passed", both models re-sent the identical edit until the stall
  rule aborted them. A bare success reads as no signal at all. The observation
  now says the edit has landed, not to send it again, and to emit `finish` if
  the task is done.
- **An edit whose `replace` equals its `search` is not an edit.** This one was
  corrupting the headline metric rather than merely wasting turns. The model
  emits the two fields byte-identical; the harness applies the change, and
  `cargo check` passes because nothing has changed, so FACP records a
  first-apply pass for an edit that did nothing. Two of the five tasks in one
  trial "passed the gate" this way and failed their oracles. The model's
  response was reasonable and the harness's was not: it re-sent the same no-op,
  because it could see the task was not done while the harness kept insisting
  the edit had landed. Refused before the gate, at a cost of one string
  comparison.
- **The stall check must run before the argument check, not after.** With the
  order reversed, a malformed action never updated the last-action record, so a
  model repeating the *same* malformed action was never seen to be repeating.
  The driver sent an identical argument-less `edit` twelve times and collected
  twelve separate refusals, one per turn, to the cap. A rule that only runs on
  well-formed input is not a rule against stalling. Reordered, the same failure
  costs four turns instead of twelve.
- **The stall signature must be scoped to the fields the tool uses.** Because
  the action schema requires every string field on every action (§6.1 and
  `prompts/README.md`), a `check` carries leftover `search` text that drifts
  turn to turn, and a signature computed over all fields quietly stops matching.
  Two consecutive identical `check`s were served before the rule fired. The
  signature covers `(tool, path, search, replace)` for an `edit`, `(tool,
  query)` for a `search`, `(tool, path, start, end)` for a `read`, and the tool
  alone for `check` and `finish`. It is also compared against the **last four**
  actions rather than only the previous one: refused once, the model alternates
  between two near-identical malformed forms, and A-B-A-B defeats a detector
  with a memory of one. Observed over turns 8 to 12 of a rename attempt.

The pattern across all three is the same one §6.2 arrived at independently:
these are not judgement calls the model has to get right. They are bookkeeping,
and bookkeeping belongs in the harness.

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
- **FACP**, first-apply `cargo check` pass rate. Gated at 60% for M0, and less
  trustworthy than it looks. Two things have to be said about it, both learned
  from M0 rather than reasoned:

  Its denominator must be **edits proposed**, not edits that happened to match
  the file. Counting only the ones that matched excuses a model that writes
  `search` text it never read, which is the first failure this project ever
  observed.

  And it is **necessary, not sufficient, and by a wide margin.** In one M0 trial
  four of five tasks had an edit pass the gate and one of five passed its
  oracle. A compiling change is not a correct change, and a metric derived from
  the compiler cannot tell the difference. **The oracle rate is the real quality
  number.** FACP is a diagnostic: when it and the oracle rate diverge, the model
  is writing plausible Rust that does the wrong thing, and when they move
  together the harness is doing its job.

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

- **M0, thesis spike. RUN. G2 failed, the turn-time gate passed.** A throwaway
  script, not a product. Three tools, schema constraint, a hand-written index for
  one crate, five smoke tests. Two gates, not one: FACP at 60% or better, **and**
  a median turn under 120 seconds on a realistic multi-turn task. The second gate
  is the one §8.3 put there, and a tool that is correct but takes four minutes a
  turn has failed just as surely as one that is fast and wrong. Below either
  gate, pull the model A/B (Q4) forward before writing any more harness.

  **Result**, three suites per model, full record in
  [`docs/measurements/2026-08-13-m0-suite.md`](../measurements/2026-08-13-m0-suite.md):

  | | driver | control |
  |---|---|---|
  | FACP, of edits proposed | 25% | 33% |
  | Tasks actually completed | 6/15 | 9/15 |
  | Median turn | 8.7s | 16.6s |

  G2 fails for both models on either denominator. The turn-time gate passes by a
  factor of fourteen. The A/B that §13 prescribes on a failed gate was run, and
  its answer is that the model is not the variable: both models scored **0/3 on
  both cascading tasks** and the difference between them is confined to how
  reliably they do the single-site ones. So the instruction to reconsider the
  model is satisfied and does not point at a model change. The binding
  constraint is Q7, and it is the harness's.

  **A metric proposal, made after seeing the data and flagged as such.** G2
  should be the oracle completion rate, not FACP. In one trial four of five
  tasks had an edit pass the compile gate and one of five actually did the job,
  and no metric derived from the compiler can tell a compiling change from a
  correct one (§12). This has not been applied retroactively: the verdict above
  is against the gate as written.
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
- **Q2: overlay mechanism. CLOSED.** A whole-tree copy, `cp -Rc` where the
  filesystem supports copy-on-write and plain `cp -R` otherwise, sharing one
  `--target-dir` with the working tree. `scripts/overlay-probe.py` measured the
  four candidates against the M0 target.

  **`cp -al` is disqualified, and behaviourally rather than by argument.** The
  probe writes into the overlay the way a naive tool writes — open, truncate,
  write — and then checks the working tree. Under a hardlink forest the working
  tree changed. That is the silent-corruption failure this design exists to
  refuse, so it is not a trade-off to be weighed against speed.

  **Build cost is a non-issue and the per-edited-file scheme is unnecessary.**
  All four mechanisms build in **0.03s**, because a 14.7k-line workspace is 2 MB
  of source. Copying the lot is simpler than tracking which files have been
  touched, and simpler wins when the complicated version buys nothing.

  **A shared target directory does not thrash, which was the real question.**
  Cargo fingerprints record absolute paths, so alternating between two source
  trees might have invalidated everything on every switch and turned a 0.3s gate
  into a 13s one. It does not: cargo keys its units by package path and the two
  trees' artifacts coexist. With the overlay diverged from the working tree and
  a **fresh** edit each round, alternating checks run **0.29s** in the overlay
  and **0.12s** in the working tree, repeatedly. The overlay's first check costs
  0.5 to 1.3s; a cold target directory of its own would have cost **13.2s**.

  A `git worktree` also passes the aliasing check and gives a free diff, and is
  rejected for a reason that has nothing to do with performance: it materialises
  `HEAD`, not the working tree, so a session started with uncommitted changes
  would silently work against different code from the one the user is looking at.
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
- **Q6: does the §6.3 stall rule close the quality gap? CLOSED, no, and the
  question was aimed at the wrong gap.** Measured over three full suites per
  model. The rule fires constantly and does its narrower job: 29 stall refusals
  for the driver, and `fix_test` went from four turns to two once a green gate
  said what to do next. What it does *not* do is convert a wasted turn into a
  corrected one. Twelve of the driver's fifteen tasks still ended in a stall
  abort. The rule bounds the cost of stalling; it does not turn stalling into
  progress, and the driver still finished 6/15 against the control's 9/15.

  The premise was also wrong in a way worth recording. The gap the rule was
  meant to close is not where the failures are. Every failure in the suite, for
  **both** models, was one of the two tasks needing edits in more than one
  place: 0/3 and 0/3 each. On single-site tasks the driver runs 2/3 and the
  control 3/3. So the driver's deficit there is variance rather than
  competence, which §6.3 does attack, and the headline failure is Q7's, which
  no §6.3 rule touches.
- **Q7: can a five-tool loop complete a cascading multi-site edit at all?** New,
  and now the one that matters. Zero completions in twelve attempts across two
  models, on `rename` (a method plus its callers in two other files) and
  `add_field` (a field plus the literals it breaks). The revised gate makes such
  a change *reachable* — the error count visibly falls, 3 to 2 to 1 — but
  neither model closes it inside twelve turns.

  Three candidate answers, in increasing order of how much they concede. Raise
  the turn cap, which is cheap and may simply be the answer, since one trial
  reached the cap with one error left. Have the harness present one rustc error
  at a time as a sub-task, with its file, line and surrounding lines, which is
  §6.3's provision principle applied to repair. Or have the harness perform
  mechanical multi-site refactors itself out of `scip.sqlite`, which already
  holds every reference, and leave the model the single semantic decision. The
  third is the thesis of this document taken to its conclusion, and it should be
  reached last and on evidence, not first and on enthusiasm.
