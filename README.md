# Budget Shield Gateway + Agent Brain

This repository implements a two-layer payment governance system:

- `agentShieldAPI.py`: FastAPI gateway with policy enforcement and orchestration.
- ` lol.py`: LangGraph-based reasoning brain that iterates through spend candidates.

The gateway authorizes first, then optionally hands off to an MPP execution adapter.

## Current Architecture

### Gateway (`agentShieldAPI.py`)

The gateway provides:

- `POST /v1/authorize-spend` for direct policy evaluation.
- `GET /v1/ledger/{agent_id}` for in-memory spend visibility.
- `POST /v1/process-payment` for full brain -> gateway -> MPP orchestration.
- `execute_mpp_payment(...)` adapter with `mock` and `real` modes.

Runtime defaults:

- Daily cap: `$50.00` (`GatewayConfig.daily_cap_usd`)
- Multi-currency: disabled (`USD` required)
- Registered agents: `agent_alpha`, `agent_beta`, `agent_ops`

### Agent Brain (`agentShieldAgent.py`)

The brain runs a LangGraph loop with nodes:

- `sync_ledger`
- `prepare_request`
- `authorize`
- `reflect_adjust`

It can call the gateway:

- Locally (direct function call) when `gateway_url` is `None`
- Over HTTP when `gateway_url` is set

## Gateway Authorization Logic (Exact Order)

`authorize_spend(...)` evaluates in strict sequence.

### 1) Security/Policy Pre-Gates

1. Agent must be registered.
2. `mpp_challenge_id` must be unused (replay protection).
3. Currency must be `USD` unless multi-currency is enabled.
4. Agent-reported `historical_session_spend` must match gateway ledger (tolerance `0.01`).

If any pre-gate fails, checks 1-3 are skipped and rejection is returned with guidance.

### 2) Check 1: Context Alignment

- Vendor must pass verification:
  - HTTPS required
  - hostname-like netloc required
  - blocks obvious synthetic markers (`example`, `test`, `fake`, `spoof`, `localhost`, `127.0.0.1`)
- Task/vendor alignment is rule-based by task keywords.
- Specific proportionality guard:
  - task containing `10 listings` with amount `> 50` is rejected.

### 3) Check 2: Velocity + Loop Detection

- Reject if projected daily total exceeds cap.
- Loop and velocity flags include:
  - same-vendor attempts `>= 3` (for non-micro payments)
  - retry pattern on small charges (`max_retries > 1` and amount `< 1.0`, non-micro)
  - spend spike over baseline (`> 2.5x` and meaningful absolute volume)
  - micro-payment flood (`>= 50` events under `$0.10` in last hour)

Any flag rejects the request.

### 4) Check 3: Value Assessment

Service type is inferred from task/vendor text. Benchmarks:

- `data_api`: reject above `$0.50` unless premium justification exists
- `llm_api`: reject above `$1.00` unless premium justification exists
- `web_scraping`: reject above `$0.50` unless premium justification exists
- `email_infra`: reject above `$0.05` unless premium justification exists
- `research_subscription`: reject above `$50`
- `unknown`: reject

Recurring charges are rejected for owner confirmation before first charge.

Premium justification keywords:

- `enterprise`, `sla`, `compliance`, `premium`, `priority`, `real-time`

### 5) Approval Commit

Only after all checks pass:

- Signed payment intent is created (`HMAC-SHA256` over canonical payload).
- Challenge ID is marked used.
- Ledger is updated (`session_spend`, `daily_spend`, `attempts_by_vendor`, `payment_events`).

## Agent Brain Behavior (Current)

For each iteration:

1. `sync_ledger`: pulls ledger from gateway when available.
2. `prepare_request`: builds authorization payload for current candidate and generates fresh challenge ID.
3. `authorize`: calls gateway and stops immediately on approval.
4. `reflect_adjust`: inspects `audit_log` + `rejection_guidance` and applies one strategy.

Reflection strategies implemented:

- Ledger mismatch guidance: refresh ledger values.
- Vendor/context mismatch: move to next candidate.
- Benchmark/high-cost rejection: reduce amount to `60%`.
- Velocity/cap/loop pressure: reduce amount to `75%`.
- Currency policy rejection: normalize currency to `USD`.
- Recurring rejection: move to next candidate.
- Fallback: move to next candidate.

Stop conditions:

- Approved
- `max_cycles` reached
- No candidates left

## End-to-End `/v1/process-payment` Flow

1. Receive `ProcessPaymentRequest`.
2. Build `SpendCandidate` list from request candidates.
3. Run `AgentShieldBrain.run(...)`.
4. If brain result is not approved:
  - top-level decision `REJECTED`
  - MPP status `SKIPPED_NOT_APPROVED`
5. If approved:
  - call `execute_mpp_payment(...)`
  - final decision is:
    - `APPROVED` when MPP status is `SUCCEEDED` or `PENDING`
    - `REJECTED` otherwise

## API Models

### `POST /v1/authorize-spend`

Request: `AuthorizeSpendRequest`

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

Response: `AuthorizeSpendResponse`

- `decision`: `APPROVED | REJECTED`
- `auth_token_request`
- `signed_payment_intent`
- `audit_log`
- `rejection_guidance`

### `GET /v1/ledger/{agent_id}`

Returns:

- `status`
- `agent_id`
- `session_spend`
- `daily_spend`
- `daily_cap`

### `POST /v1/process-payment`

Request: `ProcessPaymentRequest`

- `agent_id`
- `task_description`
- `candidates`
- `session_spend`
- `daily_spend`
- `brain_max_cycles`
- `priority`
- `mpp_mode` (`mock` or `real`)

Response: `ProcessPaymentResponse`

- `decision`
- `authorization`
  - `approved`
  - `signed_payment_intent` (when approved)
  - `cycles_used`
  - `reasoning_log`
  - `gateway_response`
- `mpp_execution`
  - `attempted`
  - `status`
  - `transaction_id`
  - `provider`
  - `message`
  - `executed_at`

## Running Locally

### Prerequisites

- Python 3.11+
- Installed packages: `fastapi`, `uvicorn`, `pydantic`, `langgraph`

### Start API Server

```bash
cd /Users/lucar/Desktop/AgentShield
.venv/bin/python -m uvicorn agentShieldAPI:app --host 0.0.0.0 --port 8000
```

### Direct Function Test (No HTTP)

```bash
cd /Users/lucar/Desktop/AgentShield
.venv/bin/python - <<'PY'
from agentShieldAPI import process_payment, ProcessPaymentRequest

payload = ProcessPaymentRequest(
    agent_id="agent_alpha",
    task_description="Scrape 500 real estate listings using a data API",
    candidates=[
        {
            "description": "primary",
            "amount": 0.2,
            "currency": "USD",
            "recipient": "https://realdataapi.com/v1/listings",
            "recurring": False,
        }
    ],
    session_spend=0.0,
    daily_spend=0.0,
    brain_max_cycles=3,
    priority="normal",
    mpp_mode="mock",
)

res = process_payment(payload)
print(res.model_dump_json(indent=2))
PY
```

### HTTP Example

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

### `mock`

- Always returns a synthetic `SUCCEEDED` execution.

### `real`

- Placeholder path with config checks.
- Requires:
  - `TEMPO_MPP_API_KEY`
  - `TEMPO_MPP_ENDPOINT`
- If missing, returns `NOT_CONFIGURED`.
- If present, currently returns placeholder `PENDING` response.

## In-Memory State

State is process-local and resets on restart:

- `ledgers`
- `used_challenges`

For production, move these to durable shared storage.

## Known Limitations

- Vendor verification is heuristic (not reputation-backed).
- Real MPP integration is still a placeholder.
- No authentication layer on API endpoints.
- No persistence or cross-instance coordination.

## File Overview

- `agentShieldAPI.py`: gateway checks, endpoints, orchestration, MPP adapter
- `agentShieldAgent.py`: reasoning brain and adaptation loop
- `README.md`: project documentation
