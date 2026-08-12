#!/usr/bin/env python3
"""Serving preflight and baseline measurement for a Honeyguide target.

This is the prototype of RFC-0001 §8.4. It answers, against a real server, the
questions the RFC is not allowed to assume:

  1. What is the model, architecture and quantisation, really?
  2. Does schema-constrained emission produce valid JSON?
  3. What are decode and prefill throughput at realistic context sizes?
  4. Does the KV prefix cache survive between requests?  (Q1)
  5. Does native tool calling work well enough to matter?

It is deliberately dependency-free so it can be dropped onto any box.

Usage:
    python3 scripts/serving-probe.py [--host URL] [--model NAME] [--quick]

Note on wall-clock: a full run against a CPU-only 35B-A3B takes tens of minutes,
most of it in the large prefill cases. --quick skips those.
"""

import argparse
import json
import sys
import time
import urllib.request

DEFAULT_HOST = "http://pepe-thinkpad:11434"
DEFAULT_MODEL = "heretic:latest"

# A flat action object with bounded strings and a fixed field order, exactly as
# RFC-0001 §6.3 requires. Field order removes degrees of freedom; maxLength is
# what stops the model running a string until it hits the token cap.
ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "enum": ["read", "search", "edit", "check", "finish"]},
        "path": {"type": "string", "maxLength": 256},
        "search": {"type": "string", "maxLength": 4000},
        "replace": {"type": "string", "maxLength": 4000},
        "query": {"type": "string", "maxLength": 256},
        "summary": {"type": "string", "maxLength": 1000},
    },
    "required": ["tool"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a Rust coding agent. Emit exactly one action as a JSON object. "
    "Fields: tool (one of read|search|edit|check|finish); path (a file path); "
    "search (text that already exists in the file, matched exactly); "
    "replace (the text to put in its place); query (a symbol to look up); "
    "summary (a closing note). Only include the fields the chosen tool needs."
)

# Rust-shaped filler, so token counts are representative of real context.
UNIT = """pub struct Widget%d { pub id: u64, pub name: String, pub parts: Vec<Part> }
impl Widget%d {
    pub fn new(id: u64, name: impl Into<String>) -> Self { Self { id, name: name.into(), parts: Vec::new() } }
    pub fn validate(&self) -> Result<(), WidgetError> {
        if self.name.is_empty() { return Err(WidgetError::EmptyName); }
        for p in &self.parts { p.validate()?; }
        Ok(())
    }
}
"""


def filler(n: int) -> str:
    return "\n".join(UNIT % (i, i) for i in range(n))


def post(host, path, payload, timeout=3600):
    req = urllib.request.Request(
        host + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def rates(o):
    decode = prefill = None
    if o.get("eval_count") and o.get("eval_duration"):
        decode = o["eval_count"] / (o["eval_duration"] / 1e9)
    if o.get("prompt_eval_count") and o.get("prompt_eval_duration"):
        prefill = o["prompt_eval_count"] / (o["prompt_eval_duration"] / 1e9)
    return prefill, decode


def chat(host, model, messages, **kw):
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        # The model advertises a thinking capability. Left alone it will
        # deliberate for hundreds of tokens before saying anything, which at
        # local throughput is a minute of silence per turn.
        "think": False,
        # Cold load is measured in tens of seconds. A model unloaded between
        # turns makes every first turn a bad one.
        "keep_alive": "30m",
        "options": {
            "temperature": 0.3,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.05,
            "num_ctx": 32768,
        },
    }
    for k, v in kw.items():
        if k in ("format", "tools"):
            payload[k] = v
        else:
            payload["options"][k] = v
    return post(host, "/api/chat", payload)


def show(host, model):
    print("=== model identity ===", flush=True)
    d = post(host, "/api/show", {"model": model}, timeout=60)
    mi = d.get("model_info", {})
    arch = mi.get("general.architecture", "?")
    interesting = [
        "general.base_model.0.name",
        "general.finetune",
        "general.file_type",
        "general.parameter_count",
        f"{arch}.block_count",
        f"{arch}.expert_count",
        f"{arch}.expert_used_count",
        f"{arch}.context_length",
        f"{arch}.full_attention_interval",
        f"{arch}.ssm.state_size",
    ]
    print(f"  architecture     = {arch}")
    for k in interesting:
        if k in mi:
            print(f"  {k:32s} = {mi[k]}")
    print(f"  capabilities     = {d.get('capabilities')}")
    tmpl = d.get("template") or ""
    fmt = "qwen-xml" if "<tool_call>" in tmpl and "<function=" in tmpl else (
        "hermes-json" if "tool_call" in tmpl else "none")
    print(f"  tool format in template = {fmt}")
    print(flush=True)


