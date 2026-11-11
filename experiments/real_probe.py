"""Probes against the REAL Groq API. Every probe prints its planned cost and does nothing
unless --yes is given. Every attempt is logged (headers included) to results/real_probe/.

    python -m experiments.real_probe budget
    python -m experiments.real_probe smoke --yes
    python -m experiments.real_probe latency --per-size 8 --yes      # R1
    python -m experiments.real_probe rpm --yes                       # R2
    python -m experiments.real_probe tpm --yes                       # R3
    python -m experiments.real_probe concurrency --n 8 --yes         # R4
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import pymupdf

from app.config import PAPERS_DIR, RESULTS_DIR, load_settings
from app.providers.base import ProviderError, RATE_LIMITED
from app.providers.openai_compat import SPEND_LEDGER, OpenAICompatProvider

PROBE_DIR = RESULTS_DIR / "real_probe"
TINY = [{"role": "user", "content": "Reply with the single word OK."}]
CHARS_PER_TOKEN = 4  # rough planning estimate only; real counts come from the API's usage block


def filler_text(approx_tokens: int) -> str:
    """Real paper text (Attention, from p.2) so prompts look like our pipeline's prompts."""
    doc = pymupdf.open(PAPERS_DIR / "1706.03762v7.pdf")
    text = " ".join(doc[i].get_text() for i in range(1, 9))
    return " ".join(text.split())[: approx_tokens * CHARS_PER_TOKEN]


def passage_messages(approx_tokens: int) -> list[dict]:
    return [
        {"role": "system", "content": "Answer briefly using only the passage."},
        {"role": "user", "content": f"Passage:\n{filler_text(approx_tokens)}\n\n"
                                    "Question: In two sentences, what is this passage about?"},
    ]


class Log:
    def __init__(self, probe: str):
        PROBE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = PROBE_DIR / f"{probe}_{stamp}.jsonl"
        self.probe = probe
        self.seq = 0

    def write(self, result, **extra):
        self.seq += 1
        row = {"probe": self.probe, "seq": self.seq,
               "utc": datetime.now(timezone.utc).isoformat(), **extra, **result.to_log()}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return row


def short(r) -> str:
    rl = r.rate_limit
    return (f"status={r.status_code} kind={r.error_kind or 'ok'} limit={r.limit_kind} "
            f"ttft={_ms(r.first_token_ms)} total={_ms(r.total_ms)} in={r.tokens_in} out={r.tokens_out} "
            f"rem_req_day={rl.remaining_requests_day} rem_tok_min={rl.remaining_tokens_minute} "
            f"retry_after={rl.retry_after_s}")


def _ms(v):
    return "-" if v is None else f"{v:.0f}ms"


async def one(p: OpenAICompatProvider, messages, max_tokens: int, timeout_s: float = 60.0):
    call = p.stream_chat(messages, max_tokens=max_tokens, timeout_s=timeout_s)
    try:
        async for _ in call:
            pass
    except ProviderError:
        pass
    return call.result


def confirm(args, requests: int, tokens: int) -> bool:
    print(f"[plan] {args.cmd}: up to ~{requests} requests, ~{tokens} tokens on the REAL Groq account.")
    print_budget()
    if not args.yes:
        print("[plan] dry run. Re-run with --yes to send.")
        return False
    return True


def print_budget():
    if not SPEND_LEDGER.exists():
        print("[budget] no real calls recorded yet.")
        return
    now = datetime.now(timezone.utc)
    day = now.date()
    since = now - timedelta(hours=24)
    rows = [json.loads(line) for line in SPEND_LEDGER.read_text().splitlines() if line.strip()]
    def agg(sel):
        sel = list(sel)
        toks = sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in sel)
        return len(sel), toks
    n_day, t_day = agg(r for r in rows if datetime.fromisoformat(r["utc"]).date() == day)
    n_24, t_24 = agg(r for r in rows if datetime.fromisoformat(r["utc"]) >= since)
    print(f"[budget] UTC today: {n_day} requests, {t_day} tokens | last 24h: {n_24} requests, {t_24} tokens"
          f" (documented free tier: 1000 RPD, 200000 TPD)")


# --- probes ------------------------------------------------------------------------------

async def smoke(p, args):
    if not confirm(args, 1, 100):
        return
    log = Log("smoke")
    r = await one(p, TINY, 16)
    log.write(r)
    print(short(r))
    print("headers:", json.dumps({k: v for k, v in r.raw_headers.items()
                                  if k.startswith("x-") or k == "retry-after"}, indent=1))


