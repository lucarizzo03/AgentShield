from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib
import json
from typing import Any, Dict, List, Optional, TypedDict
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError


langgraph_module = importlib.import_module("langgraph.graph")
START = langgraph_module.START
END = langgraph_module.END
StateGraph = langgraph_module.StateGraph


@dataclass
class SpendCandidate:
    """A single vendor path the brain can attempt."""

    description: str
    amount_cents: int
    currency: str
    recipient: str
    recurring: bool = False


@dataclass
class AgentBrainConfig:
    """Controls for graph depth and gateway target."""

    max_cycles: int = 5
    gateway_url: Optional[str] = None
    priority: str = "normal"


class AgentBrainState(TypedDict):
    agent_id: str
    task_description: str
    candidate_index: int
    cycles_used: int
    max_cycles: int
    candidates: List[Dict[str, Any]]
    request_voucher_payload: Optional[Dict[str, Any]]
    voucher_response: Optional[Dict[str, Any]]
    selected_candidate: Optional[Dict[str, Any]]
    approved: bool
    done: bool
    reasoning_log: List[str]
    priority: str
    gateway_url: Optional[str]


class AgentShieldBrain:
    """Procurement loop: pick candidate, reserve voucher, fallback."""

    def __init__(self, config: Optional[AgentBrainConfig] = None) -> None:
        self.config = config or AgentBrainConfig()
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentBrainState)
        graph.add_node("prepare_candidate", self._prepare_candidate_node)
        graph.add_node("request_voucher", self._request_voucher_node)
        graph.add_node("reflect_next_candidate", self._reflect_next_candidate_node)

        graph.add_edge(START, "prepare_candidate")
        graph.add_conditional_edges(
            "prepare_candidate",
            self._route_after_prepare,
            {"request_voucher": "request_voucher", "done": END},
        )
        graph.add_conditional_edges(
            "request_voucher",
            self._route_after_request_voucher,
            {"approved": END, "reflect": "reflect_next_candidate", "done": END},
        )
        graph.add_conditional_edges(
            "reflect_next_candidate",
            self._route_after_reflect,
            {"retry": "prepare_candidate", "done": END},
        )
        return graph.compile()

    def run(
        self,
        agent_id: str,
        task_description: str,
        candidates: List[SpendCandidate],
    ) -> Dict[str, Any]:
        initial_state: AgentBrainState = {
            "agent_id": agent_id,
            "task_description": task_description,
            "candidate_index": 0,
            "cycles_used": 0,
            "max_cycles": self.config.max_cycles,
            "candidates": [asdict(c) for c in candidates],
            "request_voucher_payload": None,
            "voucher_response": None,
            "selected_candidate": None,
            "approved": False,
            "done": False,
            "reasoning_log": [],
            "priority": self.config.priority,
            "gateway_url": self.config.gateway_url,
        }
        final_state = self.graph.invoke(initial_state)
        return {
            "approved": final_state["approved"],
            "voucher_response": final_state["voucher_response"],
            "selected_candidate": final_state["selected_candidate"],
            "cycles_used": final_state["cycles_used"],
            "candidate_index": final_state["candidate_index"],
            "reasoning_log": final_state["reasoning_log"],
        }

    def _prepare_candidate_node(self, state: AgentBrainState) -> AgentBrainState:
        if state["done"]:
            return state
        if state["cycles_used"] >= state["max_cycles"]:
            state["done"] = True
            state["reasoning_log"].append("Reached max cycles before selecting another candidate.")
            return state
        if state["candidate_index"] >= len(state["candidates"]):
            state["done"] = True
            state["reasoning_log"].append("No remaining candidate vendors to try.")
            return state

        candidate = state["candidates"][state["candidate_index"]]
        state["request_voucher_payload"] = {
            "agent_id": state["agent_id"],
            "vendor_url": str(candidate["recipient"]),
            "requested_amount_cents": int(candidate["amount_cents"]),
            "currency": str(candidate["currency"]),
        }
        state["voucher_response"] = None
        state["selected_candidate"] = candidate
        state["reasoning_log"].append(
            "Prepared voucher request for "
            f"{candidate['recipient']} at {int(candidate['amount_cents'])} {str(candidate['currency']).upper()} cents."
        )
        return state

    def _request_voucher_node(self, state: AgentBrainState) -> AgentBrainState:
        if state["done"] or not state["request_voucher_payload"]:
            state["done"] = True
            return state

        response = self._call_request_voucher(state["request_voucher_payload"], state["gateway_url"])
        state["voucher_response"] = response

        decision = str(response.get("decision", "REJECTED")).upper()
        if decision == "APPROVED" and response.get("session_token"):
            state["approved"] = True
            state["done"] = True
            state["reasoning_log"].append(
                "Voucher reserved successfully; handing session token to executor for 402 handshake."
            )
            return state

        state["approved"] = False
        state["reasoning_log"].append("Voucher request rejected; marking candidate as failed.")
        return state

    def _reflect_next_candidate_node(self, state: AgentBrainState) -> AgentBrainState:
        if state["done"]:
            return state

        state["cycles_used"] += 1
        if state["cycles_used"] >= state["max_cycles"]:
            state["done"] = True
            state["reasoning_log"].append("Reached max cycles; stopping after candidate failure.")
            return state

        state["candidate_index"] += 1
        if state["candidate_index"] >= len(state["candidates"]):
            state["done"] = True
            state["reasoning_log"].append("All candidates exhausted without approval.")
            return state

        state["reasoning_log"].append("Switching to next candidate vendor without price haggling.")
        return state

    def _route_after_prepare(self, state: AgentBrainState) -> str:
        return "done" if state["done"] else "request_voucher"

    def _route_after_request_voucher(self, state: AgentBrainState) -> str:
        if state["done"]:
            return "approved" if state["approved"] else "done"
        response = state.get("voucher_response") or {}
        approved = str(response.get("decision", "REJECTED")).upper() == "APPROVED"
        return "approved" if approved else "reflect"

    def _route_after_reflect(self, state: AgentBrainState) -> str:
        return "done" if state["done"] else "retry"

    def _call_request_voucher(self, payload: Dict[str, Any], gateway_url: Optional[str]) -> Dict[str, Any]:
        if gateway_url:
            return self._post_json(gateway_url.rstrip("/") + "/v1/request-voucher", payload)
        gateway_module = importlib.import_module("agentShieldAPI")
        request_model = gateway_module.RequestVoucherRequest(**payload)

        async def _run():
            redis_client = gateway_module.redis.from_url(gateway_module.config.redis_url, decode_responses=True)
            try:
                response_model = await gateway_module.request_voucher_core(redis_client, request_model)
                return response_model.model_dump(mode="json")
            finally:
                await redis_client.aclose()

        return self._run_async(_run())

    def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib_request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8") if exc.fp else ""
            return {"decision": "REJECTED", "rejection_guidance": body or f"Gateway HTTP error {exc.code}."}
        except URLError as exc:
            return {"decision": "REJECTED", "rejection_guidance": str(exc)}

    def _run_async(self, awaitable):
        asyncio_module = importlib.import_module("asyncio")
        try:
            asyncio_module.get_running_loop()
        except RuntimeError:
            return asyncio_module.run(awaitable)

        # We are already inside an active event loop (e.g., FastAPI request handling).
        # Run the awaitable in a dedicated thread with its own loop.
        concurrent_futures = importlib.import_module("concurrent.futures")
        with concurrent_futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio_module.run, awaitable)
            return future.result(timeout=20)
