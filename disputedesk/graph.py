"""Merchant context graph in FalkorDB.

Nodes (every node has source = "paypal" or "seeded"):
  Customer {payer_id}            Transaction {id = seller_transaction_id, ...}
  Dispute {id, reason, ...}      Shipment {id, carrier, status, ...}      Telemetry {id, ip, device_hash, geo}
Edges:
  (Customer)-[:INITIATED]->(Transaction)-[:HAS_DISPUTE]->(Dispute)
  (Transaction)-[:FULFILLED_BY]->(Shipment)
  (Transaction)-[:LOGGED_FROM]->(Telemetry)
  (Transaction)-[:HAS_EVENT]->(MerchantEvent {id, kind, status, location, time, label, source = "seeded"})

PayPal-sourced nodes come from the stored GET /v1/customer/disputes/{id} response:
https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_get
Shipments, telemetry and past disputes are merchant-supplied test data (source = "seeded").

All queries use Cypher parameters; values are never formatted into query text.
"""

from typing import Any, Protocol

from disputedesk.config import AgentSettings

PAYPAL = "paypal"
SEEDED = "seeded"
# Start of the fact text for each linked dispute (the decision step checks for it).
LINKED_PREFIX = "Linked dispute "
# Every merchant event imported in the sandbox carries this label (and source = "seeded").
EVENT_LABEL = "simulated merchant event"


class GraphLike(Protocol):
    def query(self, q: str, params: dict[str, Any] | None = None) -> Any: ...
    def delete(self) -> Any: ...


def connect(settings: AgentSettings) -> GraphLike:
    from falkordb import FalkorDB  # imported here so tests never need a server

    return FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port).select_graph(settings.graph_name)


def _amount(money: dict[str, Any] | None) -> tuple[float | None, str | None]:
    money = money or {}
    try:
        value = float(money["value"])
    except (KeyError, TypeError, ValueError):
        value = None
    return value, money.get("currency_code")


# One query per dispute. Verified against FalkorDB (Docker): map projections inside collect(),
# CASE, and collect() dropping NULLs. Linked disputes are found only through a shared device hash
# or IP, never through the shared buyer (the sandbox has a single test buyer for every dispute).
CONTEXT_QUERY = """
MATCH (c:Customer)-[:INITIATED]->(t:Transaction)-[:HAS_DISPUTE]->(d:Dispute {id: $id})
OPTIONAL MATCH (t)-[:FULFILLED_BY]->(s:Shipment)
OPTIONAL MATCH (t)-[:LOGGED_FROM]->(tel:Telemetry)
OPTIONAL MATCH (other_t:Transaction)-[:LOGGED_FROM]->(ot:Telemetry), (other_t)-[:HAS_DISPUTE]->(od:Dispute)
  WHERE other_t <> t AND (ot.device_hash = tel.device_hash OR ot.ip = tel.ip)
RETURN
  d.id, d.reason, d.status, d.stage, d.amount, d.currency, d.due, d.source,
  c.payer_id, c.source,
  t.id, t.buyer_transaction_id, t.create_time, t.amount, t.currency,
  t.seller_protection_eligible, t.seller_protection_type, t.source,
  s.id, s.carrier, s.tracking_id, s.status, s.shipped_at, s.delivered_at, s.last_scan,
  s.signed_by_buyer, s.destination, s.source,
  tel.id, tel.ip, tel.device_hash, tel.geo, tel.source,
  collect(DISTINCT CASE WHEN od IS NULL THEN NULL ELSE {
    id: od.id, reason: od.reason, status: od.status, source: od.source, transaction_id: other_t.id,
    via: CASE WHEN ot.device_hash = tel.device_hash THEN 'device_hash' ELSE 'ip' END
  } END) AS linked,
  [(t)-[:HAS_EVENT]->(ev:MerchantEvent) | {
    id: ev.id, kind: ev.kind, status: ev.status, location: ev.location, time: ev.time, carrier: ev.carrier,
    signed_by_buyer: ev.signed_by_buyer, note: ev.note, label: ev.label, source: ev.source
  }] AS events
LIMIT 1
"""

