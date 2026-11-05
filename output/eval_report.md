# Evaluation vs golden labels

Model: openai/gpt-oss-120b

| id | category | priority | queue | escalation | entities |
|---|---|---|---|---|---|
| sample-1 | ✅ Bug Report | ✅ High | ✅ Engineering | ✅ False | ✅ 403, arcvault.io/user/jsmith |
| sample-2 | ✅ Feature Request | ✅ Low | ✅ Product | ✅ False | ✅ - |
| sample-3 | ✅ Billing Issue | ✅ High | ✅ Billing | ✅ False | ✅ #8821, $1,240, $980 |
| sample-4 | ✅ Technical Question | ✅ Low | ✅ Technical Support | ✅ False | ✅ Okta |
| sample-5 | ✅ Incident/Outage | ✅ High | ✅ Engineering On-Call | ✅ True | ✅ 2pm EST |

**category: 5/5  priority: 5/5  queue: 5/5  escalation: 5/5  entities: 5/5**
