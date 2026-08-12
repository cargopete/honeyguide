#!/usr/bin/env python3
"""Clean, repeated benchmark of the local models. Post-reboot re-measurement.

Written after an earlier round of figures turned out not to reproduce: the host
rebooted mid-session and Ollama began offloading part of the model to an 8GB
RTX 2000 Ada, which moved prefill by roughly 6x. Everything here therefore
repeats each measurement and reports all trials rather than one sample.

Method notes, all of them learned the hard way:

  - Warm the model first. Ollama can fold model load time into
    prompt_eval_duration, which is what makes an unwarmed sample look like a
    catastrophically slow machine.
  - Use FRESH filler content for every prefill trial. Reusing the same body
    silently measures the KV prefix cache instead of prefill.
  - Report every trial. A single sample of anything here is worthless.
"""

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request

HOST = "http://pepe-thinkpad:11434"

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

UNIT = "pub struct W%d { pub id: u64, pub name: String }\nimpl W%d { pub fn v(&self) -> bool { !self.name.is_empty() } }\n"


def filler(n, salt):
    """Fresh content per call, so nothing can be served from the prefix cache."""
    return "\n".join(UNIT % (i + salt * 100000, i + salt * 100000) for i in range(n))


def post(host, payload, timeout=3600):
    req = urllib.request.Request(host + "/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def chat(host, model, messages, schema=None, num_predict=256, num_ctx=32768):
    p = {"model": model, "messages": messages, "stream": False, "think": False,
         "keep_alive": "30m",
         "options": {"temperature": 0.3, "top_p": 0.95, "top_k": 20, "min_p": 0.05,
                     "num_ctx": num_ctx, "num_predict": num_predict}}
    if schema:
        p["format"] = schema
    try:
        return post(host, p)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        p.pop("think", None)
        return post(host, p)


def pf(o):
    return o.get("prompt_eval_duration", 0) / 1e9


def dc(o):
    return o.get("eval_duration", 0) / 1e9


def bench(host, model, sys_prompt, schema, trials):
    print(f"\n### {model}", flush=True)
    r = {"model": model}
    chat(host, model, [{"role": "user", "content": "hi"}], num_predict=1)

    # prefill, fresh content each trial
    rates, dec = [], []
    for t in range(trials):
        body = filler(120, salt=t + 1)
        o = chat(host, model, [{"role": "user", "content": body + "\nReply OK."}], num_predict=1)
        n = o.get("prompt_eval_count") or 0
        rates.append(n / max(pf(o), 1e-9))
        print(f"  prefill trial {t+1}: {n} tok in {pf(o):.2f}s = {rates[-1]:.0f} tok/s", flush=True)
    r["prefill_tok_s"] = rates

    # decode, short prompt so prefill does not dominate
    for t in range(trials):
        o = chat(host, model, [{"role": "user", "content": f"Count from {t*10} to {t*10+40}, numbers only."}],
                 num_predict=120)
        dec.append((o.get("eval_count") or 0) / max(dc(o), 1e-9))
        print(f"  decode  trial {t+1}: {dec[-1]:.2f} tok/s", flush=True)
    r["decode_tok_s"] = dec

    # incremental prefix reuse: A fresh, then A+suffix. Compare against the
    # fresh-prefill rate above: reuse shows up as a suffix costing far less than
    # a same-sized fresh prompt would.
    reuse = []
    for t in range(trials):
        body = filler(120, salt=100 + t)
        base = [{"role": "user", "content": body + "\nReply OK."}]
        o1 = chat(host, model, base, num_predict=1)
        ext = [{"role": "user", "content": body + "\nReply OK.\nAlso be terse."}]
        o2 = chat(host, model, ext, num_predict=1)
        n = o2.get("prompt_eval_count") or 0
        expected_fresh = n / statistics.median(rates)
        reuse.append({"fresh_s": round(pf(o1), 2), "extended_s": round(pf(o2), 2),
                      "expected_if_no_reuse_s": round(expected_fresh, 2),
                      "reused": pf(o2) < expected_fresh * 0.3})
        print(f"  reuse   trial {t+1}: fresh {pf(o1):.2f}s  extended {pf(o2):.2f}s  "
              f"(a fresh prompt of that size would cost ~{expected_fresh:.2f}s) -> reused={reuse[-1]['reused']}",
              flush=True)
    r["reuse"] = reuse

    # six-turn loop, the number a user actually feels
    totals = []
    for rep in range(2):
        convo = [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": "Context:\n" + filler(60, salt=200 + rep) +
                  "\nTask: rename `area` to `surface` in src/geometry.rs."}]
        tot = 0.0
        for _ in range(6):
            t0 = time.time()
            o = chat(host, model, convo, schema=schema, num_predict=150)
            tot += time.time() - t0
            convo.append({"role": "assistant", "content": (o.get("message") or {}).get("content") or ""})
            convo.append({"role": "user", "content": OBS})
        totals.append(round(tot, 1))
        print(f"  six-turn rep {rep+1}: {tot:.1f}s", flush=True)
    r["six_turn_s"] = totals

    # action quality, repeated: does it emit a complete, correct edit
    ok = []
    for t in range(trials):
        msgs = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": "Task: rename the method `area` to `surface` in src/geometry.rs."},
                {"role": "assistant", "content": json.dumps(
                    {"reasoning": "I must read the file before editing it.", "tool": "read",
                     "path": "src/geometry.rs", "query": "", "search": "", "replace": "", "summary": ""})},
                {"role": "user", "content": OBS}]
        o = chat(host, model, msgs, schema=schema, num_predict=600)
        try:
            a = json.loads((o.get("message") or {}).get("content") or "")
            s, rep_ = a.get("search") or "", a.get("replace") or ""
            ok.append({"tool": a.get("tool"), "complete": bool(s and rep_),
                       "correct": "area" in s and "surface" in rep_})
        except Exception as e:
            ok.append({"error": str(e)[:80]})
        print(f"  action  trial {t+1}: {ok[-1]}", flush=True)
    r["action"] = ok
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--models", nargs="+", default=["heretic:latest", "qwen3-coder:30b"])
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--prompt", default="prompts/system.md")
    ap.add_argument("--schema", default="prompts/action-schema.json")
    a = ap.parse_args()
    sysp, schema = open(a.prompt).read(), json.load(open(a.schema))
    res = [bench(a.host, m, sysp, schema, a.trials) for m in a.models]

    print("\n" + "=" * 74)
    print(f"{'':24s}" + "".join(f"{x['model']:>24s}" for x in res))
    def row(label, fn):
        print(f"{label:24s}" + "".join(f"{fn(x):>24s}" for x in res))
    row("prefill tok/s (median)", lambda x: f"{statistics.median(x['prefill_tok_s']):.0f}")
    row("decode tok/s (median)", lambda x: f"{statistics.median(x['decode_tok_s']):.2f}")
    row("incremental reuse", lambda x: str(sum(t['reused'] for t in x['reuse'])) + f"/{len(x['reuse'])}")
    row("six-turn s", lambda x: " / ".join(str(v) for v in x['six_turn_s']))
    row("edit complete+correct", lambda x: str(sum(1 for t in x['action'] if t.get('complete') and t.get('correct'))) + f"/{len(x['action'])}")
    print("=" * 74)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
