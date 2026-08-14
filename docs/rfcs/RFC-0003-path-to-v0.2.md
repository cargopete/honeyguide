# RFC-0003: The path to v0.2

| | |
|---|---|
| Status | draft |
| Author | Pete / Nixum |
| Revision | 1 (2026-08-13) |
| Depends on | RFC-0001 (the design), RFC-0002 (the strong-model boundary) |
| Evidence | `docs/measurements/2026-08-13-m0-suite.md` |
| Target | v0.1 (single-site edits, done properly), v0.2 (cascading edits and escalation) |

## 1. Summary

M0 has been run in full: five tasks, three trials, two models, 188 turns. It
failed its quality gate. This document is about what to do with that, and its
argument is one sentence long:

**Every improvement measured today came from the harness doing more, not from the
model doing better.**

The model was constant across the whole session. `fix_test` halved, from four
turns to two, because a green gate began saying what to do next. `add_field`
became reachable at all because the gate stopped reverting failed edits. Nothing
about the model changed; six deterministic rules changed, and the numbers moved.

RFC-0001 states that thesis and then stops short of it. It gives the model a map
and a compile gate, and leaves every act of editing to the model, including acts
that are purely clerical and that the harness can perform with certainty. That is
where the failures are. This RFC proposes five changes, in the order they should
be attempted, and none of them involves choosing a different model.

The headline target: **cascading multi-site edits currently complete 0 times in
12 attempts across two models.** Nothing else in the suite fails. Fixing that one
category is the difference between a demo and a tool.

## 2. What M0 established

Restated compactly, because everything below rests on it. Full method and figures
in the measurement record.

| | driver | control |
|---|---|---|
| Well-formed action rate | 90/90 | 98/98 |
| Tasks completed (oracle) | 6/15 | 9/15 |
| `rename`, `add_field` | **0/3, 0/3** | **0/3, 0/3** |
| `fix_test`, `display_impl`, `doc_comment` | 2/3 each | 3/3 each |
| Median turn | 8.7s | 16.6s |
| Tasks ending in a stall abort | 12/15 | — |

Five facts carry the argument.

**F1. The failure is a cliff, not a slope.** Every failure in the suite is one of
the two tasks requiring edits in more than one place. Single-site tasks run at
67% and 100%.

**F2. It is not the model.** Both models fail the same two tasks identically,
including a purpose-built coding model that is perfect on everything else.

**F3. The driver's remaining deficit is variance, not capability.** It completes
each single-site task two times in three; the control completes each three times
in three, identically on every trial. Same inputs, same temperature.

**F4. Stalling is the mechanism.** Twelve of fifteen driver tasks ended in a
stall abort, with 29 stall refusals across the suite. It is not fabricating and
it is not emitting malformed actions: WFA was 100% over 188 turns.

**F5. Speed is not the constraint and neither is the gate.** Median turn 8.7s
against a 120s allowance. The compile gate costs 0.29s in the overlay (Q2), which
is under 4% of a turn.

The corollary of F5 deserves stating on its own: **the harness has an enormous
latency budget it is not spending.** Anything deterministic the harness can do in
under a second is, on this hardware, free. The proposals below spend that budget.

## 3. Mechanical propagation: the harness finishes the edit

The highest-value change available, and the one that turns F1 and F2 from a
failure into a solved case.

### 3.1 What the failing tasks actually require

`rename` asks for `Catalogue::count` to become `Catalogue::matches`. Doing it
requires four edits: the definition, one call in `dipper-cli`, and two in the
crate's own `mod tests`. The gate reports exactly three errors after the first
edit and they fall 3 → 2 → 1 → 0 as the call sites are fixed, so the model can
see its progress and the revised gate (§6.2) keeps the work.

Neither model completed it, in six attempts.

Now look at what those three remaining edits are: the identical textual
substitution, `count` for `matches`, at three locations that `scip.sqlite`
already knows precisely, because §5.1 has the harness build a reference table for
exactly this kind of question. There is no judgement in them. They are clerical.

