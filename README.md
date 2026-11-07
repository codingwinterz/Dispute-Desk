# DisputeDesk

An AI agent that decides whether to defend or settle PayPal disputes.
This branch (`sandbox-spike`) only proves that the real PayPal sandbox dispute flow works.

## What is real vs seeded

| Data | Source |
|---|---|
| PayPal disputes (details, stage, status, offers, evidence, sandbox settlement) | **Real**: PayPal sandbox REST API (`https://api-m.sandbox.paypal.com`) |
| PayPal webhooks (`CUSTOMER.DISPUTE.*`) | **Real**: delivered by the PayPal sandbox and verified with PayPal's verify-webhook-signature API |
| Customers, transactions, disputes in the graph | **Real** (`source="paypal"`): built from the stored dispute JSON |
| Shipments, IP / device telemetry, past disputes (`SEED-…`) | **Seeded** (`source="seeded"`): merchant-supplied test data in `seed/merchant_data.json`, not from PayPal |
| Decisions (DEFEND / ACCEPT) | **Generated** by the LLM from the graph facts and policy rules above |

Sandbox only. There are no live credentials and no live base URL anywhere in this code.

## Layout

- `disputedesk/auth.py`: OAuth client-credentials token with caching
- `disputedesk/webhooks.py`: FastAPI receiver `POST /webhooks/paypal`
- `disputedesk/disputes.py`: Disputes API client
- `disputedesk/poller.py`: background poller started with the app (read-only GETs)
- `disputedesk/dispute_store.py`: SQLite store of processed dispute IDs, shared by poller and webhook
- `disputedesk/events.py`: in-process `dispute_detected` and `dispute_updated` events
- `disputedesk/graph.py`: FalkorDB graph store and the one-query dispute context
- `disputedesk/seed.py`, `seed/merchant_data.json`: seeded merchant context
- `disputedesk/policies.py`: policy rules P1–P4 the agent may cite
- `disputedesk/decision.py`: LiteLLM decision step with schema validation and citation checks
- `disputedesk/decision_store.py`: SQLite store of decisions
- `disputedesk/analyzer.py`: worker thread that runs decisions automatically
- `scripts/sandbox_check.py`: step-by-step check against the real sandbox (not part of pytest)
- `scripts/preflight.py`: read-only environment check (.env, FalkorDB, sandbox-only URL)
- `scripts/seed_graph.py`, `scripts/analyze.py`: seed the graph; run decisions from the CLI
- `tests/`: unit tests with mocked HTTP

## Setup

