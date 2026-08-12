#!/usr/bin/env python3
"""A/B two local models on the things that decide an agent loop.

RFC-0001 Q4 and Q5. The question is not which model writes better Rust. It is
whether a hybrid SSM MoE can be driven in a ReAct loop at all, given that it
recomputes its whole prompt on every turn while a pure-attention model of the
same active size reuses it.

Measures, per model:

  1. decode tok/s
  2. prefill tok/s at realistic context
  3. prefix cache: cold / identical / extended-by-a-few-tokens   <- the decisive one
  4. schema-constrained action emission: is the edit complete and correct
  5. simulated multi-turn wall clock: what a 6-turn task actually costs

(5) is the headline. Everything else explains it.

Usage:
    python3 scripts/model-ab.py --a heretic:latest --b qwen3-coder:30b
"""

import argparse
import json
import time
import urllib.error
import urllib.request

HOST = "http://pepe-thinkpad:11434"

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

OBS = """read src/geometry.rs lines 1-10
   1| pub struct Rect {
   2|     pub w: f64,
   3|     pub h: f64,
   4| }
   5|
   6| impl Rect {
   7|     pub fn area(&self) -> f64 {
   8|         self.w * self.h
   9|     }
  10| }
"""


def filler(n):
    return "\n".join(UNIT % (i, i) for i in range(n))


def post(host, payload, timeout=3600):
    req = urllib.request.Request(
        host + "/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def chat(host, model, messages, schema=None, num_predict=256, num_ctx=32768):
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        # Only meaningful for models that declare a thinking capability. Sending
        # it to one that does not can be rejected, so we retry without it.
        "think": False,
        "keep_alive": "30m",
        "options": {
            "temperature": 0.3, "top_p": 0.95, "top_k": 20, "min_p": 0.05,
            "num_ctx": num_ctx, "num_predict": num_predict,
        },
    }
    if schema:
        payload["format"] = schema
    try:
        return post(host, payload)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        payload.pop("think", None)
        return post(host, payload)


def prefill_s(o):
    return o.get("prompt_eval_duration", 0) / 1e9


def decode_s(o):
    return o.get("eval_duration", 0) / 1e9


def bench(host, model, sys_prompt, schema):
    r = {"model": model}
    print(f"\n### {model}", flush=True)

    # warm the weights so load time does not pollute the first measurement
    chat(host, model, [{"role": "user", "content": "hi"}], num_predict=1)

    # 1+2: decode and prefill at ~5k
    body = filler(40)
    msgs = [{"role": "system", "content": "You are a Rust coding agent."},
            {"role": "user", "content": "Context:\n" + body + "\nWrite two sentences about Rust ownership."}]
    o = chat(host, model, msgs, num_predict=120)
    r["prompt_tokens"] = o.get("prompt_eval_count")
    r["prefill_toks"] = (o.get("prompt_eval_count") or 0) / max(prefill_s(o), 1e-9)
    r["decode_toks"] = (o.get("eval_count") or 0) / max(decode_s(o), 1e-9)
    print(f"  prefill {r['prefill_toks']:7.1f} tok/s   decode {r['decode_toks']:6.2f} tok/s", flush=True)

    # 3: the decisive one
    probe = [{"role": "system", "content": "You are a Rust coding agent."},
             {"role": "user", "content": "Context:\n" + body + "\nReply with the single word OK."}]
    o1 = chat(host, model, probe, num_predict=1)
    o2 = chat(host, model, probe, num_predict=1)
    probe2 = [probe[0], {"role": "user", "content": probe[1]["content"] + "\nAlso: be terse."}]
    o3 = chat(host, model, probe2, num_predict=1)
    r["cold_s"], r["identical_s"], r["extended_s"] = prefill_s(o1), prefill_s(o2), prefill_s(o3)
    r["incremental_reuse"] = r["extended_s"] < r["cold_s"] * 0.25
    print(f"  cache: cold {r['cold_s']:6.1f}s  identical {r['identical_s']:5.1f}s  "
          f"extended {r['extended_s']:6.1f}s  -> incremental reuse: {r['incremental_reuse']}", flush=True)

    # 4: does it emit a complete, correct edit
    msgs = [{"role": "system", "content": sys_prompt},
            {"role": "user", "content": "Task: rename the method `area` to `surface` in src/geometry.rs."},
            {"role": "assistant", "content": json.dumps(
                {"reasoning": "I must read the file before editing it.", "tool": "read",
                 "path": "src/geometry.rs", "query": "", "search": "", "replace": "", "summary": ""})},
            {"role": "user", "content": OBS}]
    o = chat(host, model, msgs, schema=schema, num_predict=600)
    try:
        a = json.loads((o.get("message") or {}).get("content") or "")
        r["action_valid"] = True
        r["action_tool"] = a.get("tool")
        s, rep = a.get("search") or "", a.get("replace") or ""
        r["edit_complete"] = bool(s and rep)
        r["edit_correct"] = "area" in s and "surface" in rep
        r["gutter_leaked"] = "|" in s
        r["action_tokens"] = o.get("eval_count")
    except Exception as e:
        r["action_valid"] = False
        r["action_error"] = str(e)[:120]
    print(f"  action: valid={r.get('action_valid')} tool={r.get('action_tool')} "
          f"complete={r.get('edit_complete')} correct={r.get('edit_correct')} "
          f"gutter_leaked={r.get('gutter_leaked')} tokens={r.get('action_tokens')}", flush=True)

    # 5: the headline. Six turns over a prompt that grows by one observation each time.
    convo = [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": "Context:\n" + filler(25) + "\nTask: rename `area` to `surface`."}]
    total = 0.0
    for turn in range(6):
        t0 = time.time()
        o = chat(host, model, convo, schema=schema, num_predict=150)
        dt = time.time() - t0
        total += dt
        content = (o.get("message") or {}).get("content") or ""
        convo.append({"role": "assistant", "content": content})
        convo.append({"role": "user", "content": OBS})
        print(f"    turn {turn+1}: {dt:6.1f}s  (prefill {prefill_s(o):6.1f}s)", flush=True)
    r["six_turn_s"] = total
    print(f"  SIX-TURN TOTAL: {total:.1f}s  ({total/60:.1f} min)", flush=True)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--a", default="heretic:latest")
    ap.add_argument("--b", default="qwen3-coder:30b")
    ap.add_argument("--prompt", default="prompts/system.md")
    ap.add_argument("--schema", default="prompts/action-schema.json")
    args = ap.parse_args()

    sys_prompt = open(args.prompt).read()
    schema = json.load(open(args.schema))

    results = [bench(args.host, m, sys_prompt, schema) for m in (args.a, args.b)]

    print("\n" + "=" * 72)
    print(f"{'':22s} {results[0]['model']:>22s} {results[1]['model']:>22s}")
    rows = [
        ("decode tok/s", "decode_toks", "{:.2f}"),
        ("prefill tok/s", "prefill_toks", "{:.1f}"),
        ("cold prefill s", "cold_s", "{:.1f}"),
        ("extended prefill s", "extended_s", "{:.1f}"),
        ("incremental reuse", "incremental_reuse", "{}"),
        ("edit complete", "edit_complete", "{}"),
        ("edit correct", "edit_correct", "{}"),
        ("SIX-TURN TOTAL s", "six_turn_s", "{:.1f}"),
    ]
    for label, key, fmt in rows:
        a = results[0].get(key)
        b = results[1].get(key)
        fa = fmt.format(a) if a is not None else "-"
        fb = fmt.format(b) if b is not None else "-"
        print(f"{label:22s} {fa:>22s} {fb:>22s}")
    print("=" * 72)
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
