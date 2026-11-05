"""Generates n8n/arcvault_triage_workflow.json from the shared prompts in triage/prompts.py,
so the Python pipeline and the n8n workflow can never drift apart.

    python n8n/build_workflow.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from triage import prompts as P  # noqa: E402

CONFIG_JS = r"""
// ===== EDIT THESE =====
const config = {
  llm_base_url: 'https://api.groq.com/openai/v1',
  llm_model: 'openai/gpt-oss-120b',
  team_webhook_url: 'https://webhook.site/REPLACE-ME-team-queues',
  escalation_webhook_url: 'https://webhook.site/REPLACE-ME-escalation',
  confidence_threshold: 0.70,
  billing_escalation_usd: 500,
};
// ======================
const body = $json.body || $json;
const message = String(body.message || '').trim();
if (!message) throw new Error('Payload must include a non-empty "message"');
const source = String(body.source || 'unknown');
let hex;
try {  // same ID scheme as the Python pipeline (needs NODE_FUNCTION_ALLOW_BUILTIN=crypto on self-hosted n8n)
  hex = require('crypto').createHash('sha256').update(`${source}|${message}`).digest('hex');
} catch (e) {  // fallback: FNV-1a, still deterministic for idempotency
  let h = 0x811c9dc5; for (const ch of `${source}|${message}`) { h ^= ch.charCodeAt(0); h = Math.imul(h, 0x01000193) >>> 0; }
  hex = h.toString(16).padStart(10, '0');
}
const record_id = 'AV-' + hex.slice(0, 10).toUpperCase();
return { json: { config, message, source, external_id: body.id || null, record_id,
                 received_at: new Date().toISOString() } };
"""

BUILD_CLASSIFY_JS = r"""
const SYSTEM = __CLASSIFY_SYSTEM__;
const USER = __CLASSIFY_USER__;
const c = $json;
const user = USER.replace('{source}', c.source).replace('{message}', c.message);
return { json: { ...c, llm_request: {
  model: c.config.llm_model, temperature: 0, response_format: { type: 'json_object' },
  messages: [{ role: 'system', content: SYSTEM }, { role: 'user', content: user }] } } };
"""

PARSE_CLASSIFY_JS = r"""
const CATEGORIES = __CATEGORIES__;
const PRIORITIES = __PRIORITIES__;
const ctx = $('Build Classify Prompt').item.json;
const squash = s => String(s).toLowerCase().replace(/[^a-z0-9]/g, '');
let classification = null, error = null;
try {
  const raw = $json.choices?.[0]?.message?.content;
  if (!raw) throw new Error('LLM call failed: ' + JSON.stringify($json.error || $json).slice(0, 200));
  const d = JSON.parse(raw.slice(raw.indexOf('{'), raw.lastIndexOf('}') + 1));
  const category = CATEGORIES.find(c => squash(c) === squash(d.category));
  if (!category) throw new Error('invalid category ' + d.category);
  const p = String(d.priority || '');
  const priority = PRIORITIES.find(x => x.toLowerCase() === p.toLowerCase());
  if (!priority) throw new Error('invalid priority ' + d.priority);
  let confidence = Number(d.confidence);
  if (confidence > 1 && confidence <= 100) confidence = confidence / 100;
  if (!(confidence >= 0 && confidence <= 1)) throw new Error('invalid confidence ' + d.confidence);
  classification = { category, priority, confidence: Math.round(confidence * 100) / 100,
    runner_up_category: CATEGORIES.includes(d.runner_up_category) && d.runner_up_category !== category ? d.runner_up_category : null,
    rationale: String(d.rationale || '') };
} catch (e) { error = 'classification: ' + e.message; }
const { llm_request, ...rest } = ctx;
return { json: { ...rest, classification, errors: error ? [error] : [] } };
"""

ROUTE_JS = r"""
// Deterministic routing + escalation. Mirrors triage/routing.py exactly.
const QUEUE_MAP = { 'Bug Report': 'Engineering', 'Incident/Outage': 'Engineering On-Call',
  'Feature Request': 'Product', 'Billing Issue': 'Billing', 'Technical Question': 'Technical Support' };
