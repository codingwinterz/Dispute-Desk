"""Merchant policy rules the decision step may cite (P1..). Defaults for Phase 0; editable later."""

# P2: below this amount, contesting costs more than the dispute is worth.
ACCEPT_UNDER_AMOUNT = 15.00

DEFAULT_RULES: list[dict[str, str]] = [
    {"id": "P1", "text": "For MERCHANDISE_OR_SERVICE_NOT_RECEIVED: DEFEND when the carrier shows the parcel "
                         "DELIVERED to the shipping destination, strongest with a buyer signature."},
    {"id": "P2", "text": f"Recommend ACCEPT when the disputed amount is under {ACCEPT_UNDER_AMOUNT:.2f} USD and no "
                         "other dispute shares the checkout device or IP: contesting costs more than the amount."},
    {"id": "P3", "text": "DEFEND when the checkout device hash or IP is shared with two or more other disputes: "
                         "a repeat pattern across disputes justifies contesting. A pattern is not proof. P3 takes precedence over P2."},
    {"id": "P4", "text": "For MERCHANDISE_OR_SERVICE_NOT_RECEIVED: ACCEPT when there is no carrier scan after the "
                         "label was created, because there is no proof of shipment or delivery."},
]
