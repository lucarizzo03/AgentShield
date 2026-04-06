import asyncio
import httpx
import random
import string
import time
from typing import List

GATEWAY_URL = "http://localhost:8000/v1/authorize-spend"
AGENTS = ["agent_alpha", "agent_beta", "agent_ops"]
VENDORS = [
    "https://realdataapi.com/v1/listings",
    "https://datasourcehub.io/api/search",
    "https://llmapi.com/v1/tokens",
    "https://sendgrid.com/api/send",
    "https://mailgun.com/api/send",
    "https://researchhub.com/api/subscribe",
]
TASKS = [
    "Scrape 100 listings",
    "Send outreach email",
    "Purchase LLM tokens",
    "Subscribe to research dataset",
    "Enrich data via API",
]

async def send_request(client: httpx.AsyncClient, agent_id: str, task: str, vendor: str, amount: float, currency: str, retries: int) -> dict:
    challenge_id = "ch_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
    payload = {
        "agent_id": agent_id,
        "task_description": task,
        "payment_request": {
            "amount": amount,
            "currency": currency,
            "recipient": vendor,
            "mpp_challenge_id": challenge_id,
            "recurring": False,
        },
        "metadata": {
            "historical_session_spend": 0.0,
            "daily_spend_total": 0.0,
            "priority": "normal",
            "max_retries": retries,
        },
    }
    try:
        resp = await client.post(GATEWAY_URL, json=payload, timeout=10)
        return {"status": resp.status_code, "json": resp.json()}
    except Exception as e:
        return {"status": "error", "error": str(e)}

async def stress_test_gateway(num_requests: int = 200, concurrency: int = 20):
    results = []
    start = time.time()
    async with httpx.AsyncClient() as client:
        sem = asyncio.Semaphore(concurrency)
        async def bound_send():
            async with sem:
                agent = random.choice(AGENTS)
                task = random.choice(TASKS)
                vendor = random.choice(VENDORS)
                amount = round(random.uniform(0.01, 5.0), 2)
                currency = "USD"
                retries = random.randint(0, 3)
                return await send_request(client, agent, task, vendor, amount, currency, retries)
        tasks = [asyncio.create_task(bound_send()) for _ in range(num_requests)]
        for coro in asyncio.as_completed(tasks):
            results.append(await coro)
    elapsed = time.time() - start
    # Summarize results
    approved = sum(1 for r in results if r.get("json", {}).get("decision") == "APPROVED")
    rejected = sum(1 for r in results if r.get("json", {}).get("decision") == "REJECTED")
    errors = sum(1 for r in results if r.get("status") == "error")
    print(f"\n--- Stress Test Results ---")
    print(f"Total requests: {num_requests}")
    print(f"Approved: {approved}")
    print(f"Rejected: {rejected}")
    print(f"Errors: {errors}")
    print(f"Elapsed time: {elapsed:.2f}s")
    if errors:
        print("Sample error:", next(r["error"] for r in results if r.get("status") == "error"))

if __name__ == "__main__":
    import sys
    num = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    conc = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    asyncio.run(stress_test_gateway(num, conc))
