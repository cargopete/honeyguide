# Progress log

Newest first. One entry per meaningful slice of work.

## 2026-08-18 - ask mode, and the spike had not run since the last commit

**The spike did not run at all.** `--escalate` was plumbed into
`Session.__init__`, which never used it, while `run_task` reads `escalate_to`
in its abort branch and did not accept it. Both call sites passed it. Every
invocation, suite and free-form alike, raised `TypeError` after building the
overlay and warming cargo, before the first model turn. It landed in 192b5ee
and nothing has been run end to end since, which is the actual finding: a
harness whose own runs are the measuring instrument has to be run after it is
changed, and this one was committed on the strength of the diff.

**Ask mode.** `--ask "question"`, or `scripts/hg-ask` from inside any Rust
repository. Read-only Q&A: three tools, `read`, `search` and `answer`, no
overlay because nothing in the mode can write, and no compile gate because an
answer cannot be compiled. What replaces the gate is a **grounding check**, and
it is weaker by a long way: the harness builds a vocabulary of every identifier
in the tree, and refuses an answer that names a file, type, function or constant
which is not in it. Citations are `path:line`, must resolve to a real line of a
file the model has actually read this session, and are read back **from disk by
the harness** for display, so the quoted text is never the model's.

Five questions against two repositories, one trial each, which is an anecdote
with a denominator and is reported as one.

**The model does not volunteer an answer. It has to be forced to give one.** In
five runs of five it read a file to exhaustion, stalled, and only answered when
the harness stopped it. The paging rule handles the re-reads and the stall rule
aborts, so nothing is wasted beyond the turns, but the answer arrives on the
forced turn and not before.

**And "stop reading and answer" does not work as a sentence.** Asked in words,
it searched again. The fix was to narrow the `tool` enum to `["answer"]` for
that one turn, which is the same lesson as `prompts/README.md`: the decoder
enforces the enum, and it does not enforce a request. Instructions that must
hold belong in the schema.

**Asked about a feature the repository does not have, it invents rather than
declines.** MSE peer encryption, which dipper has no trace of. Forced to answer,
it produced a `MseCrypto` struct, `dipper-bt/src/crypto.rs`,
`dipper-bt/src/peer/connection.rs`, a `PEER_KEY` constant and a confident
citation of the wrong BEP. Told by name which of those do not exist, it invented
a different set, `MseCodec` over `tokio_util::codec::FramedRead`. The check
refused both and the mode reported no answer. This is the very first request
this project ever sent, arriving again in a mode where the compile gate cannot
follow.

**The first version of the check missed half of it, and the reason is worth
keeping.** It scanned backticked names, because that is how the prompt asks for
them. The model wrote `**MseCrypto**` in bold and `PeerConnection::connect()`
bare, and both passed. Prose is not a format the model has agreed to. The scan
now reads backticks, bold, CamelCase with a lowercase first hump, and
SCREAMING_SNAKE, and skips acronyms deliberately: `RC4` and `HTTP` are English
in a sentence about a protocol, not claims about this repository.

**What it cannot do.** `PeerConnection` is a real type in dipper, so the
fabricated claim attached to it passed the check untouched. The gate tests
*names*, not sentences. An answer built entirely from real names can still be
false, and the citations are there so the reader can check the half the machine
cannot.

Retrieval also sent an MSE question into `dipper-web/src/ffmpeg.rs` and
`play.rs`, on Media Source Extensions, which is §5.3's lexical weakness turning
up in a new mode and one more argument for the index.

**Later, from using it.** Four defects, all found by asking it real questions
rather than by reading the code.

`search` only looked at `*.rs`, so `dipper-web` returned no hits: a crate name is
hyphenated, lives in `Cargo.toml` and never appears as an identifier. It now
covers `.toml` and `.md`, and a miss reports the paths whose names are close.

Retrieval returned nothing for "explain the UI", because the question contains no
symbol, and the model was left guessing names in the dark. It now gets the file
list with line counts when retrieval comes back empty, which is the free stand-in
for §5.1's ranked repo map. That question went from three and a half minutes and
a decline to 27 seconds and a correct answer.

The model re-read files retrieval had already supplied whole, five pages of
twenty seconds on a 498-line file it had been handed complete. A rangeless read
of a file it already holds is now a page forward.