Python 3.12 (LiteLLM's native dependencies have no wheels for 3.15 yet) and Docker.

```sh
docker run -d --name falkordb -p 6379:6379 falkordb/falkordb
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
cp .env.example .env    # then fill in the values; never commit .env
.venv/Scripts/python -m pytest -q
```

## Running the sandbox check

1. Start the receiver: `.venv/Scripts/python -m uvicorn --factory disputedesk.webhooks:create_app --port 8000`
2. Expose it: `ngrok http 8000`
3. In the developer dashboard, add a webhook to your sandbox app pointing at
   `https://<ngrok-host>/webhooks/paypal` and subscribe to `CUSTOMER.DISPUTE.*`.
   Put its ID in `PAYPAL_WEBHOOK_ID` and restart the receiver.
4. As the **sandbox personal (buyer)** account, pay the **sandbox business** account,
   then open disputes on those payments at sandbox.paypal.com (Resolution Center).
5. Run `.venv/Scripts/python scripts/sandbox_check.py --inquiry-dispute-id PP-D-... [--claim-dispute-id PP-D-...]`
   (read-only: `--list` lists disputes; `--dispute-id PP-D-...` shows one dispute's details with a single GET and no mutating calls)

Logs (raw requests/responses with secrets redacted, plus webhook events) go to `logs/`, which is git-ignored.

## Dispute polling

Polling is the primary detector: no dispute webhook has arrived in the sandbox so far. The webhook
receiver stays on as a second source.

The receiver app polls `GET /v1/customer/disputes` every `POLL_SECONDS` (default 10) and follows
`next` pages. For each dispute ID it has not seen, it fetches `GET /v1/customer/disputes/{id}`,
saves the JSON to `logs/disputes/<id>.json` and emits `dispute_detected` with `source="poll"`.
Dispute webhooks emit the same event with `source="webhook"`.

- Both paths share `data/disputedesk.db` (SQLite, git-ignored). Claiming a dispute is one atomic
  insert, so each dispute is emitted once, by whichever path sees it first, even if both see it at
  the same moment. If the webhook got there first, the next poll stores the full dispute
  without emitting a second event.
- A 401 refreshes the token and retries once. 429s (honouring `Retry-After`), 5xx responses and
  network errors back off exponentially, capped at 300s. The loop never stops on an error.
- Every poll logs `poll ok count=<items> new=<unseen ids> changed=<refreshed ids>`.
- Disputes already stored are refreshed when the list shows a different `status`,
  `dispute_life_cycle_stage`, `update_time` or set of links: the poller GETs that dispute again,
  stores it and emits `dispute_updated`. Unchanged disputes cost no extra request.
- The poller only sends GETs. No code path calls provide-evidence, accept-claim, make-offer,
  escalate or adjudicate.

`GET /api/disputes` returns the stored disputes as JSON:
id, status, stage, reason, amount, due date, source, detected_at and action link names.
It makes no PayPal call, and it refuses requests that came through the ngrok tunnel
(those carry `X-Forwarded-For`).

```sh
# run the app (webhook receiver + poller); optional: POLL_SECONDS=30 in .env
.venv/Scripts/python -m uvicorn --factory disputedesk.webhooks:create_app --port 8000
# in another terminal
curl http://127.0.0.1:8000/api/disputes
```

## Decision agent

For each dispute with full details, the agent decides **DEFEND** or **ACCEPT**:

1. The dispute is merged into the FalkorDB graph (`Customer -INITIATED-> Transaction -HAS_DISPUTE-> Dispute`,
   plus seeded `FULFILLED_BY -> Shipment` and `LOGGED_FROM -> Telemetry`).
2. One Cypher query returns the buyer, transaction, shipment, telemetry, and other disputes whose
   transaction shares the checkout device hash or IP.
3. That result becomes numbered facts (`F1`…), each tagged `paypal` or `seeded`. The LLM (LiteLLM,
   model `LLM_MODEL`, default `gemini/gemini-3.5-flash-lite`) gets only those facts and the policy rules
   (`P1`…P4), and returns JSON: decision, risk_score (likelihood 0–100 that the dispute is invalid or
   fraudulent, based only on the supplied facts; a clean case accepted for cost reasons scores low),
   3–5 reasoning points and a draft evidence summary. Every draft ends with a note that shipment and
   device records are merchant-supplied sandbox test data; linked disputes are described only as
   sharing a device hash or IP, and a pattern is not proof.
4. The reply is validated. Every reasoning point must cite supplied IDs; an unknown ID or bad JSON
   or a forbidden word (fraud, abuse, abuser, ring) gets one retry, then the decision is stored as an
   error (and retried by the next sweep). A 429 or 503 from the LLM is retried up to 3 times with
   backoff (1s, 2s).
5. Decisions go to the `decisions` table in `data/disputedesk.db`.

The app runs this automatically: on `dispute_detected`, on `dispute_updated` (until a decision
exists), and in a sweep of stored disputes at startup. The work happens on one worker thread, so
the poller and webhook never wait for the LLM. No PayPal write call is made.

```sh
.venv/Scripts/python scripts/seed_graph.py --reset          # build the graph (no PayPal or LLM calls)
.venv/Scripts/python scripts/analyze.py --all --facts-only  # what the model would see (no LLM call)
.venv/Scripts/python scripts/analyze.py --all               # one LLM call per undecided dispute
```

Sandbox limit: every sandbox dispute here was opened by the same test buyer, so the graph links
disputes only through a shared device hash or IP, never through the buyer.
