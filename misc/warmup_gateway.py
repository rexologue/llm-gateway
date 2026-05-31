#!/usr/bin/env python3
"""Warm up an OpenAI-compatible chat endpoint with unique request ids."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class Result:
    """One warmup request result."""

    ok: bool
    ttft: float | None
    e2e: float | None
    chunks: int
    error: str | None = None


def build_system_prompt(target_tokens: int) -> str:
    """Build a long stable system prompt to warm backend prefix/cache logic."""

    base = (
        "You are a voice AI operator in a call center. "
        "Answer briefly and naturally without markdown, lists, or technical explanations. "
        "Preserve dialogue context, ask for missing data, and do not invent facts. "
    )
    words_needed = max(100, int(target_tokens * 0.8))
    filler = " ".join(["instruction"] * words_needed)
    return base + filler


def build_messages(system_prompt: str, i: int) -> list[dict[str, str]]:
    """Build chat messages with a stable system prefix and varied user text."""

    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"Hello, briefly tell me what you offer. Request number {i}.",
        },
    ]


def session_id_for_request(args: argparse.Namespace, idx: int) -> str | None:
    """Return the session id that should be sent for one request."""

    if not args.session_id:
        return None
    if args.session_id_mode == "per-request":
        return f"{args.session_id}-{idx}"
    return args.session_id


def request_id_prefix_for_run(args: argparse.Namespace) -> str:
    """Return the request id prefix used by this script run."""

    if args.request_id_prefix:
        return args.request_id_prefix
    return f"warmup-{uuid.uuid4().hex[:12]}"


def request_id_for_request(prefix: str, idx: int) -> str:
    """Return a unique request id for one request."""

    return f"{prefix}-{idx}"


def headers_for_request(
    base_headers: dict[str, str],
    request_header: str,
    request_id: str,
    session_header: str,
    session_id: str | None,
) -> dict[str, str]:
    """Return request headers with request and optional session ids."""

    headers = dict(base_headers)
    headers[request_header] = request_id

    if session_id:
        headers[session_header] = session_id

    return headers


async def one_request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    model: str,
    system_prompt: str,
    idx: int,
    max_tokens: int,
    temperature: float,
    disable_thinking: bool,
    timeout_sec: float,
) -> Result:
    """Send one streaming chat completion request and measure TTFT/E2E."""

    payload: dict[str, Any] = {
        "model": model,
        "messages": build_messages(system_prompt, idx),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }

    if disable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    started = time.perf_counter()
    ttft: float | None = None
    chunks = 0

    try:
        async with client.stream(
            "POST",
            url,
            headers=headers,
            json=payload,
            timeout=timeout_sec,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                body_text = body.decode("utf-8", errors="ignore")
                return Result(
                    False,
                    None,
                    None,
                    0,
                    f"HTTP {resp.status_code}: {body_text[:500]}",
                )

            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue

                if not line.startswith("data:"):
                    continue

                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    continue

                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue

                delta = obj.get("choices", [{}])[0].get("delta", {})
                text = delta.get("content") or delta.get("reasoning_content") or ""

                if text:
                    chunks += 1
                    if ttft is None:
                        ttft = time.perf_counter() - started

            e2e = time.perf_counter() - started
            return Result(True, ttft, e2e, chunks)

    except Exception as exc:
        return Result(False, None, None, chunks, repr(exc))


async def run(args: argparse.Namespace) -> None:
    """Run the configured warmup workload and print a compact summary."""

    url = args.base_url.rstrip("/") + "/chat/completions"
    base_headers = {"Content-Type": "application/json"}

    if args.api_key:
        base_headers["Authorization"] = f"Bearer {args.api_key}"

    system_prompt = build_system_prompt(args.prompt_tokens)
    request_id_prefix = request_id_prefix_for_run(args)

    limits = httpx.Limits(
        max_connections=args.concurrency,
        max_keepalive_connections=args.concurrency,
    )
    timeout = httpx.Timeout(args.timeout)

    async with httpx.AsyncClient(
        limits=limits,
        timeout=timeout,
        trust_env=False,
    ) as client:
        sem = asyncio.Semaphore(args.concurrency)
        next_id = 0
        results: list[Result] = []

        async def worker(i: int) -> None:
            """Run one bounded-concurrency request and collect its result."""

            request_id = request_id_for_request(request_id_prefix, i)
            request_session_id = session_id_for_request(args, i)
            request_headers = headers_for_request(
                base_headers,
                args.request_header,
                request_id,
                args.session_header,
                request_session_id,
            )

            async with sem:
                result = await one_request(
                    client=client,
                    url=url,
                    headers=request_headers,
                    model=args.model,
                    system_prompt=system_prompt,
                    idx=i,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                    disable_thinking=args.disable_thinking,
                    timeout_sec=args.timeout,
                )
                results.append(result)

        print(f"Warmup endpoint: {url}")
        print(f"Model:           {args.model}")
        print(f"Requests:        {args.requests}")
        print(f"Concurrency:     {args.concurrency}")
        print(f"Prompt tokens:   ~{args.prompt_tokens}")
        print(f"Max tokens:      {args.max_tokens}")
        print(f"Request header:  {args.request_header}")
        print(f"Request prefix:  {request_id_prefix}")
        print(f"Session header:  {args.session_header}")
        print(f"Session id:      {args.session_id or 'disabled'}")
        print(f"Session mode:    {args.session_id_mode if args.session_id else 'none'}")
        print()

        started = time.perf_counter()
        tasks: set[asyncio.Task[None]] = set()

        while next_id < args.requests or tasks:
            while next_id < args.requests and len(tasks) < args.concurrency:
                task = asyncio.create_task(worker(next_id))
                tasks.add(task)
                next_id += 1

            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()

            ok = sum(1 for result in results if result.ok)
            bad = len(results) - ok
            print(
                f"\rcompleted={len(results)}/{args.requests} ok={ok} errors={bad}",
                end="",
                flush=True,
            )

        total = time.perf_counter() - started
        print()
        print()

    ok_results = [result for result in results if result.ok]
    errors = [result for result in results if not result.ok]

    ttfts = [result.ttft for result in ok_results if result.ttft is not None]
    e2es = [result.e2e for result in ok_results if result.e2e is not None]

    def q(values: list[float], p: float) -> float | None:
        """Return a simple nearest-rank quantile."""

        if not values:
            return None
        sorted_values = sorted(values)
        idx = min(len(sorted_values) - 1, max(0, round((len(sorted_values) - 1) * p)))
        return sorted_values[idx]

    print("Summary")
    print(f"  total_sec:      {total:.3f}")
    print(f"  ok:             {len(ok_results)}")
    print(f"  errors:         {len(errors)}")
    if total > 0:
        print(f"  req_per_sec:    {len(ok_results) / total:.3f}")
    else:
        print("  req_per_sec:    n/a")

    if ttfts:
        print(f"  ttft_p50_sec:   {statistics.median(ttfts):.3f}")
        print(f"  ttft_p95_sec:   {q(ttfts, 0.95):.3f}")
        print(f"  ttft_p99_sec:   {q(ttfts, 0.99):.3f}")

    if e2es:
        print(f"  e2e_p50_sec:    {statistics.median(e2es):.3f}")
        print(f"  e2e_p95_sec:    {q(e2es, 0.95):.3f}")
        print(f"  e2e_p99_sec:    {q(e2es, 0.99):.3f}")

    if errors:
        print()
        print("First errors:")
        for result in errors[:5]:
            print(f"  - {result.error}")


def main() -> None:
    """Parse CLI arguments and run the async warmup workload."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:9090/v1")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--model", default="calls-model")
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--prompt-tokens", type=int, default=7000)
    parser.add_argument("--max-tokens", type=int, default=32)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--disable-thinking", action="store_true")
    parser.add_argument(
        "--request-id-prefix",
        default="",
        help="Base request id prefix. Empty generates a unique prefix for this run.",
    )
    parser.add_argument(
        "--request-header",
        default="X-Request-ID",
        help="HTTP header used by the gateway for request id propagation.",
    )
    parser.add_argument(
        "--session-id",
        default="",
        help="Base session id to send in the session header. Empty disables session headers.",
    )
    parser.add_argument(
        "--session-id-mode",
        choices=("fixed", "per-request"),
        default="fixed",
        help="Use one shared session id or append the request index for one session per request.",
    )
    parser.add_argument(
        "--session-header",
        default="X-Session-ID",
        help="HTTP header used by the gateway for session tracking.",
    )
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
