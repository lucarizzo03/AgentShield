from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import importlib
import json
import os
import secrets
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from pydantic import BaseModel, Field

fastapi_module = importlib.import_module("fastapi")
FastAPI = fastapi_module.FastAPI


class Decision(str, Enum):
    """Canonical gateway decision values used in the API response."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass
class GatewayConfig:
    """Owner-controlled policy knobs for the gateway runtime."""

    # Maximum total spend allowed per agent per day.
    daily_cap_usd: float = 50.0
    # If False, only USD is allowed.
    allow_multi_currency: bool = False
    # HMAC key used to sign outbound payment intents.
    signing_secret: str = field(default_factory=lambda: secrets.token_hex(32))


@dataclass
class AgentLedger:
    """In-memory accounting and behavior state for each agent."""

    # Running total in the active session.
    session_spend: float = 0.0
    # Running total for current day.
    daily_spend: float = 0.0
    # Per-vendor attempt count, used for loop detection.
    attempts_by_vendor: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # Timestamped payment history for velocity analysis.
    payment_events: List[Tuple[datetime, float, str]] = field(default_factory=list)


class PaymentRequest(BaseModel):
    """Details from the upstream 402/MPP payment challenge."""

    amount: float
    currency: str
    recipient: str
    mpp_challenge_id: str
    # Recurring payments are treated as owner-confirmation flows.
    recurring: bool = False


class RequestMetadata(BaseModel):
    """Telemetry provided by the worker agent for governance checks."""

    historical_session_spend: float
    daily_spend_total: float
    priority: str = Field(default="normal")
    max_retries: int = Field(default=0)


class AuthorizeSpendRequest(BaseModel):
    """Top-level request envelope accepted by POST /v1/authorize-spend."""

    agent_id: str
    task_description: str
    payment_request: PaymentRequest
    metadata: RequestMetadata


class AuditLog(BaseModel):
    """Human-readable explanation block returned on every decision."""

    check_1_context: str
    check_2_velocity: str
    check_3_value: str
    overall_reasoning: str
    agent_id: str
    session_spend_after: float
    daily_spend_after: float
    timestamp: str


class AuthorizeSpendResponse(BaseModel):
    """Exact API response contract for gateway authorization decisions."""

    decision: Decision
    auth_token_request: bool = True
    signed_payment_intent: Optional[str]
    audit_log: AuditLog
    rejection_guidance: Optional[str]


class ProcessSpendCandidate(BaseModel):
    """Candidate purchase option the brain can attempt during cycle reasoning."""

    description: str
    amount: float
    currency: str = "USD"
    recipient: str
    recurring: bool = False


class ProcessPaymentRequest(BaseModel):
    """Top-level request body for full orchestration: brain -> gateway -> MPP."""

    agent_id: str
    task_description: str
    candidates: List[ProcessSpendCandidate]
    session_spend: float = 0.0
    daily_spend: float = 0.0
    brain_max_cycles: int = 5
    priority: str = "normal"
    mpp_mode: str = "mock"


class MPPExecutionResult(BaseModel):
    """Outcome of payment handoff to Stripe/Tempo MPP rail."""

    attempted: bool
    status: str
    transaction_id: Optional[str]
    provider: str
    message: str
    executed_at: str


class ProcessPaymentResponse(BaseModel):
    """Combined orchestration output containing authorization and execution stages."""

    decision: str
    authorization: Dict[str, object]
    mpp_execution: MPPExecutionResult


# FastAPI app object for serving REST endpoints.
app = FastAPI(title="Budget Shield Gateway", version="1.0.0")
# Runtime config and in-memory state stores.
config = GatewayConfig()

# Known agents allowed to request payment authorization.
registered_agents = {"agent_alpha", "agent_beta", "agent_ops"}
# Ledger keyed by agent ID for spend state and behavior tracking.
ledgers: Dict[str, AgentLedger] = defaultdict(AgentLedger)
# Used MPP challenge IDs to block replay attacks.
used_challenges: set[str] = set()


def utc_now_iso() -> str:
    """Return a timezone-aware UTC timestamp in ISO-8601 format."""

    return datetime.now(timezone.utc).isoformat()


def normalize_vendor(recipient: str) -> str:
    """Extract a stable vendor key from a URL or plain string recipient."""

    parsed = urlparse(recipient)
    if parsed.netloc:
        return parsed.netloc.lower()
    return recipient.strip().lower()


def is_vendor_verified(recipient: str) -> bool:
    """Basic vendor verification gate: HTTPS + plausible hostname + no obvious spoof markers."""

    parsed = urlparse(recipient)
    # Require HTTPS to reduce MITM/phishing risk.
    if parsed.scheme not in {"https"}:
        return False
    # Require a domain-like host.
    if not parsed.netloc or "." not in parsed.netloc:
        return False
    # Block obvious synthetic/testing patterns.
    blocked_fragments = {"example", "test", "fake", "spoof", "localhost", "127.0.0.1"}
    lowered = recipient.lower()
    return not any(fragment in lowered for fragment in blocked_fragments)


def infer_service_type(task_description: str, recipient: str) -> str:
    """Infer benchmark category from task + vendor text for value assessment."""

    text = f"{task_description} {recipient}".lower()
    if any(k in text for k in ["llm", "openai", "anthropic", "tokens", "gpt"]):
        return "llm_api"
    if any(k in text for k in ["scrape", "crawler", "proxy", "real estate listings"]):
        return "web_scraping"
    if any(k in text for k in ["email", "smtp", "sendgrid", "mailgun", "outreach"]):
        return "email_infra"
    if any(k in text for k in ["research", "subscription", "dataset", "intel", "pricing"]):
        return "research_subscription"
    if any(k in text for k in ["api", "data", "listings", "enrichment"]):
        return "data_api"
    return "unknown"


def context_alignment_check(payload: AuthorizeSpendRequest) -> Tuple[bool, str, str]:
    """Check 1: validate that task, vendor identity, and amount are logically aligned."""

    task = payload.task_description.lower()
    recipient = payload.payment_request.recipient
    amount = payload.payment_request.amount

    # Hard stop for unknown or unverified vendors.
    if not is_vendor_verified(recipient):
        return False, "unknown_vendor", "FAIL - vendor is unverified or appears synthetic/spoofed"

    vendor_text = recipient.lower()
    aligned = False

    # Rule-based alignment by task family.
    if any(k in task for k in ["scrape", "listings", "data", "research"]):
        aligned = any(k in vendor_text for k in ["api", "data", "scrape", "proxy", "dataset", "intel"])
    elif any(k in task for k in ["python", "code", "develop", "build"]):
        aligned = any(k in vendor_text for k in ["github", "gitlab", "openai", "anthropic", "cloud", "api"])
    elif any(k in task for k in ["email", "outreach"]):
        aligned = any(k in vendor_text for k in ["sendgrid", "mailgun", "smtp", "email"])

    if not aligned:
        return False, "misaligned", "FAIL - vendor does not logically align with the stated task"

    # Example proportionality guardrail from policy prompt.
    if "10 listings" in task and amount > 50:
        return False, "disproportionate", "FAIL - requested amount is disproportionate to the task scope"

    return True, "aligned", "PASS - vendor and amount are proportionate to task requirements"


def velocity_check(payload: AuthorizeSpendRequest, ledger: AgentLedger) -> Tuple[bool, str, List[str], float]:
    """Check 2: enforce daily cap and detect retry loops / spend-velocity anomalies."""

    amount = payload.payment_request.amount
    vendor = normalize_vendor(payload.payment_request.recipient)
    retries = payload.metadata.max_retries

    # Use gateway ledger as source of truth for projected daily total.
    projected_total = ledger.daily_spend + amount
    flags: List[str] = []

    # Hard daily cap.
    if projected_total > config.daily_cap_usd:
        return False, (
            f"FAIL - projected daily total ${projected_total:.2f} exceeds cap ${config.daily_cap_usd:.2f}"
        ), flags, projected_total

    # Micro-payments get lighter sensitivity per requirements.
    micro_payment = amount < 0.10

    # Loop signal: repeated attempts to same vendor.
    if not micro_payment and ledger.attempts_by_vendor[vendor] >= 3:
        flags.append("possible retry loop: >3 attempts to same vendor in session")

    # Loop signal: repeated retries on low-dollar charge.
    if not micro_payment and retries > 1 and amount < 1.0:
        flags.append("loop behavior: max_retries > 1 on micro-payment")

    # Compare recent one-hour spend to historical baseline.
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    spend_last_hour = sum(v for (t, v, _) in ledger.payment_events if t >= one_hour_ago)
    spend_older = sum(v for (t, v, _) in ledger.payment_events if t < one_hour_ago)
    age_hours = max(1.0, (datetime.now(timezone.utc) - (ledger.payment_events[0][0] if ledger.payment_events else datetime.now(timezone.utc))).total_seconds() / 3600.0)
    baseline_hourly = spend_older / age_hours

    # Spike threshold: 2.5x baseline and meaningful absolute volume.
    if not micro_payment and baseline_hourly > 0 and spend_last_hour > 2.5 * baseline_hourly and spend_last_hour > 5:
        flags.append("spend velocity spike vs baseline; human confirmation required")

    # Special-case micro-payment flood control.
    if micro_payment:
        micro_count_last_hour = sum(1 for (t, v, _) in ledger.payment_events if t >= one_hour_ago and v < 0.10)
        if micro_count_last_hour >= 50:
            flags.append("micro-payment frequency exceeds 50/hour")

    # Any velocity/loop flag causes rejection.
    if flags:
        return False, (
            f"FAIL - projected daily total ${projected_total:.2f}; loop/velocity flags: {', '.join(flags)}"
        ), flags, projected_total

    return True, f"PASS - projected daily total ${projected_total:.2f}; no loop flags", flags, projected_total


def value_assessment_check(payload: AuthorizeSpendRequest) -> Tuple[bool, str]:
    """Check 3: benchmark requested amount against service-type pricing heuristics."""

    amount = payload.payment_request.amount
    service_type = infer_service_type(payload.task_description, payload.payment_request.recipient)
    task = payload.task_description.lower()

    # Task-level keywords that may justify above-benchmark pricing.
    premium_justification = any(k in task for k in ["enterprise", "sla", "compliance", "premium", "priority", "real-time"])

    # Recurring requires human confirmation before first charge.
    if payload.payment_request.recurring:
        if amount > 0:
            return False, "FAIL - recurring charge requires owner confirmation before first payment"

    # Benchmark checks by service class.
    if service_type == "data_api" and amount > 0.50:
        if premium_justification:
            return True, "PASS - above data API benchmark but justified by premium requirements"
        return False, "FAIL - data API cost above $0.50 benchmark without justification"

    if service_type == "llm_api" and amount > 1.00:
        if premium_justification:
            return True, "PASS - above LLM benchmark but justified by task requirements"
        return False, "FAIL - LLM call cost above $1.00 benchmark without justification"

    if service_type == "web_scraping" and amount > 0.50:
        if premium_justification:
            return True, "PASS - scraping rate above benchmark with explicit premium context"
        return False, "FAIL - scraping rate above $0.50/page benchmark without justification"

    if service_type == "email_infra" and amount > 0.05:
        if premium_justification:
            return True, "PASS - email cost above benchmark but premium justification provided"
        return False, "FAIL - email infrastructure cost above $0.05 benchmark without justification"

    if service_type == "research_subscription" and amount > 50:
        return False, "FAIL - research/data subscription above $50 requires owner approval"

    if service_type == "unknown":
        return False, "FAIL - service type unknown; cannot validate value safely"

    return True, "PASS - requested amount is within benchmark ranges"


def build_signed_intent(payload: AuthorizeSpendRequest) -> str:
    """Build an HMAC-signed payment intent token for Stripe/Tempo handoff."""

    # Canonicalized token payload.
    body = {
        "agent_id": payload.agent_id,
        "amount": payload.payment_request.amount,
        "currency": payload.payment_request.currency.upper(),
        "recipient": payload.payment_request.recipient,
        "mpp_challenge_id": payload.payment_request.mpp_challenge_id,
        "timestamp": utc_now_iso(),
    }
    # Stable JSON formatting before signing.
    packed = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(config.signing_secret.encode("utf-8"), packed, hashlib.sha256).hexdigest()
    return f"mpp_sig_{digest}"


def execute_mpp_payment(
    *,
    signed_payment_intent: str,
    payment_request: Dict[str, object],
    mode: str = "mock",
) -> MPPExecutionResult:
    """Adapter for Stripe/Tempo MPP handoff (mock by default, real path pluggable)."""

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

        # Placeholder integration point for real Tempo/Stripe MPP call.
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


def reject_response(
    payload: AuthorizeSpendRequest,
    check_1: str,
    check_2: str,
    check_3: str,
    reasoning: str,
    guidance: str,
) -> AuthorizeSpendResponse:
    """Helper to ensure rejected responses always match the exact response schema."""

    ledger = ledgers[payload.agent_id]
    return AuthorizeSpendResponse(
        decision=Decision.REJECTED,
        auth_token_request=True,
        signed_payment_intent=None,
        audit_log=AuditLog(
            check_1_context=check_1,
            check_2_velocity=check_2,
            check_3_value=check_3,
            overall_reasoning=reasoning,
            agent_id=payload.agent_id,
            session_spend_after=round(ledger.session_spend + payload.payment_request.amount, 6),
            daily_spend_after=round(ledger.daily_spend + payload.payment_request.amount, 6),
            timestamp=utc_now_iso(),
        ),
        rejection_guidance=guidance,
    )


@app.post("/v1/authorize-spend", response_model=AuthorizeSpendResponse)
def authorize_spend(payload: AuthorizeSpendRequest) -> AuthorizeSpendResponse:
    """Primary gateway endpoint: evaluate request via strict check ordering and return signed intent or rejection."""

    # Security gate: only registered worker agents may authorize spend.
    if payload.agent_id not in registered_agents:
        return reject_response(
            payload,
            "FAIL - security event: unregistered agent ID",
            "FAIL - not evaluated due to security rejection",
            "FAIL - not evaluated due to security rejection",
            "Rejected because the agent identity is not recognized in the gateway ledger. This is treated as a security event and no payment intent can be issued.",
            "Register the agent in the gateway ledger before retrying this authorization request.",
        )

    # Security gate: challenge IDs are one-time use to block replay attacks.
    if payload.payment_request.mpp_challenge_id in used_challenges:
        return reject_response(
            payload,
            "FAIL - security event: replayed MPP challenge ID",
            "FAIL - not evaluated due to security rejection",
            "FAIL - not evaluated due to security rejection",
            "Rejected because the challenge ID has already been used, which indicates a potential replay attack. The gateway blocks all reused payment challenges.",
            "Fetch a fresh 402 challenge from the vendor and submit a new authorization request.",
        )

    # Currency policy gate.
    if not config.allow_multi_currency and payload.payment_request.currency.upper() != "USD":
        return reject_response(
            payload,
            "FAIL - currency policy violation",
            "FAIL - not evaluated due to policy rejection",
            "FAIL - not evaluated due to policy rejection",
            "Rejected because the payment is not denominated in USD while multi-currency is disabled. The gateway cannot sign this payment intent under current owner policy.",
            "Resubmit the payment request in USD or enable multi-currency in owner settings.",
        )

    ledger = ledgers[payload.agent_id]

    # Integrity gate: cross-check agent-reported spend against gateway ledger source-of-truth.
    if abs(payload.metadata.historical_session_spend - ledger.session_spend) > 0.01:
        return reject_response(
            payload,
            "FAIL - session spend mismatch between agent payload and gateway ledger",
            "FAIL - not evaluated due to ledger integrity flag",
            "FAIL - not evaluated due to ledger integrity flag",
            "Rejected because the agent-reported session spend does not match gateway records. The request is blocked to prevent tampered or stale spend state from reaching Stripe.",
            "Sync your local spend counters with gateway ledger state, then retry with accurate metadata.",
        )

    # Check 1: Context Alignment.
    check_1_pass, check_1_code, check_1_text = context_alignment_check(payload)
    if not check_1_pass:
        guidance_map = {
            "unknown_vendor": "Use a verifiable HTTPS API vendor that clearly matches the task scope.",
            "misaligned": "Choose a vendor directly related to the task and justify the amount requested.",
            "disproportionate": "Reduce the amount to match task scope or provide explicit task-scale justification.",
        }
        return reject_response(
            payload,
            check_1_text,
            "FAIL - not evaluated because context alignment failed",
            "FAIL - not evaluated because context alignment failed",
            "Rejected at context alignment: the requested purchase does not safely map to the assigned task. The gateway halts at check 1 by design and does not run downstream authorization logic.",
            guidance_map.get(check_1_code, "Provide clearer task-to-vendor alignment and retry."),
        )

    # Check 2: Velocity and loop detection.
    check_2_pass, check_2_text, loop_flags, projected_total = velocity_check(payload, ledger)
    if not check_2_pass:
        return reject_response(
            payload,
            check_1_text,
            check_2_text,
            "FAIL - not evaluated because velocity check failed",
            "Rejected at velocity check to prevent spend loops or cap overrun. The projected daily total and retry behavior indicate elevated drain risk, so the gateway blocks before value assessment.",
            "Wait for spend velocity to normalize, reduce retries, or lower the amount so projected daily spend stays under cap.",
        )

    # Check 3: Value assessment against benchmark ranges.
    check_3_pass, check_3_text = value_assessment_check(payload)
    if not check_3_pass:
        return reject_response(
            payload,
            check_1_text,
            check_2_text,
            check_3_text,
            "Rejected at value assessment because pricing appears above benchmark without sufficient justification or requires owner confirmation. The gateway protects the owner from high-cost defaults.",
            "Provide benchmark justification, choose a lower-cost equivalent vendor, or request explicit owner approval for premium/recurring spend.",
        )

    # Commit usage only after all checks pass and authorization is approved.
    signed_intent = build_signed_intent(payload)
    used_challenges.add(payload.payment_request.mpp_challenge_id)

    # Update ledger after approval so future checks see latest state.
    vendor_key = normalize_vendor(payload.payment_request.recipient)
    ledger.attempts_by_vendor[vendor_key] += 1
    ledger.session_spend += payload.payment_request.amount
    ledger.daily_spend += payload.payment_request.amount
    ledger.payment_events.append((datetime.now(timezone.utc), payload.payment_request.amount, vendor_key))

    loop_note = "none" if not loop_flags else ", ".join(loop_flags)
    return AuthorizeSpendResponse(
        decision=Decision.APPROVED,
        auth_token_request=True,
        signed_payment_intent=signed_intent,
        audit_log=AuditLog(
            check_1_context=check_1_text,
            check_2_velocity=f"PASS - projected daily total ${projected_total:.2f}; loop flags: {loop_note}",
            check_3_value=check_3_text,
            overall_reasoning=(
                "Approved after passing context alignment, velocity, and value checks in sequence. "
                "The request stays within daily cap and benchmark guardrails, so the gateway issued a signed payment intent for Stripe/Tempo execution."
            ),
            agent_id=payload.agent_id,
            session_spend_after=round(ledger.session_spend, 6),
            daily_spend_after=round(ledger.daily_spend, 6),
            timestamp=utc_now_iso(),
        ),
        rejection_guidance=None,
    )


@app.get("/v1/ledger/{agent_id}")
def get_agent_ledger(agent_id: str) -> Dict[str, float | str]:
    """Read-only helper endpoint for owner dashboard visibility."""

    if agent_id not in registered_agents:
        return {
            "status": "not_found",
            "agent_id": agent_id,
            "message": "agent not registered",
        }

    ledger = ledgers[agent_id]
    return {
        "status": "ok",
        "agent_id": agent_id,
        "session_spend": round(ledger.session_spend, 6),
        "daily_spend": round(ledger.daily_spend, 6),
        "daily_cap": round(config.daily_cap_usd, 6),
    }


@app.post("/v1/process-payment", response_model=ProcessPaymentResponse)
def process_payment(payload: ProcessPaymentRequest) -> ProcessPaymentResponse:
    """Orchestrate full flow: Agent brain reasoning -> gateway auth -> Stripe/Tempo MPP execution."""

    # Lazy import avoids startup circular dependency between API and brain module.
    brain_module = importlib.import_module("agentShieldAgent")

    spend_candidates = [
        brain_module.SpendCandidate(
            description=c.description,
            amount=c.amount,
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
        session_spend=payload.session_spend,
        daily_spend=payload.daily_spend,
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
    # Best-effort extraction of the selected payment request from gateway payload.
    payment_request = {}
    if isinstance(gateway_response, dict):
        audit = gateway_response.get("audit_log", {})
        payment_request = {
            "agent_id": payload.agent_id,
            "task_description": payload.task_description,
            "audit_log": audit,
        }

    mpp_result = execute_mpp_payment(
        signed_payment_intent=signed_intent,
        payment_request=payment_request,
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
    # Local dev server entrypoint.
    uvicorn_module = importlib.import_module("uvicorn")
    uvicorn_module.run("agentShieldAPI:app", host="0.0.0.0", port=8000, reload=False)
