# Budget Shield Gateway + Agent Brain

This repository implements a two-plane payment control system:

- `agentShieldAPI.py`: FastAPI Gateway (fast path) that enforces budget and replay safety in Redis.
- `agentShieldAgent.py`: LangGraph Brain (control plane) that picks vendors and retries by moving to the next candidate.

The Brain decides *what to try next*.  
The Gateway decides *whether spending is allowed right now*.

Choose one setup path:
- Docker path below: easiest for first-time users.
- Local Dev path below: best for editing code and debugging.

## Quickstart (Docker - Recommended)

Best for users cloning from GitHub who want the fastest path to a working API.

```bash
docker compose up --build -d
```

Authenticate Tempo wallet on your host first (uses your normal browser/passkey):

```bash
tempo wallet login
tempo wallet whoami
```

The API container reuses your host wallet/session directory (`${HOME}/.tempo`) while still using a Linux `tempo` binary inside the container.

Wait for containers to become healthy, then test:

```bash
curl -s http://127.0.0.1:8000/v1/ledger/agent_alpha | jq
```

Run a payment flow:

```bash
curl -s -X POST http://127.0.0.1:8000/v1/process-payment \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent_alpha",
    "task_description": "a sunset over the ocean",
    "candidates": [
      {
        "description": "FAL Flux test",
        "amount_cents": 5,
        "currency": "USD",
        "recipient": "https://fal.mpp.tempo.xyz/fal-ai/flux/dev",
        "recurring": false
      }
    ],
    "brain_max_cycles": 1,
    "priority": "normal",
    "mpp_mode": "real"
  }' | jq
```

Stop everything:

```bash
docker compose down
```

Notes:
- Real payments in Docker use in-container `tempo` CLI.
- Wallet/session is shared from host `${HOME}/.tempo`.

## Quickstart (Local Dev)

Best for development/debugging when you want to run Python directly without rebuilding containers.

1. Start Redis
```bash
docker run -d -p 6379:6379 redis
```

2. Set env vars
```bash
export REDIS_URL="redis://localhost:6379/0"
export TEMPO_FALLBACK_ON_BLOCK=true
export TEMPO_REQUEST_JSON='{"prompt":". . ."}'
```

3. Start API
```bash
python3 -m uvicorn agentShieldAPI:app --host 0.0.0.0 --port 8000
```

4. Run a real payment flow
```bash
curl -X POST http://127.0.0.1:8000/v1/process-payment \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent_alpha",
    "task_description": "a sunset over the ocean",
    "candidates": [
      {
        "description": "FAL Flux test",
        "amount_cents": 5,
        "currency": "USD",
        "recipient": "https://fal.mpp.tempo.xyz/fal-ai/flux/dev",
        "recurring": false
      }
    ],
    "brain_max_cycles": 1,
    "priority": "normal",
    "mpp_mode": "real"
  }'
```

## Canonical End-to-End Flow

1. Client calls `POST /v1/process-payment`.
2. Brain picks a candidate and requests voucher budget via `POST /v1/request-voucher`.
3. Gateway reserves budget (USD-normalized), creates voucher, returns `session_token`.
4. Executor calls vendor endpoint without payment proof.
5. If vendor returns `HTTP 402`, executor extracts `mpp_challenge_id` (+ amount/currency when provided).
6. Executor calls `POST /v1/authorize-spend` with that exact challenge.
7. Gateway runs replay + voucher checks, deducts voucher, returns `signed_payment_intent`.
8. Executor retries vendor call with signed intent attached.
9. Executor calls `POST /v1/release-voucher` to sweep any unused reserved budget.
10. If execution fails, orchestrator moves to the next candidate until success, candidate exhaustion, or `brain_max_cycles`.

## Architecture

