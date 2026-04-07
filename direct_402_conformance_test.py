#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib import request as urllib_request


class ConformanceVendorHandler(BaseHTTPRequestHandler):
    amount_cents = 7
    currency = "USD"

    def _write_json(self, status: int, body: Dict[str, Any], headers: Dict[str, str] | None = None) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _extract_token(self) -> str:
        auth = self.headers.get("Authorization", "").strip()
        if auth.lower().startswith("payment "):
            return auth.split(" ", 1)[1].strip()
        if auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip()
        return self.headers.get("X-MPP-Payment-Intent", "").strip()

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        # Drain body if present.
        try:
            content_len = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_len = 0
        if content_len > 0:
            _ = self.rfile.read(content_len)
        self._handle()

    def _handle(self) -> None:
        token = self._extract_token()
        if not token:
            challenge_id = f"local_ch_{secrets.token_hex(8)}"
            headers = {
                "WWW-Authenticate": (
                    f'Payment id="{challenge_id}", method="tempo", intent="charge", '
                    f'amount_cents="{self.amount_cents}", currency="{self.currency}"'
                ),
                "X-MPP-Challenge-Id": challenge_id,
                "X-MPP-Amount-Cents": str(self.amount_cents),
                "X-MPP-Currency": self.currency,
            }
            self._write_json(
                402,
                {
                    "title": "Payment Required",
                    "status": 402,
                    "mpp_challenge_id": challenge_id,
                    "amount_cents": self.amount_cents,
                    "currency": self.currency,
                },
                headers=headers,
            )
            return

        headers = {
            "Payment-Receipt": f"receipt_local_{secrets.token_hex(8)}",
            "X-MPP-Transaction-Id": f"tx_local_{secrets.token_hex(8)}",
        }
        self._write_json(
            200,
            {"ok": True, "source": "local_conformance_vendor", "note": "Direct 402 retry accepted."},
            headers=headers,
        )

    def log_message(self, format: str, *args: object) -> None:
        # Keep output focused on conformance result.
        return


def _post_json(url: str, payload: Dict[str, Any], timeout: float = 60.0) -> Dict[str, Any]:
    req = urllib_request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run direct-402 conformance flow against AgentShield API.")
    parser.add_argument("--api-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--bind-host", default="127.0.0.1")
    parser.add_argument("--vendor-host", default="127.0.0.1")
    parser.add_argument("--vendor-port", type=int, default=8765)
    parser.add_argument("--agent-id", default="agent_alpha")
    parser.add_argument("--task", default="direct 402 conformance check")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.bind_host, args.vendor_port), ConformanceVendorHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)

    vendor_url = f"http://{args.vendor_host}:{args.vendor_port}/paid-resource"
    payload = {
        "agent_id": args.agent_id,
        "task_description": args.task,
        "candidates": [
            {
                "description": "direct 402 conformance vendor",
                "amount_cents": 15,
                "currency": "USD",
                "recipient": vendor_url,
                "recurring": False,
            }
        ],
        "brain_max_cycles": 1,
        "priority": "normal",
        "mpp_mode": "real",
    }

    try:
        response = _post_json(f"{args.api_base_url.rstrip('/')}/v1/process-payment", payload)
    finally:
        server.shutdown()
        server.server_close()

    mpp = response.get("mpp_execution", {})
    summary = {
        "decision": response.get("decision"),
        "status": mpp.get("status"),
        "execution_path": mpp.get("execution_path"),
        "payment_receipt": mpp.get("payment_receipt"),
        "transaction_id": mpp.get("transaction_id"),
    }
    print(json.dumps(summary, indent=2))

    ok = (
        summary["decision"] == "APPROVED"
        and summary["status"] == "SUCCEEDED"
        and summary["execution_path"] == "direct_402_http"
        and bool(summary["payment_receipt"])
    )
    if not ok:
        print("\nConformance test FAILED. Full response:")
        print(json.dumps(response, indent=2))
        return 1

    print("\nConformance test PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