**We are asking a 3B-active model to do filing.** It is bad at filing. The
harness is perfect at it, at a cost of milliseconds, and the whole premise of the
project is that this asymmetry should be exploited rather than endured.

### 3.2 Mechanism

When an `edit` is applied, the harness asks whether it was a **pure rename of a
definition**. It is, if and only if all of these hold:

1. `search` and `replace` differ in exactly one identifier token, and are
   otherwise byte-identical.
2. That token appears in the edited region as a **definition**, per the index's
   symbol table for that file and range.
3. The new identifier does not already resolve to a symbol in scope.

Any of those failing means it is an ordinary edit and nothing else happens. The
test is conservative on purpose: a false negative costs nothing, and a false
positive edits code the model did not ask to edit.

When they hold, the harness looks up every reference to the old symbol in
`callgraph.jsonl` and `scip.sqlite`, and at each recorded site:

- reads the current on-disk text at that location,
- requires the old identifier to appear there **exactly once**, as a whole token,
- applies the substitution, or **skips the site and records why**.

Then the gate runs once over the whole batch, and the result is reported as one
change. Sites that were skipped are named in the observation, because a partial
propagation the model does not know about is worse than none.

### 3.2a Measured: a lexical reference set is not merely approximate, it is wrong

Written after prototyping §3.2 with ripgrep as a stand-in for `scip.sqlite`, on
the theory that a whole-token search would be a rough version of the right
answer. It is not a rough version of anything.

Renaming `Catalogue::count` to `Catalogue::matches` on `dipper`, the lexical
propagation rewrote **84 sites across 15 files**. `picker.rs` took 30 of them,
`wire.rs` 16, `search.rs` 11. Not one was a reference to the renamed method. They
were `Iterator::count()`, struct fields named `count`, and local bindings named
`count`, in crates that do not depend on `dipper-index` at all.

The reason is not a tuning failure and no threshold fixes it. A token search
cannot distinguish a method on one type from an identically named method on
another, and common Rust identifiers — `count`, `len`, `get`, `new`, `id` — are
exactly the ones people rename. Applying an edit to a site that merely looks
right is fuzzy application, which RFC-0001 §7 forbids by name and for precisely
this consequence: a harness silently corrupting files.

**So §3 is blocked on real reference data, and the ordering in §8 changes
accordingly.** Everything else in this section survives unaltered — the detection
test, the on-disk verification, the gate, the skip-and-report behaviour — because
only the source of the reference set was wrong. `scip.sqlite` answers "every
reference to *this* symbol", which is the question being asked, and at that point
`propagate_rename` becomes correct without changing.

Until then it is behind `--propagate`, off by default, documented as unsafe. The
detection half is unit-tested at 10/10 including the cases §9 asked for, and the
string and comment masking is exact, so the machinery is ready for a reference
set that deserves it.

### 3.2b Measured: SCIP gives the reference set exactly

`crates/hg-index/src/scip_ingest.rs` now parses a real `index.scip`.
`rust-analyzer scip` takes **22.1s** on the M0 target and produces 2.7 MB,
**2,815 symbols and 28,807 occurrences**. Probed with
`cargo run -p hg-index --example scip_probe`:

| | lexical (§3.2a) | SCIP |
|---|---|---|
| Sites rewritten renaming `Catalogue::count` | **84, in 15 files** | **3** |

The three are `dipper-cli/src/main.rs:518` and lines 364 and 365 of the edited
crate's own test module. **Those are exactly the three rustc errors** the gate
reported when the rename was attempted in M0. The reference set and the compiler
agree site for site, which is the cross-check worth having: two independent
oracles, same answer.

The index also shows why the lexical version could not have worked. Six distinct
symbols in this workspace are named `count`: `Catalogue`'s, `SearchQuery`'s two,
`Bitfield`'s, `Picker`'s, and `Iterator::count` from `core`. A token search sees
one word. SCIP sees six symbols and is asked about one.

Two bugs surfaced by probing real output rather than reasoning about it, both
worth recording because both would have produced confident wrong answers:

