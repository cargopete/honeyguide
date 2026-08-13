# Progress log

Newest first. One entry per meaningful slice of work.

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
