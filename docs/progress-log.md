# Progress log

Newest first. One entry per meaningful slice of work.

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
