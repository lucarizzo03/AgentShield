# Budget Shield Gateway + Agent Brain

This repository implements a two-plane payment control system:

- `agentShieldAPI.py`: FastAPI Gateway (fast path) that enforces budget and replay safety in Redis.
- `agentShieldAgent.py`: LangGraph Brain (control plane) that picks vendors and retries by moving to the next candidate.

The Brain decides *what to try next*.  
The Gateway decides *whether spending is allowed right now*.

## Architecture

```text
                      Control Plane
+--------------------------------------------------------------+
| AgentShield Brain (`agentShieldAgent.py`)                   |
|                                                              |
| 1) Select next candidate                                     |
| 2) POST /v1/request-voucher                                  |
| 3) POST /v1/authorize-spend                                  |
| 4) If rejected, mark candidate FAILED and try next vendor    |
+-------------------------------+------------------------------+
                                |
                                | HTTP or local call
                                v
                      Fast Path / Data Plane
+--------------------------------------------------------------+
| Budget Shield Gateway (`agentShieldAPI.py`)                  |
|                                                              |
| - Reserve daily budget (atomic Redis Lua script)             |
| - Create short-lived voucher session                         |
| - Replay block on challenge ID (SET NX + TTL)                |
| - Atomic voucher decrement (Lua script)                      |
| - HMAC-sign payment intent                                   |
+-------------------------------+------------------------------+
                                |
                                v
+--------------------------------------------------------------+
| Redis                                                        |
| - daily budget keys                                          |
| - voucher balance + metadata keys                            |
| - replay-protection challenge keys                           |
+--------------------------------------------------------------+
```

## Key Design Rules

- All monetary values are integer cents (no floats).
  - Example: `$50.00` is stored as `5000`.
- The authorization hot path has no LLM calls.
- Replay protection uses one-time `mpp_challenge_id` with 5-minute TTL.
- Voucher and daily budget operations are atomic in Redis.

## Gateway Endpoints

### `POST /v1/request-voucher`

Brain calls this before vendor API spending.

Request:

- `agent_id` (string)
- `vendor_url` (string)
- `requested_amount_cents` (int > 0)
- `currency` (string, default `USD`)

Behavior:

1. Validates `agent_id` is registered.
2. Reserves from the agent daily budget in Redis.
3. Creates voucher keys with TTL.
4. Returns a `session_token`.

Response fields:

- `decision`: `APPROVED | REJECTED`
- `session_token` (nullable)
- `voucher_remaining_cents`
- `daily_budget_remaining_cents`
- `rejection_guidance` (nullable)

---

### `POST /v1/authorize-spend`

Hot path used for each MPP 402 challenge.

Request:

- `session_token` (string)
- `mpp_challenge_id` (string)
- `amount_cents` (int > 0)

Behavior:

1. Replay check: `SET NX EX` challenge key (5 minutes).
2. Atomically decrements voucher balance in Redis.
3. Rejects if voucher is missing/expired/insufficient.
4. Signs approved payload and returns `signed_payment_intent`.

Response fields:

- `decision`: `APPROVED | REJECTED`
- `signed_payment_intent` (nullable)
- `voucher_remaining_cents`
- `rejection_guidance` (nullable)

Signed intent format:

- `mpp_intent_v1.<base64url(payload_json)>.<hmac_sha256_signature>`
- Payload contains: `agent_id`, `vendor_url`, `currency`, `session_token`, `mpp_challenge_id`, `amount_cents`, `timestamp`
- Signature is verified before real MPP execution.

---

### `GET /v1/ledger/{agent_id}`

Daily budget visibility for dashboard/ops.

Returns:

- `status`
- `agent_id`
- `daily_cap_cents`
- `daily_spent_cents`
- `daily_budget_remaining_cents`

---

### `POST /v1/process-payment`

Convenience orchestration endpoint:

1. Runs the Brain candidate loop.
2. Uses voucher + authorize gateway flow.
3. If approved, runs MPP adapter (`mock` or `real`).

Request highlights:

- `agent_id`
- `task_description`
- `candidates` (each candidate uses `amount_cents`)
- `brain_max_cycles`
- `priority`
- `mpp_mode` (`mock` or `real`)