_GROUPS = {
    "dispute": ["id", "reason", "status", "stage", "amount", "currency", "due", "source"],
    "customer": ["payer_id", "source"],
    "transaction": ["id", "buyer_transaction_id", "create_time", "amount", "currency",
                    "seller_protection_eligible", "seller_protection_type", "source"],
    "shipment": ["id", "carrier", "tracking_id", "status", "shipped_at", "delivered_at", "last_scan",
                 "signed_by_buyer", "destination", "source"],
    "telemetry": ["id", "ip", "device_hash", "geo", "source"],
}


class GraphStore:
    def __init__(self, graph: GraphLike):
        self._g = graph

    # --- PayPal-sourced nodes ------------------------------------------------

    def upsert_paypal_dispute(self, dispute: dict[str, Any]) -> list[str]:
        """MERGE Dispute, Transaction(s) and Customer from a full GET response.
        Returns the seller transaction IDs linked. Buyer email and name are not stored."""
        amount, currency = _amount(dispute.get("dispute_amount"))
        self._g.query(
            "MERGE (d:Dispute {id: $id}) SET d += $props",
            {"id": dispute["dispute_id"], "props": {
                "reason": dispute.get("reason"),
                "status": dispute.get("status"),
                "stage": dispute.get("dispute_life_cycle_stage"),
                "amount": amount,
                "currency": currency,
                "due": dispute.get("seller_response_due_date"),
                "source": PAYPAL,
            }},
        )
        linked: list[str] = []
        for txn in dispute.get("disputed_transactions") or []:
            txn_id = txn.get("seller_transaction_id") if isinstance(txn, dict) else None
            if not txn_id:
                continue
            t_amount, t_currency = _amount(txn.get("gross_amount"))
            payer_id = (txn.get("buyer") or {}).get("payer_id")
            self._g.query(
                "MERGE (t:Transaction {id: $tid}) SET t += $props "
                "WITH t MATCH (d:Dispute {id: $did}) MERGE (t)-[:HAS_DISPUTE]->(d)",
                {"tid": txn_id, "did": dispute["dispute_id"], "props": {
                    "buyer_transaction_id": txn.get("buyer_transaction_id"),
                    "create_time": txn.get("create_time"),
                    "amount": t_amount,
                    "currency": t_currency,
                    "seller_protection_eligible": txn.get("seller_protection_eligible"),
                    "seller_protection_type": txn.get("seller_protection_type"),
                    "source": PAYPAL,
                }},
            )
            if payer_id:
                self.link_customer(payer_id, txn_id, PAYPAL)
            linked.append(txn_id)
        return linked

    def update_dispute_state(self, dispute: dict[str, Any]) -> None:
        """Refresh status/stage on an existing Dispute node (after a poll saw a change)."""
        self._g.query(
            "MATCH (d:Dispute {id: $id}) SET d.status = $status, d.stage = $stage",
            {"id": dispute["dispute_id"], "status": dispute.get("status"),
             "stage": dispute.get("dispute_life_cycle_stage")},
        )

    # --- shared helpers (also used for seeded data) --------------------------

    def link_customer(self, payer_id: str, txn_id: str, source: str) -> None:
        self._g.query(
            "MERGE (c:Customer {payer_id: $pid}) ON CREATE SET c.source = $source "
            "WITH c MATCH (t:Transaction {id: $tid}) MERGE (c)-[:INITIATED]->(t)",
            {"pid": payer_id, "tid": txn_id, "source": source},
        )

    def attach_shipment(self, txn_id: str, shipment: dict[str, Any]) -> None:
        props = {k: v for k, v in shipment.items() if k != "id"} | {"source": SEEDED}
        self._g.query(
            "MATCH (t:Transaction {id: $tid}) MERGE (s:Shipment {id: $sid}) SET s += $props "
            "MERGE (t)-[:FULFILLED_BY]->(s)",
            {"tid": txn_id, "sid": shipment["id"], "props": props},
        )

    def attach_telemetry(self, txn_id: str, telemetry: dict[str, Any]) -> None:
        props = {k: v for k, v in telemetry.items() if k != "id"} | {"source": SEEDED}
        self._g.query(
            "MATCH (t:Transaction {id: $tid}) MERGE (x:Telemetry {id: $xid}) SET x += $props "
            "MERGE (t)-[:LOGGED_FROM]->(x)",
            {"tid": txn_id, "xid": telemetry["id"], "props": props},
        )

    def add_seeded_dispute(self, past: dict[str, Any]) -> None:
        """A merchant-supplied past dispute: its own customer, transaction, dispute, telemetry."""
        self._g.query(
            "MERGE (t:Transaction {id: $tid}) SET t += $tprops "
            "MERGE (d:Dispute {id: $did}) SET d += $dprops "
            "MERGE (t)-[:HAS_DISPUTE]->(d)",
            {"tid": past["transaction_id"], "did": past["dispute_id"],
             "tprops": {"amount": past.get("amount"), "currency": past.get("currency"),
                        "create_time": past.get("create_time"), "source": SEEDED},
             "dprops": {"reason": past.get("reason"), "status": past.get("status"),
                        "amount": past.get("amount"), "currency": past.get("currency"), "source": SEEDED}},
        )
        self.link_customer(past["payer_id"], past["transaction_id"], SEEDED)
        if past.get("telemetry"):
            self.attach_telemetry(past["transaction_id"], past["telemetry"])

    def add_merchant_event(self, txn_id: str, event: dict[str, Any]) -> None:
        """A merchant event (for example a carrier scan) on a transaction that already exists in the
        graph. Always source = "seeded" and labelled as a simulated merchant event."""
        props = {k: v for k, v in event.items() if k != "id" and v is not None} | {"label": EVENT_LABEL, "source": SEEDED}
        self._g.query(
            "MATCH (t:Transaction {id: $tid}) MERGE (e:MerchantEvent {id: $eid}) SET e += $props "
            "MERGE (t)-[:HAS_EVENT]->(e)",
            {"tid": txn_id, "eid": event["id"], "props": props},
        )

    def reset(self) -> None:
        """Delete the whole disputedesk graph (local FalkorDB only)."""
        try:
            self._g.delete()
        except Exception as exc:  # deleting a graph that does not exist yet errors; that is fine
            if "empty key" not in str(exc).lower() and "invalid graph" not in str(exc).lower():
                raise

    # --- read ------------------------------------------------------------------

    def dispute_context(self, dispute_id: str) -> dict[str, Any] | None:
        rows = self._g.query(CONTEXT_QUERY, {"id": dispute_id}).result_set
        if not rows:
            return None
        return parse_context_row(rows[0])


