from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import base64
import hashlib
import hmac
import importlib
import json
import math
import os
import re
import secrets
import shutil
import subprocess
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
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


class ReleaseVoucherRequest(BaseModel):
    session_token: str
    reason: str = "execution_failed_or_completed"


class ReleaseVoucherResponse(BaseModel):
    decision: Decision
    released_budget_cents: int
    daily_budget_remaining_cents: int
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
    execution_path: Optional[str] = None
    message: str
    executed_at: str


class ProcessPaymentResponse(BaseModel):
    decision: str
    authorization: Dict[str, object]
    mpp_execution: MPPExecutionResult


config = GatewayConfig()
registered_agents = {"agent_alpha", "agent_beta", "agent_ops"}


RESERVE_DAILY_BUDGET_LUA = """
local budget_key = KEYS[1]
local voucher_key = KEYS[2]
local meta_key = KEYS[3]

local requested_usd = tonumber(ARGV[1])
local requested_vendor_cents = tonumber(ARGV[2])
local ttl_seconds = tonumber(ARGV[3])
local meta_agent_id = ARGV[4]
local meta_vendor_url = ARGV[5]
local meta_currency = ARGV[6]

local current_budget = redis.call("GET", budget_key)
if not current_budget then
    return {0, 0, "NO_BUDGET_FOUND"}
end
current_budget = tonumber(current_budget)

if current_budget >= requested_usd then
    redis.call("DECRBY", budget_key, requested_usd)
    redis.call("SETEX", voucher_key, ttl_seconds, requested_vendor_cents)
    redis.call("HSET", meta_key,
        "agent_id", meta_agent_id,
        "vendor_url", meta_vendor_url,
        "currency", meta_currency,
        "requested_vendor_cents", requested_vendor_cents,
        "reserved_usd_cents", requested_usd,
        "budget_key", budget_key
    )
    redis.call("EXPIRE", meta_key, ttl_seconds)
    local remaining_budget = current_budget - requested_usd
    return {1, remaining_budget, "APPROVED"}
else
    return {0, current_budget, "INSUFFICIENT_FUNDS"}
end
"""


DECREMENT_VOUCHER_LUA = """
local voucher_key = KEYS[1]
local challenge_key = KEYS[2]

local amount_cents = tonumber(ARGV[1])
local challenge_ttl = tonumber(ARGV[2])

local is_new_challenge = redis.call("SET", challenge_key, "1", "NX", "EX", challenge_ttl)
if not is_new_challenge then
    return {0, -1, "REJECTED_REPLAY_DETECTED"}
end

local current_balance = redis.call("GET", voucher_key)
if not current_balance then
    return {0, -1, "REJECTED_VOUCHER_EXPIRED_OR_MISSING"}
end

current_balance = tonumber(current_balance)
if current_balance >= amount_cents then
    local new_balance = redis.call("DECRBY", voucher_key, amount_cents)
    return {1, new_balance, "APPROVED"}
else
    return {0, current_balance, "REJECTED_INSUFFICIENT_VOUCHER_FUNDS"}
end
"""


