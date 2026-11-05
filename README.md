# ArcVault AI Intake & Triage Pipeline

An agentic workflow that ingests unstructured customer messages, then:
1. classifies them with an LLM (category, priority, confidence);
2. enriches them (core issue, identifiers, urgency, team summary);
3. routes them with deterministic rules;
4. flags high-risk or low-confidence cases for human escalation.

Each message becomes one structured JSON record.

| Deliverable (brief §4) | Where |
|---|---|
| 4.1 Working workflow | **Demo video (5½ min, narrated):** [`docs/demo_walkthrough.mp4`](docs/demo_walkthrough.mp4) (captions in `.srt`). Python service (`intake_server.py`) with a live demo console (`ui/index.html`) **and** n8n export (`n8n/arcvault_triage_workflow.json`), plus real-run screenshots in `docs/` |
| 4.2 Structured output | `output/records.json` (also `.csv`, plus per-queue files in `output/queues/`) |
| 4.3 Prompt documentation | [`PROMPTS.md`](PROMPTS.md) |
| 4.4 Architecture write-up | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| n8n run with the real model | `output/n8n_records.json`, `docs/n8n_execution_*.png` |
| Edge cases (injection, multi-intent, $3k error) | `output/edge_cases/` |
| What I changed after the first real run | [`BUILD_LOG.md`](BUILD_LOG.md) |
| Evaluation vs hand labels | `eval/golden.json` → `python eval/evaluate.py` → `output/eval_report.md` |

## Quick start (Python, ~2 minutes, no dependencies)

Requires Python 3.10+ and a free Groq key (console.groq.com → API Keys).

```bash
export GROQ_API_KEY=gsk_...            # Windows PowerShell: $env:GROQ_API_KEY="gsk_..."

# Option A: event-driven (the "real" trigger)
python intake_server.py                # terminal 1: webhook on :8080
python send_samples.py                 # terminal 2: POSTs the 5 samples

# Option B: batch (same pipeline, no server)
python run_batch.py

python eval/evaluate.py                # score against the hand-labelled golden set
python -m unittest discover -s tests -t .   # 14 tests for routing/escalation/validation (no API needed)
```

**Demo console:** while `intake_server.py` is running, open http://localhost:8080 in a browser. You can send any message (or the samples) and watch each record arrive: category, confidence against the 0.70 threshold, the routing decision, escalation reasons, and the original message with the extracted values highlighted. It's a single file (`ui/index.html`) served by the same stdlib server, with no extra dependencies. Use a fresh `OUTPUT_DIR` for a clean board.

Other providers: `LLM_PROVIDER=openai|gemini|ollama` (see `.env.example`). Edge cases: `python run_batch.py data/extra_edge_cases.json`.

## n8n version

1. Import `n8n/arcvault_triage_workflow.json` (n8n ≥ 1.30, self-hosted or cloud).
2. Create a **Header Auth** credential named `LLM API Key (Bearer)`: name `Authorization`, value `Bearer <GROQ_API_KEY>`. Attach it to the two "LLM" nodes.
3. In the **Config & Normalize** node, set the two `webhook.site` URLs (team queue and escalation queue).
4. Activate the workflow, then run `python send_samples.py --url http://localhost:5678/webhook/arcvault-intake --delay 15`. The delay stays under the free-tier rate limit, because the n8n HTTP node retries only 3 times.

The workflow is generated from the shared prompts by `python n8n/build_workflow.py`, so the prompts never diverge from the Python version.

## Layout

```
triage/prompts.py    the two LLM prompts + repair prompt (versioned)
triage/llm.py        OpenAI-compatible client: JSON mode, temperature 0, retry/backoff
triage/pipeline.py   classify → route → enrich → assemble; schema validation + one repair retry
triage/routing.py    queue map, escalation rules, billing-exposure calc, identifier grounding
triage/sinks.py      jsonl log, per-queue files, json/csv snapshot, optional webhook
intake_server.py     Step 1 trigger: POST /intake (202 + background worker)
tests/               unit tests + a mock LLM server used only to test plumbing
eval/                golden labels + scorer
n8n/                 workflow export + generator
```

## Output record (one per message)

```jsonc
{
  "record_id": "AV-1B8735F408", "source": "Support Portal", "received_at": "...",
  "category": "Billing Issue", "priority": "Medium", "confidence": 0.95,
  "classification_rationale": "...", "runner_up_category": null,
  "core_issue": "...",
  "identifiers": { "invoice_numbers": ["#8821"], "amounts": ["$1,240", "$980"], ... },
  "urgency_signal": { "level": "high", "evidence": ["..."] }, "impact_scope": "organization",
  "intended_queue": "Billing", "destination_queue": "Billing",
  "escalation_flag": false, "escalation_reasons": [],
  "summary": "2–3 sentences for the receiving team, ending with a concrete first action.",
  "meta": { "model": "...", "prompt_version": "...", "latency_ms": 0, "errors": [], "dropped_ungrounded_identifiers": [] }
}
```

The example above shows the shape only. Real values are in `output/records.json`.
