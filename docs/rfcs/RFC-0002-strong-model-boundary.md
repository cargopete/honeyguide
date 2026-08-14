# RFC-0002: The strong-model boundary

| | |
|---|---|
| Status | draft |
| Author | Pete / Nixum |
| Revision | 1 (2026-08-12) |
| Depends on | RFC-0001 §5 (the index), §9 (escalation) |
| Pinned to | Claude Code **2.1.228**, Max subscription, `--model opus` |
| Re-checked | **2.1.231** on 2026-08-14: preamble 32,740 -> 44,774, spawn gap gone (§2.1) |

## 1. Summary

RFC-0001 §5.1 describes the semantic half of index generation in one sentence:
shell out to `claude -p --output-format json` and ask for summaries "against a
strict output schema". Every clause of that sentence is either wrong or hiding
something, and since the index is the entire thesis of the project, the boundary
deserves its own document.

This RFC specifies how honeyguide talks to Claude Code: one persistent process
per index run rather than one per module, containment by tool allow-list rather
than by path pattern, and a request/response framing that removes the need for
an output schema altogether.

Everything here was measured against 2.1.228 on 2026-08-12. Pin the version,
because three of the findings are version-specific and one of them is a flag
that no longer exists.

## 2. Measured facts

### 2.1 Every invocation pays a preamble

A one-word reply, `claude -p --output-format json --model opus`, piped in on
stdin from an empty directory:

| | |
|---|---|
| Wall | 4.1s |
| API | 2.1s |
| Input tokens | 2 |
| Cache creation | 16,754 (`ephemeral_1h`) |
| Cache read | 15,986 |
| Output tokens | 4 |
| Reported cost | $0.1756 |

**32,740 tokens of Claude Code preamble to say "ok".** The roughly two-second
gap between wall and API time is process spawn. Both halves matter: the spawn is
why you do not want many processes, and the preamble is why you do not want many
invocations.

The cache is `ephemeral_1h`, so a run that takes more than an hour, or an index
refresh the next morning, pays creation again rather than read.

**Re-measured 2026-08-14 on 2.1.231, and the numbers have moved.** Same probe,
`--model sonnet`, from `/tmp`:

| | 2.1.228, opus | 2.1.231, sonnet |
|---|---|---|
| Wall | 4.1s | **1.8s** |
| API | 2.1s | 1.8s |
| Cache creation | 16,754 | 19,636 |
| Cache read | 15,986 | 25,138 |
| **Preamble total** | **32,740** | **44,774** |
| Reported cost | $0.1756 | $0.1254 |

The preamble has grown by **37%** in three patch releases, which is the number
this document's transport argument rests on, so it is worth re-checking rather
than citing. The spawn gap has closed almost entirely: wall and API time are now
within 20ms of each other, so the "two seconds of process spawn" that motivated
one persistent process per run is no longer the cost it was. The preamble still
is, and it argues the same way.

Two consequences. The pin at the top of this document should be read as "these
figures decay"; anything quantitative here wants re-measuring before it is
leaned on. And for **escalation** (RFC-0003 §5), where each call is one turn
carrying a few hundred lines, a per-escalation spawn now costs 1.8s and about
$0.13 — cheap enough that the persistent process specified in §3 is an
optimisation rather than a necessity for that path. It remains necessary for
index generation, where the invocation count is per-module.

### 2.2 An agentic call is multi-turn, and turns are the cost

The same CLI, asked to read a one-function crate and write two files:

| | |
|---|---|
| Wall | 28.9s |
| Turns | 7 |
| Cache creation | 19,095 |
| Cache read | **220,359** |
| Output | 1,552 |
| Reported cost | $0.3400 |

The context is re-read on every internal turn, so cost scales with
`turns x context`, not with the size of the answer. Seven turns for a trivial
task is not pathological, it is the shape of an agent: glob, read, write, verify.
The lever honeyguide has is **turn count**, and the way to pull it is to hand
Claude the structural artifacts it would otherwise have to go and discover.

This revises RFC-0001 §5.1, which says we do not feed file contents because
Claude Code is better at finding them than we are at guessing. That is still
true of *contents*. It is not true of *structure*: we have already built a
symbol table and a call graph in pass 1, and making Claude rediscover them by
grep is paying for a turn to learn something we know exactly. The semantic pass
therefore ships the module's file paths and symbol signatures in the request,
and lets Claude read whichever bodies it wants.