```text
                      Control Plane
+--------------------------------------------------------------+
| AgentShield Brain (`agentShieldAgent.py`)                   |
|                                                              |
| 1) Select next candidate                                     |
| 2) POST /v1/request-voucher                                  |
| 3) Hand session token to executor                            |
| 4) If execution fails, try next vendor                       |
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
2. Converts requested currency into base USD cents for budget accounting.
3. Reserves from the agent daily budget in Redis.
4. Creates voucher keys with TTL.
5. Returns a `session_token`.

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

### `POST /v1/release-voucher`

Releases unused voucher value back into the daily budget reservation pool.

Request:

- `session_token` (string)
- `reason` (string, optional)

Behavior:

1. Reads voucher metadata and remaining voucher cents.
2. Computes proportional remaining reserved budget in base currency (USD cents).
3. Credits that amount back to the original daily budget key.
4. Deletes voucher keys so the session cannot be reused.

Response fields:

- `decision`
- `released_budget_cents`
- `daily_budget_remaining_cents`
- `rejection_guidance` (nullable)

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
2. Reserves voucher for a selected candidate.
3. Executor performs HTTP handshake:
   - if vendor returns `200`, execution is considered successful without challenge.
   - if vendor returns `402`, run challenge authorize + retry flow.
4. Releases voucher residue (`/v1/release-voucher`) after each execution attempt.
5. On failure, loops to next candidate.

Request highlights:

- `agent_id`
- `task_description`
- `candidates` (each candidate uses `amount_cents`)
- `brain_max_cycles`
- `priority`
- `mpp_mode` (`mock` or `real`)

Execution response note:

- `mpp_execution.execution_path` explicitly shows which transport path executed:
  - `direct_402_http`
  - `tempo_cli_fallback`
  - `mock_adapter`
- `mpp_execution.vendor_http_status` includes the vendor HTTP status when available.
- `mpp_execution.vendor_response_preview` includes a truncated copy of the vendor/transport response payload.
- `mpp_execution.vendor_response_json` includes parsed JSON payload (when the response is valid JSON).

## Brain Loop (LangGraph)

Current nodes in `agentShieldAgent.py`:

- `prepare_candidate`
- `request_voucher`
- `reflect_next_candidate`

Behavior:

- The Brain does **not** haggle or auto-reduce prices.
- The Brain does **not** force USD normalization.
- The Brain reserves voucher budget and hands execution to the executor.
- On any voucher failure or execution failure, the current candidate is treated as failed and the next candidate is tried.
- Orchestration stop conditions:
  - Approved execution
  - `brain_max_cycles` reached
  - No candidates left

## Redis Key Model

- Daily budget:
  - `budget:daily:{agent_id}:{YYYYMMDD}` -> remaining daily cents
- Voucher balance:
  - `voucher:balance:{session_token}` -> remaining voucher cents
- Voucher metadata hash:
  - `voucher:meta:{session_token}` -> `agent_id`, `vendor_url`, `currency`, `requested_vendor_cents`, `reserved_usd_cents`, `budget_key`
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
- Tempo CLI installed and logged in (only required for `mpp_mode="real"` recommended path):
  - `tempo --version`
  - `tempo wallet login`

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
python3 -m uvicorn agentShieldAPI:app --host 0.0.0.0 --port 8000
```

## Example Requests

### 1) Request voucher

```bash
curl -X POST http://127.0.0.1:8000/v1/request-voucher \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent_alpha",
    "vendor_url": "https://fal.mpp.tempo.xyz/fal-ai/flux/dev",
    "requested_amount_cents": 5,
    "currency": "USD"
  }'
```

### 2) Authorize spend

```bash
curl -X POST http://127.0.0.1:8000/v1/authorize-spend \
  -H "Content-Type: application/json" \
  -d '{
    "session_token": "REPLACE_WITH_SESSION_TOKEN",
    "mpp_challenge_id": "mpp_ch_01JQ7F2X9Y4R8K3T6N1B5V",
    "amount_cents": 5
  }'
```

Use the exact challenge ID returned by the vendor `HTTP 402` (typically from `x-mpp-challenge-id`).

### 3) Full process-payment

```bash
curl -X POST http://127.0.0.1:8000/v1/process-payment \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent_alpha",
    "task_description": "a sunset over the ocean",
    "candidates": [
      {
        "description": "FAL Flux primary",
        "amount_cents": 5,
        "currency": "USD",
        "recipient": "https://fal.mpp.tempo.xyz/fal-ai/flux/dev",
        "recurring": false
      },
      {
        "description": "FAL Flux backup candidate",
        "amount_cents": 6,
        "currency": "USD",
        "recipient": "https://fal.mpp.tempo.xyz/fal-ai/flux/dev",
        "recurring": false
      }
    ],
    "brain_max_cycles": 5,
    "priority": "normal",
    "mpp_mode": "real"
  }'
```

## MPP Adapter Modes

- `mock`: returns synthetic `SUCCEEDED`.
- `real`: runs protocol-sequenced HTTP 402 handshake:
  - probe vendor request -> capture `402` challenge
  - authorize exact challenge via gateway
  - retry vendor request with signed intent
  - release unused voucher reservation
  - if vendor edge blocks direct probe (e.g. HTTP 403/1010), fallback uses `tempo request` transport (`TEMPO_FALLBACK_ON_BLOCK=true`, default true)

## Current Limitations

- API endpoints currently have no auth layer.
- Vendor trust/verification is minimal and should be hardened.
- Challenge parsing currently relies on common header/body conventions; vendor-specific adapters may still be required.

## Files

- `agentShieldAPI.py`: FastAPI gateway, Redis budget/voucher/replay logic, MPP adapter.
- `agentShieldAgent.py`: LangGraph control loop for candidate selection and retries.
- `README.md`: system architecture and usage docs.




## FX Accounting Note

- Gateway daily budget is tracked in base currency (USD cents).
- On `request-voucher`, Gateway converts requested foreign currency amount into USD cents using an FX rate table/oracle before reserving from `budget:daily:*`.
- Voucher spend still occurs in vendor-requested cents/currency; release logic returns the proportional unused USD reservation.

## Runtime Env Vars

- `REDIS_URL` (default `redis://localhost:6379/0`)
- `FX_RATES_TO_USD_JSON` (optional JSON map, e.g. `{"EUR":1.08,"USD":1.0}`)
- `TEMPO_FALLBACK_ON_BLOCK` (default `true`)
- `TEMPO_REQUEST_TIMEOUT_SECONDS`, `TEMPO_REQUEST_CONNECT_TIMEOUT_SECONDS`, `TEMPO_REQUEST_RETRIES`, `TEMPO_REQUEST_RETRY_BACKOFF_MS`
- `TEMPO_REQUEST_METHOD`, `TEMPO_REQUEST_JSON`, `TEMPO_NETWORK`