- rust-analyzer writes an inherent method as `impl#[Catalogue]count().`, with
  the receiver bracketed. The name parser, written from memory of the schema,
  returned nothing for that form, so the most common kind of symbol in a Rust
  codebase was missing from the by-name map entirely.
- A definition lookup keyed on file and line alone is **non-deterministic**.
  `pub fn count(&self, query: &str)` defines the method *and* the parameter on
  one line, and the first version walked a `HashMap` and took whichever it met
  first. Probed, it returned `local 25` — the parameter — in place of the
  method. Propagation would have renamed an arbitrary symbol. The lookup now
  requires a column and prefers a global symbol over a local.

§3 is therefore unblocked in principle: the reference set is exact, the cost is
one 22-second index per session, and `propagate_rename` needs only to be handed
SCIP occurrences instead of grep hits.

### 3.2c Measured: propagation through SCIP is exact, and staleness is real

`hg-scip-refs` resolves a symbol by position and prints its reference sites;
`propagate_via_scip` in the spike applies the rename at each, verifying the text
on disk before touching it. Run against `dipper`, renaming `Catalogue::count`:

```
changed: crates/dipper-cli/src/main.rs (1), crates/dipper-index/src/lib.rs (2)
skipped: none
```

Four lines: the definition, the caller in `dipper-cli`, and both callers in the
crate's own `mod tests`. `picker.rs`, `wire.rs` and `search.rs` — three of the
fifteen files the lexical version corrupted — are byte-identical. The task that
neither model completed in twelve attempts is now one edit followed by a
deterministic substitution.

**The first attempt skipped two of the three sites, and that is the more
valuable result.** The index had been generated while the working tree carried a
stale two-line duplication, so every line number past 181 was off by two.
Verification found the text was not where the index said, and skipped rather
than guessed:

```
skipped: crates/dipper-index/src/lib.rs:365 (text has moved)
         crates/dipper-index/src/lib.rs:364 (text has moved)
```

That is §5.2 working exactly as written — the index is a map, never ground truth
for file contents — and it was demonstrated by accident rather than by a test,
which is the most convincing way to learn it. A propagation built on trust in
the index would have renamed two arbitrary five-character spans. The consequence
for the design is that **the verification step is not belt-and-braces, it is
load-bearing**, and refresh (§5.2) must run whenever the overlay diverges enough
to move lines.

Cost note: a warm re-index took **5.2s** against 22.1s cold, so a per-session
index is cheaper than the first measurement suggested, and an incremental
refresh cheaper still.

### 3.3 Why this is safe

Three defences, and they are the same three the design already relies on.

**The index is never ground truth for file contents.** §5.2 says so already. Each
site is verified textually against disk before it is touched, and a mismatch is a
skip, not a guess. A stale index makes propagation incomplete, never wrong.

**The gate still decides.** Propagation proposes; `cargo check --workspace
--all-targets` disposes. A propagation that breaks the build is a red gate like
any other, and §6.2's error-count rollback covers it.

**Unique-match discipline is unchanged.** §7 forbids fuzzy application, and this
does not weaken it: every site requires exactly one whole-token match or is
skipped.

### 3.4 What it costs and what it buys

Cost: a symbol lookup and a handful of file reads, comfortably inside the 0.29s
the gate already spends. No model tokens whatsoever.

Buys: `rename` collapses from a four-edit task that neither model can complete
into a one-edit task of the kind both models do reliably. If F1 holds, that is
6/15 to 8/15 for the driver and 9/15 to 11/15 for the control, from a change that
never consults a model.

### 3.5 The general form, and its limit

The generalisation is **compiler-directed mechanical repair**: a class of edits
where the compiler names the file, the line and the required change, and no
judgement is involved. Renames are the clean case. `add_field` is the instructive
one: `error[E0063]: missing field `language` in initializer` names the site
exactly, but the *value* to insert is a choice, and `None` is only the obvious
answer because the field is an `Option`.

So the boundary this RFC draws is: **the harness performs substitutions it can
derive with certainty, and asks the model for anything requiring a value.** For
`add_field` that means the harness can offer the model each broken initializer in
turn with the missing field named, which is §6.3's provision principle applied to
repair, but it must not invent the value. Renames first; the rest on evidence.

