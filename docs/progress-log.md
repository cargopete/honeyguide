# Progress log

Newest first. One entry per meaningful slice of work.

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
