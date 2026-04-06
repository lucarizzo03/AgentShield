import asyncio
import random
import statistics
import string
import time
from typing import Any, Dict, List, Tuple

import httpx

BASE_URL = "http://127.0.0.1:8000"
AGENTS = ["agent_alpha", "agent_beta", "agent_ops"]
VENDORS = [
    "https://fal.mpp.tempo.xyz/fal-ai/flux/dev",
    "https://aviationstack.mpp.tempo.xyz/v1/aircraft_types",
]
TASKS = [
    "a sunset over the ocean",
    "city skyline at dusk",
    "mountain landscape photo",
]


def _challenge_id() -> str:
    return "ch_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=18))


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * p
    lo = int(rank)
    hi = min(lo + 1, len(values) - 1)
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (rank - lo)


async def _timed_post(client: httpx.AsyncClient, path: str, payload: Dict[str, Any], timeout_s: float = 20.0) -> Tuple[float, Dict[str, Any], int]:
    t0 = time.perf_counter()
    resp = await client.post(BASE_URL + path, json=payload, timeout=timeout_s)
    dt_ms = (time.perf_counter() - t0) * 1000.0
    body = resp.json()
    return dt_ms, body, resp.status_code


async def run_primitives_once(client: httpx.AsyncClient) -> Dict[str, Any]:
    agent = random.choice(AGENTS)
    vendor = random.choice(VENDORS)
    requested = random.randint(3, 10)

    rv_ms, rv_body, _ = await _timed_post(
        client,
        "/v1/request-voucher",
        {
            "agent_id": agent,
            "vendor_url": vendor,
            "requested_amount_cents": requested,
            "currency": "USD",
        },
    )
    if rv_body.get("decision") != "APPROVED":
        return {
            "ok": False,
            "stage": "request-voucher",
            "latencies_ms": {"request_voucher": rv_ms},
            "body": rv_body,
        }

    session = rv_body["session_token"]
    spend = random.randint(1, requested)
    challenge = _challenge_id()

    auth_ms, auth_body, _ = await _timed_post(
        client,
        "/v1/authorize-spend",
        {
            "session_token": session,
            "mpp_challenge_id": challenge,
            "amount_cents": spend,
        },
    )

    rel_ms, rel_body, _ = await _timed_post(
        client,
        "/v1/release-voucher",
        {
            "session_token": session,
            "reason": "stress_test_cleanup",
        },
    )

    ok = auth_body.get("decision") in {"APPROVED", "REJECTED"} and rel_body.get("decision") in {"APPROVED", "REJECTED"}
    return {
        "ok": ok,
        "stage": "done",
        "latencies_ms": {
            "request_voucher": rv_ms,
            "authorize_spend": auth_ms,
            "release_voucher": rel_ms,
        },
        "auth_decision": auth_body.get("decision"),
        "release_decision": rel_body.get("decision"),
    }


async def run_process_mock_once(client: httpx.AsyncClient) -> Dict[str, Any]:
    task = random.choice(TASKS)
    vendor = random.choice(VENDORS)
    amount = random.randint(3, 10)
    ms, body, _ = await _timed_post(
        client,
        "/v1/process-payment",
        {
            "agent_id": random.choice(AGENTS),
            "task_description": task,
            "candidates": [
                {
                    "description": "stress candidate",
                    "amount_cents": amount,
                    "currency": "USD",
                    "recipient": vendor,
                    "recurring": False,
                }
            ],
            "brain_max_cycles": 1,
            "priority": "normal",
            "mpp_mode": "mock",
        },
        timeout_s=30.0,
    )
    return {
        "ok": body.get("decision") in {"APPROVED", "REJECTED"},
        "latencies_ms": {"process_payment": ms},
        "decision": body.get("decision"),
        "mpp_status": body.get("mpp_execution", {}).get("status"),
    }


async def run_stress(mode: str, total: int, concurrency: int) -> None:
    results: List[Dict[str, Any]] = []
    sem = asyncio.Semaphore(concurrency)
    started = time.perf_counter()

    async with httpx.AsyncClient() as client:
        async def one() -> Dict[str, Any]:
            async with sem:
                try:
                    if mode == "process-mock":
                        return await run_process_mock_once(client)
                    return await run_primitives_once(client)
                except Exception as exc:
                    return {"ok": False, "error": str(exc), "latencies_ms": {}}

        tasks = [asyncio.create_task(one()) for _ in range(total)]
        for fut in asyncio.as_completed(tasks):
            results.append(await fut)

    elapsed_s = time.perf_counter() - started
    oks = sum(1 for r in results if r.get("ok"))
    errs = total - oks

    lat_bucket: Dict[str, List[float]] = {}
    for r in results:
        for k, v in r.get("latencies_ms", {}).items():
            lat_bucket.setdefault(k, []).append(float(v))

    print("\n=== Stress Results ===")
    print(f"mode={mode} total={total} concurrency={concurrency}")
    print(f"ok={oks} err={errs} elapsed_s={elapsed_s:.2f} rps={total/elapsed_s:.2f}")
    for name, values in lat_bucket.items():
        values.sort()
        print(
            f"{name}: avg={statistics.mean(values):.1f}ms "
            f"p50={_percentile(values,0.50):.1f}ms "
            f"p95={_percentile(values,0.95):.1f}ms "
            f"p99={_percentile(values,0.99):.1f}ms "
            f"max={max(values):.1f}ms"
        )

    if errs:
        sample = next((r for r in results if not r.get("ok")), None)
        if sample:
            print("\nSample failure:")
            print(sample)


if __name__ == "__main__":
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "primitives"
    total = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    concurrency = int(sys.argv[3]) if len(sys.argv) > 3 else 40

    if mode not in {"primitives", "process-mock"}:
        raise SystemExit("Mode must be one of: primitives, process-mock")
    asyncio.run(run_stress(mode=mode, total=total, concurrency=concurrency))
