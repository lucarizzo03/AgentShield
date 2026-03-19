from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, TypedDict
import importlib
import uuid

try:
	langgraph_module = importlib.import_module("langgraph.graph")
	END = langgraph_module.END
	START = langgraph_module.START
	StateGraph = langgraph_module.StateGraph
	LANGGRAPH_AVAILABLE = True
except ImportError:
	LANGGRAPH_AVAILABLE = False
	END = START = StateGraph = None


class TransactionType(str, Enum):
	ONE_TIME = "One-time"
	RECURRING = "Recurring"


class AuthorityLevel(str, Enum):
	FULL = "FULL"
	REDUCED = "REDUCED"
	RESTRICTED = "RESTRICTED"
	SUSPENDED = "SUSPENDED"


@dataclass
class TransactionRequest:
	description: str
	amount: float
	vendor: str
	tx_type: TransactionType
	conversation_context: str = ""
	category: str = "other"
	evidence: Dict[str, str] = field(default_factory=dict)
	owner_pre_approved: bool = False
	is_refund: bool = False
	original_transaction_id: Optional[str] = None


@dataclass
class DecisionResult:
	approved: bool
	output: str
	transaction_id: Optional[str] = None


class TransactionGraphState(TypedDict):
	request: TransactionRequest
	evaluation: Dict[str, Any]
	approved: bool
	iteration: int
	max_iterations: int
	update_queue: List[Dict[str, Any]]
	notes: List[str]


