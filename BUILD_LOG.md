# Build log: what the AI got wrong, and what I changed

I built this with Claude as a pair-programmer, as the brief encourages. This log records every problem found when the system met real conditions, and what I changed in response.

**Before the real run (mock LLM).**
- The plumbing was first tested against a deliberately misbehaving mock LLM (`tests/mock_llm_server.py`).
- The mock returns a lowercase category, a confidence of `93` instead of `0.93`, an invented category ("Integration Question"), and a hallucinated invoice ID (`INV-8821`).
- The validation, repair-retry, normalisation and grounding code handled all four cases.

## Issues found in the first real run (Groq), and the fixes

| # | What went wrong | Root cause | Fix |
|---|---|---|---|
| 1 | Every call failed with `model_not_found`, and all 5 messages fell back to Human Escalation | The AI-suggested default model (Llama 3.3 70B) had been retired from Groq's free tier | Listed the live models and switched to `openai/gpt-oss-120b`, a one-line config change. The fail-safe meant nothing was lost. |
| 2 | Urgency "evidence" contained `"Priority: High"`, text that isn't in the customer's message | I passed the classifier's priority into the enrichment prompt, and the model echoed it back | Removed priority from the enrichment input, added an explicit "customer's words only" rule, and added `ground_evidence()` to drop non-quoted evidence |
| 3 | The outage got urgency "high", not "critical" | Same anchoring on the priority label | Fixed by #2: now "critical", with evidence "Multiple users affected" |
| 4 | The grounding guard dropped the feature tag `login` (the customer wrote "logging in") | The verbatim rule was applied to descriptive tags, not just hard identifiers | Feature tags are now exempt; IDs, codes and amounts are still verified |
| 5 | Samples 3 and 5 hit HTTP 429 (8,000 tokens/minute free-tier limit) | Two parallel workers, plus backoff (1 s, 2 s) shorter than Groq's requested wait (4.4 s) | Honour `Retry-After` / "try again in X s" with jitter; default to 1 worker |
| 6 | Re-sending a failed message was skipped as a "duplicate" | The idempotency check counted failed records | Only successful records count as duplicates; the snapshot keeps the latest attempt; the duplicate check now runs before any LLM call |

## Still open (documented, not hidden)
- **Confidence is overconfident.** It's 0.96–0.97 almost everywhere, even with a named runner-up. The plan is self-consistency or logprob scoring (Phase 2).
- **Multi-intent messages.** Edge case 1 ("thing isn't working + send last month's invoice") routed to Engineering at 0.78 confidence, so the invoice request reaches the wrong team. I chose not to add a rule tuned to my own test case. Phase 2 plans splitting such messages into linked records.