## 4. Resample on a stall, do not only refuse

The cheapest change on the list, and it attacks F4 directly.

### 4.1 The problem with refusing

§6.3's stall rule fires constantly and does its narrower job: it stops the model
burning the turn cap, and after three repeats it aborts. But M0 measured what it
does *not* do. Twelve of fifteen tasks ended in that abort. The rule converts a
wasted task into a shorter wasted task.

The reason is visible once stated. Every turn is generated at temperature 0.3
from a prompt that grows by one observation. When the model produces a bad
action, the harness appends a refusal and asks again — under conditions almost
identical to the ones that just produced the bad action, at a temperature chosen
to suppress variety. We are re-rolling a loaded die and hoping.

### 4.2 Mechanism

When the stall rule fires, before emitting the refusal, **regenerate the same
turn at a higher temperature**. A ladder of 0.3 → 0.7 → 1.0, at most two
re-rolls, then fall through to the refusal as now.

This is cheap in a way specific to this workload. §8.3 established that the
prefix cache works and that an extension of an existing prompt is served in 1.65
to 2.96 seconds against ~18 for a fresh one. A re-roll changes nothing about the
prefix, so it pays cached prefill plus roughly 80 output tokens: on the measured
25.3 tok/s decode, about three seconds. Against a median turn of 8.7s and a
budget of 120s, two re-rolls are free.

It also composes with the existing rules rather than replacing them: a re-roll
that produces the same action still gets refused, and three of those still abort.

### 4.3 How we will know

The metric already exists. Stall aborts per suite, currently 12/15 for the
driver. If resampling does not move that number, it is wrong and should be
removed rather than kept because it is clever.

## 5. Escalation, brought forward from v0.2

RFC-0001 §9 places escalation in v0.2 and §13 designs it from "at least two weeks
of v0.1 session logs". The instinct is right and the schedule is now wrong.

### 5.1 Why the schedule changes

The argument for waiting was that a feature designed before its failure data
exists is guesswork. That was true when it was written. It is not true now: M0
produced full JSONL transcripts of 188 turns across two models, and the failure
data is not ambiguous. Twelve stall aborts, three no-op edits, nine
`search_not_found` refusals, and every one of them recorded with the exact action
and observation that produced it.

We do not need two weeks of logs to know what gets stuck. We know.

### 5.2 Scope: one edit, not one task

The important design decision, and the one that keeps this affordable.

Escalation does **not** hand the task to the strong model. It hands over a single
stuck edit, with:

- the task statement,
- the failing `search`/`replace` the local model proposed,
- the rustc diagnostics from the red gate,
- the relevant file region from disk,

and asks for one search/replace block back. The local model remains the driver.
The strong model is a subroutine for the specific thing the local model cannot
do, which is exactly the shape RFC-0002's boundary was built for.

This matters because of RFC-0002 §2: cost scales with `turns x context`, not with
answer length. A one-turn request carrying a few hundred lines is the cheap corner
of that product. Handing over the whole task would be the expensive one.

### 5.3 Triggers, deterministic as §9 requires

No model-initiated escalation, per §14. The triggers are facts the harness
already holds:

| Trigger | Rationale |
|---|---|
| Stall abort would fire | The measured dominant failure, 12/15 |
| Error-count rollback would fire (§6.2) | Progress has demonstrably stopped |
| Turn cap reached with the gate red | The work is unfinished and time is up |

Budget: one escalation per task by default, configurable, and every escalation
recorded in the event log with its trigger. If the local model never gets a task
over the line and escalation carries all of them, that is a finding about the
design and the telemetry should make it impossible to miss.

### 5.4 Transport

Already specified. RFC-0002 §3: one persistent `claude -p` process per session
over `--input-format stream-json`, never one per escalation, because each spawn
costs 4 seconds and a 33,000-token preamble. Containment per RFC-0002 §4: the
escalation process gets `Read Grep Glob` and no write capability, and hg applies
every edit itself through the ordinary gate.

