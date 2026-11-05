"""The triage pipeline: classify -> enrich -> route/escalate -> structured record."""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

from . import prompts as P
from .llm import LLMClient, LLMError
from .routing import decide_route, ground_evidence, ground_identifiers

IDENTIFIER_KEYS = ["account_ids", "invoice_numbers", "error_codes", "amounts",
                   "products_or_features", "time_references"]


class ValidationError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Schema validation (hand-rolled to stay dependency-free)
# ---------------------------------------------------------------------------
def validate_classification(d: dict) -> dict:
    cat = d.get("category")
    if cat not in P.CATEGORIES:
        # Tolerate case/spacing differences such as "incident / outage".
        match = next((c for c in P.CATEGORIES if _squash(c) == _squash(str(cat))), None)
        if not match:
            raise ValidationError(f"category must be one of {P.CATEGORIES}, got {cat!r}")
        cat = match
    pri = str(d.get("priority", "")).capitalize()
    if pri not in P.PRIORITIES:
        raise ValidationError(f"priority must be one of {P.PRIORITIES}, got {d.get('priority')!r}")
    try:
        conf = float(d.get("confidence"))
    except (TypeError, ValueError):
        raise ValidationError(f"confidence must be a number, got {d.get('confidence')!r}")
    if conf > 1 and conf <= 100:  # some models answer 85 instead of 0.85
        conf = conf / 100
    if not 0 <= conf <= 1:
        raise ValidationError(f"confidence must be between 0 and 1, got {conf}")
    runner = d.get("runner_up_category")
    return {
        "category": cat,
        "priority": pri,
        "confidence": round(conf, 2),
        "runner_up_category": runner if runner in P.CATEGORIES and runner != cat else None,
        "rationale": str(d.get("rationale", "")).strip(),
    }


def validate_enrichment(d: dict) -> dict:
    core = str(d.get("core_issue", "")).strip()
    if not core:
        raise ValidationError("core_issue is required")
    summary = str(d.get("summary", "")).strip()
    if not summary:
        raise ValidationError("summary is required")
    ids_in = d.get("identifiers") or {}
    if not isinstance(ids_in, dict):
        raise ValidationError("identifiers must be an object")
    identifiers = {}
    for k in IDENTIFIER_KEYS:
        v = ids_in.get(k) or []
        identifiers[k] = [str(x) for x in (v if isinstance(v, list) else [v]) if x not in (None, "")]
    urg = d.get("urgency_signal") or {}
    level = str(urg.get("level", "")).lower()
    if level not in P.URGENCY_LEVELS:
        raise ValidationError(f"urgency_signal.level must be one of {P.URGENCY_LEVELS}, got {level!r}")
    evidence = urg.get("evidence") or []
    scope = d.get("impact_scope") if d.get("impact_scope") in (
        "single_user", "multiple_users", "organization", "unknown") else "unknown"
    return {
        "core_issue": core,
        "identifiers": identifiers,
        "urgency_signal": {"level": level, "evidence": [str(e) for e in evidence][:5]},
        "impact_scope": scope,
        "summary": summary,
    }


def _squash(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


# ---------------------------------------------------------------------------
# LLM step runner: call, validate, one repair attempt with the error fed back
# ---------------------------------------------------------------------------
def run_llm_step(llm: LLMClient, system: str, user: str, validator) -> tuple[dict, int]:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    attempts = 0
    for _ in range(2):
        attempts += 1
        raw = llm.chat_json(messages)
        try:
            return validator(raw), attempts
        except ValidationError as e:
            messages += [
                {"role": "assistant", "content": str(raw)},
                {"role": "user", "content": P.REPAIR_USER.format(error=e)},
            ]
            last = e
    raise ValidationError(f"output still invalid after repair: {last}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def make_record_id(source: str, message: str) -> str:
    return "AV-" + hashlib.sha256(f"{source}|{message.strip()}".encode()).hexdigest()[:10].upper()


def process_message(llm: LLMClient, message: str, source: str = "unknown",
                    received_at: str | None = None, external_id: str | None = None) -> dict:
    t0 = time.perf_counter()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    errors: list[str] = []
    attempts = {"classification": 0, "enrichment": 0}

    # Step 2 - classification
    classification = None
    try:
        classification, attempts["classification"] = run_llm_step(
            llm, P.CLASSIFY_SYSTEM, P.CLASSIFY_USER.format(source=source, message=message),
            validate_classification)
    except (LLMError, ValidationError) as e:
        errors.append(f"classification: {e}")

    # Step 4 + 6 - routing and escalation (deterministic)
    routing = decide_route(message, classification)

    # Step 3 - enrichment + team summary (runs even if classification failed,
    # so the human reviewing the fallback still gets a useful summary)
    enrichment = None
    try:
        enrichment, attempts["enrichment"] = run_llm_step(
            llm, P.ENRICH_SYSTEM,
            P.ENRICH_USER.format(
                source=source, message=message,
                category=classification["category"] if classification else "Unknown",
                queue=routing["intended_queue"] or routing["destination_queue"]),
            validate_enrichment)
        enrichment["identifiers"], dropped = ground_identifiers(enrichment["identifiers"], message)
        if dropped:
            enrichment["dropped_ungrounded_identifiers"] = dropped
        enrichment["urgency_signal"]["evidence"] = ground_evidence(enrichment["urgency_signal"]["evidence"], message)
    except (LLMError, ValidationError) as e:
        errors.append(f"enrichment: {e}")
        if not routing["escalated"]:
            routing.update(destination_queue="Human Escalation", escalated=True)
        routing["escalation_reasons"].append("enrichment_failed")

    return {
        "record_id": make_record_id(source, message),
        "external_id": external_id,
        "source": source,
        "received_at": received_at or now,
        "processed_at": now,
        "raw_message": message,
        # Step 2
        "category": classification["category"] if classification else None,
        "priority": classification["priority"] if classification else None,
        "confidence": classification["confidence"] if classification else None,
        "classification_rationale": classification["rationale"] if classification else None,
        "runner_up_category": classification["runner_up_category"] if classification else None,
        # Step 3
        "core_issue": enrichment["core_issue"] if enrichment else None,
        "identifiers": enrichment["identifiers"] if enrichment else {},
        "urgency_signal": enrichment["urgency_signal"] if enrichment else None,
        "impact_scope": enrichment["impact_scope"] if enrichment else None,
        # Step 4 + 6
        "intended_queue": routing["intended_queue"],
        "destination_queue": routing["destination_queue"],
        "escalation_flag": routing["escalated"],
        "escalation_reasons": routing["escalation_reasons"],
        # Step 5
        "summary": enrichment["summary"] if enrichment else None,
        "meta": {
            "provider": llm.provider,
            "model": llm.model,
            "prompt_version": P.PROMPT_VERSION,
            "llm_attempts": attempts,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "errors": errors,
            "dropped_ungrounded_identifiers": (enrichment or {}).get("dropped_ungrounded_identifiers", []),
        },
    }