class BudgetShield:
	"""Autonomous spending agent with sentiment, policy, and performance controls."""

	BLOCKED_CATEGORIES = {
		"gambling",
		"crypto exchanges",
		"crypto",
		"forex",
		"adult content",
		"adult",
	}

	def __init__(self, starting_balance: float, hard_spend_limit: float) -> None:
		if starting_balance < 0 or hard_spend_limit < 0:
			raise ValueError("starting_balance and hard_spend_limit must be non-negative")

		self.starting_balance = float(starting_balance)
		self.balance = float(starting_balance)
		self.hard_spend_limit = float(hard_spend_limit)

		self.spent_total = 0.0
		self.blocked_total = 0.0
		self.monthly_commitment = 0.0

		self.approved_count = 0
		self.flagged_count = 0

		self.processed_transaction_ids: Set[str] = set()
		self.transactions: Dict[str, TransactionRequest] = {}

	def success_rate(self) -> float:
		# Bootstrap as 100% until enough decisions exist to evaluate.
		if self.approved_count == 0:
			return 1.0
		return max(0.0, (self.approved_count - self.flagged_count) / self.approved_count)

	def authority(self) -> AuthorityLevel:
		rate = self.success_rate()
		if rate >= 0.85:
			return AuthorityLevel.FULL
		if rate >= 0.70:
			return AuthorityLevel.REDUCED
		if rate >= 0.50:
			return AuthorityLevel.RESTRICTED
		return AuthorityLevel.SUSPENDED

	def authority_multiplier(self) -> float:
		level = self.authority()
		if level == AuthorityLevel.FULL:
			return 1.0
		if level == AuthorityLevel.REDUCED:
			return 0.6
		if level == AuthorityLevel.RESTRICTED:
			return 0.25
		return 0.0

	def remaining_limit(self) -> float:
		return max(0.0, self.hard_spend_limit - self.spent_total)

	def sentiment_check(self, context: str) -> tuple[bool, float, List[str]]:
		text = (context or "").lower()
		score = 0.75
		signals: List[str] = []

		suspicious_phrases = [
			"urgent",
			"immediately",
			"act now",
			"wire",
			"crypto only",
			"do not verify",
			"trust me",
			"everyone else approved",
			"perfect timing",
		]
		genuine_markers = [
			"receipt",
			"order id",
			"invoice",
			"photo",
			"tracking",
			"screenshot",
			"support ticket",
			"documented",
		]

		for phrase in suspicious_phrases:
			if phrase in text:
				score -= 0.08
				signals.append(f"manipulation signal: '{phrase}'")

		for marker in genuine_markers:
			if marker in text:
				score += 0.05

		if "!!!" in text or text.count("!") >= 3:
			score -= 0.06
			signals.append("excessive urgency punctuation")

		if len(text.strip()) < 20:
			score -= 0.07
			signals.append("insufficient context")

		score = min(1.0, max(0.0, score))
		return score >= 0.6, score, signals

	def policy_check(self, request: TransactionRequest) -> tuple[bool, List[str]]:
		violations: List[str] = []
		category = request.category.strip().lower()

		if category in self.BLOCKED_CATEGORIES:
			violations.append(f"blocked vendor category: {request.category}")

		if request.is_refund and request.amount > 40:
			has_docs = any(
				key in request.evidence and request.evidence[key].strip()
				for key in ("photo", "receipt", "order_id")
			)
			if not has_docs:
				violations.append("refund over $40 requires photo, receipt, or order ID evidence")

		if request.tx_type == TransactionType.RECURRING and request.amount > 20 and not request.owner_pre_approved:
			violations.append("recurring subscriptions over $20/month require owner pre-approval")

		if request.amount > self.remaining_limit():
			violations.append("transaction exceeds remaining spend limit")

		if request.amount > self.balance:
			violations.append("insufficient wallet balance")

		if request.is_refund and request.original_transaction_id in self.processed_transaction_ids:
			violations.append("cannot approve a refund for a transaction this agent originally processed")

		return len(violations) == 0, violations

	def performance_check(self, request: TransactionRequest) -> tuple[bool, str, float]:
		level = self.authority()
		rate = self.success_rate()
		effective_limit = self.hard_spend_limit * self.authority_multiplier()

		if level == AuthorityLevel.SUSPENDED:
			return False, "SUSPENDED", rate

		if level == AuthorityLevel.RESTRICTED and request.tx_type == TransactionType.RECURRING:
			return False, level.value, rate

		if request.amount > effective_limit:
			return False, level.value, rate

		return True, level.value, rate

	def _wallet_status_line(self) -> str:
		return (
			f"WALLET STATUS: Balance ${self.balance:.2f} | Spent ${self.spent_total:.2f} | "
			f"Blocked ${self.blocked_total:.2f} | Limit ${self.hard_spend_limit:.2f}"
		)

	def _evaluate_request(self, request: TransactionRequest) -> Dict[str, Any]:
		sentiment_pass, sentiment_score, sentiment_signals = self.sentiment_check(request.conversation_context)
		policy_pass, policy_violations = self.policy_check(request)
		performance_pass, authority_level, success_rate = self.performance_check(request)

		approved = sentiment_pass and policy_pass and performance_pass

		if approved:
			reason = "All three checks passed and the transaction is within autonomous authority and wallet constraints."
		else:
			reasons: List[str] = []
			if not sentiment_pass:
				if sentiment_signals:
					reasons.append("sentiment check failed due to manipulation signals")
				else:
					reasons.append("sentiment score is below threshold")
			if not policy_pass:
				reasons.append("policy violations detected")
			if not performance_pass:
				if authority_level == AuthorityLevel.SUSPENDED.value:
					reasons.append("authority suspended; human approval required")
				else:
					reasons.append("transaction exceeds current performance-based authority")
			reason = "; ".join(reasons) + "."

		return {
			"sentiment_pass": sentiment_pass,
			"sentiment_score": sentiment_score,
			"sentiment_signals": sentiment_signals,
			"policy_pass": policy_pass,
			"policy_violations": policy_violations,
			"performance_pass": performance_pass,
			"authority_level": authority_level,
			"success_rate": success_rate,
			"approved": approved,
			"reason": reason,
		}

	def _apply_reconsideration_update(self, request: TransactionRequest, update: Dict[str, Any]) -> TransactionRequest:
		"""Apply incremental fixes provided by owner/requester before re-running checks."""
		if "conversation_context" in update and isinstance(update["conversation_context"], str):
			request.conversation_context = update["conversation_context"]

		if "owner_pre_approved" in update:
			request.owner_pre_approved = bool(update["owner_pre_approved"])

		if "category" in update and isinstance(update["category"], str):
			request.category = update["category"]

		if "evidence" in update and isinstance(update["evidence"], dict):
			for key, value in update["evidence"].items():
				request.evidence[str(key)] = str(value)

		return request

	def _build_langgraph(self):
		if not LANGGRAPH_AVAILABLE:
			return None

		def evaluate_node(state: TransactionGraphState) -> TransactionGraphState:
			state["evaluation"] = self._evaluate_request(state["request"])
			state["approved"] = bool(state["evaluation"]["approved"])
			return state

		def reconsideration_node(state: TransactionGraphState) -> TransactionGraphState:
			if state["approved"]:
				return state

			if state["iteration"] >= state["max_iterations"]:
				state["notes"].append("max reconsideration iterations reached")
				return state

			if not state["update_queue"]:
				state["notes"].append("no reconsideration updates provided")
				return state

			update = state["update_queue"].pop(0)
			state["request"] = self._apply_reconsideration_update(state["request"], update)
			state["iteration"] += 1
			state["notes"].append(f"reconsideration update #{state['iteration']} applied")
			return state

		def route_after_evaluate(state: TransactionGraphState) -> str:
			return "approve" if state["approved"] else "reconsider"

		def route_after_reconsider(state: TransactionGraphState) -> str:
			if state["approved"]:
				return "done"
			has_more_updates = bool(state["update_queue"])
			can_retry = state["iteration"] < state["max_iterations"]
			return "retry" if (has_more_updates and can_retry) else "done"

		graph = StateGraph(TransactionGraphState)
		graph.add_node("evaluate", evaluate_node)
		graph.add_node("reconsider", reconsideration_node)

		graph.add_edge(START, "evaluate")
		graph.add_conditional_edges(
			"evaluate",
			route_after_evaluate,
			{"approve": END, "reconsider": "reconsider"},
		)
		graph.add_conditional_edges(
			"reconsider",
			route_after_reconsider,
			{"retry": "evaluate", "done": END},
		)

		return graph.compile()

	def process_transaction(
		self,
		request: TransactionRequest,
		reconsideration_updates: Optional[List[Dict[str, Any]]] = None,
		max_reconsideration_loops: int = 2,
	) -> DecisionResult:
		tx_id = str(uuid.uuid4())
		evaluation: Dict[str, Any]

		if LANGGRAPH_AVAILABLE:
			compiled_graph = self._build_langgraph()
			initial_state: TransactionGraphState = {
				"request": request,
				"evaluation": {},
				"approved": False,
				"iteration": 0,
				"max_iterations": max(0, max_reconsideration_loops),
				"update_queue": list(reconsideration_updates or []),
				"notes": [],
			}
			final_state = compiled_graph.invoke(initial_state)
			evaluation = final_state["evaluation"]
			request = final_state["request"]
		else:
			evaluation = self._evaluate_request(request)

		sentiment_pass = evaluation["sentiment_pass"]
		sentiment_score = evaluation["sentiment_score"]
		sentiment_signals = evaluation["sentiment_signals"]
		policy_pass = evaluation["policy_pass"]
		policy_violations = evaluation["policy_violations"]
		performance_pass = evaluation["performance_pass"]
		authority_level = evaluation["authority_level"]
		success_rate = evaluation["success_rate"]
		approved = evaluation["approved"]
		reason = evaluation["reason"]

		if approved:
			self.balance -= request.amount
			self.spent_total += request.amount
			self.approved_count += 1
			if request.tx_type == TransactionType.RECURRING:
				self.monthly_commitment += request.amount
			self.processed_transaction_ids.add(tx_id)
			self.transactions[tx_id] = request
			verdict = "APPROVED ✓"
		else:
			self.blocked_total += request.amount
			verdict = "BLOCKED ✗"

		tx_type_label = (
			f"Recurring (${request.amount:.2f}/mo)"
			if request.tx_type == TransactionType.RECURRING
			else "One-time"
		)

		sentiment_status = "PASS" if sentiment_pass else "FAIL"
		policy_status = "PASS" if policy_pass else "FAIL"
		performance_status = "PASS" if performance_pass else "FAIL"

		violations_text = "none"
		if not policy_pass:
			violations_text = "; ".join(policy_violations)

		decision_text = (
			f"TRANSACTION: {request.description} — ${request.amount:.2f} to {request.vendor}\n"
			f"TYPE: {tx_type_label}\n\n"
			f"CHECK 1 — SENTIMENT:    {sentiment_status} | Score: {sentiment_score:.2f}\n"
			f"CHECK 2 — POLICY:       {policy_status} | Violations: {violations_text}\n"
			f"CHECK 3 — PERFORMANCE:  {performance_status} | Success Rate: {success_rate * 100:.1f}% | Authority: {authority_level}\n\n"
			f"VERDICT: {verdict}\n"
			f"REASON:  {reason}\n\n"
			f"{self._wallet_status_line()}"
		)

		if not approved:
			decision_text += (
				"\nRECONSIDERATION: Provide missing evidence/owner approval, remove policy conflicts, "
				"or improve decision performance history to raise authority."
			)
			if not LANGGRAPH_AVAILABLE:
				decision_text += "\nLANGGRAPH: not installed; executed single-pass fallback."

		return DecisionResult(approved=approved, output=decision_text, transaction_id=(tx_id if approved else None))

	def flag_previous_approval(self, count: int = 1) -> None:
		if count < 0:
			raise ValueError("count must be non-negative")
		self.flagged_count += count


if __name__ == "__main__":
	shield = BudgetShield(starting_balance=500.0, hard_spend_limit=300.0)

	sample = TransactionRequest(
		description="Customer refund for damaged headset",
		amount=45.0,
		vendor="AudioHub",
		tx_type=TransactionType.ONE_TIME,
		conversation_context=(
			"Customer shared receipt and order ID, plus photo evidence of visible damage. "
			"No urgency language; standard support escalation."
		),
		category="retail",
		evidence={"receipt": "R-2191", "order_id": "ORD-6621", "photo": "img_0021.jpg"},
		is_refund=True,
	)

	result = shield.process_transaction(sample)
	print(result.output)