## Brain Loop (LangGraph)

Current nodes in `agentShieldAgent.py`:

- `prepare_candidate`
- `request_voucher`
- `authorize`
- `reflect_next_candidate`

Behavior:

- The Brain does **not** haggle or auto-reduce prices.
- The Brain does **not** force USD normalization.
- On any rejection, the current candidate is treated as failed and the next candidate is tried.
- Stop conditions:
  - Approved
  - Max cycles reached
  - No candidates left

## Redis Key Model

- Daily budget:
  - `budget:daily:{agent_id}:{YYYYMMDD}` -> remaining daily cents
- Voucher balance:
  - `voucher:balance:{session_token}` -> remaining voucher cents
- Voucher metadata hash:
  - `voucher:meta:{session_token}` -> `agent_id`, `vendor_url`, `currency`, `created_at`
- Replay protection:
  - `challenge:{mpp_challenge_id}` -> one-time marker with TTL

## Running Locally

### Prerequisites

- Python 3.11+ (or compatible runtime)
- Redis running locally
- Python packages:
  - `fastapi`
  - `uvicorn`
  - `pydantic`
  - `langgraph`
  - `redis`

### Start Redis

```bash
docker run -d -p 6379:6379 redis
```

If Docker is unavailable, run Redis any other way and set:

```bash
export REDIS_URL="redis://localhost:6379/0"
```

### Start API

```bash
cd /Users/lucar/Desktop/AgentShield
python3 -m uvicorn agentShieldAPI:app --host 0.0.0.0 --port 8000
```

## Example Requests

### 1) Request voucher

```bash
curl -X POST http://127.0.0.1:8000/v1/request-voucher \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent_alpha",
    "vendor_url": "https://realdataapi.com/v1/listings",
    "requested_amount_cents": 120,
    "currency": "EUR"
  }'
```

### 2) Authorize spend

```bash
curl -X POST http://127.0.0.1:8000/v1/authorize-spend \
  -H "Content-Type: application/json" \
  -d '{
    "session_token": "REPLACE_WITH_SESSION_TOKEN",
    "mpp_challenge_id": "ch_agent_alpha_abc123",
    "amount_cents": 120
  }'
```

### 3) Full process-payment

```bash
curl -X POST http://127.0.0.1:8000/v1/process-payment \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent_alpha",
    "task_description": "Scrape 500 real estate listings and enrich owner contact data",
    "candidates": [
      {
        "description": "Primary data API",
        "amount_cents": 120,
        "currency": "EUR",
        "recipient": "https://realdataapi.com/v1/listings",
        "recurring": false
      },
      {
        "description": "Backup API",
        "amount_cents": 95,
        "currency": "EUR",
        "recipient": "https://datasourcehub.io/api/search",
        "recurring": false
      }
    ],
    "brain_max_cycles": 5,
    "priority": "normal",
    "mpp_mode": "mock"
  }'
```

## MPP Adapter Modes

- `mock`: returns synthetic `SUCCEEDED`.
- `real`: sends an HTTP request to provider endpoint; requires:
  - `TEMPO_MPP_API_KEY`
  - `TEMPO_MPP_ENDPOINT`
- Real mode maps approved intent data into provider body and sets:
  - `Authorization: Bearer <TEMPO_MPP_API_KEY>`
  - `Idempotency-Key: <mpp_challenge_id>`
  - `X-AgentShield-Intent-Version: v1`

## Current Limitations

- Real MPP integration is still placeholder logic.
- API endpoints currently have no auth layer.
- Vendor trust/verification is minimal and should be hardened.

## Files

- `agentShieldAPI.py`: FastAPI gateway, Redis budget/voucher/replay logic, MPP adapter.
- `agentShieldAgent.py`: LangGraph control loop for candidate selection and retries.
- `README.md`: system architecture and usage docs.




LOOP: 

Outside request -> Brain
Brain selects vendor candidate
Brain asks Gateway for voucher (/v1/request-voucher)
Brain uses voucher to authorize challenge (/v1/authorize-spend)
Gateway approves/rejects
If approved: Brain passes signed intent to payment executor
Executor attempts payment
If execution fails: Brain moves to next vendor and repeats