def probe_schema(host, model):
    print("=== schema-constrained emission ===", flush=True)
    t0 = time.time()
    o = chat(host, model,
             [{"role": "system", "content": SYSTEM},
              {"role": "user", "content": "In src/lib.rs, rename the function `foo` to `bar`. Emit the edit action."}],
             format=ACTION_SCHEMA, num_predict=768)
    dt = time.time() - t0
    content = (o.get("message") or {}).get("content") or ""
    prefill, decode = rates(o)
    ok = True
    try:
        parsed = json.loads(content)
    except Exception as e:
        ok, parsed = False, str(e)
    print(f"  wall             = {dt:.1f}s")
    print(f"  load_duration    = {o.get('load_duration', 0)/1e9:.1f}s")
    print(f"  prompt/eval      = {o.get('prompt_eval_count')} / {o.get('eval_count')}")
    print(f"  decode           = {decode or -1:.2f} tok/s")
    print(f"  valid JSON       = {ok}")
    print(f"  action           = {json.dumps(parsed)[:400] if ok else parsed}")
    print(flush=True)


def probe_prefill(host, model, sizes):
    print("=== prefill scaling (num_predict=1) ===", flush=True)
    for units in sizes:
        body = filler(units)
        t0 = time.time()
        o = chat(host, model,
                 [{"role": "system", "content": "You are a Rust coding agent."},
                  {"role": "user", "content": "Here is context:\n" + body + "\nReply with the single word OK."}],
                 num_predict=1)
        dt = time.time() - t0
        prefill, _ = rates(o)
        print(f"  units={units:4d} prompt_tokens={o.get('prompt_eval_count'):6} "
              f"wall={dt:7.1f}s prefill={prefill or -1:7.1f} tok/s", flush=True)
    print(flush=True)


def probe_prefix_cache(host, model, units=40):
    """Q1. Ollama reports prompt_eval_count for tokens it actually computed, so a
    cache hit shows up as a collapsed count and duration on the second call."""
    print("=== prefix cache reuse (Q1) ===", flush=True)
    body = filler(units)
    msgs = [{"role": "system", "content": "You are a Rust coding agent."},
            {"role": "user", "content": "Here is context:\n" + body + "\nReply with the single word OK."}]

    t0 = time.time(); o = chat(host, model, msgs, num_predict=1); dt1 = time.time() - t0
    print(f"  cold      prompt_eval_count={o.get('prompt_eval_count'):6} "
          f"prefill={o.get('prompt_eval_duration',0)/1e9:6.1f}s wall={dt1:6.1f}s", flush=True)

    t0 = time.time(); o = chat(host, model, msgs, num_predict=1); dt2 = time.time() - t0
    print(f"  identical prompt_eval_count={o.get('prompt_eval_count'):6} "
          f"prefill={o.get('prompt_eval_duration',0)/1e9:6.1f}s wall={dt2:6.1f}s", flush=True)

    msgs2 = [msgs[0], {"role": "user", "content": msgs[1]["content"] + "\nAlso: be terse."}]
    t0 = time.time(); o = chat(host, model, msgs2, num_predict=1); dt3 = time.time() - t0
    print(f"  extended  prompt_eval_count={o.get('prompt_eval_count'):6} "
          f"prefill={o.get('prompt_eval_duration',0)/1e9:6.1f}s wall={dt3:6.1f}s", flush=True)
    print(flush=True)


def probe_native_tools(host, model):
    print("=== native tool calling ===", flush=True)
    tools = [
        {"type": "function", "function": {
            "name": "read", "description": "Read a file slice",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
                "required": ["path"]}}},
        {"type": "function", "function": {
            "name": "search", "description": "Look up a symbol in the index",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                           "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "check", "description": "Run cargo check",
            "parameters": {"type": "object", "properties": {}, "required": []}}},
    ]
    o = chat(host, model,
             [{"role": "system", "content": "You are a Rust coding agent. Use one tool per turn."},
              {"role": "user", "content": "Find where the function `validate` is defined."}],
             tools=tools, num_predict=400)
    m = o.get("message") or {}
    print(f"  tool_calls = {json.dumps(m.get('tool_calls'))[:400]}")
    print(f"  content    = {(m.get('content') or '')[:400]!r}")
    print(flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--quick", action="store_true", help="skip the large prefill cases")
    a = ap.parse_args()

    print(f"target: {a.model} @ {a.host}\n", flush=True)
    show(a.host, a.model)
    probe_schema(a.host, a.model)
    probe_prefill(a.host, a.model, [40] if a.quick else [40, 120, 260])
    probe_prefix_cache(a.host, a.model)
    probe_native_tools(a.host, a.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
