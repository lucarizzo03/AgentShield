from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import hmac
import importlib
import json
import os
import secrets
from typing import Dict, List, Optional

from pydantic import BaseModel, Field
import redis.asyncio as redis

fastapi_module = importlib.import_module("fastapi")
FastAPI = fastapi_module.FastAPI
HTTPException = fastapi_module.HTTPException
Request = fastapi_module.Request


class Decision(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass
class GatewayConfig:
    """Runtime knobs for Redis-backed gateway behavior."""

    daily_cap_cents: int = 5000
    voucher_ttl_seconds: int = 900
    challenge_ttl_seconds: int = 300
    signing_secret: str = field(default_factory=lambda: secrets.token_hex(32))
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))


class RequestVoucherRequest(BaseModel):
    agent_id: str
    vendor_url: str
    requested_amount_cents: int = Field(gt=0)
    currency: str = "USD"


class RequestVoucherResponse(BaseModel):
    decision: Decision
    session_token: Optional[str]
    voucher_remaining_cents: int
    daily_budget_remaining_cents: int
    rejection_guidance: Optional[str]


class AuthorizeSpendRequest(BaseModel):
    session_token: str
    mpp_challenge_id: str
    amount_cents: int = Field(gt=0)


class AuthorizeSpendResponse(BaseModel):
    decision: Decision
    signed_payment_intent: Optional[str]
    voucher_remaining_cents: int
    rejection_guidance: Optional[str]


class ProcessSpendCandidate(BaseModel):
    description: str
    amount_cents: int = Field(gt=0)
    currency: str = "USD"
    recipient: str
    recurring: bool = False


class ProcessPaymentRequest(BaseModel):
    agent_id: str
    task_description: str
    candidates: List[ProcessSpendCandidate]
    brain_max_cycles: int = 5
    priority: str = "normal"
    mpp_mode: str = "mock"


class MPPExecutionResult(BaseModel):
    attempted: bool
    status: str
    transaction_id: Optional[str]
    provider: str
    message: str
    executed_at: str


class ProcessPaymentResponse(BaseModel):
    decision: str
    authorization: Dict[str, object]
    mpp_execution: MPPExecutionResult


config = GatewayConfig()
registered_agents = {"agent_alpha", "agent_beta", "agent_ops"}


RESERVE_DAILY_BUDGET_LUA = """
local key = KEYS[1]
local cap = tonumber(ARGV[1])
local requested = tonumber(ARGV[2])

if redis.call('EXISTS', key) == 0 then
  redis.call('SET', key, cap)
end

local current = tonumber(redis.call('GET', key))
if current < requested then
  return -1
end

return redis.call('DECRBY', key, requested)
"""