An escalated edit is not privileged. It goes through unique-match verification and
`cargo check` exactly as a local edit does. The strong model is better, not
trusted.

## 6. Raise the turn cap and measure — DONE, and the answer is no

Measured: three trials, driver, cap 24 against the cap-12 baseline.

| | cap 12 | cap 24 |
|---|---|---|
| Tasks completed | 6/15 | **8/15** |
| `rename` | 0/3 | **1/3** |
| FACP, of edits proposed | 6/24 | 8/33 |
| Median turn | 8.7s | 8.8s |
| Total wall | 949s | **1,260s** |

The headline moved and **the cap did not cause it**, which is why §8's kill
criterion is written against the mechanism rather than the number.

Fourteen of the fifteen runs finished inside the old twelve-turn cap. The one
run that used more, `add_field` at 21 turns, failed anyway. And the `rename` that
succeeded took **11 turns**, so it was reachable at cap 12 and simply did not
happen on those three trials. The extra headroom was used by exactly one task and
that task failed.

So the two extra completions are trial-to-trial variance, of which there is
plenty: the driver's per-trial scores are 3, 1, 2 at cap 12 and 2, 3, 3 at cap
24, on identical inputs. Raising the cap costs 33% more wall-clock (949s to
1,260s) and buys nothing demonstrable. **The cap stays at 12.**

One genuinely new fact came out of it, and it is not about the cap. `rename`
completed **once**, which is the first cascading multi-site edit this project has
ever seen finish, in 21 attempts across two models and two cap settings. It is
rare rather than impossible, and §3 is about making it reliable.

## 6a. The original argument, kept

The smallest item here, listed because it is nearly free and might quietly be
part of the answer.

M0 ran with a 12-turn cap, chosen by the spike rather than specified anywhere.
One `rename` trial reached that cap having gone from three errors to one, with
the work visibly still progressing and the gate keeping it. That is not a model
failing; that is a stopwatch.

At 8.7s a turn, 24 turns is three and a half minutes, which is well inside any
plausible interactive budget and an eighth of the 120s-per-turn gate. Raise the
cap to 24, re-run the suite, and report both. If it converts nothing, that is
itself useful: it means the cap was never the constraint and the cascading failure
is structural, which strengthens the case for §3.

## 7. Scope v0.1 honestly

Not a change to the code; a change to what is claimed.

On single-site edits behind a compile gate, this thing works. The control
completes all three such tasks on all three trials; the driver completes each two
times in three, and §4 exists to close that. A tool that reliably makes small,
verified, compiling edits to a Rust workspace, driven by a model on a machine in
the next room, is genuinely useful and is nearly in hand.

So v0.1 claims that and only that. Cascading refactors are v0.2, gated on §3 and
§5 landing and on the suite showing it. The README should say so plainly, in the
same register it currently uses to say there is no working agent, because the
project's credibility rests on that habit more than on any feature.

## 8. Ordering, and how we will know

Strictly in this order, because each is cheaper than the next and the cheap ones
may reduce what the expensive ones have to do.

| | Change | Effort | Expected effect | Kill criterion |
|---|---|---|---|---|
| 1 | Turn cap 12 → 24 (§6) | minutes | unknown, possibly nil | no change in oracle rate |
| 2 | Resample on stall (§4) | hours | stall aborts 12/15 down | stall aborts unmoved |
| 3 | **`hg-index` structural pass** | days | none directly; unblocks 4 | SCIP references unusable |
| 4 | Mechanical propagation (§3) | days | `rename` 0/3 → passing | `rename` still 0/3 |
| 5 | Escalation (§5) | days | the residue, whatever it is | escalation carries everything |

Item 3 was not in the first draft of this table. It is here because §3.2a
measured what a lexical reference set does and the answer was 84 wrong edits in
15 files. Propagation cannot be built before the index that makes it correct, so
the structural pass — `rust-analyzer scip` ingest, symbols, references — moves
from "M1, later" to "prerequisite". It needs nothing from the semantic pass,
which is just as well, since Q4 says the semantic pass has no evidence behind it
yet.

