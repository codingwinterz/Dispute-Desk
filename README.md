# DisputeDesk

An AI agent that decides whether to defend or settle PayPal disputes.
This branch (`sandbox-spike`) only proves that the real PayPal sandbox dispute flow works.

## What is real vs seeded

| Data | Source |
|---|---|
| PayPal disputes (details, stage, status, offers, evidence, sandbox settlement) | **Real**: PayPal sandbox REST API (`https://api-m.sandbox.paypal.com`) |
| PayPal webhooks (`CUSTOMER.DISPUTE.*`) | **Real**: delivered by the PayPal sandbox and verified with PayPal's verify-webhook-signature API |
| IP / device telemetry for orders | **Seeded**: merchant-supplied test data, not from PayPal |

Sandbox only. There are no live credentials and no live base URL anywhere in this code.

## Layout

- `disputedesk/auth.py`: OAuth client-credentials token with caching
- `disputedesk/webhooks.py`: FastAPI receiver `POST /webhooks/paypal`
- `disputedesk/disputes.py`: Disputes API client
- `disputedesk/poller.py`: background poller started with the app (read-only GETs)
- `disputedesk/dispute_store.py`: SQLite store of processed dispute IDs, shared by poller and webhook
- `disputedesk/events.py`: in-process `dispute_detected` event
- `scripts/sandbox_check.py`: step-by-step check against the real sandbox (not part of pytest)
- `tests/`: unit tests with mocked HTTP

## Setup

```sh
python -m venv .venv
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

The receiver app also polls `GET /v1/customer/disputes` every `POLL_SECONDS` (default 10).
For each dispute ID it has not seen, it fetches `GET /v1/customer/disputes/{id}`, saves the JSON to
`logs/disputes/<id>.json` and emits `dispute_detected` with `source="poll"`. Dispute webhooks emit the
same event with `source="webhook"`. Both paths share `logs/disputes.sqlite3`, so each dispute is
emitted once, by whichever path sees it first. A 401 refreshes the token and retries once.
429 responses (honouring `Retry-After` if present), 5xx responses and network errors back off
exponentially, capped at 300s. The loop never stops on an error. The poller only uses GET.
