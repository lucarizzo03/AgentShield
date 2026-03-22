# Budget Shield Gateway + Agent Brain

This project is a two-layer autonomous payment governance system:

- `agentShieldAPI.py` is the **gateway** and policy enforcement layer.
- `agentShieldAgent.py` is the **reasoning brain** that uses LangGraph to iterate on payment attempts.

The system is designed so payment requests are evaluated, governed, and only then forwarded to a Stripe/Tempo-style Machine Payment Protocol (MPP) execution adapter.

## What You Have

## 1. Gateway Layer (`agentShieldAPI.py`)

The gateway is a FastAPI service that provides:

- Hard authorization checks (`/v1/authorize-spend`)
- Ledger visibility (`/v1/ledger/{agent_id}`)
- End-to-end orchestration (`/v1/process-payment`)
- MPP execution adapter (`execute_mpp_payment`, mock and real modes)

### Core Responsibilities

- Enforce security gates:
  - registered agent check
  - one-time challenge ID (replay protection)
  - currency policy
  - ledger integrity check
- Enforce governance checks in strict order:
  1. Context alignment
  2. Velocity and loop detection
  3. Value assessment
- Issue a signed payment intent only after all checks pass.
- Track agent spend and payment events in in-memory ledger structures.

## 2. Agent Brain Layer (`agentShieldAgent.py`)

The brain is a LangGraph loop that performs cyclical reasoning over candidate payment options.

### Graph Nodes

- `sync_ledger`
- `prepare_request`
- `authorize`
- `reflect_adjust`

### Loop Behavior

For each cycle, the brain:

1. Syncs ledger values.
2. Builds authorization payload.
3. Calls the gateway (`authorize_spend`) locally or via HTTP.
4. If rejected, reads audit + guidance and adapts:
   - switch vendor candidate
   - reduce amount for benchmark/cap issues
   - normalize currency
   - refresh spend metadata
5. Retries until:
   - approved,
   - max cycles reached, or
   - no candidates left.

## End-to-End Flow (Current)

1. Client calls `POST /v1/process-payment`.
2. API instantiates and runs LangGraph brain.
3. Brain cycles through candidate requests and calls gateway authorization checks.
4. If authorization fails, process returns rejection with reasoning trail.
5. If authorization succeeds, API calls `execute_mpp_payment`.
6. API returns combined result:
   - `authorization` status/details
   - `mpp_execution` status/details
   - final top-level `decision`

## How Reasoning Works

The agent does not submit a single blind payment request. It uses a cycle-based reasoning loop in `agentShieldAgent.py`:

1. `sync_ledger`
  - Pulls current session/daily spend from gateway ledger.
  - Prevents stale metadata from causing false rejects.

2. `prepare_request`
  - Builds a request using the current candidate vendor/amount.
  - Generates a fresh challenge ID and includes retry metadata.

3. `authorize`
  - Calls gateway authorization logic.
  - If approved, loop stops immediately with signed payment intent.
  - If rejected, moves to reflection.

4. `reflect_adjust`
  - Reads `audit_log` and `rejection_guidance`.
  - Applies strategy updates:
    - switch candidate if context/vendor mismatch
    - reduce amount for value/benchmark rejects
    - reduce amount for velocity/cap pressure
    - normalize currency if policy requires USD
    - refresh ledger if mismatch is detected
  - Retries until approved, max cycles reached, or candidates exhausted.

This is the core "reasoning" behavior: inspect rejection cause, adapt request, and retry in a controlled loop.

## What Must Pass For Approval

A payment must pass all of the following in sequence inside `authorize_spend`:

1. Security and policy gates (pre-checks)
  - Agent ID must be registered.
  - MPP challenge ID must not be reused.
  - Currency must match policy (`USD` unless multi-currency enabled).
  - Agent-reported session spend must match gateway ledger.

2. Check 1: Context Alignment
  - Vendor must be verifiable (HTTPS, plausible identity).
  - Vendor must logically match task intent.
  - Requested amount must be proportional for known scope patterns.

3. Check 2: Velocity
  - Projected daily spend must remain under daily cap.
  - Retry-loop heuristics must not trigger.
  - Velocity spike heuristics must not trigger.
  - Micro-payment flood threshold must not be exceeded.

4. Check 3: Value Assessment
  - Amount must fit benchmark range for inferred service type.
  - Above-benchmark spend requires contextual premium justification.
  - Recurring spend is guarded and requires owner confirmation flow.

Only when all checks pass does gateway issue `signed_payment_intent`.

Then `/v1/process-payment` attempts MPP execution:

- If MPP execution returns `SUCCEEDED` or `PENDING`, final decision is `APPROVED`.
- Otherwise final decision is `REJECTED` (authorization may still be approved, but execution failed/skipped).

## API Endpoints

## `POST /v1/authorize-spend`