**And `--continue` taught the sharpest lesson of the day.** Carrying the previous
answers' *prose* let the model reply from memory: asked what the endgame changes,
it recalled that the picker returns every unrequested piece, named a `next`
method that does not exist, and cited line 1, the module doc comment. The real
behaviour is a duplicate request permitted for a piece already in flight, one
filter in `next_for`. It then carried that wrong answer into the next session and
repeated it verbatim, so one bad answer becomes the thread's premise. Carrying
the *questions* and the *files* instead, and nothing else, produced the correct
answer with three exact citations. Referents from the thread, facts from the
file.

**The harness told the model to page an 856-line file, and it obeyed.** Retrieval
budgets 600 lines across three files, so `dipper-cli/src/main.rs` arrived as
fragments ending in "truncated, use `read` for the rest", and the model read the
rest: eight pages, eleven turns, 163 seconds. Two changes, and the same question
now takes two turns and 56 seconds. A file supplied in fragments also gets its
**outline**, every declaration with its line number, which is 33 lines against
856 and is §5.3's symbol signatures done lexically. And ask mode's page rose from
120 lines to 240 with the observation cap at 8000 characters, because a turn
costs about fifteen seconds of latency whatever it carries: a small page spends a
whole turn to move very little. That trades §8.5's prompt budget for turns
deliberately, and the trade is worth measuring properly rather than assuming.

**A missing attachment produced the worst answer of the day, and it passed every
check.** `-f ~/suggestion.txt "how can we do this in dipper?"`, with no such
file, warned and carried on, so the model was asked how to do *nothing* and
returned a fluent paragraph about dipper downloading torrents, correctly cited to
a doc comment. Grounded, well-formed, worthless. Every mechanism here tests the
answer, and the fault was in the question. A named file that is not there is now
fatal before the first model call. The general lesson is the one the gate cannot
fix by itself: it checks that names exist, not that a question had content.

`-f` and piped stdin attach text to a question, because the shell executes
backticks and `$(...)` inside a quoted argument before any of this sees it.
Attached text joins the vocabulary, or every name in a pasted snippet reads as a
fabrication, and it cannot be cited.

Timing, warm, `heretic:latest` over Tailscale: median turn 14.7 to 21.6s, whole
questions 40 to 206s. Four questions whose subject exists were answered and
grounded, three of them on the forced turn; the one whose subject does not exist
was refused twice and reported as unanswered.

## 2026-08-14 (later) - propagation works, and three trials cannot measure anything

Five configurations of the same five-task suite, three trials each, and the
spread settles a methodological question that outranks all of them.

| config | per trial | total |
|---|---|---|
| baseline, cap 12 | 3, 1, 2 | 6/15 |
| cap 24 | 2, 3, 3 | 8/15 |
| resample | 2, 2, 1 | 5/15 |
| propagate, detector inert | 3, 3, 3 | **9/15** |
| propagate, detector live | 3, 1, 0 | **4/15** |

Pooled 32/75 = 43%, and a single fifteen-task arm at that rate has a 95%
interval of 3/15 to 10/15. Every result above sits inside it. The best score of
the day came from a run where the feature under test **fired zero times**; the
worst came from the run where it worked. **Three trials cannot distinguish these
configurations**, and detecting a twenty-point difference needs about 20 trials
per arm rather than 3. Every completion-rate comparison made today is withdrawn,
recorded as RFC-0003 §0.

What survives is behavioural and deterministic, which is the same asymmetry
§12.1 found for timing and now looks like a standing property of this domain.

**Propagation works.** With the detector fixed it fired, renamed exactly the
three sites the compiler names, and `rename` completed — the first time that task
has passed with the mechanism live. Against grep's 84 wrong sites in 15 files.

**And it was inert before that, which is the better lesson.** `detect_rename`
required `search` and `replace` to be identical but for one token. Correct,
tested 10/10, safely conservative, and useless: models do not write minimal
edits. Asked to rename `Catalogue::count`, the driver renamed the method *and*
dropped the doc comment *and* dropped the indentation, so the test said "not a
rename" and the whole feature sat inert through a fifteen-task suite while I
credited it for variance. Replaced by a comparison of *definitions* — one name
out, one in, same keyword — which matches the real output and is still
conservative. 11/11 including the exact edit observed.

