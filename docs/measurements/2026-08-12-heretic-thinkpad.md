# Measurement: `heretic:latest` on `pepe-thinkpad`, 2026-08-12

The numbers RFC-0001 is built on. Everything here was produced by
`scripts/serving-probe.py` against the live server; nothing is estimated, and
where a figure is a single sample it says so.

**Target:** `http://pepe-thinkpad:11434`, reached over Tailscale.
**Client:** MacBook (M3 Pro), so network round-trip is included in every wall
figure and is negligible against the numbers below.

**The host**, inspected over SSH rather than assumed. The research doc planned
against a Ryzen 9 5950X desktop on DDR4; it is not that machine.

| | |
|---|---|
| CPU | 13th Gen Intel Core i9-13980HX, 32 threads |
| RAM | 62 GB total, ~48 GB available |
| Disk | 1.9 TB, ~1 TB free |
| Inference | CPU only |

A hybrid P-core/E-core laptop CPU is worth noting for anyone tuning thread
counts later: the usual "set threads to physical cores" advice assumes uniform
cores, and 32 threads here are not 32 equal ones. Untuned so far; every figure
below is at Ollama's defaults.

## 1. Identity

From `/api/show` GGUF metadata.

| Property | Value |
|---|---|
| Ollama tag | `heretic:latest`, 21.2 GB on disk |
| Base | `Qwen/Qwen3.6-35B-A3B` |
| Fine-tune | `llmfan46/Qwen3.6-35B-A3B-uncensored-heretic`, GGUF by mradermacher |
| Architecture | `qwen35moe`, 40 blocks |
| Attention | 16 heads, 2 KV heads, key/value length 256, `full_attention_interval = 4` |
| Recurrent | `ssm.state_size = 128`, `ssm.inner_size = 4096`, `ssm.conv_kernel = 4` |
| Experts | `expert_count = 256`, `expert_used_count = 8`, `expert_feed_forward_length = 512` |
| Parameters | 34,660,610,688 total, roughly 3B active |
| Quantisation | `general.file_type = 15`, which is Q4_K_M |
| Context | `context_length = 262144` |
| Sampling defaults in metadata | temp 1.0, top_k 20, top_p 0.95 |
| Licence | apache-2.0 |

Three notes.

**It is a general instruct model, not a Coder model.** The base is
`Qwen3.6-35B-A3B`, abliterated. Community reliability figures for
Qwen3-Coder-30B (roughly 96% well-formed tool calls) do not transfer, and
neither does its edit-format competence.

**Q4_K_M is the documented quant floor, and we are exactly on it.** Below it,
tool-calling degrades before chat does, which is the worst possible failure
ordering because it is the hardest to notice.

**It is a hybrid SSM/attention model.** One layer in four is full attention; the
rest carry recurrent state. This is the architecture family llama.cpp #19480
identifies as worst affected by CPU MoE sparse-activation overhead, and it is
the reason prefix-cache behaviour had to be measured rather than assumed.

### Capability reporting is inconsistent

`/api/tags` reports `capabilities: ["completion"]` for this model. `/api/show`
reports `["tools", "thinking", "completion"]` for the same model in the same
second. The lighter listing endpoint is not to be trusted for capability
detection; the preflight must use `/api/show`.

### Tool format in the chat template

The GGUF-embedded Jinja template renders the **Qwen3-Coder XML** tool format:

```
<tool_call>
<function=example_function_name>
<parameter=example_parameter_1>
value_1
</parameter>
</function>
</tool_call>
```

That is the right renderer for this family, so the Hermes-versus-Qwen mismatch
of Ollama #14493 does not apply here. The template also carries a `<think>`
splitter for assistant turns and honours `preserve_thinking`.

One oddity worth recording: `ollama show --modelfile` prints
`TEMPLATE {{ .Prompt }}`, which if taken at face value would mean no chat
framing at all. The behaviour observed in §2 is that of a correctly templated
instruct model, so the modelfile rendering is cosmetic and the embedded Jinja
template is what runs.

## 2. Schema-constrained emission

