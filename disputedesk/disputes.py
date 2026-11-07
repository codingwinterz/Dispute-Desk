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