**Also landed:** rustfmt in the gate, verified against the precise defect that
prompted it (a correct doc comment indented eight spaces, which compiled and
would have failed dipper's own `cargo fmt --check` CI). It only runs on files
that were canonically formatted beforehand.

## 2026-08-14 - the turn cap buys nothing, and SCIP settles propagation

RFC-0003's two cheapest items measured, and a lexical shortcut caught before it
could do damage.

**The turn cap is not the constraint (§6, closed).** Cap 24 against cap 12, three
trials each: completions 6/15 to 8/15, and the cap did not cause it. Fourteen of
fifteen runs finished inside the *old* cap; the one that used more turns failed
anyway; and the `rename` that succeeded took 11 turns, so it was reachable at 12
all along. Two extra completions against a per-trial spread of 3,1,2 and 2,3,3 is
noise. Raising the cap costs 33% more wall-clock (949s to 1,260s) for nothing
demonstrable, so it stays at 12.

Something genuinely new did fall out: `rename` completed **once**, the first
cascading multi-site edit this project has ever seen finish, in 21 attempts
across two models. Rare rather than impossible.

**Mechanical propagation, prototyped on grep, is dangerous.** RFC-0003 §3 assumed
a whole-token search would be a rough version of SCIP. Tried on a copy of dipper,
renaming `Catalogue::count` rewrote **84 sites across 15 files** —
`Iterator::count()`, struct fields, local bindings, in crates that do not depend
on `dipper-index`. No threshold fixes that, and applying an edit to a site that
merely looks right is fuzzy application, which RFC-0001 §7 forbids by name. It is
now behind an off-by-default `--propagate` documented as unsafe.

**SCIP gives the exact answer (§3.2b).** `hg-index` now parses a real
`index.scip`. `rust-analyzer scip` runs in **22.1s** on dipper for 2,815 symbols
and 28,807 occurrences, and the reference set for `Catalogue::count` is **3
sites** — the same three the compiler reported as errors when the rename was
attempted. Two independent oracles, identical answer. The workspace contains six
distinct symbols named `count`, including `Iterator::count` from `core`, which is
why the lexical version never stood a chance.

Two bugs surfaced by probing real output instead of reasoning about it: the
symbol-name parser missed the `impl#[Catalogue]count().` form that inherent
methods actually take, and a definition lookup keyed on file and line alone is
non-deterministic, returning the *parameter* declared on the same line as the
method about a third of the time. Both would have produced confident wrong
answers.

**Also landed:** free-form mode. `m0/spike.py --repo ~/Projects/dipper -p "..."`
runs an ad-hoc request against a real repository, working in a `cp -Rc` overlay
per Q2 and handing back a patch, never touching the working tree, and refusing to
offer a patch whose gate is red. It builds the project brief structurally from
`Cargo.toml` (§5.1 pass 3), which on dipper is good enough to be a partial
explanation for Q4's null result. Retrieval was rebuilt twice: scoring files by
matched words put a BitTorrent discovery module top for a question about
`dipper-index`, and the fix was to score *symbols* — a token only counts if it
has a definition in the tree, and the defining file wins. 8 of 9 realistic
requests now put the right file in the top two.

## 2026-08-13 - M0 run in full. The gate was the thing holding the model back

The five-task suite against a real `dipper` checkout, three trials per model,
both gates measured for the first time. Full record in
[`docs/measurements/2026-08-13-m0-suite.md`](measurements/2026-08-13-m0-suite.md).

**The result that matters is not the score.** Neither model completed a single
cascading multi-site task: `rename` and `add_field` went 0/3 for the driver and
0/3 for the control. Every other failure in the suite is one of those two tasks.
The single-site tasks run at 2/3 for the driver and 3/3 for the control. The
failure is a cliff, not a slope, and it is the harness's, not a model's. That is
now Q7.

| | driver | control |
|---|---|---|
| WFA | 90/90 | 98/98 |
| FACP, of edits proposed | 25% | 33% |
| **Tasks actually completed** | **6/15** | **9/15** |
| per trial | 3, 1, 2 | 3, 3, 3 |
| median turn | **8.7s** | 16.6s |
| total wall | 949s | 1,896s |

**G2 (FACP 60%) fails for both. The turn-time gate passes by 14x.** §13 says a
failed gate means pulling the model A/B forward; it was run and the answer is
that the model is not the variable. The control is 1.5x the completions for
exactly 2x the wall-clock, and its advantage is entirely consistency on tasks
the driver can already do: 3/3/3 against 3/1/2 on identical inputs.

**Seven harness defects, found by running it**, five of them making the model
look worse than it was. The two that matter:

*The gate was a ratchet.* §6.2 reverted any edit that failed `cargo check`, which
makes every change with no compiling intermediate state unreachable. The control
proposed eight edits on `add_field`, every one matching the file uniquely, none
fabricated, and all eight were thrown away — because adding a field is what
breaks the literals that construct it. Twelve turns, four minutes, file
unchanged, FACP recording 0/8. Edits now accumulate, errors come back, and the
harness rolls back to the last green state itself when the rustc error count
stops falling. The first version of that rollback counted red turns instead and
fired one turn before success, throwing away a correct rename.

*FACP was counting no-ops.* The model emits edits whose `replace` is
byte-identical to `search`. The harness applied them, `cargo check` passed
because nothing had changed, and it scored a first-apply pass. Two of five tasks
in one trial "passed the gate" this way and failed their oracles. Every FACP
figure taken before the fix is inflated by an unknown amount, which is why §12
now says the oracle rate is the real quality number and FACP is a diagnostic.

**Four of the five tasks had no oracle at all** before today; the fallback was
"does the workspace still compile", which is true of a workspace nobody has
touched. The control's first `rename` run — search, read, `check`, declare
victory, zero edits — would have scored a pass. `--selftest` now asserts every
oracle fails on a pristine tree.

Also measured: plain `cargo check` returns **0** on a type error inside a
`#[cfg(test)]` module where `--all-targets` returns **101**, so the gate now uses
the latter, at a cost of nothing worth counting (0.11s idle, 0.31s on a cascading
change). The first example offered for that claim was wrong and is recorded as
withdrawn in §6.2, which is becoming a tradition.

**The project brief looked decisive and then did not.** M1's first question is
whether the index earns its place. Unpaired, the driver scored 4/10 with the
brief and 0/10 without, and the mechanism looked convincing: with no brief it
paged through the entire file across four turns, exhausted it, and stalled
without ever proposing an edit. That run then died on a socket timeout against a
host which afterwards held no model, so the arms were re-run back to back. Paired,
**2/5 and 2/5**, with the no-brief arm editing normally. The unpaired figures were
measuring the ThinkPad. Unresolved rather than null — five tasks an arm decides
nothing — but it means M1's semantic pass has no evidence behind it yet, and
RFC-0003 §3 needs only the structural half.

**Q2 closed the same day**, with `scripts/overlay-probe.py`. The overlay is a
whole-tree `cp -Rc` (plain `cp -R` fallback) sharing one `--target-dir` with the
working tree. The hardlink forest was disqualified behaviourally rather than by
argument: written to the way a naive tool writes, it changed the file in the real
working tree. Build cost is 0.03s for every candidate, because 14.7k lines is
2 MB, so the per-edited-file scheme revision 3 guessed at is unnecessary. And the
shared target directory does **not** thrash, which was the actual risk: with the
overlay diverged and a fresh edit each round, alternating gates run 0.29s in the
overlay and 0.12s in the working tree, indefinitely. Its own target directory
would have cost a 13.2s cold build at session start. `git worktree` passes every
test and is rejected anyway, because it materialises `HEAD` rather than the
working tree.

## 2026-08-12 (evening) - most of today's measurements were wrong

RFC-0001 revision 3. The morning's throughput and prefix-cache figures did not
reproduce, and two architectural arguments built on them are withdrawn.

**Cause.** Probes ran unwarmed, minutes apart, under Ollama's default
five-minute `keep_alive`, so the model was unloading between them and reload
time was landing inside `prompt_eval_duration`. The host also carries an 8 GB
RTX 2000 Ada that Ollama partially offloads to (`68%/32%` CPU/GPU), and it
rebooted at 12:22 between the two runs. The methodology error is the more likely
explanation and is the one worth fixing.

**Corrected, three trials each, warmed, fresh content:**

| | morning | clean re-run |
|---|---|---|
| heretic prefill | 40-66 tok/s | **359 tok/s** |
| heretic decode | 13.0 tok/s | **25.3 tok/s** |
| incremental prefix reuse | "none, it is a hybrid SSM" | **works, 3/3** |

**Withdrawn:** "prefill is the binding cost" (decode is, by ~14x per token);
"a hybrid SSM cannot reuse a prefix incrementally"; "schema field order is
load-bearing". **Restored:** two-phase generation, append-only prompt
discipline, and a ~16k context budget, all of which revision 2 cut on the
strength of the bad numbers.

**The A/B, run against `qwen3-coder:30b` as a same-quant pure-attention
control.** heretic is *faster* end to end (49.9s and 50.5s against 56.9s and
52.5s over six turns) because decode dominates and it decodes 30% quicker. It is
worse on quality: 0/3 complete correct edits against 2/3. Q4 and Q5 close;
heretic is the committed driver and qwen3-coder stays installed as a control for
telling model regressions from harness bugs.

**The quality gap has a shape worth having.** Every heretic failure was a valid
`read` action re-reading a file it had already been shown. It stalls rather than
fabricates, which is deterministically detectable, so §6.3 gains a no-stalling
rule and Q6 asks whether that closes the gap. M0 answers it.

**Also measured:** the compile gate on `dipper`, the M0 target, is **0.77s**
warm after a real core-crate change that cascades to all four dependents (21.7s
cold). Against a fifty-second model loop, the gate is free.

**The lesson, now RFC-0001 §12.1.** Every timing finding from the morning was
wrong by up to eight times. Every behavioural finding from the same session
reproduced exactly: the fabricated file, the schema enforcement limits, the tool
surface. Timing is fragile, behaviour is not, and architectural decisions should
lean on the second. `scripts/bench-clean.py` implements the rules.

Repo made public as a public good, with the superseded measurements kept and
marked rather than deleted.

## 2026-08-12 (later still) - the prompt and schema, and what the schema cannot do

Wrote the two artifacts M0 actually runs on, `prompts/system.md` and
`prompts/action-schema.json`, then tested them against the model rather than
trusting them. Both were wrong.

The system prompt measures **410 tokens** against the ~700 budgeted, which is
fine. The schema was broken twice over.

**What Ollama's constrained decoding enforces**, tested: valid JSON, the
property whitelist, `enum` values, `maxLength`, and the top-level `required`
list. **Not** property order, and **not** `required` inside `anyOf` branches.

So per-tool argument requirements cannot be expressed in the schema. With
`required` set to `["reasoning","tool"]` the model emitted `tool: "edit"`
carrying neither `search` nor `replace`. The textbook fix, `anyOf` discriminated
on a `const` tool value, came back worse: `{"tool": "edit"}` alone, dropping
even `reasoning`.

The fix is to require every string field and let unused ones come back empty.
The obvious objection is that forcing `search` and `replace` invites fabrication
on a turn where nothing has been read, so that was tested directly: on a cold
turn the model chose `read` and emitted both as empty strings, and on the edit
turn it produced a complete correct rename in 79 output tokens against the 125
the under-constrained version spent inventing fields it had no use for.

**A claim from earlier today is withdrawn.** RFC-0001 §6 said field order was
load-bearing, so putting `reasoning` first would make the model reason before
committing to a tool. Order is not enforced: the same schema produced schema
order on one run and alphabetised on another. The single-call design survives on
prefill economics; that particular justification for it does not.

Also found: line-numbered `read` observations force the model to strip a gutter
we introduced, which is a fabrication risk of our own making. The harness will
normalise a `\s*\d+\|` prefix away before matching. That is not fuzzy matching,
which §7 forbids; it is undoing our own formatting.

Host facts corrected while checking there was disk for the model A/B. The
ThinkPad is a **13th-gen i9-13980HX, 32 threads, 62 GB RAM, ~1 TB free**, not
the Ryzen 9 5950X on DDR4 the research doc planned against. Everything measured
so far ran at Ollama's default thread settings, and a hybrid P/E-core CPU is not
a machine where "threads = physical cores" transfers cleanly.

## 2026-08-12 (later) - RFC-0002, the strong-model boundary

RFC-0001 §5.1 described the semantic half of index generation in one sentence:
shell out to `claude -p` and ask for summaries "against a strict output schema".
Probing Claude Code 2.1.228 showed that most of that sentence was wrong, so the
boundary got its own RFC.

**Measured.** A one-word reply costs 4.1s wall, 2.1s API, and **32,740 tokens of
preamble** (16,754 created plus 15,986 read, `ephemeral_1h`). An agentic call
that read one file and wrote two took 7 turns, 28.9s, and re-read 220,359 cached
tokens, so cost scales with `turns x context` rather than with answer length.

**Three things that are not what they look like.**

- **`--allowed-tools` gates tools, not paths.** Invoked with
  `Write(out/**)`, Claude wrote `out/summary.md` as asked and then also wrote
  `src/NOTES.md`, outside the pattern, unimpeded, and said so. A `Bash` call in
  the same run *was* denied, so tool-level gating works fine. Allow rules widen;
  they do not narrow. The semantic pass therefore gets `Read Grep Glob` and no
  write capability at all, and hg writes every artifact itself.
- **`--max-turns` does not exist** in 2.1.228. Since turns are the cost driver,
  it would have been the obvious lever, and specifying it would have put a
  config key in front of a flag that is not there.
- **`--bare` cannot use the Max subscription.** It is the obvious way to shed
  the 33k preamble, and its own help text closes the door: auth is strictly
  `ANTHROPIC_API_KEY`, OAuth and keychain are never read.

**Design changes.** One persistent process per index run over
`--input-format stream-json`, not one per module (fifty modules would otherwise
spend three and a half minutes purely on spawn, and pay the preamble fifty
times). No output schema at all: one request per module, one response per
module, and the framing carries the structure that a schema was being asked to
carry. And the semantic pass now ships each module's symbol signatures from pass
1, because making Claude rediscover by grep what SCIP already knows is paying
for turns to learn something we have on disk.

## 2026-08-12 - Repository opened, RFC-0001 revised against measurement

Created the repository, archived the research survey unedited, and rewrote
RFC-0001 as revision 2 after probing the actual target machine rather than the
assumed one.

**What the probe changed.** Revision 1 assumed Qwen3-Coder-30B-A3B on a local
Ryzen box, served by llama-server with GBNF. The real target is
`heretic:latest` on `pepe-thinkpad` over Tailscale, and it is a different animal
in three ways that matter:

- It is `Qwen3.6-35B-A3B-uncensored-heretic` (Q4_K_M, 256 experts, 8 active), a
  general instruct model that has been abliterated. There is no coding or
  agentic fine-tune underneath, so the community's tool-call reliability figures
  for Qwen3-Coder do not transfer.
- It is a hybrid SSM/attention MoE, one full-attention layer in four. That is
  the architecture family llama.cpp #19480 flags as worst for CPU MoE
  throughput, and recurrent state is why prefix-cache behaviour had to be
  measured rather than assumed.
- Ollama is therefore the primary backend, not the fallback, which trades GBNF
  and chat-template control for JSON-schema constraint.

**What the numbers changed.** Generation runs at ~13 tok/s, the top of the
research doc's estimated range and no surprise. Prefill runs at 40-66 tok/s and
does not improve with batch size: 15,416 tokens took 384 seconds. Prefill, not
generation, is the binding constraint, which inverts the usual context-budget
reasoning.

**What the cache measurement changed, which is most of the design.** An
identical prompt is served free (77.2s becomes 0.2s). A prompt extended by six
tokens costs a full recompute of all 5,142. A control run against `qwen3:8b` on
the same host pays 1.2s for a five-token extension, so this is the model's
hybrid SSM architecture and not the server: one rolling recurrent state, no
position to rewind to, no partial prefix reuse.

An agent loop is repeated passes over a slowly growing prefix, which is close to
the worst case for that trade. Consequences taken in the RFC: two-phase
generation is out (§6, it would buy a fraction of a reasoning benchmark for a
second full prefill), the prompt budget dropped from ~17.7k tokens to ~4k
(§8.5), the append-only rule written earlier the same day was withdrawn because
it now buys nothing, and M0 gained a second gate on median turn wall-clock. Q5
is new: hybrid SSM may simply be the wrong architecture for this workload, which
would make it a model-selection criterion rather than an implementation detail.

**What the first request changed.** The very first prompt sent to the model
asked it to rename a function in `src/lib.rs` without supplying the file. It
returned a schema-perfect edit action whose `search` field was an entirely
invented `src/lib.rs`, and ran on until it hit the token cap mid-string. Two new
deterministic rules in §6.3 came directly out of that one response:
read-before-edit, and bounded strings in the action schema.

Also landed: the RFC data model as four compiling crates, an annotated
`honeyguide.toml.example`, and `scripts/serving-probe.py`, which is the
prototype of the §8.4 serving preflight and produced every number above.

No agent code yet. M0 is next, and it is a throwaway script, not a product.
