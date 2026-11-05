"""TEST DOUBLE ONLY: a fake OpenAI-compatible /chat/completions server.

Used to test the pipeline's plumbing (triggers, validation, repair loop, routing,
sinks, n8n wiring) without spending API calls. It uses crude keyword rules. It is NOT
the classifier, and its outputs must never be submitted as model results.

It also misbehaves on purpose so the defensive code paths get exercised:
  - "audit logs" messages get a lowercase category + a 0-100 confidence on the first try
  - "SSO" messages get an INVALID category first (forces the repair round-trip)
  - enrichment for "invoice" messages includes a hallucinated identifier "INV-8821"
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

seen_invalid = set()


def classify(msg: str) -> dict:
    m = msg.lower()
    if "multiple users" in m or "stopped loading" in m or "locked out" in m:
        return {"rationale": "mock", "category": "Incident/Outage", "runner_up_category": "Bug Report", "priority": "High", "confidence": 0.9}
    if "invoice" in m or "charge" in m or "billed" in m:
        return {"rationale": "mock", "category": "Billing Issue", "runner_up_category": None, "priority": "Medium", "confidence": 0.95}
    if "403" in m or "error" in m:
        return {"rationale": "mock", "category": "Bug Report", "runner_up_category": "Incident/Outage", "priority": "High", "confidence": 0.88}
    if "sso" in m:
        if "sso" not in seen_invalid:
            seen_invalid.add("sso")
            return {"rationale": "mock", "category": "Integration Question", "priority": "Low", "confidence": 0.8}
        return {"rationale": "mock", "category": "Technical Question", "runner_up_category": "Feature Request", "priority": "Low", "confidence": 0.82}
    if "audit logs" in m:
        return {"rationale": "mock", "category": "feature request", "runner_up_category": None, "priority": "low", "confidence": 93}
    return {"rationale": "mock", "category": "Technical Question", "runner_up_category": "Bug Report", "priority": "Medium", "confidence": 0.45}


def enrich(msg: str) -> dict:
    import re
    ids = {
        "account_ids": re.findall(r"arcvault\.io/\S+?(?=[.\s]|$)", msg),
        "invoice_numbers": re.findall(r"#\d+", msg) + (["INV-8821"] if "invoice" in msg.lower() else []),
        "error_codes": re.findall(r"\b[45]\d\d\b", msg),
        "amounts": re.findall(r"\$[\d,]+", msg),
        "products_or_features": [w for w in ["dashboard", "audit logs", "Okta", "SSO"] if w.lower() in msg.lower()],
        "time_references": re.findall(r"around \d+(?:am|pm) [A-Z]{3}|last \w+day", msg),
    }
    return {"core_issue": "mock core issue: " + msg[:60], "identifiers": ids,
            "urgency_signal": {"level": "high", "evidence": [msg[:30]]}, "impact_scope": "unknown",
            "summary": "Mock summary sentence one. Mock summary sentence two."}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system = body["messages"][0]["content"]
        user_msgs = [m["content"] for m in body["messages"] if m["role"] == "user"]
        msg = user_msgs[0].split("<message>")[1].split("</message>")[0].strip()
        out = classify(msg) if "triage classifier" in system else enrich(msg)
        resp = {"choices": [{"message": {"role": "assistant", "content": json.dumps(out)}}]}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    print(f"mock LLM on http://localhost:{port}/v1", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