### 2.3 Costs are reported but not charged

`total_cost_usd` is what the same traffic would have cost on the API. On a Max
subscription nothing is billed per call, but the number is a good proxy for
quota consumption and honeyguide logs it. Treat it as a fuel gauge, not a bill.

## 3. Transport: one process per run

**Do not spawn `claude -p` per module.** At 4.1 seconds of spawn and preamble
each, a fifty-module index would spend three and a half minutes doing nothing
but starting up, and would pay the 33k preamble fifty times.

Instead, one process per index run, with `--input-format stream-json`, which
reads a stream of messages rather than a single prompt. honeyguide writes one
message per module and reads one response per module. The preamble is paid once.

Measured previously on the same transport in another project (`jay`, 2026-08-11):
first ask 3.1s, second ask 1.7s, with a 75-second idle gap between them, so the
process genuinely survives idling rather than merely pipelining work queued up
front. Re-verify at M1; it is load-bearing enough to deserve its own test.

Two consequences of a persistent process, one good and one to watch:

- **Good:** module N+1 is summarised by something that has already read module
  N, so cross-module references in summaries come for free.
- **To watch:** the conversation accumulates, and by module forty the context is
  large and every turn re-reads it (§2.2). The pipeline therefore restarts the
  process every N modules. N is unmeasured; start at 15 and tune at M1.

Session identity is available via `--session-id <uuid>` and `--resume`, and
`--fork-session` when resuming without reusing the original id. These are the
tools for restarting mid-run without losing the thread, and for making an index
run reproducible in the logs.

## 4. Containment

The semantic pass runs against a repository the user cares about. It must not be
able to modify it.

**Containment is by tool allow-list, and the allow-list gates tools, not paths.**

Measured: invoked with `--allowed-tools "Read" "Write(out/**)"`, Claude wrote
`out/summary.md` as intended and then also wrote `src/NOTES.md`, entirely
outside the pattern, and was not stopped. In the same run a `Bash` call *was*
denied and appeared in `permission_denials`, so tool-level gating works exactly
as advertised. The path pattern in an allow rule does not constrain writes
elsewhere: allow rules widen the surface, they do not narrow it. Narrowing needs
a deny rule.

So the rule for honeyguide is:

> The semantic pass runs with `--allowed-tools Read Grep Glob` and **no write
> capability of any kind**. It cannot edit the repository because it has not
> been given a tool that edits, not because a pattern says it should not.

honeyguide writes every file under `.agent-index/` itself, from the responses.
This is the safer arrangement anyway: the artifacts are validated on the way in
rather than audited after the fact, and a malformed response is a retry rather
than a corrupted index.

If a future version does want Claude to write directly, it must use a deny rule
in a `--settings` block and be tested against the case above before being
trusted. Anyone who reads the allow-list and assumes it confines writes has
already made the mistake this section exists to prevent.

## 5. Output contract: framing, not schema

`claude -p` has no structured-output mode. `--output-format json` wraps the
response in an envelope whose `result` field is prose; it does not constrain
what the model writes. Asking for JSON inside that prose and parsing it back is
possible and brittle, particularly for summaries that quote Rust code containing
braces and quotes.

The persistent-process transport removes the problem rather than solving it.
**One request per module, one response per module, and the pairing carries the
structure.** No schema, no parsing, no repair loop: the response body *is* the
summary, and honeyguide writes it to `summaries/<module_path>.md` after checking
it is non-empty, is under a length bound, and does not begin with an apology or
a refusal.

`AGENTS.md` is the exception, being one document rather than a series. It is
requested as a final message once every module summary is in hand, so the model
writes the project brief already knowing what it said about each part.

## 6. Flags that are not what they look like

Recorded because each cost time to establish, and each would otherwise be
rediscovered by whoever next reads the help text optimistically.

- **`--max-turns` does not exist in 2.1.228.** There is no CLI cap on agentic
  turns. Since turns are the cost driver (§2.2), the only levers are prompt
  design and supplying structure up front. Do not write a config key for a flag
  that is not there.