const PATTERNS = {
  outage_keyword: /\boutage\b/,
  service_down: /\b(is|are|went|been|goes)\s+down\b|\bdown for (all|everyone|every)\b/,
  multi_user_impact: /\b(multiple|several|many|all( of our)?)\s+(users|customers|accounts|people)\b/,
  security_signal: /\b(breach|hacked|compromised|data leak|unauthori[sz]ed access)\b/,
  legal_threat: /\b(lawyer|legal action|lawsuit|chargeback)\b/,
};
const c = $json, cfg = c.config, cls = c.classification;
const reasons = [];
let intended = null;
if (!cls) {
  reasons.push('classification_failed');
} else {
  intended = QUEUE_MAP[cls.category];
  if (cls.confidence < cfg.confidence_threshold)
    reasons.push(`low_confidence (${cls.confidence.toFixed(2)} < ${cfg.confidence_threshold.toFixed(2)})`);
  if (cls.category === 'Incident/Outage') reasons.push('category_incident_outage');
  const lower = c.message.toLowerCase();
  for (const [name, re] of Object.entries(PATTERNS)) if (re.test(lower)) reasons.push('keyword:' + name);
  if (cls.category === 'Billing Issue') {
    const amounts = [...c.message.matchAll(/\$\s?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)/g)]
      .map(m => parseFloat(m[1].replace(/,/g, '')));
    if (amounts.length) {
      const exposure = amounts.length >= 2 ? Math.max(...amounts) - Math.min(...amounts) : amounts[0];
      if (exposure > cfg.billing_escalation_usd)
        reasons.push(`billing_error_over_$${cfg.billing_escalation_usd} ($${exposure.toFixed(2)})`);
    }
  }
}
const escalated = reasons.length > 0;
return { json: { ...c, routing: { intended_queue: intended,
  destination_queue: escalated ? 'Human Escalation' : intended, escalated, escalation_reasons: reasons } } };
"""

BUILD_ENRICH_JS = r"""
const SYSTEM = __ENRICH_SYSTEM__;
const USER = __ENRICH_USER__;
const c = $json, cls = c.classification;
const user = USER.replace('{source}', c.source).replace('{message}', c.message)
  .replace('{category}', cls ? cls.category : 'Unknown')
  .replace('{queue}', c.routing.intended_queue || c.routing.destination_queue);
return { json: { ...c, llm_request: {
  model: c.config.llm_model, temperature: 0, response_format: { type: 'json_object' },
  messages: [{ role: 'system', content: SYSTEM }, { role: 'user', content: user }] } } };
