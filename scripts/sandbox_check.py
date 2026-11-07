"""Step-by-step check of the real PayPal sandbox dispute flow. NOT part of pytest.

Disputes are opened manually (see README). API creation needs the DISPUTE_CREATE scope.

    python scripts/sandbox_check.py --inquiry-dispute-id PP-D-... [--claim-dispute-id PP-D-...]
    python scripts/sandbox_check.py --list
    python scripts/sandbox_check.py --dispute-id PP-D-...   (read-only: one GET, no mutating calls)

The webhook receiver must be running (it writes events to logs/events/).
Every raw request/response (secrets redacted) goes to logs/sandbox_check_<ts>.jsonl.
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from disputedesk.auth import TokenProvider  # noqa: E402
from disputedesk.config import LOG_DIR, PROJECT_ROOT, SANDBOX_BASE_URL, load_settings  # noqa: E402
from disputedesk.disputes import DisputesClient  # noqa: E402
from disputedesk.http_log import make_client  # noqa: E402

# Smallest valid one-page PDF, used as the evidence file.
# UNVERIFIED: the accepted evidence file types and sizes (the spec points to a "documents"
# section that was not in the readable spec).
EVIDENCE_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


@dataclass
class Result:
    verdict: str  # PASS | FAIL | BLOCKED
    reason: str


def paypal_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    return f"HTTP {resp.status_code} {body.get('name')}: {body.get('message')} (debug_id={body.get('debug_id')})"


def state(d: dict[str, Any]) -> str:
    links = sorted(link.get("rel") for link in d.get("links", []))
    return f"stage={d.get('dispute_life_cycle_stage')} status={d.get('status')} links={links}"


class Check:
    def __init__(self, client: DisputesClient, args: argparse.Namespace):
        self.client = client
        self.args = args

    def step_a_create(self) -> Result:
        # Documented requirement: DISPUTE_CREATE scope added by a PayPal account manager,
        # buyer consent, and a PayPal-Auth-Assertion header. The create endpoint is not public.
        # https://developer.paypal.com/platforms/disputes/set-up
        reason = ("API create needs the DISPUTE_CREATE scope (only a PayPal account manager can add it) "
                  "and the endpoint is not in the public spec")
        if self.args.inquiry_dispute_id:
            reason += f"; using manually opened dispute {self.args.inquiry_dispute_id}"
        else:
            reason += "; open one in the sandbox as the personal buyer and pass --inquiry-dispute-id"
        return Result("BLOCKED", reason)

    def step_b_webhook(self) -> Result:
        dispute_id = self.args.inquiry_dispute_id
        if not dispute_id:
            return Result("BLOCKED", "no --inquiry-dispute-id")
        events_dir = LOG_DIR / "events"
        deadline = time.monotonic() + self.args.webhook_timeout
        while True:
            for path in events_dir.glob("*.json") if events_dir.exists() else []:
                try:
                    event = json.loads(path.read_bytes())
                except ValueError:
                    continue
                resource = event.get("resource") or {}
                if event.get("event_type") == "CUSTOMER.DISPUTE.CREATED" and resource.get("dispute_id") == dispute_id:
                    return Result("PASS", f"verified event {event.get('id')} in {path.name}")
            if time.monotonic() >= deadline:
                return Result("FAIL", f"no verified CUSTOMER.DISPUTE.CREATED for {dispute_id} in "
                                      f"logs/events after {self.args.webhook_timeout}s (is the receiver "
                                      "running behind ngrok, and was the webhook registered before the dispute was opened?)")
            time.sleep(3)

    def step_c_get(self) -> Result:
        if not self.args.inquiry_dispute_id:
            return Result("BLOCKED", "no --inquiry-dispute-id")
        try:
            d = self.client.get_dispute(self.args.inquiry_dispute_id)
        except httpx.HTTPStatusError as exc:
            return Result("FAIL", paypal_error(exc.response))
        return Result("PASS", f"reason={d.get('reason')} {state(d)}")

    def step_d_offer(self) -> Result:
        dispute_id = self.args.inquiry_dispute_id
        if not dispute_id:
            return Result("BLOCKED", "no --inquiry-dispute-id")
        d = self.client.get_dispute(dispute_id)
        # Documented precondition: dispute_life_cycle_stage must be INQUIRY.
        if d.get("dispute_life_cycle_stage") != "INQUIRY":
            return Result("BLOCKED", f"make-offer needs stage INQUIRY; {state(d)}")
        if not self.client.has_link(d, "make-offer"):
            return Result("BLOCKED", f"no make-offer link; {state(d)}")
        # UNVERIFIED: dispute_amount as the refund amount; the offer is sent without one if it is absent.
        resp = self.client.make_offer(
            dispute_id,
            note="DisputeDesk sandbox spike: offering a full refund.",
            offer_type="REFUND",
            offer_amount=d.get("dispute_amount"),
        )
        if resp.is_error:
            return Result("FAIL", paypal_error(resp))
        return Result("PASS", f"HTTP {resp.status_code}; now {state(self.client.get_dispute(dispute_id))}")

    def step_e_evidence(self) -> Result:
        dispute_id = self.args.claim_dispute_id
        if not dispute_id:
            return Result("BLOCKED", "no --claim-dispute-id (open a second dispute)")
        d = self.client.get_dispute(dispute_id)
        notes: list[str] = [f"start {state(d)}"]

        if d.get("dispute_life_cycle_stage") == "INQUIRY" and self.client.has_link(d, "escalate"):
            resp = self.client.escalate(dispute_id, note="DisputeDesk sandbox spike: escalating to claim.")
            if resp.is_error:
                return Result("FAIL", f"escalate {paypal_error(resp)}; " + "; ".join(notes))
            d = self.client.get_dispute(dispute_id)
            notes.append(f"after escalate {state(d)}")

        if d.get("status") == "UNDER_REVIEW" and self.client.has_link(d, "require-evidence"):
            resp = self.client.require_evidence(dispute_id, action="SELLER_EVIDENCE")
            if resp.is_error:
                return Result("FAIL", f"require-evidence {paypal_error(resp)}; " + "; ".join(notes))
            d = self.client.get_dispute(dispute_id)
            notes.append(f"after require-evidence {state(d)}")

        # Documented precondition: status WAITING_FOR_SELLER_RESPONSE and provide-evidence link present.
        if d.get("status") != "WAITING_FOR_SELLER_RESPONSE" or not self.client.has_link(d, "provide-evidence"):
            return Result("BLOCKED", "provide-evidence needs WAITING_FOR_SELLER_RESPONSE + link; " + "; ".join(notes))
        resp = self.client.provide_evidence(
            dispute_id,
            file_name="disputedesk-evidence.pdf",
            file_bytes=EVIDENCE_PDF,
            file_content_type="application/pdf",
            notes="DisputeDesk sandbox spike: test evidence.",
        )
        if resp.is_error:
            return Result("FAIL", f"provide-evidence {paypal_error(resp)}; " + "; ".join(notes))
        notes.append(f"after provide-evidence {state(self.client.get_dispute(dispute_id))}")
        return Result("PASS", "; ".join(notes))

    def step_f_settle(self) -> Result:
        dispute_id = self.args.claim_dispute_id or self.args.inquiry_dispute_id
        if not dispute_id:
            return Result("BLOCKED", "no dispute id")
        d = self.client.get_dispute(dispute_id)
        # Documented precondition: status UNDER_REVIEW and adjudicate link present.
        if d.get("status") != "UNDER_REVIEW" or not self.client.has_link(d, "adjudicate"):
            return Result("BLOCKED", f"adjudicate needs UNDER_REVIEW + adjudicate link; {dispute_id} {state(d)}")
        resp = self.client.adjudicate(dispute_id, adjudication_outcome="SELLER_FAVOR")
        if resp.is_error:
            return Result("FAIL", paypal_error(resp))
        return Result("PASS", f"HTTP {resp.status_code}; now {state(self.client.get_dispute(dispute_id))}")


def list_disputes(http: httpx.Client, tokens: TokenProvider) -> int:
    # GET /v1/customer/disputes ("List disputes"). Response: {"items": [...], "links": [...]}.
    # https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_list
    # Sandbox base URL only; SANDBOX_BASE_URL is the only base URL Settings accepts.
    url = f"{SANDBOX_BASE_URL}/v1/customer/disputes"
    for attempt in (1, 2):
        resp = http.get(url, headers=tokens.auth_headers())
        if resp.status_code == 401 and attempt == 1:
            tokens.invalidate()
            continue
        break
    print(f"HTTP {resp.status_code}")
    if resp.is_error:
        print(paypal_error(resp))
        return 1
    items = resp.json().get("items") or []
    if not items:
        print("no disputes returned")
        return 0
    for d in items:
        # The API field is dispute_life_cycle_stage; printed as dispute_stage.
        rels = [link.get("rel") for link in d.get("links", [])]
        print(f"dispute_id={d.get('dispute_id')} status={d.get('status')} "
              f"dispute_stage={d.get('dispute_life_cycle_stage')} reason={d.get('reason')} links={rels}")
    return 0


def format_dispute(d: dict[str, Any]) -> list[str]:
    """Readable lines for one dispute from GET /v1/customer/disputes/{id}. No I/O."""
    # GET /v1/customer/disputes/{id} ("Show dispute details").
    # https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_get
    # Documented fields: dispute_id, status, dispute_life_cycle_stage, reason, create_time,
    # seller_response_due_date, dispute_amount {currency_code, value}, disputed_transactions[],
    # messages[], links[] {href, rel, method}.
    # UNVERIFIED: disputed_transactions[] ids named seller_transaction_id / buyer_transaction_id,
    # and messages[].posted_by == "BUYER" for buyer messages (not checked against a real response).
    amount = d.get("dispute_amount") or {}
    amount_str = f"{amount.get('value')} {amount.get('currency_code')}" if amount else None
    lines = [
        f"dispute_id={d.get('dispute_id')}",
        f"status={d.get('status')}",
        # The API field is dispute_life_cycle_stage; printed as dispute_stage.
        f"dispute_stage={d.get('dispute_life_cycle_stage')}",
        f"reason={d.get('reason')}",
        f"create_time={d.get('create_time')}",
        f"seller_response_due_date={d.get('seller_response_due_date')}",
        f"dispute_amount={amount_str}",
    ]

    txns = d.get("disputed_transactions") or []
    if txns:
        lines.append("disputed_transactions:")
        for t in txns:
            lines.append(f"  seller_transaction_id={t.get('seller_transaction_id')} "
                         f"buyer_transaction_id={t.get('buyer_transaction_id')}")
    else:
        lines.append("disputed_transactions: none")

    buyer_msgs = [m for m in d.get("messages") or [] if m.get("posted_by") == "BUYER"]
    if buyer_msgs:
        lines.append("buyer_messages:")
        for m in buyer_msgs:
            lines.append(f"  {m.get('time_posted')}: {m.get('content')}")
    else:
        lines.append("buyer_messages: none")

    links = d.get("links") or []
    if links:
        lines.append("links:")
        for link in links:
            # Names and HTTP methods only; hrefs are not printed.
            lines.append(f"  {link.get('rel')} {link.get('method')}")
    else:
        lines.append("links: none")
    return lines


def show_dispute(client: DisputesClient, dispute_id: str) -> int:
    # Read-only: only DisputesClient.get_dispute (a GET) is called. Its base URL comes from
    # Settings, which only accepts SANDBOX_BASE_URL.
    try:
        d = client.get_dispute(dispute_id)
    except httpx.HTTPStatusError as exc:
        print(f"FAIL: {paypal_error(exc.response)}")
        return 1
    for line in format_dispute(d):
        print(line)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inquiry-dispute-id", help="dispute in the INQUIRY stage (deflect path)")
    parser.add_argument("--claim-dispute-id", help="second dispute for the defend path")
    parser.add_argument("--webhook-timeout", type=int, default=120, help="seconds to wait in step b")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="only list disputes (GET /v1/customer/disputes)")
    mode.add_argument("--dispute-id", metavar="ID",
                      help="only show one dispute (GET /v1/customer/disputes/{id}); read-only")
    args = parser.parse_args()

    if not (PROJECT_ROOT / ".env").exists():
        print("BLOCKED: no .env file; copy .env.example to .env and fill it in")
        return 2
    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(f"BLOCKED: {exc}")
        return 2

    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_DIR / f"sandbox_check_{ts}.log")],
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    http = make_client(LOG_DIR / f"sandbox_check_{ts}.jsonl", (settings.client_id, settings.client_secret))
    tokens = TokenProvider(settings, http)
    # Read-only modes return here, before Check (which calls mutating endpoints) is built.
    if args.list or args.dispute_id:
        try:
            if args.list:
                code = list_disputes(http, tokens)
            else:
                code = show_dispute(DisputesClient(settings, tokens, http), args.dispute_id)
        except httpx.HTTPStatusError as exc:
            print(f"FAIL: {paypal_error(exc.response)}")
            code = 1
        except httpx.HTTPError as exc:
            print(f"FAIL: {type(exc).__name__}: network error")
            code = 1
        print(f"raw requests/responses: logs/sandbox_check_{ts}.jsonl")
        return code
    client = DisputesClient(settings, tokens, http)
    check = Check(client, args)

    steps = [
        ("a create dispute", check.step_a_create),
        ("b CUSTOMER.DISPUTE.CREATED webhook", check.step_b_webhook),
        ("c GET dispute", check.step_c_get),
        ("d make-offer (deflect)", check.step_d_offer),
        ("e provide-evidence (defend)", check.step_e_evidence),
        ("f adjudicate (sandbox settle)", check.step_f_settle),
    ]
    failed = False
    for name, fn in steps:
        try:
            result = fn()
        except httpx.HTTPStatusError as exc:
            result = Result("FAIL", paypal_error(exc.response))
        except httpx.HTTPError as exc:
            result = Result("FAIL", f"{type(exc).__name__}: network error")
        failed |= result.verdict == "FAIL"
        print(f"[{result.verdict}] {name}: {result.reason}", flush=True)
    print(f"raw requests/responses: logs/sandbox_check_{ts}.jsonl")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
