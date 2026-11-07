"""Client for the PayPal Customer Disputes v1 API (sandbox).

Reference: https://developer.paypal.com/docs/api/customer-disputes/v1/
Spec used to check fields/enums: https://developer.paypal.com/api/customer-disputes/v1/schema.yaml
"""

import json
import logging
from typing import Any

import httpx

from disputedesk.auth import TokenProvider
from disputedesk.config import Settings

log = logging.getLogger("disputedesk.disputes")

DOC = "https://developer.paypal.com/docs/api/customer-disputes/v1/"
# Upper bound on list pages per poll, so a bad "next" link can never loop forever.
MAX_PAGES = 20


PROVIDE_EVIDENCE = "provide_evidence"
ACCEPT_CLAIM = "accept_claim"
# Evidence type for a notes-only defence. PayPal requested OTHER from the seller on every sandbox
# dispute seen so far (evidences[].source = REQUESTED_FROM_SELLER).
EVIDENCE_TYPE = "OTHER"
# Fixed accept-claim note, shown in the confirm dialog before sending.
ACCEPT_NOTE = "Accepting the claim: full refund to the buyer."


def build_action_request(base: str, action: str, dispute_id: str, text: str,
                         headers: dict[str, str]) -> httpx.Request:
    """The exact request for an approved response. The dry run builds it with a redacted token and
    shows it; the submit path builds it the same way and sends it. Never sent from here."""
    if action == PROVIDE_EVIDENCE:
        # POST /v1/customer/disputes/{id}/provide-evidence, multipart/form-data.
        # Verified from PayPal's sample (Use Disputes API guide): a part named "input" with
        # type=application/json holding {"evidences": [{"evidence_type", "notes", ...}]}; notes is
        # 1-2000 characters (OpenAPI spec, evidence.notes).
        # UNVERIFIED: a notes-only request with no file part. PayPal's sample also attaches "file1";
        # the guide implies files are optional, but no sample shows it. Marked UNVERIFIED in the UI
        # until one succeeds (disputedesk.submissions records the first success).
        # https://developer.paypal.com/platforms/disputes/handle-disputes/use-disputes-api
        # https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_provide-evidence
        metadata = {"evidences": [{"evidence_type": EVIDENCE_TYPE, "notes": text}]}
        request = httpx.Request("POST", f"{base}/{dispute_id}/provide-evidence", headers=headers,
                                files={"input": (None, json.dumps(metadata), "application/json")})
        # A multipart body is a stream; read it now so the redacting logger (and the dry run) can see
        # the exact bytes that are sent. Unread, logging the request raises httpx.RequestNotRead.
        request.read()
        return request
    if action == ACCEPT_CLAIM:
        # POST /v1/customer/disputes/{id}/accept-claim, application/json {"note": "..."} as in
        # PayPal's sample. The guide mentions an accept-claim reason and invoice ID but does not
        # name those fields, so they are not sent; with no accept_claim_type PayPal applies its
        # default (full refund). Closes the dispute in the buyer's favour.
        # https://developer.paypal.com/platforms/disputes/handle-disputes/use-disputes-api
        # https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_accept-claim
        return httpx.Request("POST", f"{base}/{dispute_id}/accept-claim", headers=headers, json={"note": text})
    raise ValueError(f"unknown action {action!r}")