- **`--bare` cannot use the Max subscription.** It is the obvious way to cut the
  33k preamble, since it skips hooks, LSP, plugin sync, auto-memory and
  CLAUDE.md discovery. Its own help text closes the door: authentication is
  "strictly `ANTHROPIC_API_KEY` or `apiKeyHelper` via `--settings`", and "OAuth
  and keychain are never read". `--bare` therefore means a separate API bill.
  Available if that is ever wanted; not available for free.
- **Path patterns in `--allowed-tools` do not confine writes.** §4.
- **`--allowed-tools ""` swallows a positional prompt**, failing with "Input
  must be provided". Always pipe the prompt on stdin. (Observed in `jay`; the
  habit is cheap and the failure is confusing.)
- **`claude -p` inherits the working directory**, which is what we want here
  (cwd is the project root, so Claude can explore it) but is a leak in other
  contexts. Set it explicitly rather than relying on the parent's.
- **`--exclude-dynamic-system-prompt-sections`** moves per-machine sections out
  of the system prompt into the first user message, which makes the cacheable
  prefix stable across invocations. Worth testing at M1 for its effect on the
  creation-versus-read split in §2.1.

## 7. Cost and quota model

Per index run, roughly:

```
spawn + preamble           ~4s, ~33k tokens, once per process
per module                 3-7 turns, each re-reading the accumulated context
process restart every N     another spawn + preamble
AGENTS.md                  one final request, long output
```

For a fifty-module crate at four turns each, expect tens of minutes and a
reported cost in the low tens of dollars. That is acceptable for something run
once and refreshed incrementally, and unacceptable for anything on the
interactive path. It is the reason RFC-0001 §5.2 refreshes only changed modules
and batches them, and the reason the local model never regenerates the index.

**The quota is shared with the user's own sessions.** A long `hg index` run
consumes the same Max allowance Chief is using to work. The pipeline therefore
prints an estimate and asks before starting a full run, defaults `auto_refresh`
to `prompt` rather than `always`, and can be told to run only the structural
pass.

## 8. Degraded mode

RFC-0001 G4 requires the tool to work with no Claude access at all. Restated
concretely: pass 1 produces `scip.sqlite`, `repomap.txt` and `callgraph.jsonl`
with no network and no CLI, and those are enough for `search` to answer symbol
queries and for retrieval to compute a blast radius. What is lost is
`AGENTS.md` and the per-module summaries, which is to say the part that lets the
local model understand a file without reading it.

Degraded mode is therefore genuinely degraded and should say so: the status line
shows the index as structural-only, so nobody mistakes a thin index for a thin
codebase.

## 9. Escalation reuses this transport

RFC-0001 §9 escalation is the same boundary pointed at a different question, and
should reuse the same code path: one `claude -p --model opus` invocation, tools
restricted to `Read Grep Glob`, cwd at the project root, prompt assembled from
the session log.

One difference. Escalation happens while the user waits, so the 4.1-second floor
and the multi-turn latency are visible rather than amortised. The TUI must show
that it is waiting on the strong model and roughly how long that has been, or a
30-second silence reads as a hang.

## 10. What honeyguide logs

Every envelope field worth keeping, appended to the session JSONL alongside
local-model calls so both halves of the asymmetry are visible in one place:
`session_id`, `num_turns`, `duration_ms`, `duration_api_ms`, `ttft_ms`,
`total_cost_usd`, the full `usage` block including the cache split, and
`permission_denials`.

`permission_denials` is the one to watch. A denial means the semantic pass tried
to do something the allow-list refused, and that is either a prompt that needs
fixing or a capability the pipeline genuinely needs. Either way it should not
pass silently.

## 11. Open questions

- **Q1: what is the right process-restart interval?** Context growth against
  preamble cost. Start at 15 modules, measure at M1.
- **Q2: how much does supplying structure actually cut turns?** §2.2 argues it
  should. Measure summarisation of the same module with and without the symbol
  list in the request.
- **Q3: does `--exclude-dynamic-system-prompt-sections` improve the cache
  split?** §6. Cheap to test, worth a few percent of a long run.
- **Q4: subagents via `--agents`.** A fan-out of module summarisers would be
  faster in wall-clock and would multiply quota consumption. Not for v0.1, but
  it is the obvious lever if index generation becomes the bottleneck.
- **Q5: is one hour of cache TTL a problem for refresh?** An incremental refresh
  the morning after pays creation, not read. If refreshes are usually small this
  does not matter; if they are usually large, batching them into one session
  does.
