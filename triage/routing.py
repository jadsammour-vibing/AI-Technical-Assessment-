"""Deterministic routing + escalation logic.

Deliberately NOT done by the LLM. The model proposes labels. Plain, auditable code
decides where a ticket goes and whether a human must look at it. That makes this
logic unit-testable, cheap to change, and explainable to the teams receiving tickets.
"""
from __future__ import annotations

import re

CONFIDENCE_THRESHOLD = 0.70
BILLING_ESCALATION_THRESHOLD_USD = 500.0

# Category -> destination queue
QUEUE_MAP = {
    "Bug Report": "Engineering",
    "Incident/Outage": "Engineering On-Call",
    "Feature Request": "Product",
    "Billing Issue": "Billing",
    "Technical Question": "Technical Support",
}
ESCALATION_QUEUE = "Human Escalation"
FALLBACK_QUEUE = "Human Escalation"  # used when classification failed entirely

# Phrases that always get a human, whatever the model says (case-insensitive).
ESCALATION_PATTERNS = {
    "outage_keyword": r"\boutage\b",
    "service_down": r"\b(is|are|went|been|goes)\s+down\b|\bdown for (all|everyone|every)\b",
    "multi_user_impact": r"\b(multiple|several|many|all( of our)?)\s+(users|customers|accounts|people)\b",
    "security_signal": r"\b(breach|hacked|compromised|data leak|unauthori[sz]ed access)\b",
    "legal_threat": r"\b(lawyer|legal action|lawsuit|chargeback)\b",
}

_AMOUNT_RE = re.compile(r"\$\s?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)")


def parse_amounts(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in _AMOUNT_RE.findall(text)]


def billing_exposure(message: str) -> float | None:
    """Estimate the disputed amount in a billing message.

    Assumption (documented in ARCHITECTURE.md): "billing error > $500" means the size
    of the ERROR, not the invoice total. With two or more amounts (charged vs
    expected) we use the difference. With one amount we use it as-is (conservative).
    """
    amounts = parse_amounts(message)
    if not amounts:
        return None
    if len(amounts) >= 2:
        return round(max(amounts) - min(amounts), 2)
    return amounts[0]


def decide_route(message: str, classification: dict | None) -> dict:
    """Return the routing block for a record.

    classification is None when the LLM step failed. In that case fail safe to a human.
    """
    reasons: list[str] = []

    if classification is None:
        return {
            "intended_queue": None,
            "destination_queue": FALLBACK_QUEUE,
            "escalated": True,
            "escalation_reasons": ["classification_failed"],
        }

    category = classification["category"]
    confidence = classification["confidence"]
    intended = QUEUE_MAP[category]

    # Rule 1 - low confidence: the model is unsure, so a human checks before routing.
    if confidence < CONFIDENCE_THRESHOLD:
        reasons.append(f"low_confidence ({confidence:.2f} < {CONFIDENCE_THRESHOLD:.2f})")

    # Rule 2 - every outage/incident gets a human incident commander.
    if category == "Incident/Outage":
        reasons.append("category_incident_outage")

    # Rule 3 - keyword / phrase criteria (independent of the model's opinion).
    lowered = message.lower()
    for name, pattern in ESCALATION_PATTERNS.items():
        if re.search(pattern, lowered):
            reasons.append(f"keyword:{name}")

    # Rule 4 - large billing error.
    if category == "Billing Issue":
        exposure = billing_exposure(message)
        if exposure is not None and exposure > BILLING_ESCALATION_THRESHOLD_USD:
            reasons.append(f"billing_error_over_${BILLING_ESCALATION_THRESHOLD_USD:.0f} (${exposure:,.2f})")

    escalated = bool(reasons)
    return {
        "intended_queue": intended,
        "destination_queue": ESCALATION_QUEUE if escalated else intended,
        "escalated": escalated,
        "escalation_reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Identifier hallucination guard
# ---------------------------------------------------------------------------
DESCRIPTIVE_KEYS = {"products_or_features"}


def ground_evidence(evidence: list[str], message: str) -> list[str]:
    """Keep only urgency evidence that is actually quoted from the customer's message."""
    norm_msg = _norm(message)
    return [e for e in evidence if _norm(e.strip(' "\'')) in norm_msg]


def ground_identifiers(identifiers: dict, message: str) -> tuple[dict, list[str]]:
    """Drop any extracted identifier that does not literally appear in the message.

    Returns (clean_identifiers, dropped_values). LLMs sometimes "normalise" an
    identifier (e.g. invoice "8821" -> "INV-8821"). A downstream team searching
    their billing system for a made-up ID is worse than no ID at all.
    """
    norm_msg = _norm(message)
    clean, dropped = {}, []
    for key, values in identifiers.items():
        if key in DESCRIPTIVE_KEYS:  # tags like "login" may legitimately paraphrase the text
            clean[key] = list(dict.fromkeys(v for v in values or [] if isinstance(v, str) and v.strip()))
            continue
        kept = []
        for v in values or []:
            if isinstance(v, str) and v.strip() and _norm(v) in norm_msg:
                if v not in kept:
                    kept.append(v)
            else:
                dropped.append(f"{key}:{v}")
        clean[key] = kept
    return clean, dropped


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().replace("’", "'")).strip()