def parse_context_row(row: list[Any]) -> dict[str, Any]:
    ctx: dict[str, Any] = {}
    i = 0
    for group, fields in _GROUPS.items():
        values = dict(zip(fields, row[i:i + len(fields)]))
        i += len(fields)
        # An unmatched OPTIONAL MATCH gives all-NULL columns.
        ctx[group] = values if values.get("id") or values.get("payer_id") else None
    ctx["linked_disputes"] = [dict(x) for x in row[i] or []]
    # Merchant events (a later column; older snapshots and rows simply have none).
    events = row[i + 1] if len(row) > i + 1 else []
    ctx["events"] = sorted((dict(x) for x in events or []), key=lambda e: str(e.get("time") or ""))
    return ctx


def _money(amount: Any, currency: Any) -> str:
    return f"{amount:.2f} {currency}" if isinstance(amount, (int, float)) else f"unknown amount {currency or ''}".strip()


def context_to_facts(ctx: dict[str, Any]) -> list[dict[str, str]]:
    """Numbered facts (F1..Fn) built only from the graph context. The LLM may cite only these.
    `ref` names the graph node a fact describes (dispute, customer, transaction, shipment, telemetry,
    linked:<id>) so the case page can highlight the cited path; it is not sent to the LLM."""
    facts: list[tuple[str, str, str]] = []  # (text, source, graph node ref)
    d, c, t, s, tel = ctx["dispute"], ctx["customer"], ctx["transaction"], ctx["shipment"], ctx["telemetry"]

    facts.append((f"Dispute {d['id']}: reason {d['reason']}, amount {_money(d['amount'], d['currency'])}, "
                  f"stage {d['stage']}, status {d['status']}, seller response due {d['due']}.", d["source"], "dispute"))
    if c:
        facts.append((f"Buyer PayPal payer_id {c['payer_id']}.", c["source"], "customer"))
    if t:
        facts.append((f"Transaction {t['id']} created {t['create_time']} for "
                      f"{_money(t['amount'], t['currency'])}.", t["source"], "transaction"))
        if t.get("seller_protection_eligible") is not None:
            facts.append((f"PayPal marks transaction {t['id']} seller_protection_eligible="
                          f"{str(t['seller_protection_eligible']).lower()} "
                          f"({t.get('seller_protection_type')}).", t["source"], "transaction"))
    if s:
        detail = f"Shipment {s['id']} via {s['carrier']} (tracking {s['tracking_id']}) to {s['destination']}: status {s['status']}"
        if s.get("shipped_at"):
            detail += f", shipped {s['shipped_at']}"
        if s.get("delivered_at"):
            detail += f", delivered {s['delivered_at']}"
        if s.get("last_scan"):
            detail += f", last carrier scan: {s['last_scan']}"
        if s.get("signed_by_buyer") is not None:
            detail += f", signed by buyer: {'yes' if s['signed_by_buyer'] else 'no'}"
        facts.append((detail + ".", s["source"], "shipment"))
    else:
        facts.append(("No shipment record is on file for this transaction.", SEEDED, "shipment"))
    for ev in ctx.get("events") or []:
        detail = (f"Simulated merchant event {ev['id']} ({str(ev.get('kind') or 'event').replace('_', ' ')}): "
                  f"{('carrier ' + ev['carrier'] + ' reported') if ev.get('carrier') else 'reported'} status {ev.get('status')}")
        if ev.get("location"):
            detail += f" at {ev['location']}"
        if ev.get("time"):
            detail += f" on {ev['time']}"
        if ev.get("signed_by_buyer") is not None:
            detail += f", signed by buyer: {'yes' if ev['signed_by_buyer'] else 'no'}"
        if ev.get("note"):
            detail += f". Note: {ev['note']}"
        facts.append((detail + ".", ev.get("source") or SEEDED, "shipment"))
    if tel:
        facts.append((f"Checkout telemetry {tel['id']}: IP {tel['ip']} (geolocated {tel['geo']}), "
                      f"device hash {tel['device_hash']}.", tel["source"], "telemetry"))
    else:
        facts.append(("No checkout telemetry is on file for this transaction.", SEEDED, "telemetry"))

    linked = ctx["linked_disputes"]
    if linked:
        facts.append((f"{len(linked)} other dispute(s) come from a transaction sharing this checkout's "
                      "device hash or IP.", SEEDED, "telemetry"))
        for o in linked:
            facts.append((f"{LINKED_PREFIX}{o['id']} (transaction {o['transaction_id']}, reason {o['reason']}, "
                          f"status {o['status']}) shares the {o['via'].replace('_', ' ')}.", o["source"], f"linked:{o['id']}"))
    else:
        facts.append(("No other dispute shares this checkout's device hash or IP.", SEEDED, "telemetry"))

    return [{"id": f"F{n}", "text": text, "source": src, "ref": ref}
            for n, (text, src, ref) in enumerate(facts, 1)]
