from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib
import json
from typing import Any, Dict, List, Optional, TypedDict
from urllib import request as urllib_request
from urllib.error import URLError, HTTPError
import uuid


langgraph_module = importlib.import_module("langgraph.graph")
START = langgraph_module.START
END = langgraph_module.END
StateGraph = langgraph_module.StateGraph


@dataclass
class SpendCandidate:
	"""A single payment path the agent can try for a task."""

	description: str
	amount: float
	currency: str
	recipient: str
	recurring: bool = False


@dataclass
class AgentBrainConfig:
	"""Controls for LangGraph loop depth and gateway target."""

	max_cycles: int = 5
	gateway_url: Optional[str] = None
	priority: str = "normal"


class AgentBrainState(TypedDict):
	"""Serializable state carried across the LangGraph cycle."""

	agent_id: str
	task_description: str
	session_spend: float
	daily_spend: float
	candidate_index: int
	candidate_retries: int
	cycles_used: int
	max_cycles: int
	candidates: List[Dict[str, Any]]
	request_payload: Optional[Dict[str, Any]]
	gateway_response: Optional[Dict[str, Any]]
	approved: bool
	done: bool
	signed_payment_intent: Optional[str]
	reasoning_log: List[str]
	priority: str
	gateway_url: Optional[str]