class DisputesClient:
    def __init__(self, settings: Settings, tokens: TokenProvider, http: httpx.Client):
        self._base = f"{settings.base_url}/v1/customer/disputes"
        self._tokens = tokens
        self._http = http
        # Last observed (stage, status) per dispute, so action log lines can include them.
        self._state: dict[str, tuple[str | None, str | None]] = {}

    # --- reads -------------------------------------------------------------

    def list_disputes(self, page_size: int = 50, max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
        # GET /v1/customer/disputes ("List disputes"). Response: {"items": [...], "links": [...]}.
        # Query params page_size and next_page_token; when more pages exist the response has a
        # link with rel "next".
        # https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_list
        # UNVERIFIED: the page_size maximum (50), and that the "next" link's href carries the
        # next_page_token query param. The 4 real sandbox disputes fit on one page, so paging has
        # only been exercised against mocked responses.
        items: list[dict[str, Any]] = []
        params: dict[str, str] = {"page_size": str(page_size)}
        seen_params: list[dict[str, str]] = []
        for _ in range(max_pages):
            resp = self._request("GET", "", dispute_id="-", action="list", params=params)
            if resp.is_error:
                self._log("-", "list", resp.status_code)
                resp.raise_for_status()
            body = resp.json()
            items.extend(body.get("items") or [])
            seen_params.append(params)
            next_params = self._next_page_params(body)
            if next_params is None:
                return items
            if next_params in seen_params:
                log.warning("list disputes: next link repeats an earlier page; stopping")
                return items
            params = next_params
        log.warning("list disputes: stopped after %d pages", max_pages)
        return items

    def _next_page_params(self, body: dict[str, Any]) -> dict[str, str] | None:
        for link in body.get("links") or []:
            if not isinstance(link, dict) or link.get("rel") != "next" or not link.get("href"):
                continue
            url = httpx.URL(link["href"])
            base = httpx.URL(self._base)
            # Only follow a link back to the same sandbox list endpoint, so the bearer token is
            # never sent anywhere else. Its query params (e.g. next_page_token) are reused.
            if (url.scheme, url.host, url.path.rstrip("/")) != (base.scheme, base.host, base.path):
                log.warning("list disputes: ignored next link to a different endpoint")
                return None
            params = dict(url.params)
            return params or None
        return None

    def get_dispute(self, dispute_id: str) -> dict[str, Any]:
        # GET /v1/customer/disputes/{id}
        # https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_get
        resp = self._request("GET", f"/{dispute_id}", dispute_id=dispute_id, action="get")
        if resp.is_error:
            self._log(dispute_id, "get", resp.status_code)
            resp.raise_for_status()
        dispute = resp.json()
        self._state[dispute_id] = (dispute.get("dispute_life_cycle_stage"), dispute.get("status"))
        self._log(dispute_id, "get", resp.status_code)
        return dispute

    # --- merchant actions --------------------------------------------------

    def make_offer(
        self,
        dispute_id: str,
        note: str,
        offer_type: str,
        offer_amount: dict[str, str] | None = None,
    ) -> httpx.Response:
        # POST /v1/customer/disputes/{id}/make-offer. Stage must be INQUIRY.
        # Required: note, offer_type (REFUND | REFUND_WITH_RETURN | REFUND_WITH_REPLACEMENT |
        # REPLACEMENT_WITHOUT_REFUND); offer_amount is {currency_code, value}.
        # https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_make-offer
        body: dict[str, Any] = {"note": note, "offer_type": offer_type}
        if offer_amount:
            body["offer_amount"] = offer_amount
        return self._request("POST", f"/{dispute_id}/make-offer", dispute_id=dispute_id,
                             action="make-offer", json=body)

    def provide_evidence(
        self,
        dispute_id: str,
        file_name: str,
        file_bytes: bytes,
        file_content_type: str,
        notes: str,
        evidence_type: str = "OTHER",
    ) -> httpx.Response:
        # POST /v1/customer/disputes/{id}/provide-evidence, multipart/form-data.
        # Status must be WAITING_FOR_SELLER_RESPONSE and the provide-evidence link must be present.
        # Verified: file part name "evidence-file"; evidence fields evidence_type, notes;
        # "OTHER" is in the evidence_type enum.
        # UNVERIFIED: the JSON metadata part named "input" holding {"evidences": [...]}. The
        # public spec only lists "evidence-file"; the "input" part is inferred, not documented.
        # https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_provide-evidence
        metadata = {"evidences": [{"evidence_type": evidence_type, "notes": notes}]}
        files = {
            "input": (None, json.dumps(metadata), "application/json"),
            "evidence-file": (file_name, file_bytes, file_content_type),
        }
        return self._request("POST", f"/{dispute_id}/provide-evidence", dispute_id=dispute_id,
                             action="provide-evidence", files=files)

    # --- approved merchant responses (Phase 3) --------------------------------
    # Only disputedesk.submissions calls these, from the confirmed approval route. The poller and the
    # webhook path never do.

    def send_action(self, action: str, dispute_id: str, text: str) -> httpx.Response:
        """Send one approved response. A 401 refreshes the token and resends once (a 401 means PayPal
        did not process the request). Any other outcome is returned to the caller as-is."""
        for attempt in (1, 2):
            request = build_action_request(self._base, action, dispute_id, text, self._tokens.auth_headers())
            resp = self._http.send(request)
            if resp.status_code == 401 and attempt == 1:
                self._tokens.invalidate()
                continue
            break
        self._log(dispute_id, action, resp.status_code)
        return resp

    def escalate(self, dispute_id: str, note: str) -> httpx.Response:
        # POST /v1/customer/disputes/{id}/escalate: escalates an inquiry to a PayPal claim.
        # Stage must be INQUIRY (documented).
        # UNVERIFIED: the body {"note": ...}. The escalate schema is truncated in the public spec.
        # UNVERIFIED: which status the dispute has after escalation (e.g. UNDER_REVIEW).
        # https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_escalate
        return self._request("POST", f"/{dispute_id}/escalate", dispute_id=dispute_id,
                             action="escalate", json={"note": note})

    # --- sandbox-only ------------------------------------------------------

    def require_evidence(self, dispute_id: str, action: str) -> httpx.Response:
        # SANDBOX ONLY. POST /v1/customer/disputes/{id}/require-evidence ("Update dispute status").
        # Status must be UNDER_REVIEW and the require-evidence link must be present.
        # action: BUYER_EVIDENCE -> WAITING_FOR_BUYER_RESPONSE, SELLER_EVIDENCE -> WAITING_FOR_SELLER_RESPONSE
        # https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_require-evidence
        return self._request("POST", f"/{dispute_id}/require-evidence", dispute_id=dispute_id,
                             action="require-evidence", json={"action": action})

    def adjudicate(self, dispute_id: str, adjudication_outcome: str) -> httpx.Response:
        # SANDBOX ONLY. POST /v1/customer/disputes/{id}/adjudicate ("Settle dispute").
        # Status must be UNDER_REVIEW and the adjudicate link must be present.
        # Required: adjudication_outcome = BUYER_FAVOR | SELLER_FAVOR
        # https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_adjudicate
        return self._request("POST", f"/{dispute_id}/adjudicate", dispute_id=dispute_id,
                             action="adjudicate", json={"adjudication_outcome": adjudication_outcome})

    def create_dispute(self, *_: Any, **__: Any) -> None:
        # NOT IMPLEMENTED. Sandbox dispute creation from the buyer side needs the DISPUTE_CREATE
        # scope (https://uri.paypal.com/services/disputes/create). Per the docs, only a PayPal
        # account manager can add it, and it also needs Log in with PayPal buyer consent and a
        # PayPal-Auth-Assertion header. The create endpoint is not in the public spec, so its
        # path and body are UNVERIFIED.
        # https://developer.paypal.com/platforms/disputes/set-up
        raise NotImplementedError(
            "API dispute creation needs the DISPUTE_CREATE scope; open the dispute manually "
            "in the sandbox as the personal buyer instead"
        )

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def has_link(dispute: dict[str, Any], rel: str) -> bool:
        """True if the dispute's HATEOAS links offer the given action (e.g. "adjudicate").

        The docs name links like "require-evidence"; whether real responses use "-" or "_"
        is UNVERIFIED, so both spellings match.
        """
        want = rel.replace("_", "-")
        return any(str(link.get("rel", "")).replace("_", "-") == want for link in dispute.get("links", []))

    def _request(self, method: str, path: str, *, dispute_id: str, action: str, **kwargs: Any) -> httpx.Response:
        for attempt in (1, 2):
            resp = self._http.request(method, self._base + path, headers=self._tokens.auth_headers(), **kwargs)
            if resp.status_code == 401 and attempt == 1:
                self._tokens.invalidate()
                continue
            break
        if method != "GET":
            self._log(dispute_id, action, resp.status_code)
        return resp

    def _log(self, dispute_id: str, action: str, status_code: int) -> None:
        stage, status = self._state.get(dispute_id, (None, None))
        log.info("dispute=%s stage=%s status=%s action=%s http=%s",
                 dispute_id, stage, status, action, status_code)
