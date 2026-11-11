"""Test doubles shared by the agent tests. No FalkorDB server or LLM is ever contacted."""

from typing import Any


class FakeResult:
    def __init__(self, rows: list[list[Any]] | None = None):
        self.result_set = rows or []


class FakeGraph:
    """Records every (query, params). Returns `rows` for the context query."""

    def __init__(self, rows: list[list[Any]] | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows or []
        self.deleted = False

    def query(self, q: str, params: dict[str, Any] | None = None) -> FakeResult:
        self.calls.append((q, params or {}))
        return FakeResult(self.rows if "RETURN" in q else [])

    def delete(self) -> None:
        self.deleted = True


def sample_dispute(dispute_id: str = "PP-R-TST-1", reason: str = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                   value: str = "25.00", txn: str = "TXN-SELLER-1", status: str = "WAITING_FOR_SELLER_RESPONSE",
                   update_time: str = "2026-11-01T10:00:00.000Z") -> dict[str, Any]:
    """Shaped like a real GET /v1/customer/disputes/{id} response (fake IDs and buyer)."""
    return {
        "dispute_id": dispute_id,
        "reason": reason,
        "status": status,
        "dispute_life_cycle_stage": "CHARGEBACK",
        "dispute_amount": {"currency_code": "USD", "value": value},
        "seller_response_due_date": "2026-11-17T04:59:59.000Z",
        "update_time": update_time,
        "disputed_transactions": [{
            "seller_transaction_id": txn,
            "buyer_transaction_id": f"B-{txn}",
            "create_time": "2026-10-30T10:00:00.000Z",
            "gross_amount": {"currency_code": "USD", "value": value},
            "seller_protection_eligible": True,
            "seller_protection_type": "EXPANDED_SELLER_PROTECTION",
            "buyer": {"payer_id": "BUYER1", "name": "Test Buyer", "email": "buyer@example.com"},
        }],
        "links": [
            {"href": "https://x/self", "rel": "self", "method": "GET"},
            {"href": "https://x/accept", "rel": "accept_claim", "method": "POST"},
            {"href": "https://x/evidence", "rel": "provide_evidence", "method": "POST"},
        ],
    }


def context_row(linked: list[dict[str, Any]] | None = None, shipment: bool = True, telemetry: bool = True) -> list[Any]:
    """One row of CONTEXT_QUERY output, in column order."""
    d = ["PP-R-TST-1", "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "WAITING_FOR_SELLER_RESPONSE", "CHARGEBACK",
         25.0, "USD", "2026-11-17T04:59:59.000Z", "paypal"]
    c = ["BUYER1", "paypal"]
    t = ["TXN-SELLER-1", "B-TXN-SELLER-1", "2026-10-30T10:00:00.000Z", 25.0, "USD", True,
         "EXPANDED_SELLER_PROTECTION", "paypal"]
    s = (["SHP-1", "UPS", "1Z999", "DELIVERED", "2026-10-31", "2026-11-02", None, True, "Austin, TX, US", "seeded"]
         if shipment else [None] * 10)
    tel = ["TEL-1", "203.0.113.10", "dev-aaa", "Austin, TX, US", "seeded"] if telemetry else [None] * 5
    return d + c + t + s + tel + [linked or []]


class FakeDisputesClient:
    """Stands in for DisputesClient in the autopilot tests. Counts every call; `send_action` is the
    PayPal write and must never be reached by autopilot code, so it also raises."""

    def __init__(self, disputes: dict[str, dict[str, Any]] | None = None):
        self.disputes = disputes or {}
        self.get_calls: list[str] = []
        self.send_action_calls: list[tuple[str, str, str]] = []

    def get_dispute(self, dispute_id: str) -> dict[str, Any]:
        self.get_calls.append(dispute_id)
        return self.disputes[dispute_id]

    def send_action(self, action: str, dispute_id: str, text: str) -> Any:
        self.send_action_calls.append((action, dispute_id, text))
        raise AssertionError("send_action must never be called by autopilot code")