class AgentShieldBrain:
	"""Worker-agent brain that reasons in cycles before spending."""

	def __init__(self, config: Optional[AgentBrainConfig] = None) -> None:
		self.config = config or AgentBrainConfig()
		self.graph = self._build_graph()

	def _build_graph(self):
		graph = StateGraph(AgentBrainState)
		graph.add_node("sync_ledger", self._sync_ledger_node)
		graph.add_node("prepare_request", self._prepare_request_node)
		graph.add_node("authorize", self._authorize_node)
		graph.add_node("reflect_adjust", self._reflect_adjust_node)

		graph.add_edge(START, "sync_ledger")
		graph.add_edge("sync_ledger", "prepare_request")
		graph.add_conditional_edges(
			"prepare_request",
			self._route_after_prepare,
			{"authorize": "authorize", "done": END},
		)
		graph.add_conditional_edges(
			"authorize",
			self._route_after_authorize,
			{"approved": END, "reflect": "reflect_adjust", "done": END},
		)
		graph.add_conditional_edges(
			"reflect_adjust",
			self._route_after_reflect,
			{"retry": "prepare_request", "done": END},
		)
		return graph.compile()

	def _sync_ledger_node(self, state: AgentBrainState) -> AgentBrainState:
		ledger = self._fetch_gateway_ledger(state["agent_id"], state["gateway_url"])
		if ledger and ledger.get("status") == "ok":
			state["session_spend"] = float(ledger.get("session_spend", state["session_spend"]))
			state["daily_spend"] = float(ledger.get("daily_spend", state["daily_spend"]))
			state["reasoning_log"].append(
				f"Synced ledger state (session=${state['session_spend']:.4f}, daily=${state['daily_spend']:.4f})."
			)
		else:
			state["reasoning_log"].append("Ledger sync unavailable; continuing with provided spend snapshot.")
		return state

	def run(
		self,
		agent_id: str,
		task_description: str,
		candidates: List[SpendCandidate],
		session_spend: float = 0.0,
		daily_spend: float = 0.0,
	) -> Dict[str, Any]:
		initial_state: AgentBrainState = {
			"agent_id": agent_id,
			"task_description": task_description,
			"session_spend": session_spend,
			"daily_spend": daily_spend,
			"candidate_index": 0,
			"candidate_retries": 0,
			"cycles_used": 0,
			"max_cycles": self.config.max_cycles,
			"candidates": [asdict(c) for c in candidates],
			"request_payload": None,
			"gateway_response": None,
			"approved": False,
			"done": False,
			"signed_payment_intent": None,
			"reasoning_log": [],
			"priority": self.config.priority,
			"gateway_url": self.config.gateway_url,
		}
		final_state = self.graph.invoke(initial_state)

		return {
			"approved": final_state["approved"],
			"signed_payment_intent": final_state["signed_payment_intent"],
			"gateway_response": final_state["gateway_response"],
			"cycles_used": final_state["cycles_used"],
			"candidate_index": final_state["candidate_index"],
			"reasoning_log": final_state["reasoning_log"],
		}

	def _prepare_request_node(self, state: AgentBrainState) -> AgentBrainState:
		if state["done"]:
			return state

		if state["candidate_index"] >= len(state["candidates"]):
			state["done"] = True
			state["reasoning_log"].append("No remaining candidate vendors to try.")
			return state

		candidate = state["candidates"][state["candidate_index"]]
		challenge_id = f"ch_{state['agent_id']}_{state['cycles_used']}_{uuid.uuid4().hex[:8]}"

		state["request_payload"] = {
			"agent_id": state["agent_id"],
			"task_description": state["task_description"],
			"payment_request": {
				"amount": float(candidate["amount"]),
				"currency": str(candidate["currency"]),
				"recipient": str(candidate["recipient"]),
				"mpp_challenge_id": challenge_id,
				"recurring": bool(candidate.get("recurring", False)),
			},
			"metadata": {
				"historical_session_spend": float(state["session_spend"]),
				"daily_spend_total": float(state["daily_spend"]),
				"priority": state["priority"],
				"max_retries": int(state["candidate_retries"]),
			},
		}
		state["reasoning_log"].append(
			f"Cycle {state['cycles_used'] + 1}: prepared request for {candidate['recipient']} at ${candidate['amount']:.4f}."
		)
		return state

	def _authorize_node(self, state: AgentBrainState) -> AgentBrainState:
		if state["done"] or not state["request_payload"]:
			state["done"] = True
			return state

		response = self._call_gateway(state["request_payload"], state["gateway_url"])
		state["gateway_response"] = response

		decision_raw = response.get("decision", "REJECTED")
		if hasattr(decision_raw, "value"):
			decision = str(decision_raw.value).upper()
		else:
			decision = str(decision_raw).upper()

		if decision == "APPROVED":
			state["approved"] = True
			state["done"] = True
			state["signed_payment_intent"] = response.get("signed_payment_intent")
			state["reasoning_log"].append("Gateway approved request and issued signed payment intent.")
			return state

		state["approved"] = False
		state["reasoning_log"].append("Gateway rejected request; entering reflection step.")
		return state

	def _reflect_adjust_node(self, state: AgentBrainState) -> AgentBrainState:
		if state["done"]:
			return state

		state["cycles_used"] += 1
		if state["cycles_used"] >= state["max_cycles"]:
			state["done"] = True
			state["reasoning_log"].append("Reached max reasoning cycles; stopping.")
			return state

		response = state.get("gateway_response") or {}
		audit = response.get("audit_log", {}) if isinstance(response, dict) else {}
		check_1 = str(audit.get("check_1_context", ""))
		check_2 = str(audit.get("check_2_velocity", ""))
		check_3 = str(audit.get("check_3_value", ""))
		guidance = str(response.get("rejection_guidance", ""))

		if state["candidate_index"] >= len(state["candidates"]):
			state["done"] = True
			return state

		candidate = state["candidates"][state["candidate_index"]]
		lowered = " ".join([check_1, check_2, check_3, guidance]).lower()

		# Stale metadata can be fixed by refreshing source-of-truth ledger values.
		if "session spend mismatch" in lowered or "ledger" in lowered and "mismatch" in lowered:
			ledger = self._fetch_gateway_ledger(state["agent_id"], state["gateway_url"])
			if ledger and ledger.get("status") == "ok":
				state["session_spend"] = float(ledger.get("session_spend", state["session_spend"]))
				state["daily_spend"] = float(ledger.get("daily_spend", state["daily_spend"]))
				state["reasoning_log"].append("Reflection: refreshed spend metadata from gateway ledger.")
				return state

		# Context alignment or vendor trust issues: move to next candidate vendor.
		if "unverified" in lowered or "misaligned" in lowered or "vendor" in lowered and "align" in lowered:
			state["candidate_index"] += 1
			state["candidate_retries"] = 0
			state["reasoning_log"].append("Reflection: vendor/context misaligned, switching to next candidate.")
			return state

		# Value/cost issues: lower amount and retry same vendor.
		if "above" in lowered and "benchmark" in lowered or "high-cost" in lowered:
			old_amount = float(candidate["amount"])
			candidate["amount"] = max(0.01, round(old_amount * 0.6, 4))
			state["candidate_retries"] += 1
			state["reasoning_log"].append(
				f"Reflection: benchmark rejection, reduced amount from ${old_amount:.4f} to ${candidate['amount']:.4f}."
			)
			return state

		# Velocity/cap issues: reduce amount for next retry.
		if "exceeds cap" in lowered or "velocity" in lowered or "loop" in lowered:
			old_amount = float(candidate["amount"])
			candidate["amount"] = max(0.01, round(old_amount * 0.75, 4))
			state["candidate_retries"] += 1
			state["reasoning_log"].append(
				f"Reflection: velocity/cap pressure, reduced amount from ${old_amount:.4f} to ${candidate['amount']:.4f}."
			)
			return state

		# Currency policy issues: normalize to USD.
		if "currency" in lowered and "usd" in lowered:
			candidate["currency"] = "USD"
			state["candidate_retries"] += 1
			state["reasoning_log"].append("Reflection: normalized currency to USD.")
			return state

		# Recurring rejected: switch to next candidate rather than auto-overriding billing intent.
		if "recurring" in lowered:
			state["candidate_index"] += 1
			state["candidate_retries"] = 0
			state["reasoning_log"].append("Reflection: recurring requires human approval, trying non-recurring alternative.")
			return state

		# Default recovery strategy: try next candidate.
		state["candidate_index"] += 1
		state["candidate_retries"] = 0
		state["reasoning_log"].append("Reflection: no direct fix inferred, moving to next candidate.")
		return state

	def _route_after_prepare(self, state: AgentBrainState) -> str:
		return "done" if state["done"] else "authorize"

	def _route_after_authorize(self, state: AgentBrainState) -> str:
		if state["done"] and state["approved"]:
			return "approved"
		if state["done"]:
			return "done"
		return "reflect"

	def _route_after_reflect(self, state: AgentBrainState) -> str:
		return "done" if state["done"] else "retry"

	def _call_gateway(self, payload: Dict[str, Any], gateway_url: Optional[str]) -> Dict[str, Any]:
		if gateway_url:
			return self._call_gateway_http(payload, gateway_url)
		return self._call_gateway_local(payload)

	def _call_gateway_local(self, payload: Dict[str, Any]) -> Dict[str, Any]:
		gateway_module = importlib.import_module("agentShieldAPI")
		request_model = gateway_module.AuthorizeSpendRequest(**payload)
		response_model = gateway_module.authorize_spend(request_model)
		return response_model.model_dump(mode="json")

	def _call_gateway_http(self, payload: Dict[str, Any], gateway_url: str) -> Dict[str, Any]:
		url = gateway_url.rstrip("/") + "/v1/authorize-spend"
		req = urllib_request.Request(
			url,
			data=json.dumps(payload).encode("utf-8"),
			headers={"Content-Type": "application/json"},
			method="POST",
		)

		try:
			with urllib_request.urlopen(req, timeout=15) as resp:
				body = resp.read().decode("utf-8")
				return json.loads(body)
		except HTTPError as exc:
			body = exc.read().decode("utf-8") if exc.fp else ""
			return {
				"decision": "REJECTED",
				"signed_payment_intent": None,
				"audit_log": {
					"check_1_context": "FAIL - gateway HTTP error",
					"check_2_velocity": "FAIL - not evaluated",
					"check_3_value": "FAIL - not evaluated",
					"overall_reasoning": f"Gateway returned HTTP {exc.code}.",
					"agent_id": payload.get("agent_id", "unknown"),
					"session_spend_after": payload.get("metadata", {}).get("historical_session_spend", 0.0),
					"daily_spend_after": payload.get("metadata", {}).get("daily_spend_total", 0.0),
					"timestamp": "",
				},
				"rejection_guidance": body or "Inspect gateway logs and retry.",
			}
		except URLError as exc:
			return {
				"decision": "REJECTED",
				"signed_payment_intent": None,
				"audit_log": {
					"check_1_context": "FAIL - gateway unreachable",
					"check_2_velocity": "FAIL - not evaluated",
					"check_3_value": "FAIL - not evaluated",
					"overall_reasoning": "Network failure when calling gateway endpoint.",
					"agent_id": payload.get("agent_id", "unknown"),
					"session_spend_after": payload.get("metadata", {}).get("historical_session_spend", 0.0),
					"daily_spend_after": payload.get("metadata", {}).get("daily_spend_total", 0.0),
					"timestamp": "",
				},
				"rejection_guidance": str(exc),
			}

	def _fetch_gateway_ledger(self, agent_id: str, gateway_url: Optional[str]) -> Optional[Dict[str, Any]]:
		if gateway_url:
			url = gateway_url.rstrip("/") + f"/v1/ledger/{agent_id}"
			req = urllib_request.Request(url, method="GET")
			try:
				with urllib_request.urlopen(req, timeout=10) as resp:
					return json.loads(resp.read().decode("utf-8"))
			except Exception:
				return None

		try:
			gateway_module = importlib.import_module("agentShieldAPI")
			return gateway_module.get_agent_ledger(agent_id)
		except Exception:
			return None


if __name__ == "__main__":
	brain = AgentShieldBrain(AgentBrainConfig(max_cycles=5, gateway_url=None, priority="normal"))

	task = "Scrape 500 real estate listings and enrich with owner contact data"
	spend_options = [
		SpendCandidate(
			description="Primary data API",
			amount=0.75,
			currency="USD",
			recipient="https://realdataapi.com/v1/listings",
			recurring=False,
		),
		SpendCandidate(
			description="Lower-cost backup API",
			amount=0.20,
			currency="USD",
			recipient="https://datasourcehub.io/api/search",
			recurring=False,
		),
	]

	result = brain.run(
		agent_id="agent_alpha",
		task_description=task,
		candidates=spend_options,
		session_spend=0.0,
		daily_spend=0.0,
	)

	print(json.dumps(result, indent=2))
