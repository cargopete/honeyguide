# prompts

The two artifacts the agent loop actually runs on, and the measurements that
shaped them. Both were wrong on the first attempt in ways that only showed up
against the real model, so the findings are recorded here rather than left to be
rediscovered.

Measured against `heretic:latest` on `pepe-thinkpad`, Ollama, 2026-08-12.

## `system.md`

**410 tokens**, measured against the model's own tokenizer by differencing
`prompt_eval_count` with and without it. RFC-0001 §8.5 budgets roughly 700, so
there is headroom, but not much: at ~50 tok/s of prefill this prompt costs about
8 seconds of every turn for the life of a session.

It names the required fields per tool, which the schema cannot enforce (below).

## `action-schema.json`

### What Ollama's constrained decoding actually enforces

Tested directly, because the whole design rests on it:

| Property | Enforced |
|---|---|
| Valid JSON | yes |
| Property whitelist (`additionalProperties: false`) | yes |
| `enum` values | yes |
| `maxLength` on strings | yes |
| Top-level `required` list | **yes** |
| Property **order** | **no** |
| `required` inside `anyOf` branches | **no** |

Two consequences, both of which cost a design iteration to find.

**Per-tool arguments cannot be expressed in the schema.** The obvious approach
is `anyOf` branches discriminated on a `const` tool value, so that choosing
`edit` requires `search` and `replace`. It does not work: the model returned
`{"tool": "edit"}` and nothing else, dropping even `reasoning`. The branch
`required` lists were ignored entirely, and the result was worse than the flat
schema it was meant to improve.

**Field order is not controllable.** The same schema produced output in schema
order on one run and alphabetised on another. Any design that depends on the
model emitting one field before another is depending on nothing.

### Why `required` lists fields the tool does not use

Because it is the only lever that works. With `required` set to just
`["reasoning", "tool"]`, the model emitted `tool: "edit"` carrying neither
`search` nor `replace`: a structurally valid, semantically empty action. The
harness catches that, but a deterministic refusal still costs a full turn, and
a turn on this hardware is about eighty seconds.

So `required` lists every string field. Tools that do not need a field get an
empty string, and the harness ignores it.

The obvious objection is that forcing `search` and `replace` to be present
invites the model to invent them on a turn where it has read nothing, which is
precisely the failure this whole design exists to prevent. Tested:

- **Cold turn, nothing read yet.** Chose `read` (correctly, per rule 1), and
  emitted `search: ""` and `replace: ""`. No fabrication.
- **Edit turn, after a read observation.** Complete and correct rename, with the
  line-number gutter correctly stripped, in **79 output tokens** against the
  **125** the under-constrained version spent inventing a `start` and a
  `summary` it had no use for. Cheaper as well as correct.

### The gutter problem

`read` observations are line-numbered (`   7|     pub fn area(...)`), so the
model must strip the gutter before putting text in `search`. It did this
correctly in testing, but it is a transformation the harness itself introduced
and therefore a fabrication risk of our own making.

The harness must **normalise the gutter away before matching**: if every line of
a `search` value carries a `\s*\d+\|` prefix, strip it. This is not fuzzy
matching, which RFC-0001 §7 forbids. It is undoing our own formatting, and it is
deterministic.

## Still to verify

The final field list (`reasoning, tool, path, query, search, replace, summary`)
was reasoned from the two runs above rather than confirmed by a third against
that exact list. Worth one run at M0 before trusting it.
