"""Process a file of messages directly (no server). Handy for re-runs and evaluation.

    python run_batch.py                       # processes data/samples.json -> output/
    python run_batch.py data/extra_edge_cases.json
"""
import json
import sys

from triage.llm import LLMClient
from triage.pipeline import process_message
from triage.sinks import OUT, write_record

path = sys.argv[1] if len(sys.argv) > 1 else "data/samples.json"
llm = LLMClient()
print(f"Provider={llm.provider} model={llm.model}\n")

for item in json.load(open(path, encoding="utf-8")):
    rec = process_message(llm, item["message"], item["source"], external_id=item.get("id"))
    new = write_record(rec)
    print(f"{item['id']:<10} {str(rec['category']):<19} {str(rec['priority']):<7} "
          f"conf={rec['confidence']}  -> {rec['destination_queue']:<20}"
          f"{' ESCALATED: ' + ', '.join(rec['escalation_reasons']) if rec['escalation_flag'] else ''}"
          f"{'' if new else '  [duplicate, skipped]'}")
    if rec["meta"]["errors"]:
        print("           errors:", rec["meta"]["errors"])

print(f"\nWrote {OUT / 'records.json'} and {OUT / 'records.csv'}")