async def latency(p, args):
    sizes = [300, 1500, 3000]
    max_out = 300
    planned = sum((s + max_out) * args.per_size for s in sizes)
    if not confirm(args, args.per_size * len(sizes), planned):
        return
    log = Log("latency")
    window: deque[tuple[float, int]] = deque()
    cap = 0.7 * p.settings.tpm_limit
    messages = {s: passage_messages(s) for s in sizes}
    for i in range(args.per_size):
        for s in sizes:  # round-robin so time-of-day drift affects all sizes equally
            est = s + max_out
            while True:
                now = time.monotonic()
                while window and now - window[0][0] > 60:
                    window.popleft()
                if sum(t for _, t in window) + est <= cap:
                    break
                await asyncio.sleep(1.0)
            window.append((time.monotonic(), est))
            r = await one(p, messages[s], max_out)
            log.write(r, prompt_size_target=s, round=i)
            print(f"size={s:<5} round={i} {short(r)}")
            if r.error_kind == RATE_LIMITED and r.rate_limit.retry_after_s:
                await asyncio.sleep(r.rate_limit.retry_after_s)
    print("log:", log.path)


async def _recovery(p, log, messages, max_tokens, every_s: float, max_polls: int, t_first_429: float):
    for k in range(max_polls):
        await asyncio.sleep(every_s)
        r = await one(p, messages, max_tokens)
        log.write(r, phase="recovery", since_first_429_s=time.monotonic() - t_first_429)
        print(f"recovery poll {k}: {short(r)}")
        if r.ok:
            print(f"recovered {time.monotonic() - t_first_429:.1f}s after the first 429")
            return
    print("did not recover within the polling window")


async def rpm(p, args):
    if not confirm(args, args.max + 30, (args.max + 30) * 90):
        return
    log = Log("rpm")
    t_first_429 = None
    n_429 = 0
    for i in range(args.max):
        t0 = time.monotonic()
        r = await one(p, TINY, 16)
        log.write(r, phase="burst", interval_s=args.interval)
        print(f"#{i:<3} {short(r)}")
        if r.error_kind == RATE_LIMITED:
            n_429 += 1
            t_first_429 = t_first_429 or r.t_start
            if n_429 >= 3:
                break
        await asyncio.sleep(max(0.0, args.interval - (time.monotonic() - t0)))
    if t_first_429 is None:
        print("no 429 reached")
        return
    await _recovery(p, log, TINY, 16, every_s=3.0, max_polls=30, t_first_429=t_first_429)
    print("log:", log.path)


async def tpm(p, args):
    msgs = passage_messages(2000)
    if not confirm(args, args.max + 30, (args.max + 6) * 2100):
        return
    log = Log("tpm")
    t_first_429 = None
    for i in range(args.max):
        r = await one(p, msgs, 50)
        log.write(r, phase="burst")
        print(f"#{i:<3} {short(r)}")
        if r.error_kind == RATE_LIMITED:
            t_first_429 = r.t_start
            break
    if t_first_429 is None:
        print("no 429 reached")
        return
    await _recovery(p, log, msgs, 50, every_s=3.0, max_polls=30, t_first_429=t_first_429)
    print("log:", log.path)


async def concurrency(p, args):
    if not confirm(args, args.n, args.n * 90):
        return
    log = Log("concurrency")
    results = await asyncio.gather(*(one(p, TINY, 16) for _ in range(args.n)))
    t0 = min(r.t_start for r in results)
    for r in sorted(results, key=lambda r: r.t_start):
        log.write(r, n=args.n)
        print(f"start+{(r.t_start - t0) * 1000:.0f}ms headers+{_ms((r.t_headers - r.t_start) * 1000 if r.t_headers else None)} {short(r)}")
    print("log:", log.path)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["budget", "smoke", "latency", "rpm", "tpm", "concurrency"])
    ap.add_argument("--yes", action="store_true", help="actually send requests")
    ap.add_argument("--per-size", type=int, default=8)
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--n", type=int, default=8)
    args = ap.parse_args()
    if args.cmd == "budget":
        print_budget()
        return
    if args.cmd == "tpm" and args.max == 40:
        args.max = 8
    settings = load_settings()
    if not args.yes and not settings.groq_api_key:
        settings = load_settings(groq_api_key="dry-run")  # dry runs never send a request
    p = OpenAICompatProvider.groq(settings)
    try:
        await {"smoke": smoke, "latency": latency, "rpm": rpm, "tpm": tpm,
               "concurrency": concurrency}[args.cmd](p, args)
    finally:
        await p.aclose()


if __name__ == "__main__":
    asyncio.run(main())