Single request, `format` set to a flat action schema, `think: false`,
`temperature 0.3`, `num_ctx 32768`, `num_predict 300`.

Prompt: *"In src/lib.rs, rename the function `foo` to `bar`. Emit the edit
action."* No file contents were supplied.

| | |
|---|---|
| Wall | 51.1s |
| Cold load | 26.1s of that |
| Prompt tokens | 83 |
| Generated tokens | 300 (hit the cap) |
| Decode | **13.01 tok/s** |
| Schema adherence | perfect: correct field names, fixed order, valid enum |
| Content | entirely invented |
| `think: false` | honoured, `thinking` returned null |

The model produced a structurally flawless edit action whose `search` field was
a fabricated `src/lib.rs`: `cargo new` boilerplate, an `add` function nobody
asked for, two duplicate `mod tests` blocks, and then a run-on string that
consumed the remaining budget without closing.

Two design rules came out of this single response, both in RFC-0001 §6.3:

- **Read before edit.** An `edit` naming a path not read this session is refused
  by the harness without a model call. A cold model will invent the file.
- **Bounded strings.** Every string field in the action schema carries a
  `maxLength`, and an action truncated at the token cap is discarded rather than
  repaired.

The useful conclusion is that schema constraint does exactly what it promises
and nothing more. It guarantees shape. It has no opinion whatsoever about truth.

## 3. Prefill scaling

`num_predict = 1`, Rust-shaped filler so tokenisation is representative,
`num_ctx = 32768`.

| Prompt tokens | Wall | Prefill rate |
|---|---|---|
| 5,136 | 112.3s | 45.9 tok/s |
| 15,416 | 383.7s | 40.2 tok/s |
| ~33,000 | HTTP 400 | request exceeded `num_ctx`; rejected, not truncated |

Prefill runs at three to five times decode and, unusually, does not improve with
batch size. It drifts slightly *downwards* as context grows. On a
memory-bandwidth-bound CPU MoE with this much sparse-activation overhead, prompt
processing is not the cheap, parallel, compute-bound phase it is on a GPU.

**Sample variance, stated rather than smoothed.** The 5,136-token prompt appears
twice in this document at two different rates: 45.9 tok/s here and 66 tok/s in
§4. Same prompt, same server, roughly twenty minutes apart. The likely cause is
that this run followed a period of inactivity and paid some reload cost inside
`prompt_eval_duration`, but that is a hypothesis and not something the probe
isolated. Both are single samples. Planning uses the pessimistic figure of
roughly 50 tok/s, and anyone quoting a precise number off this table should
run it again first.

A 15k-token prompt costs six and a half minutes to ingest. Sending the whole
context afresh every turn is not slow, it is impossible, which makes the KV
prefix cache load-bearing rather than an optimisation. Whether it bears that
load is §4, and the answer is no.

The 400 at roughly 33k tokens is worth knowing for the preflight: this Ollama
rejects an over-context request outright rather than silently truncating it. A
loud failure, for once, which is a small mercy.

## 4. KV prefix cache, and the control that explains it

Three requests: one cold, one byte-identical repeat, and one with a short
sentence appended to the end of the user message so that all but the last few
tokens are shared.

**`heretic:latest`, hybrid SSM MoE:**

| Call | Prompt tokens | Prefill |
|---|---|---|
| cold | 5,136 | 77.2s |
| identical | 5,136 | 0.2s |
| extended by 6 tokens | 5,142 | 81.7s |

**`qwen3:8b`, pure-attention dense, same host, same probe:**

| Call | Prompt tokens | Prefill |
|---|---|---|
| cold | 4,739 | 160.8s |
| identical | 4,739 | 0.2s |
| extended by 5 tokens | 4,744 | **1.2s** |

The control is the whole point of running it. Ollama is perfectly capable of
incremental prefix reuse, and demonstrates it on the same server, in the same
minute, against a pure-attention model: a five-token extension costs 1.2
seconds. The hybrid model recomputes all 5,142 tokens for a six-token extension.

