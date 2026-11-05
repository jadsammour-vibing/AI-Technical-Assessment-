"""Score output/records.json against eval/golden.json.

    python eval/evaluate.py
"""
import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles
except Exception:
    pass
ROOT = Path(__file__).resolve().parent.parent
golden = {k: v for k, v in json.loads((ROOT / "eval/golden.json").read_text()).items() if not k.startswith("_")}
OUT = Path(os.getenv("OUTPUT_DIR", ROOT / "output"))
records = {r["external_id"]: r for r in json.loads((OUT / "records.json").read_text())}

checks = {"category": 0, "priority": 0, "queue": 0, "escalation": 0, "entities": 0}
lines = ["| id | category | priority | queue | escalation | entities |", "|---|---|---|---|---|---|"]
n = 0
for sid, g in golden.items():
    r = records.get(sid)
    if not r:
        lines.append(f"| {sid} | missing | | | | |")
        continue
    n += 1
    flat_ids = " ".join(v for vals in (r.get("identifiers") or {}).values() for v in vals)
    ok = {
        "category": r["category"] in g["category"],
        "priority": r["priority"] in g["priority"],
        "queue": r["intended_queue"] == g["queue"],
        "escalation": g["escalated"] is None or r["escalation_flag"] == g["escalated"],
        "entities": all(e.lower() in flat_ids.lower() for e in g["must_extract"]),
    }
    for k, v in ok.items():
        checks[k] += v
    cell = lambda k, val: f"{'✅' if ok[k] else '❌'} {val}"
    lines.append(f"| {sid} | {cell('category', r['category'])} | {cell('priority', r['priority'])} | "
                 f"{cell('queue', r['intended_queue'])} | {cell('escalation', r['escalation_flag'])} | "
                 f"{cell('entities', ', '.join(g['must_extract']) or '-')} |")

summary = "  ".join(f"{k}: {v}/{n}" for k, v in checks.items())
report = f"# Evaluation vs golden labels\n\nModel: {next(iter(records.values()))['meta']['model'] if records else '?'}\n\n" \
         + "\n".join(lines) + f"\n\n**{summary}**\n"
(OUT / "eval_report.md").write_text(report, encoding="utf-8")
print(report)
sys.exit(0 if all(v == n for v in checks.values()) else 1)
