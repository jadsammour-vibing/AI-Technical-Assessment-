"""Unit tests for the deterministic parts (no LLM needed).  Run: python -m unittest -v"""
import unittest

from triage.pipeline import ValidationError, validate_classification
from triage.routing import billing_exposure, decide_route, ground_evidence, ground_identifiers


def cls(category, confidence=0.9, priority="Medium"):
    return {"category": category, "confidence": confidence, "priority": priority}


class Routing(unittest.TestCase):
    def test_category_to_queue(self):
        cases = {"Bug Report": "Engineering", "Feature Request": "Product",
                 "Billing Issue": "Billing", "Technical Question": "Technical Support"}
        for cat, q in cases.items():
            r = decide_route("plain message", cls(cat))
            self.assertEqual(r["destination_queue"], q)
            self.assertFalse(r["escalated"])

    def test_low_confidence_escalates_but_keeps_intended_queue(self):
        r = decide_route("plain message", cls("Bug Report", 0.69))
        self.assertTrue(r["escalated"])
        self.assertEqual(r["destination_queue"], "Human Escalation")
        self.assertEqual(r["intended_queue"], "Engineering")

    def test_threshold_boundary_is_strictly_below_70(self):
        self.assertFalse(decide_route("plain", cls("Bug Report", 0.70))["escalated"])

    def test_outage_category_always_escalates(self):
        self.assertTrue(decide_route("something odd", cls("Incident/Outage", 0.99))["escalated"])

    def test_keyword_escalates_even_if_model_says_bug(self):
        r = decide_route("Dashboard broken, multiple users affected", cls("Bug Report", 0.95))
        self.assertTrue(r["escalated"])
        self.assertIn("keyword:multi_user_impact", r["escalation_reasons"])

    def test_billing_discrepancy_under_threshold_not_escalated(self):
        msg = "Invoice #8821 shows a charge of $1,240 but our contract rate is $980/month."
        self.assertEqual(billing_exposure(msg), 260.0)
        self.assertFalse(decide_route(msg, cls("Billing Issue"))["escalated"])

    def test_billing_discrepancy_over_threshold_escalates(self):
        msg = "We were billed $4,800 but our plan is $1,800."
        r = decide_route(msg, cls("Billing Issue"))
        self.assertTrue(r["escalated"])

    def test_classification_failure_falls_back_to_human(self):
        r = decide_route("anything", None)
        self.assertEqual(r["destination_queue"], "Human Escalation")
        self.assertIn("classification_failed", r["escalation_reasons"])

    def test_no_false_positive_on_word_download(self):
        self.assertFalse(decide_route("How do I download a report?", cls("Technical Question"))["escalated"])


class Grounding(unittest.TestCase):
    def test_drops_hallucinated_identifier(self):
        clean, dropped = ground_identifiers({"invoice_numbers": ["#8821", "INV-8821"]}, "Invoice #8821 is wrong")
        self.assertEqual(clean["invoice_numbers"], ["#8821"])
        self.assertEqual(dropped, ["invoice_numbers:INV-8821"])


    def test_descriptive_tags_not_grounded(self):
        clean, dropped = ground_identifiers({"products_or_features": ["login"]}, "I tried logging in")
        self.assertEqual(clean["products_or_features"], ["login"])
        self.assertEqual(dropped, [])

    def test_evidence_must_come_from_message(self):
        self.assertEqual(ground_evidence(["Priority: High", "Multiple users affected"],
                                         "Dashboard down. Multiple users affected."), ["Multiple users affected"])


class Validation(unittest.TestCase):
    def test_normalises_case_and_percent_confidence(self):
        v = validate_classification({"category": "incident / outage", "priority": "high", "confidence": 85})
        self.assertEqual((v["category"], v["priority"], v["confidence"]), ("Incident/Outage", "High", 0.85))

    def test_rejects_unknown_category(self):
        with self.assertRaises(ValidationError):
            validate_classification({"category": "Sales", "priority": "Low", "confidence": 0.9})


if __name__ == "__main__":
    unittest.main()