Runs direct gateway checks and returns an authorization decision.

Request model:

- `AuthorizeSpendRequest`
  - `agent_id`
  - `task_description`
  - `payment_request`
    - `amount`
    - `currency`
    - `recipient`
    - `mpp_challenge_id`
    - `recurring`
  - `metadata`
    - `historical_session_spend`
    - `daily_spend_total`
    - `priority`
    - `max_retries`

Response model:

- `AuthorizeSpendResponse`
  - `decision`: `APPROVED` or `REJECTED`
  - `auth_token_request`
  - `signed_payment_intent`
  - `audit_log`
  - `rejection_guidance`

## `GET /v1/ledger/{agent_id}`

Returns current in-memory ledger totals for a registered agent.

## `POST /v1/process-payment`

Runs full orchestration: brain reasoning -> gateway authorization -> MPP adapter.

Request model:

- `ProcessPaymentRequest`
  - `agent_id`
  - `task_description`
  - `candidates`: list of spend options
  - `session_spend`
  - `daily_spend`
  - `brain_max_cycles`
  - `priority`
  - `mpp_mode`: `mock` or `real`

Response model:

- `ProcessPaymentResponse`
  - `decision`
  - `authorization`
    - includes reasoning log, cycles used, gateway response, signed intent if approved
  - `mpp_execution`
    - attempted/status/provider/transaction/message/timestamp

## Running Locally

## Prerequisites

- Python 3.11+
- Virtual environment with installed packages:
  - `fastapi`
  - `uvicorn`
  - `pydantic`
  - `langgraph`

## Start API Server

```bash
/Users/lucar/Desktop/AgentWallet/.venv/bin/python /Users/lucar/Desktop/AgentWallet/agentShieldAPI.py
```

Server binds to `http://0.0.0.0:8000`.

## Quick Direct Function Test (No HTTP)

```bash
/Users/lucar/Desktop/AgentWallet/.venv/bin/python - <<'PY'
from agentShieldAPI import process_payment, ProcessPaymentRequest

payload = ProcessPaymentRequest(
    agent_id='agent_alpha',
    task_description='Scrape 500 real estate listings using a data API',
    candidates=[
        {
            'description': 'primary',
            'amount': 0.2,
            'currency': 'USD',
            'recipient': 'https://realdataapi.com/v1/listings',
            'recurring': False,
        }
    ],
    session_spend=0.0,
    daily_spend=0.0,
    brain_max_cycles=3,
    priority='normal',
    mpp_mode='mock',
)

res = process_payment(payload)
print(res.model_dump_json(indent=2))
PY
```

## HTTP Example (`/v1/process-payment`)

```bash
curl -X POST http://127.0.0.1:8000/v1/process-payment \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent_alpha",
    "task_description": "Scrape 500 real estate listings using a data API",
    "candidates": [
      {
        "description": "primary",
        "amount": 0.2,
        "currency": "USD",
        "recipient": "https://realdataapi.com/v1/listings",
        "recurring": false
      }
    ],
    "session_spend": 0.0,
    "daily_spend": 0.0,
    "brain_max_cycles": 3,
    "priority": "normal",
    "mpp_mode": "mock"
  }'
```

## MPP Adapter Modes

## `mock` (default)

- Returns successful synthetic execution.
- Useful for local development and integration tests.

## `real`

Current real mode is a placeholder with configuration checks.

Required environment variables:

- `TEMPO_MPP_API_KEY`
- `TEMPO_MPP_ENDPOINT`

If either variable is missing, the adapter returns `NOT_CONFIGURED`.

## In-Memory State Notes

The following are in-memory and reset on process restart:

- agent ledger (`ledgers`)
- used challenge IDs (`used_challenges`)

For production, replace these with durable storage (Redis/Postgres/etc.) and concurrency-safe primitives.

## Security and Governance Characteristics

- One-time challenge IDs reduce replay risk.
- Ledger mismatch check protects against stale/tampered agent metadata.
- Daily cap + retry-loop heuristics prevent runaway spend.
- Benchmark checks discourage overpayment.
- Recurring purchases are explicitly guarded.

## Known Limitations

- Vendor verification is heuristic (URL pattern based), not reputation-backed.
- Real MPP execution is a stub in `real` mode.
- Ledger and replay set are process-local memory.
- No auth layer on API endpoints yet.
- No persistence or cross-instance coordination.

## Suggested Next Steps

1. Add persistent ledger/challenge storage.
2. Add API authentication/authorization.
3. Implement live Tempo/Stripe SDK call in `execute_mpp_payment`.
4. Add structured logging and request IDs.
5. Add unit/integration tests for each check and graph path.

## File Overview

- `agentShieldAPI.py`: gateway, policy checks, orchestration endpoint, MPP adapter
- `agentShieldAgent.py`: LangGraph reasoning brain, cycle adaptation logic
- `README.md`: this document