DECREMENT_VOUCHER_LUA = """
local key = KEYS[1]
local spend = tonumber(ARGV[1])

if redis.call('EXISTS', key) == 0 then
  return -2
end

local current = tonumber(redis.call('GET', key))
if current < spend then
  return -1
end

return redis.call('DECRBY', key, spend)
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = redis.from_url(config.redis_url, decode_responses=True)
    # Fail fast if Redis is unavailable.
    await app.state.redis.ping()
    yield
    await app.state.redis.aclose()


app = FastAPI(title="Budget Shield Gateway", version="2.0.0", lifespan=lifespan)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def daily_budget_key(agent_id: str) -> str:
    return f"budget:daily:{agent_id}:{utc_day_key()}"


def voucher_balance_key(session_token: str) -> str:
    return f"voucher:balance:{session_token}"


def voucher_meta_key(session_token: str) -> str:
    return f"voucher:meta:{session_token}"


def challenge_key(mpp_challenge_id: str) -> str:
    return f"challenge:{mpp_challenge_id}"


def normalize_vendor(recipient: str) -> str:
    return recipient.strip().lower()


def build_signed_intent(payload: Dict[str, object]) -> str:
    packed = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(config.signing_secret.encode("utf-8"), packed, hashlib.sha256).hexdigest()
    return f"mpp_sig_{digest}"


def execute_mpp_payment(
    *,
    signed_payment_intent: str,
    payment_request: Dict[str, object],
    mode: str = "mock",
) -> MPPExecutionResult:
    now = utc_now_iso()
    if mode.lower() == "mock":
        tx_id = f"mpp_mock_{secrets.token_hex(8)}"
        return MPPExecutionResult(
            attempted=True,
            status="SUCCEEDED",
            transaction_id=tx_id,
            provider="tempo-mpp-mock",
            message="Mock MPP execution succeeded.",
            executed_at=now,
        )

    if mode.lower() == "real":
        api_key = os.getenv("TEMPO_MPP_API_KEY")
        endpoint = os.getenv("TEMPO_MPP_ENDPOINT")
        if not api_key or not endpoint:
            return MPPExecutionResult(
                attempted=False,
                status="NOT_CONFIGURED",
                transaction_id=None,
                provider="tempo-mpp",
                message="Real MPP mode selected but TEMPO_MPP_API_KEY or TEMPO_MPP_ENDPOINT is missing.",
                executed_at=now,
            )

        return MPPExecutionResult(
            attempted=True,
            status="PENDING",
            transaction_id=f"mpp_real_{secrets.token_hex(8)}",
            provider="tempo-mpp",
            message="Real MPP adapter configured; replace placeholder with live provider SDK/API call.",
            executed_at=now,
        )

    return MPPExecutionResult(
        attempted=False,
        status="INVALID_MODE",
        transaction_id=None,
        provider="tempo-mpp",
        message="Unsupported mpp_mode. Use 'mock' or 'real'.",
        executed_at=now,
    )


async def request_voucher_core(r: redis.Redis, payload: RequestVoucherRequest) -> RequestVoucherResponse:
    if payload.agent_id not in registered_agents:
        return RequestVoucherResponse(
            decision=Decision.REJECTED,
            session_token=None,
            voucher_remaining_cents=0,
            daily_budget_remaining_cents=0,
            rejection_guidance="Unregistered agent_id.",
        )

    daily_key = daily_budget_key(payload.agent_id)
    reserve = await r.eval(
        RESERVE_DAILY_BUDGET_LUA,
        1,
        daily_key,
        config.daily_cap_cents,
        payload.requested_amount_cents,
    )

    if int(reserve) < 0:
        current_budget = await r.get(daily_key)
        remaining = int(current_budget) if current_budget is not None else config.daily_cap_cents
        return RequestVoucherResponse(
            decision=Decision.REJECTED,
            session_token=None,
            voucher_remaining_cents=0,
            daily_budget_remaining_cents=remaining,
            rejection_guidance="Daily budget cap exceeded for this request.",
        )

    session_token = secrets.token_urlsafe(24)
    balance_key = voucher_balance_key(session_token)
    meta_key = voucher_meta_key(session_token)
    vendor = normalize_vendor(payload.vendor_url)

    async with r.pipeline() as pipe:
        pipe.set(balance_key, payload.requested_amount_cents, ex=config.voucher_ttl_seconds)
        pipe.hset(
            meta_key,
            mapping={
                "agent_id": payload.agent_id,
                "vendor_url": vendor,
                "currency": payload.currency.upper(),
                "created_at": utc_now_iso(),
            },
        )
        pipe.expire(meta_key, config.voucher_ttl_seconds)
        await pipe.execute()

    return RequestVoucherResponse(
        decision=Decision.APPROVED,
        session_token=session_token,
        voucher_remaining_cents=payload.requested_amount_cents,
        daily_budget_remaining_cents=int(reserve),
        rejection_guidance=None,
    )


async def authorize_spend_core(r: redis.Redis, payload: AuthorizeSpendRequest) -> AuthorizeSpendResponse:
    replay_ok = await r.set(
        challenge_key(payload.mpp_challenge_id),
        "1",
        ex=config.challenge_ttl_seconds,
        nx=True,
    )
    if not replay_ok:
        return AuthorizeSpendResponse(
            decision=Decision.REJECTED,
            signed_payment_intent=None,
            voucher_remaining_cents=0,
            rejection_guidance="Replay detected: mpp_challenge_id already used.",
        )

    balance_key = voucher_balance_key(payload.session_token)
    meta_key = voucher_meta_key(payload.session_token)
    voucher_remaining = await r.eval(DECREMENT_VOUCHER_LUA, 1, balance_key, payload.amount_cents)
    voucher_remaining_int = int(voucher_remaining)

    if voucher_remaining_int == -2:
        return AuthorizeSpendResponse(
            decision=Decision.REJECTED,
            signed_payment_intent=None,
            voucher_remaining_cents=0,
            rejection_guidance="Invalid or expired session_token.",
        )
    if voucher_remaining_int < 0:
        current = await r.get(balance_key)
        current_remaining = int(current) if current is not None else 0
        return AuthorizeSpendResponse(
            decision=Decision.REJECTED,
            signed_payment_intent=None,
            voucher_remaining_cents=current_remaining,
            rejection_guidance="Insufficient voucher balance.",
        )

    voucher_meta = await r.hgetall(meta_key)
    signed_payload = {
        "agent_id": voucher_meta.get("agent_id", ""),
        "vendor_url": voucher_meta.get("vendor_url", ""),
        "currency": voucher_meta.get("currency", "USD"),
        "session_token": payload.session_token,
        "mpp_challenge_id": payload.mpp_challenge_id,
        "amount_cents": payload.amount_cents,
        "timestamp": utc_now_iso(),
    }
    signed_intent = build_signed_intent(signed_payload)
    return AuthorizeSpendResponse(
        decision=Decision.APPROVED,
        signed_payment_intent=signed_intent,
        voucher_remaining_cents=voucher_remaining_int,
        rejection_guidance=None,
    )


@app.post("/v1/request-voucher", response_model=RequestVoucherResponse)
async def request_voucher(payload: RequestVoucherRequest, request: Request) -> RequestVoucherResponse:
    r = request.app.state.redis
    return await request_voucher_core(r, payload)


@app.post("/v1/authorize-spend", response_model=AuthorizeSpendResponse)
async def authorize_spend(payload: AuthorizeSpendRequest, request: Request) -> AuthorizeSpendResponse:
    r = request.app.state.redis
    return await authorize_spend_core(r, payload)


@app.get("/v1/ledger/{agent_id}")
async def get_agent_ledger(agent_id: str, request: Request) -> Dict[str, int | str]:
    if agent_id not in registered_agents:
        return {"status": "not_found", "agent_id": agent_id, "message": "agent not registered"}

    r = request.app.state.redis
    key = daily_budget_key(agent_id)
    current = await r.get(key)
    remaining = int(current) if current is not None else config.daily_cap_cents
    spent = config.daily_cap_cents - remaining
    return {
        "status": "ok",
        "agent_id": agent_id,
        "daily_cap_cents": config.daily_cap_cents,
        "daily_spent_cents": max(0, spent),
        "daily_budget_remaining_cents": remaining,
    }


@app.post("/v1/process-payment", response_model=ProcessPaymentResponse)
async def process_payment(payload: ProcessPaymentRequest) -> ProcessPaymentResponse:
    brain_module = importlib.import_module("agentShieldAgent")
    spend_candidates = [
        brain_module.SpendCandidate(
            description=c.description,
            amount_cents=c.amount_cents,
            currency=c.currency,
            recipient=c.recipient,
            recurring=c.recurring,
        )
        for c in payload.candidates
    ]
    brain = brain_module.AgentShieldBrain(
        brain_module.AgentBrainConfig(
            max_cycles=max(1, int(payload.brain_max_cycles)),
            gateway_url=None,
            priority=payload.priority,
        )
    )

    brain_result = brain.run(
        agent_id=payload.agent_id,
        task_description=payload.task_description,
        candidates=spend_candidates,
    )

    approved = bool(brain_result.get("approved"))
    gateway_response = brain_result.get("gateway_response") or {}
    if not approved:
        return ProcessPaymentResponse(
            decision="REJECTED",
            authorization={
                "approved": False,
                "cycles_used": brain_result.get("cycles_used"),
                "reasoning_log": brain_result.get("reasoning_log"),
                "gateway_response": gateway_response,
            },
            mpp_execution=MPPExecutionResult(
                attempted=False,
                status="SKIPPED_NOT_APPROVED",
                transaction_id=None,
                provider="tempo-mpp",
                message="Authorization rejected; payment not forwarded to MPP rail.",
                executed_at=utc_now_iso(),
            ),
        )

    signed_intent = str(brain_result.get("signed_payment_intent") or "")
    mpp_result = execute_mpp_payment(
        signed_payment_intent=signed_intent,
        payment_request={
            "agent_id": payload.agent_id,
            "task_description": payload.task_description,
            "gateway_response": gateway_response,
        },
        mode=payload.mpp_mode,
    )
    final_decision = "APPROVED" if mpp_result.status in {"SUCCEEDED", "PENDING"} else "REJECTED"
    return ProcessPaymentResponse(
        decision=final_decision,
        authorization={
            "approved": True,
            "signed_payment_intent": signed_intent,
            "cycles_used": brain_result.get("cycles_used"),
            "reasoning_log": brain_result.get("reasoning_log"),
            "gateway_response": gateway_response,
        },
        mpp_execution=mpp_result,
    )


if __name__ == "__main__":
    uvicorn_module = importlib.import_module("uvicorn")
    uvicorn_module.run("agentShieldAPI:app", host="0.0.0.0", port=8000, reload=False)