RELEASE_VOUCHER_LUA = """
local voucher_key = KEYS[1]
local meta_key = KEYS[2]

local balance_str = redis.call("GET", voucher_key)
if not balance_str then
    return {0, 0, "VOUCHER_NOT_FOUND"}
end

local balance = tonumber(balance_str)
local meta = redis.call("HMGET", meta_key, "requested_vendor_cents", "reserved_usd_cents", "budget_key")

local requested_vendor = tonumber(meta[1])
local reserved_usd = tonumber(meta[2])
local budget_key = meta[3]

local refund_usd = 0
if requested_vendor and requested_vendor > 0 then
    refund_usd = math.floor((balance / requested_vendor) * reserved_usd)
end

if refund_usd > 0 then
    redis.call("INCRBY", budget_key, refund_usd)
end

redis.call("DEL", voucher_key)
redis.call("DEL", meta_key)

return {1, refund_usd, "RELEASED"}
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


def _fx_rates_to_usd() -> Dict[str, float]:
    raw = os.getenv("FX_RATES_TO_USD_JSON", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                rates: Dict[str, float] = {"USD": 1.0}
                for k, v in parsed.items():
                    try:
                        rates[str(k).upper()] = float(v)
                    except Exception:
                        continue
                return rates
        except Exception:
            pass
    # Conservative defaults for local/dev only.
    return {
        "USD": 1.0,
        "EUR": 1.08,
        "GBP": 1.26,
        "CAD": 0.74,
        "AUD": 0.66,
        "JPY": 0.0067,
    }


def convert_to_usd_budget_cents(amount_cents: int, currency: str) -> int:
    rates = _fx_rates_to_usd()
    fx = rates.get(currency.upper())
    if fx is None:
        raise ValueError(f"Unsupported currency for budget conversion: {currency}")
    # Round up to avoid under-reserving budget.
    return max(1, int(math.ceil(float(amount_cents) * fx)))


def build_signed_intent(payload: Dict[str, object]) -> str:
    packed = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(config.signing_secret.encode("utf-8"), packed, hashlib.sha256).hexdigest()
    payload_b64 = base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")
    return f"mpp_intent_v1.{payload_b64}.{signature}"


def verify_and_unpack_signed_intent(signed_payment_intent: str) -> Optional[Dict[str, object]]:
    parts = signed_payment_intent.split(".")
    if len(parts) != 3 or parts[0] != "mpp_intent_v1":
        return None

    payload_b64, provided_sig = parts[1], parts[2]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        packed = base64.urlsafe_b64decode((payload_b64 + padding).encode("ascii"))
    except Exception:
        return None

    expected_sig = hmac.new(config.signing_secret.encode("utf-8"), packed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, provided_sig):
        return None

    try:
        unpacked = json.loads(packed.decode("utf-8"))
    except Exception:
        return None
    return unpacked if isinstance(unpacked, dict) else None


def execute_mpp_payment_real(
    *,
    signed_payment_intent: str,
    payment_request: Dict[str, object],
) -> MPPExecutionResult:
    now = utc_now_iso()
    approved_intent = verify_and_unpack_signed_intent(signed_payment_intent)
    if not approved_intent:
        return MPPExecutionResult(
            attempted=False,
            status="INVALID_INTENT",
            transaction_id=None,
            provider="tempo-mpp",
            message="signed_payment_intent failed verification.",
            executed_at=now,
        )

    recipient_url = str(approved_intent.get("vendor_url", "")).strip()
    if not recipient_url:
        return MPPExecutionResult(
            attempted=False,
            status="INVALID_RECIPIENT",
            transaction_id=None,
            provider="tempo-mpp",
            message="Signed intent does not contain a recipient vendor URL.",
            executed_at=now,
        )

    # Preferred path: use authenticated tempo wallet session against recipient.
    use_tempo_cli = os.getenv("TEMPO_USE_CLI", "true").strip().lower() in {"1", "true", "yes"}
    if use_tempo_cli and shutil.which("tempo"):
        timeout_seconds = max(5, int(os.getenv("TEMPO_REQUEST_TIMEOUT_SECONDS", "60")))
        connect_timeout_seconds = max(2, int(os.getenv("TEMPO_REQUEST_CONNECT_TIMEOUT_SECONDS", "10")))
        retries = max(0, int(os.getenv("TEMPO_REQUEST_RETRIES", "1")))
        retry_backoff_ms = max(50, int(os.getenv("TEMPO_REQUEST_RETRY_BACKOFF_MS", "400")))
        method = os.getenv("TEMPO_REQUEST_METHOD", "POST").strip().upper() or "POST"
        network = os.getenv("TEMPO_NETWORK", "").strip()
        max_spend = max(1, int(approved_intent.get("amount_cents", 1))) / 100.0

        vendor_payload: Dict[str, object] = {
            # Many generative endpoints expect a prompt; task text is the safest default.
            "prompt": str(payment_request.get("task_description", "")),
            "agent_id": approved_intent.get("agent_id"),
            "amount_cents": approved_intent.get("amount_cents"),
            "currency": approved_intent.get("currency"),
        }
        custom_json = os.getenv("TEMPO_REQUEST_JSON", "").strip()
        if custom_json:
            try:
                parsed = json.loads(custom_json)
                if isinstance(parsed, dict):
                    vendor_payload = parsed
            except Exception:
                pass

        cmd = [
            "tempo",
            "request",
            "--json-output",
            "--silent",
            "--max-spend",
            f"{max_spend:.2f}",
            "--timeout",
            str(timeout_seconds),
            "--connect-timeout",
            str(connect_timeout_seconds),
            "--retries",
            str(retries),
            "--retry-backoff",
            str(retry_backoff_ms),
            "-X",
            method,
            "--json",
            json.dumps(vendor_payload),
            recipient_url,
        ]
        if network:
            cmd.extend(["--network", network])

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds + 10)
            if proc.returncode == 0:
                output = (proc.stdout or "").strip()
                parsed = {}
                if output.startswith("{") and output.endswith("}"):
                    try:
                        parsed = json.loads(output)
                    except Exception:
                        parsed = {}
                tx_id = parsed.get("transaction_id") or parsed.get("id")
                if not tx_id:
                    match = re.search(r"(tx_[A-Za-z0-9_-]+|mpp_[A-Za-z0-9_-]+)", output)
                    tx_id = match.group(1) if match else None
                return MPPExecutionResult(
                    attempted=True,
                    status="SUCCEEDED",
                    transaction_id=str(tx_id) if tx_id else None,
                    provider="tempo-cli",
                    message=(
                        "Payment executed via tempo request "
                        f"(timeout={timeout_seconds}s retries={retries})."
                    ),
                    executed_at=utc_now_iso(),
                )

            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            return MPPExecutionResult(
                attempted=True,
                status="PROVIDER_HTTP_ERROR",
                transaction_id=None,
                provider="tempo-cli",
                message=(
                    "tempo request failed: "
                    f"{stderr or 'non-zero exit without stderr'}"
                    + (f" | stdout={stdout[:400]}" if stdout else "")
                ),
                executed_at=utc_now_iso(),
            )
        except subprocess.TimeoutExpired:
            return MPPExecutionResult(
                attempted=True,
                status="PROVIDER_TIMEOUT",
                transaction_id=None,
                provider="tempo-cli",
                message=(
                    "tempo request timed out at wrapper level "
                    "(increase TEMPO_REQUEST_TIMEOUT_SECONDS and/or ensure endpoint responsiveness)."
                ),
                executed_at=utc_now_iso(),
            )

    # Legacy direct provider fallback.
    endpoint = os.getenv("TEMPO_MPP_ENDPOINT", "").strip()
    api_key = os.getenv("TEMPO_MPP_API_KEY", "").strip()
    if not endpoint or not api_key:
        return MPPExecutionResult(
            attempted=False,
            status="NOT_CONFIGURED",
            transaction_id=None,
            provider="tempo-mpp",
            message=(
                "Real mode requires tempo CLI login (recommended), or "
                "TEMPO_MPP_ENDPOINT + TEMPO_MPP_API_KEY for direct provider POST."
            ),
            executed_at=now,
        )

    mpp_body = {
        "authorization": {
            "intent_token": signed_payment_intent,
            "approved_at": approved_intent.get("timestamp"),
        },
        "payment": {
            "agent_id": approved_intent.get("agent_id"),
            "vendor_url": approved_intent.get("vendor_url"),
            "currency": approved_intent.get("currency"),
            "amount_cents": approved_intent.get("amount_cents"),
            "mpp_challenge_id": approved_intent.get("mpp_challenge_id"),
        },
        "context": {
            "task_description": payment_request.get("task_description", ""),
            "gateway_version": "agent-shield-2.0.0",
            "requested_at": now,
        },
    }

    challenge_id = str(approved_intent.get("mpp_challenge_id", ""))
    request_obj = urllib_request.Request(
        endpoint,
        data=json.dumps(mpp_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": challenge_id or f"idem_{secrets.token_hex(8)}",
            "X-AgentShield-Intent-Version": "v1",
        },
        method="POST",
    )

    try:
        with urllib_request.urlopen(request_obj, timeout=15) as resp:
            raw_body = resp.read().decode("utf-8")
            parsed = json.loads(raw_body) if raw_body else {}
            provider_status = str(parsed.get("status", "SUCCEEDED")).upper()
            tx_id = parsed.get("transaction_id") or parsed.get("id")
            mapped_status = "SUCCEEDED" if provider_status in {"SUCCEEDED", "APPROVED", "COMPLETED"} else provider_status
            return MPPExecutionResult(
                attempted=True,
                status=mapped_status,
                transaction_id=str(tx_id) if tx_id else None,
                provider="tempo-mpp",
                message="Real MPP execution request accepted by provider.",
                executed_at=utc_now_iso(),
            )
    except HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        return MPPExecutionResult(
            attempted=True,
            status="PROVIDER_HTTP_ERROR",
            transaction_id=None,
            provider="tempo-mpp",
            message=f"Provider returned HTTP {exc.code}: {body or 'no response body'}",
            executed_at=utc_now_iso(),
        )
    except URLError as exc:
        return MPPExecutionResult(
            attempted=True,
            status="PROVIDER_NETWORK_ERROR",
            transaction_id=None,
            provider="tempo-mpp",
            message=f"Provider network error: {exc}",
            executed_at=utc_now_iso(),
        )
    except Exception as exc:
        return MPPExecutionResult(
            attempted=True,
            status="PROVIDER_PARSE_ERROR",
            transaction_id=None,
            provider="tempo-mpp",
            message=f"Unexpected provider response handling error: {exc}",
            executed_at=utc_now_iso(),
        )


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
            execution_path="mock_adapter",
            message="Mock MPP execution succeeded.",
            executed_at=now,
        )

    if mode.lower() == "real":
        return execute_mpp_payment_real(
            signed_payment_intent=signed_payment_intent,
            payment_request=payment_request,
        )

    return MPPExecutionResult(
        attempted=False,
        status="INVALID_MODE",
        transaction_id=None,
        provider="tempo-mpp",
        execution_path="adapter_dispatch",
        message="Unsupported mpp_mode. Use 'mock' or 'real'.",
        executed_at=now,
    )


def _build_vendor_request(
    *,
    url: str,
    task_description: str,
    signed_payment_intent: Optional[str] = None,
) -> urllib_request.Request:
    method = os.getenv("TEMPO_REQUEST_METHOD", "POST").strip().upper() or "POST"
    custom_json = os.getenv("TEMPO_REQUEST_JSON", "").strip()
    payload = {"prompt": task_description} if not custom_json else {}
    if custom_json:
        try:
            parsed = json.loads(custom_json)
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {"prompt": task_description}

    data = None
    headers = {"Content-Type": "application/json"}
    if method in {"POST", "PUT", "PATCH"}:
        data = json.dumps(payload).encode("utf-8")
    if signed_payment_intent:
        headers["Authorization"] = f"Bearer {signed_payment_intent}"
        headers["X-MPP-Payment-Intent"] = signed_payment_intent
    return urllib_request.Request(url, data=data, headers=headers, method=method)


def _extract_402_details(exc: HTTPError) -> Dict[str, object]:
    raw_body = exc.read().decode("utf-8") if exc.fp else ""
    body: Dict[str, object] = {}
    if raw_body:
        try:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = {}

    headers = {k.lower(): v for k, v in dict(exc.headers.items()).items()} if exc.headers else {}
    challenge_id = (
        headers.get("x-mpp-challenge-id")
        or headers.get("mpp-challenge-id")
        or body.get("mpp_challenge_id")
        or body.get("challenge_id")
    )
    amount_cents = (
        headers.get("x-mpp-amount-cents")
        or headers.get("mpp-amount-cents")
        or body.get("amount_cents")
        or body.get("required_amount_cents")
    )
    currency = (
        headers.get("x-mpp-currency")
        or headers.get("mpp-currency")
        or body.get("currency")
        or "USD"
    )
    out: Dict[str, object] = {
        "challenge_id": str(challenge_id) if challenge_id else "",
        "currency": str(currency),
        "raw_body": raw_body,
    }
    try:
        out["amount_cents"] = int(amount_cents) if amount_cents is not None else None
    except Exception:
        out["amount_cents"] = None
    return out


def _execute_via_tempo_cli(*, vendor_url: str, task_description: str, max_spend_cents: int) -> MPPExecutionResult:
    now = utc_now_iso()
    if not shutil.which("tempo"):
        return MPPExecutionResult(
            attempted=False,
            status="NOT_CONFIGURED",
            transaction_id=None,
            provider="tempo-cli",
            execution_path="tempo_cli_fallback",
            message="tempo CLI not found for fallback transport.",
            executed_at=now,
        )

    timeout_seconds = max(5, int(os.getenv("TEMPO_REQUEST_TIMEOUT_SECONDS", "60")))
    connect_timeout_seconds = max(2, int(os.getenv("TEMPO_REQUEST_CONNECT_TIMEOUT_SECONDS", "10")))
    retries = max(0, int(os.getenv("TEMPO_REQUEST_RETRIES", "1")))
    retry_backoff_ms = max(50, int(os.getenv("TEMPO_REQUEST_RETRY_BACKOFF_MS", "400")))
    method = os.getenv("TEMPO_REQUEST_METHOD", "POST").strip().upper() or "POST"
    network = os.getenv("TEMPO_NETWORK", "").strip()

    vendor_payload: Dict[str, object] = {"prompt": task_description}
    custom_json = os.getenv("TEMPO_REQUEST_JSON", "").strip()
    if custom_json:
        try:
            parsed = json.loads(custom_json)
            if isinstance(parsed, dict):
                vendor_payload = parsed
        except Exception:
            pass

    cmd = [
        "tempo",
        "request",
        "--json-output",
        "--silent",
        "--max-spend",
        f"{max(1, max_spend_cents) / 100.0:.2f}",
        "--timeout",
        str(timeout_seconds),
        "--connect-timeout",
        str(connect_timeout_seconds),
        "--retries",
        str(retries),
        "--retry-backoff",
        str(retry_backoff_ms),
        "-X",
        method,
        "--json",
        json.dumps(vendor_payload),
        vendor_url,
    ]
    if network:
        cmd.extend(["--network", network])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds + 10)
    except subprocess.TimeoutExpired:
        return MPPExecutionResult(
            attempted=True,
            status="PROVIDER_TIMEOUT",
            transaction_id=None,
            provider="tempo-cli",
            execution_path="tempo_cli_fallback",
            message="tempo request fallback timed out.",
            executed_at=utc_now_iso(),
        )

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return MPPExecutionResult(
            attempted=True,
            status="PROVIDER_HTTP_ERROR",
            transaction_id=None,
            provider="tempo-cli",
            execution_path="tempo_cli_fallback",
            message=f"tempo fallback failed: {stderr or 'non-zero exit'}" + (f" | stdout={stdout[:400]}" if stdout else ""),
            executed_at=utc_now_iso(),
        )

    parsed = {}
    if stdout.startswith("{") and stdout.endswith("}"):
        try:
            parsed = json.loads(stdout)
        except Exception:
            parsed = {}
    tx_id = parsed.get("transaction_id") or parsed.get("id")
    if not tx_id:
        match = re.search(r"(tx_[A-Za-z0-9_-]+|mpp_[A-Za-z0-9_-]+)", stdout)
        tx_id = match.group(1) if match else None
    return MPPExecutionResult(
        attempted=True,
        status="SUCCEEDED",
        transaction_id=str(tx_id) if tx_id else None,
        provider="tempo-cli",
        execution_path="tempo_cli_fallback",
        message="Payment executed via tempo CLI fallback transport.",
        executed_at=utc_now_iso(),
    )


async def execute_mpp_402_handshake(
    *,
    redis_client: redis.Redis,
    session_token: str,
    candidate: Dict[str, object],
    task_description: str,
) -> MPPExecutionResult:
    now = utc_now_iso()
    vendor_url = str(candidate.get("recipient", "")).strip()
    fallback_amount = int(candidate.get("amount_cents", 0) or 0)
    if not vendor_url:
        return MPPExecutionResult(
            attempted=False,
            status="INVALID_RECIPIENT",
            transaction_id=None,
            provider="tempo-mpp",
            execution_path="direct_402_http",
            message="Missing vendor recipient URL.",
            executed_at=now,
        )

    # Step 1: provoke vendor to issue 402 challenge.
    initial_req = _build_vendor_request(url=vendor_url, task_description=task_description, signed_payment_intent=None)
    try:
        # Some endpoints can return 200 without payment challenge.
        with urllib_request.urlopen(initial_req, timeout=20):
            return MPPExecutionResult(
                attempted=True,
                status="SUCCEEDED",
                transaction_id=None,
                provider="tempo-mpp-http",
                execution_path="direct_402_http",
                message="Vendor request succeeded without 402 challenge.",
                executed_at=utc_now_iso(),
            )
    except HTTPError as exc:
        if exc.code != 402:
            body = exc.read().decode("utf-8") if exc.fp else ""
            fallback_enabled = os.getenv("TEMPO_FALLBACK_ON_BLOCK", "true").strip().lower() in {"1", "true", "yes"}
            if fallback_enabled and exc.code == 403 and ("1010" in body or "Access denied" in body or "Forbidden" in body):
                return _execute_via_tempo_cli(
                    vendor_url=vendor_url,
                    task_description=task_description,
                    max_spend_cents=fallback_amount,
                )
            return MPPExecutionResult(
                attempted=True,
                status="PROVIDER_HTTP_ERROR",
                transaction_id=None,
                provider="tempo-mpp-http",
                execution_path="direct_402_http",
                message=f"Vendor returned HTTP {exc.code} before challenge authorization: {body or 'no body'}",
                executed_at=utc_now_iso(),
            )
        challenge = _extract_402_details(exc)
    except URLError as exc:
        return MPPExecutionResult(
            attempted=True,
            status="PROVIDER_NETWORK_ERROR",
            transaction_id=None,
            provider="tempo-mpp-http",
            execution_path="direct_402_http",
            message=f"Vendor network error during challenge probe: {exc}",
            executed_at=utc_now_iso(),
        )

    challenge_id = str(challenge.get("challenge_id", "")).strip()
    if not challenge_id:
        return MPPExecutionResult(
            attempted=True,
            status="CHALLENGE_PARSE_ERROR",
            transaction_id=None,
            provider="tempo-mpp-http",
            execution_path="direct_402_http",
            message=f"Vendor returned 402 but no parseable challenge ID. Body: {challenge.get('raw_body', '')}",
            executed_at=utc_now_iso(),
        )

    amount_cents = challenge.get("amount_cents")
    authorize_amount = int(amount_cents) if amount_cents is not None else max(1, fallback_amount)
    auth_response = await authorize_spend_core(
        redis_client,
        AuthorizeSpendRequest(
            session_token=session_token,
            mpp_challenge_id=challenge_id,
            amount_cents=authorize_amount,
        ),
    )
    if auth_response.decision != Decision.APPROVED or not auth_response.signed_payment_intent:
        return MPPExecutionResult(
            attempted=True,
            status="AUTHORIZATION_REJECTED",
            transaction_id=None,
            provider="tempo-mpp-http",
            execution_path="direct_402_http",
            message=f"Gateway rejected challenge authorization: {auth_response.rejection_guidance or 'no guidance'}",
            executed_at=utc_now_iso(),
        )

    # Step 2: retry vendor request with payment intent attached.
    retry_req = _build_vendor_request(
        url=vendor_url,
        task_description=task_description,
        signed_payment_intent=auth_response.signed_payment_intent,
    )
    try:
        with urllib_request.urlopen(retry_req, timeout=20) as resp:
            tx_id = resp.headers.get("x-mpp-transaction-id") or resp.headers.get("x-transaction-id")
            return MPPExecutionResult(
                attempted=True,
                status="SUCCEEDED",
                transaction_id=tx_id,
                provider="tempo-mpp-http",
                execution_path="direct_402_http",
                message="402 challenge authorized and vendor request succeeded on retry.",
                executed_at=utc_now_iso(),
            )
    except HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        return MPPExecutionResult(
            attempted=True,
            status="PROVIDER_HTTP_ERROR",
            transaction_id=None,
            provider="tempo-mpp-http",
            execution_path="direct_402_http",
            message=f"Vendor rejected retry after authorization (HTTP {exc.code}): {body or 'no body'}",
            executed_at=utc_now_iso(),
        )
    except URLError as exc:
        return MPPExecutionResult(
            attempted=True,
            status="PROVIDER_NETWORK_ERROR",
            transaction_id=None,
            provider="tempo-mpp-http",
            execution_path="direct_402_http",
            message=f"Vendor network error on authorized retry: {exc}",
            executed_at=utc_now_iso(),
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
    try:
        reserve_budget_cents = convert_to_usd_budget_cents(payload.requested_amount_cents, payload.currency)
    except ValueError as exc:
        return RequestVoucherResponse(
            decision=Decision.REJECTED,
            session_token=None,
            voucher_remaining_cents=0,
            daily_budget_remaining_cents=0,
            rejection_guidance=str(exc),
        )

    # runs the lua script to reserve the budget
    await r.setnx(daily_key, config.daily_cap_cents)
    session_token = secrets.token_urlsafe(24)
    balance_key = voucher_balance_key(session_token)
    meta_key = voucher_meta_key(session_token)
    vendor = normalize_vendor(payload.vendor_url)
    reserve_raw = await r.eval(
        RESERVE_DAILY_BUDGET_LUA,
        3,
        daily_key,
        balance_key,
        meta_key,
        reserve_budget_cents,
        payload.requested_amount_cents,
        config.voucher_ttl_seconds,
        payload.agent_id,
        vendor,
        payload.currency.upper(),
    )
    reserve = list(reserve_raw) if isinstance(reserve_raw, (list, tuple)) else [0, 0, "UNKNOWN"]
    approved = int(reserve[0]) if len(reserve) > 0 else 0
    remaining = int(reserve[1]) if len(reserve) > 1 else 0
    status_code = str(reserve[2]) if len(reserve) > 2 else "UNKNOWN"

    if approved != 1:
        return RequestVoucherResponse(
            decision=Decision.REJECTED,
            session_token=None,
            voucher_remaining_cents=0,
            daily_budget_remaining_cents=remaining,
            rejection_guidance=(
                "Daily budget cap exceeded for this request."
                if status_code == "INSUFFICIENT_FUNDS"
                else f"Voucher reservation rejected: {status_code}"
            ),
        )

    return RequestVoucherResponse(
        decision=Decision.APPROVED,
        session_token=session_token,
        voucher_remaining_cents=payload.requested_amount_cents,
        daily_budget_remaining_cents=remaining,
        rejection_guidance=None,
    )


async def authorize_spend_core(r: redis.Redis, payload: AuthorizeSpendRequest) -> AuthorizeSpendResponse:
    balance_key = voucher_balance_key(payload.session_token)
    meta_key = voucher_meta_key(payload.session_token)
    challenge_id_key = challenge_key(payload.mpp_challenge_id)
    auth_raw = await r.eval(
        DECREMENT_VOUCHER_LUA,
        2,
        balance_key,
        challenge_id_key,
        payload.amount_cents,
        config.challenge_ttl_seconds,
    )
    auth = list(auth_raw) if isinstance(auth_raw, (list, tuple)) else [0, -1, "UNKNOWN"]
    approved = int(auth[0]) if len(auth) > 0 else 0
    voucher_remaining_int = int(auth[1]) if len(auth) > 1 else -1
    status_code = str(auth[2]) if len(auth) > 2 else "UNKNOWN"

    if approved != 1:
        guidance = {
            "REJECTED_REPLAY_DETECTED": "Replay detected: mpp_challenge_id already used.",
            "REJECTED_VOUCHER_EXPIRED_OR_MISSING": "Invalid or expired session_token.",
            "REJECTED_INSUFFICIENT_VOUCHER_FUNDS": "Insufficient voucher balance.",
        }.get(status_code, f"Authorization rejected: {status_code}")
        return AuthorizeSpendResponse(
            decision=Decision.REJECTED,
            signed_payment_intent=None,
            voucher_remaining_cents=max(0, voucher_remaining_int),
            rejection_guidance=guidance,
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


async def release_voucher_core(r: redis.Redis, payload: ReleaseVoucherRequest) -> ReleaseVoucherResponse:
    balance_key = voucher_balance_key(payload.session_token)
    meta_key = voucher_meta_key(payload.session_token)
    budget_day = await r.hget(meta_key, "budget_key")
    if not budget_day:
        return ReleaseVoucherResponse(
            decision=Decision.REJECTED,
            released_budget_cents=0,
            daily_budget_remaining_cents=0,
            rejection_guidance="Voucher metadata not found (already released or expired).",
        )
    release_raw = await r.eval(RELEASE_VOUCHER_LUA, 2, balance_key, meta_key)
    release = list(release_raw) if isinstance(release_raw, (list, tuple)) else [0, 0, "UNKNOWN"]
    released_ok = int(release[0]) if len(release) > 0 else 0
    released_budget_cents = int(release[1]) if len(release) > 1 else 0
    status_code = str(release[2]) if len(release) > 2 else "UNKNOWN"
    if released_ok != 1:
        return ReleaseVoucherResponse(
            decision=Decision.REJECTED,
            released_budget_cents=0,
            daily_budget_remaining_cents=0,
            rejection_guidance=f"Voucher release failed: {status_code}",
        )

    current_budget = await r.get(budget_day)
    remaining_daily = int(current_budget) if current_budget is not None else config.daily_cap_cents
    return ReleaseVoucherResponse(
        decision=Decision.APPROVED,
        released_budget_cents=released_budget_cents,
        daily_budget_remaining_cents=remaining_daily,
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


@app.post("/v1/release-voucher", response_model=ReleaseVoucherResponse)
async def release_voucher(payload: ReleaseVoucherRequest, request: Request) -> ReleaseVoucherResponse:
    r = request.app.state.redis
    return await release_voucher_core(r, payload)


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
async def process_payment(payload: ProcessPaymentRequest, request: Request) -> ProcessPaymentResponse:
    brain_module = importlib.import_module("agentShieldAgent")
    all_candidates = [
        brain_module.SpendCandidate(
            description=c.description,
            amount_cents=c.amount_cents,
            currency=c.currency,
            recipient=c.recipient,
            recurring=c.recurring,
        )
        for c in payload.candidates
    ]
    remaining_candidates = list(all_candidates)
    aggregate_log: List[str] = []
    last_mpp_result = MPPExecutionResult(
        attempted=False,
        status="SKIPPED_NOT_APPROVED",
        transaction_id=None,
        provider="tempo-mpp",
        message="No candidates executed.",
        executed_at=utc_now_iso(),
    )
    last_auth: Dict[str, object] = {"approved": False}
    max_cycles = max(1, int(payload.brain_max_cycles))

    for cycle in range(max_cycles):
        if not remaining_candidates:
            break

        brain = brain_module.AgentShieldBrain(
            brain_module.AgentBrainConfig(
                max_cycles=1,
                gateway_url=None,
                priority=payload.priority,
            )
        )
        brain_result = brain.run(
            agent_id=payload.agent_id,
            task_description=payload.task_description,
            candidates=remaining_candidates,
        )
        aggregate_log.extend(brain_result.get("reasoning_log", []))

        voucher_ok = bool(brain_result.get("approved"))
        voucher_response = brain_result.get("voucher_response") or {}
        selected_candidate = brain_result.get("selected_candidate") or {}
        session_token = str(voucher_response.get("session_token") or "")

        if not voucher_ok or not session_token:
            last_auth = {
                "approved": False,
                "cycle": cycle + 1,
                "gateway_response": voucher_response,
                "selected_candidate": selected_candidate,
            }
            last_mpp_result = MPPExecutionResult(
                attempted=False,
                status="SKIPPED_NOT_APPROVED",
                transaction_id=None,
                provider="tempo-mpp",
                message="Voucher reservation rejected; execution not started.",
                executed_at=utc_now_iso(),
            )
            break

        if payload.mpp_mode.lower() == "real":
            mpp_result = await execute_mpp_402_handshake(
                redis_client=request.app.state.redis,
                session_token=session_token,
                candidate=selected_candidate,
                task_description=payload.task_description,
            )
        else:
            signed_intent = build_signed_intent(
                {
                    "agent_id": payload.agent_id,
                    "vendor_url": selected_candidate.get("recipient", ""),
                    "currency": selected_candidate.get("currency", "USD"),
                    "session_token": session_token,
                    "mpp_challenge_id": f"mock_{secrets.token_hex(6)}",
                    "amount_cents": int(selected_candidate.get("amount_cents", 1)),
                    "timestamp": utc_now_iso(),
                }
            )
            mpp_result = execute_mpp_payment(
                signed_payment_intent=signed_intent,
                payment_request={
                    "agent_id": payload.agent_id,
                    "task_description": payload.task_description,
                    "gateway_response": voucher_response,
                },
                mode=payload.mpp_mode,
            )

        release_response = await release_voucher_core(
            request.app.state.redis,
            ReleaseVoucherRequest(session_token=session_token, reason="execution_cycle_end"),
        )
        aggregate_log.append(
            f"Released voucher after cycle {cycle + 1}: refunded {release_response.released_budget_cents} budget cents."
        )

        last_auth = {
            "approved": True,
            "cycle": cycle + 1,
            "session_token": session_token,
            "gateway_response": voucher_response,
            "selected_candidate": selected_candidate,
            "release_voucher": release_response.model_dump(mode="json"),
        }
        last_mpp_result = mpp_result

        if mpp_result.status in {"SUCCEEDED", "PENDING"}:
            return ProcessPaymentResponse(
                decision="APPROVED",
                authorization={
                    **last_auth,
                    "cycles_used": cycle,
                    "reasoning_log": aggregate_log,
                },
                mpp_execution=mpp_result,
            )

        # Execution failed -> remove current candidate and continue.
        next_candidates = []
        removed = False
        for cand in remaining_candidates:
            same = (
                cand.recipient == selected_candidate.get("recipient")
                and cand.amount_cents == int(selected_candidate.get("amount_cents", -1))
                and cand.currency == selected_candidate.get("currency")
                and cand.description == selected_candidate.get("description")
            )
            if same and not removed:
                removed = True
                continue
            next_candidates.append(cand)
        remaining_candidates = next_candidates
        aggregate_log.append(f"Execution failed at cycle {cycle + 1}; moving to next candidate.")

    return ProcessPaymentResponse(
        decision="REJECTED",
        authorization={
            **last_auth,
            "cycles_used": min(max_cycles, len(all_candidates)),
            "reasoning_log": aggregate_log,
        },
        mpp_execution=last_mpp_result,
    )


if __name__ == "__main__":
    uvicorn_module = importlib.import_module("uvicorn")
    uvicorn_module.run("agentShieldAPI:app", host="0.0.0.0", port=8000, reload=False)