"""

ASSEMBLE_JS = r"""
const KEYS = ['account_ids', 'invoice_numbers', 'error_codes', 'amounts', 'products_or_features', 'time_references'];
const LEVELS = __URGENCY__;
const c = $('Build Enrich Prompt').item.json;
const cls = c.classification, routing = { ...c.routing, escalation_reasons: [...c.routing.escalation_reasons] };
const errors = [...c.errors];
const norm = s => String(s).toLowerCase().replace(/’/g, "'").replace(/\s+/g, ' ').trim();
let e = null; const dropped = [];
try {
  const raw = $json.choices?.[0]?.message?.content;
  if (!raw) throw new Error('LLM call failed: ' + JSON.stringify($json.error || $json).slice(0, 200));
  const d = JSON.parse(raw.slice(raw.indexOf('{'), raw.lastIndexOf('}') + 1));
  if (!d.core_issue || !d.summary) throw new Error('core_issue and summary are required');
  const level = String(d.urgency_signal?.level || '').toLowerCase();
  if (!LEVELS.includes(level)) throw new Error('invalid urgency level ' + level);
  const identifiers = {};
  for (const k of KEYS) {
    identifiers[k] = [];
    for (const v of [].concat(d.identifiers?.[k] || [])) {
      if (k === 'products_or_features') { if (typeof v === 'string' && v.trim() && !identifiers[k].includes(v)) identifiers[k].push(v); continue; }
      // Grounding guard: keep only identifiers that literally appear in the message.
      if (typeof v === 'string' && v.trim() && norm(c.message).includes(norm(v))) {
        if (!identifiers[k].includes(v)) identifiers[k].push(v);
      } else dropped.push(`${k}:${v}`);
    }
  }
  e = { core_issue: d.core_issue, identifiers, summary: d.summary,
        urgency_signal: { level, evidence: (d.urgency_signal?.evidence || []).filter(x => norm(c.message).includes(norm(String(x).replace(/^[\s"']+|[\s"']+$/g, '')))).slice(0, 5) },
        impact_scope: ['single_user', 'multiple_users', 'organization', 'unknown'].includes(d.impact_scope) ? d.impact_scope : 'unknown' };
} catch (err) {
  errors.push('enrichment: ' + err.message);
  routing.escalated = true; routing.destination_queue = 'Human Escalation';
  routing.escalation_reasons.push('enrichment_failed');
}
return { json: {
  record_id: c.record_id, external_id: c.external_id, source: c.source,
  received_at: c.received_at, processed_at: new Date().toISOString(), raw_message: c.message,
  category: cls?.category ?? null, priority: cls?.priority ?? null, confidence: cls?.confidence ?? null,
  classification_rationale: cls?.rationale ?? null, runner_up_category: cls?.runner_up_category ?? null,
  core_issue: e?.core_issue ?? null, identifiers: e?.identifiers ?? {}, urgency_signal: e?.urgency_signal ?? null,
  impact_scope: e?.impact_scope ?? null,
  intended_queue: routing.intended_queue, destination_queue: routing.destination_queue,
  escalation_flag: routing.escalated, escalation_reasons: routing.escalation_reasons,
  summary: e?.summary ?? null,
  meta: { orchestrator: 'n8n', model: c.config.llm_model, prompt_version: __PROMPT_VERSION__,
          errors, dropped_ungrounded_identifiers: dropped },
} };
"""


def js(code: str) -> str:
    subs = {
        "__CLASSIFY_SYSTEM__": json.dumps(P.CLASSIFY_SYSTEM),
        "__CLASSIFY_USER__": json.dumps(P.CLASSIFY_USER),
        "__ENRICH_SYSTEM__": json.dumps(P.ENRICH_SYSTEM),
        "__ENRICH_USER__": json.dumps(P.ENRICH_USER),
        "__CATEGORIES__": json.dumps(P.CATEGORIES),
        "__PRIORITIES__": json.dumps(P.PRIORITIES),
        "__URGENCY__": json.dumps(P.URGENCY_LEVELS),
        "__PROMPT_VERSION__": json.dumps(P.PROMPT_VERSION),
    }
    for k, v in subs.items():
        code = code.replace(k, v)
    return code.strip()


CRED = {"httpHeaderAuth": {"id": "arcvaultLlmKey", "name": "LLM API Key (Bearer)"}}


def code_node(name, code, x, y, note=None):
    n = {"parameters": {"mode": "runOnceForEachItem", "jsCode": js(code)}, "name": name,
         "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [x, y], "id": name.lower().replace(" ", "-")}
    if note:
        n["notes"], n["notesInFlow"] = note, True
    return n


def llm_node(name, x, y, note):
    return {
        "parameters": {
            "method": "POST",
            "url": "={{ $json.config.llm_base_url }}/chat/completions",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify($json.llm_request) }}",
            "options": {"timeout": 30000},
        },
        "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [x, y],
        "id": name.lower().replace(" ", "-").replace(":", "").replace("+", "and"),
        "credentials": CRED, "retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000,
        "onError": "continueRegularOutput", "notes": note, "notesInFlow": True,
    }


def sink_node(name, url_field, x, y):
    return {
        "parameters": {
            "method": "POST",
            "url": "={{ $('Config & Normalize').item.json.config." + url_field + " }}",
            "sendHeaders": True,
            "headerParameters": {"parameters": [{"name": "X-ArcVault-Queue", "value": "={{ $json.destination_queue }}"}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json) }}", "options": {},
        },
        "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [x, y],
        "id": name.lower().replace(" ", "-").replace("→", "to"), "onError": "continueRegularOutput",
        "retryOnFail": True, "maxTries": 3,
    }


nodes = [
    {"parameters": {"httpMethod": "POST", "path": "arcvault-intake", "responseMode": "responseNode", "options": {}},
     "name": "Step 1: Intake Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
     "position": [0, 300], "id": "intake-webhook", "webhookId": "7c1f3a2e-arcv-4ault-9000-intake000001",
     "notes": "POST {source, message, id}", "notesInFlow": True},
    code_node("Config & Normalize", CONFIG_JS, 220, 300, "Edit URLs/model here"),
    code_node("Build Classify Prompt", BUILD_CLASSIFY_JS, 440, 300),
    llm_node("Step 2: LLM Classify", 660, 300, "category / priority / confidence"),
    code_node("Validate Classification", PARSE_CLASSIFY_JS, 880, 300, "schema check; failure -> human"),
    code_node("Steps 4+6: Route & Escalate", ROUTE_JS, 1100, 300, "deterministic rules, no LLM"),
    code_node("Build Enrich Prompt", BUILD_ENRICH_JS, 1320, 300),
    llm_node("Step 3: LLM Enrich + Summary", 1540, 300, "entities / urgency / summary"),
    code_node("Step 5: Assemble Record", ASSEMBLE_JS, 1760, 300, "validate + ground identifiers"),
    {"parameters": {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                                   "conditions": [{"id": "esc", "leftValue": "={{ $json.escalation_flag }}",
                                                   "rightValue": True, "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                                   "combinator": "and"}, "options": {}},
     "name": "Escalated?", "type": "n8n-nodes-base.if", "typeVersion": 2, "position": [1980, 300], "id": "escalated-if"},
    sink_node("Escalation Queue", "escalation_webhook_url", 2200, 180),
    sink_node("Team Queue", "team_webhook_url", 2200, 420),
    {"parameters": {"respondWith": "json", "responseBody": "={{ JSON.stringify($('Step 5: Assemble Record').item.json) }}",
                    "options": {"responseCode": 200}},
     "name": "Respond with Record", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.1,
     "position": [2420, 300], "id": "respond"},
]


def link(*names):
    return {"main": [[{"node": n, "type": "main", "index": 0} for n in names]]}


connections = {
    "Step 1: Intake Webhook": link("Config & Normalize"),
    "Config & Normalize": link("Build Classify Prompt"),
    "Build Classify Prompt": link("Step 2: LLM Classify"),
    "Step 2: LLM Classify": link("Validate Classification"),
    "Validate Classification": link("Steps 4+6: Route & Escalate"),
    "Steps 4+6: Route & Escalate": link("Build Enrich Prompt"),
    "Build Enrich Prompt": link("Step 3: LLM Enrich + Summary"),
    "Step 3: LLM Enrich + Summary": link("Step 5: Assemble Record"),
    "Step 5: Assemble Record": link("Escalated?"),
    "Escalated?": {"main": [[{"node": "Escalation Queue", "type": "main", "index": 0}],
                            [{"node": "Team Queue", "type": "main", "index": 0}]]},
    "Escalation Queue": link("Respond with Record"),
    "Team Queue": link("Respond with Record"),
}

workflow = {"name": "ArcVault Intake & Triage", "nodes": nodes, "connections": connections,
            "settings": {"executionOrder": "v1"}, "active": False, "pinData": {},
            "meta": {"templateCredsSetupCompleted": False}, "tags": []}

out = ROOT / "n8n" / "arcvault_triage_workflow.json"
out.write_text(json.dumps(workflow, indent=2, ensure_ascii=False), encoding="utf-8")
print("wrote", out)
