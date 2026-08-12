# Measurement: clean re-run, 2026-08-12 afternoon

**This supersedes `2026-08-12-heretic-thinkpad.md`**, whose throughput and
prefix-cache figures did not reproduce. That document is kept, with a warning at
the top, because how it went wrong is worth more than the numbers it contains.

Produced by `scripts/bench-clean.py`. Every figure is three trials with fresh
content, against a warmed model, and all trials are reported.

## What was wrong the first time

Two things changed between the morning and afternoon runs, and I cannot fully
separate them after the fact.

1. **The host has a GPU, and rebooted at 12:22.** It carries an NVIDIA RTX 2000
   Ada Laptop GPU with 8 GB of VRAM, and Ollama splits the model roughly
   `68% / 32%` CPU/GPU. Whether that split was active before the reboot is not
   something I can now establish.
2. **The morning probes were not warmed, and ran minutes apart** under Ollama's
   default five-minute `keep_alive`. The model was very likely unloading between
   them, and Ollama can fold model load time into `prompt_eval_duration`. An
   unwarmed sample makes a healthy machine look ruined.

The second is a straightforward methodology error and is the more likely
explanation of the two. Both are now guarded against: `bench-clean.py` warms the
model, sets `keep_alive`, uses fresh filler content per trial so nothing is
served from cache by accident, and repeats everything.

## The numbers

`heretic:latest` is the committed driver. `qwen3-coder:30b` is kept as a
**control**, not a candidate: a pure-attention, non-abliterated model of the
same active size and quant, for telling "the model got worse" apart from "I
broke the harness".

| | `heretic:latest` | `qwen3-coder:30b` |
|---|---|---|
| Architecture | `qwen35moe`, hybrid SSM | `qwen3moe`, pure attention |
| Quant | Q4_K_M | Q4_K_M |
| Prefill, median | **359 tok/s** (359 / 359 / 361) | 467 tok/s (453 / 469 / 467) |
| Decode, median | **25.3 tok/s** (25.3 / 26.7 / 24.5) | 19.5 tok/s (19.5 / 19.6 / 18.2) |
| Incremental prefix reuse | **3/3 yes** | 3/3 yes |
| Six-turn wall clock | **49.9s, 50.5s** | 56.9s, 52.5s |
| Complete + correct edit | **0/3** | 2/3 |

### Prefix reuse works, on both

The morning's headline finding, that a hybrid SSM cannot reuse a prefix
incrementally because it carries one rolling recurrent state, is **withdrawn**.
It reuses fine:

| Trial | Fresh prompt | Same prompt plus a few tokens | Cost if no reuse |
|---|---|---|---|
| 1 | 18.20s | 2.96s | ~18.09s |
| 2 | 17.99s | 1.65s | ~18.09s |
| 3 | 18.04s | 1.67s | ~18.09s |

The morning's contrary result was a model reload wearing a cache miss's
clothing. The architectural story built on it was tidy, plausible, and false.

### heretic is the faster model end to end

Worth stating plainly because it is counterintuitive and it settles a question
that was open all day. `qwen3-coder` prefills 30% faster; `heretic` decodes 30%
faster; **decode dominates**, so heretic finishes a six-turn task in about 50
seconds against qwen3-coder's 52 to 57. Choosing heretic costs nothing in speed.

### Decode is the binding cost, not prefill

At 359 tok/s of prefill against 25.3 of decode, **one generated token costs
about fourteen prompt tokens**. Combined with working prefix reuse, that inverts
the morning's conclusion completely:

```
first turn   ~=  prompt_tokens / 360  +  generated_tokens / 25
later turns  ~=  new_tokens_only / 360  +  generated_tokens / 25
```

Context is cheap and can be paid for once. Output is expensive and is paid every
turn. The lever is **how much the model writes**, not how much it reads.

## The quality gap, which is the real finding

On an identical task, handed the file contents and asked to rename a method,
repeated three times each:

- **`heretic` 0/3.** It chose `read` every time, re-reading a file it had
  already been given, and never produced an edit at all.
- **`qwen3-coder` 2/3.** Two complete, correct edits; one turn spent on
  `search` first.

Three samples is thin, and this is one task rather than a suite. But 0/3 against
2/3 is the direction the research predicted: `heretic` is a general instruct
model that has been abliterated, with no coding or agentic fine-tune under it,
while `qwen3-coder` has exactly that.

**The failure mode matters more than the rate.** heretic is not emitting
malformed actions or inventing file contents. It emits perfectly valid `read`
actions. It *stalls*: it repeats an action instead of progressing. That is
cheap to detect deterministically (the same action twice in a row on the same
target) and cheap to correct without spending a model turn on discovering it.
RFC-0001 §6.3 gains a rule for it.

This is the asymmetric thesis working as intended. The harness is supposed to
carry a model that cannot carry itself.

## The compile gate on `dipper`

Measured against the M0 target, a 14,753 LoC workspace of five crates:

| | |
|---|---|
| Cold full workspace check | 21.7s |
| Warm, no changes | 0.30s |
| **Warm, after a real change in the core crate** (cascades to all four dependents) | **0.77s** |

The gate costs under a second against a model loop of roughly fifty. It is, as
designed, the cheapest thing in the system and the only one that tells the
truth.

## What is still not measured

- **FACP.** Whether the model makes *correct* edits across a real task suite.
  That is M0 and nothing here substitutes for it.
- **Thread tuning and GPU layer split.** Everything ran at Ollama's defaults on
  a hybrid P/E-core CPU with an 8 GB GPU. Nobody has tried `num_gpu` or thread
  counts.
- **Whether the stall behaviour survives a harness that refuses duplicate
  reads.** The proposed §6.3 rule is untested.