So this is a property of the architecture, not of the server. A hybrid SSM layer
carries a single rolling recurrent state rather than a per-token cache. An exact
repeat can be served because the state is still sitting there from last time.
There is no way to rewind that state to an arbitrary earlier position, so any
prompt that is not an exact match starts again from nothing.

Note also which model is faster cold: heretic ingests at 66 tok/s against
qwen3:8b's 29, because 3B active beats 8B dense. It wins the first turn
comfortably and loses every turn afterwards by two orders of magnitude.

**This is the single most consequential measurement in the project.** An agent
loop is nothing but repeated passes over a slowly growing prefix, which is close
to the worst case for that trade. Over a ten-turn session at 5k of context, a
pure-attention model pays one full prefill and then a second or two per turn; a
hybrid pays a full prefill every turn, which here is thirteen minutes of
waiting.

The design consequences are taken in RFC-0001 §6 (one model call per turn rather
than two), §8.5 (a ~4k prompt budget, and the withdrawal of append-only prompt
discipline), and Q4/Q5, where the model A/B stops being a question about
abliteration and becomes a question about architecture.

### Detection

`prompt_eval_count` is unchanged on a cache hit: Ollama reports the number of
tokens the prompt contains, not the number it recomputed. The signal is entirely
in `prompt_eval_duration`, and the two populations differ by three orders of
magnitude, so any threshold between them works. `hg_llm::Usage::prefix_cache_hit`
uses 1,000 effective tok/s.

## 5. Native tool calling

Three tools exposed via Ollama's `tools` parameter, `think: false`.

Prompt: *"Find where the function `validate` is defined."*

```json
tool_calls = [{"function": {"name": "search", "arguments": {"query": "fn validate"}}}]
content    = ""
```

Correct tool, sensible argument, empty content, no XML leaking into the content
field. Better than the survey's expectations, and consistent with the chat
template rendering the Qwen3-Coder XML format correctly rather than the Hermes
JSON format that Ollama #14493 describes.

Not tested: the behaviour above five tools, which is where Goose #6883 reports
the format collapsing. Honeyguide caps at five regardless, so the cliff is out
of reach by construction.

Honeyguide still does not use this path, for one reason given in RFC-0001 §14:
Ollama's tool-parameter schemas are rendered into the prompt but not enforced
during decoding, whereas `format` is enforced. Native calling would have given
us a well-chosen tool with unbounded arguments. §2 is what unbounded arguments
look like.

## 6. Schema enforcement

What Ollama's `format` constraint actually guarantees, tested directly because
the design rests on it.

| Property | Enforced |
|---|---|
| Valid JSON, property whitelist, `enum`, `maxLength` | yes |
| Top-level `required` list | yes |
| Property order | **no** |
| `required` inside `anyOf` branches | **no** |

Consequences, and the evidence, are written up in
[`prompts/README.md`](../../prompts/README.md). The short version: per-tool
argument requirements cannot be expressed in the schema, so `required` lists
every string field and unused ones come back empty. An `anyOf` schema
discriminated on a `const` tool value returned `{"tool": "edit"}` and nothing
else, dropping even `reasoning`.

Also measured here: the system prompt at `prompts/system.md` costs **410
tokens**, against the ~700 budgeted in RFC-0001 §8.5.

## 7. What was not measured

- **Thread tuning.** The host is a hybrid P/E-core i9-13980HX with 32 threads,
  and everything above ran at Ollama's defaults. The standard "threads =
  physical cores" advice does not transfer cleanly to asymmetric cores, and
  nobody has tried.
- **`ik_llama.cpp` or llama-server as an alternative backend.** RFC-0001 §8.1
  expects a meaningful improvement on this architecture. Unmeasured.
- **FACP for this model.** The whole point of M0. Nothing here says whether the
  model can actually make a correct edit, only that it will make a well-formed
  one.
- **Stock Qwen3-Coder-30B on the same box**, which is the A/B that RFC-0001 Q4
  calls for and which nobody appears to have published.
- **Behaviour above 32k context.** Untested beyond the 400.
