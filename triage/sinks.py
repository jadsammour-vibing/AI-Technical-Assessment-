"""Persistent output destinations.

- output/records.jsonl          append-only log of every processed record (source of truth)
- output/queues/<queue>.jsonl   one file per destination queue (what each team would consume)
- output/records.json / .csv    pretty snapshot of all records, rebuilt after each write
- DOWNSTREAM_WEBHOOK_URL        optional: POST each record to e.g. a webhook.site URL
"""
from __future__ import annotations

import csv
import json
import os
import threading
import urllib.request
from pathlib import Path

OUT = Path(os.getenv("OUTPUT_DIR", Path(__file__).resolve().parent.parent / "output"))
_lock = threading.Lock()

CSV_COLUMNS = [
    "record_id", "source", "received_at", "category", "priority", "confidence",
    "core_issue", "account_ids", "invoice_numbers", "error_codes", "amounts",
    "products_or_features", "time_references", "urgency_level", "urgency_evidence",
    "impact_scope", "intended_queue", "destination_queue", "escalation_flag",
    "escalation_reasons", "summary", "raw_message", "model", "prompt_version",
]


def write_record(record: dict) -> bool:
    """Persist a record. Returns False if the record_id was already processed (idempotency)."""
    with _lock:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "queues").mkdir(exist_ok=True)
        log = OUT / "records.jsonl"
        if record["record_id"] in _seen_ids(log):
            return False
        line = json.dumps(record, ensure_ascii=False)
        with log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        qfile = OUT / "queues" / (record["destination_queue"].lower().replace(" ", "_") + ".jsonl")
        with qfile.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        _rebuild_snapshots(log)
    _post_webhook(record)
    return True


def already_processed(record_id: str) -> bool:
    """Cheap pre-check so duplicates don't spend LLM calls."""
    with _lock:
        return record_id in _seen_ids(OUT / "records.jsonl")


def load_records() -> list[dict]:
    """Latest attempt of every record, in first-seen order (used by the demo console)."""
    with _lock:
        log = OUT / "records.jsonl"
        if not log.exists():
            return []
        latest = {}
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                latest[r["record_id"]] = r
        return list(latest.values())


def _seen_ids(log: Path) -> set[str]:
    if not log.exists():
        return set()
    # Only successfully processed records count as duplicates; a record that failed
    # (e.g. LLM rate-limited) must be retryable by re-sending the same message.
    ids = set()
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if not r["meta"]["errors"]:
                ids.add(r["record_id"])
    return ids


def _rebuild_snapshots(log: Path) -> None:
    latest = {}
    for l in log.read_text(encoding="utf-8").splitlines():
        if l.strip():
            r = json.loads(l)
            latest[r["record_id"]] = r  # later attempts replace earlier failed ones
    records = list(latest.values())
    (OUT / "records.json").write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    with (OUT / "records.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in records:
            ids = r.get("identifiers") or {}
            urg = r.get("urgency_signal") or {}
            w.writerow({
                **{k: r.get(k) for k in CSV_COLUMNS if k in r},
                **{k: "; ".join(ids.get(k, [])) for k in
                   ["account_ids", "invoice_numbers", "error_codes", "amounts",
                    "products_or_features", "time_references"]},
                "urgency_level": urg.get("level"),
                "urgency_evidence": "; ".join(urg.get("evidence", [])),
                "escalation_reasons": "; ".join(r.get("escalation_reasons", [])),
                "model": r["meta"]["model"],
                "prompt_version": r["meta"]["prompt_version"],
            })


def _post_webhook(record: dict) -> None:
    url = os.getenv("DOWNSTREAM_WEBHOOK_URL")
    if not url:
        return
    try:
        req = urllib.request.Request(url, data=json.dumps(record).encode(),
                                     headers={"Content-Type": "application/json",
                                              "X-ArcVault-Queue": record["destination_queue"]})
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:  # a downstream outage must never lose the record. It's already on disk.
        print(f"  [warn] webhook delivery failed for {record['record_id']}: {e}")