Each lands with the same instrument: the five-task suite, three trials, both
models, oracle rate as the headline (§12), FACP reported alongside as the
diagnostic it turned out to be.

**The v0.1 bar**: single-site tasks at 3/3 for both models, three trials running.
The control is already there; the driver is at 2/3 and §4 is the attempt on it.

**The v0.2 bar**: `rename` and `add_field` completing at all, from 0/12.

## 9. Risks

**Mechanical propagation edits code the user did not ask to change.** The most
serious risk in this document, because it is the one that could corrupt a
repository. Mitigated by the conservative detection test (§3.2), by textual
verification at every site, by skipping rather than guessing, and by the gate. The
detection test should be unit-tested against deliberately awkward cases — a
rename that also changes the body, a name that shadows, a name that appears in a
string or comment — before it is allowed near a real repository.

**Resampling masks a prompt problem.** If the model stalls because the system
prompt or the observation format invites it, re-rolling treats a symptom. The
transcripts are in hand and should be read for that before §4 is called a
success.

**Escalation becomes the product.** If the strong model carries every task, this
is not an asymmetric-intelligence agent; it is Claude Code with extra steps and a
worse interface. The telemetry in §5.3 exists to catch that early, and the honest
response would be to say so.

**All of this rests on one repository.** Every figure in M0 comes from `dipper`,
one 14.7k-line workspace, with one 413-line file as the target of all five tasks.
The cliff in F1 might be a property of that file rather than of cascading edits in
general. A second target crate before v0.2 would settle it, and §12's smoke suite
already anticipates "two or three pinned small Rust crates".

## 10. Alternatives considered

**Change the model.** §13 prescribes exactly this on a failed gate, and the A/B
was run. Both models fail the same two tasks identically and the control is twice
the wall-clock, so the model is not the variable. Q4 settles the driver choice on
grounds outside these documents in any case.

**Give the model more tools.** A `rename` tool, a `multi_edit` tool, a shell. §6.1
caps the surface at five for measured reasons, and the M0 evidence is that the
model does not fail for lack of verbs. It fails at clerical execution and at
knowing when to stop, neither of which a sixth tool improves.

**Two-phase generation.** Already specified in §6 and available behind
`--phases 2`, untested in this suite. Worth measuring, but it addresses reasoning
quality, and F4 says the failure is repetition rather than bad reasoning.

**Let the model batch several edits in one action.** Tempting for cascading
changes, and rejected: it multiplies the size of the largest thing the model must
emit correctly, and §8.3's first-ever request ran a single unbounded string to the
token cap. The bounded action schema is load-bearing.

## 11. Open questions

- **Q1: does mechanical propagation generalise past renames?** The
  compiler-directed repair class is clearly larger than renames and clearly
  smaller than "all edits". Where the line sits is unknown and should be found by
  measurement, one error code at a time.
- **Q2: is the cliff real or is it `dipper-index/src/lib.rs`?** See §9. Needs a
  second target crate.
- **Q3: what actually causes the argument-less `edit`?** It appears across both
  models and both prompt wordings, costs a turn each time, and has no known
  trigger. A prompt-length explanation was proposed and tested and did not
  survive.
- **Q4: does the project brief carry the loop? UNRESOLVED, and the first answer
  was wrong.** An unpaired comparison gave 4/10 completions with the brief and
  0/10 without, with a convincing mechanism: given no brief the model paged
  through the whole file and stalled without proposing anything. The no-brief arm
  then died on a host timeout, so the two arms were re-run back to back. Paired,
  they are **2/5 and 2/5**. The effect vanished and the no-brief arm edited
  normally. The unpaired figures were measuring the ThinkPad.

  Five tasks an arm is not enough to call a null result either, so this is open
  rather than closed. It matters because it gates how much of M1 is worth
  building: §3's mechanical propagation needs only the *structural* index, and if
  the brief carries nothing measurable then the semantic pass — the expensive
  half, the one that spends the subscription — has no evidence behind it and
  should wait. Resolve with paired trials before building it.
